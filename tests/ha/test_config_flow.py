"""Config-flow and options-flow behaviour tests for Tortoise-UFH.

Drives the full setup wizard (location -> rooms -> per-room entities ->
algorithm -> confirm) to a created entry, exercises the options flow
(per-room control state + advanced knobs), and pins the locked guard rails:
duplicate-location abort, the cooling-room ``humidity_required`` error, and
:class:`EntityValidator` unit rejection.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tortoise_ufh.config_flow import (
    CONF_CONTROLLER,
    CONF_HAS_FAST_SOURCE,
    CONF_SELECTED_ROOM,
)
from custom_components.tortoise_ufh.const import (
    CONF_ADD_ANOTHER,
    CONF_COOLING_ENABLED,
    CONF_ENTITY_FAST_SOURCE,
    CONF_ENTITY_HUMIDITY,
    CONF_ENTITY_MODE,
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_TEMP_OUTDOOR,
    CONF_ENTITY_TEMP_ROOM,
    CONF_ENTITY_VALVES,
    CONF_FAST_SOURCE_GROUP,
    CONF_FAST_SOURCE_KIND,
    CONF_ROOM_AREA,
    CONF_ROOM_NAME,
    CONF_ROOM_OFFSET,
    CONF_ROOM_STATE,
    CONF_ROOM_TUNING,
    CONF_ROOMS,
    DOMAIN,
    FAST_SOURCE_KIND_NONE,
    FAST_SOURCE_KIND_SPLIT,
    ROOM_STATE_OFF,
    ROOM_STATE_SHADOW,
    VALID_TEMP_UNITS,
)
from custom_components.tortoise_ufh.entity_validator import EntityValidator

pytestmark = pytest.mark.ha

_LAT = 50.5
_LON = 19.5

_TEMP_ATTRS = {"unit_of_measurement": "°C", "device_class": "temperature"}
_PCT_ATTRS = {"unit_of_measurement": "%"}

_SALON_ROOM: dict[str, Any] = {
    CONF_ROOM_NAME: "Salon",
    CONF_ROOM_AREA: 30.0,
    CONF_HAS_FAST_SOURCE: True,
    CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_SPLIT,
    CONF_FAST_SOURCE_GROUP: "outdoor_unit_a",
    CONF_COOLING_ENABLED: True,
    CONF_ADD_ANOTHER: True,
}
_LAZIENKA_ROOM: dict[str, Any] = {
    CONF_ROOM_NAME: "Lazienka",
    CONF_ROOM_AREA: 6.0,
    CONF_HAS_FAST_SOURCE: False,
    CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
    CONF_COOLING_ENABLED: False,
    CONF_ADD_ANOTHER: False,
}
_SALON_ENTITIES: dict[str, Any] = {
    CONF_ENTITY_TEMP_ROOM: "sensor.salon_temp",
    CONF_ENTITY_HUMIDITY: "sensor.salon_humidity",
    CONF_ENTITY_VALVES: ["number.salon_valve"],
    CONF_ENTITY_SUPPLY: ["sensor.salon_supply"],
    CONF_ENTITY_RETURN: ["sensor.salon_return"],
    CONF_ENTITY_FAST_SOURCE: "climate.salon_split",
    CONF_ENTITY_TEMP_OUTDOOR: "sensor.outdoor_temp",
    CONF_ENTITY_MODE: "input_select.home_mode",
}
_LAZIENKA_ENTITIES: dict[str, Any] = {
    CONF_ENTITY_TEMP_ROOM: "sensor.lazienka_temp",
    CONF_ENTITY_VALVES: ["number.lazienka_valve"],
    CONF_ENTITY_SUPPLY: ["sensor.lazienka_supply"],
    CONF_ENTITY_RETURN: ["sensor.lazienka_return"],
}


async def _drive_to_confirm(hass: HomeAssistant, lat: float, lon: float) -> Any:
    """Drive the wizard through to (but not past) the confirm form.

    Asserts the step sequence along the way: the ``rooms`` step loops on
    itself via ``add_another`` and the ``entities`` step runs once per room.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # Step 1 -> lands on the first rooms form.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LATITUDE: lat, CONF_LONGITUDE: lon}
    )
    assert result["step_id"] == "rooms"

    # First room with add_another -> rooms form is shown again.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _SALON_ROOM
    )
    assert result["step_id"] == "rooms"

    # Second room without add_another -> first per-room entities form.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _LAZIENKA_ROOM
    )
    assert result["step_id"] == "entities"
    assert result["description_placeholders"]["room_name"] == "Salon"

    # Salon entities -> second room's entities form.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _SALON_ENTITIES
    )
    assert result["step_id"] == "entities"
    assert result["description_placeholders"]["room_name"] == "Lazienka"

    # Lazienka entities -> algorithm knobs form.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _LAZIENKA_ENTITIES
    )
    assert result["step_id"] == "algorithm"

    # Accept default controller knobs -> confirm form.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "confirm"
    return result


async def test_full_wizard_creates_entry(
    hass: HomeAssistant, register_sources: None
) -> None:
    """The wizard creates an entry with both rooms and the global mapping."""
    result = await _drive_to_confirm(hass, _LAT, _LON)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_LATITUDE] == _LAT
    assert data[CONF_LONGITUDE] == _LON
    assert data[CONF_ENTITY_MODE] == "input_select.home_mode"
    assert CONF_CONTROLLER in data

    rooms = data[CONF_ROOMS]
    assert [r[CONF_ROOM_NAME] for r in rooms] == ["Salon", "Lazienka"]

    salon, lazienka = rooms
    assert salon[CONF_ENTITY_FAST_SOURCE] == "climate.salon_split"
    assert salon[CONF_COOLING_ENABLED] is True
    assert salon[CONF_ENTITY_VALVES] == ["number.salon_valve"]
    # The multisplit group key persists with the room (K4, 2026-07-12).
    assert salon[CONF_FAST_SOURCE_GROUP] == "outdoor_unit_a"
    # The outdoor sensor is global but fanned out to every room.
    assert salon[CONF_ENTITY_TEMP_OUTDOOR] == "sensor.outdoor_temp"
    assert lazienka[CONF_ENTITY_TEMP_OUTDOOR] == "sensor.outdoor_temp"
    # A floor-only room carries no fast-source entity and no group.
    assert CONF_ENTITY_FAST_SOURCE not in lazienka
    assert lazienka[CONF_FAST_SOURCE_GROUP] == ""


async def test_duplicate_location_aborts(
    hass: HomeAssistant, register_sources: None, mock_entry: MockConfigEntry
) -> None:
    """A second setup for the same lat/lon aborts as already_configured."""
    assert mock_entry.unique_id == f"{_LAT}_{_LON}"

    result = await _drive_to_confirm(hass, _LAT, _LON)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cooling_room_without_humidity_errors(
    hass: HomeAssistant, register_sources: None
) -> None:
    """A cooling-enabled room missing its humidity sensor is rejected."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LATITUDE: _LAT, CONF_LONGITUDE: _LON}
    )
    # One cooling room, no add_another -> straight to its entities form.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "Salon",
            CONF_ROOM_AREA: 30.0,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: True,
            CONF_ADD_ANOTHER: False,
        },
    )
    assert result["step_id"] == "entities"

    # Submit valves + temperature but omit the humidity sensor.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_TEMP_ROOM: "sensor.salon_temp",
            CONF_ENTITY_VALVES: ["number.salon_valve"],
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "entities"
    assert result["errors"] == {"base": "humidity_required"}


async def test_options_menu_lists_all_leaves(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The options entry point is a menu with the four room/settings leaves."""
    entry = setup_integration

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert set(result["menu_options"]) == {
        "add_room",
        "edit_room",
        "remove_room",
        "settings",
    }


async def test_options_flow_saves_room_state_and_knobs(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The settings leaf renders a per-room state select + knobs and persists."""
    entry = setup_integration

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    # Pick the settings leaf from the menu.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"

    schema_keys = {str(key) for key in result["data_schema"].schema}
    # One index-keyed control-state select per room, plus the advanced knobs.
    # The retired kill switch is gone from this form.
    assert "room_state_0" in schema_keys
    assert "room_state_1" in schema_keys
    assert "kill_switch" not in schema_keys
    assert "kp" in schema_keys

    # Off / shadow are write-free, so this settings save (which reloads the
    # entry) never drives an actuator during the ensuing refresh.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"room_state_0": ROOM_STATE_OFF, "room_state_1": ROOM_STATE_SHADOW},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Salon (index 0) switched off, Lazienka (index 1) left in shadow.
    assert entry.options[CONF_ROOM_STATE] == {
        "Salon": ROOM_STATE_OFF,
        "Lazienka": ROOM_STATE_SHADOW,
    }
    assert CONF_CONTROLLER in entry.options


async def test_settings_save_preserves_room_tuning(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Saving the settings leaf preserves the sparse per-room tuning map.

    The settings form manages only the control-state map and the global knobs;
    it must not wipe ``CONF_ROOM_TUNING`` (per-room overrides set from the panel).
    An options flow's ``async_create_entry`` replaces ``entry.options`` wholesale,
    so the step merges the existing options.
    """
    entry = setup_integration
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_ROOM_TUNING: {"Salon": {"kp": 12.0}}},
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"room_state_0": ROOM_STATE_OFF, "room_state_1": ROOM_STATE_SHADOW},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The per-room tuning override survives the settings save (not wiped).
    assert entry.options.get(CONF_ROOM_TUNING) == {"Salon": {"kp": 12.0}}


async def _open_menu_leaf(
    hass: HomeAssistant, entry: MockConfigEntry, leaf: str
) -> Any:
    """Open the options menu and select ``leaf``; return the resulting step."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": leaf}
    )


async def test_options_flow_add_room(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Adding a room appends it to CONF_ROOMS (with the global outdoor sensor)."""
    entry = setup_integration
    hass.states.async_set("sensor.kuchnia_temp", "21.0", _TEMP_ATTRS)
    hass.states.async_set("number.kuchnia_valve", "0", _PCT_ATTRS)

    result = await _open_menu_leaf(hass, entry, "add_room")
    assert result["step_id"] == "add_room"

    # Room attributes -> its entity-mapping step.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "Kuchnia",
            CONF_ROOM_AREA: 15.0,
            CONF_ROOM_OFFSET: 0.5,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: False,
        },
    )
    assert result["step_id"] == "room_entities"
    assert result["description_placeholders"]["room_name"] == "Kuchnia"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_TEMP_ROOM: "sensor.kuchnia_temp",
            CONF_ENTITY_VALVES: ["number.kuchnia_valve"],
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    rooms = entry.data[CONF_ROOMS]
    assert [r[CONF_ROOM_NAME] for r in rooms] == ["Salon", "Lazienka", "Kuchnia"]
    kuchnia = rooms[-1]
    assert kuchnia[CONF_ROOM_AREA] == 15.0
    assert kuchnia[CONF_ROOM_OFFSET] == 0.5
    assert kuchnia[CONF_ENTITY_VALVES] == ["number.kuchnia_valve"]
    # The global outdoor sensor is fanned out into the new room too.
    assert kuchnia[CONF_ENTITY_TEMP_OUTDOOR] == "sensor.outdoor_temp"
    # A floor-only room carries no fast-source entity.
    assert CONF_ENTITY_FAST_SOURCE not in kuchnia


async def test_options_flow_add_room_rejects_duplicate_name(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Adding a room whose name already exists is rejected."""
    entry = setup_integration

    result = await _open_menu_leaf(hass, entry, "add_room")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "Salon",
            CONF_ROOM_AREA: 15.0,
            CONF_ROOM_OFFSET: 0.0,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: False,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_room"
    assert result["errors"] == {"base": "duplicate_room_name"}


async def test_options_flow_edit_room(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Editing a room updates its attributes / entities in place (name fixed)."""
    entry = setup_integration
    # Offset is add-only: the edit form omits it (runtime offset lives in the
    # coordinator's Store), so an edit must preserve the configured value.
    original_offset = entry.data[CONF_ROOMS][1][CONF_ROOM_OFFSET]

    result = await _open_menu_leaf(hass, entry, "edit_room")
    assert result["step_id"] == "edit_room"

    # Pick Lazienka -> its attribute form (name immutable, so not shown).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SELECTED_ROOM: "Lazienka"}
    )
    assert result["step_id"] == "edit_room_attrs"
    assert result["description_placeholders"]["room_name"] == "Lazienka"
    attr_keys = {str(key) for key in result["data_schema"].schema}
    assert CONF_ROOM_NAME not in attr_keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_AREA: 6.0,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: False,
        },
    )
    assert result["step_id"] == "room_entities"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_TEMP_ROOM: "sensor.lazienka_temp",
            CONF_ENTITY_VALVES: ["number.lazienka_valve"],
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    rooms = entry.data[CONF_ROOMS]
    # Same rooms, same order, same names — only the edited attributes changed.
    assert [r[CONF_ROOM_NAME] for r in rooms] == ["Salon", "Lazienka"]
    lazienka = rooms[1]
    # Offset is immutable in edit — preserved from the original config.
    assert lazienka[CONF_ROOM_OFFSET] == original_offset
    assert lazienka[CONF_ENTITY_TEMP_ROOM] == "sensor.lazienka_temp"
    assert lazienka[CONF_ENTITY_VALVES] == ["number.lazienka_valve"]


async def test_options_flow_remove_room_cleans_registry(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Removing a room drops it from CONF_ROOMS, its entities and its device."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entry = setup_integration

    # Seed a control-state map so the prune of the removed room is observable.
    # Both rooms stay in shadow (write-free), so the reloaded coordinator emits
    # no valve writes; the seed matches the in-memory state, so it does not even
    # force a reload here.
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_ROOM_STATE: {
                "Salon": ROOM_STATE_SHADOW,
                "Lazienka": ROOM_STATE_SHADOW,
            },
        },
    )
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    lazienka_prefix = f"{entry.entry_id}_lazienka_"
    before = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.startswith(lazienka_prefix)
    ]
    assert before, "expected Lazienka entities in the registry before removal"

    result = await _open_menu_leaf(hass, entry, "remove_room")
    assert result["step_id"] == "remove_room"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SELECTED_ROOM: "Lazienka"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Gone from the config entry.
    rooms = entry.data[CONF_ROOMS]
    assert [r[CONF_ROOM_NAME] for r in rooms] == ["Salon"]

    # Gone from the entity registry (and not recreated on reload).
    after = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.startswith(lazienka_prefix)
    ]
    assert after == []

    # The surviving room keeps its entities.
    salon_prefix = f"{entry.entry_id}_salon_"
    salon_after = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.startswith(salon_prefix)
    ]
    assert salon_after

    # The control-state map no longer mentions the removed room; Salon survives.
    room_state = entry.options.get(CONF_ROOM_STATE, {})
    assert "Lazienka" not in room_state
    assert room_state.get("Salon") == ROOM_STATE_SHADOW

    # The removed room's device is gone too; Salon's device survives.
    device_registry = dr.async_get(hass)
    assert (
        device_registry.async_get_device(
            identifiers={(DOMAIN, f"{entry.entry_id}_lazienka")}
        )
        is None
    )
    assert (
        device_registry.async_get_device(
            identifiers={(DOMAIN, f"{entry.entry_id}_salon")}
        )
        is not None
    )


async def test_options_flow_remove_last_room_blocked(
    hass: HomeAssistant, register_sources: None
) -> None:
    """Removing the only remaining room aborts (at least one is required)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LATITUDE: _LAT,
            CONF_LONGITUDE: _LON,
            CONF_ENTITY_MODE: "input_select.home_mode",
            CONF_ROOMS: [
                {
                    CONF_ROOM_NAME: "Salon",
                    CONF_ROOM_AREA: 30.0,
                    CONF_ENTITY_TEMP_ROOM: "sensor.salon_temp",
                    CONF_ENTITY_VALVES: ["number.salon_valve"],
                    CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
                    CONF_COOLING_ENABLED: False,
                }
            ],
        },
        options={},
        title="Tortoise-UFH",
        unique_id=f"{_LAT}_{_LON}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await _open_menu_leaf(hass, entry, "remove_room")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_remove_last_room"


async def test_entity_validator_rejects_wrong_unit(hass: HomeAssistant) -> None:
    """A temperature sensor reporting °F is rejected with invalid_unit."""
    hass.states.async_set(
        "sensor.bad_temp",
        "70.0",
        {"unit_of_measurement": "°F", "device_class": "temperature"},
    )
    validator = EntityValidator(hass)

    result = validator.validate_entity(
        "sensor.bad_temp",
        valid_units=VALID_TEMP_UNITS,
        expected_device_class="temperature",
    )

    assert result.valid is False
    assert result.error_key == "invalid_unit"


async def _drive_to_first_room_entities(hass: HomeAssistant) -> Any:
    """Drive the wizard to the single floor-only room's entities form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LATITUDE: _LAT, CONF_LONGITUDE: _LON}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "Salon",
            CONF_ROOM_AREA: 30.0,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: False,
            CONF_ADD_ANOTHER: False,
        },
    )
    assert result["step_id"] == "entities"
    return result


async def test_valve_domain_actuator_accepted(
    hass: HomeAssistant, register_sources: None
) -> None:
    """A position-capable ``valve`` actuator passes the entities step."""
    # supported_features 7 = OPEN|CLOSE|SET_POSITION.
    hass.states.async_set(
        "valve.salon_loop", "open", {"current_position": 50, "supported_features": 7}
    )
    result = await _drive_to_first_room_entities(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_TEMP_ROOM: "sensor.salon_temp",
            CONF_ENTITY_VALVES: ["valve.salon_loop"],
        },
    )

    # The only room, so a clean entities submit advances to the algorithm step.
    assert result["step_id"] == "algorithm"


async def test_valve_without_set_position_rejected(
    hass: HomeAssistant, register_sources: None
) -> None:
    """A ``valve`` lacking SET_POSITION is rejected with valve_no_set_position."""
    # supported_features 3 = OPEN|CLOSE only (no SET_POSITION bit 4).
    hass.states.async_set("valve.salon_no_pos", "closed", {"supported_features": 3})
    result = await _drive_to_first_room_entities(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_TEMP_ROOM: "sensor.salon_temp",
            CONF_ENTITY_VALVES: ["valve.salon_no_pos"],
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "entities"
    assert result["errors"] == {"base": "valve_no_set_position"}
