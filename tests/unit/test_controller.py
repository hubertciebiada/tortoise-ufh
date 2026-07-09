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
        assert out.fast_source.target_temperature_c == pytest.approx(21.0)

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
