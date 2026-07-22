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
    ROOM_STATE_LIVE,
    ROOM_STATE_OFF,
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
    """The options entry point is a menu with the room/settings/pump leaves."""
    entry = setup_integration

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert set(result["menu_options"]) == {
        "add_room",
        "edit_room",
        "remove_room",
        "settings",
        "heat_pump",
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

    # Lazienka goes live, so the reloaded entry's first refresh writes its
    # actuators — capture the writes instead of dispatching them.
    from pytest_homeassistant_custom_component.common import async_mock_service

    async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"room_state_0": ROOM_STATE_OFF, "room_state_1": ROOM_STATE_LIVE},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Salon (index 0) stays off, Lazienka (index 1) promoted to live.
    assert entry.options[CONF_ROOM_STATE] == {
        "Salon": ROOM_STATE_OFF,
        "Lazienka": ROOM_STATE_LIVE,
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
        {"room_state_0": ROOM_STATE_OFF, "room_state_1": ROOM_STATE_OFF},
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
    # Both rooms stay off (write-free), so the reloaded coordinator emits
    # no valve writes; the seed matches the in-memory state, so it does not even
    # force a reload here.
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_ROOM_STATE: {
                "Salon": ROOM_STATE_OFF,
                "Lazienka": ROOM_STATE_OFF,
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
    assert room_state.get("Salon") == ROOM_STATE_OFF

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


# -- Round 3 hardening (2026-07-12): K8 tuning prune, K10 slug, D2 group -----


async def test_options_flow_add_room_rejects_slug_collision(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """K10: a name differing only in case/spacing collides in the registry.

    Unique ids and device identifiers are built from the slug (lower +
    spaces->underscores): "salon" next to "Salon" would collide unique ids
    and remove-room would delete BOTH rooms' entities.
    """
    entry = setup_integration

    result = await _open_menu_leaf(hass, entry, "add_room")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "salon",
            CONF_ROOM_AREA: 15.0,
            CONF_ROOM_OFFSET: 0.0,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: False,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_room"
    assert result["errors"] == {"base": "duplicate_room_slug"}


async def test_wizard_rejects_slug_collision(hass: HomeAssistant) -> None:
    """K10: the setup wizard applies the same slug-uniqueness gate."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LATITUDE: _LAT, CONF_LONGITUDE: _LON}
    )
    assert result["step_id"] == "rooms"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "Kids Room",
            CONF_ROOM_AREA: 12.0,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: False,
            CONF_ADD_ANOTHER: True,
        },
    )
    assert result["step_id"] == "rooms"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "kids room",
            CONF_ROOM_AREA: 12.0,
            CONF_HAS_FAST_SOURCE: False,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_NONE,
            CONF_COOLING_ENABLED: False,
            CONF_ADD_ANOTHER: False,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "duplicate_room_slug"}


async def test_remove_room_prunes_room_tuning_override(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """K8: removing a room drops its sparse tuning override too.

    Without the prune a NEW room reusing the name silently inherited the old
    room's kp/ki — a real regulation risk.
    """
    entry = setup_integration
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_ROOM_TUNING: {"Lazienka": {"kp": 30.0}, "Salon": {"kt": 8.0}},
        },
    )
    await hass.async_block_till_done()

    result = await _open_menu_leaf(hass, entry, "remove_room")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SELECTED_ROOM: "Lazienka"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    room_tuning = entry.options.get(CONF_ROOM_TUNING, {})
    assert "Lazienka" not in room_tuning
    assert room_tuning.get("Salon") == {"kt": 8.0}


async def test_add_room_heater_group_is_silently_cleared(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """D2: a group on a non-split fast source is dropped, not an error.

    Only splits share a multisplit outdoor unit; the flow clears the group
    exactly like it already did for ``has_fast_source=False`` instead of
    surfacing an unexplained generic ``invalid_room``.
    """
    from custom_components.tortoise_ufh.const import FAST_SOURCE_KIND_HEATER

    entry = setup_integration
    hass.states.async_set("sensor.gabinet_temp", "21.0", _TEMP_ATTRS)
    hass.states.async_set("number.gabinet_valve", "0", _PCT_ATTRS)
    hass.states.async_set("climate.gabinet_heater", "off", {})

    result = await _open_menu_leaf(hass, entry, "add_room")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "Gabinet",
            CONF_ROOM_AREA: 10.0,
            CONF_ROOM_OFFSET: 0.0,
            CONF_HAS_FAST_SOURCE: True,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_HEATER,
            CONF_FAST_SOURCE_GROUP: "outdoor_unit_a",
            CONF_COOLING_ENABLED: False,
        },
    )
    assert result["step_id"] == "room_entities"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_TEMP_ROOM: "sensor.gabinet_temp",
            CONF_ENTITY_VALVES: ["number.gabinet_valve"],
            CONF_ENTITY_FAST_SOURCE: "climate.gabinet_heater",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    gabinet = entry.data[CONF_ROOMS][-1]
    assert gabinet[CONF_ROOM_NAME] == "Gabinet"
    assert gabinet[CONF_FAST_SOURCE_GROUP] == ""


async def test_options_flow_heat_pump_saves_and_clears_entities(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The heat-pump leaf persists only the filled pickers; empty removes all."""
    from custom_components.tortoise_ufh.const import (
        CONF_ENTITY_HP_COOLING_SETPOINT,
        CONF_ENTITY_HP_MODE,
        CONF_HEAT_PUMP,
    )

    entry = setup_integration
    result = await _open_menu_leaf(hass, entry, "heat_pump")
    assert result["step_id"] == "heat_pump"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_HP_MODE: "select.heat_pump_mode",
            CONF_ENTITY_HP_COOLING_SETPOINT: "number.z1_cool_request_temp",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HEAT_PUMP] == {
        CONF_ENTITY_HP_MODE: "select.heat_pump_mode",
        CONF_ENTITY_HP_COOLING_SETPOINT: "number.z1_cool_request_temp",
    }

    # Clearing every picker removes the whole section (feature fully off).
    result = await _open_menu_leaf(hass, entry, "heat_pump")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_HEAT_PUMP not in entry.options


async def test_add_room_quiet_window_pair_validated(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A lone quiet-hours time (or start == end) raises quiet_window_invalid."""
    from custom_components.tortoise_ufh.const import (
        CONF_FAST_WINDOW_END,
        CONF_FAST_WINDOW_START,
    )

    entry = setup_integration
    result = await _open_menu_leaf(hass, entry, "add_room")

    base = {
        CONF_ROOM_NAME: "Gabinet",
        CONF_ROOM_AREA: 10.0,
        CONF_ROOM_OFFSET: 0.0,
        CONF_HAS_FAST_SOURCE: True,
        CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_SPLIT,
        CONF_COOLING_ENABLED: False,
    }
    # Only the start set -> rejected.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, CONF_FAST_WINDOW_START: "07:00:00"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "quiet_window_invalid"}

    # start == end -> rejected too (degenerate window).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **base,
            CONF_FAST_WINDOW_START: "07:00:00",
            CONF_FAST_WINDOW_END: "07:00:00",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "quiet_window_invalid"}


async def test_add_room_quiet_window_normalised_and_persisted(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A valid HH:MM:SS window pair is stored normalised to HH:MM."""
    from custom_components.tortoise_ufh.const import (
        CONF_FAST_WINDOW_END,
        CONF_FAST_WINDOW_START,
    )

    entry = setup_integration
    hass.states.async_set("sensor.gabinet_temp", "21.0", _TEMP_ATTRS)
    hass.states.async_set("number.gabinet_valve", "0", _PCT_ATTRS)

    result = await _open_menu_leaf(hass, entry, "add_room")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ROOM_NAME: "Gabinet",
            CONF_ROOM_AREA: 10.0,
            CONF_ROOM_OFFSET: 0.0,
            CONF_HAS_FAST_SOURCE: True,
            CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_SPLIT,
            CONF_COOLING_ENABLED: False,
            CONF_FAST_WINDOW_START: "07:00:00",
            CONF_FAST_WINDOW_END: "22:30:00",
        },
    )
    assert result["step_id"] == "room_entities"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENTITY_TEMP_ROOM: "sensor.gabinet_temp",
            CONF_ENTITY_VALVES: ["number.gabinet_valve"],
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    gabinet = entry.data[CONF_ROOMS][-1]
    assert gabinet[CONF_FAST_WINDOW_START] == "07:00"
    assert gabinet[CONF_FAST_WINDOW_END] == "22:30"
