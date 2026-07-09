"""Smoke tests: the Tortoise-UFH integration sets up, registers, and unloads."""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.tortoise_ufh.const import DOMAIN, ROOM_STATE_LIVE

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


# -- Phase A hardening: farewell on unload (C5) + mode persistence (S9) ------


async def test_unload_parks_live_room_actuators(
    hass: HomeAssistant, setup_integration
) -> None:
    """C5: unloading the entry sends the farewell for every LIVE room."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    coordinator = setup_integration.runtime_data.coordinator
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    hvac_calls = async_mock_service(hass, "climate", "set_hvac_mode")

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert [(c.data["entity_id"], c.data["hvac_mode"]) for c in hvac_calls] == [
        ("climate.salon_split", "off")
    ]


async def test_unload_shadow_rooms_send_nothing(
    hass: HomeAssistant, setup_integration
) -> None:
    """C5: unloading with only shadow rooms emits no farewell writes."""
    from pytest_homeassistant_custom_component.common import async_mock_service

    hvac_calls = async_mock_service(hass, "climate", "set_hvac_mode")
    valve_calls = async_mock_service(hass, "number", "set_value")

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert hvac_calls == []
    assert valve_calls == []


async def test_persisted_mode_restored_on_restart(
    hass: HomeAssistant,
    register_sources: None,
    entry_data: dict,
    hass_storage: dict,
) -> None:
    """S9: with no mode entity, a stored COOLING mode survives a restart."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.tortoise_ufh.const import CONF_ENTITY_MODE

    data = dict(entry_data)
    data.pop(CONF_ENTITY_MODE)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        options={},
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
    )
    entry.add_to_hass(hass)
    key = f"{DOMAIN}.setpoints.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"home_setpoint": 22.0, "room_offset": {}, "mode": "cooling"},
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data.mode == "cooling"
    assert coordinator.get_home_temperature() == 22.0
