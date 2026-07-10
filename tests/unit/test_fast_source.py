"""Unit tests for the fast-source (split) logic of :mod:`tortoise_ufh.controller`.

Exercises the :class:`~tortoise_ufh.controller.RoomController` fast-source
decision path against the frozen black-box contract:

* The fast source (split) engages only beyond ``boost_offset_c`` and respects
  the minimum ON/OFF dwell timers.
* Anti priority-inversion: enabling the split never lowers the computed valve.
* The ``fast_dwell_remaining_s`` report field counts the dwell locks down.
* dt accumulates into the fast dwell timer exactly once per step (fix
  2026-07-10), also under a sustained S1 safety override.
* The split direction is machine state (C6): debounced recomputes, mode
  changes during dwell holds and deadband crossings never flip it; a real
  reversal is OFF-gated through the full min-OFF.
* The machine consumes the physical on/off feedback (S4) and flags a
  persistent divergence.
* ``boost_offset_c`` must exceed ``deadband_c`` (D2) at construction.

Units: temperatures / setpoints in degC; valve in percent (0..100);
``dt_seconds`` in seconds; dwell timers in minutes (config) and seconds
(report). This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import RoomController
from custom_components.tortoise_ufh.core.models import (
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
    RoomOutputs,
)
from tests.unit.conftest import make_inputs


class TestFastSourceBoost:
    """Split engagement gate and minimum ON/OFF dwell timers."""

    @pytest.mark.unit
    def test_split_stays_off_within_boost_offset(self) -> None:
        """Demand below the boost offset keeps the split OFF."""
        cfg = ControllerConfig(boost_offset_c=1.0)
        controller = RoomController(cfg, name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=20.5,  # demand 0.5 K < boost 1.0 K
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_split_engages_beyond_boost_offset(self) -> None:
        """Demand beyond the boost offset engages the split in the heat mode."""
        cfg = ControllerConfig(boost_offset_c=1.0)
        controller = RoomController(cfg, name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,  # demand 2.0 K > boost 1.0 K
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING
        # S12 (2026-07-09): the boost target is setpoint + 1 K so the split's
        # ceiling-mounted sensor does not throttle the unit before the boost
        # is delivered; release still belongs to OUR room sensor.
        assert out.fast_source.target_temperature_c == pytest.approx(22.0)

    @pytest.mark.unit
    def test_split_respects_min_on_dwell(self) -> None:
        """An engaged split cannot turn off until its min ON dwell has elapsed."""
        cfg = ControllerConfig(
            boost_offset_c=1.0, fast_min_on_minutes=10.0, deadband_c=0.3
        )
        controller = RoomController(cfg, name="salon")
        # Engage (demand 2 K); dwell timer resets to 0.
        engaged = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert engaged.fast_source.on is True

        # Now satisfied, but only 5 min < 10 min min-ON have elapsed: blocked.
        blocked = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=21.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert blocked.fast_source.on is True
        assert "fast_source_min_runtime" in blocked.report.flags

        # A further 5 min reaches 10 min total: the split may now release.
        released = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=21.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert released.fast_source.on is False

    @pytest.mark.unit
    def test_split_respects_min_off_dwell(self) -> None:
        """A released split cannot re-engage until its min OFF dwell elapses."""
        cfg = ControllerConfig(
            boost_offset_c=1.0,
            fast_min_on_minutes=5.0,
            fast_min_off_minutes=10.0,
            deadband_c=0.3,
        )
        controller = RoomController(cfg, name="salon")
        # Engage (demand 2 K); dwell timer resets to 0.
        engaged = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert engaged.fast_source.on is True

        # Satisfied after 5 min == min-ON: the split releases; timer resets.
        released = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=21.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert released.fast_source.on is False

        # Boost re-demanded, but only 5 min < 10 min min-OFF: re-engage blocked.
        blocked = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert blocked.fast_source.on is False
        assert "fast_source_min_runtime" in blocked.report.flags

        # A further 5 min reaches 10 min min-OFF: the split may re-engage.
        reengaged = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert reengaged.fast_source.on is True


class TestAntiPriorityInversion:
    """Enabling the split never lowers the computed valve position."""

    @pytest.mark.unit
    def test_split_does_not_lower_valve(self) -> None:
        """A split heating the room never suppresses the base valve.

        Drives the real feedback loop: as the split closes the comfort gap the
        room warms cycle by cycle, the PID (with trend damping) winds down and
        would fall below the floor. While heat is still called for the base
        valve must stay at or above ``valve_floor_pct`` (step-11 protection),
        and the split path must never drop below a no-split controller fed the
        same warming trajectory.
        """
        cfg = ControllerConfig(boost_offset_c=1.0, valve_floor_pct=15.0, deadband_c=0.3)
        without = RoomController(cfg, name="a")
        with_split = RoomController(cfg, name="b")
        # Room warms toward the setpoint as the split heats it.
        trajectory = (19.0, 19.6, 20.0, 20.3, 20.6)
        floor_exercised = False
        engaged_once = False
        for room in trajectory:
            out_none = without.step(
                make_inputs(
                    setpoint_c=21.0,
                    room_temperature_c=room,
                    fast_source_kind=FastSourceKind.NONE,
                ),
                dt_seconds=300.0,
            )
            out_split = with_split.step(
                make_inputs(
                    setpoint_c=21.0,
                    room_temperature_c=room,
                    fast_source_kind=FastSourceKind.SPLIT,
                ),
                dt_seconds=300.0,
            )
            assert out_none.fast_source.on is False
            engaged_once = engaged_once or out_split.fast_source.on
            # error_c > deadband <=> error_db > 0 <=> heat is still called for.
            if out_split.report.error_c > cfg.deadband_c:
                assert out_split.valve_position_pct >= cfg.valve_floor_pct
                # The split path must not additionally suppress the valve.
                assert out_split.valve_position_pct >= out_none.valve_position_pct
                assert out_split.valve_position_pct == pytest.approx(
                    out_none.valve_position_pct
                )
            if out_split.report.valve_floor_applied:
                floor_exercised = True
        # The scenario really engaged the split and really hit the floor guard.
        assert engaged_once is True
        assert floor_exercised is True


class TestFastDwellRemaining:
    """The ``fast_dwell_remaining_s`` report field counts down the min dwell."""

    @pytest.mark.unit
    def test_no_fast_source_reports_none(self) -> None:
        """A room without a fast source reports no dwell timer."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        assert out.report.fast_dwell_remaining_s is None

    @pytest.mark.unit
    def test_dwell_counts_down_then_clears(self) -> None:
        """After engaging, the min-ON lock counts down and clears to None."""
        cfg = ControllerConfig(
            boost_offset_c=1.0, fast_min_on_minutes=10.0, deadband_c=0.3
        )
        controller = RoomController(cfg, name="salon")

        def demand(temp: float) -> RoomOutputs:
            return controller.step(
                make_inputs(
                    setpoint_c=21.0,
                    room_temperature_c=temp,
                    fast_source_kind=FastSourceKind.SPLIT,
                ),
                dt_seconds=300.0,
            )

        # Engage (demand 2 K): timer resets to 0, min-ON lock = 600 s remains.
        engaged = demand(19.0)
        assert engaged.fast_source.on is True
        assert engaged.report.fast_dwell_remaining_s == pytest.approx(600.0)

        # 300 s later, satisfied but min-ON blocks release: 300 s remain.
        blocked = demand(21.0)
        assert blocked.fast_source.on is True
        assert "fast_source_min_runtime" in blocked.report.flags
        assert blocked.report.fast_dwell_remaining_s == pytest.approx(300.0)

        # 600 s total reaches min-ON: the split releases, min-OFF lock starts.
        released = demand(21.0)
        assert released.fast_source.on is False
        assert released.report.fast_dwell_remaining_s == pytest.approx(600.0)

        # Idle and satisfied: the min-OFF lock elapses, then clears to None.
        assert demand(21.0).report.fast_dwell_remaining_s == pytest.approx(300.0)
        assert demand(21.0).report.fast_dwell_remaining_s is None


class TestFastDwellSingleAccumulation:
    """Fix 2026-07-10: dt accumulates into the fast dwell timer exactly once.

    Before the fix the normal fast-source decision added dt AND a safety
    override in the same step added it again, so under a sustained S1 the
    min-OFF wait elapsed twice as fast as wall-clock time.
    """

    def _s1_inputs(self, room_temp: float, supply: float) -> RoomInputs:
        return make_inputs(
            room_temperature_c=room_temp,
            loops=(LoopInput(None, supply, None),),
            fast_source_kind=FastSourceKind.SPLIT,
        )

    @pytest.mark.unit
    def test_sustained_s1_advances_dwell_by_exactly_dt(self) -> None:
        """N held-S1 steps of dt=300 s advance the timer by N*300, not 2N*300.

        The room sits INSIDE the comfort band so the normal path issues no
        engage requests: the timer moves only through the per-step
        accumulation (plus the safety force-OFF edge reset).
        """
        controller = RoomController(ControllerConfig(), name="salon")
        # Cold room, healthy supply: the split engages (dwell clock -> 0).
        engaged = controller.step(self._s1_inputs(19.0, 35.0), dt_seconds=300.0)
        assert engaged.fast_source.on is True
        # Room recovers into the band; S1 trips (45 > 40): safety forces OFF.
        tripped = controller.step(self._s1_inputs(21.0, 45.0), dt_seconds=300.0)
        assert "s1_floor_overheat" in tripped.report.flags
        assert tripped.fast_source.on is False
        # The ON->OFF edge restarts the OFF stretch at 0.
        assert controller._fast_timer_s == pytest.approx(0.0)
        # S1 held by hysteresis (39 > 38): exactly +300 s per 300 s step.
        for n in range(1, 4):
            out = controller.step(self._s1_inputs(21.0, 39.0), dt_seconds=300.0)
            assert "s1_floor_overheat" in out.report.flags
            assert out.fast_source.on is False
            assert controller._fast_timer_s == pytest.approx(n * 300.0)

    @pytest.mark.unit
    def test_min_off_after_safety_release_counts_from_actual_stop(self) -> None:
        """Min-OFF needs the full 10 min from the ACTUAL stop, not 5.

        One held-S1 step (300 s) plus a 2 s debounced recompute after the rule
        clears is only 302 s of real OFF time — the double-counting bug saw
        602 s and re-engaged the compressor half a dwell too early.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        engaged = controller.step(self._s1_inputs(19.0, 35.0), dt_seconds=300.0)
        assert engaged.fast_source.on is True
        # S1 trip: force OFF (edge -> OFF stretch starts at 0).
        controller.step(self._s1_inputs(19.0, 45.0), dt_seconds=300.0)
        # One full cycle with S1 held: 300 s of genuine OFF time.
        held = controller.step(self._s1_inputs(19.0, 39.0), dt_seconds=300.0)
        assert held.fast_source.on is False
        # S1 clears (30 < 38); the cold room demands boost 2 s later, but only
        # 302 s have passed since the actual stop: min-OFF (600 s) must block.
        blocked = controller.step(self._s1_inputs(19.0, 30.0), dt_seconds=2.0)
        assert blocked.fast_source.on is False
        assert "fast_source_min_runtime" in blocked.report.flags
        # Once the full 10 min from the stop elapse, the boost returns.
        released = controller.step(self._s1_inputs(19.0, 30.0), dt_seconds=300.0)
        assert released.fast_source.on is True
        assert released.fast_source.mode is FastSourceMode.HEATING


class TestFastDirectionMachine:
    """C6 (2026-07-09): the split direction is machine state, never a flip.

    Covers the three failure scenarios from the algo-fast FMEA: a 2-second
    debounced recompute after a setpoint change, a global mode change during a
    min-ON hold, and the transitional fallback inside the deadband.
    """

    def _transitional(self, setpoint: float, temp: float) -> RoomInputs:
        return make_inputs(
            mode=Mode.TRANSITIONAL,
            setpoint_c=setpoint,
            room_temperature_c=temp,
            fast_source_kind=FastSourceKind.SPLIT,
        )

    @pytest.mark.unit
    def test_transitional_setpoint_drop_never_flips_in_two_seconds(self) -> None:
        """Scenario S1: a 2 s recompute after a -2 K setpoint change must
        re-emit the REMEMBERED heating direction, not cooling."""
        controller = RoomController(ControllerConfig(), name="salon")
        engaged = controller.step(self._transitional(21.0, 19.0), dt_seconds=300.0)
        assert engaged.fast_source.on is True
        assert engaged.fast_source.mode is FastSourceMode.HEATING

        # User drops the setpoint 2 K BELOW the room (a cooling demand
        # appears); the debounced recompute runs 2 s later.
        recompute = controller.step(self._transitional(17.0, 19.0), dt_seconds=2.0)
        assert recompute.fast_source.mode is not FastSourceMode.COOLING
        # The min-ON dwell holds the machine in its remembered direction.
        assert recompute.fast_source.on is True
        assert recompute.fast_source.mode is FastSourceMode.HEATING
        assert "fast_source_min_runtime" in recompute.report.flags

    @pytest.mark.unit
    def test_mode_change_during_min_on_hold_reemits_remembered_direction(
        self,
    ) -> None:
        """Scenario S2: HEATING -> COOLING while held by min-ON keeps HEATING."""
        controller = RoomController(ControllerConfig(), name="salon")
        engaged = controller.step(
            make_inputs(
                mode=Mode.HEATING,
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert engaged.fast_source.mode is FastSourceMode.HEATING

        # 5 min later the global mode flips to COOLING; min-ON (10 min) holds.
        held = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=21.0,
                room_temperature_c=19.0,
                humidity_pct=50.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        # The hold must NOT command active cooling for a room 2 K BELOW the
        # setpoint: the remembered HEATING direction is re-emitted.
        assert held.fast_source.on is True
        assert held.fast_source.mode is FastSourceMode.HEATING
        assert "fast_source_min_runtime" in held.report.flags

    @pytest.mark.unit
    def test_transitional_deadband_crossing_keeps_heating(self) -> None:
        """Scenario S3: 0.1 K above the setpoint while ON stays HEATING (S12:
        the split self-regulates at the setpoint; no OFF-COOLING fallback)."""
        controller = RoomController(ControllerConfig(), name="salon")
        engaged = controller.step(self._transitional(21.0, 19.0), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.HEATING

        crossed = controller.step(self._transitional(21.0, 21.1), dt_seconds=300.0)
        assert crossed.fast_source.mode is not FastSourceMode.COOLING
        # Inside/near the comfort band the split stays ON at target=setpoint.
        assert crossed.fast_source.on is True
        assert crossed.fast_source.mode is FastSourceMode.HEATING
        assert crossed.fast_source.target_temperature_c == pytest.approx(21.0)

    @pytest.mark.unit
    def test_direction_flip_goes_through_off_with_full_min_off(self) -> None:
        """A real reversal is OFF-gated: min-ON, then OFF for the FULL min-OFF."""
        cfg = ControllerConfig(fast_min_on_minutes=10.0, fast_min_off_minutes=10.0)
        controller = RoomController(cfg, name="salon")
        engaged = controller.step(self._transitional(21.0, 19.0), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.HEATING

        # Free gains overshoot the room far above the setpoint (spring sun).
        # min-ON elapsed (10 min after engage) -> the machine stops.
        controller.step(self._transitional(21.0, 20.0), dt_seconds=300.0)
        stopped = controller.step(self._transitional(21.0, 22.5), dt_seconds=300.0)
        assert stopped.fast_source.on is False

        # Cooling demand is present but min-OFF has not elapsed: still OFF.
        blocked = controller.step(self._transitional(21.0, 22.5), dt_seconds=300.0)
        assert blocked.fast_source.on is False
        assert "fast_source_min_runtime" in blocked.report.flags

        # Full min-OFF elapsed: the machine may finally engage COOLING.
        cooled = controller.step(self._transitional(21.0, 22.5), dt_seconds=300.0)
        assert cooled.fast_source.on is True
        assert cooled.fast_source.mode is FastSourceMode.COOLING

    @pytest.mark.unit
    def test_active_mode_boost_target_is_offset(self) -> None:
        """S12: active-mode boost targets are setpoint +1 K / -1 K."""
        heating = RoomController(ControllerConfig(), name="a")
        out_h = heating.step(
            make_inputs(
                mode=Mode.HEATING,
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert out_h.fast_source.target_temperature_c == pytest.approx(22.0)

        cooling = RoomController(ControllerConfig(), name="b")
        out_c = cooling.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=26.0,
                humidity_pct=45.0,
                loops=(LoopInput(None, 22.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert out_c.fast_source.on is True
        assert out_c.fast_source.mode is FastSourceMode.COOLING
        assert out_c.fast_source.target_temperature_c == pytest.approx(23.0)


class TestFastPhysicalSync:
    """S4 (2026-07-09): the machine consumes the physical on/off feedback."""

    def _inputs(self, temp: float, *, fast_on: bool | None) -> RoomInputs:
        return RoomInputs(
            mode=Mode.HEATING,
            setpoint_c=21.0,
            room_temperature_c=temp,
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_on=fast_on,
        )

    @pytest.mark.unit
    def test_running_unit_adopted_on_first_feedback(self) -> None:
        """A physically running split is adopted as ON with a fresh min-ON."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Room satisfied (no demand) but the unit is physically running.
        out = controller.step(self._inputs(21.0, fast_on=True), dt_seconds=300.0)
        # The machine adopted ON; min-ON (seeded conservatively) blocks the
        # immediate OFF, so the just-discovered compressor keeps running.
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING
        assert "fast_source_min_runtime" in out.report.flags

    @pytest.mark.unit
    def test_stopped_unit_seeds_conservative_min_off(self) -> None:
        """A physically stopped split waits a FULL min-OFF before engaging."""
        controller = RoomController(ControllerConfig(), name="salon")
        first = controller.step(self._inputs(19.0, fast_on=False), dt_seconds=300.0)
        # Demand is 2 K > boost, but the conservative restart seed blocks the
        # engage until the full min-OFF (10 min) has elapsed.
        assert first.fast_source.on is False
        assert "fast_source_min_runtime" in first.report.flags

        second = controller.step(self._inputs(19.0, fast_on=False), dt_seconds=300.0)
        assert second.fast_source.on is True

    @pytest.mark.unit
    def test_unknown_feedback_keeps_free_first_transition(self) -> None:
        """No feedback configured (None): the first engage stays unblocked."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._inputs(19.0, fast_on=None), dt_seconds=300.0)
        assert out.fast_source.on is True

    @pytest.mark.unit
    def test_persistent_divergence_flags_mismatch(self) -> None:
        """Feedback disagreeing with the previous command raises the flag."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0, fast_on=False), dt_seconds=300.0)
        engaged = controller.step(self._inputs(19.0, fast_on=False), dt_seconds=300.0)
        assert engaged.fast_source.on is True
        assert "fast_source_mismatch" not in engaged.report.flags

        # One settling cycle later the unit is STILL physically off.
        stuck = controller.step(self._inputs(19.0, fast_on=False), dt_seconds=300.0)
        assert "fast_source_mismatch" in stuck.report.flags

    @pytest.mark.unit
    def test_agreeing_feedback_never_flags(self) -> None:
        """Feedback matching the emitted command raises no mismatch."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0, fast_on=False), dt_seconds=300.0)
        engaged = controller.step(self._inputs(19.0, fast_on=False), dt_seconds=300.0)
        assert engaged.fast_source.on is True
        tracking = controller.step(self._inputs(19.0, fast_on=True), dt_seconds=300.0)
        assert "fast_source_mismatch" not in tracking.report.flags


class TestBoostOffsetValidation:
    """D2 (2026-07-09): boost_offset_c must exceed deadband_c."""

    @pytest.mark.unit
    def test_boost_below_deadband_rejected(self) -> None:
        """An inverted engage/release hysteresis is rejected at construction."""
        with pytest.raises(ValueError, match="boost_offset_c must be > deadband_c"):
            ControllerConfig(boost_offset_c=0.2, deadband_c=0.3)

    @pytest.mark.unit
    def test_boost_equal_deadband_rejected(self) -> None:
        """A zero-width hysteresis band is rejected too."""
        with pytest.raises(ValueError, match="boost_offset_c must be > deadband_c"):
            ControllerConfig(boost_offset_c=0.3, deadband_c=0.3)
