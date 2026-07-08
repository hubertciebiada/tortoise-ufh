"""Unit tests for metrics aggregation, assertion helpers, and the safety layer.

Covers three core modules against their frozen public contracts:

    * :mod:`tortoise_ufh.metrics` -- :meth:`SimMetrics.from_log` computes
      ``comfort_pct``, overshoot/undershoot, and ``condensation_events`` from a
      small synthetic :class:`~tortoise_ufh.simulation_log.SimulationLog`; the
      ``assert_*`` helpers pass on a well-behaved log and raise
      :class:`AssertionError` on a pathological one.
    * :mod:`tortoise_ufh.safety` -- :class:`SafetyEvaluator` flags S1 (floor
      overheat via the supply-water proxy) and S2 (floor-cooling condensation).

Synthetic records are built from a frozen :class:`_StepSpec` value object so
every fixture datum is validated at construction, mirroring the repo-wide
"frozen dataclass + ``__post_init__``" convention.

Units:
    Temperatures: degC
    Valve position / humidity / comfort: percent (0..100)
    Time: minutes
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tortoise_ufh.dew_point import dew_point
from tortoise_ufh.metrics import (
    SimMetrics,
    assert_comfort,
    assert_floor_temp_safe,
    assert_no_condensation,
    assert_no_freezing,
    assert_no_prolonged_cold,
)
from tortoise_ufh.models import (
    FastSourceCommand,
    FastSourceMode,
    Mode,
    RoomInputs,
    RoomOutputs,
    RoomReport,
)
from tortoise_ufh.safety import SafetyEvaluator, SensorSnapshot
from tortoise_ufh.simulation_log import SimulationLog
from tortoise_ufh.weather import WeatherPoint

# Shared constants for the synthetic fixtures (degC).
_SETPOINT_C: float = 21.0
_COMFORT_BAND_C: float = 0.5


# ---------------------------------------------------------------------------
# Synthetic-record value object + log builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StepSpec:
    """One synthetic simulation timestep for building a test log.

    Attributes:
        t: Simulation time [minutes], must be >= 0.
        t_room: Measured room air temperature [degC], or ``None`` (sensor lost).
        t_slab: Ground-truth slab temperature [degC].
        valve_pct: Commanded valve position [0-100 %].
        humidity_pct: Relative humidity [%] in (0, 100].
        mode: The room's operating :class:`~tortoise_ufh.models.Mode`.
        fast_on: Whether the fast-source command is on.

    Raises:
        ValueError: If ``t`` is negative, ``valve_pct`` is outside [0, 100], or
            ``humidity_pct`` is outside (0, 100].
    """

    t: int
    t_room: float | None
    t_slab: float
    valve_pct: float = 0.0
    humidity_pct: float = 50.0
    mode: Mode = Mode.HEATING
    fast_on: bool = False

    def __post_init__(self) -> None:
        """Validate time, valve, and humidity ranges."""
        if self.t < 0:
            msg = f"t must be >= 0 minutes, got {self.t}"
            raise ValueError(msg)
        if not (0.0 <= self.valve_pct <= 100.0):
            msg = f"valve_pct must be in [0, 100] %, got {self.valve_pct}"
            raise ValueError(msg)
        if not (0.0 < self.humidity_pct <= 100.0):
            msg = f"humidity_pct must be in (0, 100] %, got {self.humidity_pct}"
            raise ValueError(msg)


def _make_report(valve_pct: float) -> RoomReport:
    """Build a minimal valid :class:`RoomReport` for a synthetic step.

    Args:
        valve_pct: Raw valve position [0-100 %] to record.

    Returns:
        A frozen :class:`RoomReport` with neutral decision terms.
    """
    return RoomReport(
        error_c=0.0,
        trend_c_per_h=0.0,
        room_dew_point_c=None,
        p_term=0.0,
        i_term=0.0,
        trend_term=0.0,
        feedforward_term=0.0,
        raw_valve_pct=valve_pct,
        valve_floor_applied=False,
        saturated=False,
        dew_throttle_factor=1.0,
        integrator_frozen=False,
    )


def _make_outputs(spec: _StepSpec) -> RoomOutputs:
    """Build :class:`RoomOutputs` for a synthetic step spec.

    Args:
        spec: The step specification.

    Returns:
        A frozen :class:`RoomOutputs` carrying the valve and fast-source command.
    """
    fast_source = (
        FastSourceCommand(
            on=True,
            mode=FastSourceMode.HEATING,
            target_temperature_c=_SETPOINT_C,
        )
        if spec.fast_on
        else FastSourceCommand(on=False, mode=FastSourceMode.OFF)
    )
    return RoomOutputs(
        valve_position_pct=spec.valve_pct,
        fast_source=fast_source,
        report=_make_report(spec.valve_pct),
    )


def _build_log(specs: list[_StepSpec], *, room_name: str = "salon") -> SimulationLog:
    """Assemble a :class:`SimulationLog` from a list of step specs.

    Args:
        specs: The synthetic steps, in chronological order.
        room_name: Room identifier stamped on every record.

    Returns:
        A populated :class:`SimulationLog`.
    """
    log = SimulationLog()
    for spec in specs:
        inputs = RoomInputs(
            mode=spec.mode,
            setpoint_c=_SETPOINT_C,
            room_temperature_c=spec.t_room,
            humidity_pct=spec.humidity_pct,
        )
        weather = WeatherPoint(
            T_out=5.0,
            GHI=0.0,
            wind_speed=0.0,
            humidity=spec.humidity_pct,
        )
        log.append_from_step(
            t=spec.t,
            inputs=inputs,
            outputs=_make_outputs(spec),
            weather=weather,
            t_slab=spec.t_slab,
            room_name=room_name,
        )
    return log


# ---------------------------------------------------------------------------
# SimMetrics.from_log
# ---------------------------------------------------------------------------


class TestSimMetricsFromLog:
    """Deterministic aggregation of a synthetic log into :class:`SimMetrics`."""

    @pytest.mark.unit
    def test_comfort_pct_counts_in_band_steps(self) -> None:
        """comfort_pct equals the fraction of steps within the comfort band."""
        specs = [
            _StepSpec(t=0, t_room=21.0, t_slab=24.0),  # comfortable
            _StepSpec(t=1, t_room=21.3, t_slab=24.0),  # comfortable (0.3 K)
            _StepSpec(t=2, t_room=20.8, t_slab=24.0),  # comfortable (0.2 K)
            _StepSpec(t=3, t_room=22.5, t_slab=24.0),  # not comfortable (1.5 K)
        ]
        metrics = SimMetrics.from_log(
            _build_log(specs), setpoint=_SETPOINT_C, comfort_band=_COMFORT_BAND_C
        )
        assert metrics.comfort_pct == pytest.approx(75.0)

    @pytest.mark.unit
    def test_overshoot_and_undershoot_are_signed_extremes(self) -> None:
        """Overshoot/undershoot are the largest deviations above/below setpoint."""
        specs = [
            _StepSpec(t=0, t_room=23.0, t_slab=24.0),  # +2.0 K over
            _StepSpec(t=1, t_room=19.5, t_slab=24.0),  # -1.5 K under
            _StepSpec(t=2, t_room=21.0, t_slab=24.0),
        ]
        metrics = SimMetrics.from_log(_build_log(specs), setpoint=_SETPOINT_C)
        assert metrics.max_overshoot == pytest.approx(2.0)
        assert metrics.max_undershoot == pytest.approx(1.5)

    @pytest.mark.unit
    def test_condensation_events_count_slab_below_dew_margin(self) -> None:
        """Steps with T_slab < T_dew + 2 K are counted as condensation events."""
        # At 24 degC / 70 %RH the dew point is ~18.2 degC, so the +2 K margin
        # ceiling is ~20.2 degC. A 19.0 degC slab is a condensation event; a
        # 22.0 degC slab is safe.
        t_dew = dew_point(24.0, 70.0)
        assert t_dew + 2.0 == pytest.approx(20.19, abs=0.1)
        specs = [
            _StepSpec(
                t=0, t_room=24.0, t_slab=19.0, humidity_pct=70.0, mode=Mode.COOLING
            ),  # risk
            _StepSpec(
                t=1, t_room=24.0, t_slab=18.5, humidity_pct=70.0, mode=Mode.COOLING
            ),  # risk
            _StepSpec(
                t=2, t_room=24.0, t_slab=22.0, humidity_pct=70.0, mode=Mode.COOLING
            ),  # safe
        ]
        metrics = SimMetrics.from_log(_build_log(specs), setpoint=24.0)
        assert metrics.condensation_events == 2

    @pytest.mark.unit
    def test_empty_log_returns_zeroed_metrics(self) -> None:
        """An empty log yields zeroed, still-valid metrics."""
        metrics = SimMetrics.from_log(SimulationLog(), setpoint=_SETPOINT_C)
        assert metrics.comfort_pct == 0.0
        assert metrics.condensation_events == 0
        assert metrics.energy_kwh is None


# ---------------------------------------------------------------------------
# Assertion helpers: pass on a good log, raise on a bad one
# ---------------------------------------------------------------------------


def _good_log() -> SimulationLog:
    """A well-behaved heating log: comfortable, safe slab, dry, never cold."""
    return _build_log(
        [_StepSpec(t=t, t_room=21.0, t_slab=25.0, valve_pct=20.0) for t in range(6)]
    )


class TestAssertionHelpersPassOnGoodLog:
    """Every ``assert_*`` helper accepts a nominal log without raising."""

    @pytest.mark.unit
    def test_all_helpers_pass(self) -> None:
        """The good log satisfies comfort, floor-safety, and cold checks."""
        log = _good_log()
        assert_comfort(log, _SETPOINT_C, comfort_band=_COMFORT_BAND_C)
        assert_floor_temp_safe(log)
        assert_no_condensation(log)
        assert_no_freezing(log)
        assert_no_prolonged_cold(log)


class TestAssertionHelpersRaiseOnBadLog:
    """Each helper raises :class:`AssertionError` on its specific violation."""

    @pytest.mark.unit
    def test_assert_comfort_raises_when_out_of_band(self) -> None:
        """A persistently off-target room fails the comfort assertion."""
        log = _build_log([_StepSpec(t=t, t_room=25.0, t_slab=25.0) for t in range(6)])
        with pytest.raises(AssertionError, match="comfort"):
            assert_comfort(log, _SETPOINT_C, comfort_band=_COMFORT_BAND_C)

    @pytest.mark.unit
    def test_assert_floor_temp_safe_raises_on_overheat(self) -> None:
        """A slab above the 34 degC ceiling fails the floor-safety assertion."""
        log = _build_log([_StepSpec(t=0, t_room=21.0, t_slab=36.0)])
        with pytest.raises(AssertionError, match="exceeds"):
            assert_floor_temp_safe(log)

    @pytest.mark.unit
    def test_assert_no_condensation_raises_on_wet_cold_slab(self) -> None:
        """A cold slab below the dew-point margin fails the condensation check."""
        log = _build_log(
            [
                _StepSpec(
                    t=0, t_room=25.0, t_slab=17.0, humidity_pct=80.0, mode=Mode.COOLING
                ),
            ]
        )
        with pytest.raises(AssertionError, match="condensation"):
            assert_no_condensation(log)

    @pytest.mark.unit
    def test_assert_no_condensation_rejects_negative_margin(self) -> None:
        """A negative margin argument is a programming error (ValueError)."""
        with pytest.raises(ValueError, match="margin must be >= 0"):
            assert_no_condensation(_good_log(), margin=-1.0)

    @pytest.mark.unit
    def test_assert_no_freezing_raises_below_hard_min(self) -> None:
        """A single sub-16 degC record fails the freeze-protection assertion."""
        log = _build_log(
            [
                _StepSpec(t=0, t_room=21.0, t_slab=25.0),
                _StepSpec(t=1, t_room=15.0, t_slab=25.0),
            ]
        )
        with pytest.raises(AssertionError, match="hard_min"):
            assert_no_freezing(log)

    @pytest.mark.unit
    def test_assert_no_prolonged_cold_raises_on_long_cold_run(self) -> None:
        """A cold run longer than the allowed duration fails the assertion."""
        # Two records 200 min apart, both below the 18 degC ceiling, exceed a
        # 100 min budget.
        log = _build_log(
            [
                _StepSpec(t=0, t_room=17.0, t_slab=22.0),
                _StepSpec(t=200, t_room=17.0, t_slab=22.0),
            ]
        )
        with pytest.raises(AssertionError, match="prolonged_cold"):
            assert_no_prolonged_cold(log, max_duration_minutes=100)


# ---------------------------------------------------------------------------
# SafetyEvaluator: S1 floor overheat + S2 condensation
# ---------------------------------------------------------------------------


class TestSafetyEvaluator:
    """The evaluator flags S1 and S2 on the appropriate snapshots."""

    @pytest.mark.unit
    def test_s1_floor_overheat_flagged_above_threshold(self) -> None:
        """Supply water above 40 degC trips S1 (floor overheat)."""
        evaluator = SafetyEvaluator()
        snapshot = SensorSnapshot(
            supply_temperature_c=42.0,
            room_temperature_c=22.0,
            humidity_pct=50.0,
            last_update_age_minutes=1.0,
        )
        flags = evaluator.active_flags(snapshot)
        assert "s1_floor_overheat" in flags
        assert "s2_condensation" not in flags

    @pytest.mark.unit
    def test_s1_hysteresis_holds_between_on_and_off(self) -> None:
        """Once tripped, S1 stays active until supply drops below 38 degC."""
        evaluator = SafetyEvaluator()
        hot = SensorSnapshot(
            supply_temperature_c=41.0,
            room_temperature_c=22.0,
            humidity_pct=50.0,
            last_update_age_minutes=1.0,
        )
        evaluator.evaluate(hot)  # trip S1
        # 39 degC is inside the hysteresis band (< 40 on, > 38 off): stays on.
        warm = SensorSnapshot(
            supply_temperature_c=39.0,
            room_temperature_c=22.0,
            humidity_pct=50.0,
            last_update_age_minutes=1.0,
        )
        assert "s1_floor_overheat" in evaluator.active_flags(warm)

    @pytest.mark.unit
    def test_s2_condensation_flagged_when_supply_near_dew_point(self) -> None:
        """Cold cooling supply near the room dew point trips S2 (condensation)."""
        evaluator = SafetyEvaluator()
        # 24 degC / 70 %RH -> dew ~18.2 degC; a 17 degC supply is within the
        # dew-point margin, so the condensation margin is negative and S2 trips.
        snapshot = SensorSnapshot(
            supply_temperature_c=17.0,
            room_temperature_c=24.0,
            humidity_pct=70.0,
            last_update_age_minutes=1.0,
        )
        flags = evaluator.active_flags(snapshot)
        assert "s2_condensation" in flags
        assert "s1_floor_overheat" not in flags

    @pytest.mark.unit
    def test_s2_holds_state_when_humidity_missing(self) -> None:
        """A missing humidity reading preserves S2's prior (inactive) state."""
        evaluator = SafetyEvaluator()
        snapshot = SensorSnapshot(
            supply_temperature_c=17.0,
            room_temperature_c=24.0,
            humidity_pct=None,
            last_update_age_minutes=1.0,
        )
        assert "s2_condensation" not in evaluator.active_flags(snapshot)
