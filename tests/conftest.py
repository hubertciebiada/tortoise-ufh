"""Shared pytest fixtures for the tortoise-ufh test suite.

This root ``conftest.py`` provides the reusable, HA-free building blocks that
both the unit and simulation tiers depend on: realistic RC thermal parameters,
a discretized RC model, a small single-room building/room configuration, a
constant disturbance vector, and a seeded random generator.

These fixtures import ONLY from the pure core package ``tortoise_ufh`` — never
from ``homeassistant`` — so the core test suite runs with just numpy, scipy and
pytest installed.

Units (repo-wide):
    Temperatures degC; power W; resistances R in K/W; capacitances C in J/K;
    valve position 0..100 %; area m^2; latitude/longitude degrees; RC model
    ``dt`` in seconds.

The disturbance vector layout matches the 3R3C model: ``d = [T_out (degC),
Q_sol (W), Q_int (W)]``.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from custom_components.tortoise_ufh.core.config import (
    BuildingConfig,
    ControllerConfig,
    Orientation,
    RoomConfig,
    WindowConfig,
)
from custom_components.tortoise_ufh.core.rc_model import ModelOrder, RCModel, RCParams
from custom_components.tortoise_ufh.core.ufh_loop import LoopGeometry

# ---------------------------------------------------------------------------
# RC parameter / model fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def params_3r3c() -> RCParams:
    """Realistic 3R3C parameters for a ~20 m^2 UFH room (SISO, UFH only).

    High-thermal-mass values: ``C_slab / C_air`` ~ 54:1 (an 80 mm wet screed
    over a well-insulated slab), giving a slab discharge time constant of a few
    hours. Resistances in K/W, capacitances in J/K.

    Returns:
        A validated SISO (``has_split=False``) 3R3C :class:`RCParams`.
    """
    return RCParams(
        C_air=60_000.0,
        C_slab=3_250_000.0,
        C_wall=1_500_000.0,
        R_sf=0.01,
        R_wi=0.02,
        R_wo=0.03,
        R_ve=0.03,
        R_ins=0.01,
        f_conv=0.6,
        f_rad=0.4,
        T_ground=10.0,
        has_split=False,
    )


@pytest.fixture
def model_3r3c(params_3r3c: RCParams) -> RCModel:
    """3R3C SISO :class:`RCModel` discretized at ``dt = 60 s``.

    Args:
        params_3r3c: The realistic 3R3C parameters fixture.

    Returns:
        A ready-to-step 3R3C model (ZOH discretization via ``expm``).
    """
    return RCModel(params_3r3c, ModelOrder.THREE, dt=60.0)


# ---------------------------------------------------------------------------
# Configuration fixtures (a small single-room building)
# ---------------------------------------------------------------------------


@pytest.fixture
def controller_config() -> ControllerConfig:
    """Default per-room controller tuning (:class:`ControllerConfig`).

    Returns:
        A :class:`ControllerConfig` with the frozen default gains.
    """
    return ControllerConfig()


@pytest.fixture
def loop_geometry() -> LoopGeometry:
    """A single 16x2 mm PE-X UFH loop for a 20 m^2 room.

    ~110 m of pipe at 0.20 m spacing covering 20 m^2.

    Returns:
        A validated :class:`LoopGeometry`.
    """
    return LoopGeometry(
        effective_pipe_length_m=110.0,
        pipe_spacing_m=0.20,
        pipe_diameter_outer_mm=16.0,
        pipe_wall_thickness_mm=2.0,
        area_m2=20.0,
    )


@pytest.fixture
def room_config(
    params_3r3c: RCParams,
    controller_config: ControllerConfig,
    loop_geometry: LoopGeometry,
) -> RoomConfig:
    """A small single-room configuration (one UFH loop, no fast source).

    A 20 m^2 south-facing living room used as the canonical single-zone fixture
    across tests.

    Args:
        params_3r3c: The room's RC thermal parameters.
        controller_config: The room's controller tuning.
        loop_geometry: The room's UFH loop geometry.

    Returns:
        A validated :class:`RoomConfig` named ``"salon"``.
    """
    return RoomConfig(
        name="salon",
        area_m2=20.0,
        params=params_3r3c,
        n_loops=1,
        controller=controller_config,
        windows=(
            WindowConfig(orientation=Orientation.SOUTH, area_m2=3.0, g_value=0.6),
        ),
        loop_geometry=loop_geometry,
    )


@pytest.fixture
def building_config(room_config: RoomConfig) -> BuildingConfig:
    """A small single-room :class:`BuildingConfig` in southern Poland.

    One room, a 4.9 kW heat pump, at latitude 50.5 / longitude 19.5.

    Args:
        room_config: The single room to place in the building.

    Returns:
        A validated :class:`BuildingConfig` with one room.
    """
    return BuildingConfig(
        rooms=(room_config,),
        hp_max_power_w=4900.0,
        latitude=50.5,
        longitude=19.5,
        home_setpoint_c=21.0,
    )


# ---------------------------------------------------------------------------
# Disturbance / RNG fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def constant_disturbance() -> NDArray[np.float64]:
    """Constant 3R3C disturbance vector: ``T_out = 5 degC``, no solar/internal.

    Layout ``d = [T_out (degC), Q_sol (W), Q_int (W)]``.

    Returns:
        A ``float64`` array of shape ``(3,)``.
    """
    return np.array([5.0, 0.0, 0.0], dtype=np.float64)


@pytest.fixture
def rng() -> np.random.Generator:
    """A deterministically seeded random generator (seed 42).

    Uses ``np.random.default_rng`` so no test touches the global numpy RNG.

    Returns:
        A seeded :class:`numpy.random.Generator`.
    """
    return np.random.default_rng(42)
