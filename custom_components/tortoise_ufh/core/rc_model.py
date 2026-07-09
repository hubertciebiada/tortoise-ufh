"""RC thermal state-space model for the Tortoise-UFH building simulator.

Implements a 3R3C (and optional 2R2C) lumped-parameter thermal model with
zero-order-hold (ZOH) discretization via the augmented matrix exponential.
The model is the physics engine of the digital twin; the pure controller does
not depend on it. This module never imports ``homeassistant``.

State-space form (continuous / discrete)::

    dx/dt   = A_c @ x + B_c @ u + E_c @ d + b_c          (continuous)
    x[k+1]  = A_d @ x[k] + B_d @ u[k] + E_d @ d[k] + b_d  (discrete, ZOH)

3R3C states: ``x = [T_air, T_slab, T_wall]``.
2R2C states: ``x = [T_air, T_slab]``.

SISO control input (UFH only): ``u = [Q_floor]``.
MIMO control input (UFH + fast source): ``u = [Q_conv, Q_floor]``.
Disturbances (3R3C): ``d = [T_out, Q_sol, Q_int]``; (2R2C): ``d = [T_out, Q_sol]``.

Units: resistances R in K/W, capacitances C in J/K, temperatures T in degC,
heat flows Q in W, time (``dt``) in seconds.

Typical usage::

    params = RCParams(
        C_air=60_000.0, C_slab=3_250_000.0, R_sf=0.01,
        C_wall=1_500_000.0, R_wi=0.02, R_wo=0.03, R_ve=0.03, R_ins=0.01,
    )
    model = RCModel(params, ModelOrder.THREE, dt=60.0)
    x_next = model.step(model.reset(), np.array([500.0]), np.array([0.0, 0.0, 0.0]))
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
import scipy.linalg
from numpy.typing import NDArray


class ModelOrder(Enum):
    """RC model order (number of lumped thermal nodes)."""

    TWO = 2
    THREE = 3


@dataclass(frozen=True)
class RCParams:
    """Immutable thermal parameters for an RC model.

    All resistances are in K/W and all capacitances in J/K. The solar split
    obeys ``f_conv + f_rad + f_slab <= 1.0`` (the remaining fraction is
    reflected). Sunlight entering through windows mostly lands on the FLOOR of
    a UFH room, so ``f_slab`` routes that share straight into the slab node —
    the main physical mechanism behind solar overshoot in high-mass floors
    (amendment 2026-07-09, simulator calibration).

    Attributes:
        C_air: Thermal capacitance of the air node [J/K].
        C_slab: Thermal capacitance of the slab node [J/K].
        R_sf: Thermal resistance slab-to-air (floor surface) [K/W].
        f_conv: Fraction of solar gain absorbed convectively by the air [-].
        f_rad: Fraction of solar gain absorbed radiatively by the walls [-].
        f_slab: Fraction of solar gain absorbed by the slab (sun on the
            floor) [-]. Default 0.0 keeps legacy parameter sets valid.
        T_ground: Ground temperature beneath the slab [degC].
        has_split: Whether the room has a fast source (MIMO input when True).
        C_wall: Thermal capacitance of the wall node [J/K] (3R3C only).
        R_wi: Thermal resistance wall-to-interior-air [K/W] (3R3C only).
        R_wo: Thermal resistance wall-to-outdoor [K/W] (3R3C only).
        R_ve: Thermal resistance for ventilation/infiltration [K/W] (3R3C only).
        R_ins: Thermal resistance of insulation beneath the slab [K/W] (3R3C only).
        R_env: Combined envelope resistance air-to-outdoor [K/W] (2R2C only).
    """

    C_air: float
    C_slab: float
    R_sf: float
    f_conv: float = 0.6
    f_rad: float = 0.4
    f_slab: float = 0.0
    T_ground: float = 10.0
    has_split: bool = False

    # 3R3C-only parameters
    C_wall: float | None = None
    R_wi: float | None = None
    R_wo: float | None = None
    R_ve: float | None = None
    R_ins: float | None = None

    # 2R2C-only parameters
    R_env: float | None = None

    def __post_init__(self) -> None:
        """Validate the order-independent parameter constraints.

        Raises:
            ValueError: If a resistance/capacitance is non-positive or the
                solar fractions are negative or sum above 1.0.
        """
        if self.R_sf <= 0:
            msg = f"R_sf must be positive, got {self.R_sf}"
            raise ValueError(msg)
        if self.C_air <= 0:
            msg = f"C_air must be positive, got {self.C_air}"
            raise ValueError(msg)
        if self.C_slab <= 0:
            msg = f"C_slab must be positive, got {self.C_slab}"
            raise ValueError(msg)
        if self.f_conv < 0 or self.f_rad < 0 or self.f_slab < 0:
            msg = (
                f"Solar fractions must be non-negative, got f_conv={self.f_conv}, "
                f"f_rad={self.f_rad}, f_slab={self.f_slab}"
            )
            raise ValueError(msg)
        if self.f_conv + self.f_rad > 1.0:
            total = self.f_conv + self.f_rad
            msg = f"f_conv + f_rad must be <= 1.0, got {total}"
            raise ValueError(msg)
        if self.f_conv + self.f_rad + self.f_slab > 1.0:
            total_all = self.f_conv + self.f_rad + self.f_slab
            msg = f"f_conv + f_rad + f_slab must be <= 1.0, got {total_all}"
            raise ValueError(msg)

    def validate_for_order(self, order: ModelOrder) -> None:
        """Validate that the parameters required by an order are present and valid.

        Args:
            order: The model order the parameters must satisfy.

        Raises:
            ValueError: If a required parameter for the order is missing or
                non-positive.
        """
        if order == ModelOrder.THREE:
            required: dict[str, float | None] = {
                "C_wall": self.C_wall,
                "R_wi": self.R_wi,
                "R_wo": self.R_wo,
                "R_ve": self.R_ve,
                "R_ins": self.R_ins,
            }
            for name, value in required.items():
                if value is None:
                    msg = f"{name} is required for the 3R3C model"
                    raise ValueError(msg)
                if value <= 0:
                    msg = f"{name} must be positive, got {value}"
                    raise ValueError(msg)
        else:
            if self.R_env is None:
                msg = "R_env is required for the 2R2C model"
                raise ValueError(msg)
            if self.R_env <= 0:
                msg = f"R_env must be positive, got {self.R_env}"
                raise ValueError(msg)


class RCModel:
    """RC thermal model supporting 3R3C (default) and 2R2C configurations.

    The model is built from physical parameters (:class:`RCParams`) and supports
    SISO (UFH-only, ``u = [Q_floor]``) and MIMO (UFH + fast source,
    ``u = [Q_conv, Q_floor]``) inputs. Discretization uses the augmented matrix
    exponential (ZOH), which is numerically stable even for the stiff
    ``C_slab / C_air`` ratio typical of high-thermal-mass underfloor heating.

    All propagation methods are pure: they never mutate their input arrays and
    return freshly allocated vectors.

    Typical usage::

        model = RCModel(params, ModelOrder.THREE, dt=60.0)
        x_next = model.step(x, u, d)
        traj = model.predict(x0, u_sequence, d_sequence)
    """

    def __init__(
        self,
        params: RCParams,
        order: ModelOrder = ModelOrder.THREE,
        dt: float = 60.0,
    ) -> None:
        """Initialize and discretize the RC model.

        Args:
            params: Thermal parameters for the model.
            order: Model order (``THREE`` for 3R3C, ``TWO`` for 2R2C).
            dt: Discretization time step in seconds.

        Raises:
            ValueError: If ``dt`` is non-positive or ``params`` are invalid for
                the requested order.
        """
        if dt <= 0:
            msg = f"dt must be positive, got {dt}"
            raise ValueError(msg)

        params.validate_for_order(order)

        self._params = params
        self._order = order
        self._dt = dt

        self._A_c: NDArray[np.float64]
        self._B_c: NDArray[np.float64]
        self._E_c: NDArray[np.float64]
        self._b_c: NDArray[np.float64]
        self._build_continuous_matrices()

        self._A_d: NDArray[np.float64]
        self._B_d: NDArray[np.float64]
        self._E_d: NDArray[np.float64]
        self._b_d: NDArray[np.float64]
        self._discretize()

    @property
    def params(self) -> RCParams:
        """Return the model parameters."""
        return self._params

    @property
    def order(self) -> ModelOrder:
        """Return the model order."""
        return self._order

    @property
    def dt(self) -> float:
        """Return the discretization time step in seconds."""
        return self._dt

    @property
    def n_states(self) -> int:
        """Number of state variables (2 for 2R2C, 3 for 3R3C)."""
        return self._order.value

    @property
    def n_inputs(self) -> int:
        """Number of control inputs (1 for SISO, 2 for MIMO)."""
        return 2 if self._params.has_split else 1

    @property
    def n_disturbances(self) -> int:
        """Number of disturbance inputs (3 for 3R3C, 2 for 2R2C)."""
        return 3 if self._order == ModelOrder.THREE else 2

    @property
    def state_names(self) -> list[str]:
        """Names of the state variables, in order."""
        if self._order == ModelOrder.THREE:
            return ["T_air", "T_slab", "T_wall"]
        return ["T_air", "T_slab"]

    @property
    def C_obs(self) -> NDArray[np.float64]:
        """Observation matrix (1, n_states) extracting ``T_air`` from the state."""
        c: NDArray[np.float64] = np.zeros((1, self.n_states))
        c[0, 0] = 1.0
        return c

    def _build_continuous_matrices(self) -> None:
        """Construct the continuous-time matrices for the configured order."""
        if self._order == ModelOrder.THREE:
            self._build_3r3c_matrices()
        else:
            self._build_2r2c_matrices()

    def _build_3r3c_matrices(self) -> None:
        """Build continuous-time A_c, B_c, E_c, b_c for the 3R3C model.

        State ``x = [T_air, T_slab, T_wall]``; SISO ``u = [Q_floor]`` or MIMO
        ``u = [Q_conv, Q_floor]``; disturbance ``d = [T_out, Q_sol, Q_int]``.
        """
        p = self._params
        # Narrowed to non-None by RCParams.validate_for_order in __init__.
        assert p.C_wall is not None
        assert p.R_wi is not None
        assert p.R_wo is not None
        assert p.R_ve is not None
        assert p.R_ins is not None

        A_c: NDArray[np.float64] = np.zeros((3, 3))
        A_c[0, 0] = -(
            1 / (p.R_sf * p.C_air) + 1 / (p.R_wi * p.C_air) + 1 / (p.R_ve * p.C_air)
        )
        A_c[0, 1] = 1 / (p.R_sf * p.C_air)
        A_c[0, 2] = 1 / (p.R_wi * p.C_air)
        A_c[1, 0] = 1 / (p.R_sf * p.C_slab)
        A_c[1, 1] = -(1 / (p.R_sf * p.C_slab) + 1 / (p.R_ins * p.C_slab))
        A_c[2, 0] = 1 / (p.R_wi * p.C_wall)
        A_c[2, 2] = -(1 / (p.R_wi * p.C_wall) + 1 / (p.R_wo * p.C_wall))
        self._A_c = A_c

        B_c: NDArray[np.float64]
        if p.has_split:
            B_c = np.zeros((3, 2))
            B_c[0, 0] = 1 / p.C_air  # Q_conv -> T_air
            B_c[1, 1] = 1 / p.C_slab  # Q_floor -> T_slab
        else:
            B_c = np.zeros((3, 1))
            B_c[1, 0] = 1 / p.C_slab  # Q_floor -> T_slab
        self._B_c = B_c

        E_c: NDArray[np.float64] = np.zeros((3, 3))
        E_c[0, 0] = 1 / (p.R_ve * p.C_air)  # T_out -> T_air (ventilation)
        E_c[0, 1] = p.f_conv / p.C_air  # Q_sol convective -> T_air
        E_c[0, 2] = 1 / p.C_air  # Q_int -> T_air
        E_c[1, 1] = p.f_slab / p.C_slab  # Q_sol on the floor -> T_slab
        E_c[2, 0] = 1 / (p.R_wo * p.C_wall)  # T_out -> T_wall
        E_c[2, 1] = p.f_rad / p.C_wall  # Q_sol radiative -> T_wall
        self._E_c = E_c

        b_c: NDArray[np.float64] = np.zeros(3)
        b_c[1] = p.T_ground / (p.R_ins * p.C_slab)  # ground coupling under slab
        self._b_c = b_c

    def _build_2r2c_matrices(self) -> None:
        """Build continuous-time A_c, B_c, E_c, b_c for the 2R2C model.

        State ``x = [T_air, T_slab]``; SISO ``u = [Q_floor]`` or MIMO
        ``u = [Q_conv, Q_floor]``; disturbance ``d = [T_out, Q_sol]``.
        """
        p = self._params
        assert p.R_env is not None

        A_c: NDArray[np.float64] = np.zeros((2, 2))
        A_c[0, 0] = -(1 / (p.R_sf * p.C_air) + 1 / (p.R_env * p.C_air))
        A_c[0, 1] = 1 / (p.R_sf * p.C_air)
        A_c[1, 0] = 1 / (p.R_sf * p.C_slab)
        A_c[1, 1] = -1 / (p.R_sf * p.C_slab)
        self._A_c = A_c

        B_c: NDArray[np.float64]
        if p.has_split:
            B_c = np.zeros((2, 2))
            B_c[0, 0] = 1 / p.C_air  # Q_conv -> T_air
            B_c[1, 1] = 1 / p.C_slab  # Q_floor -> T_slab
        else:
            B_c = np.zeros((2, 1))
            B_c[1, 0] = 1 / p.C_slab  # Q_floor -> T_slab
        self._B_c = B_c

        E_c: NDArray[np.float64] = np.zeros((2, 2))
        E_c[0, 0] = 1 / (p.R_env * p.C_air)  # T_out -> T_air
        E_c[0, 1] = p.f_conv / p.C_air  # Q_sol convective -> T_air
        E_c[1, 1] = p.f_slab / p.C_slab  # Q_sol on the floor -> T_slab
        self._E_c = E_c

        self._b_c = np.zeros(2)

    def _discretize(self) -> None:
        """Discretize the continuous system via the augmented matrix exponential.

        Builds the block-upper-triangular augmented generator whose top rows are
        ``[A_c | B_c | E_c | b_c]`` and whose remaining rows are zero, computes
        ``expm(M * dt)``, and reads off ``A_d, B_d, E_d, b_d``. This ZOH scheme
        is numerically stable for stiff RC systems.
        """
        n = self.n_states
        m = self.n_inputs
        q = self.n_disturbances
        total = n + m + q + 1

        aug: NDArray[np.float64] = np.zeros((total, total))
        aug[:n, :n] = self._A_c
        aug[:n, n : n + m] = self._B_c
        aug[:n, n + m : n + m + q] = self._E_c
        aug[:n, n + m + q :] = self._b_c.reshape(-1, 1)

        expm_aug = scipy.linalg.expm(aug * self._dt)

        self._A_d = np.asarray(expm_aug[:n, :n], dtype=np.float64).copy()
        self._B_d = np.asarray(expm_aug[:n, n : n + m], dtype=np.float64).copy()
        self._E_d = np.asarray(expm_aug[:n, n + m : n + m + q], dtype=np.float64).copy()
        self._b_d = (
            np.asarray(expm_aug[:n, n + m + q :], dtype=np.float64).flatten().copy()
        )

    def set_dt(self, dt: float) -> None:
        """Change the discretization time step and re-discretize in place.

        Args:
            dt: New time step in seconds.

        Raises:
            ValueError: If ``dt`` is non-positive.
        """
        if dt <= 0:
            msg = f"dt must be positive, got {dt}"
            raise ValueError(msg)
        self._dt = dt
        self._discretize()

    def step(
        self,
        x: NDArray[np.float64],
        u: NDArray[np.float64],
        d: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        """Propagate the state by one discrete time step (pure).

        Args:
            x: Current state vector, shape ``(n_states,)`` [degC].
            u: Control input vector, shape ``(n_inputs,)`` [W].
            d: Disturbance vector, shape ``(n_disturbances,)``
                (``[T_out (degC), Q_sol (W), Q_int (W)]`` for 3R3C).

        Returns:
            The next state vector, shape ``(n_states,)`` [degC]. A new array;
            ``x`` is not mutated.
        """
        result: NDArray[np.float64] = (
            self._A_d @ x + self._B_d @ u + self._E_d @ d + self._b_d
        )
        return result

    def predict(
        self,
        x0: NDArray[np.float64],
        u_sequence: NDArray[np.float64],
        d_sequence: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        """Predict the state trajectory over ``N`` steps.

        Args:
            x0: Initial state vector, shape ``(n_states,)`` [degC].
            u_sequence: Control-input sequence, shape ``(N, n_inputs)`` [W].
            d_sequence: Disturbance sequence, shape ``(N, n_disturbances)``.

        Returns:
            The state trajectory, shape ``(N + 1, n_states)``; the first row is
            ``x0``.

        Raises:
            ValueError: If ``u_sequence`` and ``d_sequence`` have different
                lengths.
        """
        n_steps = u_sequence.shape[0]
        if d_sequence.shape[0] != n_steps:
            msg = (
                f"u_sequence and d_sequence must have the same length, "
                f"got {u_sequence.shape[0]} and {d_sequence.shape[0]}"
            )
            raise ValueError(msg)

        trajectory: NDArray[np.float64] = np.zeros((n_steps + 1, self.n_states))
        trajectory[0] = x0
        for k in range(n_steps):
            trajectory[k + 1] = self.step(trajectory[k], u_sequence[k], d_sequence[k])
        return trajectory

    def steady_state(
        self,
        u: NDArray[np.float64],
        d: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        """Compute the steady-state temperature vector for constant inputs.

        Solves ``0 = A_c @ x_ss + B_c @ u + E_c @ d + b_c`` for ``x_ss``.

        Args:
            u: Constant control-input vector, shape ``(n_inputs,)`` [W].
            d: Constant disturbance vector, shape ``(n_disturbances,)``.

        Returns:
            The steady-state vector, shape ``(n_states,)`` [degC].

        Raises:
            ValueError: If ``A_c`` is singular (no unique steady state).
        """
        rhs = self._B_c @ u + self._E_c @ d + self._b_c
        try:
            x_ss = np.linalg.solve(self._A_c, -rhs)
        except np.linalg.LinAlgError as exc:
            msg = "A_c is singular; no unique steady state exists"
            raise ValueError(msg) from exc
        return np.asarray(x_ss, dtype=np.float64)

    def reset(self) -> NDArray[np.float64]:
        """Return a default initial state (all nodes at 20 degC).

        Returns:
            The default state vector, shape ``(n_states,)`` [degC].
        """
        return np.full(self.n_states, 20.0)

    def get_matrices(self) -> dict[str, Any]:
        """Return copies of all continuous and discrete model matrices.

        Returns:
            A dict with keys ``A_c, B_c, E_c, b_c, A_d, B_d, E_d, b_d`` mapping
            to copies of the corresponding arrays.
        """
        return {
            "A_c": self._A_c.copy(),
            "B_c": self._B_c.copy(),
            "E_c": self._E_c.copy(),
            "b_c": self._b_c.copy(),
            "A_d": self._A_d.copy(),
            "B_d": self._B_d.copy(),
            "E_d": self._E_d.copy(),
            "b_d": self._b_d.copy(),
        }
