"""Integration tests for the Tortoise-UFH Home Assistant services.

Exercises the three whole-home services registered by
``custom_components.tortoise_ufh.services`` against a live coordinator (set up
through the shared ``setup_integration`` fixture), asserting they mutate the
coordinator's authoritative setpoint/mode state and enforce their schemas.
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tortoise_ufh.const import DOMAIN
from custom_components.tortoise_ufh.core.models import Mode
from custom_components.tortoise_ufh.services import (
    SERVICE_SET_HOME_TEMPERATURE,
    SERVICE_SET_MODE,
    SERVICE_SET_ROOM_OFFSET,
)

pytestmark = pytest.mark.ha


async def test_services_registered(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """All three whole-home services are registered under the domain."""
    assert hass.services.has_service(DOMAIN, SERVICE_SET_HOME_TEMPERATURE)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_ROOM_OFFSET)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_MODE)


async def test_set_home_temperature_changes_getter(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """set_home_temperature updates the coordinator's home temperature."""
    coordinator = setup_integration.runtime_data.coordinator
    assert coordinator.get_home_temperature() == pytest.approx(21.0)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_HOME_TEMPERATURE,
        {"temperature": 23.5},
        blocking=True,
    )

    assert coordinator.get_home_temperature() == pytest.approx(23.5)


async def test_set_room_offset_known_room(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """set_room_offset updates the offset for a configured room."""
    coordinator = setup_integration.runtime_data.coordinator
    assert coordinator.get_room_offset("Salon") == pytest.approx(0.0)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_ROOM_OFFSET,
        {"room": "Salon", "offset": 2.0},
        blocking=True,
    )

    assert coordinator.get_room_offset("Salon") == pytest.approx(2.0)


async def test_set_room_offset_unknown_room_raises(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An unknown room name is rejected with ServiceValidationError."""
    coordinator = setup_integration.runtime_data.coordinator

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_ROOM_OFFSET,
            {"room": "DoesNotExist", "offset": 1.0},
            blocking=True,
        )

    # The failed call left the real rooms untouched.
    assert coordinator.get_room_offset("Salon") == pytest.approx(0.0)


async def test_set_mode_changes_getter(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """set_mode overrides the coordinator's active mode."""
    coordinator = setup_integration.runtime_data.coordinator
    assert coordinator.get_mode() is Mode.HEATING

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_MODE,
        {"mode": "cooling"},
        blocking=True,
    )

    assert coordinator.get_mode() is Mode.COOLING


async def test_set_home_temperature_out_of_range_rejected(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A temperature above HOME_SETPOINT_MAX_C is rejected by the schema."""
    coordinator = setup_integration.runtime_data.coordinator

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOME_TEMPERATURE,
            {"temperature": 50.0},
            blocking=True,
        )

    # Schema rejection means the setter never ran.
    assert coordinator.get_home_temperature() == pytest.approx(21.0)
