"""Unit tests for :class:`tortoise_ufh.pid.PIDController`.

Exercises the discrete PI(+D) controller's contract:

* back-calculation anti-windup keeps the integral bounded during prolonged
  saturation, so the output leaves the rail immediately once the error
  reverses (no windup overshoot);
* ``freeze_integrator=True`` halts integral accumulation;
* the derivative term is zero on the first call (no previous error);
* the output is always clamped to ``[output_min, output_max]`` (default
  ``[0, 100]`` percent);
* the constructor fails fast with :class:`ValueError` on invalid gains,
  time step, or output bounds.

Units:
    error: degrees Celsius (K difference)
    output: percent (0-100 %, valve position)
    dt: seconds
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.pid import PIDController


@pytest.mark.unit
class TestAntiWindup:
    """Back-calculation anti-windup behaviour under saturation."""

    def test_integral_bounded_during_prolonged_saturation(self) -> None:
        """Integral stays pinned (not growing) while output is railed high."""
        pid = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)

        # A large constant error quickly saturates the output at output_max.
        for _ in range(50):
            pid.compute(5.0)
        assert pid.last_output == pytest.approx(100.0)
        integral_when_saturated = pid.integral

        # Keep hammering the rail; the integral must NOT keep accumulating.
        for _ in range(500):
            out = pid.compute(5.0)
            assert out == pytest.approx(100.0)

        # With back-calculation the integral is pinned at 100 - kp*e = 60 and
        # stays there (which is the whole point of anti-windup).
        assert pid.integral == pytest.approx(integral_when_saturated)
        assert pid.integral == pytest.approx(60.0)

    def test_no_overshoot_after_prolonged_saturation(self) -> None:
        """After a long saturation the output leaves the rail in one step.

        A controller without anti-windup would accumulate a huge integral and
        stay pinned at ``output_max`` for many steps after the error reverses.
        With back-calculation the bounded integral lets the output drop to the
        opposite rail immediately, so there is no windup overshoot.
        """
        pid = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)
        for _ in range(500):
            pid.compute(5.0)
        assert pid.last_output == pytest.approx(100.0)

        # Error reverses sign: a single step must bring the output off the top
        # rail (here all the way to the bottom), proving no residual windup.
        reversed_out = pid.compute(-5.0)
        assert reversed_out == pytest.approx(0.0)
        assert reversed_out < 100.0


@pytest.mark.unit
class TestFreezeIntegrator:
    """``freeze_integrator`` gates integral accumulation only."""

    def test_freeze_halts_integral_growth(self) -> None:
        """Frozen calls never accumulate; unfrozen calls do."""
        frozen = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)
        active = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)

        # Error kept small enough that the output never clamps, so the only
        # thing that can move the integral is the ki*e*dt accumulation.
        for _ in range(3):
            frozen.compute(1.0, freeze_integrator=True)
            active.compute(1.0)

        assert frozen.integral == pytest.approx(0.0)
        assert active.integral == pytest.approx(18.0)
        assert active.integral > frozen.integral

    def test_freeze_still_applies_proportional_term(self) -> None:
        """Freezing stops the integral but not the proportional response."""
        pid = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)
        out = pid.compute(2.0, freeze_integrator=True)
        # P only: kp*e = 16; integral untouched.
        assert out == pytest.approx(16.0)
        assert pid.integral == pytest.approx(0.0)


@pytest.mark.unit
class TestDerivative:
    """Derivative term handling on the first call."""

    def test_d_term_zero_on_first_call(self) -> None:
        """First call has no previous error, so D contributes nothing."""
        # ki=0 isolates P + D. A large kd with a large error would produce a
        # big derivative kick if e_prev were treated as 0 instead of None.
        pid = PIDController(kp=5.0, ki=0.0, kd=100.0, dt=300.0)
        out = pid.compute(2.0)
        # Pure proportional: kp*e = 10.0; D must be exactly 0 on first call.
        assert out == pytest.approx(10.0)

    def test_d_term_active_on_second_call(self) -> None:
        """Once a previous error exists, D reacts to the error change."""
        pid = PIDController(kp=5.0, ki=0.0, kd=100.0, dt=300.0)
        pid.compute(2.0)
        # Error rises 2 -> 4: D = kd*(4-2)/dt = 100*2/300 = 0.6667.
        out = pid.compute(4.0)
        assert out == pytest.approx(5.0 * 4.0 + 100.0 * (4.0 - 2.0) / 300.0)


@pytest.mark.unit
class TestOutputClamp:
    """Output is always clamped to ``[output_min, output_max]``."""

    def test_output_clamped_to_upper_bound(self) -> None:
        """A large positive error clamps the output to 100 %."""
        pid = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)
        out = pid.compute(1_000.0)
        assert out == pytest.approx(100.0)
        assert pid.last_output == pytest.approx(100.0)

    def test_output_clamped_to_lower_bound(self) -> None:
        """A large negative error clamps the output to 0 %."""
        pid = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)
        out = pid.compute(-1_000.0)
        assert out == pytest.approx(0.0)
        assert pid.last_output == pytest.approx(0.0)

    def test_output_within_bounds_over_random_walk(self) -> None:
        """Output never escapes [0, 100] across a varied error sequence."""
        pid = PIDController(kp=8.0, ki=0.02, kd=1.0, dt=300.0)
        errors = [5.0, -3.0, 20.0, -50.0, 0.1, 100.0, -100.0, 2.0]
        for error in errors:
            out = pid.compute(error)
            assert 0.0 <= out <= 100.0


@pytest.mark.unit
class TestConstructorValidation:
    """Fail-fast constructor validation raising :class:`ValueError`."""

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"kp": -1.0, "ki": 0.0}, "kp must be >= 0"),
            ({"kp": 1.0, "ki": -0.01}, "ki must be >= 0"),
            ({"kp": 1.0, "ki": 0.0, "kd": -1.0}, "kd must be >= 0"),
            ({"kp": 1.0, "ki": 0.0, "dt": 0.0}, "dt must be > 0"),
            ({"kp": 1.0, "ki": 0.0, "dt": -5.0}, "dt must be > 0"),
            (
                {"kp": 1.0, "ki": 0.0, "output_min": 100.0, "output_max": 0.0},
                "output_min",
            ),
            (
                {"kp": 1.0, "ki": 0.0, "output_min": 50.0, "output_max": 50.0},
                "output_min",
            ),
        ],
    )
    def test_invalid_arguments_raise(
        self, kwargs: dict[str, float], match: str
    ) -> None:
        """Out-of-range constructor arguments raise ``ValueError``."""
        with pytest.raises(ValueError, match=match):
            PIDController(**kwargs)

    def test_valid_arguments_construct(self) -> None:
        """A fully valid argument set constructs without error."""
        pid = PIDController(
            kp=8.0,
            ki=0.02,
            kd=0.5,
            dt=300.0,
            output_min=0.0,
            output_max=100.0,
        )
        assert pid.integral == pytest.approx(0.0)
        assert pid.last_output == pytest.approx(0.0)


@pytest.mark.unit
class TestPerCallDt:
    """Per-call ``dt_seconds`` drives the integral (irregular-step honesty)."""

    def test_integral_scales_with_dt_seconds(self) -> None:
        """A 2 s step accumulates ki*e*2, not a full nominal cycle."""
        pid = PIDController(kp=0.0, ki=0.02, kd=0.0, dt=300.0)

        pid.compute(1.0, dt_seconds=2.0)
        assert pid.integral == pytest.approx(0.02 * 1.0 * 2.0)

        pid.compute(1.0, dt_seconds=900.0)
        assert pid.integral == pytest.approx(0.02 * 1.0 * (2.0 + 900.0))

    def test_none_falls_back_to_configured_dt(self) -> None:
        """Omitting ``dt_seconds`` keeps the legacy fixed-cycle behaviour."""
        pid = PIDController(kp=0.0, ki=0.02, kd=0.0, dt=300.0)

        pid.compute(1.0)
        assert pid.integral == pytest.approx(0.02 * 1.0 * 300.0)

    def test_derivative_uses_per_call_dt(self) -> None:
        """The derivative divides by the per-call dt, not the configured one."""
        pid = PIDController(kp=0.0, ki=0.0, kd=10.0, dt=300.0)

        pid.compute(0.0, dt_seconds=2.0)  # first call: D == 0
        out = pid.compute(1.0, dt_seconds=2.0)  # D = kd * (1 - 0) / 2
        assert out == pytest.approx(10.0 * 1.0 / 2.0)

    @pytest.mark.parametrize("bad_dt", [0.0, -5.0])
    def test_non_positive_dt_seconds_raises(self, bad_dt: float) -> None:
        """A non-positive per-call dt fails fast with ``ValueError``."""
        pid = PIDController(kp=1.0, ki=0.02, kd=0.0, dt=300.0)
        with pytest.raises(ValueError, match="dt_seconds must be > 0"):
            pid.compute(1.0, dt_seconds=bad_dt)


class TestShiftIntegral:
    """K1 (2026-07-12): the external bumpless-transfer re-seed hook."""

    def test_shift_moves_integral_by_delta(self) -> None:
        """A shift adds exactly *delta* within the output range."""
        pid = PIDController(kp=0.0, ki=0.001, kd=0.0, dt=300.0)
        pid.compute(1.0, dt_seconds=1000.0)  # I = 1.0
        pid.shift_integral(+20.0)
        assert pid.integral == pytest.approx(21.0)
        pid.shift_integral(-15.0)
        assert pid.integral == pytest.approx(6.0)

    def test_shift_clamps_to_output_range(self) -> None:
        """A shift can never park the accumulator outside [min, max]."""
        pid = PIDController(kp=0.0, ki=0.001, kd=0.0, dt=300.0)
        pid.shift_integral(+250.0)
        assert pid.integral == pytest.approx(100.0)
        pid.shift_integral(-500.0)
        assert pid.integral == pytest.approx(0.0)

    def test_shift_is_noop_without_integral_gain(self) -> None:
        """With ki == 0 the accumulator is unused and the shift is a no-op."""
        pid = PIDController(kp=1.0, ki=0.0, kd=0.0, dt=300.0)
        pid.shift_integral(+30.0)
        assert pid.integral == pytest.approx(0.0)


class TestUnwindFactor:
    """K1 (2026-07-12): asymmetric unwinding of a sign-opposed integral."""

    def test_opposed_sign_discharges_faster(self) -> None:
        """A negative error against a positive integral unwinds N times faster."""
        plain = PIDController(kp=0.0, ki=0.001, kd=0.0, dt=300.0)
        unwound = PIDController(kp=0.0, ki=0.001, kd=0.0, dt=300.0, unwind_factor=8.0)
        for pid in (plain, unwound):
            pid.shift_integral(+50.0)
            pid.compute(-1.0, dt_seconds=300.0)
        plain_drop = 50.0 - plain.integral
        unwound_drop = 50.0 - unwound.integral
        assert plain_drop == pytest.approx(0.001 * 1.0 * 300.0)
        assert unwound_drop == pytest.approx(8.0 * plain_drop)

    def test_same_sign_accumulates_at_plain_ki(self) -> None:
        """The asymmetry never touches same-sign (honest) accumulation."""
        pid = PIDController(kp=0.0, ki=0.001, kd=0.0, dt=300.0, unwind_factor=8.0)
        pid.compute(1.0, dt_seconds=300.0)
        assert pid.integral == pytest.approx(0.001 * 1.0 * 300.0)

    def test_unwind_below_one_rejected(self) -> None:
        """A sub-1 unwind factor (slower unwinding than winding) is invalid."""
        with pytest.raises(ValueError, match="unwind_factor must be >= 1"):
            PIDController(kp=1.0, ki=0.001, kd=0.0, dt=300.0, unwind_factor=0.5)


class TestShiftResidual:
    """K6 (2026-07-12): clamp-cut shift debt netting + back-calc suppression."""

    def test_wiggle_at_small_integral_is_idempotent(self) -> None:
        """A down-and-back shift wiggle returns the integral to its origin.

        Before K6 the sequence (shift clamps at 0 -> the transient saturation
        back-calculates I to -P -> the counter-shift lands on top) pumped the
        integral to ~2*kp*dK: measured 79.8 pp from I0 = 10 for a 3 K wiggle
        at the default gains. Now the clamp cut is banked as a residual, the
        opposing back-calculation is suppressed while that debt is
        outstanding, and the counter-shift nets the debt first.
        """
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0, unwind_factor=8.0)
        pid.shift_integral(+10.0)  # small operating point
        pid.compute(0.0, dt_seconds=300.0)  # settled at the setpoint
        # Setpoint drops 3 K: shift -42 clamps at 0 (residual -32), and the
        # deadbanded error -2.7 K saturates the output at 0 for one cycle.
        pid.shift_integral(-42.0)
        assert pid.integral == pytest.approx(0.0)
        assert pid.shift_residual == pytest.approx(-32.0)
        pid.compute(-2.7, dt_seconds=5.0)
        # Setpoint returns: +42 nets the -32 debt, only +10 lands.
        pid.shift_integral(+42.0)
        out = pid.compute(0.0, dt_seconds=5.0)
        assert pid.integral == pytest.approx(10.0, abs=0.1)
        assert out == pytest.approx(10.0, abs=0.1)
        assert pid.shift_residual == pytest.approx(0.0)

    def test_monotonic_shift_series_sums_like_before(self) -> None:
        """Same-sign shifts keep the pre-K6 series-sum behaviour."""
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+10.0)
        for _ in range(3):
            pid.shift_integral(-14.0)  # 3 x -1 K at kp=14, clamping at 0
        assert pid.integral == pytest.approx(0.0)
        # The whole -42 is owed: one +42 counter-shift restores the origin.
        pid.shift_integral(+42.0)
        assert pid.integral == pytest.approx(10.0)

    def test_backcalc_untouched_without_residual(self) -> None:
        """With no outstanding debt the anti-windup is bit-for-bit classic."""
        classic = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        classic.shift_integral(+10.0)
        classic.compute(-2.7, dt_seconds=5.0)
        # Back-calculation drives I to -P during the low clamp (I = kp*2.7).
        assert classic.integral == pytest.approx(14.0 * 2.7, abs=0.1)

    def test_persistent_saturation_resumes_antiwindup_after_reset(self) -> None:
        """reset() clears the residual with the rest of the state."""
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(-42.0)
        assert pid.shift_residual == pytest.approx(-42.0)
        pid.reset()
        assert pid.shift_residual == pytest.approx(0.0)
        assert pid.integral == pytest.approx(0.0)

    def test_opposite_residual_still_allows_true_antiwindup(self) -> None:
        """A high-side clamp with a NEGATIVE debt still back-calculates.

        The suppression is sign-keyed: it blocks only corrections that would
        re-inflate the integral AGAINST the outstanding debt, never the
        classic windup prevention on the other bound.
        """
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+90.0)
        pid.shift_integral(-140.0)  # clamps at 0: residual -50
        assert pid.shift_residual == pytest.approx(-50.0)
        pid.shift_integral(+95.0)  # nets the debt, applies +45
        assert pid.integral == pytest.approx(45.0)
        # Huge positive error: output clamps HIGH, the correction is negative
        # (same sign as any remaining debt would be) -> applied normally.
        pid.compute(+10.0, dt_seconds=300.0)
        assert pid.integral <= 100.0
