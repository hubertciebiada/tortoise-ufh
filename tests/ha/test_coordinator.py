"""Integration tests for :class:`TortoiseUfhCoordinator`.

Exercises the coordinator through the real config-entry setup (see
``conftest.py``): the typed payload it stores, its response to a changed source
sensor, and the off/shadow/live gating of actuator writes. The actuator
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

from custom_components.tortoise_ufh.const import (
    CONF_ENTITY_VALVES,
    ROOM_STATE_LIVE,
    ROOM_STATE_OFF,
    ROOM_STATE_SHADOW,
)
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
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: Any,
) -> None:
    """Dropping the room temperature raises the error and opens the valve.

    The drop to 15.0 is a >4 K jump, so the C3 plausibility gate holds the
    first sample; a consistent sample one control cycle later (B5,
    2026-07-12: the confirmation must span real time) confirms the new level.
    """
    from datetime import timedelta

    coordinator = _get_coordinator(setup_integration)
    before = coordinator.data.rooms["Salon"]
    # Room started at 21.5 (above the 21.0 setpoint): not calling for heat.
    assert before.report.error_c == pytest.approx(-0.5, abs=0.05)

    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)  # first sample: held for confirmation
    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)  # a cycle later: accepted

    after = coordinator.data.rooms["Salon"]
    # error = setpoint - room = 21.0 - 15.0.
    assert after.report.error_c == pytest.approx(6.0, abs=0.05)
    assert after.outputs.valve_position_pct > before.outputs.valve_position_pct


async def test_shadow_mode_issues_no_actuator_writes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The default shadow state computes results but writes nothing."""
    coordinator = _get_coordinator(setup_integration)
    assert coordinator.get_room_state("Salon") == ROOM_STATE_SHADOW

    mocks = _mock_actuator_services(hass)
    await _refresh(hass, coordinator)

    assert _all_actuator_calls(mocks) == []
    # Results are still produced despite emitting no commands.
    assert coordinator.data.rooms["Salon"].outputs is not None


async def test_live_room_writes_valve(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A room in the live state writes to number.set_value."""
    coordinator = _get_coordinator(setup_integration)
    # Flip the state directly on the in-memory map: the public setter persists
    # options and could reload the entry, replacing this coordinator instance.
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE

    mocks = _mock_actuator_services(hass)
    await _refresh(hass, coordinator)

    valve_writes = mocks[("number", "set_value")]
    assert any(
        call.data.get("entity_id") == "number.salon_valve" for call in valve_writes
    )


async def test_off_room_issues_no_writes(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A room switched fully off is fed Mode.OFF and writes nothing."""
    coordinator = _get_coordinator(setup_integration)
    coordinator._room_states["Salon"] = ROOM_STATE_OFF

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


# -- Setpoint-change recompute + measurement echo (F1 / bugs J1, J2) ---------


async def test_report_echoes_measured_room_temperature(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The report carries the measured room temperature (J1), not a fake."""
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("sensor.salon_temp", "19.25", _TEMP_ATTRS)
    await _refresh(hass, coordinator)

    report = coordinator.data.rooms["Salon"].report
    assert report.room_temperature_c == pytest.approx(19.25)
    # It survives serialisation for the websocket / panel.
    assert report.to_dict()["room_temperature_c"] == pytest.approx(19.25)


async def test_room_offset_change_rewrites_split_target_same_cycle(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: Any,
) -> None:
    """J2 regression: a room offset change re-emits the split target promptly.

    ``set_room_offset`` schedules a debounced recompute; the full update re-runs
    the control step and the write path emits ``climate.set_temperature`` with
    the NEW target, with the report/setpoint staying mutually consistent. The
    per-instance recompute debouncer is swapped for an immediate one so the
    trailing refresh completes deterministically under ``async_block_till_done``
    instead of waiting out the real ~2 s cooldown.
    """
    from datetime import timedelta

    from homeassistant.helpers.debounce import Debouncer

    coordinator = _get_coordinator(setup_integration)
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    # A cold room makes the split boost and self-regulate to the room target.
    # Two refreshes: the >4 K drop needs the C3 two-sample confirmation; the
    # 11-min tick lets the S4 conservative restart seed's min-OFF dwell elapse.
    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    freezer.tick(timedelta(minutes=11))
    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    assert coordinator.data.rooms["Salon"].outputs.fast_source.on is True

    # Force the scheduled recompute to run immediately (function -> async_refresh
    # so the full update path executes within block_till_done).
    coordinator._recompute_debouncer = Debouncer(
        hass,
        coordinator.logger,
        cooldown=0.0,
        immediate=True,
        function=coordinator.async_refresh,
    )

    mocks = _mock_actuator_services(hass)
    coordinator.set_room_offset("Salon", 2.0)
    await hass.async_block_till_done()

    salon = coordinator.data.rooms["Salon"]
    # Setpoint rebroadcast immediately and the recompute stays consistent.
    assert salon.setpoint_c == pytest.approx(23.0)
    assert salon.report.error_c == pytest.approx(8.0, abs=0.05)  # 23.0 - 15.0

    # S12 (2026-07-09): the heating boost target is setpoint + 1 K.
    set_temp_calls = mocks[("climate", "set_temperature")]
    assert any(
        call.data.get("entity_id") == "climate.salon_split"
        and call.data.get("temperature") == pytest.approx(24.0)
        for call in set_temp_calls
    )


async def test_set_mode_schedules_prompt_recompute(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A panel/service mode change recomputes promptly (fix 2026-07-10).

    ``set_mode`` schedules the same debounced recompute as
    ``set_home_temperature``; without it the new mode's control step would
    wait out the 5-min cycle. The mode source entity is detached so the
    override is authoritative (with one configured, the recompute re-reads
    the entity), and the per-instance debouncer is swapped for an immediate
    one exactly like the offset-recompute test above.
    """
    from homeassistant.helpers.debounce import Debouncer

    from custom_components.tortoise_ufh.core.models import Mode

    coordinator = _get_coordinator(setup_integration)
    assert coordinator.data.rooms["Salon"].report.explanation.startswith("Grzanie")

    coordinator._mode_entity = ""
    coordinator._recompute_debouncer = Debouncer(
        hass,
        coordinator.logger,
        cooldown=0.0,
        immediate=True,
        function=coordinator.async_refresh,
    )
    coordinator.set_mode(Mode.OFF)
    await hass.async_block_till_done()

    assert coordinator.data.mode == "off"
    # The FULL recompute ran under the new mode (not just the rebroadcast):
    # the room report was rebuilt by the OFF path of the control step.
    salon = coordinator.data.rooms["Salon"]
    assert salon.report.explanation.startswith("Off")
    assert salon.outputs.valve_position_pct == 0.0


# -- Phase A hardening: input plausibility, staleness, farewell, mismatch ----


async def test_temperature_spike_rejected_then_confirmed(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: Any,
) -> None:
    """C3+B5: a >4 K jump needs a consistent sample >= 1 cycle later.

    Updated 2026-07-12 (B5): the old contract confirmed the new level on ANY
    second consistent read — a debounced recompute burst could confirm a
    bogus spike within seconds. Now the confirmation must span at least ~one
    nominal control cycle of real time.
    """
    from datetime import timedelta

    coordinator = _get_coordinator(setup_integration)
    # Setup accepted 21.5 degC. A 9 K jump is implausible in one 5-min cycle.
    hass.states.async_set("sensor.salon_temp", "30.5", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    report = coordinator.data.rooms["Salon"].report
    assert "sensor_lost" in report.flags
    assert report.room_temperature_c is None

    # A consistent sample only 4 s later (recompute burst) must NOT confirm.
    freezer.tick(timedelta(seconds=4))
    hass.states.async_set("sensor.salon_temp", "30.6", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    report = coordinator.data.rooms["Salon"].report
    assert "sensor_lost" in report.flags
    assert report.room_temperature_c is None

    # A consistent sample a full cycle later confirms the new level.
    freezer.tick(timedelta(minutes=5))
    hass.states.async_set("sensor.salon_temp", "30.6", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    report = coordinator.data.rooms["Salon"].report
    assert "sensor_lost" not in report.flags
    assert report.room_temperature_c == pytest.approx(30.6)


async def test_temperature_out_of_range_always_rejected(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """C3: the 1-wire 85 degC power-on-reset can never be confirmed."""
    coordinator = _get_coordinator(setup_integration)
    for _ in range(3):
        hass.states.async_set("sensor.salon_temp", "85.0", _TEMP_ATTRS)
        await _refresh(hass, coordinator)
        assert "sensor_lost" in coordinator.data.rooms["Salon"].report.flags

    # A plausible reading recovers the room immediately (within 4 K of 21.5).
    hass.states.async_set("sensor.salon_temp", "21.4", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    report = coordinator.data.rooms["Salon"].report
    assert "sensor_lost" not in report.flags
    assert report.room_temperature_c == pytest.approx(21.4)


async def test_small_change_accepted_immediately(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """C3: normal cycle-to-cycle drift passes the gate untouched."""
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("sensor.salon_temp", "20.8", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    report = coordinator.data.rooms["Salon"].report
    assert report.room_temperature_c == pytest.approx(20.8)


async def test_stale_room_temperature_treated_as_lost(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: Any,
) -> None:
    """C4: a present-but-frozen temperature (>45 min old) degrades the room."""
    from datetime import timedelta

    coordinator = _get_coordinator(setup_integration)
    assert "sensor_lost" not in coordinator.data.rooms["Salon"].report.flags

    # Nobody re-reports the sensor for 46 minutes.
    freezer.tick(timedelta(minutes=46))
    await _refresh(hass, coordinator)
    report = coordinator.data.rooms["Salon"].report
    assert "sensor_lost" in report.flags
    assert report.room_temperature_c is None


async def test_stale_humidity_two_stage_gate(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: Any,
) -> None:
    """C4+K7: RH staleness is two-stage — held+padded 60-120 min, None past.

    Updated 2026-07-12 (K7, linearised D5): the old binary 60-min gate made
    a threshold-reporting RH sensor limit-cycle the cooling. Now a 60-120 min
    old reading is still served with ``humidity_stale_frac`` (the core pads
    its dew points by ``frac * 1 K`` and flags ``rh_stale_gated``); only past
    120 min does the reading become unusable (conservative full stop).
    """
    from datetime import timedelta

    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("input_select.home_mode", "cooling", _MODE_ATTRS)
    await _refresh(hass, coordinator)
    fresh_global = coordinator.data.global_safe_dew_point_c
    assert fresh_global is not None

    # 61 min later the humidity was never re-reported; temperature is fresh.
    # Stage 1: the reading is HELD, the dew points are padded +1 K.
    freezer.tick(timedelta(minutes=61))
    hass.states.async_set("sensor.salon_temp", "21.6", _TEMP_ATTRS)
    hass.states.async_set("input_select.home_mode", "cooling", _MODE_ATTRS)
    await _refresh(hass, coordinator)

    data = coordinator.data
    report = data.rooms["Salon"].report
    assert report.dew_excluded_reason is None  # the room still contributes
    assert data.global_safe_dew_point_c is not None
    assert "rh_stale_gated" in report.flags

    # Stage 2: past 120 min total the reading is unusable — the room falls
    # out of the global maximum and the local throttle stops conservatively.
    freezer.tick(timedelta(minutes=61))
    hass.states.async_set("sensor.salon_temp", "21.7", _TEMP_ATTRS)
    hass.states.async_set("input_select.home_mode", "cooling", _MODE_ATTRS)
    await _refresh(hass, coordinator)

    data = coordinator.data
    report = data.rooms["Salon"].report
    assert report.dew_excluded_reason == "no_humidity"
    assert data.global_safe_dew_point_c is None
    assert "s2_throttle" in report.flags


async def test_live_to_shadow_emits_farewell_split_off(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """C5: leaving live parks the split OFF once (heating keeps the valve)."""
    coordinator = _get_coordinator(setup_integration)
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE

    mocks = _mock_actuator_services(hass)
    coordinator.set_room_state("Salon", ROOM_STATE_SHADOW)
    await hass.async_block_till_done()

    hvac_calls = mocks[("climate", "set_hvac_mode")]
    assert [(c.data["entity_id"], c.data["hvac_mode"]) for c in hvac_calls] == [
        ("climate.salon_split", "off")
    ]
    # Heating mode: the valve position is deliberately left untouched.
    assert mocks[("number", "set_value")] == []


async def test_live_to_off_in_cooling_closes_valve(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """C5: in COOLING the farewell also drives the orphaned valve to 0."""
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("input_select.home_mode", "cooling", _MODE_ATTRS)
    await _refresh(hass, coordinator)
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE

    mocks = _mock_actuator_services(hass)
    coordinator.set_room_state("Salon", ROOM_STATE_OFF)
    await hass.async_block_till_done()

    valve_writes = mocks[("number", "set_value")]
    assert [(c.data["entity_id"], c.data["value"]) for c in valve_writes] == [
        ("number.salon_valve", 0.0)
    ]
    hvac_calls = mocks[("climate", "set_hvac_mode")]
    assert [c.data["hvac_mode"] for c in hvac_calls] == ["off"]


async def test_shadow_to_off_emits_no_farewell(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """C5: transitions not leaving live never write to the hardware."""
    coordinator = _get_coordinator(setup_integration)
    assert coordinator.get_room_state("Salon") == ROOM_STATE_SHADOW

    mocks = _mock_actuator_services(hass)
    coordinator.set_room_state("Salon", ROOM_STATE_OFF)
    await hass.async_block_till_done()

    assert _all_actuator_calls(mocks) == []


async def test_bad_valve_feedback_does_not_degrade_room(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """S8: a garbage feedback (255) nulls one loop, not the whole room."""
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("number.salon_valve", "255", {"unit_of_measurement": "%"})
    await _refresh(hass, coordinator)

    report = coordinator.data.rooms["Salon"].report
    assert "sensor_lost" not in report.flags
    assert report.error_c is not None
    assert report.room_temperature_c == pytest.approx(21.5)


async def test_valve_mismatch_flag_after_three_cycles(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """S8: persistent command-vs-feedback divergence raises valve_mismatch."""
    coordinator = _get_coordinator(setup_integration)
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    # A cold room commands a wide-open valve; the mock service never moves the
    # physical entity, whose feedback stays 0 -> persistent divergence.
    hass.states.async_set("sensor.salon_temp", "18.0", _TEMP_ATTRS)
    _mock_actuator_services(hass)

    # Cycle 1 writes the command; feedback comparison starts on cycle 2.
    await _refresh(hass, coordinator)
    assert coordinator.data.rooms["Salon"].outputs.valve_position_pct > 15.0
    for _ in range(2):
        await _refresh(hass, coordinator)
        assert "valve_mismatch" not in coordinator.data.rooms["Salon"].report.flags

    await _refresh(hass, coordinator)
    assert "valve_mismatch" in coordinator.data.rooms["Salon"].report.flags


async def test_valve_mismatch_not_tracked_in_shadow(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """S8: a shadow room (nothing written) never accuses the valve."""
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("sensor.salon_temp", "18.0", _TEMP_ATTRS)
    for _ in range(4):
        await _refresh(hass, coordinator)
        assert "valve_mismatch" not in coordinator.data.rooms["Salon"].report.flags


async def test_set_mode_snapshot_carries_mode(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """S9: the persisted setpoint snapshot includes the global mode."""
    from custom_components.tortoise_ufh.core.models import Mode

    coordinator = _get_coordinator(setup_integration)
    coordinator.set_mode(Mode.COOLING)
    snapshot = coordinator._setpoint_snapshot()
    assert snapshot["mode"] == "cooling"


# -- Phase B: split command cache (S3) ---------------------------------------


async def test_split_command_cached_not_respammed(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: Any,
) -> None:
    """S3: an unchanged split command is written once, not every cycle."""
    from datetime import timedelta

    coordinator = _get_coordinator(setup_integration)
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    # A cold room engages the split (two refreshes for the C3 confirmation;
    # the tick lets the S4 conservative min-OFF seed elapse).
    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    freezer.tick(timedelta(minutes=11))
    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)

    mocks = _mock_actuator_services(hass)
    await _refresh(hass, coordinator)
    assert coordinator.data.rooms["Salon"].outputs.fast_source.on is True
    hvac_calls = mocks[("climate", "set_hvac_mode")]
    temp_calls = mocks[("climate", "set_temperature")]
    assert len(hvac_calls) == 1
    assert len(temp_calls) == 1

    # Unchanged command: further cycles are silent (cache, re-assert 45 min).
    await _refresh(hass, coordinator)
    await _refresh(hass, coordinator)
    assert len(hvac_calls) == 1
    assert len(temp_calls) == 1


async def test_split_command_change_writes_immediately(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    freezer: Any,
) -> None:
    """S3: a changed target breaks the cache and is written the same cycle."""
    from datetime import timedelta

    coordinator = _get_coordinator(setup_integration)
    coordinator._room_states["Salon"] = ROOM_STATE_LIVE
    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)
    await _refresh(hass, coordinator)
    freezer.tick(timedelta(minutes=11))
    hass.states.async_set("sensor.salon_temp", "15.0", _TEMP_ATTRS)

    mocks = _mock_actuator_services(hass)
    await _refresh(hass, coordinator)
    temp_calls = mocks[("climate", "set_temperature")]
    assert [c.data["temperature"] for c in temp_calls] == [pytest.approx(22.0)]

    coordinator.set_room_offset("Salon", 1.0)
    await _refresh(hass, coordinator)
    assert [c.data["temperature"] for c in temp_calls] == [
        pytest.approx(22.0),
        pytest.approx(23.0),
    ]


# -- Round 3 hardening (2026-07-12): K1 shadow vote, K4 NaN guard, K7 cache --


async def test_nan_setpoint_rejected_and_cycle_survives(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """K4: a NaN through the number-entity path must not wedge the cycles.

    HA's ``number.set_value`` min/max check lets NaN through (every NaN
    comparison is false); before the guard the mutated setpoint made every
    subsequent ``RoomRuntime`` validation raise and the whole integration
    went unavailable until a reload.
    """
    coordinator = _get_coordinator(setup_integration)
    before = coordinator.get_home_temperature()

    coordinator.set_home_temperature(float("nan"))
    assert coordinator.get_home_temperature() == pytest.approx(before)

    coordinator.set_room_offset("Salon", float("inf"))
    assert coordinator.get_room_offset("Salon") == pytest.approx(0.0)

    await _refresh(hass, coordinator)
    assert coordinator.last_update_success
    assert math.isfinite(coordinator.data.rooms["Salon"].setpoint_c)


async def test_unavailable_rh_entity_keeps_the_stale_pad(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """K7: a value served from the short cache is STALE, never fresh.

    The cache branch fires when the RH entity itself goes unavailable; the
    held value's true age is unknowable, so it must carry the full +1 K pad.
    Before the fix the branch reported FRESH — the conservative pad vanished
    at the very moment the sensor died.
    """
    coordinator = _get_coordinator(setup_integration)
    hass.states.async_set("input_select.home_mode", "cooling", _MODE_ATTRS)
    await _refresh(hass, coordinator)
    fresh_global = coordinator.data.global_safe_dew_point_c
    assert fresh_global is not None
    assert "rh_stale_gated" not in coordinator.data.rooms["Salon"].report.flags

    # The RH entity dies; the reader serves the <=5-min-old cached value.
    hass.states.async_set("sensor.salon_humidity", "unavailable", {})
    await _refresh(hass, coordinator)

    report = coordinator.data.rooms["Salon"].report
    assert "rh_stale_gated" in report.flags
    # Same reading, +1 K pad: the global maximum moves up by exactly 1 K.
    assert coordinator.data.global_safe_dew_point_c == pytest.approx(fresh_global + 1.0)


async def test_shadow_room_does_not_vote_in_group_arbitration(
    hass: HomeAssistant, register_sources: None
) -> None:
    """K1: only LIVE rooms arbitrate a shared outdoor unit.

    A SHADOW room's split command is never written, yet its (uncontrolled,
    so never-shrinking) error used to win every re-engage and permanently
    force the LIVE room's split OFF — and shadow is the default state of a
    new room.
    """
    from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.tortoise_ufh.const import (
        CONF_ENTITY_FAST_SOURCE,
        CONF_ENTITY_TEMP_ROOM,
        CONF_FAST_SOURCE_GROUP,
        CONF_FAST_SOURCE_KIND,
        CONF_HOME_SETPOINT,
        CONF_ROOM_AREA,
        CONF_ROOM_NAME,
        CONF_ROOM_STATE,
        CONF_ROOMS,
        DOMAIN,
        FAST_SOURCE_KIND_SPLIT,
    )

    hass.states.async_set("sensor.polnoc_temp", "19.5", _TEMP_ATTRS)  # wants heat
    hass.states.async_set("sensor.poludnie_temp", "24.5", _TEMP_ATTRS)  # wants cool
    # No climate feedback states on purpose: an observed OFF would seed the
    # conservative full min-OFF dwell (S4) and keep both splits idle for the
    # whole first cycle regardless of the arbitration under test.
    _mock_actuator_services(hass)
    hass.states.async_set(
        "input_select.home_mode",
        "transitional",
        {"options": ["heating", "transitional", "cooling", "off"]},
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LATITUDE: 50.5,
            CONF_LONGITUDE: 19.5,
            CONF_HOME_SETPOINT: 21.0,
            "entity_mode": "input_select.home_mode",
            CONF_ROOMS: [
                {
                    CONF_ROOM_NAME: "Polnoc",
                    CONF_ROOM_AREA: 20.0,
                    CONF_ENTITY_TEMP_ROOM: "sensor.polnoc_temp",
                    CONF_ENTITY_VALVES: [],
                    CONF_ENTITY_FAST_SOURCE: "climate.polnoc_split",
                    CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_SPLIT,
                    CONF_FAST_SOURCE_GROUP: "outdoor_unit_a",
                },
                {
                    CONF_ROOM_NAME: "Poludnie",
                    CONF_ROOM_AREA: 20.0,
                    CONF_ENTITY_TEMP_ROOM: "sensor.poludnie_temp",
                    CONF_ENTITY_VALVES: [],
                    CONF_ENTITY_FAST_SOURCE: "climate.poludnie_split",
                    CONF_FAST_SOURCE_KIND: FAST_SOURCE_KIND_SPLIT,
                    CONF_FAST_SOURCE_GROUP: "outdoor_unit_a",
                },
            ],
        },
        # The LIVE room asks for a modest heat boost; the SHADOW room's much
        # larger cooling error must not out-vote it.
        options={CONF_ROOM_STATE: {"Polnoc": "live", "Poludnie": "shadow"}},
        title="Tortoise-UFH",
        unique_id="50.5_19.5",
        version=2,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    data = entry.runtime_data.coordinator.data
    live = data.rooms["Polnoc"].outputs
    assert live.fast_source.on is True
    assert live.fast_source.mode.value == "heating"
    assert "fast_source_group_conflict" not in live.report.flags
