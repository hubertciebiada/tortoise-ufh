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
> **Superseded in part by §13 (2026-07-12, v0.7.0):** the `shadow` state was removed
> permanently; the control state is a two-state `off` / `live` and the default is `off`.

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
  is `setpoint + 1.0 K` / `setpoint - 1.0 K` (fixed `FAST_TARGET_OFFSET_K`; since 2026-07-13 the
  `fast_target_offset_k` tuning knob, default 1.0, 0 disables — owner found the silent offset
  confusing in the panel): the split's own
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
  integral. ~~Note: kt's noise cost (sigma = 0.1 K → ~38 pp/h commanded travel vs 5.4 at
  kt=0) is bounded in practice by the 2 % valve-write threshold and by real sensor noise
  being ~2x lower; kt stays per the frozen trend-member decision (its value is anticipatory
  damping on approach, e.g. after setpoint changes and under morning sun).~~
  > **CORRECTION (2026-07-12, K2b — see §11):** the bounded-by-the-threshold claim was
  > FALSE — measured, the 2 pp threshold passed 31.3 pp/h of written travel at
  > σ = 0.1 K (11.2 pp/h at σ = 0.05). The default threshold is now 5 pp (cuts σ = 0.05
  > to 1.4 pp/h at zero regulation cost). Also stale: the cold-snap tail column of the
  > sweep table above was generated on an intermediate phase-C code snapshot and its
  > ABSOLUTE values do not reproduce on any released version (v0.4.0/v0.5.0 measure
  > 75.3 % for salon / 73.2 % worst room, at both plant discretisations); the relative
  > ranking stands. kt itself remains an open question with data (§11): no scenario on
  > the calibrated twin shows a measurable kt benefit (every contrast ≤ 0.03 K).
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

---

## 10. Revision — per-room devices, translated entity names, `live_control` cleanup (2026-07-10, v0.5.0)

> **Breaking (minor).** No config-entry migration needed (data/options schema untouched;
> entry stays at version 2). Recorded 2026-07-10; resolves GH issue #3 and finishes the
> `live_control_enabled` deprecation announced with the v2 control-state refactor.

- **Devices.** Every configured room is now a HA **device**: identifier
  `(DOMAIN, f"{entry_id}_{room_slug}")` (the same frozen slug every per-room `unique_id`
  uses), name = room name, manufacturer "Tortoise-UFH", model "Room zone", `via_device` →
  a per-entry **hub device** `(DOMAIN, entry_id)` (name "Tortoise-UFH", model
  "UFH controller") that carries the global entities (home temperature, global safe dew
  point, algorithm/watchdog status, last update). Rooms can be assigned to HA areas.
  Helpers live in the new adapter module `device.py`.
- **Entity naming via translations (GH #3).** Platforms no longer set `_attr_name` (which
  silently overrode `translation_key`, leaving the name translations dead); every entity
  is `has_entity_name = True` + `translation_key`, with full EN/PL name parity in
  `strings.json` / `translations/{en,pl}.json`. **`unique_id` formats are unchanged**, so
  upgraded installs keep their entity ids via the registry; only NEW installs derive ids
  from the device + translated-name convention (a few ids differ from pre-0.5.0, e.g.
  `sensor.<room>_dew_point` vs `sensor.<room>_room_dew_point`).
- **`live_control` fully retired.** The per-room `binary_sensor.*_live_control` (a pure
  mirror of `control_state == "live"`) is gone; orphaned registry entries are swept on
  every setup (`__init__._async_purge_retired_entities` — registry hygiene only, no entry
  version bump). The transitional `live_control_enabled` field is removed from the
  `tortoise_ufh/get_live` websocket payload and the diagnostics dump; consumers read the
  canonical `control_state`. The coordinator-internal `RoomRuntime.live_control_enabled`
  write-gate flag stays (never part of the external contract).

---

## 11. Revision — round-2 algorithmic review: operating-point changes, margin de-stacking, multisplit arbiter (2026-07-12, v0.6.0)

> **Status: REVERSES three frozen details** (the local dew-margin semantics §2/§8.4; the
> S1/S2-forces-split-off coupling §6/S7; the 2 pp valve-write threshold §8) **and extends
> the control law** (bumpless setpoint transfer + asymmetric integrator unwind — a dated
> §8.3 amendment, not a drift). Recorded 2026-07-12 after the second five-agent algorithmic
> review (`scratchpad/algo-analysis/round2-*`); every number below was measured on the
> calibrated twin before/after the change. Mirrored in `prd-control-brain.md` §8.3/§8.4/
> §8.5/§8.7, `docs/BUILD_SPEC.md` and `docs/ALGORITHM_SPEC.md` in the same pass.

- **K1 — bumpless setpoint transfer + asymmetric unwind (the round-2 headline).** The
  steady-state gate never exercised OPERATING-POINT changes, and a daily night setback is
  daily use: measured, a 23 → 21 °C drop at a saturated integral kept the valve actively
  heating an already-too-warm room for **17.4 h (642 %·h of valve integral)** — the
  integral discharged at bare `ki` speed. Two mechanisms fix it:
  (a) *bumpless transfer*: an effective-setpoint change of `dK` between PID-active cycles
  re-seeds the integral by `kp·dK` in the mode's error convention (sign inverts in
  COOLING), clamped to the output range; the reference dies with every PID reset;
  (b) *asymmetric unwind* (`PIDController.unwind_factor = 8`): while the deadbanded error
  OPPOSES the integral, the integral discharges 8× faster than it accumulated — it only
  ever moves toward zero, so equilibrium and in-band behaviour are untouched.
  Measured after: valve ≈ 0 in **0.8 h** (23 %·h), back in the band FASTER (35 h vs
  48.8 h) with a 100 % settled tail; the honest cost is a deeper coast-down trough
  (−0.92 K vs −0.54 K — the price of not heating an overheated slab; noise σ = 0.1 K
  leaves no steady-state bias, tail 100 % in-band). New gate scenario **`night_setback`**
  (4 days of 21↔19 every 12 h on the bungalow, `SimScenario.setpoint_schedule` — additive
  field): heating-above-band integral ≤ 400 %·h (measured 264 vs **2577** without K1),
  no active heating clearly above the band past a 240-min grace, no room below 17.8 °C.
- **K2a — kt: an open question WITH data (the trend member stays).** After honest attempts
  no scenario measurably contrasts `kt=12` vs `kt=0` on the calibrated twin: solar
  overshoot +5.71/+5.71 K (identical — gains dominate with valves closed), steady warm-up
  +0.177/+0.130 K (kt slightly WORSE), cold-snap recovery +0.784/+0.799 K, split_boost
  +2.949/+2.979 K, strong-plant +2 K step +0.660/+0.672 K — every contrast ≤ 0.03 K.
  The anti-overshoot of the defaults is carried by the small `ki` and (since this
  revision) by K1. Per the frozen PRD trend-member decision **kt stays 12**; the gate hole
  ("a sign-flipped trend term passes everything") is closed by a UNIT canary pinning
  `trend_term == −kt·filtered_trend` and the approach-only asymmetry. The false
  `solar_overshoot` docstring and the `config.py` calibration comment were corrected.
- **K2b — valve-write threshold 2 → 5 pp + CORRECTION of a false §8 note.** §8 claimed
  kt's noise cost "is bounded in practice by the 2 % valve-write threshold" — measured,
  it is NOT: at σ = 0.1 K the written travel after the 2 pp threshold was **31.3 pp/h**
  (11.2 pp/h at σ = 0.05). The 5 pp default cuts σ = 0.05 to **1.4 pp/h** (σ = 0.1 to
  ~18 pp/h) with no regulation cost (the loop repositions ~1 pp/h without noise).
- **K3 — CLOSE_VALVE is water-side only.** The S7 split (water/air decided independently)
  was incomplete: an S1/S2 *without* a parallel S3/S4 still force-stopped the fast source
  every cycle — killing a wanted boost under S1, killing the ONE safe cooling source
  (the split has a condensate tray) exactly when humid under S2, and sawtoothing the
  dwell clock (flapping `fast_source_min_runtime`). Now the CLOSE_VALVE branch parks the
  valve and leaves the air-side decision from the normal coordination standing;
  Mode.OFF / sensor-lost / EMERGENCY_COOL-without-split still force OFF as before.
- **K4 — multisplit direction arbiter (new opt-in configuration).** Indoor units sharing
  an outdoor unit can be asked for opposite directions (routinely in TRANSITIONAL — the
  direction is per-room error sign; and during seasonal mode changes under min-ON holds).
  New per-room key `fast_source_group` (config flow; generic labels like
  `outdoor_unit_a`; carried to the core via `RoomInputs.fast_source_group`):
  `BuildingController` enforces ONE direction per group each cycle — a min-ON-locked or
  S3/S4-forced unit pins the group; otherwise the largest comfort-band excess wins;
  losers are rewritten OFF with the `fast_source_group_conflict` flag and re-engage
  through a full min-OFF; a pathological double-pin (inconsistent physical adoption)
  overrides nobody and flags. Additionally the S4 reconciliation now sees the DIRECTION:
  the adapter passes the raw `hvac_mode` feedback (`RoomInputs.fast_source_hvac_mode`)
  and a unit physically running opposite to its command raises `fast_source_mismatch`
  even though the on/off bool agrees (previously invisible). Ungrouped rooms are
  untouched.
- **K5 — `controller_error` degrade is mode-aware.** `_degraded_room_output` held the
  last valve regardless of mode — the last bypass of the dew-F1 invariant (a crashed
  controller computes neither condensation defence). Now symmetric with sensor-loss:
  HEATING holds, COOLING/TRANSITIONAL/OFF close.
- **K6 — margin de-stacking (owner decision: "tylko pompa +2").** The two condensation
  margins STACKED: the pump floor guarantees `supply ≥ dew_max + 2 K`, but the local
  throttle only fully opened at `gap ≥ margin + ramp = 4 K` — the most humid room (the
  one defining the floor) sat at `gap = margin` → factor 0. Measured in `hot_july`
  @ 300 s: factor < 1 for 80.9 % of records, full cooling 19.1 %, valves open 29.7 %,
  living-room mean 26.75 °C at a 24 °C setpoint — "fasadowe" cooling. New semantics: the
  ramp ENDS at `dew_margin_k` (factor 1 exactly on the pump floor) and spans
  `(max(0, margin−ramp), margin)` — with the defaults full cooling from gap 2 K, hard 0
  at the room's actual dew point. The hard S2 rule moved BELOW the ramp
  (`S2_HARD_MARGIN_K = 0`: trip at `supply − dew < 0`, clear above +1 K) — a backstop
  behind a backstop, not a second margin. Measured after: valves open **94.2 %**, factor
  1 for 68.8 %, factor < 1 still genuinely exercised (31.2 % — humid transients where
  the supply dips under the floor), min slab-dew margin **+1.51 K** (gate grades ≥ 1.0),
  living-room mean 25.36 °C. Gate updated: `open_share > 0.60`, hot_july also at the
  production 300 s takt (B8).
- **K7 — two-stage RH age gate (+1 K stale pad).** The binary 60-min age gate versus a
  threshold-reporting RH sensor (SCD41 over Matter) risked a cooling limit cycle
  (fresh → cool → aged out → full stop → repeat). Now: ≤ 60 min fresh; 60-120 min the
  LAST value is served flagged stale (`RoomInputs.humidity_stale`; renamed
  `humidity_stale_frac` in §12) and the core pads
  BOTH protective dew points (+1 K, flag `rh_stale_gated`); > 120 min unusable (`None`,
  conservative full stop) as before. Owner action item: measure the sensor's real
  reporting cadence before the cooling season.
- **K8 — cold-snap recovery assertion + tail-figure discrepancy explained.** The supply
  step (29.5 → 37 °C at the −15 °C snap) is a 2-3× plant-gain change; round 2 measured
  an unasserted +1.15 K recovery peak ~10 h after the step. With K1's unwind the peak
  itself dropped to **+0.78 K** and from step+12 h the worst room stays at **+0.34 K**;
  the gate now asserts ≤ 0.5 K from step+12 h. The §8 table's "92.9 %" cold-snap tail vs
  round 2's "75.3 %" was re-measured for this revision: 75.3 % (salon; 73.2 % worst
  room) REPRODUCES on both released code versions (v0.4.0 and v0.5.0) and at both plant
  discretisations (physics 60 s with the 300 s takt, and physics 300 s), while the
  92.9 % does not reproduce anywhere — the sweep table was generated on an INTERMEDIATE
  phase-C code snapshot (squashed away by the release history), so its absolute
  cold-snap tail figures are stale. The table's RELATIVE ranking (which drove the gain
  choice) is unaffected, and recovery quality is now asserted explicitly instead of via
  that tail figure. Gain normalisation by `(T_supply − T_room)` filed as backlog (a
  control-law change, not this round).
- **K9 — throttle-freeze STAYS; back-calc from the final valve REJECTED with data.** The
  proposed `I += (u_final − u_raw)` under `factor < 1` was implemented and measured:
  any tracking anti-windup enforces `u_raw ≈ u_final`, pinning the integral at ~0 for
  the whole episode, so the post-release catch-up was NOT faster (6 h full throttle →
  ±0.3 K after **8.6 h either way**; partial throttle 9.5 h, worse) — the catch-up is a
  `ki`-speed property (the integral must legitimately rebuild 0 → ~50 pp), not a windup
  artifact; under a persistent partial throttle the tracking loop additionally suppresses
  the legitimate integral entirely. Note: after K6 the throttle episodes themselves
  became rare and shallow.
- **K10 — farewell synchronises the machine.** The C5 farewell wrote a physical OFF
  outside the model: the machine kept "emitting" ON in shadow, so live→shadow→live was a
  physical OFF/ON with no dwell, and a reload could adopt a stale ON feedback and write
  ON seconds after the farewell OFF. Now the adapter calls the new core hook
  (`BuildingController.notify_fast_source_farewell`) — the machine transitions to OFF
  with the dwell reset, so the way back to live passes an honest min-OFF; additionally
  a module-level farewell registry (surviving reloads) makes the read path distrust an
  ON feedback younger than one cycle after the farewell (R5).
- **Minor, same pass:** local-throttle flag split from the hard rule (`s2_throttle` vs
  `s2_condensation`; panel labels PL/EN); temperature-jump confirmation must span ≥ 270 s
  of real time (B5 — a debounced recompute burst could confirm a bogus spike in 4 s);
  the adapter invalidates the trend filter when the measured step interval exceeds the
  900 s clamp (R2-F6); trend-filter warm-up (~30-45 min after a gap) documented (R2-F8);
  the constant-outdoor-RH artifact of `hot_july` documented as a protection-friendly
  bias (B9); README notes on the 45-min re-assert, shadow as the manual-override mode,
  supply-probe placement (manifold beam, BEFORE the valves) and RH reporting cadence.
- **Backlog (deliberately NOT this round):** gain normalisation by `(T_supply − T_room)`
  (control-law change; draft issue in the owner's scratchpad), inter-loop hydraulics in
  the twin, the kt question (revisit with real-house data), and per-mode integrator
  memory (R2-F7 — today every HEATING↔COOLING flip resets the integral, costing ~2-3 h
  of re-convergence per seasonal mode wobble; correct for safety, optional comfort win).

---

## 12. Revision — round-3 hardening: arbiter incumbency, unload window, shift residual (2026-07-12, v0.6.1)

> **Status: extends the contract; no §8 reversal.** Recorded 2026-07-12 after the third
> adversarial review (`scratchpad/algo-analysis/round3-*` — mechanisms, adapter
> concurrency/lifecycle, WS/panel/flow contracts; the async findings were CONFIRMED by
> diagnostic tests before fixing). Every number below was measured before/after. Release
> v0.6.1 (fixes + one additive WS field; non-breaking — no new required configuration).

- **K1 — SHADOW rooms no longer vote in the multisplit arbiter.** The adapter empties
  `RoomInputs.fast_source_group` for every non-LIVE room. A shadow room's command is never
  written, yet its uncontrolled — so never-shrinking — error won EVERY re-engage and
  permanently strangled the LIVE rooms' split (shadow is the default state of a new room);
  its phantom machine could even min-ON-pin the group. Shadow observes, never votes.
- **K2 — incumbent hysteresis in the group arbiter + UI dwell floor.** The conflict winner
  was a bare `max(band_excess)`: two rooms with persistent opposite ~2 K demands plus
  σ = 0.05 K noise reversed the shared aggregate 15×/2.5 h at zero dwells (7× at the default
  10/10 min). Now the incumbent direction (a unit already running at step entry, else the
  group's stored last winner — kept in `BuildingController`, cleared by `reset()`) defends
  its seat: the challenger takes over only when its best excess exceeds the incumbent's by
  more than the fixed `_GROUP_CHALLENGER_HYSTERESIS_K = 0.5 K` (not a knob).
  Measured after: 0 reversals in both configurations; a genuinely stronger challenger
  (> +0.5 K) still flips the group through honest dwells. The tie-break with no incumbent
  stays **largest single-room excess** (documented in ALGORITHM_SPEC §8.4 — deliberately not
  the side head-count). UI: `fast_min_on/off_minutes` knob floor raised 0 → **3 min**
  (zero dwells let a conflicted group flip every cycle; the CORE still accepts 0 for tests).
- **K3 — the unload window can no longer re-open a parked valve.** CONFIRMED race: a
  setpoint nudge armed the 2-s recompute debouncer, `async_unload_entry` ran the farewell
  (valve → 0), and the timer/5-min tick fired DURING `async_unload_platforms` (the
  `async_on_unload` cancellations only run after the unload returns) — measured 53 → 0 →
  **81.5 %** on an orphaned cooling valve outside both condensation guards. Now
  `async_unload_entry` cancels the recompute and awaits `coordinator.async_shutdown()`
  BEFORE the farewell, and `async_farewell_all` raises a permanent `_parked` flag gating the
  coordinator's write loop (belt-and-braces).
- **K4 — non-finite setpoint guard.** HA's `number.set_value` min/max check passes NaN
  (every NaN comparison is false); the mutated setpoint made every later cycle raise in
  `RoomRuntime.__post_init__` — entities unavailable, zero commands until reload
  (CONFIRMED). `set_home_temperature` / `set_room_offset` now reject non-finite values
  before mutating (log + no-op). The WS/service paths were already safe (`vol.Range`).
- **K5 — setpoint Store flushed on unload.** A setpoint changed < 1 s before a reload sat
  only in the Store's delayed-save timer and the new coordinator read the stale file
  (CONFIRMED: 23.5 → 21.0). `TortoiseUfhCoordinator.async_shutdown` now flushes the
  snapshot synchronously (only when the Store was actually loaded — a failed first refresh
  must not overwrite persisted values with config-entry seeds).
- **K6 — shift residual + suppressed back-calculation (PID).** A setpoint wiggle
  (down-and-back within a couple of cycles) at a SMALL integral pumped I to ~2·kp·ΔK:
  the down-shift clamped at 0 (losing kp·ΔK of intent), the transient low saturation
  back-calculated I to −P, and the counter-shift landed on top — measured I: 10 → **79.8**
  for a 3 K wiggle, with the valve at 79.8 % at zero error inside the band where the unwind
  is dead. The night_setback twin gate is blind to this regime (winter integrals never
  clamp). Fix, in `PIDController` (the clamp lives there, so its debt does too — a
  deliberate placement refinement of the "RoomController residual" plan):
  (a) `shift_integral` banks the clamp-cut as a signed **residual**, netted against future
  shifts of the OPPOSITE sign (same-sign series keep their sum — monotonic behaviour
  unchanged); (b) the back-calculation correction is **suppressed** while an opposite-sign
  residual is outstanding (the accumulator is only range-clamped) — without (b) the
  netting alone still left I at 47.8 (the −P pump is conserved regardless of where the
  residual is booked; verified empirically). Measured after: 10 → 0 → **10.0** (idempotent);
  residual dies with `reset()` (mode flip, 12-h decay, full reset), so a slow return hours
  later simply under-shifts and converges at ki speed — the safe direction. With no
  outstanding residual the anti-windup is bit-for-bit classic.
- **K7 — the unavailable-RH cache branch reads as fully stale.** The ≤ 5-min cache served a
  60-119-min-old held value as FRESH the moment the RH entity died — the +1 K pad vanished
  exactly when the sensor did. The branch now returns staleness fraction 1.0.
- **K8 — `remove_room` prunes `CONF_ROOM_TUNING`.** Symmetric with the control-state map
  and the setpoint Store; a NEW room reusing the name silently inherited the removed room's
  kp/ki (real regulation risk).
- **K9 — arbitration is observable (additive WS field).** `get_config` rooms now carry
  `fast_source_group`; the panel's Assist tab gained a "Grupa" column (rendered only when
  any room has a group), a "konflikt grupy" chip in the Timer column for the arbitration
  loser (whose dwell is cleared by the force-off, so it showed a bare "—"), and the tab's
  flag subset now includes `fast_source_group_conflict` + `fast_source_mismatch` (i18n
  PL/EN).
- **K10 — room-name SLUG uniqueness.** Unique ids and device identifiers are slug-based
  (lower + spaces→underscores): "Salon"/"salon" passed validation, collided unique ids and
  made remove-room delete BOTH rooms' entities. The wizard and options add-room now reject
  slug collisions (`duplicate_room_slug`, i18n PL/EN).
- **K11 — hub device registered before platforms.** Room devices reference the hub via
  `via_device`; adding them before the hub existed in the registry logged HA's
  "will stop working" deprecation per entity. `async_setup_entry` now creates the hub
  device explicitly before forwarding platforms.
- **Minor, same pass:** D1 WS/services resolve only LOADED entries (commands can no longer
  mutate a coordinator dying inside the unload window); D2 a `fast_source_group` on a
  non-split fast source is SILENTLY cleared by `RoomDefinition.as_dict` (consistent with
  `has_fast_source=False`; the `ValueError` → generic `invalid_room` is gone); D4 UI floor
  `dew_margin_k ≥ 0.5 K` (0 degenerated the local ramp into a hard step); D5 the RH
  staleness pad is LINEAR (`RoomInputs.humidity_stale_frac` ∈ [0, 1] replaces the boolean
  `humidity_stale` — an internal core-contract rename; pad = frac × 1 K, flag when
  frac > 0) — removes the 0.5 factor step at the 60-min edge; D6 the panel tuning stepper
  renders values honestly (ki 0.0015 no longer shows as "0.002"); D7 `get_config` skips a
  corrupted room entry with a log instead of failing the whole reply; D9 the options flow
  prunes a removed room's offset through the loaded coordinator's own Store instance
  (flushes the pending delayed save; lost-update window closed); D10 dead panel flag labels
  (`saturated`/`valve_floor`) removed and the `_save_rooms` docstring no longer mentions the
  retired `live_control`; dev/panel-preview.html re-synced with the v0.6.1 contract
  (`room_temperature_c`, current tuning defaults/knobs, `s2_throttle`, `sensor_lost_rooms`,
  a group-conflict scenario).
- **Backlog (deliberately NOT this round):** `get_tuning.defaults` stays (a future
  "reset to factory" UI); assist-tab mismatch detection for climates without
  `hvac_action`; drawer auto-deselect on room removal; `CONF_ENTITY_HP_ACTIVE` in the
  flow/wiring tab; `unload_ok=False` re-park recovery; the shared farewell registry across
  entries.

---

## 13. Revision — PERMANENT removal of the shadow state: two-state `off` / `live` (2026-07-12, v0.7.0)

> **Status: this REVERSES frozen decisions.** It reverses PRD Aneks §8.9 (the shadow /
> dry-run rollout switch) and reduces §8.11's three-state `RoomControlState` to a
> two-state. Recorded here (and in `prd-control-brain.md` §8.12) as a deliberate, dated
> contract change, not a drift: **2026-07-12, shipped in v0.7.0** (breaking; config-entry
> migration v2 → v3, with the full v1 → v3 chain running in one `async_migrate_entry`
> call).

**Context.** The owner's verdict: the application was getting too hard to use, and shadow
made it worse. Three states per room forced every user-facing surface (select entity,
panel segment, options flow, WS validation, docs) to explain a mode whose only purpose —
"compute in the real mode but write nothing" — was a rollout aid, not a living feature.
The v0.6.x rounds kept paying for it in complexity (K1: shadow rooms voting in the
multisplit arbiter; K10: phantom ON after farewell in shadow).

**Decision.**

- `RoomControlState` = **two-state** `off` / `live`. The "compute but don't write"
  capability is gone entirely.
- **`DEFAULT_ROOM_STATE = "off"`** for new and unknown/corrupted rooms — safe: nothing is
  written until the user deliberately switches a room to `live`.
- **Migration v2 → v3:** every persisted state ∉ {`off`, `live`} maps to `off` (covers
  `shadow` and garbage). This preserves the WRITE behaviour exactly — neither shadow nor
  off ever wrote a command. An entry without a state map keeps not having one (the
  coordinator defaults to `off`). No registry cleanup: `select.*_control_state` keeps its
  domain, unique id and translation key; only the option list shrinks to `[off, live]`
  (PL: „Wyłączony" / „Steruje").
- **Deliberate reporting change (not a sensor regression):** a shadow room used to compute
  FULL commands in the real mode — the panel and diagnostic sensors showed "what it would
  do". An `off` room is fed `Mode.OFF` (report: valve 0 %, fast source off), so that
  observational dry-run value disappears. That loss is the point of the reversal; document
  it so users do not file "my sensors broke after the update".
- **"Manual mode" is now `off`:** to drive a room's hardware by hand, switch it to `off`
  (the farewell parks the split OFF; the valve is closed in cooling, held in heating) and
  operate the devices directly. A whole-home stop = every room `off`.

**Consequences / cleanups.**

- `RoomRuntime.live_control_enabled` (coordinator-internal) removed; the write gate asks
  `get_room_state(name) == live` directly, and `set_room_state` no longer rebuilds the
  cached payload (it notifies entity listeners instead).
- K1 (§12) reduces to "an OFF room does not vote": the adapter still empties
  `fast_source_group` for every non-LIVE room — an OFF room's direction machine (with
  dwell timers) still exists, so the empty group stays as belt-and-braces on top of the
  `Mode.OFF` feed.
- The C5 farewell is now exactly the `live → off` transition (plus entry unload).
- `ROOM_STATE_SHADOW`, `CONF_LIVE_CONTROL` and `CONF_PARTICIPATES` remain in `const.py` as
  **legacy-migration-only** constants read (or transiently written) exclusively by
  `async_migrate_entry`.
- `CONTROL_ALGORITHMS_REVIEW.md` is deliberately untouched: SHADOW appears there as a
  rollout-methodology stage in a frozen historical review, not as a product feature.

Unchanged: the three external outputs, the safe-degrade contract, the dew-point layers,
the control law, and the no-reload-on-state-change rule (PID integrator preserved).

---

## 14. Revision — opt-in heat-pump link + per-room quiet hours (2026-07-12, v0.8.0)

> **Status: this EXTENDS a frozen contract.** PRD §8.10 excludes heat-pump control and
> Q8/§8.8 defines "three outputs"; the extension is recorded as a deliberate, dated
> owner decision (2026-07-12) in `prd-control-brain.md` §8.13. Tortoise STILL never
> controls the compressor, the power or the pump's own weather curve. Everything below
> is **opt-in**: an unconfigured "Heat pump" options section keeps the pre-0.8.0
> behaviour bit-for-bit.

**A. Heat-pump link (B2).** New pure core module `core/hp_link.py` (no `homeassistant`),
adapter plumbing in `coordinator._sync_heat_pump` / `readers.read_select_option` /
`writers.write_hp_mode` + `write_hp_setpoint`, options-flow leaf `heat_pump`, websocket
`get_live.heat_pump` + `set_hp_dhw`, and a new panel tab.

- **Direction sync write table** (`direction_option(mode, current)`; HeishaMon-style
  select, matching case-insensitive, output canonical, the WRITE re-canonicalised to the
  live entity's own option list — no match ⇒ skip + log, never a blind
  `select_option`):

  | Tortoise \ pump | Heat only | Cool only | Auto | Heat+DHW | Cool+DHW | Auto+DHW | DHW only | unknown/None |
  |---|---|---|---|---|---|---|---|---|
  | heating | (in sync) | Heat only | Heat only | (in sync) | Heat+DHW | Heat+DHW | **skip** | skip |
  | cooling | Cool only | (in sync) | Cool only | Cool+DHW | (in sync) | Cool+DHW | **skip** | skip |
  | transitional / off | — never forces a direction — | | | | | | | |

- **DHW rules (the race with the external DHW automation):** the `+DHW` part ALWAYS
  survives a direction write; `"DHW only"` is never written as a direction; a pump
  currently in `"DHW only"` is never written at all (the DHW automation is mid-cycle and
  restores its remembered direction itself — writing would race it; the panel shows the
  divergence with a DHW-only note instead). Because `Heat+DHW` counts as "in sync" for
  heating, the DHW automation's normal operation produces ZERO false divergences.
- **Anti-flap:** a direction write needs (Tortoise mode changed since our last write OR
  divergence persisting ≥ 2 consecutive cycles) AND ≥ 15 min since our last direction
  write. Water setpoints go through a 0.5 K threshold + the 45-min re-assert (the same
  S3 philosophy as the splits: the machine stays the owner, self-heals a manual change).
- **Water setpoints:** cooling = `max(cooling_supply_base_c, global safe dew point)`
  (never into the condensation zone); heating = optional
  `WeatherCompCurve(heating_supply_base_c, heating_supply_slope, ff_neutral_c)` clamped
  to fixed 20–40 °C (firmware/screed limits, deliberately not knobs). No outdoor
  reading ⇒ no heating write that cycle (the pump keeps its last setpoint — bounded by
  its own firmware).
- **Gating:** pump writes only while NOT parked and ≥ 1 room is `live` (a global
  actuator may be touched only while somebody handed Tortoise the controls). The panel
  shows "writes paused" so a divergence in an all-off home does not look like a bug.
  The farewell/unload deliberately does NOT touch the pump.
- **New GLOBAL-ONLY knobs** `cooling_supply_base_c` (18 °C, 10–25),
  `heating_supply_base_c` (26 °C, 20–40), `heating_supply_slope` (0.5 K/K, 0–2):
  building-level physics, so `coerce_tuning_values` rejects them for a room scope and
  the panel renders their tuning group only in the Global scope
  (`HP_GLOBAL_ONLY_KNOBS`).
- The dormant `CONF_ENTITY_HP_ACTIVE` moved from the coordinator's dead zone into the
  same options section; the global entity feeds `hp_active_for_ufh` of EVERY room
  (integrator freeze during DHW/defrost); a legacy per-room key still overrides.

**B. Per-room assist quiet hours (B1).** Optional per-room window (`"HH:MM"` pair, may
cross midnight) in which the fast source MAY run. THE ONE HARD RULE preserved: the
adapter evaluates the window against HA's LOCAL clock and hands the core a plain
`RoomInputs.fast_source_allowed` bool (additive, default `True`); the pure window
arithmetic (`window_allows`) lives in `core/fast_source.py` so the midnight/edge cases
are unit-tested offline. Outside the window the controller requests OFF through the
NORMAL `decide()` path: an idle unit never engages, a running unit stops only after its
min-ON dwell (compressor protection outranks punctuality; flag
`fast_source_quiet_hours`, severity "ok"). TRANSITIONAL suppresses the split too (quiet
is quiet — a deliberate, documented trade-off), while the S3/S4 emergency force-on
IGNORES the window (room safety outranks acoustic comfort). The window edge is honoured
at control-cycle granularity (5 min) — deliberately no extra time listener. Validation:
both times or neither, `start != end` (`quiet_window_invalid`).

**C. Panel UX round (A1–A7, same release):** sentence-case table headers; a shared
inline confirmation popover for the room-state segment and the home-mode switch (no
native `confirm()`); the Rooms table's Tryb/Temp columns grouped under a "Wspomaganie"
two-row header with honest empty states; the Tuning tab re-laid as titled vertical
groups with full knob names; diagnostics folded into their own collapsed section;
"Okablowanie" renamed to "Czujniki i sygnały" with the live VALUE as the headline and
the entity id behind an "i" icon; a tooltip + manual paragraph explaining the deliberate
±1 K split-target offset (S12).

---

## 15. Revision — hydraulic no-flow watchdog (S6) + valve write-path fixes (2026-07-13, v0.9.0)

> **Status: this EXTENDS the frozen contract additively.** New `RoomInputs` /
> `RoomReport` / `ControllerConfig` fields (all defaulted, so old callers are
> unchanged) and a new optional `BuildingController.step` keyword. No output-contract
> change: the three external outputs (valve %, fast-source command, safe dew point)
> are untouched; the watchdog only ADDS flags and freezes integrators. Motivated by a
> production incident on the owner's LIVE house (11 rooms cooling) — see GitHub issue
> #4 and `docs/NO_FLOW_WATCHDOG.md`.

**The incident.** After a power-event reboot both valve controllers accepted targets
but never moved the actuators, and the HA `valve` entities **echoed the commanded
position back as `current_position`**. For ~2.5 h of LIVE cooling nothing on the data
path noticed: `valve_mismatch` never fired (command == feedback, perfectly), the
integrators wound up against an unresponsive plant, and a frozen-**open** cooling loop
sat outside BOTH condensation guards (S2 modulates the *command*; the water kept
flowing). Only a human watching the manifold rotameters caught it.

**A. Hydraulic no-flow watchdog (new safety rule S6, pure core `core/flow_watchdog.py`).**
The already-wired per-loop `entity_supply` / `entity_return` probes are an INDEPENDENT
physical witness of actuation; the watchdog **never trusts the `valve` entity feedback**
(that channel proved it can lie end-to-end).

- **`loop_no_flow`:** a room LIVE + heating/cooling, valve commanded ≥
  `flow_open_threshold_pct` for ≥ `flow_response_window_min`, but the loop shows no
  hydraulic signature — `|T_supply − T_return| < flow_epsilon_k` AND no `T_supply`
  displacement toward the source side — raises the flag and FREEZES that room's
  integrator (stops the wind-up seen in the incident). Window starts on the second
  cycle (the first only establishes the reference), so the flag lands within one
  `response_window` + one cycle.
- **`loop_stuck_open`:** valve commanded 0 for ≥ the window but the loop keeps passing
  water — raises the flag and, in cooling, feeds the global safe-dew logic (treated as
  "cold floor active" so the pump floor cannot drop under a loop that is really flowing
  cold). **The witness for this flag was replaced 2026-07-13 (§16, issue #6):** it no
  longer reads the loop water probes at all — see below. **SUPERSEDED by §17
  (2026-07-13): `loop_stuck_open` was removed entirely — see §17.**
- **False-positive gating (circulation evidence).** A loop is only judged when
  circulation is plausible: at least one OTHER loop in the system shows a healthy ΔT,
  or the optional `entity_global_supply` manifold probe reads source-side. Otherwise
  the window is HELD (no accrual). Multi-loop rooms are judged per loop, worst
  reported. The RETURN probe is weighted more than supply (supply sometimes sits on the
  manifold bar before the valve). Rooms without probes: watchdog silently inactive,
  panel shows "—". The global-probe path is **SUSPENDED while any room reports
  `hp_active_for_ufh is False`** (DHW/defrost): a manifold probe can keep reading
  source-side while every UFH loop is legitimately starved, so trusting it would bank
  no-flow windows on healthy loops. The per-loop witness path needs no such guard (the
  other loops are not flowing either → HELD).
- **Reaction is passive:** flag + a per-room `binary_sensor` (`flow_fault`, device class
  PROBLEM) + integrator freeze. **No automatic valve banging.**

**B. Actuation self-test (manual service `tortoise_ufh.test_actuation`).** Per room, on
demand (never scheduled): drive the valve to 100 % for `duration_minutes` (20–30),
verify the loop's ΔT / supply-trend response, report pass/fail in the panel and via the
`actuation_test_running` / `actuation_test_failed` flags. Requires the room LIVE with
probes; overheat / condensation safety aborts it; the integrator is frozen during the
run.

**C. Valve write-path fixes (`writers.py`).**

- **Re-assert parity with the splits.** Valve commands now re-assert unconditionally
  every ~45 min (`_VALVE_REASSERT_SECONDS`) AND immediately when the entity's feedback
  diverges from the last CACHED command by ≥ `_VALVE_FEEDBACK_DIVERGENCE_PCT` (10 %,
  numerically the S8 mismatch tolerance). This heals the class where an external
  controller reset reverts targets to its park position and the write cache said
  "already written" forever. An echoing (lying) channel never trips this trigger — that
  failure mode is S6's job.
- **Diagnosis correction (issue #4 item 2).** The reported "threshold compares the wrong
  baseline" bug did NOT reproduce: the threshold already compares against the
  last-WRITTEN position, so a slow 30 % → 0.8 % decay DID emit writes
  (30 → 24.5 → 17.5 → 10.5 → 4.0). The real residue was the final sub-threshold tail
  (ending ~4 % while the command is 0.8 %); the re-assert above closes it within one
  period. The write cache is still updated on DISPATCH (fire-and-forget) — a dropped
  write is healed by the re-assert / feedback triggers, never trusted forever.

**Simulator.** `BuildingSimulator` gained fault injection (`set_actuator_fault` +
`update_loop_probes`) so the digital twin can model an echoing, frozen actuator; the
acceptance-criteria tests (`tests/simulation/test_no_flow_watchdog.py`) exercise
`loop_no_flow` / `loop_stuck_open` and assert ZERO false S6 flags across the existing
gate scenarios (hot_july, night_setback, cold_snap, solar_overshoot).

**New tuning knobs** (all in the "Flow watchdog (S6)" tuning group): `flow_epsilon_k`
(0.3 K default), `flow_open_threshold_pct` (15 %), `flow_response_window_min` (45 min,
UI floor 30, 1440 disables).

## 16. Revision — `loop_stuck_open` becomes a room-air consequence, COOLING-only (2026-07-13, issue #6)

> **SUPERSEDED by §17 (2026-07-13):** `loop_stuck_open` was removed entirely. The room-air
> witness described below could not hard-verify actuation, so it produced only false
> alarms. The content is retained as the decision record that led to the removal.

> **Status: EXTENDS the frozen contract additively — no signature or output change.**
> Only the INTERNAL witness of the `loop_stuck_open` flag is replaced; the flag name,
> the per-loop `"stuck_open"` status, the panel chip and the stuck-open → global-dew feed
> are all unchanged. `flow_epsilon_k`, `flow_open_threshold_pct` and
> `flow_response_window_min` are untouched (the first two now serve `loop_no_flow` only).
> Motivated by GitHub issue #6 ("S6 loop_stuck_open false positives") — the owner's
> comment plus a later clarification are the binding ruling.

**The problem.** The §15 `loop_stuck_open` witness read the loop supply/return water
probes: a clear ΔT ≥ `flow_epsilon_k` refined by the return probe sitting on the source
side of the room. In production this false-alarms: **there are two probes, on supply and
return, and when the valve is closed the manifold bar still cools them by conduction.** A
sub-Kelvin ΔT across a manifold-bar pair is below the noise floor, and a whole manifold of
closed loops sitting uniformly cold raised spurious stuck-open flags for any
`flow_epsilon_k`.

**The ruling (owner comment + clarification).** Drop **all** water-probe temperature tests
from `loop_stuck_open`. The manifold probes are considered unreliable for this decision,
full stop — no hydraulic corroborator, no return-vs-room test, no supply/return ΔT. The
ONLY witness is the **thermal consequence in the room**: a leaking closed valve keeps the
room on the wrong side of setpoint. `loop_no_flow` keeps its small-ΔT / displacement test
exactly as before; `flow_epsilon_k` remains the through-flow floor for `loop_no_flow` only.

**What we implemented (`core/flow_watchdog.py::LoopFlowMonitor._update_stuck`).**
- Reads **no water probes at all**. New per-monitor state `_ref_room_c` (the room temp
  captured when the stuck window opened). Two fixed module constants (NOT knobs):
  `_STUCK_ROOM_SETPOINT_MARGIN_K = 0.5` (how far below setpoint the room must sit — above
  the 0.3 K deadband and the split's 0.5 K quanta) and `_STUCK_ROOM_RELAX_TOL_K = 0.2`
  (tolerance for "not relaxing back up toward setpoint" vs `_ref_room_c`). The old
  probe-based stuck-open test (a supply/return signature refined by a return-probe check
  against the room) and its dedicated margin constant are **deleted**.
- Fires only when a CLOSED-COMMANDED **cooling** room sits ≥ 0.5 K below setpoint and is
  not relaxing back, AND no actively-cooling split (`fast_direction`, the direction the
  split was actually running in at step entry) explains the cold room. The window still
  requires `flow_response_window_min` of continuous signature; it is NOT gated by the
  circulation evidence (the room-air consequence is its own proof).
- **COOLING-only.** Heating is out of scope. The room-air witness cannot separate a
  stuck-open **heating** valve from a room warmed by solar gain or held above a lowered
  night-setback setpoint — both leave a healthy closed-valve room persistently ABOVE
  setpoint for hours behind the high-mass slab. Empirically the heating mirror re-raised
  false `loop_stuck_open` across `solar_overshoot` (rooms ~2.8 K over setpoint, valves
  correctly closed, for thousands of cycles) and `night_setback` (a room held ~1.3 K over
  the lowered setpoint, cooling at ~0.04 K/h) — the very false positives this issue exists
  to remove, and both are named criterion-4 gate scenarios that must stay silent. A
  stuck-open heating valve is a comfort issue, never the condensation safety concern that
  motivates the flag, and the one safety consumer (the global safe dew-point floor) is
  cooling-only anyway. **Trade-off:** a genuinely stuck-open heating valve is no longer
  flagged by S6 (still surfaced indirectly by the room overheating).

**Decoupling.** `flow_epsilon_k` and the loop water probes no longer influence
`loop_stuck_open` at all; `_flow_evidence` and the `ActuationSelfTest` are untouched
(still probe-based, for `loop_no_flow` and the manual self-test). The stuck-open →
global-dew feed and the `_update_no_flow` path keep their §15 behaviour verbatim.

## 17. `loop_stuck_open` detection removed (2026-07-13)

> **Status: SUPERSEDES the stuck-open parts of §15 and §16.** Removes the `loop_stuck_open`
> flag, its per-loop `"stuck_open"` status, the stuck-open → global-dew feed and the panel
> chip. **`loop_no_flow`, the `ActuationSelfTest`, the `flow_response_window_min` /
> `flow_epsilon_k` / `flow_open_threshold_pct` knobs and the `flow_fault` entity are
> UNCHANGED** — this decision touches only the stuck-open reverse detection.

**Why.** After §16 reduced the stuck-open witness to the ROOM-air consequence (a closed
COOLING valve suspected of leaking because the room sits below setpoint and will not relax
back), that witness proved to be the *wrong kind of evidence*: room air temperature versus
setpoint cannot hard-verify whether the actuator physically passes water. Many benign states
reproduce the same air signature — a cold room after a setpoint raise, a neighbouring cold
mass, a slow-draining slab, sensor offsets — so the flag delivered false alarms without ever
proving actuation. Unlike `loop_no_flow`, whose witness is the loop supply/return water
probes (a direct hydraulic measurement), stuck-open had no independent physical corroborator
once the manifold-conducted probes were (correctly, §16) declared unreliable for a closed
loop. A detection that cannot distinguish its fault from ordinary operation is net-negative.

**What replaces it.** Nothing, for now. Hard verification that a *closed-commanded* valve
truly stops flow is a real need, but it requires a deliberate, actuation-level mechanism
(e.g. a scheduled or on-demand close-and-measure probe test), NOT a passive room-temperature
heuristic. That is deferred to a future, dedicated feature. In the meantime a genuinely
leaking closed cooling valve surfaces indirectly (the room over-cools) and the global safe
dew-point floor still protects every room that is *actually* cooling.

**What was removed (`core/flow_watchdog.py`, `core/controller.py`).** The
`LoopFlowMonitor` stuck-open window (state `_stuck_elapsed_s` / `_ref_room_c` /
`_stuck_active`, the `stuck_open_active` property, `_reset_stuck` / `_update_stuck`), the
module constants `_FLOW_CLOSED_CMD_PCT` / `_STUCK_ROOM_SETPOINT_MARGIN_K` /
`_STUCK_ROOM_RELAX_TOL_K`, the `"stuck_open"` entry in `_LOOP_STATUSES` and
`RoomReport._LOOP_FLOW_STATUSES`, `FlowWatchdog.stuck_open_active`, the
`room_temperature_c` / `setpoint_c` / `fast_direction` parameters threaded into the
watchdog `update`, `BuildingController._stuck_open_dew_point` and the `else` branch that
re-included a stuck-open room in the global dew maximum, the `loop_stuck_open` flag merge in
`_finalize`, and the `flow_fault` binary sensor's second trigger. A command below the open
threshold now simply resets the no-flow window and reports `"ok"`. `_fast_entry_state`
STAYS (it still feeds `fast_source_locked_on`); only its hand-off to the watchdog is gone.

## 18. Cooling boost-hold — the split must not starve the floor on the measurement path (2026-07-13, P2)

> **Status: additive.** New behaviour in `RoomController._active_result` (COOLING only). **No new
> tuning knob, no new `RoomReport` field, no change to the frozen I/O contract, HEATING untouched.**

**The failure loop.** The frozen anti priority-inversion invariant (ALGORITHM_SPEC §8.3) guarantees
the split's *command* never lowers the floor valve, because the valve is computed independently of,
and before, the fast-source decision. But the owner (11 cooling rooms LIVE) reported the floor valve
collapsing to 0 during a split boost anyway. The leak is on the **measurement path**: in COOLING the
engaged split cools the *air*, so the room error `t_room − setpoint` and the filtered trend fall
toward zero → the cooling PI and the trend damper both retreat → the floor valve drops to 0. The
split thus *indirectly* strangles the base floor source at the exact moment the high-mass slab most
needs discharging. Two consequences: (a) the slab never discharges through the floor during the
boost (wasted capacity), and (b) when the split releases, the air rebounds off the still-warm slab
and re-engages the split — short-cycling the compressor.

**Chosen fix (variant: "hold-not-close", #1 only).** While a room is in `Mode.COOLING` and its split
was ENGAGED at step entry (`_fast_entry_state is FastSourceMode.COOLING`), hold the floor at a `max`
floor of the raw (pre-throttle, clamped) valve it held the cycle the split engaged:
`if boost: valve = max(valve, _boost_hold_pct)`. The guiding principle: *since the boost only
engaged because the floor alone could not cope, the split cannot be the reason the floor retreats.*
This is the §8.3 invariant extended from the command path to the measurement path. Ordering is
load-bearing: hold (step 11b) → S2 dew throttle (step 12, still scales the held value; `dew_factor =
0` still closes) → hard-safety override (sensor-loss still parks at 0). The **global safe dew point
(§7.1) is never weakened** — the hold only ever raises *flow* at an already-safe supply, so it is
condensation-neutral. The snapshot is per-engagement (cleared on release, mode change, and every
inactive cycle via `_note_inactive`), one-cycle-delayed (the slab's mass absorbs the lag), and not
persisted across a restart (a lost snapshot merely reverts one engagement to the old behaviour).

**What was deliberately dropped as unnecessary.** The plan proposed three elements; only #1 shipped.
#3 (zero the trend damper during boost) is redundant: `max(valve, hold)` already dominates the trend
term regardless of its value, so suppressing it changes nothing on the emitted valve. #2 (freeze the
integrator during boost) was evaluated on the digital twin and dropped: the concern was that
split-cooled air (negative cooling error) would discharge the integrator and lag the floor after
release, but the twin showed the integrator does **not** collapse (post-release `i_term` ≈ 23 %*h
with or without the hold, post-release valve ≈ 2.5 % — correctly low, because the room is inside the
band after a release) — so #2 addresses a problem that does not occur. Keeping the mechanism to its
minimal `max()` floor honours the project's simplification mandate.

**Rejected alternatives (from the plan).** A separate "slab-mass" control lane (needs a floor sensor
/ mass estimator and touches the frozen I/O contract — too large a conceptual jump) and dwell/
hysteresis alone (treats the symptom — cycling — not the cause — the un-discharged slab; the dwell
machine already exists and stays as the second line of defence).

**Evidence.** Digital-twin reproduction (`tests/simulation/test_boost_hold.py`, heavy-slab
well-insulated room, hot day, 24 h, deterministic): pre-fix baseline collapsed the engaged-cycle
valve to a **0.0 %** mean and cycled the split **24×**; with the hold the engaged valve mean is
**35.9 %** (min 20.7 %) and the split cycles **22×**, the hot slab discharging 27 → 24.1 degC. Under
78 % humidity the worst slab-vs-dew margin is identical with and without the hold (condensation-
neutral). Unit coverage in `tests/unit/test_controller_boost_hold.py` (hold ≥ snapshot despite
negative error; hold × throttle ordering; sensor-loss safety wins; HEATING byte-identical; snapshot
lifecycle).

**Open items for the owner (assumptions, non-blocking).** No diagnostic `"boost_hold"` flag in the
report/panel (would be new i18n strings — a separate small PR if wanted). No on/off knob for the
mechanism (it is always active in cooling); a `boost_hold_enabled` knob can be added later if the
owner wants a switch.

## 19. Panel severity taxonomy — an `info` tier for intentional steady states (2026-07-14, v0.11.1)

> **Status: panel-only.** Pure display taxonomy in `tortoise-ufh-panel.js`. **No core change, no
> `RoomReport` field, no change to the frozen I/O contract, no new tuning knob.** The core still
> emits the `cooling_disabled` flag exactly as before; only its rendered colour changed.

**The noise.** The owner (11 cooling rooms LIVE) opts some rooms out of cooling. Such a room emits
the `cooling_disabled` flag *for the entire cooling season* — a permanent, intended configuration
fact, not a fault. The flag was registered `sev: "warn"`, and the flag annunciator rolls a room's
worst flag up into its status dot (`_severity` → `max(SEV_RANK[...])`), so an opted-out room glowed
yellow all season. That is alarm fatigue: a steady, deliberate state reading with the same visual
weight as a stale-humidity warning or a dew-throttle event. The same fact was *already* surfaced
neutrally elsewhere (the per-room "safe dew-point exclusion reason" row and the hero dew-chip
tooltip), so the yellow flag was largely duplicate noise.

**Chosen fix (owner-selected).** Add a neutral severity tier **`info`** between `ok` and `warn`
(`SEV_RANK = { ok:0, info:1, warn:2, problem:3, alarm:4 }`, `_sevName` extended to match) and
reclassify `cooling_disabled` from `warn` → `info`. `info` renders in the calm `--t-info` token
(theme-aware via HA `--info-color`), so the room dot, the flags stat tile, the annunciator summary
and the flag row all read as a quiet neutral rather than a yellow alarm. Crucially `info` still
*rolls up*: a room with `cooling_disabled` **and** a genuine `warn`/`problem` flag escalates to that
worse tier as before — `info` only sets the floor when it is the single worst thing about the room.
The information is preserved (the flag stays visible and fully explained); only its alarm connotation
is removed.

**Alternatives considered (with the owner).** (a) `sev: "ok"` (green) — a one-word change but
semantically wrong: green reads as "on / running", contradicting a flag whose text is "cooling
disabled". (b) Drop the flag entirely and lean on the existing dew-exclusion reason — simplest, but
loses the at-a-glance "this room is deliberately non-standard" signal on the Rooms table (a plain
green dot would be indistinguishable from a normally-cooling room). (c) A per-entry `roll: false`
that keeps the chip but excludes it from the dot roll-up — a half-measure that leaves the chip itself
yellow. The `info` tier was chosen because it is the general, reusable fix: intentional steady states
now have a home distinct from both "good/running" and "needs attention", and other informational
flags (e.g. `fast_source_cannot_cool`) can migrate to it later without new machinery.

**Guard.** `tests/unit/test_panel_i18n.py` validates every `FLAG_LABELS.sev` against a fixed
vocabulary; `info` was added to `_VALID_FLAG_SEV` so the registry-completeness test admits the new
tier. The class-reset list in `applyRow` gained `sev-info` so a row cannot retain a stale severity
class. `-m unit` green.

## 20. German (de) localization + pre-publication doc audit (2026-07-14, v0.12.0)

> **Status: additive (i18n + docs).** No core change, no `RoomReport` field, no I/O-contract
> change, no new tuning knob, no config migration. Prompted by a public-release readiness pass
> (three read-only reviewers: privacy, i18n, docs-vs-code).

**Readiness pass.** Before promoting the public repo, three auditors ran read-only. Privacy: clean —
the real coordinates / email / user-paths / secrets are absent from the tree AND the full git
history (no history rewrite needed); only the owner's own commit-author metadata and intentional
attribution remain. i18n: EN/PL at parity, and — critically — the EN fallback is correct on every
surface, so a non-`pl` locale already renders 100% English (no blanks / raw keys / Polish leak).
Docs-vs-code: one real value drift (`valve_write_threshold_pct` documented `2.0` while the code and
the doc's own changelog say `5.0`) plus three completeness gaps; the code itself was clean and THE
ONE HARD RULE holds.

**German added.** German (`de`) is now a third first-class language with English as the guaranteed
fallback: `translations/de.json` (mirror of `en.json`), a `STR.de` block in the panel, `de`/`descDe`
on every `FLAG_LABELS` entry, and — the load-bearing edit — `_resolveLang` maps a `de*` locale to
`"de"` (without it `STR.de` is dead code and a German user still gets English). `_flagDesc` gained a
`de` branch. `tests/unit/test_panel_i18n.py` now ENFORCES DE (STR.de ≡ STR.en; `de.json` in the
JSON-parity quad; every knob/surface loop and the flag-registry completeness check include `de`), so
DE can never silently drift out of parity. `strings.json` needs no DE variant (it is the English
source; HA derives per-locale from `translations/*.json`).

**Docs restructured.** The single top-level Polish-named `docs/INSTRUKCJA.md` was confusing for a
multilingual project. The manual now lives in `docs/manual/` as per-language files (`pl.md`, `de.md`,
plus an index `README.md`), section numbering kept consistent across languages so a "§N" reference
resolves the same everywhere; every reference (panel strings, `NO_FLOW_WATCHDOG.md`) was repointed.
The root `README.md` gained a language switcher (English page · Polish manual · German `README.de.md`);
`README.de.md` is a full German landing page. English has no standalone manual yet — the README is
its overview (a deliberate, revisitable choice; there is likewise no Polish README, so the switcher
points each language at its best existing entry point).

**Doc-currency fixes.** `ALGORITHM_SPEC.md` write-threshold `2.0`→`5.0`; README panel tabs `4`→`6`
(Flags + Heat pump) plus a heat-pump-link / quiet-hours feature line; `INSTRUKCJA`/`pl.md` §6 gained
the `flow_fault` binary sensor; `CLAUDE.md` "Four tabs"→"Six tabs". Merge gate green (unit,
`mypy` core 22, ruff, format, `node --check`).

## 21. Cooling setpoint-flicker — trip the Panasonic compressor out of its fixed deadband (2026-07-15, v0.13.0, issue #7)

> **Status: additive opt-in hp-link behaviour.** New pure-core state machine (`SetpointFlicker` +
> `cooling_demand` + `FlickerDecision` in `core/hp_link.py`) and adapter wiring only. **No change to
> the frozen three-output I/O contract** (the flicker only bends the ONE existing cooling-setpoint
> write for a single cycle), **no `RoomReport` field, no config migration.** Off by default; the
> whole feature is inert unless the owner opts in AND maps the pump entities.

**The problem (verified on the owner's live unit).** On a Panasonic Aquarea the cooling compressor is
gated by a **FIXED 3 K hysteresis on the RETURN (inlet) water**: it STARTS when the inlet reaches
`cool-setpoint + 3 K` and STOPS at ~setpoint. That band is firmware-baked and cannot be changed over
HeishaMon. So through long idles the return parks near `setpoint + 3 K` while rooms still call for
cooling and the high-mass floor under-delivers — the water is, on average, warmer than the dew-safe
setpoint we wrote.

**The mechanism.** When Tortoise sees the pump **idle in the deadband with genuine unmet demand**, it
drops the WRITTEN cooling setpoint for ONE cycle to the dew-safe pulse floor `p`, which trips the
pump's own `+3 K` rule and starts the compressor; the very next cycle it restores the normal
dew-safe setpoint, so the run finishes at the safe value. Net effect: a **tighter EFFECTIVE deadband
→ colder AVERAGE water**, while the return floor stays dew-safe. Manually verified on the live pump.

**Settled decisions.**

- **The 3 K START band is a fixed constant** (`FLICKER_START_OFFSET_K = 3.0`), NOT a variable —
  it is a property of the pump firmware, verified on the owner's unit.
- **The pulse floor is the RAW worst-room dew point**, ceiled onto the pump's own grid step so it can
  never land BELOW the dew point. `p = ceil((safe_dew − FLICKER_DEW_RESERVE_K) / step) · step`, with
  `FLICKER_DEW_RESERVE_K = DEW_MARGIN_DEFAULT_K = 2 K` — the exact margin the global safe dew point
  adds on top of `max_over_cooled(T_dew)` (kept equal by referencing the shared constant, and equal
  to `controller.GLOBAL_SAFE_DEW_MARGIN_K`), so the reserve subtraction recovers the raw dew point
  precisely. `trigger = max(w + band, p + 3)` — the pump arms only once the return has climbed
  `band` above the written setpoint AND high enough that a pulse to `p` would actually cross the
  pump's `+3 K` start. If `p` cannot drop even one grid step below `w` (`p > w − step`, e.g. a coarse
  1–3 K pump grid on a humid day) the pulse is withheld and `flicker_dew_blocked` is flagged —
  cooling stays dew-safe over cooling harder.
- **Idle is detected by compressor frequency == 0**, not by a falling-return heuristic. Simpler and
  unambiguous; a missing inlet/frequency reading disarms the machine and flags `flicker_no_sensor`.
- **Four global-only knobs, exposed on the Tuning tab + options flow** (rejected per-room like the
  other HP-water knobs): `hp_flicker_band_k` (1.5, [0.5, 3.0] K), `hp_flicker_stuck_minutes` (10,
  [5, 120]), `hp_flicker_min_off_minutes` (20, [5, 120] — compressor-protection cooldown between
  forced starts), `hp_flicker_max_starts_per_h` (2, [1, 6] — a hard cap over a rolling hour). The
  band must stay below 3 K to actually tighten the deadband.
- **The outlet/supply entity is DIAGNOSTIC-ONLY in v1** — read and surfaced on the Heat pump tab, no
  logic consumes it. It is mapped now for a future supply-side condensation guard.
- **The machine is persistent and ticked EXACTLY ONCE per cycle** with the SAME real measured `dt`
  the core step used (one `SetpointFlicker` per config entry, rebuilt only on a reload, never
  re-created or double-ticked — the class of bug fixed in `FastSourceMachine` on 2026-07-10). It
  starts in `cooldown` with the full gap to run so an HA restart never pulses immediately; the
  adapter's empty write cache makes the first cooling cycle write the normal target — self-healing.
- **The restore is unconditional** (`FlickerDecision.restore_pending`): if the mode flips out of
  COOLING one step after a pulse, the adapter still writes the normal target so the pump is never
  left parked at the low pulse floor (mirrors the C5 farewell philosophy). Because both the drop and
  the restore are ≥ one grid step (≥ 0.5 K on a real pump), they cross the writer's existing 0.5 K /
  45-min skip with no throttle change.
- **Tooltips (ⓘ) in PL/EN/DE** for every new entity, knob and diagnostic label (panel `STR` tables +
  options-flow `data_description` across `strings.json` and the three `translations/*.json`), plus
  three new `FLAG_LABELS` rows (`flicker_pulsing` info, `flicker_dew_blocked` info,
  `flicker_no_sensor` warn — group `assist`).

**Alternatives considered.** (a) Model the 3 K band as a knob — rejected: it is a firmware constant,
a knob would only invite mis-tuning. (b) Pulse to a fixed low setpoint (e.g. 5 °C) — rejected: it
would cross the dew point on humid days; pulsing to the raw dew point is the aggressive-but-safe
choice. (c) A falling-return heuristic for idle — rejected in favour of the unambiguous
compressor-frequency == 0 reading. (d) Route the pulse through a new forced-write path — rejected: the
drop/restore magnitudes already clear the existing write threshold, so no writer change was needed.

**Guard.** Pure-core unit tests for the five machine scenarios + `cooling_demand` + the four knob
validations (`tests/unit/test_flicker.py`, `test_config_models.py`); the panel-i18n parity/knob-count
guard bumped 21→25 knobs and pinned the flicker STR surfaces. Merge gate green (unit, simulation,
`mypy` core, ruff, format, `node --check`).

## 22. Force-cooling-start UX refinement — on/off becomes a tuning knob, jargon renamed (2026-07-15, v0.13.1)

Owner UI review of the shipped §21 feature found two rough edges on the panel Tuning tab: the labels
were internal jargon ("Flicker: …" prefixed on every knob) and there was **no on/off control on the
tab** — the enable lived only in the options-flow "Heat pump" step, so you could tune the feature
where you could not switch it on.

Two decisions:

- **The flicker on/off is now a global boolean tuning knob** (`ControllerConfig.hp_flicker_enabled`,
  default `False`), NOT a `CONF_HEAT_PUMP` option. `CONTROLLER_BOOL_KNOB` (a single string) was
  generalised to `CONTROLLER_BOOL_KNOBS` (a tuple) so the existing tuning machinery — `get_tuning` /
  `set_tuning`, the options-flow `algorithm`/`settings` schema, and the panel's generic
  `type:"bool"` toggle — renders it as a toggle at the top of the **"Force cooling start"** group,
  beside its four timing knobs. The coordinator reads it from `self._global_config.hp_flicker_enabled`.
  The three flicker ENTITIES (return, compressor-frequency, outlet) stay in the "Heat pump" step (they
  are I/O), consistent with how the pump's water-setpoint knobs already sit on the Tuning tab while its
  entities sit in the Heat pump step. No config migration — the feature shipped OFF the same day and
  was not yet enabled in production; a v0.13.0 install that had toggled it re-enables via the new knob.
- **User-facing rename (PL/EN/DE):** the group and every knob drop the "Flicker" jargon — group
  "Force cooling start" / "Wymuszanie startu chłodzenia" / "Kühlstart erzwingen"; knobs become natural
  phrases ("Target cooling deadband", "Delay before forcing a start", …). The internal code name
  stays `flicker` everywhere (identifiers, docs, flags). The Heat-pump-tab diagnostics card header is
  renamed to match.

Panel-only + a bounded adapter/core refactor; no I/O-contract change. Merge gate green (unit 532,
simulation 39, `mypy` core, ruff, format, `node --check`).

**Follow-up (v0.13.2, 2026-07-15) — panel JS cache-busting.** The v0.13.1 rename was correct in code
(tests proved it) but a live install still showed the OLD panel: the JS is served from a fixed static
path (`/tortoise_ufh_panel/panel.js`) with no version, so the browser/HA kept serving the cached file
and the new panel never loaded — the symptom was the new `hp_flicker_enabled` toggle appearing under
the fallback "Pozostałe / Other" group with a raw, untranslated label (new backend + stale front-end).
Fix: `panel_config()` now appends `?v={integration version}` to `module_url`, so every version bump
changes the URL and forces a fresh fetch (the static route ignores the query string). This is why
earlier panel-only releases sometimes "looked wrong" until a manual hard-refresh — now updates
self-bust. `tests/ha/test_panel.py` asserts the versioned URL still points at the static route.

**Follow-up (v0.13.3, 2026-07-16) — the rename reaches the last user-facing surfaces.** A review of
v0.13.1/v0.13.2 found the "Flicker" jargon still visible in four places the rename had missed: the
panel `FLAG_LABELS` for `flicker_pulsing` / `flicker_dew_blocked` / `flicker_no_sensor` (labels +
tooltip descriptions, PL/EN/DE), the Heat-pump options step's entity labels ("optional, for flicker")
and the return-probe description in `strings.json` + all three translation files, the flag-table rows
in `docs/manual/{pl,de}.md`, and the README feature blurb. All now use "Force cooling start /
Wymuszanie startu chłodzenia / Kühlstart erzwingen" phrasing. Identifiers stay `flicker_*` per the
§22 rule — the flag CODES in the manuals' first column and the `hp_flicker_*` JSON keys are code, not
copy. Text-only; no logic change.

## 23. Force-cooling-start demand gate — loop-weighted valve opening vs the buffer tank (2026-07-16, v0.14.0)

**Problem (owner observation, first cooling nights on v0.13.x):** the flicker armed whenever ANY
cooled room sat >= 0.3 K above its setpoint — ~7 forced compressor starts in one night for
essentially ONE room ~1 K over a low 21.5 °C setpoint, every other room at/below setpoint. The
hydraulic context the gate ignored: the installation has a **100 l parallel buffer tank**. A small
draw — even a single loop commanded 100 % — is comfortably covered by the cold water already
stored; a forced start in that situation merely knocks the buffer down 1–2 K (~0.3 kWh of cold)
and short-cycles the compressor. Pure wear, no comfort.

**Decision: the demand gate becomes loop-weighted and thresholded.** A room "calls" exactly as
before (dew-eligible, `error_c <= -0.3 K`, dew throttle >= 0.5, no `sensor_lost`), but demand is
real only when the calling rooms together command enough opening:

    demand = (any room calls) AND (sum over calling rooms of valve_pct x loop_count >= threshold)

- **Metric: percent-loops** (`valve % x number of the room's loops`), owner-confirmed over a plain
  room count — the PI valve already integrates error magnitude x persistence (and the S2 throttle
  shrinks a dew-limited room's contribution), and weighting by loops makes the sum a true
  hydraulic-draw proxy: a 3-loop living room fully open draws 3x a single-loop bath.
- **New global-only knob `hp_flicker_min_open_pct`** — default **250** (2.5 fully open loops),
  minimum **100** (one full loop), UI maximum **dynamic = total configured loops x 100**
  (`tuning.flicker_open_max_pct`, threaded into `get_tuning`/`set_tuning` and both options-flow
  steps; the core validates a loose static [100, 10000] since it cannot know the loop count; the
  panel needs no range logic — it renders min/max straight from the websocket payload). Step 25.
- **Loop count travels inside the core report:** `len(RoomReport.loop_flow_status)` — the S6
  wrapper stamps exactly one entry per configured loop every cycle; an unstamped report degrades
  to weight 1. `cooling_demand` therefore moved from `Iterable[RoomReport]` to
  `Iterable[RoomOutputs]` (it needs the FINAL commanded valve, not `raw_valve_pct`) and returns a
  `CoolingDemand` dataclass (`open_pct`, `threshold_pct`, `demand`) instead of a bare bool.
- **Diagnostics:** the heat-pump runtime flicker payload gains `demand_open_pct` /
  `demand_threshold_pct`, rendered as the "Demand (sum loop opening)" row on the Heat-pump tab —
  computed even while the flicker is DISABLED, so the threshold can be tuned against the live sum
  before switching the feature on.

**Deliberately NOT added (simplicity mandate):** no persistence knob — `hp_flicker_stuck_minutes`
already requires the armed condition (demand included) to hold uninterrupted, so the sum must sit
above the threshold for the whole window, and flapping around it resets the timer in the SAFE
direction (fewer starts). No hysteresis on the sum, no per-room weights beyond the loop count, no
new flags. The 0.3 K calling margin stays a constant (`FLICKER_DEMAND_ERROR_K`). The return
trigger, cooldown and starts-per-hour cap are untouched — the fix is purely "stop forcing at
trivial demand"; the pump's own `setpoint + 3 K` return rule still handles a warming buffer.

No config migration (the new field has a default). Effect on the observed night: the single
caller sums to <= 100 < 250 — zero forced starts; the widespread-demand evening (e.g. 2.5+
loop-equivalents calling) still forces exactly as designed.

**Review finding folded in before release (the shrinking-ceiling trap):** the dynamic ceiling can
DROP below an already-persisted value when rooms/loops are removed. Left alone, that broke both
write-back paths: the panel's global save resends EVERY global knob, so `set_tuning` would reject
the whole batch over a knob the user never touched, and the options `settings` form clamped the
prefill, silently overwriting the stored value on any unrelated save. Rule adopted: **the effective
ceiling is floored by the stored/prefill value** (`flicker_open_max_pct(..., current_value=...)` +
the schema helper widens `max` to the prefill) — an over-ceiling value always round-trips
unchanged; the user may keep or LOWER it, never raise it further.

## 24. Dry assist — humidity-triggered split DRY, presented off the COOLING state (2026-07-16, v0.15.0)

**Problem (owner observation, a muggy non-heatwave day):** rooms at setpoint, floor cooling
nicely, but humidity — and the dew point — climbing hard; air feels sticky/stale. The system was
structurally blind to it: every fast-source trigger is temperature-based, and the floor cools only
SENSIBLY (S2 exists precisely so it never condenses a gram of moisture). Worse, the coupling runs
backwards: a rising dew point lifts the global safe dew point, which lifts the cooling-water
floor, which strips the floor of capacity exactly when the air is muggy. The only dehumidifier in
the system is the split — its DRY mode removes latent heat at minimal sensible cooling. Owner
calibration: air feels stale from a REAL dew point of ~17-18 degC (safe dew 19-20 observed), so
the default threshold is 17.0 degC. Ventilation is out of scope (the recuperator manages itself).

**Decision: a latent branch in the fast-source coordination — DRY as a PRESENTATION of the
COOLING state, not a fourth machine state.** The three-state `FastSourceMachine`
(OFF/HEATING/COOLING, dwells, direction changes through OFF) is untouched; the controller's
`_coordinate_fast_source` computes a `dry_want` beside the temperature `want` and, when the
machine runs ON+COOLING with no temperature demand, emits the command as `FastSourceMode.DRY`
(additive member on the frozen contract; target `None` — splits self-regulate in dry). Because
the machine state stays COOLING: dwell timers, S3/S4 forces, `locked_on` and the multisplit
arbitration all work unchanged, and a temperature boost pre-empts DRY -> COOLING in the same
cycle with no OFF cycle (same refrigerant side).

- **Trigger/release:** `dry_enabled` (bool, default OFF) + `dry_dew_max_c` (default 17.0, range
  [12, 22]) — both per-room overridable. Engage when the room dew point exceeds the knob;
  release `DRY_HYSTERESIS_K` (1 K, a CONSTANT — RH sensors are slow/noisy) below it, or as soon
  as the room overcools past the deadband. Cooling mode + SPLIT kind + quiet-hours-allowed +
  usable dew point required; the persistence comes free from the dwell discipline.
- **Separate hysteresis states:** a dry run must not lower the temperature engage threshold to
  the deadband (anti-inversion) — temperature `want` is keyed `engaged` only when the previous
  emitted command was a TEMPERATURE run (`FastSourceMachine.last_command_mode`). A dew release
  inside min-ON keeps the DRY presentation for the blocked tail (gentler than cool-at-target).
- **Direction normalisation (`fast_source.direction_of`):** DRY counts as the cooling side
  everywhere directions are compared — S4 sync (feedback "dry" now maps to COOLING; a unit
  drying while commanded to HEAT is a real mismatch) and the group arbiter (DRY + COOLING
  coexist on one aggregate; DRY vs HEATING conflicts and the dry room, band excess 0, is the
  weakest claimant and loses).
- **Adapter:** writer maps DRY -> hvac "dry"; before writing it introspects the entity's
  `hvac_modes` (the `hp_setpoint_step` pattern) — a list without "dry" demotes the command to
  OFF and the coordinator merges the `dry_unsupported` flag (valve_mismatch pattern). A missing
  state/attribute assumes support (S4 catches liars). New flag `dry_assist` (info, assist).
- **Twin:** no moisture state exists, so DRY is modelled ONLY as `_DRY_SENSIBLE_FRACTION = 0.3`
  of the split's rated power, sensible; the latent effect is outside the model (documented).
- **System bonus:** drying lowers the room dew point -> lowers the global safe dew point -> the
  cooling water may drop -> the floor RECOVERS capacity. Self-limiting loop; the colder water's
  compressor cycling is already guarded by the §23 demand gate.

**Deliberately NOT built:** no humidity PID or RH% control variable (RH is temperature-dependent;
the dew point measures stickiness directly and is already computed per cycle), no fourth machine
state, no TRANSITIONAL-mode drying (v1 is COOLING-only), no new dwell knobs, no ventilation
control. No config migration (both knobs have defaults).

**Review finding folded in before release (the mode-flip leak):** the DRY presentation rode the
min-ON blocked tail across a global COOLING -> HEATING flip — the machine re-emits its remembered
COOLING while dwell-blocked, and the presentation branch (keyed on `last_dry`) kept rewriting it
to "dry" every cycle until the dwell elapsed, in a home already heating. Fix: the presentation
branch is gated on the CURRENT mode being COOLING; outside it the blocked tail falls back to the
pre-§24 behaviour (remembered COOLING with its target). Regression-pinned by
`test_mode_flip_mid_dry_drops_the_dry_presentation`.

**Post-release fix (v0.17.1, owner-observed short-cycling):** the shipped temperature gate was
the single condition `error_c < deadband_c` for BOTH engage and keep — a zero-width hysteresis.
A split blows cold air in dry too, so in a satisfied bedroom (valve ~4 %, dew above threshold)
the loop flapped: dry engages a hair inside the deadband -> the run overcools past the deadband
-> release -> min-OFF -> the room warms a hair -> dry again, with a period of just the dwell
times ("dry..pauza..dry..pauza"). The boost never does this because its gate is
engage-at-`boost_offset` / release-in-band. Fix (no new knob): the dry gate now mirrors that
shape using the existing band — ENGAGE only while the room is at or above its setpoint
(`error_c <= 0`), KEEP until the overcool release (`error_c < deadband_c`) — a full
deadband-wide hysteresis. After a release the room must warm back TO the setpoint before the
next run, so cycles are few and long (more latent work per compressor start). Regression-pinned
by `test_slightly_overcooled_room_does_not_engage_dry`,
`test_running_dry_keeps_through_sub_deadband_overcool` and
`test_overcool_release_needs_return_to_setpoint_to_rearm`.

## 25. Readable history chart + split-mode band + unified mode colours (2026-07-17, v0.16.0)

**Problem (owner):** the panel's 6h/24h history windows fetch raw recorder history (every state
change, `significant_changes_only: false`) and draw it point-to-point, so a pushy 0.1 K
temperature sensor renders as a saw and the derived "setpoint" series (temp + error) inherits the
noise; the 7d window (hourly statistics means) was already smooth. Separately, the owner wanted to
SEE on the chart when the split ran, and the three active split modes shared one identical orange
badge everywhere in the panel.

**Decision: fix it entirely in the presentation layer.** The recorder, the 5-min cycle, and the
sensors are untouched; no new tuning knobs (simplicity mandate — the two bucket sizes are JS
constants in `WINDOWS`).

- **Bucket means, not spline/epsilon:** the 6h/24h temperature-family series are down-sampled to
  per-bucket means on the absolute epoch grid (2 min / 8 min, ~180 points per chart). A mean is
  chosen over spline smoothing (which invents values that were never measured and still keeps
  every point) and over epsilon/Douglas-Peucker simplification (which preserves the saw's extremes
  — exactly the noise being removed); it is also what the 7d stats path already does, so the three
  windows now share one visual language. The series is split at explicit `unavailable` samples
  FIRST and each run is bucketed independently (one null separator between runs), so real outages
  keep their gap while sample spacing never creates one. Valve samples are NOT averaged.
- **Valve draws step-after:** the recorder stores only changes, so a linear ramp between two
  sparse valve commands is fiction; each command now holds flat until the next sample and the last
  one extends to "now" (not across explicit gaps). The tooltip uses a matching `stepAt` lookup
  (temperature family stays interpolated via `interpAt`).
- **Split-mode band from `fast_source_mode` history:** the per-room textual sensor (states
  `off|heating|cooling|dry`) joins `_DIAGNOSTIC_SENSOR_KEYS`, so the panel resolves its entity id
  through the EXISTING `diagnostic_entities` path — no new websocket command, no report change,
  and rooms without the entity simply draw no band. Textual series always fetch in history mode
  (long-term statistics do not exist for text sensors) under a cache key that carries a `|text`
  marker. The band is thin rects under the time axis, one per run of consecutive non-off states,
  always drawn when data exists — no legend entry, no toggle; hovering adds an "Assist" tooltip
  row. The X axis moved down 10 px (`CHART_MARGIN.b` 26 -> 36, `CHART_H` 240 -> 250) so the plot
  area is unchanged.
- **One colour per direction, everywhere:** new theme-derived tokens `--t-heat` (error red),
  `--t-cool` (info blue), `--t-dry` (accent orange). The badge builders append
  `mode-heating|mode-cooling|mode-dry` to the active class, which covers all three badge surfaces
  (Rooms table "Tryb" column, room-detail tile, Assist tab) plus the Assist tab's real
  `hvac_action` text; the chart band uses the same tokens. A bare `on` stays accent as a fallback.
- **Flags stay severity-coloured (deliberate):** flag chips (e.g. `dry_assist` = info/blue) encode
  SEVERITY, not direction — recolouring them by mode would break the annunciator's triage
  semantics, so they are intentionally unchanged.

**Post-release fix (v0.16.1):** the shipped `bucketMean` inserted an explicit gap whenever
bucketed points were spaced more than 2 buckets apart, "mirroring the stats-path outage
handling". That rule is correct for statistics (a bucket exists for every recorded hour, so a
missing row IS an outage) but wrong for raw history: the recorder stores only state CHANGES, so
a temperature that sits still for 20 minutes produces no samples at all — absence means
"unchanged", not "unknown". On the owner's live data this fabricated broken (dashed-looking)
temperature and setpoint lines. Fixed by deriving gaps ONLY from explicit `unavailable` samples:
the series is split via `segments()` before bucketing and each run is bucketed independently.
The stepped valve rendering was reviewed with the owner and stays (honest for a hold-position
command; "nie boli").

## 26. Durable "forced starts (last 24 h)" from recorder history (2026-07-17, v0.17.0)

**Problem (owner):** the Heat-pump tab's "Forced starts (last h)" counter is in-memory
(`SetpointFlicker._pulse_times_s`), so it resets on every HA restart AND on every config-entry
reload — which a panel tuning save triggers. The owner wants a trustworthy 24-hour count to
judge whether the §23 demand gate actually stops needless night starts; a counter that zeroes
exactly when he is saving knobs would quietly lie.

**Decision: derive the 24 h count from recorder history, not from process memory.**

- New GLOBAL diagnostic text sensor `hp_flicker_state` (states `idle | pulse | cooldown`,
  straight from the flicker payload; `idle` when the payload is absent — the machine cannot
  pulse then, so the recorded history stays truthful). A pulse lasts exactly one cycle, so each
  forced start is one recorded `pulse` state change — the recorder becomes the durable store for
  free.
- The websocket `get_config` exposes `hp_flicker_state_entity_id` next to
  `global_safe_dew_point_entity_id` (the dew-point resolver was generalised to
  `_resolve_global_sensor_entity(key)`), and the panel counts rising edges into `pulse` over
  `WINDOWS["24h"]` using the v0.16.0 text-history fetch (`_series(id, "24h", true)`, 60 s
  cache) — no new websocket command, no polling cost beyond one recorder query a minute while
  the Heat-pump tab is open.
- The volatile last-hour row STAYS next to the new one: it is the flicker's own live view (and
  feeds the `max_starts_per_h` guard); the 24 h row is the durable statistic. Extending the
  in-memory deque to 24 h was rejected precisely because of the reload-reset problem.
