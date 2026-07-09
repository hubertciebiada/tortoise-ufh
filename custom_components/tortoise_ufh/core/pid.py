"""Discrete PID controller with back-calculation anti-windup.

This module provides :class:`PIDController`, a stateful discrete PID
controller used by the per-room UFH ``RoomController`` to map a temperature
error onto a valve position.  It is pure Python (stdlib only) and never
imports Home Assistant.

Discretisation (backward Euler at the per-call ``dt``; defaults to the
configured ``dt`` when :meth:`PIDController.compute` is not given one)::

    P = kp * e
    I += ki * e * dt          # skipped when freeze_integrator is True
    D = kd * (e - e_prev) / dt # zero on the first call
    u_raw = P + I + D
    u = clip(u_raw, output_min, output_max)
    I += (u - u_raw)          # back-calculation anti-windup, only when ki > 0

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

        Raises:
            ValueError: If any gain is negative, ``dt <= 0``, or
                ``output_min >= output_max``.
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

        self._kp = kp
        self._ki = ki
        self._kd = kd
        self._dt = dt
        self._output_min = output_min
        self._output_max = output_max

        self._integral: float = 0.0
        self._prev_error: float | None = None
        self._last_output: float = 0.0

    @property
    def integral(self) -> float:
        """Current integral accumulator value [%] (read-only)."""
        return self._integral

    @property
    def last_output(self) -> float:
        """Output [%] from the most recent :meth:`compute` call (read-only)."""
        return self._last_output

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
            self._integral += self._ki * error * dt

        if self._prev_error is not None:
            d_term = self._kd * (error - self._prev_error) / dt
        else:
            d_term = 0.0

        u_raw = p_term + self._integral + d_term
        u_clamped = max(self._output_min, min(self._output_max, u_raw))

        if self._ki > 0:
            self._integral += u_clamped - u_raw

        self._prev_error = error
        self._last_output = u_clamped

        return u_clamped

    def reset(self) -> None:
        """Reset the integral accumulator, previous error and last output."""
        self._integral = 0.0
        self._prev_error = None
        self._last_output = 0.0
