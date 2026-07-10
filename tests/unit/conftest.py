"""Unit-only pytest fixtures and shared helpers for the Tortoise-UFH core suite.

These fixtures are scoped to ``tests/unit`` and are deliberately fast and
deterministic. All randomness is drawn from a seeded generator (seed 42) so
unit tests are perfectly reproducible, and the RC model uses a short time step
(``dt = 10 s``) for rapid convergence in short unit-test horizons. The module
also hosts :func:`make_inputs`, the plain (non-fixture) ``RoomInputs`` builder
shared by the controller test modules (``test_controller.py``,
``test_controller_safety.py``, ``test_fast_source.py``).

Units: temperatures in degC, capacitances in J/K, resistances in K/W, time in
seconds. This module never imports ``homeassistant``.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.random import Generator

from custom_components.tortoise_ufh.core.models import (
    FastSourceKind,
    LoopInput,
    Mode,
    RoomInputs,
)
from custom_components.tortoise_ufh.core.rc_model import ModelOrder, RCModel, RCParams


@pytest.fixture
def rng() -> Generator:
    """Return a seeded NumPy random generator (seed 42).

    Using a fixed seed keeps unit tests deterministic and independent of the
    global NumPy random state.

    Returns:
        A ``numpy.random.Generator`` seeded with 42.
    """
    return np.random.default_rng(42)


@pytest.fixture
def short_dt_model() -> RCModel:
    """Return a 3R3C RC model with a short 10-second time step.

    The short ``dt`` speeds convergence over the brief horizons exercised by
    unit tests while keeping the augmented-matrix-exponential ZOH stable. RC
    values are physically realistic for a ~20 m2 high-thermal-mass UFH room.

    Returns:
        An :class:`~tortoise_ufh.rc_model.RCModel` (3R3C, SISO) with
        ``dt = 10.0`` seconds.
    """
    params = RCParams(
        C_air=60_000.0,
        C_slab=3_250_000.0,
        R_sf=0.01,
        C_wall=1_500_000.0,
        R_wi=0.02,
        R_wo=0.03,
        R_ve=0.03,
        R_ins=0.01,
        f_conv=0.6,
        f_rad=0.4,
        T_ground=10.0,
        has_split=False,
    )
    return RCModel(params, ModelOrder.THREE, dt=10.0)


def make_inputs(
    *,
    mode: Mode = Mode.HEATING,
    setpoint_c: float = 21.0,
    room_temperature_c: float | None = 20.0,
    humidity_pct: float | None = None,
    outdoor_temperature_c: float | None = None,
    loops: tuple[LoopInput, ...] = (),
    fast_source_kind: FastSourceKind = FastSourceKind.NONE,
    hp_active_for_ufh: bool | None = None,
    cooling_enabled: bool = True,
) -> RoomInputs:
    """Build a :class:`RoomInputs` with test-friendly keyword defaults.

    Args:
        mode: Operating mode. Defaults to ``HEATING``.
        setpoint_c: Target temperature [degC]. Defaults to 21.0.
        room_temperature_c: Measured room temperature [degC], or ``None``.
        humidity_pct: Relative humidity [%], or ``None``.
        outdoor_temperature_c: Outdoor temperature [degC], or ``None``.
        loops: UFH loop probes/valve feedback.
        fast_source_kind: Kind of fast source available.
        hp_active_for_ufh: Heat-pump availability for UFH (freeze flag), or
            ``None``.
        cooling_enabled: Whether the room participates in cooling.

    Returns:
        A validated :class:`RoomInputs`.
    """
    return RoomInputs(
        mode=mode,
        setpoint_c=setpoint_c,
        room_temperature_c=room_temperature_c,
        humidity_pct=humidity_pct,
        outdoor_temperature_c=outdoor_temperature_c,
        loops=loops,
        fast_source_kind=fast_source_kind,
        hp_active_for_ufh=hp_active_for_ufh,
        cooling_enabled=cooling_enabled,
    )
