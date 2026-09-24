"""Unit tests of ``RoomController`` behaviours the other suites leave unpinned.

Mutation testing (``scripts/mutation.py``) showed these documented behaviours
of :class:`~tortoise_ufh.controller.RoomController` had no test that would
notice a change:

* ``step`` input validation (``dt_seconds > 0``) and the PID integrating on the
  REAL elapsed ``dt_seconds`` of the cycle.
* The bounded outdoor feedforward (gain, neutral point, mode-dependent
  deviation, clamp) and its additive effect on the valve.
* The heating valve-floor boundary (lift only when strictly below the floor).
* Clamp / saturation boundary semantics (control-F8: an S2-throttle zero is
  throttling, not PI saturation) and the boost-hold snapshot clamp.
* Polish explanation strings on the active / transitional / safe-degrade
  paths (they are a user-facing contract) and the safety banner cap of 200
  characters.
* Integrator hygiene: mode-change reset, inactivity decay clock, bumpless
  setpoint re-seed boundary.
* The actuation self-test gates (begin preconditions, dew abort, safety
  abort, watchdog pause) and the fast-source reconciliation hooks
  (farewell, group-conflict resolution).
* Dry-assist hysteresis (separate temperature / humidity states) and the
  governing-supply selection feeding the S1/S2 safety proxies.

Units: temperatures / setpoints / dew points in degC; valve in percent
(0..100); trend in K/h; ``dt_seconds`` in seconds. This module never imports
``homeassistant``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    BuildingController,
    RoomController,
)
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.models import (
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
)
from tests.unit.conftest import make_inputs

pytestmark = pytest.mark.unit

_DT = 300.0
_SAFETY_EXPLANATION_MAX_LEN = 200  # documented cap in controller.py


class TestStepDtContract:
    """``step`` validates ``dt_seconds`` and the PID integrates the real dt."""

    def test_dt_must_be_positive(self) -> None:
        """A zero or negative dt raises ValueError naming the constraint.

        OFF mode pins this: the passive path never reaches the PID's own
        dt guard, so the raise can only come from ``step`` itself.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        for bad in (0.0, -1.0):
            with pytest.raises(ValueError, match="dt_seconds must be > 0"):
                controller.step(make_inputs(mode=Mode.OFF), dt_seconds=bad)

    def test_sub_second_dt_is_accepted(self) -> None:
        """The bound is strict: any dt > 0 (e.g. 0.5 s) is a valid cycle."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(mode=Mode.OFF, room_temperature_c=20.0), dt_seconds=0.5
        )
        assert out.valve_position_pct == pytest.approx(0.0)

    def test_integral_uses_real_elapsed_dt(self) -> None:
        """A 600 s cycle banks twice the integral of a 300 s cycle.

        The PI compute receives the cycle's real ``dt_seconds`` (not the
        PID's nominal ``cycle_seconds``): ``I += ki * error_db * dt``.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=19.0),
            dt_seconds=600.0,
        )
        # error_db = 2.0 - 0.3 = 1.7 K; I = ki * error_db * dt.
        assert out.report.i_term == pytest.approx(0.0015 * 1.7 * 600.0)

    def test_default_dt_is_one_nominal_cycle(self) -> None:
        """Omitting ``dt_seconds`` integrates exactly one 300 s cycle."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(make_inputs(setpoint_c=21.0, room_temperature_c=19.0))
        assert out.report.i_term == pytest.approx(0.0015 * 1.7 * 300.0)


class TestFeedforward:
    """The bounded outdoor feedforward (control-F6 shaping constants)."""

    def _cfg(self, **kw: float | bool) -> ControllerConfig:
        """Feedforward-enabled config with the integrator/trend neutralised."""
        return ControllerConfig(
            ki=0.0,
            kt=0.0,
            outdoor_ff_enabled=True,
            ff_neutral_c=15.0,
            ff_gain_pct_per_k=1.5,
            ff_max_pct=20.0,
            **kw,  # type: ignore[arg-type]
        )

    def test_heating_feedforward_adds_gain_times_deviation(self) -> None:
        """Heating: ``ff = gain * (ff_neutral_c - t_out)``, added to the valve."""
        controller = RoomController(self._cfg(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                outdoor_temperature_c=5.0,
            ),
            dt_seconds=_DT,
        )
        # deviation = 15 - 5 = 10 K -> ff = 1.5 * 10 = 15 % (uncapped).
        assert out.report.feedforward_term == pytest.approx(15.0)
        # valve = kp * error_db + ff = 14 * 1.7 + 15.
        assert out.valve_position_pct == pytest.approx(14.0 * 1.7 + 15.0)

    def test_heating_feedforward_zero_deviation_is_not_floored(self) -> None:
        """A sub-1 K deviation contributes its exact fraction, no 1 K floor."""
        controller = RoomController(self._cfg(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                outdoor_temperature_c=14.5,
            ),
            dt_seconds=_DT,
        )
        assert out.report.feedforward_term == pytest.approx(1.5 * 0.5)

    def test_cooling_feedforward_uses_hot_deviation(self) -> None:
        """Cooling: ``ff = gain * (t_out - ff_neutral_c)`` (hot outside)."""
        controller = RoomController(self._cfg(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=26.0,
                humidity_pct=50.0,
                outdoor_temperature_c=25.0,
                loops=(LoopInput(None, 20.0, None),),
            ),
            dt_seconds=_DT,
        )
        assert out.report.feedforward_term == pytest.approx(15.0)

    def test_cooling_feedforward_small_deviation_is_not_floored(self) -> None:
        """Cooling deviation below 1 K contributes its exact fraction."""
        controller = RoomController(self._cfg(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=26.0,
                humidity_pct=50.0,
                outdoor_temperature_c=15.5,
                loops=(LoopInput(None, 20.0, None),),
            ),
            dt_seconds=_DT,
        )
        assert out.report.feedforward_term == pytest.approx(1.5 * 0.5)

    def test_feedforward_capped_at_max(self) -> None:
        """The term never exceeds ``ff_max_pct`` however cold it is outside."""
        controller = RoomController(self._cfg(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                outdoor_temperature_c=-10.0,
            ),
            dt_seconds=_DT,
        )
        assert out.report.feedforward_term == pytest.approx(20.0)

    def test_feedforward_off_when_disabled_or_outdoor_missing(self) -> None:
        """No outdoor temperature (or the knob off) means a zero term."""
        disabled = RoomController(
            ControllerConfig(ki=0.0, kt=0.0, outdoor_ff_enabled=False), name="a"
        )
        out = disabled.step(
            make_inputs(room_temperature_c=19.0, outdoor_temperature_c=5.0),
            dt_seconds=_DT,
        )
        assert out.report.feedforward_term == pytest.approx(0.0)

        missing = RoomController(self._cfg(), name="b")
        out = missing.step(
            make_inputs(room_temperature_c=19.0, outdoor_temperature_c=None),
            dt_seconds=_DT,
        )
        assert out.report.feedforward_term == pytest.approx(0.0)


class TestHeatingValveFloorBoundary:
    """The heating floor lifts the valve only when STRICTLY below it."""

    def _controller(self) -> RoomController:
        """A controller whose P term lands exactly on the floor at 1 K error."""
        return RoomController(
            ControllerConfig(
                kp=14.0,
                ki=0.0,
                kt=0.0,
                deadband_c=0.0,
                valve_floor_pct=14.0,
            ),
            name="salon",
        )

    def test_valve_exactly_at_floor_is_not_lifted(self) -> None:
        """``valve == floor`` is not "below the floor": no floor application."""
        out = self._controller().step(
            make_inputs(setpoint_c=21.0, room_temperature_c=20.0),
            dt_seconds=_DT,
        )
        assert out.report.valve_floor_applied is False
        assert out.valve_position_pct == pytest.approx(14.0)

    def test_valve_just_below_floor_is_lifted(self) -> None:
        """``valve < floor`` (by any margin) is lifted to the floor."""
        out = self._controller().step(
            make_inputs(setpoint_c=21.0, room_temperature_c=20.01),
            dt_seconds=_DT,
        )
        assert out.report.valve_floor_applied is True
        assert out.valve_position_pct == pytest.approx(14.0)


class TestSaturationBoundaries:
    """Clamp and saturation edge semantics (control-F8 included)."""

    def test_valve_clamps_at_exactly_100_and_saturates(self) -> None:
        """A huge heating demand emits exactly 100 % and reports saturation."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=8.0),
            dt_seconds=_DT,
        )
        assert out.valve_position_pct == pytest.approx(100.0)
        assert out.report.saturated is True

    def test_feedforward_beyond_saturation_still_clamps_at_100(self) -> None:
        """PID at 100 plus a positive feedforward still emits exactly 100 %.

        The PID clamps its own output to [0, 100]; only the additive
        feedforward can push the sum above 100, and step 13 clamps it back.
        """
        cfg = ControllerConfig(
            outdoor_ff_enabled=True,
            ff_neutral_c=15.0,
            ff_gain_pct_per_k=1.5,
            ff_max_pct=20.0,
        )
        controller = RoomController(cfg, name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=8.0,
                outdoor_temperature_c=5.0,
            ),
            dt_seconds=_DT,
        )
        assert out.report.feedforward_term > 0.0
        assert out.valve_position_pct == pytest.approx(100.0)

    def test_zero_valve_inside_deadband_is_saturated(self) -> None:
        """A PI output of exactly 0 (no throttle) is a saturated-low signal."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=20.9),
            dt_seconds=_DT,
        )
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.report.dew_throttle_factor == pytest.approx(1.0)
        assert out.report.saturated is True

    def test_small_positive_valve_is_not_saturated(self) -> None:
        """A 0.5 % valve sits strictly inside the bounds: not saturated."""
        controller = RoomController(
            ControllerConfig(
                kp=1.0, ki=0.0, kt=0.0, deadband_c=0.0, valve_floor_pct=0.0
            ),
            name="salon",
        )
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=20.5),
            dt_seconds=_DT,
        )
        assert out.valve_position_pct == pytest.approx(0.5)
        assert out.report.saturated is False

    def test_cooling_throttle_to_zero_from_zero_valve_is_saturated(self) -> None:
        """A valve already 0 BEFORE the S2 throttle is genuinely saturated-low.

        ``low_before_throttle`` (the pre-throttle valve was at the floor)
        distinguishes this from control-F8's throttle-produced zero.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        t_dew = dew_point(24.1, 60.0)
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=24.1,  # inside the deadband -> valve 0
                humidity_pct=60.0,
                loops=(LoopInput(None, t_dew, None),),  # supply AT dew -> 0
            ),
            dt_seconds=_DT,
        )
        assert out.report.dew_throttle_factor == pytest.approx(0.0)
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.report.saturated is True

    def test_cooling_full_throttle_of_small_valve_is_not_saturated(self) -> None:
        """A 0.5 % pre-throttle valve zeroed by the S2 throttle: control-F8.

        The pre-throttle valve was strictly positive (the control law wanted
        cooling), so the zero is purely the throttle's doing — reported via
        ``dew_throttle_factor``, NOT as PI saturation.
        """
        controller = RoomController(
            ControllerConfig(kp=1.0, ki=0.0, kt=0.0, deadband_c=0.0),
            name="salon",
        )
        t_dew = dew_point(24.5, 55.0)
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=24.5,  # error 0.5 K -> valve 0.5 %
                humidity_pct=55.0,
                loops=(LoopInput(None, t_dew, None),),  # supply AT dew -> 0
            ),
            dt_seconds=_DT,
        )
        assert out.report.dew_throttle_factor == pytest.approx(0.0)
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.report.saturated is False


class TestBoostHoldSnapshotClamp:
    """The cooling boost-hold snapshots the raw valve clamped to [0, 100]."""

    def _cool(
        self, room: float, *, humidity: float = 50.0, allowed: bool = True
    ) -> RoomInputs:
        """Cooling inputs (setpoint 24, supply 20) with a split."""
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=room,
            humidity_pct=humidity,
            loops=(LoopInput(None, 20.0, None),),
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_allowed=allowed,
        )

    def test_raw_valve_clamped_at_100_on_clamp_cycle(self) -> None:
        """An overwhelming cooling demand still emits exactly 100 %."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._cool(24.2), dt_seconds=_DT)
        # RH 35 keeps the dew point (~14.6) far below the 20 degC supply.
        out = controller.step(self._cool(32.0, humidity=35.0), dt_seconds=_DT)
        assert out.fast_source.on is True
        assert out.valve_position_pct == pytest.approx(100.0)

    def test_hold_snapshot_capped_at_100(self) -> None:
        """The engage-cycle raw valve is snapshotted clamped: hold <= 100 %."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._cool(24.2), dt_seconds=_DT)
        engaged = controller.step(self._cool(32.0, humidity=35.0), dt_seconds=_DT)
        assert engaged.fast_source.on is True
        # Room back at the setpoint: the PI collapses far below the hold,
        # the min-ON keeps the split engaged, so the hold floors the valve.
        held = controller.step(self._cool(24.0), dt_seconds=_DT)
        assert held.valve_position_pct == pytest.approx(100.0)

    def test_hold_snapshot_floored_at_zero(self) -> None:
        """A raw valve below 0 on the engage cycle snapshots as 0, not more."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Quiet hours block the engage while the room is hot; the raw valve
        # of the (later) engage cycle is driven negative by trend damping as
        # the room falls, so the snapshot clamps to exactly 0.
        controller.step(self._cool(28.0, allowed=False), dt_seconds=_DT)
        engaged = controller.step(self._cool(25.5), dt_seconds=_DT)
        assert engaged.fast_source.on is True
        held = controller.step(self._cool(24.0), dt_seconds=_DT)
        assert held.valve_position_pct == pytest.approx(0.0)


class TestActiveExplanation:
    """The Polish explanation of the active path is a user-facing contract."""

    def test_heating_boost_explanation(self) -> None:
        """Heating with an engaged split: "Grzanie, blad ...", "Split ON (boost)."."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=_DT,
        )
        assert out.fast_source.on is True
        assert out.report.explanation.startswith("Grzanie, blad +2.0 K")
        assert "Split ON (boost)." in out.report.explanation

    def test_cooling_explanation_with_idle_split(self) -> None:
        """Cooling with an idle split: "Chlodzenie, ...", " Split OFF."."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=24.1,
                humidity_pct=50.0,
                loops=(LoopInput(None, 20.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=_DT,
        )
        assert out.fast_source.on is False
        assert out.report.explanation.startswith("Chlodzenie, blad -0.1 K")
        assert out.report.explanation.endswith(" Split OFF.")

    def test_no_fast_source_leaves_no_split_text(self) -> None:
        """A floor-only room's explanation carries no Split fragment at all."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=19.0),
            dt_seconds=_DT,
        )
        assert "Split" not in out.report.explanation
        assert "None" not in out.report.explanation
        valve_pct = out.valve_position_pct
        assert out.report.explanation.endswith(f"Zawor {valve_pct:.0f}%.")

    def test_manual_hold_on_explanation(self) -> None:
        """A held-ON unit is named: "reczne sterowanie ... , ON."."""
        controller = RoomController(ControllerConfig(), name="salon")
        for _ in range(3):
            controller.step(
                make_inputs(
                    setpoint_c=21.0,
                    room_temperature_c=21.0,
                    fast_source_kind=FastSourceKind.SPLIT,
                    fast_source_on=False,
                ),
                dt_seconds=_DT,
            )
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=21.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=True,
                fast_source_hvac_mode="heat",
            ),
            dt_seconds=_DT,
        )
        assert "fast_source_manual" in out.report.flags
        assert "reczne sterowanie (jeszcze" in out.report.explanation
        assert out.report.explanation.endswith(", ON.")

    def test_manual_hold_off_explanation(self) -> None:
        """A held-OFF unit is named: "reczne sterowanie ... , OFF."."""
        controller = RoomController(ControllerConfig(), name="salon")
        for _ in range(3):
            controller.step(
                make_inputs(
                    setpoint_c=21.0,
                    room_temperature_c=19.0,
                    fast_source_kind=FastSourceKind.SPLIT,
                    fast_source_on=True,
                    fast_source_hvac_mode="heat",
                ),
                dt_seconds=_DT,
            )
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=False,
            ),
            dt_seconds=_DT,
        )
        assert "fast_source_manual" in out.report.flags
        assert "reczne sterowanie (jeszcze" in out.report.explanation
        assert out.report.explanation.endswith(", OFF.")


class TestTransitionalResult:
    """The TRANSITIONAL path: parked valve, bidirectional split, direction text."""

    def _inputs(
        self,
        room: float | None,
        *,
        kind: FastSourceKind = FastSourceKind.SPLIT,
        allowed: bool = True,
    ) -> RoomInputs:
        """TRANSITIONAL inputs at setpoint 21 with the given room temperature."""
        return make_inputs(
            mode=Mode.TRANSITIONAL,
            setpoint_c=21.0,
            room_temperature_c=room,
            humidity_pct=50.0,
            fast_source_kind=kind,
            fast_source_allowed=allowed,
        )

    @pytest.mark.parametrize(
        ("engage_room_c", "edge_room_c"),
        [(19.0, 21.25), (23.0, 20.75)],
        ids=["heating", "cooling"],
    )
    def test_running_split_releases_exactly_at_the_far_band_edge(
        self, engage_room_c: float, edge_room_c: float
    ) -> None:
        """An engaged split releases when the room reaches the FAR band edge.

        Setpoint 21, deadband 0.25 (exact in binary): a heater engaged at
        19 degC is released at 21.25 (heating demand == -deadband), a cooler
        engaged at 23 degC at 20.75. The release is strict (``demand >
        -deadband`` keeps it), so the edge itself already turns it OFF once
        min-ON has elapsed.
        """
        cfg = ControllerConfig(
            deadband_c=0.25,
            boost_offset_c=1.0,
            fast_min_on_minutes=5.0,
            fast_min_off_minutes=5.0,
        )
        controller = RoomController(cfg, name="salon")
        engaged = controller.step(self._inputs(engage_room_c), dt_seconds=_DT)
        assert engaged.fast_source.on is True
        released = controller.step(self._inputs(edge_room_c), dt_seconds=_DT)
        assert released.fast_source.on is False

    def test_no_fast_source_parks_and_reports_brak(self) -> None:
        """Without a fast source: valve 0, split OFF, direction "brak"."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            self._inputs(19.0, kind=FastSourceKind.NONE), dt_seconds=_DT
        )
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.fast_source.on is False
        assert "Split brak, OFF." in out.report.explanation

    def test_idle_in_band_reports_brak_and_echoes_report_fields(self) -> None:
        """An idle split inside the band: "brak", parked valve, full report."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._inputs(20.9), dt_seconds=_DT)
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.fast_source.on is False
        assert "Split brak, OFF." in out.report.explanation
        assert out.report.saturated is False
        assert out.report.error_c == pytest.approx(0.1)
        assert out.report.trend_c_per_h == pytest.approx(0.0)
        assert out.report.room_dew_point_c == pytest.approx(dew_point(20.9, 50.0))

    def test_heating_demand_engages_and_names_grzanie(self) -> None:
        """Demand beyond the boost offset engages the split: "grzanie, ON"."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._inputs(19.0), dt_seconds=_DT)
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING
        assert "Split grzanie, ON." in out.report.explanation

    def test_demand_exactly_at_boost_offset_does_not_engage(self) -> None:
        """The engage threshold is strict: error == boost_offset stays OFF."""
        controller = RoomController(ControllerConfig(), name="salon")
        heating = controller.step(self._inputs(20.0), dt_seconds=_DT)  # error 1.0
        assert heating.fast_source.on is False
        cooling = controller.step(self._inputs(22.0), dt_seconds=_DT)  # demand 1.0
        assert cooling.fast_source.on is False

    def test_engaged_heater_keeps_direction_inside_band(self) -> None:
        """A running heater holds "grzanie" until the FAR band edge is crossed."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0), dt_seconds=_DT)
        out = controller.step(self._inputs(20.5), dt_seconds=_DT)  # error 0.5
        assert out.fast_source.on is True
        assert "Split grzanie, ON." in out.report.explanation
        assert "fast_source_min_runtime" not in out.report.flags

    def test_engaged_heater_at_far_edge_requests_release(self) -> None:
        """At error == -deadband the heater asks to stop (min-ON may block)."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0), dt_seconds=_DT)
        out = controller.step(self._inputs(21.3), dt_seconds=_DT)  # error -0.3
        # Release requested but blocked by the min-ON dwell (300 s < 600 s).
        assert "fast_source_min_runtime" in out.report.flags
        assert out.fast_source.on is True
        # The report text follows the actually emitted (remembered) command.
        assert "Split grzanie, ON." in out.report.explanation

    def test_engaged_heater_inside_band_does_not_request_release(self) -> None:
        """At error == -0.1 K (inside band) no release is requested at all."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0), dt_seconds=_DT)
        out = controller.step(self._inputs(21.1), dt_seconds=_DT)
        assert out.fast_source.on is True
        assert "fast_source_min_runtime" not in out.report.flags

    def test_engaged_heater_releases_after_dwell(self) -> None:
        """Past the min-ON dwell the far-edge release parks the split: "brak"."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0), dt_seconds=_DT)
        out = controller.step(self._inputs(21.5), dt_seconds=600.0)
        assert out.fast_source.on is False
        assert "Split brak, OFF." in out.report.explanation

    def test_cooling_demand_engages_and_names_chlodzenie(self) -> None:
        """A cooling demand beyond the offset engages: "chlodzenie, ON"."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._inputs(23.0), dt_seconds=_DT)
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.COOLING
        assert "Split chlodzenie, ON." in out.report.explanation

    def test_engaged_cooler_keeps_direction_inside_band(self) -> None:
        """A running cooler holds "chlodzenie" until the far band edge."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(23.0), dt_seconds=_DT)
        out = controller.step(self._inputs(21.5), dt_seconds=_DT)
        assert out.fast_source.on is True
        assert "Split chlodzenie, ON." in out.report.explanation
        assert "fast_source_min_runtime" not in out.report.flags

    def test_engaged_cooler_at_far_edge_requests_release(self) -> None:
        """A cooler at cooling_demand == -deadband asks to stop (dwell blocks)."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(23.0), dt_seconds=_DT)
        out = controller.step(self._inputs(20.7), dt_seconds=_DT)  # demand -0.3
        assert "fast_source_min_runtime" in out.report.flags
        assert out.fast_source.on is True
        assert "Split chlodzenie, ON." in out.report.explanation

    def test_engaged_cooler_inside_band_does_not_request_release(self) -> None:
        """A cooler at cooling_demand -0.1 K requests no release."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(23.0), dt_seconds=_DT)
        out = controller.step(self._inputs(20.9), dt_seconds=_DT)
        assert out.fast_source.on is True
        assert "fast_source_min_runtime" not in out.report.flags

    def test_engaged_cooler_releases_after_dwell(self) -> None:
        """Past the min-ON dwell the cooler parks: "brak, OFF"."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(23.0), dt_seconds=_DT)
        out = controller.step(self._inputs(20.5), dt_seconds=600.0)
        assert out.fast_source.on is False
        assert "Split brak, OFF." in out.report.explanation

    def test_heater_kind_cannot_cool(self) -> None:
        """A HEATER with a cooling demand stays OFF and is flagged."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            self._inputs(23.0, kind=FastSourceKind.HEATER), dt_seconds=_DT
        )
        assert out.fast_source.on is False
        assert "fast_source_cannot_cool" in out.report.flags
        assert "Split brak (grzejnik nie chlodzi), OFF." in out.report.explanation

    def test_blocked_heating_reengage_names_grzanie(self) -> None:
        """A min-OFF-blocked heating engage still names the demand direction.

        The machine re-emits OFF (compressor protection) while the report
        keeps the computed direction "grzanie" — the only cycle where the
        engage branch's own text is observable (an emitted ON overwrites it
        with the remembered direction).
        """
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0), dt_seconds=_DT)  # engage HEATING
        released = controller.step(self._inputs(21.5), dt_seconds=600.0)
        assert released.fast_source.on is False
        out = controller.step(self._inputs(19.0), dt_seconds=_DT)  # blocked
        assert out.fast_source.on is False
        assert "fast_source_min_runtime" in out.report.flags
        assert "Split grzanie, OFF." in out.report.explanation

    def test_blocked_cooling_reengage_names_chlodzenie(self) -> None:
        """A min-OFF-blocked cooling engage names "chlodzenie" while OFF."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(23.0), dt_seconds=_DT)  # engage COOLING
        released = controller.step(self._inputs(20.5), dt_seconds=600.0)
        assert released.fast_source.on is False
        out = controller.step(self._inputs(23.0), dt_seconds=_DT)  # blocked
        assert out.fast_source.on is False
        assert "fast_source_min_runtime" in out.report.flags
        assert "Split chlodzenie, OFF." in out.report.explanation

    def test_heater_cannot_cool_force_off_leaves_no_dwell(self) -> None:
        """The heater force-off shows no assist timer (unlike a decide()-OFF)."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._inputs(19.0, kind=FastSourceKind.HEATER), dt_seconds=_DT)
        released = controller.step(
            self._inputs(21.5, kind=FastSourceKind.HEATER), dt_seconds=600.0
        )
        assert released.fast_source.on is False
        out = controller.step(
            self._inputs(23.5, kind=FastSourceKind.HEATER), dt_seconds=_DT
        )
        assert "fast_source_cannot_cool" in out.report.flags
        assert out.report.fast_dwell_remaining_s is None

    def test_quiet_hours_block_engagement(self) -> None:
        """Quiet hours veto a fresh engage: "brak, OFF" plus the quiet flag."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._inputs(19.0, allowed=False), dt_seconds=_DT)
        assert out.fast_source.on is False
        assert "fast_source_quiet_hours" in out.report.flags
        assert "Split brak, OFF." in out.report.explanation

    def test_no_quiet_flag_when_allowed(self) -> None:
        """The quiet flag is only stamped when the verdict actually blocks."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._inputs(20.9), dt_seconds=_DT)
        assert "fast_source_quiet_hours" not in out.report.flags


class TestCoolingOptOut:
    """A room excluded from cooling parks the valve and keeps an honest report."""

    def _optout(self, room: float | None) -> RoomInputs:
        """COOLING inputs with ``cooling_enabled=False`` and humidity."""
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=room,
            humidity_pct=60.0,
            cooling_enabled=False,
        )

    def test_optout_report_echoes_and_saturation(self) -> None:
        """The opt-out report echoes error/trend/dew, raw 0, saturated=True."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._optout(26.0), dt_seconds=_DT)
        assert "cooling_disabled" in out.report.flags
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.report.raw_valve_pct == pytest.approx(0.0)
        assert out.report.saturated is True
        assert out.report.error_c == pytest.approx(-2.0)
        assert out.report.trend_c_per_h == pytest.approx(0.0)
        assert out.report.room_dew_point_c == pytest.approx(dew_point(26.0, 60.0))

    def test_optout_zeroes_the_sensor_lost_hold(self) -> None:
        """The opt-out parks the hold memory at 0 (never freeze-open chilled)."""
        controller = RoomController(ControllerConfig(), name="salon")
        warm = controller.step(make_inputs(room_temperature_c=15.0), dt_seconds=_DT)
        assert warm.valve_position_pct > 0.0
        controller.step(self._optout(26.0), dt_seconds=_DT)
        lost = controller.step(make_inputs(room_temperature_c=None), dt_seconds=_DT)
        assert lost.valve_position_pct == pytest.approx(0.0)


class TestIntegratorStateMachine:
    """Mode-change reset, inactivity decay clock, bumpless re-seed boundary."""

    def _charged(self, cfg: ControllerConfig | None = None) -> RoomController:
        """Return a controller with a banked heating integral."""
        controller = RoomController(cfg or ControllerConfig(), name="salon")
        for _ in range(3):
            out = controller.step(
                make_inputs(setpoint_c=24.0, room_temperature_c=19.0),
                dt_seconds=_DT,
            )
        assert out.report.i_term > 0.0
        return controller

    def test_mode_change_resets_integrator_even_without_setpoint_change(self) -> None:
        """HEATING -> COOLING clears the integral with no bumpless interference.

        Same setpoint in both modes: the K1 setpoint delta is zero, so the
        observed i_term comes from the S2 mode-change reset alone.
        """
        controller = self._charged()
        cooled = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=24.1,
                humidity_pct=50.0,
                loops=(LoopInput(None, 20.0, None),),
            ),
            dt_seconds=_DT,
        )
        assert cooled.report.i_term == pytest.approx(0.0)

    def test_active_cycle_restarts_the_inactivity_clock_at_zero(self) -> None:
        """Just under 12 h of inactivity after a live cycle keeps the integral."""
        controller = self._charged()
        out = controller.step(
            make_inputs(mode=Mode.OFF, room_temperature_c=21.0),
            dt_seconds=12.0 * 3600.0 - 0.5,
        )
        assert out.report.i_term > 0.0

    def test_inactivity_accumulates_across_cycles(self) -> None:
        """Two 6.5 h OFF stretches (13 h total) decay the stored integral."""
        controller = self._charged()
        parked = make_inputs(mode=Mode.OFF, room_temperature_c=21.0)
        out = controller.step(parked, dt_seconds=6.5 * 3600.0)
        assert out.report.i_term > 0.0  # 6.5 h < 12 h: survives
        out = controller.step(parked, dt_seconds=6.5 * 3600.0)
        assert out.report.i_term == pytest.approx(0.0)  # 13 h >= 12 h: cleared

    def test_decay_at_exactly_12h(self) -> None:
        """The decay threshold is inclusive: exactly 12 h clears the integral."""
        controller = self._charged()
        out = controller.step(
            make_inputs(mode=Mode.OFF, room_temperature_c=21.0),
            dt_seconds=12.0 * 3600.0,
        )
        assert out.report.i_term == pytest.approx(0.0)

    def test_decay_requires_a_nonzero_integral(self) -> None:
        """An integral of exactly 1.0 still counts as non-zero and decays."""
        cfg = ControllerConfig(
            kp=1.0, ki=0.5, kt=0.0, deadband_c=0.0, valve_floor_pct=0.0
        )
        controller = RoomController(cfg, name="salon")
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=20.0), dt_seconds=2.0
        )
        assert out.report.i_term == pytest.approx(1.0)  # 0.5 * 1.0 K * 2 s
        decayed = controller.step(
            make_inputs(mode=Mode.OFF, room_temperature_c=21.0),
            dt_seconds=12.0 * 3600.0,
        )
        assert decayed.report.i_term == pytest.approx(0.0)

    def test_setpoint_step_of_exactly_1k_reseeds_the_integral(self) -> None:
        """A +1.0 K setpoint step re-seeds the integral by kp * 1 K (K1)."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=19.0), dt_seconds=_DT
        )
        out = controller.step(
            make_inputs(setpoint_c=22.0, room_temperature_c=19.0), dt_seconds=_DT
        )
        # Without the bumpless re-seed the integral would be ~2 %.
        assert out.report.i_term > 10.0


class TestSelfTestGates:
    """Actuation self-test: begin gates, dew abort, safety abort, watchdog."""

    def _probed_cooling(self, humidity: float) -> RoomInputs:
        """Cooling inputs with both water probes on one loop (supply 20)."""
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=26.0,
            humidity_pct=humidity,
            loops=(LoopInput(None, 20.0, 21.0),),
        )

    def test_begin_allowed_in_heating(self) -> None:
        """A heating room with both probes may start the test."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(
            make_inputs(room_temperature_c=19.0, loops=(LoopInput(None, 30.0, 29.0),)),
            dt_seconds=_DT,
        )
        assert controller.begin_actuation_test(1500.0) is None

    def test_begin_allowed_in_cooling_with_full_dew_headroom(self) -> None:
        """Cooling may start only with the dew throttle fully open (== 1.0)."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._probed_cooling(50.0), dt_seconds=_DT)
        assert controller.begin_actuation_test(1500.0) is None

    def test_running_test_continues_at_full_dew_factor(self) -> None:
        """A cooling test with the throttle fully open keeps running."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._probed_cooling(50.0), dt_seconds=_DT)
        assert controller.begin_actuation_test(1500.0) is None
        out = controller.step(self._probed_cooling(50.0), dt_seconds=_DT)
        assert "actuation_test_running" in out.report.flags
        assert out.valve_position_pct == pytest.approx(100.0)

    def test_running_test_aborts_when_dew_throttle_engages(self) -> None:
        """The throttle collapsing mid-test aborts it, never "finishes by force"."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._probed_cooling(50.0), dt_seconds=_DT)
        assert controller.begin_actuation_test(1500.0) is None
        # RH 65 -> dew ~18.9, supply 20 -> gap ~1.1: factor < 1, no hard S2.
        out = controller.step(self._probed_cooling(65.0), dt_seconds=_DT)
        assert 0.0 < out.report.dew_throttle_factor < 1.0
        assert "actuation_test_running" not in out.report.flags
        assert "s2_condensation" not in out.report.flags

    def test_s1_overheat_aborts_a_running_test(self) -> None:
        """An S1 trip overrides the excursion and aborts the measurement."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(
            make_inputs(room_temperature_c=19.0, loops=(LoopInput(None, 30.0, 29.0),)),
            dt_seconds=_DT,
        )
        assert controller.begin_actuation_test(1500.0) is None
        out = controller.step(
            make_inputs(room_temperature_c=19.0, loops=(LoopInput(None, 45.0, 44.0),)),
            dt_seconds=_DT,
        )
        assert "s1_floor_overheat" in out.report.flags
        assert "actuation_test_running" not in out.report.flags

    def test_flow_watchdog_paused_during_test(self) -> None:
        """The S6 no-flow watchdog is held while the excursion runs (S6/C)."""
        cfg = ControllerConfig(
            flow_response_window_min=1.0, flow_open_threshold_pct=15.0
        )
        controller = RoomController(cfg, name="salon")
        stagnant = make_inputs(
            room_temperature_c=19.0,
            loops=(LoopInput(None, 30.0, 30.0),),  # zero delta-T: no flow
            circulation_evident=True,
        )
        controller.step(stagnant, dt_seconds=_DT)
        assert controller.begin_actuation_test(1500.0) is None
        for _ in range(3):
            out = controller.step(stagnant, dt_seconds=_DT)
            assert "actuation_test_running" in out.report.flags
            assert "loop_no_flow" not in out.report.flags


class TestFarewellAndGroupConflict:
    """Fast-source reconciliation hooks: farewell OFF and group arbitration."""

    def _synced_heating(self, *, cooling: bool = False) -> RoomController:
        """A controller whose split is synced ON (HEATING, or COOLING)."""
        controller = RoomController(ControllerConfig(), name="salon")
        inputs = (
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=27.0,
                humidity_pct=50.0,
                loops=(LoopInput(None, 20.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=True,
                fast_source_hvac_mode="cool",
            )
            if cooling
            else make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=True,
                fast_source_hvac_mode="heat",
            )
        )
        for _ in range(2):
            out = controller.step(inputs, dt_seconds=_DT)
        assert out.fast_source.on is True
        return controller

    def test_farewell_records_the_off_command(self) -> None:
        """After a farewell OFF the physical OFF feedback agrees: no mismatch."""
        controller = self._synced_heating()
        controller.notify_fast_source_farewell()
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=False,
            ),
            dt_seconds=_DT,
        )
        assert "fast_source_mismatch" not in out.report.flags
        assert "fast_source_manual" not in out.report.flags

    def test_touch_after_farewell_is_adopted_as_user_intent(self) -> None:
        """A touch right after a farewell (long-idle machine) is adopted (§28).

        The farewell records an OFF command identical to what the machine
        was already emitting, so the settle clock is untouched and the very
        next divergence counts as a settled user touch.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        idle = make_inputs(
            mode=Mode.TRANSITIONAL,
            setpoint_c=21.0,
            room_temperature_c=21.0,
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_on=False,
        )
        for _ in range(3):
            controller.step(idle, dt_seconds=_DT)
        controller.notify_fast_source_farewell()
        out = controller.step(
            make_inputs(
                mode=Mode.TRANSITIONAL,
                setpoint_c=21.0,
                room_temperature_c=21.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=True,
                fast_source_hvac_mode="heat",
            ),
            dt_seconds=_DT,
        )
        assert "fast_source_manual" in out.report.flags

    def test_resolve_group_conflict_forces_off_and_clears_the_dwell(self) -> None:
        """The loser emits OFF, is flagged, and shows no assist timer."""
        controller = self._synced_heating(cooling=True)
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=27.0,
                humidity_pct=50.0,
                loops=(LoopInput(None, 20.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=True,
                fast_source_hvac_mode="cool",
            ),
            dt_seconds=_DT,
        )
        resolved = controller.resolve_group_conflict(out)
        assert resolved.fast_source.on is False
        assert resolved.fast_source.mode is FastSourceMode.OFF
        assert "fast_source_group_conflict" in resolved.report.flags
        assert resolved.report.fast_dwell_remaining_s is None

    def test_after_conflict_the_physical_off_agrees(self) -> None:
        """The recorded conflict OFF matches the hardware: no mismatch later."""
        controller = self._synced_heating(cooling=True)
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=27.0,
            humidity_pct=50.0,
            loops=(LoopInput(None, 20.0, None),),
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_on=True,
            fast_source_hvac_mode="cool",
        )
        out = controller.step(inputs, dt_seconds=_DT)
        controller.resolve_group_conflict(out)
        nxt = controller.step(
            replace(inputs, fast_source_on=False, fast_source_hvac_mode=None),
            dt_seconds=_DT,
        )
        assert "fast_source_mismatch" not in nxt.report.flags
        assert "fast_source_manual" not in nxt.report.flags

    def test_touch_two_cycles_after_conflict_is_adopted(self) -> None:
        """A settled touch after a lost arbitration is adopted as user intent.

        The conflict OFF was recorded as (off, OFF); the next emitted OFF
        matches it, so the settle clock keeps running and the touch at the
        second regular cycle is adopted (§28 settle rule).
        """
        controller = self._synced_heating(cooling=True)
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=27.0,
            humidity_pct=50.0,
            loops=(LoopInput(None, 20.0, None),),
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_on=True,
            fast_source_hvac_mode="cool",
        )
        out = controller.step(inputs, dt_seconds=_DT)
        controller.resolve_group_conflict(out)
        controller.step(
            replace(inputs, fast_source_on=False, fast_source_hvac_mode=None),
            dt_seconds=_DT,
        )
        touched = controller.step(
            replace(inputs, fast_source_on=True),
            dt_seconds=_DT,
        )
        assert "fast_source_manual" in touched.report.flags


class TestSafeDegradeText:
    """The sensor-lost safe-degrade explanation strings and saturation."""

    def test_heating_sensor_lost_holds_and_explains(self) -> None:
        """HEATING: hold text "zawor trzyma ostatnia pozycje N%"."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(make_inputs(room_temperature_c=15.0), dt_seconds=_DT)
        lost = controller.step(make_inputs(room_temperature_c=None), dt_seconds=_DT)
        assert lost.report.explanation.startswith(
            "Utrata czujnika pokoju: zawor trzyma ostatnia pozycje "
        )
        assert lost.report.explanation.endswith("%, split OFF.")

    def test_cooling_sensor_lost_closes_and_explains(self) -> None:
        """COOLING: exact park-at-0 text, and the park is not saturation."""
        controller = RoomController(ControllerConfig(), name="salon")
        lost = controller.step(
            make_inputs(mode=Mode.COOLING, room_temperature_c=None),
            dt_seconds=_DT,
        )
        assert lost.valve_position_pct == pytest.approx(0.0)
        assert lost.report.explanation == (
            "Utrata czujnika pokoju: zawor 0% (chlodzenie), split OFF."
        )
        assert lost.report.saturated is False


class TestSafetyOverrideReport:
    """The safety layer's valve/air-side split of duties and report stamping."""

    def test_s1_close_stamps_saturation_and_banner(self) -> None:
        """S1 parks the valve at 0: saturated=True, banner prepended."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(room_temperature_c=20.0, loops=(LoopInput(None, 45.0, None),)),
            dt_seconds=_DT,
        )
        assert "s1_floor_overheat" in out.report.flags
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.report.saturated is True
        assert out.report.explanation.startswith(
            "Bezpieczenstwo s1_floor_overheat: zawor 0%. | "
        )

    def test_s3_emergency_heat_saturates_at_100(self) -> None:
        """S3 without a CLOSE_VALVE rule opens the floor fully: saturated."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=4.0,
                loops=(LoopInput(None, 30.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=_DT,
        )
        assert "s3_emergency_heat" in out.report.flags
        assert out.valve_position_pct == pytest.approx(100.0)
        assert out.report.saturated is True

    def test_s3_without_fast_source_cannot_force_on(self) -> None:
        """A floor-only room under S3 gets valve 100 but never a split ON."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(room_temperature_c=4.0, loops=(LoopInput(None, 30.0, None),)),
            dt_seconds=_DT,
        )
        assert "s3_emergency_heat" in out.report.flags
        assert out.valve_position_pct == pytest.approx(100.0)
        assert out.fast_source.on is False

    def test_s4_emergency_cool_is_air_side_only(self) -> None:
        """S4 cools by split alone: the floor valve is driven to exactly 0."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=36.0,
                humidity_pct=50.0,
                loops=(LoopInput(None, 20.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=_DT,
        )
        assert "s4_emergency_cool" in out.report.flags
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.COOLING

    def test_s4_heater_is_flagged_cannot_cool(self) -> None:
        """A HEATER under S4 stays OFF and carries "fast_source_cannot_cool"."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=36.0,
                loops=(LoopInput(None, 20.0, None),),
                fast_source_kind=FastSourceKind.HEATER,
            ),
            dt_seconds=_DT,
        )
        assert "s4_emergency_cool" in out.report.flags
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.fast_source.on is False
        assert "fast_source_cannot_cool" in out.report.flags

    def test_s5_neutral_position_is_not_saturated(self) -> None:
        """The S5 fallback (heating floor 0.5 %) is a neutral, open position."""
        controller = RoomController(ControllerConfig(valve_floor_pct=0.5), name="salon")
        stale = replace(
            make_inputs(room_temperature_c=17.0), last_update_age_minutes=20.0
        )
        out = controller.step(stale, dt_seconds=_DT)
        assert "s5_watchdog" in out.report.flags
        assert out.valve_position_pct == pytest.approx(0.5)
        assert out.report.saturated is False

    def _off_with_s5(self, name: str) -> str:
        """Return the safety-capped explanation of an OFF room named ``name``."""
        controller = RoomController(ControllerConfig(), name=name)
        stale = replace(
            make_inputs(mode=Mode.OFF, room_temperature_c=20.0),
            last_update_age_minutes=20.0,
        )
        out = controller.step(stale, dt_seconds=_DT)
        assert "s5_watchdog" in out.report.flags
        return out.report.explanation

    def test_explanation_capped_at_200_chars(self) -> None:
        """A bloated banner+regulation text is cut to 200 chars ending "..."."""
        prefix = "Bezpieczenstwo s5_watchdog: zawor 0%. | Off ("
        suffix = "): zawor 0%, brak komend."
        name = "x" * (_SAFETY_EXPLANATION_MAX_LEN - len(prefix) - len(suffix) + 20)
        explanation = self._off_with_s5(name)
        assert len(explanation) == _SAFETY_EXPLANATION_MAX_LEN
        assert explanation.endswith("...")

    def test_explanation_exactly_200_chars_is_not_capped(self) -> None:
        """The cap is strict: exactly 200 chars pass through untouched."""
        prefix = "Bezpieczenstwo s5_watchdog: zawor 0%. | Off ("
        suffix = "): zawor 0%, brak komend."
        name = "x" * (_SAFETY_EXPLANATION_MAX_LEN - len(prefix) - len(suffix))
        explanation = self._off_with_s5(name)
        assert len(explanation) == _SAFETY_EXPLANATION_MAX_LEN
        assert not explanation.endswith("...")


class TestGoverningSupply:
    """The S1/S2 proxy: hottest supply in heating, coldest in cooling."""

    def test_heating_governing_supply_is_the_hottest_loop(self) -> None:
        """Loops 30/45 degC in heating: the 45 degC loop trips S1."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=20.0,
                loops=(LoopInput(None, 30.0, None), LoopInput(None, 45.0, None)),
            ),
            dt_seconds=_DT,
        )
        assert "s1_floor_overheat" in out.report.flags

    def test_cooling_governing_supply_is_the_coldest_loop(self) -> None:
        """Loops 15/18 degC with dew ~15.8: only the 15 degC loop trips S2."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=24.0,
                humidity_pct=60.0,  # dew ~15.8 degC
                loops=(LoopInput(None, 15.0, None), LoopInput(None, 18.0, None)),
            ),
            dt_seconds=_DT,
        )
        assert "s2_condensation" in out.report.flags


class TestFastSourceCoordination:
    """Step-14 coordination: heater rule, dry-assist hysteresis separation."""

    def test_heater_in_cooling_mode_is_forced_off_and_flagged(self) -> None:
        """A HEATER can never be commanded to cool, even with demand."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=27.0,
                humidity_pct=50.0,
                loops=(LoopInput(None, 20.0, None),),
                fast_source_kind=FastSourceKind.HEATER,
            ),
            dt_seconds=_DT,
        )
        assert out.fast_source.on is False
        assert "fast_source_cannot_cool" in out.report.flags

    def test_temperature_run_keeps_its_own_release_threshold(self) -> None:
        """A split running on TEMPERATURE releases at the deadband, not offset.

        The dry-assist hysteresis state must not leak into a temperature run:
        engaged in cooling, demand 0.5 K (deadband 0.3 < 0.5 < offset 1.0)
        keeps the split ON.
        """
        cfg = ControllerConfig(
            fast_min_on_minutes=0.0, fast_min_off_minutes=0.0, dry_enabled=True
        )
        controller = RoomController(cfg, name="salon")
        cool = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=25.5,
            humidity_pct=50.0,
            loops=(LoopInput(None, 20.0, None),),
            fast_source_kind=FastSourceKind.SPLIT,
        )
        engaged = controller.step(cool, dt_seconds=_DT)
        assert engaged.fast_source.on is True
        assert engaged.fast_source.mode is FastSourceMode.COOLING
        out = controller.step(replace(cool, room_temperature_c=24.5), dt_seconds=_DT)
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.COOLING

    def test_dry_run_releases_only_past_the_deadband(self) -> None:
        """A running dry at error == deadband (overcool edge) releases.

        The dry temperature gate is a full band of hysteresis: engage at
        error <= 0, release only when the room is overcooled to the deadband
        (strictly: error < deadband keeps it, == releases).
        """
        cfg = ControllerConfig(
            fast_min_on_minutes=0.0,
            fast_min_off_minutes=0.0,
            dry_enabled=True,
            deadband_c=0.5,
        )
        controller = RoomController(cfg, name="salon")
        humid = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=24.0,
            humidity_pct=70.0,  # dew ~18.2 > dry_dew_max_c 17
            loops=(LoopInput(None, 20.0, None),),
            fast_source_kind=FastSourceKind.SPLIT,
        )
        engaged = controller.step(humid, dt_seconds=_DT)
        assert engaged.fast_source.on is True
        assert engaged.fast_source.mode is FastSourceMode.DRY
        # Room overcooled to exactly the deadband (24.0 - 23.5 == 0.5 in
        # float): the strict overcool gate releases the dry run.
        out = controller.step(replace(humid, room_temperature_c=23.5), dt_seconds=_DT)
        assert out.fast_source.on is False

    def test_dry_engages_only_strictly_above_the_dew_threshold(self) -> None:
        """A room dew point exactly at ``dry_dew_max_c`` does not engage."""
        dew = dew_point(26.0, 70.0)
        assert 12.0 <= dew <= 22.0  # config bound for dry_dew_max_c
        cfg = ControllerConfig(
            fast_min_on_minutes=0.0,
            fast_min_off_minutes=0.0,
            dry_enabled=True,
            dry_dew_max_c=dew,
        )
        controller = RoomController(cfg, name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=26.0,
                room_temperature_c=26.0,
                humidity_pct=70.0,
                loops=(LoopInput(None, 20.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=_DT,
        )
        assert out.fast_source.on is False


class TestRoomDewPoint:
    """The room dew point needs a usable (positive) humidity reading."""

    def test_zero_humidity_yields_no_dew_point(self) -> None:
        """RH exactly 0 is unusable: the report dew point is None."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(room_temperature_c=20.0, humidity_pct=0.0),
            dt_seconds=_DT,
        )
        assert out.report.room_dew_point_c is None

    def test_tiny_positive_humidity_computes_a_dew_point(self) -> None:
        """RH 1 % is a usable reading: a dew point is computed."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(room_temperature_c=20.0, humidity_pct=1.0),
            dt_seconds=_DT,
        )
        assert out.report.room_dew_point_c == pytest.approx(dew_point(20.0, 1.0))


class TestOffModeReport:
    """The OFF path echoes the live signals and saturates at the parked 0."""

    def test_off_report_echoes_and_saturation(self) -> None:
        """OFF: valve 0 (saturated), error/trend/dew echoed from live inputs."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.OFF,
                setpoint_c=21.0,
                room_temperature_c=20.0,
                humidity_pct=55.0,
            ),
            dt_seconds=_DT,
        )
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.report.saturated is True
        assert out.report.error_c == pytest.approx(1.0)
        assert out.report.trend_c_per_h == pytest.approx(0.0)
        assert out.report.room_dew_point_c == pytest.approx(dew_point(20.0, 55.0))


class TestManualHoldText:
    """The hold text rounds the remaining minutes UP (never claims 0)."""

    def test_hold_text_rounds_minutes_up(self) -> None:
        """3541 s left on the hold reads "jeszcze 60 min", not 59."""
        controller = RoomController(ControllerConfig(), name="salon")
        idle = make_inputs(
            mode=Mode.TRANSITIONAL,
            setpoint_c=21.0,
            room_temperature_c=21.0,
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_on=False,
        )
        for _ in range(3):
            controller.step(idle, dt_seconds=_DT)
        # The touch cycle itself is 59 s long: 3600 - 59 = 3541 s remain.
        out = controller.step(
            make_inputs(
                mode=Mode.TRANSITIONAL,
                setpoint_c=21.0,
                room_temperature_c=21.0,
                fast_source_kind=FastSourceKind.SPLIT,
                fast_source_on=True,
                fast_source_hvac_mode="heat",
            ),
            dt_seconds=59.0,
        )
        assert "fast_source_manual" in out.report.flags
        assert "reczne sterowanie (jeszcze 60 min)" in out.report.explanation


class TestBuildingArbitrationIntegration:
    """The conflict-loser rewrite is observable through the building step."""

    def test_group_conflict_loser_is_forced_off(self) -> None:
        """Two rooms, one group, opposite demands: the loser is rewritten OFF."""
        building = BuildingController(
            {"a": ControllerConfig(), "b": ControllerConfig()}
        )
        out = building.step(
            {
                "a": make_inputs(
                    mode=Mode.COOLING,
                    setpoint_c=24.0,
                    room_temperature_c=27.0,
                    humidity_pct=50.0,
                    loops=(LoopInput(None, 20.0, None),),
                    fast_source_kind=FastSourceKind.SPLIT,
                    fast_source_group="g1",
                ),
                "b": make_inputs(
                    mode=Mode.HEATING,
                    setpoint_c=21.0,
                    room_temperature_c=18.0,
                    fast_source_kind=FastSourceKind.SPLIT,
                    fast_source_group="g1",
                ),
            },
            dt_seconds=_DT,
        )
        winner_side = {
            n: out.rooms[n].fast_source.mode
            for n in ("a", "b")
            if out.rooms[n].fast_source.on
        }
        loser = next(n for n in ("a", "b") if n not in winner_side)
        assert len(winner_side) == 1
        assert "fast_source_group_conflict" in out.rooms[loser].report.flags
        assert out.rooms[loser].fast_source.on is False
        assert out.rooms[loser].report.fast_dwell_remaining_s is None
