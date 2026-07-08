# DECISIONS — Tortoise-UFH locked decision log

> **Status:** FROZEN contract. This file is a standalone quick-reference mirror of
> **PRD `prd-control-brain.md` Aneks §8** (ULTRACODE interview, 2026-07-08).
> Where this log and the body of the PRD disagree, **§8 (and therefore this log) wins**
> — most notably: **floor cooling is in v1** (§8.4), which supersedes §3.3 and §6.
> Source of truth for *implementation* remains `docs/BUILD_SPEC.md`; this log records
> *why* the contract is shaped the way it is.

---

## 1. The 10 Q&A decisions

Each row distills one owner-interview question into its locked answer and points back to
the authoritative PRD clause. "Units" are stated wherever a value is implied.

| #  | Question (owner interview) | Locked decision | PRD |
|----|----------------------------|-----------------|-----|
| Q1 | How is the integration packaged and layered? | HACS custom integration `tortoise_ufh`. **Hard two-layer split:** pure-Python **core `tortoise_ufh/`** (numpy/scipy, `py.typed`, offline-testable) that **never imports `homeassistant`**, plus HA **adapter `custom_components/tortoise_ufh/`** (coordinator + entities + config flow + websocket + panel). Structural template: sibling `pump-ahead`. | §8.1 |
| Q2 | Where does configuration live and how does it survive restart? | Room config in the config entry (`entry.data` / `entry.options`) — **survives HA restart**. Global "home temperature" and per-room **offset** are writable `number` entities (room target = global + offset, °C). Per-room flags: **participation** and **cooling participation**. Entities are picked via domain/device-class-filtered dropdowns, **no uniqueness requirement**. | §8.2 |
| Q3 | What actuates the floor loop and what is the valve output? | **Proportional valves 0–100 %, holding a setpoint** — one position number per room (zone). **No PWM / TPI, no actuator cycling.** | §8.3 |
| Q4 | What control law runs in the black box? | **Single PI loop on `T_room` error (°C) + a trend term (`dT_room/dt`, K/s)** to damp overshoot (the main enemy under high thermal inertia). **No D-on-error term.** Anti-windup (back-calculation / clamp), **deadband**, and **valve-floor** (minimum opening while in heating readiness). No slab sensor; supply water assumed correctly conditioned by the heat pump (water side out of scope). Optional `T_out` feedforward. Freeze the integrator on DHW/defrost when the heat-pump state is available. **Control cycle: every 5 min** (adjustable); persist position on change ≥ threshold. | §8.3 |
| Q5 | Is floor cooling in scope for v1? | **Yes — floor cooling is in v1** (scope change vs §6). PI with **inverted sign**; valves are **modulated, not merely closed**. Split cooling is also supported. See the dew-point addendum in §2 below. | §8.4 |
| Q6 | How is the fast source (split) commanded and gated? | Command = **`ON + mode (heat/cool) + room temperature`** (target = global + offset, °C); the split self-regulates (compressor power untouched) via `climate.set_hvac_mode` + `climate.set_temperature`. **Engage when `\|T_room − target\|` > boost-offset (K, adjustable).** Compressor protection: **minimum ON and OFF times** (anti-short-cycle). **Anti priority-inversion:** the split never closes/holds the floor valve — floor stays the base; the split tops up above the band and backs off inside the comfort band. | §8.5 |
| Q7 | How are operating modes structured? | **One global house mode**: `heat / transitional / cool / off` (input entity). Per-room only: participation + cooling-participation + offset. **Transitional:** valves parked, split regulates bidirectionally. **Off:** room to rest, no commands issued. | §8.6 |
| Q8 | What are the outputs and the report contract? | **Three outputs.** (1) Per-room valve position 0–100 % (one per zone). (2) Per-room fast-source command (`ON + mode + room temp`). (3) **Global safe dew point `max_i(T_dew_i) + 2 K`** (°C, `sensor` entity) fed to the heat pump. Plus (4) a per-room **under-the-hood report**: error, trend, decision components, flags (saturation, sensor-loss, protection), human- and AI-readable "what and why". | §8.8 |
| Q9 | How is it deployed and validated for quality? | **Shadow / dry-run switch:** computes and logs the full report but **sends no commands**; then LIVE with per-room takeover. **Digital-twin simulator** modeled on `pump-ahead` (RC 3R3C, ZOH via `expm`; `BuildingSimulator` / `SimulatedRoom`, `SyntheticWeather`, `SensorNoise`, `SimMetrics` + assertions) for offline PID tuning and scenario tests — `T_slab` exists as simulator ground truth but is **never given to the controller**. Two-layer tests (unit TDD + simulation), `mypy --strict`, `ruff`, `filterwarnings=error`. | §8.9 |
| Q10 | What is explicitly out of v1? | MPC / horizon optimization / tariffs, online RC identification / model-learning, heat-pump and water-side control, floor sensor, ERV / CO₂ / free-cooling. **Floor cooling is NOT in this list — it was moved into v1** (§8.4). | §8.10 |

---

## 2. Addendum — floor cooling & two-layer dew-point protection (§8.4)

Floor cooling entering v1 pulls in a **hard anti-condensation guard**, implemented in
**two independent layers** (defense-in-depth):

- **Layer 1 — Global (primary, advisory to the heat pump).** The module computes a
  per-room dew point (**Magnus formula** from `RH` [%] + `T_room` [°C]), takes the
  **maximum over cooled rooms**, and **adds 2 K**, then **publishes that ready-to-use
  safe value** as a `sensor` entity (°C). The owner feeds it to the heat pump as the
  lower limit for chilled-water temperature. **The module does not control the water** —
  it only supplies the safe value.
- **Layer 2 — Local (secondary, per-room valve throttle).** If a room's **measured
  supply-water temperature** (coldest of its loops, °C) falls to `T_dew_room + 2 K`, the
  module **graduated-throttles** that room's cooling valve, and on exceedance **hard-closes**
  it — **independent of the heat pump**, with hysteresis (safety class S2).

**New measurement requirement:** **per-cooled-room `RH`** (a humidity `sensor` entity) —
an added input versus the original PRD.

---

## 3. Confirmation — split OFF on sensor loss (§8.7)

On **loss of the room sensor**, the safe-degradation contract is confirmed and binding:

- The **floor valve freezes at its last commanded position** (does not open or close blindly).
- The **fast source (split) is driven OFF** — not held, not left running.
- A **flag is raised in the per-room report** so the human/agent sees the degraded state.

Related guards (§8.7): a **watchdog** flips a room to fault/alarm when data is stale
> 15 min (recovery after 5 min of fresh data); floor is protected without a slab probe via
supply-water temperature plus conservative position ranges; the module is the **sole owner**
of participating rooms' valves and splits, with an external **kill-switch** (`off` = no
commands) and the heat pump / DHW as the water-side owner.
