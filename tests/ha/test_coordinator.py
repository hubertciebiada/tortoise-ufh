"""Integration tests for :class:`TortoiseUfhCoordinator`.

Exercises the coordinator through the real config-entry setup (see
``conftest.py``): the typed payload it stores, its response to a changed source
sensor, and the shadow/live/kill-switch gating of actuator writes. The actuator
service calls (``number.set_value`` / ``climate.*``) are intercepted by
registering mock service handlers (``async_mock_service``) so we assert on what
the coordinator *would* write. ``ServiceRegistry.async_call`` is a read-only
attribute in modern Home Assistant and cannot be monkeypatched directly.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.tortoise_ufh.const import CONF_ENTITY_VALVES
from custom_components.tortoise_ufh.core.controller import GLOBAL_SAFE_DEW_MARGIN_K
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.models import RoomOutputs

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall
    from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.ha

_TEMP_ATTRS = {"unit_of_measurement": "°C", "device_class": "temperature"}
_MODE_ATTRS = {"options": ["heating", "transitional", "cooling", "off"]}

# The (domain, service) pairs the coordinator uses to command actuators.
_ACTUATOR_SERVICES = (
    ("number", "set_value"),
    ("valve", "set_valve_position"),
    ("climate", "set_hvac_mode"),
    ("climate", "set_temperature"),
)


def _get_coordinator(entry: MockConfigEntry) -> Any:
    """Return the live coordinator stored on the entry's runtime data."""
    return entry.runtime_data.coordinator


def _mock_actuator_services(
    hass: HomeAssistant,
) -> dict[tuple[str, str], list[ServiceCall]]:
    """Register capturing handlers for every actuator service.

    Returns a mapping of ``(domain, service)`` to the live list each handler
    appends its :class:`ServiceCall` to, so tests can assert on what the
    coordinator wrote.
    """
    return {
        (domain, service): async_mock_service(hass, domain, service)
        for domain, service in _ACTUATOR_SERVICES
    }


def _all_actuator_calls(
    mocks: dict[tuple[str, str], list[ServiceCall]],
) -> list[ServiceCall]:
    """Flatten every captured actuator service call across all handlers."""
    return [call for calls in mocks.values() for call in calls]


async def _refresh(hass: HomeAssistant, coordinator: Any) -> None:
    """Force a full update cycle and let entities settle."""
    await coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_data_has_both_rooms_with_outputs_report_and_setpoint(
    setup_integration: MockConfigEntry,
) -> None:
    """After setup the payload carries both rooms fully populated."""
    data = _get_coordinator(setup_integration).data

    assert set(data.rooms) == {"Salon", "Lazienka"}
    for runtime in data.rooms.values():
        assert isinstance(runtime.outputs, RoomOutputs)
        # report is surfaced but must be the very object on outputs.
        assert runtime.report is runtime.outputs.report
        assert math.isfinite(runtime.setpoint_c)

    # setpoint = home_setpoint (21.0) + per-room offset (Salon 0.0, Lazienka 1.0).
    assert data.rooms["Salon"].setpoint_c == pytest.approx(21.0)
    assert data.rooms["Lazienka"].setpoint_c == pytest.approx(22.0)
    assert data.mode == "heating"


async def test_changed_room_sensor_updates_report_and_valve(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Dropping the room temperature raises the error and opens the valve."""
    coordinator = _get_coordinator(setup_integration)
    before = coordinator.data.rooms["Salon"]
    # Room started at 21.5 (above the 21.0 setpoint): not calling for heat.
    assert before.report.error_c == pytest.approx(-0.5, abs=0.05)

    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)

    after = coordinator.data.rooms["Salon"]
    # error = setpoint - room = 21.0 - 15.0.
    assert after.report.error_c == pytest.approx(6.0, abs=0.05)
    assert after.outputs.valve_position_pct > before.outputs.valve_position_pct


async def test_shadow_mode_issues_no_actuator_writes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The default shadow state computes results but writes nothing."""
    coordinator = _get_coordinator(setup_integration)
    assert not coordinator.get_live_control("Salon")

    mocks = _mock_actuator_services(hass)
    await _refresh(hass, coordinator)

    assert _all_actuator_calls(mocks) == []
    # Results are still produced despite emitting no commands.
    assert coordinator.data.rooms["Salon"].outputs is not None


async def test_live_room_writes_valve_when_kill_switch_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A live room with the kill-switch off writes to number.set_value."""
    coordinator = _get_coordinator(setup_integration)
    # Flip live control directly: the public setter persists options and would
    # reload the whole entry, replacing this coordinator instance.
    coordinator._live_control["Salon"] = True
    assert not coordinator.get_kill_switch()

    mocks = _mock_actuator_services(hass)
    await _refresh(hass, coordinator)

    valve_writes = mocks[("number", "set_value")]
    assert any(
        call.data.get("entity_id") == "number.salon_valve" for call in valve_writes
    )


async def test_kill_switch_suppresses_all_writes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """An engaged kill-switch suppresses writes even for a live room."""
    coordinator = _get_coordinator(setup_integration)
    coordinator._live_control["Salon"] = True
    coordinator._kill_switch_engaged = True

    mocks = _mock_actuator_services(hass)
    await _refresh(hass, coordinator)

    assert _all_actuator_calls(mocks) == []


async def test_cooling_global_safe_dew_point_is_max_room_dew_plus_margin(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """In cooling the global dew point is the eligible room max plus 2 K."""
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("input_select.home_mode", "cooling", _MODE_ATTRS)
    await _refresh(hass, coordinator)

    data = coordinator.data
    assert data.mode == "cooling"
    # Only Salon is cooling_enabled (Lazienka opts out), so it sets the max.
    expected = dew_point(21.5, 45.0) + GLOBAL_SAFE_DEW_MARGIN_K
    assert data.global_safe_dew_point_c is not None
    assert math.isfinite(data.global_safe_dew_point_c)
    assert data.global_safe_dew_point_c == pytest.approx(expected, abs=0.01)


# -- valve-domain actuator support ------------------------------------------


async def test_read_valve_domain_reads_current_position_attribute(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A ``valve`` reads its position from ``current_position``, not the state.

    The ``number`` path is unchanged (reads the numeric state), and a valve with
    no ``current_position`` degrades to ``None`` instead of crashing.
    """
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set(
        "valve.salon_loop",
        "open",  # non-numeric state; float("open") would fail.
        {"current_position": 42, "supported_features": 7},
    )

    assert coordinator._read_valve_position("valve.salon_loop") == pytest.approx(42.0)
    # number.* still reads its numeric state (register_sources seeds it at 0).
    assert coordinator._read_valve_position("number.salon_valve") == pytest.approx(0.0)
    # A valve missing current_position → None (no exception, no room degrade).
    hass.states.async_set("valve.no_pos", "closed", {"supported_features": 7})
    assert coordinator._read_valve_position("valve.no_pos") is None
    # None / empty entity id → None.
    assert coordinator._read_valve_position(None) is None


async def test_write_valve_domain_uses_set_valve_position(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A ``valve``-domain actuator is driven via ``valve.set_valve_position``."""
    coordinator = _get_coordinator(setup_integration)
    outputs = coordinator.data.rooms["Salon"].outputs
    mocks = _mock_actuator_services(hass)

    room_cfg = {CONF_ENTITY_VALVES: ["valve.salon_loop"]}
    await coordinator._write_valves(room_cfg, "Salon", outputs)
    await hass.async_block_till_done()

    valve_writes = mocks[("valve", "set_valve_position")]
    assert len(valve_writes) == 1
    call = valve_writes[0]
    assert call.data["entity_id"] == "valve.salon_loop"
    # position is an int (round of the float percentage).
    assert call.data["position"] == round(outputs.valve_position_pct)
    assert isinstance(call.data["position"], int)
    # A valve-domain entity is never written through number.set_value.
    assert mocks[("number", "set_value")] == []


async def test_write_number_domain_still_uses_number_set_value(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A ``number``-domain actuator is still driven via ``number.set_value``."""
    coordinator = _get_coordinator(setup_integration)
    outputs = coordinator.data.rooms["Salon"].outputs
    mocks = _mock_actuator_services(hass)

    room_cfg = {CONF_ENTITY_VALVES: ["number.salon_valve"]}
    await coordinator._write_valves(room_cfg, "Salon", outputs)
    await hass.async_block_till_done()

    number_writes = mocks[("number", "set_value")]
    assert len(number_writes) == 1
    assert number_writes[0].data["entity_id"] == "number.salon_valve"
    assert number_writes[0].data["value"] == pytest.approx(outputs.valve_position_pct)
    # A number-domain entity never touches valve.set_valve_position.
    assert mocks[("valve", "set_valve_position")] == []


async def test_write_mixed_valve_list_dispatches_by_domain(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A mixed number+valve loop list dispatches each entity by its domain."""
    coordinator = _get_coordinator(setup_integration)
    outputs = coordinator.data.rooms["Salon"].outputs
    mocks = _mock_actuator_services(hass)

    room_cfg = {CONF_ENTITY_VALVES: ["number.salon_valve", "valve.salon_loop"]}
    await coordinator._write_valves(room_cfg, "Salon", outputs)
    await hass.async_block_till_done()

    number_writes = mocks[("number", "set_value")]
    valve_writes = mocks[("valve", "set_valve_position")]
    assert [c.data["entity_id"] for c in number_writes] == ["number.salon_valve"]
    assert [c.data["entity_id"] for c in valve_writes] == ["valve.salon_loop"]
    # Both carry the same single computed room position, formatted per domain.
    assert number_writes[0].data["value"] == pytest.approx(outputs.valve_position_pct)
    assert valve_writes[0].data["position"] == round(outputs.valve_position_pct)


async def test_write_debounce_suppresses_repeat_position(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The ``valve_write_threshold_pct`` de-bounce still gates repeat writes.

    Writing the identical computed position twice must emit exactly one command
    for a ``number`` entity and one for a ``valve`` entity (default threshold
    2 %, so a zero delta is suppressed) — the de-bounce is domain-agnostic.
    """
    coordinator = _get_coordinator(setup_integration)
    outputs = coordinator.data.rooms["Salon"].outputs
    mocks = _mock_actuator_services(hass)

    room_cfg = {CONF_ENTITY_VALVES: ["number.salon_valve", "valve.salon_loop"]}
    await coordinator._write_valves(room_cfg, "Salon", outputs)
    await coordinator._write_valves(room_cfg, "Salon", outputs)
    await hass.async_block_till_done()

    assert len(mocks[("number", "set_value")]) == 1
    assert len(mocks[("valve", "set_valve_position")]) == 1
