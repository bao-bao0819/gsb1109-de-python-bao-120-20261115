""" Export TDMS timestamps to integer-only columns for columnar stores

    TDMS timestamps carry sub-second precision of 2**-64 seconds, which does not
    fit any integer or floating point column type directly. This module exports
    timestamps as a set of integer columns that a columnar warehouse can store
    losslessly:

    - ``epoch_seconds``: Signed 64 bit seconds since the TDMS epoch
      (1904-01-01 00:00:00 UTC). Negative values are before the epoch.
    - ``second_fractions``: Unsigned 64 bit number of 2**-64 second fractions.
      This is always a positive offset from ``epoch_seconds``, so the exact
      timestamp is ``epoch_seconds + second_fractions * 2**-64`` seconds after
      the epoch.
    - ``is_valid``: Boolean validity mask. A missing (null) timestamp has
      ``is_valid == False`` and must not be confused with a real timestamp at
      the epoch. The integer values in null rows are undefined; consumers must
      use ``is_valid`` to tell a null apart from a genuine epoch zero value.
    - ``display_time``: Human readable ISO 8601 strings, computed separately
      from the exact columns. Values are rounded half-up to the display
      resolution, so this column is for presentation only and must not be used
      to reconstruct or order timestamps. Null timestamps are ``None``.
    - ``display_rounded``: Boolean column noting, per value, whether the
      display string was rounded, i.e. the exact fraction is not representable
      at the display resolution.

    Use :func:`columnar_to_timestamps` to exactly reconstruct
    :class:`~nptdms.timestamp.TdmsTimestamp` objects from the exported columns,
    and :func:`timestamp_sort_indices` to order rows by time using integer
    comparisons only, without converting to floating point.
"""

from collections import OrderedDict
from datetime import datetime, timedelta
import numpy as np

from nptdms.timestamp import TdmsTimestamp, TimestampArray

_TDMS_EPOCH_DATETIME = datetime(1904, 1, 1, 0, 0, 0)

_FRACTIONS_PER_SECOND = 1 << 64

_UNITS_PER_SECOND = {
    's': 1,
    'ms': 10 ** 3,
    'us': 10 ** 6,
    'ns': 10 ** 9,
}

_FRACTION_DIGITS = {
    's': 0,
    'ms': 3,
    'us': 6,
    'ns': 9,
}


def timestamps_to_columnar(timestamps, display_resolution='us'):
    """ Export TDMS timestamps to integer-only columns for a columnar store

        The ``epoch_seconds`` and ``second_fractions`` columns exactly preserve
        the TDMS timestamp and can be converted back with
        :func:`columnar_to_timestamps`. The ``display_time`` column is rounded
        half-up to ``display_resolution``; values where this rounding changed
        the displayed value are flagged in the ``display_rounded`` column.

        :param timestamps: A TimestampArray, or a sequence of TdmsTimestamp
            objects. ``None`` entries are exported as nulls (``is_valid`` is
            False and ``display_time`` is None) and never masquerade as the
            epoch zero timestamp.
        :param display_resolution: Resolution of the display time column.
            Must be one of 's', 'ms', 'us' or 'ns'.
        :return: An OrderedDict with columns ``epoch_seconds`` (int64),
            ``second_fractions`` (uint64), ``is_valid`` (bool),
            ``display_time`` (object) and ``display_rounded`` (bool).
    """
    if display_resolution not in _UNITS_PER_SECOND:
        raise ValueError(
            "Unsupported resolution for display time: '{0}'".format(display_resolution))

    seconds, fractions, valid = _as_integer_columns(timestamps)

    display_time = np.empty(len(seconds), dtype=object)
    display_rounded = np.zeros(len(seconds), dtype=bool)
    for i in range(len(seconds)):
        if not valid[i]:
            display_time[i] = None
            continue
        display_time[i], display_rounded[i] = _format_display_time(
            int(seconds[i]), int(fractions[i]), display_resolution)

    columns = OrderedDict()
    columns['epoch_seconds'] = seconds
    columns['second_fractions'] = fractions
    columns['is_valid'] = valid
    columns['display_time'] = display_time
    columns['display_rounded'] = display_rounded
    return columns


def columnar_to_timestamps(epoch_seconds, second_fractions, is_valid=None):
    """ Reconstruct TDMS timestamps from exported integer columns

        This is the exact inverse of :func:`timestamps_to_columnar` for the
        ``epoch_seconds`` and ``second_fractions`` columns.

        :param epoch_seconds: Array of signed seconds since the TDMS epoch.
        :param second_fractions: Array of unsigned 2**-64 second fractions.
        :param is_valid: Optional boolean validity mask. Rows where this is
            False are nulls and are returned as ``None``.
        :return: A list of TdmsTimestamp objects, with ``None`` for null rows.
    """
    seconds = np.asarray(epoch_seconds)
    fractions = np.asarray(second_fractions)
    if is_valid is None:
        valid = np.ones(len(seconds), dtype=bool)
    else:
        valid = np.asarray(is_valid, dtype=bool)

    timestamps = []
    for row_seconds, row_fractions, row_valid in zip(seconds, fractions, valid):
        if row_valid:
            timestamps.append(TdmsTimestamp(int(row_seconds), int(row_fractions)))
        else:
            timestamps.append(None)
    return timestamps


def timestamp_sort_indices(epoch_seconds, second_fractions, is_valid=None):
    """ Get indices that order rows by time using integer comparisons only

        Sorts by ``epoch_seconds`` then ``second_fractions``, so ordering is
        exact and does not depend on converting timestamps to floating point,
        which would lose sub-microsecond precision. Null rows (where
        ``is_valid`` is False) sort last regardless of their integer values.

        :param epoch_seconds: Array of signed seconds since the TDMS epoch.
        :param second_fractions: Array of unsigned 2**-64 second fractions.
        :param is_valid: Optional boolean validity mask.
        :return: An array of indices that sorts the rows by time.
    """
    seconds = np.asarray(epoch_seconds, dtype=np.int64)
    fractions = np.asarray(second_fractions, dtype=np.uint64)
    if is_valid is None:
        invalid = np.zeros(len(seconds), dtype=np.int8)
    else:
        invalid = (~np.asarray(is_valid, dtype=bool)).astype(np.int8)
    # np.lexsort treats the last key as the primary sort key
    return np.lexsort((fractions, seconds, invalid))


def _as_integer_columns(timestamps):
    """ Normalise input to (epoch_seconds, second_fractions, is_valid) arrays
    """
    if isinstance(timestamps, TimestampArray):
        seconds = np.ascontiguousarray(timestamps.seconds, dtype=np.int64)
        fractions = np.ascontiguousarray(timestamps.second_fractions, dtype=np.uint64)
        valid = np.ones(len(seconds), dtype=bool)
        return seconds, fractions, valid

    seconds_list = []
    fractions_list = []
    valid_list = []
    for timestamp in timestamps:
        if timestamp is None:
            seconds_list.append(0)
            fractions_list.append(0)
            valid_list.append(False)
        else:
            seconds_list.append(int(timestamp.seconds))
            fractions_list.append(int(timestamp.second_fractions))
            valid_list.append(True)
    return (
        np.array(seconds_list, dtype=np.int64),
        np.array(fractions_list, dtype=np.uint64),
        np.array(valid_list, dtype=bool))


def _format_display_time(seconds, fractions, resolution):
    """ Build a display string for a timestamp, rounded half-up to the resolution

        Returns the display string and whether rounding changed the value.
        Rounding is done with exact integer arithmetic and carries over a
        second boundary when the fraction rounds up to a whole second.
    """
    units_per_second = _UNITS_PER_SECOND[resolution]
    digits = _FRACTION_DIGITS[resolution]

    scaled = fractions * units_per_second
    rounded_units = (scaled + _FRACTIONS_PER_SECOND // 2) // _FRACTIONS_PER_SECOND
    was_rounded = (scaled % _FRACTIONS_PER_SECOND) != 0
    if rounded_units >= units_per_second:
        # The fraction rounded up to a whole second, carry into the seconds
        seconds += 1
        rounded_units -= units_per_second

    dt = _TDMS_EPOCH_DATETIME + timedelta(seconds=seconds)
    display = dt.strftime('%Y-%m-%dT%H:%M:%S')
    if digits:
        display += '.{0:0{1}d}'.format(rounded_units, digits)
    return display, was_rounded
