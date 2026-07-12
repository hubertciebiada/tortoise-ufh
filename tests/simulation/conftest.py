"""Simulation-tier pytest fixtures for the tortoise-ufh test suite.

Provides the end-to-end ``run_scenario`` harness that closes the control loop
of :class:`~tortoise_ufh.controller.BuildingController` against the pure-Python
digital twin (:class:`~tortoise_ufh.simulator.BuildingSimulator`), records every
timestep into a :class:`~tortoise_ufh.simulation_log.SimulationLog`, and grades
the run with :class:`~tortoise_ufh.metrics.SimMetrics`.

The harness deliberately drives the controller through the **same**
:class:`~tortoise_ufh.models.RoomInputs` snapshot the Home Assistant coordinator
would build (``BuildingSimulator.get_all_measurements``), so a scenario exercises
exactly the black-box contract used in production.

Seeding: this tier seeds sensor noise with ``12345`` (distinct from the unit
tier's ``42``) so seed-dependent bugs surface. These fixtures import ONLY from
the pure core package ``tortoise_ufh`` and never from ``homeassistant``.

Units (repo-wide):
    Temperatures / setpoints: degC; power: W; energy: kWh; valve: 0-100 %;
    humidity: 0-100 %; time: minutes (simulation clock) / seconds (RC model dt
    and the controller cycle).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

import pytest

from custom_components.tortoise_ufh.core.controller import BuildingController
from custom_components.tortoise_ufh.core.metrics import SimMetrics
from custom_components.tortoise_ufh.core.models import Mode
from custom_components.tortoise_ufh.core.rc_model import ModelOrder, RCModel
from custom_components.tortoise_ufh.core.sensor_noise import SensorNoise
from custom_components.tortoise_ufh.core.simulation_log import SimulationLog
from custom_components.tortoise_ufh.core.simulator import (
    BuildingSimulator,
    HeatPumpMode,
    SimulatedRoom,
)
from custom_components.tortoise_ufh.core.ufh_loop import LoopGeometry, loop_power

if TYPE_CHECKING:
    from custom_components.tortoise_ufh.core.config import RoomConfig, SimScenario
    from custom_components.tortoise_ufh.core.models import RoomInputs

# ---------------------------------------------------------------------------
# Harness constants
# ---------------------------------------------------------------------------

_SIM_NOISE_SEED: int = 12345
"""Seed for the simulation tier's sensor noise (distinct from unit tier's 42)."""

_NOMINAL_SUPPLY_C: float = 35.0
"""Representative heating supply temperature for the energy nominal [degC]."""

_NOMINAL_SLAB_C: float = 20.0
"""Representative slab temperature for the energy nominal [degC]."""

_SENSOR_DROPOUT_SCENARIO: str = "sensor_dropout"
"""Scenario whose harness masks the room-temperature reading to ``None``."""

_DROPOUT_WINDOW: tuple[int, int] = (600, 660)
"""``[start, end)`` simulation-minute window over which the ``sensor_dropout``
scenario masks every room's ``room_temperature_c`` to ``None``, so the closed
loop actually drives the controller safe-degrade branch (hold last valve, fast
source OFF, ``"sensor_lost"`` flag) rather than merely adding heavy noise."""

# The twin has no TRANSITIONAL heat-pump state (valves parked, no floor power),
# so it maps to OFF: no HP floor heat is injected, matching a parked-valve
# shoulder season. HEATING/COOLING/OFF map straight through.
_MODE_TO_HP_MODE: dict[Mode, HeatPumpMode] = {
    Mode.HEATING: HeatPumpMode.HEATING,
    Mode.COOLING: HeatPumpMode.COOLING,
    Mode.TRANSITIONAL: HeatPumpMode.OFF,
    Mode.OFF: HeatPumpMode.OFF,
}


class _ScenarioRunner(Protocol):
    """Callable protocol for the ``run_scenario`` harness."""

    def __call__(
        self,
        scenario: SimScenario,
        max_steps: int | None = None,
    ) -> tuple[SimulationLog, SimMetrics]:
        """Run a scenario and return its log and first-room metrics."""
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nominal_ufh_power_w(geometry: LoopGeometry) -> float:
    """Full-valve heating power of one room's UFH loop group [W].

    Evaluates the EN 1264 :func:`~tortoise_ufh.ufh_loop.loop_power` at a
    representative heating supply/slab pair. Used as the nominal power that
    :meth:`SimMetrics.from_log` integrates against for ``energy_kwh``.

    Args:
        geometry: The room's UFH loop geometry.

    Returns:
        Nominal heating power [W] (>= 0).
    """
    return abs(loop_power(_NOMINAL_SUPPLY_C, _NOMINAL_SLAB_C, geometry, "heating"))


def _mask_sensor_dropout(
    scenario: SimScenario,
    t: int,
    all_inputs: dict[str, RoomInputs],
) -> dict[str, RoomInputs]:
    """Mask room-temperature readings to ``None`` during a sensor dropout.

    For the :data:`_SENSOR_DROPOUT_SCENARIO` scenario, replaces every room's
    ``room_temperature_c`` with ``None`` while *t* is inside
    :data:`_DROPOUT_WINDOW`, so the closed loop genuinely exercises the
    controller's safe-degrade path (hold last valve, drive the fast source OFF,
    raise ``"sensor_lost"``). Any other scenario, or any tick outside the
    window, returns the measurements unchanged.

    Args:
        scenario: The running scenario.
        t: Current simulation minute.
        all_inputs: Per-room black-box inputs from ``get_all_measurements``.

    Returns:
        The (possibly masked) per-room inputs mapping.
    """
    if scenario.name != _SENSOR_DROPOUT_SCENARIO:
        return all_inputs
    start, end = _DROPOUT_WINDOW
    if not start <= t < end:
        return all_inputs
    return {
        name: replace(inputs, room_temperature_c=None)
        for name, inputs in all_inputs.items()
    }


def _build_simulator(scenario: SimScenario) -> BuildingSimulator:
    """Construct the digital twin for *scenario*.

    Builds, per room: an :class:`RCModel` (3R3C, discretized at the scenario's
    ``dt_seconds``), a :class:`LoopGeometry` via
    :meth:`LoopGeometry.from_room_config`, and a :class:`SimulatedRoom`. Wraps
    them in a :class:`BuildingSimulator` fed the scenario's weather, heat-pump
    mode, capacity, and (optionally) seeded sensor noise. Per-room setpoints are
    seeded from ``home_setpoint_c`` plus each room's offset.

    Args:
        scenario: The scenario to instantiate.

    Returns:
        A ready-to-step :class:`BuildingSimulator`.
    """
    building = scenario.building
    rooms: list[SimulatedRoom] = []
    for room_cfg in building.rooms:
        model = RCModel(room_cfg.params, ModelOrder.THREE, dt=scenario.dt_seconds)
        geometry = LoopGeometry.from_room_config(room_cfg)
        rooms.append(
            SimulatedRoom(
                room_cfg.name,
                model,
                n_loops=room_cfg.n_loops,
                fast_source_power_w=room_cfg.fast_source_power_w,
                fast_source_kind=room_cfg.fast_source_kind,
                cooling_enabled=room_cfg.cooling_enabled,
                windows=room_cfg.windows,
                initial_temperature_c=scenario.initial_temperature_c,
                loop_geometry=geometry,
            )
        )

    sensor_noise = (
        SensorNoise(scenario.sensor_noise_std, seed=_SIM_NOISE_SEED)
        if scenario.sensor_noise_std > 0.0
        else None
    )

    simulator = BuildingSimulator(
        rooms,
        scenario.weather,
        hp_mode=_MODE_TO_HP_MODE[scenario.mode],
        hp_max_power_w=building.hp_max_power_w,
        sensor_noise=sensor_noise,
        weather_comp=scenario.weather_comp,
        cooling_comp=scenario.cooling_comp,
    )
    simulator.set_setpoints(
        {
            room_cfg.name: building.home_setpoint_c
            + scenario.room_offsets.get(room_cfg.name, 0.0)
            for room_cfg in building.rooms
        }
    )
    return simulator


def _first_room_setpoint(scenario: SimScenario, room_cfg: RoomConfig) -> float:
    """Return the effective setpoint of *room_cfg* [degC].

    Args:
        scenario: The running scenario.
        room_cfg: The room whose setpoint is wanted.

    Returns:
        ``home_setpoint_c`` plus the room's offset [degC].
    """
    return scenario.building.home_setpoint_c + scenario.room_offsets.get(
        room_cfg.name, 0.0
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def run_scenario() -> _ScenarioRunner:
    """Session-scoped closed-loop scenario harness (cached per scenario).

    Returns a callable ``run(scenario, max_steps=None)`` that builds the digital
    twin and the :class:`BuildingController`, closes the control loop for the
    scenario's duration (one tick per ``dt_seconds``), records every timestep,
    and returns ``(SimulationLog, SimMetrics)`` where the metrics are computed
    for the **first** room. Runs are deterministic, so results are cached by
    ``(scenario.name, dt_seconds, max_steps)`` — several tests grading the same
    scenario share one closed-loop run.

    Each tick performs ``get_all_measurements -> BuildingController.step ->
    set_cooling_supply_floor -> step_all``: the controller's global safe
    dew-point output is fed back as the twin heat pump's chilled-supply lower
    limit (I1, 2026-07-09), closing the contract's third output in the loop.
    The per-room ALLOCATED floor power is recorded into each
    :class:`~tortoise_ufh.simulation_log.SimRecord` for the energy metric.

    Returns:
        A :class:`_ScenarioRunner` callable.
    """
    cache: dict[tuple[str, float, int | None], tuple[SimulationLog, SimMetrics]] = {}

    def run(
        scenario: SimScenario,
        max_steps: int | None = None,
    ) -> tuple[SimulationLog, SimMetrics]:
        """Execute the closed-loop simulation for *scenario*.

        Args:
            scenario: The fully configured scenario to run.
            max_steps: Optional cap on the number of control ticks. ``None``
                runs the scenario's full ``duration_minutes``.

        Returns:
            A tuple ``(log, metrics)`` — the full multi-room simulation log and
            the first room's :class:`SimMetrics`.
        """
        key = (scenario.name, scenario.dt_seconds, max_steps)
        cached = cache.get(key)
        if cached is not None:
            return cached

        simulator = _build_simulator(scenario)
        configs = {
            room_cfg.name: room_cfg.controller for room_cfg in scenario.building.rooms
        }
        controller = BuildingController(configs)

        dt_ctrl = float(scenario.dt_seconds)
        dt_minutes = dt_ctrl / 60.0
        n_steps = int(round(scenario.duration_minutes / dt_minutes))
        if max_steps is not None:
            n_steps = min(n_steps, max_steps)

        log = SimulationLog()
        # Home-setpoint schedule (K1, 2026-07-12): entries are applied to the
        # twin the first tick at/after their minute, exactly like the owner's
        # HA automation would nudge the home-temperature number entity.
        schedule = list(scenario.setpoint_schedule)
        schedule_idx = 0
        for i in range(n_steps):
            t_min = i * dt_minutes
            t = int(round(t_min))
            while schedule_idx < len(schedule) and t_min >= schedule[schedule_idx][0]:
                home_c = schedule[schedule_idx][1]
                simulator.set_setpoints(
                    {
                        room_cfg.name: home_c
                        + scenario.room_offsets.get(room_cfg.name, 0.0)
                        for room_cfg in scenario.building.rooms
                    }
                )
                schedule_idx += 1
            all_inputs = _mask_sensor_dropout(
                scenario, t, simulator.get_all_measurements()
            )
            weather_point = scenario.weather.get(t_min)
            slabs = {name: room.T_slab for name, room in simulator.rooms.items()}

            outputs = controller.step(all_inputs, dt_seconds=dt_ctrl)
            # I1: the twin heat pump honours the controller's global safe
            # dew point as its chilled-supply lower limit.
            simulator.set_cooling_supply_floor(outputs.global_safe_dew_point_c)
            simulator.step_all(outputs.rooms)
            allocated = simulator.last_step_info["q_floor_w"]
            assert isinstance(allocated, dict)

            for room_cfg in scenario.building.rooms:
                name = room_cfg.name
                log.append_from_step(
                    t=t,
                    inputs=all_inputs[name],
                    outputs=outputs.rooms[name],
                    weather=weather_point,
                    t_slab=slabs[name],
                    room_name=name,
                    q_floor_w=allocated.get(name),
                )

        first_room = scenario.building.rooms[0]
        metrics = SimMetrics.from_log(
            log.get_room(first_room.name),
            setpoint=_first_room_setpoint(scenario, first_room),
            ufh_nominal_power_w=_nominal_ufh_power_w(
                LoopGeometry.from_room_config(first_room)
            ),
            dt_minutes=max(1, int(round(dt_minutes))),
        )
        result = (log, metrics)
        cache[key] = result
        return result

    return run
