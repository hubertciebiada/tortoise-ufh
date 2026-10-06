"""Loop water-probe health tracking for the Tortoise-UFH coordinator (issue #16).

The adapter half of :mod:`~tortoise_ufh.core.probe_health`: once per control
cycle it reads every configured loop supply/return probe, every manifold main
probe and the optional global supply probe through the
:class:`~tortoise_ufh.readers.SourceReader` sample gate, groups the loops by
the configured manifolds, feeds the pure
:class:`~tortoise_ufh.core.probe_health.ProbeHealthMonitor`, and persists its
learned baselines in a private :class:`~homeassistant.helpers.storage.Store`.

The coordinator then builds its ``LoopInput`` values from :meth:`loop_supply` /
:meth:`loop_return`, which hide a flagged probe from the core: a flagged
supply falls back to its manifold's main supply (or the global supply for a
loop on no manifold) and otherwise reads as missing; a flagged return reads
as missing. Nothing here writes to a device.

Units: temperatures degrees Celsius, valve positions percent 0..100, time
seconds.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import (
    CONF_ENTITY_RETURN,
    CONF_ENTITY_SUPPLY,
    CONF_ENTITY_VALVES,
    DOMAIN,
)
from .core.manifold import ManifoldConfig
from .core.probe_health import (
    LoopProbes,
    ManifoldProbes,
    ProbeHealthMonitor,
    ProbeHealthReport,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .readers import SourceReader

_LOGGER = logging.getLogger(__name__)

__all__ = ["PROBE_DRIFT_FLAG", "LoopWiring", "ProbeHealthTracker"]

PROBE_DRIFT_FLAG: str = "probe_drift"
"""Room report flag raised while any of the room's loop probes is flagged."""

_STORE_VERSION: int = 1
"""Schema version of the private probe-health Store."""

_SAVE_DELAY_S: float = 600.0
"""Delay of the coalesced Store save [s]; baselines change at most hourly."""

_UNASSIGNED_GROUP: str = "_unassigned"
"""Group id of the loops that sit on no configured manifold."""

_MIN_DT_S: float = 1.0
_MAX_DT_S: float = 900.0
"""Clamp of the measured cycle time fed to the monitor [s] (as the core's)."""


@dataclass(frozen=True)
class LoopWiring:
    """One configured loop: its room and its three entity ids.

    Attributes:
        room: The owning room's name.
        valve: The valve entity id, or ``None``.
        supply: The supply probe entity id, or ``None``.
        ret: The return probe entity id, or ``None``.
    """

    room: str
    valve: str | None
    supply: str | None
    ret: str | None


def room_loops(room_cfg: Mapping[str, Any], name: str) -> list[LoopWiring]:
    """Resolve a room's parallel entity lists into per-loop wiring.

    Loop count is the longest list, exactly like the coordinator's
    ``_build_loops``; a missing or empty position reads as ``None``.

    Args:
        room_cfg: The room's configuration dict.
        name: The room's name.

    Returns:
        One :class:`LoopWiring` per loop.
    """
    valves = [str(v or "") or None for v in room_cfg.get(CONF_ENTITY_VALVES) or []]
    supplies = [str(v or "") or None for v in room_cfg.get(CONF_ENTITY_SUPPLY) or []]
    returns = [str(v or "") or None for v in room_cfg.get(CONF_ENTITY_RETURN) or []]
    n_loops = max(len(valves), len(supplies), len(returns))
    return [
        LoopWiring(
            room=name,
            valve=valves[i] if i < len(valves) else None,
            supply=supplies[i] if i < len(supplies) else None,
            ret=returns[i] if i < len(returns) else None,
        )
        for i in range(n_loops)
    ]


class ProbeHealthTracker:
    """Per-coordinator probe-health state: readings, monitor, Store."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, reader: SourceReader
    ) -> None:
        """Initialise an empty tracker.

        Args:
            hass: The Home Assistant instance (for the Store).
            entry_id: The config entry id (Store key suffix).
            reader: The coordinator's source reader (sample-gated reads).
        """
        self._reader = reader
        self._store: Store[dict[str, Any]] = Store(
            hass, _STORE_VERSION, f"{DOMAIN}.probe_health.{entry_id}"
        )
        self._loaded: bool = False
        self._monitor = ProbeHealthMonitor()
        self._last_monotonic: float | None = None
        self._water: dict[str, float | None] = {}
        self._fallback: dict[str, float | None] = {}
        self._flagged: frozenset[str] = frozenset()
        self._report: ProbeHealthReport = ProbeHealthReport()
        self._saved: dict[str, Any] | None = None

    # -- persistence ----------------------------------------------------------

    async def async_load(self) -> None:
        """Restore the learned baselines once per instance."""
        if self._loaded:
            return
        self._loaded = True
        stored = await self._store.async_load()
        if isinstance(stored, Mapping):
            self._monitor = ProbeHealthMonitor.from_dict(stored)
            self._saved = self._monitor.to_dict()

    def schedule_save(self) -> None:
        """Queue a coalesced Store save when the learned state changed."""
        if not self._loaded:
            return
        snapshot = self._monitor.to_dict()
        if snapshot == self._saved:
            return
        self._saved = snapshot
        self._store.async_delay_save(lambda: snapshot, _SAVE_DELAY_S)

    async def async_flush(self) -> None:
        """Write the current learned state immediately (entry unload)."""
        if self._loaded:
            self._saved = self._monitor.to_dict()
            await self._store.async_save(self._saved)

    def reset_baselines(self, probe_ids: frozenset[str] | None = None) -> None:
        """Relearn some or all probes from scratch (sensor moved/replaced).

        Args:
            probe_ids: Probe entity ids, or ``None`` for every probe.
        """
        self._monitor.reset_baselines(probe_ids)
        self._flagged = self._monitor.flagged()
        self.schedule_save()

    # -- per-cycle -------------------------------------------------------------

    def update(
        self,
        *,
        loops: Sequence[LoopWiring],
        manifolds_raw: Any,
        global_supply_entity: str | None,
        valve_pct: Mapping[str, float | None],
        open_threshold_pct: float,
    ) -> None:
        """Read every probe once and advance the monitor by one cycle.

        Args:
            loops: Every configured loop of every room.
            manifolds_raw: ``entry.data[CONF_MANIFOLDS]`` (storage dicts);
                a corrupt entry is skipped.
            global_supply_entity: The optional global supply probe.
            valve_pct: Valve position per VALVE entity id [%] — the last
                command for a live room, the feedback otherwise.
            open_threshold_pct: The S6 ``flow_open_threshold_pct`` [%].
        """
        now = time.monotonic()
        if self._last_monotonic is None:
            dt_s = 300.0
        else:
            dt_s = min(_MAX_DT_S, max(_MIN_DT_S, now - self._last_monotonic))
        self._last_monotonic = now

        manifolds = _parse_manifolds(manifolds_raw)
        water: dict[str, float | None] = {}

        def read(entity_id: str | None) -> float | None:
            if not entity_id:
                return None
            if entity_id not in water:
                water[entity_id] = self._reader.read_water_temperature(entity_id)
            return water[entity_id]

        global_supply = read(global_supply_entity)
        by_valve = {loop.valve: loop for loop in loops if loop.valve}
        assigned: set[str] = set()
        groups: list[ManifoldProbes] = []
        fallback: dict[str, float | None] = {}
        for manifold in manifolds:
            main_supply = read(manifold.entity_supply_main)
            probes: list[LoopProbes] = []
            for slot in manifold.loops:
                wiring = by_valve.get(slot.entity_valve)
                if wiring is None or slot.entity_valve in assigned:
                    continue
                assigned.add(slot.entity_valve)
                probes.append(_loop_probes(wiring, read, valve_pct))
                if wiring.supply:
                    fallback[wiring.supply] = main_supply
            groups.append(
                ManifoldProbes(
                    group_id=manifold.manifold_id,
                    loops=tuple(probes),
                    main_supply_c=main_supply,
                    main_return_c=read(manifold.entity_return_main),
                )
            )
        rest = [loop for loop in loops if not loop.valve or loop.valve not in assigned]
        if rest:
            groups.append(
                ManifoldProbes(
                    group_id=_UNASSIGNED_GROUP,
                    loops=tuple(_loop_probes(w, read, valve_pct) for w in rest),
                    main_supply_c=global_supply,
                    learn=False,
                )
            )
            for wiring in rest:
                if wiring.supply:
                    fallback[wiring.supply] = global_supply

        self._report = self._monitor.update(
            groups, dt_seconds=dt_s, open_threshold_pct=open_threshold_pct
        )
        self._monitor.retain(
            frozenset(p for loop in loops for p in (loop.supply, loop.ret) if p)
        )
        newly = self._report.flagged - self._flagged
        for probe in sorted(newly):
            _LOGGER.warning(
                "Loop probe %s flagged as implausible (%s); hiding it from the "
                "safety inputs",
                probe,
                ", ".join(self._report.probes[probe].reasons),
            )
        self._flagged = self._report.flagged
        self._water = water
        self._fallback = fallback
        self.schedule_save()

    def invalidate(self) -> None:
        """Drop this cycle's readings after a failed :meth:`update`.

        The loop views then read each probe straight through the sample gate;
        probes already flagged stay hidden (a known-bad probe stays out).
        """
        self._water = {}
        self._fallback = {}

    # -- views ------------------------------------------------------------------

    @property
    def flagged(self) -> frozenset[str]:
        """Probe entity ids currently flagged."""
        return self._flagged

    def report(self) -> dict[str, Any]:
        """Per-probe diagnostics keyed by entity id (JSON-serialisable)."""
        return self._report.to_dict()

    def loop_supply(self, entity_id: str | None) -> float | None:
        """The supply value the core should see for one loop [degC].

        Args:
            entity_id: The loop's supply probe, or ``None``.

        Returns:
            The gated reading; for a flagged probe its manifold main supply
            (or ``None``).
        """
        if not entity_id:
            return None
        if entity_id in self._flagged:
            return self._fallback.get(entity_id)
        return self.water(entity_id)

    def loop_return(self, entity_id: str | None) -> float | None:
        """The return value the core should see for one loop [degC].

        Args:
            entity_id: The loop's return probe, or ``None``.

        Returns:
            The gated reading, or ``None`` when flagged or unreadable.
        """
        if not entity_id or entity_id in self._flagged:
            return None
        return self.water(entity_id)

    def water(self, entity_id: str | None) -> float | None:
        """The gated reading of any probe [degC].

        Served from this cycle's single read when the probe was part of it,
        else read through the sample gate now.

        Args:
            entity_id: The probe entity id, or ``None``.

        Returns:
            The gated reading, or ``None``.
        """
        if not entity_id:
            return None
        if entity_id in self._water:
            return self._water[entity_id]
        value = self._reader.read_water_temperature(entity_id)
        self._water[entity_id] = value
        return value


def _loop_probes(
    wiring: LoopWiring,
    read: Callable[[str | None], float | None],
    valve_pct: Mapping[str, float | None],
) -> LoopProbes:
    """Build the core :class:`LoopProbes` of one loop."""
    pct = valve_pct.get(wiring.valve) if wiring.valve else None
    if pct is not None and not 0.0 <= pct <= 100.0:
        pct = None
    return LoopProbes(
        supply_id=wiring.supply,
        return_id=wiring.ret,
        supply_c=read(wiring.supply),
        return_c=read(wiring.ret),
        valve_pct=pct,
    )


def _parse_manifolds(raw: Any) -> list[ManifoldConfig]:
    """Parse the stored manifold dicts, skipping a corrupt one."""
    manifolds: list[ManifoldConfig] = []
    for item in raw or []:
        try:
            manifolds.append(ManifoldConfig.from_dict(item))
        except (KeyError, TypeError, ValueError):
            _LOGGER.debug("Skipping invalid manifold %r for probe health", item)
    return manifolds
