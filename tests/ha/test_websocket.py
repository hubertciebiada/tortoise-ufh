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
from homeassistant.helpers import entity_registry as er

from custom_components.tortoise_ufh.config_flow import CONF_CONTROLLER
from custom_components.tortoise_ufh.const import (
    CONF_ROOM_TUNING,
    DOMAIN,
    ROOM_STATE_LIVE,
    ROOM_STATE_SHADOW,
    ROOM_STATES,
)
from custom_components.tortoise_ufh.core.models import Mode

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
    """get_config exposes home_setpoint_c + mode and per-room control-state views."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/get_config"})

    assert msg["success"] is True
    result = msg["result"]
    # Globals the panel reads verbatim (frontend picks "home_setpoint_c").
    assert result["home_setpoint_c"] == 21.0
    assert result["mode"] in {"heating", "transitional", "cooling", "off"}
    # The retired kill-switch is gone from the config reply.
    assert "kill_switch" not in result

    rooms = {room["name"]: room for room in result["rooms"]}
    assert set(rooms) == {"Salon", "Lazienka"}
    # Panel picks "offset_c" and "control_state" per room; assert real values.
    assert rooms["Lazienka"]["offset_c"] == 1.0
    assert rooms["Salon"]["offset_c"] == 0.0
    for room in rooms.values():
        assert "offset_c" in room
        # Canonical three-state field (off / shadow / live).
        assert room["control_state"] in ROOM_STATES
        assert room["control_state"] == ROOM_STATE_SHADOW
    # Cooling opt-out from the PRD survives the round-trip.
    assert rooms["Salon"]["cooling_enabled"] is True
    assert rooms["Lazienka"]["cooling_enabled"] is False


async def test_get_config_exposes_diagnostic_entity_ids(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """get_config maps each room's diagnostic sensors to real registered ids."""
    entry_id = setup_integration.entry_id
    registry = er.async_get(hass)

    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/get_config"})

    assert msg["success"] is True
    result = msg["result"]

    # The chartable diagnostic subset the panel resolves for history/statistics.
    expected_keys = {
        "recommended_valve",
        "error_c",
        "trend_c_per_h",
        "room_dew_point",
        "i_term",
        "trend_term",
    }

    rooms = {room["name"]: room for room in result["rooms"]}
    assert set(rooms) == {"Salon", "Lazienka"}
    for name, room in rooms.items():
        # New additive field: a {sensor_key: entity_id} map.
        assert "diagnostic_entities" in room
        diag = room["diagnostic_entities"]
        assert isinstance(diag, dict)
        # The sensor platform is set up by the fixture, so every key resolves.
        assert set(diag) == expected_keys
        safe_room = name.lower().replace(" ", "_")
        for key, entity_id in diag.items():
            # Each value is exactly the registry's id for the frozen unique_id...
            unique_id = f"{entry_id}_{safe_room}_{key}"
            assert (
                registry.async_get_entity_id("sensor", DOMAIN, unique_id) == entity_id
            )
            # ...and points at a real, registered entity.
            assert registry.async_get(entity_id) is not None

    # The global safe dew-point sensor id is exposed at the top level.
    assert "global_safe_dew_point_entity_id" in result
    global_id = result["global_safe_dew_point_entity_id"]
    assert global_id == registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_global_safe_dew_point"
    )
    assert global_id is not None
    assert registry.async_get(global_id) is not None


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
        # Canonical control state plus the derived convenience flag.
        assert room["control_state"] in ROOM_STATES
        assert "live_control_enabled" in room
        assert room["live_control_enabled"] == (
            room["control_state"] == ROOM_STATE_LIVE
        )
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


async def test_set_room_state_mutates_control_state(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_room_state promotes a room to live and echoes the new state."""
    coordinator = _coordinator(hass)
    assert coordinator.get_room_state("Salon") == ROOM_STATE_SHADOW

    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {
            "type": f"{DOMAIN}/set_room_state",
            "room": "Salon",
            "state": ROOM_STATE_LIVE,
        },
    )

    assert msg["success"] is True
    assert msg["result"] == {"room": "Salon", "state": ROOM_STATE_LIVE}
    # A control-state-only change is applied in memory without reloading the
    # entry, so the same coordinator instance reflects it.
    await hass.async_block_till_done()
    assert _coordinator(hass) is coordinator
    assert coordinator.get_room_state("Salon") == ROOM_STATE_LIVE
    # The other room is untouched.
    assert coordinator.get_room_state("Lazienka") == ROOM_STATE_SHADOW


async def test_set_room_state_unknown_room_errors(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """An unknown room name is rejected rather than silently mutating state."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_room_state", "room": "Nope", "state": ROOM_STATE_LIVE},
    )

    assert msg["success"] is False
    assert msg["error"]["code"] == "unknown_room"


# -- Controller tuning (get_tuning / set_tuning) ----------------------------


async def test_get_tuning_returns_fields_and_values(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """get_tuning exposes knob metadata, global values, defaults and overrides."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/get_tuning"})

    assert msg["success"] is True
    result = msg["result"]
    by_name = {f["name"]: f for f in result["fields"]}
    # A numeric knob carries a full range + unit the panel renders straight.
    assert by_name["kp"]["type"] == "number"
    assert by_name["kp"]["min"] == 0.0
    assert by_name["kp"]["max"] == 50.0
    assert by_name["kp"]["unit"] == "%/K"
    # The single boolean knob is flagged as such.
    assert by_name["outdoor_ff_enabled"]["type"] == "bool"
    # Global + defaults reflect the library defaults (no persisted override).
    assert result["global"]["kp"] == 8.0
    assert result["defaults"]["kp"] == 8.0
    assert result["defaults"]["outdoor_ff_enabled"] is False
    # No per-room overrides configured yet.
    assert result["rooms"] == {}


async def test_set_tuning_global_persists_and_reloads(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_tuning(global) writes entry.options and rebuilds every room's config."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_tuning", "scope": "global", "values": {"kp": 12.0}},
    )

    assert msg["success"] is True
    assert msg["result"]["global"]["kp"] == 12.0
    await hass.async_block_till_done()

    entry = setup_integration
    assert entry.options[CONF_CONTROLLER]["kp"] == 12.0
    coordinator = _coordinator(hass)
    assert coordinator._controller_configs["Salon"].kp == 12.0
    assert coordinator._controller_configs["Lazienka"].kp == 12.0


async def test_set_tuning_room_override_merges(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """A per-room override layers over the global tuning for that room only."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_tuning", "scope": "Salon", "values": {"kt": 10.0}},
    )

    assert msg["success"] is True
    assert msg["result"]["rooms"]["Salon"]["kt"] == 10.0
    await hass.async_block_till_done()

    entry = setup_integration
    assert entry.options[CONF_ROOM_TUNING]["Salon"] == {"kt": 10.0}
    coordinator = _coordinator(hass)
    assert coordinator._controller_configs["Salon"].kt == 10.0
    # The other room keeps the global default.
    assert coordinator._controller_configs["Lazienka"].kt == 6.0


async def test_set_tuning_revert_room_field_prunes_override(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """Clearing a room's only override (value None) prunes the room key."""
    client = await hass_ws_client(hass)
    await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_tuning", "scope": "Salon", "values": {"kp": 20.0}},
    )
    await hass.async_block_till_done()
    assert setup_integration.options[CONF_ROOM_TUNING]["Salon"] == {"kp": 20.0}

    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_tuning", "scope": "Salon", "values": {"kp": None}},
    )
    assert msg["success"] is True
    assert msg["result"]["rooms"] == {}
    await hass.async_block_till_done()
    assert "Salon" not in setup_integration.options.get(CONF_ROOM_TUNING, {})


async def test_set_tuning_out_of_range_rejected(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """A value outside the knob's declared range is rejected, state untouched."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_tuning", "scope": "global", "values": {"kp": 999.0}},
    )

    assert msg["success"] is False
    assert msg["error"]["code"] == "invalid_tuning"
    assert CONF_CONTROLLER not in setup_integration.options


async def test_set_tuning_unknown_scope_rejected(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """An unknown room scope is rejected."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_tuning", "scope": "Nope", "values": {"kp": 10.0}},
    )

    assert msg["success"] is False
    assert msg["error"]["code"] == "invalid_scope"
