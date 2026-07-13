"""Digital-twin reproduction of the cooling boost-hold fix (2026-07-13, P2).

The owner's complaint: while the split boosts a cooling room, the floor valve
collapses to 0 (the split-cooled air drives the room error toward zero, so the
PI and the trend damper retreat the floor) — the slab stops discharging and the
split short-cycles off the warm mass. The fix (``core/controller.py`` step
11b): while the split is ENGAGED in cooling, hold the floor valve at (a ``max``
floor of) the position it held the cycle the split engaged.

These tests run the SAME high-mass cooling twin twice on a hot day: once with
the boost-hold neutralised (the pre-fix baseline, reproduced by forcing the
per-cycle hold snapshot to 0) and once with the fix live, and assert the fix
(1) keeps the floor materially open during boost instead of starving it to 0,
(2) does not increase — and here strictly reduces — the split cycle count, and
(3) never weakens the condensation defences under high humidity.

Only #1 (the ``max`` floor) is implemented; the plan's #2/#3 were dropped as
empirically unnecessary (see docs/DECISIONS.md §18), so there are no
freeze/trend-suppression assertions here.

Units: temperatures degC, valve percent 0..100, time minutes (simulation) /
seconds (controller ``dt``). This module never imports ``homeassistant``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from custom_components.tortoise_ufh.core.building_profiles import (
    well_insulated_with_split,
)
from custom_components.tortoise_ufh.core.controller import BuildingController
from custom_components.tortoise_ufh.core.dew_point import dew_point
from custom_components.tortoise_ufh.core.models import FastSourceMode
from custom_components.tortoise_ufh.core.rc_model import ModelOrder, RCModel
from custom_components.tortoise_ufh.core.simulator import (
    BuildingSimulator,
    HeatPumpMode,
    SimulatedRoom,
)
from custom_components.tortoise_ufh.core.ufh_loop import LoopGeometry
from custom_components.tortoise_ufh.core.weather import SyntheticWeather

_DT_S = 300.0
"""Production control takt [s]."""

_STEPS = 288
"""24 h at 300 s — the reproduction window."""

_SLAB_START_C = 27.0
"""Initial (hot) temperature of every thermal node [degC]."""


@dataclass(frozen=True)
class _Result:
    """Aggregate metrics of one closed-loop reproduction run."""

    split_cycles: int
    engaged_valve_mean: float
    engaged_valve_min: float
    engaged_dew_factor_min: float
    slab_end_c: float
    worst_slab_minus_dew_c: float


def _build(humidity_pct: float) -> tuple[BuildingSimulator, BuildingController, str]:
    """High-mass, well-insulated single room in LIVE cooling on a hot day.

    A 1.6x reference-slab room (heavy screed) starting hot at 27 degC with a
    24 degC setpoint, T_out 34 degC and strong solar gain — the floor alone
    cannot hold the room, so the 2.5 kW split repeatedly boosts and releases.
    The seasonal ground is warm (23 degC) so the slab discharges through the
    FLOOR, not into the ground, making the floor-during-boost the load-bearing
    discharge path.
    """
    cfg = well_insulated_with_split(t_ground=23.0).rooms[0]
    params = replace(cfg.params, C_slab=cfg.params.C_slab * 1.6)
    model = RCModel(params, ModelOrder.THREE, dt=_DT_S)
    room = SimulatedRoom(
        cfg.name,
        model,
        n_loops=cfg.n_loops,
        fast_source_power_w=cfg.fast_source_power_w,
        fast_source_kind=cfg.fast_source_kind,
        cooling_enabled=True,
        windows=cfg.windows,
        initial_temperature_c=_SLAB_START_C,
        loop_geometry=LoopGeometry.from_room_config(cfg),
    )
    weather = SyntheticWeather.constant(T_out=34.0, GHI=600.0, humidity=humidity_pct)
    simulator = BuildingSimulator(
        [room], weather, hp_mode=HeatPumpMode.COOLING, hp_max_power_w=6000.0
    )
    simulator.set_setpoint(cfg.name, 24.0)
    controller = BuildingController({cfg.name: cfg.controller})
    return simulator, controller, cfg.name


def _run(*, humidity_pct: float, neutralise_hold: bool) -> _Result:
    """Run the closed loop and aggregate the boost-hold reproduction metrics.

    When ``neutralise_hold`` is set, the per-cycle hold snapshot is forced to 0
    before every control step, reproducing the pre-fix behaviour exactly (the
    ``max(valve, 0)`` floor never lifts the valve) — the in-test A/B baseline.
    """
    simulator, controller, name = _build(humidity_pct)
    rc = controller._controllers[name]  # noqa: SLF001
    prev_on = False
    cycles = 0
    engaged_valves: list[float] = []
    engaged_dews: list[float] = []
    slab_last = _SLAB_START_C
    worst_margin = float("inf")
    for _ in range(_STEPS):
        inputs = simulator.get_all_measurements()
        if neutralise_hold:
            rc._last_raw_valve_pct = 0.0  # noqa: SLF001
            rc._boost_hold_pct = None  # noqa: SLF001
        outputs = controller.step(inputs, dt_seconds=_DT_S)
        simulator.set_cooling_supply_floor(outputs.global_safe_dew_point_c)
        out = outputs.rooms[name]
        on = out.fast_source.on and out.fast_source.mode is FastSourceMode.COOLING
        if on and not prev_on:
            cycles += 1
        # "Engaged" == the split ran at STEP ENTRY (the controller's own hold
        # witness), so read the metric on the cycle AFTER the engage edge.
        if prev_on:
            engaged_valves.append(out.valve_position_pct)
            engaged_dews.append(out.report.dew_throttle_factor)
        prev_on = on
        simulator.step_all(outputs.rooms)
        slab_last = simulator.rooms[name].T_slab
        meas = simulator.get_all_measurements()[name]
        if meas.humidity_pct:
            air_c = simulator.rooms[name].T_air
            margin = slab_last - dew_point(air_c, meas.humidity_pct)
            worst_margin = min(worst_margin, margin)
    n = len(engaged_valves)
    return _Result(
        split_cycles=cycles,
        engaged_valve_mean=sum(engaged_valves) / n if n else 0.0,
        engaged_valve_min=min(engaged_valves) if engaged_valves else 0.0,
        engaged_dew_factor_min=min(engaged_dews) if engaged_dews else 1.0,
        slab_end_c=slab_last,
        worst_slab_minus_dew_c=worst_margin,
    )


@pytest.mark.simulation
class TestBoostHoldReproduction:
    """S1: the boost-hold keeps the floor open and cuts split short-cycling."""

    def test_floor_held_and_fewer_cycles_than_baseline(self) -> None:
        """Fix vs neutralised baseline on the isolated (dry) reproduction.

        Empirical numbers on this deterministic twin (no RNG), captured on the
        first run and used to pin the thresholds:

            baseline (hold off): 24 split cycles, engaged-valve mean 0.0 %
            fixed  (#1 hold on): 22 split cycles, engaged-valve mean 35.9 %,
                                 engaged-valve min 20.7 %, dew factor 1.0

        The humidity is kept low so the S2 dew throttle stays fully open and
        the boost-hold is what moves the floor (throttle interaction is covered
        by the ``TestBoostHoldDewNeutral`` case and the unit suite).
        """
        base = _run(humidity_pct=20.0, neutralise_hold=True)
        fix = _run(humidity_pct=20.0, neutralise_hold=False)

        # The dry scenario really does keep the throttle open both ways.
        assert base.engaged_dew_factor_min == pytest.approx(1.0)
        assert fix.engaged_dew_factor_min == pytest.approx(1.0)

        # (1) Baseline starves the floor to 0 during every boost; the fix holds
        #     it materially open — the load-bearing behaviour change.
        assert base.engaged_valve_mean < 1.0, (
            "pre-fix baseline should collapse the floor to 0 during boost"
        )
        assert fix.engaged_valve_mean >= 25.0, (
            "the boost-hold must keep the floor materially open during boost"
        )
        assert fix.engaged_valve_min > 5.0, (
            "the floor must never be starved to (near) 0 while the split boosts"
        )

        # (2) The fix never increases the split cycle count, and here strictly
        #     reduces it (22 < 24) — the slab discharges through the held floor
        #     instead of rebounding the air off a warm mass.
        assert fix.split_cycles < base.split_cycles

        # (3) Cooling actually flows: the hot slab discharges over the window.
        assert fix.slab_end_c < _SLAB_START_C - 2.0


@pytest.mark.simulation
class TestBoostHoldDewNeutral:
    """S2: under high humidity the hold never weakens the dew defences."""

    def test_high_humidity_hold_is_condensation_neutral(self) -> None:
        """Holding the floor open must not push the slab below the dew margin.

        The global safe dew-point floor keeps the supply water at ``dew + 2 K``
        regardless of valve position, so the extra flow the hold admits cannot
        drive the slab below where the baseline already sits: the worst
        slab-vs-room-dew margin is dew-neutral (identical to 0.05 K here). The
        S2 throttle scaling the HELD valve toward 0 as the supply nears the dew
        point is exercised precisely in the unit suite (T4).
        """
        base = _run(humidity_pct=78.0, neutralise_hold=True)
        fix = _run(humidity_pct=78.0, neutralise_hold=False)

        # The hold still opens the floor under high humidity.
        assert base.engaged_valve_mean < 1.0
        assert fix.engaged_valve_mean >= 25.0

        # Condensation-neutral: the fix's worst margin is no worse than the
        # baseline's (they coincide because the pump dew floor, untouched by the
        # hold, drives the supply — the hold only ever raises FLOW, never lowers
        # the supply temperature).
        assert fix.worst_slab_minus_dew_c >= base.worst_slab_minus_dew_c - 0.05
