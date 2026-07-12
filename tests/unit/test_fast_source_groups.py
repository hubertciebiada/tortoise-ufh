"""Unit tests for the multisplit group arbiter and the farewell sync (K4/K10).

Pins the 2026-07-12 round-2 contracts:

* K4 — ``BuildingController`` enforces ONE fast-source direction per
  ``fast_source_group`` (a shared multisplit outdoor unit): conflicting ON
  commands are arbitrated (largest comfort-band excess wins; a min-ON-locked
  unit pins the group), losers are forced OFF with the
  ``"fast_source_group_conflict"`` flag and re-engage only through a full
  min-OFF. Ungrouped rooms are untouched.
* K4c — the S4 reconciliation sees the DIRECTION: a unit physically running
  in a single-direction HVAC mode opposite to the commanded one raises
  ``"fast_source_mismatch"`` even though the on/off feedback agrees.
* K10 — ``notify_fast_source_farewell`` synchronises the machine with the
  adapter's out-of-band farewell OFF, so a return to live passes through an
  honest min-OFF instead of an instant ON.

Group names in the docstrings/fixtures are deliberately generic
(``outdoor_unit_a``). Units: temperatures in degC, ``dt_seconds`` in seconds.
This module never imports ``homeassistant``.
"""

from __future__ import annotations

import pytest

from custom_components.tortoise_ufh.core.config import ControllerConfig
from custom_components.tortoise_ufh.core.controller import (
    BuildingController,
    RoomController,
)
from custom_components.tortoise_ufh.core.models import (
    FastSourceKind,
    FastSourceMode,
    Mode,
    RoomInputs,
)
from tests.unit.conftest import make_inputs

pytestmark = pytest.mark.unit

_GROUP = "outdoor_unit_a"


def _transitional(
    *,
    setpoint_c: float = 21.0,
    room_temperature_c: float,
    group: str = _GROUP,
) -> RoomInputs:
    """Build TRANSITIONAL inputs for a grouped split room.

    Args:
        setpoint_c: Room target [degC].
        room_temperature_c: Measured temperature [degC].
        group: Multisplit group key (``""`` for ungrouped).

    Returns:
        A validated :class:`RoomInputs`.
    """
    return make_inputs(
        mode=Mode.TRANSITIONAL,
        setpoint_c=setpoint_c,
        room_temperature_c=room_temperature_c,
        fast_source_kind=FastSourceKind.SPLIT,
        fast_source_group=group,
    )


class TestGroupArbiter:
    """K4: one direction per shared outdoor unit."""

    def test_transitional_conflict_loser_forced_off(self) -> None:
        """Opposite TRANSITIONAL demands: the larger excess wins, the loser
        is OFF with the conflict flag."""
        building = BuildingController(
            {"south": ControllerConfig(), "north": ControllerConfig()}
        )
        out = building.step(
            {
                # South room overheated by 3 K -> wants COOLING (excess 2.7 K).
                "south": _transitional(room_temperature_c=24.0),
                # North room undercooled by 1.5 K -> wants HEATING (excess 1.2).
                "north": _transitional(room_temperature_c=19.5),
            },
            dt_seconds=300.0,
        )
        assert out.rooms["south"].fast_source.on is True
        assert out.rooms["south"].fast_source.mode is FastSourceMode.COOLING
        assert out.rooms["north"].fast_source.on is False
        assert "fast_source_group_conflict" in out.rooms["north"].report.flags
        assert "fast_source_group_conflict" not in out.rooms["south"].report.flags

    def test_loser_reengages_only_after_min_off(self) -> None:
        """The arbitrated OFF passes through an honest min-OFF dwell."""
        building = BuildingController(
            {"south": ControllerConfig(), "north": ControllerConfig()}
        )
        first = building.step(
            {
                "south": _transitional(room_temperature_c=24.0),
                "north": _transitional(room_temperature_c=19.5),
            },
            dt_seconds=300.0,
        )
        assert first.rooms["north"].fast_source.on is False
        # The south demand clears (room back at target); the north room still
        # wants heat 5 min later — but its machine just did ON->OFF, so the
        # full min-OFF (10 min) blocks it.
        second = building.step(
            {
                "south": _transitional(room_temperature_c=21.0),
                "north": _transitional(room_temperature_c=19.5),
            },
            dt_seconds=300.0,
        )
        assert second.rooms["north"].fast_source.on is False
        assert "fast_source_min_runtime" in second.rooms["north"].report.flags
        third = building.step(
            {
                "south": _transitional(room_temperature_c=21.0),
                "north": _transitional(room_temperature_c=19.5),
            },
            dt_seconds=300.0,
        )
        assert third.rooms["north"].fast_source.on is True
        assert third.rooms["north"].fast_source.mode is FastSourceMode.HEATING

    def test_min_on_locked_unit_pins_group_direction(self) -> None:
        """A unit held by min-ON in direction A blocks the group's B request.

        The arbiter never breaks a min-ON: the bigger|error| challenger loses
        while the running unit's dwell holds.
        """
        building = BuildingController(
            {"south": ControllerConfig(), "north": ControllerConfig()}
        )
        # Cycle 1: only the north room wants (and gets) HEATING.
        building.step(
            {
                "south": _transitional(room_temperature_c=21.0),
                "north": _transitional(room_temperature_c=19.5),
            },
            dt_seconds=300.0,
        )
        # Cycle 2 (5 min later, min-ON=10 min still holds): the south room now
        # screams for cooling with a LARGER excess — and still loses.
        out = building.step(
            {
                "south": _transitional(room_temperature_c=25.0),
                "north": _transitional(room_temperature_c=19.5),
            },
            dt_seconds=300.0,
        )
        assert out.rooms["north"].fast_source.on is True
        assert out.rooms["north"].fast_source.mode is FastSourceMode.HEATING
        assert out.rooms["south"].fast_source.on is False
        assert "fast_source_group_conflict" in out.rooms["south"].report.flags

    def test_ungrouped_rooms_keep_opposite_directions(self) -> None:
        """Rooms WITHOUT a group are untouched — opposite directions stand."""
        building = BuildingController(
            {"south": ControllerConfig(), "north": ControllerConfig()}
        )
        out = building.step(
            {
                "south": _transitional(room_temperature_c=24.0, group=""),
                "north": _transitional(room_temperature_c=19.5, group=""),
            },
            dt_seconds=300.0,
        )
        assert out.rooms["south"].fast_source.mode is FastSourceMode.COOLING
        assert out.rooms["north"].fast_source.mode is FastSourceMode.HEATING
        for room in out.rooms.values():
            assert "fast_source_group_conflict" not in room.report.flags

    def test_same_direction_group_not_arbitrated(self) -> None:
        """A group agreeing on one direction is left alone (no flags)."""
        building = BuildingController(
            {"south": ControllerConfig(), "north": ControllerConfig()}
        )
        out = building.step(
            {
                "south": _transitional(room_temperature_c=24.0),
                "north": _transitional(room_temperature_c=23.5),
            },
            dt_seconds=300.0,
        )
        for room in out.rooms.values():
            assert room.fast_source.mode is FastSourceMode.COOLING
            assert "fast_source_group_conflict" not in room.report.flags


class TestDirectionMismatch:
    """K4c: the S4 reconciliation sees a DIRECTION divergence."""

    def _heating_split(self, hvac_mode: str | None) -> RoomInputs:
        return make_inputs(
            room_temperature_c=19.0,  # past the boost offset: split ON
            fast_source_kind=FastSourceKind.SPLIT,
            fast_source_on=True,
            fast_source_hvac_mode=hvac_mode,
        )

    def test_opposite_direction_flags_mismatch(self) -> None:
        """Physically COOLING while commanded HEATING raises the flag."""
        controller = RoomController(ControllerConfig(), name="salon")
        first = controller.step(self._heating_split("heat"), dt_seconds=300.0)
        assert first.fast_source.mode is FastSourceMode.HEATING
        assert "fast_source_mismatch" not in first.report.flags
        # The unit reports ON but in "cool" (multisplit standby / manual
        # override): on/off agrees, the DIRECTION does not.
        out = controller.step(self._heating_split("cool"), dt_seconds=300.0)
        assert "fast_source_mismatch" in out.report.flags

    def test_agreeing_direction_stays_quiet(self) -> None:
        """A matching direction never flags."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._heating_split("heat"), dt_seconds=300.0)
        out = controller.step(self._heating_split("heat"), dt_seconds=300.0)
        assert "fast_source_mismatch" not in out.report.flags

    def test_unknown_mode_string_falls_back_to_onoff(self) -> None:
        """Non-directional strings (auto/dry/...) skip the direction check."""
        controller = RoomController(ControllerConfig(), name="salon")
        controller.step(self._heating_split("heat"), dt_seconds=300.0)
        out = controller.step(self._heating_split("dry"), dt_seconds=300.0)
        assert "fast_source_mismatch" not in out.report.flags


class TestFarewellSync:
    """K10: the adapter's farewell OFF is mirrored into the machine."""

    def test_farewell_forces_min_off_on_return(self) -> None:
        """After a farewell, re-engaging waits out the full min-OFF."""
        building = BuildingController({"salon": ControllerConfig()})
        boost = make_inputs(
            room_temperature_c=19.0,
            fast_source_kind=FastSourceKind.SPLIT,
        )
        first = building.step({"salon": boost}, dt_seconds=300.0)
        assert first.rooms["salon"].fast_source.on is True
        # The room leaves live: the adapter parks the split and notifies.
        building.notify_fast_source_farewell("salon")
        # Back to live seconds later, still demanding boost: min-OFF blocks.
        second = building.step({"salon": boost}, dt_seconds=5.0)
        assert second.rooms["salon"].fast_source.on is False
        assert "fast_source_min_runtime" in second.rooms["salon"].report.flags
        # After the full 10 min OFF, the boost honestly returns.
        third = building.step({"salon": boost}, dt_seconds=600.0)
        assert third.rooms["salon"].fast_source.on is True

    def test_farewell_unknown_room_is_ignored(self) -> None:
        """A farewell racing a room removal must not raise."""
        building = BuildingController({"salon": ControllerConfig()})
        building.notify_fast_source_farewell("nie_ma_takiego_pokoju")


class TestIncumbentHysteresis:
    """K2 (2026-07-12): the incumbent direction defends the aggregate."""

    def _step_conflict(
        self,
        building: BuildingController,
        *,
        t_heat_room: float,
        t_cool_room: float,
        dt_seconds: float = 300.0,
    ) -> dict[str, FastSourceMode | None]:
        out = building.step(
            {
                "north": _transitional(room_temperature_c=t_heat_room),
                "south": _transitional(room_temperature_c=t_cool_room),
            },
            dt_seconds=dt_seconds,
        )
        return {
            name: (room.fast_source.mode if room.fast_source.on else None)
            for name, room in out.rooms.items()
        }

    def test_noise_cannot_pingpong_a_persistent_conflict(self) -> None:
        """Symmetric ~2 K opposite demands + sensor noise: <= 1 reversal.

        The R3 audit measured 15 direction reversals in 2.5 h at zero dwells
        (7 at the default 10/10 min) with the bare max-excess winner; the
        +0.5 K incumbent hysteresis must hold the first winner against noise.
        """
        import random

        rng = random.Random(42)
        cfg = ControllerConfig(fast_min_on_minutes=0.0, fast_min_off_minutes=0.0)
        building = BuildingController({"north": cfg, "south": cfg})
        directions: list[FastSourceMode] = []
        for _ in range(30):
            modes = self._step_conflict(
                building,
                t_heat_room=19.0 + rng.gauss(0, 0.05),
                t_cool_room=23.0 + rng.gauss(0, 0.05),
            )
            on_modes = {m for m in modes.values() if m is not None}
            assert len(on_modes) <= 1  # one direction per aggregate, always
            if on_modes:
                directions.append(next(iter(on_modes)))
        reversals = sum(
            1 for a, b in zip(directions, directions[1:], strict=False) if a is not b
        )
        assert reversals <= 1

    def test_clearly_stronger_challenger_takes_over(self) -> None:
        """An excess advantage beyond +0.5 K still flips the group."""
        building = BuildingController(
            {"north": ControllerConfig(), "south": ControllerConfig()}
        )
        # North engages HEATING alone (excess 1.7 K) and becomes incumbent.
        modes = self._step_conflict(building, t_heat_room=19.0, t_cool_room=21.0)
        assert modes["north"] is FastSourceMode.HEATING
        # South heats up to a 2.7 K excess (> 1.7 + 0.5): it must win once
        # the incumbent's min-ON (10 min) and its own min-OFF elapse.
        seen: list[dict[str, FastSourceMode | None]] = []
        for _ in range(4):
            seen.append(
                self._step_conflict(building, t_heat_room=19.0, t_cool_room=24.0)
            )
        assert seen[-1]["south"] is FastSourceMode.COOLING
        assert seen[-1]["north"] is None

    def test_marginally_stronger_challenger_does_not_take_over(self) -> None:
        """An excess advantage inside the 0.5 K hysteresis changes nothing."""
        building = BuildingController(
            {"north": ControllerConfig(), "south": ControllerConfig()}
        )
        modes = self._step_conflict(building, t_heat_room=19.0, t_cool_room=21.0)
        assert modes["north"] is FastSourceMode.HEATING
        # South's excess 2.0 K vs north's 1.7 K: within the +0.5 K band.
        for _ in range(6):
            modes = self._step_conflict(building, t_heat_room=19.0, t_cool_room=23.3)
        assert modes["north"] is FastSourceMode.HEATING
        assert modes["south"] is None

    def test_reset_clears_the_incumbency(self) -> None:
        """reset() forgets the last winner: the next conflict is excess-only."""
        building = BuildingController(
            {"north": ControllerConfig(), "south": ControllerConfig()}
        )
        modes = self._step_conflict(building, t_heat_room=19.0, t_cool_room=21.0)
        assert modes["north"] is FastSourceMode.HEATING
        building.reset()
        # Fresh machines, marginally bigger cooling excess: cooling wins now
        # (with the stored incumbent it would have stayed inside hysteresis).
        modes = self._step_conflict(building, t_heat_room=19.0, t_cool_room=23.3)
        assert modes["south"] is FastSourceMode.COOLING
        assert modes["north"] is None
