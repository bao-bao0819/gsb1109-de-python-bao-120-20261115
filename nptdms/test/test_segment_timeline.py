"""Test segment-aware channel timelines."""

import struct

import numpy as np
import pytest

from nptdms import TdmsFile
from nptdms.segment_timeline import AmbiguousTimeError
from nptdms.test.util import (
    GeneratedFile, channel_metadata, channel_metadata_with_no_data,
    hexlify_value, segment_objects_metadata,
)


TDS_TYPE_INT32 = 3
TDS_TYPE_TIMESTAMP = 0x44


def _time_properties(increment, start_offset=0.0, start_seconds=None, start_fractions=0):
    properties = {
        "wf_increment": (10, hexlify_value("<d", increment)),
        "wf_start_offset": (10, hexlify_value("<d", start_offset)),
    }
    if start_seconds is not None:
        properties["wf_start_time"] = (
            TDS_TYPE_TIMESTAMP,
            hexlify_value("<Q", start_fractions) + hexlify_value("<q", start_seconds))
    return properties


def _channel_segment(channel_name, values, increment, start_offset=0.0,
                     start_seconds=None, start_fractions=0):
    properties = _time_properties(increment, start_offset, start_seconds, start_fractions)
    metadata = channel_metadata(channel_name, TDS_TYPE_INT32, len(values), properties)
    data = struct.pack("<" + "i" * len(values), *values).hex()
    return metadata, data


def _add_time_segment(test_file, channel_name, values, increment, start_offset=0.0,
                      start_seconds=None, start_fractions=0):
    metadata, data = _channel_segment(
        channel_name, values, increment, start_offset, start_seconds, start_fractions)
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(metadata), data)


def _gap_segment(test_file, channel_name="/'Group'/'Channel1'"):
    """ A segment with no data for the channel, representing stopped acquisition """
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(channel_metadata_with_no_data(channel_name)),
        "")


def _load(test_file, **kwargs):
    with test_file.get_tempfile() as temp_file:
        return TdmsFile.read(temp_file.file, **kwargs)


def test_timeline_keeps_each_segments_increment():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2, 3], 0.1)
    _add_time_segment(test_file, "/'Group'/'Channel1'", [4, 5], 0.5)
    _add_time_segment(test_file, "/'Group'/'Channel1'", [6, 7, 8, 9], 0.2)

    channel = _load(test_file)["Group"]["Channel1"]
    timeline = channel.segment_timeline()

    assert [span.start_index for span in timeline.spans] == [0, 3, 5]
    assert [span.sample_count for span in timeline.spans] == [3, 2, 4]
    assert [span.increment for span in timeline.spans] == [0.1, 0.5, 0.2]
    # Without wf_start_time the sample clock continues across segments, so
    # each segment keeps its own increment instead of one file-wide value.
    np.testing.assert_allclose(
        timeline.time_track(),
        [0.0, 0.1, 0.2, 0.3, 0.8, 1.3, 1.5, 1.7, 1.9],
        atol=1e-12)
    # Each sample keeps its exact channel position
    assert [timeline.time_at_sample(i) for i in range(9)] == pytest.approx(
        [0.0, 0.1, 0.2, 0.3, 0.8, 1.3, 1.5, 1.7, 1.9])


def test_timeline_does_not_fill_stopped_acquisition():
    test_file = GeneratedFile()
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [1, 2, 3], 0.1,
        start_seconds=100)
    _gap_segment(test_file)
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [4, 5], 0.1,
        start_seconds=110)

    channel = _load(test_file)["Group"]["Channel1"]
    timeline = channel.segment_timeline()

    # The gap segment contributes no samples and the next starts at 10 seconds
    assert len(timeline.spans) == 2
    assert timeline.sample_count == 5
    assert timeline.time_at_sample(2) == pytest.approx(0.2)
    assert timeline.time_at_sample(3) == pytest.approx(10.0)
    assert timeline.time_at_sample(4) == pytest.approx(10.1)

    # No sample exists in the stopped interval
    assert timeline.samples_at_time(5.0) == []
    with pytest.raises(KeyError):
        timeline.sample_at_time(5.0)


def test_time_track_uses_segment_properties_when_increment_changes():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2], 0.1)
    _add_time_segment(test_file, "/'Group'/'Channel1'", [3, 4], 0.25)

    channel = _load(test_file)["Group"]["Channel1"]
    np.testing.assert_allclose(
        channel.time_track(), [0.0, 0.1, 0.2, 0.45], atol=1e-12)


def test_time_track_keeps_gap_instead_of_compressing_it():
    test_file = GeneratedFile()
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [1, 2], 1.0, start_seconds=100)
    _gap_segment(test_file)
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [3, 4], 1.0, start_seconds=105)

    channel = _load(test_file)["Group"]["Channel1"]
    np.testing.assert_allclose(channel.time_track(), [0.0, 1.0, 5.0, 6.0])


def test_absolute_segment_times_and_reverse_lookup():
    test_file = GeneratedFile()
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [1, 2, 3], 0.1,
        start_seconds=3524551547)
    _gap_segment(test_file)
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [4, 5], 0.2,
        start_seconds=3524551552)

    channel = _load(test_file)["Group"]["Channel1"]
    absolute = channel.segment_times(absolute_time=True, accuracy="ns")
    assert absolute[0] == np.datetime64("2015-09-08T10:05:47.000000000", "ns")
    assert absolute[2] == np.datetime64("2015-09-08T10:05:47.200000000", "ns")
    assert absolute[3] == np.datetime64("2015-09-08T10:05:52.000000000", "ns")
    assert absolute[4] == np.datetime64("2015-09-08T10:05:52.200000000", "ns")

    for index, value in enumerate(absolute):
        assert channel.time_at(index, absolute_time=True, accuracy="ns") == value
        assert channel.sample_index_at(value) == index
    with pytest.raises(KeyError):
        channel.sample_index_at(np.datetime64("2015-09-08T10:05:49", "ns"))


def test_overlapping_segments_keep_both_sources():
    test_file = GeneratedFile()
    # The first segment covers absolute times 100.0 .. 100.2.  The second
    # declares a different start whose first sample is at 100.1, so the two
    # segments genuinely overlap in time.
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [10, 11, 12], 0.1, start_seconds=100)
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [20, 21], 0.1, start_seconds=99,
        start_offset=1.1)

    channel = _load(test_file)["Group"]["Channel1"]
    timeline = channel.segment_timeline()

    # Physical order and channel sample order are unchanged
    assert [span.sample_count for span in timeline.spans] == [3, 2]
    np.testing.assert_array_equal(channel[:], [10, 11, 12, 20, 21])
    np.testing.assert_allclose(
        timeline.time_track(), [0.0, 0.1, 0.2, 0.1, 0.2], atol=1e-12)

    matches = timeline.samples_at_time(0.1)
    assert len(matches) == 2
    assert [m.sample_index for m in matches] == [1, 3]
    assert [m.segment_index for m in matches] == [0, 1]
    assert [m.segment_sample_index for m in matches] == [1, 0]
    with pytest.raises(AmbiguousTimeError):
        timeline.sample_at_time(0.1)
    with pytest.raises(AmbiguousTimeError):
        channel.sample_index_at(0.1)

    # A time with only one source still resolves
    assert timeline.sample_at_time(0.0).sample_index == 0
    assert len(timeline.samples_at_time(0.2)) == 2


def test_overlap_is_not_overwritten_by_later_segment_properties():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2, 3], 0.1)
    # Repeat the channel with a different increment without a time gap
    _add_time_segment(test_file, "/'Group'/'Channel1'", [4, 5], 0.2)

    channel = _load(test_file)["Group"]["Channel1"]
    assert [span.increment for span in channel.segment_timeline().spans] == [0.1, 0.2]


def test_relative_lookup_without_start_time():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2], 0.5)
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [3], 2.0)

    channel = _load(test_file)["Group"]["Channel1"]
    assert channel.time_at(0) == 0.0
    assert channel.time_at(1) == 0.5
    assert channel.time_at(2) == 1.0
    assert channel.sample_index_at(0.5) == 1
    assert channel.sample_index_at(1.0) == 2
    with pytest.raises(IndexError):
        channel.time_at(3)
    with pytest.raises(KeyError):
        channel.sample_index_at(0.25)


def test_segment_without_data_is_not_a_sample_block():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2], 0.1)
    _gap_segment(test_file)
    _gap_segment(test_file)
    _add_time_segment(test_file, "/'Group'/'Channel1'", [3], 0.1)

    channel = _load(test_file)["Group"]["Channel1"]
    timeline = channel.segment_timeline()
    assert len(timeline.spans) == 2
    assert timeline.spans[1].start_index == 2
    assert len(channel) == 3
    assert timeline.spans[1].start_index == len(channel) - 1
    # The physical break is reported, and the run after it is not placed by
    # extending the clock over unknown time.
    assert timeline.spans[1].gap_before
    assert timeline.spans[1].gap_segments == (1, 2)
    assert not timeline.spans[1].clock_continuous
    assert timeline.spans[1].start_is_unknown
    assert timeline.spans[1].to_dict()["start_is_unknown"] is True


def test_metadata_only_segment_does_not_create_clock_continuation():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2], 0.1)
    # A segment that repeats the channel's metadata but has no values
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'Group'/'Channel1'", TDS_TYPE_INT32, 0,
                             _time_properties(0.5))),
        "")
    _add_time_segment(test_file, "/'Group'/'Channel1'", [3], 0.1)

    channel = _load(test_file)["Group"]["Channel1"]
    timeline = channel.segment_timeline()
    assert [span.sample_count for span in timeline.spans] == [2, 1]
    assert timeline.spans[1].gap_segments == (1,)
    assert timeline.spans[1].start_is_unknown


def test_serialized_timeline_keeps_indexes_and_segment_sources():
    test_file = GeneratedFile()
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [1, 2, 3], 0.1, start_seconds=100)
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [4, 5], 0.25, start_seconds=100,
        start_offset=0.3)

    channel = _load(test_file)["Group"]["Channel1"]
    data = channel.segment_timeline().to_dict(absolute_time=True)
    assert data["sample_count"] == 5
    assert data["overlap"] is False
    first, second = data["segments"]
    assert (first["segment_index"], first["start_index"], first["stop_index"]) == (0, 0, 3)
    assert (second["segment_index"], second["start_index"], second["stop_index"]) == (1, 3, 5)
    assert first["start_time"] == "1904-01-01T00:01:40.000000000"
    assert second["start_time"] == "1904-01-01T00:01:40.300000000"
    assert second["increment"] == 0.25


def test_multiple_channels_keep_independent_segment_runs():
    test_file = GeneratedFile()
    properties = _time_properties(0.1)
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'Group'/'A'", TDS_TYPE_INT32, 2, properties),
            channel_metadata("/'Group'/'B'", TDS_TYPE_INT32, 2, properties)),
        struct.pack("<iiii", 1, 2, 10, 11).hex())
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata_with_no_data("/'Group'/'A'"),
            channel_metadata("/'Group'/'B'", TDS_TYPE_INT32, 2, properties)),
        struct.pack("<ii", 20, 21).hex())
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'Group'/'A'", TDS_TYPE_INT32, 2, properties),
            channel_metadata_with_no_data("/'Group'/'B'")),
        struct.pack("<ii", 3, 4).hex())

    tdms_file = _load(test_file)
    timeline_a = tdms_file["Group"]["A"].segment_timeline()
    timeline_b = tdms_file["Group"]["B"].segment_timeline()

    assert [(s.segment_index, s.start_index) for s in timeline_a.spans] == [(0, 0), (2, 2)]
    assert timeline_a.spans[1].gap_before
    assert [(s.segment_index, s.start_index) for s in timeline_b.spans] == [(0, 0), (1, 2)]
    assert not timeline_b.spans[1].gap_before
    assert timeline_b.time_track() == pytest.approx([0.0, 0.1, 0.2, 0.3])


def test_legacy_time_track_is_unchanged_for_single_segment():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2, 3], 0.1)

    channel = _load(test_file)["Group"]["Channel1"]
    np.testing.assert_allclose(channel.time_track(), [0.0, 0.1, 0.2])
    np.testing.assert_allclose(channel.segment_times(), [0.0, 0.1, 0.2])


def test_identical_metadata_segments_continue_the_clock():
    test_file = GeneratedFile()
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [1, 2], 0.1, start_seconds=100)
    _add_time_segment(
        test_file, "/'Group'/'Channel1'", [3, 4], 0.1, start_seconds=100)

    channel = _load(test_file)["Group"]["Channel1"]
    timeline = channel.segment_timeline()
    assert all(span.clock_continuous for span in timeline.spans[1:])
    assert not timeline.has_overlap
    np.testing.assert_allclose(channel.time_track(), [0.0, 0.1, 0.2, 0.3])
    assert [s.start_index for s in timeline.spans] == [0, 2]


def test_single_data_run_ignores_later_metadata_only_properties():
    test_file = GeneratedFile()
    _add_time_segment(test_file, "/'Group'/'Channel1'", [1, 2], 0.1)
    # Later metadata changes the cumulative channel properties but writes no
    # samples, so the existing samples keep the increment in effect when written.
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata_with_no_data(
                "/'Group'/'Channel1'", _time_properties(1.0))),
        "")

    channel = _load(test_file)["Group"]["Channel1"]
    assert channel.properties["wf_increment"] == 1.0
    np.testing.assert_allclose(channel.time_track(), [0.0, 0.1])
