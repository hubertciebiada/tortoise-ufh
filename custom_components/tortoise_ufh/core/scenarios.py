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

    from custom_components.tortoise_ufh.core.scenarios import (
        SCENARIO_LIBRARY,
        steady_heating,
    )

    scenario = steady_heating()
    assert scenario.name == "steady_heating"

    # Or via the lookup dict:
    factory = SCENARIO_LIBRARY["cold_snap"]
    scenario = factory()
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .building_profiles import (
    modern_bungalow,
    well_insulated,
    well_insulated_with_split,
)
from .config import SimScenario
from .models import Mode
from .weather import ChannelProfile, ProfileKind, SyntheticWeather
from .weather_comp import WeatherCompCurve

__all__ = [
    "SCENARIO_LIBRARY",
    "cold_snap",
    "hot_july_floor_cooling",
    "night_setback",
    "sensor_dropout",
    "solar_overshoot",
    "split_boost",
    "spring_transition",
    "steady_heating",
]

# Shared heating weather-compensation curve for the heating scenarios
# (2026-07-09): a realistic UFH curve instead of the 35 degC constant fallback,
# so cold_snap tests regulation rather than a saturated, over-hot plant.
# t_supply_max stays below the S1 floor-overheat trip (40 degC).
_HEATING_CURVE = WeatherCompCurve(
    t_supply_base=22.0,
    slope=0.5,
    t_neutral=15.0,
    t_supply_max=38.0,
    t_supply_min=20.0,
)


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
        weather_comp=_HEATING_CURVE,
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
        weather_comp=_HEATING_CURVE,
    )


def solar_overshoot() -> SimScenario:
    """March-like conditions with strong daytime solar gains.

    Sinusoidal ``T_out`` (mean 5 degC, amplitude 8 degC) and GHI (mean 250,
    amplitude 250 W/m^2) on a 24 h period drive large daytime free heat.

    What this scenario really tests (corrected 2026-07-12, K2/B1): that the
    controller NEVER ADDS heat to a sunlit surplus — valves fully closed
    whenever free gains carry a room clearly above its setpoint. It does NOT
    exercise the ``kt`` trend damping: the solar gains (~1.65 kW peak in the
    living room) dominate with the valves already closed, and the measured
    peak overshoot is IDENTICAL for kt=12 and kt=0 (+5.71 K both). The
    earlier docstring claiming this as the trend-member gate was wrong.

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
        weather_comp=_HEATING_CURVE,
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
        building=modern_bungalow(t_ground=16.0),
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
    """Hot July driving floor cooling into dew-point protection.

    :attr:`Mode.COOLING` with a sinusoidal ``T_out`` (mean 28 degC, amplitude
    6 degC), summer ground temperature (17 degC) and strong solar (GHI mean
    400, amplitude 400 W/m^2) push rooms above the setpoint so floor cooling
    engages.  Outdoor humidity of 45 % plus the twin's indoor occupancy
    vapour surplus yields an indoor dew point around 16-18 degC — close
    enough to the ~18 degC chilled supply that the per-room S2 throttle and
    the building-level safe dew-point limit genuinely modulate the cooling
    (recalibrated 2026-07-09: the old 80 % RH corresponded to a tropical dew
    point at which floor cooling is physically impossible).

    Returns:
        ``SimScenario`` on the ``modern_bungalow`` building, 48 h cooling.
    """
    # GHI is deliberately moderate (mean 150, peak 300 W/m^2 effective): in the
    # cooling season external blinds shade the south glazing, so only a
    # fraction of the clear-sky irradiance reaches the rooms. Unshaded summer
    # sun (~800 W/m^2 through 8 m^2 of glass) overwhelms gentle floor cooling
    # by 3-4x regardless of the controller — physically true, but useless as a
    # regulation gate.
    # Known artifact (B9, 2026-07-12): the OUTDOOR relative humidity is a
    # CONSTANT 45 % while T_out swings sinusoidally, so the outdoor vapour
    # pressure — and with it the indoor dew point — peaks together with the
    # afternoon heat instead of staying roughly constant over the day. This
    # exaggerates the diurnal dew-point swing and aligns the dew peak with
    # peak cooling demand: a conservative, protection-friendly bias. Treat
    # this scenario as the CONDENSATION-PROTECTION gate, not a comfort gate.
    weather = SyntheticWeather(
        t_out=ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=28.0,
            amplitude=6.0,
            period_minutes=1440.0,
        ),
        ghi=ChannelProfile(
            kind=ProfileKind.SINUSOIDAL,
            baseline=150.0,
            amplitude=150.0,
            period_minutes=1440.0,
        ),
        wind_speed=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=1.0),
        humidity=ChannelProfile(kind=ProfileKind.CONSTANT, baseline=45.0),
    )
    return SimScenario(
        name="hot_july_floor_cooling",
        # Summer target 24 degC (a 21 degC cooling setpoint would be a heating
        # habit, not a July reality) and summer ground temperature.
        building=replace(modern_bungalow(t_ground=17.0), home_setpoint_c=24.0),
        weather=weather,
        duration_minutes=2880,
        mode=Mode.COOLING,
        dt_seconds=60.0,
        description=(
            "Hot July (T_out ~22-34 degC) in cooling mode with summer ground. "
            "The indoor dew point sits near the chilled-supply temperature, "
            "exercising the S2 condensation throttle and the global safe "
            "dew-point limit."
        ),
        # A July house does not start at the 20 degC winter reset state — a
        # cold start under summer vapour pressure would spend hours at
        # RH ~100 % (an artifact, not a controllable condition).
        initial_temperature_c=24.0,
    )


def night_setback() -> SimScenario:
    """Daily 21 <-> 19 degC setpoint schedule on the multi-room bungalow.

    The operating-point scenario the steady-state gate never exercised
    (K1, 2026-07-12): a night setback is DAILY use, and round 2 measured that
    a lowered setpoint left the saturated integral actively heating an
    already-too-warm room for 17.4 h (642 %*h of valve integral). Constant
    ``T_out = 0`` degC, no sun, so every response is the controller's own.
    The home setpoint alternates 21 -> 19 -> 21 ... every 12 h for 4 days via
    ``setpoint_schedule``; the gate asserts a bounded heating integral while
    a room sits above the band, a bounded undershoot, and a prompt
    valve-close after each setback (bumpless transfer + asymmetric unwind).

    Returns:
        ``SimScenario`` on the ``modern_bungalow`` building, 4-day heating
        with a 12 h setpoint schedule.
    """
    weather = SyntheticWeather.constant(
        T_out=0.0,
        GHI=0.0,
        wind_speed=1.0,
        humidity=60.0,
    )
    return SimScenario(
        name="night_setback",
        building=modern_bungalow(),
        weather=weather,
        duration_minutes=5760,
        mode=Mode.HEATING,
        dt_seconds=60.0,
        description=(
            "Daily 21 <-> 19 degC setpoint schedule (12 h period) at "
            "T_out=0 degC, no sun. Tests the bumpless setpoint transfer and "
            "the asymmetric integrator unwind on operating-point changes."
        ),
        weather_comp=_HEATING_CURVE,
        setpoint_schedule=(
            (720.0, 19.0),
            (1440.0, 21.0),
            (2160.0, 19.0),
            (2880.0, 21.0),
            (3600.0, 19.0),
            (4320.0, 21.0),
            (5040.0, 19.0),
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


def split_boost() -> SimScenario:
    """Cold day with a split-assisted room exercising the boost machinery.

    A single ``well_insulated_with_split`` room (2.5 kW split, MIMO RC input)
    at constant ``T_out=-5`` degC with a +2 K room offset: the initial error
    of ~3 K exceeds ``boost_offset_c`` so the split engages, the floor stays
    the base source (anti priority-inversion), and once the room enters the
    comfort band the split must release through the min-ON dwell.  Closes the
    "zero split scenarios in the gate" gap (2026-07-09).

    Returns:
        ``SimScenario`` on ``well_insulated_with_split``, 24 h heating.
    """
    weather = SyntheticWeather.constant(
        T_out=-5.0,
        GHI=0.0,
        wind_speed=1.0,
        humidity=50.0,
    )
    return SimScenario(
        name="split_boost",
        building=well_insulated_with_split(),
        weather=weather,
        duration_minutes=1440,
        mode=Mode.HEATING,
        dt_seconds=60.0,
        description=(
            "Cold day (T_out=-5 degC) with a 2.5 kW split boost: engage past "
            "boost_offset_c, floor stays base, release inside the comfort "
            "band through the min-ON dwell."
        ),
        room_offsets={"main": 2.0},
        weather_comp=_HEATING_CURVE,
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
    "night_setback": night_setback,
    "sensor_dropout": sensor_dropout,
    "split_boost": split_boost,
}
"""Mapping of scenario name to factory function."""
