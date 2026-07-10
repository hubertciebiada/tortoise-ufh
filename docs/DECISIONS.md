# DECISIONS — Tortoise-UFH locked decision log

> **Status:** FROZEN contract. This file is a standalone quick-reference mirror of
> **PRD `prd-control-brain.md` Aneks §8** (ULTRACODE interview, 2026-07-08).
> Where this log and the body of the PRD disagree, **§8 (and therefore this log) wins**
> — most notably: **floor cooling is in v1** (§8.4), which supersedes §3.3 and §6.
> Source of truth for *implementation* remains `docs/BUILD_SPEC.md`; this log records
> *why* the contract is shaped the way it is. Dated revisions and addenda (§4–§5)
> extend the frozen baseline; each states explicitly whether it reverses a §8 decision.

---

## 1. The 10 Q&A decisions

Each row distills one owner-interview question into its locked answer and points back to
the authoritative PRD clause. "Units" are stated wherever a value is implied.

| #  | Question (owner interview) | Locked decision | PRD |
|----|----------------------------|-----------------|-----|
| Q1 | How is the integration packaged and layered? | HACS custom integration `tortoise_ufh`. **Hard two-layer split:** pure-Python **core `tortoise_ufh/`** (numpy/scipy, `py.typed`, offline-testable) that **never imports `homeassistant`**, plus HA **adapter `custom_components/tortoise_ufh/`** (coordinator + entities + config flow + websocket + panel). Structural template: sibling `pump-ahead`. | §8.1 |
| Q2 | Where does configuration live and how does it survive restart? | Room config in the config entry (`entry.data` / `entry.options`) — **survives HA restart**. Global "home temperature" and per-room **offset** are writable `number` entities (room target = global + offset, °C). Per-room flags: **participation** and **cooling participation** *(participation later merged into the per-room three-state — §4)*. Entities are picked via domain/device-class-filtered dropdowns, **no uniqueness requirement**. | §8.2 |
| Q3 | What actuates the floor loop and what is the valve output? | **Proportional valves 0–100 %, holding a setpoint** — one position number per room (zone). **No PWM / TPI, no actuator cycling.** | §8.3 |
| Q4 | What control law runs in the black box? | **Single PI loop on `T_room` error (°C) + a trend term (`dT_room/dt`, K/s)** to damp overshoot (the main enemy under high thermal inertia). **No D-on-error term.** Anti-windup (back-calculation / clamp), **deadband**, and **valve-floor** (minimum opening while in heating readiness). No slab sensor; supply water assumed correctly conditioned by the heat pump (water side out of scope). Optional `T_out` feedforward. Freeze the integrator on DHW/defrost when the heat-pump state is available *(2026-07-08: dropped as a v1 requirement — the owner's buffer tank bridges DHW/defrost gaps; the freeze stays as dormant, optional plumbing behind an unset-by-default HP-status entity — PRD §8.3)*. **Control cycle: every 5 min** (adjustable); persist position on change ≥ threshold. | §8.3 |
| Q5 | Is floor cooling in scope for v1? | **Yes — floor cooling is in v1** (scope change vs §6). PI with **inverted sign**; valves are **modulated, not merely closed**. Split cooling is also supported. See the dew-point addendum in §2 below. | §8.4 |
| Q6 | How is the fast source (split) commanded and gated? | Command = **`ON + mode (heat/cool) + room temperature`** (target = global + offset, °C); the split self-regulates (compressor power untouched) via `climate.set_hvac_mode` + `climate.set_temperature`. **Engage when `\|T_room − target\|` > boost-offset (K, adjustable).** Compressor protection: **minimum ON and OFF times** (anti-short-cycle). **Anti priority-inversion:** the split never closes/holds the floor valve — floor stays the base; the split tops up above the band and backs off inside the comfort band. | §8.5 |
| Q7 | How are operating modes structured? | **One global house mode**: `heat / transitional / cool / off` (input entity). Per-room only: participation + cooling-participation + offset. **Transitional:** valves parked, split regulates bidirectionally. **Off:** room to rest, no commands issued. | §8.6 |
| Q8 | What are the outputs and the report contract? | **Three outputs.** (1) Per-room valve position 0–100 % (one per zone). (2) Per-room fast-source command (`ON + mode + room temp`). (3) **Global safe dew point `max_i(T_dew_i) + 2 K`** (°C, `sensor` entity) fed to the heat pump. Plus (4) a per-room **under-the-hood report**: error, trend, decision components, flags (saturation, sensor-loss, protection), human- and AI-readable "what and why". | §8.8 |
| Q9 | How is it deployed and validated for quality? | **Shadow / dry-run switch:** computes and logs the full report but **sends no commands**; then LIVE with per-room takeover *(v2: `shadow`/`live` are states of the per-room three-state — §4)*. **Digital-twin simulator** modeled on `pump-ahead` (RC 3R3C, ZOH via `expm`; `BuildingSimulator` / `SimulatedRoom`, `SyntheticWeather`, `SensorNoise`, `SimMetrics` + assertions) for offline PID tuning and scenario tests — `T_slab` exists as simulator ground truth but is **never given to the controller**. Two-layer tests (unit TDD + simulation), `mypy --strict`, `ruff`, `filterwarnings=error`. | §8.9 |
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
> **Superseded in v2 (v0.3.0, 2026-07-09) — see §4.** The global kill-switch and the
> per-room participation/live-control booleans were merged into a single canonical
> per-room three-state control (`off` / `shadow` / `live`).

---

## 4. Revision — per-room control state supersedes the kill-switch (v2)

> **Status: this REVERSES a frozen §8 decision.** Recorded here (and in
> `prd-control-brain.md` §8.11) as a deliberate, dated contract change, not a drift:
> **2026-07-09, shipped in v0.3.0** (breaking; config-entry migration v1 → v2).

The original interview locked three separate participation controls: a per-room
**participation** flag (Q2, `entry.data`), a per-room **live/shadow** toggle (the "shadow /
dry-run switch", Q9), and a **global kill-switch** (§3, `off` ⇒ emit no commands). In
practice these three booleans encoded a single question per room — *how much authority does
Tortoise-UFH have here?* — with redundant, overlapping states (e.g. "participating but
shadow" vs "kill-switch on"), and the global kill-switch was strictly weaker than "put every
room in shadow".

**Decision (v2):** collapse all three into one canonical **`RoomControlState` per room**:

| State    | Participates (core sees) | Computes & reports | Writes to hardware |
|----------|--------------------------|--------------------|--------------------|
| `off`    | no — core fed `Mode.OFF` (reports valve 0 %, fast source off; nothing is written, so the physical actuator is left untouched) | yes | no |
| `shadow` | yes                      | yes                | **no** (dry-run)   |
| `live`   | yes                      | yes                | **yes**            |

- **Single source of truth:** `entry.options[CONF_ROOM_STATE] = {room: state}`. Derived:
  `participates := state != off`, `write := state == live`. New rooms default to `shadow`
  (preserves the old "start in dry-run" safety).
- **Whole-home stop** = every room `off`/`shadow` — the intent the kill-switch served,
  without a separate persisted global flag.
- **Surfaces:** a per-room `select` entity (`control_state`), the sidebar panel, the
  `tortoise_ufh/set_room_state` WebSocket command, and the options-flow settings step
  (emergency / automation-free fallback). The retired `switch.tortoise_ufh_kill_switch` and
  `switch.*_live_control` entities are removed.
- **No integrator reset on state change:** a control-state change applied through the
  canonical path (select entity / panel / `set_room_state` WS) updates the coordinator in
  memory first and does **not** reload the config entry (only tuning changes do), so the PID
  integrator is preserved. A state map written directly by the options flow that disagrees
  with the in-memory state still forces a reload so the coordinator adopts it.
- **Migration (config-entry v1 → v2, one-time, binding):** per room, **safety precedence** —
  `participates == false` ⇒ `off` (wins even over `live_control == true`); otherwise
  `live_control` decides `live` vs `shadow`. The legacy `participates`, `live_control` and
  `kill_switch` keys and the retired switch registry entries are purged.

Unchanged by this revision: the three external outputs (Q8), the safe-degrade contract (§3),
the dew-point layers (§2), and the control law (Q4).

---

## 5. Addendum — additive v0.3.x changes (non-breaking)

> Recorded 2026-07-09 for completeness. None of these reverses a §8 decision; they extend
> the contract additively.

- **Tuning: global values + sparse per-room overrides (v0.3.0).** The advanced knobs (Q4;
  PRD §4.5) stay optional with sane defaults. Their specs (name/min/max/step/unit) live once,
  in the adapter's `const.py` (`CONTROLLER_NUMBER_KNOBS` + the boolean feedforward knob), and
  persist as the global `entry.options[CONF_CONTROLLER]` plus a sparse per-room map
  `entry.options[CONF_ROOM_TUNING] = {room: {field: value}}` (an emptied override reverts the
  room to global). Surfaces: the panel's Tuning tab through the `tortoise_ufh/get_tuning` /
  `set_tuning` websocket commands (range-validated against the same specs). A tuning change
  reloads the entry (clean controller rebuild); a control-state-only change does not (§4).
- **Report: additive `RoomReport` fields (v0.3.0/v0.3.1).** `room_temperature_c` (echoed
  measurement), `dew_excluded_reason` (why a room is excluded from the global safe dew-point
  maximum) and `fast_dwell_remaining_s` (min ON/OFF dwell-lock countdown) — all defaulting to
  `None`; the JSON contract is extended, not changed.
- **Coordinator honesty (v0.3.0).** The core `step` is fed the REAL measured elapsed time
  (clamped) instead of the nominal 5-minute cycle, and a setpoint change triggers one
  debounced off-cycle recompute (so e.g. the split target follows promptly).
- **Panel (v0.3.0/v0.3.1).** Tabs: Rooms (table columns: three-state | room | measured |
  setpoint | error | valve % | supply | return | assist mode | assist temp), Tuning, Valves,
  Assist.

---

## 6. Revision — Phase A safety hardening (2026-07-09)

> **Status: one item REVERSES part of a frozen decision** (the §3 / §8.7 sensor-loss freeze,
> for COOLING only); the rest extends the contract. Recorded 2026-07-09 after the five-agent
> algorithmic FMEA (`scratchpad/algo-analysis/`). Mirrored in `prd-control-brain.md` §8.7 and
> `docs/BUILD_SPEC.md` §5.2 step 1 + §9 in the same pass. All thresholds below are **fixed
> constants tailored to the owner's house, deliberately NOT config knobs.**

- **C2 — sensor-loss safe-degrade is mode-dependent.** The §3 freeze ("valve holds the last
  commanded position") now applies **only in HEATING**. In **COOLING** a lost room temperature
  drives the valve to **0**: without `T_room` the room silently leaves the global safe
  dew-point maximum (`no_temperature`) AND the local S2 throttle cannot compute, so a
  frozen-open valve would pass unprotected chilled water indefinitely — the one scenario that
  defeated both condensation layers at once. Split OFF and the `sensor_lost` flag are unchanged.
- **C3 — room-temperature plausibility gate (adapter).** Range −10..50 °C (rejects e.g. the
  DS18B20 85 °C power-on-reset that used to poison the integrator to a 100 % valve for hours)
  plus a rate-of-change gate: a jump > 4 K between cycles is rejected like a missing reading;
  two consecutive mutually consistent samples accept a genuinely new level within 2 cycles.
- **C4 — state-age gate (adapter).** A present-but-frozen entity (dead battery, stuck bridge)
  is aged out via `last_reported`/`last_updated`: room temperature > 45 min and humidity
  > 60 min ⇒ treated as unavailable, WITHOUT the short-cache fallback (the cache would return
  the same stale value). Stale winter RH was the most dangerous single input: both condensation
  defences trusted it. (Docs recommendation kept for Phase E: a hardware pipe-condensation
  sensor as an independent third layer.)
- **C5 — farewell command.** A room leaving `live` (→ `shadow`/`off`) and an entry unload park
  the released actuators exactly once: **split OFF** always; **valve → 0 only in COOLING**
  (an orphaned open valve escapes both dew defences). In HEATING the valve position is left
  untouched — warm supply water is bounded by the heat pump's own curve, so holding the last
  position keeps the house warm and is strictly safer in winter than cold-parking. This
  narrows the §4 statement "off ⇒ the physical actuator is left untouched" to the heating case.
  *Note (2026-07-10):* the farewell also fires on every entry **reload** (each tuning change
  unloads the entry), so the split turns off and the conservative restart seed then demands a
  full min-OFF before the boost may return. This is a deliberate, safe cost: reloads are rare
  and compressor hygiene outranks a few minutes of missing boost.
- **S5 — safety override keeps controller state honest.** A safety force-ON (S3/S4) now syncs
  the fast-source dwell machine (`_fast_on` + timer), so releasing the override cannot stop a
  compressor started seconds earlier; and the override no longer writes `_last_valve_pct`, so
  the sensor-lost freeze holds the last position of *healthy* regulation, never an emergency
  0/100 that could outlive the fault by days.
- **S7 — water side and air side decided independently.** S1/S2 (`CLOSE_VALVE`) parks the valve
  but no longer silences an active S3/S4 fast source: a freezing room with overheated supply
  water keeps its air-side heat (the split) while the floor valve stays closed.
- **S8 — valve feedback validated per loop + `valve_mismatch`.** A garbage feedback reading
  (e.g. 255 from a stuck register) nulls only that loop instead of degrading the whole room to
  `sensor_lost`; a LIVE room whose feedback diverges from the last written command by > 10 pp
  for 3 consecutive cycles raises the additive `valve_mismatch` report flag.
- **S9 — global mode persisted.** The mode joins the setpoint Store (restored on startup; a
  configured, available mode entity still wins), so an HA restart in July can never silently
  resume heating logic.

---

## 7. Revision — Phase B fast-source direction machine (2026-07-09)

> **Status: extends §2/§8.5 and REVERSES two behavioural details** (transitional hysteresis
> band; split target = setpoint). Recorded 2026-07-09 after the five-agent algorithmic FMEA
> (`scratchpad/algo-analysis/`, algo-fast). Mirrored in `prd-control-brain.md` §8.5/§8.6 and
> `docs/BUILD_SPEC.md` §5.2 steps 3 + 14 in the same pass. Numeric choices are **fixed
> constants**, not config knobs.

- **C6 — the split direction is machine state.** The fast source is a three-state machine
  `OFF / HEATING / COOLING`. `OFF -> direction` needs the full min-OFF; `running -> OFF`
  (requested OFF **or the opposite direction**) needs the full min-ON; a HEATING<->COOLING
  reversal is therefore only reachable through OFF with the full min-OFF dwell. A min-ON hold
  re-emits the REMEMBERED direction, never a freshly computed one (the old `_fallback_mode`
  error-sign fallback is deleted). Rationale (hard requirement): indoor units may share a
  multisplit outdoor unit — simultaneous heat and cool requests on one aggregate are a mode
  conflict/lockout. The single exception: a hard S3/S4 emergency force-ON sets the direction
  immediately (frost protection outranks compressor hygiene; opposite-season co-occurrence is
  physically implausible).
- **S3 — split command cache + periodic re-assert.** The adapter writes the split command only
  when the `(hvac_mode, target)` pair changes (mirror of `_last_written_valve`), and re-asserts
  an unchanged command after ~45 min. The splits are local (ESPHome/LAN) — this is hygiene (no
  beeps, no stomping on manual tweaks), not an API budget — and the re-assert keeps the machine
  the owner after a missed write or a manual override.
- **S4 — the machine consumes the physical split state.** The first observed `fast_source_on`
  feedback wins over a cold machine (running unit adopted as ON with the mode's direction,
  stopped unit as OFF) and the dwell timer is re-seeded to **0** — a FULL dwell before any state
  change — replacing the old `1e9` free-transition seed, so an HA restart/reload loop (every
  tuning change reloads!) can never short-cycle a compressor. Later feedback disagreeing with
  the previous cycle's command raises the additive `fast_source_mismatch` flag. Rooms without
  feedback (`fast_source_on is None`) keep the legacy free first transition.
- **S12 — boost target offset + transitional bias removal.** In HEATING/COOLING the split target
  is `setpoint + 1.0 K` / `setpoint - 1.0 K` (fixed `FAST_TARGET_OFFSET_K`): the split's own
  ceiling-mounted sensor reads warm, so `target = setpoint` throttled the unit before the boost
  was delivered; the release still belongs to OUR room sensor (hysteresis + min-ON), so the room
  cannot run away. In TRANSITIONAL the target stays exactly `setpoint` and the release moves to
  the FAR edge of the comfort band (`demand < -deadband`): the split's own regulation holds the
  room AT the setpoint while ON, eliminating the old `[setpoint-1.0, setpoint-0.3]` band's
  ~-0.65 K seasonal bias.
- **D2 — relational validation.** `ControllerConfig` now rejects `boost_offset_c <= deadband_c`
  (an inverted engage/release hysteresis was previously constructible).
- **fast-F6 — dwell accumulates on forced paths.** Sensor-lost / OFF cycles advance the dwell
  timer too, so a recovered room does not restart its min-OFF wait from scratch.

---

## 8. Revision — Phases C+D+E: tuning, calibrated twin, watchdog escalation (2026-07-09)

> **Status: REVERSES the frozen default gains** (§3 / BUILD_SPEC §6) and extends the
> simulator/metrics contract. Recorded 2026-07-09 after the five-agent algorithmic FMEA
> (`scratchpad/algo-analysis/`, reports algo-control + algo-thermal). Mirrored in
> `prd-control-brain.md` §8.3/§8.7 and `docs/BUILD_SPEC.md` §5.2/§6/§7/§8 +
> `docs/ALGORITHM_SPEC.md` in the same pass. The order was deliberate: the twin was FIXED
> AND CALIBRATED FIRST (phase D), and only then were the new gains chosen empirically on it
> (phase C).

### Phase D — the digital twin becomes a real gate

- **C7a — solar reaches the physics.** `step_all` now computes each room's through-window
  gain `q_sol = GHI * sum(area * g * f_orient(time-of-day))` (fixed diffuse share + cosine
  direct envelope per facade; solar noon pinned to the scenario GHI convention) and the RC
  model routes it `f_slab/f_conv/f_rad = 0.5/0.3/0.2` — sun on a UFH room lands mostly on
  the FLOOR (new `RCParams.f_slab`, new slab row in `E_c`). Previously `q_sol_w=0.0` was
  hard-coded: the trend member was never tested against its main enemy.
- **C7b — ground fixed, seasonal.** `R_ins_ref` 0.05 → **0.28 K/W @ 20 m²** (sub-slab
  U ≈ 0.18 W/m²K incl. ground mass; the old value made the ground a bigger sink than the
  whole envelope) and `T_ground` is seasonal: **14 °C winter / 17 °C summer** (`t_ground`
  factory parameter). Result: `hot_july` genuinely needs cooling and `spring_transition`
  drifts across the setpoint instead of falling to 12 °C.
- **S11 — plant calibration.** `loop_power` now uses the EN 1264 characteristic
  `Q = K_H * A * dT_log^1.1` with the screed spreading resistance in series
  (`R_SCREED_M2K_W = 0.10`; K_H lands in the 4-7 W/m²K table band); floor film
  h ≈ 10 W/m²K (`R_sf_ref` 0.01 → 0.005; slab eigenmode tau ≈ 4.4 h);
  `C_air_ref` 60 → **300 kJ/K** (air + furnishings; the split response is no longer a
  physically absurd ~7 min). `leaky_old_house` recalibrated from a ~620 W/m² absurdity to a
  plausible ~140 W/m² design loss.
- **I1 — the contract's third output closed in the loop.** The twin heat pump honours the
  controller's global safe dew point: `BuildingSimulator.set_cooling_supply_floor()` lifts
  the chilled supply to `max(curve/fallback, global_safe_dew_point)`; the harness feeds it
  back every cycle. The gate asserts the S2 throttle is genuinely ACTIVE in `hot_july` and
  that cooling actually flows (valves > 0) — the old assertion passed with the valves flat
  at 0 %.
- **C7c/S13 — the gate is real.** ALL library scenarios now gate the merge (`cold_snap` —
  with a realistic `WeatherCompCurve` capped below the S1 trip, `solar_overshoot`,
  `spring_transition` were dead; the "exercised elsewhere" comment was false). New
  assertions: `assert_max_overshoot <= 0.5 K` on `steady_heating` (S13 — the primary goal;
  the old defaults would fail it at +1.2 K), solar surplus ⇒ valves fully closed,
  transitional ⇒ valves parked while the house drifts, `split_boost` (new scenario, 2.5 kW
  split) ⇒ boost engages AND releases with anti-priority-inversion held.
  `steady_heating` runs at BOTH dt = 60 s and the production 300 s takt.
- **thermal-D2 — indoor humidity model.** Indoor RH = outdoor vapour pressure + constant
  occupancy surplus (~2 g/kg) evaluated at the room temperature — a credible per-room dew
  point (the old twin used outdoor RH verbatim). Summer scenarios start at a summer state
  (`SimScenario.initial_temperature_c = 24`), because a 20 °C winter-reset house under July
  vapour pressure spends hours at RH ≈ 100 % — an artifact no controller can influence.
- **D1/D6/D7 — hygiene.** Dead `simulated_room.py` deleted (canonical `SimulatedRoom` is in
  `simulator.py`); `energy_kwh` integrates the recorded ALLOCATED floor power
  (`SimRecord.q_floor_w`) instead of nominal x valve; new `valve_travel_pct_per_h` metric +
  `assert_valve_movement_moderate` (actuator wear <= 30 pp/h in the gate).
- **Gate grading note:** `assert_no_condensation` is graded at margin **1.0 K** (not the
  2.0 K control target): with valves closed on a muggy afternoon the slab temperature is
  weather-driven and sits exactly AT `dew+2` — equality at the control margin measures the
  weather, not the loop. `assert_no_freezing` skips TRANSITIONAL (valves parked by design).

### Phase C — control law and defaults (empirical, on the calibrated twin)

- **S10 — the trend is filtered.** Raw `dT/dt` samples are taken only once >= 60 s has
  accumulated (a 2 s debounced recompute HOLDS the previous value instead of dividing a
  sensor tick by 2 s — the old code could emit a fictitious 180 K/h), then smoothed by a
  first-order EMA with tau = 15 min. Sensor loss resets the filter (a gap invalidates the
  trend).
- **S1 — no windup under the dew throttle.** The S2 factor is computed BEFORE the PI and
  the integrator is frozen whenever `factor < 1` in cooling: hours of throttled cooling no
  longer bank an integral that slams the valve open the moment the humidity clears.
- **S2 — integrator seasonal hygiene.** A HEATING↔COOLING transition resets the
  integrator (the error convention flips — one season's integral is anti-knowledge for the
  other), and more than 12 h of accumulated inactivity (OFF/TRANSITIONAL/opt-out/
  sensor-lost) clears it.
- **control-F8 — `saturated` semantics.** A zero produced solely by the S2 throttle no
  longer reports `saturated`; `dew_throttle_factor` carries the condensation story.
- **C1 — new default gains (REVERSAL of the frozen §3 defaults).**
  `kp 8→14, ki 0.02→0.0015 (Ti ≈ 2.6 h), kt 6→12` (on the FILTERED trend). The old
  Ti ≈ 7 min was an order of magnitude too aggressive for a tau = 3-6 h slab. Empirical
  sweep on the calibrated twin (production 300 s takt; worst room; tail = last 24 h):

  | wariant | steady_heating: max_over / ogon ±0,3 K / ruch zaworu | steady + szum 0,1 K: ruch | cold_snap: max_over / ogon od dnia 3 / ruch |
  |---|---|---|---|
  | kp=8 ki=0.02 kt=6 (STARE) | +0,42 K / 68,9 % / 5,8 pp/h | 25,2 pp/h | +0,57 K / 44,2 % / 5,3 pp/h |
  | kp=8 ki=0.0011 kt=6 | +0,18 K / 100 % / 0,6 pp/h | 19,7 pp/h | +0,13 K / 81,5 % / 1,2 pp/h |
  | kp=12 ki=0.0011 kt=12 | +0,03 K / 100 % / 0,6 pp/h | 37,4 pp/h | −0,16 K / 69,7 % / 1,0 pp/h |
  | kp=14 ki=0.0011 kt=12 | −0,05 K / 100 % / 0,6 pp/h | 38,7 pp/h | −0,23 K / 60,2 % / 1,0 pp/h |
  | kp=12 ki=0.0015 kt=12 | +0,26 K / 100 % / 0,7 pp/h | 37,7 pp/h | +0,06 K / 96,6 % / 1,1 pp/h |
  | **kp=14 ki=0.0015 kt=12 (NOWE)** | **+0,18 K / 100 % / 0,6 pp/h** | 37,8 pp/h | **−0,06 K / 92,9 % / 1,1 pp/h** |
  | kp=16 ki=0.0015 kt=12 | +0,10 K / 100 % / 0,6 pp/h | 38,5 pp/h | −0,15 K / 88,2 % / 1,0 pp/h |
  | kp=14 ki=0.002 kt=12 | +0,35 K / 26 % / 0,8 pp/h | 41,0 pp/h | +0,18 K / 100 % / 1,2 pp/h |
  | kp=14 ki=0.0015 kt=0 | +0,13 K / 100 % / 0,6 pp/h | 5,4 pp/h | −0,08 K / 94,9 % / 1,1 pp/h |

  `ki = 0.002` already limit-cycles (steady tail 26 %); `ki = 0.0011` undershoots the
  cold-snap tail. `kp=14/ki=0.0015/kt=12` is the best compromise: <= +0.2 K overshoot,
  100 % steady tail, ~93 % worst-room cold-snap tail (plant-authority-limited), ~1 pp/h
  valve travel. Measurement-window note: the table's overshoot figures are measured
  post-settle (steady-state window); the whole-run figure from t=0 for the OLD defaults
  is +0.75 K on the calibrated twin, and the often-quoted "+1.2 K" comes from the
  original analysis run on the pre-calibration plant — all three windows agree on the
  conclusion (the old defaults fail the 0.5 K gate; the new ones pass with margin). The report's provisional `ki ≈ 0.0011` was measured on the UNCALIBRATED
  plant; the calibrated twin (weaker plant, EN 1264^1.1) needs the slightly faster
  integral. Note: kt's noise cost (sigma = 0.1 K → ~38 pp/h commanded travel vs 5.4 at
  kt=0) is bounded in practice by the 2 % valve-write threshold and by real sensor noise
  being ~2x lower; kt stays per the frozen trend-member decision (its value is anticipatory
  damping on approach, e.g. after setpoint changes and under morning sun).
- **control-F6 — FF constants are knobs.** `ff_neutral_c` / `ff_gain_pct_per_k` /
  `ff_max_pct` moved from module constants into `ControllerConfig` (validated), exposed in
  `CONTROLLER_NUMBER_KNOBS` → config flow, `get_tuning`/`set_tuning` and the panel.

### Phase E — watchdog escalation, contracts, docs

- **S6 — S5 watchdog is live, action = NEUTRAL.** The adapter tracks each room's last
  fresh-data timestamp and feeds the age via `RoomInputs.last_update_age_minutes` (additive
  field) into the core `SensorSnapshot` — `FALLBACK_HP_CURVE` was dead code (age was a
  hard-coded 0). Its action changes from "valve 0" to the **neutral position**:
  `valve_floor_pct` in HEATING (defer to the HP curve with a tempered floor), 0 in COOLING.
  Escalation ladder for a silent room: freeze/hold (sensor lost at ~45 min of staleness) →
  neutral (S5, ~15 min later). The adapter's building-level watchdog stays report-only.
- **S14 — the global dew sensor's `None` contract** documented (BUILD_SPEC §3 + README):
  `unknown` is NOT "safe"; consumers must fail conservative. Reference automation in the
  README (generic entity names).
- **safety-F13 — building staleness counter.** `BuildingOutputs.sensor_lost_rooms`
  (additive) → `CoordinatorData` → `get_live` websocket payload; deliberately NOT a new
  entity. README documents per-room alerting.
- **fast-F6/F7 — closed by A+B+C.** The dwell timer accumulates on sensor-lost/OFF paths
  (F6, done in phase B), and sensor flicker no longer force-stops a running split every few
  minutes: the C3 rate-of-change gate rejects single-sample glitches and C4 only ages out
  genuinely stale states, so a flapping sensor becomes a *steady* sensor-lost (split OFF
  once, min-OFF accumulated) rather than a stop/start cycle (F7).
- Installation docs: hardware pipe-condensation sensor on the manifold as the independent
  third protection layer, manifold insulation, supply probes on the manifold beam (before
  the valves — dew-F5), an RH sensor for every cooled room.

## 9. Revision — architectural round: modularity without behaviour change (2026-07-10)

A purely structural refactor after v0.4.0: module boundaries and single
responsibility, with a hard zero-behaviour-change gate (all 295 unit + 20
simulation tests pass unmodified; closed-loop simulation fingerprints on both
seeds are **bit-identical** before and after — the order of floating-point
operations was preserved by extracting code verbatim).

- **`core/fast_source.py` — `FastSourceMachine`** (BUILD_SPEC §2 annex
  2026-07-10). The three-state split direction machine (OFF/HEATING/COOLING),
  the min ON/OFF dwell clock (single per-step `tick`), and the S4 physical
  feedback sync moved out of `RoomController` (~350 lines, 6 methods, 5 state
  fields — the part with the dwell double-accumulation bug history) into a
  self-contained, unit-testable class. The controller delegates; the
  mode→demand mapping and HEATER-cannot-cool stay in `controller.py`.
  `FAST_TARGET_OFFSET_K` moved with it (re-exported from `controller.py`).
- **`core/trend.py` — `TrendEstimator`.** The debounce-aware EMA trend filter
  (S10: min-dt 60 s accumulation, tau = 900 s, sensor-loss invalidation) moved
  out of `RoomController.step` / `_safe_degrade` into a 1:1 class.
- **`readers.py` — `SourceReader` / `writers.py` — `CommandWriter`.** The
  coordinator's read path (stale cache, C4 state-age gate, C3 room-temperature
  plausibility, S8 per-loop valve plausibility, HP/fast on-off mapping) and
  write path (valve write threshold + domain dispatch, S3 fast-command cache
  with periodic re-assert, C5 farewell parking) each own their caches in a
  dedicated adapter class. The coordinator keeps orchestration, setpoints +
  Store, watchdog and `RoomInputs` assembly, and exposes thin delegates where
  the HA test tier observes the old surface (`_read_valve_position`,
  `_write_valves`, `_entity_cache`).
- **`tuning.py`.** Knob introspection (names, ranges, descriptors, effective
  values, sparse room overrides, payload coercion) moved out of
  `websocket.py`; the websocket keeps handlers + payload-view dataclasses, and
  `config_flow._current_controller` now reuses `tuning.global_controller`
  (identical merge/fallback semantics).
- **`CONF_CONTROLLER` → `const.py`.** The one inverted edge of the adapter
  import graph (runtime modules importing the config wizard for a single
  string) is gone; `config_flow.py` re-imports the key for compatibility.
  The key string is unchanged (Store/entry contract untouched).
- **Panel: internal reorganization only.** Still ONE vanilla-JS file (CSP, no
  build step); the 100+ methods of `TortoiseUfhPanel` are now grouped under
  eleven section banners (lifecycle → i18n → WS/data → view-model → skeleton/
  hero → rooms table → detail drawer → valves → assist → tuning → charts) —
  a pure method-reordering (verified line-multiset-identical plus banners).
- **Deliberately NOT done:** no `controller.py` split (frozen §2 keeps
  `RoomController` + `BuildingController` together), no changes to `models.py`
  / `config.py` / `pid.py` / `safety.py` / `dew_point.py` (frozen contracts),
  no touching `simulator.py` (dense seeded numeric path), no Protocol layers
  between coordinator and reader/writer (one consumer, one implementation),
  no splitting of the panel into files, no renames of public symbols.
