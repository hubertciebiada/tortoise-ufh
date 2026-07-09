# Tortoise-UFH

**Per-room closed-loop climate control for high-thermal-mass underfloor heating in Home Assistant — the slow tortoise carries the load, the fast hare closes the gap.**

[![HACS Custom][hacs-badge]][hacs-url]
[![Home Assistant][ha-badge]][ha-url]
[![License: MIT][license-badge]][license-url]

[hacs-badge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg
[hacs-url]: https://hacs.xyz
[ha-badge]: https://img.shields.io/badge/Home%20Assistant-2024.1+-blue.svg
[ha-url]: https://www.home-assistant.io
[license-badge]: https://img.shields.io/badge/License-MIT-green.svg
[license-url]: LICENSE

---

## What it is

Tortoise-UFH is a Home Assistant custom integration that runs an independent, per-room
closed-loop controller for an underfloor-heating (UFH) house with a heavy concrete slab —
and, optionally, a fast source (split / AC) to assist. It supports both **heating** and
**floor cooling**.

The name is the fable:

- **The tortoise (żółw) = the slow UFH.** A 60–80 mm concrete slab has a thermal time
  constant of 4–6 hours. It is unhurried but it always reaches the setpoint, and it stores
  energy like a battery. Tortoise-UFH modulates each room's valve to steer that slab
  without overshoot.
- **The hare (zając) = the fast source.** A split/AC responds in minutes. Tortoise-UFH
  engages it only to *shorten the wait* for comfort when the room is well outside the band,
  and never lets it quietly become the primary source (anti priority-inversion). The split
  self-regulates — Tortoise-UFH commands mode and target temperature, never compressor power.

The controller is **PID-family** (PI with a trend-damping term plus optional weather
feedforward) — deliberately *not* MPC. There is no Kalman filter, no online model
identification, and no domestic-hot-water control. The core reacts robustly to what it can
measure and degrades safely when a sensor drops out.

The pure-Python control core (`custom_components/tortoise_ufh/core/`) never imports Home
Assistant, so the whole algorithm is unit- and simulation-testable offline. The rest of
`custom_components/tortoise_ufh/` is a thin Home Assistant adapter around it.

---

## The three outputs

Every 5-minute cycle, for the whole building, Tortoise-UFH produces exactly three kinds of
command:

1. **Per room — valve position, `0..100 %`.** One value per room/zone; every UFH loop in
   that room receives it. A valve floor keeps the slab gently active while heating; a
   per-room dew-point throttle closes the valve as the supply water approaches the room's
   dew point while cooling.
2. **Per room — fast-source command.** `ON` + direction (heating / cooling, per mode) +
   the room's target temperature. The split regulates itself around that target; minimum
   on/off runtimes are enforced so it does not short-cycle.
3. **Global — a single safe dew-point value (°C).** Computed as
   `max over cooled rooms( dew_point(T_room, humidity) ) + 2 K`. It is exposed as one sensor
   entity. **You feed this value to your heat pump as the cooling-supply lower limit**, so
   the pump never sends water colder than any room can tolerate without condensation. It
   reports `unknown` when no room is cooling or no humidity is available — the sidebar
   panel additionally explains per room *why* no value is produced.

Alongside the commands, each room emits a rich **report** ("a window into the black box"):
error, measured trend (°C/h), the individual PID / trend / feedforward terms, the raw valve
value before clamps, saturation and dew-throttle flags, and a short human- and AI-readable
explanation of what it did and why.

---

## Features

- **Per-room PI + trend control** — one independent controller per room; back-calculation
  anti-windup; a trend-damping term that tames the slab's inertia and prevents overshoot.
- **Slow + fast coordination** — UFH is always primary; the split only *adds* boost above a
  configurable offset and releases inside the comfort band. It never reduces the valve
  (no priority inversion) and never short-cycles (min on/off timers).
- **Heating and floor cooling** — with per-room Magnus dew-point calculation and a graduated
  supply-vs-dew-point valve throttle, plus the global safe dew-point limit for the pump.
- **Safe degradation** — a missing room-temperature sensor holds the last valve position and
  turns the fast source off, flagged in the report rather than failing.
- **Integrator freeze** — when the heat pump is unavailable for UFH (e.g. defrost), the
  integral term is frozen so it does not wind up against a dead actuator.
- **Optional weather feedforward** — a modest baseline valve term from outdoor temperature;
  the PI loop does the rest.
- **Per-room control state (off / shadow / live)** — one three-state select per room:
  *off* excludes it from control, *shadow* computes and reports without touching any
  actuator, *live* drives its hardware. A whole-house "hands off" is simply every room in
  off or shadow (see below).
- **Sidebar panel** — a dependency-free Home Assistant panel with Rooms / Tuning /
  Valves / Assist tabs: a live per-room table (control state, measured temperature,
  setpoint, error, valve %, supply/return water, mode), controller tuning (global gains
  plus sparse per-room overrides), and each room's full report.
- **Hardware-agnostic** — you map Home Assistant entities to roles at setup; units are
  validated (°C, %, W), brands are not.
- **Built-in building simulator** — a digital twin (3R3C RC model per room, ZOH via matrix
  exponential) that drives the *same* controller code as Home Assistant, so behaviour in
  tests matches behaviour in production.

---

## Installation

### Via HACS (custom repository)

1. In Home Assistant, open **HACS**.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/hubertciebiada/tortoise-ufh` with category **Integration**.
4. Search for **Tortoise-UFH** and install it.
5. **Restart Home Assistant.**
6. Go to **Settings → Devices & Services → Add Integration** and search for **Tortoise-UFH**.

### Manual installation

1. Download the latest release from GitHub.
2. Copy `custom_components/tortoise_ufh/` into your Home Assistant
   `config/custom_components/` directory.
3. Restart Home Assistant and add the integration through the UI.

Requires Home Assistant 2024.1.0 or newer and Python 3.12+. The core runtime dependencies
(`numpy`, `scipy`) are installed automatically by Home Assistant.

---

## Configuration

The setup wizard (Entity / Number / Select / Boolean selectors, hardware-agnostic unit
validation) first asks for the building location (used for weather compensation and
solar-gain feedforward), then maps your entities to roles: per room a temperature
sensor, one or more valve actuators (`number` or `valve` entities), optional
supply/return water sensors, optional humidity (required for cooled rooms), and an
optional split `climate` entity; globally an outdoor-temperature sensor and a mode
selector.

Day-to-day control lives in the **Tortoise-UFH sidebar panel** (added automatically, admin
only). From the panel you can:

- Set the **global home temperature** and pick the **mode** (heating / transitional /
  cooling / off).
- Adjust a **per-room offset** — each room's setpoint is `home temperature + room offset`.
- Set each room's **control state** (off / shadow / live). A room's participation in
  cooling is configured in the integration options, not the panel.
- Tune the **PI + trend controller** from the Tuning tab — global gains with sparse
  per-room overrides.
- Open any room's **live report** to read the full decision breakdown.

The global home temperature and per-room offsets are also exposed as writable `number`
entities, and the same actions are available as the `set_home_temperature`,
`set_room_offset` and `set_mode` services and over the panel WebSocket API. No YAML editing
is required. A `dashboard_tortoise_ufh.yaml` Lovelace template ships as a fallback.

---

## Control state: off / shadow / live (per room)

Tortoise-UFH is built for cautious rollout. It **always** computes commands and publishes
the full report — but whether those commands reach your actuators is gated by each room's
**control state**, exposed as a per-room `select` entity (`select.<room>_control_state`),
in the panel, and over the `tortoise_ufh/set_room_state` WebSocket command. A new room
starts in the safe **shadow** default.

- **`off`.** The room is excluded from control entirely — the core is fed `Mode.OFF`, so
  its valve is held at its last position and its fast source is idled. Nothing is written.
- **`shadow`.** The coordinator computes the valve and fast-source commands and shows them
  in the panel and diagnostic sensors, but writes nothing. Watch its recommendations
  against your existing controller for as long as you like, then promote rooms to live one
  at a time.
- **`live`.** The coordinator writes the room's valve entities (only when the new value
  differs from the last by at least the write threshold, to avoid actuator chatter) and the
  split's mode and target temperature.

A whole-house "hands off the hardware" is simply **every room in off or shadow** — the
three-state control replaced the earlier global kill-switch. Changing a room's control
state takes effect immediately and does **not** reset the PID integrator (only tuning
changes reload the controller).

In short: **a room in off or shadow → compute and report, but emit no commands; only live
rooms drive hardware.**

> Upgrading from an older install? The config entry migrates automatically (v1 → v2): the
> old `participates` flag, per-room `live_control` toggles and the global kill switch are
> folded into the new per-room control state (`participates == false` becomes `off`). The
> `switch.tortoise_ufh_kill_switch` and `switch.*_live_control` entities are removed and
> replaced by `select.*_control_state`.

---

## For developers

The integration is split into a pure control core — vendored inside the integration so a
HACS install is self-contained — and a thin Home Assistant adapter around it:

```
tortoise-ufh/
└── custom_components/tortoise_ufh/  # HA adapter — imports FROM the core via .core
    ├── coordinator.py               # 5-min DataUpdateCoordinator: read → run core → write
    ├── config_flow.py  panel.py  websocket.py  sensor.py  number.py  select.py ...
    ├── frontend/tortoise-ufh-panel.js
    └── core/                        # pure core — never imports homeassistant
        ├── models.py                # I/O dataclasses + RoomReport (the black-box contract)
        ├── config.py                # RoomConfig / BuildingConfig / ControllerConfig
        ├── pid.py                   # PIDController (PI + anti-windup)
        ├── controller.py            # RoomController + BuildingController
        ├── dew_point.py             # Magnus dew point + cooling throttle
        ├── rc_model.py              # 3R3C RC model (ZOH via scipy.linalg.expm)
        ├── simulator.py             # BuildingSimulator digital twin
        └── ...                      # weather, metrics, scenarios, profiles, safety
```

The one hard rule: **`custom_components/tortoise_ufh/core/` must never import
`homeassistant`.** The core depends only on `numpy` and `scipy` (plus the stdlib), ships
`py.typed`, and is fully testable without Home Assistant.

### Setup

```bash
git clone https://github.com/hubertciebiada/tortoise-ufh.git
cd tortoise-ufh
pip install -e ".[dev]"
```

### Tests

The suite uses three pytest markers: `unit` (fast, isolated), `simulation` (scenario-based,
end-to-end through the digital twin), and `slow`. Core tests need only
`numpy`, `scipy` and `pytest` — no Home Assistant install.

```bash
# Fast unit tests
python -m pytest -m unit

# Simulation scenarios
python -m pytest -m simulation

# Everything
python -m pytest
```

### Code quality

```bash
ruff check .
ruff format --check .
mypy custom_components/tortoise_ufh/core
```

The core is kept `mypy --strict` clean; ruff runs with `line-length = 88` and the
`E, F, I, UP, B, SIM` rule sets. Every value, config and result type is a frozen dataclass
that validates itself in `__post_init__`.

---

## License

Tortoise-UFH is released under the [MIT License](LICENSE).
