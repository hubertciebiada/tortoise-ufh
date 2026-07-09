# Docker harness — debug Tortoise-UFH locally before touching your HA

Two independent setups. Neither touches your real Home Assistant instance.

## 1. Automated integration tests (CI-grade, headless)

Runs the `tests/ha/` tier against a **real in-process Home Assistant** test harness
(`pytest-homeassistant-custom-component`). This is what actually exercises the adapter:
config flow, entry setup/unload, the coordinator's read→core→write cycle, entities, the
websocket API, and the services.

```bash
docker compose -f docker/docker-compose.test.yml build      # once (installs HA + deps)
docker compose -f docker/docker-compose.test.yml run --rm ha-tests
```

The repo is bind-mounted, so after editing `custom_components/` or `tests/ha/` just re-run
`run --rm ha-tests` — no rebuild needed. Pass extra pytest args after the service name, e.g.
`run --rm ha-tests python -m pytest tests/ha/test_config_flow.py -q`.

> The pure-core tiers (`tests/unit`, `tests/simulation`, 266 tests) run without Docker on any
> machine with numpy/scipy: `python -m pytest tests/unit tests/simulation`. The `tests/ha`
> tier is auto-skipped there (Home Assistant isn't installed) and only runs in this container.

## 2. Interactive Home Assistant (click around the panel)

Boots a throwaway HA at <http://localhost:8123> with the integration mounted in, so you can
add it through the UI, open the **Tortoise-UFH** sidebar panel, and watch the debug logs.

```bash
docker compose -f docker/docker-compose.dev.yml up          # Ctrl-C to stop
```

First run: create a local account, then **Settings → Devices & Services → Add Integration →
Tortoise-UFH**. Point the global mode entity at `input_select.tortoise_home_mode` (pre-created
in `config/configuration.yaml`). Add fake room temperature / humidity / valve entities via
**Settings → Devices & Services → Helpers** (or template sensors) to drive the controller
without real hardware. Rooms start in the **shadow** control state — nothing is written until you
switch a room's control state to **live** (`select.<room>_control_state`), so it is
safe to explore.

Config and state persist in `docker/config/` (git-ignored). Delete it to start clean.
