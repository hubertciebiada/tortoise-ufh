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
| `kp` | 14.0 | %/K | proportional gain (retuned 2026-07-09, C1) |
| `ki` | 0.0015 | %/(K·s) | integral gain — Ti = kp/ki ≈ 2.6 h (retuned 2026-07-09) |
| `kd` | 0.0 | %·s/K | derivative (unused; **error D disabled**) |
| `kt` | 12.0 | %/(K/h) | **trend-damping gain**, applied to the FILTERED trend |
| `deadband_c` | 0.3 | K | comfort band |
| `valve_floor_pct` | 15.0 | % | minimum open when calling for heat |
| `outdoor_ff_enabled` | False | — | optional feedforward toggle |
| `ff_neutral_c` | 15.0 | °C | FF neutral outdoor temperature (knob since 2026-07-09) |
| `ff_gain_pct_per_k` | 1.0 | %/K | FF gain |
| `ff_max_pct` | 20.0 | % | FF cap |
| `boost_offset_c` | 1.0 | K | split engage threshold (must be > `deadband_c`) |
| `fast_min_on_minutes` | 10.0 | min | split min ON dwell |
| `fast_min_off_minutes` | 10.0 | min | split min OFF dwell |
| `dew_margin_k` | 2.0 | K | local S2 dew margin |
| `dew_ramp_k` | 2.0 | K | S2 graduated ramp width |
| `cycle_seconds` | 300.0 | s | control cycle (= PID `dt`) |
| `valve_write_threshold_pct` | 2.0 | % | HA write dead-zone |

> **Why the 2026-07-09 retune (C1, DECISIONS §8):** the original `ki = 0.02` meant
> Ti ≈ 7 min against a τ = 3–6 h slab — the integrator saturated during every approach and the
> measured closed-loop result was **+1.2 K overshoot with a ±0.6 K limit cycle**. The new
> defaults were chosen by an empirical sweep on the CALIBRATED digital twin (solar wired, EN
> 1264^1.1 plant, realistic ground): overshoot ≤ +0.2 K, 24–48 h tail 100 % inside ±0.3 K,
> ~1 pp/h valve travel. Full sweep table in `docs/DECISIONS.md` §8.

### 5.1 Error sign convention

Report always uses the **heating convention** `error_c = setpoint − T_room`. Internally a
"need-more-actuation" error is derived so a positive error always means "open more":

```
heating:  error = setpoint − T_room ;  trend_toward = +dT_room/dt
cooling:  error = T_room − setpoint ;  trend_toward = −dT_room/dt
```

### 5.2 Trend estimate — FILTERED (S10, 2026-07-09)

```
# accumulate elapsed time; take a raw sample only once >= 60 s has passed
raw   = (T_room − T_room_at_last_sample) / accumulated_hours    # [K/h]
alpha = 1 − exp(−accumulated_dt / 900 s)                         # EMA, tau = 15 min
trend = trend + alpha · (raw − trend)                            # 0 on the first call
```

`RoomController` stores the temperature at the last accepted sample and the filtered trend
between calls. Two protections (S10): **(1) the 60 s floor** — a debounced recompute ~2 s after
a setpoint change HOLDS the previous filtered value instead of dividing a 0.1 K sensor tick by
2 s (a fictitious 180 K/h); shorter intervals accumulate until the floor is reached; **(2) the
15 min EMA** — σ = 0.1 K of sensor noise creates raw trend noise on the order of the true
signal, and unfiltered it converted the (deliberately one-sided) damping term into actuator
wear and a downward bias. Sensor loss resets the filter (a gap invalidates the trend).
`dt_seconds` is the *actual* elapsed time passed to `step` (monotonic, clamped `[1, 900]` s).

### 5.3 Deadband (sign-preserving magnitude reduction)

```
error_db = sign(error) · max(0, |error| − deadband_c)
```

Inside the band `error_db = 0`, so the PI integral does not grow — and the valve **rests at the
accumulated integral** (plus trend/FF terms). It does NOT decay toward the floor/0: the resting
integral is the steady-state heat demand, and bleeding it off inside the band would saw-tooth
the room (control-F7 clarification, 2026-07-09).

### 5.4 Discrete PI with back-calculation anti-windup

`PIDController.compute(error_db, dt_seconds=dt_seconds, freeze_integrator=freeze)` (backward
Euler at the per-call `dt`; falls back to the configured `dt = cycle_seconds` when the caller
does not pass one):

```
P     = kp · e
rate  = ki · unwind_factor if e·I < 0 else ki   # asymmetric unwind (K1, 2026-07-12)
I    += rate · e · dt        # skipped when freeze_integrator is True
D     = kd · (e − e_prev)/dt # 0 on first call; kd = 0 in v1 so D ≡ 0
u_raw = P + I + D
u     = clip(u_raw, output_min, output_max)     # [0, 100]
if ki > 0:  I += (u − u_raw)                     # back-calculation anti-windup
```

Validation (`__init__`): `kp, ki, kd ≥ 0`, `dt > 0`, `output_min < output_max`,
`unwind_factor ≥ 1`; `compute` raises on a non-positive `dt_seconds`.

**Asymmetric unwind (K1, 2026-07-12):** while the (deadbanded) error OPPOSES the accumulated
integral — stale knowledge from a previous operating point, e.g. a heating integral above a
freshly lowered setpoint — the integral discharges `unwind_factor` (8 in the room controller)
times faster than it accumulated. The accelerated step only ever pulls `I` toward zero, and
inside the comfort band `e = 0`, so equilibrium and steady-state behaviour are untouched.

**Bumpless setpoint transfer (K1, 2026-07-12):** a change of the effective setpoint by `dK`
between PID-active cycles in the same mode calls `pid.shift_integral(kp · d_err)`
(`d_err = dK` heating / `−dK` cooling, clamped to the output range): the integral — the loop's
memory of the operating point — moves WITH the setpoint instead of discharging the difference
at `ki` speed. Measured on the twin (23 → 21 °C at a saturated integral): active heating of an
already-too-warm room fell from 17.4 h (642 %·h) to 0.8 h (23 %·h), the return to the band got
FASTER (35 h vs 49 h), at the cost of a deeper coast-down trough (−0.92 K vs −0.54 K).

All time-dependent pieces share ONE time base: the PI integral, the trend (§5.2) and the
fast-source dwell timers (§8.2) all use the measured `dt_seconds` passed to
`RoomController.step` (the coordinator measures it monotonically and clamps to [1, 900] s).
This keeps the integral honest when steps are irregular — an immediate debounced recompute
~2 s after a setpoint change accumulates ~2 s of integral, not a full nominal 300 s cycle.

### 5.5 Integrator freeze + seasonal hygiene (extended 2026-07-09, S1/S2)

`freeze = (inputs.hp_active_for_ufh is False) or (COOLING and dew_factor < 1.0)`:

* **DHW/defrost** — the heat pump is not serving UFH, so the integral must not wind up against
  an inactive source.
* **Active S2 dew throttle (S1/dew-F2)** — the throttle multiplies the valve AFTER the PI,
  invisibly to the back-calculation anti-windup; without the freeze, hours of throttled cooling
  banked an integral that slammed the valve to ~100 % the moment the humidity cleared. The
  factor is computed before the PI (it depends only on inputs) and applied after it.

P (and D) still act; anti-windup back-calc still applies. Two further hygiene rules (S2):
a **HEATING↔COOLING transition resets the integrator** (the error convention flips — one
season's integral is anti-knowledge for the other), and **> 12 h of accumulated inactivity**
(OFF / TRANSITIONAL / cooling opt-out / sensor lost) clears it, so the last winter integral is
never the first cooling command.

*(K9, 2026-07-12.)* The throttle-freeze was re-examined against a back-calculation from the
FINAL (throttled) valve (`I += u_final − u_raw` per throttled cycle) and the freeze STAYS: any
tracking anti-windup enforces `u_raw ≈ u_final`, pinning the integral at ~0 for the whole
episode, so the measured post-release catch-up was not faster (±0.3 K after 8.6 h either way
following 6 h of full throttle; 9.5 h — worse — for a partial throttle), and under a persistent
partial throttle the tracking variant suppresses the legitimate integral entirely. The catch-up
time is a `ki`-speed property (the integral must honestly rebuild), not a windup artifact.

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
outside raises it. Directional: `deviation = max(0, ff_neutral_c − T_out)` in heating,
`max(0, T_out − ff_neutral_c)` in cooling; `ff = min(ff_max_pct, ff_gain_pct_per_k · deviation)`.
The shaping constants are `ControllerConfig` knobs since 2026-07-09 (control-F6; previously
module constants). The PI does the real work; this only shortens the transient. We never command
supply temperature — that is the heat pump's job.

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

`setpoint = 21`, `T_room = 20.6`, sustained climb `+2.4 K/h` (filtered trend has converged),
`dt = 300 s`, current defaults (`kp = 14`, `kt = 12`):
`error = +0.4`; `error_db = 0.4−0.3 = 0.1`; `P = 14·0.1 = 1.4`;
`trend_damp = 12·2.4 = 28.8` → `trend_term = −28.8`; `valve = pid_out − 28.8`, clamped ≥ 0 and
then floored to `15 %` (still calling for heat). The rapidly warming floor is throttled back
toward the floor minimum well before it reaches setpoint.

---

## 6. Per-room algorithm (the 15 steps)

`RoomController.step(inputs: RoomInputs, *, dt_seconds=300.0) -> RoomOutputs`. Order (matches the
code exactly):

1. **Missing room temp** (`room_temperature_c is None`) → safe degrade (§9): HEATING holds the
   last valve; COOLING/TRANSITIONAL/OFF park the valve at 0 (2026-07-09 — freeze-open in cooling
   would bypass both condensation defences); split OFF, flag `"sensor_lost"`, no PI.
2. **`Mode.OFF`** → valve 0, split OFF, report "off".
3. **`Mode.TRANSITIONAL`** → valve parked at 0; split only, bidirectional on error sign (subject to
   `boost_offset_c` and the dwell timers).
4. **Filtered trend** (§5.2, S10 2026-07-09): raw sample only after ≥ 60 s accumulated, then a
   15 min EMA; a fast recompute holds the previous value.
5. **Error** in need-more-actuation convention (§5.1); a HEATING↔COOLING transition resets the
   integrator here (§5.5).
6. **Deadband** (§5.3).
7. **Integrator freeze** (§5.5): DHW/defrost OR an active S2 dew throttle (`dew_factor < 1`,
   computed here from the inputs, applied in step 12).
8. **PI compute** → `pid_out` (§5.4).
9. **Trend damping** (§5.6).
10. **Optional feedforward** (§5.7).
11. **Valve floor**, heating only (§5.8).
12. **Cooling local dew throttle (S2)** (§7.2) — applies the factor computed in step 7.
13. **Clamp + saturation** (§5.9). A zero produced solely by the S2 throttle does NOT set
    `saturated` (control-F8, 2026-07-09): `saturated` means "the PI hit a 0/100 bound";
    `dew_throttle_factor` carries the condensation story.
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
  temperature, humidity AND the per-room data age (`RoomInputs.last_update_age_minutes`,
  adapter-supplied — S6 2026-07-09); the water side and the air side are decided independently
  across the active rules and merged into the report flags. `FALLBACK_HP_CURVE` (S5) alone
  commands the NEUTRAL position — `valve_floor_pct` in heating, 0 in cooling — deferring to the
  heat pump's own curve; the adapter's building-level watchdog stays report-only (§9).
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

`cooling_throttle_factor` (in `dew_point.py`), with `gap = t_supply_min − T_dew` and
`lo = max(0, margin − ramp)` *(semantics REVISED 2026-07-12, K6 — owner decision "tylko pompa
+2"; see DECISIONS §11)*:

```
gap ≥ margin            → 1.0   (fully open — full cooling exactly on the pump's dew floor)
gap ≤ lo                → 0.0   (fully throttled; with the defaults: supply at the room's dew)
otherwise               → (gap − lo)/(margin − lo)   (graduated linear ramp BELOW the margin)
```

The heat pump's global `dew_max + 2 K` supply floor (Layer 1) is the system's ONE working
margin; the local ramp now ENDS at that design gap instead of stacking a second margin above
it. Before this revision the ramp spanned `(margin, margin + ramp)`: the most humid room — the
one defining the pump floor — sat at `gap = margin` with `factor = 0`, and `hot_july` measured
"fasadowe" cooling (valves open 29.7 % of records, living room ~2.7 K above the setpoint);
after the change the valves are open 94.2 % of records with the minimum slab-dew margin still
at +1.51 K. A STALE humidity reading (held 60-120 min, `RoomInputs.humidity_stale`, K7) pads
the effective dew point by +1 K and flags `"rh_stale_gated"`.

**Conservative on missing data:** if humidity is missing/non-positive *or* no loop supply reading is
available, `factor = 0.0` and the flag `"s2_throttle"` is raised — better a warm room than a wet
floor. `factor == 0` also raises the flag. (Renamed from `"s2_condensation"` 2026-07-12, B7 —
that name now belongs exclusively to the independent hard-safety rule, which itself moved BELOW
the ramp: `S2_HARD_MARGIN_K = 0`, trip at `supply < dew`, clear at `gap > +1 K` — a backstop
behind a backstop.)

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
`mode ∈ {HEATING, COOLING, OFF}` + `target_temperature_c` (S12, 2026-07-09: `setpoint + 1 K`
while HEATING / `setpoint - 1 K` while COOLING — `FAST_TARGET_OFFSET_K`; exactly `setpoint` in
TRANSITIONAL). We set the split's own setpoint and mode (`climate.set_hvac_mode` +
`climate.set_temperature`); we never touch compressor power — the split self-regulates. The
adapter caches the last written `(hvac_mode, target)` per entity and re-sends only on change or
after a ~45-min re-assert (S3).

### 8.1 Engage / release (hysteresis) + the direction machine (C6, 2026-07-09)

The direction is **machine state** (`_fast_state ∈ {OFF, HEATING, COOLING}`), never a per-cycle
computation. `RoomController._want_fast(demand, engaged=...)` where `demand` is the
need-more-actuation error in the requested direction:

```
if engaged in THIS direction:  stay ON while  demand > deadband_c   (release inside comfort band)
else:                          turn ON  when  demand > boost_offset_c
```

Transitions: `OFF → direction` requires the full min-OFF; `running → OFF` (requested OFF **or the
opposite direction**) requires the full min-ON — a HEATING↔COOLING reversal is only reachable
through OFF with the full min-OFF dwell (indoor units may share a multisplit outdoor unit). A
blocked request re-emits the REMEMBERED direction. In TRANSITIONAL a running split releases only
past the FAR edge of the comfort band (`demand < -deadband_c`) — while ON it self-regulates at
`target = setpoint`, which removes the old below-setpoint bias band (S12).

### 8.2 Min ON / min OFF (compressor protection) + physical sync (S4)

`_decide_fast_source` advances an internal dwell timer by `dt_seconds` and permits a state change
only when the relevant minimum dwell (`fast_min_on_minutes` / `fast_min_off_minutes`) has elapsed;
otherwise the flag `"fast_source_min_runtime"` is raised and the previous state is held. The timer
also accumulates on sensor-lost/OFF forced paths (fast-F6), so a long outage counts toward the
min-OFF wait. Rooms without a physical feedback (`fast_source_on is None`) seed the timer large
(`_INITIAL_FAST_TIMER_S`) so the very first transition is never blocked; the FIRST observed
feedback wins over the machine (running unit adopted as ON, stopped as OFF) and re-seeds the timer
conservatively to 0 — a full dwell after every restart/reload, so a restart loop cannot
short-cycle a compressor. Later feedback disagreeing with the previous cycle's command raises the
additive `"fast_source_mismatch"` flag. The seconds
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
| Room temp lost (`None`) | HEATING: **hold last valve position** of healthy regulation (cold-start init `valve_floor_pct`); COOLING/TRANSITIONAL/OFF: **valve 0** — never freeze-open in cooling (2026-07-09, both condensation layers need `T_room`); split **OFF**, no PI | `sensor_lost` |
| `Mode.OFF` | valve 0, split OFF | — |
| `COOLING` with `cooling_enabled=False` | valve 0 (never floor-cool an opted-out room), split OFF, no PI | `cooling_disabled` |
| Missing humidity / supply in cooling | S2 `factor → 0` (conservative close) | `s2_throttle` |
| Humidity held 60-120 min old (K7, 2026-07-12) | effective dew point +1 K in both layers | `rh_stale_gated` |
| Split change blocked by dwell timer | hold previous split state | `fast_source_min_runtime` |
| Split lost the multisplit group arbitration (K4, 2026-07-12) | fast OFF (honest min-OFF before re-engaging) | `fast_source_group_conflict` |
| HEATER-kind fast source asked to cool | fast source forced OFF (a heater never cools) | `fast_source_cannot_cool` |
| Room has no controller (orchestrator) | valve 0, split OFF | `unknown_room` |
| Room controller raised | HEATING: hold last valve; COOLING/TRANSITIONAL/OFF: valve 0 (K5, 2026-07-12 — a crashed controller computes neither condensation defence); split OFF | `controller_error` |
| Per-room data age > 15 min (S5, 2026-07-09) | **neutral position**: `valve_floor_pct` in heating / 0 in cooling (defer to the HP curve), split OFF; clears below 5 min | `s5_watchdog` |

`BuildingController.step` never raises on a single room: it catches `(ValueError, ArithmeticError)`
per room and substitutes a degraded `RoomOutputs`; it also counts the currently degraded rooms
into `BuildingOutputs.sensor_lost_rooms` (2026-07-09, safety-F13 — a building-level staleness
counter surfaced via websocket, not a new entity). A **watchdog** (HA adapter): no fresh data
> 15 min → emergency/alarm state in the report (recovery after 5 min); report-only — the
per-room actuator escalation belongs to S5 above. The module is the *sole
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

1. **Unit (TDD, `-m unit`, seed 42):** `PIDController` convergence and anti-windup (plus the K1
   `shift_integral` / `unwind_factor` contracts); deadband sign-preservation; trend-damping
   arithmetic (incl. the kt sign canary — see §10.3 note); `dew_point` vs psychrometric tables;
   `cooling_throttle_factor` boundaries (K6: `gap ≥ margin` → 1, `≤ max(0, margin−ramp)` → 0);
   the multisplit group arbiter and the farewell sync; every `__post_init__` `ValueError`
   (`pytest.raises(ValueError, match=...)`); safe-degrade holds the last valve.
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
`T_room ≥ setpoint − 1.5 K` + recovery overshoot ≤ 0.5 K from 12 h after the step (K8);
`hot_july_floor_cooling` → zero condensation events, valves genuinely open (> 60 % of records
after K6); `night_setback` (K1) → bounded heating-above-band integral, prompt post-setback
close, bounded sag.

**kt measurement note (K2, 2026-07-12; DECISIONS §11):** after honest attempts no scenario on
the calibrated twin measurably contrasts `kt = 12` vs `kt = 0` (every peak-overshoot delta
≤ 0.03 K — solar gains dominate with valves closed; the anti-overshoot of the defaults is
carried by the small `ki` and, since K1, the bumpless transfer + unwind). `kt` stays per the
frozen trend-member decision; the trend term's SIGN and magnitude are pinned by a unit canary
(`trend_term == −kt · filtered_trend`, damping only on approach), because a sign regression
would pass the whole simulation gate unnoticed.

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
| 2026-07-09 | Phase A safety hardening (DECISIONS §6): sensor-lost safe-degrade is mode-dependent (COOLING parks the valve at 0, never freeze-open; HEATING keeps the freeze); the safety override decides the water side and the air side independently (S1/S2 close the valve without silencing an active S3/S4 fast source), syncs the fast-source dwell machine on force-ON, and never poisons the sensor-lost hold (`_last_valve_pct` keeps the last healthy position). Adapter: room-temperature plausibility gate (−10..50 °C, > 4 K/cycle held for a 2-sample confirmation), state-age gate (temp 45 min / RH 60 min ⇒ unavailable), per-loop valve-feedback validation + `valve_mismatch` flag, farewell command on live→shadow/off + unload, persisted global mode. |
| 2026-07-09 | Phase B fast-source direction machine (DECISIONS §7): three-state `OFF/HEATING/COOLING` machine — direction change only through OFF with the full min-OFF, min-ON hold re-emits the REMEMBERED direction (`_fallback_mode` deleted); physical `fast_source_on` consumed (first feedback wins, conservative 0-seeded dwell after restart, `fast_source_mismatch` flag); split targets `setpoint ± 1 K` in active modes (S12) and exactly `setpoint` in TRANSITIONAL with far-edge release (bias removed); adapter split-command cache + ~45 min re-assert (S3); `boost_offset_c > deadband_c` validation (D2); dwell accumulates on sensor-lost/OFF paths (fast-F6). |
| 2026-07-09 | Phases C+D+E (DECISIONS §8): retuned defaults kp=14/ki=0.0015/kt=12 (empirical sweep on the CALIBRATED twin; old ki=0.02 measured +1.2 K overshoot); FILTERED trend (>= 60 s sample floor + 15 min EMA); integrator frozen under an active S2 throttle, reset on HEATING<->COOLING, decayed after > 12 h inactivity; saturated no longer set by an S2 zero; FF constants -> ControllerConfig knobs; S5 watchdog LIVE (adapter-fed per-room data age, neutral-position action); BuildingOutputs.sensor_lost_rooms; simulator: solar wired (f_slab row), seasonal ground, EN 1264^1.1 plant with screed resistance, indoor-humidity model, cooling supply floored by the global safe dew point, split_boost scenario, ALL scenarios gate the merge with the S13 overshoot assertion. |
| 2026-07-12 | Round-2 review (DECISIONS §11): K1 bumpless setpoint transfer (`shift_integral(kp·dK)`, mode-correct sign) + asymmetric integrator unwind (`unwind_factor = 8`) with the `night_setback` gate scenario (`SimScenario.setpoint_schedule`); K6 margin de-stacking — the local throttle ramp ENDS at `dew_margin_k` (full cooling on the pump's dew floor; hard S2 at the dew point itself); K3 CLOSE_VALVE is water-side only (the air-side decision stands); K4 multisplit group arbiter (`fast_source_group`, one direction per aggregate, direction-aware S4 mismatch via `fast_source_hvac_mode`); K5 mode-aware `controller_error` degrade; K7 two-stage RH staleness (+1 K dew pad, `rh_stale_gated`); K8 cold-snap recovery assertion; K9 throttle-freeze retained (back-calc from the final valve measured and rejected); K10 farewell syncs the fast machine; flag split `s2_throttle` vs `s2_condensation`; write threshold 2 → 5 pp; kt documented as an open question with data (unit sign canary). |
