# Bug report — HACS install succeeds but the integration fails to load (`Invalid handler`)

> **Audience:** an AI coding agent (or developer) tasked with fixing the packaging
> so that a **HACS install of this repo yields a working, loadable integration**.
> **Status:** confirmed on a live Home Assistant instance, 2026-07-09, against `v0.1.0`.
> **Severity:** blocker — the integration cannot be set up at all after a HACS install.

---

## TL;DR

The HA adapter in `custom_components/tortoise_ufh/` imports the **top-level core
package `tortoise_ufh/`** (the pure-Python control core: `config`, `controller`,
`models`, …). HACS, for `category: integration`, installs **only**
`custom_components/tortoise_ufh/` — it does **not** ship the sibling `tortoise_ufh/`
core, and the core is **not declared** in `manifest.json` `requirements`
(which lists only `numpy`, `scipy`).

Result on a HACS-installed instance: `import tortoise_ufh` raises
`ModuleNotFoundError`, so importing `config_flow.py` fails, so the `ConfigFlow`
subclass never registers, so Home Assistant rejects the config flow with
**`Invalid handler specified`** (HTTP 404). No entry, no entities, no panel.

**Fix = make the core importable inside a HACS install** — vendor it into the
integration, or declare it as an installable requirement (see §5).

---

## 1. Symptom

- HACS custom-repository add + download of `v0.1.0` **succeeds**; files land at
  `/config/custom_components/tortoise_ufh/` (adapter only).
- Attempting to add the integration fails immediately:
  ```
  POST /api/config/config_entries/flow  {"handler": "tortoise_ufh"}
  → HTTP 404  {"message": "Invalid handler specified"}
  ```
- The integration never sets up: no config entry, no entities, no sidebar panel.
- `numpy`/`scipy` are **not** installed either (setup never starts, so HA never
  runs the requirements install).

## 2. Reproduction (verified live, HA core, 2026-07-09)

1. HACS → add custom repository `hubertciebiada/tortoise-ufh`, category **Integration**.
2. Download `v0.1.0` → files appear in `/config/custom_components/tortoise_ufh/`.
3. Restart Home Assistant.
4. Start the config flow (UI "Add Integration → Tortoise-UFH", or the REST call above).
5. **Observed:** `Invalid handler specified`.

Diagnostic cross-checks performed:

- `manifest/get` (WS) for `tortoise_ufh` **returns the manifest** → HA *can* read
  the integration folder; the problem is not discovery, it is **import**.
- `/config/deps` is empty → requirements were never installed (setup never ran).
- On-disk listing of `/config/custom_components/tortoise_ufh/` contains the adapter
  modules **only** — there is no `tortoise_ufh` core package anywhere on the HA
  Python path.

## 3. Root cause

The repository ships **two** Python packages:

```
tortoise-ufh/
├── custom_components/tortoise_ufh/   ← HA adapter  (HACS installs ONLY this subtree)
│   ├── __init__.py, config_flow.py, coordinator.py, services.py, websocket.py, …
│   └── manifest.json                 ← requirements: ["numpy>=1.26", "scipy>=1.12"]
└── tortoise_ufh/                     ← pure control CORE  (NOT shipped by HACS)
    ├── config.py, controller.py, models.py, pid.py, safety.py, ufh_loop.py, …
```

The adapter imports the core with **absolute** imports. HACS `category:
integration` copies only `custom_components/<domain>/` into `config/custom_components/`,
so the bare top-level package `tortoise_ufh` is absent at runtime, and it is not
listed in `manifest.json` `requirements`, so HA does not `pip install` it either.

Import chain that breaks handler registration:

```
config flow init
  → import custom_components.tortoise_ufh.config_flow
      → from tortoise_ufh.config import ControllerConfig   # ModuleNotFoundError: tortoise_ufh
  → ConfigFlow subclass is never defined/registered
  → HANDLERS has no "tortoise_ufh"  → data_entry_flow.UnknownHandler
  → REST returns 404 "Invalid handler specified"
```

### 3.1 Exact adapter → core import sites (what must be satisfied)

| File | Line | Import |
|---|---|---|
| `custom_components/tortoise_ufh/coordinator.py` | 42 | `from tortoise_ufh.config import ControllerConfig` |
| `custom_components/tortoise_ufh/coordinator.py` | 43 | `from tortoise_ufh.controller import BuildingController` |
| `custom_components/tortoise_ufh/coordinator.py` | 44 | `from tortoise_ufh.models import (...)` |
| `custom_components/tortoise_ufh/config_flow.py` | 53 | `from tortoise_ufh.config import ControllerConfig` |
| `custom_components/tortoise_ufh/services.py` | 26 | `from tortoise_ufh.models import Mode` |
| `custom_components/tortoise_ufh/websocket.py` | 36 | `from tortoise_ufh.models import Mode` |

(6 import sites across 4 adapter files. `config_flow.py` is the one that directly
breaks *handler registration*; the others break *setup* the same way.)

### 3.2 Why it passes CI/tests/dev but fails under HACS

`pyproject.toml` declares `[tool.setuptools.packages.find] include = ["tortoise_ufh*"]`,
so `pip install -e .` (dev, CI, tests) puts the **core** on `sys.path` as
`tortoise_ufh`. Under HACS there is **no `pip install` of the repo** — HACS only
copies the `custom_components` subtree — so the core is missing. The green
`hassfest` / HACS-Action CI validate the *adapter's* manifest/structure, not that
its runtime imports resolve after a HACS-style copy, so they do not catch this.

## 4. What is NOT the problem (ruled out)

- Not a discovery problem — `manifest/get` sees the integration; `hacs.json`
  (`content_in_root` defaults to `false`) correctly points at `custom_components/`.
- Not a top-level `numpy`/`scipy` import issue — no module imports them at module
  top level; they are used lazily and would install fine at setup **if setup ran**.
- Not a manifest schema issue — `version`, `documentation`, `issue_tracker`,
  `codeowners`, `config_flow: true` are all present; CI `hassfest`/HACS are green.

## 5. Fix options

Pick one. All must satisfy: **after a clean HACS install + HA restart, the config
flow completes and the entry loads.**

### Option A — Vendor the core into the integration (self-contained, offline)
- Move `tortoise_ufh/` → `custom_components/tortoise_ufh/core/`.
- Rewrite the 6 adapter import sites in §3.1: `from tortoise_ufh.X` → `from .core.X`.
- Rewrite the **core's internal absolute self-imports** to relative
  (`from tortoise_ufh.Y import …` → `from .Y import …`) in every core module that
  imports a sibling — currently at least:
  `__init__.py, building_profiles.py, config.py, controller.py, metrics.py,
  safety.py, scenarios.py, simulation_log.py, simulator.py, ufh_loop.py`
  (grep `^(from|import) tortoise_ufh` under `tortoise_ufh/` for the full set).
- Keep `numpy`/`scipy` in `manifest.json` `requirements`.
- Update dev/test import paths as needed (`pyproject.toml`, `tests/`) so the core
  is importable under its new location for CI.
- **Pros:** HACS ships everything; works offline; no external publish.
  **Cons:** physically couples core under `custom_components/` (the core still
  imports no Home Assistant — the logical separation is preserved).

### Option B — Publish the core to PyPI and declare it as a requirement (recommended if the pure-core / HA-adapter split is to be preserved)
- Publish `tortoise-ufh` (the core, already built by `pyproject.toml`) to PyPI.
- Add it to `custom_components/tortoise_ufh/manifest.json` `requirements`, e.g.
  `"requirements": ["tortoise-ufh==<version>"]` (`numpy`/`scipy` arrive
  transitively via the core's own dependencies; the explicit entries may be dropped).
- **Pros:** zero adapter/core code changes; keeps the intentional separation
  (core never imports HA). **Cons:** requires a PyPI release step in the pipeline;
  the manifest requirement version must track the core release.

### Option C — Git requirement (quick, but CI friction)
- `manifest.json`: `"requirements": ["tortoise-ufh @ git+https://github.com/hubertciebiada/tortoise-ufh@<tag>"]`.
- **Pros:** no PyPI. **Cons:** `hassfest`/HACS-Action will flag a non-PyPI
  requirement; needs `git` available in the HA runtime; pins to a tag.

## 6. Recommendation

- Preserve the deliberate **pure-core (no HA import) ↔ adapter** separation the
  codebase documents → **Option B**.
- Ship a working HACS release with the least infrastructure → **Option A**.

## 7. Acceptance criteria

1. Fresh HACS install of the fixed release (custom repo → download → HA restart),
   then **Add Integration → Tortoise-UFH**, **completes the config flow and creates
   a config entry** — no `Invalid handler specified`.
2. In the HA runtime, the adapter's imports of the core resolve (whichever form the
   fix takes); `numpy`/`scipy` install at setup.
3. The sidebar panel registers and platform entities are created; HA logs contain
   **no** `ModuleNotFoundError: tortoise_ufh` and no failed-setup traceback.
4. Existing CI stays green (`hassfest`, HACS-Action, unit/HA/simulation tests),
   with import paths updated for whichever option is chosen.

## 8. Environment / references

- Home Assistant: live core instance (HACS present; custom repo id `1293921442`).
- Integration under test: `tortoise_ufh` `v0.1.0`.
- `manifest.json` `requirements`: `["numpy>=1.26", "scipy>=1.12"]` (no core).
- `pyproject.toml`: `build-backend = setuptools`, `packages.find include = ["tortoise_ufh*"]`.
- Adapter files importing the core: `coordinator.py`, `config_flow.py`,
  `services.py`, `websocket.py` (see §3.1).
- Core package: `tortoise_ufh/` (20 modules).
