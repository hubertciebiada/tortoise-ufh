"""Unit tests for :mod:`tortoise_ufh.weather_comp`.

Covers both weather-compensation curves (all temperatures in degC, slope in
K_supply / K_outdoor):

* clamping of ``t_supply`` to ``[t_supply_min, t_supply_max]``;
* monotonic response — heating supply *rises* as outdoor drops, cooling supply
  *rises* as outdoor climbs;
* ``to_dict`` / ``from_dict`` round-trip;
* ``__post_init__`` validation raising :class:`ValueError`.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.weather_comp import (
    CoolingCompCurve,
    WeatherCompCurve,
)

pytestmark = pytest.mark.unit


def _heating_curve() -> WeatherCompCurve:
    """Return a representative heating curve [degC / K_supply per K_outdoor]."""
    return WeatherCompCurve(
        t_supply_base=30.0,
        slope=1.5,
        t_neutral=15.0,
        t_supply_max=45.0,
        t_supply_min=20.0,
    )


def _cooling_curve() -> CoolingCompCurve:
    """Return a representative cooling curve [degC / K_supply per K_outdoor]."""
    return CoolingCompCurve(
        t_supply_base=18.0,
        slope=0.5,
        t_neutral=26.0,
        t_supply_max=22.0,
        t_supply_min=16.0,
    )


class TestWeatherCompCurve:
    """Heating weather-compensation curve."""

    def test_neutral_returns_base(self) -> None:
        """At/above t_neutral supply equals the base temperature."""
        curve = _heating_curve()
        assert curve.t_supply(15.0) == pytest.approx(30.0)
        assert curve.t_supply(25.0) == pytest.approx(30.0)

    def test_slope_increases_supply_as_outdoor_drops(self) -> None:
        """Colder outdoor -> higher supply (monotonic non-increasing in t_out)."""
        curve = _heating_curve()
        temps = [15.0, 10.0, 5.0, 0.0, -5.0]
        supplies = [curve.t_supply(t) for t in temps]
        assert all(a <= b for a, b in zip(supplies, supplies[1:], strict=False))
        # Explicit slope check within the linear region.
        assert curve.t_supply(10.0) == pytest.approx(30.0 + 1.5 * 5.0)

    def test_clips_to_max(self) -> None:
        """Very cold outdoor saturates at t_supply_max."""
        curve = _heating_curve()
        assert curve.t_supply(-40.0) == pytest.approx(45.0)

    def test_clips_to_min(self) -> None:
        """A base equal to the floor never dips below t_supply_min."""
        curve = WeatherCompCurve(
            t_supply_base=20.0,
            slope=2.0,
            t_neutral=15.0,
            t_supply_max=40.0,
            t_supply_min=20.0,
        )
        assert curve.t_supply(100.0) == pytest.approx(20.0)

    def test_output_within_bounds(self) -> None:
        """Output stays within [min, max] across a wide sweep."""
        curve = _heating_curve()
        for t_out in range(-50, 51):
            supply = curve.t_supply(float(t_out))
            assert 20.0 <= supply <= 45.0

    def test_default_min(self) -> None:
        """t_supply_min defaults to 20.0 degC."""
        curve = WeatherCompCurve(
            t_supply_base=30.0, slope=1.0, t_neutral=15.0, t_supply_max=45.0
        )
        assert curve.t_supply_min == pytest.approx(20.0)

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Serialisation is lossless and reconstructs an equal instance."""
        curve = _heating_curve()
        data = curve.to_dict()
        assert data == {
            "t_supply_base": 30.0,
            "slope": 1.5,
            "t_neutral": 15.0,
            "t_supply_max": 45.0,
            "t_supply_min": 20.0,
        }
        assert WeatherCompCurve.from_dict(data) == curve

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"slope": -0.1},
            {"t_supply_min": 0.0},
            {"t_supply_min": -5.0},
            {"t_supply_max": 20.0},  # not > min
            {"t_supply_base": 10.0},  # below min
            {"t_supply_base": 50.0},  # above max
        ],
    )
    def test_invalid_params_raise(self, kwargs: dict[str, float]) -> None:
        """Out-of-range parameters raise ValueError."""
        base = {
            "t_supply_base": 30.0,
            "slope": 1.5,
            "t_neutral": 15.0,
            "t_supply_max": 45.0,
            "t_supply_min": 20.0,
        }
        with pytest.raises(ValueError):
            WeatherCompCurve(**{**base, **kwargs})


class TestCoolingCompCurve:
    """Cooling weather-compensation curve."""

    def test_neutral_returns_base(self) -> None:
        """At/below t_neutral supply equals the base temperature."""
        curve = _cooling_curve()
        assert curve.t_supply(26.0) == pytest.approx(18.0)
        assert curve.t_supply(20.0) == pytest.approx(18.0)

    def test_slope_increases_supply_as_outdoor_rises(self) -> None:
        """Hotter outdoor -> higher supply (monotonic non-decreasing in t_out)."""
        curve = _cooling_curve()
        temps = [26.0, 28.0, 30.0, 35.0, 40.0]
        supplies = [curve.t_supply(t) for t in temps]
        assert all(a <= b for a, b in zip(supplies, supplies[1:], strict=False))
        assert curve.t_supply(28.0) == pytest.approx(18.0 + 0.5 * 2.0)

    def test_clips_to_max(self) -> None:
        """Very hot outdoor saturates at t_supply_max."""
        curve = _cooling_curve()
        assert curve.t_supply(80.0) == pytest.approx(22.0)

    def test_output_within_bounds(self) -> None:
        """Output stays within [min, max] across a wide sweep."""
        curve = _cooling_curve()
        for t_out in range(-10, 61):
            supply = curve.t_supply(float(t_out))
            assert 16.0 <= supply <= 22.0

    def test_default_min(self) -> None:
        """t_supply_min defaults to 16.0 degC."""
        curve = CoolingCompCurve(
            t_supply_base=18.0, slope=0.5, t_neutral=26.0, t_supply_max=22.0
        )
        assert curve.t_supply_min == pytest.approx(16.0)

    def test_to_dict_from_dict_roundtrip(self) -> None:
        """Serialisation is lossless and reconstructs an equal instance."""
        curve = _cooling_curve()
        data = curve.to_dict()
        assert data == {
            "t_supply_base": 18.0,
            "slope": 0.5,
            "t_neutral": 26.0,
            "t_supply_max": 22.0,
            "t_supply_min": 16.0,
        }
        assert CoolingCompCurve.from_dict(data) == curve

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"slope": -1.0},
            {"t_supply_min": 0.0},
            {"t_supply_max": 16.0},  # not > min
            {"t_supply_base": 10.0},  # below min
            {"t_supply_base": 30.0},  # above max
        ],
    )
    def test_invalid_params_raise(self, kwargs: dict[str, float]) -> None:
        """Out-of-range parameters raise ValueError."""
        base = {
            "t_supply_base": 18.0,
            "slope": 0.5,
            "t_neutral": 26.0,
            "t_supply_max": 22.0,
            "t_supply_min": 16.0,
        }
        with pytest.raises(ValueError):
            CoolingCompCurve(**{**base, **kwargs})
