"""Unit tests for ``SyntheticWeather`` profiles and ``SensorNoise``.

Verifies that:

* ``ChannelProfile`` / ``SyntheticWeather`` evaluate the four profile kinds
  (constant, step, ramp, sinusoidal) to their analytically-known values at
  sample simulation times [minutes].
* ``SyntheticWeather.get`` clamps GHI/wind/humidity into physical ranges.
* ``SensorNoise`` produces a reproducible sequence for a given seed and is a
  strict no-op when ``std == 0.0`` [degC].

Units:
    Temperatures: degC; GHI: W/m^2; wind: m/s; humidity: %; time: minutes.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tortoise_ufh.sensor_noise import SensorNoise
from tortoise_ufh.weather import (
    ChannelProfile,
    ProfileKind,
    SyntheticWeather,
    WeatherSource,
)


@pytest.mark.unit
class TestChannelProfileConstant:
    """CONSTANT profiles return the baseline at every sample time."""

    def test_constant_is_time_invariant(self) -> None:
        """A constant profile returns its baseline for any t_minutes."""
        profile = ChannelProfile(kind=ProfileKind.CONSTANT, baseline=7.5)
        for t in (0.0, 30.0, 1440.0, 1e6):
            assert profile.evaluate(t) == 7.5

    def test_constant_ignores_amplitude(self) -> None:
        """Amplitude has no effect on a CONSTANT profile."""
        profile = ChannelProfile(
            kind=ProfileKind.CONSTANT, baseline=3.0, amplitude=99.0
        )
        assert profile.evaluate(500.0) == 3.0


@pytest.mark.unit
class TestChannelProfileStep:
    """STEP profiles jump by amplitude at step_time_minutes."""

    def test_before_step_returns_baseline(self) -> None:
        """t below the step time yields the baseline."""
        profile = ChannelProfile(
            kind=ProfileKind.STEP,
            baseline=0.0,
            amplitude=-15.0,
            step_time_minutes=60.0,
        )
        assert profile.evaluate(59.999) == 0.0

    def test_at_and_after_step_returns_baseline_plus_amplitude(self) -> None:
        """t at or beyond the step time yields baseline + amplitude."""
        profile = ChannelProfile(
            kind=ProfileKind.STEP,
            baseline=0.0,
            amplitude=-15.0,
            step_time_minutes=60.0,
        )
        assert profile.evaluate(60.0) == -15.0
        assert profile.evaluate(120.0) == -15.0

    def test_negative_step_time_rejected(self) -> None:
        """A negative step time raises ValueError."""
        with pytest.raises(ValueError, match="step_time_minutes must be >= 0"):
            ChannelProfile(
                kind=ProfileKind.STEP,
                step_time_minutes=-1.0,
            )


@pytest.mark.unit
class TestChannelProfileRamp:
    """RAMP profiles rise linearly, then hold the final value."""

    def test_ramp_endpoints_and_midpoint(self) -> None:
        """Ramp yields baseline at t=0, midpoint at half, final at the end."""
        profile = ChannelProfile(
            kind=ProfileKind.RAMP,
            baseline=10.0,
            amplitude=20.0,
            period_minutes=100.0,
        )
        assert profile.evaluate(0.0) == pytest.approx(10.0)
        assert profile.evaluate(50.0) == pytest.approx(20.0)
        assert profile.evaluate(100.0) == pytest.approx(30.0)

    def test_ramp_saturates_after_period(self) -> None:
        """Past the period the ramp holds baseline + amplitude."""
        profile = ChannelProfile(
            kind=ProfileKind.RAMP,
            baseline=10.0,
            amplitude=20.0,
            period_minutes=100.0,
        )
        assert profile.evaluate(1000.0) == pytest.approx(30.0)

    def test_ramp_clamps_negative_time(self) -> None:
        """Negative time is clamped to the baseline (progress=0)."""
        profile = ChannelProfile(
            kind=ProfileKind.RAMP,
            baseline=10.0,
            amplitude=20.0,
            period_minutes=100.0,
        )
        assert profile.evaluate(-50.0) == pytest.approx(10.0)

    def test_nonpositive_period_rejected(self) -> None:
        """A zero/negative period raises ValueError for RAMP."""
        with pytest.raises(ValueError, match="period_minutes must be > 0"):
            ChannelProfile(kind=ProfileKind.RAMP, period_minutes=0.0)


@pytest.mark.unit
class TestChannelProfileSinusoidal:
    """SINUSOIDAL profiles follow baseline + amplitude*sin(2*pi*t/period)."""

    def test_sinusoidal_quarter_points(self) -> None:
        """Sine hits baseline, +amp, baseline, -amp at cardinal phases."""
        period = 1440.0
        profile = ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=5.0,
            amplitude=8.0,
            period_minutes=period,
        )
        assert profile.evaluate(0.0) == pytest.approx(5.0)
        assert profile.evaluate(period / 4.0) == pytest.approx(13.0)
        assert profile.evaluate(period / 2.0) == pytest.approx(5.0, abs=1e-9)
        assert profile.evaluate(3.0 * period / 4.0) == pytest.approx(-3.0)

    def test_sinusoidal_matches_closed_form(self) -> None:
        """Arbitrary sample equals the analytic sine expression."""
        profile = ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=0.0,
            amplitude=2.5,
            period_minutes=360.0,
        )
        t = 47.0
        expected = 2.5 * math.sin(2.0 * math.pi * t / 360.0)
        assert profile.evaluate(t) == pytest.approx(expected)

    def test_nonpositive_period_rejected(self) -> None:
        """A zero/negative period raises ValueError for SINUSOIDAL."""
        with pytest.raises(ValueError, match="period_minutes must be > 0"):
            ChannelProfile(kind=ProfileKind.SINUSOIDAL, period_minutes=-5.0)


@pytest.mark.unit
class TestSyntheticWeather:
    """SyntheticWeather composes four channels into WeatherPoints."""

    def test_satisfies_weather_source_protocol(self) -> None:
        """SyntheticWeather is a structural WeatherSource."""
        weather = SyntheticWeather.constant()
        assert isinstance(weather, WeatherSource)

    def test_constant_factory_values(self) -> None:
        """The constant factory returns the requested channel values."""
        weather = SyntheticWeather.constant(
            T_out=-5.0, GHI=300.0, wind_speed=2.0, humidity=80.0
        )
        point = weather.get(123.0)
        assert point.T_out == -5.0
        assert point.GHI == 300.0
        assert point.wind_speed == 2.0
        assert point.humidity == 80.0

    def test_step_factory_transition(self) -> None:
        """step_t_out changes T_out only at the step time."""
        weather = SyntheticWeather.step_t_out(
            baseline=0.0, amplitude=-15.0, step_time_minutes=1440.0
        )
        assert weather.get(1439.0).T_out == 0.0
        assert weather.get(1440.0).T_out == -15.0

    def test_ramp_factory_midpoint(self) -> None:
        """ramp_t_out reaches the midpoint value at half the period."""
        weather = SyntheticWeather.ramp_t_out(
            baseline=0.0, amplitude=10.0, period_minutes=120.0
        )
        assert weather.get(60.0).T_out == pytest.approx(5.0)

    def test_sinusoidal_factory_peak(self) -> None:
        """sinusoidal_t_out peaks at a quarter period."""
        weather = SyntheticWeather.sinusoidal_t_out(
            baseline=10.0, amplitude=6.0, period_minutes=1440.0
        )
        assert weather.get(360.0).T_out == pytest.approx(16.0)

    def test_get_clamps_channels_to_physical_ranges(self) -> None:
        """Out-of-range channel values are clamped inside get()."""
        weather = SyntheticWeather(
            t_out=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=0.0),
            ghi=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=-100.0),
            wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=-3.0),
            humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=150.0),
        )
        point = weather.get(0.0)
        assert point.GHI == 0.0
        assert point.wind_speed == 0.0
        assert point.humidity == 100.0


@pytest.mark.unit
class TestSensorNoise:
    """SensorNoise is reproducible per-seed and a no-op at std=0."""

    def test_zero_std_is_noop(self) -> None:
        """std == 0.0 returns the input unchanged for every call."""
        noise = SensorNoise(std=0.0, seed=42)
        for value in (-5.0, 0.0, 20.5, 100.0):
            assert noise.corrupt(value) == value

    def test_zero_std_returns_float(self) -> None:
        """The no-op path still returns a float type."""
        noise = SensorNoise(std=0.0)
        result = noise.corrupt(21)
        assert isinstance(result, float)
        assert result == 21.0

    def test_same_seed_is_reproducible(self) -> None:
        """Two generators with the same seed emit identical sequences."""
        a = SensorNoise(std=0.5, seed=7)
        b = SensorNoise(std=0.5, seed=7)
        seq_a = [a.corrupt(20.0) for _ in range(10)]
        seq_b = [b.corrupt(20.0) for _ in range(10)]
        assert seq_a == seq_b

    def test_matches_reference_generator(self) -> None:
        """corrupt equals value + default_rng(seed).normal(0, std)."""
        seed, std = 123, 0.3
        noise = SensorNoise(std=std, seed=seed)
        ref = np.random.default_rng(seed)
        for _ in range(5):
            expected = 18.0 + float(ref.normal(0.0, std))
            assert noise.corrupt(18.0) == pytest.approx(expected)

    def test_different_seeds_diverge(self) -> None:
        """Different seeds produce different noise sequences."""
        a = SensorNoise(std=1.0, seed=1)
        b = SensorNoise(std=1.0, seed=2)
        seq_a = [a.corrupt(0.0) for _ in range(10)]
        seq_b = [b.corrupt(0.0) for _ in range(10)]
        assert seq_a != seq_b

    def test_nonzero_std_adds_noise(self) -> None:
        """A positive std perturbs the clean value."""
        noise = SensorNoise(std=1.0, seed=42)
        assert noise.corrupt(20.0) != 20.0

    def test_negative_std_rejected(self) -> None:
        """A negative std raises ValueError."""
        with pytest.raises(ValueError, match="std must be >= 0.0"):
            SensorNoise(std=-0.1)

    def test_properties_expose_config(self) -> None:
        """std and seed properties reflect construction arguments."""
        noise = SensorNoise(std=0.25, seed=99)
        assert noise.std == 0.25
        assert noise.seed == 99
