"""Deterministic simulation scenario library for tortoise-ufh.

Central catalog of reproducible offline scenarios for the per-room UFH
controller. Each factory function returns a fully configured
:class:`~tortoise_ufh.config.SimScenario` (building + weather + run knobs)
ready for use with the building simulator and the ``run_scenario`` test
harness.

Every scenario composes its disturbances from
:class:`~tortoise_ufh.weather.SyntheticWeather` /
:class:`~tortoise_ufh.weather.ChannelProfile` objects (no external data), so
runs are perfectly reproducible and analytically verifiable.

This module is part of the pure core: it MUST NOT import ``homeassistant``.

Structure (mirrors ``tortoise_ufh.building_profiles``):
    * Individual factory functions returning ``SimScenario`` instances.
    * ``SCENARIO_LIBRARY`` — registry mapping name to factory callable.

Units:
    * Temperatures / setpoints: degrees Celsius (``_c``).
    * GHI: W/m^2; humidity: percent 0..100; wind: m/s.
    * Durations: minutes (``_minutes``); simulation step: seconds (``_seconds``).
    * Sensor-noise standard deviation: kelvin.

Usage::

    from tortoise_ufh.scenarios import SCENARIO_LIBRARY, steady_heating

    scenario = steady_heating()
    assert scenario.name == "steady_heating"

    # Or via the lookup dict:
    factory = SCENARIO_LIBRARY["cold_snap"]
    scenario = factory()
"""

from __future__ import annotations

from collections.abc import Callable

from tortoise_ufh.building_profiles import modern_bungalow, well_insulated
from tortoise_ufh.config import SimScenario
from tortoise_ufh.models import Mode
from tortoise_ufh.weather import ChannelProfile, ProfileKind, SyntheticWeather

__all__ = [
    "SCENARIO_LIBRARY",
    "cold_snap",
    "hot_july_floor_cooling",
    "sensor_dropout",
    "solar_overshoot",
    "spring_transition",
    "steady_heating",
]


# ---------------------------------------------------------------------------
# Single-scenario factory functions
# ---------------------------------------------------------------------------


def steady_heating() -> SimScenario:
    """Steady-state heating at a constant outdoor temperature.

    Constant ``T_out=0`` degC, no solar, no wind, humidity 50 %.  Exercises
    the controller's ability to stabilise room temperature around the
    setpoint with pure disturbance rejection (no transients).

    Returns:
        ``SimScenario`` on the ``well_insulated`` building, 48 h heating.
    """
    weather = SyntheticWeather.constant(
        T_out=0.0,
        GHI=0.0,
        wind_speed=0.0,
        humidity=50.0,
    )
    return SimScenario(
        name="steady_heating",
        building=well_insulated(),
        weather=weather,
        duration_minutes=2880,
        mode=Mode.HEATING,
        dt_seconds=60.0,
        description=(
            "Steady-state heating at T_out=0 degC, no solar. "
            "Tests temperature stabilization around the setpoint."
        ),
    )


def cold_snap() -> SimScenario:
    """Step drop from 0 degC to -15 degC after 24 h.

    Multi-room ``modern_bungalow`` building.  The first 24 h let the house
    reach equilibrium; the step then tests recovery and UFH heat-up under a
    severe cold load with light wind and 60 % humidity.

    Returns:
        ``SimScenario`` on the ``modern_bungalow`` building, 5-day heating.
    """
    weather = SyntheticWeather(
        t_out=ChannelProfile(
            kind=ProfileKind.STEP,
            baseline=0.0,
            amplitude=-15.0,
            step_time_minutes=1440.0,
        ),
        ghi=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=0.0),
        wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=2.0),
        humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=60.0),
    )
    return SimScenario(
        name="cold_snap",
        building=modern_bungalow(),
        weather=weather,
        duration_minutes=7200,
        mode=Mode.HEATING,
        dt_seconds=60.0,
        description=(
            "Step drop from 0 degC to -15 degC after 24 h. "
            "Tests UFH heat-up and recovery under severe cold."
        ),
    )


def solar_overshoot() -> SimScenario:
    """March-like conditions with strong daytime solar gains.

    Sinusoidal ``T_out`` (mean 5 degC, amplitude 8 degC) and GHI (mean 250,
    amplitude 250 W/m^2) on a 24 h period drive large daytime free heat.
    Exercises the trend-damping ("czlon trendu") anti-overshoot path: the
    valve must back off as the room rises toward the setpoint under solar
    load.

    Returns:
        ``SimScenario`` on the ``modern_bungalow`` building, 3-day heating.
    """
    weather = SyntheticWeather(
        t_out=ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=5.0,
            amplitude=8.0,
            period_minutes=1440.0,
        ),
        ghi=ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=250.0,
            amplitude=250.0,
            period_minutes=1440.0,
        ),
        wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=1.5),
        humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=55.0),
    )
    return SimScenario(
        name="solar_overshoot",
        building=modern_bungalow(),
        weather=weather,
        duration_minutes=4320,
        mode=Mode.HEATING,
        dt_seconds=60.0,
        description=(
            "March-like conditions with strong solar gains (GHI up to "
            "500 W/m^2). Tests trend-damping overshoot prevention."
        ),
    )


def spring_transition() -> SimScenario:
    """Spring shoulder season exercising transitional valve-parking.

    :attr:`Mode.TRANSITIONAL` parks all UFH valves at 0.  ``T_out`` ramps
    from 5 degC to 22 degC over 5 days with a daily solar cycle, so rooms
    drift across the setpoint, yet the controller must keep every valve
    parked regardless of the sign of the error.  The ``modern_bungalow``
    rooms have no fast source, so this scenario verifies pure valve-parking
    (valve=0, fast source OFF) across the setpoint crossing; it does not
    exercise fast-source bidirectional control.

    Returns:
        ``SimScenario`` on the ``modern_bungalow`` building, 7-day
        transitional run.
    """
    weather = SyntheticWeather(
        t_out=ChannelProfile(
            kind=ProfileKind.RAMP,
            baseline=5.0,
            amplitude=17.0,
            period_minutes=7200.0,
        ),
        ghi=ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=200.0,
            amplitude=200.0,
            period_minutes=1440.0,
        ),
        wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=1.5),
        humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=55.0),
    )
    return SimScenario(
        name="spring_transition",
        building=modern_bungalow(),
        weather=weather,
        duration_minutes=10080,
        mode=Mode.TRANSITIONAL,
        dt_seconds=60.0,
        description=(
            "Spring transition: T_out ramps 5 degC -> 22 degC over 5 days "
            "in transitional mode. Verifies valves stay parked (valve=0, "
            "fast source OFF) across the setpoint crossing."
        ),
    )


def hot_july_floor_cooling() -> SimScenario:
    """Hot, humid July driving floor cooling into dew-point protection.

    :attr:`Mode.COOLING` with a sinusoidal ``T_out`` (mean 30 degC, amplitude
    5 degC) and strong solar (GHI mean 400, amplitude 400 W/m^2) pushes rooms
    above the setpoint so floor cooling engages.  Humidity is held high at
    80 % so the room dew point is elevated (~21 degC at 25 degC air), forcing
    the per-room S2 throttle and the building-level safe dew-point limit to
    act.

    Returns:
        ``SimScenario`` on the ``modern_bungalow`` building, 48 h cooling.
    """
    weather = SyntheticWeather(
        t_out=ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=30.0,
            amplitude=5.0,
            period_minutes=1440.0,
        ),
        ghi=ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=400.0,
            amplitude=400.0,
            period_minutes=1440.0,
        ),
        wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=1.0),
        humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=80.0),
    )
    return SimScenario(
        name="hot_july_floor_cooling",
        building=modern_bungalow(),
        weather=weather,
        duration_minutes=2880,
        mode=Mode.COOLING,
        dt_seconds=60.0,
        description=(
            "Hot July (T_out ~25-35 degC) at 80 % humidity in cooling mode. "
            "High dew point exercises the S2 condensation throttle and the "
            "global safe dew-point limit."
        ),
    )


def sensor_dropout() -> SimScenario:
    """Cold day with heavy measurement noise to stress the degrade path.

    Constant ``T_out=-2`` degC heating with an elevated sensor-noise standard
    deviation (2.0 K).  The noise drives the room-temperature reading through
    unavailable/erratic values, exercising the controller's safe-degrade
    behaviour (hold last valve, fast source OFF, ``"sensor_lost"`` flag) as
    the test harness masks readings to ``None`` during the dropout window.

    Returns:
        ``SimScenario`` on the ``well_insulated`` building, 24 h heating with
        ``sensor_noise_std=2.0``.
    """
    weather = SyntheticWeather.constant(
        T_out=-2.0,
        GHI=0.0,
        wind_speed=1.0,
        humidity=55.0,
    )
    return SimScenario(
        name="sensor_dropout",
        building=well_insulated(),
        weather=weather,
        duration_minutes=1440,
        mode=Mode.HEATING,
        dt_seconds=60.0,
        sensor_noise_std=2.0,
        description=(
            "Cold day (T_out=-2 degC) with heavy sensor noise (std=2.0 K). "
            "Tests safe degrade when the room-temperature sensor drops out."
        ),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SCENARIO_LIBRARY: dict[str, Callable[[], SimScenario]] = {
    "steady_heating": steady_heating,
    "cold_snap": cold_snap,
    "solar_overshoot": solar_overshoot,
    "spring_transition": spring_transition,
    "hot_july_floor_cooling": hot_july_floor_cooling,
    "sensor_dropout": sensor_dropout,
}
"""Mapping of scenario name to factory function."""
