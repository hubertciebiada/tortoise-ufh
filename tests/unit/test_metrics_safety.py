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

import json
from dataclasses import dataclass
from typing import Any

import pytest

from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.metrics import (
    SimMetrics,
    assert_comfort,
    assert_floor_temp_safe,
    assert_no_condensation,
    assert_no_freezing,
    assert_no_prolonged_cold,
)
from custom_components.tortoise_ufh.core.models import (
    FastSourceCommand,
    FastSourceMode,
    Mode,
    RoomInputs,
    RoomOutputs,
    RoomReport,
)
from custom_components.tortoise_ufh.core.safety import (
    S1_FLOOR_OVERHEAT,
    S1_SUPPLY_OFF_C,
    S1_SUPPLY_ON_C,
    S3_ROOM_OFF_C,
    S3_ROOM_ON_C,
    SafetyAction,
    SafetyEvaluator,
    SafetyRule,
    SensorSnapshot,
)
from custom_components.tortoise_ufh.core.simulation_log import SimulationLog
from custom_components.tortoise_ufh.core.weather import WeatherPoint

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

    @pytest.mark.unit
    def test_results_carry_measurement_and_action_per_rule(self) -> None:
        """Every rule reports what it measured; only an active rule an action."""
        evaluator = SafetyEvaluator()
        snapshot = SensorSnapshot(
            supply_temperature_c=42.0,
            room_temperature_c=22.0,
            humidity_pct=None,
            last_update_age_minutes=3.0,
        )
        by_name = {r.rule.name: r for r in evaluator.evaluate(snapshot)}
        s1 = by_name["s1_floor_overheat"]
        assert s1.triggered is True
        assert s1.measured_value == pytest.approx(42.0)
        assert s1.action is SafetyAction.CLOSE_VALVE
        s5 = by_name["s5_watchdog"]
        assert s5.triggered is False
        assert s5.measured_value == pytest.approx(3.0)
        assert s5.action is None
        assert by_name["s2_condensation"].measured_value is None
        assert by_name["s3_emergency_heat"].measured_value == pytest.approx(22.0)

    @pytest.mark.unit
    def test_result_to_dict_is_plain_json(self) -> None:
        """``to_dict`` flattens the rule and the enum action for the panel."""
        evaluator = SafetyEvaluator()
        snapshot = SensorSnapshot(
            supply_temperature_c=42.0,
            room_temperature_c=22.0,
            humidity_pct=50.0,
            last_update_age_minutes=1.0,
        )
        dicts = [r.to_dict() for r in evaluator.evaluate(snapshot)]
        s1 = next(d for d in dicts if d["name"] == "s1_floor_overheat")
        assert s1 == {
            "name": "s1_floor_overheat",
            "priority": 1,
            "triggered": True,
            "measured_value": 42.0,
            "action": "close_valve",
        }
        s5 = next(d for d in dicts if d["name"] == "s5_watchdog")
        assert s5["triggered"] is False
        assert s5["action"] is None
        assert json.loads(json.dumps(dicts)) == dicts

    @pytest.mark.unit
    def test_rule_names_must_be_unique(self) -> None:
        """Two rules sharing a name are rejected at construction."""
        with pytest.raises(ValueError, match="safety rule names must be unique"):
            SafetyEvaluator(rules=(S1_FLOOR_OVERHEAT, S1_FLOOR_OVERHEAT))

    @pytest.mark.unit
    def test_rules_are_evaluated_in_priority_order(self) -> None:
        """Results come back sorted by ascending priority."""
        evaluator = SafetyEvaluator()
        priorities = [rule.priority for rule in evaluator.rules]
        assert priorities == sorted(priorities)
        assert evaluator.active_rule_names == ()


class TestS2HardThresholdsK6:
    """Pin the K6 (2026-07-12) hard-S2 thresholds: trip at the RAW dew point.

    After the margin de-stacking the hard rule is a backstop BELOW the local
    graduated ramp: it must NOT trip anywhere inside the ramp band
    (``0 < supply - dew < 2 K``) and must trip only at physical condensation
    onset (``supply - dew < 0``), clearing with hysteresis above ``+1 K``.
    A regression back to the old ``+2 K`` trip margin fails these tests.
    """

    @staticmethod
    def _snapshot(supply_offset_k: float) -> SensorSnapshot:
        """Build a 24 degC / 70 %RH snapshot with supply = dew + offset."""
        t_dew = dew_point(24.0, 70.0)
        return SensorSnapshot(
            supply_temperature_c=t_dew + supply_offset_k,
            room_temperature_c=24.0,
            humidity_pct=70.0,
            last_update_age_minutes=1.0,
        )

    @pytest.mark.unit
    def test_inside_local_ramp_band_does_not_trip(self) -> None:
        """A supply 0.8 K above the dew point (inside the ramp) is NOT S2."""
        evaluator = SafetyEvaluator()
        flags = evaluator.active_flags(self._snapshot(+0.8))
        assert "s2_condensation" not in flags

    @pytest.mark.unit
    def test_trips_only_below_raw_dew_point(self) -> None:
        """Supply below the raw dew point trips the hard rule."""
        evaluator = SafetyEvaluator()
        assert "s2_condensation" in evaluator.active_flags(self._snapshot(-0.2))

    @pytest.mark.unit
    def test_clear_hysteresis_above_one_kelvin(self) -> None:
        """A tripped rule holds at +0.5 K and clears only above +1 K."""
        evaluator = SafetyEvaluator()
        assert "s2_condensation" in evaluator.active_flags(self._snapshot(-0.2))
        # Inside the hysteresis band: stays tripped.
        assert "s2_condensation" in evaluator.active_flags(self._snapshot(+0.5))
        # Above the +1 K clear threshold: releases.
        assert "s2_condensation" not in evaluator.active_flags(self._snapshot(+1.3))


# ---------------------------------------------------------------------------
# SensorSnapshot / SafetyRule validation and evaluator boundary behaviour
# ---------------------------------------------------------------------------


def _safety_snapshot(
    *,
    supply: float | None = 25.0,
    room: float | None = 22.0,
    humidity: float | None = 50.0,
    age_minutes: float = 1.0,
) -> SensorSnapshot:
    """Build a :class:`SensorSnapshot` with safe, neutral defaults.

    Args:
        supply: Supply-water temperature [degC], or ``None``.
        room: Room air temperature [degC], or ``None``.
        humidity: Relative humidity [%], or ``None``.
        age_minutes: Minutes since the last successful update.

    Returns:
        A validated :class:`SensorSnapshot`.
    """
    return SensorSnapshot(
        supply_temperature_c=supply,
        room_temperature_c=room,
        humidity_pct=humidity,
        last_update_age_minutes=age_minutes,
    )


def _room_condition(snapshot: SensorSnapshot) -> float | None:
    """Condition callable extracting the room temperature (test rules).

    Args:
        snapshot: The snapshot to read.

    Returns:
        The room air temperature [degC], or ``None``.
    """
    return snapshot.room_temperature_c


def _rule_kwargs(**overrides: Any) -> dict[str, Any]:
    """Return valid :class:`SafetyRule` kwargs with *overrides* applied.

    Args:
        overrides: Constructor fields to replace.

    Returns:
        A kwargs dict that constructs a valid ``trigger_above`` rule.
    """
    kwargs: dict[str, Any] = {
        "name": "test_rule",
        "description": "rule under test",
        "priority": 1,
        "threshold_on": 10.0,
        "threshold_off": 8.0,
        "action": SafetyAction.CLOSE_VALVE,
        "condition": _room_condition,
        "trigger_above": True,
    }
    kwargs.update(overrides)
    return kwargs


class TestSensorSnapshotValidation:
    """``SensorSnapshot`` validates humidity range and watchdog age."""

    @pytest.mark.unit
    @pytest.mark.parametrize("humidity", [0.0, 0.5, 100.0])
    def test_humidity_boundary_values_accepted(self, humidity: float) -> None:
        """Humidity is valid across the whole CLOSED interval [0, 100] %."""
        snapshot = _safety_snapshot(humidity=humidity)
        assert snapshot.humidity_pct == humidity

    @pytest.mark.unit
    @pytest.mark.parametrize("humidity", [-0.1, 100.5])
    def test_humidity_out_of_range_rejected(self, humidity: float) -> None:
        """Humidity outside [0, 100] % raises with the documented message."""
        with pytest.raises(ValueError, match=r"humidity_pct must be in \[0, 100\]"):
            _safety_snapshot(humidity=humidity)

    @pytest.mark.unit
    def test_negative_update_age_rejected(self) -> None:
        """A negative watchdog age raises with the documented message."""
        with pytest.raises(
            ValueError, match=r"^last_update_age_minutes must be >= 0, got "
        ):
            _safety_snapshot(age_minutes=-0.1)

    @pytest.mark.unit
    def test_zero_update_age_accepted(self) -> None:
        """A zero watchdog age (a just-received update) is valid."""
        assert _safety_snapshot(age_minutes=0.0).last_update_age_minutes == 0.0


class TestSafetyRuleValidation:
    """``SafetyRule.__post_init__`` enforces identity, priority and band order."""

    @pytest.mark.unit
    def test_valid_trigger_above_rule_constructs(self) -> None:
        """A trigger_above rule with ``threshold_off < threshold_on`` is valid."""
        rule = SafetyRule(**_rule_kwargs())
        assert rule.name == "test_rule"
        assert rule.trigger_above is True

    @pytest.mark.unit
    def test_valid_trigger_below_rule_constructs(self) -> None:
        """A trigger_below rule with ``threshold_off > threshold_on`` is valid."""
        rule = SafetyRule(
            **_rule_kwargs(trigger_above=False, threshold_on=8.0, threshold_off=10.0)
        )
        assert rule.trigger_above is False

    @pytest.mark.unit
    def test_equal_thresholds_allowed_for_both_directions(self) -> None:
        """A degenerate zero-width band (off == on) is valid both ways."""
        above = SafetyRule(**_rule_kwargs(threshold_on=9.0, threshold_off=9.0))
        below = SafetyRule(
            **_rule_kwargs(trigger_above=False, threshold_on=9.0, threshold_off=9.0)
        )
        assert above.threshold_off == above.threshold_on
        assert below.threshold_off == below.threshold_on

    @pytest.mark.unit
    def test_empty_name_rejected_with_exact_message(self) -> None:
        """An empty rule name raises with the documented message, verbatim."""
        with pytest.raises(ValueError, match=r"^SafetyRule name must be non-empty$"):
            SafetyRule(**_rule_kwargs(name=""))

    @pytest.mark.unit
    def test_priority_one_accepted(self) -> None:
        """Priority 1 (the highest priority) is the smallest valid value."""
        assert SafetyRule(**_rule_kwargs(priority=1)).priority == 1

    @pytest.mark.unit
    def test_priority_zero_rejected(self) -> None:
        """Priority < 1 raises with the documented message."""
        with pytest.raises(ValueError, match=r"priority must be >= 1"):
            SafetyRule(**_rule_kwargs(priority=0))

    @pytest.mark.unit
    def test_trigger_above_off_above_on_rejected(self) -> None:
        """A trigger_above band with off > on (inverted hysteresis) raises."""
        with pytest.raises(ValueError, match=r"must be <= threshold_on"):
            SafetyRule(**_rule_kwargs(threshold_on=8.0, threshold_off=10.0))

    @pytest.mark.unit
    def test_trigger_below_off_below_on_rejected(self) -> None:
        """A trigger_below band with off < on (inverted hysteresis) raises."""
        with pytest.raises(ValueError, match=r"must be >= threshold_on"):
            SafetyRule(
                **_rule_kwargs(
                    trigger_above=False, threshold_on=10.0, threshold_off=8.0
                )
            )


class TestEvaluatorThresholdBoundaries:
    """Hysteresis comparisons are strict: equality never flips the state.

    A rule trips only strictly ABOVE/BELOW ``threshold_on`` and clears only
    strictly past ``threshold_off``, so a measurement sitting exactly on a
    threshold leaves the prior state untouched.
    """

    @pytest.mark.unit
    def test_s1_does_not_trip_exactly_at_threshold_on(self) -> None:
        """Supply exactly at 40 degC does NOT trip S1 (strict ``>``)."""
        evaluator = SafetyEvaluator()
        flags = evaluator.active_flags(_safety_snapshot(supply=S1_SUPPLY_ON_C))
        assert "s1_floor_overheat" not in flags

    @pytest.mark.unit
    def test_s1_stays_active_exactly_at_threshold_off(self) -> None:
        """A tripped S1 holds at exactly 38 degC (clears only strictly below)."""
        evaluator = SafetyEvaluator()
        evaluator.evaluate(_safety_snapshot(supply=S1_SUPPLY_ON_C + 1.0))
        flags = evaluator.active_flags(_safety_snapshot(supply=S1_SUPPLY_OFF_C))
        assert "s1_floor_overheat" in flags

    @pytest.mark.unit
    def test_s3_does_not_trip_exactly_at_threshold_on(self) -> None:
        """Room exactly at 5 degC does NOT trip S3 (strict ``<``)."""
        evaluator = SafetyEvaluator()
        flags = evaluator.active_flags(_safety_snapshot(room=S3_ROOM_ON_C))
        assert "s3_emergency_heat" not in flags

    @pytest.mark.unit
    def test_s3_stays_active_exactly_at_threshold_off(self) -> None:
        """A tripped S3 holds at exactly 6 degC (clears only strictly above)."""
        evaluator = SafetyEvaluator()
        evaluator.evaluate(_safety_snapshot(room=S3_ROOM_ON_C - 1.0))
        flags = evaluator.active_flags(_safety_snapshot(room=S3_ROOM_OFF_C))
        assert "s3_emergency_heat" in flags

    @pytest.mark.unit
    def test_reset_clears_all_tripped_rules(self) -> None:
        """``reset()`` returns every rule to INACTIVE, not to tripped."""
        evaluator = SafetyEvaluator()
        evaluator.evaluate(_safety_snapshot(supply=S1_SUPPLY_ON_C + 1.0))
        assert evaluator.active_rule_names != ()
        evaluator.reset()
        assert evaluator.active_rule_names == ()
        assert evaluator.active_flags(_safety_snapshot()) == ()


class TestCondensationMarginHumidityEdge:
    """S2 treats a non-positive humidity as "no usable sensor", 1 % as valid."""

    @pytest.mark.unit
    def test_zero_humidity_yields_no_measurement(self) -> None:
        """0 % RH is not a usable reading: S2 reports ``None``, no exception."""
        evaluator = SafetyEvaluator()
        snapshot = _safety_snapshot(supply=17.0, room=24.0, humidity=0.0)
        by_name = {r.rule.name: r for r in evaluator.evaluate(snapshot)}
        assert by_name["s2_condensation"].measured_value is None
        assert by_name["s2_condensation"].triggered is False

    @pytest.mark.unit
    def test_one_percent_humidity_yields_a_measurement(self) -> None:
        """1 % RH is a valid (if dry) reading: S2 measures ``supply - dew``."""
        evaluator = SafetyEvaluator()
        snapshot = _safety_snapshot(supply=20.0, room=24.0, humidity=1.0)
        by_name = {r.rule.name: r for r in evaluator.evaluate(snapshot)}
        assert by_name["s2_condensation"].measured_value == pytest.approx(
            20.0 - dew_point(24.0, 1.0)
        )
        # Far above the dew point: no condensation alarm on dry air.
        assert by_name["s2_condensation"].triggered is False
