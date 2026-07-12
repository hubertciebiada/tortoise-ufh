"""Physical constants and core defaults for the tortoise-ufh control brain.

This is a *pure* constants module: no classes, no runtime logic, no imports of
Home Assistant. Every value below is a module-level ``Final`` with its unit baked
into the name or documented inline. Config/result dataclasses live in
``config.py`` and ``models.py``; this file only holds scalars, unit-hint sets, and
reference ranges used across the core library.

Units contract (repo-wide, non-negotiable):
    * Temperatures in degrees Celsius (``_c``) or temperature *differences* in
      kelvin (``_k`` / ``dt``); 1 degC step == 1 K.
    * Thermal resistance ``R`` in K/W; thermal capacitance ``C`` in J/K.
    * Power in W; valve position in percent (0..100, float, ``_pct``).
    * Relative humidity in percent (0..100); global horizontal irradiance in W/m^2.
    * Time in minutes for simulation bookkeeping; seconds for the real-time cycle
      and ``RCModel.dt``.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Underfloor-loop physics (EN 1264 reduced model; see ufh_loop.loop_power)
# ---------------------------------------------------------------------------

K_PEX: Final[float] = 0.35
"""Effective heat-transfer coefficient of a PE-X pipe wall, in W/(m*K).

Lumped conductance per metre of pipe used by the EN 1264 reduced loop-power
model. Typical cross-linked-polyethylene UFH pipe.
"""

DEFAULT_DT_HEATING: Final[float] = 5.0
"""Default supply/return temperature spread in heating, in K.

Water cools by ~5 K across a loop at nominal heating flow.
"""

DEFAULT_DT_COOLING: Final[float] = 3.0
"""Default supply/return temperature spread in floor cooling, in K.

Smaller than heating because the driving gradient (and thus extracted power) is
lower in cooling.
"""

# ---------------------------------------------------------------------------
# Safety and comfort defaults
# ---------------------------------------------------------------------------

T_FLOOR_MAX_C: Final[float] = 34.0
"""Hard maximum floor-surface temperature, in degrees Celsius.

Comfort/standards ceiling for occupied living space; the controller and the
independent safety layer both treat this as a not-to-exceed limit.
"""

DEW_MARGIN_DEFAULT_K: Final[float] = 2.0
"""The system's DESIGN condensation margin above the dew point, in K.

The one working margin of the two-layer protection (2026-07-12, K6 — owner
decision "tylko pompa +2"): the global safe dew-point sensor reports
``max_over_cooled_rooms(T_dew) + this`` and the heat pump keeps the chilled
supply at/above that floor. The per-room graduated throttle
(``ControllerConfig.dew_margin_k``, same default value) reaches full opening
exactly at this gap and only throttles BELOW it; the hard S2 rule
(``safety.S2_HARD_MARGIN_K = 0``) backstops at the dew point itself. The
margins are deliberately NOT stacked on top of each other.
"""

DEFAULT_HOME_SETPOINT_C: Final[float] = 21.0
"""Default whole-home comfort setpoint, in degrees Celsius.

Per-room targets are ``home_setpoint + room_offset``.
"""

# ---------------------------------------------------------------------------
# Unit-hint sets (hardware-agnostic validation; accepted source-entity units)
# ---------------------------------------------------------------------------

VALID_TEMP_UNITS: Final[frozenset[str]] = frozenset({"°C", "C"})
"""Accepted unit strings for a temperature source entity (degrees Celsius)."""

VALID_PERCENT_UNITS: Final[frozenset[str]] = frozenset({"%"})
"""Accepted unit strings for percent quantities (valve position, humidity)."""

VALID_POWER_UNITS: Final[frozenset[str]] = frozenset({"W"})
"""Accepted unit strings for a power source entity (watts)."""

VALID_IRRADIANCE_UNITS: Final[frozenset[str]] = frozenset({"W/m²", "W/m2"})
"""Accepted unit strings for global horizontal irradiance (watts per m^2)."""

# ---------------------------------------------------------------------------
# Typical 3R3C RC-model ranges (documentation only; for building_profiles.py)
# ---------------------------------------------------------------------------
#
# Physically realistic values for a high-thermal-mass UFH room, given as
# reference ranges only. Real profiles are built in ``building_profiles.py``.
# States x = [T_air, T_slab, T_wall]; SISO input u = [Q_floor];
# disturbances d = [T_out, Q_sol, Q_int].
#
#   C_air   : air + light furnishings capacitance, J/K
#             ~60 kJ/K per 20 m^2 (scale ~proportional to floor area)
#   C_slab  : screed/slab capacitance, J/K
#             ~3 250 kJ/K per 80 mm screed over ~80 m^2 (dominant thermal mass)
#   C_wall  : wall/envelope capacitance, J/K   ~1 500 kJ/K
#   R_sf    : slab<->floor-surface resistance, K/W   ~0.01
#   R_wi    : wall-inner-node resistance, K/W         ~0.02
#   R_wo    : wall-outer-node resistance, K/W         ~0.03
#   R_ve    : ventilation/infiltration resistance, K/W ~0.03
#   R_ins   : insulation resistance, K/W              ~0.01
#   f_conv  : convective floor-emission fraction      ~0.6  (f_conv + f_rad <= 1)
#   f_rad   : radiative floor-emission fraction       ~0.4
#   T_ground: ground boundary temperature, degC       ~10.0
#
#   Derived: C_slab/C_air ratio ~50:1 (stiff system -> ZOH via expm);
#            slab time constant tau_slab = R_sf * C_slab ~ 4-6 h.
