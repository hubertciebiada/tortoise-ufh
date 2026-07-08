"""Smoke test for the tortoise-ufh simulation suite.

Guarantees that ``pytest tests/simulation/ -m simulation`` collects at least
one test, so the suite returns exit code 0 (success) rather than exit code 5
(no tests collected). It also exercises the end-to-end ``run_scenario`` harness
on the ``steady_heating`` scenario for a handful of steps, giving the simulation
plumbing a minimal but real integration check before the full scenario tests
land.

Units:
    * Temperatures / setpoints: degrees Celsius.
    * ``comfort_pct``: percent in ``[0, 100]``.
    * Simulation step count: dimensionless (a "handful" of 1-minute ticks).
"""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from tortoise_ufh.config import SimScenario
from tortoise_ufh.metrics import SimMetrics
from tortoise_ufh.scenarios import steady_heating
from tortoise_ufh.simulation_log import SimulationLog

# A "handful" of 1-minute simulation ticks: enough to produce records without
# paying for the scenario's full 48 h duration.
_SMOKE_STEPS: int = 5


@pytest.mark.simulation
def test_steady_heating_smoke(
    run_scenario: Callable[[SimScenario, int | None], tuple[SimulationLog, SimMetrics]],
) -> None:
    """Run ``steady_heating`` a few steps and sanity-check the outputs.

    Asserts that the harness produced a non-empty log and that the resulting
    ``comfort_pct`` is a finite number within the valid ``[0, 100]`` range.

    Args:
        run_scenario: Session-scoped harness fixture that runs a
            :class:`~tortoise_ufh.config.SimScenario` and returns the
            ``(SimulationLog, SimMetrics)`` for the first room.
    """
    scenario = steady_heating()

    log, metrics = run_scenario(scenario, _SMOKE_STEPS)

    assert len(log) > 0

    comfort_pct = metrics.comfort_pct
    assert math.isfinite(comfort_pct)
    assert 0.0 <= comfort_pct <= 100.0
