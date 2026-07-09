# Tortoise-UFH — Control Algorithm Specification

> **Project:** `tortoise_ufh` — per-room closed-loop controller for a high-thermal-mass
> underfloor-heating (UFH) house, with fast-source (split) assist, heating **and** floor cooling.
> **Status:** Implementation spec — mirrors the real code of the pure core, vendored at
> `custom_components/tortoise_ufh/core/` (frozen contract). Module references of the form
> `tortoise_ufh.X` below are shorthand for `custom_components/tortoise_ufh/core/X`.
> **Family:** PID-family (**PI + trend damping**), *not* MPC. No Kalman, no online RC
> identification, no CWU control.
> **Structural sibling of** `pump-ahead`; this document is the tortoise analogue of
> `PumpAhead_Algorithm_Spec.md`.
>
> **Units (repo-wide, non-negotiable):** temperatures / setpoints / dew points °C; errors and
> trends in kelvin (`error_c` [K], trend [K/h]); valve/actuator position `0..100 %` (float, `_pct`);
> power W; R in K/W; C in J/K; GHI W/m²; humidity `0..100 %`; time in **minutes** (simulation) /
> **seconds** (`RCModel.dt`, `dt_seconds`, real-time cycle).
>
> **The one architectural rule:** the core (`custom_components/tortoise_ufh/core/`, vendored
> inside the integration so a HACS install is self-contained) **NEVER imports `homeassistant`**.
> It is pure numpy/scipy + stdlib, ships `py.typed`, and is fully offline-testable. The HA adapter
> `custom_components/tortoise_ufh/` imports *from* the core via `.core` and never the reverse;
> the core imports its siblings relatively. Core talks to the
> outside only through frozen dataclasses and structural `Protocol`s.

---

## 1. Problem definition

### 1.1 The general problem

Control a heat/cold source of **large thermal inertia** (water in a concrete screed, UFH) with
optional **fast convective assist** (air-to-air split) in selected rooms, so that each room holds
its target temperature in winter (heating) and summer (floor cooling).

**The slow source (UFH) is the priority.** Every room has UFH; not every room has a split. The
algorithm runs in both configurations, per room, fully independently.

| Source | Carrier | τ (time constant) | Role | Mandatory |
|--------|---------|-------------------|------|-----------|
| UFH (floor) | water in screed | **4–6 h** | base — slow, continuous, efficient | always |
| Split (air-to-air) | convection | **5–15 min** | boost — fast correction, costlier | optional per room |

The controller is **modular**: the core is per-room UFH regulation; fast-source coordination is an
optional layer activated per room (`fast_source_kind != NONE`). A room without a split still gets
the full benefit of PI + trend damping — it simply has no fast fallback when the slow loop lags.

### 1.2 Operating modes (one global house mode)

A single global house mode (`Mode` enum) drives every room; per room only the *control state*
(off / shadow / live — `off` maps to `Mode.OFF` in the core), *cooling participation* and *offset*
differ.

| `Mode` | Valve | Fast source |
|--------|-------|-------------|
| `HEATING` | PI-regulated `0..100 %`, holds setpoint | heating-only boost above `boost_offset_c` |
| `TRANSITIONAL` | **parked at 0** | bidirectional (heat below setpoint, cool above), split only |
| `COOLING` | PI-regulated (inverted sign) with dew-point throttle | cooling-only boost |
| `OFF` | 0, no regulation | OFF |

**Deadband:** when `|setpoint − T_room| ≤ deadband_c`, the PI error is zeroed (no integral growth)
and the split does not engage in any mode.

### 1.3 The cooling dew-point constraint

In floor cooling the cooled surface must never fall to the **dew point** or condensation ruins the
floor. This is the one case where **humidity is a critical measurement** — a humidity sensor is
required in every cooled room.

Magnus dew point (`tortoise_ufh.dew_point.dew_point`, coefficients `a = 17.625`, `b = 243.04`,
Alduchov & Eskridge 1996):

```
γ      = a·T_air/(b + T_air) + ln(RH/100)
T_dew  = b·γ / (a − γ)
```

Constraint: **T_surface ≥ T_dew + margin** (margin = 2 K). Enforced two ways (§7).

### 1.4 Cooling power asymmetry

Floor cooling has *less* power than floor heating (~30–40 W/m² vs ~50–80 W/m²) because of the
smaller ΔT between floor and air. The split at full power is unaffected. Consequence: in summer the
split is more active than in winter.

### 1.5 The pathology to avoid — priority inversion

If the split heats/cools a room within minutes → the thermostat is satisfied → the UFH valve closes
→ the screed loses temperature → the split runs non-stop as the *primary* source. COP collapses,
costs rise, the floor is dead.

**Countermeasure (Aneks §8.5, §8.7):** the floor is always the base and **is never closed just
because the split warmed/cooled the air**. The split only *adds* boost above `boost_offset_c` and
releases once inside the comfort band. The valve floor keeps the slow loop alive. See §8.

---

## 2. State of knowledge (condensed)

Coordinating a slow radiant source with a fast convective one is a mature HVAC problem: solved
commercially (Tekmar 557 — PWM on the slow source, stage 2 engages only at 100 % duty of stage 1;
Ekinex KNX — setpoint offset, fan-coil as auxiliary stage) and academically (hybridGEOTABS
two-layer MPC). Key takeaways relevant to a **PID-family** choice:

1. A **2nd-order model (2R2C) is enough** for control-grade prediction (Sourbron & Verhelst 2013);
   we use 3R3C in the simulator only, for a faithful digital twin.
2. **Simple heuristic rules approximate ~80 % of the optimum** — full MPC is not required to gain
   most of the benefit. Tortoise deliberately stops at PI + trend + feedforward.
3. **Overshoot is the main enemy** at high thermal mass — a reactive PI without inertia awareness
   overshoots badly; hence the explicit **trend-damping term** `−kt·(dT_room/dt)` (§5).
4. **Night setback is counterproductive** with a heavy screed.
5. The priority-inversion pathology (Tekmar Essay E006) is the central failure mode to design out.

Reference: `CONTROL_ALGORITHMS_REVIEW.md` (portable algorithm survey), which recommends the PID
family (cascade, anti-windup, feedforward/outdoor reset) as first-line for UFH.

---

## 3. Mathematical model (RC) — summary

The controller itself is **model-free** (it never sees `T_slab` and runs no RC model online). The
RC model lives only in the **simulator** (digital twin) as ground truth for offline PID tuning and
scenario tests. It is summarised here because it defines the plant the control law must tame.

### 3.1 RC network — electrical analogy

Temperature ↔ voltage, heat flow ↔ current, thermal resistance R [K/W] ↔ resistance, heat
capacity C [J/K] ↔ capacitor. Node equation:

```
C_j · dT_j/dt = Σ_h (T_h − T_j)/R_{h,j} + Q_j
```

### 3.2 3R3C minimal structure

State `x = [T_air, T_slab, T_wall]ᵀ`; SISO input `u = [Q_floor]`; disturbances
`d = [T_out, Q_sol, Q_int]ᵀ`.

```
C_air ·dT_air /dt = (T_slab−T_air)/R_sf + (T_wall−T_air)/R_wi + (T_out−T_air)/R_ve + Q_int + f_conv·Q_sol
C_slab·dT_slab/dt = (T_air−T_slab)/R_sf + (T_ground−T_slab)/R_ins + Q_floor
C_wall·dT_wall/dt = (T_air−T_wall)/R_wi + (T_out−T_wall)/R_wo + f_rad·Q_sol
```

The UFH heat `Q_floor` enters the **screed node only** — structurally far from the air node the
controller regulates. That distance (through `R_sf` and `C_slab`) *is* the inertia the trend term
fights.

### 3.3 Matrix form and discretisation

`ẋ = A_c x + B_c u + E_c d + b_c`, discretised by augmented-matrix ZOH via `scipy.linalg.expm`
(numerically stable for the stiff `C_slab/C_air ≈ 54:1` system):
`x[k+1] = A_d x + B_d u + E_d d + b_d`. Implemented in `tortoise_ufh.rc_model.RCModel`
(`ModelOrder.THREE`, SISO), used by `SimulatedRoom.step_with_power`.

### 3.4 Typical numeric values (20 m² room)

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `C_air` | ~60 kJ/K | air mass ρ·c·V |
| `C_slab` | ~3250 kJ/K | 80 mm screed (2300·880·0.08·20) |
| `C_wall` | 0.5–5 MJ/K | envelope mass |
| `R_sf` | ~0.01 K/W | floor→air convection |
| `R_ins` | 0.005–0.02 K/W | under-screed insulation |
| `R_ve` | 0.01–0.05 K/W | ventilation/infiltration |

`C_slab/C_air ≈ 54:1` — the source of the time-scale separation. Floor transfer function
`G_floor(s) ≈ K_f·e^(−θ·s)/[(τ₁s+1)(τ₂s+1)]` with `τ₁ ≈ 3–6 h`; split `G_conv(s) ≈ K_c/(τ_c s+1)`
with `τ_c ≈ 5–15 min`. Order (2nd vs 1st) and time-constant gap (hours vs minutes) are the
mathematical signature of the problem.

---

## 4. Control approaches — why PI + trend

Cascade PID (outer `T_room → T_slab`, inner `T_slab → valve`) is the classical answer but requires a
slab sensor and inverts the natural cascade (inner loop slower than outer). MPC (24 h horizon QP)
gives the best comfort/energy trade-off but needs an identified model, a state estimator and a
solver — explicitly **out of scope for v1** (Aneks §8.10).

Tortoise takes the pragmatic middle: **a single PI loop on the room-temperature error, plus an
explicit trend-damping term** that emulates the anticipatory value of derivative/predictive control
*without* differentiating the noisy error signal. This is the locked decision (Aneks §8.3): "single
PI loop on `T_room` error + trend term (`dT_room/dt`) damping overshoot; **no D term on the
error**; anti-windup; deadband; valve-floor."

---

## 5. The control law — PI + trend damping

Implemented in `tortoise_ufh.pid.PIDController` and `tortoise_ufh.controller.RoomController`. All
knobs come from `tortoise_ufh.config.ControllerConfig` (a frozen dataclass, `__post_init__`
`ValueError` validation). Defaults:

| Knob | Default | Unit | Role |
|------|---------|------|------|
| `kp` | 8.0 | %/K | proportional gain |
| `ki` | 0.02 | %/(K·s) | integral gain |
| `kd` | 0.0 | %·s/K | derivative (unused; **error D disabled**) |
| `kt` | 6.0 | %/(K/h) | **trend-damping gain** |
| `deadband_c` | 0.3 | K | comfort band |
| `valve_floor_pct` | 15.0 | % | minimum open when calling for heat |
| `outdoor_ff_enabled` | False | — | optional feedforward toggle |
| `boost_offset_c` | 1.0 | K | split engage threshold |
| `fast_min_on_minutes` | 10.0 | min | split min ON dwell |
| `fast_min_off_minutes` | 10.0 | min | split min OFF dwell |
| `dew_margin_k` | 2.0 | K | local S2 dew margin |
| `dew_ramp_k` | 2.0 | K | S2 graduated ramp width |
| `cycle_seconds` | 300.0 | s | control cycle (= PID `dt`) |
| `valve_write_threshold_pct` | 2.0 | % | HA write dead-zone |

### 5.1 Error sign convention

Report always uses the **heating convention** `error_c = setpoint − T_room`. Internally a
"need-more-actuation" error is derived so a positive error always means "open more":

```
heating:  error = setpoint − T_room ;  trend_toward = +dT_room/dt
cooling:  error = T_room − setpoint ;  trend_toward = −dT_room/dt
```

### 5.2 Trend estimate

```
dt_hours = dt_seconds / 3600
trend    = (T_room − T_room_prev) / dt_hours     # [K/h], 0 on the first call
```

`RoomController` stores `T_room_prev` between calls. `dt_seconds` is the *actual* elapsed time
passed to `step`: the HA coordinator feeds the measured interval since the previous control step
(monotonic clock, clamped to `[1, 900]` s; the nominal 300 s only on the very first step), so a
debounced off-cycle recompute — e.g. right after a setpoint change — does not distort the trend
or advance the fast-source dwell timers by a full cycle.

### 5.3 Deadband (sign-preserving magnitude reduction)

```
error_db = sign(error) · max(0, |error| − deadband_c)
```

Inside the band `error_db = 0`, so the PI integral does not grow and the valve trends toward its
floor (heating) or 0 (cooling).

### 5.4 Discrete PI with back-calculation anti-windup

`PIDController.compute(error_db, dt_seconds=dt_seconds, freeze_integrator=freeze)` (backward
Euler at the per-call `dt`; falls back to the configured `dt = cycle_seconds` when the caller
does not pass one):

```
P     = kp · e
I    += ki · e · dt          # skipped when freeze_integrator is True
D     = kd · (e − e_prev)/dt # 0 on first call; kd = 0 in v1 so D ≡ 0
u_raw = P + I + D
u     = clip(u_raw, output_min, output_max)     # [0, 100]
if ki > 0:  I += (u − u_raw)                     # back-calculation anti-windup
```

Validation (`__init__`): `kp, ki, kd ≥ 0`, `dt > 0`, `output_min < output_max`; `compute`
raises on a non-positive `dt_seconds`.

All time-dependent pieces share ONE time base: the PI integral, the trend (§5.2) and the
fast-source dwell timers (§8.2) all use the measured `dt_seconds` passed to
`RoomController.step` (the coordinator measures it monotonically and clamps to [1, 900] s).
This keeps the integral honest when steps are irregular — an immediate debounced recompute
~2 s after a setpoint change accumulates ~2 s of integral, not a full nominal 300 s cycle.

### 5.5 Integrator freeze (water-side awareness)

`freeze = (inputs.hp_active_for_ufh is False)` — during DHW/defrost the heat pump is not serving
UFH, so the integral must not wind up against an inactive source. P (and D) still act; anti-windup
back-calc still applies.

### 5.6 Trend damping — the inertia tamer

The key anti-overshoot member ("człon trendu"). When the room is moving *toward* setpoint the valve
is pre-emptively reduced in proportion to the rate of approach:

```
trend_damp = kt · max(0, trend_toward)     # only damp motion toward setpoint
trend_term = − trend_damp
valve      = pid_out + trend_term
```

Clamping `trend_toward` at 0 means the term never *adds* actuation when the room is drifting away —
it only bleeds off actuation as the heavy floor closes on the target, absorbing the pipeline of heat
already stored in the screed. This is the single most important overshoot control for a 4–6 h τ
plant.

### 5.7 Optional outdoor feedforward

If `outdoor_ff_enabled` and an outdoor temperature is present, a small bounded baseline is added
(`RoomController._feedforward`): heating → colder outside raises the baseline; cooling → hotter
outside raises it. Directional: `deviation = max(0, 15 °C − T_out)` in heating,
`max(0, T_out − 15 °C)` in cooling; `ff = min(20 %, 1.0 %/K · deviation)`.
The PI does the real work; this only shortens the transient. We never command supply temperature —
that is the heat pump's job.

### 5.8 Valve floor (heating only)

When heating and still calling for heat (`error_db > 0`):

```
if valve < valve_floor_pct:  valve = valve_floor_pct ;  valve_floor_applied = True
```

Keeps a trickle of flow through the loop (anti priority-inversion + keeps the screed charged). Not
applied in cooling/off/transitional or once satisfied.

### 5.9 Clamp and saturation

```
saturated = (valve ≤ 0) or (valve ≥ 100)      # measured on the pre-clamp value
valve     = clip(valve, 0, 100)
raw_valve_pct  = pre-floor / pre-dew / pre-clamp value   # recorded in the report
```

### 5.10 Worked step (heating)

`setpoint = 21`, `T_room = 20.6`, `T_room_prev = 20.4`, `dt = 300 s`, defaults:
`error = +0.4`; `trend = (20.6−20.4)/(300/3600) = +2.4 K/h`; `error_db = 0.4−0.3 = 0.1`;
`P = 8·0.1 = 0.8`; `trend_damp = 6·2.4 = 14.4` → `trend_term = −14.4`; `valve = pid_out − 14.4`,
then floored to `15 %`. The rapidly warming floor is throttled back toward the floor minimum well
before it reaches setpoint — overshoot avoided.

---

## 6. Per-room algorithm (the 15 steps)

`RoomController.step(inputs: RoomInputs, *, dt_seconds=300.0) -> RoomOutputs`. Order (matches the
code exactly):

1. **Missing room temp** (`room_temperature_c is None`) → safe degrade (§9): hold last valve, split
   OFF, flag `"sensor_lost"`, no PI.
2. **`Mode.OFF`** → valve 0, split OFF, report "off".
3. **`Mode.TRANSITIONAL`** → valve parked at 0; split only, bidirectional on error sign (subject to
   `boost_offset_c` and the dwell timers).
4. **Trend** (§5.2); store `T_room_prev`.
5. **Error** in need-more-actuation convention (§5.1).
6. **Deadband** (§5.3).
7. **Integrator freeze** (§5.5).
8. **PI compute** → `pid_out` (§5.4).
9. **Trend damping** (§5.6).
10. **Optional feedforward** (§5.7).
11. **Valve floor**, heating only (§5.8).
12. **Cooling local dew throttle (S2)** (§7.2).
13. **Clamp + saturation** (§5.9).
14. **Fast-source coordination** (§8).
15. **Build `RoomReport`** — every term filled + a concise human/AI explanation string.

Three refinements around the numbered list:

- **Cooling opt-out (between steps 4 and 5):** a `COOLING` room with `cooling_enabled=False`
  never runs the cooling PI — it returns early with the valve parked at 0, the fast source OFF
  and the flag `"cooling_disabled"` (an opted-out room must never receive chilled water, which
  would bypass both condensation defences of §7).
- **Hard-safety override (after step 15, every path incl. safe degrade):** the stateful
  `tortoise_ufh.safety.SafetyEvaluator` (rules S1–S5, per-rule hysteresis carried across cycles)
  is fed the governing loop supply (hottest loop in heating / coldest in cooling), room
  temperature and humidity; if any rule triggers, the highest-priority action overrides the
  computed valve / fast-source command and the rule names are merged into the report flags. The
  S5 watchdog age is fed as 0 — update staleness is owned by the HA adapter's watchdog (§9).
- **Additive report stamping (last):** `dew_excluded_reason` (from `classify_dew_eligibility`,
  §7.1) and `fast_dwell_remaining_s` (§8.2) are stamped onto the final post-safety report, so the
  dwell value reflects the final fast-source state (a safety force-off clears it).

`RoomOutputs = {valve_position_pct, fast_source: FastSourceCommand, report: RoomReport}`. All I/O
types are frozen dataclasses in `tortoise_ufh.models` with JSON `to_dict()` helpers (enums → their
`.value`) consumed by the HA websocket and panel. The report additionally echoes the measured
`room_temperature_c` (`None` on sensor loss) so consumers never reconstruct the measurement from
`setpoint − error_c`.

---

## 7. Cooling and two-layer dew-point protection

Floor cooling is in scope for v1 (Aneks §8.4). Cooling runs the same PI with inverted sign (§5.1);
condensation protection is **defense-in-depth**, two independent layers.

### 7.1 Layer 1 — global safe dew point (exposed value, primary)

Computed by `BuildingController.step`. Over rooms that are `COOLING`, `cooling_enabled`, and have a
usable `T_room` and `humidity_pct`:

```
T_dew_i             = dew_point(T_room_i, RH_i)                 # Magnus
global_safe_dew_c   = max_i(T_dew_i) + 2 K                       # None if no eligible room
```

Exposed as a global sensor entity (`BuildingOutputs.global_safe_dew_point_c`). **The module does not
control water** — the owner pipes this value to the heat pump as the cooling-supply lower limit.
Because it is a *maximum over rooms + fixed margin*, it never lowers on any single room's behalf.
Margin constant: `controller.GLOBAL_SAFE_DEW_MARGIN_K = 2.0`.

Eligibility is decided by the pure helper `controller.classify_dew_eligibility(RoomInputs)` — the
single source of truth with two consumers: `BuildingController._eligible_dew_point` (a `None`
return means "eligible, feeds the maximum") and `RoomController.step`, which records the result in
`RoomReport.dew_excluded_reason` (`None`, or one of `"not_cooling_mode"` / `"cooling_disabled"` /
`"no_temperature"` / `"no_humidity"`) so the panel can explain *why* the global safe dew point is
`None`.

### 7.2 Layer 2 — local S2 valve throttle (per room, secondary)

Independent of the heat pump. Runs only in `COOLING` with `cooling_enabled`
(`RoomController._cooling_throttle`):

```
T_dew        = dew_point(T_room, RH)
t_supply_min = min(loop.supply_temperature_c for loop in inputs.loops if present)
factor       = cooling_throttle_factor(t_supply_min, T_dew,
                                       margin=dew_margin_k, ramp=dew_ramp_k)   # ∈ [0, 1]
valve       *= factor
```

`cooling_throttle_factor` (in `dew_point.py`), with `gap = t_supply_min − T_dew`:

```
gap ≤ margin            → 0.0   (fully throttled, hard close; condensation risk)
gap ≥ margin + ramp     → 1.0   (fully open, safe)
otherwise               → (gap − margin)/ramp    (graduated linear ramp — the hysteresis band)
```

**Conservative on missing data:** if humidity is missing/non-positive *or* no loop supply reading is
available, `factor = 0.0` and the flag `"s2_condensation"` is raised — better a warm room than a wet
floor. `factor == 0` also raises the flag.

### 7.3 Why two layers

Layer 1 keeps the *whole* water circuit above the worst room's dew point (prevents the pump ever
delivering condensing water). Layer 2 protects a single room whose local humidity spiked (a shower,
a cracked window) even if the global limit was still nominally safe, and does so without waiting a
5-minute cycle for the pump to react. Either layer alone is sufficient for safety; together they are
belt-and-braces (Aneks §8.4). This is distinct from the independent hard-safety module
`tortoise_ufh.safety` (rules S1–S5), which is a separate last line of defence.

---

## 8. Fast-source coordination and anti priority-inversion

Only when `fast_source_kind != NONE`. Command shape (`FastSourceCommand`): `on` +
`mode ∈ {HEATING, COOLING, OFF}` + `target_temperature_c = setpoint`. We set the split's own
setpoint and mode (`climate.set_hvac_mode` + `climate.set_temperature`); we never touch compressor
power — the split self-regulates.

### 8.1 Engage / release (hysteresis)

`RoomController._want_fast(demand)` where `demand` is the need-more-actuation error in the mode's
direction:

```
if currently ON:  stay ON while  demand > deadband_c        (release inside comfort band)
else:             turn ON  when  demand > boost_offset_c    (engage only past the boost offset)
```

### 8.2 Min ON / min OFF (compressor protection)

`_decide_fast_source` advances an internal dwell timer by `dt_seconds` and permits a state change
only when the relevant minimum dwell (`fast_min_on_minutes` / `fast_min_off_minutes`) has elapsed;
otherwise the flag `"fast_source_min_runtime"` is raised and the previous state is held. Timers are
seeded large (`_INITIAL_FAST_TIMER_S`) so the very first transition is never blocked. The seconds
left on the *current* state's lock (min ON while running, min OFF while idle) are surfaced as
`RoomReport.fast_dwell_remaining_s` (`None` once elapsed, when there is no fast source, or after a
safety force-off); the panel renders it as "unlocks in ~N min".

### 8.3 Anti priority-inversion (the core invariant)

**The split decision NEVER reduces or holds the valve.** In the code the valve is fully computed
(steps 8–13) *before* and *independently of* the fast-source decision (step 14). The split only
*adds* boost above `boost_offset_c` and releases once inside the comfort band; the valve floor keeps
the slow loop charged. There is no path by which "the split satisfied the air" closes the floor —
structurally eliminating the Tekmar E006 pathology (§1.5). `_force_fast_off` bypasses the min-ON
timer for safety conditions (lost sensor, OFF mode).

---

## 9. Safe degradation

| Condition | Behaviour | Flag |
|-----------|-----------|------|
| Room temp lost (`None`) | **hold last valve position** (cold-start init is mode-aware: `valve_floor_pct` in heating, 0 in cooling/transitional/off), split **OFF**, no PI | `sensor_lost` |
| `Mode.OFF` | valve 0, split OFF | — |
| `COOLING` with `cooling_enabled=False` | valve 0 (never floor-cool an opted-out room), split OFF, no PI | `cooling_disabled` |
| Missing humidity / supply in cooling | S2 `factor → 0` (conservative close) | `s2_condensation` |
| Split change blocked by dwell timer | hold previous split state | `fast_source_min_runtime` |
| HEATER-kind fast source asked to cool | fast source forced OFF (a heater never cools) | `fast_source_cannot_cool` |
| Room has no controller (orchestrator) | valve 0, split OFF | `unknown_room` |
| Room controller raised | hold last valve, split OFF | `controller_error` |

`BuildingController.step` never raises on a single room: it catches `(ValueError, ArithmeticError)`
per room and substitutes a degraded `RoomOutputs`. A **watchdog** (HA adapter): no fresh data
> 15 min → emergency/alarm state in the report (recovery after 5 min). The module is the *sole
owner* of participating rooms' valves and splits; externally there is only the global mode, the
per-room control state (off / shadow / live), and the water-side owner (heat pump / DHW). A room in
**off** (core fed `Mode.OFF`, valve held) or **shadow** → compute + full report but emit **no**
commands; only a **live** room writes. A whole-home stop is every room off/shadow (the per-room
three-state replaced the earlier global kill-switch in v2). Floor protection without a slab sensor
relies on the supply-water temperature proxy (safety rules S1/S2) plus conservative valve ranges.

---

## 10. Simulator and test strategy

### 10.1 Digital twin (mirror of pump-ahead §4)

- `RCModel` (3R3C ZOH via `expm`) is ground truth; `SimulatedRoom` owns the thermal state
  `_x = model.reset()` and applies `valve_pct` + finite HP power; `BuildingSimulator` orchestrates
  time and distributes finite HP power via `ufh_loop.loop_power` (EN 1264, returns 0 on wrong
  gradient — Axiom-3-safe).
- `BuildingSimulator.get_all_measurements()` produces the **same `dict[str, RoomInputs]`** the HA
  coordinator builds, so `BuildingController.step` is called *identically* in tests and in HA.
- **`T_slab` is ground truth inside the sim and is NOT placed into `RoomInputs`** — the controller
  must not see it (Aneks §8.9). The log records it for metrics/plots only.
- `SensorNoise` (seeded `np.random.default_rng`) corrupts only the measurement snapshot (`T_room`,
  optionally supply), never the physics.

### 10.2 Three test layers

1. **Unit (TDD, `-m unit`, seed 42):** `PIDController` convergence and anti-windup; deadband
   sign-preservation; trend-damping arithmetic; `dew_point` vs psychrometric tables;
   `cooling_throttle_factor` boundaries (`gap ≤ margin` → 0, `≥ margin+ramp` → 1); every
   `__post_init__` `ValueError` (`pytest.raises(ValueError, match=...)`); safe-degrade holds the
   last valve.
2. **Simulation (`-m simulation`, seed 12345):** a session-scoped `run_scenario` harness returning
   `(SimulationLog, SimMetrics)`; parametrized scenarios calling `assert_*` per room.
3. **Shadow mode on the live system:** the coordinator computes and logs the full report but emits
   no commands until a room's control state is switched to `live` (Aneks §8.9 / §8.11).

### 10.3 Scenario library and acceptance metrics

`scenarios.py` factory functions + `SCENARIO_LIBRARY` registry:
`steady_heating`, `cold_snap`, `solar_overshoot`, `spring_transition` (transitional),
`hot_july_floor_cooling` (high humidity → exercises both dew-point layers), `sensor_dropout`.
`building_profiles.py`: `modern_bungalow()` (parterowy, ~13 UFH loops, HP ~4.9 kW, ~7 cm screed,
lat 50.5 / lon 19.5) + parametric single-room variants (`well_insulated`, `leaky_old_house`,
`thin_screed`, `heavy_construction`).

`metrics.SimMetrics.from_log` (single deterministic pass) + assertion helpers (raise
`AssertionError` with diagnostics): `assert_comfort`, `assert_floor_temp_safe(max_temp=34.0)`,
`assert_no_condensation(margin=2.0)`, `assert_no_freezing(hard_min=16.0)`,
`assert_no_prolonged_cold`. Split-specific pump-ahead assertions are dropped. A known-fail control
case (e.g. a badly under-powered leaky house in a cold snap) is wrapped in `pytest.raises`.

Acceptance targets: `steady_heating` → comfort > 95 %, split never engages; `cold_snap` →
`T_room ≥ setpoint − 1.5 K`; `hot_july_floor_cooling` → zero condensation events, comfort > 90 %.

---

## 11. Out of scope for v1

MPC / horizon optimisation / dynamic tariffs; online RC identification / model learning; heat-pump
and water-side control; a physical slab sensor; recuperator / CO₂ / free-cooling; CWU (DHW)
scheduling. (Floor cooling was moved *into* v1 — Aneks §8.4.) The trend-damping term deliberately
substitutes for the anticipatory value MPC would provide, at a fraction of the complexity.

---

## Changelog

| Date | Change |
|------|--------|
| 2026-07-08 | Created from BUILD_SPEC + PRD Aneks §8 + `CONTROL_ALGORITHMS_REVIEW.md`; mirrors real `controller.py` / `pid.py` / `dew_point.py` signatures. |
| 2026-07-09 | Aligned with v0.3.x code: vendored-core paths; measured `dt_seconds` (coordinator clamp [1, 900] s) now drives ALL time-dependent terms — the PI integral included (`compute(..., dt_seconds=...)`, fixing double integration on debounced recomputes) — alongside trend and dwell; cooling opt-out early return; safety override + additive report stamping (`dew_excluded_reason` via `classify_dew_eligibility`, `fast_dwell_remaining_s`, `room_temperature_c`); directional feedforward formula; degradation-table rows (mode-aware sensor-lost hold, `cooling_disabled`, `fast_source_cannot_cool`). |
