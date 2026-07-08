"""UFH loop thermal power calculation (EN 1264 reduced formula).

Implements the simplified EN 1264 formula for underfloor-heating (UFH) loop
thermal power output::

    Q = U * A * DeltaT_log

where *U* is the overall heat-transfer coefficient through the PE-X pipe
wall [W/(m^2*K)], *A* is the effective heated floor area [m^2] and
*DeltaT_log* is the log-mean temperature difference [K] between the
supply/return water and the slab.

The module bakes the "never oppose the mode" rule into the physics:
``loop_power`` returns exactly ``0.0`` when the temperature gradient would
drive heat in the wrong direction for the requested mode.

Public symbols:
    ``LoopGeometry``            — frozen dataclass with pipe/floor geometry.
    ``loop_power``              — EN 1264 reduced power [W].
    ``loop_power_with_valve``   — power scaled by valve duty cycle [0, 1].

Units:
    Temperatures: degC
    Lengths: m (pipe diameter/wall thickness in mm)
    Area: m^2
    Power: W
    Valve position: 0.0 – 1.0 (duty-cycle fraction)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .const import K_PEX

if TYPE_CHECKING:
    from .config import RoomConfig

# ---------------------------------------------------------------------------
# Module constants (defaults; K_PEX comes from tortoise_ufh.const)
# ---------------------------------------------------------------------------

DEFAULT_DT_HEATING: float = 5.0
"""Default supply-return water temperature drop in heating mode [K]."""

DEFAULT_DT_COOLING: float = 3.0
"""Default return-supply water temperature rise in cooling mode [K]."""

DEFAULT_PIPE_SPACING_M: float = 0.15
"""Default centre-to-centre pipe spacing when unspecified [m]."""

DEFAULT_PIPE_DIAMETER_OUTER_MM: float = 16.0
"""Default outer pipe diameter when unspecified [mm]."""

DEFAULT_PIPE_WALL_THICKNESS_MM: float = 2.0
"""Default pipe wall thickness when unspecified [mm]."""

PIPE_BEND_FACTOR: float = 1.1
"""Bend/return-margin factor applied to the estimated pipe length."""

_EPSILON: float = 1e-6
"""Guard threshold for near-equal delta-T values in the LMTD calculation."""


# ---------------------------------------------------------------------------
# LoopGeometry — frozen dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopGeometry:
    """Pipe and floor geometry for one UFH loop group.

    All five fields are validated in ``__post_init__``.

    Attributes:
        effective_pipe_length_m: Total installed pipe length [m] (> 0).
        pipe_spacing_m: Centre-to-centre pipe spacing [m] (> 0).
        pipe_diameter_outer_mm: Outer pipe diameter [mm] (> 0).
        pipe_wall_thickness_mm: Pipe wall thickness [mm]
            (> 0, < ``pipe_diameter_outer_mm / 2``).
        area_m2: Effective heated floor area [m^2] (> 0).
    """

    effective_pipe_length_m: float
    pipe_spacing_m: float
    pipe_diameter_outer_mm: float
    pipe_wall_thickness_mm: float
    area_m2: float

    def __post_init__(self) -> None:
        """Validate all geometry fields.

        Raises:
            ValueError: If any field is out of its permitted range.
        """
        if self.effective_pipe_length_m <= 0:
            msg = (
                f"effective_pipe_length_m must be > 0, "
                f"got {self.effective_pipe_length_m}"
            )
            raise ValueError(msg)
        if self.pipe_spacing_m <= 0:
            msg = f"pipe_spacing_m must be > 0, got {self.pipe_spacing_m}"
            raise ValueError(msg)
        if self.pipe_diameter_outer_mm <= 0:
            msg = (
                f"pipe_diameter_outer_mm must be > 0, got {self.pipe_diameter_outer_mm}"
            )
            raise ValueError(msg)
        if self.pipe_wall_thickness_mm <= 0:
            msg = (
                f"pipe_wall_thickness_mm must be > 0, got {self.pipe_wall_thickness_mm}"
            )
            raise ValueError(msg)
        if self.pipe_wall_thickness_mm >= self.pipe_diameter_outer_mm / 2:
            msg = (
                f"pipe_wall_thickness_mm ({self.pipe_wall_thickness_mm}) must be "
                f"< pipe_diameter_outer_mm / 2 ({self.pipe_diameter_outer_mm / 2})"
            )
            raise ValueError(msg)
        if self.area_m2 <= 0:
            msg = f"area_m2 must be > 0, got {self.area_m2}"
            raise ValueError(msg)

    @classmethod
    def from_room_config(cls, room: RoomConfig) -> LoopGeometry:
        """Build a ``LoopGeometry`` from a ``RoomConfig``.

        If the room already carries an explicit ``loop_geometry`` it is
        returned unchanged. Otherwise the geometry is estimated from the
        room's floor area using module defaults: the pipe length is
        approximated as ``area_m2 / spacing * PIPE_BEND_FACTOR`` at the
        default spacing, with default pipe diameter and wall thickness.

        Args:
            room: Room configuration providing at least ``area_m2`` and an
                optional ``loop_geometry``.

        Returns:
            A validated ``LoopGeometry`` instance.

        Raises:
            ValueError: If the estimated geometry is invalid (e.g. the
                room reports a non-positive ``area_m2``).
        """
        if room.loop_geometry is not None:
            return room.loop_geometry

        spacing_m = DEFAULT_PIPE_SPACING_M
        length_m = room.area_m2 / spacing_m * PIPE_BEND_FACTOR
        return cls(
            effective_pipe_length_m=length_m,
            pipe_spacing_m=spacing_m,
            pipe_diameter_outer_mm=DEFAULT_PIPE_DIAMETER_OUTER_MM,
            pipe_wall_thickness_mm=DEFAULT_PIPE_WALL_THICKNESS_MM,
            area_m2=room.area_m2,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _delta_t_log(delta_t_in: float, delta_t_out: float) -> float:
    """Log-mean temperature difference (LMTD).

    When both deltas are strictly positive the standard LMTD formula
    applies::

        LMTD = (dT_in - dT_out) / ln(dT_in / dT_out)

    Edge cases:
        - If either delta is <= 0, return ``0.0`` (no valid heat transfer).
        - If the two deltas are nearly equal, return their arithmetic mean
          to avoid a 0/0 division.

    Args:
        delta_t_in: Temperature difference at the inlet side [K].
        delta_t_out: Temperature difference at the outlet side [K].

    Returns:
        Log-mean temperature difference [K], always >= 0.
    """
    if delta_t_in <= 0.0 or delta_t_out <= 0.0:
        return 0.0
    if abs(delta_t_in - delta_t_out) < _EPSILON:
        return (delta_t_in + delta_t_out) / 2.0
    return (delta_t_in - delta_t_out) / math.log(delta_t_in / delta_t_out)


def _compute_u_effective(geometry: LoopGeometry) -> float:
    """Compute the effective area-based U-value [W/(m^2*K)].

    The per-metre pipe-wall conductance is::

        U_pipe_m = 2 * pi * K_PEX / ln(d_outer / d_inner)

    A spacing correction reduces the effective conductance::

        f_spacing = 1 / (1 + spacing / (pi * d_outer))

    The overall area-based coefficient is::

        U = U_pipe_m * f_spacing * pipe_length / area

    Args:
        geometry: Pipe and floor geometry.

    Returns:
        Effective U-value [W/(m^2*K)].
    """
    d_outer_m = geometry.pipe_diameter_outer_mm / 1000.0
    d_inner_m = (
        geometry.pipe_diameter_outer_mm - 2.0 * geometry.pipe_wall_thickness_mm
    ) / 1000.0

    u_pipe_per_m = (2.0 * math.pi * K_PEX) / math.log(d_outer_m / d_inner_m)
    f_spacing = 1.0 / (1.0 + geometry.pipe_spacing_m / (math.pi * d_outer_m))
    return (
        u_pipe_per_m * f_spacing * geometry.effective_pipe_length_m / geometry.area_m2
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def loop_power(
    t_supply: float,
    t_slab: float,
    geometry: LoopGeometry,
    mode: Literal["heating", "cooling"],
    t_return_estimate: float | None = None,
) -> float:
    """Compute UFH loop thermal power using the EN 1264 reduced formula.

    ``Q = U * A * DeltaT_log``.

    In **heating** mode the function returns ``Q >= 0``; in **cooling**
    mode it returns ``Q <= 0`` (heat extracted from the slab). If the
    supply temperature would drive heat in the wrong direction for the
    requested mode, the function returns exactly ``0.0``.

    Args:
        t_supply: Supply water temperature [degC].
        t_slab: Slab (screed) temperature [degC].
        geometry: ``LoopGeometry`` with pipe and floor data.
        mode: Operating mode — ``"heating"`` or ``"cooling"``.
        t_return_estimate: Estimated return water temperature [degC].
            When ``None`` it is derived from ``t_supply`` using
            ``DEFAULT_DT_HEATING`` (heating) or ``DEFAULT_DT_COOLING``
            (cooling).

    Returns:
        Thermal power [W]: positive in heating, negative in cooling,
        exactly ``0.0`` on a wrong-direction gradient.

    Raises:
        ValueError: If *mode* is not ``"heating"`` or ``"cooling"``.
    """
    if mode not in ("heating", "cooling"):
        msg = f"mode must be 'heating' or 'cooling', got '{mode}'"
        raise ValueError(msg)

    # Never oppose the mode: wrong gradient -> no heat transfer.
    if mode == "heating" and t_supply <= t_slab:
        return 0.0
    if mode == "cooling" and t_supply >= t_slab:
        return 0.0

    # Estimate the return water temperature when not provided.
    if t_return_estimate is None:
        if mode == "heating":
            # Clamp so the estimated return cannot reach or cross the slab,
            # keeping delta_t_out strictly positive for any favourable gradient.
            t_return_estimate = max(t_supply - DEFAULT_DT_HEATING, t_slab + _EPSILON)
        else:
            t_return_estimate = min(t_supply + DEFAULT_DT_COOLING, t_slab - _EPSILON)

    # Build the inlet/outlet temperature differences for the LMTD.
    if mode == "heating":
        delta_t_in = t_supply - t_slab
        delta_t_out = t_return_estimate - t_slab
    else:
        # Cooling: the slab is warmer than the circulating water.
        delta_t_in = t_slab - t_supply
        delta_t_out = t_slab - t_return_estimate

    dt_log = _delta_t_log(delta_t_in, delta_t_out)
    if dt_log == 0.0:
        return 0.0

    u_eff = _compute_u_effective(geometry)
    q = u_eff * geometry.area_m2 * dt_log

    if mode == "cooling":
        return -q
    return q


def loop_power_with_valve(
    valve: float,
    t_supply: float,
    t_slab: float,
    geometry: LoopGeometry,
    mode: Literal["heating", "cooling"],
    t_return_estimate: float | None = None,
) -> float:
    """Compute UFH loop thermal power scaled by the valve position.

    Equivalent to ``valve * loop_power(...)`` with the valve clamped to
    the ``[0, 1]`` duty-cycle range.

    Args:
        valve: Valve duty cycle [0, 1]. Values outside this range are
            clamped defensively.
        t_supply: Supply water temperature [degC].
        t_slab: Slab (screed) temperature [degC].
        geometry: ``LoopGeometry`` with pipe and floor data.
        mode: Operating mode — ``"heating"`` or ``"cooling"``.
        t_return_estimate: Optional return water temperature [degC].

    Returns:
        Thermal power [W] scaled by the (clamped) valve position.

    Raises:
        ValueError: If *mode* is not ``"heating"`` or ``"cooling"``.
    """
    valve_clamped = max(0.0, min(1.0, valve))
    if valve_clamped == 0.0:
        return 0.0
    return valve_clamped * loop_power(
        t_supply, t_slab, geometry, mode, t_return_estimate
    )
