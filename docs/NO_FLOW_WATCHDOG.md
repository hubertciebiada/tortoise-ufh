# Change request: hydraulic no-flow watchdog — valve feedback can lie, the loop probes don't

**Status:** proposal (agent-facing change request)
**Date:** 2026-07-12
**Severity:** high — a silent actuation failure defeats BOTH condensation guards and the whole control loop, with zero flags raised.

## Incident that motivates this (production, 2026-07-12)

Both VdMot valve controllers (east 7 loops, west 6 loops) froze after a simultaneous
power-event reboot on 2026-07-09: the motor MCU (STM32) accepted targets but never
executed them. For ~2.5 h of LIVE cooling:

- Tortoise wrote valve commands; the HA `valve` entities **echoed the target back as
  `current_position`** (the bridge published the requested value, not the measured one).
- `valve_mismatch` never fired — command↔feedback agreed perfectly while every rotameter
  stood still and `moves/open_count/close_count` on the controllers stayed at 0.
- Room temperatures did not respond → the per-room integrators kept winding up
  (commands grew 13 % → 29/44 % over the frozen window).
- The failure was only caught by a human looking at rotameters.

Recovery was controller-side (`valvesDetect` re-detection), but two Tortoise-side gaps
surfaced:

1. **No physics-based check.** Every existing validation (age gates, plausibility
   gates, `valve_mismatch`) lives on the *data* path. When the data path lies
   consistently (echo feedback), nothing cross-checks against the *thermal* path.
2. **The valve write cache never re-asserts.** After the controller reset, its targets
   reverted to the park position (30 %). Rooms whose Tortoise command had not changed
   since (e.g. a `cooling_disabled` bathroom commanded 0 %) were never re-written —
   the write cache said "already written", so the stale 30 % stood indefinitely.
   (Splits already re-assert ≈ 45 min; valves seem to have no equivalent.)

## Why this is a safety issue, not just a comfort issue

A valve frozen **open** during cooling sits outside both condensation defences: S2 and
the dew-point ramp modulate the *commanded* position, but the physical loop keeps
flowing cold water. With echo feedback, Tortoise believes the valve obeyed. The slab
can condense while every panel indicator is green.

## Proposal

### A. Hydraulic no-flow watchdog (new rule, e.g. S6) — the core ask

Use the already-wired per-loop `entity_supply` / `entity_return` probes as an
independent witness of actuation:

- **No-flow detection:** if a room is LIVE, mode is heating/cooling, and the valve has
  been commanded ≥ `open_threshold` (e.g. 15 %) for ≥ `response_window` (e.g. 45 min —
  slab dynamics are slow), but the loop shows no hydraulic signature —
  `|T_supply − T_return| < ε` (e.g. 0.3 K) **and** `T_supply` shows no approach toward
  the source side (no downward trend in cooling / upward in heating) — raise
  `loop_no_flow` for that room.
- **Reverse check (frozen open / leaking valve) — PROPOSED, then REMOVED 2026-07-13
  (see docs/DECISIONS.md §17).** A closed-commanded valve that still leaks was to be caught
  by the room sitting persistently below its cooling setpoint. In practice that room-air
  witness could not hard-verify actuation (too many benign states reproduce the same
  signature), so it produced only false alarms — see "Why there is no stuck-open detection"
  below. Hard verification of a closed valve is deferred to a future, dedicated mechanism.
- **False-positive gating** (pump state is out of scope for Tortoise, §6): only
  evaluate when circulation is plausible — at least one *other* loop in the system
  currently shows a healthy ΔT signature, or (optional new global input) a
  `entity_global_supply` manifold probe reads source-side. Multi-loop rooms: evaluate
  per loop, report the worst.
- **Reaction:** flag + per-room binary sensor (so HA automations can notify) + freeze
  that room's integrator (prevent the wind-up observed in the incident). No automatic
  valve banging.

### Why there is no stuck-open detection

The reverse check above (a closed valve that still leaks, witnessed by the room over-cooling)
was implemented and then removed on 2026-07-13 (docs/DECISIONS.md §17). Room air temperature
versus setpoint cannot hard-verify whether an actuator physically passes water: too many
benign states (a cold room after a setpoint raise, a neighbouring cold mass, a slow-draining
slab, a sensor offset) reproduce the same signature, and — once the manifold-conducted probes
of a closed loop are correctly declared unreliable — there is no independent physical
corroborator. The detection produced only false positives. `loop_no_flow` keeps its water-probe
witness and stays; a proper close-and-measure actuation verification is deferred to a future,
dedicated mechanism.

### B. Valve command re-assert (parity with splits)

Re-write each room's valve command unconditionally every ~45–60 min (or whenever
feedback diverges from the *cached command* — not only from the previous feedback).
This heals external controller resets/reboots that silently revert targets to a park
position. Cheap, no config needed.

### C. Optional: actuation self-test service

`tortoise_ufh.test_actuation` (per room, manual): command a deliberate excursion
(e.g. 100 % for 20–30 min, then back), verify the loop's ΔT/supply-trend response, and
report pass/fail in the panel + a flag. Meant to be run after maintenance or a power
event — not scheduled.

### D. Panel

The Valves tab already shows per-loop ΔT — add a flow-health chip
(`ok / no-flow?`) derived from A, and surface the new flags in the room
detail and the flags dictionary in `docs/manual/pl.md`.

## Notes / constraints

- Probe placement caveat (`loop_no_flow` only): supply probes sit on the manifold bar
  before the valves in some installations — the *return* probe is the more reliable flow
  witness, so the no-flow detector weights `T_return` movement accordingly. (This same
  bar-conduction of a closed loop's probes is why the removed stuck-open reverse check
  could not use them — see "Why there is no stuck-open detection" above.)
- Thresholds (`ε`, windows, open_threshold) should be tuning knobs with sane defaults;
  slab response is slow, so windows must be ≥ 30 min to avoid flapping.
- Rooms without supply/return probes: watchdog silently inactive (availability-based),
  panel shows "—" for flow health.
- This must NOT trust `valve` entity feedback at all — the entire point is that the
  feedback channel proved capable of lying end-to-end.

## Acceptance criteria

1. Simulated echo-feedback + frozen actuator (constant loop temps) in LIVE cooling
   raises `loop_no_flow` within one `response_window`, freezes the integrator, and
   exposes a binary sensor — with zero writes beyond the normal command.
2. *(Removed 2026-07-13, docs/DECISIONS.md §17.)* The stuck-open reverse detection —
   a cooling-disabled room whose leaking closed valve held it below setpoint — was
   implemented and then withdrawn: its room-air witness could not hard-verify actuation
   and produced only false alarms. See "Why there is no stuck-open detection".
3. A controller-side target reset (feedback jumps to park while the cached command is
   unchanged) is healed by the re-assert within its period.
4. No flags on healthy loops across the existing simulation gate scenarios
   (hot_july, night_setback, cold_snap).
