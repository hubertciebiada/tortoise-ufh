"""Config-flow and options-flow behaviour tests for Tortoise-UFH.

Drives the full setup wizard (location -> rooms -> per-room entities ->
algorithm -> confirm) to a created entry, exercises the options flow
(per-room live control + kill switch + advanced knobs), and pins the locked
guard rails: duplicate-location abort, the cooling-room ``humidity_required``
error, and :class:`EntityValidator` unit rejection.
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
    CONF_FAST_SOURCE_KIND,
    CONF_KILL_SWITCH,
    CONF_LIVE_CONTROL,
    CONF_ROOM_AREA,
    CONF_ROOM_NAME,
    CONF_ROOMS,
    DOMAIN,
    FAST_SOURCE_KIND_NONE,
    FAST_SOURCE_KIND_SPLIT,
    VALID_TEMP_UNITS,
)
from custom_components.tortoise_ufh.entity_validator import EntityValidator

pytestmark = pytest.mark.ha

_LAT = 50.5
_LON = 19.5

_SALON_ROOM: dict[str, Any] = {
    CONF_ROOM_NAME: "Salon",
    CONF_ROOM_AREA: 30.0,
    CONF_HAS_FAST_SOURCE: True,
    CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_SPLIT,
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
    # The outdoor sensor is global but fanned out to every room.
    assert salon[CONF_ENTITY_TEMP_OUTDOOR] == "sensor.outdoor_temp"
    assert lazienka[CONF_ENTITY_TEMP_OUTDOOR] == "sensor.outdoor_temp"
    # A floor-only room carries no fast-source entity.
    assert CONF_ENTITY_FAST_SOURCE not in lazienka


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


async def test_options_flow_saves_live_control_and_kill_switch(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The options form renders per-room + kill + knobs and persists them."""
    entry = setup_integration

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    schema_keys = {str(key) for key in result["data_schema"].schema}
    # One live-control toggle per room (index-keyed), the kill switch, and at
    # least one advanced controller knob.
    assert "enable_live_control_0" in schema_keys
    assert "enable_live_control_1" in schema_keys
    assert CONF_KILL_SWITCH in schema_keys
    assert "kp" in schema_keys

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"enable_live_control_0": True, CONF_KILL_SWITCH: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Salon (index 0) promoted to live, Lazienka stays in shadow.
    assert entry.options[CONF_LIVE_CONTROL] == {"Salon": True, "Lazienka": False}
    assert entry.options[CONF_KILL_SWITCH] is True
    assert CONF_CONTROLLER in entry.options


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
