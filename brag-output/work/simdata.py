"""Run a library scenario on the digital twin with the real BuildingController
and dump the time series the video charts (no invented numbers).

Run from the repo root with Python 3.12 + numpy/scipy:
    PYTHONPATH=. python brag-output/work/simdata.py steady_heating
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from custom_components.tortoise_ufh.core.controller import BuildingController
from custom_components.tortoise_ufh.core.models import Mode
from custom_components.tortoise_ufh.core.rc_model import ModelOrder, RCModel
from custom_components.tortoise_ufh.core.scenarios import SCENARIO_LIBRARY
from custom_components.tortoise_ufh.core.simulator import (
    BuildingSimulator,
    HeatPumpMode,
    SimulatedRoom,
)
from custom_components.tortoise_ufh.core.ufh_loop import LoopGeometry

HERE = Path(__file__).resolve().parent

MODE = {
    Mode.HEATING: HeatPumpMode.HEATING,
    Mode.COOLING: HeatPumpMode.COOLING,
    Mode.TRANSITIONAL: HeatPumpMode.OFF,
    Mode.OFF: HeatPumpMode.OFF,
}


def build(sc):
    rooms = []
    for rc in sc.building.rooms:
        rooms.append(
            SimulatedRoom(
                rc.name,
                RCModel(rc.params, ModelOrder.THREE, dt=sc.dt_seconds),
                n_loops=rc.n_loops,
                fast_source_power_w=rc.fast_source_power_w,
                fast_source_kind=rc.fast_source_kind,
                cooling_enabled=rc.cooling_enabled,
                windows=rc.windows,
                initial_temperature_c=sc.initial_temperature_c,
                loop_geometry=LoopGeometry.from_room_config(rc),
            )
        )
    sim = BuildingSimulator(
        rooms,
        sc.weather,
        hp_mode=MODE[sc.mode],
        hp_max_power_w=sc.building.hp_max_power_w,
        weather_comp=sc.weather_comp,
        cooling_comp=sc.cooling_comp,
    )
    sim.set_setpoints(
        {
            rc.name: sc.building.home_setpoint_c + sc.room_offsets.get(rc.name, 0.0)
            for rc in sc.building.rooms
        }
    )
    return sim


def run(name, every=10):
    sc = SCENARIO_LIBRARY[name]()
    sim = build(sc)
    ctrl = BuildingController({rc.name: rc.controller for rc in sc.building.rooms})
    dt = float(sc.dt_seconds)
    n = int(round(sc.duration_minutes / (dt / 60.0)))
    sched = list(sc.setpoint_schedule)
    si = 0
    out = {rc.name: [] for rc in sc.building.rooms}
    for i in range(n):
        t_min = i * dt / 60.0
        while si < len(sched) and t_min >= sched[si][0]:
            sim.set_setpoints(
                {
                    rc.name: sched[si][1] + sc.room_offsets.get(rc.name, 0.0)
                    for rc in sc.building.rooms
                }
            )
            si += 1
        inputs = sim.get_all_measurements()
        outputs = ctrl.step(inputs, dt_seconds=dt)
        sim.set_cooling_supply_floor(outputs.global_safe_dew_point_c)
        if i % every == 0:
            for rn, room in sim.rooms.items():
                o = outputs.rooms[rn]
                out[rn].append(
                    {
                        "t_h": round(t_min / 60.0, 4),
                        "t_air": round(room.T_air, 3),
                        "t_slab": round(room.T_slab, 3),
                        "setpoint": inputs[rn].setpoint_c,
                        "valve": round(o.valve_position_pct, 2),
                        "fast_on": bool(o.fast_source.on),
                    }
                )
        sim.step_all(outputs.rooms)
    return {"scenario": name, "description": sc.description, "rooms": out}


if __name__ == "__main__":
    names = sys.argv[1:] or ["steady_heating"]
    res = {n: run(n) for n in names}
    with open(HERE / "simdata.json", "w") as fh:
        json.dump(res, fh, separators=(",", ":"))
    for n, r in res.items():
        for rn, rows in r["rooms"].items():
            sp = rows[-1]["setpoint"]
            peak = max(x["t_air"] for x in rows)
            on = [x["t_h"] for x in rows if x["fast_on"]]
            print(
                n,
                rn,
                "start",
                rows[0]["t_air"],
                "sp",
                sp,
                "peak",
                round(peak, 3),
                "overshoot",
                round(peak - sp, 3),
                "split on h",
                (on[0], on[-1]) if on else None,
                "valve@0",
                rows[0]["valve"],
                "valve max",
                max(x["valve"] for x in rows),
            )
