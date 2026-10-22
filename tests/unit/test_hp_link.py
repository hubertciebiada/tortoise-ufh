"""Unit tests for :mod:`tortoise_ufh.hp_link` (the optional heat-pump link).

Exercises the pure decision logic of the B2 (2026-07-12) opt-in extension:

* ``direction_option`` — the FULL write table (all seven HeishaMon options
  times all four Tortoise modes), the DHW-flag preservation, the hard
  ``"DHW only"`` skip and the never-write-blind rule.
* ``dhw_option`` — every add/remove transition plus the refusals.
* ``cooling_setpoint_c`` — the ``max(base, safe dew point)`` floor.
* ``heating_curve`` — the knob-fed weather curve: its own neutral point and
  min/max clamps (issue #35), independent of the feedforward's ``ff_neutral_c``.
* ``heating_curve_points`` — the panel's curve preview (issue #35).

Units: temperatures in degC; curve slope in K/K. This module never imports
``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.hp_link import (
    HEATING_CURVE_PREVIEW_OUTDOOR_C,
    HEISHAMON_MODE_OPTIONS,
    cooling_setpoint_c,
    dhw_option,
    direction_option,
    heating_curve,
    heating_curve_points,
    round_to_step_c,
)
from custom_components.tortoise_ufh.core.models import Mode


class TestDirectionOption:
    """The mode-direction write table (DHW always preserved)."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            ("Heat only", "Heat only"),  # already in sync
            ("Cool only", "Heat only"),
            ("Auto", "Heat only"),
            ("Heat+DHW", "Heat+DHW"),  # already in sync, DHW kept
            ("Cool+DHW", "Heat+DHW"),  # DHW preserved across the flip
            ("Auto+DHW", "Heat+DHW"),
            ("DHW only", None),  # hard skip: the DHW automation is mid-cycle
            (None, None),  # never write blind
            ("Vendor Weirdness", None),  # unrecognised option: never write
        ],
    )
    def test_heating_table(self, current: str | None, expected: str | None) -> None:
        """HEATING maps every option to its Heat variant (or a skip)."""
        assert direction_option(Mode.HEATING, current) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            ("Heat only", "Cool only"),
            ("Cool only", "Cool only"),
            ("Auto", "Cool only"),
            ("Heat+DHW", "Cool+DHW"),
            ("Cool+DHW", "Cool+DHW"),
            ("Auto+DHW", "Cool+DHW"),
            ("DHW only", None),
            (None, None),
        ],
    )
    def test_cooling_table(self, current: str | None, expected: str | None) -> None:
        """COOLING maps every option to its Cool variant (or a skip)."""
        assert direction_option(Mode.COOLING, current) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize("mode", [Mode.TRANSITIONAL, Mode.OFF])
    @pytest.mark.parametrize("current", [*HEISHAMON_MODE_OPTIONS, None])
    def test_transitional_and_off_never_force(
        self, mode: Mode, current: str | None
    ) -> None:
        """TRANSITIONAL / OFF never force a direction, whatever the pump does."""
        assert direction_option(mode, current) is None

    @pytest.mark.unit
    def test_matching_is_case_insensitive_and_trimmed(self) -> None:
        """A HeishaMon build's spelling quirks still match; output is canonical."""
        assert direction_option(Mode.HEATING, "  cool+dhw ") == "Heat+DHW"
        assert direction_option(Mode.COOLING, "HEAT ONLY") == "Cool only"
        assert direction_option(Mode.HEATING, " dhw ONLY ") is None

    @pytest.mark.unit
    def test_never_returns_dhw_only(self) -> None:
        """No (mode, option) pair ever asks to write "DHW only"."""
        for mode in Mode:
            for current in (*HEISHAMON_MODE_OPTIONS, None, "garbage"):
                assert direction_option(mode, current) != "DHW only"


class TestDhwOption:
    """The manual DHW switch: toggle only the +DHW part, never the direction."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            ("Heat only", "Heat+DHW"),
            ("Cool only", "Cool+DHW"),
            ("Auto", "Auto+DHW"),
            ("Heat+DHW", "Heat+DHW"),  # already on: nothing to change
            ("Cool+DHW", "Cool+DHW"),
            ("Auto+DHW", "Auto+DHW"),
            ("DHW only", "DHW only"),  # already DHW: nothing to change
            (None, None),
            ("garbage", None),
        ],
    )
    def test_add_dhw(self, current: str | None, expected: str | None) -> None:
        """Adding the flag maps each direction to its +DHW variant."""
        assert dhw_option(current, True) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            ("Heat+DHW", "Heat only"),
            ("Cool+DHW", "Cool only"),
            ("Auto+DHW", "Auto"),
            ("Heat only", "Heat only"),  # already off: nothing to change
            ("Cool only", "Cool only"),
            ("Auto", "Auto"),
            ("DHW only", None),  # refusal: no base direction to fall back to
            (None, None),
            ("garbage", None),
        ],
    )
    def test_remove_dhw(self, current: str | None, expected: str | None) -> None:
        """Removing the flag maps each +DHW variant back to its direction."""
        assert dhw_option(current, False) == expected

    @pytest.mark.unit
    def test_case_insensitive_input_canonical_output(self) -> None:
        """Raw select spellings are matched loosely; output stays canonical."""
        assert dhw_option(" heat ONLY ", True) == "Heat+DHW"
        assert dhw_option("auto+dhw", False) == "Auto"


class TestCoolingSetpoint:
    """cooling_setpoint_c = max(base, safe dew point)."""

    @pytest.mark.unit
    def test_no_dew_point_returns_base(self) -> None:
        """Without a global safe dew point the base alone applies."""
        assert cooling_setpoint_c(18.0, None) == pytest.approx(18.0)

    @pytest.mark.unit
    def test_dew_above_base_wins(self) -> None:
        """A safe dew point above the base floors the water setpoint."""
        assert cooling_setpoint_c(18.0, 19.2) == pytest.approx(19.2)

    @pytest.mark.unit
    def test_dew_below_base_keeps_base(self) -> None:
        """A safe dew point below the base leaves the base in charge."""
        assert cooling_setpoint_c(18.0, 15.4) == pytest.approx(18.0)


class TestRoundToStepC:
    """round_to_step_c: quantize a water setpoint to the entity's step grid."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("value_c", "step_c", "expected"),
        [
            # Issue #5 reported case: 16.56 dew-safe target, 1 degC pump step.
            # Round-to-nearest lands it at 17, above the 16.56 floor (NOT 16).
            (16.56, 1.0, 17.0),
            (16.5, 1.0, 17.0),  # exact half rounds UP
            (16.4, 1.0, 16.0),  # nearest is 16 (accepted: dips into the 2 K margin)
            (16.0, 1.0, 16.0),  # already on the grid -> unchanged
            (16.01, 1.0, 16.0),  # nearest is still 16 (round, not ceil)
            (27.35, 0.5, 27.5),  # 0.5 K grid: nearest is 27.5
            (27.0, 0.5, 27.0),  # on grid -> unchanged
            (27.24, 0.5, 27.0),  # below the half-way point of the 0.5 grid
        ],
    )
    def test_round_to_nearest_grid_point(
        self, value_c: float, step_c: float, expected: float
    ) -> None:
        """The value snaps to the nearest multiple of the step (half up)."""
        assert round_to_step_c(value_c, step_c) == pytest.approx(expected)

    @pytest.mark.unit
    @pytest.mark.parametrize("step_c", [0.0, -1.0])
    def test_non_positive_step_returns_value_unchanged(self, step_c: float) -> None:
        """A zero/negative step means 'no grid' and the value passes through."""
        assert round_to_step_c(16.56, step_c) == pytest.approx(16.56)

    @pytest.mark.unit
    def test_reported_chain_lands_above_the_dew_floor(self) -> None:
        """End-to-end issue #5: max(base, dew) then quantize stays >= the floor."""
        target = cooling_setpoint_c(16.0, 16.56)  # -> 16.56
        assert round_to_step_c(target, 1.0) == pytest.approx(17.0)


class TestHeatingCurve:
    """The optional heating-water curve built from the global knobs."""

    @pytest.mark.unit
    def test_curve_uses_knobs_and_neutral(self) -> None:
        """Base at neutral; slope per K below the curve's own neutral point."""
        cfg = ControllerConfig(
            heating_supply_base_c=26.0,
            heating_supply_slope=0.5,
            heating_curve_neutral_c=15.0,
        )
        curve = heating_curve(cfg)
        assert curve.t_supply(15.0) == pytest.approx(26.0)
        assert curve.t_supply(20.0) == pytest.approx(26.0)  # above neutral: base
        assert curve.t_supply(5.0) == pytest.approx(26.0 + 0.5 * 10.0)

    @pytest.mark.unit
    def test_curve_ignores_the_feedforward_neutral(self) -> None:
        """Issue #35: ff_neutral_c no longer moves the heating curve."""
        base = heating_curve(ControllerConfig())
        moved_ff = heating_curve(ControllerConfig(ff_neutral_c=5.0))
        assert moved_ff == base
        assert moved_ff.t_neutral == pytest.approx(15.0)
        own = heating_curve(ControllerConfig(heating_curve_neutral_c=10.0))
        assert own.t_neutral == pytest.approx(10.0)
        assert own.t_supply(0.0) == pytest.approx(26.0 + 0.5 * 10.0)

    @pytest.mark.unit
    def test_curve_clamps_to_the_default_water_limits(self) -> None:
        """With default knobs the result is clamped to 20..40 degC."""
        cfg = ControllerConfig(heating_supply_base_c=38.0, heating_supply_slope=2.0)
        curve = heating_curve(cfg)
        assert curve.t_supply(-30.0) == pytest.approx(40.0)
        low = ControllerConfig(heating_supply_base_c=20.0, heating_supply_slope=0.0)
        assert heating_curve(low).t_supply(30.0) == pytest.approx(20.0)

    @pytest.mark.unit
    def test_curve_clamps_to_the_min_max_knobs(self) -> None:
        """Issue #35: the clamps are the heating_supply_min/max_c knobs."""
        cfg = ControllerConfig(
            heating_supply_base_c=26.0,
            heating_supply_slope=1.0,
            heating_supply_min_c=24.0,
            heating_supply_max_c=33.0,
        )
        curve = heating_curve(cfg)
        assert curve.t_supply_min == pytest.approx(24.0)
        assert curve.t_supply_max == pytest.approx(33.0)
        assert curve.t_supply(-20.0) == pytest.approx(33.0)
        assert curve.t_supply(10.0) == pytest.approx(31.0)


class TestHeatingCurvePoints:
    """The panel's heating-curve preview (issue #35)."""

    @pytest.mark.unit
    def test_default_points_are_the_preview_outdoor_temperatures(self) -> None:
        """Defaults: 26 degC at +15, +0.5 K per K below, in display order."""
        points = heating_curve_points(ControllerConfig())
        assert [t_out for t_out, _ in points] == list(HEATING_CURVE_PREVIEW_OUTDOOR_C)
        assert [t for _, t in points] == pytest.approx([26.0, 31.0, 36.0, 40.0])

    @pytest.mark.unit
    def test_matches_a_typical_pump_curve_with_a_0_01_step_slope(self) -> None:
        """The issue's example: 25 degC at +15, ~32 degC at -15 with 0.23 K/K."""
        cfg = ControllerConfig(heating_supply_base_c=25.0, heating_supply_slope=0.23)
        points = dict(heating_curve_points(cfg))
        assert points[15.0] == pytest.approx(25.0)
        assert points[-15.0] == pytest.approx(31.9)
        assert round(points[-15.0], 1) == pytest.approx(31.9)

    @pytest.mark.unit
    def test_points_follow_the_written_curve_and_custom_outdoor(self) -> None:
        """Every point equals heating_curve().t_supply at that outdoor temp."""
        cfg = ControllerConfig(
            heating_supply_base_c=24.0,
            heating_supply_slope=0.6,
            heating_curve_neutral_c=12.0,
            heating_supply_max_c=30.0,
        )
        curve = heating_curve(cfg)
        points = heating_curve_points(cfg, outdoor_c=(20.0, 0.0, -25.0))
        assert points == tuple((t, curve.t_supply(t)) for t in (20.0, 0.0, -25.0))
        assert points[-1][1] == pytest.approx(30.0)
