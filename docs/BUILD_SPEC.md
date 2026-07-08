# tortoise-ufh — BUILD SPEC (authoritative, frozen)

> **This is the single source of truth for every agent building this repo.** Read it fully before
> writing any file. It freezes: layout, the two-layer rule, the units contract, the exact public
> API of the core "brain", the config dataclasses, the simulator interface, the Home Assistant
> adapter contract, and the panel contract. If something here conflicts with your intuition, follow
> this doc. If something is genuinely underspecified, follow the reference blueprint
> `scratchpad/pump-ahead-blueprint.md` (path given to you) and the PRD `prd-control-brain.md` (esp.
> the **Aneks §8** — our locked decisions), and prefer the *simplest* choice consistent with both.

Companion docs (read these too): `prd-control-brain.md` (§8 Aneks = decisions), `CONTROL_ALGORITHMS_REVIEW.md`
(algorithm reference), `scratchpad/pump-ahead-blueprint.md` (structural template).

---

## 0. What we are building

A Home Assistant custom integration **`tortoise_ufh`**, HACS-installable, that runs an independent
**per-room closed-loop climate controller** for a high-thermal-mass underfloor-heating (UFH) house,
with fast-source (split/AC) assist, **heating and floor cooling**. Structurally a sibling of
`pump-ahead` (same repo shape, packaging, simulator design, conventions) but the controller is
**PID-family (PI + trend), not MPC**. No Kalman, no online RC identification, no MPC, no CWU control.

### The three outputs (the external contract)
1. **Per room:** valve position `0..100 %` (one value per room/zone; all its loops get it).
2. **Per room:** fast-source command = `ON + direction(mode) + room target temp` (the split
   self-regulates; we never touch compressor power).
3. **Global:** a **safe dew-point value** = `max_over_cooled_rooms(T_dew_room) + 2 K` (°C) exposed as
   a sensor entity; the owner feeds it to the heat pump as the cooling-supply lower limit.

Plus a rich **per-room report** ("under the hood"): error, trend, decision components, flags, and a
short human+AI-readable text explanation.

---

## 1. The one rule that shapes everything

**`tortoise_ufh/` (the core library) MUST NEVER import `homeassistant`.** It is pure Python
(numpy/scipy + stdlib), ships `py.typed`, and is fully unit- and simulation-testable offline.
`custom_components/tortoise_ufh/` (the HA adapter) imports FROM the core and never the reverse.
Core talks to the outside only through plain dataclasses and structural `Protocol`s
(`WeatherSource`). Any core file that does `import homeassistant` is a bug and will be rejected.

---

## 2. Repo layout (exact)

```
tortoise-ufh/
  pyproject.toml                     # setuptools, py>=3.12, ruff/mypy/pytest config
  hacs.json                          # {name, homeassistant, render_readme}
  LICENSE                            # MIT (keep pyproject + LICENSE + README consistent)
  README.md                          # HACS landing page (install + usage)
  CLAUDE.md                          # project axioms + architecture + conventions
  .gitignore
  .pre-commit-config.yaml
  .github/workflows/ci.yml           # ruff, mypy(core), unit, simulation
  .github/workflows/validate.yml     # hassfest + HACS validation
  docs/BUILD_SPEC.md                 # this file
  docs/DECISIONS.md                  # the 10 Q&A decisions + floor-cooling addendum
  docs/ALGORITHM_SPEC.md             # math + control spec (analogous to PumpAhead_Algorithm_Spec.md)

  tortoise_ufh/                      # PURE CORE (no HA import)
    __init__.py                      # flat re-export hub + __version__
    py.typed
    const.py                         # physical constants + core defaults
    models.py                        # enums + I/O dataclasses (black-box contract) + RoomReport
    config.py                        # RoomConfig, BuildingConfig, ControllerConfig, SimScenario
    rc_model.py                      # RCParams, ModelOrder, RCModel (3R3C ZOH via expm)
    pid.py                           # PIDController (PI + anti-windup)
    controller.py                    # RoomController (black box) + BuildingController (orchestrator)
    dew_point.py                     # Magnus dew point + cooling throttle
    weather_comp.py                  # WeatherCompCurve / CoolingCompCurve (feedforward)
    ufh_loop.py                      # LoopGeometry + loop_power (EN 1264) — used by simulator
    weather.py                       # WeatherPoint, WeatherSource protocol, SyntheticWeather
    sensor_noise.py                  # SensorNoise (seeded Gaussian)
    safety.py                        # safety rules S1..S5 (data) + SafetyEvaluator
    simulator.py                     # BuildingSimulator + SimulatedRoom bridge + HeatPumpMode
    simulation_log.py                # SimRecord + SimulationLog
    metrics.py                       # SimMetrics + assert_* helpers
    scenarios.py                     # scenario factories + SCENARIO_LIBRARY registry
    building_profiles.py             # building factories + BUILDING_PROFILES registry

  custom_components/tortoise_ufh/    # HA ADAPTER (imports core)
    __init__.py                      # setup_entry/unload_entry, runtime_data, panel+static reg
    manifest.json
    const.py                         # CONF_* vocabulary + defaults + PLATFORMS
    coordinator.py                   # DataUpdateCoordinator: read states, run core, write commands
    config_flow.py                   # multi-step wizard + options flow (selectors)
    entity_validator.py              # hardware-agnostic unit validation
    number.py                        # global home-temp + per-room offset (writable)
    sensor.py                        # per-room report/diagnostics + GLOBAL safe dew-point sensor
    binary_sensor.py                 # per-room flags (sensor_lost, saturated, live/shadow, safety)
    switch.py                        # global kill-switch (off = emit no commands)
    websocket.py                     # panel API commands (get/set config, live data)
    panel.py                         # register sidebar panel + static path for the JS module
    services.yaml                    # set_home_temperature, set_room_offset, set_mode
    strings.json
    translations/en.json
    translations/pl.json
    frontend/tortoise-ufh-panel.js   # self-contained vanilla-JS panel (no build step)
    dashboard_tortoise_ufh.yaml      # fallback Lovelace template
    brand/icon.svg

  tests/
    conftest.py                      # shared fixtures (RC params/models, seeded rng)
    unit/conftest.py                 # unit-only fixtures (seed 42)
    unit/test_*.py                   # one per core module
    simulation/conftest.py           # run_scenario harness (seed 12345)
    simulation/test_scenarios.py     # parametrized scenario tests calling assert_* helpers
    simulation/test_sim_smoke.py     # trivial simulation test so suite collects >=1
```

---

## 3. Units & conventions (repo-wide, non-negotiable)

- Temperatures **°C**; power **W**; valve **0..100 % (float)**; R in **K/W**; C in **J/K**; GHI **W/m²**;
  humidity **0..100 %**; time in **minutes** (simulation) / **seconds** (`RCModel.dt`, real-time cycle).
- `from __future__ import annotations` at the top of every module. `mypy --strict` clean on `tortoise_ufh/`.
- Modern generics (`list[str]`, `dict[str, float]`, `X | None`), `Literal[...]` for closed string sets,
  `Protocol` + `@runtime_checkable` for structural interfaces, `type Alias = ...` (PEP 695) where useful.
  numpy typed `NDArray[np.float64]` (`from numpy.typing import NDArray`).
- **Every value/config/result type is `@dataclass(frozen=True)`**; validation in `__post_init__`
  raising `ValueError` (assign message to a local `msg` first: `msg = f"..."; raise ValueError(msg)`).
  Mutable defaults via `field(default_factory=...)`. `kw_only=True` for entity descriptions.
- Google-style docstrings with `Args:`/`Returns:`/`Raises:`; module docstrings state units.
- Pure functions do not mutate inputs; return `.copy()` of arrays. Seeded `np.random.default_rng(seed)`,
  never global `np.random`. Matrix ops with `@`.
- Naming: modules snake_case, classes PascalCase, funcs snake_case, constants UPPER_SNAKE, private
  `_leading`. Units baked into names (`_w`, `_m2`, `_minutes`, `_pct`, `t_supply`, `t_room`).
- Errors: catch specific exceptions in core; broad `except Exception:` only at HA/IO boundaries with
  `# noqa: BLE001` + `_LOGGER.exception(...)`. Fail-fast validation in constructors.
- ruff `line-length=88`, select `["E","F","I","UP","B","SIM"]`. Warnings are errors in tests
  (`filterwarnings=["error"]`).

---

## 4. Core black-box contract — `models.py` (FROZEN — implement exactly)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

class Mode(Enum):
    HEATING = "heating"
    TRANSITIONAL = "transitional"   # valves parked; only fast source, bidirectional
    COOLING = "cooling"
    OFF = "off"

class FastSourceKind(Enum):
    NONE = "none"
    SPLIT = "split"
    HEATER = "heater"               # supported for the future; behaves like heating-only split

class FastSourceMode(Enum):
    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"

@dataclass(frozen=True)
class LoopInput:
    """One UFH loop's raw water probes + valve feedback."""
    valve_position_pct: float | None        # current actuator position (feedback), 0..100
    supply_temperature_c: float | None
    return_temperature_c: float | None

@dataclass(frozen=True)
class RoomInputs:
    """Raw inputs for ONE room's black-box controller. Values may be None (missing sensor)."""
    mode: Mode
    setpoint_c: float                        # already = home_setpoint + room_offset
    room_temperature_c: float | None
    humidity_pct: float | None = None        # required for cooled rooms (dew point)
    outdoor_temperature_c: float | None = None
    loops: tuple[LoopInput, ...] = ()
    fast_source_kind: FastSourceKind = FastSourceKind.NONE
    fast_source_on: bool | None = None       # current state feedback
    hp_active_for_ufh: bool | None = None     # False during DHW/defrost -> freeze integrator
    cooling_enabled: bool = True             # per-room "udział w chłodzeniu"

@dataclass(frozen=True)
class FastSourceCommand:
    on: bool
    mode: FastSourceMode = FastSourceMode.OFF
    target_temperature_c: float | None = None

@dataclass(frozen=True)
class RoomReport:
    """Under-the-hood, human + AI readable."""
    error_c: float | None                    # setpoint - room_temp (heating sign convention)
    trend_c_per_h: float | None              # measured dT_room/dt
    room_dew_point_c: float | None
    p_term: float
    i_term: float
    trend_term: float
    feedforward_term: float
    raw_valve_pct: float                     # before clamps/floors/safety
    valve_floor_applied: bool
    saturated: bool
    dew_throttle_factor: float               # 1.0 = open, 0.0 = fully throttled (cooling)
    integrator_frozen: bool
    flags: tuple[str, ...] = ()              # e.g. "sensor_lost","fast_source_min_runtime","s2_condensation"
    explanation: str = ""                    # short "what & why" text

@dataclass(frozen=True)
class RoomOutputs:
    """The per-room result: the two commands + the report."""
    valve_position_pct: float                # 0..100, final
    fast_source: FastSourceCommand
    report: RoomReport

@dataclass(frozen=True)
class BuildingOutputs:
    rooms: dict[str, RoomOutputs]
    global_safe_dew_point_c: float | None    # max_over_cooled(T_dew)+2K, or None if no cooled/humidity
```

**Report must be JSON-serializable** (provide `to_dict()` helpers on `RoomOutputs`/`RoomReport`/
`BuildingOutputs` returning plain dict/list/str/float/bool; enums -> their `.value`). The HA websocket
and the panel consume these dicts.

---

## 5. Controller spec — `pid.py` + `controller.py` (the heart)

### 5.1 `pid.py` — `PIDController`
```python
class PIDController:
    def __init__(self, kp: float, ki: float, kd: float = 0.0, *, dt: float = 300.0,
                 output_min: float = 0.0, output_max: float = 100.0) -> None: ...
    def compute(self, error: float, *, freeze_integrator: bool = False) -> float: ...
    def reset(self) -> None: ...
    @property
    def integral(self) -> float: ...
    @property
    def last_output(self) -> float: ...
```
Discrete PI(+optional D) with **back-calculation anti-windup**: `P=kp*e`; if not frozen `I += ki*e*dt`;
`D=kd*(e-e_prev)/dt` (0 on first call); `u_raw=P+I+D`; `u=clip(u_raw,min,max)`; then if `ki>0`,
`I += (u - u_raw)` (back-calc). `freeze_integrator=True` skips the `I += ki*e*dt` accumulation (used
when `hp_active_for_ufh is False`). `dt` in seconds (default 300 = 5 min cycle). Validate kp,ki,kd>=0,
dt>0, output_min<output_max.

### 5.2 `controller.py` — `RoomController` (the black box, one per room)
```python
class RoomController:
    def __init__(self, config: ControllerConfig, *, name: str = "") -> None: ...
    def step(self, inputs: RoomInputs, *, dt_seconds: float = 300.0) -> RoomOutputs: ...
    def reset(self) -> None: ...
```
Algorithm (all knobs from `ControllerConfig`, §6.3):

1. **Missing room temp** (`room_temperature_c is None`): SAFE DEGRADE — hold last valve position
   (freeze; `RoomController` remembers `_last_valve_pct`, init to `config.valve_floor_pct` for heating,
   0 for cooling), fast source **OFF**, flag `"sensor_lost"`, explanation says so. Do not run PID.
2. **Mode == OFF**: valve 0, fast source OFF, report explains "off".
3. **Mode == TRANSITIONAL**: valve parked (0), only fast source, bidirectional on sign of error
   (heating if room below setpoint-deadband beyond boost offset, cooling if above). No PID on valve.
4. **Trend**: `trend_c_per_h = (T_room - _prev_T_room)/dt_hours` (0 on first call). Store prev.
5. **Error** (heating convention): `error = setpoint - T_room`. Cooling uses `error = T_room - setpoint`
   internally so a positive error always means "need more actuation".
6. **Deadband**: if `|setpoint - T_room| <= deadband`, PID sees `error=0` region (no integral growth);
   valve trends toward floor (heating) or 0 (cooling). Keep it simple: pass `error` reduced by deadband
   (`error_db = sign(error)*max(0, |err|-deadband)`).
7. **Integrator freeze**: `freeze = (inputs.hp_active_for_ufh is False)`. Pass to `pid.compute`.
8. **PID**: `pid_out = pid.compute(error_db, freeze_integrator=freeze)` -> 0..100.
9. **Trend damping** (anti-overshoot, the "człon trendu"): subtract `kt * trend_toward_setpoint`.
   In heating, if room is rising toward setpoint (`trend>0`) reduce valve by `config.kt * trend_c_per_h`
   (clamped >=0 contribution). In cooling mirror. This is the key inertia/overshoot tamer.
10. **Feedforward** (optional): if `outdoor_temperature_c` present and `config.outdoor_ff_enabled`,
    add a small baseline term from `WeatherCompCurve`-style mapping (heating: colder outside -> higher
    baseline valve). Keep bounded and modest; PI does the rest.
11. **Valve floor** (heating only, when calling for heat i.e. error>0 beyond deadband):
    `valve = max(valve, config.valve_floor_pct)`. Not applied in cooling/off/transitional or when
    satisfied.
12. **Cooling dew-point local throttle (S2, per room)**: only in COOLING and `cooling_enabled`.
    Compute `T_dew = dew_point(T_room, humidity)`. Take coldest loop supply `t_supply_min` (from
    `inputs.loops`). `factor = cooling_throttle_factor(t_supply_min, T_dew, margin=config.dew_margin_k,
    ramp=config.dew_ramp_k)` in [0,1]; `valve *= factor`; if `factor==0` flag `"s2_condensation"`.
    (When humidity or supply missing -> be conservative: factor toward 0, flag.)
13. **Clamp** valve to [0,100]; set `saturated` if hit a bound; `raw_valve_pct` = pre-clamp/floor value.
14. **Fast source (split) coordination** (only if `fast_source_kind != NONE`):
    - Engage when `|setpoint - T_room| > config.boost_offset_c` in the mode's needed direction.
    - Respect **min ON / min OFF** (`config.fast_min_on_minutes`, `config.fast_min_off_minutes`); track
      elapsed via internal timers advanced by `dt_seconds`; flag `"fast_source_min_runtime"` when a
      change is blocked by the timer.
    - **Anti priority-inversion:** the split decision NEVER reduces/holds the valve; floor stays base.
      Split only *adds* boost above the threshold and releases once inside the comfort band.
    - Command: `on=True, mode=HEATING|COOLING (per Mode), target = setpoint`. Else `on=False, mode=OFF`.
15. Build `RoomReport` with every term filled and a concise Polish/English-neutral `explanation`
    (e.g. `"Grzanie, błąd -0.4 K, trend +0.3 K/h. Zawór 34%. Split ON (boost)."`).

`RoomController` holds internal state: `_pid`, `_prev_T_room`, `_last_valve_pct`, split timers. `reset()`
clears them.

### 5.3 `controller.py` — `BuildingController` (orchestrator)
```python
class BuildingController:
    def __init__(self, configs: dict[str, ControllerConfig]) -> None: ...
    def step(self, inputs: dict[str, RoomInputs], *, dt_seconds: float = 300.0) -> BuildingOutputs: ...
    def reset(self) -> None: ...
```
- One `RoomController` per room. Runs each `step`.
- **Global safe dew point:** over rooms where `mode==COOLING and cooling_enabled and humidity present`,
  compute `T_dew_i = dew_point(T_room_i, rh_i)`, take `max_i`, add `config dew margin (2K)` ->
  `global_safe_dew_point_c`. `None` if no eligible room.
- Never raises on a single room's failure; a room that errors becomes a safe-degraded `RoomOutputs`
  with a flag.

---

## 6. `config.py` (FROZEN shapes — implement with full `__post_init__` validation)

```python
@dataclass(frozen=True)
class ControllerConfig:
    kp: float = 8.0
    ki: float = 0.02
    kd: float = 0.0
    kt: float = 6.0                      # trend-damping gain (%/(°C/h))
    deadband_c: float = 0.3
    valve_floor_pct: float = 15.0
    outdoor_ff_enabled: bool = False
    boost_offset_c: float = 1.0          # split engages beyond this |error|
    fast_min_on_minutes: float = 10.0
    fast_min_off_minutes: float = 10.0
    dew_margin_k: float = 2.0            # local S2 margin
    dew_ramp_k: float = 2.0             # graduated throttle ramp width
    cycle_seconds: float = 300.0        # 5 min
    valve_write_threshold_pct: float = 2.0
    # __post_init__: all gains >=0, 0<=valve_floor<=100, deadband>=0, margins>=0, cycle>0

@dataclass(frozen=True)
class RoomConfig:
    name: str
    area_m2: float
    params: RCParams                     # for the simulator only
    n_loops: int = 1
    has_fast_source: bool = False
    fast_source_kind: FastSourceKind = FastSourceKind.NONE
    fast_source_power_w: float = 0.0
    cooling_enabled: bool = True
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    windows: tuple[WindowConfig, ...] = ()
    loop_geometry: LoopGeometry | None = None   # for simulator power
    # validation: area>0, n_loops>=1, kind consistency with has_fast_source

@dataclass(frozen=True)
class BuildingConfig:
    rooms: tuple[RoomConfig, ...]
    hp_max_power_w: float
    latitude: float
    longitude: float
    home_setpoint_c: float = 21.0
    # validation: >=1 room, unique names, hp power>0, lat/lon ranges

@dataclass(frozen=True)
class SimScenario:
    name: str
    building: BuildingConfig
    weather: WeatherSource
    duration_minutes: int
    mode: Mode = Mode.HEATING
    dt_seconds: float = 60.0
    sensor_noise_std: float = 0.0
    description: str = ""
    room_offsets: dict[str, float] = field(default_factory=dict)   # per-room offset from home setpoint
```
`RCParams`, `ModelOrder`, `RCModel` are **identical in spirit to pump-ahead** (blueprint §3 model.py):
3R3C `x=[T_air,T_slab,T_wall]`, SISO `u=[Q_floor]`, `d=[T_out,Q_sol,Q_int]`, ZOH via augmented matrix
`scipy.linalg.expm`. `WindowConfig`, `LoopGeometry`, `loop_power` (EN 1264) identical to blueprint.
`dew_point`, `cooling_throttle_factor` identical to blueprint dew_point.py. `WeatherPoint`,
`WeatherSource` protocol, `SyntheticWeather`, `ChannelProfile`, `ProfileKind`, `SensorNoise` identical
to blueprint. Reuse those shapes verbatim; do not reinvent.

---

## 7. Simulator — `simulator.py`, `simulated_room.py` (digital twin)

Mirror blueprint §4 closely, adapted to our contract:
- `class HeatPumpMode(Enum): HEATING; COOLING; OFF`.
- `SimulatedRoom(name, model: RCModel, *, n_loops=1, fast_source_power_w=0.0, q_int_w=0.0,
  loop_geometry: LoopGeometry)` — owns thermal state `_x=model.reset()`; `apply_actions(valve_pct,
  fast_source_power_w=0)`; `step_with_power(weather: WeatherPoint, q_floor_w, q_sol_w=0)`; props
  `T_air`, `T_slab`, `valve_position`, `state`.
- `class BuildingSimulator(rooms, weather, *, hp_mode=HEATING, hp_max_power_w=None,
  sensor_noise=None, weather_comp=None, cooling_comp=None)`.
  - `get_all_measurements() -> dict[str, RoomInputs]` — **produces the SAME `RoomInputs` the HA
    coordinator builds**, so `BuildingController.step` is called identically in tests and in HA. Fill
    `room_temperature_c` (noised), `humidity_pct` (from weather), `outdoor_temperature_c`,
    `loops` with realistic `supply/return/valve` (supply from weather-comp curve, return = supply -
    ΔT estimate, valve = last applied), `mode`, `hp_active_for_ufh=not is_cwu` (no CWU here -> True).
  - `step_all(actions: dict[str, RoomOutputs]) -> dict[str, RoomInputs]` — apply valve %, distribute
    finite HP power via `loop_power`, integrate each room's `RCModel.step` one tick, advance clock.
  - Provide single-room convenience `get_measurements()/step()` too.
- Noise corrupts only the measurement snapshot (`T_room`, and optionally supply), never physics.
- `T_slab` is ground-truth inside the sim and is **NOT** placed into `RoomInputs` (the controller must
  not see it) — but the log records it for metrics/plots.

---

## 8. Metrics, log, scenarios, profiles

- `simulation_log.py`: `SimRecord(t, inputs: RoomInputs, outputs: RoomOutputs, weather: WeatherPoint,
  t_slab: float, room_name="")` frozen with flat props (`T_room`, `T_slab`, `valve_pct`, `T_out`, ...);
  `SimulationLog` with append/append_from_step/len/iter/getitem/get_room(name)/time_range/to_dataframe.
- `metrics.py`: `SimMetrics` frozen (`comfort_pct, max_overshoot, max_undershoot, mean_deviation,
  fast_source_runtime_pct, energy_kwh, condensation_events, max_floor_temp, min_floor_temp`) with
  `from_log(log, setpoint, *, comfort_band=0.5, ufh_nominal_power_w=None, dt_minutes=1)` and
  `compare(other)`. Assertion helpers (raise AssertionError w/ diagnostic): `assert_comfort`,
  `assert_floor_temp_safe(max_temp=34.0)`, `assert_no_condensation(margin=2.0)`,
  `assert_no_freezing(hard_min=16.0)`, `assert_no_prolonged_cold`.
- `scenarios.py`: factory functions returning `SimScenario`, `SCENARIO_LIBRARY: dict[str, Callable]`.
  Include: `steady_heating`, `cold_snap`, `solar_overshoot`, `spring_transition` (mode transitional),
  `hot_july_floor_cooling` (mode cooling, high humidity -> exercises dew-point), `sensor_dropout`.
- `building_profiles.py`: factory functions returning `BuildingConfig`, `BUILDING_PROFILES` registry.
  Include a `modern_bungalow()` multi-room reference (parterowy, ~13 UFH loops, HP ~4.9 kW, wylewka
  ~7 cm, lat 50.5/lon 19.5 — from the PRD reference house) and single-room parametric variants
  (`well_insulated`, `leaky_old_house`, `thin_screed`, `heavy_construction`), plus a
  `_make_3r3c_params(area_m2, ...)` helper. Use physically realistic RC values (blueprint §13 table:
  C_air~60kJ/K per 20 m², C_slab~3250 kJ/K per 80 mm, R_sf~0.01, τ_slab 4–6 h).

---

## 9. Home Assistant adapter contract

Mirror blueprint §2 exactly, with these tortoise-specific choices:

- **`manifest.json`**: `domain:"tortoise_ufh"`, `name:"Tortoise-UFH"`, `config_flow:true`,
  `integration_type:"hub"`, `iot_class:"local_polling"`, `loggers:["tortoise_ufh"]`,
  `requirements:["numpy>=1.26","scipy>=1.12"]`, `version:"0.1.0"`, `codeowners:["@hubertciebiada"]`,
  `documentation`/`issue_tracker` -> github `hubertciebiada/tortoise-ufh`.
- **`const.py`**: `DOMAIN`, `PLATFORMS=[NUMBER, SENSOR, BINARY_SENSOR, SWITCH]`, `UPDATE_INTERVAL_MINUTES=5`,
  full `CONF_*` vocabulary (home_setpoint, mode entity, per-room: temp/humidity/outdoor/valves(list)/
  supply(list)/return(list)/fast_source entity+kind/offset/enabled/cooling_enabled/participates),
  `WATCHDOG_TIMEOUT_MINUTES=15`, unit sets (°C/%/W), `ENTITY_STALE_MAX_SECONDS=300`,
  `CONF_LIVE_CONTROL`, `CONF_KILL_SWITCH`.
- **`coordinator.py`** (`TortoiseUfhCoordinator(DataUpdateCoordinator)`, 5-min): builds one
  `BuildingController`; each cycle READS source entity states (`_read_float_state` with stale cache like
  blueprint), assembles `dict[str, RoomInputs]`, calls `BuildingController.step`, stores typed payload
  (per-room outputs+report, global dew point, watchdog/status), and — for rooms with live control AND
  the global kill-switch OFF-not-engaged — WRITES: valve via `number.set_value` (all the room's valve
  entities, but only when the new value differs from last by >= `valve_write_threshold_pct`), split via
  `climate.set_hvac_mode` + `climate.set_temperature`. All writes `blocking=False`, try/except +
  `_LOGGER.exception` (`# noqa: BLE001`). Watchdog + shadow-mode identical pattern to blueprint.
  **Kill-switch ON or shadow mode => compute + report but emit NO commands.**
- **`number.py`**: `NumberEntity` for global **home temperature** (writable; range 5..30, step 0.5) and
  one per-room **offset** (writable; range -5..+5, step 0.5). `async_set_native_value` -> coordinator
  setter -> `async_set_updated_data`. These are the setpoint source of truth (config exposure decision).
- **`sensor.py`**: description-driven (`value_fn`) per-room diagnostics (recommended_valve, error, trend,
  dew_point, explanation as a text sensor, fast_source_mode) all `EntityCategory.DIAGNOSTIC`; **one GLOBAL
  sensor `global_safe_dew_point`** (°C) = coordinator's `global_safe_dew_point_c` — the value the owner
  pipes to the HP; plus global `algorithm_status`, `last_update` (TIMESTAMP), `watchdog_status`.
- **`binary_sensor.py`**: per-room `sensor_lost`, `output_saturated`, `s2_condensation_active`,
  `live_control`. **`switch.py`**: global `kill_switch` (on = module emits no commands) and per-room
  `live_control` toggle (shadow<->live).
- **`config_flow.py`**: multi-step wizard with selectors (blueprint §2 pattern). Valve selector uses
  `EntitySelectorConfig(domain=["number"])` **multiple=True** (a room has 1..n valves); supply/return
  `domain=["sensor"], device_class=["temperature"]` (per loop, multiple); room temp sensor+temperature;
  humidity `device_class=["humidity"]`; fast source `domain=["climate"]`; global mode entity
  `domain=["select","input_select"]`. Rooms can be seeded from HA areas but manual add is fine. Options
  flow: per-room live-control booleans + kill switch + advanced controller knobs (hidden by default).
  Use `entity_validator.py` (unit-only, hardware-agnostic).
- **`websocket.py`**: register WS commands `tortoise_ufh/get_config`, `tortoise_ufh/get_live`
  (returns `BuildingOutputs.to_dict()` + setpoints + statuses), `tortoise_ufh/set_home_temperature`,
  `tortoise_ufh/set_room_offset`, `tortoise_ufh/set_room_enabled`, `tortoise_ufh/set_mode`,
  `tortoise_ufh/set_kill_switch`. Each `@websocket_api.websocket_command` + `@callback`, admin-guarded.
- **`panel.py`** + `__init__.py`: serve `frontend/tortoise-ufh-panel.js` via
  `hass.http.async_register_static_paths([StaticPathConfig("/tortoise_ufh_panel/panel.js", <path>,
  False)])` and register a custom sidebar panel with
  `frontend.async_register_built_in_panel(hass, "custom", sidebar_title="Tortoise-UFH",
  sidebar_icon="mdi:tortoise", frontend_url_path="tortoise-ufh", require_admin=True,
  config={"_panel_custom": {"name":"tortoise-ufh-panel","module_url":"/tortoise_ufh_panel/panel.js",
  "embed_iframe": False}})`. Unregister on unload. Guard against double registration across entries.
- **`services.yaml`**: `set_home_temperature{temperature}`, `set_room_offset{room, offset}`,
  `set_mode{mode}`.

## 10. Panel — `frontend/tortoise-ufh-panel.js` (self-contained, NO build step)

A single ES module defining `class TortoiseUfhPanel extends HTMLElement` and
`customElements.define("tortoise-ufh-panel", TortoiseUfhPanel)`. HA sets `.hass` and `.narrow`/`.route`
properties on the element. **No imports from CDNs** (CSP) — plain DOM + inline `<style>` in a shadow root.
Must render:
- Header with global **home temperature** (editable -> `set_home_temperature` WS) and **mode** selector
  (`set_mode`) and a **kill-switch** toggle.
- A **table, one row per room** (from `get_config`): name, current temp, setpoint (=global+offset),
  error, valve %, fast-source state, status; editable **offset** and **participation/live** toggles.
- A **live preview** panel per selected room showing the full **report** JSON (the "okno do black-boxa",
  readable for human + AI). Poll `get_live` every ~5 s (or subscribe if you add an event).
- Light/dark aware via `hass.themes`; degrade gracefully if a WS command errors (show message, never
  throw). Keep it dependency-free and defensive.

Also ship `dashboard_tortoise_ufh.yaml` as a Lovelace fallback (glance + history-graph cards).

---

## 11. Packaging, CI, tests

- `pyproject.toml`: like pump-ahead but `name="tortoise-ufh"`, deps `["numpy>=1.26","scipy>=1.12"]`
  (NO cvxpy/osqp — PID only), extras `viz=[matplotlib,pandas]`, `dev=[pytest,pytest-cov,ruff,mypy,
  pre-commit]`. pytest markers `unit|simulation|slow`, `filterwarnings=["error"]`, `--strict-markers`.
  ruff select `["E","F","I","UP","B","SIM"]`, line 88. mypy strict, ignore `homeassistant.*`,
  `custom_components.*`, `scipy.*`, `pandas.*`, `matplotlib.*`.
- `hacs.json = {"name":"Tortoise-UFH","homeassistant":"2024.1.0","render_readme":true}`.
- CI: 4 jobs (ruff check+format, `mypy tortoise_ufh/`, `pytest tests/unit -m unit`,
  `pytest tests/simulation -m simulation`) + `validate.yml` (hassfest + hacs/action).
- Tests mirror blueprint §5: 3-tier conftest, seeded rng (unit 42 / sim 12345), a session-scoped
  `run_scenario` harness returning `(SimulationLog, SimMetrics)`, parametrized scenario tests calling
  `assert_*` per room, `pytest.raises` for a known-fail control case. **Core tests must run with only
  numpy/scipy/pytest installed** (no HA). HA-layer tests are optional/skipped if
  `pytest_homeassistant_custom_component` is unavailable.

---

## 12. Fan-out ownership (each file has ONE owner; read this doc + your deps first)

Every implementation agent: (1) read this BUILD_SPEC + the blueprint + PRD §8; (2) read any core files
your module imports (they exist on disk by your phase); (3) write EXACTLY your file(s); (4) keep the
public signatures above verbatim; (5) full type hints + frozen dataclasses + `__post_init__` validation
+ Google docstrings; (6) no `homeassistant` import in `tortoise_ufh/`.

Do not edit files you do not own. Do not add new runtime dependencies beyond numpy/scipy (core) and
Home Assistant (adapter). When in doubt, simpler + matches pump-ahead.
