"""Smoke tests: the Tortoise-UFH integration sets up, registers, and unloads."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.tortoise_ufh.const import DOMAIN

pytestmark = pytest.mark.ha


async def test_setup_and_unload(hass: HomeAssistant, setup_integration) -> None:
    """The entry loads to LOADED with runtime_data, then unloads cleanly."""
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not None
    assert entry.runtime_data.coordinator is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_services_registered(hass: HomeAssistant, setup_integration) -> None:
    """The three whole-home services are registered on setup."""
    for service in ("set_home_temperature", "set_room_offset", "set_mode"):
        assert hass.services.has_service(DOMAIN, service)


async def test_coordinator_produced_data(
    hass: HomeAssistant, setup_integration
) -> None:
    """The first refresh produced per-room runtime data for both rooms."""
    coordinator = setup_integration.runtime_data.coordinator
    assert coordinator.data is not None
    assert set(coordinator.data.rooms) == {"Salon", "Lazienka"}
