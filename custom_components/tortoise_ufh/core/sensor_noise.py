"""Deterministic Gaussian sensor noise for simulation measurements.

Provides :class:`SensorNoise`, which adds seeded Gaussian noise to a
scalar temperature measurement. Noise corrupts only the measurement
snapshot returned to the controller -- it never affects the physical
simulation state inside ``SimulatedRoom``.

Units:
    Temperatures: degC (Celsius)
    Standard deviation: degC (Celsius)
"""

from __future__ import annotations

import numpy as np


class SensorNoise:
    """Adds deterministic Gaussian noise to temperature measurements.

    Wraps a seeded ``numpy.random.Generator`` (``np.random.default_rng``)
    so that the noise sequence is reproducible across runs sharing the
    same seed. A standard deviation of ``0.0`` makes :meth:`corrupt` a
    no-op, drawing no numbers from the generator.

    Typical usage::

        noise = SensorNoise(std=0.1, seed=42)
        noisy_t_room = noise.corrupt(20.5)

    Attributes:
        std: Configured noise standard deviation [degC].
        seed: Random seed used to construct the generator.
    """

    def __init__(self, std: float, seed: int = 42) -> None:
        """Initialize the sensor noise source.

        Args:
            std: Standard deviation of the zero-mean Gaussian noise
                [degC]. Must be >= 0.0; a value of ``0.0`` disables
                noise (:meth:`corrupt` becomes a no-op).
            seed: Seed for the underlying ``np.random.default_rng``.

        Raises:
            ValueError: If ``std`` is negative.
        """
        if std < 0.0:
            msg = f"std must be >= 0.0, got {std}"
            raise ValueError(msg)
        self._std = float(std)
        self._seed = seed
        self._rng = np.random.default_rng(seed)

    @property
    def std(self) -> float:
        """Standard deviation of the Gaussian noise [degC]."""
        return self._std

    @property
    def seed(self) -> int:
        """Seed used to construct the random generator."""
        return self._seed

    def corrupt(self, value: float) -> float:
        """Add zero-mean Gaussian noise to a single measurement.

        When ``std == 0.0`` the original value is returned unchanged and
        no random number is drawn.

        Args:
            value: Clean temperature measurement [degC].

        Returns:
            Noisy temperature measurement ``value + N(0, std)`` [degC].
        """
        if self._std == 0.0:
            return float(value)
        return float(value + self._rng.normal(0.0, self._std))
