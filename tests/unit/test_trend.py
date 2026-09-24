"""Unit tests for :mod:`tortoise_ufh.trend` (debounce-aware EMA trend).

Pins the S10 (2026-07-09) sequence of the filtered room-temperature trend
estimator (:class:`TrendEstimator`):

* the first call establishes the reference temperature and returns 0.0 K/h;
* raw ``dT/dt`` samples are taken only once at least ``_TREND_MIN_DT_S``
  (60 s) has *accumulated*; shorter intervals HOLD the previous filtered
  value instead of dividing a sensor tick by a tiny dt;
* each accepted sample blends into the first-order EMA with time constant
  ``_TREND_FILTER_TAU_S`` (900 s): ``alpha = 1 - exp(-dt / tau)``;
* :meth:`TrendEstimator.invalidate` drops all state so the first recovered
  cycle restarts from a zero trend rather than dividing a multi-cycle
  temperature delta by one dt.

Units: temperatures in degC, ``dt_seconds`` in seconds, trend in K/h.
This module is pure stdlib and never imports ``homeassistant``.
"""

from __future__ import annotations

import math

import pytest

from custom_components.tortoise_ufh.core.trend import (
    _TREND_FILTER_TAU_S,
    _TREND_MIN_DT_S,
    TrendEstimator,
)

pytestmark = pytest.mark.unit


def _alpha(dt_s: float) -> float:
    """Return the documented EMA weight ``1 - exp(-dt / tau)`` for ``dt_s``.

    Args:
        dt_s: Accumulated sample interval [s].

    Returns:
        The EMA blending weight for one accepted sample.
    """
    return 1.0 - math.exp(-dt_s / _TREND_FILTER_TAU_S)


class TestTrendEstimatorWarmup:
    """Reference capture and sub-threshold debounce behaviour."""

    def test_first_call_returns_zero(self) -> None:
        """The very first call returns 0.0 K/h and stores the reference."""
        estimator = TrendEstimator()
        assert estimator.update(20.0, 300.0) == 0.0

    def test_sub_threshold_intervals_accumulate(self) -> None:
        """Intervals below 60 s accumulate until the threshold is reached.

        Two consecutive 40 s steps cross the 60 s threshold together: the
        sample then taken uses the accumulated 80 s, so a non-zero trend
        appears exactly on the second step (not on the first, and never if
        each interval overwrote the accumulator instead of adding to it).
        """
        estimator = TrendEstimator()
        assert estimator.update(20.0, 300.0) == 0.0  # reference
        assert estimator.update(20.1, 40.0) == 0.0  # 40 s < 60 s: hold
        trend_k_h = estimator.update(20.3, 40.0)  # 80 s accumulated: sample
        raw_k_h = (20.3 - 20.0) / (80.0 / 3600.0)
        assert trend_k_h == pytest.approx(_alpha(80.0) * raw_k_h)
        assert trend_k_h > 0.0

    def test_exact_threshold_triggers_sample(self) -> None:
        """A dt of exactly ``_TREND_MIN_DT_S`` takes a sample (>= boundary).

        A +0.5 K rise over 60 s is a raw trend of 30 K/h, blended into the
        zero-initialised EMA with ``alpha = 1 - exp(-60 / 900)``.
        """
        estimator = TrendEstimator()
        estimator.update(20.0, 300.0)  # reference
        trend_k_h = estimator.update(20.5, _TREND_MIN_DT_S)
        assert trend_k_h == pytest.approx(_alpha(60.0) * 30.0)


class TestTrendEstimatorFilter:
    """EMA blending of successive accepted samples."""

    def test_second_sample_blends_toward_raw(self) -> None:
        """Each accepted sample moves the EMA by ``alpha * (raw - filtered)``.

        First sample: +0.5 K over 60 s (raw 30 K/h). Second: +0.2 K over
        60 s (raw 12 K/h). The filtered value must follow the documented
        first-order recursion exactly.
        """
        estimator = TrendEstimator()
        estimator.update(20.0, 300.0)  # reference
        filtered = estimator.update(20.5, 60.0)
        alpha = _alpha(60.0)
        assert filtered == pytest.approx(alpha * 30.0)
        trend_k_h = estimator.update(20.7, 60.0)
        expected = filtered + alpha * (12.0 - filtered)
        assert trend_k_h == pytest.approx(expected)

    def test_pending_time_resets_after_sample(self) -> None:
        """After a sample the accumulator restarts from zero.

        Once a sample is taken, a further 59 s step must HOLD the previous
        filtered trend (59 s < 60 s); the sample is only taken after yet
        another second has accumulated.
        """
        estimator = TrendEstimator()
        estimator.update(20.0, 300.0)  # reference
        filtered = estimator.update(20.5, 60.0)  # accepted sample
        assert estimator.update(20.6, 59.0) == pytest.approx(filtered)  # hold
        trend_k_h = estimator.update(20.6, 1.0)  # 60 s accumulated: sample
        raw_k_h = (20.6 - 20.5) / (60.0 / 3600.0)
        assert trend_k_h == pytest.approx(
            filtered + _alpha(60.0) * (raw_k_h - filtered)
        )


class TestTrendEstimatorInvalidate:
    """State drop after a sensor-loss gap."""

    def test_invalidate_restarts_from_zero_trend(self) -> None:
        """After ``invalidate`` the next cycle returns 0.0 K/h.

        The stale reference is dropped, so the first recovered cycle takes
        the reference branch instead of dividing a multi-cycle temperature
        delta by one dt.
        """
        estimator = TrendEstimator()
        estimator.update(20.0, 300.0)  # reference
        filtered = estimator.update(20.5, 60.0)  # non-zero trend
        assert filtered > 0.0
        estimator.invalidate()
        assert estimator.update(20.9, 60.0) == 0.0
        # The reference was dropped: a sub-threshold step still holds 0.0.
        assert estimator.update(21.0, 30.0) == 0.0

    def test_reset_behaves_like_invalidate(self) -> None:
        """``reset`` clears all state exactly like ``invalidate``."""
        estimator = TrendEstimator()
        estimator.update(20.0, 300.0)  # reference
        estimator.update(20.5, 60.0)  # non-zero trend
        estimator.reset()
        assert estimator.update(20.9, 60.0) == 0.0
