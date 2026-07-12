"""Unit tests for the round-2 regulation fixes (2026-07-12).

Pins the behavioural contracts introduced by the second algorithmic review:

* K1 — bumpless setpoint transfer: an effective-setpoint change of ``dK``
  between PID-active cycles re-seeds the integral by ``kp * dK`` in the
  mode's error convention (sign INVERTS in COOLING), and the reference dies
  with every PID reset (no stale-delta re-seed).
* K7 — stale-RH gate: a held 60-120 min old humidity reading pads the
  effective dew point by +1 K in BOTH protection layers and flags
  ``"rh_stale_gated"``.
* K2/B1 — the kt canary: no gate scenario measurably contrasts kt=12 vs
  kt=0 on the calibrated twin (docs/DECISIONS.md §11), so the trend-damping
  SIGN and magnitude are pinned here at unit level instead.
* K5 — ``BuildingController._degraded_room_output`` degrades mode-aware
  (HEATING holds, everything else closes), symmetric with safe degrade.
* R2-F6 — the public trend-invalidation hook for the adapter's dt clamp.

Units: temperatures / setpoints in degC; valve in percent (0..100); trend in
K/h; ``dt_seconds`` in seconds. This module never imports ``homeassistant``.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    BuildingController,
    RoomController,
)
from custom_components.tortoise_ufh.core.models import LoopInput, Mode
from tests.unit.conftest import make_inputs

pytestmark = pytest.mark.unit


class TestBumplessSetpointTransfer:
    """K1: a setpoint change re-seeds the integral by kp*dK."""

    def test_heating_setpoint_drop_discharges_integral(self) -> None:
        """Lowering the heating setpoint by dK shifts I down by ~kp*dK."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        # A couple of live cycles at setpoint 23 establish the reference...
        for _ in range(3):
            controller.step(
                make_inputs(setpoint_c=23.0, room_temperature_c=22.0),
                dt_seconds=300.0,
            )
        # ...and a white-box pre-charge stands in for the hours of lagging
        # plant that legitimately saturate the integral (ki-scale growth is
        # far too slow for a unit test).
        controller._pid.shift_integral(+50.0)
        out = controller.step(
            make_inputs(setpoint_c=23.0, room_temperature_c=22.0),
            dt_seconds=300.0,
        )
        i_before = out.report.i_term
        assert i_before > cfg.kp * 2.0  # enough charge to observe the shift
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=22.0),
            dt_seconds=300.0,
        )
        # The -2 K change shifts I by kp * (-2) = -28, plus one cycle of the
        # 8x unwind (8 * ki * 0.7 K * 300 s = 2.5 pp) — tolerance covers it.
        assert out.report.i_term == pytest.approx(i_before - cfg.kp * 2.0, abs=3.5)

    def test_cooling_setpoint_drop_charges_integral(self) -> None:
        """COOLING inverts the error sign: a LOWER setpoint means MORE cooling.

        The same -2 K change must shift I UP by ~kp*2.
        """
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        cooling: dict[str, Any] = dict(
            mode=Mode.COOLING,
            room_temperature_c=25.0,
            humidity_pct=45.0,
            loops=(LoopInput(None, 20.0, None),),  # gap >> margin: factor 1
        )
        for _ in range(20):
            out = controller.step(
                make_inputs(setpoint_c=24.0, **cooling), dt_seconds=300.0
            )
        i_before = out.report.i_term
        assert i_before > 0.0
        out = controller.step(make_inputs(setpoint_c=22.0, **cooling), dt_seconds=300.0)
        assert out.report.i_term == pytest.approx(i_before + cfg.kp * 2.0, abs=2.0)

    def test_mode_flip_does_not_apply_stale_delta(self) -> None:
        """A HEATING->COOLING flip resets the PID and the setpoint reference.

        The first cooling cycle must not receive a stale-delta re-seed.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        for _ in range(5):
            controller.step(
                make_inputs(setpoint_c=23.0, room_temperature_c=20.0),
                dt_seconds=300.0,
            )
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=25.0,
                room_temperature_c=26.0,
                humidity_pct=45.0,
                loops=(LoopInput(None, 20.0, None),),
            ),
            dt_seconds=300.0,
        )
        # A stale +2 K delta would have shifted the cleared integral by
        # -/+ kp*2 = 28 pp; it must stay ki-scale small instead.
        assert abs(out.report.i_term) < 1.0


class TestStaleHumidityGate:
    """K7/D5: a held 60-120 min old RH pads the dew point by frac * 1 K."""

    _ROOM: dict[str, Any] = {
        "mode": Mode.COOLING,
        "setpoint_c": 24.0,
        "room_temperature_c": 26.0,
        "humidity_pct": 60.0,
    }

    def test_stale_rh_throttles_earlier_and_flags(self) -> None:
        """The same reading throttles harder when stale (+1 K pad) + flag."""
        # Room 26/60 % -> dew ~17.9. Supply 19.4 -> gap ~1.5 -> factor ~0.73
        # fresh; stale pads dew to ~18.9 -> gap ~0.5 -> factor ~0.23 (the
        # +1 K pad moves the factor down by exactly pad/ramp = 0.5).
        fresh_ctrl = RoomController(ControllerConfig(), name="a")
        stale_ctrl = RoomController(ControllerConfig(), name="b")
        loops = (LoopInput(None, 19.4, None),)
        fresh = fresh_ctrl.step(
            make_inputs(loops=loops, **self._ROOM), dt_seconds=300.0
        )
        stale = stale_ctrl.step(
            make_inputs(loops=loops, humidity_stale_frac=1.0, **self._ROOM),
            dt_seconds=300.0,
        )
        assert stale.report.dew_throttle_factor == pytest.approx(
            max(0.0, fresh.report.dew_throttle_factor - 0.5), abs=1e-6
        )
        assert "rh_stale_gated" in stale.report.flags
        assert "rh_stale_gated" not in fresh.report.flags

    def test_stale_pad_is_linear_in_the_fraction(self) -> None:
        """D5 (2026-07-12): half the staleness pads half the kelvin.

        A fraction of 0.5 (age 90 min) moves the throttle factor down by
        exactly ``0.5 * pad / ramp = 0.25`` — the pad grows continuously with
        the age instead of jumping a full +1 K at the 60-min edge (the jump
        was itself a mini limit cycle: factor stepped by 0.5 across one
        reporting cadence).
        """
        fresh_ctrl = RoomController(ControllerConfig(), name="a")
        half_ctrl = RoomController(ControllerConfig(), name="b")
        loops = (LoopInput(None, 19.4, None),)
        fresh = fresh_ctrl.step(
            make_inputs(loops=loops, **self._ROOM), dt_seconds=300.0
        )
        half = half_ctrl.step(
            make_inputs(loops=loops, humidity_stale_frac=0.5, **self._ROOM),
            dt_seconds=300.0,
        )
        assert half.report.dew_throttle_factor == pytest.approx(
            max(0.0, fresh.report.dew_throttle_factor - 0.25), abs=1e-6
        )
        assert "rh_stale_gated" in half.report.flags

    def test_stale_rh_raises_global_safe_dew_point(self) -> None:
        """The global maximum prices the staleness in with the same frac pad."""
        building = BuildingController({"salon": ControllerConfig()})
        loops = (LoopInput(None, 22.0, None),)
        fresh = building.step(
            {"salon": make_inputs(loops=loops, **self._ROOM)}, dt_seconds=300.0
        )
        building.reset()
        stale = building.step(
            {"salon": make_inputs(loops=loops, humidity_stale_frac=1.0, **self._ROOM)},
            dt_seconds=300.0,
        )
        building.reset()
        half = building.step(
            {"salon": make_inputs(loops=loops, humidity_stale_frac=0.5, **self._ROOM)},
            dt_seconds=300.0,
        )
        assert fresh.global_safe_dew_point_c is not None
        assert stale.global_safe_dew_point_c is not None
        assert half.global_safe_dew_point_c is not None
        assert stale.global_safe_dew_point_c == pytest.approx(
            fresh.global_safe_dew_point_c + 1.0
        )
        assert half.global_safe_dew_point_c == pytest.approx(
            fresh.global_safe_dew_point_c + 0.5
        )


class TestTrendTermSign:
    """The kt canary (K2/B1): pins the trend-damping sign and magnitude."""

    def test_rising_toward_setpoint_damps_negatively(self) -> None:
        """A room rising toward the heating setpoint is DAMPED.

        ``trend_term == -kt * filtered_trend`` — a sign regression (trend
        excitation) would pass the whole simulation gate unnoticed, because
        no scenario measurably contrasts kt=12 vs kt=0 on the calibrated
        twin.
        """
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        temp = 19.0
        out = controller.step(make_inputs(room_temperature_c=temp), dt_seconds=300.0)
        for _ in range(12):  # sustained +0.6 K/h climb
            temp += 0.05
            out = controller.step(
                make_inputs(room_temperature_c=temp), dt_seconds=300.0
            )
        trend = out.report.trend_c_per_h
        assert trend is not None
        assert trend > 0.2  # the filter tracks the climb
        assert out.report.trend_term == pytest.approx(-cfg.kt * trend)
        assert out.report.trend_term < 0.0

    def test_falling_away_from_setpoint_is_not_damped(self) -> None:
        """A room falling AWAY from the heating setpoint is never damped.

        The ``max(0, trend_toward)`` asymmetry: damping acts only on
        approach, never as a brake against recovery.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        temp = 20.5
        out = controller.step(make_inputs(room_temperature_c=temp), dt_seconds=300.0)
        for _ in range(12):
            temp -= 0.05
            out = controller.step(
                make_inputs(room_temperature_c=temp), dt_seconds=300.0
            )
        assert out.report.trend_term == pytest.approx(0.0)


class TestDegradedRoomOutputModeAware:
    """K5: a crashed controller degrades mode-aware (dew-F1 symmetry)."""

    def test_heating_holds_last_valve(self) -> None:
        """HEATING keeps the last healthy position (bounded warm water)."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        held = controller.last_valve_pct
        out = BuildingController._degraded_room_output(
            controller, ValueError("boom"), Mode.HEATING, 19.0
        )
        assert out.valve_position_pct == pytest.approx(held)
        assert out.fast_source.on is False
        assert "controller_error" in out.report.flags

    @pytest.mark.parametrize("mode", [Mode.COOLING, Mode.TRANSITIONAL, Mode.OFF])
    def test_non_heating_closes_valve(self, mode: Mode) -> None:
        """COOLING/TRANSITIONAL/OFF drive the valve to 0.

        A crashed controller computes neither condensation defence, so a
        held-open valve would pass unprotected chilled water indefinitely.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        assert controller.last_valve_pct > 0.0
        out = BuildingController._degraded_room_output(
            controller, ValueError("boom"), mode, 25.0
        )
        assert out.valve_position_pct == pytest.approx(0.0)
        assert out.fast_source.on is False
        assert "controller_error" in out.report.flags


class TestTrendInvalidationHook:
    """R2-F6: the adapter's dt-clamp hook restarts the trend from zero."""

    def test_invalidate_trends_restarts_filter(self) -> None:
        """After ``invalidate_trends`` the next cycle reports a zero trend."""
        building = BuildingController({"salon": ControllerConfig()})
        temp = 19.0
        for _ in range(6):
            temp += 0.05
            out = building.step(
                {"salon": make_inputs(room_temperature_c=temp)},
                dt_seconds=300.0,
            )
        report = out.rooms["salon"].report
        assert report.trend_c_per_h is not None
        assert report.trend_c_per_h > 0.0
        building.invalidate_trends()
        out = building.step(
            {"salon": make_inputs(room_temperature_c=temp + 1.0)},
            dt_seconds=300.0,
        )
        # The reference sample was dropped: the big post-gap delta must not
        # be divided by one clamped dt (a fictitious trend spike).
        assert out.rooms["salon"].report.trend_c_per_h == pytest.approx(0.0)


class TestSetpointWiggleResidual:
    """K6 (2026-07-12): a setpoint wiggle at a small integral is idempotent."""

    def test_down_up_wiggle_restores_operating_point(self) -> None:
        """-3 K and back within two debounced recomputes: I returns to I0.

        The R3 audit measured the pre-fix pump at ~2*kp*dK (I0 = 10 ->
        I = 79.8 with the valve at 79.8 % at zero error inside the band,
        where the unwind is dead). The night_setback twin gate is blind to
        this regime (its winter integral never clamps), so the regression
        pin lives here on the pure core.
        """
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        controller._pid.shift_integral(+10.0)
        settled = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=21.0),
            dt_seconds=300.0,
        )
        assert settled.valve_position_pct == pytest.approx(10.0, abs=0.1)
        controller.step(
            make_inputs(setpoint_c=18.0, room_temperature_c=21.0), dt_seconds=5.0
        )
        back = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=21.0), dt_seconds=5.0
        )
        assert back.report.i_term == pytest.approx(10.0, abs=0.5)
        assert back.valve_position_pct == pytest.approx(10.0, abs=0.5)
