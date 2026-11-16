"""Per-segment effective metadata, source segments and interleaving changes.

Later TDMS segments often omit object metadata and inherit the raw data index
from an earlier segment, and occasionally change the channel layout or switch
between contiguous and interleaved data. These tests check that the metadata
effectively in place for each segment (and the segment it came from) is
recorded, that explicit updates only apply from the updating segment onward,
and that data is always decoded with the layout in effect for its own segment.
"""

from io import BytesIO

import numpy as np
import pytest

from nptdms import TdmsFile
from nptdms.test.util import (
    GeneratedFile,
    channel_metadata,
    channel_metadata_with_repeated_structure,
    segment_objects_metadata,
    hexlify_value,
)


TDS_TYPE_INT32 = 3
CHANNEL_A = "/'group'/'channel_a'"
CHANNEL_B = "/'group'/'channel_b'"


def int32_data(*values):
    return "".join(hexlify_value("<i", value) for value in values)


def flipped_layout_file():
    """ Five segments exercising inheritance, interleaving flips and a layout change

    seg0: contiguous, channels A and B, chunk size 2
    seg1: no metadata, inherits seg0 (contiguous)
    seg2: explicit repeated-structure metadata, switches to interleaved
    seg3: no metadata, inherits seg2 (interleaved)
    seg4: new object list, only channel A, chunk size 3, contiguous
    """
    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata(CHANNEL_A, TDS_TYPE_INT32, 2),
            channel_metadata(CHANNEL_B, TDS_TYPE_INT32, 2)),
        int32_data(1, 2, 3, 4))
    test_file.add_segment(
        ("kTocRawData",), "", int32_data(5, 6, 7, 8))
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocInterleavedData"),
        segment_objects_metadata(
            channel_metadata_with_repeated_structure(CHANNEL_A),
            channel_metadata_with_repeated_structure(CHANNEL_B)),
        int32_data(9, 10, 11, 12))
    test_file.add_segment(
        ("kTocRawData", "kTocInterleavedData"), "",
        int32_data(13, 14, 15, 16))
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata(CHANNEL_A, TDS_TYPE_INT32, 3)),
        int32_data(17, 18, 19))
    return test_file


def test_segment_metadata_records_effective_layout():
    tdms_file = TdmsFile.read(flipped_layout_file().get_bytes_io_file())
    segments = tdms_file.segment_metadata

    assert len(segments) == 5
    assert [segment.index for segment in segments] == [0, 1, 2, 3, 4]
    assert [segment.interleaved for segment in segments] == [False, False, True, True, False]
    assert all(not segment.big_endian for segment in segments)
    assert all(segment.has_raw_data for segment in segments)

    # The object list is inherited until the explicit new object list in segment 4
    assert [segment.list_source for segment in segments] == [0, 0, 0, 0, 4]

    effective_channels = [
        [obj.path for obj in segment.objects] for segment in segments]
    assert effective_channels == [
        [CHANNEL_A, CHANNEL_B],
        [CHANNEL_A, CHANNEL_B],
        [CHANNEL_A, CHANNEL_B],
        [CHANNEL_A, CHANNEL_B],
        [CHANNEL_A],
    ]


def test_segment_metadata_records_source_segment():
    tdms_file = TdmsFile.read(flipped_layout_file().get_bytes_io_file())
    segments = tdms_file.segment_metadata

    # Channel A's raw data index is defined in seg0 and reused until the explicit
    # update in seg4, even where segments omit metadata or switch interleaving.
    a_sources = [segments[i].object(CHANNEL_A).index_segment for i in range(5)]
    assert a_sources == [0, 0, 0, 0, 4]

    # Channel B keeps the index from seg0 until it disappears from the new list.
    b_sources = [segments[i].object(CHANNEL_B).index_segment for i in range(4)]
    assert b_sources == [0, 0, 0, 0]
    assert segments[4].object(CHANNEL_B) is None

    for segment in segments[:4]:
        obj = segment.object(CHANNEL_A)
        assert obj.number_values == 2
        assert obj.data_type is not None
        assert obj.has_data
    assert segments[4].object(CHANNEL_A).number_values == 3


def test_explicit_update_only_affects_later_segments():
    tdms_file = TdmsFile.read(flipped_layout_file().get_bytes_io_file())
    segments = tdms_file.segment_metadata

    # Earlier segments keep their original chunk size and source.
    for segment in segments[:4]:
        assert segment.object(CHANNEL_A).number_values == 2
        assert segment.object(CHANNEL_A).index_segment == 0
    # The seg4 update changes the layout for seg4 only; earlier snapshots are frozen.
    assert segments[4].object(CHANNEL_A).number_values == 3
    assert segments[4].object(CHANNEL_A).index_segment == 4
    assert segments[4].list_source == 4


def test_channel_level_segment_metadata():
    tdms_file = TdmsFile.read(flipped_layout_file().get_bytes_io_file())
    channel = tdms_file["group"]["channel_a"]

    entries = channel.get_segment_metadata()
    assert [segment_index for (segment_index, _) in entries] == [0, 1, 2, 3, 4]
    assert [obj.index_segment for (_, obj) in entries] == [0, 0, 0, 0, 4]

    b_entries = tdms_file["group"]["channel_b"].get_segment_metadata()
    assert [segment_index for (segment_index, _) in b_entries] == [0, 1, 2, 3]

    # File-level lookup by full path matches the channel-level view.
    file_entries = tdms_file.get_object_segment_metadata(CHANNEL_A)
    assert [index for (index, _) in file_entries] == [index for (index, _) in entries]
    assert [obj.index_segment for (_, obj) in file_entries] == [
        obj.index_segment for (_, obj) in entries]


def test_interleaving_flip_decodes_each_segment_with_its_own_layout():
    data = flipped_layout_file().get_bytes_io_file().getvalue()
    tdms_file = TdmsFile.read(BytesIO(data))

    # Contiguous segments keep blocks together, interleaved segments split rows by channel.
    np.testing.assert_array_equal(
        tdms_file["group"]["channel_a"][:],
        np.array([1, 2, 5, 6, 9, 11, 13, 15, 17, 18, 19], dtype=np.int32))
    np.testing.assert_array_equal(
        tdms_file["group"]["channel_b"][:],
        np.array([3, 4, 7, 8, 10, 12, 14, 16], dtype=np.int32))


def test_interleaving_flip_streaming_and_slicing():
    data = flipped_layout_file().get_bytes_io_file().getvalue()
    expected_a = np.array([1, 2, 5, 6, 9, 11, 13, 15, 17, 18, 19], dtype=np.int32)
    with TdmsFile.open(BytesIO(data)) as tdms_file:
        channel = tdms_file["group"]["channel_a"]
        np.testing.assert_array_equal(channel.read_data(), expected_a)
        # Point and slice reads cross every segment and layout boundary.
        for index, expected in enumerate(expected_a):
            assert channel[index] == expected
        for start in range(len(expected_a) + 1):
            for stop in range(start, len(expected_a) + 1):
                np.testing.assert_array_equal(
                    channel[start:stop], expected_a[start:stop])

        # The streaming data chunk interface reassembles values in the same order.
        streamed = []
        for chunk in tdms_file.data_chunks():
            streamed.extend(chunk["group"]["channel_a"][:])
        np.testing.assert_array_equal(np.array(streamed, dtype=np.int32), expected_a)


def test_reused_index_from_non_adjacent_segment():
    """ A channel that drops out and returns reuses its original raw data index
    """
    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata(CHANNEL_A, TDS_TYPE_INT32, 2),
            channel_metadata(CHANNEL_B, TDS_TYPE_INT32, 2)),
        int32_data(1, 2, 3, 4))
    # New object list drops channel A.
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata(CHANNEL_B, TDS_TYPE_INT32, 2)),
        int32_data(5, 6))
    # Incremental segment re-adds A reusing its structure from the earlier segment.
    # A is appended after the existing list [B], so contiguous data is B then A.
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData"),
        segment_objects_metadata(
            channel_metadata_with_repeated_structure(CHANNEL_A)),
        int32_data(7, 8, 9, 10))
    test_file.add_segment(
        ("kTocRawData",), "", int32_data(11, 12, 13, 14))

    tdms_file = TdmsFile.read(test_file.get_bytes_io_file())
    segments = tdms_file.segment_metadata

    # A is absent in seg1, then returns with its raw data index still sourced from seg0.
    assert segments[1].object(CHANNEL_A) is None
    assert segments[2].object(CHANNEL_A).index_segment == 0
    assert segments[3].object(CHANNEL_A).index_segment == 0

    np.testing.assert_array_equal(
        tdms_file["group"]["channel_a"][:],
        np.array([1, 2, 9, 10, 13, 14], dtype=np.int32))
    np.testing.assert_array_equal(
        tdms_file["group"]["channel_b"][:],
        np.array([3, 4, 5, 6, 7, 8, 11, 12], dtype=np.int32))


def test_no_metadata_segment_inherits_interleaving_source():
    """ An interleaved segment followed by a metadata-less one keeps the source layout
    """
    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList", "kTocInterleavedData"),
        segment_objects_metadata(
            channel_metadata(CHANNEL_A, TDS_TYPE_INT32, 2),
            channel_metadata(CHANNEL_B, TDS_TYPE_INT32, 2)),
        int32_data(1, 2, 3, 4))
    test_file.add_segment(
        ("kTocRawData", "kTocInterleavedData"), "",
        int32_data(5, 6, 7, 8))

    tdms_file = TdmsFile.read(test_file.get_bytes_io_file())
    segments = tdms_file.segment_metadata
    assert all(segment.interleaved for segment in segments)
    assert segments[1].object(CHANNEL_A).index_segment == 0
    np.testing.assert_array_equal(
        tdms_file["group"]["channel_a"][:], np.array([1, 3, 5, 7], dtype=np.int32))
    np.testing.assert_array_equal(
        tdms_file["group"]["channel_b"][:], np.array([2, 4, 6, 8], dtype=np.int32))


@pytest.mark.parametrize("read_metadata_only", [False, True])
def test_metadata_available_without_reading_data(read_metadata_only):
    test_file = flipped_layout_file()
    if read_metadata_only:
        tdms_file = TdmsFile.read_metadata(test_file.get_bytes_io_file())
    else:
        tdms_file = TdmsFile.read(test_file.get_bytes_io_file())

    segments = tdms_file.segment_metadata
    assert len(segments) == 5
    assert segments[2].interleaved and not segments[1].interleaved
    assert tdms_file["group"]["channel_a"].get_segment_metadata()[2][1].index_segment == 0
