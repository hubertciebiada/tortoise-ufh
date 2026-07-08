"""Panel websocket API for the Tortoise-UFH Home Assistant adapter.

This module exposes the read/write commands the self-contained frontend panel
(``frontend/tortoise-ufh-panel.js``) uses to render the per-room table and the
under-the-hood report, and to mutate the writable state (home temperature,
per-room offset, per-room live control, global mode, global kill-switch).

Registration is process-wide: :func:`async_register_ws` is called exactly once
per Home Assistant instance (guarded by the caller in ``__init__.py``). Every
command is an admin-guarded, synchronous ``@callback`` websocket handler that
resolves the single :class:`~custom_components.tortoise_ufh.coordinator.
TortoiseUfhCoordinator` from the loaded config entries and replies through
``connection.send_result`` / ``connection.send_error``.

The commands mutate the coordinator's authoritative setpoint / flag state
(``set_home_temperature`` / ``set_room_offset`` / ``set_live_control`` /
``set_mode`` / ``set_kill_switch``); the coordinator rebroadcasts the cached
payload immediately where relevant so entities and the panel see the change
before the next 5-minute refresh.

Units: temperatures in degrees Celsius (``_c``); offsets in kelvin (``_c`` as a
delta); valve position and humidity in percent (0..100). This module holds no
physical control logic of its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import callback

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
    CONF_PARTICIPATES,
    CONF_ROOM_AREA,
    CONF_ROOM_NAME,
    CONF_ROOMS,
    DEFAULT_COOLING_ENABLED,
    DEFAULT_FAST_SOURCE_KIND,
    DEFAULT_PARTICIPATES,
    DOMAIN,
    FAST_SOURCE_KINDS,
    HOME_SETPOINT_MAX_C,
    HOME_SETPOINT_MIN_C,
    MODE_OPTIONS,
    ROOM_OFFSET_MAX_C,
    ROOM_OFFSET_MIN_C,
)
from .core.models import Mode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import TortoiseUfhCoordinator

# --- Command type strings ---------------------------------------------------

WS_GET_CONFIG: str = f"{DOMAIN}/get_config"
WS_GET_LIVE: str = f"{DOMAIN}/get_live"
WS_SET_HOME_TEMPERATURE: str = f"{DOMAIN}/set_home_temperature"
WS_SET_ROOM_OFFSET: str = f"{DOMAIN}/set_room_offset"
WS_SET_ROOM_ENABLED: str = f"{DOMAIN}/set_room_enabled"
WS_SET_MODE: str = f"{DOMAIN}/set_mode"
WS_SET_KILL_SWITCH: str = f"{DOMAIN}/set_kill_switch"

# Websocket error codes.
_ERR_NOT_FOUND: str = "not_found"
_ERR_UNKNOWN_ROOM: str = "unknown_room"


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
        participates: Whether the room participates in control at all.
        cooling_enabled: Whether the room participates in floor cooling.
        live_control: Whether the room is in live control (writes) vs shadow.
        fast_source_kind: Configured fast-source kind (one of
            :data:`FAST_SOURCE_KINDS`).
        entities: Assigned source entities keyed by their ``CONF_*`` name;
            values are entity-id strings, lists of strings, or ``None``.

    Raises:
        ValueError: If ``name`` is empty, ``area_m2`` is negative or not finite,
            ``offset_c`` is not finite, or ``fast_source_kind`` is unrecognised.
    """

    name: str
    area_m2: float
    offset_c: float
    participates: bool
    cooling_enabled: bool
    live_control: bool
    fast_source_kind: str
    entities: dict[str, Any] = field(default_factory=dict)

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
            "participates": self.participates,
            "cooling_enabled": self.cooling_enabled,
            "live_control": self.live_control,
            "fast_source_kind": self.fast_source_kind,
            "entities": dict(self.entities),
        }


@dataclass(frozen=True)
class ConfigResult:
    """The ``get_config`` reply: global settings + per-room config views.

    Attributes:
        home_setpoint_c: Global home target temperature in degrees Celsius.
        mode: Active global :class:`~tortoise_ufh.models.Mode` value string.
        kill_switch: Whether the global kill-switch is engaged (no writes).
        rooms: Per-room configuration views.

    Raises:
        ValueError: If ``home_setpoint_c`` is not finite or ``mode`` is not a
            recognised mode string.
    """

    home_setpoint_c: float
    mode: str
    kill_switch: bool
    rooms: tuple[RoomConfigView, ...]

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
            "kill_switch": self.kill_switch,
            "rooms": [room.to_dict() for room in self.rooms],
        }


@dataclass(frozen=True)
class LiveRoomView:
    """One room's live payload: its outputs plus setpoint / live-control state.

    Attributes:
        name: Room name (non-empty).
        outputs: The room's :meth:`~tortoise_ufh.models.RoomOutputs.to_dict`
            result (final valve percent, fast-source command, report).
        setpoint_c: The effective target temperature in degrees Celsius used
            this cycle (home temperature + room offset).
        live_control_enabled: Whether the room is in live control vs shadow.

    Raises:
        ValueError: If ``name`` is empty or ``setpoint_c`` is not finite.
    """

    name: str
    outputs: dict[str, Any]
    setpoint_c: float
    live_control_enabled: bool

    def __post_init__(self) -> None:
        """Validate the live-room view fields."""
        if not self.name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if not math.isfinite(self.setpoint_c):
            msg = f"setpoint_c must be finite, got {self.setpoint_c}"
            raise ValueError(msg)

    def to_dict(self) -> dict[str, Any]:
        """Return the room outputs merged with setpoint / live-control state."""
        merged: dict[str, Any] = dict(self.outputs)
        merged["setpoint_c"] = self.setpoint_c
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
    websocket_api.async_register_command(hass, ws_set_room_enabled)
    websocket_api.async_register_command(hass, ws_set_mode)
    websocket_api.async_register_command(hass, ws_set_kill_switch)


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
                participates=bool(
                    room_cfg.get(CONF_PARTICIPATES, DEFAULT_PARTICIPATES)
                ),
                cooling_enabled=bool(
                    room_cfg.get(CONF_COOLING_ENABLED, DEFAULT_COOLING_ENABLED)
                ),
                live_control=coordinator.get_live_control(name),
                fast_source_kind=str(
                    room_cfg.get(CONF_FAST_SOURCE_KIND, DEFAULT_FAST_SOURCE_KIND)
                    or DEFAULT_FAST_SOURCE_KIND
                ),
                entities=entities,
            )
        )

    result = ConfigResult(
        home_setpoint_c=coordinator.get_home_temperature(),
        mode=coordinator.get_mode().value,
        kill_switch=coordinator.get_kill_switch(),
        rooms=tuple(rooms),
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
        vol.Required("type"): WS_SET_ROOM_ENABLED,
        vol.Required("room"): str,
        vol.Required("enabled"): bool,
    }
)
@callback
def ws_set_room_enabled(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Toggle a room between live control (writes) and shadow mode.

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message with ``room`` and ``enabled`` fields.

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

    enabled = bool(msg["enabled"])
    coordinator.set_live_control(room, enabled)
    connection.send_result(msg["id"], {"room": room, "live_control": enabled})


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


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_SET_KILL_SWITCH,
        vol.Required("engaged"): bool,
    }
)
@callback
def ws_set_kill_switch(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Engage or release the global kill-switch (engaged = emit no commands).

    Args:
        hass: The running Home Assistant instance.
        connection: The active websocket connection.
        msg: The decoded request message with an ``engaged`` field.

    Returns:
        None. The new state is echoed through ``connection.send_result``.
    """
    coordinator = _resolve_coordinator(hass)
    if coordinator is None:
        connection.send_error(msg["id"], _ERR_NOT_FOUND, "No Tortoise-UFH entry loaded")
        return

    engaged = bool(msg["engaged"])
    coordinator.set_kill_switch(engaged)
    connection.send_result(msg["id"], {"kill_switch": engaged})
