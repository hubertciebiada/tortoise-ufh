"""Simulated room bridging an :class:`RCModel` to actuator commands.

Provides :class:`SimulatedRoom`, the digital-twin bridge between
controller-level abstractions (valve position as a percentage, fast-source
power in Watts) and the mathematical physics engine (``RCModel`` with a floor
power input ``Q_floor`` and, for rooms with a fast source, a convective input
``Q_conv``).

The room owns its thermal state vector ``_x`` (initialised from
``model.reset()``) and its actuator state (valve position, fast-source power
request). It is *stateful* by design — one instance per physical room — so it
is a plain class, not a frozen dataclass. Constructor arguments are validated
fail-fast (raising :class:`ValueError`).

The core library never imports ``homeassistant``; this module is pure Python
(numpy) and offline-testable.

Units:
    Temperatures: degC
    Powers (fast source, floor, solar, internal gains): W
    Valve position: 0..100 % (float)
    Time step: inherited from the ``RCModel`` (``dt`` in seconds)

Typical usage::

    model = RCModel(params, ModelOrder.THREE, dt=60.0)
    geom = LoopGeometry(
        effective_pipe_length_m=130.0,
        pipe_spacing_m=0.15,
        pipe_diameter_outer_mm=16.0,
        pipe_wall_thickness_mm=2.0,
        area_m2=20.0,
    )
    room = SimulatedRoom("living_room", model, loop_geometry=geom)
    room.apply_actions(50.0)
    room.step_with_power(weather.get(0.0), q_floor_w=600.0)
    print(room.T_air, room.T_slab)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray

if TYPE_CHECKING:
    from .rc_model import RCModel
    from .ufh_loop import LoopGeometry
    from .weather import WeatherPoint


class SimulatedRoom:
    """A simulated room wrapping an :class:`RCModel` with actuator state.

    ``SimulatedRoom`` owns the thermal state vector and translates actuator
    commands (valve position as ``0..100 %``, fast-source power in Watts) into
    the control-input vector expected by the underlying :class:`RCModel`. For a
    SISO (UFH-only) model the input is ``u = [Q_floor]``; for a MIMO model (a
    room with a fast source) it is ``u = [Q_conv, Q_floor]``.

    All state manipulation is confined to :meth:`apply_actions` (actuators) and
    :meth:`step_with_power` (physics propagation). Read-only properties expose
    the air/slab temperatures, the valve position and a copy of the raw state.
    """

    def __init__(
        self,
        name: str,
        model: RCModel,
        *,
        n_loops: int = 1,
        fast_source_power_w: float = 0.0,
        q_int_w: float = 0.0,
        loop_geometry: LoopGeometry,
    ) -> None:
        """Initialise the simulated room.

        Args:
            name: Human-readable room name (e.g. ``"living_room"``).
            model: The RC thermal model driving this room's physics.
            n_loops: Number of UFH loops serving the room (>= 1). All loops
                share the room's single valve command.
            fast_source_power_w: Maximum fast-source (split/heater) power [W]
                (>= 0). Zero when the room has no fast source. Only used when
                the model is MIMO (``model.params.has_split``).
            q_int_w: Constant internal heat gains [W] (occupancy, appliances);
                must be >= 0.
            loop_geometry: ``LoopGeometry`` describing the room's UFH loop pipe
                and floor area. Required — the EN 1264 physics
                (``tortoise_ufh.ufh_loop.loop_power``) needs it to convert a
                valve fraction into a floor power.

        Raises:
            ValueError: If ``name`` is empty, ``n_loops`` < 1,
                ``fast_source_power_w`` < 0, or ``q_int_w`` < 0.
        """
        if not name:
            msg = "name must be a non-empty string"
            raise ValueError(msg)
        if n_loops < 1:
            msg = f"n_loops must be >= 1, got {n_loops}"
            raise ValueError(msg)
        if fast_source_power_w < 0.0:
            msg = f"fast_source_power_w must be >= 0 W, got {fast_source_power_w}"
            raise ValueError(msg)
        if q_int_w < 0.0:
            msg = f"q_int_w must be >= 0 W, got {q_int_w}"
            raise ValueError(msg)

        self._name = name
        self._model = model
        self._n_loops = n_loops
        self._fast_source_power_w = fast_source_power_w
        self._q_int_w = q_int_w
        self._loop_geometry = loop_geometry

        # Thermal state: all nodes at 20 degC (from RCModel.reset()).
        self._x: NDArray[np.float64] = model.reset()

        # Actuator state.
        self._valve_position: float = 0.0
        self._fast_source_request_w: float = 0.0

    # -- Properties ----------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the room name."""
        return self._name

    @property
    def n_loops(self) -> int:
        """Return the number of UFH loops serving the room."""
        return self._n_loops

    @property
    def state(self) -> NDArray[np.float64]:
        """Return a copy of the current thermal state vector [degC]."""
        return self._x.copy()

    @property
    def T_air(self) -> float:
        """Return the current air temperature ``x[0]`` [degC]."""
        return float(self._x[0])

    @property
    def T_slab(self) -> float:
        """Return the current slab temperature ``x[1]`` [degC]."""
        return float(self._x[1])

    @property
    def valve_position(self) -> float:
        """Return the current valve position [0..100 %]."""
        return self._valve_position

    @property
    def has_fast_source(self) -> bool:
        """Return whether the room's model carries a fast source (MIMO input)."""
        return self._model.params.has_split

    @property
    def fast_source_power_w(self) -> float:
        """Return the maximum fast-source power [W]."""
        return self._fast_source_power_w

    @property
    def loop_geometry(self) -> LoopGeometry:
        """Return the room's UFH loop geometry."""
        return self._loop_geometry

    # -- Actuator commands ---------------------------------------------------

    def apply_actions(
        self,
        valve_pct: float,
        fast_source_power_w: float = 0.0,
    ) -> None:
        """Apply actuator commands.

        The valve position is clamped to ``[0.0, 100.0]``. If the room has no
        fast source (SISO model) the fast-source power request is silently
        forced to zero.

        Args:
            valve_pct: Desired valve position [0..100 %]. Values outside the
                range are clamped defensively.
            fast_source_power_w: Desired fast-source power [W]; positive for
                heating, negative for cooling.
        """
        self._valve_position = max(0.0, min(100.0, valve_pct))
        self._fast_source_request_w = fast_source_power_w
        if not self.has_fast_source:
            self._fast_source_request_w = 0.0

    # -- Physics step --------------------------------------------------------

    def step_with_power(
        self,
        weather: WeatherPoint,
        q_floor_w: float,
        q_sol_w: float = 0.0,
    ) -> None:
        """Propagate the thermal state one step with a pre-computed floor power.

        The floor power ``q_floor_w`` is computed externally (by the building
        simulator's finite-heat-pump power distribution) rather than derived
        here. The fast-source input ``Q_conv`` is read from the actuator state
        set by :meth:`apply_actions` and clamped to
        ``[-fast_source_power_w, +fast_source_power_w]``.

        Builds the control-input vector ``u`` (SISO ``[q_floor]`` or MIMO
        ``[q_conv, q_floor]``) and the disturbance vector ``d``
        (``[T_out, Q_sol, Q_int]`` for 3R3C, ``[T_out, Q_sol]`` for 2R2C), then
        calls :meth:`RCModel.step`.

        Args:
            weather: Weather conditions at the current time step (supplies
                ``T_out`` [degC]).
            q_floor_w: Floor heating/cooling power [W] (pre-computed; positive
                for heating, negative for cooling).
            q_sol_w: Solar heat gain reaching the room [W] (>= 0 in practice).
        """
        q_conv = max(
            -self._fast_source_power_w,
            min(self._fast_source_power_w, self._fast_source_request_w),
        )

        u: NDArray[np.float64] = (
            np.array([q_conv, q_floor_w], dtype=np.float64)
            if self.has_fast_source
            else np.array([q_floor_w], dtype=np.float64)
        )

        d: NDArray[np.float64]
        if self._model.n_disturbances == 3:
            # 3R3C: d = [T_out, Q_sol, Q_int].
            d = np.array([weather.T_out, q_sol_w, self._q_int_w], dtype=np.float64)
        else:
            # 2R2C: d = [T_out, Q_sol].
            d = np.array([weather.T_out, q_sol_w], dtype=np.float64)

        self._x = self._model.step(self._x, u, d)
