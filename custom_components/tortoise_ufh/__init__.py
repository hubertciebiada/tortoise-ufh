"""Tortoise-UFH Home Assistant custom integration.

Home Assistant adapter entry point. Wires a :class:`TortoiseUfhCoordinator`
(one per config entry) into HA's config-entry lifecycle and, once per HA
instance, registers the sidebar panel (with its static JS path) and the panel
websocket API. Temperatures are in degrees Celsius; this module holds no
physical values of its own.

Nothing that depends on Home Assistant is imported at module top level. The pure
control core is vendored under this package at
``custom_components.tortoise_ufh.core``; importing any of its submodules runs
*this* package ``__init__`` first, so the top level must stay importable without
``homeassistant`` (the core is unit- and simulation-tested in an HA-free
environment). Every Home Assistant symbol, every HA-importing submodule, and even
:mod:`.const` (which imports the HA ``Platform`` enum) is therefore deferred to
:data:`typing.TYPE_CHECKING` or to a local import inside the function that needs
it — the module top level pulls in stdlib only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import TortoiseUfhCoordinator

_LOGGER = logging.getLogger(__name__)

# Key under ``hass.data[DOMAIN]`` guarding one-time global frontend setup
# (the sidebar panel and websocket commands are process-wide, not per-entry).
_FRONTEND_REGISTERED: str = "_frontend_registered"


@dataclass(frozen=True)
class RuntimeData:
    """Per-entry runtime state stored on ``entry.runtime_data``.

    Attributes:
        coordinator: The live data-update coordinator driving this entry.
        hub_device_id: Device-registry id of the per-entry hub device that
            every room device links to (issue #23).
    """

    coordinator: TortoiseUfhCoordinator
    hub_device_id: str


type TortoiseUfhConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TortoiseUfhConfigEntry) -> bool:
    """Set up Tortoise-UFH from a config entry.

    Builds the coordinator, performs the first refresh, forwards the entry to
    all platforms, wires the options-reload listener, and — on the first entry
    for this HA instance — registers the sidebar panel and websocket commands.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.

    Returns:
        ``True`` when setup succeeded.
    """
    from .const import DOMAIN, PLATFORMS
    from .coordinator import TortoiseUfhCoordinator
    from .device import register_hub_device
    from .panel import async_register_panel
    from .services import async_register_services
    from .websocket import async_register_ws

    _LOGGER.debug("Setting up Tortoise-UFH entry: %s", entry.entry_id)

    coordinator = TortoiseUfhCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # K11 (2026-07-12): the hub device must exist in the registry BEFORE any
    # platform adds a room entity whose device links to the hub, or HA logs a
    # "will stop working" deprecation per entity. Its registry id is what room
    # devices link to via `via_device_id` (issue #23).
    hub_device_id = register_hub_device(hass, entry.entry_id)

    entry.runtime_data = RuntimeData(
        coordinator=coordinator, hub_device_id=hub_device_id
    )

    _async_purge_retired_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the whole entry when options that need a rebuild change (controller
    # / per-room tuning). A control-state-only change (CONF_ROOM_STATE applied via
    # the coordinator) is skipped by the listener so the PID integrator survives.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Cancel the coordinator's pending debounced recompute on unload so no
    # stray refresh fires against a torn-down entry.
    entry.async_on_unload(coordinator.async_cancel_recompute)

    # Register process-wide frontend once, guarded against double registration
    # across multiple config entries.
    domain_data: dict[str, object] = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(_FRONTEND_REGISTERED, False):
        async_register_ws(hass)
        await async_register_panel(hass)
        async_register_services(hass)
        domain_data[_FRONTEND_REGISTERED] = True
        _LOGGER.debug("Registered Tortoise-UFH panel, websocket commands, and services")

    return True


def _async_purge_retired_entities(
    hass: HomeAssistant, entry: TortoiseUfhConfigEntry
) -> None:
    """Remove registry entries of entities retired by later releases.

    v0.5.0 retired the per-room ``live_control`` binary sensor (it merely
    mirrored ``control_state == "live"``, fully covered by the control-state
    select); v0.22.3 retired the per-room ``flow_fault`` binary sensor with the
    S6 no-flow watchdog (DECISIONS §34). The platform no longer creates them,
    so on upgrade their registry entries would linger as orphans — sweep them
    here on every setup. This is plain registry hygiene keyed on the frozen
    unique-id suffixes; it needs no config-entry version bump.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry whose orphaned entities are purged.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "binary_sensor" and reg_entry.unique_id.endswith(
            ("_live_control", "_flow_fault")
        ):
            registry.async_remove(reg_entry.entity_id)
            _LOGGER.debug("Purged retired binary sensor %s", reg_entry.entity_id)


async def async_migrate_entry(
    hass: HomeAssistant, entry: TortoiseUfhConfigEntry
) -> bool:
    """Migrate an old config entry to the current schema version.

    v1 -> v2 (the RoomControlState refactor): the two legacy participation flags
    — per-room ``participates`` (in ``entry.data``) and the per-room
    ``live_control`` map plus the global ``kill_switch`` (in ``entry.options``) —
    are collapsed into a single canonical per-room state map
    ``entry.options[CONF_ROOM_STATE]``. This block deliberately still writes the
    historical ``"shadow"`` literal (its logic is frozen); the v2 -> v3 block
    below converts it in the same call.

    v2 -> v3 (shadow removal, 2026-07-12; DECISIONS §13): the per-room control
    state becomes a two-state (``off`` / ``live``). Every persisted state that is
    not ``off`` or ``live`` — notably the retired ``shadow``, but also any
    corrupted value — maps to ``off``, which preserves the write behaviour
    exactly (neither shadow nor off ever wrote a command). No registry cleanup
    is needed: the ``select.*_control_state`` entity keeps its domain and
    unique id; only its option list shrinks.

    v3 -> v4 (the mode entity retires, 2026-07-22; DECISIONS §27): the global
    operating mode is now owned by the integration and exposed as the home-mode
    ``select`` entity, so the legacy ``entity_mode`` key in ``entry.data`` (an
    external ``select`` / ``input_select`` the coordinator polled every cycle
    and which overrode the internal mode) is dropped. Nothing replaces it in
    the config: the persisted mode in the setpoint Store (S9) carries across
    the upgrade untouched, and an owner who wants to keep driving the mode from
    a helper writes an automation calling ``select.select_option``. No registry
    cleanup is needed — the key never had an entity of its own.

    v4 -> v5 (S6 no-flow watchdog removed, 2026-10-12, v0.22.3; DECISIONS §34):
    the watchdog's three tuning keys (``flow_epsilon_k``,
    ``flow_open_threshold_pct``, ``flow_response_window_min``) are stripped
    from the global tuning (``data`` and ``options``) and from every per-room
    override; a room override left empty is dropped. Without this an old key
    would make ``ControllerConfig(**kwargs)`` reject the whole tuning.

    v5 -> v6 (tuning persisted as sparse overrides, 2026-10-22, issue #31;
    DECISIONS §35): the global tuning used to be stored as a FULL
    ``ControllerConfig`` snapshot (seeded into ``data`` at setup, rewritten
    whole into ``options`` by every save), so a retuned library default never
    reached an existing install. The stored global tuning (``data`` <-
    ``options``, the old merge rule) is reduced to the user-facing knobs whose
    value differs from the current default AND from every earlier shipped
    default (:data:`~tortoise_ufh.config.SUPERSEDED_CONTROLLER_DEFAULTS`);
    what is left moves to ``options`` (dropped when empty) and ``data`` loses
    the key. Non-user-facing fields (``kd``, ``cycle_seconds``,
    ``valve_write_threshold_pct``) are dropped too. Per-room overrides are
    already sparse and stay as they are. Every dropped key is logged.

    A v1 entry passes every block sequentially in one call (each block ends at
    the version the next one picks up) and lands on v6.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being migrated.

    Returns:
        ``True`` when migration succeeded, ``False`` for an unsupported
        (newer-than-known) version.
    """
    from homeassistant.helpers import entity_registry as er

    from .const import (
        CONF_CONTROLLER,
        CONF_LIVE_CONTROL,
        CONF_PARTICIPATES,
        CONF_ROOM_NAME,
        CONF_ROOM_STATE,
        CONF_ROOM_TUNING,
        CONF_ROOMS,
        DEFAULT_PARTICIPATES,
        DOMAIN,
        RETIRED_CONTROLLER_KEYS,
        ROOM_STATE_LIVE,
        ROOM_STATE_OFF,
        ROOM_STATE_SHADOW,
    )

    # Legacy option/entity key for the retired global kill-switch (no longer a
    # named constant; kept here only to purge it from options and the registry).
    legacy_kill_switch_key = "kill_switch"
    # Legacy data key of the retired external global-mode entity (v0.19.0);
    # likewise no longer a named constant, kept only to purge it from data.
    legacy_mode_entity_key = "entity_mode"

    if entry.version > 6:
        # Downgrade from an unknown future version is not supported.
        return False

    if entry.version == 1:
        live_map: dict[str, Any] = dict(entry.options.get(CONF_LIVE_CONTROL, {}) or {})
        room_states: dict[str, str] = {}
        new_rooms: list[dict[str, Any]] = []
        for room_cfg in entry.data.get(CONF_ROOMS, []):
            room = dict(room_cfg)
            name = str(room.get(CONF_ROOM_NAME, ""))
            participates = bool(room.pop(CONF_PARTICIPATES, DEFAULT_PARTICIPATES))
            if name:
                if not participates:
                    room_states[name] = ROOM_STATE_OFF
                elif bool(live_map.get(name, False)):
                    room_states[name] = ROOM_STATE_LIVE
                else:
                    room_states[name] = ROOM_STATE_SHADOW
            new_rooms.append(room)

        new_data = {**entry.data, CONF_ROOMS: new_rooms}
        new_options = {
            key: value
            for key, value in entry.options.items()
            if key not in (CONF_LIVE_CONTROL, legacy_kill_switch_key)
        }
        new_options[CONF_ROOM_STATE] = room_states

        # Purge the retired switch entities from the registry so their unique ids
        # can never resurrect as orphaned entities.
        registry = er.async_get(hass)
        stale_unique_ids = [f"{entry.entry_id}_{legacy_kill_switch_key}"]
        for room in new_rooms:
            name = str(room.get(CONF_ROOM_NAME, ""))
            if not name:
                continue
            slug = name.lower().replace(" ", "_")
            stale_unique_ids.append(f"{entry.entry_id}_{slug}_{CONF_LIVE_CONTROL}")
        for unique_id in stale_unique_ids:
            entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
            if entity_id is not None:
                registry.async_remove(entity_id)

        hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options, version=2
        )
        _LOGGER.debug(
            "Migrated Tortoise-UFH entry %s to v2 (room states: %s)",
            entry.entry_id,
            room_states,
        )

    if entry.version == 2:
        # Shadow removal (2026-07-12, v0.7.0; DECISIONS §13): reduce the
        # per-room state map to the two-state off/live. "shadow" (and any
        # garbage value) becomes "off" — identical write behaviour, since
        # neither state ever wrote a command. An entry without the map keeps
        # not having it (the coordinator defaults every room to "off").
        new_options_v3 = dict(entry.options)
        if CONF_ROOM_STATE in new_options_v3:
            state_map: dict[str, Any] = dict(
                new_options_v3.get(CONF_ROOM_STATE, {}) or {}
            )
            converted = {
                name: (
                    state
                    if state in (ROOM_STATE_OFF, ROOM_STATE_LIVE)
                    else ROOM_STATE_OFF
                )
                for name, state in state_map.items()
            }
            new_options_v3[CONF_ROOM_STATE] = converted
            _LOGGER.debug(
                "Migrated Tortoise-UFH entry %s to v3 (room states: %s)",
                entry.entry_id,
                converted,
            )
        hass.config_entries.async_update_entry(entry, options=new_options_v3, version=3)

    if entry.version == 3:
        # Mode-entity retirement (2026-07-22, v0.19.0; DECISIONS §27): drop the
        # external mode-input key. An entry that never had one migrates just as
        # cleanly — the version bump alone marks it current.
        new_data_v4 = {
            key: value
            for key, value in entry.data.items()
            if key != legacy_mode_entity_key
        }
        hass.config_entries.async_update_entry(entry, data=new_data_v4, version=4)
        _LOGGER.debug(
            "Migrated Tortoise-UFH entry %s to v4 (retired the mode entity)",
            entry.entry_id,
        )

    if entry.version == 4:
        # S6 no-flow watchdog removed (2026-10-12, v0.22.3; DECISIONS §34).
        def _strip(tuning: Any) -> Any:
            if not isinstance(tuning, dict):
                return tuning
            return {
                key: value
                for key, value in tuning.items()
                if key not in RETIRED_CONTROLLER_KEYS
            }

        new_data_v5 = dict(entry.data)
        if CONF_CONTROLLER in new_data_v5:
            new_data_v5[CONF_CONTROLLER] = _strip(new_data_v5[CONF_CONTROLLER])
        new_options_v5 = dict(entry.options)
        if CONF_CONTROLLER in new_options_v5:
            new_options_v5[CONF_CONTROLLER] = _strip(new_options_v5[CONF_CONTROLLER])
        room_tuning = new_options_v5.get(CONF_ROOM_TUNING)
        if isinstance(room_tuning, dict):
            stripped = {name: _strip(t) for name, t in room_tuning.items()}
            new_options_v5[CONF_ROOM_TUNING] = {
                name: t for name, t in stripped.items() if t
            }
        hass.config_entries.async_update_entry(
            entry, data=new_data_v5, options=new_options_v5, version=5
        )
        _LOGGER.debug(
            "Migrated Tortoise-UFH entry %s to v5 (retired the S6 tuning keys)",
            entry.entry_id,
        )

    if entry.version == 5:
        # Sparse global tuning (2026-10-22, issue #31; DECISIONS §35).
        from .core.config import controller_overrides
        from .tuning import knob_names

        stored: dict[str, Any] = {}
        for source in (entry.data, entry.options):
            tuning = source.get(CONF_CONTROLLER)
            if isinstance(tuning, dict):
                stored.update(tuning)
        kept = controller_overrides(stored, fields=knob_names(), drop_superseded=True)
        dropped = {key: value for key, value in stored.items() if key not in kept}
        new_data_v6 = {
            key: value for key, value in entry.data.items() if key != CONF_CONTROLLER
        }
        new_options_v6 = {
            key: value for key, value in entry.options.items() if key != CONF_CONTROLLER
        }
        if kept:
            new_options_v6[CONF_CONTROLLER] = kept
        hass.config_entries.async_update_entry(
            entry, data=new_data_v6, options=new_options_v6, version=6
        )
        if dropped:
            _LOGGER.info(
                "Migrated Tortoise-UFH entry %s to v6: global tuning is now stored "
                "as overrides only; dropped stored defaults and non-tuning "
                "fields %s, kept overrides %s",
                entry.entry_id,
                dropped,
                kept,
            )
        else:
            _LOGGER.debug(
                "Migrated Tortoise-UFH entry %s to v6 (kept overrides %s)",
                entry.entry_id,
                kept,
            )

    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: TortoiseUfhConfigEntry
) -> None:
    """Reload the integration when its options are updated.

    A change limited to the per-room control-state map
    (``entry.options[CONF_ROOM_STATE]``) applied through the coordinator's
    ``set_room_state`` is already reflected in memory (and rebroadcast), so it is
    NOT reloaded — a reload would needlessly reset the PID integrator. Any other
    options change (controller / per-room tuning, or a state map written directly
    by the options flow) reloads the entry, rebuilding the coordinator.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry whose options changed.
    """
    runtime = getattr(entry, "runtime_data", None)
    coordinator = runtime.coordinator if runtime is not None else None
    if coordinator is not None and not coordinator.options_require_reload(
        entry.options
    ):
        _LOGGER.debug(
            "Options change limited to room state; skipping reload for %s",
            entry.entry_id,
        )
        return
    _LOGGER.debug("Options updated for Tortoise-UFH entry: %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: TortoiseUfhConfigEntry
) -> bool:
    """Unload a Tortoise-UFH config entry.

    Unloads all platforms; when this was the last loaded entry for the HA
    instance, tears down the process-wide sidebar panel.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being unloaded.

    Returns:
        ``True`` when every platform unloaded cleanly.
    """
    from homeassistant.config_entries import ConfigEntryState

    from .const import DOMAIN, PLATFORMS
    from .panel import async_unregister_panel
    from .services import async_unregister_services

    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        # K3 (2026-07-12): kill the control machinery BEFORE the farewell.
        # The `entry.async_on_unload` callbacks (debouncer cancel, the base
        # coordinator auto-shutdown) only run AFTER this function returns, so
        # the 2-s recompute timer and the 5-min tick stayed alive through the
        # whole unload window and a cycle firing after the farewell re-opened
        # a parked cooling valve. The explicit shutdown also flushes the
        # setpoint Store (K5) while the entry still owns it.
        runtime.coordinator.async_cancel_recompute()
        await runtime.coordinator.async_shutdown()
        # Farewell command (C5): before releasing ownership, park every LIVE
        # room's actuators safely (split OFF; valve 0 in cooling) so an
        # unloaded entry never leaves an orphaned open valve outside the
        # dew-point guards.
        await runtime.coordinator.async_farewell_all()

    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        remaining = [
            other
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != entry.entry_id
            and other.state is ConfigEntryState.LOADED
        ]
        if not remaining:
            await async_unregister_panel(hass)
            async_unregister_services(hass)
            hass.data.get(DOMAIN, {}).pop(_FRONTEND_REGISTERED, None)
            _LOGGER.debug(
                "Unregistered Tortoise-UFH panel and services (last entry unloaded)"
            )

    _LOGGER.debug("Unloaded Tortoise-UFH entry: %s (ok=%s)", entry.entry_id, unload_ok)

    return unload_ok
