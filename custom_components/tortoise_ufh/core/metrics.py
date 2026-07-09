"""Simulation quality metrics and assertion helpers for tortoise-ufh.

Provides :class:`SimMetrics`, a frozen dataclass that aggregates the
per-timestep records of a :class:`~tortoise_ufh.simulation_log.SimulationLog`
into numeric quality indicators covering comfort, fast-source utilisation,
energy, and floor safety. Metrics are computed by the deterministic
:meth:`SimMetrics.from_log` classmethod in a single pass over the log, so the
same log and parameters always produce the same result.

Also provides assertion helpers that grade a log for correctness. Each raises
:class:`AssertionError` with a diagnostic message when a constraint is
violated (they are meant to be called directly inside simulation tests):

    - :func:`assert_comfort` -- comfort percentage above a threshold.
    - :func:`assert_floor_temp_safe` -- slab temperature below a hard ceiling.
    - :func:`assert_no_condensation` -- slab stays above the Magnus dew point
      plus a safety margin.
    - :func:`assert_no_freezing` -- no room ever drops below a hard minimum.
    - :func:`assert_no_prolonged_cold` -- no room stays cold for too long.
    - :func:`assert_max_overshoot` -- peak overshoot below a hard limit.
    - :func:`assert_valve_movement_moderate` -- actuator wear below a limit.

Units follow the simulation convention:
    Temperatures: degC
    Power: W
    Energy: kWh
    Valve position / percentages: 0-100 %
    Relative humidity: % in (0, 100]
    Time: minutes
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from .dew_point import dew_point as _dew_point

if TYPE_CHECKING:
    from .simulation_log import SimRecord, SimulationLog

# Fixed condensation margin used by the metric counter (Axiom: T_floor >= T_dew
# + 2 K). The assertion helper exposes it as a tunable ``margin`` parameter.
_CONDENSATION_MARGIN_K: float = 2.0

# Type alias for the optional room filter accepted by the cold-comfort helpers.
type RoomFilter = frozenset[str] | set[str] | None


# ---------------------------------------------------------------------------
# Record accessors -- bind to the frozen SimRecord constructor fields only
# ---------------------------------------------------------------------------


def _room_humidity(rec: SimRecord) -> float | None:
    """Return a usable relative humidity for *rec*, or ``None``.

    Prefers the room's own humidity probe (``inputs.humidity_pct``) and falls
    back to the weather snapshot. Values outside ``(0, 100]`` are unusable for
    the Magnus dew point and reported as ``None``.

    Args:
        rec: The simulation record to read.

    Returns:
        Relative humidity [%] in ``(0, 100]``, or ``None`` when unavailable.
    """
    rh = rec.inputs.humidity_pct
    if rh is None:
        rh = rec.weather.humidity
    if rh is None or not (0.0 < rh <= 100.0):
        return None
    return rh


def _record_dew_point(rec: SimRecord) -> float | None:
    """Return the Magnus dew point for *rec*, or ``None`` when uncomputable.

    Requires both a room air temperature and a usable relative humidity.

    Args:
        rec: The simulation record to read.

    Returns:
        Dew-point temperature [degC], or ``None``.
    """
    t_room = rec.inputs.room_temperature_c
    rh = _room_humidity(rec)
    if t_room is None or rh is None:
        return None
    return _dew_point(t_room, rh)


# ---------------------------------------------------------------------------
# SimMetrics -- frozen aggregation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimMetrics:
    """Immutable aggregation of simulation quality metrics.

    Computed from a :class:`~tortoise_ufh.simulation_log.SimulationLog` via
    :meth:`from_log`. All percentage fields use the 0-100 scale, consistent
    with the repo convention for valve position and humidity.

    Attributes:
        comfort_pct: Percentage of timesteps where
            ``|T_room - setpoint| <= comfort_band`` [0-100 %]. Records with a
            missing room temperature count as not comfortable.
        max_overshoot: Maximum positive deviation ``T_room - setpoint``
            [degC]; ``0.0`` when the room never exceeds the setpoint.
        max_undershoot: Maximum positive shortfall ``setpoint - T_room``
            [degC]; ``0.0`` when the room never falls below the setpoint.
        mean_deviation: Mean absolute deviation ``|T_room - setpoint|`` over
            records with a valid room temperature [degC].
        fast_source_runtime_pct: Percentage of timesteps where the fast source
            (split) command is ON [0-100 %].
        energy_kwh: Total UFH energy delivered [kWh]. Integrated from the
            recorded per-step allocated floor power (``SimRecord.q_floor_w``)
            when available (D6, 2026-07-09); otherwise from valve position and
            ``ufh_nominal_power_w``. ``None`` when neither source is supplied.
        condensation_events: Number of timesteps where
            ``T_slab < T_dew + 2`` (floor-cooling condensation risk).
        max_floor_temp: Maximum slab/floor temperature over the log [degC].
        min_floor_temp: Minimum slab/floor temperature over the log [degC].
        valve_travel_pct_per_h: Mean absolute commanded-valve movement per
            hour [pp/h] — the actuator-wear proxy (D7, 2026-07-09).
    """

    # -- Comfort --------------------------------------------------------------
    comfort_pct: float
    max_overshoot: float
    max_undershoot: float
    mean_deviation: float

    # -- Fast source ----------------------------------------------------------
    fast_source_runtime_pct: float

    # -- Energy (nullable) ----------------------------------------------------
    energy_kwh: float | None

    # -- Safety ---------------------------------------------------------------
    condensation_events: int
    max_floor_temp: float
    min_floor_temp: float

    # -- Actuator wear (additive, 2026-07-09) ----------------------------------
    valve_travel_pct_per_h: float = 0.0

    def __post_init__(self) -> None:
        """Validate metric ranges.

        Raises:
            ValueError: If any percentage is outside ``[0, 100]``, any
                non-negative quantity is negative, or the floor-temperature
                bounds are inconsistent (``min > max``).
        """
        if not (0.0 <= self.comfort_pct <= 100.0):
            msg = f"comfort_pct must be in [0, 100], got {self.comfort_pct}"
            raise ValueError(msg)
        if not (0.0 <= self.fast_source_runtime_pct <= 100.0):
            msg = (
                "fast_source_runtime_pct must be in [0, 100], "
                f"got {self.fast_source_runtime_pct}"
            )
            raise ValueError(msg)
        if self.max_overshoot < 0.0:
            msg = f"max_overshoot must be >= 0, got {self.max_overshoot}"
            raise ValueError(msg)
        if self.max_undershoot < 0.0:
            msg = f"max_undershoot must be >= 0, got {self.max_undershoot}"
            raise ValueError(msg)
        if self.mean_deviation < 0.0:
            msg = f"mean_deviation must be >= 0, got {self.mean_deviation}"
            raise ValueError(msg)
        if self.energy_kwh is not None and self.energy_kwh < 0.0:
            msg = f"energy_kwh must be >= 0 or None, got {self.energy_kwh}"
            raise ValueError(msg)
        if self.condensation_events < 0:
            msg = f"condensation_events must be >= 0, got {self.condensation_events}"
            raise ValueError(msg)
        if self.min_floor_temp > self.max_floor_temp:
            msg = (
                f"min_floor_temp ({self.min_floor_temp}) must be <= "
                f"max_floor_temp ({self.max_floor_temp})"
            )
            raise ValueError(msg)
        if self.valve_travel_pct_per_h < 0.0:
            msg = (
                "valve_travel_pct_per_h must be >= 0, "
                f"got {self.valve_travel_pct_per_h}"
            )
            raise ValueError(msg)

    # -- Factory --------------------------------------------------------------

    @classmethod
    def from_log(
        cls,
        log: SimulationLog,
        setpoint: float,
        *,
        comfort_band: float = 0.5,
        ufh_nominal_power_w: float | None = None,
        dt_minutes: int = 1,
    ) -> SimMetrics:
        """Compute metrics from a simulation log in a single pass.

        The computation is deterministic and safe for empty or single-record
        logs. For multi-room logs the caller should filter with
        ``log.get_room()`` first so comfort/energy refer to one room.

        Args:
            log: Simulation log to analyse.
            setpoint: Target room temperature [degC].
            comfort_band: Half-width of the comfort band [degC]; a timestep is
                comfortable when ``|T_room - setpoint| <= comfort_band``.
            ufh_nominal_power_w: Nominal UFH loop power at full valve [W].
                Required to compute ``energy_kwh``; ``None`` leaves it ``None``.
            dt_minutes: Simulation timestep length [minutes].

        Returns:
            A frozen :class:`SimMetrics` instance.

        Raises:
            ValueError: If ``ufh_nominal_power_w`` is negative or
                ``dt_minutes`` is not positive.
        """
        if ufh_nominal_power_w is not None and ufh_nominal_power_w < 0.0:
            msg = f"ufh_nominal_power_w must be >= 0, got {ufh_nominal_power_w}"
            raise ValueError(msg)
        if dt_minutes <= 0:
            msg = f"dt_minutes must be > 0, got {dt_minutes}"
            raise ValueError(msg)

        n = len(log)
        compute_energy = ufh_nominal_power_w is not None

        # -- Empty log --------------------------------------------------------
        if n == 0:
            return cls(
                comfort_pct=0.0,
                max_overshoot=0.0,
                max_undershoot=0.0,
                mean_deviation=0.0,
                fast_source_runtime_pct=0.0,
                energy_kwh=0.0 if compute_energy else None,
                condensation_events=0,
                max_floor_temp=0.0,
                min_floor_temp=0.0,
                valve_travel_pct_per_h=0.0,
            )

        # -- Accumulators -----------------------------------------------------
        comfort_count = 0
        valid_temp_count = 0
        max_over = 0.0
        max_under = 0.0
        total_abs_dev = 0.0

        fast_on_count = 0

        condensation_count = 0
        floor_max = float("-inf")
        floor_min = float("inf")

        total_floor_energy_j = 0.0
        allocated_energy_j = 0.0
        any_allocated = False
        dt_seconds = dt_minutes * 60.0

        valve_travel_pp = 0.0
        prev_valve: float | None = None

        # -- Single pass ------------------------------------------------------
        for rec in log:
            t_room = rec.inputs.room_temperature_c
            if t_room is not None:
                valid_temp_count += 1
                deviation = t_room - setpoint
                abs_dev = abs(deviation)
                total_abs_dev += abs_dev
                if abs_dev <= comfort_band:
                    comfort_count += 1
                if deviation > max_over:
                    max_over = deviation
                if -deviation > max_under:
                    max_under = -deviation

            # Fast-source runtime.
            if rec.outputs.fast_source.on:
                fast_on_count += 1

            # Floor (slab) temperature extremes.
            t_slab = rec.t_slab
            floor_max = max(floor_max, t_slab)
            floor_min = min(floor_min, t_slab)

            # Condensation risk (floor cooling): T_slab below dew point + 2 K.
            t_dew = _record_dew_point(rec)
            if t_dew is not None and t_slab < t_dew + _CONDENSATION_MARGIN_K:
                condensation_count += 1

            # Energy: prefer the recorded allocated floor power (D6); keep the
            # nominal-power estimate as the fallback for older logs.
            if rec.q_floor_w is not None:
                any_allocated = True
                allocated_energy_j += abs(rec.q_floor_w) * dt_seconds
            if compute_energy:
                assert ufh_nominal_power_w is not None
                floor_power = (
                    rec.outputs.valve_position_pct / 100.0
                ) * ufh_nominal_power_w
                total_floor_energy_j += floor_power * dt_seconds

            # Actuator wear: accumulate |delta valve| between records (D7).
            valve = rec.outputs.valve_position_pct
            if prev_valve is not None:
                valve_travel_pp += abs(valve - prev_valve)
            prev_valve = valve

        # -- Finalise ---------------------------------------------------------
        comfort_pct = (comfort_count / n) * 100.0
        fast_source_runtime_pct = (fast_on_count / n) * 100.0
        mean_deviation = total_abs_dev / valid_temp_count if valid_temp_count else 0.0
        if any_allocated:
            energy_kwh: float | None = allocated_energy_j / 3_600_000.0
        elif compute_energy:
            energy_kwh = total_floor_energy_j / 3_600_000.0
        else:
            energy_kwh = None
        duration_hours = (n * dt_minutes) / 60.0
        travel_per_h = valve_travel_pp / duration_hours if duration_hours > 0 else 0.0

        return cls(
            comfort_pct=comfort_pct,
            max_overshoot=max(max_over, 0.0),
            max_undershoot=max(max_under, 0.0),
            mean_deviation=mean_deviation,
            fast_source_runtime_pct=fast_source_runtime_pct,
            energy_kwh=energy_kwh,
            condensation_events=condensation_count,
            max_floor_temp=floor_max,
            min_floor_temp=floor_min,
            valve_travel_pct_per_h=travel_per_h,
        )

    # -- Comparison -----------------------------------------------------------

    def compare(self, other: SimMetrics) -> dict[str, float | None]:
        """Compute per-field deltas ``self.value - other.value``.

        A positive delta means ``self`` is higher than ``other``. When either
        side is ``None`` (energy without power params) the delta is ``None``.

        Args:
            other: The :class:`SimMetrics` to compare against.

        Returns:
            A mapping from field name to numeric delta (or ``None``).
        """
        result: dict[str, float | None] = {}
        for f in fields(SimMetrics):
            self_val = getattr(self, f.name)
            other_val = getattr(other, f.name)
            if self_val is None or other_val is None:
                result[f.name] = None
            elif isinstance(self_val, int | float) and isinstance(
                other_val, int | float
            ):
                result[f.name] = float(self_val) - float(other_val)
        return result


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def assert_comfort(
    log: SimulationLog,
    setpoint: float,
    *,
    comfort_band: float = 0.5,
    threshold: float = 90.0,
) -> None:
    """Assert the comfort percentage meets or exceeds *threshold*.

    A timestep is comfortable when ``|T_room - setpoint| <= comfort_band``.
    Records with a missing room temperature count as not comfortable.

    Args:
        log: Simulation log to check.
        setpoint: Target room temperature [degC].
        comfort_band: Half-width of the comfort band [degC].
        threshold: Minimum acceptable comfort percentage [0-100 %].

    Raises:
        AssertionError: If the log is empty or the comfort percentage is
            below *threshold*.
    """
    n = len(log)
    if n == 0:
        msg = "assert_comfort: empty log -- cannot assess comfort"
        raise AssertionError(msg)

    comfort_count = 0
    for rec in log:
        t_room = rec.inputs.room_temperature_c
        if t_room is not None and abs(t_room - setpoint) <= comfort_band:
            comfort_count += 1

    comfort_pct = (comfort_count / n) * 100.0
    if comfort_pct < threshold:
        msg = (
            f"assert_comfort: comfort {comfort_pct:.1f}% is below threshold "
            f"{threshold:.1f}% (setpoint={setpoint}, band={comfort_band}, "
            f"comfortable={comfort_count}/{n})"
        )
        raise AssertionError(msg)


def assert_floor_temp_safe(
    log: SimulationLog,
    *,
    max_temp: float = 34.0,
) -> None:
    """Assert the slab/floor temperature never exceeds a hard ceiling.

    Enforces ``T_slab <= max_temp`` at every timestep. Empty logs pass
    silently (no records to violate). Condensation is checked separately by
    :func:`assert_no_condensation`.

    Args:
        log: Simulation log to check.
        max_temp: Maximum allowed floor temperature [degC].

    Raises:
        AssertionError: On the first record where ``T_slab > max_temp``.
    """
    for rec in log:
        t_slab = rec.t_slab
        if t_slab > max_temp:
            msg = (
                f"assert_floor_temp_safe: T_slab={t_slab:.2f} degC exceeds "
                f"max {max_temp:.2f} degC at t={rec.t} min"
            )
            raise AssertionError(msg)


def assert_no_condensation(
    log: SimulationLog,
    *,
    margin: float = 2.0,
) -> None:
    """Assert the slab stays above the dew point by at least *margin*.

    Enforces ``T_slab >= T_dew + margin`` at every timestep where a dew point
    can be computed (room temperature and relative humidity available), using
    the Magnus :func:`~tortoise_ufh.dew_point.dew_point`. Records without
    humidity data are skipped. Empty logs pass silently.

    Args:
        log: Simulation log to check.
        margin: Required gap above the dew point [degC]; must be >= 0.

    Raises:
        ValueError: If *margin* is negative.
        AssertionError: On the first record where ``T_slab < T_dew + margin``.
    """
    if margin < 0.0:
        msg = f"margin must be >= 0, got {margin}"
        raise ValueError(msg)

    for rec in log:
        t_dew = _record_dew_point(rec)
        if t_dew is None:
            continue
        t_slab = rec.t_slab
        if t_slab < t_dew + margin:
            msg = (
                f"assert_no_condensation: T_slab={t_slab:.2f} degC "
                f"< T_dew+{margin:.1f}={t_dew + margin:.2f} degC "
                f"(condensation risk) at t={rec.t} min"
            )
            raise AssertionError(msg)


def assert_no_freezing(
    log: SimulationLog,
    *,
    hard_min: float = 16.0,
    skip_rooms: RoomFilter = None,
) -> None:
    """Assert no room ever drops below ``hard_min`` degC.

    Hard-fail comfort check: a single record with ``T_room < hard_min`` fails.
    Multi-room aware -- every record is checked regardless of ``room_name``,
    so interleaved multi-room logs are handled. Records with a missing room
    temperature are skipped. Empty logs pass silently.

    Args:
        log: Simulation log to check.
        hard_min: Hard minimum room temperature [degC].
        skip_rooms: Optional set of room names to exclude entirely. Use
            sparingly, only for rooms that are physically under-powered by a
            known, tracked issue.

    Raises:
        AssertionError: On the first record where ``T_room < hard_min``.
    """
    skip = skip_rooms or frozenset()
    for rec in log:
        if rec.room_name in skip:
            continue
        t_room = rec.inputs.room_temperature_c
        if t_room is not None and t_room < hard_min:
            room_label = rec.room_name if rec.room_name else "<unnamed>"
            msg = (
                f"assert_no_freezing: T_room={t_room:.2f} degC "
                f"< hard_min={hard_min:.2f} degC in room '{room_label}' "
                f"at t={rec.t} min"
            )
            raise AssertionError(msg)


def _raise_prolonged_cold(
    room_name: str,
    run_start_t: int,
    duration: int,
    min_temp: float,
    threshold: float,
    max_duration_minutes: int,
) -> None:
    """Raise :class:`AssertionError` for a prolonged cold-run violation."""
    room_label = room_name if room_name else "<unnamed>"
    msg = (
        f"assert_no_prolonged_cold: room '{room_label}' stayed below "
        f"{threshold:.2f} degC for {duration} min "
        f"(max allowed: {max_duration_minutes} min) "
        f"starting at t={run_start_t} min, "
        f"reaching min T_room={min_temp:.2f} degC"
    )
    raise AssertionError(msg)


def assert_no_prolonged_cold(
    log: SimulationLog,
    *,
    threshold: float = 18.0,
    max_duration_minutes: int = 1440,
    skip_rooms: RoomFilter = None,
) -> None:
    """Assert no room stays below *threshold* for too long.

    A "cold run" is a maximal contiguous block of records (per room) where
    ``T_room < threshold``; its duration is the difference in ``rec.t``
    between the last and first records of the block. A record that meets or
    exceeds the threshold -- or whose room temperature is missing -- resets
    the current run. Records are grouped by ``room_name`` and each room's
    chronological sequence is scanned independently. Empty logs pass silently.

    Args:
        log: Simulation log to check.
        threshold: Cold-run temperature ceiling [degC].
        max_duration_minutes: Maximum allowed cold-run duration [minutes].
        skip_rooms: Optional set of room names to exclude entirely.

    Raises:
        AssertionError: On the first cold run whose duration strictly exceeds
            ``max_duration_minutes``.
    """
    skip = skip_rooms or frozenset()
    by_room: dict[str, list[SimRecord]] = {}
    for rec in log:
        if rec.room_name in skip:
            continue
        by_room.setdefault(rec.room_name, []).append(rec)

    for room_name, records in by_room.items():
        run_start_t: int | None = None
        run_min_temp = float("inf")
        for rec in records:
            t_room = rec.inputs.room_temperature_c
            if t_room is not None and t_room < threshold:
                if run_start_t is None:
                    run_start_t = rec.t
                    run_min_temp = t_room
                elif t_room < run_min_temp:
                    run_min_temp = t_room
                duration = rec.t - run_start_t
                if duration > max_duration_minutes:
                    _raise_prolonged_cold(
                        room_name,
                        run_start_t,
                        duration,
                        run_min_temp,
                        threshold,
                        max_duration_minutes,
                    )
            else:
                run_start_t = None
                run_min_temp = float("inf")


def assert_max_overshoot(
    log: SimulationLog,
    setpoint: float,
    *,
    max_overshoot: float = 0.5,
    settle_from_minute: int = 0,
) -> None:
    """Assert the room never overshoots the setpoint by more than a limit.

    Guards the project's PRIMARY goal (anti-overshoot on a high-mass floor,
    S13 2026-07-09): the plain comfort-percentage check happily passed a
    +1.2 K overshoot because the band is symmetric and time-averaged.

    Args:
        log: Simulation log to check (single room).
        setpoint: Target room temperature [degC].
        max_overshoot: Maximum allowed ``T_room - setpoint`` [K]; must be >= 0.
        settle_from_minute: Ignore records before this simulation minute
            (e.g. a deliberate cold-start transient), default 0.

    Raises:
        ValueError: If ``max_overshoot`` is negative.
        AssertionError: On the first record exceeding the limit.
    """
    if max_overshoot < 0.0:
        msg = f"max_overshoot must be >= 0, got {max_overshoot}"
        raise ValueError(msg)
    for rec in log:
        if rec.t < settle_from_minute:
            continue
        t_room = rec.inputs.room_temperature_c
        if t_room is not None and t_room - setpoint > max_overshoot:
            msg = (
                f"assert_max_overshoot: T_room={t_room:.2f} degC overshoots "
                f"setpoint={setpoint:.2f} by {t_room - setpoint:.2f} K "
                f"(> {max_overshoot:.2f} K) at t={rec.t} min"
            )
            raise AssertionError(msg)


def assert_valve_movement_moderate(
    log: SimulationLog,
    *,
    max_travel_pct_per_h: float = 30.0,
    dt_minutes: int = 1,
) -> None:
    """Assert the mean commanded-valve travel stays below a wear limit.

    Accumulates ``|delta valve|`` between consecutive records of a single
    room's log and normalises per hour (D7, 2026-07-09): a well-damped loop
    on a high-mass floor should reposition its actuators a few percentage
    points per hour, not tens.

    Args:
        log: Simulation log to check (single room, chronological).
        max_travel_pct_per_h: Maximum allowed mean travel [pp/h]; must be > 0.
        dt_minutes: Simulation timestep length [minutes].

    Raises:
        ValueError: If ``max_travel_pct_per_h`` or ``dt_minutes`` is not
            positive.
        AssertionError: If the mean travel exceeds the limit.
    """
    if max_travel_pct_per_h <= 0.0:
        msg = f"max_travel_pct_per_h must be > 0, got {max_travel_pct_per_h}"
        raise ValueError(msg)
    if dt_minutes <= 0:
        msg = f"dt_minutes must be > 0, got {dt_minutes}"
        raise ValueError(msg)
    n = len(log)
    if n < 2:
        return
    travel_pp = 0.0
    prev: float | None = None
    for rec in log:
        valve = rec.outputs.valve_position_pct
        if prev is not None:
            travel_pp += abs(valve - prev)
        prev = valve
    hours = (n * dt_minutes) / 60.0
    travel_per_h = travel_pp / hours
    if travel_per_h > max_travel_pct_per_h:
        msg = (
            f"assert_valve_movement_moderate: mean valve travel "
            f"{travel_per_h:.1f} pp/h exceeds {max_travel_pct_per_h:.1f} pp/h "
            f"(total {travel_pp:.0f} pp over {hours:.1f} h)"
        )
        raise AssertionError(msg)
