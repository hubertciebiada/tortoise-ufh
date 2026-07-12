"""Magnus dew point and graduated condensation protection for floor cooling.

Pure, HA-free core helpers used by the room controller's cooling path (S2) and
by the building-level safe dew-point sensor. Provides the Magnus dew-point
formula, a simplified linear approximation, a graduated cooling-valve throttle
factor, and a condensation safety margin.

The Magnus formula uses the coefficients ``a = 17.625`` and ``b = 243.04``
(Alduchov & Eskridge, 1996), giving < 0.1 degC error against psychrometric
tables for ``t_air`` in [-40, 60] degC.

Units:
    Temperatures: degC
    Relative humidity: % in (0, 100]
    Throttle factor: 0.0 (fully throttled) to 1.0 (fully open)
"""

from __future__ import annotations

import math

MAGNUS_A: float = 17.625
"""Dimensionless coefficient *a* in the Magnus formula."""

MAGNUS_B: float = 243.04
"""Coefficient *b* [degC] in the Magnus formula."""


def _validate_rh(rh: float) -> None:
    """Validate relative humidity is in (0, 100].

    Args:
        rh: Relative humidity [%].

    Raises:
        ValueError: If *rh* is not in (0, 100].
    """
    if not (0.0 < rh <= 100.0):
        msg = f"rh must be in (0, 100], got {rh}"
        raise ValueError(msg)


def dew_point(t_air: float, rh: float) -> float:
    """Compute dew-point temperature using the Magnus formula.

    Uses coefficients ``a = 17.625`` and ``b = 243.04``::

        gamma = (a * t_air) / (b + t_air) + ln(rh / 100)
        t_dew = (b * gamma) / (a - gamma)

    Args:
        t_air: Air temperature [degC].
        rh: Relative humidity [%] in (0, 100].

    Returns:
        Dew-point temperature [degC].

    Raises:
        ValueError: If *rh* is not in (0, 100].
    """
    _validate_rh(rh)
    gamma = (MAGNUS_A * t_air) / (MAGNUS_B + t_air) + math.log(rh / 100.0)
    return (MAGNUS_B * gamma) / (MAGNUS_A - gamma)


def dew_point_simplified(t_air: float, rh: float) -> float:
    """Compute dew-point temperature using the simplified linear formula.

    Legacy approximation ``t_dew = t_air - (100 - rh) / 5``, accurate to a few
    degrees near typical indoor humidities. Prefer :func:`dew_point`.

    Args:
        t_air: Air temperature [degC].
        rh: Relative humidity [%] in (0, 100].

    Returns:
        Estimated dew-point temperature [degC].

    Raises:
        ValueError: If *rh* is not in (0, 100].
    """
    _validate_rh(rh)
    return t_air - (100.0 - rh) / 5.0


def cooling_throttle_factor(
    t_surface: float,
    t_dew: float,
    margin: float = 2.0,
    ramp: float = 2.0,
) -> float:
    """Compute a graduated cooling-valve throttle factor in [0, 1].

    Multiply the cooling valve output by this factor to prevent condensation.

    Semantics revised 2026-07-12 (K6, owner decision "tylko pompa +2"): the
    heat pump's global safe dew-point floor (``max_over_cooled(T_dew) + 2 K``)
    is the ONE working margin of the system, so the local throttle no longer
    stacks a second full margin on top of it. The ramp now ENDS at *margin*
    (full cooling exactly at the design gap the pump floor already guarantees)
    instead of STARTING there, and this layer degrades into an emergency
    backstop that only bites when the supply water dips below the pump floor.
    With ``gap = t_surface - t_dew`` and ``lo = max(0, margin - ramp)``:

    - ``gap <= lo``: returns 0.0 (fully throttled; with the defaults
      ``margin = ramp = 2`` this means supply at/below the room's actual dew
      point).
    - ``gap >= margin``: returns 1.0 (fully open — full cooling on the pump
      floor).
    - in between: linear ramp from 0.0 to 1.0 over ``(lo, margin)``.

    Before 2026-07-12 the ramp spanned ``(margin, margin + ramp)``, which
    stacked the local margin on the pump floor: the most humid room — the one
    defining the global floor — sat at ``gap == margin`` and got
    ``factor == 0`` (measured in ``hot_july_floor_cooling``: factor < 1 for
    80.9 % of records, full cooling only 19.1 %, "fasadowe" cooling).

    Args:
        t_surface: Cooled surface temperature (coldest loop supply or slab)
            [degC].
        t_dew: Dew-point temperature [degC].
        margin: Gap above the dew point at (and above) which the valve is
            fully open [degC]. Must be >= 0.
        ramp: Width of the linear ramp zone below *margin* [degC]. Must be
            > 0. Values larger than *margin* are effectively clipped (the
            ramp cannot extend below ``gap = 0``).

    Returns:
        Throttle factor in [0.0, 1.0].

    Raises:
        ValueError: If *margin* < 0 or *ramp* <= 0.
    """
    if margin < 0.0:
        msg = f"margin must be >= 0, got {margin}"
        raise ValueError(msg)
    if ramp <= 0.0:
        msg = f"ramp must be > 0, got {ramp}"
        raise ValueError(msg)

    gap = t_surface - t_dew
    lo = max(0.0, margin - ramp)
    # Conservative ordering: at margin == 0 (degenerate config) a zero gap
    # reads as fully throttled, never as fully open.
    if gap <= lo:
        return 0.0
    if gap >= margin:
        return 1.0
    return (gap - lo) / (margin - lo)


def condensation_margin(
    t_surface: float,
    t_air: float,
    rh: float,
    safety_margin: float = 2.0,
) -> float:
    """Compute the condensation safety margin above the dew point.

    Returns ``t_surface - (t_dew + safety_margin)`` using the Magnus dew point.
    Positive values indicate safe conditions; negative values indicate
    condensation risk.

    Args:
        t_surface: Cooled surface temperature (coldest loop supply or slab)
            [degC].
        t_air: Air temperature [degC].
        rh: Relative humidity [%] in (0, 100].
        safety_margin: Required gap above dew point [degC]. Must be >= 0.

    Returns:
        Condensation margin [degC]. Positive = safe, negative = risk.

    Raises:
        ValueError: If *rh* is not in (0, 100] or *safety_margin* < 0.
    """
    if safety_margin < 0.0:
        msg = f"safety_margin must be >= 0, got {safety_margin}"
        raise ValueError(msg)
    t_dew = dew_point(t_air, rh)
    return t_surface - (t_dew + safety_margin)
