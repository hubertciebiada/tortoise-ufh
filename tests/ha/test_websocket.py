"""Integration tests for the Tortoise-UFH panel websocket API.

Drives the ``tortoise_ufh/*`` websocket commands through a live
``hass_ws_client`` against the coordinator set up by the shared
``setup_integration`` fixture. Asserts the read commands return the payload keys
the self-contained panel (``frontend/tortoise-ufh-panel.js``) relies on and that
the write commands mutate the coordinator's authoritative setpoint/flag state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.tortoise_ufh.const import DOMAIN
from tortoise_ufh.models import Mode

if TYPE_CHECKING:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.tortoise_ufh.coordinator import TortoiseUfhCoordinator

pytestmark = pytest.mark.ha


def _coordinator(hass: HomeAssistant) -> TortoiseUfhCoordinator:
    """Resolve the live coordinator from the (possibly reloaded) config entry."""
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return entry.runtime_data.coordinator


async def _round_trip(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one command and return the decoded reply message."""
    await client.send_json_auto_id(payload)
    return await client.receive_json()


async def test_get_config_returns_globals_and_rooms(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """get_config exposes home_setpoint_c + mode + kill_switch and per-room views."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/get_config"})

    assert msg["success"] is True
    result = msg["result"]
    # Globals the panel reads verbatim (frontend picks "home_setpoint_c").
    assert result["home_setpoint_c"] == 21.0
    assert result["mode"] in {"heating", "transitional", "cooling", "off"}
    assert result["kill_switch"] is False

    rooms = {room["name"]: room for room in result["rooms"]}
    assert set(rooms) == {"Salon", "Lazienka"}
    # Panel picks "offset_c" and "live_control" per room; assert real values.
    assert rooms["Lazienka"]["offset_c"] == 1.0
    assert rooms["Salon"]["offset_c"] == 0.0
    for room in rooms.values():
        assert "offset_c" in room
        assert "live_control" in room
        assert isinstance(room["live_control"], bool)
    # Cooling opt-out from the PRD survives the round-trip.
    assert rooms["Salon"]["cooling_enabled"] is True
    assert rooms["Lazienka"]["cooling_enabled"] is False


async def test_get_live_returns_outputs_and_dew_point(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """get_live returns per-room outputs/report keyed by name + the global dew point."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/get_live"})

    assert msg["success"] is True
    result = msg["result"]
    # Global cooling-supply floor the owner pipes to the heat pump; key must exist
    # (value is None here: heating mode, so no room is cooled/eligible).
    assert "global_safe_dew_point_c" in result

    rooms = result["rooms"]
    assert set(rooms) == {"Salon", "Lazienka"}
    for room in rooms.values():
        # Panel reads "valve_position_pct" and "report" off each live room.
        assert "valve_position_pct" in room
        assert 0.0 <= room["valve_position_pct"] <= 100.0
        assert "report" in room
        assert "explanation" in room["report"]
        assert isinstance(room["report"]["flags"], list)
        assert "fast_source" in room
        assert "live_control_enabled" in room
    # Effective setpoint is home + the room's configured offset.
    assert rooms["Salon"]["setpoint_c"] == _coordinator(hass).get_room_setpoint("Salon")
    assert rooms["Salon"]["setpoint_c"] == 21.0
    assert rooms["Lazienka"]["setpoint_c"] == 22.0


async def test_set_home_temperature_mutates_coordinator(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_home_temperature echoes the value and updates the authoritative state."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client, {"type": f"{DOMAIN}/set_home_temperature", "temperature": 22.5}
    )

    assert msg["success"] is True
    assert msg["result"]["home_setpoint_c"] == 22.5
    assert _coordinator(hass).get_home_temperature() == 22.5


async def test_set_room_offset_mutates_setpoint(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_room_offset updates the offset and returns the recomputed setpoint."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_room_offset", "room": "Salon", "offset": 1.5},
    )

    assert msg["success"] is True
    result = msg["result"]
    assert result["offset_c"] == 1.5
    # setpoint_c = home (21.0) + new offset (1.5).
    assert result["setpoint_c"] == 22.5
    assert _coordinator(hass).get_room_offset("Salon") == 1.5


async def test_set_room_offset_unknown_room_errors(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """An unknown room name is rejected rather than silently mutating state."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_room_offset", "room": "Nope", "offset": 1.0},
    )

    assert msg["success"] is False
    assert msg["error"]["code"] == "unknown_room"


async def test_set_mode_mutates_coordinator(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_mode overrides the global operating mode."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/set_mode", "mode": "cooling"})

    assert msg["success"] is True
    assert msg["result"]["mode"] == "cooling"
    assert _coordinator(hass).get_mode() is Mode.COOLING


async def test_set_kill_switch_mutates_coordinator(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_kill_switch engages the global safety cut-out and it survives reload."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client, {"type": f"{DOMAIN}/set_kill_switch", "engaged": True}
    )

    assert msg["success"] is True
    assert msg["result"]["kill_switch"] is True
    # The setter persists to entry.options, which reloads the entry; let it settle
    # and re-resolve the rebuilt coordinator.
    await hass.async_block_till_done()
    assert _coordinator(hass).get_kill_switch() is True


async def test_set_room_enabled_mutates_live_control(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_room_enabled toggles a room into live control (panel reads live_control)."""
    coordinator = _coordinator(hass)
    assert coordinator.get_live_control("Salon") is False

    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_room_enabled", "room": "Salon", "enabled": True},
    )

    assert msg["success"] is True
    # Panel picks "live_control" off this reply.
    assert msg["result"]["live_control"] is True
    # Persisting live-control options reloads the entry; re-resolve after settle.
    await hass.async_block_till_done()
    assert _coordinator(hass).get_live_control("Salon") is True
