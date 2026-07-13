"""Closed-loop digital-twin tests of the S6 hydraulic no-flow watchdog.

Exercises the acceptance criteria of ``docs/NO_FLOW_WATCHDOG.md``
(2026-07-13, issue #4) end to end on the :class:`BuildingSimulator` twin with
its actuator-fault injection (:meth:`SimulatedRoom.set_actuator_fault`):

* **Criterion 1** — echo feedback + a frozen actuator in LIVE cooling raises
  ``loop_no_flow`` within one ``response_window``, freezes the integrator,
  and never moves the valve; a healthy sibling room stays clean.
* **Criterion 2** — a valve commanded 0 with a persistent source-side loop
  signature raises ``loop_stuck_open`` and re-enters the global safe
  dew-point maximum despite being ``cooling_disabled``.
* The manual actuation self-test graded on the twin's hydraulic response —
  pass on a healthy actuator, fail on a frozen one — plus the manifold-bar
  supply-probe placement variant that must NOT false-alarm.

(Criterion 3 — the write-path re-assert — is an adapter concern, covered by
the ``tests/ha`` CommandWriter suite; criterion 4 lives in
``test_scenarios.py::test_flow_watchdog_silent_on_healthy_loops``.)

Units: temperatures degC, valve percent 0..100, time minutes (simulation) /
seconds (controller ``dt``). This module never imports ``homeassistant``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.tortoise_ufh.core.building_profiles import well_insulated
from custom_components.tortoise_ufh.core.config import RoomConfig
from custom_components.tortoise_ufh.core.controller import BuildingController
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.models import BuildingOutputs
from custom_components.tortoise_ufh.core.rc_model import ModelOrder, RCModel
from custom_components.tortoise_ufh.core.simulator import (
    BuildingSimulator,
    HeatPumpMode,
    SimulatedRoom,
)
from custom_components.tortoise_ufh.core.ufh_loop import LoopGeometry
from custom_components.tortoise_ufh.core.weather import SyntheticWeather

_DT_S = 300.0
"""Production control takt [s]."""

_WINDOW_MIN = 45.0
"""Default ``flow_response_window_min`` [min]."""

_STEPS_PER_WINDOW = int(_WINDOW_MIN * 60.0 / _DT_S)
"""Control cycles per response window (9 at 300 s)."""

_GLOBAL_DEW_MARGIN_K = 2.0
"""Global safe dew point = max room dew point + this margin [K] (frozen)."""


def _make_room(
    name: str,
    *,
    t_ground: float,
    cooling_enabled: bool = True,
    initial_c: float,
    supply_probe_on_manifold: bool = False,
) -> tuple[SimulatedRoom, RoomConfig]:
    """Build one twin room from the ``well_insulated`` reference profile.

    Args:
        name: Room name.
        t_ground: Seasonal ground temperature for the RC params [degC].
        cooling_enabled: Whether the room participates in floor cooling.
        initial_c: Initial temperature of every thermal node [degC].
        supply_probe_on_manifold: Probe-placement variant (S6).

    Returns:
        The simulated room and its (renamed) :class:`RoomConfig`.
    """
    base = well_insulated(t_ground=t_ground).rooms[0]
    cfg = replace(base, name=name, cooling_enabled=cooling_enabled)
    model = RCModel(cfg.params, ModelOrder.THREE, dt=_DT_S)
    room = SimulatedRoom(
        name,
        model,
        n_loops=cfg.n_loops,
        cooling_enabled=cooling_enabled,
        windows=cfg.windows,
        initial_temperature_c=initial_c,
        loop_geometry=LoopGeometry.from_room_config(cfg),
        supply_probe_on_manifold=supply_probe_on_manifold,
    )
    return room, cfg


def _tick(
    simulator: BuildingSimulator,
    controller: BuildingController,
    n_steps: int,
) -> BuildingOutputs:
    """Run *n_steps* closed-loop control cycles and return the last outputs.

    Mirrors the ``run_scenario`` harness: measurements -> controller step ->
    dew-point feedback to the twin heat pump -> physics.

    Args:
        simulator: The digital twin.
        controller: The building controller under test.
        n_steps: Number of 300 s cycles to run (>= 1).

    Returns:
        The final cycle's :class:`BuildingOutputs`.
    """
    outputs: BuildingOutputs | None = None
    for _ in range(n_steps):
        inputs = simulator.get_all_measurements()
        outputs = controller.step(inputs, dt_seconds=_DT_S)
        simulator.set_cooling_supply_floor(outputs.global_safe_dew_point_c)
        simulator.step_all(outputs.rooms)
    assert outputs is not None
    return outputs


def _cooling_pair() -> tuple[BuildingSimulator, BuildingController, SimulatedRoom]:
    """Two-room LIVE-cooling twin: a to-be-faulted room and a healthy one.

    Hot summer day (T_out 30 degC, RH 50 %), rooms starting at 27 degC with
    24 degC setpoints, so both cooling valves open hard from the first cycle.

    Returns:
        ``(simulator, controller, faulty_room)``.
    """
    faulty, faulty_cfg = _make_room("faulty", t_ground=17.0, initial_c=27.0)
    healthy, healthy_cfg = _make_room("healthy", t_ground=17.0, initial_c=27.0)
    weather = SyntheticWeather.constant(T_out=30.0, GHI=0.0, humidity=50.0)
    simulator = BuildingSimulator(
        [faulty, healthy],
        weather,
        hp_mode=HeatPumpMode.COOLING,
        hp_max_power_w=6000.0,
    )
    simulator.set_setpoints({"faulty": 24.0, "healthy": 24.0})
    controller = BuildingController(
        {"faulty": faulty_cfg.controller, "healthy": healthy_cfg.controller}
    )
    return simulator, controller, faulty


@pytest.mark.simulation
class TestCriterion1NoFlow:
    """Echo feedback + frozen actuator in LIVE cooling (criterion 1)."""

    def test_frozen_echoing_actuator_raises_no_flow_within_a_window(
        self,
    ) -> None:
        """The exact incident: targets accepted, never executed, echo feedback.

        The faulty room's valve is frozen CLOSED while the commanded position
        is echoed back as feedback (``valve_mismatch`` stays blind). The loop
        probes stagnate toward the slab, the healthy sibling proves
        circulation, and ``loop_no_flow`` must latch within one response
        window (+1 establishing cycle), freezing the integrator — with the
        valve command left entirely to the PI (no banging). The healthy room
        must stay clean the whole run.
        """
        simulator, controller, faulty = _cooling_pair()

        # Park the faulty room first (setpoint above the room: command 0),
        # so the frozen physical valve is stuck CLOSED — the incident shape.
        simulator.set_setpoint("faulty", 30.0)
        _tick(simulator, controller, 3)
        assert faulty.valve_position == 0.0

        faulty.set_actuator_fault(frozen=True, echo_feedback=True)
        # Let the dead loop's residual water signature decay to CONSTANT
        # slab-level temperatures (the criterion's "constant loop temps") —
        # a freshly closed loop's fading delta-T legitimately counts as
        # flow evidence for the first ~30 min.
        _tick(simulator, controller, 10)
        simulator.set_setpoint("faulty", 22.0)

        flagged_at: int | None = None
        open_since: int | None = None
        outputs: BuildingOutputs | None = None
        for i in range(_STEPS_PER_WINDOW + 6):
            outputs = _tick(simulator, controller, 1)
            report = outputs.rooms["faulty"].report
            command = outputs.rooms["faulty"].valve_position_pct
            # The echo lies: feedback equals the command, mismatch is blind.
            feedback = simulator.get_all_measurements()["faulty"].loops[0]
            assert feedback.valve_position_pct == pytest.approx(command)
            # The physical valve never moved.
            assert faulty.valve_position == 0.0
            # No automatic valve banging: the command stays the PI's own
            # (saturation at 100 % from honest wind-up is legitimate) — the
            # watchdog never starts an excursion on its own.
            assert "actuation_test_running" not in report.flags
            if open_since is None and command >= 15.0:
                open_since = i
            if "loop_no_flow" in report.flags:
                flagged_at = i
                break
        assert outputs is not None
        assert open_since is not None, "the cooling command never opened"
        assert flagged_at is not None, "loop_no_flow never latched"
        assert flagged_at - open_since <= _STEPS_PER_WINDOW + 1, (
            "loop_no_flow must latch within one response window "
            f"(opened at cycle {open_since}, flagged at {flagged_at})"
        )

        report = outputs.rooms["faulty"].report
        assert report.integrator_frozen is True, (
            "the incident's wind-up: the integrator must freeze on no-flow"
        )
        assert "no_flow" in report.loop_flow_status

        healthy_report = outputs.rooms["healthy"].report
        assert "loop_no_flow" not in healthy_report.flags
        assert "loop_stuck_open" not in healthy_report.flags
        assert set(healthy_report.loop_flow_status) <= {"ok", "inactive"}

    def test_recovered_actuator_clears_the_flag(self) -> None:
        """Physical evidence clears the alarm as soon as water moves again."""
        simulator, controller, faulty = _cooling_pair()
        simulator.set_setpoint("faulty", 30.0)
        _tick(simulator, controller, 3)
        faulty.set_actuator_fault(frozen=True, echo_feedback=True)
        simulator.set_setpoint("faulty", 22.0)
        outputs = _tick(simulator, controller, _STEPS_PER_WINDOW + 5)
        assert "loop_no_flow" in outputs.rooms["faulty"].report.flags

        # Controller-side repair (the incident's valvesDetect): the actuator
        # executes targets again; the loop re-fills and the flag clears.
        faulty.set_actuator_fault(frozen=False, echo_feedback=False)
        outputs = _tick(simulator, controller, 3)
        assert "loop_no_flow" not in outputs.rooms["faulty"].report.flags


@pytest.mark.simulation
class TestCriterion2StuckOpen:
    """Persistent source signature on a 0-commanded valve (criterion 2)."""

    def test_stuck_open_flags_and_feeds_the_global_dew_point(self) -> None:
        """A cooling-disabled room with a physically open valve re-enters
        the global safe dew-point maximum once ``loop_stuck_open`` latches.
        """
        bathroom, bathroom_cfg = _make_room(
            "bathroom", t_ground=17.0, cooling_enabled=False, initial_c=27.0
        )
        healthy, healthy_cfg = _make_room("healthy", t_ground=17.0, initial_c=27.0)
        weather = SyntheticWeather.constant(T_out=30.0, GHI=0.0, humidity=50.0)
        simulator = BuildingSimulator(
            [bathroom, healthy],
            weather,
            hp_mode=HeatPumpMode.COOLING,
            hp_max_power_w=6000.0,
        )
        simulator.set_setpoints({"bathroom": 24.0, "healthy": 24.0})
        controller = BuildingController(
            {
                "bathroom": bathroom_cfg.controller,
                "healthy": healthy_cfg.controller,
            }
        )

        # The physical valve sits open (e.g. a controller reset to its park
        # position) and the actuator ignores all further targets.
        bathroom.apply_actions(valve_position=60.0, fast_source_power_w=0.0)
        bathroom.set_actuator_fault(frozen=True, echo_feedback=True)

        early = _tick(simulator, controller, 2)
        early_report = early.rooms["bathroom"].report
        # Cooling-disabled: commanded 0, excluded from the dew maximum...
        assert early.rooms["bathroom"].valve_position_pct == 0.0
        assert early_report.dew_excluded_reason == "cooling_disabled"
        assert "loop_stuck_open" not in early_report.flags

        outputs = _tick(simulator, controller, _STEPS_PER_WINDOW + 4)
        report = outputs.rooms["bathroom"].report
        assert "loop_stuck_open" in report.flags, (
            "a persistently flowing loop on a 0 command must latch stuck_open"
        )
        assert "stuck_open" in report.loop_flow_status
        # ... but the physically cold floor is real: the room re-enters the
        # global dew maximum (the heat pump's water floor is the only
        # defence an actuator that ignores commands cannot bypass).
        assert report.dew_excluded_reason is None
        inputs = simulator.get_all_measurements()["bathroom"]
        assert inputs.room_temperature_c is not None
        assert inputs.humidity_pct is not None
        room_dew = dew_point(inputs.room_temperature_c, inputs.humidity_pct)
        assert outputs.global_safe_dew_point_c is not None
        assert (
            outputs.global_safe_dew_point_c >= room_dew + _GLOBAL_DEW_MARGIN_K - 0.2
        ), "the stuck-open room's dew point must bound the global floor"


@pytest.mark.simulation
class TestActuationSelfTestOnTwin:
    """The manual self-test graded against the twin's hydraulics."""

    def _heating_single(
        self, *, supply_probe_on_manifold: bool = False
    ) -> tuple[BuildingSimulator, BuildingController, SimulatedRoom]:
        """One-room heating twin (T_out 0 degC, setpoint 21, start 20)."""
        room, cfg = _make_room(
            "main",
            t_ground=14.0,
            initial_c=20.0,
            supply_probe_on_manifold=supply_probe_on_manifold,
        )
        weather = SyntheticWeather.constant(T_out=0.0, GHI=0.0, humidity=50.0)
        simulator = BuildingSimulator(
            [room], weather, hp_mode=HeatPumpMode.HEATING, hp_max_power_w=6000.0
        )
        simulator.set_setpoint("main", 21.0)
        controller = BuildingController({"main": cfg.controller})
        return simulator, controller, room

    def test_healthy_actuator_passes(self) -> None:
        """A healthy loop grades ``passed``; the excursion drives 100 %."""
        simulator, controller, _room = self._heating_single()
        _tick(simulator, controller, 4)

        assert controller.begin_actuation_test("main", duration_s=1500.0) is None
        outputs = _tick(simulator, controller, 1)
        report = outputs.rooms["main"].report
        assert outputs.rooms["main"].valve_position_pct == 100.0
        assert report.actuation_test_status == "running"
        assert "actuation_test_running" in report.flags
        assert report.integrator_frozen is True

        outputs = _tick(simulator, controller, 5)
        report = outputs.rooms["main"].report
        assert report.actuation_test_status == "passed"
        assert report.actuation_test_loops
        assert all(r == "passed" for r in report.actuation_test_loops)
        assert "actuation_test_failed" not in report.flags

    def test_frozen_actuator_fails(self) -> None:
        """A frozen-closed actuator leaves no hydraulic response: failed."""
        simulator, controller, room = self._heating_single()
        # Park the valve closed, then freeze the actuator before any demand.
        simulator.set_setpoint("main", 15.0)
        _tick(simulator, controller, 2)
        assert room.valve_position == 0.0
        room.set_actuator_fault(frozen=True, echo_feedback=True)
        # Let the stagnating probes settle near the slab so the pre-test
        # references are honest (the twin initialises them source-side).
        _tick(simulator, controller, 10)

        assert controller.begin_actuation_test("main", duration_s=1500.0) is None
        outputs = _tick(simulator, controller, 6)
        report = outputs.rooms["main"].report
        assert report.actuation_test_status == "failed"
        assert report.actuation_test_loops
        assert all(r == "failed" for r in report.actuation_test_loops)
        assert "actuation_test_failed" in report.flags
        assert room.valve_position == 0.0

    def test_cancel_marks_aborted(self) -> None:
        """A user cancel aborts without a verdict; the valve returns to PI."""
        simulator, controller, _room = self._heating_single()
        _tick(simulator, controller, 4)
        assert controller.begin_actuation_test("main", duration_s=1500.0) is None
        _tick(simulator, controller, 1)
        controller.cancel_actuation_test("main")
        outputs = _tick(simulator, controller, 1)
        report = outputs.rooms["main"].report
        assert report.actuation_test_status == "aborted"
        assert outputs.rooms["main"].valve_position_pct < 100.0

    def test_manifold_supply_probe_placement_does_not_false_alarm(self) -> None:
        """The bar-mounted supply probe variant never fakes stuck-open.

        With the supply probe BEFORE the valve it keeps the source
        temperature during stagnation (large delta-T), but the return probe
        rests at the slab — the return-vs-room condition holds the alarm
        down across several windows of a genuinely closed, healthy valve.
        """
        simulator, controller, room = self._heating_single(
            supply_probe_on_manifold=True
        )
        # No heating demand: the valve is commanded (and physically) closed.
        simulator.set_setpoint("main", 15.0)
        outputs = _tick(simulator, controller, 3 * _STEPS_PER_WINDOW)
        report = outputs.rooms["main"].report
        assert room.valve_position == 0.0
        assert "loop_stuck_open" not in report.flags
        assert "stuck_open" not in report.loop_flow_status
