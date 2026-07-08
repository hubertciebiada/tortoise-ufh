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

The pure-Python control core (`tortoise_ufh/`) never imports Home Assistant, so the whole
algorithm is unit- and simulation-testable offline. The Home Assistant adapter
(`custom_components/tortoise_ufh/`) is a thin layer on top.

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
   reports `unknown` when no room is cooling or no humidity is available.

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
- **Shadow mode and a global kill-switch** — compute and report without touching any
  actuator, per room or for the whole house (see below).
- **Sidebar panel** — a dependency-free Home Assistant panel to set the home temperature,
  per-room offsets, mode, participation and live/shadow toggles, and to inspect each room's
  live report.
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
validation) maps your entities to roles: per room a temperature sensor, one or more valve
`number` entities, optional supply/return water sensors, optional humidity (required for
cooled rooms), and an optional split `climate` entity; globally an outdoor-temperature
sensor and a mode selector.

Day-to-day control lives in the **Tortoise-UFH sidebar panel** (added automatically, admin
only). From the panel you can:

- Set the **global home temperature** and pick the **mode** (heating / transitional /
  cooling / off).
- Adjust a **per-room offset** — each room's setpoint is `home temperature + room offset`.
- Toggle a room's **participation** in cooling and its **live/shadow** state.
- Flip the global **kill-switch**.
- Open any room's **live report** to read the full decision breakdown as JSON.

The global home temperature and per-room offsets are also exposed as writable `number`
entities, and the same actions are available as the `set_home_temperature`,
`set_room_offset` and `set_mode` services and over the panel WebSocket API. No YAML editing
is required. A `dashboard_tortoise_ufh.yaml` Lovelace template ships as a fallback.

---

## Shadow vs live (the kill-switch)

Tortoise-UFH is built for cautious rollout. It **always** computes commands and publishes
the full report — but whether those commands reach your actuators is gated:

- **Shadow mode (per room).** A room's `live_control` toggle is off: the coordinator
  computes the valve and fast-source commands and shows them in the panel and diagnostic
  sensors, but writes nothing. Watch its recommendations against your existing controller
  for as long as you like, then enable live control one room at a time.
- **Live mode (per room).** With `live_control` on, the coordinator writes the room's valve
  entities (only when the new value differs from the last by at least the write threshold,
  to avoid actuator chatter) and the split's mode and target temperature.
- **Global kill-switch.** A single global switch that, when **on**, suppresses *all*
  command output regardless of per-room settings. Computation and reporting continue, so
  you keep full visibility while emitting nothing. Use it as an instant, house-wide "hands
  off the hardware".

In short: **kill-switch on, or a room in shadow → compute and report, but emit no
commands.**

---

## For developers

The repository is split into a pure control core and a thin Home Assistant adapter:

```
tortoise-ufh/
├── tortoise_ufh/                    # pure core — never imports homeassistant
│   ├── models.py                    # I/O dataclasses + RoomReport (the black-box contract)
│   ├── config.py                    # RoomConfig / BuildingConfig / ControllerConfig
│   ├── pid.py                       # PIDController (PI + anti-windup)
│   ├── controller.py                # RoomController + BuildingController
│   ├── dew_point.py                 # Magnus dew point + cooling throttle
│   ├── rc_model.py                  # 3R3C RC model (ZOH via scipy.linalg.expm)
│   ├── simulator.py                 # BuildingSimulator digital twin
│   └── ...                          # weather, metrics, scenarios, profiles, safety
└── custom_components/tortoise_ufh/  # HA adapter — imports FROM the core
    ├── coordinator.py               # 5-min DataUpdateCoordinator: read → run core → write
    ├── config_flow.py  panel.py  websocket.py  sensor.py  number.py  switch.py ...
    └── frontend/tortoise-ufh-panel.js
```

The one hard rule: **`tortoise_ufh/` must never import `homeassistant`.** The core depends
only on `numpy` and `scipy` (plus the stdlib), ships `py.typed`, and is fully testable
without Home Assistant.

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
mypy tortoise_ufh/
```

The core is kept `mypy --strict` clean; ruff runs with `line-length = 88` and the
`E, F, I, UP, B, SIM` rule sets. Every value, config and result type is a frozen dataclass
that validates itself in `__post_init__`.

---

## License

Tortoise-UFH is released under the [MIT License](LICENSE).
