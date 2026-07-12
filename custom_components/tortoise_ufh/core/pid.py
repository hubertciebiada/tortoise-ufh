"""Discrete PID controller with back-calculation anti-windup.

This module provides :class:`PIDController`, a stateful discrete PID
controller used by the per-room UFH ``RoomController`` to map a temperature
error onto a valve position.  It is pure Python (stdlib only) and never
imports Home Assistant.

Discretisation (backward Euler at the per-call ``dt``; defaults to the
configured ``dt`` when :meth:`PIDController.compute` is not given one)::

    P = kp * e
    I += ki * e * dt          # skipped when freeze_integrator is True;
                              # ki is scaled by unwind_factor while e and I
                              # have opposite signs (K1, 2026-07-12)
    D = kd * (e - e_prev) / dt # zero on the first call
    u_raw = P + I + D
    u = clip(u_raw, output_min, output_max)
    I += (u - u_raw)          # back-calculation anti-windup, only when ki > 0;
                              # suppressed while an opposite-sign shift
                              # residual is outstanding (K6, 2026-07-12)

The per-call ``dt_seconds`` keeps the integral honest when the caller steps
at irregular intervals (e.g. an immediate recompute a few seconds after a
setpoint change): the integral then accumulates the REAL elapsed time
instead of a full nominal cycle per call.

The controller is stateful: it maintains the integral accumulator and the
previous error between calls.  Use :meth:`reset` to clear that state.

Units:
    error: degrees Celsius (K difference)
    output: percent (0-100 %, valve position)
    dt: seconds (default 300 s = 5 min control cycle)
    kp: %/K
    ki: %/(K*s)
    kd: %*s/K
"""

from __future__ import annotations


class PIDController:
    """Discrete PID controller with back-calculation anti-windup.

    Computes a clamped control output (percent) from a temperature error
    signal.  Internal state (integral accumulator and previous error) is
    maintained between calls to :meth:`compute` and can be cleared with
    :meth:`reset`.

    Typical usage::

        pid = PIDController(kp=8.0, ki=0.02, kd=0.0, dt=300.0)
        for _ in range(288):
            error = setpoint_c - room_temperature_c
            valve_pct = pid.compute(error)
    """

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float = 0.0,
        *,
        dt: float = 300.0,
        output_min: float = 0.0,
        output_max: float = 100.0,
        unwind_factor: float = 1.0,
    ) -> None:
        """Initialise the PID controller.

        Args:
            kp: Proportional gain [%/K]. Must be >= 0.
            ki: Integral gain [%/(K*s)]. Must be >= 0.
            kd: Derivative gain [%*s/K]. Must be >= 0. Defaults to 0.0.
            dt: Control time step [seconds]. Must be > 0. Defaults to 300.0
                (a 5-minute control cycle).
            output_min: Minimum output [%]. Defaults to 0.0.
            output_max: Maximum output [%]. Must be strictly greater than
                *output_min*. Defaults to 100.0.
            unwind_factor: Multiplier (>= 1) applied to ``ki`` while the error
                and the integral accumulator have OPPOSITE signs (K1,
                2026-07-12): a sign-opposed integral is stale knowledge (e.g. a
                heating integral while the room sits above the new, lowered
                setpoint) and is unwound ``unwind_factor`` times faster than it
                accumulated. The accelerated rate only ever pulls the integral
                TOWARD zero, so the loop's equilibrium is untouched; inside the
                deadband the (deadbanded) error is 0 and no unwinding happens.
                ``1.0`` (default) disables the asymmetry.

        Raises:
            ValueError: If any gain is negative, ``dt <= 0``,
                ``output_min >= output_max``, or ``unwind_factor < 1``.
        """
        if kp < 0:
            msg = f"kp must be >= 0, got {kp}"
            raise ValueError(msg)
        if ki < 0:
            msg = f"ki must be >= 0, got {ki}"
            raise ValueError(msg)
        if kd < 0:
            msg = f"kd must be >= 0, got {kd}"
            raise ValueError(msg)
        if dt <= 0:
            msg = f"dt must be > 0, got {dt}"
            raise ValueError(msg)
        if output_min >= output_max:
            msg = f"output_min ({output_min}) must be < output_max ({output_max})"
            raise ValueError(msg)
        if unwind_factor < 1.0:
            msg = f"unwind_factor must be >= 1, got {unwind_factor}"
            raise ValueError(msg)

        self._kp = kp
        self._ki = ki
        self._kd = kd
        self._dt = dt
        self._output_min = output_min
        self._output_max = output_max
        self._unwind_factor = unwind_factor

        self._integral: float = 0.0
        self._prev_error: float | None = None
        self._last_output: float = 0.0
        # Unconsumed shift debt [%] (K6, 2026-07-12): the part of a
        # shift_integral request the output-range clamp cut off. Netted
        # against future shifts of the OPPOSITE sign so a setpoint wiggle
        # (down-and-back within a couple of cycles) is idempotent; cleared by
        # reset() together with the rest of the state.
        self._shift_residual: float = 0.0

    @property
    def integral(self) -> float:
        """Current integral accumulator value [%] (read-only)."""
        return self._integral

    @property
    def shift_residual(self) -> float:
        """Outstanding clamp-cut shift debt [%] (read-only; K6, 2026-07-12)."""
        return self._shift_residual

    @property
    def last_output(self) -> float:
        """Output [%] from the most recent :meth:`compute` call (read-only)."""
        return self._last_output

    def shift_integral(self, delta: float) -> None:
        """Shift the integral accumulator by *delta* [%], clamped to the range.

        External re-seed hook used by the room controller for the bumpless
        setpoint-change transfer (K1, 2026-07-12): a setpoint change of
        ``delta_e`` kelvin (in the controller's error convention) shifts the
        operating point by roughly ``kp * delta_e`` percent of valve, and the
        integral — which encodes the plant's steady-state offset — is moved
        along instead of discharging the difference at ``ki`` speed over many
        hours. The result is clamped to ``[output_min, output_max]`` so a
        shift can never park the accumulator outside the usable output range.
        A no-op when ``ki == 0`` (the accumulator is unused then).

        Shift-residual bookkeeping (K6, 2026-07-12): the part of a shift the
        clamp cuts off is BANKED as a signed residual instead of being lost.
        A future shift of the OPPOSITE sign first cancels that residual and
        only the remainder touches the accumulator, so a setpoint wiggle
        (e.g. -3 K and back +3 K at a small integral) returns the integral to
        its original value instead of pumping it by ~2·kp·ΔK. Same-sign
        shifts accumulate the residual, so the sum property of a monotonic
        shift series is unchanged. The residual dies with :meth:`reset`
        (which the room controller invokes on every reference reset — mode
        flip, integrator decay, full reset).

        Args:
            delta: Shift to apply to the integral accumulator [%]. May be
                negative.
        """
        if self._ki <= 0:
            return
        # Net an opposite-sign residual first (K6): the debt of an earlier
        # clamped shift consumes the counter-shift before the accumulator
        # moves at all.
        if self._shift_residual != 0.0 and delta * self._shift_residual < 0.0:
            if abs(delta) <= abs(self._shift_residual):
                self._shift_residual += delta
                return
            delta += self._shift_residual
            self._shift_residual = 0.0
        target = self._integral + delta
        clamped = max(self._output_min, min(self._output_max, target))
        self._shift_residual += target - clamped
        self._integral = clamped

    def compute(
        self,
        error: float,
        *,
        dt_seconds: float | None = None,
        freeze_integrator: bool = False,
    ) -> float:
        """Compute the clamped PID output for the given error.

        Applies proportional, integral and derivative terms, clamps the
        result to ``[output_min, output_max]`` and corrects the integral via
        back-calculation anti-windup.

        Args:
            error: Control error (setpoint - measured) [degC]. A positive
                error always means "more actuation needed".
            dt_seconds: Actual elapsed time since the previous call [s].
                Must be > 0 when given. When ``None`` (default) the configured
                ``dt`` is used. Passing the measured interval keeps the
                integral honest when steps are irregular (e.g. an immediate
                recompute a few seconds after a setpoint change would
                otherwise accumulate a full nominal cycle).
            freeze_integrator: When ``True``, the integral accumulation step
                (``I += ki * error * dt``) is skipped. Used when the heat pump
                is not available for UFH (e.g. DHW or defrost) so the integral
                does not wind up against an inactive source. The proportional
                and derivative terms and the anti-windup back-calculation are
                still applied. Defaults to ``False``.

        Returns:
            Clamped control output [%] in ``[output_min, output_max]``.

        Raises:
            ValueError: If ``dt_seconds`` is given and not positive.
        """
        if dt_seconds is None:
            dt = self._dt
        elif dt_seconds > 0:
            dt = dt_seconds
        else:
            msg = f"dt_seconds must be > 0, got {dt_seconds}"
            raise ValueError(msg)

        p_term = self._kp * error

        if not freeze_integrator:
            # Asymmetric unwinding (K1, 2026-07-12): a sign-opposed integral
            # (error pushes one way, accumulated integral the other) is stale
            # knowledge from a previous operating point and discharges
            # ``unwind_factor`` times faster than it accumulated. The
            # accelerated rate applies ONLY up to zero: if one step would
            # overshoot, the remainder of the interval accumulates at the
            # normal 1x rate (review 2026-07-12) — so the accelerated portion
            # truly never pushes the integral PAST zero.
            delta = self._ki * error * dt
            if error * self._integral < 0.0:
                accelerated = delta * self._unwind_factor
                if abs(accelerated) < abs(self._integral):
                    self._integral += accelerated
                else:
                    # Fraction of the interval spent reaching zero at Nx; the
                    # rest accumulates in the error's direction at 1x.
                    spent = abs(self._integral) / abs(accelerated)
                    self._integral = delta * (1.0 - spent)
            else:
                self._integral += delta

        if self._prev_error is not None:
            d_term = self._kd * (error - self._prev_error) / dt
        else:
            d_term = 0.0

        u_raw = p_term + self._integral + d_term
        u_clamped = max(self._output_min, min(self._output_max, u_raw))

        if self._ki > 0:
            correction = u_clamped - u_raw
            if self._shift_residual != 0.0 and correction * self._shift_residual < 0.0:
                # K6 (2026-07-12): while an opposite-sign shift debt is
                # outstanding, the back-calculation correction is the very
                # pump that made the wiggle non-idempotent — a transient
                # saturation caused by the (clamped-off) setpoint excursion
                # would write the P transient into the integral (I -> -P) and
                # the counter-shift would then land on top of it. Suppress the
                # correction and only keep the accumulator inside the output
                # range; a PERSISTENT saturation resumes normal anti-windup
                # the moment the residual is consumed or cleared.
                self._integral = max(
                    self._output_min, min(self._output_max, self._integral)
                )
            else:
                self._integral += correction

        self._prev_error = error
        self._last_output = u_clamped

        return u_clamped

    def reset(self) -> None:
        """Reset the integral accumulator, shift residual, previous error and
        last output."""
        self._integral = 0.0
        self._shift_residual = 0.0
        self._prev_error = None
        self._last_output = 0.0
