"""Unit tests for :mod:`tortoise_ufh.controller`.

Exercises the per-room :class:`~tortoise_ufh.controller.RoomController` and the
whole-building :class:`~tortoise_ufh.controller.BuildingController` against the
frozen black-box contract:

* Missing room temperature safe-degrade (hold last valve, fast source OFF,
  ``sensor_lost`` flag, PID not run).
* The deadband suppresses integral growth.
* The heating valve floor is applied only when calling for heat.
* The cooling local dew-point throttle (S2) drives the valve toward 0 near the
  dew point and raises ``s2_condensation``.
* The fast source (split) engages only beyond ``boost_offset_c`` and respects
  the minimum ON/OFF dwell timers.
* Anti priority-inversion: enabling the split never lowers the computed valve.
* ``BuildingController`` global safe dew point equals ``max_over_cooled(T_dew)``
  plus the fixed margin, and is ``None`` with no eligible room.
* The under-the-hood :class:`~tortoise_ufh.models.RoomReport` fields are
  populated.

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
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
    RoomOutputs,
)


def make_inputs(
    *,
    mode: Mode = Mode.HEATING,
    setpoint_c: float = 21.0,
    room_temperature_c: float | None = 20.0,
    humidity_pct: float | None = None,
    outdoor_temperature_c: float | None = None,
    loops: tuple[LoopInput, ...] = (),
    fast_source_kind: FastSourceKind = FastSourceKind.NONE,
    hp_active_for_ufh: bool | None = None,
    cooling_enabled: bool = True,
) -> RoomInputs:
    """Build a :class:`RoomInputs` with test-friendly keyword defaults.

    Args:
        mode: Operating mode. Defaults to ``HEATING``.
        setpoint_c: Target temperature [degC]. Defaults to 21.0.
        room_temperature_c: Measured room temperature [degC], or ``None``.
        humidity_pct: Relative humidity [%], or ``None``.
        outdoor_temperature_c: Outdoor temperature [degC], or ``None``.
        loops: UFH loop probes/valve feedback.
        fast_source_kind: Kind of fast source available.
        hp_active_for_ufh: Heat-pump availability for UFH (freeze flag), or
            ``None``.
        cooling_enabled: Whether the room participates in cooling.

    Returns:
        A validated :class:`RoomInputs`.
    """
    return RoomInputs(
        mode=mode,
        setpoint_c=setpoint_c,
        room_temperature_c=room_temperature_c,
        humidity_pct=humidity_pct,
        outdoor_temperature_c=outdoor_temperature_c,
        loops=loops,
        fast_source_kind=fast_source_kind,
        hp_active_for_ufh=hp_active_for_ufh,
        cooling_enabled=cooling_enabled,
    )


class TestSensorLost:
    """Safe-degrade behaviour when the room temperature sensor is lost."""

    @pytest.mark.unit
    def test_missing_temp_holds_last_valve_and_flags_sensor_lost(self) -> None:
        """A lost room sensor holds the last valve, forces fast OFF, flags loss."""
        controller = RoomController(ControllerConfig(), name="salon")
        # First a normal heating step establishes a non-trivial valve position.
        warm = controller.step(make_inputs(room_temperature_c=15.0), dt_seconds=300.0)
        held = warm.valve_position_pct
        assert held > ControllerConfig().valve_floor_pct

        lost = controller.step(make_inputs(room_temperature_c=None), dt_seconds=300.0)
        assert lost.valve_position_pct == held
        assert lost.fast_source.on is False
        assert lost.fast_source.mode is FastSourceMode.OFF
        assert "sensor_lost" in lost.report.flags
        assert lost.report.error_c is None
        assert lost.report.integrator_frozen is True

    @pytest.mark.unit
    def test_missing_temp_first_step_holds_valve_floor(self) -> None:
        """With no prior step, the held valve defaults to the heating floor."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        out = controller.step(make_inputs(room_temperature_c=None), dt_seconds=300.0)
        assert out.valve_position_pct == cfg.valve_floor_pct
        assert "sensor_lost" in out.report.flags

    @pytest.mark.unit
    def test_missing_temp_turns_running_fast_source_off(self) -> None:
        """A lost sensor forces an already-running split OFF (safety)."""
        controller = RoomController(ControllerConfig(), name="salon")
        engaged = controller.step(
            make_inputs(
                room_temperature_c=18.0,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert engaged.fast_source.on is True

        lost = controller.step(
            make_inputs(
                room_temperature_c=None,
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert lost.fast_source.on is False


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
    def test_throttle_drives_valve_to_zero_near_dew_point(self) -> None:
        """Supply within the margin of the dew point throttles the valve to 0."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        t_dew = dew_point(26.0, 70.0)
        # Coldest loop supply only 1 K above dew -> gap < margin -> factor 0.
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
        assert out.report.dew_throttle_factor == pytest.approx(0.0)
        assert out.valve_position_pct == pytest.approx(0.0)
        assert "s2_condensation" in out.report.flags

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
        assert "s2_condensation" not in out.report.flags

    @pytest.mark.unit
    def test_throttle_conservative_when_humidity_missing(self) -> None:
        """Missing humidity in cooling is conservative: factor 0 + S2 flag."""
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
        assert "s2_condensation" in out.report.flags


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


class TestSensorLostCooling:
    """C2 (2026-07-09): sensor loss in COOLING closes the valve, never freezes."""

    def _cooling_inputs(
        self, temp: float | None, *, humidity: float | None = 50.0
    ) -> RoomInputs:
        """Cooling inputs with a supply probe far above the dew point."""
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=temp,
            humidity_pct=humidity,
            loops=(LoopInput(None, 20.0, None),),
        )

    @pytest.mark.unit
    def test_missing_temp_in_cooling_closes_valve(self) -> None:
        """A seeded cooling room with an open valve parks at 0 on sensor loss."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Warm room (27 > 24 setpoint) opens the cooling valve.
        open_step = controller.step(self._cooling_inputs(27.0), dt_seconds=300.0)
        assert open_step.valve_position_pct > 0.0

        lost = controller.step(self._cooling_inputs(None), dt_seconds=300.0)
        assert lost.valve_position_pct == 0.0
        assert lost.fast_source.on is False
        assert "sensor_lost" in lost.report.flags

    @pytest.mark.unit
    def test_missing_temp_cooling_cold_start_is_zero(self) -> None:
        """With no prior live step, a cooling room parks at 0 on sensor loss."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(self._cooling_inputs(None), dt_seconds=300.0)
        assert out.valve_position_pct == 0.0
        assert "sensor_lost" in out.report.flags

    @pytest.mark.unit
    def test_cooling_loss_does_not_poison_heating_hold(self) -> None:
        """The cooling 0-park leaves the heating freeze memory untouched."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Healthy heating step establishes a hold position.
        warm = controller.step(make_inputs(room_temperature_c=15.0), dt_seconds=300.0)
        held = warm.valve_position_pct
        assert held > 0.0
        # Sensor loss in cooling parks at 0 ...
        lost_cool = controller.step(self._cooling_inputs(None), dt_seconds=300.0)
        assert lost_cool.valve_position_pct == 0.0
        # ... but a later heating-mode loss still freezes the healthy position.
        lost_heat = controller.step(
            make_inputs(room_temperature_c=None), dt_seconds=300.0
        )
        assert lost_heat.valve_position_pct == pytest.approx(held)


class TestSafetyOverrideStateSync:
    """S5 (2026-07-09): the safety override keeps controller state honest."""

    @pytest.mark.unit
    def test_safety_close_does_not_poison_sensor_lost_hold(self) -> None:
        """An S1 trip must not leave 0 % as the sensor-lost freeze position."""
        controller = RoomController(ControllerConfig(), name="salon")
        hot_supply = (LoopInput(None, 45.0, None),)
        ok_supply = (LoopInput(None, 30.0, None),)

        # Healthy heating regulation (supply fine) establishes the hold.
        healthy = controller.step(
            make_inputs(room_temperature_c=15.0, loops=ok_supply), dt_seconds=300.0
        )
        assert healthy.valve_position_pct > 0.0

        # S1 floor-overheat trips: the OUTPUT closes the valve ...
        tripped = controller.step(
            make_inputs(room_temperature_c=15.0, loops=hot_supply), dt_seconds=300.0
        )
        assert "s1_floor_overheat" in tripped.report.flags
        assert tripped.valve_position_pct == 0.0
        # ... but the healthy hold memory survives the override.
        assert controller.last_valve_pct > 0.0
        held = controller.last_valve_pct

        # Supply recovers (S1 clears below 38) and the sensor is lost: the
        # freeze holds the healthy position, not the emergency 0.
        lost = controller.step(
            make_inputs(room_temperature_c=None, loops=ok_supply), dt_seconds=300.0
        )
        assert lost.valve_position_pct == pytest.approx(held)
        assert lost.valve_position_pct > 0.0

    @pytest.mark.unit
    def test_safety_force_on_syncs_split_machine(self) -> None:
        """S3 force-ON registers in the dwell machine: no instant OFF later."""
        controller = RoomController(ControllerConfig(), name="salon")
        # Frost trip (room 4 degC): emergency heat forces the split ON.
        frozen = controller.step(
            make_inputs(room_temperature_c=4.0, fast_source_kind=FastSourceKind.SPLIT),
            dt_seconds=300.0,
        )
        assert "s3_emergency_heat" in frozen.report.flags
        assert frozen.fast_source.on is True
        assert frozen.fast_source.mode is FastSourceMode.HEATING
        # The machine is synced: the min-ON lock is armed for the report.
        assert frozen.report.fast_dwell_remaining_s == pytest.approx(600.0)

        # The room recovers into the comfort band (S3 cleared, no demand): the
        # min-ON dwell keeps the just-started compressor running instead of an
        # abrupt OFF two seconds after the safety releases.
        recovered = controller.step(
            make_inputs(room_temperature_c=21.0, fast_source_kind=FastSourceKind.SPLIT),
            dt_seconds=300.0,
        )
        assert recovered.fast_source.on is True
        assert "fast_source_min_runtime" in recovered.report.flags


class TestSafetyValveVsAirSource:
    """S7 (2026-07-09): closing the valve never silences the air-side source."""

    @pytest.mark.unit
    def test_s1_with_s3_keeps_split_heating(self) -> None:
        """Frost + overheated water: valve closed, split still heats the air."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=4.0,
                loops=(LoopInput(None, 45.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert "s1_floor_overheat" in out.report.flags
        assert "s3_emergency_heat" in out.report.flags
        # Water side: S1 wins, the valve is closed.
        assert out.valve_position_pct == 0.0
        # Air side: S3 wins, the split heats.
        assert out.fast_source.on is True
        assert out.fast_source.mode is FastSourceMode.HEATING

    @pytest.mark.unit
    def test_s1_alone_still_forces_split_off(self) -> None:
        """S1 without an emergency keeps the fast source released."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=20.0,
                loops=(LoopInput(None, 45.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert "s1_floor_overheat" in out.report.flags
        assert out.valve_position_pct == 0.0
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_s3_alone_opens_valve_fully(self) -> None:
        """S3 without a CLOSE_VALVE rule still opens the floor fully."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                room_temperature_c=4.0,
                loops=(LoopInput(None, 30.0, None),),
                fast_source_kind=FastSourceKind.SPLIT,
            ),
            dt_seconds=300.0,
        )
        assert "s3_emergency_heat" in out.report.flags
        assert out.valve_position_pct == 100.0
        assert out.fast_source.on is True


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
        # Room 26 degC / RH 60 % -> dew ~ 17.9; margin 2, ramp 2: a supply of
        # 21 degC sits mid-ramp (factor ~ 0.5) - throttle active, not closed.
        return make_inputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=temp,
            humidity_pct=60.0,
            loops=(LoopInput(None, 21.0, None),),
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
        assert "s2_condensation" in out.report.flags
        assert out.report.saturated is False


class TestWatchdogNeutral:
    """S6 (2026-07-09): S5 fed by the adapter age, action = neutral position."""

    @pytest.mark.unit
    def test_stale_age_drives_neutral_position_heating(self) -> None:
        """Age > 15 min trips S5: heating parks at the valve floor."""
        cfg = ControllerConfig()
        controller = RoomController(cfg, name="salon")
        # Healthy cycle establishes a high valve (cold room).
        busy = controller.step(make_inputs(room_temperature_c=17.0), dt_seconds=300.0)
        assert busy.valve_position_pct > cfg.valve_floor_pct

        stale = RoomInputs(
            mode=Mode.HEATING,
            setpoint_c=21.0,
            room_temperature_c=17.0,
            last_update_age_minutes=20.0,
        )
        out = controller.step(stale, dt_seconds=300.0)
        assert "s5_watchdog" in out.report.flags
        assert out.valve_position_pct == pytest.approx(cfg.valve_floor_pct)
        assert out.fast_source.on is False

    @pytest.mark.unit
    def test_stale_age_closes_valve_cooling(self) -> None:
        """In COOLING the S5 neutral position is 0 (no blind chilled water)."""
        controller = RoomController(ControllerConfig(), name="salon")
        stale = RoomInputs(
            mode=Mode.COOLING,
            setpoint_c=24.0,
            room_temperature_c=27.0,
            humidity_pct=45.0,
            loops=(LoopInput(None, 22.0, None),),
            last_update_age_minutes=20.0,
        )
        out = controller.step(stale, dt_seconds=300.0)
        assert "s5_watchdog" in out.report.flags
        assert out.valve_position_pct == 0.0

    @pytest.mark.unit
    def test_fresh_age_keeps_s5_quiet(self) -> None:
        """The default age 0.0 never trips S5 (compat for old callers)."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(make_inputs(room_temperature_c=19.0), dt_seconds=300.0)
        assert "s5_watchdog" not in out.report.flags


class TestSensorLostRoomsCounter:
    """safety-F13 (2026-07-09): building-level degraded-rooms counter."""

    @pytest.mark.unit
    def test_counts_sensor_lost_rooms(self) -> None:
        """The building output counts rooms currently flagged sensor_lost."""
        building = BuildingController(
            {"a": ControllerConfig(), "b": ControllerConfig()}
        )
        outputs = building.step(
            {
                "a": make_inputs(room_temperature_c=None),
                "b": make_inputs(room_temperature_c=20.0),
            },
            dt_seconds=300.0,
        )
        assert outputs.sensor_lost_rooms == 1
        assert outputs.to_dict()["sensor_lost_rooms"] == 1

        outputs = building.step(
            {
                "a": make_inputs(room_temperature_c=20.0),
                "b": make_inputs(room_temperature_c=20.0),
            },
            dt_seconds=300.0,
        )
        assert outputs.sensor_lost_rooms == 0
