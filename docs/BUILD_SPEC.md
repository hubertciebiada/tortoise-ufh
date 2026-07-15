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

**`custom_components/tortoise_ufh/core/` (the pure core) MUST NEVER import `homeassistant`.**
It is pure Python (numpy/scipy + stdlib), ships `py.typed`, and is fully unit- and
simulation-testable offline. *(Amendment: the core was originally specified as a top-level
`tortoise_ufh/` package; it is now **vendored inside the integration** so a HACS install — which
ships only `custom_components/tortoise_ufh/` — is self-contained.)* It stays *logically*
separate: the HA adapter `custom_components/tortoise_ufh/` imports FROM the core via `.core` and
never the reverse; the core imports its own siblings **relatively** (`from .models import ...`).
Because importing any core submodule first runs the adapter `__init__.py`, that `__init__` must
ALSO stay importable WITHOUT homeassistant — every HA import there is lazy (deferred into
function bodies or `TYPE_CHECKING`), and it does not import the HA-dependent `.const` at module
top level. Core talks to the outside only through plain dataclasses and structural `Protocol`s
(`WeatherSource`). Any core file that does `import homeassistant` is a bug and will be rejected.
Throughout this spec the shorthand `tortoise_ufh.X` refers to the core module
`custom_components/tortoise_ufh/core/X`.

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
  .github/workflows/ci.yml           # ruff, mypy(core), unit, simulation, ha (optional layer)
  .github/workflows/validate.yml     # hassfest + HACS validation
  docs/BUILD_SPEC.md                 # this file
  docs/DECISIONS.md                  # the 10 Q&A decisions + floor-cooling addendum
  docs/ALGORITHM_SPEC.md             # math + control spec (analogous to PumpAhead_Algorithm_Spec.md)

  custom_components/tortoise_ufh/    # HA ADAPTER (imports the core via .core)
    __init__.py                      # setup/unload/migrate entry, runtime_data, panel+ws+services reg
                                     #   (module top level imports stdlib ONLY — see §1)
    manifest.json
    const.py                         # CONF_* vocabulary + defaults + PLATFORMS + knob specs
    coordinator.py                   # DataUpdateCoordinator: read states, run core, write commands
    readers.py                       # SourceReader: entity reads + plausibility (annex 2026-07-10)
    writers.py                       # CommandWriter: valve/split writes + caches (annex 2026-07-10)
    tuning.py                        # controller-knob introspection helpers (annex 2026-07-10)
    config_flow.py                   # multi-step wizard + options-flow menu (selectors)
    entity_validator.py              # hardware-agnostic unit validation
    device.py                        # DeviceInfo helpers: room + hub devices (v0.5.0)
    number.py                        # global home-temp + per-room offset (writable)
    sensor.py                        # per-room report/diagnostics + GLOBAL safe dew-point sensor
    binary_sensor.py                 # per-room flags (sensor_lost, saturated, condensation)
    select.py                        # per-room control-state select (off / live)
    websocket.py                     # panel API commands (config/live/setters + room state + tuning)
    panel.py                         # register sidebar panel + static path for the JS module
    services.py                      # register/unregister the services declared in services.yaml
    diagnostics.py                   # HA config-entry diagnostics download
    services.yaml                    # set_home_temperature, set_room_offset, set_mode
    strings.json
    translations/en.json
    translations/pl.json
    frontend/tortoise-ufh-panel.js   # self-contained vanilla-JS panel (no build step)
    dashboard_tortoise_ufh.yaml      # fallback Lovelace template
    brand/icon.svg

    core/                            # PURE CORE, vendored (no HA import — see §1)
      __init__.py                    # flat re-export hub + __version__
      py.typed
      const.py                       # physical constants + core defaults
      models.py                      # enums + I/O dataclasses (black-box contract) + RoomReport
      config.py                      # RoomConfig, BuildingConfig, ControllerConfig, SimScenario
      rc_model.py                    # RCParams, ModelOrder, RCModel (3R3C ZOH via expm)
      pid.py                         # PIDController (PI + anti-windup)
      controller.py                  # RoomController (black box) + BuildingController (orchestrator)
      fast_source.py                 # FastSourceMachine (split direction/dwell; annex 2026-07-10)
      trend.py                       # TrendEstimator (filtered dT/dt trend; annex 2026-07-10)
      dew_point.py                   # Magnus dew point + cooling throttle
      weather_comp.py                # WeatherCompCurve / CoolingCompCurve (feedforward)
      ufh_loop.py                    # LoopGeometry + loop_power (EN 1264) — used by simulator
      weather.py                     # WeatherPoint, WeatherSource protocol, SyntheticWeather
      sensor_noise.py                # SensorNoise (seeded Gaussian)
      safety.py                      # safety rules S1..S5 (data) + SafetyEvaluator
      simulator.py                   # BuildingSimulator + SimulatedRoom + HeatPumpMode
      simulation_log.py              # SimRecord + SimulationLog
      metrics.py                     # SimMetrics + assert_* helpers
      scenarios.py                   # scenario factories + SCENARIO_LIBRARY registry
      building_profiles.py           # building factories + BUILDING_PROFILES registry

  tests/
    conftest.py                      # shared fixtures (RC params/models, seeded rng)
    unit/conftest.py                 # unit-only fixtures (seed 42)
    unit/test_*.py                   # one per core module
    simulation/conftest.py           # run_scenario harness (seed 12345)
    simulation/test_scenarios.py     # parametrized scenario tests calling assert_* helpers
    simulation/test_sim_smoke.py     # trivial simulation test so suite collects >=1
    ha/test_*.py                     # optional HA-layer tests (skipped without
                                     #   pytest_homeassistant_custom_component)

  dev/panel-preview.html             # offline panel preview harness (no HA needed)
  docker/                            # dockerised HA dev/test harness (compose + Dockerfile.test)
```

---

> **Annex 2026-07-10 (structural refactor, zero behaviour change).** Five submodules were
> extracted verbatim from the largest modules; every public API, signature and numeric
> path is unchanged (simulation results on both seeds are bit-identical):
>
> * `core/fast_source.py` — `FastSourceMachine`: the three-state split direction machine
>   (OFF/HEATING/COOLING), min ON/OFF dwell clock and S4 physical-feedback sync, formerly
>   ~6 methods + 5 state fields inside `RoomController`. `RoomController` delegates; the
>   mode→demand mapping and the HEATER-cannot-cool rule stay in `controller.py`.
>   `FAST_TARGET_OFFSET_K` moved with it and is re-exported from `controller.py`.
> * `core/trend.py` — `TrendEstimator`: the debounce-aware EMA trend filter (S10),
>   formerly 3 fields + inline logic in `RoomController.step` / `_safe_degrade`.
> * `readers.py` — `SourceReader`: entity reads, stale cache, state-age gate (C4) and the
>   C3/S8 plausibility gates, formerly `coordinator._read_*` + two private caches.
> * `writers.py` — `CommandWriter`: valve/split service calls, write-threshold and S3
>   re-assert caches, farewell parking (C5), formerly `coordinator._write_*` / farewell.
> * `tuning.py` — controller-knob introspection (names/ranges/descriptors/values/coercion),
>   formerly private helpers in `websocket.py`; the options flow reuses `global_controller`.
>
> `controller.py` keeps `RoomController` + `BuildingController` per this section (no split);
> the coordinator remains the only owner of the cycle, setpoints/Store and the watchdog.
> `CONF_CONTROLLER` moved from `config_flow.py` to `const.py` (same key string; re-imported
> in `config_flow.py` for compatibility) so runtime modules no longer import the wizard.
> The core re-export hub gained `FastSourceMachine` and `TrendEstimator` (additive only).

## 3. Units & conventions (repo-wide, non-negotiable)

- Temperatures **°C**; power **W**; valve **0..100 % (float)**; R in **K/W**; C in **J/K**; GHI **W/m²**;
  humidity **0..100 %**; time in **minutes** (simulation) / **seconds** (`RCModel.dt`, real-time cycle).
- `from __future__ import annotations` at the top of every module. `mypy --strict` clean on the
  core (`mypy custom_components/tortoise_ufh/core`).
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
    last_update_age_minutes: float = 0.0     # ADDITIVE (2026-07-09, S6): per-room data age -> S5 watchdog
    fast_source_group: str = ""              # ADDITIVE (2026-07-12, K4): multisplit outdoor-unit group key
    fast_source_hvac_mode: str | None = None # ADDITIVE (2026-07-12, K4): raw hvac-mode feedback -> S4 sees direction
    humidity_stale_frac: float = 0.0         # K7 2026-07-12 (linearised D5, R3): RH staleness 0..1 (age 60->120 min) -> frac * 1 K dew pad
    fast_source_allowed: bool = True         # ADDITIVE (2026-07-12, B1 quiet hours): adapter's per-room allowed-window verdict (crosses midnight); False = fast source may not engage
    # LoopInput additionally carries the S6 hydraulic no-flow watchdog probe reads
    # (2026-07-13): supply/return already present; loop_no_flow reads them as an
    # INDEPENDENT witness and never trusts valve_position_pct feedback (it can echo).
    # (The stuck-open reverse detection was removed 2026-07-13 — see docs/DECISIONS.md §17.)

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
    room_temperature_c: float | None = None  # ADDITIVE: echoed measured room temp, None if sensor lost
    dew_excluded_reason: str | None = None   # ADDITIVE: None if eligible, else not_cooling_mode|cooling_disabled|no_temperature|no_humidity
    fast_dwell_remaining_s: float | None = None  # ADDITIVE: min ON/OFF dwell lock remaining [s], None if unlocked / no fast source
    loop_flow_status: tuple[str, ...] = ()   # ADDITIVE (2026-07-13, S6): per-loop watchdog status "ok"|"no_flow"|"inactive"
    actuation_test_status: str | None = None # ADDITIVE (2026-07-13, S6): self-test "running"|"passed"|"failed"|"aborted", None when idle/untested
    actuation_test_remaining_min: float | None = None  # ADDITIVE (2026-07-13): minutes left of a running self-test
    actuation_test_loops: tuple[str, ...] = ()  # ADDITIVE (2026-07-13): per-loop verdicts of the last completed self-test
    # New flags: "loop_no_flow","actuation_test_running","actuation_test_failed".
    # BuildingController.step gained an optional kw-only `global_supply_temperature_c: float | None = None`
    # (the manifold-bar probe feeding the S6 circulation gate); default None keeps old callers unchanged.

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
    sensor_lost_rooms: int = 0               # ADDITIVE (2026-07-09, safety-F13): rooms flagged sensor_lost
```

**`global_safe_dew_point_c = None` contract (S14, 2026-07-09):** `None` means "no eligible cooled
room right now" (not cooling season, cooling disabled, or the temperature/humidity inputs are
unusable) — it does NOT mean "no condensation risk". The HA sensor renders it as
`unknown`/`unavailable`. Any consumer piping this value into the heat pump's cooling-supply lower
limit MUST be fail-safe: on `unknown` either hold a conservative fixed lower limit (e.g. 18-19 °C)
or stop floor cooling entirely — never "no limit". See the README for a reference automation.

**Amendment 2026-07-15 (cooling setpoint-flicker, DECISIONS §21, v0.13.0, issue #7):** an ADDITIVE
opt-in hp-link behaviour — **no change to the frozen three-output contract, no `RoomReport` field,
no config migration.** The pure-core `SetpointFlicker` (`core/hp_link.py`) may, for ONE cycle, lower
the ONE existing cooling-setpoint write to the raw worst-room dew point (`p`, ceiled onto the pump
grid; reserve = `DEW_MARGIN_DEFAULT_K`) to trip a Panasonic compressor out of its FIXED 3 K return
deadband when it idles (`compressor_freq == 0`) with real `cooling_demand`, then unconditionally
restores the dew-safe target. The four `hp_flicker_*` knobs above are the only new config; three
optional pump entities (return + compressor-frequency drive it, outlet is diagnostic-only). OFF by
default; Panasonic-specific.

**Report must be JSON-serializable** (provide `to_dict()` helpers on `RoomOutputs`/`RoomReport`/
`BuildingOutputs` returning plain dict/list/str/float/bool; enums -> their `.value`). The HA websocket
and the panel consume these dicts.

`room_temperature_c` is an **additive, non-breaking** report field (default `None`, appended after
`explanation` so positional constructors are unaffected). Every `RoomReport` construction site echoes
the room's measured temperature into it (`None` when the sensor is lost); the panel reads it directly
instead of reconstructing the measurement from `setpoint - error_c`, which can transiently disagree
with the broadcast setpoint between a setpoint rebroadcast and the next recompute.

`dew_excluded_reason` and `fast_dwell_remaining_s` are **additive, non-breaking** report fields
(default `None`, appended after `room_temperature_c`). `dew_excluded_reason` is computed by the pure
`classify_dew_eligibility(RoomInputs) -> str | None` helper in `controller.py`, the single source of
truth shared with `BuildingController._eligible_dew_point` (one classifier, two consumers): `None`
means the room IS eligible for the global safe dew point (COOLING + `cooling_enabled` + usable temp +
humidity), otherwise one of `not_cooling_mode` / `cooling_disabled` / `no_temperature` / `no_humidity`
explains why it is excluded (panel surfaces it when the global safe dew point is `None`).
`fast_dwell_remaining_s` is the seconds left on the fast source's min ON/OFF dwell lock for its
current state (min-ON while running, min-OFF while idle), `None` once elapsed or when there is no fast
source; the panel renders it as "unlocks in ~N min".

---

## 5. Controller spec — `pid.py` + `controller.py` (the heart)

### 5.1 `pid.py` — `PIDController`
```python
class PIDController:
    def __init__(self, kp: float, ki: float, kd: float = 0.0, *, dt: float = 300.0,
                 output_min: float = 0.0, output_max: float = 100.0,
                 unwind_factor: float = 1.0) -> None: ...   # ADDITIVE (2026-07-12, K1)
    def compute(self, error: float, *, dt_seconds: float | None = None,
                freeze_integrator: bool = False) -> float: ...
    def shift_integral(self, delta: float) -> None: ...     # ADDITIVE (2026-07-12, K1)
    def reset(self) -> None: ...
    @property
    def integral(self) -> float: ...
    @property
    def last_output(self) -> float: ...
```
Discrete PI(+optional D) with **back-calculation anti-windup**: `P=kp*e`; if not frozen `I += ki*e*dt`;
`D=kd*(e-e_prev)/dt` (0 on first call); `u_raw=P+I+D`; `u=clip(u_raw,min,max)`; then if `ki>0`,
`I += (u - u_raw)` (back-calc). `freeze_integrator=True` skips the `I += ki*e*dt` accumulation (used
when `hp_active_for_ufh is False`). `dt` is per-call: `compute(..., dt_seconds=...)` uses the REAL
elapsed interval for the integral and derivative (raises on `dt_seconds <= 0`); when `None` it falls
back to the configured `dt` (default 300 = 5 min cycle). Validate kp,ki,kd>=0, dt>0,
output_min<output_max.

**Amendment 2026-07-12 (K1, DECISIONS §11):** (a) `shift_integral(delta)` — external re-seed hook
(clamped to `[output_min, output_max]`, no-op at `ki == 0`) used by the room controller's bumpless
setpoint transfer; (b) `unwind_factor >= 1` — while `error * I < 0` (a sign-opposed, i.e. stale,
integral) the accumulation step runs at `unwind_factor * ki`; it only ever pulls `I` toward zero, so
equilibrium is untouched. The room controller passes `_INTEGRATOR_UNWIND_FACTOR = 8.0`.

### 5.2 `controller.py` — `RoomController` (the black box, one per room)
```python
class RoomController:
    def __init__(self, config: ControllerConfig, *, name: str = "") -> None: ...
    def step(self, inputs: RoomInputs, *, dt_seconds: float = 300.0) -> RoomOutputs: ...
    def reset(self) -> None: ...
```
Algorithm (all knobs from `ControllerConfig`, §6.3):

1. **Missing room temp** (`room_temperature_c is None`): SAFE DEGRADE — mode-dependent
   *(amendment 2026-07-09, supersedes the unconditional freeze; see PRD §8.7 note +
   DECISIONS §6)*:
   - **HEATING:** hold last valve position (freeze; `RoomController` remembers
     `_last_valve_pct` — the last position of *healthy* regulation, never a safety-override
     extreme; the cold-start hold before any live step is `config.valve_floor_pct`).
   - **COOLING (and TRANSITIONAL/OFF):** valve **0**, never freeze-open. A lost temperature
     breaks BOTH condensation defences at once (the room drops out of the global dew maximum
     as `no_temperature` AND the local S2 throttle cannot run without `T_room`), so a
     frozen-open valve would pass unprotected chilled water indefinitely. The heating hold
     memory is left untouched by this branch.
   In both branches: fast source **OFF**, flag `"sensor_lost"`, explanation says so. Do not
   run PID.
2. **Mode == OFF**: valve 0, fast source OFF, report explains "off".
3. **Mode == TRANSITIONAL**: valve parked (0), only fast source, bidirectional. *(Amendment
   2026-07-09, C6+S12 — see DECISIONS §7.)* An idle machine engages in the direction whose demand
   exceeds `boost_offset_c`; a running machine keeps its REMEMBERED direction with
   `target = setpoint` (the split's own regulation holds the room AT the setpoint — the old
   `[setpoint-1.0, setpoint-0.3]` hysteresis band's ~-0.65 K seasonal bias is gone) and releases
   only once the room crosses the FAR edge of the comfort band (`demand < -deadband_c`, i.e. free
   gains carry the room), through the min-ON dwell. No PID on valve.
4. **Trend** *(amendment 2026-07-09, S10 — see DECISIONS §8)*: FILTERED. Raw samples
   `raw = (T_room - _prev_T_room)/dt_hours` are taken only once at least **60 s** has accumulated
   since the previous sample (a 2 s debounced recompute HOLDS the previous filtered value instead
   of dividing a sensor tick by 2 s), then smoothed by a first-order EMA with **tau = 15 min**
   (`alpha = 1 - exp(-dt/tau)`). `trend_c_per_h` in the report is the filtered value; 0 on the
   first call and after sensor loss (a gap invalidates the trend).
5. **Error** (heating convention): `error = setpoint - T_room`. Cooling uses `error = T_room - setpoint`
   internally so a positive error always means "need more actuation".
6. **Deadband**: if `|setpoint - T_room| <= deadband`, PID sees `error=0` region (no integral
   growth); the valve then RESTS at the accumulated integral (plus trend/FF terms) — it does NOT
   decay toward the floor/0 (control-F7 clarification, 2026-07-09: the resting integral IS the
   steady heat demand). Keep it simple: pass `error` reduced by deadband
   (`error_db = sign(error)*max(0, |err|-deadband)`).
7. **Integrator freeze** *(extended 2026-07-09, S1/S2 — see DECISIONS §8)*:
   `freeze = (inputs.hp_active_for_ufh is False) or (COOLING and dew_factor < 1.0)` — the S2 dew
   throttle multiplies the valve AFTER the PID, invisibly to the back-calculation, so integrating
   under an active throttle banks a windup that slams the valve open when the humidity clears; the
   throttle factor is computed here (it depends only on inputs) and applied in step 12.
   Additionally: a HEATING<->COOLING transition RESETS the integrator (the error convention flips),
   and >12 h of accumulated inactivity (OFF / TRANSITIONAL / cooling opt-out / sensor lost) clears
   it (one season's integral must not become the first command of the next).
   *(2026-07-12, K9 — see DECISIONS §11.)* The throttle-freeze STAYS: a back-calculation from the
   FINAL (throttled) valve was measured and rejected (it pins the integral at ~0 and the
   post-release catch-up is not faster — a `ki`-speed property, not windup).
7b. **Bumpless setpoint transfer** *(ADDITIVE 2026-07-12, K1 — see DECISIONS §11)*: when the
   effective setpoint changed by `dK` since the previous PID-active cycle in the SAME mode,
   `pid.shift_integral(kp * d_err)` where `d_err = dK` in HEATING and `-dK` in COOLING — the
   integral (the loop's memory of the operating point) moves WITH the setpoint instead of
   discharging the difference at `ki` speed. The reference (`_last_pid_setpoint_c`) dies with
   every PID reset (mode flip, inactivity decay, `reset()`), so no stale delta is ever applied.
   Together with the PID's `unwind_factor = 8` (a sign-opposed integral discharges 8× faster,
   §5.1) this is what makes a daily night setback behave: measured 17.4 h → 0.8 h of active
   heating above a freshly lowered setpoint.
8. **PID**: `pid_out = pid.compute(error_db, dt_seconds=dt_seconds, freeze_integrator=freeze)`
   -> 0..100 (the integral accumulates the step's REAL `dt_seconds`).
9. **Trend damping** (anti-overshoot, the "człon trendu"): subtract `kt * trend_toward_setpoint`.
   In heating, if room is rising toward setpoint (`trend>0`) reduce valve by `config.kt * trend_c_per_h`
   (clamped >=0 contribution). In cooling mirror. This is the key inertia/overshoot tamer.
10. **Feedforward** (optional): if `outdoor_temperature_c` present and `config.outdoor_ff_enabled`,
    add a small baseline term from `WeatherCompCurve`-style mapping (heating: colder outside -> higher
    baseline valve). Keep bounded and modest; PI does the rest. The shaping constants are
    `ControllerConfig` knobs since 2026-07-09 (control-F6): `ff_neutral_c` (default 15 °C),
    `ff_gain_pct_per_k` (1 %/K), `ff_max_pct` (20 %).
11. **Valve floor** (heating only, when calling for heat i.e. error>0 beyond deadband):
    `valve = max(valve, config.valve_floor_pct)`. Not applied in cooling/off/transitional or when
    satisfied.
12. **Cooling dew-point local throttle (S2, per room)**: only in COOLING and `cooling_enabled`.
    Compute `T_dew = dew_point(T_room, humidity)`. Take coldest loop supply `t_supply_min` (from
    `inputs.loops`). `factor = cooling_throttle_factor(t_supply_min, T_dew, margin=config.dew_margin_k,
    ramp=config.dew_ramp_k)` in [0,1]; `valve *= factor`; if `factor==0` flag `"s2_throttle"`.
    (When humidity or supply missing -> be conservative: factor toward 0, flag.)
    *(REVISED 2026-07-12, K6 — owner decision "tylko pompa +2"; see DECISIONS §11.)* The ramp
    ENDS at `dew_margin_k` instead of starting there: `factor = 1` at `gap >= margin` (full
    cooling exactly on the heat pump's global `dew + 2 K` floor), linear down to `0` at
    `gap <= max(0, margin - ramp)` (the room's actual dew point with the defaults). The local
    layer no longer stacks a second margin on the pump floor — it is the emergency backstop
    below it (and the hard S2 rule in `safety.py` sits at the dew point itself,
    `S2_HARD_MARGIN_K = 0`, trip `gap < 0` / clear `> +1 K`). A STALE humidity
    (`inputs.humidity_stale_frac`, K7; linear D5 2026-07-12) pads the effective dew by
    `frac * 1 K` and flags `"rh_stale_gated"`.
    The flag rename (`"s2_throttle"`; previously `"s2_condensation"`) leaves the old name
    exclusively to the hard-safety rule.
    A COOLING room with `cooling_enabled=False` never reaches this step: it short-circuits
    (before step 5) to valve 0, fast source OFF, flag `"cooling_disabled"` — an opted-out room
    must never receive chilled water, which would bypass both condensation defences.
13. **Clamp** valve to [0,100]; set `saturated` if hit a bound — EXCEPT a zero produced solely by
    the S2 throttle (control-F8, 2026-07-09): `saturated` stays the "PI hit a bound" signal,
    `dew_throttle_factor` carries the condensation story. `raw_valve_pct` = pre-clamp/floor value.
14. **Fast source (split) coordination** (only if `fast_source_kind != NONE`). *(Amendment
    2026-07-09, C6+S3+S4+S12 — see DECISIONS §7.)*
    - Engage when `|setpoint - T_room| > config.boost_offset_c` in the mode's needed direction.
    - **Three-state direction machine (C6):** the machine state is `OFF | HEATING | COOLING` —
      the direction is state, not a per-cycle computation. `OFF -> direction` requires the full
      min-OFF dwell; `running -> OFF` (requested OFF **or the opposite direction**) requires the
      full min-ON dwell; a HEATING<->COOLING reversal is therefore only reachable through OFF
      with the full min-OFF. A blocked request flags `"fast_source_min_runtime"` and the machine
      re-emits its REMEMBERED direction (never a freshly computed one) — indoor units may share a
      multisplit outdoor unit, so mixed directions across a debounced recompute are forbidden.
      The one exception: a hard S3/S4 safety force-ON may set the direction immediately.
    - Respect **min ON / min OFF** (`config.fast_min_on_minutes`, `config.fast_min_off_minutes`); track
      elapsed via internal timers advanced by `dt_seconds` (also on sensor-lost/OFF cycles, so a
      long outage counts toward the min-OFF wait); flag `"fast_source_min_runtime"` when a
      change is blocked by the timer.
    - **Physical-state sync (S4):** the first observed `fast_source_on` feedback wins over a cold
      machine — a running unit is adopted as ON (direction from the reported
      `fast_source_hvac_mode` when unambiguous, else from the global mode; K4 2026-07-12), a
      stopped one as OFF — and the dwell timer is re-seeded to 0 (a FULL dwell before any
      change), so an HA restart/reload (= every tuning change) can never short-cycle a
      compressor. Later feedback that disagrees with the previous cycle's command raises the
      additive `"fast_source_mismatch"` flag — since 2026-07-12 (K4) this comparison also sees
      the DIRECTION: a unit physically running a single-direction HVAC mode opposite to the
      command flags too (the bool-only comparison was blind to multisplit standby / manual
      reversal). The machine stays the owner (the adapter re-asserts). Without feedback
      (`fast_source_on is None`) the legacy free first transition is kept.
    - **Anti priority-inversion:** the split decision NEVER reduces/holds the valve; floor stays base.
      Split only *adds* boost above the threshold and releases once inside the comfort band.
    - Command (S12): `on=True, mode=HEATING|COOLING (per machine state),
      target = setpoint + 1 K (heating) / setpoint - 1 K (cooling)` — the split's ceiling-mounted
      sensor reads warm, so a plain `target = setpoint` throttled the unit before the boost was
      delivered; release still belongs to OUR room sensor. Else `on=False, mode=OFF`.
15. Build `RoomReport` with every term filled and a concise Polish/English-neutral `explanation`
    (e.g. `"Grzanie, błąd -0.4 K, trend +0.3 K/h. Zawór 34%. Split ON (boost)."`).

After step 15 two post-processing passes run on EVERY path (including safe degrade):
- **Hard-safety override (S1..S5):** a stateful `SafetyEvaluator` (`safety.py`, hysteresis kept
  across cycles) is fed the governing loop supply (hottest in heating / coldest in cooling), the
  room temperature and humidity; if any rule triggers, the override replaces the computed valve /
  fast-source command and the rule names are merged into the report flags.
  *(Amendment 2026-07-09, S5+S7 — see DECISIONS §6; COMPLETED 2026-07-12, K3 — see DECISIONS
  §11.)* The override decides the **water side and the air side independently** across all
  active rules: any `CLOSE_VALVE` rule (S1/S2) parks the valve at 0, but an active S3
  (`EMERGENCY_HEAT`) / S4 (`EMERGENCY_COOL`) still runs the fast source — S1 closing an
  overheated floor must not silence the only remaining heat source of a freezing room.
  Since 2026-07-12 (K3) a CLOSE_VALVE **without** a parallel emergency no longer touches the
  fast source at all: the air-side decision from the normal coordination (step 14 /
  transitional) stands — an S1 keeps a wanted boost running, an S2 in cooling keeps the split
  (the one source that can still cool safely) running, and the per-cycle force-off's dwell-clock
  sawtooth plus the flapping `"fast_source_min_runtime"` flag are gone. Mode.OFF / sensor-lost
  paths still force OFF upstream. A safety force-ON goes through `_force_fast_on`, which keeps
  the fast-source direction machine in sync (state set to the commanded direction, timer
  restarted on any state change) so releasing the override hands a *running* machine to the
  min-ON dwell instead of instantly stopping a fresh compressor. The override **never writes
  `_last_valve_pct`** — the sensor-lost freeze holds the last position of healthy regulation,
  not a safety extreme.
  *(Amendment 2026-07-09, S6 — see DECISIONS §8.)* The S5 watchdog is LIVE: the adapter feeds the
  per-room data age via `RoomInputs.last_update_age_minutes` (additive field) into
  `SensorSnapshot`, and `FALLBACK_HP_CURVE` alone now commands the **neutral position** —
  `valve_floor_pct` in HEATING (a passive baseline tempered by the heat pump's own curve), 0 in
  COOLING (stale data never justifies chilled water) — instead of a hard close. The adapter's
  building-level watchdog stays report-only; S5 is the per-room actuator-side escalation
  (sensor-lost freeze at ~45 min of staleness, neutral position ~15 min later).
- **Additive report stamping:** `dew_excluded_reason` (via `classify_dew_eligibility`, §4) and
  `fast_dwell_remaining_s` are stamped onto the final report (post-safety, so the dwell value
  reflects the final fast-source state; a safety force-off clears it).

`dt_seconds` semantics: ONE time base — the trend (step 4), the fast-source dwell timers
(step 14) AND the PI integral (step 8) all advance by the *actual* `dt_seconds` passed to `step`
(the HA coordinator feeds the measured elapsed time, clamped to [1, 900] s — see §9). This keeps
the integral honest on irregular steps: a debounced recompute ~2 s after a setpoint change
accumulates ~2 s of integral, not a full nominal 300 s cycle.

`RoomController` holds internal state: `_pid`, `_prev_T_room`, `_last_valve_pct`, split timers,
and the `SafetyEvaluator`. `reset()` clears them.

### 5.3 `controller.py` — `BuildingController` (orchestrator)
```python
class BuildingController:
    def __init__(self, configs: dict[str, ControllerConfig]) -> None: ...
    def step(self, inputs: dict[str, RoomInputs], *, dt_seconds: float = 300.0) -> BuildingOutputs: ...
    def reset(self) -> None: ...
    def invalidate_trends(self) -> None: ...                       # ADDITIVE (2026-07-12, R2-F6)
    def notify_fast_source_farewell(self, room_name: str) -> None: ...  # ADDITIVE (2026-07-12, K10)
```
- One `RoomController` per room. Runs each `step`.
- **Global safe dew point:** over rooms where `mode==COOLING and cooling_enabled` with a usable
  room temperature and humidity (eligibility decided by the shared `classify_dew_eligibility`
  helper, §4 — one classifier, two consumers),
  compute `T_dew_i = dew_point(T_room_i, rh_i)`, take `max_i`, add `config dew margin (2K)` ->
  `global_safe_dew_point_c`. `None` if no eligible room. A STALE humidity
  (`humidity_stale_frac`, K7 2026-07-12; linear D5) pads that room's contribution by
  `frac * 1 K`.
- **Multisplit group arbiter** *(ADDITIVE 2026-07-12, K4 — see DECISIONS §11)*: after stepping
  all rooms, rooms sharing a non-empty `RoomInputs.fast_source_group` are direction-arbitrated —
  ONE direction per group per cycle. A unit that was already running under its min-ON lock (or
  is S3/S4-forced) pins the group's direction; otherwise the room with the largest comfort-band
  excess `max(0, |error| - deadband)` wins. Losing ON commands are rewritten through
  `RoomController.resolve_group_conflict` (fast OFF + flag `"fast_source_group_conflict"`,
  dwell reset -> an honest min-OFF before re-engaging). A pathological double-pin (two opposite
  min-ON locks, only reachable by adopting an inconsistent physical state) overrides nobody and
  flags every conflicting room. Ungrouped rooms are untouched.
- Never raises on a single room's failure; a room that errors becomes a safe-degraded `RoomOutputs`
  with a flag — MODE-AWARE since 2026-07-12 (K5): HEATING holds the last healthy valve,
  COOLING/TRANSITIONAL/OFF close to 0 (a crashed controller computes neither condensation
  defence), symmetric with the sensor-loss degrade of step 1.
- `invalidate_trends()`: adapter hook for a control-cycle gap that hit the adapter's dt clamp
  (900 s) — restarts every room's trend filter so the clamped dt cannot inflate the next raw
  dT/dt sample. `notify_fast_source_farewell(room)`: adapter hook after the C5 farewell write —
  transitions the room's fast machine to OFF (dwell reset), so live -> off -> live passes an
  honest min-OFF instead of an instant ON (K10).

---

## 6. `config.py` (FROZEN shapes — implement with full `__post_init__` validation)

```python
@dataclass(frozen=True)
class ControllerConfig:
    # Defaults retuned 2026-07-09 (C1, DECISIONS §8; empirical sweep on the
    # calibrated twin — the original kp=8/ki=0.02/kt=6 gave Ti ~ 7 min, an
    # order of magnitude too aggressive for a tau = 3-6 h slab: +1.2 K
    # measured overshoot and a persistent +-0.6 K limit cycle).
    kp: float = 14.0
    ki: float = 0.0015                   # Ti = kp/ki ~ 2.6 h
    kd: float = 0.0
    kt: float = 12.0                     # trend-damping gain (%/(°C/h)), on the FILTERED trend
    deadband_c: float = 0.3
    valve_floor_pct: float = 15.0
    outdoor_ff_enabled: bool = False
    ff_neutral_c: float = 15.0           # FF shaping knobs (control-F6, 2026-07-09)
    ff_gain_pct_per_k: float = 1.0
    ff_max_pct: float = 20.0
    boost_offset_c: float = 1.0          # split engages beyond this |error|; must be > deadband_c (D2)
    fast_min_on_minutes: float = 10.0
    fast_min_off_minutes: float = 10.0
    fast_target_offset_k: float = 1.0    # ADDITIVE (2026-07-13): S12 boost overdrive, now a knob in [0,3]; 0 = split gets the plain setpoint
    dew_margin_k: float = 2.0            # gap at which the local throttle is FULLY OPEN
                                         # (K6 2026-07-12: the ramp ENDS here — the same design
                                         # gap the pump's global dew floor already guarantees)
    dew_ramp_k: float = 2.0             # ramp width BELOW dew_margin_k (K6: previously above)
    cooling_supply_base_c: float = 18.0  # ADDITIVE (2026-07-12, B2): heat-pump cooling water base, GLOBAL-only, [10,25]
    heating_supply_base_c: float = 26.0  # ADDITIVE (2026-07-12, B2): heating water base at ff_neutral_c, GLOBAL-only, [20,40]
    heating_supply_slope: float = 0.5    # ADDITIVE (2026-07-12, B2): heating curve slope, GLOBAL-only, [0,2]
    hp_flicker_band_k: float = 1.5       # ADDITIVE (2026-07-15, #7): flicker target cooling deadband, GLOBAL-only, [0.5,3.0]
    hp_flicker_stuck_minutes: float = 10.0   # ADDITIVE (2026-07-15, #7): stuck&armed time before a pulse, GLOBAL-only, [5,120]
    hp_flicker_min_off_minutes: float = 20.0  # ADDITIVE (2026-07-15, #7): forced-start cooldown, GLOBAL-only, [5,120]
    hp_flicker_max_starts_per_h: float = 2.0  # ADDITIVE (2026-07-15, #7): forced starts/rolling hour cap, GLOBAL-only, [1,6]
    flow_epsilon_k: float = 0.3          # ADDITIVE (2026-07-13, S6): min loop |ΔT| counted as flow, (0,3]
    flow_open_threshold_pct: float = 15.0  # ADDITIVE (2026-07-13, S6): valve cmd above which no-flow is evaluated, [0,100]
    flow_response_window_min: float = 45.0  # ADDITIVE (2026-07-13, S6): no-flow window, >0 (UI floor 30, 1440 disables)
    cycle_seconds: float = 300.0        # 5 min
    valve_write_threshold_pct: float = 5.0   # 2.0 -> 5.0 (K2b 2026-07-12: measured, 2 pp did
                                         # NOT bound kt's noise cost — 11.2 pp/h @ sigma 0.05;
                                         # 5 pp cuts it to 1.4 pp/h at zero regulation cost)
    # __post_init__: all gains >=0, 0<=valve_floor<=100, deadband>=0, margins>=0, cycle>0,
    # boost_offset_c > deadband_c, ff_neutral_c in [-30,40], ff_max_pct in [0,100],
    # fast_target_offset_k in [0,3], cooling_supply_base_c in [10,25], heating_supply_base_c in [20,40],
    # heating_supply_slope in [0,2], flow_epsilon_k > 0, 0<=flow_open_threshold_pct<=100, flow_response_window_min > 0,
    # hp_flicker_band_k in [0.5,3.0], hp_flicker_stuck_minutes in [5,120], hp_flicker_min_off_minutes in [5,120], hp_flicker_max_starts_per_h in [1,6]

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
    weather_comp: WeatherCompCurve | None = None    # twin heating supply curve (2026-07-09)
    cooling_comp: CoolingCompCurve | None = None    # twin cooling supply curve
    initial_temperature_c: float | None = None      # initial node temp (summer scenarios)
    setpoint_schedule: tuple[tuple[float, float], ...] = ()  # ADDITIVE (2026-07-12, K1):
                                         # (minute, home_setpoint_c) pairs, strictly increasing;
                                         # the harness re-applies setpoints from each minute on —
                                         # enables the night_setback operating-point gate
```
`RCParams`, `ModelOrder`, `RCModel` are **identical in spirit to pump-ahead** (blueprint §3 model.py):
3R3C `x=[T_air,T_slab,T_wall]`, SISO `u=[Q_floor]`, `d=[T_out,Q_sol,Q_int]`, ZOH via augmented matrix
`scipy.linalg.expm`. `WindowConfig`, `LoopGeometry`, `loop_power` (EN 1264) identical to blueprint.
`dew_point`, `cooling_throttle_factor` identical to blueprint dew_point.py. `WeatherPoint`,
`WeatherSource` protocol, `SyntheticWeather`, `ChannelProfile`, `ProfileKind`, `SensorNoise` identical
to blueprint. Reuse those shapes verbatim; do not reinvent.

---

## 7. Simulator — `simulator.py` (digital twin)

*(`simulated_room.py` removed 2026-07-09, D1 — it was a dead, diverging duplicate; the canonical
`SimulatedRoom` lives in `simulator.py`.)*

Mirror blueprint §4 closely, adapted to our contract:
- `class HeatPumpMode(Enum): HEATING; COOLING; OFF`.
- `SimulatedRoom(name, model: RCModel, *, n_loops=1, fast_source_power_w=0.0, q_int_w=0.0,
  windows=(), initial_temperature_c=None, loop_geometry: LoopGeometry)` — owns thermal state
  `_x=model.reset()` (or a uniform `initial_temperature_c`); `apply_actions(valve_pct,
  fast_source_power_w=0)`; `step_with_power(weather: WeatherPoint, q_floor_w, q_sol_w=0)`; props
  `T_air`, `T_slab`, `valve_position`, `state`, `windows`.
- `class BuildingSimulator(rooms, weather, *, hp_mode=HEATING, hp_max_power_w=None,
  sensor_noise=None, weather_comp=None, cooling_comp=None)`.
  - `get_all_measurements() -> dict[str, RoomInputs]` — **produces the SAME `RoomInputs` the HA
    coordinator builds**, so `BuildingController.step` is called identically in tests and in HA. Fill
    `room_temperature_c` (noised), `humidity_pct` (INDOOR humidity model, 2026-07-09: outdoor
    vapour pressure + a constant occupancy surplus of ~2 g/kg, evaluated against the room air —
    a credible per-room dew point instead of the old "indoor RH = outdoor RH" shortcut),
    `outdoor_temperature_c`, `loops` with realistic `supply/return/valve` (supply from
    weather-comp curve, return = supply - ΔT estimate, valve = last applied), `mode`,
    `hp_active_for_ufh=not is_cwu` (no CWU here -> True).
  - `step_all(actions: dict[str, RoomOutputs]) -> dict[str, RoomInputs]` — apply valve %, compute
    each room's **through-window solar gain** (2026-07-09, C7a: `q_sol = GHI * sum(area * g_value
    * f_orient(time-of-day))`, a diffuse share plus a cosine direct envelope per facade; the RC
    model splits it `f_slab/f_conv/f_rad = 0.5/0.3/0.2` — sun lands mostly on a UFH FLOOR),
    distribute finite HP power via `loop_power`, integrate each room's `RCModel.step` one tick,
    advance clock.
  - `set_cooling_supply_floor(floor_c)` (2026-07-09, I1) — the twin heat pump honours the
    controller's **global safe dew point** as its chilled-supply lower limit
    (`t_supply_cooling = max(curve/fallback, floor)`), closing the contract's third output in
    the simulated loop; the test harness feeds it back every cycle.
  - Provide single-room convenience `get_measurements()/step()` too.
- Noise corrupts only the measurement snapshot (`T_room`, and optionally supply), never physics.
- `T_slab` is ground-truth inside the sim and is **NOT** placed into `RoomInputs` (the controller must
  not see it) — but the log records it for metrics/plots.
- Plant calibration 2026-07-09 (S11, report-algo-thermal): `loop_power` uses the EN 1264
  characteristic `Q = K_H * A * dT_log^1.1` with the screed spreading resistance in series
  (`K_H` lands in the 4-7 W/m²K band); `building_profiles` reference values give
  h_floor ~ 10 W/m²K (`R_sf_ref=0.005`), sub-slab U ~ 0.18 W/m²K (`R_ins_ref=0.28`) with a
  SEASONAL ground temperature (winter ~14 °C, summer ~17 °C via the `t_ground` factory
  parameter), slab eigenmode tau ~ 4.4 h, and `C_air_ref = 300 kJ/K` (air + furnishings).

---

## 8. Metrics, log, scenarios, profiles

- `simulation_log.py`: `SimRecord(t, inputs: RoomInputs, outputs: RoomOutputs, weather: WeatherPoint,
  t_slab: float, room_name="", q_floor_w=None)` frozen with flat props (`T_room`, `T_slab`,
  `valve_pct`, `T_out`, ...); `q_floor_w` records the ALLOCATED floor power for the energy metric
  (D6, 2026-07-09).
  `SimulationLog` with append/append_from_step/len/iter/getitem/get_room(name)/time_range/to_dataframe.
- `metrics.py`: `SimMetrics` frozen (`comfort_pct, max_overshoot, max_undershoot, mean_deviation,
  fast_source_runtime_pct, energy_kwh, condensation_events, max_floor_temp, min_floor_temp,
  valve_travel_pct_per_h`) with
  `from_log(log, setpoint, *, comfort_band=0.5, ufh_nominal_power_w=None, dt_minutes=1)` and
  `compare(other)`. `energy_kwh` prefers the recorded allocation over the nominal-power estimate;
  `valve_travel_pct_per_h` is the actuator-wear proxy (D7). Assertion helpers (raise
  AssertionError w/ diagnostic): `assert_comfort`,
  `assert_floor_temp_safe(max_temp=34.0)`, `assert_no_condensation(margin=2.0)`,
  `assert_no_freezing(hard_min=16.0)`, `assert_no_prolonged_cold`,
  `assert_max_overshoot(max_overshoot=0.5)` (S13 — guards the primary anti-overshoot goal),
  `assert_valve_movement_moderate(max_travel_pct_per_h=30.0)`.
- `scenarios.py`: factory functions returning `SimScenario`, `SCENARIO_LIBRARY: dict[str, Callable]`.
  Include: `steady_heating`, `cold_snap` (with a realistic `WeatherCompCurve`), `solar_overshoot`,
  `spring_transition` (mode transitional), `hot_july_floor_cooling` (mode cooling, summer ground +
  moderate shaded GHI + realistic humidity -> the S2 throttle and the global dew limit genuinely
  modulate the loop), `night_setback` (ADDITIVE 2026-07-12, K1: 4 days of a 21<->19 degC schedule
  via `setpoint_schedule` on the bungalow — the operating-point gate: bounded heating-above-band
  integral, prompt post-setback close, bounded sag), `sensor_dropout`, `split_boost` (2.5 kW split
  boost on a single room).
  **ALL scenarios gate the merge** in `tests/simulation/test_scenarios.py`; `steady_heating` and
  `hot_july_floor_cooling` (B8, 2026-07-12) additionally run at BOTH dt = 60 s and the production
  300 s takt (2026-07-09, C7c/S11/S13). `cold_snap` also asserts the recovery overshoot
  (<= 0.5 K from 12 h after the weather step; K8 2026-07-12).
- `building_profiles.py`: factory functions returning `BuildingConfig`, `BUILDING_PROFILES` registry.
  Include a `modern_bungalow(t_ground=14.0)` multi-room reference (parterowy, ~13 UFH loops, HP
  ~4.9 kW, wylewka ~7 cm, lat 50.5/lon 19.5 — from the PRD reference house) and single-room
  parametric variants (`well_insulated`, `well_insulated_with_split`, `leaky_old_house`,
  `thin_screed`, `heavy_construction`), plus a `_make_3r3c_params(area_m2, ...)` helper. Use the
  CALIBRATED reference values (2026-07-09, §7 above: C_air 300 kJ/K, C_slab 3250 kJ/K per 80 mm,
  R_sf 0.005, R_ins 0.28, T_ground seasonal, solar split 0.5/0.3/0.2).

---

## 9. Home Assistant adapter contract

Mirror blueprint §2 exactly, with these tortoise-specific choices:

- **`manifest.json`**: `domain:"tortoise_ufh"`, `name:"Tortoise-UFH"`, `config_flow:true`,
  `integration_type:"hub"`, `iot_class:"local_polling"`, `loggers:["tortoise_ufh"]`,
  `requirements:["numpy>=1.26","scipy>=1.12"]`, `dependencies:["http"]`,
  `after_dependencies:["frontend"]`, `version` tracking the current release,
  `codeowners:["@hubertciebiada"]`,
  `documentation`/`issue_tracker` -> github `hubertciebiada/tortoise-ufh`.
- **`const.py`**: `DOMAIN`, `PLATFORMS=[NUMBER, SENSOR, BINARY_SENSOR, SELECT]`, `UPDATE_INTERVAL_MINUTES=5`,
  full `CONF_*` vocabulary (home_setpoint, mode entity, per-room: temp/humidity/outdoor/valves(list)/
  supply(list)/return(list)/fast_source entity+kind/offset/cooling_enabled),
  `WATCHDOG_TIMEOUT_MINUTES=15`, `WATCHDOG_RECOVERY_MINUTES=5`, unit sets (°C/%/W),
  `ENTITY_STALE_MAX_SECONDS=300`. **Canonical
  per-room control state (two-state since 2026-07-12, v0.7.0 — DECISIONS §13):** `CONF_ROOM_STATE`
  (options map `{room: state}`), `ROOM_STATE_OFF/LIVE`, `ROOM_STATES = [off, live]`,
  `DEFAULT_ROOM_STATE = "off"`. `CONF_LIVE_CONTROL` / `CONF_PARTICIPATES` / `ROOM_STATE_SHADOW` remain only so
  `async_migrate_entry` can read (v1) and convert (v2 -> v3) a legacy entry; the kill-switch key is retired (no constant).
  **Tuning:** `CONF_ROOM_TUNING` (options map `{room: {field: value}}` of *sparse* per-room
  `ControllerConfig` overrides; an emptied room override is pruned = "back to global") plus the
  single source of truth for the exposed knobs: `CONTROLLER_NUMBER_KNOBS` (`(field, min, max,
  step)` tuples), `CONTROLLER_BOOL_KNOB = "outdoor_ff_enabled"`, `CONTROLLER_KNOB_UNITS` —
  consumed by the config flow, the `get_tuning`/`set_tuning` range validation, and (via the
  `get_tuning` payload) the panel, so ranges are never duplicated in JS. `kd` is deliberately not
  exposed (Aneks §8.3); `cycle_seconds`/`valve_write_threshold_pct` are not user-facing.
- **`coordinator.py`** (`TortoiseUfhCoordinator(DataUpdateCoordinator)`, 5-min): builds one
  `BuildingController` from the **merged tuning** — global `ControllerConfig`
  (`entry.data[CONF_CONTROLLER]` overlaid by `entry.options[CONF_CONTROLLER]`) plus, per room, the
  sparse `entry.options[CONF_ROOM_TUNING]` override layered on top (`{**global, **override}`; an
  invalid override degrades that room to the global config with a warning). Each cycle READS
  source entity states (`_read_float_state` with stale cache like
  blueprint), assembles `dict[str, RoomInputs]`, calls `BuildingController.step`, stores typed payload
  (per-room outputs+report, global dew point, watchdog/status), and — for rooms whose control state is
  **`live`** — WRITES: valve dispatched by the entity's domain (`number.set_value` for `number`
  valves, `valve.set_valve_position` with an integer `position` for `valve`-domain actuators; all
  the room's valve
  entities, but only when the new value differs from last by >= `valve_write_threshold_pct`), split via
  `climate.set_hvac_mode` + `climate.set_temperature`. **Split command cache (S3, 2026-07-09):**
  an unchanged `(hvac_mode, target)` pair is NOT re-sent every cycle (mirror of
  `_last_written_valve`; no IR beeps, no stomping on manual louvre/fan tweaks) but is re-asserted
  after ~45 min so a missed write or manual override self-heals — the machine stays the owner.
  All writes `blocking=False`, try/except +
  `_LOGGER.exception` (`# noqa: BLE001`). Watchdog identical pattern to blueprint.
  Holds `_room_states: dict[str, str]` (seeded from `CONF_ROOM_STATE`); `get_room_state`/`set_room_state`
  (`@callback`, persists options WITHOUT reload — `options_require_reload` treats a state-only change as
  no-reload so the PID integrator survives). Derived: `participates := state != off` (→ `Mode.OFF` when
  off), `write := state == live`.
  **An `off` room is computed against `Mode.OFF` and reported but never written; only `live` writes.**
  **Measured dt:** the core `step` is fed the REAL elapsed time since the previous step
  (monotonic clock, clamped to `[1, 900]` s; nominal 300 s on the very first step) instead of a
  hard-coded 300 s, so an off-cycle recompute does not advance trend/dwell by a full cycle.
  **Debounced recompute:** a home-temperature / offset change immediately rebroadcasts the cached
  payload with fresh setpoints AND schedules a trailing full refresh through a ~2 s `Debouncer`
  (a burst of stepper clicks collapses to one recompute + re-write, notably of the split target).
  **Setpoint persistence:** the home temperature + per-room offsets are runtime state persisted
  to a private `Store` (`tortoise_ufh.setpoints.<entry_id>`, debounced save, restored on first
  refresh) — NOT to `entry.data`, which would reload the whole entry on every nudge. Per-room an
  optional `entity_hp_active` source entity may be configured; its on/off state feeds
  `hp_active_for_ufh` (integrator freeze during DHW/defrost), absent → `None`.
  **Input hardening (amendment 2026-07-09, C3+C4+S8 — fixed constants, deliberately NOT config
  knobs):** the room temperature passes a plausibility gate — range −10..50 °C plus a
  rate-of-change gate (a sample jumping > 4 K from the last accepted value is rejected like a
  missing reading; a consistent sample taken **at least ~one nominal cycle later** — 270 s, B5
  2026-07-12 — accepts the new level; a debounced recompute burst can no longer confirm a bogus
  spike within seconds). A present-but-frozen state is aged out via
  `last_reported`/`last_updated`: room temperature older than 45 min is treated as unavailable
  (WITHOUT the short cache fallback — the cache would hold the same stale value). **Humidity is
  TWO-stage (K7, 2026-07-12):** ≤ 60 min fresh; 60-120 min the LAST value is served with a
  linear staleness fraction (`RoomInputs.humidity_stale_frac` 0 → 1 across the window; D5,
  2026-07-12 — the core pads both protective dew points by `frac * 1 K` and flags
  `rh_stale_gated`) so a threshold-reporting RH sensor can neither limit-cycle the cooling nor
  step the throttle at the 60-min edge; > 120 min unavailable (conservative full stop). A value
  served from the short unavailable-entity cache carries the FULL fraction 1.0 (K7, R3): the
  pad must not vanish at the very moment the RH sensor dies. Valve feedback is validated per loop (an out-of-range
  reading nulls only that loop, never degrading the room to `sensor_lost`), and a LIVE room
  whose feedback diverges from the last written command by > 10 pp for 3 consecutive cycles gets
  a `valve_mismatch` report flag. When the measured step interval exceeds the 900 s dt clamp,
  the coordinator calls `BuildingController.invalidate_trends()` (R2-F6) so the clamped dt
  cannot inflate the next trend sample.
  **Multisplit groups (K4, 2026-07-12):** per-room optional `CONF_FAST_SOURCE_GROUP` (config
  flow room step; generic labels like `outdoor_unit_a`) is passed to the core as
  `RoomInputs.fast_source_group`; the fast-source read path also passes the raw climate state
  as `RoomInputs.fast_source_hvac_mode`, so the core reconciliation sees DIRECTION divergences.
  **Mode persistence (S9):** the global mode is persisted in the setpoint Store and restored on
  startup (a configured, available mode entity still wins), so a restart in July never falls
  back to heating logic.
  **Farewell command (C5):** on a room's `live → off` transition and on entry unload,
  one-shot safe parking of the released actuators: split **OFF** always; valve → **0** when the
  global mode is COOLING (an orphaned open valve would keep passing chilled water outside both
  dew defences), position left untouched in HEATING (warm water is bounded by the HP curve;
  holding keeps the house warm). *(K10, 2026-07-12.)* After the write the coordinator calls
  `BuildingController.notify_fast_source_farewell(room)` so the core machine mirrors the OFF
  (honest min-OFF on the way back to live), and a module-level farewell registry (surviving
  entry reloads) makes the read path treat an ON feedback younger than one cycle after the
  farewell as OFF (R5 — a stale pre-parking state must not be adopted and re-written as ON).
- **`number.py`**: `NumberEntity` for global **home temperature** (writable; range 5..30, step 0.5) and
  one per-room **offset** (writable; range -5..+5, step 0.5). `async_set_native_value` -> coordinator
  setter -> `async_set_updated_data`. These are the setpoint source of truth (config exposure decision).
- **`sensor.py`**: description-driven (`value_fn`) per-room diagnostics (keys: `recommended_valve`,
  `error_c`, `trend_c_per_h`, `i_term`, `trend_term`, `room_dew_point`, plus the text sensors
  `fast_source_mode` and `explanation`) all `EntityCategory.DIAGNOSTIC`; **one GLOBAL
  sensor `global_safe_dew_point`** (°C) = coordinator's `global_safe_dew_point_c` — the value the owner
  pipes to the HP; plus global `algorithm_status`, `last_update` (TIMESTAMP), `watchdog_status`.
- **`binary_sensor.py`**: per-room `sensor_lost`, `output_saturated`, `s2_condensation_active`.
  *(The derived per-room `live_control` binary sensor was retired in v0.5.0 — redundant with the
  control-state select; orphaned registry entries are swept on setup by
  `__init__._async_purge_retired_entities`, no entry-version bump.)* **`select.py`**: one per-room
  `control_state` select (`off` / `live`, `translation_key="control_state"`,
  `EntityCategory.CONFIG`); `async_select_option` → `coordinator.set_room_state`. This native select
  replaced the retired global kill-switch and per-room `live_control` switch (both purged by migration).
- **Devices & entity naming (v0.5.0, GH #3)**: helpers in **`device.py`** — every room is a device
  (`(DOMAIN, f"{entry_id}_{room_slug}")`, name = room name, model "Room zone", `via_device` → hub), plus
  one per-entry hub device (`(DOMAIN, entry_id)`, name "Tortoise-UFH", model "UFH controller") carrying
  the global entities. NO platform sets `_attr_name`; every entity is `has_entity_name = True` and named
  via its `translation_key` (full EN/PL parity in `strings.json` / `translations/{en,pl}.json`).
  **`unique_id` templates are frozen** (`{entry_id}[_{room_slug}]_{key}`) — upgrades keep entity ids via
  the registry; only new installs derive ids from device + translated name.
- **`config_flow.py`**: multi-step wizard with selectors (blueprint §2 pattern). Valve selector uses
  `EntitySelectorConfig(domain=["number", "valve"])` **multiple=True** (a room has 1..n valves;
  both `number`- and `valve`-domain actuators are accepted — see the coordinator's per-domain
  write dispatch); supply/return
  `domain=["sensor"], device_class=["temperature"]` (per loop, multiple); room temp sensor+temperature;
  humidity `device_class=["humidity"]`; fast source `domain=["climate"]`; global mode entity
  `domain=["select","input_select"]`. Rooms can be seeded from HA areas but manual add is fine. Config
  flow `VERSION = 3`; `async_migrate_entry` folds legacy v1 (`participates` + `live_control` +
  `kill_switch`) into `CONF_ROOM_STATE` (safety precedence: `participates == false` ⇒ `off`),
  purges the retired switch entities from the registry, and (v2 → v3, shadow removal
  2026-07-12 — DECISIONS §13) maps every state ∉ {`off`, `live`} to `off`; a v1 entry passes
  both blocks in ONE call. The **options flow is a menu** with four
  leaves: `add_room` / `edit_room` (name immutable) / `remove_room` (with entity-registry and
  setpoint-Store cleanup) / `settings` — the settings form carries the per-room control-state
  selects (off/live) + the advanced global controller knobs, merged over existing options
  so the panel-managed `CONF_ROOM_TUNING` map is preserved. Use `entity_validator.py` (unit-only,
  hardware-agnostic).
- **`websocket.py`**: register WS commands `tortoise_ufh/get_config` (global settings + per-room
  config views: offset, `control_state`, assigned entities, resolved diagnostic-sensor entity ids
  for the panel's history charts, and the global dew-point sensor's entity id),
  `tortoise_ufh/get_live`
  (returns `BuildingOutputs.to_dict()` + setpoints + per-room `control_state` + statuses),
  `tortoise_ufh/set_home_temperature`, `tortoise_ufh/set_room_offset`,
  `tortoise_ufh/set_room_state` (`{room, state ∈ ROOM_STATES}`), `tortoise_ufh/set_mode`,
  `tortoise_ufh/get_tuning` (knob descriptors with ranges/units from
  `CONTROLLER_NUMBER_KNOBS`, effective global values, sparse per-room overrides, library
  defaults) and `tortoise_ufh/set_tuning` (`{scope: "global"|room, values: {field: value}}`;
  range-checked, cross-validated by constructing a `ControllerConfig`, persisted to
  `entry.options[CONF_CONTROLLER]` / `[CONF_ROOM_TUNING]`; a `None` value clears a room's field
  override and an emptied room map is pruned; the write reloads the entry). The retired
  `set_room_enabled` / `set_kill_switch` commands do not exist.
  Each `@websocket_api.websocket_command` + `@callback`, admin-guarded.
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
- Header with global **home temperature** (editable -> `set_home_temperature` WS), **mode** selector
  (`set_mode`), the global safe dew point and algorithm/watchdog status. A whole-home stop is
  expressed as putting every room in `off` (no separate kill toggle).
- Four **tabs** (order authoritative for keyboard navigation): **Pokoje** (rooms table),
  **Strojenie** (tuning), **Zawory** (valves), **Wspomaganie** (fast-source assist).
- **Pokoje**: a table, one row per room, columns
  `[control-state] | Pokój | Pomiar | Zadana | Uchył | Zawór % | Zasilanie | Powrót | Tryb | Temp.`
  — the leading control is a two-button per-room **control-state** toggle (off / live
  -> `set_room_state` WS); `Pomiar` reads `report.room_temperature_c` (never reconstructed from
  `setpoint - error_c`); `Tryb`/`Temp.` show the fast-source command. Editable **offset** (->
  `set_room_offset`). Narrow layouts drop the least-critical columns.
- A per-room **detail drawer** showing the full decision **report** (the "okno do black-boxa",
  readable for human + AI), wiring, diagnostic entities and **history charts** fed by the HA
  recorder (`history/history_during_period` for short windows,
  `recorder/statistics_during_period` for 7 d) through the diagnostic-sensor entity ids resolved
  by `get_config`.
- **Strojenie**: rendered entirely from the `get_tuning` payload (knob descriptors + units +
  ranges, global values, sparse per-room overrides, defaults); edits go through `set_tuning`
  (global scope or per-room override; clearing a field reverts to global).
- Poll `get_live` every ~5 s (or subscribe if you add an event).
- Light/dark aware via `hass.themes`; degrade gracefully if a WS command errors (show message, never
  throw). Keep it dependency-free and defensive.

Also ship `dashboard_tortoise_ufh.yaml` as a Lovelace fallback (glance + history-graph cards).

---

## 11. Packaging, CI, tests

- `pyproject.toml`: like pump-ahead but `name="tortoise-ufh"`, deps `["numpy>=1.26","scipy>=1.12"]`
  (NO cvxpy/osqp — PID only), extras `viz=[matplotlib,pandas]`, `dev=[pytest,pytest-cov,ruff,mypy,
  pre-commit]`, `ha-test=[pytest-homeassistant-custom-component]` (HA-layer tests only).
  pytest markers `unit|simulation|slow`, `filterwarnings=["error"]`, `--strict-markers`.
  ruff select `["E","F","I","UP","B","SIM"]`, line 88. mypy strict, ignore `homeassistant.*`,
  `custom_components.*`, `scipy.*`, `pandas.*`, `matplotlib.*`.
- `hacs.json = {"name":"Tortoise-UFH","homeassistant":"2024.1.0","render_readme":true}`.
- CI: 5 jobs (ruff check+format, `mypy custom_components/tortoise_ufh/core`,
  `pytest tests/unit -m unit`, `pytest tests/simulation -m simulation`, and the optional HA layer
  `pytest tests/ha -m ha` installed via the `ha-test` extra) + `validate.yml`
  (hassfest + hacs/action).
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
+ Google docstrings; (6) no `homeassistant` import in `custom_components/tortoise_ufh/core/`.

Do not edit files you do not own. Do not add new runtime dependencies beyond numpy/scipy (core) and
Home Assistant (adapter). When in doubt, simpler + matches pump-ahead.
