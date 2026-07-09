"""Parametrized simulation scenario tests for tortoise-ufh.

Drives the deterministic scenarios registered in
:data:`tortoise_ufh.scenarios.SCENARIO_LIBRARY` through the session-scoped
``run_scenario`` harness (defined in ``tests/simulation/conftest.py``) and
grades each run with the ``assert_*`` helpers from
:mod:`tortoise_ufh.metrics`, applied per room.

Since 2026-07-09 (five-agent FMEA, phases C+D) the WHOLE library is the merge
gate — the previous "SLOW tier exercised elsewhere" comment was false; nothing
exercised ``cold_snap`` / ``solar_overshoot`` / ``spring_transition``:

    * ``steady_heating``          -- 48 h heating; run at BOTH the 60 s harness
      step and the production 300 s takt, with the anti-overshoot assertion
      (S13) guarding the project's primary goal.
    * ``cold_snap``               -- 5-day recovery from a -15 degC step with a
      realistic weather-compensation curve.
    * ``solar_overshoot``         -- March sun; the controller must fully close
      the valves whenever free gains carry a room above the setpoint.
    * ``spring_transition``       -- 7-day transitional drift; every valve must
      stay parked at 0 across the setpoint crossing.
    * ``hot_july_floor_cooling``  -- 48 h floor cooling; the S2 throttle and
      the global safe dew point must be ACTIVE, and cooling must actually flow
      (the pre-calibration twin cooled itself through an absurd ground sink and
      the assertion passed with permanently closed valves).
    * ``sensor_dropout``          -- 24 h heating with heavy sensor noise.
    * ``split_boost``             -- 24 h heating with a 2.5 kW split boost.

``sensor_dropout`` is the deliberate *control* case for the no-freezing
check: its 2 K sensor-noise standard deviation drives the **measured** room
temperature below the hard freeze floor (the physical slab never freezes), so
the heating-only assertion is expected to trip and is wrapped in
``pytest.raises``.

Units:
    Temperatures / setpoints: degC; margins: kelvin; comfort: percent 0-100.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

import pytest

from custom_components.tortoise_ufh.core.config import SimScenario
from custom_components.tortoise_ufh.core.metrics import (
    SimMetrics,
    assert_comfort,
    assert_floor_temp_safe,
    assert_max_overshoot,
    assert_no_condensation,
    assert_no_freezing,
    assert_no_prolonged_cold,
    assert_valve_movement_moderate,
)
from custom_components.tortoise_ufh.core.models import Mode
from custom_components.tortoise_ufh.core.scenarios import SCENARIO_LIBRARY
from custom_components.tortoise_ufh.core.simulation_log import SimulationLog

# ---------------------------------------------------------------------------
# Scenario tiers + grading constants
# ---------------------------------------------------------------------------

GATE_SCENARIOS: list[str] = [
    "steady_heating",
    "cold_snap",
    "solar_overshoot",
    "spring_transition",
    "hot_july_floor_cooling",
    "sensor_dropout",
    "split_boost",
]
"""Every library scenario — ALL of them gate the merge (2026-07-09)."""

# The single deliberate control case: heavy sensor noise makes the *measured*
# room temperature dip below the hard freeze floor, so no-freezing must trip.
_FREEZE_CONTROL_SCENARIO: str = "sensor_dropout"

_COMFORT_BAND_C: float = 0.5
"""Half-width of the comfort band around the setpoint [K]."""

_COMFORT_THRESHOLD_PCT: float = 60.0
"""Minimum comfort percentage for the lenient steady-state check [%].

Kept modest on purpose: the 48 h ``steady_heating`` window includes the
initial warm-up from the 20 degC reset state up to the 21 degC setpoint, so a
meaningful-but-forgiving floor is used rather than a tight tolerance.
"""

_MAX_OVERSHOOT_K: float = 0.5
"""Hard anti-overshoot ceiling for disturbance-free heating [K] (S13).

The project's PRIMARY goal. With the retuned 2026-07-09 defaults the measured
steady_heating overshoot is +0.18 K; the old ki=0.02 defaults produced +1.2 K
and would fail this gate.
"""

_CONDENSATION_MARGIN_K: float = 1.0
"""Required slab-above-dew-point gap for the no-condensation check [K].

Deliberately BELOW the 2.0 K control margin: on a muggy afternoon with the
valves closed the slab temperature is weather-driven and can sit exactly AT
``dew + 2`` (the controller cannot warm the slab in cooling mode), so grading
at the control margin fails on float-equality noise the loop cannot influence.
1.0 K still proves a solid buffer above physical condensation (margin 0).
"""

_FLOOR_MAX_C: float = 34.0
"""Hard ceiling on slab/floor temperature [degC]."""

_FREEZE_HARD_MIN_C: float = 16.0
"""Hard minimum room temperature for the no-freezing check [degC]."""

_MAX_VALVE_TRAVEL_PP_PER_H: float = 30.0
"""Actuator-wear ceiling: mean commanded-valve travel [pp/h] (D7)."""

# Fail fast on a stale scenario name so a library rename surfaces at collection.
_UNKNOWN: list[str] = [name for name in GATE_SCENARIOS if name not in SCENARIO_LIBRARY]
if _UNKNOWN:  # pragma: no cover - guards the GATE_SCENARIOS <-> library contract
    _msg = f"GATE_SCENARIOS references unknown scenarios: {sorted(_UNKNOWN)}"
    raise ValueError(_msg)
_MISSING: list[str] = [name for name in SCENARIO_LIBRARY if name not in GATE_SCENARIOS]
if _MISSING:  # pragma: no cover - every library scenario must gate the merge
    _msg = f"library scenarios missing from GATE_SCENARIOS: {sorted(_MISSING)}"
    raise ValueError(_msg)


# ---------------------------------------------------------------------------
# Harness protocol + helpers
# ---------------------------------------------------------------------------


class RunScenario(Protocol):
    """Structural type of the ``run_scenario`` conftest fixture.

    The harness builds the simulator and controller for *scenario*, runs the
    closed loop, and returns the recorded log together with its aggregate
    metrics. ``max_steps`` optionally caps the number of control ticks.
    """

    def __call__(
        self,
        scenario: SimScenario,
        max_steps: int | None = None,
    ) -> tuple[SimulationLog, SimMetrics]:
        """Run *scenario* and return ``(log, metrics)``."""
        ...


def _room_setpoint(scenario: SimScenario, room_name: str) -> float:
    """Return the effective setpoint for a room [degC].

    The per-room setpoint is the building's global home setpoint plus the
    scenario's per-room offset (zero when the room is not listed).

    Args:
        scenario: The scenario whose building/offsets to read.
        room_name: The room to resolve the setpoint for.

    Returns:
        The room's target temperature [degC].
    """
    offset = scenario.room_offsets.get(room_name, 0.0)
    return scenario.building.home_setpoint_c + offset


# ---------------------------------------------------------------------------
# TestScenarioSimulation -- gate scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.simulation
class TestScenarioSimulation:
    """Gate scenario tests parametrized over :data:`GATE_SCENARIOS`."""

    @pytest.mark.parametrize("scenario_name", GATE_SCENARIOS)
    def test_floor_temp_safe(
        self,
        scenario_name: str,
        run_scenario: RunScenario,
    ) -> None:
        """Slab temperature never exceeds the hard ceiling, per room.

        Applies :func:`~tortoise_ufh.metrics.assert_floor_temp_safe` to every
        room's sub-log. The slab temperature is ground truth (never noised),
        so this holds across heating, cooling and the sensor-dropout run.

        Args:
            scenario_name: Gate scenario key.
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY[scenario_name]()
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, f"{scenario_name}: empty simulation log"
        for room in scenario.building.rooms:
            assert_floor_temp_safe(log.get_room(room.name), max_temp=_FLOOR_MAX_C)

    @pytest.mark.parametrize("dt_seconds", [60.0, 300.0])
    def test_steady_heating_comfort_and_overshoot(
        self,
        dt_seconds: float,
        run_scenario: RunScenario,
    ) -> None:
        """Steady heating: comfortable, NO overshoot, moderate valve travel.

        Runs ``steady_heating`` at both the 60 s harness step and the
        production 300 s takt (S11: the shipped cycle time must be what the
        gate exercises). Asserts a lenient comfort percentage, the hard S13
        anti-overshoot ceiling (the project's primary goal) and the D7
        actuator-wear limit.

        Args:
            dt_seconds: Control/physics step for this parametrization [s].
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = replace(SCENARIO_LIBRARY["steady_heating"](), dt_seconds=dt_seconds)
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, "steady_heating: empty simulation log"
        dt_minutes = max(1, int(round(dt_seconds / 60.0)))
        for room in scenario.building.rooms:
            room_log = log.get_room(room.name)
            setpoint = _room_setpoint(scenario, room.name)
            assert_comfort(
                room_log,
                setpoint,
                comfort_band=_COMFORT_BAND_C,
                threshold=_COMFORT_THRESHOLD_PCT,
            )
            assert_max_overshoot(
                room_log,
                setpoint,
                max_overshoot=_MAX_OVERSHOOT_K,
            )
            assert_valve_movement_moderate(
                room_log,
                max_travel_pct_per_h=_MAX_VALVE_TRAVEL_PP_PER_H,
                dt_minutes=dt_minutes,
            )

    def test_cold_snap_recovery(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """A -15 degC snap never freezes a room nor leaves it cold for a day.

        With the weather-compensation curve the plant has finite, realistic
        authority; the controller must still keep every room above the hard
        freeze floor and recover it within the prolonged-cold budget.

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["cold_snap"]()
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, "cold_snap: empty simulation log"
        for room in scenario.building.rooms:
            room_log = log.get_room(room.name)
            assert_no_freezing(room_log, hard_min=_FREEZE_HARD_MIN_C)
            assert_no_prolonged_cold(
                room_log,
                threshold=18.0,
                max_duration_minutes=1440,
            )

    def test_solar_surplus_closes_valves(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """Solar overshoot: the floor must never ADD heat to a sunlit surplus.

        Free solar gains can physically carry an unshaded room above the
        setpoint — no heating-mode controller can prevent that. What the
        controller MUST guarantee is that it does not contribute: whenever a
        room sits clearly (>= 1 K) above its setpoint, its valve is fully
        closed (just past the deadband a small lingering integral is by
        design — the anti-windup back-calculation pins the PI output to the
        0 bound once the surplus exceeds ~1 K).

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["solar_overshoot"]()
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, "solar_overshoot: empty simulation log"
        for room in scenario.building.rooms:
            setpoint = _room_setpoint(scenario, room.name)
            for rec in log.get_room(room.name):
                t_room = rec.inputs.room_temperature_c
                if t_room is None or t_room <= setpoint + 1.0:
                    continue
                assert rec.outputs.valve_position_pct == 0.0, (
                    f"solar_overshoot: room '{room.name}' at "
                    f"{t_room:.2f} degC (setpoint {setpoint:.1f}) still had "
                    f"valve {rec.outputs.valve_position_pct:.0f}% at t={rec.t}"
                )

    def test_spring_transition_valves_parked_and_drifting(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """Transitional mode: valves parked at 0 while the house drifts.

        Every record of the 7-day shoulder-season run must command valve 0
        and no fast source (the bungalow has none), while the free-running
        rooms genuinely drift across the setpoint (the pre-calibration ground
        sink dragged the house to 12 degC instead).

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["spring_transition"]()
        assert scenario.mode == Mode.TRANSITIONAL
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, "spring_transition: empty simulation log"
        temps: list[float] = []
        for rec in log:
            assert rec.outputs.valve_position_pct == 0.0
            assert rec.outputs.fast_source.on is False
            if rec.inputs.room_temperature_c is not None:
                temps.append(rec.inputs.room_temperature_c)
        setpoint = scenario.building.home_setpoint_c
        assert min(temps) < setpoint < max(temps), (
            "spring_transition: expected the free-running house to drift "
            f"across the {setpoint} degC setpoint, got "
            f"{min(temps):.1f}..{max(temps):.1f} degC"
        )

    def test_no_condensation_and_cooling_actually_runs(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """Floor cooling stays condensation-safe WHILE genuinely cooling.

        Runs ``hot_july_floor_cooling`` and asserts three things per the I1
        amendment (2026-07-09):

        1. The slab stays ``margin`` kelvin above the Magnus dew point in
           every room (both protection layers + the supply floor fed back
           from the global safe dew point).
        2. Cooling actually flows — valves open for a substantial share of
           the run (the pre-calibration twin passed condensation checks with
           valves at a flat 0 %).
        3. The local S2 throttle is genuinely ACTIVE (factor < 1 at times) —
           the scenario exercises the protection, not just the PI loop.

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["hot_july_floor_cooling"]()
        assert scenario.mode == Mode.COOLING, "expected a cooling scenario"
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, "hot_july_floor_cooling: empty simulation log"
        for room in scenario.building.rooms:
            assert_no_condensation(
                log.get_room(room.name),
                margin=_CONDENSATION_MARGIN_K,
            )

        n_records = len(log)
        open_share = sum(1 for r in log if r.outputs.valve_position_pct > 0.0) / (
            n_records
        )
        assert open_share > 0.25, (
            "hot_july_floor_cooling: cooling never really ran — valves open "
            f"only {open_share:.0%} of room-records"
        )
        throttled = (
            sum(1 for r in log if r.outputs.report.dew_throttle_factor < 1.0)
            / n_records
        )
        assert throttled > 0.10, (
            "hot_july_floor_cooling: the S2 dew throttle never engaged "
            f"({throttled:.0%} of room-records) — the scenario no longer "
            "exercises the condensation protection"
        )

    def test_split_boost_engages_and_releases(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """The split boosts a cold room and releases inside the comfort band.

        Asserts the fast source actually ran (the demand starts ~3 K past the
        boost offset), did NOT run permanently (the release hysteresis works),
        and that anti priority-inversion held: whenever the split was ON while
        the room still called for heat, the floor valve stayed open too.

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["split_boost"]()
        log, metrics = run_scenario(scenario)

        assert len(log) > 0, "split_boost: empty simulation log"
        assert metrics.fast_source_runtime_pct > 5.0, (
            "split_boost: the split never engaged "
            f"({metrics.fast_source_runtime_pct:.1f}% runtime)"
        )
        assert metrics.fast_source_runtime_pct < 95.0, (
            "split_boost: the split never released "
            f"({metrics.fast_source_runtime_pct:.1f}% runtime)"
        )
        room = scenario.building.rooms[0]
        setpoint = _room_setpoint(scenario, room.name)
        for rec in log.get_room(room.name):
            t_room = rec.inputs.room_temperature_c
            if t_room is None:
                continue
            calling_for_heat = setpoint - t_room > 1.0
            if rec.outputs.fast_source.on and calling_for_heat:
                assert rec.outputs.valve_position_pct > 0.0, (
                    "split_boost: anti priority-inversion violated — split ON "
                    f"with a closed valve at t={rec.t} (T_room={t_room:.2f})"
                )

    @pytest.mark.parametrize("scenario_name", GATE_SCENARIOS)
    def test_no_freezing(
        self,
        scenario_name: str,
        run_scenario: RunScenario,
    ) -> None:
        """No heating room ever drops below the hard freeze floor, per room.

        The no-freezing floor is an active-heating invariant, so cooling- and
        transitional-mode scenarios are skipped. ``sensor_dropout`` is the
        deliberate control
        case: its 2 K measurement noise drives the *reported* room temperature
        below :data:`_FREEZE_HARD_MIN_C` (the physical slab never freezes), so
        the assertion is expected to trip and is wrapped in ``pytest.raises``.

        Args:
            scenario_name: Gate scenario key.
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY[scenario_name]()
        if scenario.mode in (Mode.COOLING, Mode.TRANSITIONAL):
            # TRANSITIONAL parks every valve BY DESIGN — a free-drifting
            # corridor touching 16 degC on a shoulder-season night measures
            # the weather, not the controller.
            pytest.skip(f"{scenario_name}: no-freezing needs an active heater")

        log, _metrics = run_scenario(scenario)
        assert len(log) > 0, f"{scenario_name}: empty simulation log"

        if scenario_name == _FREEZE_CONTROL_SCENARIO:
            with pytest.raises(AssertionError):
                for room in scenario.building.rooms:
                    assert_no_freezing(
                        log.get_room(room.name),
                        hard_min=_FREEZE_HARD_MIN_C,
                    )
            return

        for room in scenario.building.rooms:
            assert_no_freezing(
                log.get_room(room.name),
                hard_min=_FREEZE_HARD_MIN_C,
            )
