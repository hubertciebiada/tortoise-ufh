"""Command-delivery regressions: unreachable actuators and restart grace.

Covers three gaps in the write path (2026-10-05):

* issue #11 — a write to a missing / ``unavailable`` valve (or heat-pump
  setpoint) entity is neither sent nor cached, so the first cycle that can
  reach the actuator again writes the current command;
* issue #10 — a farewell that could not reach the split (or, in COOLING, a
  valve) is retried every cycle while the room stays off, and dropped when the
  room returns to live;
* issue #13 — a room whose temperature has not reported since start-up is not
  stepped (no sensor-lost force-OFF of a running split) until it reports or
  the start-up grace runs out.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.tortoise_ufh.const import (
    CONF_ENTITY_VALVES,
    ROOM_STATE_LIVE,
    ROOM_STATE_OFF,
)
from custom_components.tortoise_ufh.core.models import Mode

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall
    from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.ha

_TEMP_ATTRS = {"unit_of_measurement": "°C", "device_class": "temperature"}
_PCT_ATTRS = {"unit_of_measurement": "%"}

_ACTUATOR_SERVICES = (
    ("number", "set_value"),
    ("valve", "set_valve_position"),
    ("climate", "set_hvac_mode"),
    ("climate", "set_temperature"),
)


def _coordinator(entry: MockConfigEntry) -> Any:
    """Return the live coordinator stored on the entry's runtime data."""
    return entry.runtime_data.coordinator


def _mock_actuators(
    hass: HomeAssistant,
) -> dict[tuple[str, str], list[ServiceCall]]:
    """Register capturing handlers for every actuator service."""
    return {
        (domain, service): async_mock_service(hass, domain, service)
        for domain, service in _ACTUATOR_SERVICES
    }


def _calls_to(calls: list[ServiceCall], entity_id: str) -> list[ServiceCall]:
    """Filter captured calls down to one target entity."""
    return [c for c in calls if c.data.get("entity_id") == entity_id]


async def _refresh(hass: HomeAssistant, coordinator: Any) -> None:
    """Force a full update cycle and let entities settle."""
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def _setup_live_salon(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
    hass_storage: dict[str, Any],
    *,
    mode: str = "transitional",
) -> MockConfigEntry:
    """Set up an entry whose Salon is LIVE from the very first cycle."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.tortoise_ufh.const import CONF_ROOM_STATE, DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options={CONF_ROOM_STATE: {"Salon": ROOM_STATE_LIVE}},
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
        version=4,
    )
    entry.add_to_hass(hass)
    store_key = f"{DOMAIN}.setpoints.{entry.entry_id}"
    hass_storage[store_key] = {
        "version": 1,
        "minor_version": 1,
        "key": store_key,
        "data": {"home_setpoint": 21.0, "room_offset": {}, "mode": mode},
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# -- Issue #11: unreachable valve / heat-pump writes are not cached ----------


async def test_valve_write_to_unavailable_actuator_is_not_cached(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A closing command missed while offline lands once the valve is back.

    The repro from the issue: the cache holds 60 %, the actuator drops out,
    the command goes to 0 % and the actuator returns ``unknown`` (no usable
    feedback). The 0 % must be written on that first reachable cycle, not
    after the 45-min re-assert.
    """
    coordinator = _coordinator(setup_integration)
    mocks = _mock_actuators(hass)
    outputs = coordinator.data.rooms["Salon"].outputs
    room_cfg = {CONF_ENTITY_VALVES: ["number.salon_valve"]}
    set_value = mocks[("number", "set_value")]

    await coordinator._write_valves(
        room_cfg, "Salon", replace(outputs, valve_position_pct=60.0)
    )
    assert [c.data["value"] for c in set_value] == [60.0]

    hass.states.async_set("number.salon_valve", "unavailable", _PCT_ATTRS)
    await coordinator._write_valves(
        room_cfg, "Salon", replace(outputs, valve_position_pct=0.0)
    )
    assert [c.data["value"] for c in set_value] == [60.0]
    assert coordinator._writer.last_written_valve("number.salon_valve") == 60.0

    hass.states.async_set("number.salon_valve", "unknown", _PCT_ATTRS)
    await coordinator._write_valves(
        room_cfg, "Salon", replace(outputs, valve_position_pct=0.0)
    )
    assert [c.data["value"] for c in set_value] == [60.0, 0.0]
    assert coordinator._writer.last_written_valve("number.salon_valve") == 0.0


async def test_valve_write_to_missing_actuator_is_skipped(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A configured valve entity that does not exist gets no command."""
    coordinator = _coordinator(setup_integration)
    mocks = _mock_actuators(hass)
    outputs = coordinator.data.rooms["Salon"].outputs
    await coordinator._write_valves(
        {CONF_ENTITY_VALVES: ["valve.ghost_loop"]}, "Salon", outputs
    )
    assert mocks[("valve", "set_valve_position")] == []
    assert coordinator._writer.last_written_valve("valve.ghost_loop") is None


async def test_hp_setpoint_to_unavailable_entity_is_not_cached(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The dew-point floor written to the pump never looks delivered offline."""
    coordinator = _coordinator(setup_integration)
    set_value = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("number.hp_cool", "unavailable", {})
    await coordinator._writer.write_hp_setpoint("number.hp_cool", 18.0)
    assert set_value == []
    assert "number.hp_cool" not in coordinator._writer._last_written_hp_setpoint

    hass.states.async_set("number.hp_cool", "20", {"step": 1.0})
    await coordinator._writer.write_hp_setpoint("number.hp_cool", 18.0)
    assert [c.data["value"] for c in set_value] == [18.0]


async def test_hp_mode_to_unavailable_select_is_not_written(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An unavailable pump-mode select reports the write as not issued."""
    coordinator = _coordinator(setup_integration)
    calls = async_mock_service(hass, "select", "select_option")
    hass.states.async_set("select.hp_mode", "unavailable", {})
    assert await coordinator._writer.write_hp_mode("select.hp_mode", "Cool") is False
    assert calls == []
    assert "select.hp_mode" not in coordinator._writer._last_written_hp_mode


# -- Issue #10: an undelivered farewell is retried while the room is off -----


async def test_farewell_off_is_retried_when_the_split_returns(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The split missed the farewell OFF; it gets it on its first return.

    The retry runs before the inputs are read, so the unit that came back
    running is read through the fresh farewell stamp: no mismatch on the off
    room, and nothing more is written once the OFF has landed.
    """
    coordinator = _coordinator(setup_integration)
    coordinator._mode = Mode.COOLING
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    mocks = _mock_actuators(hass)
    hvac = mocks[("climate", "set_hvac_mode")]

    hass.states.async_set("climate.salon_split", "unavailable", {})
    coordinator.set_room_state("Salon", ROOM_STATE_OFF)
    await hass.async_block_till_done()
    assert hvac == []
    assert coordinator._farewell_pending["Salon"].fast_source_pending is True

    # Still away: nothing is written, the marker stays.
    await _refresh(hass, coordinator)
    assert hvac == []
    assert "Salon" in coordinator._farewell_pending

    hass.states.async_set("climate.salon_split", "cool", {})
    await _refresh(hass, coordinator)
    assert [c.data["hvac_mode"] for c in hvac] == ["off"]
    assert "Salon" not in coordinator._farewell_pending
    flags = coordinator.data.rooms["Salon"].report.flags
    assert "fast_source_mismatch" not in flags
    assert "fast_source_manual" not in flags

    hass.states.async_set("climate.salon_split", "off", {})
    await _refresh(hass, coordinator)
    assert len(hvac) == 1
    assert "fast_source_mismatch" not in coordinator.data.rooms["Salon"].report.flags


async def test_cooling_valve_park_is_retried_when_the_valve_returns(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A COOLING farewell park that missed an offline valve lands later."""
    coordinator = _coordinator(setup_integration)
    coordinator._mode = Mode.COOLING
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    mocks = _mock_actuators(hass)
    salon_valve = "number.salon_valve"

    hass.states.async_set(salon_valve, "unavailable", _PCT_ATTRS)
    coordinator.set_room_state("Salon", ROOM_STATE_OFF)
    await hass.async_block_till_done()
    assert _calls_to(mocks[("number", "set_value")], salon_valve) == []
    assert coordinator._farewell_pending["Salon"].valves_pending == (salon_valve,)
    assert coordinator._writer.last_written_valve(salon_valve) is None

    hass.states.async_set(salon_valve, "60", _PCT_ATTRS)
    await _refresh(hass, coordinator)
    parks = _calls_to(mocks[("number", "set_value")], salon_valve)
    assert [c.data["value"] for c in parks] == [0.0]
    assert "Salon" not in coordinator._farewell_pending


async def test_pending_valve_park_is_dropped_outside_cooling(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Outside COOLING the valve is left holding: nothing is left to retry."""
    coordinator = _coordinator(setup_integration)
    coordinator._mode = Mode.COOLING
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    mocks = _mock_actuators(hass)

    hass.states.async_set("number.salon_valve", "unavailable", _PCT_ATTRS)
    coordinator.set_room_state("Salon", ROOM_STATE_OFF)
    await hass.async_block_till_done()
    assert "Salon" in coordinator._farewell_pending

    coordinator._mode = Mode.HEATING
    await _refresh(hass, coordinator)
    assert "Salon" not in coordinator._farewell_pending
    assert _calls_to(mocks[("number", "set_value")], "number.salon_valve") == []


async def test_return_to_live_drops_the_pending_farewell(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Back in live, the regular write path owns the split again."""
    coordinator = _coordinator(setup_integration)
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    _mock_actuators(hass)
    hass.states.async_set("climate.salon_split", "unavailable", {})
    coordinator.set_room_state("Salon", ROOM_STATE_OFF)
    await hass.async_block_till_done()
    assert "Salon" in coordinator._farewell_pending

    coordinator.set_room_state("Salon", ROOM_STATE_LIVE)
    await hass.async_block_till_done()
    assert "Salon" not in coordinator._farewell_pending


async def test_delivered_farewell_leaves_nothing_pending(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A farewell that reached everything arms no retry."""
    coordinator = _coordinator(setup_integration)
    coordinator._mode = Mode.COOLING
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    mocks = _mock_actuators(hass)
    coordinator.set_room_state("Salon", ROOM_STATE_OFF)
    await hass.async_block_till_done()
    assert [c.data["hvac_mode"] for c in mocks[("climate", "set_hvac_mode")]] == ["off"]
    assert coordinator._farewell_pending == {}


# -- Issue #13: start-up grace for a room sensor that is not up yet ----------


async def test_restart_before_room_sensor_keeps_running_split(
    hass: HomeAssistant,
    register_sources: None,
    entry_data: dict[str, Any],
    hass_storage: dict[str, Any],
) -> None:
    """The first cycle after a restart does not force-stop a running split.

    The room sensor has no state yet while the split already reports
    ``cool``: the room is neither stepped nor written. When the sensor
    reports, the running unit is adopted with its dwell seed instead of
    being stopped and restarted.
    """
    hass.states.async_remove("sensor.salon_temp")
    hass.states.async_set("climate.salon_split", "cool", {})
    hvac_calls = async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    valve_calls = async_mock_service(hass, "number", "set_value")
    entry = await _setup_live_salon(hass, entry_data, hass_storage)
    coordinator = _coordinator(entry)

    assert "Salon" not in coordinator.data.rooms
    assert hvac_calls == []
    assert _calls_to(valve_calls, "number.salon_valve") == []
    # A room whose sensor did report is stepped as usual.
    assert "Lazienka" in coordinator.data.rooms

    hass.states.async_set("sensor.salon_temp", "21.5", _TEMP_ATTRS)
    coordinator._last_step_monotonic = time.monotonic() - 300.0
    await _refresh(hass, coordinator)
    room = coordinator.data.rooms["Salon"]
    assert "sensor_lost" not in room.report.flags
    assert room.outputs.fast_source.on is True
    assert "off" not in [c.data["hvac_mode"] for c in hvac_calls]


async def test_startup_grace_expires_into_safe_degrade(
    hass: HomeAssistant,
    register_sources: None,
    entry_data: dict[str, Any],
    hass_storage: dict[str, Any],
) -> None:
    """A sensor that never reports falls back to the normal safe degrade."""
    hass.states.async_remove("sensor.salon_temp")
    async_mock_service(hass, "climate", "set_hvac_mode")
    async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "number", "set_value")
    entry = await _setup_live_salon(hass, entry_data, hass_storage)
    coordinator = _coordinator(entry)
    assert "Salon" not in coordinator.data.rooms

    coordinator._built_monotonic -= 2 * coordinator._cycle_seconds + 1.0
    await _refresh(hass, coordinator)
    assert "sensor_lost" in coordinator.data.rooms["Salon"].report.flags


async def test_sensor_lost_after_first_report_is_not_graced(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The grace covers only start-up: a later loss degrades at once."""
    coordinator = _coordinator(setup_integration)
    hass.states.async_set("sensor.salon_temp", "unavailable", _TEMP_ATTRS)
    coordinator._entity_cache.clear()
    await _refresh(hass, coordinator)
    assert "sensor_lost" in coordinator.data.rooms["Salon"].report.flags
