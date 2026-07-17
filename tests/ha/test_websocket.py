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
    ROOM_STATE_OFF,
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
        # Canonical two-state field (off / live); new rooms default to off.
        assert room["control_state"] in ROOM_STATES
        assert room["control_state"] == ROOM_STATE_OFF
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
        "fast_source_mode",
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

    # ...and so is the force-cooling-start state sensor id (the panel counts
    # forced starts over the last 24 h from its recorder history).
    assert "hp_flicker_state_entity_id" in result
    flicker_id = result["hp_flicker_state_entity_id"]
    assert flicker_id == registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}_hp_flicker_state"
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
        # Canonical control state; the transitional live_control_enabled
        # convenience flag was removed in v0.5.0.
        assert room["control_state"] in ROOM_STATES
        assert "live_control_enabled" not in room
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
    assert coordinator.get_room_state("Salon") == ROOM_STATE_OFF

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
    assert coordinator.get_room_state("Lazienka") == ROOM_STATE_OFF


async def test_set_room_state_rejects_retired_shadow(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """The retired ``shadow`` state fails schema validation (v0.7.0)."""
    coordinator = _coordinator(hass)
    client = await hass_ws_client(hass)
    msg = await _round_trip(
        client,
        {"type": f"{DOMAIN}/set_room_state", "room": "Salon", "state": "shadow"},
    )

    assert msg["success"] is False
    assert msg["error"]["code"] == "invalid_format"
    # State untouched by the rejected command.
    assert coordinator.get_room_state("Salon") == ROOM_STATE_OFF


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
    # Retuned 2026-07-09 (C1): kp=14 / ki=0.0015 / kt=12.
    assert result["global"]["kp"] == 14.0
    assert result["defaults"]["kp"] == 14.0
    assert result["defaults"]["ki"] == 0.0015
    assert result["defaults"]["kt"] == 12.0
    # control-F6: the feedforward shaping constants are knobs now.
    assert by_name["ff_neutral_c"]["unit"] == "°C"
    assert result["defaults"]["ff_max_pct"] == 20.0
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
    # The other room keeps the global default (retuned 2026-07-09: kt=12).
    assert coordinator._controller_configs["Lazienka"].kt == 12.0


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


# -- Round 3 hardening (2026-07-12): K9 group topology, D7 broken room -------


async def test_get_config_exposes_fast_source_group(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """K9: the multisplit group key flows to the panel (empty when ungrouped)."""
    from custom_components.tortoise_ufh.const import (
        CONF_FAST_SOURCE_GROUP,
        CONF_ROOM_NAME,
        CONF_ROOMS,
    )

    entry = setup_integration
    rooms_cfg = [dict(room) for room in entry.data[CONF_ROOMS]]
    for room in rooms_cfg:
        if room[CONF_ROOM_NAME] == "Salon":
            room[CONF_FAST_SOURCE_GROUP] = "outdoor_unit_a"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ROOMS: rooms_cfg}
    )
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/get_config"})

    assert msg["success"] is True
    rooms = {room["name"]: room for room in msg["result"]["rooms"]}
    assert rooms["Salon"]["fast_source_group"] == "outdoor_unit_a"
    assert rooms["Lazienka"]["fast_source_group"] == ""


async def test_get_config_skips_a_broken_room_entry(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """D7: one corrupted room dict must not blow up the whole reply."""
    from custom_components.tortoise_ufh.const import (
        CONF_ROOM_AREA,
        CONF_ROOM_NAME,
        CONF_ROOMS,
    )

    entry = setup_integration
    rooms_cfg = [dict(room) for room in entry.data[CONF_ROOMS]]
    for room in rooms_cfg:
        if room[CONF_ROOM_NAME] == "Lazienka":
            room[CONF_ROOM_AREA] = "zepsute"  # float() -> ValueError
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ROOMS: rooms_cfg}
    )
    await hass.async_block_till_done()

    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/get_config"})

    assert msg["success"] is True
    rooms = {room["name"]: room for room in msg["result"]["rooms"]}
    # The healthy room is still served; the corrupted one is skipped.
    assert "Salon" in rooms
    assert "Lazienka" not in rooms


async def test_get_live_and_config_carry_heat_pump_and_window_fields(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """The v0.8.0 additive payload fields exist (null when unconfigured)."""
    client = await hass_ws_client(hass)

    live = await _round_trip(client, {"type": f"{DOMAIN}/get_live"})
    assert live["success"] is True
    assert live["result"]["heat_pump"] is None

    cfg = await _round_trip(client, {"type": f"{DOMAIN}/get_config"})
    assert cfg["success"] is True
    assert cfg["result"]["heat_pump"] is None
    for room in cfg["result"]["rooms"]:
        assert "fast_source_window_start" in room
        assert "fast_source_window_end" in room
        assert room["fast_source_window_start"] is None


async def test_set_hp_dhw_unconfigured_errors(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_hp_dhw without a configured mode entity returns hp_not_configured."""
    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/set_hp_dhw", "dhw": True})
    assert msg["success"] is False
    assert msg["error"]["code"] == "hp_not_configured"


async def test_set_hp_dhw_toggles_the_flag(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    hass_ws_client: Any,
) -> None:
    """set_hp_dhw adds/removes the +DHW variant through select.select_option."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.tortoise_ufh.const import CONF_ENTITY_HP_MODE

    options = ["Heat only", "Heat+DHW", "Cool only", "Cool+DHW", "DHW only"]
    hass.states.async_set("select.heat_pump_mode", "Heat only", {"options": options})
    coordinator = _coordinator(hass)
    coordinator._hp_options = {CONF_ENTITY_HP_MODE: "select.heat_pump_mode"}
    select_calls = async_mock_service(hass, "select", "select_option")

    client = await hass_ws_client(hass)
    msg = await _round_trip(client, {"type": f"{DOMAIN}/set_hp_dhw", "dhw": True})
    assert msg["success"] is True
    assert msg["result"] == {"dhw": True, "option": "Heat+DHW"}
    assert [c.data["option"] for c in select_calls] == ["Heat+DHW"]

    # Removing the flag from "DHW only" is refused: no base direction.
    hass.states.async_set("select.heat_pump_mode", "DHW only", {"options": options})
    msg = await _round_trip(client, {"type": f"{DOMAIN}/set_hp_dhw", "dhw": False})
    assert msg["success"] is False
    assert msg["error"]["code"] == "hp_dhw_unavailable"
    assert len(select_calls) == 1
