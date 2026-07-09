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
from dataclasses import dataclass
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

    Raises:
        ValueError: If ``algorithm_status``, ``watchdog_state`` or ``mode`` is
            not a recognised value.
    """

    rooms: dict[str, RoomRuntime]
    global_safe_dew_point_c: float | None
    algorithm_status: str
    watchdog_state: str
    last_update_timestamp: str | None
    mode: str

    def __post_init__(self) -> None:
        """Validate the enumerated status/mode fields."""
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

        # Last value written to each valve entity (for the write threshold).
        self._last_written_valve: dict[str, float] = {}

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
        """Override the global operating mode and rebroadcast.

        Args:
            mode: New global :class:`~tortoise_ufh.models.Mode`.
        """
        self._mode = mode
        if self.data is not None:
            self.async_set_updated_data(
                CoordinatorData(
                    rooms=self.data.rooms,
                    global_safe_dew_point_c=self.data.global_safe_dew_point_c,
                    algorithm_status=self.data.algorithm_status,
                    watchdog_state=self.data.watchdog_state,
                    last_update_timestamp=self.data.last_update_timestamp,
                    mode=mode.value,
                )
            )

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
        self._room_states[room_name] = state
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
        """Return the current setpoint state to persist to the Store."""
        return {
            CONF_HOME_SETPOINT: self._home_temperature_c,
            CONF_ROOM_OFFSET: dict(self._room_offsets),
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
        self._mode = self._read_mode()

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
        for name in self._room_names:
            room_outputs = outputs_by_room.get(name)
            if room_outputs is None:
                continue
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
        # the dew-point input rather than degrading the whole room.
        humidity = self._read_float_state(room_cfg.get(CONF_ENTITY_HUMIDITY))
        if humidity is not None and not 0.0 <= humidity <= 100.0:
            humidity = None
        try:
            loops = self._build_loops(room_cfg)
            return RoomInputs(
                mode=mode,
                setpoint_c=setpoint,
                room_temperature_c=self._read_float_state(
                    room_cfg.get(CONF_ENTITY_TEMP_ROOM)
                ),
                humidity_pct=humidity,
                outdoor_temperature_c=self._read_float_state(
                    room_cfg.get(CONF_ENTITY_TEMP_OUTDOOR)
                ),
                loops=loops,
                fast_source_kind=self._read_fast_source_kind(room_cfg),
                fast_source_on=self._read_fast_source_on(room_cfg),
                hp_active_for_ufh=self._read_hp_active_for_ufh(room_cfg),
                cooling_enabled=cooling_enabled,
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
            return self._read_float_state(entity_id)
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
        return value if math.isfinite(value) else None

    def _read_float_state(self, entity_id: str | None) -> float | None:
        """Read a numeric entity state with a short stale cache.

        On a successful read the value is cached with the current time. When the
        entity is unavailable/unknown the cached value is returned if it is
        younger than :data:`ENTITY_STALE_MAX_SECONDS`; otherwise ``None``.

        Args:
            entity_id: The source entity id, or ``None`` / empty for "not
                configured".

        Returns:
            The numeric value, or ``None`` when unreadable and no fresh cache
            exists.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
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
