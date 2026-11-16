""" Test exporting timestamps to integer-only columnar representations
"""

import numpy as np
import pytest

from nptdms.export.columnar_export import (
    columnar_to_timestamps,
    timestamp_sort_indices,
    timestamps_to_columnar,
)
from nptdms.timestamp import TdmsTimestamp, TimestampArray

FRACTIONS_PER_SECOND = 1 << 64


def test_columnar_export_exact_round_trip():
    """ Exported integer columns reconstruct the exact timestamps
    """
    timestamps = [
        TdmsTimestamp(0, 0),
        TdmsTimestamp(3524551547, 1234567890 * 10 ** 10),
        TdmsTimestamp(-1, FRACTIONS_PER_SECOND - 1),
        TdmsTimestamp(-2, 1),
    ]

    columns = timestamps_to_columnar(timestamps)

    assert columns['epoch_seconds'].dtype == np.dtype('int64')
    assert columns['second_fractions'].dtype == np.dtype('uint64')
    restored = columnar_to_timestamps(
        columns['epoch_seconds'], columns['second_fractions'], columns['is_valid'])
    assert restored == timestamps


def test_columnar_export_from_timestamp_array():
    """ A TimestampArray can be exported directly
    """
    dtype = np.dtype([('seconds', '<i8'), ('second_fractions', '<u8')])
    array = TimestampArray(np.array([(3672033330, 0), (3672033331, 2 ** 63)], dtype=dtype))

    columns = timestamps_to_columnar(array)

    np.testing.assert_array_equal(columns['epoch_seconds'], [3672033330, 3672033331])
    np.testing.assert_array_equal(columns['second_fractions'], [0, 2 ** 63])
    np.testing.assert_array_equal(columns['is_valid'], [True, True])
    assert columns['display_time'][0] == '2020-05-11T09:15:30.000000'
    assert columns['display_time'][1] == '2020-05-11T09:15:31.500000'


def test_display_time_notes_rounding():
    """ Display time is rounded and each rounded value is flagged
    """
    timestamps = [
        # Not representable at microsecond resolution, rounds up
        TdmsTimestamp(3672033330, 1234567890 * 10 ** 10),
        # Exactly half a second, representable at microsecond resolution
        TdmsTimestamp(3672033330, 2 ** 63),
    ]

    columns = timestamps_to_columnar(timestamps)

    assert columns['display_time'][0] == '2020-05-11T09:15:30.669261'
    assert columns['display_rounded'][0]
    assert columns['display_time'][1] == '2020-05-11T09:15:30.500000'
    assert not columns['display_rounded'][1]


def test_display_time_rounding_carries_over_second():
    """ A fraction that rounds up to a whole second carries into the seconds
    """
    columns = timestamps_to_columnar([TdmsTimestamp(3672033330, FRACTIONS_PER_SECOND - 1)])

    assert columns['display_time'][0] == '2020-05-11T09:15:31.000000'
    assert columns['display_rounded'][0]


def test_display_time_carry_from_negative_seconds_to_epoch():
    """ Carry works across the epoch when seconds are negative
    """
    columns = timestamps_to_columnar([TdmsTimestamp(-1, FRACTIONS_PER_SECOND - 1)])

    assert columns['display_time'][0] == '1904-01-01T00:00:00.000000'
    assert columns['display_rounded'][0]
    # The exact value is still before the epoch in the integer columns
    assert columns['epoch_seconds'][0] == -1
    assert columns['second_fractions'][0] == FRACTIONS_PER_SECOND - 1


def test_display_time_with_negative_epoch_seconds():
    """ Fractions are a positive offset from negative seconds
    """
    timestamps = [
        TdmsTimestamp(-1, 0),
        TdmsTimestamp(-1, 2 ** 63),
    ]

    columns = timestamps_to_columnar(timestamps)

    assert columns['display_time'][0] == '1903-12-31T23:59:59.000000'
    assert columns['display_time'][1] == '1903-12-31T23:59:59.500000'


def test_display_time_at_second_resolution():
    """ At second resolution a half second fraction rounds up to the next second
    """
    columns = timestamps_to_columnar([TdmsTimestamp(5, 2 ** 63)], display_resolution='s')

    assert columns['display_time'][0] == '1904-01-01T00:00:06'
    assert columns['display_rounded'][0]


def test_display_time_at_nanosecond_resolution():
    columns = timestamps_to_columnar(
        [TdmsTimestamp(3524551547, 1234567890 * 10 ** 10)], display_resolution='ns')

    assert columns['display_time'][0] == '2015-09-08T10:05:47.669260594'
    assert columns['display_rounded'][0]


def test_nulls_do_not_masquerade_as_epoch_zero():
    """ Null timestamps are masked out and never look like the epoch zero timestamp
    """
    columns = timestamps_to_columnar([None, TdmsTimestamp(0, 0)])

    np.testing.assert_array_equal(columns['is_valid'], [False, True])
    assert columns['display_time'][0] is None
    assert columns['display_time'][1] == '1904-01-01T00:00:00.000000'
    assert not columns['display_rounded'][0]

    restored = columnar_to_timestamps(
        columns['epoch_seconds'], columns['second_fractions'], columns['is_valid'])
    assert restored[0] is None
    assert restored[1] == TdmsTimestamp(0, 0)


def test_sort_indices_orders_by_integer_fields():
    """ Sorting compares seconds then fractions, including negative seconds
    """
    seconds = np.array([0, -1, -1, 5], dtype=np.int64)
    fractions = np.array([0, 0, 2 ** 63, 3], dtype=np.uint64)

    order = timestamp_sort_indices(seconds, fractions)

    np.testing.assert_array_equal(order, [1, 2, 0, 3])


def test_sort_indices_distinguishes_fractions_beyond_float_precision():
    """ Fractions that are equal in float64 still sort correctly
    """
    seconds = np.array([7, 7], dtype=np.int64)
    fractions = np.array([FRACTIONS_PER_SECOND - 1, FRACTIONS_PER_SECOND - 2], dtype=np.uint64)

    order = timestamp_sort_indices(seconds, fractions)

    np.testing.assert_array_equal(order, [1, 0])


def test_sort_indices_puts_nulls_last():
    """ Null rows sort last and are not treated as epoch zero
    """
    seconds = np.array([0, 0, -5], dtype=np.int64)
    fractions = np.array([0, 0, 0], dtype=np.uint64)
    is_valid = np.array([True, False, True])

    order = timestamp_sort_indices(seconds, fractions, is_valid)

    np.testing.assert_array_equal(order, [2, 0, 1])


def test_timestamps_order_by_integer_fields():
    """ TdmsTimestamp objects order by seconds then fractions without floats
    """
    assert TdmsTimestamp(-1, 0) < TdmsTimestamp(-1, 2 ** 63)
    assert TdmsTimestamp(-1, 2 ** 63) < TdmsTimestamp(0, 0)
    assert TdmsTimestamp(0, 0) < TdmsTimestamp(0, 1)

    unordered = [
        TdmsTimestamp(0, 0),
        TdmsTimestamp(-1, 2 ** 63),
        TdmsTimestamp(-1, 0),
    ]
    assert sorted(unordered) == [
        TdmsTimestamp(-1, 0),
        TdmsTimestamp(-1, 2 ** 63),
        TdmsTimestamp(0, 0),
    ]


def test_error_raised_for_invalid_display_resolution():
    with pytest.raises(ValueError) as exc_info:
        timestamps_to_columnar([TdmsTimestamp(0, 0)], display_resolution='invalid_res')
    assert str(exc_info.value) == "Unsupported resolution for display time: 'invalid_res'"
