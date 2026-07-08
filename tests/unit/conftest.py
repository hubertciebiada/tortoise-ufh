"""Unit-only pytest fixtures for the Tortoise-UFH core test suite.

These fixtures are scoped to ``tests/unit`` and are deliberately fast and
deterministic. All randomness is drawn from a seeded generator (seed 42) so
unit tests are perfectly reproducible, and the RC model uses a short time step
(``dt = 10 s``) for rapid convergence in short unit-test horizons.

Units: temperatures in degC, capacitances in J/K, resistances in K/W, time in
seconds. This module never imports ``homeassistant``.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.random import Generator

from tortoise_ufh.rc_model import ModelOrder, RCModel, RCParams


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
