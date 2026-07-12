"""Unit tests for :mod:`tortoise_ufh.dew_point`.

Covers the Magnus ``dew_point`` formula against textbook psychrometric
reference points (tolerance < 0.1 degC) and the graduated
``cooling_throttle_factor`` (K6 semantics 2026-07-12: 1 at/above margin —
full cooling on the pump's dew floor — ramping linearly to 0 at
``max(0, margin - ramp)``), plus input validation.

Units:
    Temperatures: degC
    Relative humidity: % in (0, 100]
    Throttle factor: 0.0 (fully throttled) to 1.0 (fully open)
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from custom_components.tortoise_ufh.core.dew_point import (
    cooling_throttle_factor,
    dew_point,
    dew_point_simplified,
)

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class DewPointCase:
    """One textbook dew-point reference point for the Magnus formula.

    Attributes:
        t_air_c: Air temperature [degC].
        rh_pct: Relative humidity [%] in (0, 100].
        expected_dew_c: Textbook dew-point temperature [degC].
        tol_c: Allowed absolute error [degC]. Must be > 0.
    """

    t_air_c: float
    rh_pct: float
    expected_dew_c: float
    tol_c: float = 0.1

    def __post_init__(self) -> None:
        """Validate the reference case.

        Raises:
            ValueError: If humidity is outside (0, 100] or tolerance <= 0.
        """
        if not (0.0 < self.rh_pct <= 100.0):
            msg = f"rh_pct must be in (0, 100], got {self.rh_pct}"
            raise ValueError(msg)
        if self.tol_c <= 0.0:
            msg = f"tol_c must be > 0, got {self.tol_c}"
            raise ValueError(msg)


# Reference points from standard psychrometric tables, rounded to 0.1 degC.
REFERENCE_CASES: tuple[DewPointCase, ...] = (
    DewPointCase(t_air_c=20.0, rh_pct=50.0, expected_dew_c=9.3),
    DewPointCase(t_air_c=25.0, rh_pct=60.0, expected_dew_c=16.7),
    DewPointCase(t_air_c=30.0, rh_pct=80.0, expected_dew_c=26.2),
    DewPointCase(t_air_c=15.0, rh_pct=70.0, expected_dew_c=9.6),
    DewPointCase(t_air_c=0.0, rh_pct=60.0, expected_dew_c=-6.8),
    DewPointCase(t_air_c=20.0, rh_pct=100.0, expected_dew_c=20.0),
)


class TestDewPointReference:
    """Magnus dew point matches textbook reference points within 0.1 degC."""

    @pytest.mark.parametrize("case", REFERENCE_CASES, ids=lambda c: repr(c))
    def test_dew_point_matches_textbook(self, case: DewPointCase) -> None:
        """Magnus dew point is within tolerance of the textbook value."""
        result = dew_point(case.t_air_c, case.rh_pct)
        assert abs(result - case.expected_dew_c) < case.tol_c

    def test_saturation_equals_air_temperature(self) -> None:
        """At 100 % RH the dew point equals the air temperature."""
        for t_air in (-10.0, 0.0, 18.0, 25.0):
            assert dew_point(t_air, 100.0) == pytest.approx(t_air, abs=1e-6)

    def test_dew_point_below_air_temperature(self) -> None:
        """Below saturation the dew point is strictly below air temperature."""
        assert dew_point(22.0, 40.0) < 22.0

    def test_dew_point_monotonic_in_humidity(self) -> None:
        """Higher humidity yields a higher dew point at fixed temperature."""
        low = dew_point(24.0, 30.0)
        mid = dew_point(24.0, 55.0)
        high = dew_point(24.0, 85.0)
        assert low < mid < high


class TestDewPointValidation:
    """Humidity outside (0, 100] is rejected by both dew-point functions."""

    @pytest.mark.parametrize("rh", [0.0, -5.0, 100.1, 150.0])
    def test_dew_point_rejects_invalid_humidity(self, rh: float) -> None:
        """Out-of-range humidity raises ValueError."""
        with pytest.raises(ValueError, match="rh must be in"):
            dew_point(20.0, rh)

    @pytest.mark.parametrize("rh", [0.0, -1.0, 101.0])
    def test_simplified_rejects_invalid_humidity(self, rh: float) -> None:
        """The simplified approximation also validates humidity."""
        with pytest.raises(ValueError, match="rh must be in"):
            dew_point_simplified(20.0, rh)


class TestCoolingThrottleFactor:
    """Graduated cooling throttle, K6 semantics (2026-07-12).

    The ramp ENDS at ``margin`` (full cooling on the heat pump's global dew
    floor) and spans ``(max(0, margin - ramp), margin)``; with the defaults
    the valve closes fully only at the room's actual dew point. The old
    semantics (ramp ABOVE the margin) stacked a second margin on the pump
    floor and strangled the cooling — these tests pin the NEW contract.
    """

    def test_returns_zero_at_dew_point(self) -> None:
        """gap <= max(0, margin - ramp) fully throttles the valve (0.0)."""
        # gap = 0.0 <= lo = max(0, 2 - 2) = 0: supply AT the dew point.
        assert cooling_throttle_factor(15.0, 15.0, margin=2.0, ramp=2.0) == 0.0

    def test_returns_zero_below_dew_point(self) -> None:
        """A supply below the dew point is fully throttled (0.0)."""
        # gap = -1.0 < 0.
        assert cooling_throttle_factor(14.0, 15.0, margin=2.0, ramp=2.0) == 0.0

    def test_returns_one_at_margin(self) -> None:
        """gap exactly equal to margin opens fully (1.0) — the pump floor."""
        # gap = 2.0 == margin 2.0 (K6: full cooling right on the dew floor).
        assert cooling_throttle_factor(17.0, 15.0, margin=2.0, ramp=2.0) == 1.0

    def test_returns_one_above_margin(self) -> None:
        """gap >= margin opens the valve fully (1.0)."""
        # gap = 5.0 >= margin 2.0.
        assert cooling_throttle_factor(20.0, 15.0, margin=2.0, ramp=2.0) == 1.0

    def test_graduated_midpoint(self) -> None:
        """Halfway up the ramp returns 0.5."""
        # gap = 1.0, lo = 0, margin 2.0 -> (1-0)/2 = 0.5.
        result = cooling_throttle_factor(16.0, 15.0, margin=2.0, ramp=2.0)
        assert result == pytest.approx(0.5)

    @pytest.mark.parametrize(
        ("gap", "expected"),
        [(0.5, 0.25), (1.0, 0.5), (1.5, 0.75)],
    )
    def test_graduated_linear(self, gap: float, expected: float) -> None:
        """The ramp zone is linear from 0.0 at ``lo`` to 1.0 at ``margin``."""
        t_dew = 15.0
        result = cooling_throttle_factor(t_dew + gap, t_dew, margin=2.0, ramp=2.0)
        assert result == pytest.approx(expected)

    def test_ramp_wider_than_margin_is_clipped(self) -> None:
        """A ramp wider than the margin cannot extend below gap = 0."""
        # lo = max(0, 1 - 4) = 0; gap = 0.5, margin 1.0 -> 0.5/1.0.
        result = cooling_throttle_factor(15.5, 15.0, margin=1.0, ramp=4.0)
        assert result == pytest.approx(0.5)

    def test_zero_margin_is_conservative_at_zero_gap(self) -> None:
        """The degenerate margin = 0 config reads gap 0 as fully throttled."""
        assert cooling_throttle_factor(15.0, 15.0, margin=0.0, ramp=2.0) == 0.0

    def test_factor_bounded_unit_interval(self) -> None:
        """The factor never leaves [0, 1] across a wide surface sweep."""
        t_dew = 12.0
        for surface_offset in range(-5, 12):
            factor = cooling_throttle_factor(
                t_dew + surface_offset, t_dew, margin=2.0, ramp=3.0
            )
            assert 0.0 <= factor <= 1.0

    def test_factor_nondecreasing_in_surface_temp(self) -> None:
        """A warmer surface never lowers the throttle factor."""
        t_dew = 14.0
        prev = -1.0
        for surface_offset in range(0, 10):
            factor = cooling_throttle_factor(
                t_dew + surface_offset, t_dew, margin=1.0, ramp=4.0
            )
            assert factor >= prev
            prev = factor


class TestCoolingThrottleValidation:
    """cooling_throttle_factor validates its margin and ramp arguments."""

    def test_rejects_negative_margin(self) -> None:
        """A negative margin raises ValueError."""
        with pytest.raises(ValueError, match="margin must be >= 0"):
            cooling_throttle_factor(18.0, 15.0, margin=-0.1, ramp=2.0)

    @pytest.mark.parametrize("ramp", [0.0, -1.0])
    def test_rejects_nonpositive_ramp(self, ramp: float) -> None:
        """A zero or negative ramp raises ValueError."""
        with pytest.raises(ValueError, match="ramp must be > 0"):
            cooling_throttle_factor(18.0, 15.0, margin=2.0, ramp=ramp)


class TestDewPointCaseValidation:
    """The reference-case dataclass validates its own fields."""

    def test_rejects_invalid_humidity(self) -> None:
        """Out-of-range humidity raises ValueError."""
        with pytest.raises(ValueError, match="rh_pct must be in"):
            DewPointCase(t_air_c=20.0, rh_pct=0.0, expected_dew_c=9.0)

    def test_rejects_nonpositive_tolerance(self) -> None:
        """A non-positive tolerance raises ValueError."""
        with pytest.raises(ValueError, match="tol_c must be > 0"):
            DewPointCase(t_air_c=20.0, rh_pct=50.0, expected_dew_c=9.0, tol_c=0.0)
