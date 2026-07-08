"""Weather-compensation supply-temperature curves (feedforward).

In real heat-pump installations the supply water temperature ``t_supply`` is
not constant — it follows a weather-compensation curve mapping the outdoor
temperature ``t_out`` to a target supply temperature.  The controller uses
these curves as a modest feedforward term and the simulator uses them to pick
the supply temperature that feeds ``ufh_loop.loop_power``.

Two frozen dataclasses are provided:

* :class:`WeatherCompCurve` — **heating** mode.  Supply temperature *rises* as
  ``t_out`` drops below ``t_neutral`` (more heat needed in colder weather).
* :class:`CoolingCompCurve` — **cooling** mode.  Supply temperature *rises* as
  ``t_out`` climbs above ``t_neutral`` (less aggressive chilled-water supply in
  hotter weather, protecting against condensation and matching compressor
  limits).

Both expose ``t_supply(t_out)`` returning the clamped supply temperature, plus
``to_dict`` / ``from_dict`` for JSON-safe serialisation.

Units follow project convention: all temperatures in **degC**; ``slope`` in
**K_supply / K_outdoor**.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Self

__all__ = [
    "CoolingCompCurve",
    "WeatherCompCurve",
]


@dataclass(frozen=True)
class WeatherCompCurve:
    """Weather-compensation curve for **heating** supply temperature.

    The supply temperature is computed as::

        t_supply = clip(
            t_supply_base + slope * max(0, t_neutral - t_out),
            t_supply_min,
            t_supply_max,
        )

    When ``t_out >= t_neutral`` the curve evaluates to ``t_supply_base``.  As
    ``t_out`` drops below ``t_neutral``, ``t_supply`` increases linearly at the
    given *slope* until it hits ``t_supply_max``.

    Attributes:
        t_supply_base: Supply temperature at the neutral outdoor temperature
            [degC].  Must lie in ``[t_supply_min, t_supply_max]``.
        slope: Curve steepness [K_supply / K_outdoor].  Non-negative.
        t_neutral: Outdoor temperature at which supply equals ``t_supply_base``
            [degC].
        t_supply_max: Upper clamp for supply temperature [degC].
        t_supply_min: Lower clamp for supply temperature [degC].  Must be > 0.
            Defaults to 20.0.
    """

    t_supply_base: float
    slope: float
    t_neutral: float
    t_supply_max: float
    t_supply_min: float = 20.0

    def __post_init__(self) -> None:
        """Validate curve parameters.

        Raises:
            ValueError: If any parameter is out of range.
        """
        if self.slope < 0:
            msg = f"slope must be >= 0, got {self.slope}"
            raise ValueError(msg)
        if self.t_supply_min <= 0:
            msg = f"t_supply_min must be > 0, got {self.t_supply_min}"
            raise ValueError(msg)
        if self.t_supply_max <= self.t_supply_min:
            msg = (
                f"t_supply_max ({self.t_supply_max}) must be > "
                f"t_supply_min ({self.t_supply_min})"
            )
            raise ValueError(msg)
        if self.t_supply_base < self.t_supply_min:
            msg = (
                f"t_supply_base ({self.t_supply_base}) must be >= "
                f"t_supply_min ({self.t_supply_min})"
            )
            raise ValueError(msg)
        if self.t_supply_base > self.t_supply_max:
            msg = (
                f"t_supply_base ({self.t_supply_base}) must be <= "
                f"t_supply_max ({self.t_supply_max})"
            )
            raise ValueError(msg)

    def t_supply(self, t_out: float) -> float:
        """Compute heating supply temperature for the given outdoor temperature.

        Args:
            t_out: Outdoor temperature [degC].

        Returns:
            Supply temperature [degC], clamped to
            ``[t_supply_min, t_supply_max]``.
        """
        raw = self.t_supply_base + self.slope * max(0.0, self.t_neutral - t_out)
        return min(max(raw, self.t_supply_min), self.t_supply_max)

    def to_dict(self) -> dict[str, float]:
        """Serialise to a plain ``dict`` suitable for JSON.

        Returns:
            Dictionary with all fields as ``str -> float`` pairs.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialise from a plain ``dict``.

        Args:
            data: Dictionary previously produced by :meth:`to_dict`.

        Returns:
            A validated :class:`WeatherCompCurve` instance.

        Raises:
            TypeError: If required keys are missing.
            ValueError: If values fail validation.
        """
        return cls(**data)


@dataclass(frozen=True)
class CoolingCompCurve:
    """Weather-compensation curve for **cooling** supply temperature.

    The supply temperature is computed as::

        t_supply = clip(
            t_supply_base + slope * max(0, t_out - t_neutral),
            t_supply_min,
            t_supply_max,
        )

    When ``t_out <= t_neutral`` the curve evaluates to ``t_supply_base``.  As
    ``t_out`` rises above ``t_neutral``, ``t_supply`` increases (warmer
    chilled-water supply) until it hits ``t_supply_max``.

    Note: in cooling mode *lower* supply temperatures provide more cooling.
    The rising curve models heat-pump behaviour where the chilled-water
    setpoint is raised (less aggressive cooling) at higher outdoor temperatures
    to protect against floor condensation and to match compressor limits.

    Attributes:
        t_supply_base: Supply temperature at the neutral outdoor temperature
            [degC].  Must lie in ``[t_supply_min, t_supply_max]``.
        slope: Curve steepness [K_supply / K_outdoor].  Non-negative.
        t_neutral: Outdoor temperature at which supply equals ``t_supply_base``
            [degC].
        t_supply_max: Upper clamp for supply temperature [degC].
        t_supply_min: Lower clamp for supply temperature [degC].  Must be > 0.
            Defaults to 16.0.
    """

    t_supply_base: float
    slope: float
    t_neutral: float
    t_supply_max: float
    t_supply_min: float = 16.0

    def __post_init__(self) -> None:
        """Validate curve parameters.

        Raises:
            ValueError: If any parameter is out of range.
        """
        if self.slope < 0:
            msg = f"slope must be >= 0, got {self.slope}"
            raise ValueError(msg)
        if self.t_supply_min <= 0:
            msg = f"t_supply_min must be > 0, got {self.t_supply_min}"
            raise ValueError(msg)
        if self.t_supply_max <= self.t_supply_min:
            msg = (
                f"t_supply_max ({self.t_supply_max}) must be > "
                f"t_supply_min ({self.t_supply_min})"
            )
            raise ValueError(msg)
        if self.t_supply_base < self.t_supply_min:
            msg = (
                f"t_supply_base ({self.t_supply_base}) must be >= "
                f"t_supply_min ({self.t_supply_min})"
            )
            raise ValueError(msg)
        if self.t_supply_base > self.t_supply_max:
            msg = (
                f"t_supply_base ({self.t_supply_base}) must be <= "
                f"t_supply_max ({self.t_supply_max})"
            )
            raise ValueError(msg)

    def t_supply(self, t_out: float) -> float:
        """Compute cooling supply temperature for the given outdoor temperature.

        Args:
            t_out: Outdoor temperature [degC].

        Returns:
            Supply temperature [degC], clamped to
            ``[t_supply_min, t_supply_max]``.
        """
        raw = self.t_supply_base + self.slope * max(0.0, t_out - self.t_neutral)
        return min(max(raw, self.t_supply_min), self.t_supply_max)

    def to_dict(self) -> dict[str, float]:
        """Serialise to a plain ``dict`` suitable for JSON.

        Returns:
            Dictionary with all fields as ``str -> float`` pairs.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Deserialise from a plain ``dict``.

        Args:
            data: Dictionary previously produced by :meth:`to_dict`.

        Returns:
            A validated :class:`CoolingCompCurve` instance.

        Raises:
            TypeError: If required keys are missing.
            ValueError: If values fail validation.
        """
        return cls(**data)
