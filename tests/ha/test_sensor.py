"""Sensor-platform tests: per-room + global diagnostic sensors after setup.

Resolves each entity via the entity registry from its deterministic
``unique_id`` (``{entry_id}[_{room}]_{key}``) rather than a guessed slug, then
asserts the published state / unit reflects the coordinator's typed payload and
the locked units contract.
"""

from __future__ import annotations

import math
from datetime import datetime

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.tortoise_ufh.const import DOMAIN

pytestmark = pytest.mark.ha

_UNSET = {"unknown", "unavailable"}


def _state(hass: HomeAssistant, entry_id: str, unique_id: str):
    """Resolve a sensor entity by unique_id and return its live state."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
    assert entity_id is not None, f"no sensor registered for {unique_id!r}"
    state = hass.states.get(entity_id)
    assert state is not None, f"no state for {entity_id}"
    return state


@pytest.mark.parametrize("room", ["salon", "lazienka"])
async def test_room_sensors_exist_with_sane_values(
    hass: HomeAssistant, setup_integration, room: str
) -> None:
    """Every per-room diagnostic sensor exists with a sane value and unit."""
    entry_id = setup_integration.entry_id

    valve = _state(hass, entry_id, f"{entry_id}_{room}_recommended_valve")
    assert valve.attributes["unit_of_measurement"] == "%"
    assert 0.0 <= float(valve.state) <= 100.0

    error = _state(hass, entry_id, f"{entry_id}_{room}_error_c")
    assert error.attributes["unit_of_measurement"] == "°C"
    assert math.isfinite(float(error.state))

    trend = _state(hass, entry_id, f"{entry_id}_{room}_trend_c_per_h")
    assert trend.attributes["unit_of_measurement"] == "°C/h"
    assert math.isfinite(float(trend.state))

    # Dew point below room temperature is a real thermodynamic invariant.
    dew = _state(hass, entry_id, f"{entry_id}_{room}_room_dew_point")
    assert dew.attributes["unit_of_measurement"] == "°C"
    room_temp = 21.5 if room == "salon" else 22.0
    assert float(dew.state) < room_temp

    mode = _state(hass, entry_id, f"{entry_id}_{room}_fast_source_mode")
    assert mode.state in {"off", "heating", "cooling"}

    explanation = _state(hass, entry_id, f"{entry_id}_{room}_explanation")
    assert explanation.state not in _UNSET
    assert explanation.state.strip() != ""


async def test_global_safe_dew_point_reflects_coordinator(
    hass: HomeAssistant, setup_integration
) -> None:
    """The global safe dew-point sensor mirrors coordinator.data (°C)."""
    entry_id = setup_integration.entry_id
    coordinator = setup_integration.runtime_data.coordinator
    expected = coordinator.data.global_safe_dew_point_c

    state = _state(hass, entry_id, f"{entry_id}_global_safe_dew_point")
    if expected is None:
        assert state.state in _UNSET
    else:
        assert state.attributes["unit_of_measurement"] == "°C"
        assert float(state.state) == pytest.approx(expected)


async def test_global_status_sensors(hass: HomeAssistant, setup_integration) -> None:
    """Global algorithm/watchdog/last-update sensors reflect a clean cycle."""
    entry_id = setup_integration.entry_id
    coordinator = setup_integration.runtime_data.coordinator

    algo = _state(hass, entry_id, f"{entry_id}_algorithm_status")
    # Both rooms have fresh source data, so the first cycle ran cleanly.
    assert algo.state == "running"
    assert algo.state == coordinator.data.algorithm_status

    watchdog = _state(hass, entry_id, f"{entry_id}_watchdog_status")
    # Clean start never faulted -> fresh data clears to "ok" immediately.
    assert watchdog.state == "ok"
    assert watchdog.state == coordinator.data.watchdog_state

    last_update = _state(hass, entry_id, f"{entry_id}_last_update")
    assert last_update.state not in _UNSET
    # A TIMESTAMP sensor publishes an ISO-8601 datetime string.
    assert isinstance(datetime.fromisoformat(last_update.state), datetime)


async def test_global_hp_flicker_state_never_unavailable(
    hass: HomeAssistant, setup_integration
) -> None:
    """Issue #8: the flicker-state sensor renders a real state, not unavailable.

    The v0.17.0 extractor read ``data.flicker`` (no such field on
    ``CoordinatorData``) and the AttributeError left the entity permanently
    unavailable — with an empty recorder history under the forced-starts-24h
    panel counter.
    """
    entry_id = setup_integration.entry_id
    coordinator = setup_integration.runtime_data.coordinator

    state = _state(hass, entry_id, f"{entry_id}_hp_flicker_state")
    assert state.state not in _UNSET
    hp = coordinator.data.heat_pump
    expected = (
        "idle" if hp is None or hp.flicker is None else hp.flicker.get("state", "idle")
    )
    assert state.state == expected


async def test_global_hp_flicker_state_reads_heat_pump_payload(
    hass: HomeAssistant, setup_integration
) -> None:
    """The sensor mirrors ``data.heat_pump.flicker`` through the REAL runtime.

    A synthetic ``data.flicker`` attribute would mask issue #8 — the payload
    must travel on :class:`HeatPumpRuntime`, exactly as the coordinator
    builds it.
    """
    from dataclasses import replace

    from custom_components.tortoise_ufh.coordinator import HeatPumpRuntime

    entry_id = setup_integration.entry_id
    coordinator = setup_integration.runtime_data.coordinator

    runtime = HeatPumpRuntime(
        mode_entity_id=None,
        current_option=None,
        desired_option=None,
        in_sync=None,
        dhw_active=False,
        dhw_only=False,
        cooling=None,
        heating=None,
        hp_active=None,
        hp_active_configured=False,
        writes_enabled=False,
        flicker={"enabled": True, "state": "cooldown"},
    )
    coordinator.async_set_updated_data(replace(coordinator.data, heat_pump=runtime))
    await hass.async_block_till_done()

    state = _state(hass, entry_id, f"{entry_id}_hp_flicker_state")
    assert state.state == "cooldown"


@pytest.mark.parametrize("room", ["salon", "lazienka"])
async def test_room_pi_term_sensors_reflect_report(
    hass: HomeAssistant, setup_integration, room: str
) -> None:
    """i_term / trend_term sensors publish the report's valve contributions (%)."""
    entry_id = setup_integration.entry_id
    coordinator = setup_integration.runtime_data.coordinator
    # coordinator.data.rooms is keyed by the configured room name (title-case).
    report = coordinator.data.rooms[room.capitalize()].report

    i_term = _state(hass, entry_id, f"{entry_id}_{room}_i_term")
    assert i_term.attributes["unit_of_measurement"] == "%"
    assert i_term.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert float(i_term.state) == pytest.approx(report.i_term)

    trend_term = _state(hass, entry_id, f"{entry_id}_{room}_trend_term")
    assert trend_term.attributes["unit_of_measurement"] == "%"
    assert trend_term.attributes["state_class"] == SensorStateClass.MEASUREMENT
    assert float(trend_term.state) == pytest.approx(report.trend_term)


async def test_numeric_sensor_carries_measurement_state_class(
    hass: HomeAssistant, setup_integration
) -> None:
    """A numeric diagnostic sensor records statistics (state_class=measurement)."""
    entry_id = setup_integration.entry_id
    valve = _state(hass, entry_id, f"{entry_id}_salon_recommended_valve")
    assert valve.attributes["state_class"] == SensorStateClass.MEASUREMENT
