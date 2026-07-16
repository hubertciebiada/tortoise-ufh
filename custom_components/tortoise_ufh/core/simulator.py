"""Building simulation engine (digital twin) for the tortoise-ufh core.

Provides the offline simulation harness that closes the control loop against a
pure-Python physics model, so :class:`~tortoise_ufh.controller.BuildingController`
can be exercised identically in tests and inside Home Assistant.

Three cooperating layers:

* :class:`SimulatedRoom` — owns one room's :class:`~tortoise_ufh.rc_model.RCModel`
  thermal state and actuator state; propagates the physics one tick at a time.
* :class:`BuildingSimulator` — orchestrates time, finite heat-pump power sharing,
  and weather, and — crucially — produces the **same**
  :class:`~tortoise_ufh.models.RoomInputs` the HA coordinator builds so the
  controller sees an identical black-box contract offline and online.
* :class:`HeatPumpMode` — the heat-pump operating mode of the twin.

Key design rules honoured here:

* This module is part of the pure core and MUST NOT import ``homeassistant``.
* ``T_slab`` is ground truth inside the twin and is **never** placed into
  :class:`~tortoise_ufh.models.RoomInputs` — the controller must not see it.
* Sensor noise corrupts only the measurement *snapshot* (``room_temperature_c``),
  never the physical state.

The stepping convention is zero-order hold (ZOH): actions are applied at the
start of a step, held constant for the step duration, and the RC model
propagates the state.

Units:
    Temperatures: degC
    Power: W
    Valve position: 0-100 % (float)
    Humidity: 0-100 %
    Time: minutes (simulation convention); RCModel dt in seconds.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from .config import Orientation, WindowConfig
from .models import (
    FastSourceKind,
    FastSourceMode,
    LoopInput,
    Mode,
    RoomInputs,
    RoomOutputs,
)
from .rc_model import RCModel
from .sensor_noise import SensorNoise
from .ufh_loop import (
    DEFAULT_DT_COOLING,
    DEFAULT_DT_HEATING,
    LoopGeometry,
    loop_power,
)
from .weather import WeatherPoint, WeatherSource
from .weather_comp import CoolingCompCurve, WeatherCompCurve

__all__ = [
    "BuildingSimulator",
    "HeatPumpMode",
    "SimulatedRoom",
]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class HeatPumpMode(Enum):
    """Operating mode of the (single, shared) heat pump in the twin."""

    HEATING = "heating"
    COOLING = "cooling"
    OFF = "off"


# Fallback supply temperatures — used only when no weather-compensation curve
# is supplied.  They sit inside the default clamp ranges of ``WeatherCompCurve``
# (>= 20 C) and ``CoolingCompCurve`` (>= 16 C) so they remain representative of
# a real heat-pump operating point.
_FALLBACK_T_SUPPLY_HEATING_C: float = 35.0
"""Fallback UFH supply temperature when no WeatherCompCurve is provided [degC]."""

_FALLBACK_T_SUPPLY_COOLING_C: float = 18.0
"""Fallback UFH supply temperature when no CoolingCompCurve is provided [degC]."""

_DEFAULT_SETPOINT_C: float = 21.0
"""Default per-room setpoint used until overridden via ``set_setpoint`` [degC]."""

# -- Loop water-probe thermal model (S6, 2026-07-13) ---------------------------
#
# Before S6 the twin was idealised: the loop probes reported the SOURCE
# signature (supply = t_supply, return = supply -/+ delta-T) regardless of the
# valve position, so a loop commanded 0 % for hours still "flowed" — exactly
# the stuck-open signature. The probes are now actuation-aware
# UNCONDITIONALLY (an opt-in flag would have left the old scenarios with
# unphysical readings and false S6 alarms): with flow the probes relax fast
# to the source targets; in stagnation both relax to the slab temperature.

_PROBE_FLOW_TAU_S: float = 120.0
"""Probe relaxation time constant WITH flow [s] — pipe water renews quickly."""

_PROBE_STAGNATION_TAU_S: float = 600.0
"""Probe relaxation time constant in STAGNATION [s].

Chosen deliberately: after flow stops, the pipe water near the probes
equilibrates with the slab/manifold environment within tens of minutes —
much faster than the slab's own multi-hour tau. With this value the residual
supply-return difference of a freshly closed healthy loop decays below the
default ``flow_epsilon_k`` (0.3 K) in ~28 min, comfortably inside the 45-min
S6 stuck-open window, so healthy scenario runs raise no flags (acceptance
criterion 4)."""

_PROBE_STAGNATION_MAX_VALVE_PCT: float = 5.0
"""Physical valve opening at/below which the loop water STAGNATES [%].

A thermal/gear actuator has dead travel near fully-closed, so the sub-5 %
closing tails the PI emits while parking a room do not establish a
measurable through-flow — the probes must decay toward the slab exactly as
they do at a hard 0 %. Matches the actuator write threshold, so a loop
commanded closed never keeps a synthetic source signature in the twin, and a
healthy closing tail cannot fake a hydraulic response (acceptance
criterion 4)."""

# Mapping from the twin's heat-pump mode to the controller's room mode.  The
# twin has no TRANSITIONAL state (that is a controller-only concept), so only
# the three shared modes appear here.
_HP_MODE_TO_MODE: dict[HeatPumpMode, Mode] = {
    HeatPumpMode.HEATING: Mode.HEATING,
    HeatPumpMode.COOLING: Mode.COOLING,
    HeatPumpMode.OFF: Mode.OFF,
}

# -- Solar gains through windows (calibration amendment 2026-07-09) -----------
#
# ``q_sol = GHI * sum_over_windows(area * g_value * f_orient(time-of-day))``.
# The orientation factor is a deliberately simple transposition model: a fixed
# diffuse share for every glazing plus a direct share following a cosine
# envelope around the orientation's peak hour. The synthetic GHI sinus used by
# the scenario library peaks at t = period/4 (minute 360 of each day), so SOLAR
# NOON is pinned to ``_SOLAR_NOON_MINUTE`` to stay consistent with those
# profiles.

_MINUTES_PER_DAY: float = 1440.0
"""Minutes in one day, for the solar time-of-day phase."""

_SOLAR_NOON_MINUTE: float = 360.0
"""Minute-of-day treated as solar noon [min].

Matches the scenario library's sinusoidal GHI profiles
(``baseline + amp * sin(2*pi*t/1440)``), whose daily peak falls at
t mod 1440 = 360.
"""

_SOLAR_DIFFUSE_FACTOR: float = 0.15
"""Diffuse (sky) share of GHI transmitted regardless of orientation [-]."""

_SOLAR_DIRECT_FACTOR: dict[Orientation, float] = {
    Orientation.SOUTH: 0.65,
    Orientation.EAST: 0.45,
    Orientation.WEST: 0.45,
    Orientation.NORTH: 0.0,
}
"""Peak direct-beam transposition factor per facade orientation [-]."""

_SOLAR_PEAK_OFFSET_H: dict[Orientation, float] = {
    Orientation.SOUTH: 0.0,
    Orientation.EAST: -3.0,
    Orientation.WEST: 3.0,
    Orientation.NORTH: 0.0,
}
"""Hours relative to solar noon at which each facade sees peak direct sun."""


def _window_solar_gain_w(
    windows: tuple[WindowConfig, ...],
    ghi_w_m2: float,
    t_minutes: float,
) -> float:
    """Compute a room's through-window solar gain [W].

    Args:
        windows: The room's glazing configuration.
        ghi_w_m2: Global Horizontal Irradiance at this instant [W/m^2].
        t_minutes: Simulation time [min] (t=0 == midnight).

    Returns:
        Total transmitted solar power [W] (>= 0).
    """
    if ghi_w_m2 <= 0.0 or not windows:
        return 0.0
    day_phase = (t_minutes - _SOLAR_NOON_MINUTE) % _MINUTES_PER_DAY
    hour_angle = 2.0 * math.pi * day_phase / _MINUTES_PER_DAY
    total = 0.0
    for window in windows:
        offset = 2.0 * math.pi * _SOLAR_PEAK_OFFSET_H[window.orientation] / 24.0
        direct = _SOLAR_DIRECT_FACTOR[window.orientation] * max(
            0.0, math.cos(hour_angle - offset)
        )
        factor = _SOLAR_DIFFUSE_FACTOR + direct
        total += ghi_w_m2 * window.area_m2 * window.g_value * factor
    return total


# -- Indoor humidity model (thermal-D2, 2026-07-09) ---------------------------
#
# Indoor air carries the OUTDOOR absolute humidity plus a constant vapour
# surplus from occupancy (cooking, showers, people) diluted by ventilation.
# ``RH_in = e_in / e_sat(T_air)`` then yields a credible per-room dew point,
# instead of the old unphysical "indoor RH = outdoor RH" shortcut.

_INDOOR_VAPOUR_SURPLUS_HPA: float = 3.3
"""Constant indoor vapour-pressure surplus over outdoor [hPa].

Equivalent to ~2 g/kg absolute-humidity rise — a typical steady-state
occupancy moisture load at normal residential ventilation rates.
"""

_DRY_SENSIBLE_FRACTION: float = 0.3
"""Sensible cooling delivered by a split's DRY mode, as a fraction of its
rated power (dry assist, §24).

The twin has no moisture state, so a DRY command's latent removal cannot be
modelled; only this small sensible side-effect enters the thermal physics.
"""


def _saturation_vapour_pressure_hpa(t_c: float) -> float:
    """Magnus saturation vapour pressure [hPa] over water.

    Args:
        t_c: Air temperature [degC].

    Returns:
        Saturation vapour pressure [hPa].
    """
    return 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))


def _indoor_humidity_pct(t_air_c: float, weather: WeatherPoint) -> float:
    """Compute the indoor relative humidity from outdoor conditions [%].

    Args:
        t_air_c: Indoor air temperature [degC].
        weather: Current outdoor weather (temperature + relative humidity).

    Returns:
        Indoor relative humidity [%], clamped to ``[1, 100]``.
    """
    e_out = weather.humidity / 100.0 * _saturation_vapour_pressure_hpa(weather.T_out)
    e_in = e_out + _INDOOR_VAPOUR_SURPLUS_HPA
    rh_in = 100.0 * e_in / _saturation_vapour_pressure_hpa(t_air_c)
    return min(100.0, max(1.0, rh_in))


# ---------------------------------------------------------------------------
# SimulatedRoom — per-room physics + actuator bridge
# ---------------------------------------------------------------------------


class SimulatedRoom:
    """One room's thermal state and actuators, wrapping an :class:`RCModel`.

    Holds the ground-truth state vector ``x`` (``[T_air, T_slab, T_wall]`` for
    3R3C) and the last-applied actuator commands, and propagates the physics one
    discrete tick at a time.  ``T_slab`` is ground truth for metrics/plots and is
    never exposed to the controller.

    Typical usage::

        geom = LoopGeometry(
            effective_pipe_length_m=130.0, pipe_spacing_m=0.15,
            pipe_diameter_outer_mm=16.0, pipe_wall_thickness_mm=2.0, area_m2=20.0,
        )
        room = SimulatedRoom("living", model, loop_geometry=geom)
        room.apply_actions(valve_position=50.0)
        room.step_with_power(weather_point, q_floor_w=500.0)
    """

    def __init__(
        self,
        name: str,
        model: RCModel,
        *,
        n_loops: int = 1,
        fast_source_power_w: float = 0.0,
        fast_source_kind: FastSourceKind = FastSourceKind.NONE,
        cooling_enabled: bool = True,
        q_int_w: float = 0.0,
        windows: tuple[WindowConfig, ...] = (),
        initial_temperature_c: float | None = None,
        loop_geometry: LoopGeometry,
        supply_probe_on_manifold: bool = False,
    ) -> None:
        """Initialize a simulated room.

        Args:
            name: Human-readable room identifier (must be non-empty).
            model: The room's RC thermal model (the physics engine).
            n_loops: Number of UFH loops that share the room's valve (>= 1).
            fast_source_power_w: Nominal fast-source (split/heater) power [W]
                (>= 0). Applied as convective heat when the room's model has a
                fast source (MIMO input).
            fast_source_kind: Kind of fast auxiliary source the room exposes to
                the controller via :class:`RoomInputs`. Must be paired with a
                positive ``fast_source_power_w`` when not ``NONE``.
            cooling_enabled: Whether the room participates in floor cooling
                (mirrors ``RoomConfig.cooling_enabled``); surfaced to the
                controller so per-room cooling opt-out is honoured in the twin.
            q_int_w: Constant internal heat gain [W] (>= 0).
            windows: The room's glazing (orientation, area, g-value) used by
                the through-window solar-gain model (2026-07-09). Empty means
                no solar gain.
            initial_temperature_c: Optional initial temperature applied to
                every thermal node [degC]; ``None`` keeps the model's 20 degC
                reset default (2026-07-09 — summer runs start summer-warm).
            loop_geometry: Pipe/floor geometry for the EN 1264 power model
                (required so ``ufh_loop.loop_power`` can be evaluated).
            supply_probe_on_manifold: When ``True``, the SUPPLY probe sits on
                the manifold bar BEFORE the valve (S6, 2026-07-13): in
                stagnation it stays at the source temperature while only the
                return probe relaxes to the slab — the installation variant
                that makes the return probe the decisive flow witness.

        Raises:
            ValueError: If ``name`` is empty, ``n_loops`` < 1,
                ``fast_source_power_w`` / ``q_int_w`` is negative, or
                ``fast_source_kind`` is not ``NONE`` while
                ``fast_source_power_w`` is not positive.
        """
        if not name or not name.strip():
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if n_loops < 1:
            msg = f"n_loops must be >= 1, got {n_loops}"
            raise ValueError(msg)
        if fast_source_power_w < 0.0:
            msg = f"fast_source_power_w must be >= 0, got {fast_source_power_w}"
            raise ValueError(msg)
        if fast_source_kind is not FastSourceKind.NONE and fast_source_power_w <= 0.0:
            msg = (
                "fast_source_power_w must be > 0 when fast_source_kind is not "
                f"NONE, got {fast_source_power_w}"
            )
            raise ValueError(msg)
        if q_int_w < 0.0:
            msg = f"q_int_w must be >= 0, got {q_int_w}"
            raise ValueError(msg)

        self._name = name
        self._model = model
        self._n_loops = n_loops
        self._fast_source_power_w = float(fast_source_power_w)
        self._fast_source_kind = fast_source_kind
        self._cooling_enabled = cooling_enabled
        self._q_int_w = float(q_int_w)
        self._windows = windows
        self._loop_geometry = loop_geometry

        self._x: NDArray[np.float64] = model.reset()
        if initial_temperature_c is not None:
            self._x = np.full(model.n_states, float(initial_temperature_c))
        # Commanded vs physical valve split (S6, 2026-07-13): normally
        # identical; the fault-injection API below can freeze the physical
        # actuator (targets accepted, never executed) and/or make the
        # reported feedback ECHO the command — the incident of issue #4.
        self._commanded_valve: float = 0.0
        self._physical_valve: float = 0.0
        self._actuator_frozen: bool = False
        self._echo_feedback: bool = False
        self._supply_probe_on_manifold = supply_probe_on_manifold
        # Loop water-probe thermal state (S6): ``None`` while the pump is
        # OFF; initialised to the flowing targets on the first pump-ON step
        # so healthy runs read identically to the pre-S6 twin.
        self._probe_supply_c: float | None = None
        self._probe_return_c: float | None = None
        self._applied_fast_source_power_w: float = 0.0

    # -- Properties ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the room name."""
        return self._name

    @property
    def n_loops(self) -> int:
        """Return the number of UFH loops sharing the room's valve."""
        return self._n_loops

    @property
    def fast_source_power_w(self) -> float:
        """Return the room's nominal fast-source power [W]."""
        return self._fast_source_power_w

    @property
    def fast_source_kind(self) -> FastSourceKind:
        """Return the kind of fast auxiliary source the room exposes."""
        return self._fast_source_kind

    @property
    def fast_source_on(self) -> bool:
        """Return whether a non-zero fast-source power is currently applied."""
        return self._applied_fast_source_power_w != 0.0

    @property
    def cooling_enabled(self) -> bool:
        """Return whether the room participates in floor cooling."""
        return self._cooling_enabled

    @property
    def dt_seconds(self) -> float:
        """Return the room model's discretization step [s]."""
        return self._model.dt

    @property
    def loop_geometry(self) -> LoopGeometry:
        """Return the room's UFH loop geometry."""
        return self._loop_geometry

    @property
    def windows(self) -> tuple[WindowConfig, ...]:
        """Return the room's glazing configuration (solar-gain model)."""
        return self._windows

    @property
    def state(self) -> NDArray[np.float64]:
        """Return a copy of the ground-truth state vector [degC]."""
        return self._x.copy()

    @property
    def T_air(self) -> float:
        """Return the air-node temperature ``x[0]`` [degC]."""
        return float(self._x[0])

    @property
    def T_slab(self) -> float:
        """Return the slab-node temperature ``x[1]`` [degC] (ground truth)."""
        return float(self._x[1])

    @property
    def valve_position(self) -> float:
        """Return the PHYSICAL valve position [0-100 %] (drives the physics)."""
        return self._physical_valve

    @property
    def commanded_valve_position(self) -> float:
        """Return the last COMMANDED valve position [0-100 %] (S6)."""
        return self._commanded_valve

    @property
    def echo_feedback(self) -> bool:
        """Whether the valve feedback echoes the command (fault injection)."""
        return self._echo_feedback

    @property
    def probe_supply_c(self) -> float | None:
        """Loop supply-probe temperature [degC], or ``None`` (pump OFF)."""
        return self._probe_supply_c

    @property
    def probe_return_c(self) -> float | None:
        """Loop return-probe temperature [degC], or ``None`` (pump OFF)."""
        return self._probe_return_c

    # -- Actuation + physics -------------------------------------------------

    def set_actuator_fault(
        self, *, frozen: bool = False, echo_feedback: bool = False
    ) -> None:
        """Inject (or clear) the issue-#4 actuator fault (S6, 2026-07-13).

        Args:
            frozen: ``True`` freezes the PHYSICAL valve at its current
                position — targets keep being accepted but are never
                executed (the incident's motor-MCU state). ``False`` releases
                the freeze (the physical valve snaps to the current command
                on the next :meth:`apply_actions`).
            echo_feedback: ``True`` makes the reported feedback
                (``LoopInput.valve_position_pct``) ECHO the commanded value
                instead of the physical one — the bridge publishing the
                requested position, which blinded ``valve_mismatch``.
        """
        self._actuator_frozen = frozen
        self._echo_feedback = echo_feedback

    def apply_actions(
        self,
        valve_position: float,
        fast_source_power_w: float = 0.0,
    ) -> None:
        """Apply actuator commands for the upcoming step.

        Args:
            valve_position: Desired UFH valve position [0-100 %]. Clamped
                defensively to the valid range. With a ``frozen`` actuator
                fault the command is recorded but the physical valve does
                not move.
            fast_source_power_w: Signed fast-source power to apply [W]
                (positive heating, negative cooling). Its magnitude is capped
                at the room's nominal ``fast_source_power_w``.
        """
        self._commanded_valve = max(0.0, min(100.0, valve_position))
        if not self._actuator_frozen:
            self._physical_valve = self._commanded_valve
        capped = max(
            -self._fast_source_power_w,
            min(self._fast_source_power_w, fast_source_power_w),
        )
        self._applied_fast_source_power_w = capped

    def update_loop_probes(
        self,
        *,
        flowing_supply_c: float | None,
        flowing_return_c: float | None,
        dt_seconds: float,
    ) -> None:
        """Relax the loop water probes one tick (S6, 2026-07-13).

        With the pump OFF the probes read ``None`` (pre-S6 behaviour, keeps
        ``spring_transition`` untouched). On the first pump-ON step they
        initialise directly to the flowing targets (healthy runs identical
        to the pre-S6 twin from the first step). Afterwards: with flow
        (physical valve above :data:`_PROBE_STAGNATION_MAX_VALVE_PCT`) they
        relax fast (:data:`_PROBE_FLOW_TAU_S`) to the source targets; in
        stagnation both relax slowly (:data:`_PROBE_STAGNATION_TAU_S`) to
        the slab temperature — except a manifold-mounted supply probe, which
        stays at the source.

        Args:
            flowing_supply_c: The source supply temperature [degC], or
                ``None`` while the pump is OFF.
            flowing_return_c: The flowing return target [degC]
                (``supply -/+`` the EN 1264 default delta-T), or ``None``.
            dt_seconds: Physics tick [s].
        """
        if flowing_supply_c is None or flowing_return_c is None:
            self._probe_supply_c = None
            self._probe_return_c = None
            return
        if self._probe_supply_c is None or self._probe_return_c is None:
            self._probe_supply_c = flowing_supply_c
            self._probe_return_c = flowing_return_c
            return
        if self._physical_valve > _PROBE_STAGNATION_MAX_VALVE_PCT:
            tau = _PROBE_FLOW_TAU_S
            supply_target = flowing_supply_c
            return_target = flowing_return_c
        else:
            tau = _PROBE_STAGNATION_TAU_S
            supply_target = (
                flowing_supply_c if self._supply_probe_on_manifold else self.T_slab
            )
            return_target = self.T_slab
        alpha = 1.0 - math.exp(-dt_seconds / tau)
        self._probe_supply_c += alpha * (supply_target - self._probe_supply_c)
        self._probe_return_c += alpha * (return_target - self._probe_return_c)

    def step_with_power(
        self,
        weather: WeatherPoint,
        q_floor_w: float,
        q_sol_w: float = 0.0,
    ) -> None:
        """Propagate the RC state one tick with the given floor power.

        Builds the control input ``u`` (SISO ``[Q_floor]`` or MIMO
        ``[Q_conv, Q_floor]``) and the disturbance ``d``
        (``[T_out, Q_sol, Q_int]`` for 3R3C, ``[T_out, Q_sol]`` for 2R2C), then
        advances the model by one discrete step. Mutates the internal state
        vector in place.

        Args:
            weather: Weather conditions for this step (supplies ``T_out``).
            q_floor_w: UFH floor heat injected this step [W] (signed: positive
                heating, negative cooling).
            q_sol_w: Solar heat gain reaching the room this step [W] (>= 0).
        """
        if self._model.n_inputs == 2:
            u: NDArray[np.float64] = np.array(
                [self._applied_fast_source_power_w, q_floor_w],
                dtype=np.float64,
            )
        else:
            u = np.array([q_floor_w], dtype=np.float64)

        if self._model.n_disturbances == 3:
            d: NDArray[np.float64] = np.array(
                [weather.T_out, q_sol_w, self._q_int_w],
                dtype=np.float64,
            )
        else:
            d = np.array([weather.T_out, q_sol_w], dtype=np.float64)

        self._x = self._model.step(self._x, u, d)


# ---------------------------------------------------------------------------
# BuildingSimulator — time + HP-power orchestration
# ---------------------------------------------------------------------------


class BuildingSimulator:
    """Digital twin driving one or more :class:`SimulatedRoom` instances.

    Manages the simulation clock, finite heat-pump power sharing, and weather,
    and produces :class:`~tortoise_ufh.models.RoomInputs` snapshots that are
    structurally identical to the ones the Home Assistant coordinator assembles.
    This lets :class:`~tortoise_ufh.controller.BuildingController` close the loop
    identically offline and online.

    Per-room setpoints default to :data:`_DEFAULT_SETPOINT_C` and can be set via
    :meth:`set_setpoint` / :meth:`set_setpoints` (the constructor mirrors the
    frozen BUILD_SPEC signature and takes no setpoints).

    Typical single-room usage::

        room = SimulatedRoom("living", model, loop_geometry=geom)
        weather = SyntheticWeather.constant(T_out=-5.0)
        sim = BuildingSimulator(room, weather)
        inputs = sim.get_measurements()
        outputs = controller.step(inputs)
        sim.step(outputs)

    Typical multi-room usage::

        sim = BuildingSimulator(rooms, weather, hp_max_power_w=4900.0)
        all_inputs = sim.get_all_measurements()
        all_outputs = building_controller.step(all_inputs)
        sim.step_all({name: out for name, out in all_outputs.rooms.items()})
    """

    def __init__(
        self,
        rooms: SimulatedRoom | list[SimulatedRoom],
        weather: WeatherSource,
        *,
        hp_mode: HeatPumpMode = HeatPumpMode.HEATING,
        hp_max_power_w: float | None = None,
        sensor_noise: SensorNoise | None = None,
        weather_comp: WeatherCompCurve | None = None,
        cooling_comp: CoolingCompCurve | None = None,
    ) -> None:
        """Initialize the simulator.

        Args:
            rooms: A single :class:`SimulatedRoom` or a list of rooms.
            weather: Weather data source (any :class:`WeatherSource`).
            hp_mode: Initial heat-pump operating mode.
            hp_max_power_w: Total heat-pump thermal capacity [W]. ``None`` means
                unlimited (no scaling). Must be > 0 when provided.
            sensor_noise: Optional seeded Gaussian noise added to the measured
                ``room_temperature_c`` only (never to the physical state).
            weather_comp: Optional heating weather-compensation curve used to
                derive the UFH supply temperature. Falls back to
                :data:`_FALLBACK_T_SUPPLY_HEATING_C` when ``None``.
            cooling_comp: Optional cooling weather-compensation curve. Falls back
                to :data:`_FALLBACK_T_SUPPLY_COOLING_C` when ``None``.

        Raises:
            ValueError: If the room list is empty, room names are not unique, or
                ``hp_max_power_w`` is non-positive.
        """
        if isinstance(rooms, list):
            if len(rooms) == 0:
                msg = "rooms list must not be empty"
                raise ValueError(msg)
            names = [r.name for r in rooms]
            if len(names) != len(set(names)):
                duplicates = sorted({n for n in names if names.count(n) > 1})
                msg = f"room names must be unique, duplicates: {duplicates}"
                raise ValueError(msg)
            self._rooms: list[SimulatedRoom] = rooms
        else:
            self._rooms = [rooms]

        if hp_max_power_w is not None and hp_max_power_w <= 0:
            msg = f"hp_max_power_w must be > 0, got {hp_max_power_w}"
            raise ValueError(msg)

        self._weather = weather
        self._hp_mode = hp_mode
        self._hp_max_power_w = (
            hp_max_power_w if hp_max_power_w is not None else math.inf
        )
        self._sensor_noise = sensor_noise
        self._weather_comp = weather_comp
        self._cooling_comp = cooling_comp

        # Derive the clock step from the room physics so the weather timeline and
        # energy metrics stay in lock-step with the integrator's ``dt`` instead
        # of assuming a fixed 1-minute tick.  All rooms must share one ``dt``.
        dts = {r.dt_seconds for r in self._rooms}
        if len(dts) != 1:
            msg = f"all rooms must share one model dt, got {sorted(dts)}"
            raise ValueError(msg)
        self._time_minutes: float = 0.0
        self._dt_minutes: float = dts.pop() / 60.0

        self._setpoints: dict[str, float] = {
            r.name: _DEFAULT_SETPOINT_C for r in self._rooms
        }

        # Lower limit on the cooling supply temperature [degC] — normally the
        # controller's global safe dew point, closing the third output of the
        # contract in the twin (amendment 2026-07-09, I1). ``None`` = no limit.
        self._cooling_supply_floor_c: float | None = None

        # Diagnostics populated by ``_distribute_hp_power``.
        self._last_t_supply_c: float | None = None
        self._last_q_floor_w: dict[str, float] = {}

    # -- Properties ----------------------------------------------------------

    @property
    def time_minutes(self) -> int:
        """Return the current simulation time [minutes] (integer part)."""
        return int(self._time_minutes)

    @property
    def room(self) -> SimulatedRoom:
        """Return the first (or only) room, for single-room convenience."""
        return self._rooms[0]

    @property
    def rooms(self) -> dict[str, SimulatedRoom]:
        """Return all rooms as a name-keyed dictionary (fresh dict per call)."""
        return {r.name: r for r in self._rooms}

    @property
    def hp_mode(self) -> HeatPumpMode:
        """Return the current heat-pump operating mode."""
        return self._hp_mode

    @property
    def last_step_info(self) -> dict[str, object]:
        """Return diagnostics from the most recent power-distribution step.

        Returns:
            Dict with ``t_supply_c`` (float or ``None`` when the pump was OFF),
            ``q_floor_w`` (defensive copy of per-room allocated floor power [W]),
            and ``hp_mode`` (:class:`HeatPumpMode`).
        """
        return {
            "t_supply_c": self._last_t_supply_c,
            "q_floor_w": dict(self._last_q_floor_w),
            "hp_mode": self._hp_mode,
        }

    def set_hp_mode(self, mode: HeatPumpMode) -> None:
        """Update the heat-pump operating mode.

        Args:
            mode: The new heat-pump operating mode.
        """
        self._hp_mode = mode

    def set_cooling_supply_floor(self, floor_c: float | None) -> None:
        """Set the lower limit for the COOLING supply temperature.

        Models the heat pump honouring the controller's **global safe
        dew-point** output (the third output of the external contract): the
        chilled-water supply never goes below this value. Call each cycle with
        ``BuildingOutputs.global_safe_dew_point_c`` (amendment 2026-07-09).

        Args:
            floor_c: Lower supply limit [degC], or ``None`` for no limit.
        """
        self._cooling_supply_floor_c = floor_c

    def set_setpoint(self, room_name: str, setpoint_c: float) -> None:
        """Set one room's control setpoint used when building ``RoomInputs``.

        Args:
            room_name: Name of an existing room.
            setpoint_c: Target temperature [degC] (already ``home + offset``).

        Raises:
            ValueError: If ``room_name`` is not a known room.
        """
        if room_name not in self._setpoints:
            msg = f"unknown room name: {room_name!r}"
            raise ValueError(msg)
        self._setpoints[room_name] = setpoint_c

    def set_setpoints(self, setpoints: dict[str, float]) -> None:
        """Set several room setpoints at once.

        Args:
            setpoints: Mapping of room name to setpoint [degC]. Every key must
                be a known room.

        Raises:
            ValueError: If any key is not a known room.
        """
        unknown = sorted(set(setpoints) - set(self._setpoints))
        if unknown:
            msg = f"unknown room names in setpoints: {unknown}"
            raise ValueError(msg)
        self._setpoints.update(setpoints)

    # -- Private helpers -----------------------------------------------------

    def _apply_temp_noise(self, value: float) -> float:
        """Apply sensor noise to a single measured temperature [degC].

        Args:
            value: Clean measured temperature [degC].

        Returns:
            Noisy temperature, or the value unchanged when no noise is set.
        """
        if self._sensor_noise is None:
            return value
        return self._sensor_noise.corrupt(value)

    def _compute_t_supply(self, t_out: float) -> float:
        """Compute the UFH supply temperature for the current mode [degC].

        Uses the appropriate weather-compensation curve when configured,
        otherwise the mode's fallback constant. Callers must not use the value
        when the pump is OFF.

        Args:
            t_out: Current outdoor temperature [degC].

        Returns:
            Supply temperature [degC]; ``0.0`` when the pump is OFF.
        """
        if self._hp_mode == HeatPumpMode.HEATING:
            if self._weather_comp is not None:
                return self._weather_comp.t_supply(t_out)
            return _FALLBACK_T_SUPPLY_HEATING_C
        if self._hp_mode == HeatPumpMode.COOLING:
            base = (
                self._cooling_comp.t_supply(t_out)
                if self._cooling_comp is not None
                else _FALLBACK_T_SUPPLY_COOLING_C
            )
            if self._cooling_supply_floor_c is not None:
                return max(base, self._cooling_supply_floor_c)
            return base
        return 0.0

    def _build_loops(
        self,
        room: SimulatedRoom,
        t_supply: float | None,
    ) -> tuple[LoopInput, ...]:
        """Build the per-loop water probes + valve feedback for one room.

        All of a room's loops share the same valve and probe pair. Since S6
        (2026-07-13) the probes come from the room's actuation-aware thermal
        model (:meth:`SimulatedRoom.update_loop_probes`): with real flow they
        track the source signature, in stagnation they relax to the slab —
        so a valve that is physically closed stops "flowing" after ~30 min,
        and a physically OPEN valve keeps the source signature even when the
        COMMAND says 0 (the stuck-open case). Before the first physics step
        with the pump ON, the probes fall back to the idealised flowing
        signature (supply, supply -/+ the EN 1264 default delta-T), matching
        the pre-S6 first reading. When the pump is OFF the probes are
        ``None`` (unchanged behaviour).

        The valve feedback is the PHYSICAL position — unless the
        ``echo_feedback`` fault is injected, in which case it ECHOES the
        commanded position (the issue-#4 lying bridge).

        Args:
            room: The simulated room.
            t_supply: Current supply temperature [degC], or ``None`` when OFF.

        Returns:
            A tuple of ``room.n_loops`` identical :class:`LoopInput` values.
        """
        probe_supply = room.probe_supply_c
        probe_return = room.probe_return_c
        if t_supply is None:
            probe_supply = None
            probe_return = None
        elif probe_supply is None or probe_return is None:
            # First reading with the pump ON, before any physics step has
            # initialised the probe model: idealised flowing signature.
            probe_supply = t_supply
            probe_return = (
                t_supply + DEFAULT_DT_COOLING
                if self._hp_mode == HeatPumpMode.COOLING
                else t_supply - DEFAULT_DT_HEATING
            )
        feedback = (
            room.commanded_valve_position if room.echo_feedback else room.valve_position
        )
        loop = LoopInput(
            valve_position_pct=feedback,
            supply_temperature_c=probe_supply,
            return_temperature_c=probe_return,
        )
        return tuple(loop for _ in range(room.n_loops))

    def _measurements_for(
        self,
        room: SimulatedRoom,
        weather: WeatherPoint,
        t_supply: float | None,
    ) -> RoomInputs:
        """Assemble one room's ``RoomInputs`` snapshot.

        ``room_temperature_c`` is noised; ``T_slab`` is deliberately excluded so
        the controller cannot see the ground-truth slab temperature.

        Args:
            room: The simulated room.
            weather: Weather conditions at the current time.
            t_supply: Current supply temperature [degC], or ``None`` when OFF.

        Returns:
            A :class:`RoomInputs` for the room.
        """
        return RoomInputs(
            mode=_HP_MODE_TO_MODE[self._hp_mode],
            setpoint_c=self._setpoints[room.name],
            room_temperature_c=self._apply_temp_noise(room.T_air),
            humidity_pct=_indoor_humidity_pct(room.T_air, weather),
            outdoor_temperature_c=weather.T_out,
            loops=self._build_loops(room, t_supply),
            fast_source_kind=room.fast_source_kind,
            fast_source_on=room.fast_source_on,
            hp_active_for_ufh=True,
            cooling_enabled=room.cooling_enabled,
        )

    def _distribute_hp_power(self) -> dict[str, float]:
        """Compute per-room floor power respecting finite HP capacity [W].

        Algorithm:

        1. When the pump is OFF, every room gets ``0.0`` W and diagnostics reset.
        2. Otherwise the supply temperature is derived once from the mode's
           weather-compensation curve (or fallback constant).
        3. Per-room demand is ``valve_fraction * loop_power(...)``; ``loop_power``
           carries the mode-correct sign (positive heating, negative cooling) and
           returns ``0.0`` on a wrong-direction gradient. The valve fraction is
           the PHYSICAL position (S6, 2026-07-13) — set by ``apply_actions``
           just before — never the commanded one, so a frozen actuator keeps
           heating/cooling at its frozen opening regardless of the command.
        4. If the absolute total demand exceeds ``hp_max_power_w``, every room's
           allocation is scaled uniformly (signs preserved).

        Returns:
            Allocated floor power [W] keyed by room name.
        """
        if self._hp_mode == HeatPumpMode.OFF:
            result = {r.name: 0.0 for r in self._rooms}
            self._last_t_supply_c = None
            self._last_q_floor_w = dict(result)
            return result

        t_out = self._weather.get(float(self._time_minutes)).T_out
        t_supply = self._compute_t_supply(t_out)
        self._last_t_supply_c = t_supply

        mode_str: Literal["heating", "cooling"] = (
            "cooling" if self._hp_mode == HeatPumpMode.COOLING else "heating"
        )

        demands: dict[str, float] = {}
        for r in self._rooms:
            valve_pct = r.valve_position
            valve_frac = max(0.0, min(100.0, valve_pct)) / 100.0
            q_max = loop_power(t_supply, r.T_slab, r.loop_geometry, mode_str)
            demands[r.name] = valve_frac * q_max

        total_abs = sum(abs(d) for d in demands.values())
        if total_abs == 0.0:
            result = dict.fromkeys(demands, 0.0)
        elif total_abs <= self._hp_max_power_w:
            result = dict(demands)
        else:
            scale = self._hp_max_power_w / total_abs
            result = {name: d * scale for name, d in demands.items()}

        self._last_q_floor_w = dict(result)
        return result

    # -- Public interface — multi-room ---------------------------------------

    def get_all_measurements(self) -> dict[str, RoomInputs]:
        """Return current ``RoomInputs`` for every room.

        Produces the same snapshot the HA coordinator builds:
        ``room_temperature_c`` (noised), ``humidity_pct`` / ``outdoor_temperature_c``
        from weather, per-loop supply/return from the actuation-aware probe
        model (S6), valve feedback (physical position, or the echoed command
        under the ``echo_feedback`` fault), ``mode`` mapped from the pump
        mode, and ``hp_active_for_ufh=True``. ``T_slab`` is never included.

        Returns:
            Dictionary keyed by room name with :class:`RoomInputs` values.
        """
        wp = self._weather.get(float(self._time_minutes))
        t_supply = (
            None
            if self._hp_mode == HeatPumpMode.OFF
            else self._compute_t_supply(wp.T_out)
        )
        return {r.name: self._measurements_for(r, wp, t_supply) for r in self._rooms}

    def step_all(
        self,
        actions: dict[str, RoomOutputs],
    ) -> dict[str, RoomInputs]:
        """Apply per-room outputs, share HP power, and propagate all rooms.

        Applies each room's valve position and fast-source command, distributes
        the finite heat-pump power via ``loop_power`` (scaling uniformly when
        total demand exceeds ``hp_max_power_w``), advances every room's RC model
        one tick, then advances the clock.

        Args:
            actions: Per-room :class:`RoomOutputs` keyed by room name. Must
                contain exactly one entry per room.

        Returns:
            The post-step ``RoomInputs`` for every room.

        Raises:
            ValueError: If ``actions`` keys do not match the room names exactly.
        """
        room_names = {r.name for r in self._rooms}
        action_names = set(actions.keys())
        if action_names != room_names:
            missing = sorted(room_names - action_names)
            extra = sorted(action_names - room_names)
            parts: list[str] = []
            if missing:
                parts.append(f"missing rooms: {missing}")
            if extra:
                parts.append(f"unknown rooms: {extra}")
            msg = f"action keys do not match room names: {', '.join(parts)}"
            raise ValueError(msg)

        # Apply actuator commands (valve % + signed fast-source power).
        for r in self._rooms:
            out = actions[r.name]
            fs = out.fast_source
            if not fs.on or fs.mode is FastSourceMode.OFF:
                fast_power = 0.0
            elif fs.mode is FastSourceMode.HEATING:
                fast_power = r.fast_source_power_w
            elif fs.mode is FastSourceMode.DRY:
                # Dry assist (§24): the twin has NO moisture state (RH is a
                # weather-driven input), so DRY is modelled ONLY as a small
                # fraction of the split's sensible cooling power; the latent
                # effect is outside the model.
                fast_power = -_DRY_SENSIBLE_FRACTION * r.fast_source_power_w
            else:
                fast_power = -r.fast_source_power_w
            r.apply_actions(
                valve_position=out.valve_position_pct,
                fast_source_power_w=fast_power,
            )

        allocated = self._distribute_hp_power()
        wp = self._weather.get(float(self._time_minutes))
        # Loop water-probe relaxation targets (S6): the flowing source
        # signature this tick, or None while the pump is OFF.
        flowing_supply = self._last_t_supply_c
        if flowing_supply is None:
            flowing_return: float | None = None
        elif self._hp_mode == HeatPumpMode.COOLING:
            flowing_return = flowing_supply + DEFAULT_DT_COOLING
        else:
            flowing_return = flowing_supply - DEFAULT_DT_HEATING
        for r in self._rooms:
            q_sol = _window_solar_gain_w(r.windows, wp.GHI, float(self._time_minutes))
            r.step_with_power(wp, q_floor_w=allocated[r.name], q_sol_w=q_sol)
            # Probes relax AFTER the physics tick so a stagnating loop chases
            # the post-step slab temperature (S6, 2026-07-13).
            r.update_loop_probes(
                flowing_supply_c=flowing_supply,
                flowing_return_c=flowing_return,
                dt_seconds=r.dt_seconds,
            )

        self._time_minutes += self._dt_minutes
        return self.get_all_measurements()

    # -- Public interface — single room (convenience) ------------------------

    def get_measurements(self) -> RoomInputs:
        """Return current ``RoomInputs`` for the first (or only) room.

        Returns:
            The :class:`RoomInputs` snapshot for the first room.
        """
        return self.get_all_measurements()[self._rooms[0].name]

    def step(self, actions: RoomOutputs) -> RoomInputs:
        """Apply one room's outputs, propagate, and return its measurements.

        Delegates to :meth:`step_all` so the single- and multi-room paths share
        the exact same physical distributor.

        Args:
            actions: Controller outputs for the first (or only) room.

        Returns:
            The post-step :class:`RoomInputs` for the first room.
        """
        first = self._rooms[0]
        return self.step_all({first.name: actions})[first.name]
