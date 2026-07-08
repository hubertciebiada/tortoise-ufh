"""Integration tests for the Tortoise-UFH ``number`` platform.

Covers the single global "home temperature" control and the per-room "offset"
controls: their locked min/max/step/unit, that writing them mutates the
coordinator's authoritative setpoint state, and that the room setpoint tracks
``home_temperature + room_offset``.
"""

from __future__ import annotations

import pytest
from homeassistant.components.number import (
    ATTR_MAX,
    ATTR_MIN,
    ATTR_STEP,
    ATTR_VALUE,
    SERVICE_SET_VALUE,
)
from homeassistant.components.number import (
    DOMAIN as NUMBER_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tortoise_ufh.const import (
    HOME_SETPOINT_MAX_C,
    HOME_SETPOINT_MIN_C,
    HOME_SETPOINT_STEP_C,
    ROOM_OFFSET_MAX_C,
    ROOM_OFFSET_MIN_C,
    ROOM_OFFSET_STEP_C,
)

pytestmark = pytest.mark.ha


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, unique_suffix: str) -> str:
    """Resolve one of the integration's number entities via the registry."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        NUMBER_DOMAIN, "tortoise_ufh", f"{entry.entry_id}_{unique_suffix}"
    )
    assert entity_id is not None, f"no number entity for {unique_suffix!r}"
    return entity_id


async def _set_value(hass: HomeAssistant, entity_id: str, value: float) -> None:
    """Drive the real ``number.set_value`` service and let it settle."""
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_home_temperature_entity_bounds(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The single global home-temperature control exists with locked bounds."""
    entity_id = _entity_id(hass, setup_integration, "home_temperature")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes[ATTR_MIN] == HOME_SETPOINT_MIN_C == 5.0
    assert state.attributes[ATTR_MAX] == HOME_SETPOINT_MAX_C == 30.0
    assert state.attributes[ATTR_STEP] == HOME_SETPOINT_STEP_C == 0.5
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "°C"
    # Seeded from CONF_HOME_SETPOINT in entry_data.
    assert float(state.state) == 21.0


async def test_room_offset_entities_exist_with_bounds(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """One offset control per room, in kelvin, with the locked -5..+5 range."""
    for room in ("salon", "lazienka"):
        entity_id = _entity_id(hass, setup_integration, f"{room}_offset")
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes[ATTR_MIN] == ROOM_OFFSET_MIN_C == -5.0
        assert state.attributes[ATTR_MAX] == ROOM_OFFSET_MAX_C == 5.0
        assert state.attributes[ATTR_STEP] == ROOM_OFFSET_STEP_C == 0.5
        assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "K"

    # Offsets are seeded per-room from entry_data (Salon 0.0, Lazienka 1.0).
    salon = hass.states.get(_entity_id(hass, setup_integration, "salon_offset"))
    lazienka = hass.states.get(_entity_id(hass, setup_integration, "lazienka_offset"))
    assert salon is not None and float(salon.state) == 0.0
    assert lazienka is not None and float(lazienka.state) == 1.0


async def test_set_home_temperature_updates_coordinator_and_setpoints(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Setting the home control mutates the coordinator and every setpoint."""
    coordinator = setup_integration.runtime_data.coordinator
    entity_id = _entity_id(hass, setup_integration, "home_temperature")

    await _set_value(hass, entity_id, 23.0)

    assert coordinator.get_home_temperature() == 23.0
    assert float(hass.states.get(entity_id).state) == 23.0
    # Room setpoint = home + offset (Salon offset 0.0, Lazienka offset 1.0).
    assert coordinator.get_room_setpoint("Salon") == 23.0
    assert coordinator.get_room_setpoint("Lazienka") == 24.0


async def test_set_room_offset_updates_only_that_room(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Writing a room offset moves only that room's setpoint by the offset."""
    coordinator = setup_integration.runtime_data.coordinator
    salon_offset = _entity_id(hass, setup_integration, "salon_offset")

    await _set_value(hass, salon_offset, -2.0)

    assert coordinator.get_room_offset("Salon") == -2.0
    assert float(hass.states.get(salon_offset).state) == -2.0
    # home (21.0) + salon offset (-2.0); Lazienka untouched (21.0 + 1.0).
    assert coordinator.get_room_setpoint("Salon") == 19.0
    assert coordinator.get_room_setpoint("Lazienka") == 22.0
    assert coordinator.get_room_offset("Lazienka") == 1.0


async def test_set_values_stay_within_range(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Boundary values are accepted; the coordinator stores them verbatim."""
    coordinator = setup_integration.runtime_data.coordinator
    home = _entity_id(hass, setup_integration, "home_temperature")
    salon_offset = _entity_id(hass, setup_integration, "salon_offset")

    await _set_value(hass, home, HOME_SETPOINT_MAX_C)
    await _set_value(hass, salon_offset, ROOM_OFFSET_MIN_C)

    assert coordinator.get_home_temperature() == HOME_SETPOINT_MAX_C
    assert coordinator.get_room_offset("Salon") == ROOM_OFFSET_MIN_C
    setpoint = coordinator.get_room_setpoint("Salon")
    assert setpoint == HOME_SETPOINT_MAX_C + ROOM_OFFSET_MIN_C == 25.0
