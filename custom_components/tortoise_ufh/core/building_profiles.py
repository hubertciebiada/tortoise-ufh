"""Predefined building profiles for the tortoise-ufh simulator.

Factory functions returning validated :class:`~tortoise_ufh.config.BuildingConfig`
instances with physically realistic RC parameters, plus a name -> factory
registry (:data:`BUILDING_PROFILES`).

``modern_bungalow`` is the calibrated single-storey (parterowy) reference house
from the PRD: ~158 m^2 heated across 13 rooms, one UFH loop per room (13 loops),
a 4.9 kW air-source heat pump, ~7 cm wet screed, at latitude 50.5, longitude
19.5 (southern Poland).  The remaining profiles (``well_insulated``,
``leaky_old_house``, ``thin_screed``, ``heavy_construction``) are single-room
parametric variants for sweep studies and sanity checks.

This module is part of the pure core: it MUST NOT import ``homeassistant``. Its
only dependencies are the standard library and sibling core modules.

Units (repo-wide):
    R in K/W, C in J/K, temperatures in degC, power in W, length in m,
    pipe diameter/thickness in mm, area in m^2, latitude/longitude in degrees.

Typical usage::

    from custom_components.tortoise_ufh.core.building_profiles import (
        BUILDING_PROFILES,
        modern_bungalow,
    )

    building = modern_bungalow()
    assert len(building.rooms) == 13

    factory = BUILDING_PROFILES["leaky_old_house"]
    building = factory()
"""

from __future__ import annotations

from collections.abc import Callable

from .config import (
    BuildingConfig,
    Orientation,
    RoomConfig,
    WindowConfig,
)
from .models import FastSourceKind
from .rc_model import RCParams
from .ufh_loop import LoopGeometry

__all__ = [
    "BUILDING_PROFILES",
    "MODERN_BUNGALOW_ROOMS",
    "heavy_construction",
    "leaky_old_house",
    "modern_bungalow",
    "thin_screed",
    "well_insulated",
    "well_insulated_with_split",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_REF_AREA: float = 20.0
"""Reference room area [m^2] at which the ``*_ref`` capacitances/resistances
below are quoted.  Capacitances scale proportionally to area (mass/volume
proxy); resistances scale inversely (larger surfaces conduct more)."""

# Standard UFH pipe used throughout (PE-X/Al/PE-X 16x2 mm).
_PIPE_DIAMETER_OUTER_MM: float = 16.0
_PIPE_WALL_THICKNESS_MM: float = 2.0

# Fallback pipe spacing [m] for the single-room parametric profiles.
_SINGLE_ROOM_PIPE_SPACING_M: float = 0.20


def _make_3r3c_params(
    area_m2: float,
    *,
    has_split: bool = False,
    C_air_ref: float = 300_000.0,
    C_slab_ref: float = 3_250_000.0,
    C_wall_ref: float = 1_500_000.0,
    R_sf_ref: float = 0.005,
    R_wi_ref: float = 0.04,
    R_wo_ref: float = 0.15,
    R_ve_ref: float = 0.20,
    R_ins_ref: float = 0.28,
    f_conv: float = 0.3,
    f_rad: float = 0.2,
    f_slab: float = 0.5,
    T_ground: float = 14.0,
) -> RCParams:
    """Build a 3R3C :class:`RCParams` with area-based scaling.

    Capacitances are scaled proportionally to ``area_m2 / _REF_AREA``
    (a proxy for thermal mass); resistances are scaled inversely
    (``_REF_AREA / area_m2``: larger heat-exchange surfaces conduct more).

    The reference defaults describe a well-insulated modern single-storey house
    (~30 cm mineral-wool walls, ~20 cm ceiling wool, ~7 cm wet screed).
    Calibration amendment 2026-07-09 (five-agent FMEA, report-algo-thermal
    K2/I3 + R2/R6):

    * ``R_sf_ref = 0.005`` K/W @ 20 m^2 — combined radiative+convective floor
      film coefficient h ~ 10 W/(m^2*K) (the old 0.01 meant h ~ 5, a slab 2x
      too slow to discharge). Slab eigenmode
      ``tau = C_slab / (1/R_sf + 1/R_ins) ~ 4.4 h`` — inside the realistic
      2-5 h band for a 7 cm wet screed.
    * ``R_ins_ref = 0.28`` K/W @ 20 m^2 — effective sub-slab U ~ 0.18
      W/(m^2*K) (WT-2021 floor-on-ground incl. ground mass; the old 0.05 made
      the ground a bigger heat sink than the entire envelope and silently
      cooled the house in July).
    * ``T_ground = 14`` degC winter effective ground temperature under a
      heated slab; summer scenarios pass ~17 degC (seasonal, see the
      ``t_ground`` factory parameters).
    * ``C_air_ref = 300 kJ/K`` — room air PLUS fast-responding furnishings
      (bare air alone gave an unrealistic ~7 min split response).
    * Solar split ``f_slab/f_conv/f_rad = 0.5/0.3/0.2`` — through-window sun
      lands mostly on the FLOOR of a UFH room, charging the slab.

    Args:
        area_m2: Room floor area [m^2] (must be > 0).
        has_split: Whether the room has a fast source (sets the MIMO input flag
            on the returned :class:`RCParams`).
        C_air_ref: Air+furnishings capacitance at ``_REF_AREA`` [J/K].
        C_slab_ref: Slab-node capacitance at ``_REF_AREA`` [J/K].
        C_wall_ref: Wall-node capacitance at ``_REF_AREA`` [J/K].
        R_sf_ref: Slab-to-air (floor surface) resistance at ``_REF_AREA`` [K/W].
        R_wi_ref: Wall-to-interior resistance at ``_REF_AREA`` [K/W].
        R_wo_ref: Wall-to-outdoor resistance at ``_REF_AREA`` [K/W].
        R_ve_ref: Ventilation/infiltration resistance at ``_REF_AREA`` [K/W].
        R_ins_ref: Sub-slab insulation resistance at ``_REF_AREA`` [K/W].
        f_conv: Convective solar fraction to the air node [-].
        f_rad: Radiative solar fraction to the wall node [-].
        f_slab: Solar fraction landing on the floor (slab node) [-].
        T_ground: Ground temperature beneath the slab [degC].

    Returns:
        A validated 3R3C :class:`RCParams` instance.

    Raises:
        ValueError: If ``area_m2`` is non-positive, or the produced parameters
            violate :class:`RCParams` invariants.
    """
    if area_m2 <= 0:
        msg = f"area_m2 must be > 0, got {area_m2}"
        raise ValueError(msg)

    scale = area_m2 / _REF_AREA
    inv_scale = _REF_AREA / area_m2
    return RCParams(
        C_air=C_air_ref * scale,
        C_slab=C_slab_ref * scale,
        C_wall=C_wall_ref * scale,
        R_sf=R_sf_ref * inv_scale,
        R_wi=R_wi_ref * inv_scale,
        R_wo=R_wo_ref * inv_scale,
        R_ve=R_ve_ref * inv_scale,
        R_ins=R_ins_ref * inv_scale,
        f_conv=f_conv,
        f_rad=f_rad,
        f_slab=f_slab,
        T_ground=T_ground,
        has_split=has_split,
    )


def _loop_geometry(
    length_m: float,
    spacing_m: float,
    area_m2: float,
) -> LoopGeometry:
    """Build a :class:`LoopGeometry` for a 16x2 mm PE-X UFH loop.

    Args:
        length_m: Total installed pipe length in the zone [m] (> 0).
        spacing_m: Centre-to-centre pipe spacing [m] (> 0).
        area_m2: Heated floor area covered by the loop [m^2] (> 0).

    Returns:
        A validated :class:`LoopGeometry` instance.
    """
    return LoopGeometry(
        effective_pipe_length_m=length_m,
        pipe_spacing_m=spacing_m,
        pipe_diameter_outer_mm=_PIPE_DIAMETER_OUTER_MM,
        pipe_wall_thickness_mm=_PIPE_WALL_THICKNESS_MM,
        area_m2=area_m2,
    )


# ---------------------------------------------------------------------------
# modern_bungalow — single-storey (parterowy) reference house, 13 UFH loops
# ---------------------------------------------------------------------------
#
# The PRD reference house: an anonymized real-world WT-2021-class single-storey
# building in southern Poland (lat 50.5, lon 19.5).  ~165 m^2 total, ~158 m^2
# heated across 13 rooms; ~30 cm mineral-wool walls, ~20 cm ceiling wool, ~7 cm
# wet screed.  Heated by a 4.9 kW air-source heat pump.  One UFH loop per room
# (13 loops) via two distributors; pipe PE-X/Al/PE-X 16x2 mm throughout.  No
# fast sources.  Loop lengths/spacings are anonymized real installation data.

_BUNGALOW_LAT: float = 50.5
_BUNGALOW_LON: float = 19.5
_BUNGALOW_HP_MAX_W: float = 4900.0

# (name, area_m2, pipe_length_m, pipe_spacing_m, windows) per room / loop.
_BUNGALOW_ROOM_SPECS: tuple[
    tuple[str, float, float, float, tuple[WindowConfig, ...]], ...
] = (
    (
        "salon",
        36.28,
        96.0,
        0.20,
        (
            WindowConfig(orientation=Orientation.SOUTH, area_m2=5.0, g_value=0.6),
            WindowConfig(orientation=Orientation.WEST, area_m2=3.0, g_value=0.6),
        ),
    ),
    (
        "sypialnia",
        12.68,
        42.9,
        0.20,
        (WindowConfig(orientation=Orientation.SOUTH, area_m2=2.5, g_value=0.6),),
    ),
    (
        "pokoj_dziecka_1",
        14.33,
        36.5,
        0.20,
        (WindowConfig(orientation=Orientation.EAST, area_m2=2.0, g_value=0.6),),
    ),
    (
        "pokoj_dziecka_2",
        11.65,
        31.1,
        0.20,
        (WindowConfig(orientation=Orientation.EAST, area_m2=2.0, g_value=0.6),),
    ),
    (
        "kuchnia_jadalnia",
        13.59,
        29.3,
        0.20,
        (WindowConfig(orientation=Orientation.EAST, area_m2=2.0, g_value=0.6),),
    ),
    (
        "gabinet_1",
        13.00,
        29.6,
        0.20,
        (WindowConfig(orientation=Orientation.NORTH, area_m2=1.5, g_value=0.6),),
    ),
    (
        "gabinet_2",
        12.62,
        27.4,
        0.20,
        (WindowConfig(orientation=Orientation.NORTH, area_m2=1.5, g_value=0.6),),
    ),
    ("dlugi_korytarz", 12.12, 25.4, 0.20, ()),
    ("garderoba", 7.40, 23.3, 0.20, ()),
    (
        "lazienka",
        8.90,
        34.6,
        0.15,
        (WindowConfig(orientation=Orientation.NORTH, area_m2=0.5, g_value=0.6),),
    ),
    ("korytarz_witryna", 5.00, 15.5, 0.20, ()),
    (
        "wiatrolap",
        5.05,
        15.2,
        0.20,
        (WindowConfig(orientation=Orientation.NORTH, area_m2=1.0, g_value=0.6),),
    ),
    ("toaleta", 5.49, 24.6, 0.20, ()),
)


def _bungalow_room(
    name: str,
    area_m2: float,
    length_m: float,
    spacing_m: float,
    windows: tuple[WindowConfig, ...],
    t_ground: float = 14.0,
) -> RoomConfig:
    """Build one modern-bungalow room with a single UFH loop.

    Args:
        name: Room identifier.
        area_m2: Floor area [m^2].
        length_m: Installed pipe length for the room's loop [m].
        spacing_m: Pipe spacing for the room's loop [m].
        windows: Window configurations for solar-gain modelling.
        t_ground: Seasonal effective ground temperature beneath the slab
            [degC] (winter ~14, summer ~17).

    Returns:
        A validated :class:`RoomConfig` (one loop, no fast source).
    """
    return RoomConfig(
        name=name,
        area_m2=area_m2,
        params=_make_3r3c_params(area_m2, T_ground=t_ground),
        n_loops=1,
        windows=windows,
        loop_geometry=_loop_geometry(length_m, spacing_m, area_m2),
    )


MODERN_BUNGALOW_ROOMS: tuple[RoomConfig, ...] = tuple(
    _bungalow_room(name, area, length, spacing, windows)
    for name, area, length, spacing, windows in _BUNGALOW_ROOM_SPECS
)
"""All 13 heated rooms of the reference modern bungalow (one UFH loop each,
winter ground temperature)."""


def modern_bungalow(*, t_ground: float = 14.0) -> BuildingConfig:
    """Reference modern bungalow — 13 rooms, 13 UFH loops, no fast source.

    Single-storey (parterowy) house calibrated to an anonymized real-world
    WT-2021-class building in southern Poland (lat 50.5, lon 19.5), heated by
    a 4.9 kW air-source heat pump.  RC parameters reflect ~30 cm mineral-wool
    walls, ~20 cm ceiling wool and a ~7 cm (80 mm) wet screed.  One UFH loop per
    room (13 loops total, PE-X 16x2 mm); loop geometries come from anonymized
    real installation data.

    Args:
        t_ground: Seasonal effective ground temperature [degC] — ~14 in the
            heating season, ~17 in summer (cooling scenarios).

    Returns:
        A validated :class:`BuildingConfig` with 13 rooms.
    """
    rooms = (
        MODERN_BUNGALOW_ROOMS
        if t_ground == 14.0
        else tuple(
            _bungalow_room(name, area, length, spacing, windows, t_ground=t_ground)
            for name, area, length, spacing, windows in _BUNGALOW_ROOM_SPECS
        )
    )
    return BuildingConfig(
        rooms=rooms,
        hp_max_power_w=_BUNGALOW_HP_MAX_W,
        latitude=_BUNGALOW_LAT,
        longitude=_BUNGALOW_LON,
    )


# ---------------------------------------------------------------------------
# Single-room parametric variants (for sweeps / sanity checks)
# ---------------------------------------------------------------------------


def _single_room(
    params: RCParams,
    *,
    windows: tuple[WindowConfig, ...],
    n_loops: int = 2,
    area_m2: float = 20.0,
) -> RoomConfig:
    """Build a single ``main`` room with an estimated UFH loop geometry.

    The loop length is estimated as ``area_m2 / spacing * 1.1`` (bend margin)
    at :data:`_SINGLE_ROOM_PIPE_SPACING_M`.

    Args:
        params: Thermal parameters for the room.
        windows: Window configurations for solar-gain modelling.
        n_loops: Number of UFH loops sharing the room's valve (>= 1).
        area_m2: Floor area [m^2].

    Returns:
        A validated :class:`RoomConfig` named ``"main"``.
    """
    length_m = area_m2 / _SINGLE_ROOM_PIPE_SPACING_M * 1.1
    return RoomConfig(
        name="main",
        area_m2=area_m2,
        params=params,
        n_loops=n_loops,
        windows=windows,
        loop_geometry=_loop_geometry(length_m, _SINGLE_ROOM_PIPE_SPACING_M, area_m2),
    )


def well_insulated(*, t_ground: float = 14.0) -> BuildingConfig:
    """Well-insulated modern building — low heat loss, high thermal mass.

    Thick walls, triple glazing and mechanical ventilation with heat recovery
    (MVHR): high wall/ventilation/insulation resistances across the board.

    Args:
        t_ground: Seasonal effective ground temperature [degC] — ~14 in the
            heating season, ~17 in summer (cooling scenarios).

    Returns:
        A validated :class:`BuildingConfig` with 1 room.
    """
    params = _make_3r3c_params(
        20.0,
        C_wall_ref=2_000_000.0,
        R_wo_ref=0.18,
        R_ve_ref=0.25,
        R_ins_ref=0.30,
        T_ground=t_ground,
    )
    room = _single_room(
        params,
        windows=(
            WindowConfig(orientation=Orientation.SOUTH, area_m2=3.0, g_value=0.5),
        ),
    )
    return BuildingConfig(
        rooms=(room,),
        hp_max_power_w=6000.0,
        latitude=50.0,
        longitude=20.0,
    )


def well_insulated_with_split(*, t_ground: float = 14.0) -> BuildingConfig:
    """The ``well_insulated`` room plus a 2.5 kW split as fast source.

    Same fabric as :func:`well_insulated`, but the room carries a 2.5 kW
    wall-split (``FastSourceKind.SPLIT``, MIMO RC input), so closed-loop
    scenarios can exercise the boost engage/release hysteresis, the min ON/OFF
    dwell machine and anti priority-inversion against the physics
    (calibration amendment 2026-07-09, report-algo-thermal I7).

    Args:
        t_ground: Seasonal effective ground temperature [degC].

    Returns:
        A validated :class:`BuildingConfig` with 1 split-assisted room.
    """
    params = _make_3r3c_params(
        20.0,
        has_split=True,
        C_wall_ref=2_000_000.0,
        R_wo_ref=0.18,
        R_ve_ref=0.25,
        R_ins_ref=0.30,
        T_ground=t_ground,
    )
    length_m = 20.0 / _SINGLE_ROOM_PIPE_SPACING_M * 1.1
    room = RoomConfig(
        name="main",
        area_m2=20.0,
        params=params,
        n_loops=2,
        has_fast_source=True,
        fast_source_kind=FastSourceKind.SPLIT,
        fast_source_power_w=2500.0,
        windows=(
            WindowConfig(orientation=Orientation.SOUTH, area_m2=3.0, g_value=0.5),
        ),
        loop_geometry=_loop_geometry(length_m, _SINGLE_ROOM_PIPE_SPACING_M, 20.0),
    )
    return BuildingConfig(
        rooms=(room,),
        hp_max_power_w=6000.0,
        latitude=50.0,
        longitude=20.0,
    )


def leaky_old_house() -> BuildingConfig:
    """Leaky, poorly insulated pre-1970s house — high heat loss.

    Thin uninsulated walls, single glazing and natural ventilation with
    significant infiltration: low wall/ventilation/insulation resistances.

    Returns:
        A validated :class:`BuildingConfig` with 1 room.
    """
    # Calibration 2026-07-09: the old values (R_wo 0.012 / R_ve 0.008 /
    # R_ins 0.005 @ 20 m^2) implied a physically absurd ~620 W/m^2 design
    # loss; these land at a plausible ~130-150 W/m^2 at -20 degC design.
    params = _make_3r3c_params(
        20.0,
        C_wall_ref=1_200_000.0,
        R_wi_ref=0.03,
        R_wo_ref=0.05,
        R_ve_ref=0.04,
        R_ins_ref=0.10,
    )
    room = _single_room(
        params,
        windows=(
            WindowConfig(orientation=Orientation.SOUTH, area_m2=2.0, g_value=0.7),
            WindowConfig(orientation=Orientation.NORTH, area_m2=1.5, g_value=0.7),
        ),
    )
    return BuildingConfig(
        rooms=(room,),
        hp_max_power_w=12000.0,
        latitude=52.0,
        longitude=21.0,
    )


def thin_screed() -> BuildingConfig:
    """Thin-screed building — fast thermal response, low slab mass.

    A ~30 mm dry screed instead of an 80 mm wet screed: ``C_slab`` is roughly
    40 % of the reference value.  Faster to heat up but stores less energy.

    Returns:
        A validated :class:`BuildingConfig` with 1 room.
    """
    params = _make_3r3c_params(
        20.0,
        C_slab_ref=1_300_000.0,
        R_sf_ref=0.004,
    )
    room = _single_room(
        params,
        windows=(
            WindowConfig(orientation=Orientation.SOUTH, area_m2=2.5, g_value=0.6),
        ),
    )
    return BuildingConfig(
        rooms=(room,),
        hp_max_power_w=8000.0,
        latitude=50.0,
        longitude=20.0,
    )


def heavy_construction() -> BuildingConfig:
    """Heavy-construction building — very high thermal mass throughout.

    A thick concrete screed (~120 mm) and massive brick/concrete walls:
    ``C_slab`` and ``C_wall`` are well above the reference.  Slow thermal
    response but excellent energy storage.

    Returns:
        A validated :class:`BuildingConfig` with 1 room.
    """
    params = _make_3r3c_params(
        20.0,
        C_slab_ref=4_875_000.0,
        C_wall_ref=3_000_000.0,
        R_sf_ref=0.006,
    )
    room = _single_room(
        params,
        windows=(
            WindowConfig(orientation=Orientation.SOUTH, area_m2=2.5, g_value=0.6),
        ),
    )
    return BuildingConfig(
        rooms=(room,),
        hp_max_power_w=10000.0,
        latitude=50.0,
        longitude=20.0,
    )


# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------

BUILDING_PROFILES: dict[str, Callable[[], BuildingConfig]] = {
    "modern_bungalow": modern_bungalow,
    "well_insulated": well_insulated,
    "well_insulated_with_split": well_insulated_with_split,
    "leaky_old_house": leaky_old_house,
    "thin_screed": thin_screed,
    "heavy_construction": heavy_construction,
}
"""Mapping of profile name to its :class:`BuildingConfig` factory function."""
