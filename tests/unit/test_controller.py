"""Unit tests for :mod:`tortoise_ufh.controller`.

Exercises the per-room :class:`~tortoise_ufh.controller.RoomController` and the
whole-building :class:`~tortoise_ufh.controller.BuildingController` against the
frozen black-box contract:

* The deadband suppresses integral growth.
* The heating valve floor is applied only when calling for heat.
* The cooling local dew-point throttle (S2) drives the valve toward 0 near the
  dew point and raises ``s2_condensation`` (throttling, not saturation).
* ``BuildingController`` global safe dew point equals ``max_over_cooled(T_dew)``
  plus the fixed margin, and is ``None`` with no eligible room; per-room
  eligibility is classified by one shared logic.
* The under-the-hood :class:`~tortoise_ufh.models.RoomReport` fields are
  populated and echo the measured room temperature on every path.
* The trend is EMA-filtered (S10) and the integrator hygiene rules (S1/S2)
  freeze, reset and decay the integral.

The fast-source (split) decision path is covered in ``test_fast_source.py``;
the safety / safe-degrade rules in ``test_controller_safety.py``.

Units: temperatures / setpoints / dew points in degC; valve in percent
(0..100); trend in K/h; ``dt_seconds`` in seconds. This module never imports
``homeassistant``.
"""

from __future__ import annotations

import math

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    GLOBAL_SAFE_DEW_MARGIN_K,
    BuildingController,
    RoomController,
    classify_dew_eligibility,
)
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.models import (
    LoopInput,
    Mode,
    RoomInputs,
)
from tests.unit.conftest import make_inputs


class TestDeadband:
    """The deadband band suppresses integral wind-up."""

    @pytest.mark.unit
    def test_error_inside_deadband_keeps_integral_zero(self) -> None:
        """An error inside the deadband leaves the integral term at zero."""
        cfg = ControllerConfig(deadband_c=0.3)
        controller = RoomController(cfg, name="salon")
        # |setpoint - room| = 0.1 K < deadband -> error_db == 0.
        for _ in range(5):
            out = controller.step(
                make_inputs(setpoint_c=21.0, room_temperature_c=20.9),
                dt_seconds=300.0,
            )
        assert out.report.i_term == pytest.approx(0.0)
        assert out.report.p_term == pytest.approx(0.0)

    @pytest.mark.unit
    def test_error_beyond_deadband_grows_integral(self) -> None:
        """An error beyond the deadband makes the integral term accumulate."""
        cfg = ControllerConfig(deadband_c=0.3, ki=0.02)
        controller = RoomController(cfg, name="salon")
        first = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=19.0),
            dt_seconds=300.0,
        )
        second = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=19.0),
            dt_seconds=300.0,
        )
        assert first.report.i_term > 0.0
        assert second.report.i_term > first.report.i_term


class TestValveFloor:
    """The heating valve floor is applied only when calling for heat."""

    @pytest.mark.unit
    def test_floor_applied_when_calling_for_heat(self) -> None:
        """A small heating demand is lifted up to the valve floor."""
        cfg = ControllerConfig(valve_floor_pct=15.0, deadband_c=0.3)
        controller = RoomController(cfg, name="salon")
        # error 0.35 K -> error_db 0.05 K -> PID output well below the floor.
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=20.65),
            dt_seconds=300.0,
        )
        assert out.report.valve_floor_applied is True
        assert out.valve_position_pct == pytest.approx(cfg.valve_floor_pct)
        assert out.report.raw_valve_pct < cfg.valve_floor_pct

    @pytest.mark.unit
    def test_floor_not_applied_when_satisfied(self) -> None:
        """Above setpoint (no heat demand) the valve floor is not applied."""
        cfg = ControllerConfig(valve_floor_pct=15.0, deadband_c=0.3)
        controller = RoomController(cfg, name="salon")
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=21.5),
            dt_seconds=300.0,
        )
        assert out.report.valve_floor_applied is False
        assert out.valve_position_pct == pytest.approx(0.0)


class TestCoolingDewThrottle:
    """The cooling local dew-point throttle (S2)."""

    @pytest.mark.unit
    def test_throttle_drives_valve_to_zero_at_dew_point(self) -> None:
        """Supply at the room dew point throttles the valve fully to 0.

        K6 (2026-07-12): the local ramp ends at the dew point itself — the
        heat pump's global ``dew + 2 K`` floor is the working margin, and the
        local layer hard-closes only when the supply reaches the actual dew.
        """
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        t_dew = dew_point(26.0, 70.0)
        # Coldest loop supply AT the dew point -> gap <= 0 -> factor 0.
        loops = (LoopInput(None, t_dew, None),)
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=23.0,
                room_temperature_c=26.0,
                humidity_pct=70.0,
                loops=loops,
            ),
            dt_seconds=300.0,
        )
        assert out.report.dew_throttle_factor == pytest.approx(0.0)
        assert out.valve_position_pct == pytest.approx(0.0)
        assert "s2_throttle" in out.report.flags

    @pytest.mark.unit
    def test_throttle_partial_inside_pump_floor_band(self) -> None:
        """A supply between the dew point and the pump floor ramps the valve.

        K6 (2026-07-12): gap = 1 K with margin 2 / ramp 2 sits mid-ramp
        (factor 0.5) — under the OLD stacked semantics this was a hard 0.
        """
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        t_dew = dew_point(26.0, 70.0)
        loops = (LoopInput(None, t_dew + 1.0, None),)
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=23.0,
                room_temperature_c=26.0,
                humidity_pct=70.0,
                loops=loops,
            ),
            dt_seconds=300.0,
        )
        assert out.report.dew_throttle_factor == pytest.approx(0.5)
        assert "s2_throttle" not in out.report.flags

    @pytest.mark.unit
    def test_throttle_open_when_supply_far_above_dew(self) -> None:
        """Supply well above the dew point leaves the valve un-throttled."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        t_dew = dew_point(24.0, 55.0)
        loops = (LoopInput(None, t_dew + 5.0, None),)
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=23.0,
                room_temperature_c=24.0,
                humidity_pct=55.0,
                loops=loops,
            ),
            dt_seconds=300.0,
        )
        assert out.report.dew_throttle_factor == pytest.approx(1.0)
        assert out.valve_position_pct > 0.0
        assert "s2_throttle" not in out.report.flags

    @pytest.mark.unit
    def test_throttle_conservative_when_humidity_missing(self) -> None:
        """Missing humidity in cooling is conservative: factor 0 + S2 flag.

        The local-layer flag is ``"s2_throttle"`` since 2026-07-12 (B7);
        ``"s2_condensation"`` now belongs exclusively to the hard-safety rule.
        """
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        loops = (LoopInput(None, 18.0, None),)
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=23.0,
                room_temperature_c=26.0,
                humidity_pct=None,
                loops=loops,
            ),
            dt_seconds=300.0,
        )
        assert out.report.dew_throttle_factor == pytest.approx(0.0)
        assert out.valve_position_pct == pytest.approx(0.0)
        assert "s2_throttle" in out.report.flags


class TestGlobalSafeDewPoint:
    """BuildingController global safe dew-point aggregation."""

    @pytest.mark.unit
    def test_global_dew_point_is_max_over_cooled_plus_margin(self) -> None:
        """Global safe dew point == max eligible room dew point + fixed margin."""
        configs = {name: ControllerConfig() for name in ("a", "b", "c", "d")}
        building = BuildingController(configs)
        loops = (LoopInput(None, 18.0, None),)
        inputs = {
            "a": make_inputs(
                mode=Mode.COOLING,
                room_temperature_c=25.0,
                humidity_pct=60.0,
                loops=loops,
            ),
            "b": make_inputs(
                mode=Mode.COOLING,
                room_temperature_c=27.0,
                humidity_pct=70.0,
                loops=loops,
            ),
            "c": make_inputs(  # heating -> excluded
                mode=Mode.HEATING,
                room_temperature_c=20.0,
                humidity_pct=50.0,
            ),
            "d": make_inputs(  # cooling but not participating -> excluded
                mode=Mode.COOLING,
                room_temperature_c=30.0,
                humidity_pct=80.0,
                cooling_enabled=False,
                loops=loops,
            ),
        }
        out = building.step(inputs, dt_seconds=300.0)

        expected = (
            max(dew_point(25.0, 60.0), dew_point(27.0, 70.0)) + GLOBAL_SAFE_DEW_MARGIN_K
        )
        assert out.global_safe_dew_point_c == pytest.approx(expected)
        assert GLOBAL_SAFE_DEW_MARGIN_K == pytest.approx(2.0)  # noqa: SIM300

    @pytest.mark.unit
    def test_global_dew_point_none_without_eligible_room(self) -> None:
        """No cooled/humid room yields a ``None`` global safe dew point."""
        building = BuildingController({"a": ControllerConfig()})
        inputs = {
            "a": make_inputs(mode=Mode.HEATING, room_temperature_c=20.0),
        }
        out = building.step(inputs, dt_seconds=300.0)
        assert out.global_safe_dew_point_c is None

    @pytest.mark.unit
    def test_building_never_raises_on_unknown_room(self) -> None:
        """An input for a room with no controller degrades safely, no raise."""
        building = BuildingController({"a": ControllerConfig()})
        inputs = {
            "a": make_inputs(room_temperature_c=20.0),
            "ghost": make_inputs(room_temperature_c=20.0),
        }
        out = building.step(inputs, dt_seconds=300.0)
        assert out.rooms["ghost"].valve_position_pct == pytest.approx(0.0)
        assert "unknown_room" in out.rooms["ghost"].report.flags


class TestReportPopulated:
    """The under-the-hood report exposes every decision field."""

    @pytest.mark.unit
    def test_active_heating_report_fields_populated(self) -> None:
        """A heating step fills error, trend, terms, raw valve and explanation."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        # Prime the trend by stepping twice.
        controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=18.0),
            dt_seconds=300.0,
        )
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=18.5),
            dt_seconds=300.0,
        )
        report = out.report
        assert report.error_c == pytest.approx(21.0 - 18.5)
        assert report.trend_c_per_h is not None
        assert report.trend_c_per_h > 0.0
        assert report.p_term > 0.0
        assert isinstance(report.raw_valve_pct, float)
        assert report.integrator_frozen is False
        assert isinstance(report.flags, tuple)
        assert report.explanation != ""

    @pytest.mark.unit
    def test_integrator_frozen_when_hp_inactive(self) -> None:
        """``hp_active_for_ufh is False`` freezes the integrator this step."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                setpoint_c=21.0,
                room_temperature_c=19.0,
                hp_active_for_ufh=False,
            ),
            dt_seconds=300.0,
        )
        assert out.report.integrator_frozen is True
        assert out.report.i_term == pytest.approx(0.0)

    @pytest.mark.unit
    def test_report_round_trips_to_dict(self) -> None:
        """The report serialises to plain JSON-friendly primitives."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(setpoint_c=21.0, room_temperature_c=19.0),
            dt_seconds=300.0,
        )
        data = out.to_dict()
        assert isinstance(data["valve_position_pct"], float)
        assert isinstance(data["report"]["flags"], list)
        assert data["fast_source"]["mode"] in {"off", "heating", "cooling"}
        assert data["report"]["room_temperature_c"] == pytest.approx(19.0)


class TestRoomTemperatureEcho:
    """The report echoes the measured room temperature on every path."""

    @pytest.mark.unit
    def test_active_heating_echoes_measurement(self) -> None:
        """The HEATING/COOLING PID path echoes the measured room temperature."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(mode=Mode.HEATING, room_temperature_c=19.3),
            dt_seconds=300.0,
        )
        assert out.report.room_temperature_c == pytest.approx(19.3)

    @pytest.mark.unit
    def test_off_path_echoes_measurement(self) -> None:
        """The OFF path echoes the measured room temperature."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(mode=Mode.OFF, room_temperature_c=20.7),
            dt_seconds=300.0,
        )
        assert out.report.room_temperature_c == pytest.approx(20.7)

    @pytest.mark.unit
    def test_transitional_path_echoes_measurement(self) -> None:
        """The TRANSITIONAL path echoes the measured room temperature."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(mode=Mode.TRANSITIONAL, room_temperature_c=22.1),
            dt_seconds=300.0,
        )
        assert out.report.room_temperature_c == pytest.approx(22.1)

    @pytest.mark.unit
    def test_cooling_disabled_path_echoes_measurement(self) -> None:
        """The cooling-disabled path echoes the measured room temperature."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=23.0,
                room_temperature_c=25.4,
                cooling_enabled=False,
            ),
            dt_seconds=300.0,
        )
        assert "cooling_disabled" in out.report.flags
        assert out.report.room_temperature_c == pytest.approx(25.4)

    @pytest.mark.unit
    def test_sensor_lost_path_reports_none(self) -> None:
        """A lost sensor reports ``room_temperature_c`` as ``None``, not a fake."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(room_temperature_c=None),
            dt_seconds=300.0,
        )
        assert "sensor_lost" in out.report.flags
        assert out.report.room_temperature_c is None

    @pytest.mark.unit
    def test_unknown_room_echoes_measurement(self) -> None:
        """An unknown room echoes its input measurement into the report."""
        building = BuildingController({"a": ControllerConfig()})
        out = building.step(
            {"ghost": make_inputs(room_temperature_c=18.2)},
            dt_seconds=300.0,
        )
        report = out.rooms["ghost"].report
        assert "unknown_room" in report.flags
        assert report.room_temperature_c == pytest.approx(18.2)


class TestDewExcludedReason:
    """Classification of a room's safe-dew-point eligibility (one logic)."""

    @pytest.mark.unit
    def test_eligible_cooling_room_has_none_reason(self) -> None:
        """A cooling, humid, cooling-enabled room is eligible (reason None)."""
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=23.0,
            room_temperature_c=25.0,
            humidity_pct=60.0,
        )
        assert classify_dew_eligibility(inputs) is None
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(inputs, dt_seconds=300.0)
        assert out.report.dew_excluded_reason is None

    @pytest.mark.unit
    def test_not_cooling_mode_reason(self) -> None:
        """A heating room is excluded with ``not_cooling_mode``."""
        inputs = make_inputs(
            mode=Mode.HEATING, room_temperature_c=20.0, humidity_pct=55.0
        )
        assert classify_dew_eligibility(inputs) == "not_cooling_mode"
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(inputs, dt_seconds=300.0)
        assert out.report.dew_excluded_reason == "not_cooling_mode"

    @pytest.mark.unit
    def test_cooling_disabled_reason(self) -> None:
        """A cooling-opt-out room is excluded with ``cooling_disabled``."""
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=23.0,
            room_temperature_c=25.0,
            humidity_pct=60.0,
            cooling_enabled=False,
        )
        assert classify_dew_eligibility(inputs) == "cooling_disabled"
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(inputs, dt_seconds=300.0)
        assert out.report.dew_excluded_reason == "cooling_disabled"

    @pytest.mark.unit
    def test_no_temperature_reason(self) -> None:
        """A cooling room with a lost sensor is excluded with ``no_temperature``."""
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=23.0,
            room_temperature_c=None,
            humidity_pct=60.0,
        )
        assert classify_dew_eligibility(inputs) == "no_temperature"
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(inputs, dt_seconds=300.0)
        assert out.report.dew_excluded_reason == "no_temperature"

    @pytest.mark.unit
    def test_no_humidity_reason(self) -> None:
        """A cooling room without humidity is excluded with ``no_humidity``."""
        for rh in (None, 0.0):
            inputs = make_inputs(
                mode=Mode.COOLING,
                setpoint_c=23.0,
                room_temperature_c=25.0,
                humidity_pct=rh,
            )
            assert classify_dew_eligibility(inputs) == "no_humidity"
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=23.0,
                room_temperature_c=25.0,
                humidity_pct=None,
            ),
            dt_seconds=300.0,
        )
        assert out.report.dew_excluded_reason == "no_humidity"

    @pytest.mark.unit
    def test_classifier_matches_eligible_dew_point(self) -> None:
        """The classifier and the building's dew-point gate agree per room."""
        building = BuildingController({"salon": ControllerConfig()})
        # Eligible cooling room contributes to the global maximum.
        eligible = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=23.0,
            room_temperature_c=26.0,
            humidity_pct=70.0,
        )
        out = building.step({"salon": eligible}, dt_seconds=300.0)
        assert out.global_safe_dew_point_c is not None
        # Excluded (no humidity) yields no global value.
        excluded = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=23.0,
            room_temperature_c=26.0,
            humidity_pct=None,
        )
        out2 = building.step({"salon": excluded}, dt_seconds=300.0)
        assert out2.global_safe_dew_point_c is None


class TestTrendFilter:
    """S10 (2026-07-09): the trend is EMA-filtered with a 60 s sample floor."""

    @pytest.mark.unit
    def test_fast_recompute_holds_trend(self) -> None:
        """A 2 s debounced recompute must NOT explode the trend estimate."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        warmed = controller.step(make_inputs(room_temperature_c=19.5), dt_seconds=300.0)
        held_trend = warmed.report.trend_c_per_h
        assert held_trend is not None
        assert held_trend > 0.0

        # 2 s later a sensor tick of +0.1 K arrives: raw would be 180 K/h.
        recompute = controller.step(
            make_inputs(room_temperature_c=19.6), dt_seconds=2.0
        )
        assert recompute.report.trend_c_per_h == pytest.approx(held_trend)

    @pytest.mark.unit
    def test_trend_is_smoothed_not_raw(self) -> None:
        """One raw sample moves the filtered trend only by the EMA fraction."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        # Raw sample: +0.5 K over 300 s = +6 K/h; EMA alpha = 1 - exp(-1/3).
        out = controller.step(make_inputs(room_temperature_c=19.5), dt_seconds=300.0)
        alpha = 1.0 - math.exp(-300.0 / 900.0)
        assert out.report.trend_c_per_h == pytest.approx(alpha * 6.0, rel=1e-6)

    @pytest.mark.unit
    def test_sensor_loss_resets_filter(self) -> None:
        """A sensor gap invalidates the trend: recovery restarts from 0."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        controller.step(make_inputs(room_temperature_c=19.5), dt_seconds=300.0)
        controller.step(make_inputs(room_temperature_c=None), dt_seconds=300.0)
        recovered = controller.step(
            make_inputs(room_temperature_c=20.5), dt_seconds=300.0
        )
        assert recovered.report.trend_c_per_h == pytest.approx(0.0)


class TestIntegratorHygiene:
    """S1/S2 (2026-07-09): dew-throttle freeze, mode reset, inactivity decay."""

    def _throttled_cooling(self, temp: float) -> RoomInputs:
        """Cooling inputs whose supply sits INSIDE the dew ramp (factor < 1)."""
        # Room 26 degC / RH 60 % -> dew ~ 17.9; K6 semantics (margin 2,
        # ramp 2 -> ramp spans gap 0..2): a supply of 19 degC gives gap ~1.1,
        # mid-ramp (factor ~ 0.55) - throttle active, not closed.
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=temp,
            humidity_pct=60.0,
            loops=(LoopInput(None, 19.0, None),),
        )

    @pytest.mark.unit
    def test_integrator_frozen_under_dew_throttle(self) -> None:
        """An active S2 throttle (< 1) freezes the integrator (no windup)."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._throttled_cooling(26.0), dt_seconds=300.0)
        assert 0.0 < out.report.dew_throttle_factor < 1.0
        assert out.report.integrator_frozen is True
        assert out.report.i_term == pytest.approx(0.0)

    @pytest.mark.unit
    def test_mode_change_resets_integrator(self) -> None:
        """HEATING -> COOLING clears the accumulated heating integral."""
        controller = RoomController(ControllerConfig(), name="salon")
        for _ in range(5):
            heated = controller.step(
                make_inputs(room_temperature_c=19.0), dt_seconds=300.0
            )
        assert heated.report.i_term > 0.0

        cooled = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=24.1,
                humidity_pct=45.0,
                loops=(LoopInput(None, 20.0, None),),
            ),
            dt_seconds=300.0,
        )
        # The first cooling cycle must not inherit the heating-scale integral.
        assert abs(cooled.report.i_term) < 0.1

    @pytest.mark.unit
    def test_long_transitional_decays_integrator(self) -> None:
        """More than 12 h in TRANSITIONAL clears the stored integral."""
        controller = RoomController(ControllerConfig(), name="salon")
        for _ in range(5):
            controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)

        parked = make_inputs(mode=Mode.TRANSITIONAL, room_temperature_c=21.0)
        out = controller.step(parked, dt_seconds=300.0)
        assert out.report.i_term > 0.0  # shortly after: the integral survives
        out = controller.step(parked, dt_seconds=13.0 * 3600.0)
        assert out.report.i_term == pytest.approx(0.0)


class TestSaturatedSemantics:
    """control-F8 (2026-07-09): an S2 zero is throttling, not saturation."""

    @pytest.mark.unit
    def test_s2_zero_is_not_saturated(self) -> None:
        """A valve closed purely by the dew throttle reports saturated=False.

        Missing humidity makes the LOCAL throttle conservatively 0 while the
        hard-safety S2 rule cannot evaluate (its condition needs humidity), so
        the observable zero comes from the throttle alone: since 2026-07-09
        (control-F8) that is NOT reported as PI saturation.
        """
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.COOLING,
                setpoint_c=24.0,
                room_temperature_c=27.0,
                humidity_pct=None,
                loops=(LoopInput(None, 18.0, None),),
            ),
            dt_seconds=300.0,
        )
        assert out.valve_position_pct == 0.0
        assert out.report.dew_throttle_factor == 0.0
        assert "s2_throttle" in out.report.flags
        assert out.report.saturated is False
