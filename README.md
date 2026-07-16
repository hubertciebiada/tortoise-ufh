<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="custom_components/tortoise_ufh/brand/logo-dark.png">
    <img src="custom_components/tortoise_ufh/brand/logo.png" alt="Tortoise-UFH — under floor heating, simplified" width="360">
  </picture>
</p>

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

> **Language / Język / Sprache:** **English** (this page) · [Polski](docs/manual/pl.md) · [Deutsch](README.de.md)
>
> The full end-user manual (installation, configuration, the sidebar panel, tuning and the flag
> dictionary) is maintained per language in [`docs/manual/`](docs/manual/) —
> [Polski](docs/manual/pl.md) · [Deutsch](docs/manual/de.md). This README is the English overview.

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

   **The `unknown` contract — the consumer must be fail-safe.** `unknown` means "no room is
   eligible for the calculation right now"; it does **not** mean "no condensation risk". Any
   automation piping this sensor into the heat pump must treat `unknown` conservatively:
   hold a fixed safe lower limit (e.g. 18–19 °C) or stop floor cooling — never "no limit".
   A reference automation (generic entity names):

   ```yaml
   alias: "UFH cooling: safe supply lower limit"
   triggers:
     - trigger: state
       entity_id: sensor.tortoise_ufh_global_safe_dew_point
   actions:
     - choose:
         - conditions:
             - condition: template
               value_template: >-
                 {{ states('sensor.tortoise_ufh_global_safe_dew_point')
                    not in ['unknown', 'unavailable'] }}
           sequence:
             - action: number.set_value
               target:
                 entity_id: number.heat_pump_cool_supply_min
               data:
                 value: "{{ states('sensor.tortoise_ufh_global_safe_dew_point') }}"
       default:
         # Fail-safe: no eligible room -> conservative fixed limit.
         - action: number.set_value
           target:
             entity_id: number.heat_pump_cool_supply_min
           data:
             value: 19
   ```

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
- **Safe degradation** — a missing room-temperature sensor holds the last healthy valve
  position while heating (and parks the valve at 0 while cooling, where a frozen-open valve
  would bypass both condensation defences), turns the fast source off, and flags the room in
  the report rather than failing. Inputs are plausibility-checked (range, rate-of-change,
  state age) before the controller ever sees them.
- **Integrator freeze** — when the heat pump is unavailable for UFH (e.g. defrost), the
  integral term is frozen so it does not wind up against a dead actuator.
- **Optional weather feedforward** — a modest baseline valve term from outdoor temperature;
  the PI loop does the rest.
- **Optional heat-pump link & quiet hours** — an opt-in link can steer the heat pump's mode
  and cooling/heating water setpoints (always honouring domestic-hot-water priority), and each
  room can restrict its fast source to an allowed time window. An opt-in **dry assist** engages
  a room's split in its dry mode when the dew point climbs past a comfort threshold — the floor
  cools only sensibly, so the split's latent capacity is the system's only dehumidifier, and
  drying the air also lets the cooling water drop, restoring floor capacity on muggy days. An optional, Panasonic-specific
  **force cooling start** (a one-cycle setpoint drop) can trip the compressor out of its fixed
  3 K return-water deadband when rooms still call for cooling, for colder average water at a
  dew-safe return — but only when the calling rooms' loop-weighted valve opening clears a
  configurable demand gate, so a draw small enough for the buffer tank never forces a start.
- **Per-room control state (off / live)** — one two-state select per room:
  *off* excludes it from control (the core idles it and nothing is ever written),
  *live* drives its hardware. A whole-house "hands off" is simply every room in
  off (see below).
- **Sidebar panel** — a dependency-free Home Assistant panel with six tabs (Rooms, Flags,
  Tuning, Valves, Assist, Heat pump): a live per-room table (control state, measured
  temperature, setpoint, error, valve %, supply/return water, mode), a flag annunciator,
  controller tuning (global gains plus sparse per-room overrides), the optional heat-pump
  link, and each room's full report.
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

### Floor-cooling hardware recommendations

Both software condensation layers (the global safe dew point and the per-room throttle)
ultimately trust your **humidity sensors** — a stale or drifting RH reading is their shared
failure mode. Before enabling floor cooling:

- **Fit a hardware condensation (pipe dew) sensor on the manifold** — a cheap
  normally-closed sensor strapped to the coldest supply pipe, wired to stop the cooling
  circulation directly. It is an independent third protection layer that works even when
  every software layer is fed bad data.
- **Insulate the manifold and any exposed chilled-water pipework.** Low-thermal-mass parts
  (manifold beam, fittings) reach water temperature in minutes and are the first place dew
  forms during a humidity spike — long before the slab is at risk.
- **Mount loop supply probes on the manifold beam** (before the loop valves), not after
  them: a probe downstream of a closed valve reads stagnant, room-warmed water, which can
  make the dew throttle oscillate open/closed against its own measurement.
- Give every cooled room a real humidity sensor. A room without RH is cooled **blind** and
  the controller will conservatively refuse to open its valve (throttle 0).
- **Check your RH sensors' reporting cadence** before the cooling season. Threshold-reporting
  sensors (e.g. a CO₂/RH combo over Matter) may legitimately stay silent for tens of minutes
  in a stable room. The controller tolerates this with a two-stage age gate: a reading is
  fresh up to 60 min; between 60 and 120 min the last value is still used with an extra
  **+1 K dew-point margin** (report flag `rh_stale_gated`); past 120 min the room's cooling
  stops conservatively. If you see `rh_stale_gated` regularly, shorten the sensor's
  max-report interval.

### Alerting on degraded rooms

The building payload carries `sensor_lost_rooms` — the number of rooms currently running in
the degraded `sensor_lost` state (visible in the panel and via the `tortoise_ufh/get_live`
websocket, deliberately not another entity). For per-room alerting, watch each room's
report `flags` (websocket/panel) or simply alert on your source temperature sensors going
`unavailable`/stale — the controller degrades a room whenever its temperature stops being
trustworthy (out-of-range, jumping, or older than ~45 min).

---

## Configuration

The setup wizard (Entity / Number / Select / Boolean selectors, hardware-agnostic unit
validation) first asks for the building location (used for weather compensation and
solar-gain feedforward), then maps your entities to roles: per room a temperature
sensor, one or more valve actuators (`number` or `valve` entities), optional
supply/return water sensors, optional humidity (required for cooled rooms), and an
optional split `climate` entity; globally an outdoor-temperature sensor and a mode
selector.

**Multisplit owners:** if several rooms' indoor units share one physical outdoor unit,
give them the same **shared outdoor unit group** label in the room step (any name, e.g.
`outdoor_unit_a`). The controller then never asks one aggregate to heat and cool at the
same time: when demands conflict (routine in the transitional season), the room furthest
outside its comfort band wins the direction, a unit inside its minimum-ON time keeps it,
and the losing room waits its minimum-OFF time with the `fast_source_group_conflict` flag
visible in its report. Leave the field empty for independent units.

Day-to-day control lives in the **Tortoise-UFH sidebar panel** (added automatically, admin
only). From the panel you can:

- Set the **global home temperature** and pick the **mode** (heating / transitional /
  cooling / off).
- Adjust a **per-room offset** — each room's setpoint is `home temperature + room offset`.
- Set each room's **control state** (off / live). A room's participation in
  cooling is configured in the integration options, not the panel.
- Tune the **PI + trend controller** from the Tuning tab — global gains with sparse
  per-room overrides.
- Open any room's **live report** to read the full decision breakdown.

The global home temperature and per-room offsets are also exposed as writable `number`
entities, and the same actions are available as the `set_home_temperature`,
`set_room_offset` and `set_mode` services and over the panel WebSocket API. No YAML editing
is required. A `dashboard_tortoise_ufh.yaml` Lovelace template ships as a fallback.

### Devices and entity naming

Since v0.5.0 every room is a **device** in Home Assistant (model "Room zone", linked to a
per-entry "UFH controller" hub device that carries the global entities), so rooms can be
assigned to HA **areas** and browsed on their device page. Entities are named through
Home Assistant's translation system (`has_entity_name` + `translation_key`), so entity
names follow your HA language (English and Polish ship with the integration).

Upgrading installations keep their existing entity ids — entities are matched by their
unchanged `unique_id` in the registry. **New** installations derive entity ids from the
device + translated-name convention, so a few ids differ from pre-0.5.0 installs (e.g.
`sensor.salon_dew_point` instead of `sensor.salon_room_dew_point`); resolve entities via
the UI pickers rather than hard-coding pre-0.5.0 ids.

---

## Control state: off / live (per room)

Whether Tortoise-UFH touches a room's actuators is gated by that room's **control
state**, exposed as a per-room `select` entity (`select.<room>_control_state`),
in the panel, and over the `tortoise_ufh/set_room_state` WebSocket command. A new room
starts in the safe **off** default — nothing is written until you deliberately switch it
to **live**.

- **`off`.** The room is excluded from control — the core is fed `Mode.OFF`, so its
  report shows valve 0 % and the fast source idle, and **nothing is written**: the
  physical actuators stay exactly as you left them. Switching a live room off first sends
  a one-shot farewell (split OFF; the valve is driven to 0 in cooling and left holding in
  heating).
- **`live`.** The coordinator writes the room's valve entities (only when the new value
  differs from the last by at least the write threshold, to avoid actuator chatter) and the
  split's mode and target temperature.

> **Manual overrides & the 45-minute re-assert.** In `live`, Tortoise-UFH is the OWNER of
> the room's split: an unchanged command is not re-sent every cycle (no beeps, your fan and
> louvre tweaks survive), but the mode + target pair is **re-asserted roughly every
> 45 minutes**, so a manual change of the split's mode or target from its remote will be
> overwritten within that window (the report raises `fast_source_mismatch` in the
> meantime). If you want to drive a room's hardware by hand for a while, **switch the room
> to `off`** — that is the supported "manual mode": the controller writes nothing, and the
> way back to `live` re-parks the actuators safely (the split passes an honest
> minimum-OFF before any restart).

A whole-house "hands off the hardware" is simply **every room off** — the per-room control
state replaced the earlier global kill-switch. Changing a room's control state takes
effect immediately and does **not** reset the PID integrator (only tuning changes reload
the controller).

In short: **an off room → report only, no commands; only live rooms drive hardware.**

> **v0.7.0 removed the third `shadow` state** (compute in the real mode but write
> nothing — a dry-run rollout aid). The config entry migrates automatically (v2 → v3):
> rooms in `shadow` become `off`. Note the deliberate reporting change: a shadow room used
> to show "what it would do" in its sensors and panel row; an `off` room reports the idle
> state (valve 0 %, fast source off) instead — this is the intended behaviour, not broken
> sensors. See `docs/DECISIONS.md` §13.

> Upgrading from an older install? The config entry migrates automatically (v1 → v3 in
> one step): the old `participates` flag, per-room `live_control` toggles and the global
> kill switch are folded into the per-room control state (`participates == false` becomes
> `off`; a room that was not `live` lands on `off`). The
> `switch.tortoise_ufh_kill_switch` and `switch.*_live_control` entities are removed and
> replaced by `select.*_control_state`. v0.5.0 additionally retires the redundant
> `binary_sensor.*_live_control` (it merely mirrored `control_state == "live"`; orphaned
> registry entries are cleaned up on setup) and drops the transitional
> `live_control_enabled` field from the `tortoise_ufh/get_live` websocket payload — read
> `control_state` instead.

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
