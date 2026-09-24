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


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
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

    def test_same_sign_shift_moves_integral_and_banks_only_the_clamp_cut(self) -> None:
        """A same-sign shift applies to the accumulator, not to the debt.

        The residual nets only AGAINST opposite-sign shifts (K6): a same-sign
        shift must move the integral like before K6, with only the part the
        output-range clamp cuts off added to the outstanding debt.
        """
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+150.0)  # clamps at 100, banks a +50 residual
        # Pull the accumulator off the rail via plain integration (no clamp).
        pid.compute(-1.0, dt_seconds=300.0)
        assert pid.integral == pytest.approx(100.0 - 0.45)
        assert pid.shift_residual == pytest.approx(50.0)
        # Same-sign +10: the integral must take it (clamped at 100) and only
        # the cut part (9.55) is banked on top of the existing +50.
        pid.shift_integral(+10.0)
        assert pid.integral == pytest.approx(100.0)
        assert pid.shift_residual == pytest.approx(59.55)

    def test_tiny_same_sign_shift_leaves_residual_untouched(self) -> None:
        """A same-sign shift that does not clamp adds nothing to the debt."""
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+150.0)  # clamps at 100, banks a +50 residual
        pid.compute(-1.0, dt_seconds=300.0)  # integral off the rail: 99.55
        pid.shift_integral(+0.01)  # tiny same-sign nudge, no clamp cut
        assert pid.integral == pytest.approx(99.56)
        assert pid.shift_residual == pytest.approx(50.0)

    def test_unit_residual_is_netted_by_counter_shift(self) -> None:
        """The netting gate keys on ``residual != 0``, also for a tiny debt."""
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+101.0)  # clamps at 100, banks exactly +1
        assert pid.shift_residual == pytest.approx(1.0)
        # A -5 counter-shift consumes the +1 debt first; only -4 lands.
        pid.shift_integral(-5.0)
        assert pid.integral == pytest.approx(96.0)
        assert pid.shift_residual == pytest.approx(0.0)

    def test_partial_counter_shift_reduces_residual_additively(self) -> None:
        """A counter-shift smaller than the debt shrinks it by exactly delta."""
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+150.0)  # clamps at 100, banks a +50 residual
        pid.shift_integral(-30.0)  # nets: 50 - 30, accumulator untouched
        assert pid.integral == pytest.approx(100.0)
        assert pid.shift_residual == pytest.approx(20.0)

    def test_zero_shift_reclamps_out_of_range_accumulator(self) -> None:
        """A zero shift skips the netting branch and re-clamps the accumulator.

        Anti-windup back-calculation can park the accumulator outside the
        output range while a residual is outstanding; a zero-delta shift is
        not an opposite-sign shift (``0 * residual`` is not negative), so it
        must take the normal path: clamp the accumulator back into range and
        bank the cut on top of the residual.
        """
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+150.0)  # clamps at 100, banks a +50 residual
        pid.compute(-10.0, dt_seconds=300.0)  # low clamp, correction +44.5
        assert pid.integral == pytest.approx(140.0)
        pid.shift_integral(0.0)
        assert pid.integral == pytest.approx(100.0)
        assert pid.shift_residual == pytest.approx(90.0)

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


@pytest.mark.unit
class TestBackCalcSuppressionGating:
    """K6 (2026-07-12): suppression keys on an OPPOSITE-sign correction only.

    While a shift residual is outstanding the anti-windup correction is
    suppressed only when ``correction * residual < 0`` — the very pump that
    made setpoint wiggles non-idempotent. Same-sign and zero corrections must
    be applied exactly like classic back-calculation.
    """

    def test_same_sign_correction_is_applied_normally(self) -> None:
        """A low-rail correction with the debt's sign is NOT suppressed."""
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+150.0)  # clamps at 100, banks a +50 residual
        # Saturate low: correction = +44.5, same sign as the +50 debt.
        pid.compute(-10.0, dt_seconds=300.0)
        # Classic back-calculation applies: I = 95.5 + 44.5 = 140 (= -P).
        assert pid.integral == pytest.approx(140.0)

    def test_zero_correction_is_not_a_suppression_trigger(self) -> None:
        """An exactly zero correction leaves an out-of-range accumulator be.

        Gains chosen so every intermediate is exact in binary floating point:
        after pumping the accumulator to 200 via anti-windup, the next call's
        raw output lands strictly inside the range, so the correction is
        exactly 0.0 — not an opposite-sign correction — and the accumulator
        must keep its (out-of-range) value rather than being re-clamped.
        """
        pid = PIDController(kp=10.0, ki=0.01, kd=0.0, dt=100.0)
        pid.shift_integral(+150.0)  # clamps at 100, banks a +50 residual
        pid.compute(-20.0, dt_seconds=100.0)  # low clamp: I = 80 + 120 = 200
        assert pid.integral == pytest.approx(200.0)
        # delta = -14 -> I = 186; u_raw = -140 + 186 = 46 in range -> corr 0.
        pid.compute(-14.0, dt_seconds=100.0)
        assert pid.integral == pytest.approx(186.0)

    def test_residual_of_one_still_suppresses_opposing_correction(self) -> None:
        """The suppression gate keys on ``residual != 0``, even for debt of 1."""
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+101.0)  # clamps at 100, banks exactly +1
        # Saturate high: correction is negative, opposite to the +1 debt.
        pid.compute(+10.0, dt_seconds=300.0)
        # Suppressed: the accumulator is only re-clamped into the range.
        assert pid.integral == pytest.approx(100.0)
        assert pid.shift_residual == pytest.approx(1.0)

    def test_sub_unit_same_sign_product_is_applied_normally(self) -> None:
        """A correction whose product with the debt is in [0, 1) is applied.

        The suppression test is a strict sign test (``< 0``), not a magnitude
        test: a small POSITIVE product (same-sign correction) must not be
        treated as opposition.
        """
        pid = PIDController(kp=14.0, ki=0.0015, kd=0.0, dt=300.0)
        pid.shift_integral(+100.5)  # clamps at 100, banks a +0.5 residual
        # u_raw = -1 exactly below the rail: correction +1, product +0.5.
        error = -101.0 / 14.45
        pid.compute(error, dt_seconds=300.0)
        expected = 100.0 + 0.0015 * error * 300.0 + 1.0
        assert pid.integral == pytest.approx(expected)


@pytest.mark.unit
class TestUnwindZeroCrossing:
    """K1 review (2026-07-12): the accelerated unwind stops AT zero."""

    def test_step_crossing_zero_finishes_the_interval_at_plain_ki(self) -> None:
        """The part of the step left after reaching zero accumulates at 1x.

        I = +1, error -1 K, ki 0.001, dt 300 s: the plain step is -0.3 and the
        8x step (-2.4) reaches zero after 1/2.4 of the interval; the remaining
        (1 - 1/2.4) of the interval accumulates at 1x -> -0.3 * (1 - 1/2.4).
        A symmetric output range keeps back-calculation out of the picture.
        """
        pid = PIDController(
            kp=0.0,
            ki=0.001,
            dt=300.0,
            output_min=-100.0,
            output_max=100.0,
            unwind_factor=8.0,
        )
        pid.shift_integral(+1.0)
        pid.compute(-1.0, dt_seconds=300.0)
        assert pid.integral == pytest.approx(-0.3 * (1.0 - 1.0 / 2.4))

    def test_step_short_of_zero_stays_on_the_accelerated_rate(self) -> None:
        """A step that does not reach zero discharges at the full 8x rate."""
        pid = PIDController(
            kp=0.0,
            ki=0.001,
            dt=300.0,
            output_min=-100.0,
            output_max=100.0,
            unwind_factor=8.0,
        )
        pid.shift_integral(+10.0)
        pid.compute(-1.0, dt_seconds=300.0)
        assert pid.integral == pytest.approx(10.0 - 2.4)

    def test_default_unwind_factor_is_plain_rate(self) -> None:
        """Without ``unwind_factor`` a sign-opposed integral discharges at 1x."""
        pid = PIDController(kp=0.0, ki=0.001, output_min=-100.0, output_max=100.0)
        pid.shift_integral(+10.0)
        pid.compute(-1.0, dt_seconds=300.0)
        assert pid.integral == pytest.approx(10.0 - 0.3)


@pytest.mark.unit
class TestPlainGainsEdges:
    """Edge values of the constructor and per-call arguments."""

    def test_sub_second_dt_is_accepted(self) -> None:
        """Any positive per-call dt is valid, also below one second."""
        pid = PIDController(kp=0.0, ki=0.01, output_min=-100.0, output_max=100.0)
        pid.compute(1.0, dt_seconds=0.5)
        assert pid.integral == pytest.approx(0.005)

    def test_sub_second_configured_dt_is_accepted(self) -> None:
        """A configured dt below one second is valid and used by default."""
        pid = PIDController(
            kp=0.0, ki=0.01, dt=0.5, output_min=-100.0, output_max=100.0
        )
        pid.compute(1.0)
        assert pid.integral == pytest.approx(0.005)

    def test_default_dt_is_five_minutes(self) -> None:
        """Without ``dt`` a call without ``dt_seconds`` integrates 300 s."""
        pid = PIDController(kp=0.0, ki=0.001, output_min=-100.0, output_max=100.0)
        pid.compute(1.0)
        assert pid.integral == pytest.approx(0.3)

    def test_default_kd_is_zero(self) -> None:
        """Without ``kd`` an error step adds no derivative kick."""
        pid = PIDController(kp=1.0, ki=0.0, output_min=-100.0, output_max=100.0)
        pid.compute(0.0)
        assert pid.compute(5.0) == pytest.approx(5.0)

    def test_pure_p_controller_never_touches_the_integral(self) -> None:
        """With ki = 0 a saturated output leaves the accumulator at zero."""
        pid = PIDController(kp=200.0, ki=0.0)
        assert pid.compute(1.0) == pytest.approx(100.0)
        assert pid.integral == 0.0

    def test_reset_clears_every_piece_of_state(self) -> None:
        """reset() zeroes the integral, residual, previous error and output."""
        pid = PIDController(kp=10.0, ki=0.001, kd=100.0)
        pid.shift_integral(+150.0)  # clamps at 100, banks a +50 residual
        pid.compute(2.0)
        pid.reset()
        assert pid.integral == 0.0
        assert pid.shift_residual == 0.0
        assert pid.last_output == 0.0
        # No previous error: the first call after reset has no D kick.
        assert pid.compute(1.0) == pytest.approx(10.0 + 0.3)
