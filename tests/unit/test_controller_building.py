"""Unit tests for ``BuildingController`` plumbing and module-level helpers.

Pins the building-orchestrator contracts that the per-room suites do not
reach:

* ``_passive_report`` — every non-PI result builder shares the same
  PI-inactive commons (``p_term = trend_term = feedforward_term = 0.0``,
  ``dew_throttle_factor = 1.0``, ``integrator_frozen = True``,
  ``valve_floor_applied = False``) and echoes the caller's error, trend and
  dew point verbatim.
* ``classify_dew_eligibility`` — a humidity reading just above 0 % is still
  a usable reading (the exclusion boundary is ``rh <= 0.0``).
* ``BuildingController.step`` — ``dt_seconds`` validation and forwarding,
  the 300 s default, unknown-room degradation without breaking the
  remaining rooms, and the K5 controller-error degrade echo.
* ``_unknown_room_output`` — the closed-valve, fast-OFF safe output.
* ``_band_excess_k`` — the group arbiter's demand strength
  ``max(0, |error| - deadband)``.

Units: temperatures in degC, ``dt_seconds`` in seconds. This module never
imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    BuildingController,
    RoomController,
    classify_dew_eligibility,
)
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.models import (
    FastSourceMode,
    Mode,
    RoomInputs,
    RoomOutputs,
)
from tests.unit.conftest import make_inputs

pytestmark = pytest.mark.unit


class TestPassiveReportCommons:
    """The passive-path report shares PI-inactive commons across builders."""

    def test_off_mode_report_pins_passive_commons(self) -> None:
        """An OFF cycle echoes error/trend/dew and keeps the PI terms zeroed."""
        controller = RoomController(ControllerConfig(), name="salon")
        out = controller.step(
            make_inputs(
                mode=Mode.OFF,
                setpoint_c=21.0,
                room_temperature_c=20.0,
                humidity_pct=50.0,
            ),
            dt_seconds=300.0,
        )
        report = out.report
        assert report.error_c == pytest.approx(1.0)
        assert report.trend_c_per_h == pytest.approx(0.0)
        assert report.room_dew_point_c == pytest.approx(dew_point(20.0, 50.0))
        assert report.p_term == 0.0
        assert report.trend_term == 0.0
        assert report.feedforward_term == 0.0
        assert report.dew_throttle_factor == 1.0
        assert report.integrator_frozen is True
        assert report.valve_floor_applied is False


class TestDewEligibilityBoundary:
    """The humidity exclusion boundary is ``rh <= 0.0``, nothing higher."""

    def test_positive_sub_percent_humidity_is_usable(self) -> None:
        """A 0.5 % reading is a valid (if dry) reading: the room is eligible."""
        inputs = make_inputs(
            mode=Mode.COOLING,
            setpoint_c=23.0,
            room_temperature_c=25.0,
            humidity_pct=0.5,
        )
        assert classify_dew_eligibility(inputs) is None
        building = BuildingController({"salon": ControllerConfig()})
        out = building.step({"salon": inputs}, dt_seconds=300.0)
        assert out.global_safe_dew_point_c is not None


class TestUnknownRoomOutput:
    """A room without a configured controller degrades to a safe output."""

    def test_unknown_room_output_is_closed_and_flagged(self) -> None:
        """Valve 0, fast source OFF, ``unknown_room`` flag, PI-inert report."""
        out = BuildingController._unknown_room_output("piwnica", 18.5)
        assert out.valve_position_pct == 0.0
        assert out.fast_source.on is False
        assert out.fast_source.mode is FastSourceMode.OFF
        report = out.report
        assert report.flags == ("unknown_room",)
        assert report.error_c is None
        assert report.trend_c_per_h is None
        assert report.room_dew_point_c is None
        assert report.i_term == 0.0
        assert report.raw_valve_pct == 0.0
        assert report.saturated is False
        assert report.valve_floor_applied is False
        assert report.p_term == 0.0
        assert report.trend_term == 0.0
        assert report.feedforward_term == 0.0
        assert report.dew_throttle_factor == 1.0
        assert report.integrator_frozen is True
        assert report.room_temperature_c == pytest.approx(18.5)


class _RaisingController:
    """Room-controller stand-in whose ``step`` always raises (K5 path)."""

    def __init__(self, name: str) -> None:
        """Store the room name and a held valve position.

        Args:
            name: The room name surfaced in the degrade explanation.
        """
        self.name = name
        self.last_valve_pct = 42.0

    def step(self, inputs: RoomInputs, *, dt_seconds: float = 300.0) -> RoomOutputs:
        """Raise like a faulted regulator.

        Args:
            inputs: The room inputs (unused).
            dt_seconds: Elapsed time [s] (unused).

        Raises:
            ValueError: Always.
        """
        raise ValueError("boom")


class TestBuildingStep:
    """``BuildingController.step`` validation, dt handling and degradation."""

    def test_non_positive_dt_raises(self) -> None:
        """``dt_seconds <= 0`` is a ValueError naming the violated contract."""
        building = BuildingController({"salon": ControllerConfig()})
        for bad in (0.0, -5.0):
            with pytest.raises(ValueError, match="dt_seconds must be > 0"):
                building.step({"salon": make_inputs()}, dt_seconds=bad)

    def test_sub_second_dt_is_accepted(self) -> None:
        """Any strictly positive dt is valid — the floor is 0, not 1 s."""
        building = BuildingController({"salon": ControllerConfig()})
        out = building.step({"salon": make_inputs()}, dt_seconds=0.5)
        assert "salon" in out.rooms

    def test_default_dt_is_300_seconds(self) -> None:
        """Omitting ``dt_seconds`` behaves exactly like passing 300 s."""
        default = BuildingController({"salon": ControllerConfig()})
        explicit = BuildingController({"salon": ControllerConfig()})
        out_default = default.step({"salon": make_inputs(room_temperature_c=19.0)})
        out_explicit = explicit.step(
            {"salon": make_inputs(room_temperature_c=19.0)}, dt_seconds=300.0
        )
        i_default = out_default.rooms["salon"].report.i_term
        i_explicit = out_explicit.rooms["salon"].report.i_term
        assert i_default > 0.0  # the integral moved — dt is observable here
        assert i_default == pytest.approx(i_explicit, rel=1e-9)

    def test_dt_is_forwarded_to_room_controllers(self) -> None:
        """A 600 s building step integrates twice as much as a 300 s one."""
        slow = BuildingController({"salon": ControllerConfig()})
        fast = BuildingController({"salon": ControllerConfig()})
        i_slow = (
            slow.step({"salon": make_inputs(room_temperature_c=19.0)}, dt_seconds=600.0)
            .rooms["salon"]
            .report.i_term
        )
        i_fast = (
            fast.step({"salon": make_inputs(room_temperature_c=19.0)}, dt_seconds=300.0)
            .rooms["salon"]
            .report.i_term
        )
        assert i_fast > 0.0
        assert i_slow == pytest.approx(2.0 * i_fast, rel=0.02)

    def test_unknown_room_does_not_break_remaining_rooms(self) -> None:
        """An unknown room degrades flagged; the known room still regulates."""
        building = BuildingController({"salon": ControllerConfig()})
        out = building.step(
            {
                "ghost": make_inputs(room_temperature_c=18.0),
                "salon": make_inputs(room_temperature_c=19.0),
            },
            dt_seconds=300.0,
        )
        assert "unknown_room" in out.rooms["ghost"].report.flags
        assert out.rooms["ghost"].valve_position_pct == 0.0
        assert "salon" in out.rooms
        assert out.rooms["salon"].valve_position_pct > 0.0
        assert "unknown_room" not in out.rooms["salon"].report.flags

    def test_controller_error_degrades_and_echoes_room_temperature(self) -> None:
        """A raising room controller degrades flagged, fast-OFF, temp echoed."""
        building = BuildingController({"salon": ControllerConfig()})
        building._controllers["salon"] = _RaisingController("salon")  # type: ignore[assignment]
        out = building.step(
            {
                "salon": make_inputs(
                    mode=Mode.COOLING,
                    setpoint_c=23.0,
                    room_temperature_c=25.0,
                    humidity_pct=None,
                )
            },
            dt_seconds=300.0,
        )
        room = out.rooms["salon"]
        assert "controller_error" in room.report.flags
        assert room.valve_position_pct == 0.0
        assert room.fast_source.on is False
        assert room.report.room_temperature_c == pytest.approx(25.0)
        assert room.report.explanation == (
            "Blad regulatora pokoju 'salon': boom. "
            "Zawor 0% (tryb bez grzania), split OFF."
        )

    def test_controller_error_in_heating_holds_and_says_so(self) -> None:
        """HEATING degrade holds the last valve and names it in the text."""
        building = BuildingController({"salon": ControllerConfig()})
        building._controllers["salon"] = _RaisingController("salon")  # type: ignore[assignment]
        out = building.step(
            {"salon": make_inputs(mode=Mode.HEATING, room_temperature_c=20.0)},
            dt_seconds=300.0,
        )
        room = out.rooms["salon"]
        assert room.valve_position_pct == pytest.approx(42.0)
        assert room.report.explanation == (
            "Blad regulatora pokoju 'salon': boom. Zawor trzyma 42%, split OFF."
        )


class TestBandExcess:
    """The group arbiter's demand strength ``max(0, |error| - deadband)``."""

    def test_band_excess_k(self) -> None:
        """Zero without a sensor; |error| - deadband outside the band."""
        building = BuildingController({"salon": ControllerConfig()})
        missing = make_inputs(room_temperature_c=None)
        assert building._band_excess_k(missing, "salon") == 0.0
        # Default deadband 0.3 K: |21 - 19.5| - 0.3 = 1.2 K outside the band.
        outside = make_inputs(setpoint_c=21.0, room_temperature_c=19.5)
        assert building._band_excess_k(outside, "salon") == pytest.approx(1.2)
        # Inside the band the excess clamps to 0.
        inside = make_inputs(setpoint_c=21.0, room_temperature_c=21.1)
        assert building._band_excess_k(inside, "salon") == 0.0
