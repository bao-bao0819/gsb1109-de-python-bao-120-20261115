"""Tests for reading small ranges of string samples without reading whole chunks"""

import binascii
from io import BytesIO
import struct

import numpy as np
import pytest

from nptdms import TdmsFile, TdmsWriter, ChannelObject
from nptdms import types
from nptdms.test.util import GeneratedFile, segment_objects_metadata


CHANNEL_PATH = "/'group'/'strings'"
TOC = ("kTocMetaData", "kTocRawData", "kTocNewObjList")


def _string_chunk(values):
    encoded = [value.encode('utf-8') for value in values]
    end_offsets = []
    offset = 0
    for value in encoded:
        offset += len(value)
        end_offsets.append(offset)
    return struct.pack('<%dI' % len(values), *end_offsets) + b''.join(encoded)


def _string_metadata(path, number_values, first_chunk, properties=""):
    data_size = len(_string_chunk(first_chunk))
    header = struct.pack('<LlLQQ', 28, types.String.enum_value, 1, number_values, data_size)
    return (
        struct.pack('<I', len(path)).hex() +
        binascii.hexlify(path.encode('utf-8')).decode('ascii') +
        header.hex() + properties + "00000000")


def _chunked_string_file(chunks, values_per_chunk=2):
    test_file = GeneratedFile()
    test_file.add_segment(
        TOC,
        segment_objects_metadata(_string_metadata(CHANNEL_PATH, values_per_chunk, chunks[0])),
        b''.join(_string_chunk(chunk) for chunk in chunks).hex())
    return test_file


def _assert_ranges_match_samples(file, result):
    for sample, (offset, length) in zip(result.samples, result.source_ranges):
        file.seek(offset)
        assert file.read(length).decode('utf-8') == sample


def _corrupt_offset(file, chunk_index, offset_index, value, chunk_size):
    data = bytearray(file.getvalue())
    _, next_segment_offset, raw_data_offset = struct.unpack_from('<lQQ', data, 8)
    data_position = 28 + raw_data_offset
    struct.pack_into(
        '<I', data,
        data_position + chunk_index * chunk_size + 4 * offset_index,
        value)
    return BytesIO(bytes(data))


def test_reads_small_range_of_strings_across_chunks():
    chunks = [["", "abcd"], ["ab", "cd"], ["é", "é"]]
    test_file = _chunked_string_file(chunks)
    bio = test_file.get_bytes_io_file()

    with TdmsFile.open(bio) as tdms_file:
        channel = tdms_file['group']['strings']
        assert list(channel[:]) == sum(chunks, [])

        result = channel.read_string_samples(1, 4)
        assert result.samples == ["abcd", "ab", "cd", "é"]
        assert result.error is None
        assert result.complete
        assert len(result.source_ranges) == 4
        _assert_ranges_match_samples(bio, result)


def test_empty_string_has_empty_source_range():
    test_file = _chunked_string_file([["", "value"]])
    bio = test_file.get_bytes_io_file()

    with TdmsFile.open(bio) as tdms_file:
        channel = tdms_file['group']['strings']
        result = channel.read_string_samples(0, 1)
        assert result.samples == [""]
        assert len(result.source_ranges) == 1
        offset, length = result.source_ranges[0]
        assert length == 0
        bio.seek(offset)
        assert bio.read(1) == b'v'


def test_reads_strings_across_segments():
    output = BytesIO()
    with TdmsWriter(output) as writer:
        writer.write_segment([ChannelObject('group', 'strings', np.array(["a", "b"], dtype=object))])
        writer.write_segment([ChannelObject('group', 'strings', np.array(["c", ""], dtype=object))])
        writer.write_segment([ChannelObject('group', 'strings', np.array(["d"], dtype=object))])

    bio = BytesIO(output.getvalue())
    with TdmsFile.open(bio) as tdms_file:
        channel = tdms_file['group']['strings']
        result = channel.read_string_samples(1, 3)
        assert result.samples == ["b", "c", ""]
        assert result.error is None
        _assert_ranges_match_samples(bio, result)


def test_only_requested_chunk_is_read():
    """Reading one sample should not read the whole file or whole chunks"""
    chunks = [["value%d" % i, "other%d" % i] for i in range(20)]
    test_file = _chunked_string_file(chunks)
    data = test_file.get_bytes_io_file().getvalue()

    class CountingFile(BytesIO):
        def __init__(self, initial):
            super().__init__(initial)
            self.bytes_read = 0

        def read(self, *args):
            result = super().read(*args)
            self.bytes_read += len(result)
            return result

    bio = CountingFile(data)
    with TdmsFile.open(bio) as tdms_file:
        channel = tdms_file['group']['strings']
        bio.bytes_read = 0
        result = channel.read_string_samples(20, 1)
        assert result.samples == ["value10"]
        # Reading one chunk of the offset table and one string is far less than the file
        assert bio.bytes_read < len(data) // 4


def test_bad_offset_stops_at_affected_chunk():
    chunks = [["aa", "bb"], ["cc", "dd"], ["ee", "ff"]]
    test_file = _chunked_string_file(chunks)
    data = _corrupt_offset(
        test_file.get_bytes_io_file(), 1, 0, 0x7FFFFFFF, len(_string_chunk(chunks[0])))

    with TdmsFile.open(data) as tdms_file:
        result = tdms_file['group']['strings'].read_string_samples(0, 6)
        assert result.samples == ["aa", "bb"]
        assert len(result.source_ranges) == 2
        assert result.error is not None
        assert isinstance(result.error, types.StringOffsetError)
        assert result.error.chunk_index == 1
        assert result.error.sample_index == 0
        assert not result.complete

        # Asking only for the affected chunk returns no samples and does not spill into the next chunk
        result = tdms_file['group']['strings'].read_string_samples(2, 2)
        assert result.samples == []
        assert result.error is not None
        assert result.error.chunk_index == 1


def test_bad_second_offset_returns_valid_prefix_of_chunk():
    chunks = [["aa", "bb"], ["cc", "dd"], ["ee", "ff"]]
    test_file = _chunked_string_file(chunks)
    data = _corrupt_offset(
        test_file.get_bytes_io_file(), 1, 1, 0x7FFFFFFF, len(_string_chunk(chunks[0])))

    with TdmsFile.open(data) as tdms_file:
        result = tdms_file['group']['strings'].read_string_samples(0, 6)
        assert result.samples == ["aa", "bb", "cc"]
        assert len(result.source_ranges) == 3
        assert result.error is not None
        assert result.error.chunk_index == 1
        assert result.error.sample_index == 1


def test_read_values_does_not_cross_chunk_boundary():
    """A corrupt offset must not pull bytes from the following chunk into one string"""
    chunk = struct.pack('<II', 3, 0x7FFFFFFF) + b'abc'
    following_chunk = struct.pack('<II', 3, 6) + b'def'
    bio = BytesIO(chunk + following_chunk)

    with pytest.raises(types.StringOffsetError):
        types.String.read_values(bio, 2, data_size=len(chunk))


def test_read_samples_rejects_invalid_arguments():
    test_file = _chunked_string_file([["a", "b"]])
    with TdmsFile.open(test_file.get_bytes_io_file()) as tdms_file:
        channel = tdms_file['group']['strings']
        with pytest.raises(ValueError):
            channel.read_string_samples(-1)
        with pytest.raises(ValueError):
            channel.read_string_samples(0, -1)


def test_read_samples_rejects_non_string_channel():
    output = BytesIO()
    with TdmsWriter(output) as writer:
        writer.write_segment([ChannelObject('group', 'numbers', np.array([1, 2], dtype=np.int32))])

    with TdmsFile.open(BytesIO(output.getvalue())) as tdms_file:
        with pytest.raises(ValueError):
            tdms_file['group']['numbers'].read_string_samples()


def test_read_samples_from_channel_read_into_memory():
    output = BytesIO()
    with TdmsWriter(output) as writer:
        writer.write_segment([ChannelObject('group', 'strings', np.array(["a", "", "c"], dtype=object))])

    tdms_file = TdmsFile.read(BytesIO(output.getvalue()))
    result = tdms_file['group']['strings'].read_string_samples(1, 2)
    assert result.samples == ["", "c"]
    assert result.source_ranges is None
    assert not result.ranges_available
