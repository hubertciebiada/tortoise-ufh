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


# -- Round 3 hardening (2026-07-12): the unload window (K3) + Store flush (K5)


async def test_no_actuator_writes_inside_the_unload_window(
    hass: HomeAssistant, setup_integration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K3: a pending recompute may not re-command actuators after farewell.

    Regression for the confirmed race: a setpoint nudge armed the 2-s
    recompute debouncer, the unload's farewell parked the cooling valve at 0,
    and the timer then fired INSIDE the unload window (the ``async_on_unload``
    cancellations only run after ``async_unload_entry`` returns) — re-opening
    the orphaned valve (measured 53 -> 0 -> 81 %). Now the coordinator is
    shut down BEFORE the farewell and the ``_parked`` flag gates the write
    loop as a second belt.
    """
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import (
        async_fire_time_changed,
        async_mock_service,
    )

    entry = setup_integration
    coordinator = entry.runtime_data.coordinator

    # A cooling scenario with a wide-open valve on a LIVE room.
    hass.states.async_set("sensor.salon_temp", "25.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.salon_supply", "18.0", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "input_select.home_mode",
        "cooling",
        {"options": ["heating", "transitional", "cooling", "off"]},
    )
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    valve_calls = async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert valve_calls, "the cooling cycle must have commanded the valve"
    assert valve_calls[-1].data["value"] > 0.0

    # Arm the 2-s recompute debouncer just before the unload...
    coordinator.set_home_temperature(20.0)
    await hass.async_block_till_done()

    orig = hass.config_entries.async_unload_platforms

    async def unload_platforms_with_timer_burst(entry_arg, platforms):
        # ...and make the timer land HERE — inside the unload window, after
        # the farewell already parked the valve.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
        for _ in range(10):
            await hass.async_block_till_done(wait_background_tasks=True)
        return await orig(entry_arg, platforms)

    monkeypatch.setattr(
        hass.config_entries, "async_unload_platforms", unload_platforms_with_timer_burst
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    salon_values = [
        c.data["value"]
        for c in valve_calls
        if c.data["entity_id"] == "number.salon_valve"
    ]
    assert 0.0 in salon_values, "the farewell must park the cooling valve"
    after_farewell = salon_values[salon_values.index(0.0) + 1 :]
    assert after_farewell == [], (
        f"valve re-commanded inside the unload window: {after_farewell}"
    )


async def test_setpoint_changed_just_before_reload_survives(
    hass: HomeAssistant, setup_integration
) -> None:
    """K5: a setpoint <1 s old is flushed to the Store before the unload.

    The Store's 1-s delayed save used to race the reload: the new
    coordinator read the file before the timer flushed, silently reverting
    the change (confirmed 23.5 -> 21.0).
    """
    from pytest_homeassistant_custom_component.common import async_mock_service

    entry = setup_integration
    async_mock_service(hass, "number", "set_value")
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")

    entry.runtime_data.coordinator.set_home_temperature(23.5)
    # No time advance: the delayed save has NOT fired yet.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.runtime_data.coordinator.get_home_temperature() == 23.5
