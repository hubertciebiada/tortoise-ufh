"""Panel websocket API for the Tortoise-UFH Home Assistant adapter.

This module exposes the read/write commands the self-contained frontend panel
(``frontend/tortoise-ufh-panel.js``) uses to render the per-room table and the
under-the-hood report, and to mutate the writable state (home temperature,
per-room offset, per-room control state, global mode).

Registration is process-wide: :func:`async_register_ws` is called exactly once
per Home Assistant instance (guarded by the caller in ``__init__.py``). Every
command is an admin-guarded, synchronous ``@callback`` websocket handler that
resolves the single :class:`~custom_components.tortoise_ufh.coordinator.
TortoiseUfhCoordinator` from the loaded config entries and replies through
``connection.send_result`` / ``connection.send_error``.

The commands mutate the coordinator's authoritative setpoint / state
(``set_home_temperature`` / ``set_room_offset`` / ``set_room_state`` /
``set_mode``); the coordinator rebroadcasts the cached payload immediately where
relevant so entities and the panel see the change before the next 5-minute
refresh.

Units: temperatures in degrees Celsius (``_c``); offsets in kelvin (``_c`` as a
delta); valve position and humidity in percent (0..100). This module holds no
physical control logic of its own.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .config_flow import CONF_CONTROLLER
from .const import (
    CONF_COOLING_ENABLED,
    CONF_ENTITY_FAST_SOURCE,
    CONF_ENTITY_HUMIDITY,
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_TEMP_OUTDOOR,
    CONF_ENTITY_TEMP_ROOM,
    CONF_ENTITY_VALVES,
    CONF_FAST_SOURCE_KIND,
    CONF_ROOM_AREA,
    CONF_ROOM_NAME,
    CONF_ROOM_TUNING,
    CONF_ROOMS,
    CONTROLLER_BOOL_KNOB,
    CONTROLLER_KNOB_UNITS,
    CONTROLLER_NUMBER_KNOBS,
    DEFAULT_COOLING_ENABLED,
    DEFAULT_FAST_SOURCE_KIND,
    DOMAIN,
    FAST_SOURCE_KINDS,
    HOME_SETPOINT_MAX_C,
    HOME_SETPOINT_MIN_C,
    MODE_OPTIONS,
    ROOM_OFFSET_MAX_C,
    ROOM_OFFSET_MIN_C,
    ROOM_STATES,
)
from .core.config import ControllerConfig
from .core.models import Mode

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import TortoiseUfhCoordinator

# --- Command type strings ---------------------------------------------------

WS_GET_CONFIG: str = f"{DOMAIN}/get_config"
WS_GET_LIVE: str = f"{DOMAIN}/get_live"
WS_SET_HOME_TEMPERATURE: str = f"{DOMAIN}/set_home_temperature"
WS_SET_ROOM_OFFSET: str = f"{DOMAIN}/set_room_offset"
WS_SET_ROOM_STATE: str = f"{DOMAIN}/set_room_state"
WS_SET_MODE: str = f"{DOMAIN}/set_mode"
WS_GET_TUNING: str = f"{DOMAIN}/get_tuning"
WS_SET_TUNING: str = f"{DOMAIN}/set_tuning"

# Websocket error codes.
_ERR_NOT_FOUND: str = "not_found"
_ERR_UNKNOWN_ROOM: str = "unknown_room"
_ERR_INVALID_SCOPE: str = "invalid_scope"
_ERR_INVALID_TUNING: str = "invalid_tuning"

# Scope keyword for the *global* controller tuning (not a room name).
_TUNING_SCOPE_GLOBAL: str = "global"

# Per-room diagnostic ``sensor`` keys whose registered entity ids ``get_config``
# resolves so the panel can pull recorder history / statistics for its charts.
# This is the numeric/chartable subset of ``sensor.ROOM_SENSORS`` (the ``str``
# sensors ``fast_source_mode`` / ``explanation`` are intentionally excluded).
# Kept as a literal tuple here — rather than importing the ``sensor`` platform —
# to avoid a websocket -> platform import dependency; keys mirror ``sensor.py``.
_DIAGNOSTIC_SENSOR_KEYS: tuple[str, ...] = (
    "recommended_valve",
    "error_c",
    "trend_c_per_h",
    "room_dew_point",
    "i_term",
    "trend_term",
)

# Global safe dew-point ``sensor`` key (see ``sensor.GLOBAL_SENSORS``); the
# global unique id carries no room segment.
_GLOBAL_DEW_POINT_KEY: str = "global_safe_dew_point"


# ---------------------------------------------------------------------------
# Frozen result / view dataclasses (JSON-serializable via ``to_dict``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoomConfigView:
    """One room's static configuration as returned by ``get_config``.

    Attributes:
        name: Room name (non-empty).
        area_m2: Floor area in square metres (>= 0).
        offset_c: Per-room offset from the home setpoint in kelvin.
        cooling_enabled: Whether the room participates in floor cooling.
        control_state: The room's control state (one of :data:`ROOM_STATES`:
            ``off`` / ``shadow`` / ``live``).
        fast_source_kind: Configured fast-source kind (one of
            :data:`FAST_SOURCE_KINDS`).
        entities: Assigned source entities keyed by their ``CONF_*`` name;
            values are entity-id strings, lists of strings, or ``None``.
        diagnostic_entities: Registered diagnostic ``sensor`` entity ids keyed by
            their sensor ``key`` (subset of :data:`_DIAGNOSTIC_SENSOR_KEYS`), for
            the panel to chart via recorder history / statistics. Contains only
            keys the entity registry resolved; empty when none are registered.

    Raises:
        ValueError: If ``name`` is empty, ``area_m2`` is negative or not finite,
            ``offset_c`` is not finite, ``control_state`` is unrecognised, or
            ``fast_source_kind`` is unrecognised.
    """

    name: str
    area_m2: float
    offset_c: float
    cooling_enabled: bool
    control_state: str
    fast_source_kind: str
    entities: dict[str, Any] = field(default_factory=dict)
    diagnostic_entities: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the room-config view fields."""
        if not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if not math.isfinite(self.area_m2) or self.area_m2 < 0.0:
            msg = f"area_m2 must be finite and >= 0, got {self.area_m2}"
            raise ValueError(msg)
        if not math.isfinite(self.offset_c):
            msg = f"offset_c must be finite, got {self.offset_c}"
            raise ValueError(msg)
        if self.control_state not in ROOM_STATES:
            msg = (
                "control_state must be one of "
                f"{ROOM_STATES}, got {self.control_state!r}"
            )
            raise ValueError(msg)
        if self.fast_source_kind not in FAST_SOURCE_KINDS:
            msg = (
                "fast_source_kind must be one of "
                f"{FAST_SOURCE_KINDS}, got {self.fast_source_kind!r}"
            )
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of this room's config view."""
        return {
            "name": self.name,
            "area_m2": self.area_m2,
            "offset_c": self.offset_c,
            "cooling_enabled": self.cooling_enabled,
            "control_state": self.control_state,
            "fast_source_kind": self.fast_source_kind,
            "entities": dict(self.entities),
            "diagnostic_entities": dict(self.diagnostic_entities),
        }


@dataclass(frozen=True)
class ConfigResult:
    """The ``get_config`` reply: global settings + per-room config views.

    Attributes:
        home_setpoint_c: Global home target temperature in degrees Celsius.
        mode: Active global :class:`~tortoise_ufh.models.Mode` value string.
        rooms: Per-room configuration views.
        global_safe_dew_point_entity_id: Registered entity id of the global
            safe dew-point ``sensor`` (the cooling-supply lower limit the owner
            feeds the heat pump), or ``None`` when the registry has no match.

    Raises:
        ValueError: If ``home_setpoint_c`` is not finite or ``mode`` is not a
            recognised mode string.
    """

    home_setpoint_c: float
    mode: str
    rooms: tuple[RoomConfigView, ...]
    global_safe_dew_point_entity_id: str | None = None

    def __post_init__(self) -> None:
        """Validate the global fields of the config reply."""
        if not math.isfinite(self.home_setpoint_c):
            msg = f"home_setpoint_c must be finite, got {self.home_setpoint_c}"
            raise ValueError(msg)
        if self.mode not in MODE_OPTIONS:
            msg = f"mode must be one of {MODE_OPTIONS}, got {self.mode!r}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the config reply."""
        return {
            "home_setpoint_c": self.home_setpoint_c,
            "mode": self.mode,
            "rooms": [room.to_dict() for room in self.rooms],
            "global_safe_dew_point_entity_id": self.global_safe_dew_point_entity_id,
        }


@dataclass(frozen=True)
class LiveRoomView:
    """One room's live payload: its outputs plus setpoint / control state.

    Attributes:
        name: Room name (non-empty).
        outputs: The room's :meth:`~tortoise_ufh.models.RoomOutputs.to_dict`
            result (final valve percent, fast-source command, report).
        setpoint_c: The effective target temperature in degrees Celsius used
            this cycle (home temperature + room offset).
        control_state: The room's control state (one of :data:`ROOM_STATES`:
            ``off`` / ``shadow`` / ``live``).
        live_control_enabled: Whether the room is in live control; a derived
            convenience equal to ``control_state == "live"``, retained for one
            release for backward compatibility with the panel.

    Raises:
        ValueError: If ``name`` is empty, ``setpoint_c`` is not finite, or
            ``control_state`` is unrecognised.
    """

    name: str
    outputs: dict[str, Any]
    setpoint_c: float
    control_state: str
    live_control_enabled: bool

    def __post_init__(self) -> None:
        """Validate the live-room view fields."""
        if not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if not math.isfinite(self.setpoint_c):
            msg = f"setpoint_c must be finite, got {self.setpoint_c}"
            raise ValueError(msg)
        if self.control_state not in ROOM_STATES:
            msg = (
                "control_state must be one of "
                f"{ROOM_STATES}, got {self.control_state!r}"
            )
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return the room outputs merged with setpoint / control state."""
        merged: dict[str, Any] = dict(self.outputs)
        merged["setpoint_c"] = self.setpoint_c
        merged["control_state"] = self.control_state
        merged["live_control_enabled"] = self.live_control_enabled
        return merged


@dataclass(frozen=True)
class LiveResult:
    """The ``get_live`` reply: per-room outputs + global statuses.

    Attributes:
        rooms: Per-room live views.
        global_safe_dew_point_c: ``max_over_cooled(T_dew) + 2 K`` in degrees
            Celsius, or ``None`` when no room is eligible.
        algorithm_status: ``"running"`` / ``"stale"`` / ``"error"``.
        watchdog_state: ``"ok"`` / ``"stale"``.
        last_update_timestamp: ISO-8601 UTC timestamp of the last cycle, or
            ``None`` if no cycle has completed.
        mode: Active global :class:`~tortoise_ufh.models.Mode` value string.
        sensor_lost_rooms: Number of rooms currently degraded with the
            ``sensor_lost`` flag (building-level staleness counter,
            safety-F13 2026-07-09).

    Raises:
        ValueError: If ``mode`` is not a recognised mode string or
            ``global_safe_dew_point_c`` is not finite when present.
    """

    rooms: tuple[LiveRoomView, ...]
    global_safe_dew_point_c: float | None
    algorithm_status: str
    watchdog_state: str
    last_update_timestamp: str | None
    mode: str
    sensor_lost_rooms: int = 0

    def __post_init__(self) -> None:
        """Validate the global fields of the live reply."""
        if self.mode not in MODE_OPTIONS:
            msg = f"mode must be one of {MODE_OPTIONS}, got {self.mode!r}"
            raise ValueError(msg)
        if self.global_safe_dew_point_c is not None and not math.isfinite(
            self.global_safe_dew_point_c
        ):
            msg = (
                "global_safe_dew_point_c must be finite when present, got "
                f"{self.global_safe_dew_point_c}"
            )
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the live reply."""
        return {
            "rooms": {room.name: room.to_dict() for room in self.rooms},
            "global_safe_dew_point_c": self.global_safe_dew_point_c,
            "algorithm_status": self.algorithm_status,
            "watchdog_state": self.watchdog_state,
            "last_update_timestamp": self.last_update_timestamp,
            "mode": self.mode,
            "sensor_lost_rooms": self.sensor_lost_rooms,
        }


@dataclass(frozen=True)
class TuningResult:
    """The ``get_tuning`` reply: knob metadata + global / per-room values.

    Attributes:
        fields: Ordered knob descriptors. Each numeric knob is
            ``{"name", "type": "number", "min", "max", "step", "unit"}``; the
            boolean knob is ``{"name", "type": "bool", "unit": ""}``. The panel
            renders steppers / a toggle straight from this — the ranges live only
            in the adapter (:data:`~const.CONTROLLER_NUMBER_KNOBS`).
        global_values: The effective global value of every exposed knob (defaults
            merged with the persisted global override).
        rooms: Sparse per-room overrides ``{room: {field: value}}`` — only the
            fields a room actually overrides.
        defaults: The library-default value of every exposed knob (the value a
            reverted room / global reset returns to).

    Raises:
        ValueError: If ``global_values`` or ``defaults`` is missing an exposed
            knob (an internal-consistency guard).
    """

    fields: tuple[dict[str, Any], ...]
    global_values: dict[str, Any]
    rooms: dict[str, dict[str, Any]]
    defaults: dict[str, Any]

    def __post_init__(self) -> None:
        """Validate that the value maps cover every exposed knob."""
        expected = set(_knob_names())
        for label, values in (
            ("global_values", self.global_values),
            ("defaults", self.defaults),
        ):
            missing = expected - set(values)
            if missing:
                msg = f"{label} is missing knob(s): {sorted(missing)}"
                raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of the tuning reply."""
        return {
            "fields": [dict(f) for f in self.fields],
            "global": dict(self.global_values),
            "rooms": {room: dict(vals) for room, vals in self.rooms.items()},
            "defaults": dict(self.defaults),
        }


# ---------------------------------------------------------------------------
# Coordinator resolution
# ---------------------------------------------------------------------------


def _resolve_coordinator(hass: HomeAssistant) -> TortoiseUfhCoordinator | None:
    """Resolve the coordinator from the first loaded config entry.

    Tortoise-UFH is a ``hub`` integration: a single config entry manages every
    room. This returns that entry's coordinator, or ``None`` when no entry has
    finished loading (``runtime_data`` not yet assigned).

    Args:
        hass: The running Home Assistant instance.

    Returns:
        The live :class:`TortoiseUfhCoordinator`, or ``None`` if unavailable.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            continue
        coordinator: TortoiseUfhCoordinator = runtime.coordinator
        return coordinator
    return None


def _room_configs(coordinator: TortoiseUfhCoordinator) -> list[dict[str, Any]]:
    """Return the per-room configuration dicts from the config entry.

    Args:
        coordinator: The live coordinator.

    Returns:
        A list of per-room configuration dicts (empty when none configured).
    """
    raw: Any = coordinator.config_entry.data.get(CONF_ROOMS, [])
    return list(raw) if raw else []


def _room_names(coordinator: TortoiseUfhCoordinator) -> set[str]:
    """Return the set of configured room names.

    Args:
        coordinator: The live coordinator.

    Returns:
        The set of non-empty configured room names.
    """
    names: set[str] = set()
    for room_cfg in _room_configs(coordinator):
        name = str(room_cfg.get(CONF_ROOM_NAME, ""))
        if name:
            names.add(name)
    return names


def _resolve_diagnostic_entities(
    registry: er.EntityRegistry, entry_id: str, room_name: str
) -> dict[str, str]:
    """Resolve a room's diagnostic ``sensor`` entity ids from the registry.

    Maps each key in :data:`_DIAGNOSTIC_SENSOR_KEYS` to the registered ``sensor``
    entity id for this config entry, using the frozen per-room unique-id template
    ``{entry_id}_{safe_room}_{key}`` where
    ``safe_room = room_name.lower().replace(" ", "_")`` (matching ``sensor.py``'s
    id scheme exactly). Keys with no registry match are omitted, so the map
    degrades to ``{}`` when the sensor platform has not registered yet — the
    panel reads it defensively.

    Args:
        registry: The Home Assistant entity registry.
        entry_id: The config entry id that owns the diagnostic sensors.
        room_name: The configured room name.

    Returns:
        A ``{sensor_key: entity_id}`` dict containing only resolved keys.
    """
    safe_room = room_name.lower().replace(" ", "_")
    resolved: dict[str, str] = {}
    for key in _DIAGNOSTIC_SENSOR_KEYS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry_id}_{safe_room}_{key}"
        )
        if entity_id is not None:
            resolved[key] = entity_id
    return resolved


def _resolve_global_dew_point_entity(
    registry: er.EntityRegistry, entry_id: str
) -> str | None:
    """Resolve the global safe dew-point ``sensor`` entity id, or ``None``.

    Uses the frozen global unique-id template
    ``{entry_id}_global_safe_dew_point`` (no room segment; see ``sensor.py``).

    Args:
        registry: The Home Assistant entity registry.
        entry_id: The config entry id that owns the sensor.

    Returns:
        The registered ``sensor`` entity id, or ``None`` when unregistered.
    """
    return registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_{_GLOBAL_DEW_POINT_KEY}"
    )


# ---------------------------------------------------------------------------
# Controller-tuning helpers (get_tuning / set_tuning)
# ---------------------------------------------------------------------------


def _knob_names() -> list[str]:
    """Return every exposed knob field name (numeric knobs + the boolean knob)."""
    return [name for name, _low, _high, _step in CONTROLLER_NUMBER_KNOBS] + [
        CONTROLLER_BOOL_KNOB
    ]


def _knob_range(field_name: str) -> tuple[float, float] | None:
    """Return a numeric knob's ``(min, max)``, or ``None`` for the boolean knob.

    Args:
        field_name: A candidate knob field name.

    Returns:
        The inclusive ``(min, max)`` for a numeric knob, or ``None`` when the
        field is the boolean knob or is not an exposed knob.
    """
    for name, low, high, _step in CONTROLLER_NUMBER_KNOBS:
        if name == field_name:
            return (low, high)
    return None


def _tuning_fields() -> tuple[dict[str, Any], ...]:
    """Build the ordered knob descriptors for the ``get_tuning`` payload."""
    fields: list[dict[str, Any]] = []
    for name, low, high, step in CONTROLLER_NUMBER_KNOBS:
        fields.append(
            {
                "name": name,
                "type": "number",
                "min": low,
                "max": high,
                "step": step,
                "unit": CONTROLLER_KNOB_UNITS.get(name, ""),
            }
        )
    fields.append(
        {
            "name": CONTROLLER_BOOL_KNOB,
            "type": "bool",
            "unit": CONTROLLER_KNOB_UNITS.get(CONTROLLER_BOOL_KNOB, ""),
        }
    )
    return tuple(fields)


def _knob_values(config: ControllerConfig) -> dict[str, Any]:
    """Extract the exposed-knob values (numeric + boolean) from a config."""
    values: dict[str, Any] = {
        name: float(getattr(config, name))
        for name, _low, _high, _step in CONTROLLER_NUMBER_KNOBS
    }
    values[CONTROLLER_BOOL_KNOB] = bool(getattr(config, CONTROLLER_BOOL_KNOB))
    return values


def _global_controller_dict(entry: ConfigEntry) -> dict[str, Any]:
    """Return the merged global controller dict (``entry.data`` <- ``options``)."""
    return {
        **entry.data.get(CONF_CONTROLLER, {}),
        **entry.options.get(CONF_CONTROLLER, {}),
    }


def _global_controller(entry: ConfigEntry) -> ControllerConfig:
    """Resolve the effective global :class:`ControllerConfig` for an entry.

    Args:
        entry: The config entry.

    Returns:
        The validated global controller config, or library defaults when the
        persisted values are absent or invalid.
    """
    try:
        return ControllerConfig(**_global_controller_dict(entry))
    except (TypeError, ValueError):
        return ControllerConfig()


def _room_overrides(entry: ConfigEntry) -> dict[str, dict[str, Any]]:
    """Return the sparse per-room override map, filtered to known knob fields.

    Args:
        entry: The config entry.

    Returns:
        ``{room: {field: value}}`` containing only recognised knob fields; rooms
        or fields that are not valid knobs are dropped.
    """
    raw: Any = entry.options.get(CONF_ROOM_TUNING, {})
    knobs = set(_knob_names())
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for room, override in raw.items():
        if not isinstance(override, dict):
            continue
        clean = {
            field_name: value
            for field_name, value in override.items()
            if field_name in knobs
        }
        if clean:
            out[str(room)] = clean
    return out


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    """Register the Tortoise-UFH panel websocket commands.

    Idempotent registration is the caller's responsibility (``__init__.py``
    guards this call to run once per Home Assistant instance).

    Args:
        hass: The running Home Assistant instance.

    Returns:
        None.
    """
    websocket_api.async_register_command(hass, ws_get_config)
    websocket_api.async_register_command(hass, ws_get_live)
    websocket_api.async_register_command(hass, ws_set_home_temperature)
    websocket_api.async_register_command(hass, ws_set_room_offset)
    websocket_api.async_register_command(hass, ws_set_room_state)
    websocket_api.async_register_command(hass, ws_set_mode)
    websocket_api.async_register_command(hass, ws_get_tuning)
    websocket_api.async_register_command(hass, ws_set_tuning)


# ---------------------------------------------------------------------------
# Read commands
# ---------------------------------------------------------------------------


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_GET_CONFIG})
@callback
def ws_get_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the rooms, their assigned entities, offsets and flags.

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message.

    Returns:
        None. The reply is sent through ``connection.send_result``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    registry = er.async_get(hass)
    entry_id = coordinator.config_entry.entry_id

    rooms: list[RoomConfigView] = []
    for room_cfg in _room_configs(coordinator):
        name = str(room_cfg.get(CONF_ROOM_NAME, ""))
        if not name:
            continue
        entities: dict[str, Any] = {
            CONF_ENTITY_TEMP_ROOM: room_cfg.get(CONF_ENTITY_TEMP_ROOM),
            CONF_ENTITY_HUMIDITY: room_cfg.get(CONF_ENTITY_HUMIDITY),
            CONF_ENTITY_TEMP_OUTDOOR: room_cfg.get(CONF_ENTITY_TEMP_OUTDOOR),
            CONF_ENTITY_VALVES: list(room_cfg.get(CONF_ENTITY_VALVES) or []),
            CONF_ENTITY_SUPPLY: list(room_cfg.get(CONF_ENTITY_SUPPLY) or []),
            CONF_ENTITY_RETURN: list(room_cfg.get(CONF_ENTITY_RETURN) or []),
            CONF_ENTITY_FAST_SOURCE: room_cfg.get(CONF_ENTITY_FAST_SOURCE),
        }
        rooms.append(
            RoomConfigView(
                name=name,
                area_m2=float(room_cfg.get(CONF_ROOM_AREA, 0.0) or 0.0),
                offset_c=coordinator.get_room_offset(name),
                cooling_enabled=bool(
                    room_cfg.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED)
                ),
                control_state=coordinator.get_room_state(name),
                fast_source_kind=str(
                    room_cfg.get(CONF_FAST_SOURCE_KIND, DEFAULT_FAST_SOURCE_KIND)
                    or DEFAULT_FAST_SOURCE_KIND
                ),
                entities=entities,
                diagnostic_entities=_resolve_diagnostic_entities(
                    registry, entry_id, name
                ),
            )
        )

    result = ConfigResult(
        home_setpoint_c=coordinator.get_home_temperature(),
        mode=coordinator.get_mode().value,
        rooms=tuple(rooms),
        global_safe_dew_point_entity_id=_resolve_global_dew_point_entity(
            registry, entry_id
        ),
    )
    connection.send_result(msg["id"], result.to_dict())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_GET_LIVE})
@callback
def ws_get_live(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the per-room live outputs, setpoints, statuses and dew point.

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message.

    Returns:
        None. The reply is sent through ``connection.send_result``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    data = coordinator.data
    if data is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No data computed yet")
        return

    rooms: list[LiveRoomView] = [
        LiveRoomView(
            name=name,
            outputs=runtime.outputs.to_dict(),
            setpoint_c=runtime.setpoint_c,
            control_state=coordinator.get_room_state(name),
            live_control_enabled=runtime.live_control_enabled,
        )
        for name, runtime in data.rooms.items()
    ]

    result = LiveResult(
        rooms=tuple(rooms),
        global_safe_dew_point_c=data.global_safe_dew_point_c,
        algorithm_status=data.algorithm_status,
        watchdog_state=data.watchdog_state,
        last_update_timestamp=data.last_update_timestamp,
        mode=data.mode,
        sensor_lost_rooms=data.sensor_lost_rooms,
    )
    connection.send_result(msg["id"], result.to_dict())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_GET_TUNING})
@callback
def ws_get_tuning(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the controller-knob metadata plus global / per-room tuning values.

    The reply carries the knob descriptors (ranges + units, the single source of
    truth from :data:`~const.CONTROLLER_NUMBER_KNOBS`), the effective global
    values, the sparse per-room overrides and the library defaults, so the panel
    renders the Tuning tab entirely from this payload.

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message.

    Returns:
        None. The reply is sent through ``connection.send_result``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    entry = coordinator.config_entry
    result = TuningResult(
        fields=_tuning_fields(),
        global_values=_knob_values(_global_controller(entry)),
        rooms=_room_overrides(entry),
        defaults=_knob_values(ControllerConfig()),
    )
    connection.send_result(msg["id"], result.to_dict())


# ---------------------------------------------------------------------------
# Write commands
# ---------------------------------------------------------------------------


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SET_HOME_TEMPERATURE,
        vol.Required("temperature"): vol.All(
            vol.Coerce(float),
            vol.Range(min=HOME_SETPOINT_MIN_C, max=HOME_SETPOINT_MAX_C),
        ),
    }
)
@callback
def ws_set_home_temperature(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Set the global home target temperature [degC].

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message with a ``temperature`` field.

    Returns:
        None. The new value is echoed through ``connection.send_result``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    value = float(msg["temperature"])
    coordinator.set_home_temperature(value)
    connection.send_result(msg["id"], {"home_setpoint_c": value})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SET_ROOM_OFFSET,
        vol.Required("room"): str,
        vol.Required("offset"): vol.All(
            vol.Coerce(float),
            vol.Range(min=ROOM_OFFSET_MIN_C, max=ROOM_OFFSET_MAX_C),
        ),
    }
)
@callback
def ws_set_room_offset(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Set a room's setpoint offset from the home temperature [K].

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message with ``room`` and ``offset`` fields.

    Returns:
        None. The result is sent through ``connection.send_result`` /
        ``connection.send_error``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    room = str(msg["room"])
    if room not in _room_names(coordinator):
        connection.send_error(msg["id"], _ERR_UNKNOWN_ROOM, f"Unknown room {room!r}")
        return

    offset = float(msg["offset"])
    coordinator.set_room_offset(room, offset)
    connection.send_result(
        msg["id"],
        {
            "room": room,
            "offset_c": offset,
            "setpoint_c": coordinator.get_room_setpoint(room),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SET_ROOM_STATE,
        vol.Required("room"): str,
        vol.Required("state"): vol.In(ROOM_STATES),
    }
)
@callback
def ws_set_room_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Set a room's control state (``off`` / ``shadow`` / ``live``).

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message with ``room`` and ``state`` fields.

    Returns:
        None. The result is sent through ``connection.send_result`` /
        ``connection.send_error``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    room = str(msg["room"])
    if room not in _room_names(coordinator):
        connection.send_error(msg["id"], _ERR_UNKNOWN_ROOM, f"Unknown room {room!r}")
        return

    state = str(msg["state"])
    coordinator.set_room_state(room, state)
    connection.send_result(msg["id"], {"room": room, "state": state})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SET_MODE,
        vol.Required("mode"): vol.In(MODE_OPTIONS),
    }
)
@callback
def ws_set_mode(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Override the global operating mode.

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message with a ``mode`` field.

    Returns:
        None. The new mode is echoed through ``connection.send_result``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    mode = Mode(str(msg["mode"]))
    coordinator.set_mode(mode)
    connection.send_result(msg["id"], {"mode": mode.value})


def _coerce_tuning_values(
    raw_values: dict[str, Any], *, allow_delete: bool
) -> dict[str, Any]:
    """Coerce + range-validate submitted knob values.

    Args:
        raw_values: The ``{field: value}`` payload. A ``None`` value requests
            deletion of that field's override (room scope only).
        allow_delete: Whether ``None`` values are permitted (room scope).

    Returns:
        A ``{field: value}`` dict where numeric knobs are floats, the boolean
        knob is a bool, and (when ``allow_delete``) a deleted field maps to
        ``None``.

    Raises:
        ValueError: If a field is not an exposed knob, a value has the wrong
            type, a numeric value is out of range, or a ``None`` is submitted
            when ``allow_delete`` is ``False``.
    """
    knobs = set(_knob_names())
    coerced: dict[str, Any] = {}
    for field_name, value in raw_values.items():
        if field_name not in knobs:
            msg = f"unknown knob {field_name!r}"
            raise ValueError(msg)
        if value is None:
            if not allow_delete:
                msg = f"cannot clear global knob {field_name!r}"
                raise ValueError(msg)
            coerced[field_name] = None
            continue
        if field_name == CONTROLLER_BOOL_KNOB:
            if not isinstance(value, bool):
                msg = f"{field_name} must be a boolean, got {value!r}"
                raise ValueError(msg)
            coerced[field_name] = value
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError) as err:
            msg = f"{field_name} must be a number, got {value!r}"
            raise ValueError(msg) from err
        if not math.isfinite(numeric):
            msg = f"{field_name} must be finite, got {numeric}"
            raise ValueError(msg)
        rng = _knob_range(field_name)
        if rng is not None and not rng[0] <= numeric <= rng[1]:
            msg = f"{field_name} must be in [{rng[0]}, {rng[1]}], got {numeric}"
            raise ValueError(msg)
        coerced[field_name] = numeric
    return coerced


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SET_TUNING,
        vol.Required("scope"): str,
        vol.Required("values"): dict,
    }
)
@callback
def ws_set_tuning(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Persist controller tuning for the global scope or one room.

    ``scope`` is ``"global"`` (writes ``entry.options[CONF_CONTROLLER]``) or a
    room name (merges into the sparse ``entry.options[CONF_ROOM_TUNING]`` map;
    a ``None`` value clears that field's override and an emptied room override is
    pruned entirely — "back to global"). Every submitted value is range-checked
    against :data:`~const.CONTROLLER_NUMBER_KNOBS` and the merged result is
    constructed as a :class:`ControllerConfig` so cross-field invariants are
    caught before persisting. The write reloads the entry (rebuilding the
    controller cleanly).

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message with ``scope`` and ``values`` fields.

    Returns:
        None. The refreshed tuning view is sent through ``connection.send_result``
        / ``connection.send_error``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    entry = coordinator.config_entry
    scope = str(msg["scope"])
    is_global = scope == _TUNING_SCOPE_GLOBAL
    if not is_global and scope not in _room_names(coordinator):
        connection.send_error(
            msg["id"], _ERR_INVALID_SCOPE, f"Unknown tuning scope {scope!r}"
        )
        return

    raw_values: dict[str, Any] = dict(msg["values"])
    try:
        coerced = _coerce_tuning_values(raw_values, allow_delete=not is_global)
    except ValueError as err:
        connection.send_error(msg["id"], _ERR_INVALID_TUNING, str(err))
        return

    global_dict = _global_controller_dict(entry)
    new_override: dict[str, Any] = {}
    if is_global:
        merged = {**global_dict, **coerced}
    else:
        new_override = dict(_room_overrides(entry).get(scope, {}))
        for field_name, value in coerced.items():
            if value is None:
                new_override.pop(field_name, None)
            else:
                new_override[field_name] = value
        merged = {**global_dict, **new_override}

    try:
        validated = ControllerConfig(**merged)
    except (TypeError, ValueError) as err:
        connection.send_error(msg["id"], _ERR_INVALID_TUNING, str(err))
        return

    if is_global:
        new_options = {**entry.options, CONF_CONTROLLER: asdict(validated)}
    else:
        room_map = {room: dict(vals) for room, vals in _room_overrides(entry).items()}
        if new_override:
            room_map[scope] = new_override
        else:
            room_map.pop(scope, None)
        new_options = {**entry.options, CONF_ROOM_TUNING: room_map}

    hass.config_entries.async_update_entry(entry, options=new_options)

    result = TuningResult(
        fields=_tuning_fields(),
        global_values=_knob_values(_global_controller(entry)),
        rooms=_room_overrides(entry),
        defaults=_knob_values(ControllerConfig()),
    )
    reply = result.to_dict()
    reply["scope"] = scope
    connection.send_result(msg["id"], reply)
