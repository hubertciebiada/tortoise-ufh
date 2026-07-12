"""Unit tests for :mod:`tortoise_ufh.hp_link` (the optional heat-pump link).

Exercises the pure decision logic of the B2 (2026-07-12) opt-in extension:

* ``direction_option`` — the FULL write table (all seven HeishaMon options
  times all four Tortoise modes), the DHW-flag preservation, the hard
  ``"DHW only"`` skip and the never-write-blind rule.
* ``dhw_option`` — every add/remove transition plus the refusals.
* ``cooling_setpoint_c`` — the ``max(base, safe dew point)`` floor.
* ``heating_curve`` — the knob-fed weather curve with its fixed 20/40 clamps.

Units: temperatures in degC; curve slope in K/K. This module never imports
``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.hp_link import (
    HEATING_SUPPLY_MAX_C,
    HEATING_SUPPLY_MIN_C,
    HEISHAMON_MODE_OPTIONS,
    cooling_setpoint_c,
    dhw_option,
    direction_option,
    heating_curve,
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


class TestHeatingCurve:
    """The optional heating-water curve built from the global knobs."""

    @pytest.mark.unit
    def test_curve_uses_knobs_and_neutral(self) -> None:
        """Base at neutral; slope per K below neutral; ff_neutral_c shared."""
        cfg = ControllerConfig(
            heating_supply_base_c=26.0, heating_supply_slope=0.5, ff_neutral_c=15.0
        )
        curve = heating_curve(cfg)
        assert curve.t_supply(15.0) == pytest.approx(26.0)
        assert curve.t_supply(20.0) == pytest.approx(26.0)  # above neutral: base
        assert curve.t_supply(5.0) == pytest.approx(26.0 + 0.5 * 10.0)

    @pytest.mark.unit
    def test_curve_clamps_to_fixed_water_limits(self) -> None:
        """The result is clamped to the fixed 20..40 degC water limits."""
        cfg = ControllerConfig(
            heating_supply_base_c=38.0, heating_supply_slope=2.0, ff_neutral_c=15.0
        )
        curve = heating_curve(cfg)
        assert curve.t_supply(-30.0) == pytest.approx(HEATING_SUPPLY_MAX_C)
        low = ControllerConfig(
            heating_supply_base_c=20.0, heating_supply_slope=0.0, ff_neutral_c=15.0
        )
        assert heating_curve(low).t_supply(30.0) == pytest.approx(HEATING_SUPPLY_MIN_C)
