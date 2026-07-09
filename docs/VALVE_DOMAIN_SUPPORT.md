# Change request — accept `valve`-domain actuators for `entity_valves` (not only `number`)

> **Audience:** an AI coding agent (or developer) implementing the change in this repo.
> **Status:** confirmed against a live Home Assistant install of `v0.1.1`, 2026-07-09,
> while wiring the integration to real underfloor-heating actuators.
> **Severity:** blocker for real deployments whose valves are `valve`-domain entities
> (the common case). Without this, users must hand-build a `number` proxy per loop.

---

## TL;DR

The config-flow **`entity_valves`** field only accepts the **`number`** domain, and the
coordinator **reads the valve position from the entity _state_** and **writes via the
`number.set_value` service**. Real HA underfloor actuators (e.g. Home Assistant
`valve` entities backed by VdMot manifold controllers) expose the loop as a **`valve`**
entity: position lives in the **`current_position` attribute** (0–100), the write is
**`valve.set_valve_position`** (`position` 0–100), and the *state* is
`open`/`closed`/`opening`/`closing` — **not a number**.

So a `valve` entity **cannot** be selected in the wizard, and even if forced in, the
coordinator would read `float("closed")` → `None` and call `number.set_value` on a
non-`number` entity. The only current workaround is a per-loop template `number` proxy
(state = position, `set_value` → `valve.set_valve_position`) — friction that this
deployment explicitly wants to remove (it is exactly the proxy the previous
`versatile_thermostat` setup needed and that was since deleted).

**Ask:** make `entity_valves` accept **both `number` and `valve`** domains, reading /
writing each entity according to its domain.

---

## 1. Motivation / impact

- The PRD (§4.2) describes the valve column as “wskazanie jednego lub wielu
  **aktuatorów**” — the natural HA actuator for a valve is a **`valve`** entity, not a
  `number`. Requiring `number` forces every user with real valves to author a template
  proxy per loop.
- Live deployment: 13 heating loops are `valve.podlogowka_wschod_*` /
  `valve.podlogowka_zachod_*` (position-capable). Requiring `number` means 13 hand-made
  proxies before a single room can run.
- Requiring `number` couples the product to a HA-modelling workaround rather than the
  first-class actuator domain.

## 2. Current behaviour (with exact references)

### 2.1 Config-flow restricts the picker to `number`
`custom_components/tortoise_ufh/config_flow.py`, entities step, `entity_valves`:
```python
# selector: EntitySelector(EntitySelectorConfig(domain=["number"], multiple=True))
schema_dict[vol.Required(CONF_ENTITY_VALVES)] = EntitySelector(...)   # domain=["number"]
```
Live REST proof — the `entities` step `data_schema` for `entity_valves`:
```json
{ "name": "entity_valves", "required": true,
  "selector": { "entity": { "domain": ["number"], "multiple": true } } }
```

### 2.2 Coordinator reads position from the entity **state**
`custom_components/tortoise_ufh/coordinator.py`:
- Build loop inputs (~line 726):
  ```python
  valve_position_pct=self._read_float_state(valves[i] if i < len(valves) else None)
  ```
- `_read_float_state` (~line 932) parses `state.state` as `float`. For a `valve`
  entity `state.state` is `"closed"`/`"open"` → `float(...)` fails → `None`.

### 2.3 Coordinator writes position via the `number.set_value` service
`custom_components/tortoise_ufh/coordinator.py`, `_write_valves` (~line 851):
```python
value = outputs.valve_position_pct                      # ~869
...
await self.hass.services.async_call(
    "number", "set_value",                              # ~876-877
    {"entity_id": v, "value": value}, blocking=False)
```
Module self-doc confirms the contract (module docstring, ~line 17):
`(number.set_value per valve entity, climate.set_hvac_mode + …)`.

### 2.4 Entity validation
`custom_components/tortoise_ufh/entity_validator.py` validates the mapped entity
(domain / unit). It currently expects `number`; it must also accept `valve`.

## 3. Reference: the real `valve` entity to support

`GET /api/states/valve.podlogowka_wschod_sypialnia` (live):
```json
{
  "state": "closed",
  "attributes": {
    "current_position": 0,          // 0..100, THIS is the position to read
    "is_closed": true,
    "device_class": "water",
    "supported_features": 7          // OPEN(1)|CLOSE(2)|SET_POSITION(4) = 7
  }
}
```
- **Read** position: `state.attributes["current_position"]` (int/float 0–100).
- **Write** position: service `valve.set_valve_position`, data `{"entity_id": …, "position": <0..100>}`.
- **Capability gate:** only valves whose `supported_features` includes
  `ValveEntityFeature.SET_POSITION` (bit `4`) can be positioned. Reject / warn otherwise.
- **Orientation:** HA `current_position` is `100 = fully open`, `0 = closed`, which
  matches the module’s “100 % = zawór otwarty maksymalnie” (PRD §3.2). No inversion.

## 4. Desired behaviour

`entity_valves` accepts a **mixed list** of `number` and `valve` entities; per entity,
the coordinator dispatches by domain:

| Domain | Read position (0–100) | Write position (0–100) |
|---|---|---|
| `number` | `float(state.state)` (current behaviour) | `number.set_value` `{value}` |
| `valve`  | `float(state.attributes["current_position"])` | `valve.set_valve_position` `{position}` |

Everything else (per-room single computed position fanned out to all loops, the
`valve_write_threshold_pct` de-bounce, `_last_written_valve` bookkeeping, safe-degrade
hold-last-position) stays identical and domain-agnostic.

## 5. Implementation notes

- **Config flow** (`config_flow.py`): `entity_valves` selector
  `domain=["number", "valve"]`. Keep `multiple=True`, keep required.
- **Read abstraction** (`coordinator.py`): replace the valve read at ~726 with a
  helper, e.g. `_read_valve_position(entity_id) -> float | None`:
  ```python
  st = self.hass.states.get(entity_id)
  if st is None: return None
  if entity_id.startswith("valve."):
      pos = st.attributes.get("current_position")
      return float(pos) if pos is not None else None
  return self._read_float_state(entity_id)   # number.* → state as float
  ```
  (Do **not** reuse `_read_float_state` for `valve`: its state is non-numeric.)
- **Write abstraction** (`coordinator.py`, `_write_valves` ~851-877): dispatch by domain:
  ```python
  if v.startswith("valve."):
      await self.hass.services.async_call(
          "valve", "set_valve_position",
          {"entity_id": v, "position": round(value)}, blocking=False)
  else:
      await self.hass.services.async_call(
          "number", "set_value", {"entity_id": v, "value": value}, blocking=False)
  ```
  `valve.set_valve_position` expects an **int** `position` — round the percentage.
- **Validation** (`entity_validator.py`): accept domain `valve`; for `valve`, do **not**
  require a `unit_of_measurement`; optionally assert
  `supported_features & ValveEntityFeature.SET_POSITION` and surface a clear flow error
  (`valve_no_set_position`) if missing.
- **Constants / strings**: if any `CONF_*`/label/description says “number”, generalise to
  “valve actuator (number or valve)”. Update `strings.json` + `translations/{en,pl}.json`.
- **Backward compatibility:** existing `number`-only configs must behave exactly as before.
- **Feedback latency note (non-blocking):** VdMot valves move slowly and sequentially
  (~40 s/valve, one at a time per device); `current_position` lags the command. The
  controller already treats valve position as *feedback* (input), so this is fine, but
  keep using the **reported** `current_position`, never assume the write took effect
  instantly.

## 6. Files to touch

- `custom_components/tortoise_ufh/config_flow.py` — `entity_valves` selector domain.
- `custom_components/tortoise_ufh/coordinator.py` — read (~726) + write (`_write_valves`).
- `custom_components/tortoise_ufh/entity_validator.py` — accept `valve` domain + SET_POSITION.
- `custom_components/tortoise_ufh/const.py` — any valve-domain constant/label if needed.
- `custom_components/tortoise_ufh/strings.json`, `translations/en.json`, `translations/pl.json`.
- `tests/` — add `valve`-domain read/write cases (and a mixed number+valve loop list).

## 7. Acceptance criteria

1. The `entities` step `entity_valves` picker accepts `valve.*` entities (and still
   `number.*`).
2. Pointing a room’s valves at `valve.podlogowka_wschod_sypialnia` results in the
   coordinator **reading `current_position`** and **writing `valve.set_valve_position`**;
   the physical valve moves to the computed percentage.
3. A `number`-only configuration is unchanged (regression-free), including the
   `valve_write_threshold_pct` de-bounce and safe-degrade hold-last-position.
4. A `valve` entity lacking `SET_POSITION` is rejected in the flow with a clear error
   (not a silent runtime failure).
5. Mixed loop lists (some `number`, some `valve`) work; each entity is dispatched by domain.
6. New tests cover read + write for `valve`, `number`, and a mixed list; existing CI
   (`hassfest`, HACS action, unit/HA tests) stays green.

## 8. Environment / provenance

- Home Assistant live instance; integration `tortoise_ufh` `v0.1.1`.
- Actuators: `valve.podlogowka_wschod_*` (7) + `valve.podlogowka_zachod_*` (6),
  `supported_features = 7`, `current_position` 0–100, `device_class: water`.
- Trigger: completing the config-flow wizard, the required `entity_valves` (domain
  `number`) could not be pointed at the real `valve.*` actuators; the mode input
  (`input_select.tortoise_ufh_tryb`, options `heating/transitional/cooling/off`) and all
  other room entities (room temp, humidity, supply/return probes, split) were available
  and mapped fine — valves were the only blocker.
