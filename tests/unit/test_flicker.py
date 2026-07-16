"""Unit tests for the cooling setpoint-flicker (issue #7, 2026-07-15).

Exercises the pure core added in :mod:`tortoise_ufh.hp_link`:

* :class:`~tortoise_ufh.hp_link.SetpointFlicker` — the persistent state machine
  that drops the written cooling setpoint for ONE cycle to trip a Panasonic
  Aquarea compressor out of its fixed 3 K return-water deadband, then restores
  the dew-safe value. Covered: the happy path (a single pulse + restore + a
  cooldown before the next), the real-demand gate, the rate / short-cycle cap,
  the dew clamp, and the interrupted / restart safety.
* :func:`~tortoise_ufh.hp_link.cooling_demand` — the loop-weighted demand gate
  (DECISIONS §23) the adapter feeds the machine: rooms genuinely calling for
  cooling must together command enough loop-weighted valve opening
  (``sum(valve_pct x loops) >= hp_flicker_min_open_pct``) before a compressor
  start may be forced.

Everything here is seed-independent and never imports ``homeassistant``. Time is
driven purely by :meth:`SetpointFlicker.tick` (dt in seconds); the machine reads
no wall clock.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.hp_link import (
    FLICKER_DEMAND_ERROR_K,
    FLICKER_DEW_RESERVE_K,
    FLICKER_START_OFFSET_K,
    FlickerDecision,
    SetpointFlicker,
    cooling_demand,
)
from custom_components.tortoise_ufh.core.models import (
    FastSourceCommand,
    RoomOutputs,
    RoomReport,
)

_DT: float = 300.0  # nominal 5-min control cycle [s]


def _report(**overrides: object) -> RoomReport:
    """Build a RoomReport with cooling-calling defaults, overridable per test.

    Defaults describe a genuinely-cooling room 0.5 K above its setpoint with an
    open dew throttle and no lost sensor — i.e. one that DOES call for cooling.
    """
    base: dict[str, object] = {
        "error_c": -0.5,  # cooling sign: 0.5 K above setpoint
        "trend_c_per_h": 0.0,
        "room_dew_point_c": 14.0,
        "p_term": 0.0,
        "i_term": 0.0,
        "trend_term": 0.0,
        "feedforward_term": 0.0,
        "raw_valve_pct": 50.0,
        "valve_floor_applied": False,
        "saturated": False,
        "dew_throttle_factor": 1.0,
        "integrator_frozen": False,
        "dew_excluded_reason": None,
    }
    base.update(overrides)
    return RoomReport(**base)  # type: ignore[arg-type]


def _output(
    valve_pct: float = 100.0, loops: int = 1, **report_overrides: object
) -> RoomOutputs:
    """Build a RoomOutputs for the demand gate: final valve + loop count.

    The loop count travels as the length of ``report.loop_flow_status`` (the
    S6 wrapper stamps one entry per configured loop); ``loops=0`` leaves the
    stamp empty to exercise the gate's one-loop fallback.
    """
    return RoomOutputs(
        valve_position_pct=valve_pct,
        fast_source=FastSourceCommand(on=False),
        report=_report(loop_flow_status=("inactive",) * loops, **report_overrides),
    )


def _run_to_idle(machine: SetpointFlicker, **step_kwargs: object) -> None:
    """Advance the machine out of its initial cooldown into ``idle``.

    Ticks with the given (armed) step kwargs until the machine reports ``idle``;
    guards against an infinite loop.
    """
    for _ in range(100):
        machine.tick(_DT)
        decision = machine.step(**step_kwargs)  # type: ignore[arg-type]
        if decision.state == "idle":
            return
    msg = "machine never reached the idle state"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# cooling_demand
# ---------------------------------------------------------------------------


class TestCoolingDemand:
    """The loop-weighted "enough rooms genuinely call for cooling" gate (§23)."""

    @pytest.mark.unit
    def test_calling_room_yields_demand(self) -> None:
        """An eligible room above setpoint, throttle open, healthy → demand."""
        gate = cooling_demand([_output()], min_open_pct=100.0)
        assert gate.demand is True
        assert gate.open_pct == 100.0
        assert gate.threshold_pct == 100.0

    @pytest.mark.unit
    def test_empty_reports_have_no_demand(self) -> None:
        """No rooms means no demand."""
        gate = cooling_demand([], min_open_pct=100.0)
        assert gate.demand is False
        assert gate.open_pct == 0.0

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reason",
        ["not_cooling_mode", "cooling_disabled", "no_temperature", "no_humidity"],
    )
    def test_dew_excluded_room_does_not_call(self, reason: str) -> None:
        """A room excluded from the safe dew point never counts as demand."""
        outputs = [_output(dew_excluded_reason=reason)]
        assert cooling_demand(outputs, min_open_pct=100.0).demand is False

    @pytest.mark.unit
    def test_room_at_or_below_setpoint_does_not_call(self) -> None:
        """The cooling sign: a room at/below its setpoint is not calling."""
        # error_c = setpoint - room_temp; a cool-enough room is >= 0.
        assert (
            cooling_demand([_output(error_c=0.0)], min_open_pct=100.0).demand is False
        )
        assert (
            cooling_demand([_output(error_c=0.5)], min_open_pct=100.0).demand is False
        )

    @pytest.mark.unit
    def test_room_just_below_the_error_threshold_does_not_call(self) -> None:
        """Being warmer than setpoint by < the error threshold does not call."""
        just_under = -(FLICKER_DEMAND_ERROR_K - 0.05)
        assert (
            cooling_demand([_output(error_c=just_under)], min_open_pct=100.0).demand
            is False
        )
        assert (
            cooling_demand(
                [_output(error_c=-FLICKER_DEMAND_ERROR_K)], min_open_pct=100.0
            ).demand
            is True
        )

    @pytest.mark.unit
    def test_none_error_does_not_call(self) -> None:
        """A room with no error reading cannot call for cooling."""
        assert (
            cooling_demand([_output(error_c=None)], min_open_pct=100.0).demand is False
        )

    @pytest.mark.unit
    def test_mostly_throttled_room_does_not_call(self) -> None:
        """A room whose local dew throttle is mostly shut is ignored."""
        assert (
            cooling_demand(
                [_output(dew_throttle_factor=0.4)], min_open_pct=100.0
            ).demand
            is False
        )
        assert (
            cooling_demand(
                [_output(dew_throttle_factor=0.5)], min_open_pct=100.0
            ).demand
            is True
        )

    @pytest.mark.unit
    def test_sensor_lost_room_does_not_call(self) -> None:
        """A degraded (sensor-lost) room never counts as demand."""
        outputs = [_output(flags=("sensor_lost",))]
        assert cooling_demand(outputs, min_open_pct=100.0).demand is False

    @pytest.mark.unit
    def test_single_small_caller_stays_below_the_gate(self) -> None:
        """The owner's night case: one fully open loop must not force a start.

        One room ~1 K over its setpoint, one loop, valve 100 % → Σ = 100,
        which sits below the 250 default — the buffer tank covers a single
        loop's draw, so the flicker must stay quiet.
        """
        outputs = [_output(valve_pct=100.0, loops=1, error_c=-1.0)] + [
            _output(valve_pct=0.0, loops=1, error_c=0.1) for _ in range(10)
        ]
        gate = cooling_demand(outputs, min_open_pct=250.0)
        assert gate.demand is False
        assert gate.open_pct == 100.0

    @pytest.mark.unit
    def test_loop_weighting_scales_a_rooms_contribution(self) -> None:
        """A 3-loop room at 90 % contributes 270 — enough to clear 250 alone."""
        gate = cooling_demand([_output(valve_pct=90.0, loops=3)], min_open_pct=250.0)
        assert gate.demand is True
        assert gate.open_pct == pytest.approx(270.0)

    @pytest.mark.unit
    def test_calling_rooms_sum_across_the_building(self) -> None:
        """Σ accumulates over every calling room; the threshold is inclusive."""
        outputs = [
            _output(valve_pct=85.0, loops=2),  # 170
            _output(valve_pct=80.0, loops=1),  # 80 -> 250 exactly
        ]
        gate = cooling_demand(outputs, min_open_pct=250.0)
        assert gate.open_pct == pytest.approx(250.0)
        assert gate.demand is True  # >= is inclusive

    @pytest.mark.unit
    def test_non_calling_rooms_never_add_their_valves(self) -> None:
        """Open valves of rooms NOT calling (at setpoint) stay out of Σ."""
        outputs = [
            _output(valve_pct=100.0, loops=1),  # the only caller: 100
            _output(valve_pct=100.0, loops=3, error_c=0.0),  # satisfied room
        ]
        gate = cooling_demand(outputs, min_open_pct=250.0)
        assert gate.open_pct == 100.0
        assert gate.demand is False

    @pytest.mark.unit
    def test_unstamped_loop_status_counts_as_one_loop(self) -> None:
        """A report without the S6 stamp degrades to a per-room weight of 1."""
        gate = cooling_demand([_output(valve_pct=100.0, loops=0)], min_open_pct=100.0)
        assert gate.open_pct == 100.0
        assert gate.demand is True

    @pytest.mark.unit
    def test_any_calling_room_wins(self) -> None:
        """One genuinely-calling room among excluded ones carries the gate."""
        outputs = [
            _output(dew_excluded_reason="cooling_disabled"),
            _output(error_c=0.2),
            _output(),  # the one caller: 100
        ]
        gate = cooling_demand(outputs, min_open_pct=100.0)
        assert gate.demand is True
        assert gate.open_pct == 100.0


# ---------------------------------------------------------------------------
# SetpointFlicker
# ---------------------------------------------------------------------------

# A machine tuned for fast tests: 5-min stuck, 5-min cooldown so a pulse is
# reachable in a couple of cycles, with the default 2 starts/hour cap.
_FAST_CFG = ControllerConfig(
    hp_flicker_band_k=1.5,
    hp_flicker_stuck_minutes=5.0,
    hp_flicker_min_off_minutes=5.0,
    hp_flicker_max_starts_per_h=2.0,
)

# Armed cooling inputs: idle compressor, return well above the trigger, a
# dew-safe pulse with headroom (w=18, safe_dew=16 -> raw dew 14, p=14 on a 1 K
# grid; trigger = max(18+1.5, 14+3) = 19.5).
_ARMED: dict[str, object] = {
    "cooling_active": True,
    "demand": True,
    "hp_return_c": 21.0,
    "compressor_freq_hz": 0.0,
    "written_target_c": 18.0,
    "safe_dew_c": 16.0,
    "step_c": 1.0,
}


class TestSetpointFlickerHappyPath:
    """Scenario 1: a single pulse + restore, then a cooldown."""

    @pytest.mark.unit
    def test_starts_in_cooldown(self) -> None:
        """A fresh machine is in cooldown (safe after a restart)."""
        assert SetpointFlicker(_FAST_CFG).state == "cooldown"

    @pytest.mark.unit
    def test_single_pulse_then_restore_then_quiet(self) -> None:
        """One pulse to the dew-safe floor, one restore, no immediate re-pulse."""
        machine = SetpointFlicker(_FAST_CFG)
        _run_to_idle(machine, **_ARMED)

        # Accumulate 'stuck & armed' time until the pulse fires.
        pulse: FlickerDecision | None = None
        for _ in range(10):
            machine.tick(_DT)
            decision = machine.step(**_ARMED)  # type: ignore[arg-type]
            if decision.pulse_target_c is not None:
                pulse = decision
                break
        assert pulse is not None, "the machine never pulsed"
        # p = ceil((16 - 2) / 1) * 1 = 14, on the grid and >= the raw dew point.
        raw_dew = _ARMED["safe_dew_c"] - FLICKER_DEW_RESERVE_K  # type: ignore[operator]
        assert pulse.pulse_target_c == pytest.approx(14.0)
        assert pulse.pulse_target_c >= raw_dew
        assert pulse.state == "pulse"
        assert "flicker_pulsing" in pulse.flags
        assert pulse.trigger_c == pytest.approx(
            max(18.0 + 1.5, 14.0 + FLICKER_START_OFFSET_K)
        )

        # The very next step restores the normal target (no write value).
        machine.tick(_DT)
        restore = machine.step(**_ARMED)  # type: ignore[arg-type]
        assert restore.pulse_target_c is None
        assert restore.restore_pending is True
        assert restore.state == "cooldown"
        assert restore.last_pulse_target_c == pytest.approx(14.0)

        # No re-pulse for at least the min-off cooldown window.
        cooldown_cycles = int(_FAST_CFG.hp_flicker_min_off_minutes * 60 / _DT)
        for _ in range(cooldown_cycles):
            machine.tick(_DT)
            decision = machine.step(**_ARMED)  # type: ignore[arg-type]
            assert decision.pulse_target_c is None

    @pytest.mark.unit
    def test_no_sensor_blocks_and_flags(self) -> None:
        """A missing inlet/compressor reading blocks pulses and flags it."""
        machine = SetpointFlicker(_FAST_CFG)
        no_return = {**_ARMED, "hp_return_c": None}
        pulsed = False
        saw_flag = False
        for _ in range(30):
            machine.tick(_DT)
            decision = machine.step(**no_return)  # type: ignore[arg-type]
            pulsed = pulsed or decision.pulse_target_c is not None
            saw_flag = saw_flag or "flicker_no_sensor" in decision.flags
        assert not pulsed
        assert saw_flag


class TestSetpointFlickerRealDemandGate:
    """Scenario 2: no demand -> no pulse, whatever the return does."""

    @pytest.mark.unit
    def test_parked_return_without_demand_never_pulses(self) -> None:
        """Inlet parked high for hours but demand=False ⇒ zero pulses."""
        machine = SetpointFlicker(_FAST_CFG)
        no_demand = {**_ARMED, "demand": False}
        # ~3 h of cycles.
        for _ in range(36):
            machine.tick(_DT)
            decision = machine.step(**no_demand)  # type: ignore[arg-type]
            assert decision.pulse_target_c is None
            assert "flicker_pulsing" not in decision.flags


class TestSetpointFlickerRateCap:
    """Scenario 3: the rolling-hour cap + cooldown gaps + the freq reset."""

    @pytest.mark.unit
    def test_pulses_respect_the_rolling_hour_cap_and_cooldown(self) -> None:
        """Over ~3 h: <= max_starts per rolling hour and gaps >= the cooldown."""
        machine = SetpointFlicker(_FAST_CFG)
        elapsed = 0.0
        pulse_times: list[float] = []
        for _ in range(36):  # 36 * 300 s = 3 h
            machine.tick(_DT)
            elapsed += _DT
            decision = machine.step(**_ARMED)  # type: ignore[arg-type]
            if decision.pulse_target_c is not None:
                pulse_times.append(elapsed)
        assert len(pulse_times) >= 2, "expected repeated pulses to exercise the cap"
        # No rolling hour holds more than the cap.
        cap = int(_FAST_CFG.hp_flicker_max_starts_per_h)
        for anchor in pulse_times:
            window = [t for t in pulse_times if anchor - 3600.0 < t <= anchor]
            assert len(window) <= cap
        # Consecutive forced starts are at least the cooldown apart.
        min_gap = _FAST_CFG.hp_flicker_min_off_minutes * 60.0
        gaps = [b - a for a, b in zip(pulse_times, pulse_times[1:], strict=False)]
        assert all(gap >= min_gap for gap in gaps)

    @pytest.mark.unit
    def test_compressor_on_resets_the_stuck_timer(self) -> None:
        """A ``freq > 0`` cycle mid-accumulation resets the stuck timer."""
        # A longer stuck time (15 min = 3 cycles) so accumulation spans several
        # cycles and the reset is observable before a pulse would fire.
        cfg = ControllerConfig(
            hp_flicker_stuck_minutes=15.0,
            hp_flicker_min_off_minutes=5.0,
            hp_flicker_max_starts_per_h=2.0,
        )
        machine = SetpointFlicker(cfg)
        _run_to_idle(machine, **_ARMED)
        # One armed cycle accumulates one tick of 'stuck' time (no pulse yet).
        machine.tick(_DT)
        first = machine.step(**_ARMED)  # type: ignore[arg-type]
        assert first.pulse_target_c is None
        assert first.stuck_remaining_s == pytest.approx(15.0 * 60 - _DT)
        # The compressor turns on: not idle -> the accumulation is reset.
        running = {**_ARMED, "compressor_freq_hz": 30.0}
        machine.tick(_DT)
        machine.step(**running)  # type: ignore[arg-type]
        # Back to idle: only one tick of progress again (NOT two), proving the
        # reset — without it the remaining time would have dropped further.
        machine.tick(_DT)
        after = machine.step(**_ARMED)  # type: ignore[arg-type]
        assert after.stuck_remaining_s == pytest.approx(15.0 * 60 - _DT)


class TestSetpointFlickerDewClamp:
    """Scenario 4: the pulse must never cross the raw dew point."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("safe_dew", "w", "step"),
        [
            (16.0, 18.0, 1.0),  # headroom: p=14, not blocked
            (20.0, 22.0, 0.5),  # headroom: p=18, not blocked
        ],
    )
    def test_headroom_case_pulses_on_grid(
        self, safe_dew: float, w: float, step: float
    ) -> None:
        """With headroom the pulse lands on-grid, >= raw dew, trigger correct."""
        inputs = {
            **_ARMED,
            "safe_dew_c": safe_dew,
            "written_target_c": w,
            "step_c": step,
            "hp_return_c": w + 10.0,
        }
        machine = SetpointFlicker(_FAST_CFG)
        _run_to_idle(machine, **inputs)
        raw_dew = safe_dew - FLICKER_DEW_RESERVE_K
        pulse: FlickerDecision | None = None
        for _ in range(10):
            machine.tick(_DT)
            decision = machine.step(**inputs)  # type: ignore[arg-type]
            assert decision.trigger_c == pytest.approx(
                max(w + 1.5, raw_dew + FLICKER_START_OFFSET_K)
            )
            if decision.pulse_target_c is not None:
                pulse = decision
                break
        assert pulse is not None
        assert pulse.pulse_target_c is not None
        assert pulse.pulse_target_c >= raw_dew
        # On the grid (a multiple of the step).
        assert pulse.pulse_target_c % step == pytest.approx(0.0)

    @pytest.mark.unit
    def test_no_headroom_blocks_the_pulse_and_flags(self) -> None:
        """A coarse grid with no drop room ⇒ no pulse + ``flicker_dew_blocked``."""
        # step=3, w=18, safe_dew=18 -> raw dew 16, p=ceil(16/3)*3=18 > w-step=15.
        blocked = {
            **_ARMED,
            "safe_dew_c": 18.0,
            "written_target_c": 18.0,
            "step_c": 3.0,
            "hp_return_c": 25.0,
        }
        machine = SetpointFlicker(_FAST_CFG)
        _run_to_idle(machine, **blocked)
        saw_flag = False
        for _ in range(30):
            machine.tick(_DT)
            decision = machine.step(**blocked)  # type: ignore[arg-type]
            assert decision.pulse_target_c is None
            saw_flag = saw_flag or "flicker_dew_blocked" in decision.flags
        assert saw_flag


class TestSetpointFlickerInterruptedAndRestart:
    """Scenario 5: mid-pulse gate break restores; a fresh machine is safe."""

    @pytest.mark.unit
    def test_gate_break_after_pulse_still_restores(self) -> None:
        """If cooling stops one step after a pulse, the restore is unconditional."""
        machine = SetpointFlicker(_FAST_CFG)
        _run_to_idle(machine, **_ARMED)
        # Drive to a pulse.
        for _ in range(10):
            machine.tick(_DT)
            decision = machine.step(**_ARMED)  # type: ignore[arg-type]
            if decision.pulse_target_c is not None:
                break
        assert machine.state == "pulse"
        # The mode flips out of COOLING before the restore cycle runs.
        broken = {
            **_ARMED,
            "cooling_active": False,
            "written_target_c": None,
            "safe_dew_c": None,
        }
        machine.tick(_DT)
        restore = machine.step(**broken)  # type: ignore[arg-type]
        assert restore.restore_pending is True
        assert restore.pulse_target_c is None
        assert restore.state == "cooldown"

    @pytest.mark.unit
    def test_fresh_machine_holds_off_for_the_cooldown(self) -> None:
        """A just-built machine cannot pulse until the min-off cooldown elapses."""
        machine = SetpointFlicker(_FAST_CFG)
        cooldown_cycles = int(_FAST_CFG.hp_flicker_min_off_minutes * 60 / _DT)
        for _ in range(cooldown_cycles):
            machine.tick(_DT)
            decision = machine.step(**_ARMED)  # type: ignore[arg-type]
            assert decision.pulse_target_c is None

    @pytest.mark.unit
    def test_feature_idle_is_benign_and_resets_stuck(self) -> None:
        """Missing dew / not cooling yields a benign decision (no pulse)."""
        machine = SetpointFlicker(_FAST_CFG)
        idle = {**_ARMED, "cooling_active": False}
        machine.tick(_DT)
        decision = machine.step(**idle)  # type: ignore[arg-type]
        assert decision.pulse_target_c is None
        assert decision.restore_pending is False
        assert decision.flags == ()
