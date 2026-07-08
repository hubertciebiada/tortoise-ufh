"""Parametrized simulation scenario tests for tortoise-ufh.

Drives the deterministic scenarios registered in
:data:`tortoise_ufh.scenarios.SCENARIO_LIBRARY` through the session-scoped
``run_scenario`` harness (defined in ``tests/simulation/conftest.py``) and
grades each run with the ``assert_*`` helpers from
:mod:`tortoise_ufh.metrics`, applied per room.

Scenarios are split into two duration tiers; only the FAST tier (<= 48 h
horizon) runs here so the simulation suite stays inside a CI time budget:

    * ``steady_heating``          -- 48 h heating, single well-insulated room.
    * ``hot_july_floor_cooling``  -- 48 h floor cooling, high humidity.
    * ``sensor_dropout``          -- 24 h heating with heavy sensor noise.

The longer heating transients (``cold_snap``, ``solar_overshoot``,
``spring_transition``) are the SLOW tier and are exercised elsewhere.

``sensor_dropout`` is the deliberate *control* case for the no-freezing
check: its 2 K sensor-noise standard deviation drives the **measured** room
temperature below the hard freeze floor (the physical slab never freezes), so
the heating-only assertion is expected to trip and is wrapped in
``pytest.raises``.

Units:
    Temperatures / setpoints: degC; margins: kelvin; comfort: percent 0-100.
"""

from __future__ import annotations

from typing import Protocol

import pytest

from tortoise_ufh.config import SimScenario
from tortoise_ufh.metrics import (
    SimMetrics,
    assert_comfort,
    assert_floor_temp_safe,
    assert_no_condensation,
    assert_no_freezing,
)
from tortoise_ufh.models import Mode
from tortoise_ufh.scenarios import SCENARIO_LIBRARY
from tortoise_ufh.simulation_log import SimulationLog

# ---------------------------------------------------------------------------
# Scenario tier + grading constants
# ---------------------------------------------------------------------------

FAST_SCENARIOS: list[str] = [
    "steady_heating",
    "hot_july_floor_cooling",
    "sensor_dropout",
]
"""FAST-tier scenario names (<= 48 h horizon) parametrising the class."""

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

_CONDENSATION_MARGIN_K: float = 2.0
"""Required slab-above-dew-point gap for the no-condensation check [K]."""

_FLOOR_MAX_C: float = 34.0
"""Hard ceiling on slab/floor temperature [degC]."""

_FREEZE_HARD_MIN_C: float = 16.0
"""Hard minimum room temperature for the no-freezing check [degC]."""

# Fail fast on a stale scenario name so a library rename surfaces at collection.
_UNKNOWN: list[str] = [name for name in FAST_SCENARIOS if name not in SCENARIO_LIBRARY]
if _UNKNOWN:  # pragma: no cover - guards the FAST_SCENARIOS <-> library contract
    _msg = f"FAST_SCENARIOS references unknown scenarios: {sorted(_UNKNOWN)}"
    raise ValueError(_msg)


# ---------------------------------------------------------------------------
# Harness protocol + helpers
# ---------------------------------------------------------------------------


class RunScenario(Protocol):
    """Structural type of the ``run_scenario`` conftest fixture.

    The harness builds the simulator and controller for *scenario*, runs the
    closed loop, and returns the recorded log together with its aggregate
    metrics. ``max_steps`` optionally caps the number of simulated minutes.
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
# TestScenarioSimulation -- FAST-tier parametrized scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.simulation
class TestScenarioSimulation:
    """FAST-tier scenario tests parametrized over :data:`FAST_SCENARIOS`."""

    @pytest.mark.parametrize("scenario_name", FAST_SCENARIOS)
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
            scenario_name: FAST-tier scenario key.
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY[scenario_name]()
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, f"{scenario_name}: empty simulation log"
        for room in scenario.building.rooms:
            assert_floor_temp_safe(log.get_room(room.name), max_temp=_FLOOR_MAX_C)

    def test_comfort_reasonable(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """Steady-state heating keeps the room reasonably comfortable.

        Runs ``steady_heating`` (single ``well_insulated`` room, constant
        ``T_out=0`` degC) and asserts a lenient comfort percentage via
        :func:`~tortoise_ufh.metrics.assert_comfort`. The PI(+trend) loop
        should hold the room inside the comfort band once warmed up.

        Args:
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY["steady_heating"]()
        log, _metrics = run_scenario(scenario)

        assert len(log) > 0, "steady_heating: empty simulation log"
        for room in scenario.building.rooms:
            assert_comfort(
                log.get_room(room.name),
                _room_setpoint(scenario, room.name),
                comfort_band=_COMFORT_BAND_C,
                threshold=_COMFORT_THRESHOLD_PCT,
            )

    def test_no_condensation(
        self,
        run_scenario: RunScenario,
    ) -> None:
        """Floor cooling never risks condensation, per room.

        Runs ``hot_july_floor_cooling`` (cooling mode, 80 % humidity, elevated
        dew point) and asserts the slab stays at least ``margin`` kelvin above
        the Magnus dew point everywhere via
        :func:`~tortoise_ufh.metrics.assert_no_condensation`. This exercises
        the per-room S2 throttle and the building-level safe dew-point limit.

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

    @pytest.mark.parametrize("scenario_name", FAST_SCENARIOS)
    def test_no_freezing(
        self,
        scenario_name: str,
        run_scenario: RunScenario,
    ) -> None:
        """No heating room ever drops below the hard freeze floor, per room.

        The no-freezing floor is a heating-only invariant, so cooling-mode
        scenarios are skipped. ``sensor_dropout`` is the deliberate control
        case: its 2 K measurement noise drives the *reported* room temperature
        below :data:`_FREEZE_HARD_MIN_C` (the physical slab never freezes), so
        the assertion is expected to trip and is wrapped in ``pytest.raises``.

        Args:
            scenario_name: FAST-tier scenario key.
            run_scenario: Session-scoped simulation harness fixture.
        """
        scenario = SCENARIO_LIBRARY[scenario_name]()
        if scenario.mode == Mode.COOLING:
            pytest.skip(f"{scenario_name}: no-freezing is a heating-only check")

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
