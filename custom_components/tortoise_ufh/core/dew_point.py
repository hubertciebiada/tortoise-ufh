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
    With ``gap = t_surface - t_dew``:

    - ``gap <= margin``: returns 0.0 (fully throttled, condensation risk).
    - ``gap >= margin + ramp``: returns 1.0 (fully open, safe).
    - in between: linear ramp from 0.0 to 1.0.

    Args:
        t_surface: Cooled surface temperature (coldest loop supply or slab)
            [degC].
        t_dew: Dew-point temperature [degC].
        margin: Minimum required gap above dew point [degC]. Must be >= 0.
        ramp: Width of the linear ramp zone [degC]. Must be > 0.

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
    if gap <= margin:
        return 0.0
    if gap >= margin + ramp:
        return 1.0
    return (gap - margin) / ramp


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
