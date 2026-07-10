"""Filtered room-temperature trend estimator (EMA of dT_room/dt).

Extracted verbatim from :mod:`tortoise_ufh.controller` (2026-07-10): the most
subtle piece of stateful numerics in the room controller — the debounce-aware,
EMA-filtered trend used by the ``kt`` damping term — now lives in its own
unit-testable class. :class:`~tortoise_ufh.controller.RoomController` owns one
:class:`TrendEstimator` and calls :meth:`TrendEstimator.update` once per
control step with the measured room temperature.

The sequence (S10, 2026-07-09) is preserved exactly: raw dT/dt samples are
taken only once at least :data:`_TREND_MIN_DT_S` has accumulated (a 2-second
debounced recompute HOLDS the previous filtered trend instead of dividing a
0.1 K sensor tick by 2 s), and each raw sample is folded into a first-order
EMA with time constant :data:`_TREND_FILTER_TAU_S` before the trend gain sees
it. A sensor-loss gap invalidates the state (:meth:`TrendEstimator.invalidate`)
so the first recovered cycle restarts from a zero trend rather than dividing a
multi-cycle temperature delta by one dt.

This module is pure Python (stdlib only) and MUST NOT import ``homeassistant``.

Units:
    * Room temperature: degrees Celsius (``_c``).
    * Elapsed step time ``dt_seconds``: seconds.
    * Returned trend: kelvin per hour (K/h).
"""

from __future__ import annotations

import math

__all__ = ["TrendEstimator"]


_SECONDS_PER_HOUR: float = 3600.0
"""Seconds in one hour, for converting ``dt_seconds`` to the trend's K/h."""

_TREND_MIN_DT_S: float = 60.0
"""Minimum elapsed time before a new raw trend sample is taken [s].

A debounced 2-second recompute after a setpoint change must HOLD the previous
filtered trend instead of dividing a 0.1 K sensor tick by 2 s (a fictitious
180 K/h); shorter intervals accumulate until the threshold is reached
(S10, 2026-07-09).
"""

_TREND_FILTER_TAU_S: float = 900.0
"""EMA time constant of the trend filter [s] (~15 min).

Sensor noise of sigma = 0.1 K produces raw cycle-to-cycle trend noise on the
order of the true signal; the first-order filter removes it before the trend
gain ``kt`` is applied, so raising ``kt`` no longer converts noise into
actuator wear (S10, 2026-07-09).
"""


class TrendEstimator:
    """Debounce-aware EMA estimator of the room-temperature trend [K/h].

    Stateful: holds the previous accepted room temperature, the filtered trend
    and the time accumulated since the last accepted sample. Every method body
    is the verbatim translocation of the controller's former inline trend
    logic (2026-07-10), so the numeric behaviour — and the order of
    floating-point operations — is unchanged.

    Typical usage (one control step)::

        trend = estimator.update(t_room, dt_seconds)   # K/h, 0.0 on first call
    """

    def __init__(self) -> None:
        """Initialise an empty estimator (no reference sample, zero trend)."""
        self._prev_t_room: float | None = None
        # Filtered trend state (S10, 2026-07-09): EMA of the raw dT/dt samples
        # plus the time accumulated since the last accepted sample (so a fast
        # debounced recompute holds the trend instead of amplifying noise).
        self._filtered: float = 0.0
        self._pending_dt_s: float = 0.0

    def update(self, t_room: float, dt_seconds: float) -> float:
        """Fold one measured room temperature into the filtered trend.

        On the first call (no reference sample) the trend is 0 and *t_room*
        becomes the reference. Afterwards *dt_seconds* accumulates until at
        least :data:`_TREND_MIN_DT_S` has passed; only then is a raw
        ``dT/dt`` sample taken and blended into the EMA
        (``alpha = 1 - exp(-dt/tau)``). Shorter intervals HOLD the previous
        filtered value.

        Args:
            t_room: Measured room temperature this cycle [degC].
            dt_seconds: Elapsed time since the previous step [s].

        Returns:
            The filtered trend [K/h] (0.0 until the first accepted sample).
        """
        if self._prev_t_room is None:
            self._filtered = 0.0
            self._pending_dt_s = 0.0
            self._prev_t_room = t_room
        else:
            self._pending_dt_s += dt_seconds
            if self._pending_dt_s >= _TREND_MIN_DT_S:
                dt_hours = self._pending_dt_s / _SECONDS_PER_HOUR
                raw_trend = (t_room - self._prev_t_room) / dt_hours
                alpha = 1.0 - math.exp(-self._pending_dt_s / _TREND_FILTER_TAU_S)
                self._filtered += alpha * (raw_trend - self._filtered)
                self._prev_t_room = t_room
                self._pending_dt_s = 0.0
        return self._filtered

    def invalidate(self) -> None:
        """Drop the state after a sensor-loss gap.

        Drops the stale reference so the first recovered cycle takes the
        trend==0 branch instead of dividing a multi-cycle delta by one dt;
        the filtered trend restarts from 0 too (a gap invalidates it).
        """
        self._prev_t_room = None
        self._filtered = 0.0
        self._pending_dt_s = 0.0

    def reset(self) -> None:
        """Clear all state (identical to :meth:`invalidate`, for symmetry)."""
        self.invalidate()
