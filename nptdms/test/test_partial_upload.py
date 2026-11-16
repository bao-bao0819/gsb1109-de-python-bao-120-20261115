"""Partial uploads: per-channel declared/read counts, gaps and no padding.

A collection station may only upload part of the last TDMS segment. These tests
check that the number of verifiable samples and the gap are reported per channel,
that complete earlier segments are still ingested, that interleaved data never
pads a missing channel, and that resuming an upload does not duplicate the
samples already confirmed.
"""

import os
import struct
import tempfile

import numpy as np
import pytest

from nptdms import TdmsFile
from nptdms.test.util import (
    GeneratedFile, channel_metadata, segment_objects_metadata,
)


def _write_then_read(contents):
    fd, path = tempfile.mkstemp(suffix=".tdms")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(contents)
        return TdmsFile.read(path)
    finally:
        os.unlink(path)


def _counts(tdms_file):
    return {
        path: (count.declared_length, count.read_length, count.missing_length)
        for path, count in tdms_file.file_status.channel_counts.items()
    }


def test_contiguous_truncation_reports_per_channel_gap_without_padding():
    """A contiguous segment cut within a channel leaves later channels at zero."""
    test_file = GeneratedFile()
    # Each channel declares four int32 values: 16 bytes each, 32 bytes in total.
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", 3, 4),
            channel_metadata("/'group'/'channel2'", 3, 4),
        ),
        struct.pack("<8i", 1, 2, 3, 4, 5, 6, 7, 8),
        binary_data=True,
        # Only the first 12 bytes (three values of channel1) are uploaded.
        data_size_override=32,
    )
    contents = test_file._get_contents()[:-20]

    tdms_file = _write_then_read(contents)

    channel1 = tdms_file["group"]["channel1"]
    channel2 = tdms_file["group"]["channel2"]
    # Real values only: channel1 has three samples, channel2 has none and is not padded.
    np.testing.assert_array_equal(channel1[:], np.array([1, 2, 3], dtype=np.int32))
    np.testing.assert_array_equal(channel2[:], np.array([], dtype=np.int32))

    assert (channel1.declared_length, channel1.read_length, channel1.missing_length) == (4, 3, 1)
    assert (channel2.declared_length, channel2.read_length, channel2.missing_length) == (4, 0, 4)
    assert _counts(tdms_file) == {
        "/'group'/'channel1'": (4, 3, 1),
        "/'group'/'channel2'": (4, 0, 4),
    }


@pytest.mark.parametrize("data_bytes,complete_rows", [(4, 0), (12, 1), (20, 2), (28, 3)])
def test_interleaved_partial_row_is_dropped_for_every_channel(data_bytes, complete_rows):
    """A partial interleaved row is never split, so no channel gets a padded value."""
    test_file = GeneratedFile()
    rows = []
    for row in range(4):
        rows.append(row * 10 + 1)
        rows.append(row * 10 + 2)
    full_data = struct.pack("<8i", *rows)
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList", "kTocInterleavedData"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", 3, 4),
            channel_metadata("/'group'/'channel2'", 3, 4),
        ),
        full_data,
        binary_data=True,
        data_size_override=len(full_data),
    )
    contents = test_file._get_contents()[:len(test_file) - len(full_data) + data_bytes]

    tdms_file = _write_then_read(contents)

    for name in ["channel1", "channel2"]:
        channel = tdms_file["group"][name]
        assert channel.read_length == complete_rows
        assert channel.declared_length == 4
        assert channel.missing_length == 4 - complete_rows
        assert len(channel[:]) == complete_rows

    assert _counts(tdms_file) == {
        "/'group'/'channel1'": (4, complete_rows, 4 - complete_rows),
        "/'group'/'channel2'": (4, complete_rows, 4 - complete_rows),
    }


def test_mixed_width_interleaved_truncation_keeps_only_full_rows():
    """Channels of different widths still share the same complete-row boundary."""
    test_file = GeneratedFile()
    rows = []
    for row in range(4):
        rows.append(struct.pack("<i", row + 1))
        rows.append(struct.pack("<q", (row + 1) * 100))
    full_data = b"".join(rows)  # 12 bytes per row, 48 bytes total
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList", "kTocInterleavedData"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", 3, 4),  # int32
            channel_metadata("/'group'/'channel2'", 4, 4),  # int64
        ),
        full_data,
        binary_data=True,
        data_size_override=len(full_data),
    )
    # 1.5 rows present: only the first complete row is kept.
    contents = test_file._get_contents()[:len(test_file) - len(full_data) + 18]

    tdms_file = _write_then_read(contents)

    np.testing.assert_array_equal(
        tdms_file["group"]["channel1"][:], np.array([1], dtype=np.int32))
    np.testing.assert_array_equal(
        tdms_file["group"]["channel2"][:], np.array([100], dtype=np.int64))
    assert _counts(tdms_file) == {
        "/'group'/'channel1'": (4, 1, 3),
        "/'group'/'channel2'": (4, 1, 3),
    }


def test_complete_segments_are_ingested_before_truncated_segment():
    """Earlier complete segments remain fully available alongside a partial last one."""
    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", 3, 2),
            channel_metadata("/'group'/'channel2'", 3, 2),
        ),
        struct.pack("<4i", 1, 2, 3, 4),
        binary_data=True,
    )
    # Second segment reuses the previous structure but uploads only one value of channel1.
    test_file.add_segment(
        ("kTocRawData",),
        "",
        struct.pack("<4i", 5, 6, 7, 8),
        binary_data=True,
        data_size_override=16,
    )
    # Drop the last 12 of the 16 data bytes, leaving one complete int32 value (5)
    # while keeping the segment lead-in intact.
    contents = test_file._get_contents()[:-12]

    tdms_file = _write_then_read(contents)

    channel1 = tdms_file["group"]["channel1"]
    channel2 = tdms_file["group"]["channel2"]
    np.testing.assert_array_equal(channel1[:], np.array([1, 2, 5], dtype=np.int32))
    np.testing.assert_array_equal(channel2[:], np.array([3, 4], dtype=np.int32))
    assert _counts(tdms_file) == {
        "/'group'/'channel1'": (4, 3, 1),
        "/'group'/'channel2'": (4, 2, 2),
    }


def test_zero_byte_final_segment_marks_every_value_missing():
    """Metadata without any data bytes must not be reported as fully read."""
    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", 3, 4),
            channel_metadata("/'group'/'channel2'", 3, 4),
        ),
        b"",
        binary_data=True,
        data_size_override=32,
    )

    tdms_file = _write_then_read(test_file._get_contents())

    assert tdms_file.file_status.incomplete_final_segment
    assert _counts(tdms_file) == {
        "/'group'/'channel1'": (4, 0, 4),
        "/'group'/'channel2'": (4, 0, 4),
    }
    segment_statuses = tdms_file.file_status.channel_statuses
    assert segment_statuses["/'group'/'channel1'"].read_length == 0
    assert segment_statuses["/'group'/'channel2'"].read_length == 0


def test_resuming_upload_does_not_duplicate_confirmed_samples():
    """Confirmed samples stay fixed when the missing bytes arrive later."""
    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", 3, 2),
            channel_metadata("/'group'/'channel2'", 3, 2),
        ),
        struct.pack("<4i", 1, 2, 3, 4),
        binary_data=True,
    )
    full_second_data = struct.pack("<4i", 5, 6, 7, 8)
    test_file.add_segment(
        ("kTocRawData",),
        "",
        full_second_data,
        binary_data=True,
        data_size_override=len(full_second_data),
    )
    full_contents = test_file._get_contents()
    # First upload stops after the first value (5) of the final segment.
    partial_contents = full_contents[:len(full_contents) - len(full_second_data) + 4]

    partial = _write_then_read(partial_contents)
    resumed = _write_then_read(full_contents)

    partial_c1 = partial["group"]["channel1"]
    resumed_c1 = resumed["group"]["channel1"]
    # The already confirmed prefix is identical and appears exactly once.
    confirmed = np.array([1, 2], dtype=np.int32)
    np.testing.assert_array_equal(partial_c1[:len(confirmed)], confirmed)
    np.testing.assert_array_equal(resumed_c1[:len(confirmed)], confirmed)
    assert len(resumed_c1) == 4
    np.testing.assert_array_equal(resumed_c1[:], np.array([1, 2, 5, 6], dtype=np.int32))
    # Only the previously missing samples are added; nothing is imported twice.
    assert resumed_c1.read_length - partial_c1.read_length == partial_c1.missing_length
    assert resumed["group"]["channel1"].missing_length == 0
