"""Per-timestep simulation recording with slicing and querying.

Provides :class:`SimRecord` (a frozen snapshot of one simulation timestep) and
:class:`SimulationLog` (a list-backed container with room filtering, time-range
queries, and optional pandas ``DataFrame`` export).

The record bundles the exact black-box I/O the controller sees and emits
(:class:`~tortoise_ufh.models.RoomInputs` /
:class:`~tortoise_ufh.models.RoomOutputs`), the driving weather
(:class:`~tortoise_ufh.weather.WeatherPoint`), and the ground-truth slab
temperature (``t_slab``, which is *not* exposed to the controller but is
recorded here for metrics and plots).

Units (simulation convention):
    Temperatures: degC
    Valve position: 0-100 %
    Humidity: % (0-100)
    Time: minutes

This module is pure Python (stdlib only; pandas is an optional lazy import in
:meth:`SimulationLog.to_dataframe`) and MUST NOT import ``homeassistant``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, overload

from tortoise_ufh.models import FastSourceMode, Mode, RoomInputs, RoomOutputs
from tortoise_ufh.weather import WeatherPoint

if TYPE_CHECKING:
    import pandas as pd

# ---------------------------------------------------------------------------
# SimRecord — immutable snapshot of one simulation timestep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimRecord:
    """Immutable record of simulation state at a single timestep.

    Bundles the controller's black-box inputs and outputs, the driving weather,
    and the ground-truth slab temperature for one room at one instant.
    Convenience properties provide flat access to the most-used fields, avoiding
    verbose chains like ``record.inputs.room_temperature_c``.

    Attributes:
        t: Simulation time [minutes], must be >= 0.
        inputs: The room's black-box inputs (what the controller saw).
        outputs: The room's black-box outputs (what the controller emitted).
        weather: Weather conditions at this step.
        t_slab: Ground-truth slab temperature [degC] (hidden from the
            controller; recorded here for metrics and plots).
        room_name: Room identifier (default ``""``).

    Raises:
        ValueError: If ``t`` is negative.
    """

    t: int
    inputs: RoomInputs
    outputs: RoomOutputs
    weather: WeatherPoint
    t_slab: float
    room_name: str = ""

    def __post_init__(self) -> None:
        """Validate the simulation time.

        Raises:
            ValueError: If ``t`` is negative.
        """
        if self.t < 0:
            msg = f"t must be >= 0 minutes, got {self.t}"
            raise ValueError(msg)

    # -- Flat input properties ------------------------------------------------

    @property
    def T_room(self) -> float | None:
        """Measured room air temperature [degC], or ``None`` if sensor lost."""
        return self.inputs.room_temperature_c

    @property
    def setpoint_c(self) -> float:
        """Room target temperature [degC] (home setpoint + room offset)."""
        return self.inputs.setpoint_c

    @property
    def mode(self) -> Mode:
        """The room's operating :class:`~tortoise_ufh.models.Mode`."""
        return self.inputs.mode

    # -- Ground-truth property ------------------------------------------------

    @property
    def T_slab(self) -> float:
        """Ground-truth slab temperature [degC]."""
        return self.t_slab

    # -- Flat output properties -----------------------------------------------

    @property
    def valve_pct(self) -> float:
        """Final commanded valve position [0-100 %]."""
        return self.outputs.valve_position_pct

    @property
    def fast_source_mode(self) -> FastSourceMode:
        """Commanded fast-source direction from the outputs."""
        return self.outputs.fast_source.mode

    @property
    def fast_source_on(self) -> bool:
        """Whether the fast source is commanded on."""
        return self.outputs.fast_source.on

    # -- Flat weather properties ----------------------------------------------

    @property
    def T_out(self) -> float:
        """Outdoor temperature [degC] from weather."""
        return self.weather.T_out

    @property
    def GHI(self) -> float:
        """Global Horizontal Irradiance [W/m^2]."""
        return self.weather.GHI

    @property
    def wind_speed(self) -> float:
        """Wind speed [m/s]."""
        return self.weather.wind_speed

    @property
    def humidity(self) -> float:
        """Relative humidity [%] (0-100)."""
        return self.weather.humidity


# ---------------------------------------------------------------------------
# SimulationLog — list-backed container with querying
# ---------------------------------------------------------------------------


class SimulationLog:
    """Ordered collection of :class:`SimRecord` with filtering and export.

    Supports append, indexed access, slicing (returns a new ``SimulationLog``),
    iteration, room-name filtering, time-range filtering, and optional pandas
    ``DataFrame`` conversion.

    Typical usage::

        log = SimulationLog()
        for t in range(1440):
            inputs = sim.get_all_measurements()["salon"]
            outputs = controller.step({"salon": inputs})["salon"]
            wp = weather.get(float(t))
            log.append_from_step(
                t, inputs, outputs, wp, t_slab=sim.rooms["salon"].T_slab,
                room_name="salon",
            )

        salon = log.get_room("salon").time_range(0, 720)
        df = salon.to_dataframe()
    """

    def __init__(self, records: list[SimRecord] | None = None) -> None:
        """Initialize with an optional list of pre-existing records.

        The provided list is copied to avoid external aliasing.

        Args:
            records: Initial records.  ``None`` creates an empty log.
        """
        self._records: list[SimRecord] = list(records) if records is not None else []

    # -- Mutation -------------------------------------------------------------

    def append(self, record: SimRecord) -> None:
        """Append a single :class:`SimRecord` to the log.

        Args:
            record: The record to add.
        """
        self._records.append(record)

    def append_from_step(
        self,
        t: int,
        inputs: RoomInputs,
        outputs: RoomOutputs,
        weather: WeatherPoint,
        t_slab: float,
        room_name: str = "",
    ) -> None:
        """Construct a :class:`SimRecord` from step components and append it.

        This is a convenience wrapper that avoids constructing a ``SimRecord``
        at the call site.

        Args:
            t: Simulation time [minutes].
            inputs: The room's black-box inputs for this step.
            outputs: The room's black-box outputs for this step.
            weather: Weather conditions at this step.
            t_slab: Ground-truth slab temperature [degC].
            room_name: Room identifier (default ``""``).
        """
        self._records.append(
            SimRecord(
                t=t,
                inputs=inputs,
                outputs=outputs,
                weather=weather,
                t_slab=t_slab,
                room_name=room_name,
            )
        )

    # -- Sized / Iterable / Container -----------------------------------------

    def __len__(self) -> int:
        """Return the number of records in the log."""
        return len(self._records)

    def __iter__(self) -> Iterator[SimRecord]:
        """Iterate over records in chronological order."""
        return iter(self._records)

    @overload
    def __getitem__(self, index: int) -> SimRecord: ...

    @overload
    def __getitem__(self, index: slice) -> SimulationLog: ...

    def __getitem__(self, index: int | slice) -> SimRecord | SimulationLog:
        """Return a single record or a sliced ``SimulationLog``.

        Args:
            index: Integer index or slice.

        Returns:
            A :class:`SimRecord` for an integer index, or a new
            :class:`SimulationLog` for a slice.

        Raises:
            IndexError: If the integer index is out of range.
        """
        if isinstance(index, slice):
            return SimulationLog(self._records[index])
        return self._records[index]

    # -- Query methods --------------------------------------------------------

    def get_room(self, name: str) -> SimulationLog:
        """Return a new log containing only records for *name*.

        Args:
            name: Room identifier to match.

        Returns:
            A new :class:`SimulationLog` with the filtered records.
        """
        return SimulationLog([r for r in self._records if r.room_name == name])

    def time_range(self, start: int, end: int) -> SimulationLog:
        """Return a new log containing records where ``start <= t <= end``.

        Args:
            start: Inclusive lower bound on simulation time [minutes].
            end: Inclusive upper bound on simulation time [minutes].

        Returns:
            A new :class:`SimulationLog` with the filtered records.
        """
        return SimulationLog([r for r in self._records if start <= r.t <= end])

    # -- Export ---------------------------------------------------------------

    def to_dataframe(self) -> pd.DataFrame:
        """Convert the log to a pandas ``DataFrame`` with flattened columns.

        Columns: ``t``, ``room_name``, ``mode``, ``setpoint_c``, ``T_room``,
        ``T_slab``, ``valve_pct``, ``fast_source_on``, ``fast_source_mode``,
        ``T_out``, ``GHI``, ``wind_speed``, ``humidity``.

        Enum fields (``mode``, ``fast_source_mode``) are stored as their string
        ``.value`` representation.  ``T_room`` may be ``None`` (missing sensor).

        Returns:
            A ``pandas.DataFrame`` with one row per record.

        Raises:
            ImportError: If pandas is not installed.
        """
        try:
            import pandas as pd
        except ImportError:
            msg = (
                "pandas is required for to_dataframe(). "
                "Install with: pip install tortoise-ufh[viz]"
            )
            raise ImportError(msg) from None

        rows: list[dict[str, Any]] = [
            {
                "t": r.t,
                "room_name": r.room_name,
                "mode": r.mode.value,
                "setpoint_c": r.setpoint_c,
                "T_room": r.T_room,
                "T_slab": r.T_slab,
                "valve_pct": r.valve_pct,
                "fast_source_on": r.fast_source_on,
                "fast_source_mode": r.fast_source_mode.value,
                "T_out": r.T_out,
                "GHI": r.GHI,
                "wind_speed": r.wind_speed,
                "humidity": r.humidity,
            }
            for r in self._records
        ]
        return pd.DataFrame.from_records(rows)
