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
      the assertion passed with permanently closed valves). Run at BOTH the
      60 s harness step and the production 300 s takt (B8, 2026-07-12).
    * ``night_setback``           -- 4-day 21 <-> 19 degC setpoint schedule;
      the K1 (2026-07-12) operating-point gate: bounded heating-above-band
      integral, prompt valve close after a setback, bounded sag.
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
    "night_setback",
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

_RECOVERY_SETTLE_HOURS: float = 12.0
"""Post-step settle window for the cold-snap recovery-overshoot check [h] (K8).

The supply step (weather-comp curve jumping 29.5 -> 37 degC) is a 2-3x plant
gain change the PI was not tuned at; the recovery transient is graded only
after this window. Measured @ 300 s with the 2026-07-12 controller: worst-room
max_over is +0.78 K from the step and +0.34 K from step+12 h (the K1 unwind
discharges the recovery integral; before it the peak was +1.15 K).
"""

_RECOVERY_MAX_OVERSHOOT_K: float = 0.5
"""Recovery-overshoot ceiling after the cold-snap settle window [K] (K8)."""

_SETBACK_MAX_HOT_HEATING_PCT_H: float = 400.0
"""Ceiling on the per-room heating integral while ABOVE the band [%*h] (K1).

Sum of ``valve_pct * dt`` over records with ``T > setpoint + 0.3`` and
``valve > 0.5 %`` across the whole 4-day ``night_setback`` run. Measured on
the twin @ 60 s: worst room 264 %*h with the bumpless transfer + asymmetric
unwind versus 2577 %*h without them (the old controller kept actively heating
rooms sitting above a freshly lowered setpoint for hours).
"""

_SETBACK_MIN_ROOM_C: float = 17.8
"""Undershoot floor for ``night_setback`` [degC] (K1).

The deepest measured room temperature is 18.14 degC (a plant-limited room
coasting below the 19 degC night target at T_out = 0). Guards against an
unwind regression aggressive enough to let rooms sag markedly deeper.
"""

_SETBACK_CLOSE_WITHIN_MINUTES: float = 240.0
"""Grace window after a 21 -> 19 setback [min] (K1).

Past this window a room must not keep ACTIVELY heating above the new band
(``valve > 5 %`` — the write threshold — while ``T > setpoint + 0.3``).
Measured @ 60 s: strong rooms close within ~2.4 h; the worst plant-limited
room trails a <= 5.5 % closing tail to ~3.2 h as the unwind rate shrinks near
the band edge. Without the K1 mechanisms the discharge ran 10+ h at 50-80 %
valve, so the regression margin stays enormous.
"""

_S6_QUIET_FLAGS: frozenset[str] = frozenset(
    {
        "loop_no_flow",
        "actuation_test_running",
        "actuation_test_failed",
    }
)
"""S6 / self-test flags that must NEVER appear on healthy loops.

Acceptance criterion 4 of ``docs/NO_FLOW_WATCHDOG.md`` (2026-07-13): the
hydraulic no-flow watchdog must be silent across every healthy library
scenario. This is the permanent regression fence — before the twin's probes
became actuation-aware, a loop commanded open with no hydraulic response
banked false ``loop_no_flow`` windows in night_setback / hot_july / solar runs.
"""

_S6_QUIET_LOOP_STATUSES: frozenset[str] = frozenset({"no_flow"})
"""Per-loop ``loop_flow_status`` values that count as an S6 alarm."""


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

        Since 2026-07-12 (K8) the RECOVERY OVERSHOOT is asserted too: after
        the supply step the plant gain is 2-3x what the PI was tuned at and
        round 2 measured an unasserted +1.15 K recovery peak. The check runs
        from :data:`_RECOVERY_SETTLE_HOURS` after the weather step (the raw
        transient peak is plant-gain physics; the settled tail is the
        controller's responsibility).

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["cold_snap"]()
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, "cold_snap: empty simulation log"
        step_minute = 1440
        settle_from = step_minute + int(_RECOVERY_SETTLE_HOURS * 60.0)
        for room in scenario.building.rooms:
            room_log = log.get_room(room.name)
            assert_no_freezing(room_log, hard_min=_FREEZE_HARD_MIN_C)
            assert_no_prolonged_cold(
                room_log,
                threshold=18.0,
                max_duration_minutes=1440,
            )
            assert_max_overshoot(
                room_log,
                _room_setpoint(scenario, room.name),
                max_overshoot=_RECOVERY_MAX_OVERSHOOT_K,
                settle_from_minute=settle_from,
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

    @pytest.mark.parametrize("dt_seconds", [60.0, 300.0])
    def test_no_condensation_and_cooling_actually_runs(
        self,
        dt_seconds: float,
        run_scenario: RunScenario,
    ) -> None:
        """Floor cooling stays condensation-safe WHILE genuinely cooling.

        Runs ``hot_july_floor_cooling`` at both the 60 s harness step and the
        production 300 s takt (B8, 2026-07-12 — mirroring steady_heating) and
        asserts three things per the I1 amendment (2026-07-09):

        1. The slab stays ``margin`` kelvin above the Magnus dew point in
           every room (both protection layers + the supply floor fed back
           from the global safe dew point). Measured minimum slab-dew margin
           @ 300 s: +1.51 K.
        2. Cooling actually flows — valves open for a substantial share of
           the run (the pre-calibration twin passed condensation checks with
           valves at a flat 0 %). After the K6 margin de-stacking
           (2026-07-12) full cooling is available right on the heat pump's
           dew floor: the measured open share jumped from 29.7 % to 94.2 %,
           so the gate now demands a solid majority instead of a token 25 %.
        3. The local S2 throttle is genuinely ACTIVE (factor < 1 at times) —
           the scenario exercises the protection, not just the PI loop
           (measured @ 300 s: factor < 1 for 31.2 % of room-records, the
           humid transients where the supply dips below the pump floor).

        Args:
            dt_seconds: Control/physics step for this parametrization [s].
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = replace(
            SCENARIO_LIBRARY["hot_july_floor_cooling"](), dt_seconds=dt_seconds
        )
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
        assert open_share > 0.60, (
            "hot_july_floor_cooling: cooling under-delivers — valves open "
            f"only {open_share:.0%} of room-records (K6 de-stacked margins "
            "should yield a solid majority; measured 94 %)"
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

    def test_night_setback_bumpless_transfer(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """A daily setpoint schedule neither heats warm rooms nor sags deep.

        The K1 gate (2026-07-12): operating-point changes were the round-2
        blind spot — a lowered setpoint left the saturated integral actively
        heating an already-too-warm room for 17.4 h. Asserts, per room, over
        the 4-day 21 <-> 19 degC schedule:

        1. The heating integral accumulated while the room sits ABOVE the
           current band (``T > setpoint + 0.3`` with ``valve > 0.5 %``) stays
           under :data:`_SETBACK_MAX_HOT_HEATING_PCT_H` (measured: 264 %*h
           with the bumpless transfer + unwind vs 2577 %*h without).
        2. From :data:`_SETBACK_CLOSE_WITHIN_MINUTES` after each 21 -> 19
           setback to the end of that phase, no room keeps ACTIVELY heating
           clearly above the new band (``T > 19.4`` with ``valve > 5 %`` —
           the actuator write threshold; a sub-5-pp closing tail parked at
           the band edge is noise-level) — either the valve closed in time
           (measured worst: strong rooms ~2.4 h) or the room legitimately
           coasted into the band.
        3. No room ever sags below :data:`_SETBACK_MIN_ROOM_C` — the unwind
           must not turn the setback into a free-fall (measured worst:
           18.14 degC on a plant-limited room).

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["night_setback"]()
        assert scenario.setpoint_schedule, "expected a setpoint schedule"
        log, _metrics = run_scenario(scenario)
        assert len(log) > 0, "night_setback: empty simulation log"

        dt_h = scenario.dt_seconds / 3600.0
        setback_minutes = [
            minute for minute, sp in scenario.setpoint_schedule if sp == 19.0
        ]
        for room in scenario.building.rooms:
            room_log = list(log.get_room(room.name))
            hot_heating_pct_h = sum(
                rec.outputs.valve_position_pct * dt_h
                for rec in room_log
                if rec.inputs.room_temperature_c is not None
                and rec.inputs.room_temperature_c > rec.inputs.setpoint_c + 0.3
                and rec.outputs.valve_position_pct > 0.5
            )
            assert hot_heating_pct_h <= _SETBACK_MAX_HOT_HEATING_PCT_H, (
                f"night_setback: room '{room.name}' spent "
                f"{hot_heating_pct_h:.0f} %*h actively heating above the band "
                f"(limit {_SETBACK_MAX_HOT_HEATING_PCT_H:.0f} %*h) — the "
                "bumpless transfer / unwind regressed"
            )
            for rec in room_log:
                t_room = rec.inputs.room_temperature_c
                assert t_room is None or t_room >= _SETBACK_MIN_ROOM_C, (
                    f"night_setback: room '{room.name}' sagged to "
                    f"{t_room:.2f} degC at t={rec.t} min (floor "
                    f"{_SETBACK_MIN_ROOM_C} degC)"
                )
            for minute in setback_minutes:
                grace_end = minute + _SETBACK_CLOSE_WITHIN_MINUTES
                phase_end = minute + 720.0  # the schedule alternates every 12 h
                for rec in room_log:
                    if not grace_end <= rec.t < phase_end:
                        continue
                    t_room = rec.inputs.room_temperature_c
                    # "Clearly above the band": +0.1 K over the band edge, so
                    # a room asymptotically parked AT 19.300x with a residual
                    # write-threshold-level valve is not a violation.
                    still_hot_and_heating = (
                        t_room is not None
                        and t_room > rec.inputs.setpoint_c + 0.4
                        and rec.outputs.valve_position_pct > 5.0
                    )
                    assert not still_hot_and_heating, (
                        f"night_setback: room '{room.name}' still heats above "
                        f"the band {rec.t - minute:.0f} min after the setback "
                        f"at t={minute:.0f} (T={t_room}, valve="
                        f"{rec.outputs.valve_position_pct:.1f} %) — grace is "
                        f"{_SETBACK_CLOSE_WITHIN_MINUTES:.0f} min"
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
    def test_flow_watchdog_silent_on_healthy_loops(
        self,
        scenario_name: str,
        run_scenario: RunScenario,
    ) -> None:
        """S6 acceptance criterion 4: zero watchdog flags on healthy loops.

        Every record of every library scenario — heating, cooling,
        transitional, setbacks, sensor dropout, split boost — must be free
        of :data:`_S6_QUIET_FLAGS` and must not report a per-loop status in
        :data:`_S6_QUIET_LOOP_STATUSES`. All actuators in these runs are
        honest and healthy, so any hit is a false positive of the hydraulic
        no-flow watchdog (or a self-test that nobody started).

        Args:
            scenario_name: Gate scenario key.
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY[scenario_name]()
        log, _metrics = run_scenario(scenario)
        assert len(log) > 0, f"{scenario_name}: empty simulation log"

        for rec in log:
            offending = _S6_QUIET_FLAGS.intersection(rec.outputs.report.flags)
            assert not offending, (
                f"{scenario_name}: false S6 flag(s) {sorted(offending)} on a "
                f"healthy loop (room '{rec.room_name}', t={rec.t} min)"
            )
            statuses = set(rec.outputs.report.loop_flow_status)
            alarmed = statuses & _S6_QUIET_LOOP_STATUSES
            assert not alarmed, (
                f"{scenario_name}: false per-loop status {sorted(alarmed)} on "
                f"a healthy loop (room '{rec.room_name}', t={rec.t} min)"
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
