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
   ``climate.set_temperature`` for the split). An ``off`` room is computed
   against ``Mode.OFF`` and never written.

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

from homeassistant.core import callback
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTROLLER,
    CONF_COOLING_ENABLED,
    CONF_ENTITY_FAST_SOURCE,
    CONF_ENTITY_GLOBAL_SUPPLY,
    CONF_ENTITY_HP_ACTIVE,
    CONF_ENTITY_HP_COMPRESSOR_FREQ,
    CONF_ENTITY_HP_COOLING_SETPOINT,
    CONF_ENTITY_HP_HEATING_SETPOINT,
    CONF_ENTITY_HP_MODE,
    CONF_ENTITY_HP_OUTLET_TEMP,
    CONF_ENTITY_HP_RETURN_TEMP,
    CONF_ENTITY_HUMIDITY,
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_TEMP_OUTDOOR,
    CONF_ENTITY_TEMP_ROOM,
    CONF_ENTITY_VALVES,
    CONF_FAST_SOURCE_GROUP,
    CONF_FAST_SOURCE_KIND,
    CONF_FAST_WINDOW_END,
    CONF_FAST_WINDOW_START,
    CONF_HEAT_PUMP,
    CONF_HOME_SETPOINT,
    CONF_ROOM_NAME,
    CONF_ROOM_OFFSET,
    CONF_ROOM_STATE,
    CONF_ROOM_TUNING,
    CONF_ROOMS,
    DEFAULT_ROOM_STATE,
    DOMAIN,
    ROOM_STATE_LIVE,
    ROOM_STATE_OFF,
    ROOM_STATES,
    UPDATE_INTERVAL_MINUTES,
    WATCHDOG_RECOVERY_MINUTES,
    WATCHDOG_TIMEOUT_MINUTES,
)
from .core.config import ControllerConfig
from .core.controller import BuildingController
from .core.fast_source import window_allows
from .core.hp_link import (
    CoolingDemand,
    FlickerDecision,
    SetpointFlicker,
    cooling_demand,
    cooling_setpoint_c,
    dhw_option,
    direction_option,
    heating_curve,
)
from .core.models import (
    LoopInput,
    Mode,
    RoomInputs,
    RoomOutputs,
    RoomReport,
)
from .readers import SourceReader
from .writers import CommandWriter

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

# The input-plausibility constants (temperature range/jump gates, state ages)
# and the write-path constants (fast re-assert, valve domain) moved to
# ``readers.py`` / ``writers.py`` with the SourceReader / CommandWriter
# extraction (2026-07-10).

_VALVE_MISMATCH_TOLERANCE_PCT: float = 10.0
"""Command-vs-feedback divergence beyond which a valve counts as mismatched [%]."""

_VALVE_MISMATCH_CYCLES: int = 3
"""Consecutive mismatched cycles before the ``valve_mismatch`` flag is raised."""

# NOTE: ``CONF_ENTITY_HP_ACTIVE`` moved to ``const.py`` (B2, 2026-07-12) —
# it is configurable in the options flow's heat-pump section now. The legacy
# per-room key of the same name is still honoured as an override in
# ``_build_room_inputs``.

_HP_MODE_MIN_REWRITE_S: float = 15.0 * 60.0
"""Minimum spacing between two pump-mode DIRECTION writes [s] (B2).

The heat pump is a slow device and the external DHW automation owns the DHW
flag; direction rewrites happen only on a Tortoise mode change or a persistent
divergence, and never more often than this."""

_HP_DIVERGENCE_CYCLES: int = 2
"""Consecutive divergent cycles before a direction rewrite is allowed (B2)."""


class HpNotConfiguredError(ValueError):
    """The heat-pump link (or its mode entity) is not configured."""


class HpDhwUnavailableError(ValueError):
    """The requested DHW toggle cannot be honoured right now."""


def _parse_minute_of_day(raw: str) -> int | None:
    """Parse an ``"HH:MM"`` (or ``"HH:MM:SS"``) string to a minute-of-day.

    Args:
        raw: The persisted time string.

    Returns:
        Minutes after local midnight (0..1439), or ``None`` when unparseable.
    """
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


_ALGORITHM_STATES: frozenset[str] = frozenset({"running", "stale", "error"})
"""Permitted :attr:`CoordinatorData.algorithm_status` values."""

_WATCHDOG_STATES: frozenset[str] = frozenset({"ok", "stale"})
"""Permitted :attr:`CoordinatorData.watchdog_state` values."""


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
        setpoint_c: The effective target temperature [degC] used this cycle,
            equal to the global home temperature plus the room's offset.

    Raises:
        ValueError: If ``report`` is not ``outputs.report`` or ``setpoint_c`` is
            not finite.
    """

    outputs: RoomOutputs
    report: RoomReport
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
class HeatPumpRuntime:
    """One cycle's view of the OPTIONAL heat-pump link (B2, 2026-07-12).

    Built by :meth:`TortoiseUfhCoordinator._sync_heat_pump` and consumed by
    the websocket's ``get_live`` (the panel's heat-pump tab). All temperatures
    in degrees Celsius. ``None`` sub-payloads mean "entity not configured".

    Attributes:
        mode_entity_id: The pump-mode select entity id, or ``None``.
        current_option: The pump's current mode option (raw), or ``None``.
        desired_option: The direction Tortoise wants (canonical HeishaMon
            string), or ``None`` when no direction is forced (transitional /
            off / DHW-only / unknown option).
        in_sync: Whether the pump's option already matches the desired one;
            ``None`` when there is no desired direction to compare.
        dhw_active: Whether the current option carries the DHW flag.
        dhw_only: Whether the pump is currently in ``"DHW only"``.
        cooling: Cooling-setpoint payload ``{entity_id, target_c, base_c,
            safe_dew_c}`` or ``None`` when the entity is not configured.
        heating: Heating-setpoint payload ``{entity_id, target_c, t_out_c,
            base_c, slope, neutral_c}`` or ``None``.
        hp_active: The "pump serves the UFH" reading, or ``None`` (unknown /
            unconfigured).
        hp_active_configured: Whether an hp-active entity is configured.
        writes_enabled: Whether the link may write this cycle (not parked and
            at least one room is LIVE).
        flicker: The cooling setpoint-flicker's per-cycle view (issue #7,
            2026-07-15) as a JSON dict ``{enabled, state, flags, trigger_c,
            stuck_remaining_s, cooldown_remaining_s, pulses_last_hour,
            last_pulse_target_c, pulse_target_c, return_c, compressor_freq_hz,
            outlet_c, demand_open_pct, demand_threshold_pct}`` (``flags`` is
            this cycle's ``FlickerDecision.flags`` — the panel renders them
            from ``FLAG_LABELS``; the two ``demand_*`` keys are the §23
            loop-weighted demand gate), or ``None`` when neither the flicker
            nor any of its diagnostic entities is configured.

    Raises:
        ValueError: If mode sub-fields are set without a mode entity.
    """

    mode_entity_id: str | None
    current_option: str | None
    desired_option: str | None
    in_sync: bool | None
    dhw_active: bool
    dhw_only: bool
    cooling: dict[str, Any] | None
    heating: dict[str, Any] | None
    hp_active: bool | None
    hp_active_configured: bool
    writes_enabled: bool
    flicker: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate internal consistency of the heat-pump payload."""
        if self.mode_entity_id is None and (
            self.current_option is not None or self.desired_option is not None
        ):
            msg = "mode options require a mode entity"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for the websocket / panel."""
        return {
            "configured": True,
            "mode_entity_id": self.mode_entity_id,
            "current_option": self.current_option,
            "desired_option": self.desired_option,
            "in_sync": self.in_sync,
            "dhw_active": self.dhw_active,
            "dhw_only": self.dhw_only,
            "cooling": dict(self.cooling) if self.cooling is not None else None,
            "heating": dict(self.heating) if self.heating is not None else None,
            "hp_active": self.hp_active,
            "hp_active_configured": self.hp_active_configured,
            "writes_enabled": self.writes_enabled,
            "flicker": dict(self.flicker) if self.flicker is not None else None,
        }


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
        heat_pump: The optional heat-pump link's per-cycle view (B2,
            2026-07-12), or ``None`` when the link is not configured.

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
    heat_pump: HeatPumpRuntime | None = None

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

        # The global operating mode. Owned by this coordinator and exposed as
        # the home-mode select entity (v0.19.0, DECISIONS §27); restored from
        # the private setpoint Store on the first refresh (S9).
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

        # Canonical per-room control state (off / live). Seeded for
        # every configured room from the persisted state map; an unknown or
        # invalid persisted value falls back to the safe default (off).
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
        # The effective GLOBAL tuning (heat-pump water knobs are read from
        # here only — they are excluded from per-room overrides).
        self._global_config: ControllerConfig = controller_config
        self._building = (
            BuildingController(self._controller_configs)
            if self._controller_configs
            else None
        )

        # Optional heat-pump link (B2, 2026-07-12): the options section plus
        # the anti-flap bookkeeping of the direction sync (divergence counter,
        # the Tortoise mode at our last direction write, its timestamp).
        raw_hp: Any = entry.options.get(CONF_HEAT_PUMP, {})
        self._hp_options: dict[str, Any] = (
            dict(raw_hp) if isinstance(raw_hp, Mapping) else {}
        )
        # Optional GLOBAL manifold supply probe (S6 circulation gate,
        # 2026-07-13): a top-level options key set in options -> settings.
        self._global_supply_entity: str = str(
            entry.options.get(CONF_ENTITY_GLOBAL_SUPPLY, "") or ""
        )
        self._hp_divergence_cycles: int = 0
        self._hp_mode_at_last_write: Mode | None = None
        self._hp_last_mode_write_monotonic: float | None = None
        # Cooling setpoint-flicker state machine (issue #7, 2026-07-15). ONE
        # per entry, persisted across control cycles beside the other stateful
        # machines; rebuilt here on every config-entry reload (a knob change)
        # and NOT on a state-only room change (which never reloads). Reads its
        # global timing knobs off the effective global tuning (the §23 demand
        # threshold is read per cycle in _sync_heat_pump).
        self._flicker: SetpointFlicker = SetpointFlicker(self._global_config)

        # Source-entity read path (stale cache + plausibility gates) and the
        # actuator write path (thresholds + command caches), each with its own
        # private state (extracted 2026-07-10; see readers.py / writers.py).
        self._reader = SourceReader(hass)
        self._writer = CommandWriter(hass)

        # Per-room last-fresh-data timestamp (S6): feeds the core S5 watchdog
        # via RoomInputs.last_update_age_minutes. Seeded on first sight so a
        # room that never delivers data ages from integration startup.
        self._room_last_fresh: dict[str, datetime] = {}

        # Consecutive command-vs-feedback divergence cycles per room (S8).
        self._valve_mismatch_cycles: dict[str, int] = {}

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

        # Parked flag (K3, 2026-07-12): set by async_farewell_all — once the
        # unload farewell has parked the actuators, no later control cycle may
        # write commands again. Belt-and-braces behind the shutdown ordering
        # in async_unload_entry (a cycle that slipped into the unload window
        # used to re-open a farewell-parked cooling valve).
        self._parked: bool = False

    @property
    def _entity_cache(self) -> dict[str, tuple[float, datetime]]:
        """The reader's stale cache (read/write delegate).

        Kept after the 2026-07-10 :class:`SourceReader` extraction so existing
        white-box staleness tests keep observing (and seeding) the cache
        through the coordinator.
        """
        return self._reader.entity_cache

    # -- Setpoint / flag accessors (single source of truth) -----------------

    def get_home_temperature(self) -> float:
        """Return the global home target temperature [degC]."""
        return self._home_temperature_c

    @callback
    def set_home_temperature(self, value: float) -> None:
        """Set the global home target temperature and rebroadcast [degC].

        A non-finite value is rejected WITHOUT mutating state (K4,
        2026-07-12): HA's ``number.set_value`` min/max check lets ``NaN``
        through (every comparison with NaN is false), and a NaN setpoint
        poisoned every subsequent control cycle until a reload.

        Args:
            value: New home target temperature [degC].
        """
        numeric = float(value)
        if not math.isfinite(numeric):
            _LOGGER.warning("Rejecting non-finite home temperature %r (K4)", value)
            return
        self._home_temperature_c = numeric
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

        A non-finite value is rejected WITHOUT mutating state (K4,
        2026-07-12) — see :meth:`set_home_temperature`.

        Args:
            room_name: The room name.
            offset: New offset from the home temperature [K].
        """
        numeric = float(offset)
        if not math.isfinite(numeric):
            _LOGGER.warning(
                "Rejecting non-finite offset %r for room %s (K4)",
                offset,
                room_name,
            )
            return
        if room_name in self._room_offsets:
            self._room_offsets[room_name] = numeric
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
        """Set the global operating mode, persist it and rebroadcast.

        The single write path for every mode surface — the home-mode select
        entity, the panel/websocket and the ``set_mode`` service. The mode is
        persisted to the private setpoint Store (S9) so a restart in July does
        not silently fall back to heating logic. Like
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
            self.async_set_updated_data(replace(self.data, mode=mode.value))
        self._schedule_recompute()

    def get_room_state(self, room_name: str) -> str:
        """Return a room's control state (``off`` / ``live``).

        Args:
            room_name: The room name.

        Returns:
            The room's state string, or :data:`DEFAULT_ROOM_STATE` for a room the
            coordinator has no persisted state for.
        """
        return self._room_states.get(room_name, DEFAULT_ROOM_STATE)

    @callback
    def set_room_state(self, room_name: str, state: str) -> None:
        """Set a room's control state (``off`` / ``live``) and notify entities.

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
            # Farewell command (C5): leaving live (= switching off) orphans the
            # physical actuators — park them safely once before releasing
            # ownership.
            self._schedule_farewell(room_name)
        state_map: dict[str, Any] = dict(
            self.config_entry.options.get(CONF_ROOM_STATE, {})
        )
        state_map[room_name] = state
        self._persist_options({CONF_ROOM_STATE: state_map})
        # Refresh the entities (notably the control-state select) without
        # rebuilding the cached payload: the state lives on the coordinator,
        # not inside RoomRuntime.
        self.async_update_listeners()

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
        # Restore the persisted global mode (S9) — the Store is its only
        # source across a restart (v0.19.0 retired the external mode entity;
        # DECISIONS §27), so a July restart stays in cooling logic.
        raw_mode = stored.get(_STORE_KEY_MODE)
        if isinstance(raw_mode, str):
            try:
                self._mode = Mode(raw_mode)
            except ValueError:
                _LOGGER.warning("Ignoring invalid persisted mode %r", raw_mode)

    async def async_prune_room_setpoint(self, room_name: str) -> None:
        """Drop a removed room's offset from memory and the setpoint Store.

        Called by the options flow's remove-room leaf (D9, 2026-07-12).
        Operating on the coordinator's OWN Store instance (instead of a
        second, parallel ``Store`` object) removes the lost-update window in
        which the coordinator's pending delayed save re-wrote the pruned
        offset back; the direct ``async_save`` also flushes any such pending
        save with the room already gone.

        Args:
            room_name: The room being removed.
        """
        self._room_offsets.pop(room_name, None)
        await self._setpoint_store.async_save(self._setpoint_snapshot())

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
                setpoint_c=self.get_room_setpoint(name),
            )
        self.async_set_updated_data(replace(self.data, rooms=new_rooms))

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

    # -- Actuation self-test (S6/C, 2026-07-13) ------------------------------

    async def async_test_actuation(
        self,
        room_name: str,
        *,
        duration_s: float,
        cancel: bool = False,
    ) -> str | None:
        """Start (or cancel) a room's manual actuation self-test (S6/C).

        Adapter-level preconditions are validated here (the panel and the
        ``tortoise_ufh.test_actuation`` service share this one entry point);
        the core re-validates its own (mode, per-loop probes, dew headroom)
        in :meth:`~tortoise_ufh.core.controller.RoomController.begin_actuation_test`.
        A successful start/cancel schedules a debounced recompute so the
        excursion (or the return to the PI value) is written within seconds
        instead of at the next 5-minute cycle.

        Args:
            room_name: The room to test.
            duration_s: Test duration [s] (the service bounds it to 20-30
                minutes).
            cancel: ``True`` cancels a running test instead of starting one.

        Returns:
            ``None`` on success, else a refusal reason: ``"unknown_room"``,
            ``"not_ready"`` (no core controller yet), ``"room_not_live"``
            (only a LIVE room may move a physical valve), ``"no_probes"``
            (the room has no loop with BOTH water probes configured), or a
            core refusal (``"already_running"``, ``"mode_inactive"``,
            ``"dew_unsafe"``).
        """
        if room_name not in self._room_names:
            return "unknown_room"
        if self._building is None:
            return "not_ready"
        if cancel:
            self._building.cancel_actuation_test(room_name)
            self._schedule_recompute()
            return None
        if self.get_room_state(room_name) != ROOM_STATE_LIVE:
            return "room_not_live"
        room_cfg = next(
            cfg
            for cfg, name in zip(self._room_configs, self._room_names, strict=True)
            if name == room_name
        )
        supplies: list[str] = list(room_cfg.get(CONF_ENTITY_SUPPLY) or [])
        returns: list[str] = list(room_cfg.get(CONF_ENTITY_RETURN) or [])
        if not any(
            supplies[i] and returns[i] for i in range(min(len(supplies), len(returns)))
        ):
            return "no_probes"
        reason = self._building.begin_actuation_test(room_name, duration_s=duration_s)
        if reason is None:
            self._schedule_recompute()
        return reason

    async def async_shutdown(self) -> None:
        """Shut the coordinator down and flush the setpoint Store (K3/K5).

        Extends the base :class:`DataUpdateCoordinator` shutdown with the
        two entry-unload hardenings of 2026-07-12:

        * K3 — the pending debounced recompute is cancelled here too, so
          whichever teardown path runs first (the explicit call at the top of
          ``async_unload_entry`` or the ``entry.async_on_unload`` callbacks)
          leaves no timer alive inside the unload window.
        * K5 — a setpoint changed less than :data:`_SETPOINT_SAVE_DELAY_S`
          before an unload/reload sat only in the Store's delayed-save timer
          and was lost to the next coordinator; the snapshot is flushed
          synchronously now (only when the Store was actually loaded — a
          failed first refresh must not overwrite persisted values with the
          config-entry seeds).
        """
        self.async_cancel_recompute()
        await super().async_shutdown()
        if self._setpoints_loaded:
            await self._setpoint_store.async_save(self._setpoint_snapshot())

    # -- Update cycle -------------------------------------------------------

    async def _async_update_data(self) -> CoordinatorData:
        """Read sources, run the controller, store data and write commands.

        Returns:
            The freshly computed :class:`CoordinatorData`.
        """
        await self._ensure_setpoints_loaded()

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
        # Feed the core the REAL elapsed time since the previous step (clamped),
        # so an off-cycle debounced recompute integrates honestly instead of
        # assuming a full nominal cycle. On the first step there is no
        # reference, so fall back to the nominal cycle length. Computed once
        # here so the SAME dt advances the core step AND the cooling
        # setpoint-flicker's tick (issue #7).
        dt_seconds: float = self._cycle_seconds
        if self._building is not None and inputs:
            now_monotonic = time.monotonic()
            if self._last_step_monotonic is None:
                dt_seconds = self._cycle_seconds
            else:
                raw_dt = now_monotonic - self._last_step_monotonic
                dt_seconds = min(_MAX_DT_SECONDS, max(_MIN_DT_SECONDS, raw_dt))
                if raw_dt > _MAX_DT_SECONDS:
                    # R2-F6 (2026-07-12): the temperature kept moving for
                    # LONGER than the clamped dt the core is about to see —
                    # the next raw dT/dt sample would be inflated and the
                    # ~15-min EMA would carry the artefact for 2-3 cycles.
                    self._building.invalidate_trends()
            self._last_step_monotonic = now_monotonic
            # S6 (2026-07-13): the optional global manifold supply probe
            # feeds the building-level circulation gate; None when unset
            # or unreadable (the per-loop witnesses still apply).
            global_supply = self._reader.read_float_state(
                self._global_supply_entity or None
            )
            try:
                building_outputs = self._building.step(
                    inputs,
                    dt_seconds=dt_seconds,
                    global_supply_temperature_c=global_supply,
                )
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
                setpoint_c=self.get_room_setpoint(name),
            )

        # Only LIVE rooms write; OFF rooms are computed against Mode.OFF and
        # never written. A coordinator whose unload farewell
        # has already parked the actuators writes NOTHING any more (K3,
        # 2026-07-12) — a cycle sneaking into the unload window used to
        # re-open a farewell-parked cooling valve.
        if self._parked:
            _LOGGER.debug("Coordinator parked by the unload farewell; skipping writes")
        else:
            for room_cfg, name in zip(
                self._room_configs, self._room_names, strict=True
            ):
                runtime = rooms.get(name)
                if runtime is None or self.get_room_state(name) != ROOM_STATE_LIVE:
                    continue
                await self._write_valves(room_cfg, name, runtime.outputs, inputs[name])
                if await self._write_fast_source(room_cfg, name, runtime.outputs):
                    # Dry assist demoted to OFF: the climate entity does not
                    # advertise a "dry" hvac mode (§24). Surface it on the
                    # room like the S8 valve_mismatch merge above.
                    report = replace(
                        runtime.report,
                        flags=tuple(
                            dict.fromkeys((*runtime.report.flags, "dry_unsupported"))
                        ),
                    )
                    outputs_flagged = replace(runtime.outputs, report=report)
                    rooms[name] = RoomRuntime(
                        outputs=outputs_flagged,
                        report=report,
                        setpoint_c=runtime.setpoint_c,
                    )

        # Optional heat-pump link (B2): computed AND written (when gated) at
        # the end of the cycle so the freshly computed global safe dew point
        # feeds the cooling setpoint. A link failure never breaks the cycle.
        heat_pump: HeatPumpRuntime | None = None
        try:
            heat_pump = await self._sync_heat_pump(
                global_dew,
                room_outputs=list(outputs_by_room.values()),
                dt_seconds=dt_seconds,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Heat-pump link sync failed")

        return CoordinatorData(
            rooms=rooms,
            global_safe_dew_point_c=global_dew,
            algorithm_status=algorithm_status,
            watchdog_state=watchdog_state,
            last_update_timestamp=now.isoformat(),
            mode=self._mode.value,
            sensor_lost_rooms=sensor_lost_rooms,
            heat_pump=heat_pump,
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
        # fast source. Only a LIVE room's commands are written (gated in
        # _async_update_data).
        participates = self.get_room_state(name) != ROOM_STATE_OFF
        mode = self._mode if participates else Mode.OFF
        # Validate humidity independently: an out-of-range reading only nulls
        # the dew-point input rather than degrading the whole room. A stale
        # (frozen-but-present) humidity is the single most dangerous input in
        # cooling — both condensation defences trust it — so it carries a
        # TWO-stage age gate (C4 + K7 2026-07-12): fresh <= 60 min, held to
        # 120 min with a linear staleness fraction (D5 — the core pads its
        # dew points by frac * 1 K), unusable beyond.
        humidity, humidity_stale_frac = self._reader.read_humidity(
            room_cfg.get(CONF_ENTITY_HUMIDITY)
        )
        if humidity is not None and not 0.0 <= humidity <= 100.0:
            humidity = None
            humidity_stale_frac = 0.0
        # S6: per-room data age for the core S5 watchdog. A fresh temperature
        # resets the clock; otherwise the age grows from the last fresh sample
        # (or from the first time the room was ever seen).
        room_temp = self._reader.read_room_temperature(
            room_cfg.get(CONF_ENTITY_TEMP_ROOM)
        )
        now = datetime.now(UTC)
        if room_temp is not None:
            self._room_last_fresh[name] = now
        last_fresh = self._room_last_fresh.setdefault(name, now)
        age_minutes = max(0.0, (now - last_fresh).total_seconds() / 60.0)
        # Fast-source feedback: on/off + the raw HVAC mode (K4 — the core's
        # S4 reconciliation sees DIRECTION divergences on shared aggregates).
        fast_entity = room_cfg.get(CONF_ENTITY_FAST_SOURCE)
        fast_on = self._reader.read_fast_source_on(fast_entity)
        fast_hvac = self._reader.read_fast_source_hvac_mode(fast_entity)
        # K10/R5 (2026-07-12): an ON feedback younger than one cycle after
        # this entity's farewell OFF is almost certainly the STALE pre-parking
        # state — after a reload the rebuilt machine would adopt it and write
        # ON seconds after the farewell OFF. Read it as OFF; a genuinely
        # running unit re-surfaces on the next cycle (mismatch + re-assert).
        if fast_on and self._writer.recent_farewell(
            fast_entity, max_age_s=self._cycle_seconds
        ):
            fast_on = False
            fast_hvac = None
        try:
            loops = self._build_loops(room_cfg)
            return RoomInputs(
                mode=mode,
                setpoint_c=setpoint,
                room_temperature_c=room_temp,
                humidity_pct=humidity,
                outdoor_temperature_c=self._reader.read_float_state(
                    room_cfg.get(CONF_ENTITY_TEMP_OUTDOOR)
                ),
                loops=loops,
                fast_source_kind=self._reader.read_fast_source_kind(
                    room_cfg.get(CONF_FAST_SOURCE_KIND)
                ),
                fast_source_on=fast_on,
                # The global heat-pump-link entity feeds every room (B2); a
                # legacy per-room key of the same name still overrides it.
                hp_active_for_ufh=self._reader.read_hp_active_for_ufh(
                    str(room_cfg.get(CONF_ENTITY_HP_ACTIVE) or "")
                    or str(self._hp_options.get(CONF_ENTITY_HP_ACTIVE) or "")
                ),
                cooling_enabled=cooling_enabled,
                last_update_age_minutes=age_minutes,
                # K1 (2026-07-12): only a LIVE room votes in the multisplit
                # group arbitration. An OFF room is fed Mode.OFF and never
                # writes, but its direction machine (with dwell timers) still
                # exists — an empty group keeps it from voting in or pinning
                # the group's arbitration: belt and braces.
                fast_source_group=(
                    str(room_cfg.get(CONF_FAST_SOURCE_GROUP, "") or "")
                    if self.get_room_state(name) == ROOM_STATE_LIVE
                    else ""
                ),
                fast_source_hvac_mode=fast_hvac,
                humidity_stale_frac=humidity_stale_frac,
                # Quiet hours (B1): the adapter evaluates the room's allowed-
                # hours window against HA's LOCAL clock; the core only sees
                # the verdict. Honoured at control-cycle granularity (5 min)
                # — the dwell timers weigh more than minute-exact edges.
                fast_source_allowed=self._fast_source_allowed(room_cfg),
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
                    supply_temperature_c=self._reader.read_float_state(
                        supplies[i] if i < len(supplies) else None
                    ),
                    return_temperature_c=self._reader.read_float_state(
                        returns[i] if i < len(returns) else None
                    ),
                )
            )
        return tuple(loops)

    def _fast_source_allowed(self, room_cfg: dict[str, Any]) -> bool:
        """Evaluate a room's quiet-hours window against HA's local clock (B1).

        The pair ``fast_source_window_start`` / ``fast_source_window_end``
        (``"HH:MM"``) is the window in which the room's fast source MAY run;
        it may cross midnight. Missing / partial / unparseable configuration
        fails OPEN (always allowed) — quiet hours are a comfort feature, not
        a safety rule.

        Args:
            room_cfg: The room's configuration dict.

        Returns:
            ``True`` when the fast source is allowed to run right now.
        """
        start_raw = str(room_cfg.get(CONF_FAST_WINDOW_START, "") or "")
        end_raw = str(room_cfg.get(CONF_FAST_WINDOW_END, "") or "")
        if not start_raw or not end_raw:
            return True
        start = _parse_minute_of_day(start_raw)
        end = _parse_minute_of_day(end_raw)
        if start is None or end is None or start == end:
            _LOGGER.debug(
                "Ignoring invalid quiet-hours window %r-%r", start_raw, end_raw
            )
            return True
        # dt_util.now() is HA's LOCAL time (the household wall clock).
        now_local = dt_util.now()
        return window_allows(now_local.hour * 60 + now_local.minute, start, end)

    # -- Internal: optional heat-pump link (B2, 2026-07-12) -------------------

    def _outdoor_entity(self) -> str | None:
        """Return the first configured outdoor-temperature entity id, if any."""
        for room_cfg in self._room_configs:
            entity = str(room_cfg.get(CONF_ENTITY_TEMP_OUTDOOR, "") or "")
            if entity:
                return entity
        return None

    def _match_select_option(self, entity_id: str, option: str) -> str | None:
        """Canonicalise ``option`` to the select entity's OWN option list.

        HeishaMon builds differ in case/whitespace; ``select.select_option``
        with an unknown option raises inside HA, so the core's canonical
        string is matched case-insensitively against the live entity's
        ``options`` attribute and the ENTITY's exact spelling is returned.

        Args:
            entity_id: The pump-mode select entity id.
            option: The canonical HeishaMon option the core computed.

        Returns:
            The entity's exact option string, or ``None`` when the entity
            offers no match (the caller skips the write and logs).
        """
        options = self._reader.read_select_options(entity_id)
        key = option.strip().lower()
        for candidate in options:
            if candidate.strip().lower() == key:
                return candidate
        return None

    @property
    def _hp_writes_enabled(self) -> bool:
        """Whether the heat-pump link may write this cycle.

        A GLOBAL actuator may only be touched while somebody has handed
        Tortoise the controls: not parked by the unload farewell AND at least
        one room is LIVE.
        """
        return not self._parked and any(
            self.get_room_state(name) == ROOM_STATE_LIVE for name in self._room_names
        )

    async def _sync_heat_pump(
        self,
        global_dew: float | None,
        *,
        room_outputs: list[RoomOutputs],
        dt_seconds: float,
    ) -> HeatPumpRuntime | None:
        """Compute (and, when gated, write) the heat-pump link's cycle (B2).

        Direction sync: ``desired = direction_option(mode, current)`` — the
        DHW flag is always preserved, ``"DHW only"`` is a hard skip, and
        TRANSITIONAL / OFF never force a direction. Writes are rare: only on
        a Tortoise mode change or a divergence persisting for at least
        :data:`_HP_DIVERGENCE_CYCLES` cycles, never more often than
        :data:`_HP_MODE_MIN_REWRITE_S`.

        Water setpoints: cooling = ``max(cooling_supply_base_c, global safe
        dew point)`` while COOLING; heating = the optional weather curve
        while HEATING (skipped without an outdoor reading — the pump keeps
        its last setpoint, bounded by its own firmware limits). Both go
        through :meth:`CommandWriter.write_hp_setpoint` (0.5 K threshold +
        45-min re-assert).

        Cooling setpoint-flicker (issue #7): when enabled AND the cooling
        setpoint entity is configured, the :class:`~tortoise_ufh.hp_link.
        SetpointFlicker` may drop the WRITTEN cooling setpoint to the dew-safe
        pulse floor for one cycle to trip the pump's compressor out of its
        fixed 3 K return deadband, then restore it — a tighter effective
        deadband (colder average water) while the return stays dew-safe. The
        flicker acts only when the calling rooms' loop-weighted valve opening
        clears the ``hp_flicker_min_open_pct`` demand gate (DECISIONS §23) —
        smaller draws are covered by the buffer tank without a forced start.

        Args:
            global_dew: This cycle's global safe dew point [degC], or ``None``.
            room_outputs: The per-room outputs of this cycle (final valve
                command + report — both feed the flicker's demand gate).
            dt_seconds: The real measured control-step interval [s] — the SAME
                value the core step used, advancing the flicker clock ONCE.

        Returns:
            The :class:`HeatPumpRuntime` payload, or ``None`` when the link is
            entirely unconfigured.
        """
        cfg = self._hp_options
        mode_entity = str(cfg.get(CONF_ENTITY_HP_MODE) or "")
        heat_entity = str(cfg.get(CONF_ENTITY_HP_HEATING_SETPOINT) or "")
        cool_entity = str(cfg.get(CONF_ENTITY_HP_COOLING_SETPOINT) or "")
        active_entity = str(cfg.get(CONF_ENTITY_HP_ACTIVE) or "")
        if not (mode_entity or heat_entity or cool_entity or active_entity):
            return None
        writes_enabled = self._hp_writes_enabled

        # -- Direction sync ---------------------------------------------------
        current: str | None = None
        desired: str | None = None
        in_sync: bool | None = None
        if mode_entity:
            current = self._reader.read_select_option(mode_entity)
            desired = direction_option(self._mode, current)
            if desired is not None and current is not None:
                in_sync = desired.strip().lower() == current.strip().lower()
            if in_sync is False:
                self._hp_divergence_cycles += 1
            else:
                self._hp_divergence_cycles = 0
            now_monotonic = time.monotonic()
            may_rewrite = (
                self._hp_last_mode_write_monotonic is None
                or now_monotonic - self._hp_last_mode_write_monotonic
                >= _HP_MODE_MIN_REWRITE_S
            )
            # Coordinator start counts as a mode change on purpose
            # (_hp_mode_at_last_write is None): the first detected divergence
            # after an HA restart is asserted IMMEDIATELY as the initial
            # synchronisation, not held for the anti-flap divergence count.
            triggered = (
                self._hp_mode_at_last_write is not self._mode
                or self._hp_divergence_cycles >= _HP_DIVERGENCE_CYCLES
            )
            if writes_enabled and in_sync is False and triggered and may_rewrite:
                assert desired is not None  # narrowed by in_sync is False
                target = self._match_select_option(mode_entity, desired)
                if target is None:
                    _LOGGER.warning(
                        "Heat-pump mode entity %s offers no option matching %r; "
                        "skipping the direction write",
                        mode_entity,
                        desired,
                    )
                elif await self._writer.write_hp_mode(mode_entity, target):
                    self._hp_last_mode_write_monotonic = now_monotonic
                    self._hp_mode_at_last_write = self._mode
                    self._hp_divergence_cycles = 0

        current_key = (current or "").strip().lower()
        dhw_active = "dhw" in current_key
        dhw_only = current_key == "dhw only"

        # -- Cooling water setpoint (+ optional setpoint-flicker, #7) ----------
        cooling: dict[str, Any] | None = None
        flicker_payload: dict[str, Any] | None = None
        if cool_entity:
            base = self._global_config.cooling_supply_base_c
            # The normal ("dew-safe") cooling target, computed unconditionally
            # so a mid-pulse restore can write it even when the mode has just
            # flipped out of COOLING (issue #7).
            normal_target = cooling_setpoint_c(base, global_dew)
            cool_target: float | None = None

            flicker_enabled = self._global_config.hp_flicker_enabled
            return_entity = str(cfg.get(CONF_ENTITY_HP_RETURN_TEMP) or "")
            freq_entity = str(cfg.get(CONF_ENTITY_HP_COMPRESSOR_FREQ) or "")
            outlet_entity = str(cfg.get(CONF_ENTITY_HP_OUTLET_TEMP) or "")
            return_c = self._reader.read_float_state(return_entity or None)
            freq_hz = self._reader.read_float_state(freq_entity or None)
            outlet_c = self._reader.read_float_state(outlet_entity or None)

            # Loop-weighted demand gate (§23) — computed even with the flicker
            # disabled, so the panel shows the live Σ before it is switched on.
            demand_gate = cooling_demand(
                room_outputs,
                min_open_pct=self._global_config.hp_flicker_min_open_pct,
            )

            decision = None
            if flicker_enabled:
                # Advance the flicker clock EXACTLY ONCE per cycle with the
                # SAME real dt the core step used, then decide (issue #7).
                self._flicker.tick(dt_seconds)
                decision = self._flicker.step(
                    cooling_active=(
                        self._mode is Mode.COOLING and writes_enabled and not dhw_active
                    ),
                    demand=demand_gate.demand,
                    hp_return_c=return_c,
                    compressor_freq_hz=freq_hz,
                    written_target_c=normal_target,
                    safe_dew_c=global_dew,
                    step_c=self._writer.hp_setpoint_step(cool_entity),
                )

            if decision is not None and decision.restore_pending:
                # A pulse was in flight and the normal cooling write below may
                # NOT run this cycle — the mode flipped out of COOLING, OR a
                # whole-home stop landed (mode is STILL COOLING but
                # writes_enabled is now False, since the mode comes from the
                # mode entity, not the room states). The restore is therefore
                # the FIRST branch and UNCONDITIONAL of both mode and
                # writes_enabled, so the pump is never left parked at the
                # raw-dew pulse floor (spec §4; mirrors the C5 farewell
                # philosophy: the safe value must always land).
                cool_target = normal_target
                await self._writer.write_hp_setpoint(cool_entity, normal_target)
            elif self._mode is Mode.COOLING:
                cool_target = normal_target
                if writes_enabled:
                    # The pulse drop and its restore are each >= one grid step
                    # (>= 0.5 K on a real pump), so both cross the writer's
                    # 0.5 K / 45-min skip — no throttle change is required.
                    value = (
                        decision.pulse_target_c
                        if decision is not None and decision.pulse_target_c is not None
                        else normal_target
                    )
                    await self._writer.write_hp_setpoint(cool_entity, value)

            cooling = {
                "entity_id": cool_entity,
                "target_c": cool_target,
                "base_c": base,
                "safe_dew_c": global_dew,
            }
            if flicker_enabled or return_entity or freq_entity or outlet_entity:
                flicker_payload = self._flicker_payload(
                    decision, flicker_enabled, return_c, freq_hz, outlet_c, demand_gate
                )

        # -- Heating water setpoint --------------------------------------------
        heating: dict[str, Any] | None = None
        if heat_entity:
            curve = heating_curve(self._global_config)
            t_out = self._reader.read_float_state(self._outdoor_entity())
            heat_target: float | None = None
            if self._mode is Mode.HEATING and t_out is not None:
                heat_target = curve.t_supply(t_out)
                if writes_enabled:
                    await self._writer.write_hp_setpoint(heat_entity, heat_target)
            heating = {
                "entity_id": heat_entity,
                "target_c": heat_target,
                "t_out_c": t_out,
                "base_c": curve.t_supply_base,
                "slope": curve.slope,
                "neutral_c": curve.t_neutral,
            }

        hp_active = (
            self._reader.read_hp_active_for_ufh(active_entity)
            if active_entity
            else None
        )
        return HeatPumpRuntime(
            mode_entity_id=mode_entity or None,
            current_option=current,
            desired_option=desired,
            in_sync=in_sync,
            dhw_active=dhw_active,
            dhw_only=dhw_only,
            cooling=cooling,
            heating=heating,
            hp_active=hp_active,
            hp_active_configured=bool(active_entity),
            writes_enabled=writes_enabled,
            flicker=flicker_payload,
        )

    @staticmethod
    def _flicker_payload(
        decision: FlickerDecision | None,
        enabled: bool,
        return_c: float | None,
        freq_hz: float | None,
        outlet_c: float | None,
        demand_gate: CoolingDemand,
    ) -> dict[str, Any]:
        """Build the JSON flicker payload for the runtime / panel (issue #7).

        Args:
            decision: This cycle's :class:`~tortoise_ufh.hp_link.
                FlickerDecision`, or ``None`` when the flicker is disabled
                (only its diagnostic sensors are configured).
            enabled: Whether the flicker master switch is on.
            return_c: The pump inlet/return temperature [degC], or ``None``.
            freq_hz: The compressor frequency [Hz], or ``None``.
            outlet_c: The pump outlet temperature [degC] (diagnostic only), or
                ``None``.
            demand_gate: This cycle's loop-weighted demand aggregate (§23) —
                surfaced even when the flicker is disabled, so the threshold
                can be tuned against the live Σ before switching it on.

        Returns:
            A JSON-serializable dict of the flicker's cycle view.
        """
        return {
            "enabled": enabled,
            "state": decision.state if decision is not None else "idle",
            "flags": list(decision.flags) if decision is not None else [],
            "trigger_c": decision.trigger_c if decision is not None else None,
            "stuck_remaining_s": (
                decision.stuck_remaining_s if decision is not None else None
            ),
            "cooldown_remaining_s": (
                decision.cooldown_remaining_s if decision is not None else None
            ),
            "pulses_last_hour": (
                decision.pulses_last_hour if decision is not None else 0
            ),
            "last_pulse_target_c": (
                decision.last_pulse_target_c if decision is not None else None
            ),
            "pulse_target_c": (
                decision.pulse_target_c if decision is not None else None
            ),
            "return_c": return_c,
            "compressor_freq_hz": freq_hz,
            "outlet_c": outlet_c,
            "demand_open_pct": demand_gate.open_pct,
            "demand_threshold_pct": demand_gate.threshold_pct,
        }

    async def async_set_hp_dhw(self, want_dhw: bool) -> str:
        """Add/remove the pump's ``+DHW`` flag on the user's request (B2).

        The manual DHW switch is an explicit user action, so it is NOT gated
        by the live-rooms write gate. The direction part of the option is
        never changed, so this cannot race the direction sync; the external
        DHW automation remains the flag's owner and may overwrite it at any
        time (its right — documented in the panel).

        Args:
            want_dhw: ``True`` to add the ``+DHW`` flag, ``False`` to remove.

        Returns:
            The option actually selected (or already active).

        Raises:
            HpNotConfiguredError: When no pump-mode entity is configured.
            HpDhwUnavailableError: When the toggle cannot be honoured (pump in
                ``"DHW only"`` with a removal request, unknown/unavailable
                option, or the write failed).
        """
        mode_entity = str(self._hp_options.get(CONF_ENTITY_HP_MODE) or "")
        if not mode_entity:
            msg = "heat-pump mode entity is not configured"
            raise HpNotConfiguredError(msg)
        current = self._reader.read_select_option(mode_entity)
        desired = dhw_option(current, want_dhw)
        if desired is None:
            msg = f"cannot toggle DHW from option {current!r}"
            raise HpDhwUnavailableError(msg)
        if current is not None and desired.strip().lower() == current.strip().lower():
            return current  # already in the requested state — nothing to write
        target = self._match_select_option(mode_entity, desired)
        if target is None:
            msg = f"mode entity offers no option matching {desired!r}"
            raise HpDhwUnavailableError(msg)
        if not await self._writer.write_hp_mode(mode_entity, target):
            msg = "select.select_option call failed"
            raise HpDhwUnavailableError(msg)
        return target

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
            written = self._writer.last_written_valve(valve_entity)
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
        self,
        room_cfg: dict[str, Any],
        name: str,
        outputs: RoomOutputs,
        room_inputs: RoomInputs | None = None,
    ) -> None:
        """Write the room's valve command through the :class:`CommandWriter`.

        Thin delegate: resolves the room's valve entity list and its
        ``valve_write_threshold_pct``, then hands off to
        :meth:`CommandWriter.write_valves` (which owns the per-entity write
        threshold cache, the re-assert clock and the domain dispatch). When
        ``room_inputs`` is given, the per-entity feedback read this cycle
        rides along (issue #4, 2026-07-13) using the SAME
        valve-index-to-loop alignment as :meth:`_track_valve_mismatch`, so
        the writer can rewrite an entity whose reported position diverged
        from the cached command (external controller reset to its park
        position). ``room_inputs`` is optional so the threshold / re-assert
        write path can be exercised without assembling inputs; then the
        feedback-divergence trigger simply does not fire this call.

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.
            outputs: The room's controller outputs.
            room_inputs: The room's inputs assembled this cycle (feedback);
                ``None`` skips the feedback-divergence rewrite trigger.
        """
        valves: list[str] = list(room_cfg.get(CONF_ENTITY_VALVES) or [])
        if not valves:
            return
        feedback: list[float | None] | None = None
        if room_inputs is not None:
            feedback = [
                room_inputs.loops[i].valve_position_pct
                if i < len(room_inputs.loops)
                else None
                for i in range(len(valves))
            ]
        await self._writer.write_valves(
            valves,
            name,
            outputs,
            threshold_pct=self._controller_configs[name].valve_write_threshold_pct,
            feedback_pct=feedback,
        )

    async def _write_fast_source(
        self, room_cfg: dict[str, Any], name: str, outputs: RoomOutputs
    ) -> bool:
        """Write the room's fast-source command through the writer.

        Thin delegate to :meth:`CommandWriter.write_fast_source` (which owns
        the S3 command cache and the periodic re-assert).

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.
            outputs: The room's controller outputs.

        Returns:
            ``True`` when a DRY command was demoted to OFF because the climate
            entity advertises no dry mode (§24) — the caller flags the room
            with ``dry_unsupported``.
        """
        return await self._writer.write_fast_source(
            room_cfg.get(CONF_ENTITY_FAST_SOURCE), name, outputs
        )

    # -- Internal: farewell command (live -> off, unload) --------------------

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

        Thin delegate to :meth:`CommandWriter.farewell_room` (split always OFF;
        valve driven to 0 in COOLING, left holding in HEATING), passing the
        coordinator's current global mode. Afterwards the CORE machine is
        synchronised with the out-of-band OFF (K10, 2026-07-12): without it
        the direction machine kept emitting ON after the farewell, so a
        return to live could write ON seconds after the farewell OFF — now
        the way back passes through an honest min-OFF dwell.

        Args:
            room_cfg: The room's configuration dict.
            name: The room name.
        """
        await self._writer.farewell_room(
            room_cfg.get(CONF_ENTITY_FAST_SOURCE),
            list(room_cfg.get(CONF_ENTITY_VALVES) or []),
            name,
            mode=self._mode,
        )
        if self._building is not None:
            self._building.notify_fast_source_farewell(name)

    async def async_farewell_all(self) -> None:
        """Park every live room's actuators (called on config-entry unload).

        Also raises the permanent ``_parked`` flag (K3, 2026-07-12): from
        this point on the coordinator computes and reports but writes no
        commands, so nothing that fires inside the unload window can undo
        the parking. The flag lives for the rest of this instance's life —
        a reload builds a fresh coordinator.
        """
        for room_cfg, name in zip(self._room_configs, self._room_names, strict=True):
            if self.get_room_state(name) == ROOM_STATE_LIVE:
                await self._async_farewell_room(room_cfg, name)
        self._parked = True

    # -- Internal: entity reads ---------------------------------------------

    def _read_valve_position(self, entity_id: str | None) -> float | None:
        """Read a valve actuator's position [0..100 %] through the reader.

        Thin delegate to :meth:`SourceReader.read_valve_position` (domain
        dispatch + per-loop S8 plausibility), kept on the coordinator after
        the 2026-07-10 extraction so existing white-box tests keep exercising
        the read path through the coordinator.

        Args:
            entity_id: The valve actuator entity id, or ``None`` / empty when
                the loop has no valve at this position.

        Returns:
            The reported position [0..100 %], or ``None`` when it cannot be
            read.
        """
        return self._reader.read_valve_position(entity_id)
