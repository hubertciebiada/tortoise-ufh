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
  persistent divergence (legacy path, ``fast_manual_hold_minutes = 0``).
* Manual hold (§28): a SETTLED divergence (our own emitted command stable for
  at least half a cycle — the second regular cycle after our change) is
  adopted as user intent and mirrored for ``fast_manual_hold_minutes``;
  safety forces end the hold and leave the mismatch flag as a trace; a demoted
  DRY reported via ``note_fast_source_written`` never raises a false hold,
  while a real touch on such a unit still does.
* Blind commands (§28 note, 2026-09-10): a command emitted while the unit's
  feedback is unavailable is no S4 reference — the first visible feedback is
  adopted as the truth (the HA-restart case), a feedback gap is judged
  against the last command the unit could have received.
* ``boost_offset_c`` must exceed ``deadband_c`` (D2) at construction.

Units: temperatures / setpoints in degC; valve in percent (0..100);
``dt_seconds`` in seconds; dwell timers in minutes (config) and seconds
(report). This module never imports ``homeassistant``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    BuildingController,
    RoomController,
)
from custom_components.tortoise_ufh.core.fast_source import (
    FastSourceMachine,
    window_allows,
)
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
    def test_sustained_s1_keeps_boost_and_timer_monotonic(self) -> None:
        """An active S1 no longer touches the split; the dwell has no sawtooth.

        Rewritten for K3 (2026-07-12): the old test pinned the FIXED bug (an
        S1 CLOSE_VALVE force-stopped the air-side boost every cycle, sawtooting
        the dwell clock and flapping the min-runtime flag). Now the water-side
        S1 parks the valve while the wanted boost keeps running, and the dwell
        timer advances by exactly dt per step — never resetting while the
        state holds.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        # Cold room, healthy supply: the split engages (dwell clock -> 0).
        engaged = controller.step(self._s1_inputs(19.0, 35.0), dt_seconds=300.0)
        assert engaged.fast_source.on is True
        # S1 trips (45 > 40) with the room still cold: the valve parks at 0,
        # the boost keeps running (air side untouched, K3).
        tripped = controller.step(self._s1_inputs(19.0, 45.0), dt_seconds=300.0)
        assert "s1_floor_overheat" in tripped.report.flags
        assert tripped.valve_position_pct == pytest.approx(0.0)
        assert tripped.fast_source.on is True
        assert tripped.fast_source.mode is FastSourceMode.HEATING
        assert controller._fast_timer_s == pytest.approx(300.0)
        # S1 held by hysteresis (39 > 38): exactly +300 s per 300 s step,
        # split still ON, no flapping min-runtime flag once the min-ON passed.
        for n in range(2, 5):
            out = controller.step(self._s1_inputs(19.0, 39.0), dt_seconds=300.0)
            assert "s1_floor_overheat" in out.report.flags
            assert out.valve_position_pct == pytest.approx(0.0)
            assert out.fast_source.on is True
            assert controller._fast_timer_s == pytest.approx(n * 300.0)
            if n * 300.0 >= 600.0:  # min-ON elapsed: no runtime lock either
                assert "fast_source_min_runtime" not in out.report.flags

    @pytest.mark.unit
    def test_min_off_after_forced_stop_counts_from_actual_stop(self) -> None:
        """Min-OFF needs the full 10 min from the ACTUAL stop, not 5.

        One held sensor-lost step (300 s) plus a 2 s debounced recompute after
        the sensor recovers is only 302 s of real OFF time — the
        double-counting bug saw 602 s and re-engaged the compressor half a
        dwell too early. (Rewritten for K3, 2026-07-12: an S1 no longer force-
        stops the split, so the forced-OFF path is exercised via sensor loss,
        which still does.)
        """
        controller = RoomController(ControllerConfig(), name="salon")
        engaged = controller.step(self._s1_inputs(19.0, 35.0), dt_seconds=300.0)
        assert engaged.fast_source.on is True
        sensor_lost = replace(self._s1_inputs(19.0, 35.0), room_temperature_c=None)
        # Sensor lost: force OFF (edge -> OFF stretch starts at 0).
        lost = controller.step(sensor_lost, dt_seconds=300.0)
        assert lost.fast_source.on is False
        # One full cycle still lost: 300 s of genuine OFF time.
        held = controller.step(sensor_lost, dt_seconds=300.0)
        assert held.fast_source.on is False
        # The sensor recovers; the cold room demands boost 2 s later, but only
        # 302 s have passed since the actual stop: min-OFF (600 s) must block.
        blocked = controller.step(self._s1_inputs(19.0, 35.0), dt_seconds=2.0)
        assert blocked.fast_source.on is False
        assert "fast_source_min_runtime" in blocked.report.flags
        # Once the full 10 min from the stop elapse, the boost returns.
        released = controller.step(self._s1_inputs(19.0, 35.0), dt_seconds=300.0)
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

    @pytest.mark.unit
    def test_boost_target_offset_is_a_knob_and_zero_disables(self) -> None:
        """fast_target_offset_k (2026-07-13): scales the S12 overdrive; 0 = off."""
        for offset, expect_h, expect_c in ((0.0, 21.0, 24.0), (2.5, 23.5, 21.5)):
            heating = RoomController(
                ControllerConfig(fast_target_offset_k=offset), name="a"
            )
            out_h = heating.step(
                make_inputs(
                    mode=Mode.HEATING,
                    setpoint_c=21.0,
                    room_temperature_c=19.0,
                    fast_source_kind=FastSourceKind.SPLIT,
                ),
                dt_seconds=300.0,
            )
            assert out_h.fast_source.target_temperature_c == pytest.approx(expect_h)

            cooling = RoomController(
                ControllerConfig(fast_target_offset_k=offset), name="b"
            )
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
            assert out_c.fast_source.target_temperature_c == pytest.approx(expect_c)

    @pytest.mark.unit
    def test_boost_target_offset_range_is_validated(self) -> None:
        """fast_target_offset_k outside [0, 3] is rejected at construction."""
        for bad in (-0.1, 3.5):
            with pytest.raises(ValueError, match="fast_target_offset_k"):
                ControllerConfig(fast_target_offset_k=bad)


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
        """Feedback disagreeing with the previous command raises the flag.

        Legacy path (§28): with ``fast_manual_hold_minutes = 0`` a divergence
        is NOT adopted — the machine stays the owner and only flags it.
        """
        controller = RoomController(
            ControllerConfig(fast_manual_hold_minutes=0.0), name="salon"
        )
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


class TestWindowAllows:
    """Pure quiet-hours window arithmetic (B1, 2026-07-12)."""

    @pytest.mark.unit
    def test_normal_window_inclusive_start_exclusive_end(self) -> None:
        """A day window allows start <= t < end."""
        start, end = 7 * 60, 22 * 60  # 07:00-22:00
        assert window_allows(7 * 60, start, end) is True
        assert window_allows(12 * 60, start, end) is True
        assert window_allows(21 * 60 + 59, start, end) is True
        assert window_allows(22 * 60, start, end) is False
        assert window_allows(6 * 60 + 59, start, end) is False
        assert window_allows(23 * 60, start, end) is False

    @pytest.mark.unit
    def test_window_crossing_midnight(self) -> None:
        """A 22:00-07:00 window allows the night and blocks the day."""
        start, end = 22 * 60, 7 * 60
        assert window_allows(23 * 60, start, end) is True
        assert window_allows(0, start, end) is True
        assert window_allows(6 * 60 + 59, start, end) is True
        assert window_allows(22 * 60, start, end) is True
        assert window_allows(7 * 60, start, end) is False
        assert window_allows(12 * 60, start, end) is False
        assert window_allows(21 * 60 + 59, start, end) is False

    @pytest.mark.unit
    def test_degenerate_equal_edges_is_empty(self) -> None:
        """start == end (rejected by the flow) reads as an EMPTY window."""
        assert window_allows(12 * 60, 8 * 60, 8 * 60) is False

    @pytest.mark.unit
    def test_out_of_range_arguments_rejected(self) -> None:
        """Minutes outside [0, 1439] are a caller bug and raise."""
        with pytest.raises(ValueError, match="minute_of_day"):
            window_allows(1440, 0, 60)
        with pytest.raises(ValueError, match="start_minute"):
            window_allows(0, -1, 60)
        with pytest.raises(ValueError, match="end_minute"):
            window_allows(0, 0, 2000)


class TestQuietHours:
    """B1 (2026-07-12): fast_source_allowed=False suppresses the fast source."""

    @pytest.mark.unit
    def test_quiet_blocks_engagement_despite_demand(self) -> None:
        """An idle split does not engage during quiet hours; flag raised."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=18.0,  # demand 3 K >> boost 1 K
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_allowed=False,
            ),
            dt_seconds=300.0,
        )
        assert out.fast_source.on is False
        assert "fast_source_quiet_hours" in out.report.flags
        # The floor keeps working: quiet hours only silence the AIR side.
        assert out.valve_position_pct > 0.0

    @pytest.mark.unit
    def test_window_end_honours_min_on_dwell(self) -> None:
        """A running split at the window edge stops only after min-ON."""
        cfg = ControllerConfig(fast_min_on_minutes=10.0)
        controller = RoomController(cfg, name="salon")
        engaged = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert engaged.fast_source.on is True

        # Quiet hours begin 5 min in: min-ON (10 min) still holds the unit ON.
        held = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_allowed=False,
            ),
            dt_seconds=300.0,
        )
        assert held.fast_source.on is True
        assert "fast_source_min_runtime" in held.report.flags
        assert "fast_source_quiet_hours" in held.report.flags

        # Another 5 min reaches the 10-min dwell: the unit releases.
        released = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_allowed=False,
            ),
            dt_seconds=300.0,
        )
        assert released.fast_source.on is False
        assert "fast_source_quiet_hours" in released.report.flags

    @pytest.mark.unit
    def test_transitional_quiet_suppresses_the_only_source(self) -> None:
        """TRANSITIONAL quiet hours idle the split too (quiet is quiet)."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.TRANSITIONAL,
                setpoint_c=21.0,
                room_temperature_c=18.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_allowed=False,
            ),
            dt_seconds=300.0,
        )
        assert out.fast_source.on is False
        assert "fast_source_quiet_hours" in out.report.flags

    @pytest.mark.unit
    def test_s3_emergency_heat_breaks_quiet_hours(self) -> None:
        """A frost emergency (S3) forces the split ON despite quiet hours."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=4.0,  # below the 5 degC S3 frost limit
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_allowed=False,
            ),
            dt_seconds=300.0,
        )
        assert "s3_emergency_heat" in out.report.flags
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING

    @pytest.mark.unit
    def test_no_flag_or_change_when_allowed(self) -> None:
        """The default fast_source_allowed=True changes nothing (regression)."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert out.fast_source.on is True
        assert "fast_source_quiet_hours" not in out.report.flags


# ---------------------------------------------------------------------------
# Dry assist (2026-07-16, DECISIONS §24)
# ---------------------------------------------------------------------------


def _dry_cfg(**overrides: object) -> ControllerConfig:
    """A dry-assist config with fast dwells so releases are observable.

    ``dry_dew_max_c = 15`` pairs with the humidity grid used below (room at
    24 degC): RH 70 % -> dew ~18.2 (engages), RH 56 % -> dew ~14.7 (inside the
    1 K hysteresis band: holds, never engages fresh), RH 52 % -> dew ~13.5
    (releases).
    """
    base: dict[str, object] = {
        "dry_enabled": True,
        "dry_dew_max_c": 15.0,
        "boost_offset_c": 1.0,
        "deadband_c": 0.3,
        "fast_min_on_minutes": 5.0,
        "fast_min_off_minutes": 5.0,
    }
    base.update(overrides)
    return ControllerConfig(**base)  # type: ignore[arg-type]


def _dry_inputs(**overrides: object) -> RoomInputs:
    """COOLING split room AT its setpoint with muggy air (dew ~18.2 degC)."""
    base: dict[str, object] = {
        "mode": Mode.COOLING,
        "setpoint_c": 24.0,
        "room_temperature_c": 24.0,
        "humidity_pct": 70.0,
        "fast_source_kind": FastSourceKind.SPLIT,
    }
    base.update(overrides)
    return make_inputs(**base)  # type: ignore[arg-type]


class TestDryAssist:
    """Humidity-triggered split DRY: engage/hysteresis/priority (§24)."""

    @pytest.mark.unit
    def test_dry_engages_above_threshold(self) -> None:
        """A room AT setpoint with dew above the knob gets ON + DRY."""
        controller = RoomController(_dry_cfg(), name="salon")
        out = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.DRY
        # Dry mode self-regulates; no temperature target is commanded.
        assert out.fast_source.target_temperature_c is None
        assert "dry_assist" in out.report.flags

    @pytest.mark.unit
    def test_dry_is_off_by_default(self) -> None:
        """Without the opt-in knob the muggy room commands nothing."""
        controller = RoomController(_dry_cfg(dry_enabled=False), name="salon")
        out = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert out.fast_source.on is False
        assert "dry_assist" not in out.report.flags

    @pytest.mark.unit
    def test_heater_never_dries(self) -> None:
        """A heater-kind fast source cannot dehumidify (nor cool)."""
        controller = RoomController(_dry_cfg(), name="salon")
        out = controller.step(
            _dry_inputs(fast_source_kind=FastSourceKind.HEATER), dt_seconds=300.0
        )
        assert out.fast_source.on is False
        assert "dry_assist" not in out.report.flags

    @pytest.mark.unit
    def test_dry_only_in_cooling_mode(self) -> None:
        """HEATING and TRANSITIONAL never emit a DRY command."""
        for mode in (Mode.HEATING, Mode.TRANSITIONAL):
            controller = RoomController(_dry_cfg(), name="salon")
            out = controller.step(_dry_inputs(mode=mode), dt_seconds=300.0)
            assert out.fast_source.mode is not FastSourceMode.DRY
            assert "dry_assist" not in out.report.flags

    @pytest.mark.unit
    def test_no_humidity_blocks_dry(self) -> None:
        """Without a dew point (no humidity) the dry assist stays quiet."""
        controller = RoomController(_dry_cfg(), name="salon")
        out = controller.step(_dry_inputs(humidity_pct=None), dt_seconds=300.0)
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_quiet_hours_block_dry(self) -> None:
        """The B1 quiet-hours verdict also gates the dry assist."""
        controller = RoomController(_dry_cfg(), name="salon")
        out = controller.step(_dry_inputs(fast_source_allowed=False), dt_seconds=300.0)
        assert out.fast_source.on is False
        assert "fast_source_quiet_hours" in out.report.flags

    @pytest.mark.unit
    def test_hysteresis_holds_then_releases(self) -> None:
        """Dew inside the 1 K band holds a run; below the band it releases."""
        controller = RoomController(_dry_cfg(), name="salon")
        engaged = controller.step(_dry_inputs(humidity_pct=70.0), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        # Dew ~14.7: below the 15.0 threshold but above 14.0 -> still drying.
        held = controller.step(_dry_inputs(humidity_pct=56.0), dt_seconds=300.0)
        assert held.fast_source.on is True
        assert held.fast_source.mode is FastSourceMode.DRY
        # Dew ~13.5: below the hysteresis band -> off (min-ON has elapsed).
        released = controller.step(_dry_inputs(humidity_pct=52.0), dt_seconds=300.0)
        assert released.fast_source.on is False

    @pytest.mark.unit
    def test_band_dew_never_engages_fresh(self) -> None:
        """Dew inside the hysteresis band does not START a run."""
        controller = RoomController(_dry_cfg(), name="salon")
        out = controller.step(_dry_inputs(humidity_pct=56.0), dt_seconds=300.0)
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_overcooled_room_releases_dry(self) -> None:
        """A room past the deadband BELOW setpoint stops drying."""
        controller = RoomController(_dry_cfg(), name="salon")
        engaged = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        # error_c = 24.0 - 23.5 = +0.5 >= deadband 0.3 -> overcool guard.
        released = controller.step(
            _dry_inputs(room_temperature_c=23.5), dt_seconds=300.0
        )
        assert released.fast_source.on is False

    @pytest.mark.unit
    def test_slightly_overcooled_room_does_not_engage_dry(self) -> None:
        """A fresh dry engage needs the room AT or ABOVE the setpoint.

        Regression (2026-07-17, owner's bedroom): with the single
        ``error < deadband`` gate a muggy room sitting a hair inside the
        deadband re-engaged the moment min-OFF elapsed, short-cycling the
        split dry -> overcool -> pause -> dry.
        """
        controller = RoomController(_dry_cfg(), name="salon")
        # error_c = 24.0 - 23.8 = +0.2 < deadband 0.3, dew ~18 > 15: the old
        # gate engaged here; the hysteretic engage side must not.
        out = controller.step(_dry_inputs(room_temperature_c=23.8), dt_seconds=300.0)
        assert out.fast_source.on is False
        assert "dry_assist" not in out.report.flags

    @pytest.mark.unit
    def test_running_dry_keeps_through_sub_deadband_overcool(self) -> None:
        """A RUNNING dry keeps drying until the deadband release (keep side)."""
        controller = RoomController(_dry_cfg(), name="salon")
        engaged = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        # error_c = +0.2 is inside the keep band (< deadband 0.3): still DRY.
        held = controller.step(_dry_inputs(room_temperature_c=23.8), dt_seconds=300.0)
        assert held.fast_source.on is True
        assert held.fast_source.mode is FastSourceMode.DRY

    @pytest.mark.unit
    def test_overcool_release_needs_return_to_setpoint_to_rearm(self) -> None:
        """The owner's bedroom cycle: after an overcool release, dew alone
        must not re-engage — the room has to warm back TO the setpoint."""
        controller = RoomController(_dry_cfg(), name="salon")
        engaged = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        # Overcooled past the deadband after min-ON: releases.
        released = controller.step(
            _dry_inputs(room_temperature_c=23.5), dt_seconds=300.0
        )
        assert released.fast_source.on is False
        # min-OFF has elapsed and the air is still muggy, but the room is
        # still 0.2 K below setpoint: the dwell is NOT what holds it off.
        still_off = controller.step(
            _dry_inputs(room_temperature_c=23.8), dt_seconds=300.0
        )
        assert still_off.fast_source.on is False
        # Back at the setpoint: the next dry run may start.
        rearmed = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert rearmed.fast_source.on is True
        assert rearmed.fast_source.mode is FastSourceMode.DRY

    @pytest.mark.unit
    def test_boost_preempts_dry_same_cycle(self) -> None:
        """A temperature boost flips DRY -> COOLING directly (no OFF cycle)."""
        controller = RoomController(_dry_cfg(), name="salon")
        engaged = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        boosted = controller.step(
            _dry_inputs(room_temperature_c=25.5),  # demand 1.5 K > boost 1.0
            dt_seconds=300.0,
        )
        assert boosted.fast_source.on is True
        assert boosted.fast_source.mode is FastSourceMode.COOLING
        assert boosted.fast_source.target_temperature_c == pytest.approx(23.0)
        assert "dry_assist" not in boosted.report.flags

    @pytest.mark.unit
    def test_dry_run_does_not_hold_a_sub_boost_temperature(self) -> None:
        """Anti-inversion: a dry run must not lower the boost threshold.

        After the dew releases, a temperature excess between the deadband and
        the boost offset must NOT keep the split running as a cooler.
        """
        controller = RoomController(_dry_cfg(), name="salon")
        engaged = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        out = controller.step(
            # Dew released (RH 50 -> ~13.4); demand 0.5 K (deadband < d < boost).
            _dry_inputs(room_temperature_c=24.5, humidity_pct=50.0),
            dt_seconds=300.0,
        )
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_min_on_tail_stays_presented_as_dry(self) -> None:
        """A dew release inside min-ON keeps the DRY presentation, not COOL."""
        controller = RoomController(_dry_cfg(fast_min_on_minutes=10.0), name="salon")
        engaged = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        # 5 min elapsed < 10 min min-ON: the machine re-emits its remembered
        # COOLING, presented as DRY (gentler than cool-at-target for a
        # satisfied room).
        blocked = controller.step(_dry_inputs(humidity_pct=52.0), dt_seconds=300.0)
        assert blocked.fast_source.on is True
        assert blocked.fast_source.mode is FastSourceMode.DRY
        assert "fast_source_min_runtime" in blocked.report.flags

    @pytest.mark.unit
    def test_mode_flip_mid_dry_drops_the_dry_presentation(self) -> None:
        """A COOLING->HEATING flip inside min-ON must not keep writing dry.

        Review finding (2026-07-16): the blocked tail re-emits the remembered
        COOLING direction, and without the mode gate the DRY presentation
        leaked into HEATING mode for the whole dwell remainder.
        """
        controller = RoomController(_dry_cfg(fast_min_on_minutes=10.0), name="salon")
        engaged = controller.step(_dry_inputs(), dt_seconds=300.0)
        assert engaged.fast_source.mode is FastSourceMode.DRY
        # Global mode flips to HEATING 5 min in (< 10 min min-ON): the machine
        # is dwell-blocked and re-emits its remembered COOLING — which must be
        # presented as COOLING, never as DRY, outside cooling mode.
        flipped = controller.step(_dry_inputs(mode=Mode.HEATING), dt_seconds=300.0)
        assert flipped.fast_source.mode is not FastSourceMode.DRY
        assert "dry_assist" not in flipped.report.flags

    @pytest.mark.unit
    def test_dry_feedback_is_not_a_mismatch(self) -> None:
        """A unit reporting hvac dry (or cool) during a dry run is in sync."""
        controller = RoomController(_dry_cfg(), name="salon")
        controller.step(_dry_inputs(), dt_seconds=300.0)
        # The first visible feedback is adopted as the truth (the blind DRY
        # above never reached the unit); the loop below then compares against
        # a DRY recorded on a synced machine.
        controller.step(
            _dry_inputs(fast_source_on=True, fast_source_hvac_mode="dry"),
            dt_seconds=300.0,
        )
        for feedback in ("dry", "cool"):
            out = controller.step(
                _dry_inputs(fast_source_on=True, fast_source_hvac_mode=feedback),
                dt_seconds=300.0,
            )
            assert "fast_source_mismatch" not in out.report.flags

    @pytest.mark.unit
    def test_unit_found_drying_keeps_the_dry_band(self) -> None:
        """A unit found in ``dry`` (restart or feedback gap) keeps drying.

        The blind sensor-lost cycle before it recorded an OFF; were that the
        last command mode, the room 0.2 K under the setpoint would fail the
        fresh dry start (``error <= 0``) and the adopted COOLING would run as
        a temperature boost instead. The first sync marks the running unit as
        drying, so the dry keep band applies.
        """
        controller = RoomController(_dry_cfg(), name="salon")
        controller.step(_dry_inputs(room_temperature_c=None), dt_seconds=300.0)
        found = controller.step(
            _dry_inputs(
                room_temperature_c=23.8,
                fast_source_on=True,
                fast_source_hvac_mode="dry",
            ),
            dt_seconds=300.0,
        )
        assert found.fast_source.on is True
        assert found.fast_source.mode is FastSourceMode.DRY

    @pytest.mark.unit
    def test_opposite_feedback_during_dry_is_a_mismatch(self) -> None:
        """A unit physically HEATING while commanded DRY raises the mismatch.

        Legacy path (§28, hold knob 0) — with the default hold the same
        divergence would be adopted as user intent instead.
        """
        controller = RoomController(
            _dry_cfg(fast_manual_hold_minutes=0.0), name="salon"
        )
        controller.step(_dry_inputs(), dt_seconds=300.0)
        # The first visible feedback (the unit drying) is adopted as the truth
        # (the blind DRY above never reached it) and becomes the reference.
        controller.step(
            _dry_inputs(fast_source_on=True, fast_source_hvac_mode="dry"),
            dt_seconds=300.0,
        )
        out = controller.step(
            _dry_inputs(fast_source_on=True, fast_source_hvac_mode="heat"),
            dt_seconds=300.0,
        )
        assert "fast_source_mismatch" in out.report.flags

    @pytest.mark.unit
    def test_demoted_dry_noted_as_written_is_not_a_divergence(self) -> None:
        """§28: the adapter reports the OFF it REALLY wrote for a demoted DRY.

        Without ``note_fast_source_written`` the unit's OFF feedback would
        disagree with the recorded DRY and, once settled, be adopted as a
        manual OFF — a false hold on a unit that simply cannot dry.
        """
        off = _dry_inputs(fast_source_on=False, fast_source_hvac_mode="off")
        building = BuildingController({"salon": _dry_cfg()})
        out = building.step({"salon": off}, dt_seconds=300.0)
        assert out.rooms["salon"].fast_source.mode is FastSourceMode.DRY
        for _ in range(4):
            # The climate entity has no "dry": the adapter wrote OFF instead.
            building.note_fast_source_written(
                "salon", on=False, mode=FastSourceMode.OFF
            )
            out = building.step({"salon": off}, dt_seconds=300.0)
            room = out.rooms["salon"]
            assert "fast_source_manual" not in room.report.flags
            assert "fast_source_mismatch" not in room.report.flags
            # The core keeps asking for DRY (the adapter keeps flagging).
            assert room.fast_source.mode is FastSourceMode.DRY
        # Unknown rooms are ignored (a write may race a room removal).
        building.note_fast_source_written(
            "nie_ma_takiego_pokoju", on=False, mode=FastSourceMode.OFF
        )

    @pytest.mark.unit
    def test_demoted_dry_room_still_adopts_a_real_touch(self) -> None:
        """§28: the written-back OFF must not block the hold in that room.

        ``note_fast_source_written`` replaces only the pair the feedback is
        compared against; the settle counter follows the EMITTED command,
        which stays DRY every cycle. Were the note treated as a command
        change, the counter would restart every cycle and a unit in a
        ``dry_unsupported`` room could never be adopted when the user turns
        it on by hand.
        """
        off = _dry_inputs(fast_source_on=False, fast_source_hvac_mode="off")
        building = BuildingController({"salon": _dry_cfg()})
        out = building.step({"salon": off}, dt_seconds=300.0)
        assert out.rooms["salon"].fast_source.mode is FastSourceMode.DRY
        for _ in range(3):
            # Steady dry_unsupported loop: emit DRY, adapter wrote OFF,
            # feedback OFF — agreement, no flags of either kind.
            building.note_fast_source_written(
                "salon", on=False, mode=FastSourceMode.OFF
            )
            out = building.step({"salon": off}, dt_seconds=300.0)
            room = out.rooms["salon"]
            assert room.fast_source.mode is FastSourceMode.DRY
            assert "fast_source_manual" not in room.report.flags
            assert "fast_source_mismatch" not in room.report.flags
        building.note_fast_source_written("salon", on=False, mode=FastSourceMode.OFF)
        # The user turns the unit on in cool from the remote: adopted, held.
        cool = _dry_inputs(fast_source_on=True, fast_source_hvac_mode="cool")
        out = building.step({"salon": cool}, dt_seconds=300.0)
        room = out.rooms["salon"]
        assert "fast_source_manual" in room.report.flags
        assert "fast_source_mismatch" not in room.report.flags
        assert room.fast_source.on is True
        assert room.fast_source.mode is FastSourceMode.COOLING

    @pytest.mark.unit
    def test_demoted_dry_without_the_note_becomes_a_false_hold(self) -> None:
        """Contrast: an un-noted demotion IS adopted as a manual OFF (settled)."""
        off = _dry_inputs(fast_source_on=False, fast_source_hvac_mode="off")
        building = BuildingController({"salon": _dry_cfg()})
        out = building.step({"salon": off}, dt_seconds=300.0)
        assert out.rooms["salon"].fast_source.mode is FastSourceMode.DRY
        # First divergence < half a cycle after our own change: flag only.
        out = building.step({"salon": off}, dt_seconds=300.0)
        assert "fast_source_mismatch" in out.rooms["salon"].report.flags
        # Settled: adopted as the user's OFF.
        out = building.step({"salon": off}, dt_seconds=300.0)
        assert "fast_source_manual" in out.rooms["salon"].report.flags
        assert out.rooms["salon"].fast_source.on is False


def _hold_cfg(**overrides: object) -> ControllerConfig:
    """The owner's live tuning (§28): 30/31 min dwells, 60 min manual hold."""
    base: dict[str, object] = {
        "boost_offset_c": 1.5,
        "deadband_c": 0.3,
        "fast_min_on_minutes": 30.0,
        "fast_min_off_minutes": 31.0,
        "fast_manual_hold_minutes": 60.0,
    }
    base.update(overrides)
    return ControllerConfig(**base)  # type: ignore[arg-type]


def _feedback(
    *,
    on: bool | None,
    hvac: str | None,
    kind: FastSourceKind = FastSourceKind.SPLIT,
) -> RoomInputs:
    """TRANSITIONAL room at its setpoint carrying a physical split feedback.

    ``on=None`` models an unavailable climate entity (a blind cycle).
    """
    return make_inputs(
        mode=Mode.TRANSITIONAL,
        setpoint_c=23.0,
        room_temperature_c=23.0,
        fast_source_kind=kind,
        fast_source_on=on,
        fast_source_hvac_mode=hvac,
    )


_HVAC_DIRECTION: dict[str, FastSourceMode] = {
    "heat": FastSourceMode.HEATING,
    "cool": FastSourceMode.COOLING,
    "dry": FastSourceMode.DRY,
}


def _settle(machine: FastSourceMachine, *, on: bool, hvac: str) -> None:
    """Run one agreeing cycle in the controller's order (sync, tick, note).

    The settle rule (§28) adopts a divergence only once the EMITTED command
    pair has been stable for at least half a ``cycle_seconds`` — and the
    counter seen by ``sync`` lags one step behind — so a machine whose
    command just changed needs one more agreeing cycle before a touch is
    adopted.
    """
    machine.sync(_feedback(on=on, hvac=hvac))
    machine.tick(300.0)
    machine.note_command(on, _HVAC_DIRECTION[hvac] if on else FastSourceMode.OFF)


class TestManualHold:
    """§28: a settled physical divergence is adopted under a manual hold."""

    def _settled_off(self, cfg: ControllerConfig | None = None) -> FastSourceMachine:
        """A machine that adopted a stopped unit and has emitted OFF, settled."""
        machine = FastSourceMachine(cfg or _hold_cfg())
        machine.sync(_feedback(on=False, hvac="off"))  # first sync: adopt OFF
        machine.tick(300.0)
        machine.note_command(False, FastSourceMode.OFF)  # pair changed: settle 0
        _settle(machine, on=False, hvac="off")  # one agreeing cycle: settle 300
        return machine

    @pytest.mark.unit
    def test_manual_cool_is_adopted_with_a_hold(self) -> None:
        """(a) Physical ON+cool vs a previous OFF: adopt COOLING, hold, no flag."""
        machine = self._settled_off()
        assert machine.timer_s == pytest.approx(600.0)
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.state is FastSourceMode.COOLING
        assert machine.timer_s == pytest.approx(0.0)
        assert machine.manual_hold_active is True
        assert machine.manual_hold_remaining_s == pytest.approx(3600.0)
        assert machine.mismatch is False

    @pytest.mark.unit
    def test_manual_off_is_adopted_from_heating(self) -> None:
        """(b) Physical OFF while the machine HEATS: adopt OFF, hold."""
        machine = FastSourceMachine(_hold_cfg())
        machine.sync(_feedback(on=True, hvac="heat"))  # first sync: adopt HEATING
        assert machine.state is FastSourceMode.HEATING
        machine.tick(300.0)
        machine.note_command(True, FastSourceMode.HEATING)
        _settle(machine, on=True, hvac="heat")
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.state is FastSourceMode.OFF
        assert machine.timer_s == pytest.approx(0.0)
        assert machine.manual_hold_active is True
        assert machine.mismatch is False

    @pytest.mark.unit
    def test_divergence_right_after_own_command_is_not_adopted(self) -> None:
        """Settle rule: a divergence < half a cycle after OUR change only flags.

        The values deliberately avoid the edges: 2 s must NOT adopt, 299 s
        (a real cycle that came in a hair short of 300) MUST — a full-cycle
        threshold would have slipped that adoption to the next cycle.
        """
        machine = self._settled_off()
        # The controller engages HEATING this cycle (pair changes: settle 0).
        machine.note_command(True, FastSourceMode.HEATING)
        # The debounced recompute 2 s later reads a unit still "off".
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.mismatch is True
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.OFF  # nothing adopted
        machine.tick(2.0)
        machine.note_command(True, FastSourceMode.HEATING)  # unchanged pair
        # A cycle later (settle 2 s at sync, < 150): still only a flag.
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.mismatch is True
        assert machine.manual_hold_active is False
        machine.tick(297.0)
        machine.note_command(True, FastSourceMode.HEATING)
        # Settle 299 s >= half a cycle: the unit REALLY is off — adopt it.
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.mismatch is False
        assert machine.manual_hold_active is True
        assert machine.state is FastSourceMode.OFF

    @pytest.mark.unit
    def test_first_feedback_after_blind_commands_is_the_truth(self) -> None:
        """Commands emitted before any feedback never reached the unit.

        2026-09-10 (§28 note): the adapter writes nothing to an unavailable
        climate entity, so the first reading is not a stale echo of our
        command — it is adopted under the S4 first-sync rule (conservative
        dwell seed), with neither a manual hold nor a mismatch flag, and it
        is agreement from then on.
        """
        machine = FastSourceMachine(_hold_cfg())
        # No feedback yet: the machine engages HEATING, blind.
        cmd = machine.decide(
            want_on=True,
            fs_mode=FastSourceMode.HEATING,
            target_heating=24.0,
            target_cooling=22.0,
            flags=[],
        )
        assert cmd.on is True
        machine.note_command(True, FastSourceMode.HEATING)
        for _ in range(3):
            machine.sync(_feedback(on=None, hvac=None))
            machine.tick(300.0)
            machine.note_command(True, FastSourceMode.HEATING)
        # The climate entity comes up: the unit is physically off.
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.mismatch is False
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.OFF
        assert machine.timer_s == pytest.approx(0.0)
        machine.tick(300.0)
        machine.note_command(False, FastSourceMode.OFF)
        for _ in range(2):
            machine.sync(_feedback(on=False, hvac="off"))
            assert machine.mismatch is False
            assert machine.manual_hold_active is False
            machine.tick(300.0)
            machine.note_command(False, FastSourceMode.OFF)

    @pytest.mark.unit
    def test_first_feedback_overrides_a_blind_running_direction(self) -> None:
        """A blind HEATING engage never flips a physically cooling unit.

        The first visible feedback replaces the machine's blind direction
        with the unit's real one, so the next ``decide`` releases through OFF
        instead of re-emitting HEATING onto a cooling compressor.
        """
        machine = FastSourceMachine(_hold_cfg())
        machine.decide(
            want_on=True,
            fs_mode=FastSourceMode.HEATING,
            target_heating=24.0,
            target_cooling=22.0,
            flags=[],
        )
        machine.note_command(True, FastSourceMode.HEATING)
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.state is FastSourceMode.COOLING
        assert machine.timer_s == pytest.approx(0.0)
        assert machine.mismatch is False
        assert machine.manual_hold_active is False

    @pytest.mark.unit
    def test_ambiguous_first_feedback_keeps_the_blind_direction(self) -> None:
        """A unit reporting "auto" keeps the machine's blind COOLING.

        The global-mode fallback would read TRANSITIONAL as HEATING and send
        ``heat`` to a room the machine engaged to cool; with no single
        direction reported, the machine's own direction is the better guess
        (the dwell is still re-seeded conservatively).
        """
        machine = FastSourceMachine(_hold_cfg())
        machine.decide(
            want_on=True,
            fs_mode=FastSourceMode.COOLING,
            target_heating=24.0,
            target_cooling=22.0,
            flags=[],
        )
        machine.note_command(True, FastSourceMode.COOLING)
        machine.sync(_feedback(on=True, hvac="auto"))
        assert machine.state is FastSourceMode.COOLING
        assert machine.timer_s == pytest.approx(0.0)
        assert machine.mismatch is False
        assert machine.manual_hold_active is False

    @pytest.mark.unit
    def test_unit_found_running_after_a_gap_is_adopted(self) -> None:
        """A feedback gap un-syncs the machine: the unit found is adopted.

        Whatever was emitted while the entity was unavailable (here an OFF)
        may never have reached the unit, so the unit that still cools when it
        comes back is re-adopted under the first-sync rule (conservative
        seed) — no mismatch, no hold. The OFF delivered on that visible cycle
        restarts the settle window, so the unit catching up with it 2 s later
        is not adopted either.
        """
        machine = self._settled_off()
        machine.note_command(True, FastSourceMode.COOLING)
        _settle(machine, on=True, hvac="cool")
        _settle(machine, on=True, hvac="cool")
        for _ in range(2):
            machine.sync(_feedback(on=None, hvac=None))
            machine.tick(300.0)
            machine.note_command(False, FastSourceMode.OFF)
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.state is FastSourceMode.COOLING
        assert machine.timer_s == pytest.approx(0.0)
        assert machine.mismatch is False
        assert machine.manual_hold_active is False
        machine.tick(300.0)
        machine.note_command(False, FastSourceMode.OFF)  # now really written
        machine.sync(_feedback(on=True, hvac="cool"))  # the 2-s recompute
        assert machine.mismatch is True
        assert machine.manual_hold_active is False

    @pytest.mark.unit
    def test_blind_force_keeps_the_manual_hold(self) -> None:
        """A safety force the unit cannot see does not end a manual hold.

        The unit found after the gap is re-adopted and mirrored for the rest
        of the hold.
        """
        machine = self._settled_off()
        machine.sync(_feedback(on=True, hvac="cool"))  # adopted, hold armed
        assert machine.manual_hold_active is True
        machine.tick(300.0)
        machine.note_command(True, FastSourceMode.COOLING)
        machine.sync(_feedback(on=None, hvac=None))
        machine.force_off()
        assert machine.manual_hold_active is True
        assert machine.mismatch is False
        machine.tick(300.0)
        machine.note_command(False, FastSourceMode.OFF)
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.state is FastSourceMode.COOLING
        assert machine.manual_hold_active is True
        assert machine.mismatch is False

    @pytest.mark.unit
    def test_state_found_after_a_gap_is_adopted_without_a_hold(self) -> None:
        """A unit found OFF after a gap is adopted OFF — without a hold.

        The machine cannot tell a remote OFF during the gap from a power cut
        or a module reboot; it re-adopts the unit like after a restart (a full
        min-OFF from now) instead of holding it.
        """
        machine = self._settled_off()
        machine.note_command(True, FastSourceMode.COOLING)
        _settle(machine, on=True, hvac="cool")
        _settle(machine, on=True, hvac="cool")
        for _ in range(2):
            machine.sync(_feedback(on=None, hvac=None))
            machine.tick(300.0)
            machine.note_command(True, FastSourceMode.COOLING)
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.state is FastSourceMode.OFF
        assert machine.timer_s == pytest.approx(0.0)
        assert machine.manual_hold_active is False
        assert machine.mismatch is False

    @pytest.mark.unit
    def test_hold_expires_in_tick_and_decide_resumes(self) -> None:
        """(c) The hold counts down once per tick; then decide() runs again."""
        machine = self._settled_off()
        machine.sync(_feedback(on=True, hvac="cool"))
        for i in range(12):
            machine.tick(300.0)
            expected = 3600.0 - 300.0 * (i + 1)
            assert machine.manual_hold_remaining_s == pytest.approx(expected)
            assert machine.manual_hold_active is (i < 11)
        # Normal logic resumes from the ADOPTED state: the room is satisfied,
        # the min-ON accumulated during the hold (60 min > 30), so the unit
        # releases to OFF — never straight to the other direction.
        cmd = machine.decide(
            want_on=False,
            fs_mode=FastSourceMode.COOLING,
            target_heating=23.0,
            target_cooling=23.0,
            flags=[],
        )
        assert cmd.on is False
        assert machine.state is FastSourceMode.OFF

    @pytest.mark.unit
    def test_second_divergence_restarts_the_hold(self) -> None:
        """(d) A new touch during the hold restarts it from the LAST touch."""
        machine = self._settled_off()
        machine.sync(_feedback(on=True, hvac="cool"))
        machine.note_command(True, FastSourceMode.COOLING)
        for _ in range(6):
            machine.tick(300.0)
        assert machine.manual_hold_remaining_s == pytest.approx(1800.0)
        # The user now switches the unit OFF.
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.state is FastSourceMode.OFF
        assert machine.manual_hold_remaining_s == pytest.approx(3600.0)
        assert machine.mismatch is False

    @pytest.mark.unit
    def test_knob_zero_keeps_the_legacy_mismatch(self) -> None:
        """(e) Hold disabled: the flag is raised and the state NOT adopted."""
        machine = self._settled_off(_hold_cfg(fast_manual_hold_minutes=0.0))
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.state is FastSourceMode.OFF
        assert machine.mismatch is True
        assert machine.manual_hold_active is False

    @pytest.mark.unit
    def test_force_off_and_force_on_end_the_hold(self) -> None:
        """(f) The safety forces outrank the hold and clear it."""
        machine = self._settled_off()
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.manual_hold_active is True
        off = machine.force_off()
        assert off.on is False
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.OFF
        # A fresh (settled) divergence re-arms it; force_on clears it again.
        machine.note_command(False, FastSourceMode.OFF)
        _settle(machine, on=False, hvac="off")
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.manual_hold_active is True
        on = machine.force_on(FastSourceMode.HEATING, 24.0)
        assert on.mode is FastSourceMode.HEATING
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.HEATING

    @pytest.mark.unit
    def test_force_cancelling_an_active_hold_leaves_a_mismatch_trace(self) -> None:
        """(f') A safety force that ends a hold raises the mismatch flag.

        The unit is physically in the USER's state while the safety layer is
        about to write something else: that divergence must show in the
        report (``fast_source_mismatch``), not vanish silently. A force with
        NO active hold leaves the flag alone.
        """
        machine = self._settled_off()
        machine.sync(_feedback(on=True, hvac="cool"))  # adopted ON, hold armed
        assert machine.manual_hold_active is True
        assert machine.mismatch is False
        machine.force_off()
        assert machine.mismatch is True
        assert machine.manual_hold_active is False
        assert machine.state is FastSourceMode.OFF
        # The next sync clears the trace like any other cycle flag; a force
        # without a hold does not re-raise it.
        machine.note_command(False, FastSourceMode.OFF)
        machine.sync(_feedback(on=False, hvac="off"))
        assert machine.mismatch is False
        machine.force_off()
        assert machine.mismatch is False
        # force_on on an active hold traces too.
        _settle(machine, on=False, hvac="off")
        machine.sync(_feedback(on=True, hvac="cool"))
        assert machine.manual_hold_active is True
        machine.force_on(FastSourceMode.HEATING, 24.0)
        assert machine.mismatch is True
        assert machine.manual_hold_active is False

    @pytest.mark.unit
    def test_reset_clears_the_hold(self) -> None:
        """(g) reset() drops the hold with the rest of the machine state."""
        machine = self._settled_off()
        machine.sync(_feedback(on=True, hvac="cool"))
        machine.reset()
        assert machine.manual_hold_active is False
        assert machine.manual_hold_remaining_s == pytest.approx(0.0)
        assert machine.state is FastSourceMode.OFF

    @pytest.mark.unit
    def test_mirror_echoes_the_adopted_state(self) -> None:
        """mirror() emits on/mode from the state and NO target (user's own)."""
        machine = self._settled_off()
        machine.sync(_feedback(on=True, hvac="cool"))
        cmd = machine.mirror()
        assert cmd.on is True
        assert cmd.mode is FastSourceMode.COOLING
        # The unit runs at the USER's setpoint, unknown to us: no fabricated
        # target for the panel/sensors (nothing is written during a hold).
        assert cmd.target_temperature_c is None
        # The min-ON lock that still applies after the hold is surfaced.
        assert machine.dwell_remaining_s == pytest.approx(30.0 * 60.0)
        machine.note_command(True, FastSourceMode.COOLING)
        _settle(machine, on=True, hvac="cool")
        machine.sync(_feedback(on=False, hvac="off"))
        off = machine.mirror()
        assert off.on is False
        assert off.mode is FastSourceMode.OFF
        assert off.target_temperature_c is None

    @pytest.mark.unit
    def test_heater_is_adopted_as_heating_whatever_the_feedback(self) -> None:
        """A HEATER kind can never be adopted as COOLING (first-sync rule)."""
        machine = FastSourceMachine(_hold_cfg())
        heater_off = _feedback(on=False, hvac="off", kind=FastSourceKind.HEATER)
        machine.sync(heater_off)
        machine.tick(300.0)
        machine.note_command(False, FastSourceMode.OFF)
        machine.sync(heater_off)
        machine.tick(300.0)
        machine.note_command(False, FastSourceMode.OFF)
        machine.sync(_feedback(on=True, hvac="cool", kind=FastSourceKind.HEATER))
        assert machine.state is FastSourceMode.HEATING
        assert machine.manual_hold_active is True

    @pytest.mark.unit
    def test_negative_hold_rejected(self) -> None:
        """The knob must be >= 0 (0 disables)."""
        with pytest.raises(ValueError, match="fast_manual_hold_minutes"):
            ControllerConfig(fast_manual_hold_minutes=-1.0)
        assert (
            ControllerConfig(fast_manual_hold_minutes=0.0).fast_manual_hold_minutes
            == 0.0
        )


class TestBlindRestart:
    """2026-09-10 (§28 note): a restart during our own boost is not a touch.

    Replays the owner's recorder case on the full room controller: the split
    runs a cooling boost the controller itself started; Home Assistant
    restarts; on the rebuilt controller's first cycle the climate entity AND
    the room sensor (the split's own probe) are still unavailable, so the
    room is sensor-lost and force-stops the split — blind. The next cycle
    finds the unit still cooling; the owner then raises the setpoint and the
    debounced recompute runs 3 s later.
    """

    def _room(
        self,
        *,
        temp: float | None,
        setpoint: float,
        on: bool | None,
        hvac: str | None,
    ) -> RoomInputs:
        return make_inputs(
            mode=Mode.TRANSITIONAL,
            setpoint_c=setpoint,
            room_temperature_c=temp,
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_on=on,
            fast_source_hvac_mode=hvac,
        )

    @pytest.mark.unit
    def test_own_boost_across_a_restart_is_not_a_manual_touch(self) -> None:
        """No hold and no mismatch: the running unit is re-owned, not held."""
        controller = RoomController(_hold_cfg(fast_target_offset_k=0.0), name="gabinet")
        lost = controller.step(
            self._room(temp=None, setpoint=21.0, on=None, hvac=None),
            dt_seconds=300.0,
        )
        assert "sensor_lost" in lost.report.flags
        assert lost.fast_source.on is False
        back = controller.step(
            self._room(temp=21.5, setpoint=21.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        assert "fast_source_manual" not in back.report.flags
        assert "fast_source_mismatch" not in back.report.flags
        # Re-owned as the controller's own boost: still needed (0.5 K above
        # the setpoint is outside the 0.3 K band), so it keeps cooling.
        assert back.fast_source.on is True
        assert back.fast_source.mode is FastSourceMode.COOLING
        assert back.fast_source.target_temperature_c == pytest.approx(21.0)
        recompute = controller.step(
            self._room(temp=21.5, setpoint=22.0, on=True, hvac="cool"),
            dt_seconds=3.0,
        )
        assert "fast_source_manual" not in recompute.report.flags
        assert "fast_source_mismatch" not in recompute.report.flags
        # Satisfied now, but the conservative min-ON seeded at the re-adoption
        # keeps the compressor running; the new target follows the setpoint.
        assert recompute.fast_source.on is True
        assert recompute.fast_source.target_temperature_c == pytest.approx(22.0)
        assert "fast_source_min_runtime" in recompute.report.flags

    @pytest.mark.unit
    def test_manual_off_after_the_restart_is_still_a_touch(self) -> None:
        """The owner's later remote OFF is adopted exactly as before §28 note."""
        controller = RoomController(_hold_cfg(fast_target_offset_k=0.0), name="gabinet")
        controller.step(
            self._room(temp=None, setpoint=21.0, on=None, hvac=None),
            dt_seconds=300.0,
        )
        controller.step(
            self._room(temp=21.5, setpoint=21.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        controller.step(
            self._room(temp=21.5, setpoint=21.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        off = controller.step(
            self._room(temp=21.5, setpoint=21.0, on=False, hvac="off"),
            dt_seconds=300.0,
        )
        assert "fast_source_manual" in off.report.flags
        assert off.fast_source.on is False

    @pytest.mark.unit
    def test_manual_hold_survives_a_blind_sensor_lost_blip(self) -> None:
        """A feedback blip must not end the user's manual cooling.

        The room sensor is the split's own probe, so one Wi-Fi blip reads as
        sensor lost AND an unavailable unit. The sensor-lost force-off cannot
        be delivered; were it to end the hold and flip the machine to OFF,
        the next visible cycle (unit still cooling = agreement with the last
        mirror) would WRITE that OFF over the user's choice.
        """
        controller = RoomController(_hold_cfg(), name="gabinet")
        idle = self._room(temp=23.0, setpoint=23.0, on=False, hvac="off")
        controller.step(idle, dt_seconds=300.0)
        controller.step(idle, dt_seconds=300.0)
        touched = controller.step(
            self._room(temp=23.0, setpoint=23.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        assert "fast_source_manual" in touched.report.flags
        blip = controller.step(
            self._room(temp=None, setpoint=23.0, on=None, hvac=None),
            dt_seconds=300.0,
        )
        assert "sensor_lost" in blip.report.flags
        assert "fast_source_manual" in blip.report.flags
        back = controller.step(
            self._room(temp=22.5, setpoint=23.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        assert "fast_source_manual" in back.report.flags
        assert "fast_source_mismatch" not in back.report.flags
        assert back.fast_source.on is True
        assert back.fast_source.mode is FastSourceMode.COOLING

    @pytest.mark.unit
    def test_own_boost_survives_a_blind_sensor_lost_blip(self) -> None:
        """Our own running boost carries on after a feedback blip.

        The blind force-off never reaches the unit, so the machine keeps its
        COOLING state and dwell clock; the next visible cycle agrees with the
        last delivered command and the boost continues instead of being cut.
        """
        controller = RoomController(_hold_cfg(fast_min_off_minutes=5.0), name="gabinet")
        hot_off = self._room(temp=25.0, setpoint=23.0, on=False, hvac="off")
        controller.step(hot_off, dt_seconds=300.0)  # first sync: min-OFF seed
        engaged = controller.step(hot_off, dt_seconds=300.0)
        assert engaged.fast_source.on is True
        assert engaged.fast_source.mode is FastSourceMode.COOLING
        controller.step(
            self._room(temp=25.0, setpoint=23.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        controller.step(
            self._room(temp=None, setpoint=23.0, on=None, hvac=None),
            dt_seconds=300.0,
        )
        # 1 K over: inside the engage threshold (1.5 K) but outside the release
        # band (0.3 K) — only a machine that is STILL engaged keeps cooling.
        back = controller.step(
            self._room(temp=24.0, setpoint=23.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        assert back.fast_source.on is True
        assert back.fast_source.mode is FastSourceMode.COOLING
        assert "fast_source_manual" not in back.report.flags
        assert "fast_source_mismatch" not in back.report.flags

    @pytest.mark.unit
    def test_min_on_counts_from_the_real_start_after_a_blind_stretch(self) -> None:
        """Min-ON counts from the real start after an unreachable stretch.

        A room with its own sensor wants a boost while the split entity is
        unavailable for 35 min. The blind engage never reached the unit; had
        its dwell clock been kept, min-ON (30 min) would be spent before the
        unit ever started and the unit released minutes after the real start.
        The gap re-syncs the machine to the unit it finds (off), so the engage
        written on the first visible cycle starts min-ON from there.
        """
        controller = RoomController(_hold_cfg(fast_min_off_minutes=5.0), name="gabinet")
        idle = self._room(temp=23.0, setpoint=23.0, on=False, hvac="off")
        controller.step(idle, dt_seconds=300.0)
        controller.step(idle, dt_seconds=300.0)
        for _ in range(7):
            controller.step(
                self._room(temp=25.0, setpoint=23.0, on=None, hvac=None),
                dt_seconds=300.0,
            )
        start = controller.step(
            self._room(temp=25.0, setpoint=23.0, on=False, hvac="off"),
            dt_seconds=300.0,
        )
        assert start.fast_source.on is True
        assert start.fast_source.mode is FastSourceMode.COOLING
        # 5 min after the real start the room is past the far edge of the
        # band (release wanted): min-ON, counted from the real start, holds.
        held = controller.step(
            self._room(temp=22.5, setpoint=23.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        assert held.fast_source.on is True
        assert "fast_source_min_runtime" in held.report.flags
        assert held.report.fast_dwell_remaining_s == pytest.approx(1500.0)

    @pytest.mark.unit
    def test_farewell_after_a_blind_cycle_is_no_manual_touch(self) -> None:
        """A farewell right after a feedback gap never becomes a false hold.

        The room leaves live right after a blind cycle; the split is back and
        off. The gap re-synced the machine, so the unit found is adopted as
        the truth — no manual hold — and re-engaging waits the full min-OFF.
        """
        controller = RoomController(_hold_cfg(fast_min_off_minutes=5.0), name="gabinet")
        hot_off = self._room(temp=25.0, setpoint=23.0, on=False, hvac="off")
        controller.step(hot_off, dt_seconds=300.0)
        controller.step(hot_off, dt_seconds=300.0)
        controller.step(
            self._room(temp=25.0, setpoint=23.0, on=True, hvac="cool"),
            dt_seconds=300.0,
        )
        controller.step(
            self._room(temp=25.0, setpoint=23.0, on=None, hvac=None),
            dt_seconds=300.0,
        )
        controller.notify_fast_source_farewell()
        back = controller.step(hot_off, dt_seconds=5.0)
        assert back.fast_source.on is False
        assert "fast_source_manual" not in back.report.flags
        assert "fast_source_mismatch" not in back.report.flags
        assert "fast_source_min_runtime" in back.report.flags
