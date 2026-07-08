"""Weather data sources for building simulation.

Defines the ``WeatherSource`` protocol and the deterministic
``SyntheticWeather`` source used to drive the offline building simulator.
The core library ships only the synthetic (stdlib-computable) source; any
real-world source (Home Assistant forecast, CSV, API) lives in the adapter
layer and merely has to satisfy the structural ``WeatherSource`` protocol.

Units:
    T_out: degC
    GHI: W/m^2  (Global Horizontal Irradiance)
    wind_speed: m/s
    humidity: % (0-100)
    time: minutes (simulation convention)

Dependencies:
    stdlib only (math, dataclasses, enum, typing).  numpy is imported for
    typing parity with the rest of the core but is not required at runtime
    here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# WeatherPoint — immutable snapshot of weather conditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeatherPoint:
    """Immutable snapshot of weather conditions at a single instant.

    Attributes:
        T_out: Outdoor air temperature [degC].
        GHI: Global Horizontal Irradiance [W/m^2] (>= 0).
        wind_speed: Wind speed [m/s] (>= 0).
        humidity: Relative humidity [%] in the range 0-100.
    """

    T_out: float
    GHI: float
    wind_speed: float
    humidity: float

    def __post_init__(self) -> None:
        """Validate physical ranges.

        Raises:
            ValueError: If GHI or wind_speed is negative, or humidity is
                outside 0-100 %.
        """
        if self.GHI < 0.0:
            msg = f"GHI must be >= 0 W/m^2, got {self.GHI}"
            raise ValueError(msg)
        if self.wind_speed < 0.0:
            msg = f"wind_speed must be >= 0 m/s, got {self.wind_speed}"
            raise ValueError(msg)
        if not 0.0 <= self.humidity <= 100.0:
            msg = f"humidity must be in [0, 100] %, got {self.humidity}"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# WeatherSource — structural-subtyping protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class WeatherSource(Protocol):
    """Protocol for objects that provide weather data at a given simulation time.

    Any object exposing a ``get(t_minutes: float) -> WeatherPoint`` method
    satisfies this protocol via structural subtyping.  This is how a Home
    Assistant weather adapter plugs into the pure core without inheritance.
    """

    def get(self, t_minutes: float) -> WeatherPoint:
        """Return weather conditions at simulation time *t_minutes*.

        Args:
            t_minutes: Simulation time in minutes.

        Returns:
            A ``WeatherPoint`` with conditions at the requested time.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# ProfileKind — enumeration of channel profile shapes
# ---------------------------------------------------------------------------


class ProfileKind(Enum):
    """Kind of time-varying profile for a single weather channel."""

    CONSTANT = "constant"
    STEP = "step"
    RAMP = "ramp"
    SINUSOIDAL = "sinusoidal"


# ---------------------------------------------------------------------------
# ChannelProfile — describes one channel's time evolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelProfile:
    """Describes how a single weather channel evolves over time.

    Attributes:
        kind: Shape of the profile (constant, step, ramp, sinusoidal).
        baseline: Base value returned at t=0 (for constant/step/ramp) or the
            DC offset (for sinusoidal).
        amplitude: Magnitude of the change.

            * STEP: added to baseline after ``step_time_minutes``.
            * RAMP: total rise from baseline to baseline + amplitude.
            * SINUSOIDAL: peak deviation from baseline.
            * CONSTANT: ignored.
        period_minutes: Duration of one full cycle (SINUSOIDAL) or ramp
            duration (RAMP).  Must be > 0 for SINUSOIDAL and RAMP.
        step_time_minutes: Time at which the step occurs (STEP only); must
            be >= 0.
    """

    kind: ProfileKind
    baseline: float = 0.0
    amplitude: float = 0.0
    period_minutes: float = 0.0
    step_time_minutes: float = 0.0

    def __post_init__(self) -> None:
        """Validate profile parameters.

        Raises:
            ValueError: If ``period_minutes`` <= 0 for a RAMP/SINUSOIDAL
                profile, or ``step_time_minutes`` < 0 for a STEP profile.
        """
        if (
            self.kind in (ProfileKind.SINUSOIDAL, ProfileKind.RAMP)
            and self.period_minutes <= 0.0
        ):
            msg = (
                f"period_minutes must be > 0 for {self.kind.name}, "
                f"got {self.period_minutes}"
            )
            raise ValueError(msg)
        if self.kind == ProfileKind.STEP and self.step_time_minutes < 0.0:
            msg = (
                f"step_time_minutes must be >= 0 for STEP, got {self.step_time_minutes}"
            )
            raise ValueError(msg)

    def evaluate(self, t_minutes: float) -> float:
        """Evaluate the profile value at simulation time *t_minutes*.

        Args:
            t_minutes: Simulation time in minutes.

        Returns:
            Channel value at the given time.
        """
        if self.kind == ProfileKind.CONSTANT:
            return self.baseline

        if self.kind == ProfileKind.STEP:
            if t_minutes < self.step_time_minutes:
                return self.baseline
            return self.baseline + self.amplitude

        if self.kind == ProfileKind.RAMP:
            progress = min(max(t_minutes / self.period_minutes, 0.0), 1.0)
            return self.baseline + self.amplitude * progress

        # SINUSOIDAL
        return self.baseline + self.amplitude * math.sin(
            2.0 * math.pi * t_minutes / self.period_minutes
        )


# ---------------------------------------------------------------------------
# SyntheticWeather — deterministic weather source for testing
# ---------------------------------------------------------------------------


class SyntheticWeather:
    """Deterministic weather source built from per-channel profiles.

    Each of the four weather channels (T_out, GHI, wind_speed, humidity) is
    driven by an independent ``ChannelProfile``, making the output
    analytically verifiable and perfectly reproducible.

    Typical usage::

        weather = SyntheticWeather.constant(T_out=-5.0, GHI=0.0)
        point = weather.get(t_minutes=60.0)
        assert point.T_out == -5.0
    """

    def __init__(
        self,
        t_out: ChannelProfile,
        ghi: ChannelProfile,
        wind_speed: ChannelProfile,
        humidity: ChannelProfile,
    ) -> None:
        """Initialize with one profile per weather channel.

        Args:
            t_out: Profile for outdoor temperature [degC].
            ghi: Profile for Global Horizontal Irradiance [W/m^2].
            wind_speed: Profile for wind speed [m/s].
            humidity: Profile for relative humidity [%].
        """
        self._t_out = t_out
        self._ghi = ghi
        self._wind_speed = wind_speed
        self._humidity = humidity

    def get(self, t_minutes: float) -> WeatherPoint:
        """Return weather conditions at simulation time *t_minutes*.

        GHI, wind_speed and humidity are clamped to their physical ranges
        (GHI/wind >= 0, humidity in [0, 100]) so that unbounded profile
        parameters never produce an invalid ``WeatherPoint``.

        Args:
            t_minutes: Simulation time in minutes.

        Returns:
            A ``WeatherPoint`` evaluated from the four channel profiles.
        """
        ghi = max(0.0, self._ghi.evaluate(t_minutes))
        wind = max(0.0, self._wind_speed.evaluate(t_minutes))
        humidity = min(100.0, max(0.0, self._humidity.evaluate(t_minutes)))
        return WeatherPoint(
            T_out=self._t_out.evaluate(t_minutes),
            GHI=ghi,
            wind_speed=wind,
            humidity=humidity,
        )

    # -- Factory class methods -----------------------------------------------

    @classmethod
    def constant(
        cls,
        T_out: float = 0.0,
        GHI: float = 0.0,
        wind_speed: float = 0.0,
        humidity: float = 50.0,
    ) -> SyntheticWeather:
        """Create a weather source with all-constant channels.

        Args:
            T_out: Outdoor temperature [degC].
            GHI: Global Horizontal Irradiance [W/m^2].
            wind_speed: Wind speed [m/s].
            humidity: Relative humidity [%].

        Returns:
            A ``SyntheticWeather`` that returns the same values for any time.
        """
        return cls(
            t_out=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=T_out),
            ghi=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=GHI),
            wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=wind_speed),
            humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=humidity),
        )

    @classmethod
    def step_t_out(
        cls,
        baseline: float = 0.0,
        amplitude: float = 10.0,
        step_time_minutes: float = 60.0,
        GHI: float = 0.0,
        wind_speed: float = 0.0,
        humidity: float = 50.0,
    ) -> SyntheticWeather:
        """Create a weather source with a step change in T_out.

        T_out is *baseline* for t < step_time, then *baseline + amplitude*.
        All other channels are constant.

        Args:
            baseline: T_out before the step [degC].
            amplitude: Temperature change at the step [degC].
            step_time_minutes: Time of the step [minutes].
            GHI: Constant GHI [W/m^2].
            wind_speed: Constant wind speed [m/s].
            humidity: Constant humidity [%].

        Returns:
            A ``SyntheticWeather`` with a step profile on T_out.
        """
        return cls(
            t_out=ChannelProfile(
                kind=ProfileKind.STEP,
                baseline=baseline,
                amplitude=amplitude,
                step_time_minutes=step_time_minutes,
            ),
            ghi=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=GHI),
            wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=wind_speed),
            humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=humidity),
        )

    @classmethod
    def ramp_t_out(
        cls,
        baseline: float = 0.0,
        amplitude: float = 10.0,
        period_minutes: float = 120.0,
        GHI: float = 0.0,
        wind_speed: float = 0.0,
        humidity: float = 50.0,
    ) -> SyntheticWeather:
        """Create a weather source with a linear ramp in T_out.

        T_out ramps from *baseline* to *baseline + amplitude* over
        *period_minutes*, then stays at the final value.

        Args:
            baseline: Starting T_out [degC].
            amplitude: Total temperature rise [degC].
            period_minutes: Duration of the ramp [minutes].
            GHI: Constant GHI [W/m^2].
            wind_speed: Constant wind speed [m/s].
            humidity: Constant humidity [%].

        Returns:
            A ``SyntheticWeather`` with a ramp profile on T_out.
        """
        return cls(
            t_out=ChannelProfile(
                kind=ProfileKind.RAMP,
                baseline=baseline,
                amplitude=amplitude,
                period_minutes=period_minutes,
            ),
            ghi=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=GHI),
            wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=wind_speed),
            humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=humidity),
        )

    @classmethod
    def sinusoidal_t_out(
        cls,
        baseline: float = 0.0,
        amplitude: float = 10.0,
        period_minutes: float = 1440.0,
        GHI: float = 0.0,
        wind_speed: float = 0.0,
        humidity: float = 50.0,
    ) -> SyntheticWeather:
        """Create a weather source with sinusoidal T_out variation.

        T_out = baseline + amplitude * sin(2*pi*t / period_minutes).

        Args:
            baseline: Mean outdoor temperature [degC].
            amplitude: Peak deviation from baseline [degC].
            period_minutes: Full cycle duration [minutes] (default 1440 = 1 day).
            GHI: Constant GHI [W/m^2].
            wind_speed: Constant wind speed [m/s].
            humidity: Constant humidity [%].

        Returns:
            A ``SyntheticWeather`` with a sinusoidal profile on T_out.
        """
        return cls(
            t_out=ChannelProfile(
                kind=ProfileKind.SINUSOIDAL,
                baseline=baseline,
                amplitude=amplitude,
                period_minutes=period_minutes,
            ),
            ghi=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=GHI),
            wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=wind_speed),
            humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=humidity),
        )
