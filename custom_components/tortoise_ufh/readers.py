"""Source-entity readers for the Tortoise-UFH coordinator.

Extracted verbatim from ``coordinator.py`` (2026-07-10): everything that turns
a raw Home Assistant entity state into a validated controller input — the
short stale cache, the state-age gate (C4), the room-temperature plausibility
gate (C3), the per-loop valve-feedback plausibility (S8) and the fast-source /
heat-pump on-off mapping — now lives in one :class:`SourceReader` with its own
private state, so the read path is testable without the whole coordinator.

The coordinator owns exactly one :class:`SourceReader` and delegates; the
composition of a full :class:`~tortoise_ufh.models.RoomInputs` (which entity
feeds which field) stays in the coordinator.

Units: temperatures degrees Celsius, valve/humidity percent 0..100, state and
cache ages in seconds.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from homeassistant.core import split_entity_id

from .const import ENTITY_STALE_MAX_SECONDS
from .core.models import FastSourceKind

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "HUMIDITY_MAX_AGE_S",
    "HUMIDITY_STALE_MAX_AGE_S",
    "ROOM_TEMP_MAX_AGE_S",
    "UNAVAILABLE_STATES",
    "VALVE_DOMAIN",
    "SourceReader",
    "is_valve_domain",
]

# -- Input plausibility (fixed constants, deliberately NOT config knobs) ------
# Tailored to the owner's house: 1-wire/Zigbee room sensors in a high-mass UFH
# building. A real room cannot leave -10..50 degC, and its air temperature
# cannot move more than ~4 K between two 5-minute cycles — a bigger jump is a
# sensor fault (e.g. the DS18B20 85 degC power-on-reset), not physics.

_TEMP_PLAUSIBLE_MIN_C: float = -10.0
"""Lowest plausible room-air temperature [degC]; below -> reject the sample."""

_TEMP_PLAUSIBLE_MAX_C: float = 50.0
"""Highest plausible room-air temperature [degC]; above -> reject the sample."""

_TEMP_MAX_JUMP_K: float = 4.0
"""Max plausible room-temperature change between control cycles [K].

A sample jumping further than this from the last accepted value is rejected
(treated as a missing reading); two consecutive mutually consistent samples
taken at least :data:`_TEMP_CONFIRM_MIN_AGE_S` apart accept the new level, so
a real fast change is adopted within about two control cycles.
"""

_TEMP_CONFIRM_MIN_AGE_S: float = 270.0
"""Minimum age of a pending jump candidate before a consistent sample may
confirm it [s] (B5, 2026-07-12).

The confirmation must span REAL time, not merely two reads: a debounced
recompute burst used to deliver the "second consistent sample" 2-4 seconds
after the first, defeating the 4 K/cycle plausibility gate. 270 s is ~0.9 of
the nominal 300 s cycle, tolerating scheduler jitter without stretching the
two-cycle adoption promise.
"""

ROOM_TEMP_MAX_AGE_S: float = 45.0 * 60.0
"""Max age of a room-temperature state before it is treated as unavailable [s].

Guards against a present-but-frozen sensor (dead battery, stuck bridge): a
reading not re-reported for this long can no longer be trusted to control heat,
let alone chilled water.
"""

HUMIDITY_MAX_AGE_S: float = 60.0 * 60.0
"""Age up to which a humidity state counts as FRESH [s].

The most dangerous stale input: a frozen winter RH makes BOTH condensation
defences (global safe dew point and local S2 throttle) agree to pass water
below the real dew point. Since 2026-07-12 (K7) staleness is TWO-stage — see
:data:`HUMIDITY_STALE_MAX_AGE_S`.
"""

HUMIDITY_STALE_MAX_AGE_S: float = 120.0 * 60.0
"""Hard age limit for a humidity state [s] (K7, 2026-07-12).

Between :data:`HUMIDITY_MAX_AGE_S` and this limit the LAST value is still
served with a staleness fraction rising linearly 0 -> 1 across the window
(``RoomInputs.humidity_stale_frac``; D5, 2026-07-12) — the core then pads
the effective dew point by ``frac * 1 K`` instead of dropping the reading
entirely. Rationale: threshold-reporting RH sensors (e.g. SCD41 over Matter)
can legitimately pause near 60 min; a binary gate at 60 min made the cooling
limit-cycle (RH fresh -> cool -> aged out -> full stop -> repeat), and a
binary +1 K pad still stepped the throttle discontinuously at the edge.
Beyond this limit the reading is unusable and reads as ``None`` (full
conservative stop).
"""

_HP_INACTIVE_STATES: frozenset[str] = frozenset(
    {"off", "false", "idle", "standby", "0"}
)
"""HP-status states meaning the pump is NOT heating the UFH supply (freeze)."""

UNAVAILABLE_STATES: frozenset[str] = frozenset({"unavailable", "unknown", "none", ""})
"""Home Assistant state strings treated as "no reading"."""

VALVE_DOMAIN: str = "valve"
"""Home Assistant domain of position-capable ``valve`` actuator entities.

A ``valve`` reports its position in the ``current_position`` attribute (0..100)
and is driven via ``valve.set_valve_position`` (integer ``position``); a
``number`` valve reports the position as its numeric state and is driven via
``number.set_value`` (float ``value``). Everything else in the read/write path
is domain-agnostic.
"""


def is_valve_domain(entity_id: str) -> bool:
    """Return whether ``entity_id`` is a Home Assistant ``valve`` entity.

    ``valve`` actuators report position in the ``current_position`` attribute
    and are driven via ``valve.set_valve_position``; every other domain
    (``number`` …) reports position as its numeric state and is driven via
    ``number.set_value``.

    Args:
        entity_id: A non-empty Home Assistant entity id.

    Returns:
        ``True`` for a ``valve``-domain entity, ``False`` otherwise.
    """
    return split_entity_id(entity_id)[0] == VALVE_DOMAIN


class SourceReader:
    """Reads and plausibility-gates the coordinator's source entities.

    Stateful: owns the short stale cache (entity_id -> (value, timestamp)) and
    the room-temperature plausibility bookkeeping (last accepted value plus the
    pending candidate awaiting a second consistent sample, C3). One instance
    per coordinator; every method body is the verbatim translocation of the
    coordinator's former ``_read_*`` helpers (2026-07-10).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise an empty reader.

        Args:
            hass: The Home Assistant instance whose states are read.
        """
        self._hass = hass
        # Fallback cache for entity reads: entity_id -> (value, timestamp).
        # Deliberately PUBLIC (unlike the rest of the reader state): the
        # coordinator re-exposes it via its `_entity_cache` property, a
        # white-box contract read AND written by the HA test suite.
        self.entity_cache: dict[str, tuple[float, datetime]] = {}
        # Room-temperature plausibility state (C3): per-entity last accepted
        # value and the pending candidate awaiting a consistent confirmation
        # sample at least _TEMP_CONFIRM_MIN_AGE_S later (B5, 2026-07-12).
        self._temp_last_accepted: dict[str, float] = {}
        self._temp_pending: dict[str, tuple[float, datetime]] = {}

    def read_valve_position(self, entity_id: str | None) -> float | None:
        """Read a valve actuator's position [0..100 %], dispatching by domain.

        A ``valve``-domain actuator reports its position in the
        ``current_position`` attribute (its *state* is ``open`` / ``closed`` /
        ``opening`` / ``closing`` and is not numeric), so it is read from that
        attribute. Every other domain (``number`` …) reports the position as its
        numeric state and is read through :meth:`read_float_state` with its
        short stale cache — so the ``number`` path is byte-for-byte the previous
        behaviour.

        Args:
            entity_id: The valve actuator entity id, or ``None`` / empty when the
                loop has no valve at this position.

        Returns:
            The reported position [0..100 %], or ``None`` when it cannot be read.
        """
        if not entity_id:
            return None
        if not is_valve_domain(entity_id):
            value_or_none = self.read_float_state(entity_id)
        else:
            state = self._hass.states.get(entity_id)
            if state is None:
                return None
            position = state.attributes.get("current_position")
            if position is None:
                return None
            try:
                value = float(position)
            except (ValueError, TypeError):
                return None
            value_or_none = value if math.isfinite(value) else None
        # Per-loop plausibility (S8): a garbage feedback (e.g. 255 from a stuck
        # Modbus register) nulls ONLY this loop's feedback instead of tripping
        # the LoopInput validator and degrading the whole room to sensor_lost.
        if value_or_none is not None and not 0.0 <= value_or_none <= 100.0:
            _LOGGER.warning(
                "Valve %s reported implausible position %s%%; ignoring",
                entity_id,
                value_or_none,
            )
            return None
        return value_or_none

    def read_room_temperature(self, entity_id: str | None) -> float | None:
        """Read a room-temperature entity with plausibility gating (C3 + C4).

        On top of :meth:`read_float_state` (which already enforces the
        :data:`ROOM_TEMP_MAX_AGE_S` state age), a sample is rejected — treated
        exactly like a missing reading, triggering the core's safe degrade —
        when it is outside :data:`_TEMP_PLAUSIBLE_MIN_C` ..
        :data:`_TEMP_PLAUSIBLE_MAX_C` (e.g. the DS18B20 85 degC power-on-reset)
        or when it jumps more than :data:`_TEMP_MAX_JUMP_K` from the last
        accepted value. A consistent sample taken at least
        :data:`_TEMP_CONFIRM_MIN_AGE_S` after the first accepts the genuinely
        new level (B5, 2026-07-12: the confirmation must span real time — a
        debounced recompute burst used to confirm a bogus jump within
        seconds), so a real fast change is adopted within about two control
        cycles instead of being locked out forever.

        Args:
            entity_id: The room-temperature entity id, or ``None`` / empty.

        Returns:
            The accepted temperature [degC], or ``None`` when unavailable or
            rejected as implausible.
        """
        if not entity_id:
            return None
        value = self.read_float_state(entity_id, max_age_seconds=ROOM_TEMP_MAX_AGE_S)
        if value is None:
            self._temp_pending.pop(entity_id, None)
            return None
        if not _TEMP_PLAUSIBLE_MIN_C <= value <= _TEMP_PLAUSIBLE_MAX_C:
            _LOGGER.warning(
                "Entity %s reported implausible room temperature %.1f degC; "
                "rejecting sample",
                entity_id,
                value,
            )
            self._temp_pending.pop(entity_id, None)
            return None
        last = self._temp_last_accepted.get(entity_id)
        pending = self._temp_pending.get(entity_id)
        now = datetime.now(UTC)
        jumped = last is not None and abs(value - last) > _TEMP_MAX_JUMP_K
        confirmed = (
            pending is not None
            and abs(value - pending[0]) <= _TEMP_MAX_JUMP_K
            and (now - pending[1]).total_seconds() >= _TEMP_CONFIRM_MIN_AGE_S
        )
        if jumped and not confirmed:
            if pending is None or abs(value - pending[0]) > _TEMP_MAX_JUMP_K:
                # First sighting of this level (or the candidate moved again):
                # (re)start the confirmation clock.
                _LOGGER.warning(
                    "Entity %s jumped %.1f -> %.1f degC in one cycle; holding "
                    "sample for confirmation",
                    entity_id,
                    last,
                    value,
                )
                self._temp_pending[entity_id] = (value, now)
            return None
        # Either plausible against the last accepted value, or a consistent
        # confirmation at least one nominal cycle after the candidate — the
        # new level is real (e.g. a window opened), accept it.
        self._temp_last_accepted[entity_id] = value
        self._temp_pending.pop(entity_id, None)
        return value

    def read_humidity(self, entity_id: str | None) -> tuple[float | None, float]:
        """Read a humidity entity with the two-stage age gate (K7, 2026-07-12).

        Age <= :data:`HUMIDITY_MAX_AGE_S`: the reading is FRESH (fraction 0).
        Between that and :data:`HUMIDITY_STALE_MAX_AGE_S`: the LAST value is
        still served with a staleness fraction rising linearly 0 -> 1 across
        the window (D5, 2026-07-12 — the core pads its dew points by
        ``frac * 1 K``), so a threshold-reporting RH sensor can neither
        limit-cycle the cooling nor step the throttle at the 60-min edge.
        Older: ``(None, 0.0)`` (unusable).

        A value served from the short unavailable-entity cache carries the
        FULL fraction 1.0 (K7, 2026-07-12): the source entity just died, so
        the held value's true age is unknowable — the conservative pad must
        not vanish exactly when the sensor does.

        Args:
            entity_id: The humidity entity id, or ``None`` / empty.

        Returns:
            ``(value_pct, stale_frac)`` — the humidity [%] (or ``None``) and
            its staleness fraction in ``[0, 1]``.
        """
        if not entity_id:
            return None, 0.0
        value = self.read_float_state(
            entity_id, max_age_seconds=HUMIDITY_STALE_MAX_AGE_S
        )
        if value is None:
            return None, 0.0
        state = self._hass.states.get(entity_id)
        if state is None or state.state.lower() in UNAVAILABLE_STATES:
            # Value served from the short stale cache (<= 5 min old) because
            # the entity itself is gone: maximum conservatism (K7) — before
            # 2026-07-12 this branch reported FRESH, so the +1 K pad
            # disappeared at the very moment the RH sensor died.
            return value, 1.0
        reported = getattr(state, "last_reported", None) or state.last_updated
        age_s = (datetime.now(UTC) - reported).total_seconds()
        if age_s > HUMIDITY_MAX_AGE_S:
            frac = min(
                1.0,
                (age_s - HUMIDITY_MAX_AGE_S)
                / (HUMIDITY_STALE_MAX_AGE_S - HUMIDITY_MAX_AGE_S),
            )
            _LOGGER.debug(
                "Entity %s humidity is %.0f min old; serving as STALE "
                "(fraction %.2f -> +%.2f K dew pad in the core)",
                entity_id,
                age_s / 60.0,
                frac,
                frac,
            )
            return value, frac
        return value, 0.0

    def read_fast_source_hvac_mode(self, entity_id: str | None) -> str | None:
        """Read the fast source's raw HVAC-mode feedback string (K4).

        A ``climate`` entity's state IS its HVAC mode (``"heat"`` /
        ``"cool"`` / ``"off"`` / ``"dry"`` ...). The core uses unambiguous
        single-direction values to reconcile the commanded DIRECTION with the
        physical unit (multisplit standby, manual reversal).

        Args:
            entity_id: The fast-source climate entity id, or ``None`` / empty.

        Returns:
            The lower-cased state string, or ``None`` when no entity is
            configured or its state is unavailable.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state.lower() in UNAVAILABLE_STATES:
            return None
        return state.state.lower()

    @staticmethod
    def read_fast_source_kind(raw_kind: str | None) -> FastSourceKind:
        """Map the configured fast-source kind string to the core enum.

        Static: unlike its siblings this maps a room CONFIGURATION string, not
        an entity state, and touches no reader state — kept on the class only
        so the coordinator addresses one reading facade.

        Args:
            raw_kind: The room's configured kind string (may be ``None``).

        Returns:
            The :class:`~tortoise_ufh.models.FastSourceKind` (``NONE`` on an
            unrecognised or missing value).
        """
        raw = str(raw_kind or "none").lower()
        try:
            return FastSourceKind(raw)
        except ValueError:
            return FastSourceKind.NONE

    def read_fast_source_on(self, entity_id: str | None) -> bool | None:
        """Read the fast source's on/off feedback from its climate entity.

        Args:
            entity_id: The fast-source climate entity id, or ``None`` / empty.

        Returns:
            ``True`` if the split is running, ``False`` if off, ``None`` when no
            entity is configured or its state is unavailable.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state.lower() in UNAVAILABLE_STATES:
            return None
        return state.state.lower() != "off"

    def read_hp_active_for_ufh(self, entity_id: str | None) -> bool | None:
        """Read whether the heat pump is actively heating the UFH supply.

        Maps the optional heat-pump-status entity's state to the core's
        tri-state used for the integrator freeze during DHW / defrost:
        ``True`` when the pump is heating the floor, ``False`` when it is
        diverted (so the integrator freezes and cannot wind up), and ``None``
        when no entity is configured or its state is unavailable.

        Args:
            entity_id: The heat-pump-status entity id, or ``None`` / empty.

        Returns:
            ``True`` / ``False`` / ``None`` per above.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state.lower() in UNAVAILABLE_STATES:
            return None
        return state.state.lower() not in _HP_INACTIVE_STATES

    def read_float_state(
        self, entity_id: str | None, *, max_age_seconds: float | None = None
    ) -> float | None:
        """Read a numeric entity state with a short stale cache.

        On a successful read the value is cached with the current time. When the
        entity is unavailable/unknown the cached value is returned if it is
        younger than :data:`ENTITY_STALE_MAX_SECONDS`; otherwise ``None``.

        When ``max_age_seconds`` is given, a present-but-frozen state whose last
        report is older than that limit is treated as **no reading at all**
        (C4) — deliberately NOT falling back to the short cache, which would
        hold the very same stale value.

        Args:
            entity_id: The source entity id, or ``None`` / empty for "not
                configured".
            max_age_seconds: Optional maximum age of the state's last report
                [s]; older states are rejected outright.

        Returns:
            The numeric value, or ``None`` when unreadable and no fresh cache
            exists.
        """
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if (
            state is not None
            and state.state.lower() not in UNAVAILABLE_STATES
            and max_age_seconds is not None
        ):
            # last_reported also covers same-value re-reports (HA 2024.4+);
            # fall back to last_updated on older cores.
            reported = getattr(state, "last_reported", None) or state.last_updated
            age_s = (datetime.now(UTC) - reported).total_seconds()
            if age_s > max_age_seconds:
                _LOGGER.warning(
                    "Entity %s state is %.0f min old (limit %.0f min); "
                    "treating as unavailable",
                    entity_id,
                    age_s / 60.0,
                    max_age_seconds / 60.0,
                )
                return None
        if state is None or state.state.lower() in UNAVAILABLE_STATES:
            cached = self.entity_cache.get(entity_id)
            if cached is not None:
                value, ts = cached
                age = (datetime.now(UTC) - ts).total_seconds()
                if age <= ENTITY_STALE_MAX_SECONDS:
                    _LOGGER.debug(
                        "Entity %s unavailable; using cached %.2f (age %.0fs)",
                        entity_id,
                        value,
                        age,
                    )
                    return value
            _LOGGER.warning(
                "Entity %s is unavailable and no recent cached value exists",
                entity_id,
            )
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if not math.isfinite(value):
            _LOGGER.warning(
                "Entity %s reported non-finite value %r; ignoring",
                entity_id,
                state.state,
            )
            return None
        self.entity_cache[entity_id] = (value, datetime.now(UTC))
        return value
