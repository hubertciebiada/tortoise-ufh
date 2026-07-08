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

# Mapping from the twin's heat-pump mode to the controller's room mode.  The
# twin has no TRANSITIONAL state (that is a controller-only concept), so only
# the three shared modes appear here.
_HP_MODE_TO_MODE: dict[HeatPumpMode, Mode] = {
    HeatPumpMode.HEATING: Mode.HEATING,
    HeatPumpMode.COOLING: Mode.COOLING,
    HeatPumpMode.OFF: Mode.OFF,
}


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
        loop_geometry: LoopGeometry,
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
            loop_geometry: Pipe/floor geometry for the EN 1264 power model
                (required so ``ufh_loop.loop_power`` can be evaluated).

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
        self._loop_geometry = loop_geometry

        self._x: NDArray[np.float64] = model.reset()
        self._valve_position: float = 0.0
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
        """Return the last-applied valve position [0-100 %]."""
        return self._valve_position

    # -- Actuation + physics -------------------------------------------------

    def apply_actions(
        self,
        valve_position: float,
        fast_source_power_w: float = 0.0,
    ) -> None:
        """Apply actuator commands for the upcoming step.

        Args:
            valve_position: Desired UFH valve position [0-100 %]. Clamped
                defensively to the valid range.
            fast_source_power_w: Signed fast-source power to apply [W]
                (positive heating, negative cooling). Its magnitude is capped
                at the room's nominal ``fast_source_power_w``.
        """
        self._valve_position = max(0.0, min(100.0, valve_position))
        capped = max(
            -self._fast_source_power_w,
            min(self._fast_source_power_w, fast_source_power_w),
        )
        self._applied_fast_source_power_w = capped

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
            if self._cooling_comp is not None:
                return self._cooling_comp.t_supply(t_out)
            return _FALLBACK_T_SUPPLY_COOLING_C
        return 0.0

    def _build_loops(
        self,
        room: SimulatedRoom,
        t_supply: float | None,
    ) -> tuple[LoopInput, ...]:
        """Build the per-loop water probes + valve feedback for one room.

        All of a room's loops share the same valve and supply. The return
        temperature is estimated from the supply using the EN 1264 default
        delta-T (subtracted in heating, added in cooling). When the pump is OFF
        the supply/return are unknown and reported as ``None``.

        Args:
            room: The simulated room.
            t_supply: Current supply temperature [degC], or ``None`` when OFF.

        Returns:
            A tuple of ``room.n_loops`` identical :class:`LoopInput` values.
        """
        if t_supply is None:
            t_return: float | None = None
        elif self._hp_mode == HeatPumpMode.COOLING:
            t_return = t_supply + DEFAULT_DT_COOLING
        else:
            t_return = t_supply - DEFAULT_DT_HEATING
        loop = LoopInput(
            valve_position_pct=room.valve_position,
            supply_temperature_c=t_supply,
            return_temperature_c=t_return,
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
            humidity_pct=weather.humidity,
            outdoor_temperature_c=weather.T_out,
            loops=self._build_loops(room, t_supply),
            fast_source_kind=room.fast_source_kind,
            fast_source_on=room.fast_source_on,
            hp_active_for_ufh=True,
            cooling_enabled=room.cooling_enabled,
        )

    def _distribute_hp_power(
        self,
        actions: dict[str, RoomOutputs],
    ) -> dict[str, float]:
        """Compute per-room floor power respecting finite HP capacity [W].

        Algorithm:

        1. When the pump is OFF, every room gets ``0.0`` W and diagnostics reset.
        2. Otherwise the supply temperature is derived once from the mode's
           weather-compensation curve (or fallback constant).
        3. Per-room demand is ``valve_fraction * loop_power(...)``; ``loop_power``
           carries the mode-correct sign (positive heating, negative cooling) and
           returns ``0.0`` on a wrong-direction gradient.
        4. If the absolute total demand exceeds ``hp_max_power_w``, every room's
           allocation is scaled uniformly (signs preserved).

        Args:
            actions: Per-room controller outputs keyed by room name.

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
            valve_pct = actions[r.name].valve_position_pct
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
        from weather, per-loop supply (weather-comp/fallback), return
        (supply -/+ delta-T), valve feedback (last applied), ``mode`` mapped from
        the pump mode, and ``hp_active_for_ufh=True``. ``T_slab`` is never
        included.

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
            else:
                fast_power = -r.fast_source_power_w
            r.apply_actions(
                valve_position=out.valve_position_pct,
                fast_source_power_w=fast_power,
            )

        allocated = self._distribute_hp_power(actions)
        wp = self._weather.get(float(self._time_minutes))
        for r in self._rooms:
            r.step_with_power(wp, q_floor_w=allocated[r.name], q_sol_w=0.0)

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
