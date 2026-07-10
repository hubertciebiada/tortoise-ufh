"""DataUpdateCoordinator for the Tortoise-UFH integration.

Every 5 minutes this coordinator:

1. Reads the configured Home Assistant source entities (room temperature,
   humidity, outdoor temperature, per-loop supply/return water probes and valve
   feedback, fast-source state) with a short stale cache so a briefly
   unavailable sensor does not immediately degrade a room.
2. Assembles one :class:`~tortoise_ufh.models.RoomInputs` per room and runs the
   pure-core :class:`~tortoise_ufh.controller.BuildingController` (the black
   box), producing per-room valve/fast-source commands, the under-the-hood
   report and the global safe dew point.
3. Stores a typed payload in :attr:`coordinator.data` for the entity platforms
   and the websocket/panel to consume.
4. For rooms in the ``live`` control state, WRITES the commands to the actuators
   (``number.set_value`` or ``valve.set_valve_position`` per valve entity,
   dispatched by the entity's domain; ``climate.set_hvac_mode`` +
   ``climate.set_temperature`` for the split). An ``off`` or ``shadow`` room
   means: compute and report, but emit no commands.

The global home temperature and per-room offsets are the single source of truth
for setpoints and live in this coordinator; the writable ``number`` entities and
the websocket setters mutate them through :meth:`set_home_temperature` /
:meth:`set_room_offset`, which rebroadcast the cached data immediately.

Units: temperatures degrees Celsius, valve percent 0..100, humidity percent
0..100, control cycle 300 s (5 min).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback, split_entity_id
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .config_flow import CONF_CONTROLLER
from .const import (
    CONF_COOLING_ENABLED,
    CONF_ENTITY_FAST_SOURCE,
    CONF_ENTITY_HUMIDITY,
    CONF_ENTITY_MODE,
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_TEMP_OUTDOOR,
    CONF_ENTITY_TEMP_ROOM,
    CONF_ENTITY_VALVES,
    CONF_FAST_SOURCE_KIND,
    CONF_HOME_SETPOINT,
    CONF_ROOM_NAME,
    CONF_ROOM_OFFSET,
    CONF_ROOM_STATE,
    CONF_ROOM_TUNING,
    CONF_ROOMS,
    DEFAULT_ROOM_STATE,
    DOMAIN,
    ENTITY_STALE_MAX_SECONDS,
    ROOM_STATE_LIVE,
    ROOM_STATE_OFF,
    ROOM_STATES,
    UPDATE_INTERVAL_MINUTES,
    WATCHDOG_RECOVERY_MINUTES,
    WATCHDOG_TIMEOUT_MINUTES,
)
from .core.config import ControllerConfig
from .core.controller import BuildingController
from .core.models import (
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
    RoomOutputs,
    RoomReport,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# --- Module constants -------------------------------------------------------

_DEFAULT_HOME_SETPOINT_C: float = 21.0
"""Fallback global home target temperature [degC] when none is configured."""

_SETPOINT_STORE_VERSION: int = 1
"""Schema version of the private setpoint :class:`Store`."""

_SETPOINT_SAVE_DELAY_S: float = 1.0
"""Debounce delay before a setpoint change is flushed to the Store [s]."""

_RECOMPUTE_DEBOUNCE_S: float = 2.0
"""Cooldown [s] before a setpoint change triggers a full trailing recompute.

A burst of stepper clicks collapses to a single ``async_request_refresh`` so the
control step is re-run once with a consistent ``error_c`` and the write path
(notably the split target temperature) is re-emitted promptly. The default
coordinator debouncer cooldown (10 s) is too slow for this UX.
"""

_MIN_DT_SECONDS: float = 1.0
"""Lower clamp for the measured control step interval [s]."""

_MAX_DT_SECONDS: float = 900.0
"""Upper clamp for the measured control step interval [s]."""

_STORE_KEY_MODE: str = "mode"
"""Key of the persisted global mode in the private setpoint Store."""

# -- Input plausibility (fixed constants, deliberately NOT config knobs) ------
# Tailored to the owner's house: 1-wire/Zigbee room sensors in a high-mass UFH
# building. A real room cannot leave -10..50 degC, and its air temperature
# cannot move more than ~4 K between two 5-minute cycles — a bigger jump is a
# sensor fault (e.g. the DS18B20 85 degC power-on-reset), not physics.

_TEMP_PLAUSIBLE_MIN_C: float = -10.0
"""Lowest plausible room-air temperature [degC]; below -> reject the sample."""

_TEMP_PLAUSIBLE_MAX_C: float = 50.0
"""Highest plausible room-air temperature [degC]; above -> reject the sample."""

_TEMP_MAX_JUMP_K: float = 4.0
"""Max plausible room-temperature change between control cycles [K].

A sample jumping further than this from the last accepted value is rejected
(treated as a missing reading); two consecutive mutually consistent samples
accept the new level, so a real fast change is adopted within two cycles.
"""

_ROOM_TEMP_MAX_AGE_S: float = 45.0 * 60.0
"""Max age of a room-temperature state before it is treated as unavailable [s].

Guards against a present-but-frozen sensor (dead battery, stuck bridge): a
reading not re-reported for this long can no longer be trusted to control heat,
let alone chilled water.
"""

_HUMIDITY_MAX_AGE_S: float = 60.0 * 60.0
"""Max age of a humidity state before it is treated as unavailable [s].

The most dangerous stale input: a frozen winter RH makes BOTH condensation
defences (global safe dew point and local S2 throttle) agree to pass water
below the real dew point. Stale RH -> None -> the core cools nothing blindly.
"""

_VALVE_MISMATCH_TOLERANCE_PCT: float = 10.0
"""Command-vs-feedback divergence beyond which a valve counts as mismatched [%]."""

_VALVE_MISMATCH_CYCLES: int = 3
"""Consecutive mismatched cycles before the ``valve_mismatch`` flag is raised."""

_FAST_REASSERT_SECONDS: float = 45.0 * 60.0
"""Age after which an unchanged fast-source command is re-written anyway [s].

The splits are local (ESPHome), so this is hygiene, not an API budget: an
unchanged (hvac_mode, target) pair is normally NOT re-sent every cycle (no IR
beeps / stomping on manual tweaks), but a periodic re-assert self-heals the
hardware after a missed write or a manual override — the machine stays the
owner (S3, 2026-07-09).
"""

# Per-room config key for an optional heat-pump-status source entity. Defined
# here (not in const.py) because this file is the only adapter surface allowed
# to change; when present its on/off/unavailable state drives the core's
# integrator freeze during DHW / defrost. Absent -> tri-state None (feature off).
CONF_ENTITY_HP_ACTIVE: str = "entity_hp_active"

_HP_INACTIVE_STATES: frozenset[str] = frozenset(
    {"off", "false", "idle", "standby", "0"}
)
"""HP-status states meaning the pump is NOT heating the UFH supply (freeze)."""

_UNAVAILABLE_STATES: frozenset[str] = frozenset({"unavailable", "unknown", "none", ""})
"""Home Assistant state strings treated as "no reading"."""

_ALGORITHM_STATES: frozenset[str] = frozenset({"running", "stale", "error"})
"""Permitted :attr:`CoordinatorData.algorithm_status` values."""

_WATCHDOG_STATES: frozenset[str] = frozenset({"ok", "stale"})
"""Permitted :attr:`CoordinatorData.watchdog_state` values."""

# FastSourceMode -> Home Assistant climate HVAC mode string.
_HVAC_MODE_BY_FAST_SOURCE: dict[FastSourceMode, str] = {
    FastSourceMode.HEATING: "heat",
    FastSourceMode.COOLING: "cool",
    FastSourceMode.OFF: "off",
}

_VALVE_DOMAIN: str = "valve"
"""Home Assistant domain of position-capable ``valve`` actuator entities.

A ``valve`` reports its position in the ``current_position`` attribute (0..100)
and is driven via ``valve.set_valve_position`` (integer ``position``); a
``number`` valve reports the position as its numeric state and is driven via
``number.set_value`` (float ``value``). Everything else in the read/write path
is domain-agnostic.
"""


def _reload_signature(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return the reload-relevant subset of ``entry.options``.

    The per-room control-state map (:data:`CONF_ROOM_STATE`) is applied in memory
    by :meth:`TortoiseUfhCoordinator.set_room_state` without a reload (a reload
    would reset the PID integrator), so it is excluded here: any change to the
    remaining keys — controller tuning, per-room tuning, … — still requires a
    full coordinator rebuild.

    Args:
        options: A config entry's options mapping.

    Returns:
        A plain dict of every option key except :data:`CONF_ROOM_STATE`.
    """
    return {key: value for key, value in options.items() if key != CONF_ROOM_STATE}


# ---------------------------------------------------------------------------
# Typed payload dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomRuntime:
    """Per-room runtime payload: the core result plus HA-layer context.

    Attributes:
        outputs: The room's :class:`~tortoise_ufh.models.RoomOutputs` (final
            valve percent, fast-source command and report) from the core
            controller.
        report: The under-the-hood :class:`~tortoise_ufh.models.RoomReport`.
            Always the same object as ``outputs.report`` (surfaced for
            convenience to the panel/websocket).
        live_control_enabled: Whether this room is in live control (``True``) or
            shadow / dry-run (``False``, commands computed but not written).
        setpoint_c: The effective target temperature [degC] used this cycle,
            equal to the global home temperature plus the room's offset.

    Raises:
        ValueError: If ``report`` is not ``outputs.report`` or ``setpoint_c`` is
            not finite.
    """

    outputs: RoomOutputs
    report: RoomReport
    live_control_enabled: bool
    setpoint_c: float

    def __post_init__(self) -> None:
        """Validate internal consistency of the runtime payload."""
        if self.report is not self.outputs.report:
            msg = "report must be the same object as outputs.report"
            raise ValueError(msg)
        if not math.isfinite(self.setpoint_c):
            msg = f"setpoint_c must be finite, got {self.setpoint_c}"
            raise ValueError(msg)


@dataclass(frozen=True)
class CoordinatorData:
    """All data produced by the coordinator in one 5-minute update cycle.

    Attributes:
        rooms: Per-room runtime payloads keyed by room name.
        global_safe_dew_point_c: ``max_over_cooled(T_dew) + 2 K`` [degC], or
            ``None`` when no room is eligible; the value the owner pipes to the
            heat pump as the cooling-supply lower limit.
        algorithm_status: ``"running"``, ``"stale"`` (no fresh room data) or
            ``"error"`` (the control step raised).
        watchdog_state: ``"ok"`` while fresh data has arrived within the
            watchdog timeout, otherwise ``"stale"``.
        last_update_timestamp: ISO-8601 UTC timestamp of this cycle, or ``None``.
        mode: The active global :class:`~tortoise_ufh.models.Mode` value string
            (``"heating"`` / ``"transitional"`` / ``"cooling"`` / ``"off"``).
        sensor_lost_rooms: Number of rooms currently degraded with the
            ``sensor_lost`` flag (building-level staleness counter,
            safety-F13 2026-07-09; surfaced via websocket, no new entity).

    Raises:
        ValueError: If ``algorithm_status``, ``watchdog_state`` or ``mode`` is
            not a recognised value, or ``sensor_lost_rooms`` is negative.
    """

    rooms: dict[str, RoomRuntime]
    global_safe_dew_point_c: float | None
    algorithm_status: str
    watchdog_state: str
    last_update_timestamp: str | None
    mode: str
    sensor_lost_rooms: int = 0

    def __post_init__(self) -> None:
        """Validate the enumerated status/mode fields."""
        if self.sensor_lost_rooms < 0:
            msg = f"sensor_lost_rooms must be >= 0, got {self.sensor_lost_rooms}"
            raise ValueError(msg)
        if self.algorithm_status not in _ALGORITHM_STATES:
            msg = (
                "algorithm_status must be one of "
                f"{sorted(_ALGORITHM_STATES)}, got {self.algorithm_status!r}"
            )
            raise ValueError(msg)
        if self.watchdog_state not in _WATCHDOG_STATES:
            msg = (
                "watchdog_state must be one of "
                f"{sorted(_WATCHDOG_STATES)}, got {self.watchdog_state!r}"
            )
            raise ValueError(msg)
        if self.mode not in {m.value for m in Mode}:
            msg = f"mode must be a valid Mode value, got {self.mode!r}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class TortoiseUfhCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Polls HA entities every 5 minutes and runs the core controller.

    The coordinator owns exactly one core
    :class:`~tortoise_ufh.controller.BuildingController` (one room controller per
    configured room) and the authoritative setpoint state (global home
    temperature + per-room offsets). It reads sources, runs the black box,
    stores :class:`CoordinatorData`, and — for rooms in the ``live`` control
    state — writes the commands to the actuators.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Build the controller and initialise setpoint/flag state from config.

        Args:
            hass: The Home Assistant instance.
            entry: The config entry holding room configs (``entry.data``) and the
                per-room control-state map (``entry.options``).
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._cycle_seconds: float = UPDATE_INTERVAL_MINUTES * 60.0
        # Monotonic timestamp of the previous control step; used to feed the core
        # the REAL elapsed time (clamped) rather than the nominal cycle length,
        # so a debounced off-cycle recompute does not advance the integrator,
        # trend and dwell timers by a full 5 minutes.
        self._last_step_monotonic: float | None = None

        # Per-room configuration (list of dicts, one per room).
        raw_rooms: Any = entry.data.get(CONF_ROOMS, [])
        self._room_configs: list[dict[str, Any]] = list(raw_rooms) if raw_rooms else []
        self._room_names: list[str] = [
            str(room_cfg[CONF_ROOM_NAME]) for room_cfg in self._room_configs
        ]

        # Global mode input entity (select / input_select). May be unset.
        self._mode_entity: str = str(entry.data.get(CONF_ENTITY_MODE, "") or "")
        self._mode: Mode = Mode.HEATING

        # Single source of truth for setpoints.
        self._home_temperature_c: float = float(
            entry.data.get(CONF_HOME_SETPOINT, _DEFAULT_HOME_SETPOINT_C)
        )
        self._room_offsets: dict[str, float] = {}
        for room_cfg, name in zip(self._room_configs, self._room_names, strict=True):
            self._room_offsets[name] = float(room_cfg.get(CONF_ROOM_OFFSET, 0.0))

        # Setpoints are runtime state, not configuration, so they are persisted
        # to a private Store rather than entry.data (writing entry.data would
        # fire the update listener and reload the whole integration). Restored
        # once on the first refresh via _ensure_setpoints_loaded.
        self._setpoint_store: Store[dict[str, Any]] = Store(
            hass,
            _SETPOINT_STORE_VERSION,
            f"{DOMAIN}.setpoints.{entry.entry_id}",
        )
        self._setpoints_loaded: bool = False

        # Canonical per-room control state (off / shadow / live). Seeded for
        # every configured room from the persisted state map; an unknown or
        # invalid persisted value falls back to the safe default (shadow).
        state_map: Any = entry.options.get(CONF_ROOM_STATE, {})
        self._room_states: dict[str, str] = {}
        for name in self._room_names:
            raw_state = str(state_map.get(name, DEFAULT_ROOM_STATE))
            self._room_states[name] = (
                raw_state if raw_state in ROOM_STATES else DEFAULT_ROOM_STATE
            )

        # Snapshot of the reload-relevant options (everything except the
        # per-room state map) captured at build time. Used by
        # :meth:`options_require_reload` so a state-only option change made
        # through :meth:`set_room_state` does not trigger a whole-entry reload.
        self._reload_signature_snapshot: dict[str, Any] = _reload_signature(
            entry.options
        )

        # One core controller (one room controller per room). Apply the
        # persisted tuning knobs (entry.options overrides entry.data) so the
        # config/options flow is not a no-op; fall back to library defaults on
        # any invalid persisted value.
        merged_controller: dict[str, Any] = {
            **entry.data.get(CONF_CONTROLLER, {}),
            **entry.options.get(CONF_CONTROLLER, {}),
        }
        try:
            controller_config = ControllerConfig(**merged_controller)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Invalid persisted controller config; using defaults",
                exc_info=True,
            )
            controller_config = ControllerConfig()
        # Sparse per-room overrides layered over the global tuning: a room's
        # ControllerConfig is built from {**global, **room_override}. An invalid
        # override degrades that room to the global config with a warning.
        room_tuning: Any = entry.options.get(CONF_ROOM_TUNING, {})
        self._controller_configs: dict[str, ControllerConfig] = {}
        for name in self._room_names:
            override: dict[str, Any] = {}
            if isinstance(room_tuning, Mapping):
                raw_override = room_tuning.get(name, {})
                if isinstance(raw_override, Mapping):
                    override = dict(raw_override)
            if not override:
                self._controller_configs[name] = controller_config
                continue
            try:
                self._controller_configs[name] = ControllerConfig(
                    **{**merged_controller, **override}
                )
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Invalid per-room tuning override for %s; using global config",
                    name,
                    exc_info=True,
                )
                self._controller_configs[name] = controller_config
        self._building = (
            BuildingController(self._controller_configs)
            if self._controller_configs
            else None
        )

        # Fallback cache for entity reads: entity_id -> (value, timestamp).
        self._entity_cache: dict[str, tuple[float, datetime]] = {}

        # Room-temperature plausibility state (C3): per-entity last accepted
        # value and the pending candidate awaiting a second consistent sample.
        self._temp_last_accepted: dict[str, float] = {}
        self._temp_pending: dict[str, float] = {}

        # Per-room last-fresh-data timestamp (S6): feeds the core S5 watchdog
        # via RoomInputs.last_update_age_minutes. Seeded on first sight so a
        # room that never delivers data ages from integration startup.
        self._room_last_fresh: dict[str, datetime] = {}

        # Consecutive command-vs-feedback divergence cycles per room (S8).
        self._valve_mismatch_cycles: dict[str, int] = {}

        # Last value written to each valve entity (for the write threshold).
        self._last_written_valve: dict[str, float] = {}

        # Last fast-source command written per climate entity (S3):
        # entity_id -> (hvac_mode, target_temp_c or None, monotonic timestamp).
        # An unchanged command younger than _FAST_REASSERT_SECONDS is skipped.
        self._last_written_fast: dict[str, tuple[str, float | None, float]] = {}

        # Watchdog heartbeat: last time at least one room had fresh data.
        self._last_heartbeat: datetime = datetime.now(UTC)
        # Start of the current uninterrupted run of fresh data, or None while
        # data is stale. Used to enforce the sustained-recovery window before
        # the watchdog clears back to "ok".
        self._fresh_since: datetime | None = None
        # Whether the watchdog has actually entered the >timeout fault. The
        # sustained-recovery window is only enforced when clearing this fault,
        # so a clean start/reload reports "ok" immediately.
        self._watchdog_faulted: bool = False

        # Trailing debouncer that turns a burst of setpoint edits into a single
        # off-cycle recompute + re-write (explicit short cooldown; see
        # _RECOMPUTE_DEBOUNCE_S). Cancelled on unload via async_cancel_recompute.
        self._recompute_debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=_RECOMPUTE_DEBOUNCE_S,
            immediate=False,
            function=self.async_request_refresh,
        )

    # -- Setpoint / flag accessors (single source of truth) -----------------

    def get_home_temperature(self) -> float:
        """Return the global home target temperature [degC]."""
        return self._home_temperature_c

    @callback
    def set_home_temperature(self, value: float) -> None:
        """Set the global home target temperature and rebroadcast [degC].

        Args:
            value: New home target temperature [degC].
        """
        self._home_temperature_c = float(value)
        self._persist_setpoints()
        self._rebroadcast_setpoints()
        self._schedule_recompute()

    def get_room_offset(self, room_name: str) -> float:
        """Return a room's setpoint offset from the home temperature [K].

        Args:
            room_name: The room name.

        Returns:
            The offset [K] (0.0 for unknown rooms).
        """
        return self._room_offsets.get(room_name, 0.0)

    @callback
    def set_room_offset(self, room_name: str, offset: float) -> None:
        """Set a room's setpoint offset and rebroadcast [K].

        Args:
            room_name: The room name.
            offset: New offset from the home temperature [K].
        """
        if room_name in self._room_offsets:
            self._room_offsets[room_name] = float(offset)
            self._persist_setpoints()
            self._rebroadcast_setpoints()
            self._schedule_recompute()

    def get_room_setpoint(self, room_name: str) -> float:
        """Return a room's effective target temperature [degC].

        Args:
            room_name: The room name.

        Returns:
            ``home_temperature + room_offset`` [degC].
        """
        return self._home_temperature_c + self.get_room_offset(room_name)

    def get_mode(self) -> Mode:
        """Return the current global operating :class:`Mode`."""
        return self._mode

    @callback
    def set_mode(self, mode: Mode) -> None:
        """Override the global operating mode, persist it and rebroadcast.

        The mode is persisted to the private setpoint Store (S9) so a restart
        in July does not silently fall back to heating logic. Like
        :meth:`set_home_temperature`, the change schedules a debounced
        recompute (fix 2026-07-10) so a panel/service mode change takes effect
        within seconds instead of waiting out the 5-min cycle — an immediate
        recompute is safe because the fast-source direction machine forces any
        HEATING<->COOLING reversal through OFF with the full dwell anyway.

        Args:
            mode: New global :class:`~tortoise_ufh.models.Mode`.
        """
        self._mode = mode
        self._persist_setpoints()
        if self.data is not None:
            self.async_set_updated_data(
                CoordinatorData(
                    rooms=self.data.rooms,
                    global_safe_dew_point_c=self.data.global_safe_dew_point_c,
                    algorithm_status=self.data.algorithm_status,
                    watchdog_state=self.data.watchdog_state,
                    last_update_timestamp=self.data.last_update_timestamp,
                    mode=mode.value,
                    sensor_lost_rooms=self.data.sensor_lost_rooms,
                )
            )
        self._schedule_recompute()

    def get_room_state(self, room_name: str) -> str:
        """Return a room's control state (``off`` / ``shadow`` / ``live``).

        Args:
            room_name: The room name.

        Returns:
            The room's state string, or :data:`DEFAULT_ROOM_STATE` for a room the
            coordinator has no persisted state for.
        """
        return self._room_states.get(room_name, DEFAULT_ROOM_STATE)

    @callback
    def set_room_state(self, room_name: str, state: str) -> None:
        """Set a room's control state and rebroadcast immediately.

        Updates the in-memory state first (so the running control loop and the
        panel see the change at once), then persists the new state map to
        ``entry.options``. Because :meth:`options_require_reload` reports a
        state-only change as *not* requiring a reload, this write does not tear
        down and rebuild the coordinator (which would reset the PID integrator);
        the persisted value simply survives a restart.

        Args:
            room_name: The room name (ignored when unknown).
            state: One of :data:`ROOM_STATES`.

        Raises:
            ValueError: If ``state`` is not a recognised room state.
        """
        if state not in ROOM_STATES:
            msg = f"state must be one of {ROOM_STATES}, got {state!r}"
            raise ValueError(msg)
        if room_name not in self._room_states:
            return
        previous = self._room_states[room_name]
        self._room_states[room_name] = state
        if previous == ROOM_STATE_LIVE and state != ROOM_STATE_LIVE:
            # Farewell command (C5): leaving live orphans the physical
            # actuators — park them safely once before releasing ownership.
            self._schedule_farewell(room_name)
        state_map: dict[str, Any] = dict(
            self.config_entry.options.get(CONF_ROOM_STATE, {})
        )
        state_map[room_name] = state
        self._persist_options({CONF_ROOM_STATE: state_map})
        if self.data is not None and room_name in self.data.rooms:
            old = self.data.rooms[room_name]
            new_rooms = dict(self.data.rooms)
            new_rooms[room_name] = RoomRuntime(
                outputs=old.outputs,
                report=old.outputs.report,
                live_control_enabled=state == ROOM_STATE_LIVE,
                setpoint_c=old.setpoint_c,
            )
            self.async_set_updated_data(
                CoordinatorData(
                    rooms=new_rooms,
                    global_safe_dew_point_c=self.data.global_safe_dew_point_c,
                    algorithm_status=self.data.algorithm_status,
                    watchdog_state=self.data.watchdog_state,
                    last_update_timestamp=self.data.last_update_timestamp,
                    mode=self.data.mode,
                    sensor_lost_rooms=self.data.sensor_lost_rooms,
                )
            )

    def options_require_reload(self, new_options: Mapping[str, Any]) -> bool:
        """Return whether an options change needs a full config-entry reload.

        A change to any reload-relevant option key (controller tuning, per-room
        tuning, …) requires rebuilding the coordinator. A change limited to the
        per-room control-state map (:data:`CONF_ROOM_STATE`) does not — *when* it
        was applied through :meth:`set_room_state`, which already updated the
        in-memory state and rebroadcast. A state map that differs from the
        in-memory state (e.g. one written directly by the options flow) still
        forces a reload so the coordinator adopts it.

        Args:
            new_options: The config entry's freshly persisted options mapping.

        Returns:
            ``True`` when the entry must be reloaded, ``False`` otherwise.
        """
        if _reload_signature(new_options) != self._reload_signature_snapshot:
            return True
        new_states = new_options.get(CONF_ROOM_STATE, {})
        if not isinstance(new_states, Mapping):
            return True
        for name in self._room_names:
            persisted = str(new_states.get(name, DEFAULT_ROOM_STATE))
            if persisted != self.get_room_state(name):
                return True
        return False

    # -- Internal: persistence of setpoint state ----------------------------

    @callback
    def _persist_setpoints(self) -> None:
        """Persist home temperature + per-room offsets to a private Store.

        Setpoints are runtime state, not configuration, so they are written to a
        dedicated :class:`~homeassistant.helpers.storage.Store` and NOT to
        ``entry.data``. Writing ``entry.data`` would call ``async_update_entry``,
        which fires the config-entry update listener and reloads the whole
        integration on every home-temperature or offset nudge. The debounced
        Store write touches neither ``entry.data`` nor ``entry.options``, so no
        reload is triggered; the values are restored on the next setup via
        :meth:`_ensure_setpoints_loaded`.
        """
        self._setpoint_store.async_delay_save(
            self._setpoint_snapshot, _SETPOINT_SAVE_DELAY_S
        )

    @callback
    def _setpoint_snapshot(self) -> dict[str, Any]:
        """Return the current setpoint state to persist to the Store.

        Includes the global operating mode (S9): a restart must not silently
        fall back to heating logic in the cooling season.
        """
        return {
            CONF_HOME_SETPOINT: self._home_temperature_c,
            CONF_ROOM_OFFSET: dict(self._room_offsets),
            _STORE_KEY_MODE: self._mode.value,
        }

    async def _ensure_setpoints_loaded(self) -> None:
        """Restore persisted setpoints from the Store, once per instance.

        Runs during the first refresh (before the entity platforms are set up),
        so restored setpoints are visible to the number entities immediately
        after a reload or restart. Values seeded from ``entry.data`` in
        :meth:`__init__` are the fallback used until the Store has a value.
        """
        if self._setpoints_loaded:
            return
        self._setpoints_loaded = True
        stored = await self._setpoint_store.async_load()
        if not stored:
            return
        home = stored.get(CONF_HOME_SETPOINT)
        if isinstance(home, int | float) and math.isfinite(float(home)):
            self._home_temperature_c = float(home)
        offsets = stored.get(CONF_ROOM_OFFSET)
        if isinstance(offsets, dict):
            for name in self._room_names:
                value = offsets.get(name)
                if isinstance(value, int | float) and math.isfinite(float(value)):
                    self._room_offsets[name] = float(value)
        # Restore the persisted global mode (S9). A configured, available mode
        # entity still wins on the very first refresh (_read_mode); the stored
        # value is the fallback that keeps a July restart in cooling logic.
        raw_mode = stored.get(_STORE_KEY_MODE)
        if isinstance(raw_mode, str):
            try:
                self._mode = Mode(raw_mode)
            except ValueError:
                _LOGGER.warning("Ignoring invalid persisted mode %r", raw_mode)

    @callback
    def _persist_options(self, changes: dict[str, Any]) -> None:
        """Merge ``changes`` into ``entry.options`` and persist them.

        Used by :meth:`set_room_state` so a control-state change made from any
        surface (the select entity or the panel/websocket) survives a reload or
        restart. Writing ``entry.options`` fires the update listener; for a
        state-only change that listener consults :meth:`options_require_reload`
        and skips the reload (the in-memory state is already current), so the PID
        integrator is preserved.

        Args:
            changes: Option keys/values to merge into ``entry.options``.
        """
        entry = self.config_entry
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, **changes}
        )

    # -- Internal: rebroadcast on setpoint change ---------------------------

    @callback
    def _rebroadcast_setpoints(self) -> None:
        """Rebuild cached room payloads with fresh setpoints and rebroadcast.

        Called after a home-temperature or offset change so entities see the new
        target immediately, before the next 5-minute refresh.
        """
        if self.data is None:
            return
        new_rooms: dict[str, RoomRuntime] = {}
        for name, runtime in self.data.rooms.items():
            new_rooms[name] = RoomRuntime(
                outputs=runtime.outputs,
                report=runtime.outputs.report,
                live_control_enabled=runtime.live_control_enabled,
                setpoint_c=self.get_room_setpoint(name),
            )
        self.async_set_updated_data(
            CoordinatorData(
                rooms=new_rooms,
                global_safe_dew_point_c=self.data.global_safe_dew_point_c,
                algorithm_status=self.data.algorithm_status,
                watchdog_state=self.data.watchdog_state,
                last_update_timestamp=self.data.last_update_timestamp,
                mode=self.data.mode,
                sensor_lost_rooms=self.data.sensor_lost_rooms,
            )
        )

    @callback
    def _schedule_recompute(self) -> None:
        """Request a debounced full recompute after a setpoint change.

        The immediate ``_rebroadcast_setpoints`` already shows the new target in
        the entities and panel; this additionally schedules a trailing
        :meth:`async_request_refresh` so a full :meth:`_async_update_data` re-runs
        the control step with a consistent ``error_c`` and re-emits the write
        path — notably the split ``set_temperature`` with the new target. A burst
        of stepper clicks collapses to one refresh via the debouncer's cooldown.
        """
        self.hass.async_create_task(
            self._recompute_debouncer.async_call(),
            name=f"{DOMAIN}_recompute_{self.config_entry.entry_id}",
        )

    @callback
    def async_cancel_recompute(self) -> None:
        """Cancel any pending debounced recompute (called on entry unload)."""
        self._recompute_debouncer.async_cancel()

    # -- Update cycle -------------------------------------------------------

    async def _async_update_data(self) -> CoordinatorData:
        """Read sources, run the controller, store data and write commands.

        Returns:
            The freshly computed :class:`CoordinatorData`.
        """
        await self._ensure_setpoints_loaded()
        new_mode = self._read_mode()
        if new_mode is not self._mode:
            self._mode = new_mode
            # Persist a mode change sourced from the mode entity too (S9).
            self._persist_setpoints()

        # Assemble one RoomInputs per room from the configured entities.
        inputs: dict[str, RoomInputs] = {}
        any_fresh = False
        for room_cfg, name in zip(self._room_configs, self._room_names, strict=True):
            room_inputs = self._build_room_inputs(room_cfg, name)
            inputs[name] = room_inputs
            if room_inputs.room_temperature_c is not None:
                any_fresh = True

        # Run the core black box. It never raises on a single room; guard the
        # whole step defensively at the HA boundary regardless.
        algorithm_status = "running"
        outputs_by_room: dict[str, RoomOutputs] = {}
        global_dew: float | None = None
        sensor_lost_rooms = 0
        if self._building is not None and inputs:
            # Feed the core the REAL elapsed time since the previous step
            # (clamped), so an off-cycle debounced recompute integrates honestly
            # instead of assuming a full nominal cycle. On the first step there is
            # no reference, so fall back to the nominal cycle length.
            now_monotonic = time.monotonic()
            if self._last_step_monotonic is None:
                dt_seconds = self._cycle_seconds
            else:
                dt_seconds = min(
                    _MAX_DT_SECONDS,
                    max(_MIN_DT_SECONDS, now_monotonic - self._last_step_monotonic),
                )
            self._last_step_monotonic = now_monotonic
            try:
                building_outputs = self._building.step(inputs, dt_seconds=dt_seconds)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("BuildingController.step failed")
                algorithm_status = "error"
            else:
                outputs_by_room = building_outputs.rooms
                global_dew = building_outputs.global_safe_dew_point_c
                sensor_lost_rooms = building_outputs.sensor_lost_rooms

        now = datetime.now(UTC)
        if any_fresh:
            self._last_heartbeat = now
            if self._fresh_since is None:
                self._fresh_since = now
        else:
            # Any gap in fresh data resets the sustained-recovery clock.
            self._fresh_since = None
        if algorithm_status != "error" and not any_fresh and self._room_names:
            algorithm_status = "stale"
        watchdog_state = self._watchdog_state(now)

        # Build the typed payload.
        rooms: dict[str, RoomRuntime] = {}
        for room_cfg, name in zip(self._room_configs, self._room_names, strict=True):
            room_outputs = outputs_by_room.get(name)
            if room_outputs is None:
                continue
            # S8: flag a live room whose valve feedback keeps disagreeing with
            # the commanded position ("the valve does not listen").
            if self._track_valve_mismatch(room_cfg, name, inputs[name]):
                report = replace(
                    room_outputs.report,
                    flags=tuple(
                        dict.fromkeys((*room_outputs.report.flags, "valve_mismatch"))
                    ),
                )
                room_outputs = replace(room_outputs, report=report)
            rooms[name] = RoomRuntime(
                outputs=room_outputs,
                report=room_outputs.report,
                live_control_enabled=self.get_room_state(name) == ROOM_STATE_LIVE,
                setpoint_c=self.get_room_setpoint(name),
            )

        # Emit commands for LIVE rooms only; OFF and SHADOW rooms are computed
        # and reported but never written.
        for room_cfg, name in zip(self._room_configs, self._room_names, strict=True):
            runtime = rooms.get(name)
            if runtime is None or not runtime.live_control_enabled:
                continue
            await self._write_valves(room_cfg, name, runtime.outputs)
            await self._write_fast_source(room_cfg, name, runtime.outputs)

        return CoordinatorData(
            rooms=rooms,
            global_safe_dew_point_c=global_dew,
            algorithm_status=algorithm_status,
            watchdog_state=watchdog_state,
            last_update_timestamp=now.isoformat(),
            mode=self._mode.value,
            sensor_lost_rooms=sensor_lost_rooms,
        )

    # -- Internal: input assembly -------------------------------------------

    def _build_room_inputs(self, room_cfg: dict[str, Any], name: str) -> RoomInputs:
        """Assemble one room's :class:`RoomInputs` from its configured entities.

        A read that violates a core dataclass invariant (e.g. an out-of-range
        humidity or valve feedback) degrades the room safely to a lost-sensor
        input rather than breaking the whole cycle.

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.

        Returns:
            The room's :class:`~tortoise_ufh.models.RoomInputs`.
        """
        setpoint = self.get_room_setpoint(name)
        cooling_enabled = bool(room_cfg.get(CONF_COOLING_ENABLED, True))
        # A room participates in control unless it is switched fully off; an
        # OFF room is fed Mode.OFF so the core holds the valve and idles the
        # fast source. SHADOW and LIVE both participate (compute); only LIVE is
        # written (gated below in _async_update_data).
        participates = self.get_room_state(name) != ROOM_STATE_OFF
        mode = self._mode if participates else Mode.OFF
        # Validate humidity independently: an out-of-range reading only nulls
        # the dew-point input rather than degrading the whole room. A stale
        # (frozen-but-present) humidity is the single most dangerous input in
        # cooling — both condensation defences trust it — so it also carries a
        # max state age (C4).
        humidity = self._read_float_state(
            room_cfg.get(CONF_ENTITY_HUMIDITY),
            max_age_seconds=_HUMIDITY_MAX_AGE_S,
        )
        if humidity is not None and not 0.0 <= humidity <= 100.0:
            humidity = None
        # S6: per-room data age for the core S5 watchdog. A fresh temperature
        # resets the clock; otherwise the age grows from the last fresh sample
        # (or from the first time the room was ever seen).
        room_temp = self._read_room_temperature(room_cfg.get(CONF_ENTITY_TEMP_ROOM))
        now = datetime.now(UTC)
        if room_temp is not None:
            self._room_last_fresh[name] = now
        last_fresh = self._room_last_fresh.setdefault(name, now)
        age_minutes = max(0.0, (now - last_fresh).total_seconds() / 60.0)
        try:
            loops = self._build_loops(room_cfg)
            return RoomInputs(
                mode=mode,
                setpoint_c=setpoint,
                room_temperature_c=room_temp,
                humidity_pct=humidity,
                outdoor_temperature_c=self._read_float_state(
                    room_cfg.get(CONF_ENTITY_TEMP_OUTDOOR)
                ),
                loops=loops,
                fast_source_kind=self._read_fast_source_kind(room_cfg),
                fast_source_on=self._read_fast_source_on(room_cfg),
                hp_active_for_ufh=self._read_hp_active_for_ufh(room_cfg),
                cooling_enabled=cooling_enabled,
                last_update_age_minutes=age_minutes,
            )
        except ValueError:
            _LOGGER.warning(
                "Room '%s' produced invalid inputs; degrading to lost sensor",
                name,
                exc_info=True,
            )
            return RoomInputs(
                mode=mode,
                setpoint_c=setpoint,
                room_temperature_c=None,
                cooling_enabled=cooling_enabled,
                last_update_age_minutes=age_minutes,
            )

    def _build_loops(self, room_cfg: dict[str, Any]) -> tuple[LoopInput, ...]:
        """Build the room's UFH loops from parallel entity lists.

        Loop count is the longest of the valve / supply / return lists; missing
        positions read as ``None``.

        Args:
            room_cfg: The room's configuration dict.

        Returns:
            A tuple of :class:`~tortoise_ufh.models.LoopInput`.
        """
        valves: list[str] = list(room_cfg.get(CONF_ENTITY_VALVES) or [])
        supplies: list[str] = list(room_cfg.get(CONF_ENTITY_SUPPLY) or [])
        returns: list[str] = list(room_cfg.get(CONF_ENTITY_RETURN) or [])
        n_loops = max(len(valves), len(supplies), len(returns))
        loops: list[LoopInput] = []
        for i in range(n_loops):
            loops.append(
                LoopInput(
                    valve_position_pct=self._read_valve_position(
                        valves[i] if i < len(valves) else None
                    ),
                    supply_temperature_c=self._read_float_state(
                        supplies[i] if i < len(supplies) else None
                    ),
                    return_temperature_c=self._read_float_state(
                        returns[i] if i < len(returns) else None
                    ),
                )
            )
        return tuple(loops)

    def _track_valve_mismatch(
        self, room_cfg: dict[str, Any], name: str, room_inputs: RoomInputs
    ) -> bool:
        """Track command-vs-feedback valve divergence for a live room (S8).

        Compares each loop's valve feedback (read this cycle, i.e. the response
        to the PREVIOUS cycle's command) with the last value actually written to
        that entity. Any loop diverging by more than
        :data:`_VALVE_MISMATCH_TOLERANCE_PCT` counts the whole room as
        mismatched this cycle; :data:`_VALVE_MISMATCH_CYCLES` consecutive
        mismatched cycles raise the ``valve_mismatch`` report flag. Non-live
        rooms (nothing is written, so feedback legitimately disagrees) and
        cycles without comparable data reset / hold the counter respectively.

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.
            room_inputs: The room's inputs assembled this cycle.

        Returns:
            ``True`` when the ``valve_mismatch`` flag should be raised.
        """
        if self.get_room_state(name) != ROOM_STATE_LIVE:
            self._valve_mismatch_cycles[name] = 0
            return False
        valves: list[str] = list(room_cfg.get(CONF_ENTITY_VALVES) or [])
        compared = False
        mismatch = False
        for i, valve_entity in enumerate(valves):
            written = self._last_written_valve.get(valve_entity)
            if written is None or i >= len(room_inputs.loops):
                continue
            feedback = room_inputs.loops[i].valve_position_pct
            if feedback is None:
                continue
            compared = True
            if abs(feedback - written) > _VALVE_MISMATCH_TOLERANCE_PCT:
                mismatch = True
        if not compared:
            # No evidence either way: hold the current verdict.
            return self._valve_mismatch_cycles.get(name, 0) >= _VALVE_MISMATCH_CYCLES
        count = self._valve_mismatch_cycles.get(name, 0) + 1 if mismatch else 0
        self._valve_mismatch_cycles[name] = count
        if count == _VALVE_MISMATCH_CYCLES:
            _LOGGER.warning(
                "Room %s valve feedback has disagreed with the command for %d "
                "cycles; flagging valve_mismatch",
                name,
                count,
            )
        return count >= _VALVE_MISMATCH_CYCLES

    def _read_fast_source_kind(self, room_cfg: dict[str, Any]) -> FastSourceKind:
        """Map the configured fast-source kind string to the core enum.

        Args:
            room_cfg: The room's configuration dict.

        Returns:
            The :class:`~tortoise_ufh.models.FastSourceKind` (``NONE`` on an
            unrecognised or missing value).
        """
        raw = str(room_cfg.get(CONF_FAST_SOURCE_KIND, "none") or "none").lower()
        try:
            return FastSourceKind(raw)
        except ValueError:
            return FastSourceKind.NONE

    def _read_fast_source_on(self, room_cfg: dict[str, Any]) -> bool | None:
        """Read the fast source's on/off feedback from its climate entity.

        Args:
            room_cfg: The room's configuration dict.

        Returns:
            ``True`` if the split is running, ``False`` if off, ``None`` when no
            entity is configured or its state is unavailable.
        """
        entity_id = room_cfg.get(CONF_ENTITY_FAST_SOURCE)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state.lower() in _UNAVAILABLE_STATES:
            return None
        return state.state.lower() != "off"

    def _read_hp_active_for_ufh(self, room_cfg: dict[str, Any]) -> bool | None:
        """Read whether the heat pump is actively heating the UFH supply.

        Maps the optional :data:`CONF_ENTITY_HP_ACTIVE` entity's state to the
        core's tri-state used for the integrator freeze during DHW / defrost:
        ``True`` when the pump is heating the floor, ``False`` when it is
        diverted (so the integrator freezes and cannot wind up), and ``None``
        when no entity is configured or its state is unavailable.

        Args:
            room_cfg: The room's configuration dict.

        Returns:
            ``True`` / ``False`` / ``None`` per above.
        """
        entity_id = room_cfg.get(CONF_ENTITY_HP_ACTIVE)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state.lower() in _UNAVAILABLE_STATES:
            return None
        return state.state.lower() not in _HP_INACTIVE_STATES

    def _read_mode(self) -> Mode:
        """Read the global mode entity, holding the last mode on failure.

        Returns:
            The resolved global :class:`~tortoise_ufh.models.Mode`.
        """
        if not self._mode_entity:
            return self._mode
        state = self.hass.states.get(self._mode_entity)
        if state is None or state.state.lower() in _UNAVAILABLE_STATES:
            return self._mode
        try:
            return Mode(state.state.lower())
        except ValueError:
            _LOGGER.warning(
                "Mode entity %s has unrecognised state %r; holding %s",
                self._mode_entity,
                state.state,
                self._mode.value,
            )
            return self._mode

    # -- Internal: watchdog -------------------------------------------------

    def _watchdog_state(self, now: datetime) -> str:
        """Compute the watchdog state from the heartbeat age.

        Args:
            now: The current UTC time.

        Returns:
            ``"ok"`` only when fresh data arrived within
            :data:`WATCHDOG_TIMEOUT_MINUTES` *and* has been sustained
            continuously for at least :data:`WATCHDOG_RECOVERY_MINUTES`;
            otherwise ``"stale"`` (including during the recovery window, so a
            single flaky sample cannot clear the fault).
        """
        age_minutes = (now - self._last_heartbeat).total_seconds() / 60.0
        if age_minutes > WATCHDOG_TIMEOUT_MINUTES:
            self._watchdog_faulted = True
            return "stale"
        if self._fresh_since is None:
            return "stale"
        # Clean start / never faulted: fresh data within timeout clears "ok"
        # immediately; the recovery window only gates clearing a real fault.
        if not self._watchdog_faulted:
            return "ok"
        recovered_minutes = (now - self._fresh_since).total_seconds() / 60.0
        if recovered_minutes >= WATCHDOG_RECOVERY_MINUTES:
            self._watchdog_faulted = False
            return "ok"
        return "stale"

    # -- Internal: command writes -------------------------------------------

    async def _write_valves(
        self, room_cfg: dict[str, Any], name: str, outputs: RoomOutputs
    ) -> None:
        """Write the recommended valve position to every room valve entity.

        Each actuator is driven per its domain: a ``valve``-domain entity via
        ``valve.set_valve_position`` (integer ``position`` 0..100) and any other
        (``number`` …) via ``number.set_value`` (float ``value``). A value is
        written to an entity only when it differs from the last value written to
        that entity by at least the room's ``valve_write_threshold_pct``. All
        calls are non-blocking.

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.
            outputs: The room's controller outputs.
        """
        valves: list[str] = list(room_cfg.get(CONF_ENTITY_VALVES) or [])
        if not valves:
            return
        threshold = self._controller_configs[name].valve_write_threshold_pct
        value = outputs.valve_position_pct
        for valve_entity in valves:
            last = self._last_written_valve.get(valve_entity)
            if last is not None and abs(value - last) < threshold:
                continue
            try:
                if self._is_valve_domain(valve_entity):
                    # valve.set_valve_position expects an int 0..100 position.
                    await self.hass.services.async_call(
                        "valve",
                        "set_valve_position",
                        {"entity_id": valve_entity, "position": round(value)},
                        blocking=False,
                    )
                else:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": valve_entity, "value": value},
                        blocking=False,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Failed to set valve %s for room %s", valve_entity, name
                )
            else:
                self._last_written_valve[valve_entity] = value

    async def _write_fast_source(
        self, room_cfg: dict[str, Any], name: str, outputs: RoomOutputs
    ) -> None:
        """Write the fast-source command to the room's climate entity.

        Issues ``set_hvac_mode`` and, when the split is on, ``set_temperature``.
        All calls are non-blocking.

        Command cache (S3, 2026-07-09): an unchanged ``(hvac_mode, target)``
        pair is NOT re-sent every cycle — mirroring ``_last_written_valve`` —
        so the split is not spammed with identical commands (IR beeps, stomping
        on manual louvre/fan tweaks). The command is still re-asserted after
        :data:`_FAST_REASSERT_SECONDS` so a missed write or a manual override
        self-heals: the machine stays the owner.

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.
            outputs: The room's controller outputs.
        """
        entity_id = room_cfg.get(CONF_ENTITY_FAST_SOURCE)
        if not entity_id:
            return
        command = outputs.fast_source
        hvac_mode = (
            _HVAC_MODE_BY_FAST_SOURCE.get(command.mode, "off") if command.on else "off"
        )
        target = command.target_temperature_c if command.on else None
        cached = self._last_written_fast.get(entity_id)
        now_monotonic = time.monotonic()
        if (
            cached is not None
            and cached[0] == hvac_mode
            and cached[1] == target
            and now_monotonic - cached[2] < _FAST_REASSERT_SECONDS
        ):
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": hvac_mode},
                blocking=False,
            )
            if command.on and command.target_temperature_c is not None:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {
                        "entity_id": entity_id,
                        "temperature": command.target_temperature_c,
                    },
                    blocking=False,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Failed to control fast source %s for room %s", entity_id, name
            )
        else:
            self._last_written_fast[entity_id] = (hvac_mode, target, now_monotonic)

    # -- Internal: farewell command (live -> shadow/off, unload) -------------

    @callback
    def _schedule_farewell(self, room_name: str) -> None:
        """Schedule the one-shot farewell write for a room leaving live.

        Args:
            room_name: The room being released from live control.
        """
        for room_cfg, name in zip(self._room_configs, self._room_names, strict=True):
            if name == room_name:
                self.hass.async_create_task(
                    self._async_farewell_room(room_cfg, name),
                    name=f"{DOMAIN}_farewell_{self.config_entry.entry_id}_{name}",
                )
                return

    async def _async_farewell_room(self, room_cfg: dict[str, Any], name: str) -> None:
        """Park a room's actuators safely when releasing live ownership (C5).

        Emitted exactly once on a live -> shadow/off transition and on entry
        unload. The split is always commanded OFF (nobody regulates it any
        more). The valve is mode-dependent: in COOLING it is driven to 0 —
        an orphaned open valve would keep passing chilled water while the room
        silently drops out of BOTH condensation defences (the global dew
        maximum and the local S2 throttle). In HEATING the position is left
        untouched: warm supply water is bounded by the heat pump's own curve,
        so holding the last position keeps the house warm and is strictly
        safer than cold-parking it in winter.

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.
        """
        entity_id = room_cfg.get(CONF_ENTITY_FAST_SOURCE)
        if entity_id:
            try:
                await self.hass.services.async_call(
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": entity_id, "hvac_mode": "off"},
                    blocking=False,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Farewell: failed to turn off fast source %s for room %s",
                    entity_id,
                    name,
                )
            else:
                self._last_written_fast[entity_id] = ("off", None, time.monotonic())
        if self._mode is not Mode.COOLING:
            return
        valves: list[str] = list(room_cfg.get(CONF_ENTITY_VALVES) or [])
        for valve_entity in valves:
            try:
                if self._is_valve_domain(valve_entity):
                    await self.hass.services.async_call(
                        "valve",
                        "set_valve_position",
                        {"entity_id": valve_entity, "position": 0},
                        blocking=False,
                    )
                else:
                    await self.hass.services.async_call(
                        "number",
                        "set_value",
                        {"entity_id": valve_entity, "value": 0.0},
                        blocking=False,
                    )
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Farewell: failed to close valve %s for room %s",
                    valve_entity,
                    name,
                )
            else:
                self._last_written_valve[valve_entity] = 0.0

    async def async_farewell_all(self) -> None:
        """Park every live room's actuators (called on config-entry unload)."""
        for room_cfg, name in zip(self._room_configs, self._room_names, strict=True):
            if self.get_room_state(name) == ROOM_STATE_LIVE:
                await self._async_farewell_room(room_cfg, name)

    # -- Internal: entity reads ---------------------------------------------

    @staticmethod
    def _is_valve_domain(entity_id: str) -> bool:
        """Return whether ``entity_id`` is a Home Assistant ``valve`` entity.

        ``valve`` actuators report position in the ``current_position``
        attribute and are driven via ``valve.set_valve_position``; every other
        domain (``number`` …) reports position as its numeric state and is
        driven via ``number.set_value``.

        Args:
            entity_id: A non-empty Home Assistant entity id.

        Returns:
            ``True`` for a ``valve``-domain entity, ``False`` otherwise.
        """
        return split_entity_id(entity_id)[0] == _VALVE_DOMAIN

    def _read_valve_position(self, entity_id: str | None) -> float | None:
        """Read a valve actuator's position [0..100 %], dispatching by domain.

        A ``valve``-domain actuator reports its position in the
        ``current_position`` attribute (its *state* is ``open`` / ``closed`` /
        ``opening`` / ``closing`` and is not numeric), so it is read from that
        attribute. Every other domain (``number`` …) reports the position as its
        numeric state and is read through :meth:`_read_float_state` with its
        short stale cache — so the ``number`` path is byte-for-byte the previous
        behaviour.

        Args:
            entity_id: The valve actuator entity id, or ``None`` / empty when the
                loop has no valve at this position.

        Returns:
            The reported position [0..100 %], or ``None`` when it cannot be read.
        """
        if not entity_id:
            return None
        if not self._is_valve_domain(entity_id):
            value_or_none = self._read_float_state(entity_id)
        else:
            state = self.hass.states.get(entity_id)
            if state is None:
                return None
            position = state.attributes.get("current_position")
            if position is None:
                return None
            try:
                value = float(position)
            except (ValueError, TypeError):
                return None
            value_or_none = value if math.isfinite(value) else None
        # Per-loop plausibility (S8): a garbage feedback (e.g. 255 from a stuck
        # Modbus register) nulls ONLY this loop's feedback instead of tripping
        # the LoopInput validator and degrading the whole room to sensor_lost.
        if value_or_none is not None and not 0.0 <= value_or_none <= 100.0:
            _LOGGER.warning(
                "Valve %s reported implausible position %s%%; ignoring",
                entity_id,
                value_or_none,
            )
            return None
        return value_or_none

    def _read_room_temperature(self, entity_id: str | None) -> float | None:
        """Read a room-temperature entity with plausibility gating (C3 + C4).

        On top of :meth:`_read_float_state` (which already enforces the
        :data:`_ROOM_TEMP_MAX_AGE_S` state age), a sample is rejected — treated
        exactly like a missing reading, triggering the core's safe degrade —
        when it is outside :data:`_TEMP_PLAUSIBLE_MIN_C` ..
        :data:`_TEMP_PLAUSIBLE_MAX_C` (e.g. the DS18B20 85 degC power-on-reset)
        or when it jumps more than :data:`_TEMP_MAX_JUMP_K` from the last
        accepted value. Two consecutive mutually consistent samples accept a
        genuinely new level, so a real fast change is adopted within two
        cycles instead of being locked out forever.

        Args:
            entity_id: The room-temperature entity id, or ``None`` / empty.

        Returns:
            The accepted temperature [degC], or ``None`` when unavailable or
            rejected as implausible.
        """
        if not entity_id:
            return None
        value = self._read_float_state(entity_id, max_age_seconds=_ROOM_TEMP_MAX_AGE_S)
        if value is None:
            self._temp_pending.pop(entity_id, None)
            return None
        if not _TEMP_PLAUSIBLE_MIN_C <= value <= _TEMP_PLAUSIBLE_MAX_C:
            _LOGGER.warning(
                "Entity %s reported implausible room temperature %.1f degC; "
                "rejecting sample",
                entity_id,
                value,
            )
            self._temp_pending.pop(entity_id, None)
            return None
        last = self._temp_last_accepted.get(entity_id)
        pending = self._temp_pending.get(entity_id)
        jumped = last is not None and abs(value - last) > _TEMP_MAX_JUMP_K
        confirmed = pending is not None and abs(value - pending) <= _TEMP_MAX_JUMP_K
        if jumped and not confirmed:
            _LOGGER.warning(
                "Entity %s jumped %.1f -> %.1f degC in one cycle; holding "
                "sample for confirmation",
                entity_id,
                last,
                value,
            )
            self._temp_pending[entity_id] = value
            return None
        # Either plausible against the last accepted value, or the second
        # consecutive sample consistent with the pending candidate — the new
        # level is real (e.g. a window opened), accept it.
        self._temp_last_accepted[entity_id] = value
        self._temp_pending.pop(entity_id, None)
        return value

    def _read_float_state(
        self, entity_id: str | None, *, max_age_seconds: float | None = None
    ) -> float | None:
        """Read a numeric entity state with a short stale cache.

        On a successful read the value is cached with the current time. When the
        entity is unavailable/unknown the cached value is returned if it is
        younger than :data:`ENTITY_STALE_MAX_SECONDS`; otherwise ``None``.

        When ``max_age_seconds`` is given, a present-but-frozen state whose last
        report is older than that limit is treated as **no reading at all**
        (C4) — deliberately NOT falling back to the short cache, which would
        hold the very same stale value.

        Args:
            entity_id: The source entity id, or ``None`` / empty for "not
                configured".
            max_age_seconds: Optional maximum age of the state's last report
                [s]; older states are rejected outright.

        Returns:
            The numeric value, or ``None`` when unreadable and no fresh cache
            exists.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if (
            state is not None
            and state.state.lower() not in _UNAVAILABLE_STATES
            and max_age_seconds is not None
        ):
            # last_reported also covers same-value re-reports (HA 2024.4+);
            # fall back to last_updated on older cores.
            reported = getattr(state, "last_reported", None) or state.last_updated
            age_s = (datetime.now(UTC) - reported).total_seconds()
            if age_s > max_age_seconds:
                _LOGGER.warning(
                    "Entity %s state is %.0f min old (limit %.0f min); "
                    "treating as unavailable",
                    entity_id,
                    age_s / 60.0,
                    max_age_seconds / 60.0,
                )
                return None
        if state is None or state.state.lower() in _UNAVAILABLE_STATES:
            cached = self._entity_cache.get(entity_id)
            if cached is not None:
                value, ts = cached
                age = (datetime.now(UTC) - ts).total_seconds()
                if age <= ENTITY_STALE_MAX_SECONDS:
                    _LOGGER.debug(
                        "Entity %s unavailable; using cached %.2f (age %.0fs)",
                        entity_id,
                        value,
                        age,
                    )
                    return value
            _LOGGER.warning(
                "Entity %s is unavailable and no recent cached value exists",
                entity_id,
            )
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if not math.isfinite(value):
            _LOGGER.warning(
                "Entity %s reported non-finite value %r; ignoring",
                entity_id,
                state.state,
            )
            return None
        self._entity_cache[entity_id] = (value, datetime.now(UTC))
        return value
