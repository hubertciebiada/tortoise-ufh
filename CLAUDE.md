# CLAUDE.md — tortoise-ufh project memory

Per-room closed-loop climate controller for a high-thermal-mass underfloor-heating (UFH)
house, with fast-source (split/AC) assist. Heating **and floor cooling**. A HACS-installable
Home Assistant custom integration. Structurally a sibling of `pump-ahead`, but the controller
is **PID-family (PI + trend damping), not MPC**.

Authoritative sources, in order: `docs/BUILD_SPEC.md` (frozen contract — always wins),
`prd-control-brain.md` §8 Aneks (locked decisions), `docs/ALGORITHM_SPEC.md`,
`CONTROL_ALGORITHMS_REVIEW.md`. If this file conflicts with `BUILD_SPEC.md`, follow BUILD_SPEC.

---

## THE ONE HARD RULE

**`custom_components/tortoise_ufh/core/` (the pure core) MUST NEVER `import homeassistant`.**
It is pure Python (numpy/scipy + stdlib), ships `py.typed`, and is fully unit- and
simulation-testable offline. The core is **vendored inside the integration** so a HACS install
(which ships only `custom_components/tortoise_ufh/`) is self-contained, but it stays *logically
separate*: the HA adapter `custom_components/tortoise_ufh/` imports FROM the core via `.core`
and never the reverse; the core imports its own siblings **relatively** (`from .models import
...`) and nothing from the adapter. Because importing any core submodule first runs
`custom_components/tortoise_ufh/__init__.py`, that adapter `__init__` must ALSO stay importable
WITHOUT homeassistant — so every HA import there is lazy (deferred into function bodies or
`TYPE_CHECKING`), and it does not even import the HA-dependent `.const` at module top level.
Core talks to the outside only through plain frozen dataclasses and structural `Protocol`s
(e.g. `WeatherSource`). Any core file that does `import homeassistant` is a bug and is rejected.

---

## Architecture map (four layers)

- **Core** — `custom_components/tortoise_ufh/core/`. Pure library: RC thermal model,
  `PIDController`, `RoomController` (the per-room black box) + `BuildingController`
  (orchestrator), dew point, weather-comp feedforward, EN 1264 loop power, safety rules,
  metrics. No HA import, ever. Vendored inside the integration for a self-contained HACS
  install; imports its siblings relatively (`from .X import ...`).
- **Adapter** — `custom_components/tortoise_ufh/`. Thin HA shim: `TortoiseUfhCoordinator`
  (`DataUpdateCoordinator`, 5-min) reads source entity states, builds `dict[str, RoomInputs]`,
  calls `BuildingController.step`, and writes commands. Entities (number/sensor/binary_sensor/
  switch), config_flow, websocket, panel registration. Imports the core via `.core`; is
  imported by nothing.
- **Panel** — `custom_components/tortoise_ufh/frontend/tortoise-ufh-panel.js`. Self-contained
  vanilla-JS sidebar panel (no build step, no CDN imports — CSP). Renders the black-box report.
- **Simulator** — `custom_components/tortoise_ufh/core/simulator.py` (`BuildingSimulator` +
  `SimulatedRoom`). Digital twin for offline tests. Crucially, `get_all_measurements()`
  produces the SAME `RoomInputs` the
  coordinator builds, so `BuildingController.step` is exercised identically in tests and in HA.

Core module layout is frozen in BUILD_SPEC §2 — use the EXACT module paths, class names, and
signatures (`models.py`, `config.py`, `pid.py`, `controller.py`, `dew_point.py`, etc.). Do not
rename. The controller I/O contract lives in `models.py` and is frozen (implement exactly).

---

## Locked decisions (PRD §8 Aneks + BUILD_SPEC)

- **Controller = PI + trend damping.** Discrete PI with back-calculation anti-windup
  (`pid.py`). The "człon trendu" (`kt * trend_toward_setpoint`) is the key inertia/overshoot
  tamer for the high-mass floor. No MPC, no Kalman, no online RC identification, no D by default.
- **Valves are proportional, hold-position.** Output is a continuous `0..100 %` position, one
  value per room applied to all its loops. On missing room temp: SAFE DEGRADE = **freeze/hold
  last valve position**, fast source OFF, flag `"sensor_lost"` (never fail open/closed).
- **Floor cooling is in v1** with **two-layer dew-point protection**: (1) GLOBAL safe dew point =
  `max_over_cooled_rooms(dew_point(T_room, rh)) + 2 K`, exposed as a sensor for the heat pump's
  cooling-supply lower limit; (2) LOCAL per-room S2 valve throttle via `cooling_throttle_factor`
  (graduated 0..1, flags `"s2_condensation"` at 0). Cooling uses PI with inverted error sign.
- **Three outputs (external contract):** (1) per-room valve `0..100 %`; (2) per-room fast-source
  command = `ON + mode(direction) + room target temp` (the split self-regulates; we never touch
  compressor power); (3) global safe dew-point value (°C). Plus a rich per-room `RoomReport`.
- **Split = ON + mode + temp, anti priority-inversion.** The split decision NEVER reduces or
  holds the valve — the floor stays the base source. Split only *adds* boost above
  `boost_offset_c` and releases inside the comfort band. Respects min-ON / min-OFF timers.
- **Per-room control state (`RoomControlState = off | shadow | live`).** One canonical
  three-state per room (adapter `select` entity + panel + `set_room_state` WS), stored in
  `entry.options[CONF_ROOM_STATE]`. `off` ⇒ core sees `Mode.OFF` (valve held, fast source idle);
  `shadow` ⇒ compute + report but emit NO commands; `live` ⇒ compute, report AND write. A
  whole-home stop = every room `off`/`shadow`. This REVERSED the frozen global kill-switch +
  per-room live-control-boolean decision (config-entry migration v1→v2; see docs/DECISIONS.md).
- **Sensor loss ⇒ freeze valve + split off** (see valve rule above). Integrator freezes when
  `hp_active_for_ufh is False` (DHW/defrost).

---

## Tech stack

- Python **≥3.12**. Core runtime deps: **numpy≥1.26, scipy≥1.12** only (NO cvxpy/osqp — PID only).
  Adapter deps: **homeassistant** (2024.1+). Extras: `viz=[matplotlib,pandas]`,
  `dev=[pytest,pytest-cov,ruff,mypy,pre-commit]`.
- Packaging: setuptools, `pyproject.toml`. HACS via `hacs.json` + `manifest.json`
  (`requirements:["numpy>=1.26","scipy>=1.12"]`). MIT license — keep pyproject/LICENSE/README consistent.

---

## Coding conventions (non-negotiable)

- `from __future__ import annotations` at the top of every module. Full type hints everywhere.
  `mypy --strict` clean on `tortoise_ufh/`. Modern generics (`list[str]`, `X | None`),
  `Literal[...]` for closed string sets, `Protocol`/`@runtime_checkable`, `NDArray[np.float64]`.
- **Every value/config/result is a `@dataclass(frozen=True)`** with `__post_init__` validation
  raising `ValueError` (assign message to a local `msg` first: `msg = f"..."; raise ValueError(msg)`).
  Mutable defaults via `field(default_factory=...)`; `kw_only=True` for entity descriptions.
- Google-style docstrings (`Args:`/`Returns:`/`Raises:`); module docstrings state units.
- **Units (repo-wide):** temperature °C, power W, valve `0..100 %` float, R in K/W, C in J/K,
  GHI W/m², humidity `0..100 %`, time minutes (simulation) / seconds (`RCModel.dt`, real cycle).
  Bake units into names (`_c`, `_w`, `_pct`, `_minutes`, `t_supply`, `t_room`).
- Pure functions do not mutate inputs; return `.copy()` of arrays. Seeded
  `np.random.default_rng(seed)`, never global `np.random`. Matrix ops with `@`.
- Catch SPECIFIC exceptions in core; broad `except Exception:` only at HA/IO boundaries with
  `# noqa: BLE001` + `_LOGGER.exception(...)`. Fail-fast validation in constructors.
- ruff `line-length=88`, select `["E","F","I","UP","B","SIM"]`. Tests treat warnings as errors
  (`filterwarnings=["error"]`), markers `unit|simulation|slow`, `--strict-markers`.
- `RoomReport`/`RoomOutputs`/`BuildingOutputs` must be JSON-serializable (`to_dict()`, enums →
  `.value`) — the websocket and panel consume the dicts.

---

## Commands

```bash
python -m pytest -m unit          # fast unit suite (seed 42; numpy/scipy/pytest only, no HA)
python -m pytest -m simulation    # scenario/digital-twin suite (seed 12345; hard merge gate)
python -m mypy custom_components/tortoise_ufh/core   # strict typecheck of the core only
ruff check                        # lint (E,F,I,UP,B,SIM); add `ruff format --check` for style
```

Core tests must run with only numpy/scipy/pytest installed. HA-layer tests are optional and
skipped when `pytest_homeassistant_custom_component` is unavailable.

---

## When building a file

1. Read `docs/BUILD_SPEC.md` fully + `scratchpad/pump-ahead-blueprint.md` + PRD §8.
2. Read every core file your module imports to match REAL signatures.
3. Write EXACTLY your file(s); keep public signatures verbatim; no TODOs/stubs/`NotImplementedError`.
4. Do not edit files you don't own. Do not add runtime deps beyond numpy/scipy (core) or HA
   (adapter). When in doubt: simpler, and matches pump-ahead.
