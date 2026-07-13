"""Unit tests for the S6 hydraulic no-flow watchdog and actuation self-test.

Covers, against :mod:`tortoise_ufh.core.flow_watchdog` and its integration in
:mod:`tortoise_ufh.core.controller` (2026-07-13, issue #4):

* the :class:`LoopFlowMonitor` window machine — accumulation / evidence
  resets / circulation-gate HOLD / the stuck-open return-vs-room condition
  (including the manifold-bar probe variant that must NOT false-alarm);
* the :class:`FlowWatchdog` per-room aggregation (multi-loop worst, pause,
  reset) and the new ``ControllerConfig`` / ``RoomReport`` validation;
* the :class:`RoomController` wiring — integrator freeze on ``loop_no_flow``,
  flag/report stamping, the post-safety ``_last_emitted_valve_pct`` command
  reference, and the actuation self-test life cycle (begin / excursion /
  verdict / refusals / aborts);
* the :class:`BuildingController` wiring — the injected
  ``circulation_evident`` gate (per-loop witnesses, the optional global
  supply probe, its DHW/defrost suspension) and the stuck-open COOLING room
  forced into the global safe dew point.

Units: temperatures degC, valve percent 0..100, ``dt_seconds`` seconds,
windows minutes. This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    BuildingController,
    RoomController,
)
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.flow_watchdog import (
    ActuationSelfTest,
    FlowWatchdog,
    LoopFlowMonitor,
)
from custom_components.tortoise_ufh.core.models import (
    LoopInput,
    Mode,
    RoomInputs,
    RoomReport,
)
from tests.unit.conftest import make_inputs

_DT_S = 300.0
"""Nominal control cycle used throughout [s]."""

_WINDOW_S = 45.0 * 60.0
"""Default ``flow_response_window_min`` in seconds."""

_STEPS_PER_WINDOW = int(_WINDOW_S / _DT_S)
"""Cycles to accumulate one full response window (9 at 300 s)."""

_GLOBAL_DEW_MARGIN_K = 2.0
"""Global safe dew point = max room dew point + this margin [K] (frozen)."""


def _update(
    monitor: LoopFlowMonitor,
    *,
    mode: Mode = Mode.HEATING,
    commanded_pct: float | None = 50.0,
    supply_c: float | None = 25.0,
    return_c: float | None = 25.0,
    room_temperature_c: float | None = 21.0,
    circulation_evident: bool | None = True,
    dt_seconds: float = _DT_S,
    epsilon_k: float = 0.3,
    open_threshold_pct: float = 15.0,
    window_s: float = _WINDOW_S,
) -> str:
    """Advance *monitor* one cycle with keyword defaults (dead open loop)."""
    return monitor.update(
        mode=mode,
        commanded_pct=commanded_pct,
        supply_c=supply_c,
        return_c=return_c,
        room_temperature_c=room_temperature_c,
        circulation_evident=circulation_evident,
        dt_seconds=dt_seconds,
        epsilon_k=epsilon_k,
        open_threshold_pct=open_threshold_pct,
        window_s=window_s,
    )


# ---------------------------------------------------------------------------
# LoopFlowMonitor — the per-loop window machine
# ---------------------------------------------------------------------------


class TestLoopFlowMonitorNoFlow:
    """No-flow window: accumulation, latch, evidence resets, gate HOLD."""

    @pytest.mark.unit
    def test_dead_open_loop_latches_after_one_window(self) -> None:
        """A commanded-open loop with zero hydraulic response latches no_flow."""
        monitor = LoopFlowMonitor()
        for i in range(_STEPS_PER_WINDOW - 1):
            status = _update(monitor)
            assert status == "ok", f"latched too early at cycle {i}"
            assert monitor.no_flow_active is False
        assert _update(monitor) == "no_flow"
        assert monitor.no_flow_active is True

    @pytest.mark.unit
    def test_delta_t_evidence_resets_the_window(self) -> None:
        """A single ``|dT| >= epsilon`` sample resets the whole window."""
        monitor = LoopFlowMonitor()
        for _ in range(_STEPS_PER_WINDOW - 1):
            _update(monitor)
        # One flowing sample: supply-return difference above epsilon.
        assert _update(monitor, supply_c=26.0, return_c=25.0) == "ok"
        # The window restarted: another full window is needed to latch.
        for _ in range(_STEPS_PER_WINDOW - 1):
            assert _update(monitor) == "ok"
        assert _update(monitor) == "no_flow"

    @pytest.mark.unit
    def test_return_displacement_toward_source_is_evidence(self) -> None:
        """Return-probe displacement toward the source counts as flow."""
        monitor = LoopFlowMonitor()
        _update(monitor)  # captures references at 25/25
        temp = 25.0
        for _ in range(_STEPS_PER_WINDOW + 2):
            # Both probes keep creeping 0.6 K warmer per cycle (heating):
            # displacement evidence refreshes the references every cycle,
            # so no window ever accumulates.
            temp += 0.6
            status = _update(monitor, supply_c=temp, return_c=temp)
            assert status == "ok"
        assert monitor.no_flow_active is False

    @pytest.mark.unit
    def test_cooling_displacement_direction_is_inverted(self) -> None:
        """In COOLING the displacement toward the source is DOWNWARD."""
        monitor = LoopFlowMonitor()
        _update(monitor, mode=Mode.COOLING, room_temperature_c=26.0)
        # Probes crept colder: evidence in cooling.
        assert (
            _update(
                monitor,
                mode=Mode.COOLING,
                supply_c=24.4,
                return_c=24.4,
                room_temperature_c=26.0,
            )
            == "ok"
        )
        # Probes crept WARMER in cooling: not evidence, window accumulates.
        monitor2 = LoopFlowMonitor()
        _update(monitor2, mode=Mode.COOLING, room_temperature_c=26.0)
        for _ in range(_STEPS_PER_WINDOW):
            status = _update(
                monitor2,
                mode=Mode.COOLING,
                supply_c=25.6,
                return_c=25.6,
                room_temperature_c=26.0,
            )
        assert status == "no_flow"

    @pytest.mark.unit
    @pytest.mark.parametrize("gate", [None, False])
    def test_circulation_gate_holds_without_resetting(self, gate: bool | None) -> None:
        """A non-True circulation gate HOLDS the window (no growth, no reset)."""
        monitor = LoopFlowMonitor()
        for _ in range(4):
            _update(monitor)
        # Gate drops: the watchdog pauses and reports inactive.
        for _ in range(10):
            assert _update(monitor, circulation_evident=gate) == "inactive"
        assert monitor.no_flow_active is False
        # Gate returns: the 4 accumulated cycles still count.
        for _ in range(_STEPS_PER_WINDOW - 4 - 1):
            assert _update(monitor) == "ok"
        assert _update(monitor) == "no_flow"

    @pytest.mark.unit
    def test_missing_probes_hold_and_report_inactive(self) -> None:
        """Stale/missing probes HOLD every window and report inactive."""
        monitor = LoopFlowMonitor()
        for _ in range(5):
            _update(monitor)
        for _ in range(6):
            assert _update(monitor, supply_c=None) == "inactive"
        for _ in range(_STEPS_PER_WINDOW - 5 - 1):
            assert _update(monitor) == "ok"
        assert _update(monitor) == "no_flow"

    @pytest.mark.unit
    def test_no_commanded_value_restarts(self) -> None:
        """``commanded_pct=None`` (nothing emitted yet) restarts the monitor."""
        monitor = LoopFlowMonitor()
        for _ in range(5):
            _update(monitor)
        assert _update(monitor, commanded_pct=None) == "inactive"
        # Full window needed again after the restart.
        for _ in range(_STEPS_PER_WINDOW - 1):
            assert _update(monitor) == "ok"
        assert _update(monitor) == "no_flow"

    @pytest.mark.unit
    def test_inactive_mode_restarts(self) -> None:
        """OFF/TRANSITIONAL modes restart the monitor entirely."""
        monitor = LoopFlowMonitor()
        for _ in range(_STEPS_PER_WINDOW):
            _update(monitor)
        assert monitor.no_flow_active is True
        assert _update(monitor, mode=Mode.OFF) == "inactive"
        assert monitor.no_flow_active is False

    @pytest.mark.unit
    def test_middle_zone_command_resets_both_windows(self) -> None:
        """A command between closed (5) and open (15) resets both windows."""
        monitor = LoopFlowMonitor()
        for _ in range(_STEPS_PER_WINDOW - 1):
            _update(monitor)
        assert _update(monitor, commanded_pct=10.0) == "ok"
        for _ in range(_STEPS_PER_WINDOW - 1):
            assert _update(monitor) == "ok"
        assert _update(monitor) == "no_flow"


class TestLoopFlowMonitorStuckOpen:
    """Stuck-open window: persistent source signature on a closed command."""

    @pytest.mark.unit
    def test_persistent_cooling_signature_latches(self) -> None:
        """Command 0 + cold delta-T + return on the source side latches."""
        monitor = LoopFlowMonitor()
        status = ""
        for _ in range(_STEPS_PER_WINDOW):
            status = _update(
                monitor,
                mode=Mode.COOLING,
                commanded_pct=0.0,
                supply_c=18.0,
                return_c=20.0,
                room_temperature_c=26.0,
            )
        assert status == "stuck_open"
        assert monitor.stuck_open_active is True

    @pytest.mark.unit
    def test_sub_threshold_command_tail_is_still_guarded(self) -> None:
        """A 0.8 % residue command counts as closed (the incident's tail)."""
        monitor = LoopFlowMonitor()
        status = ""
        for _ in range(_STEPS_PER_WINDOW):
            status = _update(
                monitor,
                mode=Mode.HEATING,
                commanded_pct=0.8,
                supply_c=32.0,
                return_c=27.0,
                room_temperature_c=21.0,
            )
        assert status == "stuck_open"

    @pytest.mark.unit
    def test_manifold_bar_supply_probe_does_not_false_alarm(self) -> None:
        """A pre-valve supply probe alone (return at room) never latches.

        The return-vs-room condition is the decisive witness: a manifold-bar
        supply probe keeps a huge delta-T on a genuinely closed loop, but its
        RETURN rests near the slab/room temperature — no alarm.
        """
        monitor = LoopFlowMonitor()
        for _ in range(_STEPS_PER_WINDOW + 3):
            status = _update(
                monitor,
                mode=Mode.HEATING,
                commanded_pct=0.0,
                supply_c=35.0,  # bar before the valve: stays at the source
                return_c=21.3,  # post-valve: settled at the room/slab
                room_temperature_c=21.0,
            )
            assert status == "ok"
        assert monitor.stuck_open_active is False

    @pytest.mark.unit
    def test_missing_room_temperature_degrades_to_delta_t_only(self) -> None:
        """Without a room temperature the delta-T alone drives the window."""
        monitor = LoopFlowMonitor()
        status = ""
        for _ in range(_STEPS_PER_WINDOW):
            status = _update(
                monitor,
                mode=Mode.HEATING,
                commanded_pct=0.0,
                supply_c=35.0,
                return_c=21.3,
                room_temperature_c=None,
            )
        assert status == "stuck_open"

    @pytest.mark.unit
    def test_signature_decay_resets_the_window(self) -> None:
        """A closing loop whose signature decays below epsilon never latches."""
        monitor = LoopFlowMonitor()
        for i in range(_STEPS_PER_WINDOW + 3):
            # The delta-T collapses after 4 cycles (healthy valve closing).
            delta = 2.0 if i < 4 else 0.1
            status = _update(
                monitor,
                mode=Mode.COOLING,
                commanded_pct=0.0,
                supply_c=18.0,
                return_c=18.0 + delta,
                room_temperature_c=26.0,
            )
        assert status == "ok"
        assert monitor.stuck_open_active is False

    @pytest.mark.unit
    def test_stuck_window_is_not_gated_by_circulation(self) -> None:
        """The stuck-open window accumulates even with the gate unknown."""
        monitor = LoopFlowMonitor()
        status = ""
        for _ in range(_STEPS_PER_WINDOW):
            status = _update(
                monitor,
                mode=Mode.COOLING,
                commanded_pct=0.0,
                supply_c=18.0,
                return_c=20.0,
                room_temperature_c=26.0,
                circulation_evident=None,
            )
        assert status == "stuck_open"


# ---------------------------------------------------------------------------
# FlowWatchdog — per-room aggregation
# ---------------------------------------------------------------------------


def _dead_loop() -> LoopInput:
    """Open-commanded loop with no hydraulic response (supply == return)."""
    return LoopInput(
        valve_position_pct=None, supply_temperature_c=25.0, return_temperature_c=25.0
    )


def _flowing_loop() -> LoopInput:
    """Loop with a healthy heating through-flow signature."""
    return LoopInput(
        valve_position_pct=None, supply_temperature_c=30.0, return_temperature_c=25.0
    )


class TestFlowWatchdog:
    """Aggregation, multi-loop worst, pause and reset."""

    def _inputs(self, loops: tuple[LoopInput, ...]) -> RoomInputs:
        """Heating inputs with *loops* and a proven circulation gate."""
        return make_inputs(
            room_temperature_c=20.0, loops=loops, circulation_evident=True
        )

    @pytest.mark.unit
    def test_multi_loop_reports_per_loop_and_worst(self) -> None:
        """One dead loop out of two flags the room; statuses stay aligned."""
        watchdog = FlowWatchdog(ControllerConfig())
        inputs = self._inputs((_dead_loop(), _flowing_loop()))
        for _ in range(_STEPS_PER_WINDOW):
            watchdog.update(inputs, commanded_pct=50.0, dt_seconds=_DT_S)
        assert watchdog.loop_statuses == ("no_flow", "ok")
        assert watchdog.no_flow_active is True
        assert watchdog.stuck_open_active is False

    @pytest.mark.unit
    def test_no_probes_room_is_silently_inactive(self) -> None:
        """A room without water probes never accumulates nor flags."""
        watchdog = FlowWatchdog(ControllerConfig())
        loop = LoopInput(
            valve_position_pct=40.0,
            supply_temperature_c=None,
            return_temperature_c=None,
        )
        inputs = self._inputs((loop,))
        for _ in range(_STEPS_PER_WINDOW + 3):
            watchdog.update(inputs, commanded_pct=50.0, dt_seconds=_DT_S)
        assert watchdog.loop_statuses == ("inactive",)
        assert watchdog.no_flow_active is False

    @pytest.mark.unit
    def test_paused_watchdog_holds_everything(self) -> None:
        """``paused=True`` (running self-test) freezes all windows."""
        watchdog = FlowWatchdog(ControllerConfig())
        inputs = self._inputs((_dead_loop(),))
        for _ in range(4):
            watchdog.update(inputs, commanded_pct=50.0, dt_seconds=_DT_S)
        for _ in range(20):
            watchdog.update(inputs, commanded_pct=50.0, dt_seconds=_DT_S, paused=True)
        assert watchdog.no_flow_active is False
        for _ in range(_STEPS_PER_WINDOW - 4):
            watchdog.update(inputs, commanded_pct=50.0, dt_seconds=_DT_S)
        assert watchdog.no_flow_active is True

    @pytest.mark.unit
    def test_restart_windows_and_reset(self) -> None:
        """``restart_windows`` clears latches; ``reset`` drops the monitors."""
        watchdog = FlowWatchdog(ControllerConfig())
        inputs = self._inputs((_dead_loop(),))
        for _ in range(_STEPS_PER_WINDOW):
            watchdog.update(inputs, commanded_pct=50.0, dt_seconds=_DT_S)
        assert watchdog.no_flow_active is True
        watchdog.restart_windows()
        assert watchdog.no_flow_active is False
        assert watchdog.loop_statuses == ("inactive",)
        watchdog.reset()
        assert watchdog.loop_statuses == ()

    @pytest.mark.unit
    def test_loop_count_resize_follows_inputs(self) -> None:
        """The monitor list tracks the room's current loop count."""
        watchdog = FlowWatchdog(ControllerConfig())
        watchdog.update(
            self._inputs((_dead_loop(), _dead_loop())),
            commanded_pct=50.0,
            dt_seconds=_DT_S,
        )
        assert len(watchdog.loop_statuses) == 2
        watchdog.update(
            self._inputs((_dead_loop(),)), commanded_pct=50.0, dt_seconds=_DT_S
        )
        assert len(watchdog.loop_statuses) == 1


# ---------------------------------------------------------------------------
# ActuationSelfTest — the state machine in isolation
# ---------------------------------------------------------------------------


class TestActuationSelfTest:
    """Begin / advance / verdict / abort life cycle."""

    @pytest.mark.unit
    def test_begin_requires_positive_duration(self) -> None:
        """A non-positive duration raises ValueError."""
        test = ActuationSelfTest()
        with pytest.raises(ValueError, match="duration_s must be > 0"):
            test.begin(duration_s=0.0, loops=(_flowing_loop(),), mode=Mode.HEATING)

    @pytest.mark.unit
    def test_begin_without_probes_refuses(self) -> None:
        """No loop with both probes -> ``"no_probes"`` refusal."""
        test = ActuationSelfTest()
        bare = LoopInput(
            valve_position_pct=40.0,
            supply_temperature_c=None,
            return_temperature_c=None,
        )
        assert (
            test.begin(duration_s=1500.0, loops=(bare,), mode=Mode.HEATING)
            == "no_probes"
        )
        assert test.running is False

    @pytest.mark.unit
    def test_passes_on_delta_t_evidence(self) -> None:
        """A clear through-flow delta-T at the end grades the loop passed."""
        test = ActuationSelfTest()
        assert (
            test.begin(duration_s=1500.0, loops=(_dead_loop(),), mode=Mode.HEATING)
            is None
        )
        assert test.running is True
        assert test.report_status == "running"
        assert test.remaining_min == pytest.approx(25.0)
        for _ in range(4):
            assert test.advance(_DT_S, (_dead_loop(),), epsilon_k=0.3) is False
        # Final cycle: the loop now shows a flowing signature.
        assert test.advance(_DT_S, (_flowing_loop(),), epsilon_k=0.3) is True
        assert test.report_status == "passed"
        assert test.loop_results == ("passed",)
        assert test.failed is False

    @pytest.mark.unit
    def test_fails_on_no_response(self) -> None:
        """No delta-T and no displacement at the end -> failed + sticky flag."""
        test = ActuationSelfTest()
        test.begin(duration_s=1500.0, loops=(_dead_loop(),), mode=Mode.HEATING)
        for _ in range(5):
            test.advance(_DT_S, (_dead_loop(),), epsilon_k=0.3)
        assert test.running is False
        assert test.report_status == "failed"
        assert test.loop_results == ("failed",)
        assert test.failed is True

    @pytest.mark.unit
    def test_mixed_loops_report_untested_and_fail_overall(self) -> None:
        """A probe-less loop grades ``untested``; any failed loop fails all."""
        test = ActuationSelfTest()
        bare = LoopInput(
            valve_position_pct=None,
            supply_temperature_c=None,
            return_temperature_c=None,
        )
        test.begin(duration_s=_DT_S, loops=(_dead_loop(), bare), mode=Mode.HEATING)
        test.advance(_DT_S, (_dead_loop(), bare), epsilon_k=0.3)
        assert test.report_status == "failed"
        assert test.loop_results == ("failed", "untested")

    @pytest.mark.unit
    def test_abort_and_cancel_mark_aborted(self) -> None:
        """Abort/cancel stop the test without a verdict."""
        test = ActuationSelfTest()
        test.begin(duration_s=1500.0, loops=(_flowing_loop(),), mode=Mode.COOLING)
        test.cancel()
        assert test.running is False
        assert test.report_status == "aborted"
        assert test.loop_results == ()
        assert test.failed is False

    @pytest.mark.unit
    def test_reset_clears_the_last_result(self) -> None:
        """``reset`` drops even the sticky failed verdict."""
        test = ActuationSelfTest()
        test.begin(duration_s=_DT_S, loops=(_dead_loop(),), mode=Mode.HEATING)
        test.advance(_DT_S, (_dead_loop(),), epsilon_k=0.3)
        assert test.failed is True
        test.reset()
        assert test.report_status is None
        assert test.failed is False


# ---------------------------------------------------------------------------
# ControllerConfig / RoomReport — the additive contract
# ---------------------------------------------------------------------------


class TestS6ContractValidation:
    """Validation of the additive S6 knobs and report fields."""

    @pytest.mark.unit
    def test_flow_knob_defaults(self) -> None:
        """The frozen S6 knob defaults construct and match the spec."""
        cfg = ControllerConfig()
        assert cfg.flow_epsilon_k == 0.3
        assert cfg.flow_open_threshold_pct == 15.0
        assert cfg.flow_response_window_min == 45.0

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("field_name", "value", "match"),
        [
            ("flow_epsilon_k", 0.0, "flow_epsilon_k must be > 0"),
            ("flow_epsilon_k", -1.0, "flow_epsilon_k must be > 0"),
            (
                "flow_open_threshold_pct",
                101.0,
                r"flow_open_threshold_pct must be in \[0, 100\]",
            ),
            (
                "flow_open_threshold_pct",
                -1.0,
                r"flow_open_threshold_pct must be in \[0, 100\]",
            ),
            (
                "flow_response_window_min",
                0.0,
                "flow_response_window_min must be > 0",
            ),
        ],
    )
    def test_invalid_flow_knobs_rejected(
        self, field_name: str, value: float, match: str
    ) -> None:
        """Out-of-range S6 knobs raise ValueError naming the knob."""
        with pytest.raises(ValueError, match=match):
            ControllerConfig(**{field_name: value})

    def _report(self, **overrides: object) -> RoomReport:
        """Minimal valid report with S6 field overrides."""
        base: dict[str, object] = {
            "error_c": 0.0,
            "trend_c_per_h": 0.0,
            "room_dew_point_c": None,
            "p_term": 0.0,
            "i_term": 0.0,
            "trend_term": 0.0,
            "feedforward_term": 0.0,
            "raw_valve_pct": 0.0,
            "valve_floor_applied": False,
            "saturated": False,
            "dew_throttle_factor": 1.0,
            "integrator_frozen": False,
            "flags": (),
            "explanation": "",
        }
        base.update(overrides)
        return RoomReport(**base)  # type: ignore[arg-type]

    @pytest.mark.unit
    def test_report_accepts_valid_s6_payload(self) -> None:
        """Valid statuses/verdicts construct and serialise to lists."""
        report = self._report(
            loop_flow_status=("ok", "no_flow", "stuck_open", "inactive"),
            actuation_test_status="running",
            actuation_test_remaining_min=12.5,
            actuation_test_loops=("passed", "failed", "untested"),
        )
        payload = report.to_dict()
        assert payload["loop_flow_status"] == [
            "ok",
            "no_flow",
            "stuck_open",
            "inactive",
        ]
        assert payload["actuation_test_status"] == "running"
        assert payload["actuation_test_remaining_min"] == 12.5
        assert payload["actuation_test_loops"] == ["passed", "failed", "untested"]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "overrides",
        [
            {"loop_flow_status": ("bogus",)},
            {"actuation_test_status": "bogus"},
            {"actuation_test_remaining_min": -1.0},
            {"actuation_test_loops": ("bogus",)},
        ],
    )
    def test_report_rejects_invalid_s6_payload(
        self, overrides: dict[str, object]
    ) -> None:
        """Out-of-vocabulary S6 report values raise ValueError."""
        with pytest.raises(ValueError, match="must be"):
            self._report(**overrides)


# ---------------------------------------------------------------------------
# RoomController wiring — freeze, flags, command reference, self-test
# ---------------------------------------------------------------------------


def _dead_open_inputs(**overrides: object) -> RoomInputs:
    """Heating inputs whose loop never responds (supply == return)."""
    defaults: dict[str, object] = {
        "room_temperature_c": 20.0,
        "loops": (_dead_loop(),),
        "circulation_evident": True,
    }
    defaults.update(overrides)
    return make_inputs(**defaults)  # type: ignore[arg-type]


class TestRoomControllerNoFlow:
    """S6 integration in the room controller's step."""

    @pytest.mark.unit
    def test_no_flow_flags_and_freezes_within_one_window(self) -> None:
        """A dead open loop flags loop_no_flow and freezes the integrator."""
        controller = RoomController(ControllerConfig(), name="salon")
        flagged_at: int | None = None
        last = None
        for i in range(_STEPS_PER_WINDOW + 3):
            last = controller.step(_dead_open_inputs(), dt_seconds=_DT_S)
            assert last.valve_position_pct >= 15.0, "command must stay open"
            if "loop_no_flow" in last.report.flags:
                flagged_at = i
                break
        assert last is not None
        assert flagged_at is not None, "loop_no_flow never raised"
        # Window + 1 cycle: the first step only ESTABLISHES the emitted
        # command; accumulation starts on the second.
        assert flagged_at <= _STEPS_PER_WINDOW + 1
        assert last.report.integrator_frozen is True
        assert last.report.loop_flow_status == ("no_flow",)
        # The reaction is flags + freeze ONLY — no valve banging.
        assert last.valve_position_pct < 100.0

    @pytest.mark.unit
    def test_unknown_circulation_holds_and_reports_inactive(self) -> None:
        """Without a circulation verdict the watchdog never flags."""
        controller = RoomController(ControllerConfig(), name="salon")
        last = None
        for _ in range(_STEPS_PER_WINDOW + 3):
            last = controller.step(
                _dead_open_inputs(circulation_evident=None), dt_seconds=_DT_S
            )
        assert last is not None
        assert "loop_no_flow" not in last.report.flags
        assert last.report.loop_flow_status == ("inactive",)

    @pytest.mark.unit
    def test_last_emitted_reference_tracks_the_safety_override(self) -> None:
        """The S6 command reference is the FINAL emitted valve position.

        An S2 hard stop forces the valve to 0 after the PI computed an open
        command; the watchdog must see 0 (a stuck-open candidate), not the
        pre-safety PI value (a no-flow candidate).
        """
        controller = RoomController(ControllerConfig(), name="lazienka")
        # Cooling with the supply BELOW the room dew point: S2 hard rule.
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=26.0,
            humidity_pct=70.0,
            loops=(
                LoopInput(
                    valve_position_pct=None,
                    supply_temperature_c=17.0,
                    return_temperature_c=19.0,
                ),
            ),
            circulation_evident=True,
        )
        out = controller.step(inputs, dt_seconds=_DT_S)
        assert "s2_condensation" in out.report.flags
        assert out.valve_position_pct == 0.0
        assert controller._last_emitted_valve_pct == 0.0  # noqa: SLF001

    @pytest.mark.unit
    def test_reset_clears_the_s6_state(self) -> None:
        """``reset()`` drops the watchdog, self-test and command reference."""
        controller = RoomController(ControllerConfig(), name="salon")
        for _ in range(_STEPS_PER_WINDOW + 1):
            controller.step(_dead_open_inputs(), dt_seconds=_DT_S)
        controller.reset()
        out = controller.step(_dead_open_inputs(), dt_seconds=_DT_S)
        assert "loop_no_flow" not in out.report.flags


class TestRoomControllerSelfTest:
    """Actuation self-test driven through the room controller."""

    def _warm_controller(self) -> RoomController:
        """Heating controller with one live step on a flowing loop."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(_dead_open_inputs(loops=(_flowing_loop(),)), dt_seconds=_DT_S)
        return controller

    @pytest.mark.unit
    def test_begin_before_any_step_refuses(self) -> None:
        """Without a seen mode the test cannot start."""
        controller = RoomController(ControllerConfig(), name="salon")
        assert controller.begin_actuation_test(1500.0) == "mode_inactive"

    @pytest.mark.unit
    def test_begin_without_probes_refuses(self) -> None:
        """A room without water probes refuses the test."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(make_inputs(room_temperature_c=20.0), dt_seconds=_DT_S)
        assert controller.begin_actuation_test(1500.0) == "no_probes"

    @pytest.mark.unit
    def test_excursion_verdict_and_watchdog_rearm(self) -> None:
        """The excursion drives 100 %, freezes the PI, then grades passed."""
        controller = self._warm_controller()
        assert controller.begin_actuation_test(1500.0) is None
        assert controller.begin_actuation_test(1500.0) == "already_running"

        inputs = _dead_open_inputs(loops=(_flowing_loop(),))
        out = controller.step(inputs, dt_seconds=_DT_S)
        assert out.valve_position_pct == 100.0
        assert "actuation_test_running" in out.report.flags
        assert out.report.actuation_test_status == "running"
        assert out.report.integrator_frozen is True
        assert out.report.actuation_test_remaining_min is not None

        for _ in range(4):
            out = controller.step(inputs, dt_seconds=_DT_S)
        assert out.report.actuation_test_status == "passed"
        assert out.report.actuation_test_loops == ("passed",)
        assert "actuation_test_running" not in out.report.flags
        # The completing step still carried the excursion; the NEXT step
        # returns to the plain PI value through the normal write path.
        out = controller.step(inputs, dt_seconds=_DT_S)
        assert out.valve_position_pct < 100.0
        assert out.report.actuation_test_status == "passed"

    @pytest.mark.unit
    def test_failed_verdict_raises_the_sticky_flag(self) -> None:
        """A dead loop grades failed and keeps ``actuation_test_failed``."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(_dead_open_inputs(), dt_seconds=_DT_S)
        assert controller.begin_actuation_test(1500.0) is None
        out = None
        for _ in range(5):
            out = controller.step(_dead_open_inputs(), dt_seconds=_DT_S)
        assert out is not None
        assert out.report.actuation_test_status == "failed"
        assert "actuation_test_failed" in out.report.flags

    @pytest.mark.unit
    def test_sensor_loss_aborts_a_running_test(self) -> None:
        """Losing the room temperature aborts the excursion."""
        controller = self._warm_controller()
        controller.begin_actuation_test(1500.0)
        out = controller.step(
            _dead_open_inputs(loops=(_flowing_loop(),), room_temperature_c=None),
            dt_seconds=_DT_S,
        )
        assert out.report.actuation_test_status == "aborted"
        assert out.valve_position_pct != 100.0

    @pytest.mark.unit
    def test_cancel_returns_the_valve_to_the_pi(self) -> None:
        """A user cancel marks aborted; the next step is plain PI."""
        controller = self._warm_controller()
        controller.begin_actuation_test(1500.0)
        controller.step(_dead_open_inputs(loops=(_flowing_loop(),)), dt_seconds=_DT_S)
        controller.cancel_actuation_test()
        out = controller.step(
            _dead_open_inputs(loops=(_flowing_loop(),)), dt_seconds=_DT_S
        )
        assert out.valve_position_pct < 100.0
        assert out.report.actuation_test_status == "aborted"

    @pytest.mark.unit
    def test_cooling_with_throttled_dew_gap_refuses(self) -> None:
        """A throttled dew margin refuses the 100 % chilled-water excursion."""
        controller = RoomController(ControllerConfig(), name="lazienka")
        # Supply above the dew point but INSIDE the margin+ramp band: the
        # graduated throttle is < 1, so the excursion must be refused.
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=26.0,
            humidity_pct=70.0,
            loops=(
                LoopInput(
                    valve_position_pct=None,
                    supply_temperature_c=20.5,
                    return_temperature_c=22.0,
                ),
            ),
            circulation_evident=True,
        )
        out = controller.step(inputs, dt_seconds=_DT_S)
        assert 0.0 < out.report.dew_throttle_factor < 1.0
        assert controller.begin_actuation_test(1500.0) == "dew_unsafe"


# ---------------------------------------------------------------------------
# BuildingController wiring — circulation gate + stuck-open dew inclusion
# ---------------------------------------------------------------------------


class TestBuildingCirculationGate:
    """The injected ``circulation_evident`` gate from raw inputs."""

    @pytest.mark.unit
    def test_other_loops_healthy_delta_t_proves_circulation(self) -> None:
        """A dead room flags once ANOTHER loop shows a live delta-T."""
        building = BuildingController(
            {"dead": ControllerConfig(), "alive": ControllerConfig()}
        )
        inputs = {
            "dead": _dead_open_inputs(circulation_evident=None),
            "alive": make_inputs(room_temperature_c=20.0, loops=(_flowing_loop(),)),
        }
        out = None
        for _ in range(_STEPS_PER_WINDOW + 2):
            out = building.step(inputs, dt_seconds=_DT_S)
        assert out is not None
        assert "loop_no_flow" in out.rooms["dead"].report.flags
        assert "loop_no_flow" not in out.rooms["alive"].report.flags

    @pytest.mark.unit
    def test_pump_provably_idle_holds_every_window(self) -> None:
        """All loops dead (delta-T ~ 0 everywhere) -> gate False -> no flags."""
        building = BuildingController(
            {"a": ControllerConfig(), "b": ControllerConfig()}
        )
        inputs = {
            "a": _dead_open_inputs(circulation_evident=None),
            "b": _dead_open_inputs(circulation_evident=None),
        }
        out = None
        for _ in range(_STEPS_PER_WINDOW + 3):
            out = building.step(inputs, dt_seconds=_DT_S)
        assert out is not None
        for name in ("a", "b"):
            assert "loop_no_flow" not in out.rooms[name].report.flags
            assert out.rooms[name].report.loop_flow_status == ("inactive",)

    @pytest.mark.unit
    def test_global_supply_probe_proves_circulation(self) -> None:
        """A source-side global probe unlocks the gate for dead loops."""
        building = BuildingController({"dead": ControllerConfig()})
        inputs = {"dead": _dead_open_inputs(circulation_evident=None)}
        out = None
        for _ in range(_STEPS_PER_WINDOW + 2):
            out = building.step(
                inputs, dt_seconds=_DT_S, global_supply_temperature_c=35.0
            )
        assert out is not None
        assert "loop_no_flow" in out.rooms["dead"].report.flags

    @pytest.mark.unit
    def test_global_probe_is_suspended_during_dhw(self) -> None:
        """``hp_active_for_ufh=False`` suspends the global-probe gate path.

        During DHW/defrost the manifold probe may keep reading source-side
        while every UFH loop is legitimately starved — the gate must not
        accumulate no-flow windows from it.
        """
        building = BuildingController({"dead": ControllerConfig()})
        inputs = {
            "dead": _dead_open_inputs(circulation_evident=None, hp_active_for_ufh=False)
        }
        out = None
        for _ in range(_STEPS_PER_WINDOW + 3):
            out = building.step(
                inputs, dt_seconds=_DT_S, global_supply_temperature_c=35.0
            )
        assert out is not None
        assert "loop_no_flow" not in out.rooms["dead"].report.flags
        assert out.rooms["dead"].report.loop_flow_status == ("inactive",)


class TestStuckOpenDewInclusion:
    """A stuck-open COOLING room is forced into the global safe dew point."""

    def _bathroom_inputs(self) -> RoomInputs:
        """Cooling-disabled room whose loop keeps a cold source signature."""
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=26.0,
            humidity_pct=55.0,
            cooling_enabled=False,
            loops=(
                LoopInput(
                    valve_position_pct=None,
                    supply_temperature_c=18.0,
                    return_temperature_c=20.0,
                ),
            ),
        )

    @pytest.mark.unit
    def test_stuck_open_room_feeds_the_global_dew_point(self) -> None:
        """The excluded room re-enters the dew maximum once stuck-open latches."""
        building = BuildingController({"lazienka": ControllerConfig()})
        inputs = {"lazienka": self._bathroom_inputs()}

        early = building.step(inputs, dt_seconds=_DT_S)
        assert early.rooms["lazienka"].report.dew_excluded_reason == (
            "cooling_disabled"
        )
        assert early.global_safe_dew_point_c is None

        out = early
        for _ in range(_STEPS_PER_WINDOW + 2):
            out = building.step(inputs, dt_seconds=_DT_S)
        report = out.rooms["lazienka"].report
        assert "loop_stuck_open" in report.flags
        assert report.dew_excluded_reason is None
        expected = dew_point(26.0, 55.0) + _GLOBAL_DEW_MARGIN_K
        assert out.global_safe_dew_point_c == pytest.approx(expected)
