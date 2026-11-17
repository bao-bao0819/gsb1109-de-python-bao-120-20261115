"""Conversions to and from bytes representation of values in TDMS files"""

import numpy as np
import struct
from nptdms.timestamp import TdmsTimestamp, TimestampArray
from nptdms.log import log_manager


log = log_manager.get_logger(__name__)


__all__ = [
    'numpy_data_types',
    'tds_data_types',
    'TdmsType',
    'Bytes',
    'Void',
    'Int8',
    'Int16',
    'Int32',
    'Int64',
    'Uint8',
    'Uint16',
    'Uint32',
    'Uint64',
    'SingleFloat',
    'DoubleFloat',
    'ExtendedFloat',
    'SingleFloatWithUnit',
    'DoubleFloatWithUnit',
    'ExtendedFloatWithUnit',
    'String',
    'Boolean',
    'TimeStamp',
    'ComplexSingleFloat',
    'ComplexDoubleFloat',
    'DaqMxRawData',
]


_struct_pack = struct.pack
_struct_unpack = struct.unpack


tds_data_types = {}
numpy_data_types = {}


def tds_data_type(enum_value, np_type, set_np_type=True):
    def decorator(cls):
        cls.enum_value = enum_value
        cls.nptype = None if np_type is None else np.dtype(np_type)
        if enum_value is not None:
            tds_data_types[enum_value] = cls
        if set_np_type and np_type is not None:
            numpy_data_types[np.dtype(np_type)] = cls
        return cls
    return decorator


class TdmsType(object):
    size = None

    def __init__(self):
        self.value = None
        self.bytes = None

    def __eq__(self, other):
        return self.bytes == other.bytes and self.value == other.value

    def __repr__(self):
        if self.value is None:
            return "%s" % self.__class__.__name__
        return "%s(%r)" % (self.__class__.__name__, self.value)

    @classmethod
    def read(cls, file, endianness="<"):
        raise NotImplementedError("Unsupported data type to read: %r" % cls)

    @classmethod
    def read_values(cls, file, number_values, endianness="<"):
        raise NotImplementedError("Unsupported data type to read: %r" % cls)


class Bytes(TdmsType):
    def __init__(self, value):
        self.value = value
        self.bytes = value


class StructType(TdmsType):
    struct_declaration = None
    nptype = None

    def __init__(self, value):
        self.value = value
        self.bytes = _struct_pack('<' + self.struct_declaration, value)

    @classmethod
    def read(cls, file, endianness="<"):
        read_bytes = file.read(cls.size)
        return _struct_unpack(endianness + cls.struct_declaration, read_bytes)[0]

    @classmethod
    def from_bytes(cls, byte_array, endianness="<"):
        """ Convert an array of bytes into a numpy array of data
        """
        return byte_array.view(cls.nptype.newbyteorder(endianness))


@tds_data_type(0, None)
class Void(TdmsType):
    pass


@tds_data_type(1, np.int8)
class Int8(StructType):
    size = 1
    struct_declaration = "b"


@tds_data_type(2, np.int16)
class Int16(StructType):
    size = 2
    struct_declaration = "h"


@tds_data_type(3, np.int32)
class Int32(StructType):
    size = 4
    struct_declaration = "l"


@tds_data_type(4, np.int64)
class Int64(StructType):
    size = 8
    struct_declaration = "q"


@tds_data_type(5, np.uint8)
class Uint8(StructType):
    size = 1
    struct_declaration = "B"


@tds_data_type(6, np.uint16)
class Uint16(StructType):
    size = 2
    struct_declaration = "H"


@tds_data_type(7, np.uint32)
class Uint32(StructType):
    size = 4
    struct_declaration = "L"


@tds_data_type(8, np.uint64)
class Uint64(StructType):
    size = 8
    struct_declaration = "Q"


@tds_data_type(9, np.single)
class SingleFloat(StructType):
    size = 4
    struct_declaration = "f"


@tds_data_type(10, np.double)
class DoubleFloat(StructType):
    size = 8
    struct_declaration = "d"


@tds_data_type(11, None)
class ExtendedFloat(TdmsType):
    pass


@tds_data_type(0x19, np.single, set_np_type=False)
class SingleFloatWithUnit(StructType):
    size = 4
    struct_declaration = "f"


@tds_data_type(0x1A, np.double, set_np_type=False)
class DoubleFloatWithUnit(StructType):
    size = 8
    struct_declaration = "d"


@tds_data_type(0x1B, None)
class ExtendedFloatWithUnit(TdmsType):
    pass


class StringOffsetError(ValueError):
    """Invalid offsets in the offset table of a chunk of string data

    :ivar sample_index: Index of the offending string within the chunk,
        or None if the offset table itself could not be read.
    :ivar offset: The offending offset value, if one could be read.
    :ivar limit: The largest offset that would have been valid.
    :ivar samples: Valid samples read before the bad offset, if any.
    :ivar source_ranges: Source ranges for ``samples``, if any.
    """

    def __init__(self, message, sample_index=None, offset=None, limit=None):
        super().__init__(message)
        self.sample_index = sample_index
        self.offset = offset
        self.limit = limit
        self.samples = []
        self.source_ranges = []


@tds_data_type(0x20, None)
class String(TdmsType):
    def __init__(self, value):
        self.value = value
        content = String.encode(value)
        length = _struct_pack('<L', len(content))
        self.bytes = length + content

    @staticmethod
    def encode(value):
        """ Encode a string value as UTF-8 bytes, passing through values that are already bytes
        """
        if isinstance(value, bytes):
            return value
        return value.encode('utf-8')

    @staticmethod
    def read(file, endianness="<"):
        size_bytes = file.read(4)
        size = _struct_unpack(endianness + 'L', size_bytes)[0]
        return String._decode(file.read(size))

    @classmethod
    def read_values(cls, file, number_values, endianness="<", data_size=None):
        """ Read string raw data

            This is stored as an array of offsets
            followed by the contiguous string data.

            :param data_size: Optional total size of this chunk of string data in bytes.
                When given, offsets are validated against this size and reads are
                bounded by it, so a corrupt offset cannot read into a later chunk.
        """
        if number_values == 0:
            return []

        payload_start, offsets = cls._read_offsets(
            file, number_values, endianness, data_size)
        payload_size = None if data_size is None else data_size - 4 * number_values
        strings = []
        previous = 0
        for i, offset in enumerate(offsets):
            length = offset - previous
            if payload_size is not None:
                length = min(length, max(0, payload_size - previous))
            s = file.read(length)
            if len(s) != length:
                raise StringOffsetError(
                    "String data for sample %d is truncated: expected %d bytes but got %d" % (
                        i, length, len(s)),
                    sample_index=i, offset=offset, limit=payload_size)
            strings.append(String._decode(s))
            previous = offset
        if payload_size is not None:
            # Leave the file at the declared end of this chunk's data, even when
            # the last offset does not account for all of it, so a following
            # object in the same chunk is read from the correct position.
            file.seek(payload_start + payload_size)
        return strings

    @classmethod
    def read_samples(
            cls, file, number_values, offset=0, length=None, endianness="<",
            data_size=None, encoding=None):
        """Read a range of string values from one chunk of string data

        Only the offset table and the bytes for the requested samples are read.
        The returned source ranges are absolute byte ranges in ``file`` and have a
        one to one correspondence with the returned samples.  An empty string has
        a zero length range at the position of its (empty) payload.

        Strings are decoded from their actual bytes, so multi-byte characters are
        handled without assuming one byte per character.

        :param data_size: Optional total size of this chunk of string data in bytes,
            used to bound reads so that corrupt offsets cannot read into another chunk.
        :param encoding: Optional text encoding to use, defaulting to UTF-8.
        :raises StringOffsetError: If the offset table is invalid or truncated.
        """
        if number_values == 0 or length == 0:
            return [], []
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if length is not None and length < 0:
            raise ValueError("length must be non-negative")

        start = min(offset, number_values)
        stop = number_values if length is None else min(number_values, start + length)
        if start >= stop:
            return [], []

        chunk_start = file.tell()
        try:
            offsets, payload_start, first_byte = cls._read_offsets_range(
                file, number_values, start, stop, endianness, data_size)
        except StringOffsetError as exc:
            bad_index = exc.sample_index
            if bad_index is not None and bad_index > start:
                # Return the valid part of the chunk before the bad offset
                try:
                    file.seek(chunk_start)
                    samples, source_ranges = cls.read_samples(
                        file, number_values, start, bad_index - start,
                        endianness, data_size, encoding)
                    exc.samples = samples
                    exc.source_ranges = source_ranges
                except StringOffsetError:
                    pass
            raise

        last_byte = offsets[-1]
        byte_count = last_byte - first_byte

        file.seek(payload_start + first_byte)
        payload = file.read(byte_count)
        if len(payload) != byte_count:
            payload_size = None if data_size is None else data_size - 4 * number_values
            raise StringOffsetError(
                "String data is truncated: expected %d bytes but got %d" % (
                    byte_count, len(payload)),
                limit=payload_size)

        samples = []
        source_ranges = []
        previous = first_byte
        for i, end in zip(range(start, stop), offsets):
            sample_bytes = payload[previous - first_byte:end - first_byte]
            samples.append(cls._decode(sample_bytes, encoding))
            source_ranges.append((payload_start + previous, end - previous))
            previous = end
        return samples, source_ranges

    @classmethod
    def _read_offsets(cls, file, number_values, endianness="<", data_size=None):
        """Read and validate the offset table for a chunk of string data

        :returns: Tuple of the absolute payload start position and list of end offsets.
        """
        offsets, payload_start, _ = cls._read_offsets_range(
            file, number_values, 0, number_values, endianness, data_size)
        return payload_start, offsets

    @classmethod
    def _read_offsets_range(
            cls, file, number_values, start, stop, endianness="<", data_size=None):
        """Read and validate the offsets needed to read samples from start to stop

        Only the offset table entries needed to locate and bound the requested
        samples are read, so reading a small range does not read the whole chunk.

        :returns: Tuple of end offsets for the requested samples, the absolute
            payload start position, and the start byte of the first requested sample.
        """
        table_size = 4 * number_values
        payload_size = None if data_size is None else data_size - table_size
        if data_size is not None and table_size > data_size:
            raise StringOffsetError(
                "String offset table of %d bytes does not fit in a chunk of %d bytes" % (
                    table_size, data_size),
                limit=data_size)

        table_start = file.tell()
        # Read the offset immediately before the first requested sample, plus one
        # extra offset when available, so a decreasing offset can be detected.
        first_index = max(0, start - 2) if start > 1 else max(0, start - 1)
        num_offsets = stop - first_index
        file.seek(table_start + 4 * first_index)
        raw_offsets = file.read(4 * num_offsets)
        if len(raw_offsets) != 4 * num_offsets:
            raise StringOffsetError(
                "String offset table is truncated: expected %d bytes but got %d" % (
                    4 * num_offsets, len(raw_offsets)))
        values = list(_struct_unpack(endianness + 'L' * num_offsets, raw_offsets))

        for j, offset in enumerate(values):
            if payload_size is not None and offset > payload_size:
                raise StringOffsetError(
                    "String offset %d for sample %d is outside the chunk payload of %d bytes" % (
                        offset, first_index + j, payload_size),
                    sample_index=first_index + j, offset=offset, limit=payload_size)
            if j > 0 and offset < values[j - 1]:
                raise StringOffsetError(
                    "String offset %d for sample %d is less than the previous offset %d" % (
                        offset, first_index + j, values[j - 1]),
                    sample_index=first_index + j, offset=offset, limit=payload_size)

        first_byte = 0 if start == 0 else values[start - 1 - first_index]
        offsets = values[start - first_index:]
        if offsets and first_byte > offsets[-1]:
            raise StringOffsetError(
                "String offset for sample %d is after the end offset %d" % (
                    start - 1, offsets[-1]),
                sample_index=start - 1, offset=first_byte, limit=payload_size)

        payload_start = table_start + table_size
        return offsets, payload_start, first_byte

    @staticmethod
    def _decode(string_bytes, encoding=None):
        if encoding is not None:
            return string_bytes.decode(encoding, errors='replace')
        try:
            return string_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            log.warning(
                "Error decoding string from bytes %s, retrying with replace handler: %s",
                string_bytes, exc)
            return string_bytes.decode('utf-8', errors='replace')


@tds_data_type(0x21, np.bool_)
class Boolean(StructType):
    size = 1
    struct_declaration = "b"

    def __init__(self, value):
        self.value = bool(value)
        self.bytes = _struct_pack('<' + self.struct_declaration, self.value)

    @classmethod
    def read(cls, file, endianness="<"):
        return bool(super(Boolean, cls).read(file, endianness))


@tds_data_type(0x44, None)
class TimeStamp(TdmsType):
    # Time stamps are stored as number of seconds since
    # 01/01/1904 00:00:00.00 UTC, ignoring leap seconds,
    # and number of 2^-64 fractions of a second.
    # Note that the TDMS epoch is not the Unix epoch.
    _tdms_epoch = np.datetime64('1904-01-01 00:00:00', 'us')
    _fractions_per_microsecond = float(10**-6) / 2**-64

    size = 16

    def __init__(self, value):
        if not isinstance(value, np.datetime64):
            value = np.datetime64(value, 'us')
        self.value = value
        epoch_delta = value - self._tdms_epoch

        seconds = int(epoch_delta / np.timedelta64(1, 's'))
        remainder = epoch_delta - np.timedelta64(seconds, 's')
        zero_delta = np.timedelta64(0, 's')
        if remainder < zero_delta:
            remainder = np.timedelta64(1, 's') + remainder
            seconds = seconds - 1
        microseconds = int(remainder / np.timedelta64(1, 'us'))
        second_fractions = int(microseconds * self._fractions_per_microsecond)
        self.bytes = _struct_pack('<Qq', second_fractions, seconds)

    @classmethod
    def read(cls, file, endianness="<"):
        data = file.read(16)
        if endianness == "<":
            (second_fractions, seconds) = _struct_unpack(
                endianness + 'Qq', data)
        else:
            (seconds, second_fractions) = _struct_unpack(
                 endianness + 'qQ', data)
        return TdmsTimestamp(seconds, second_fractions)

    @classmethod
    def from_bytes(cls, byte_array, endianness="<"):
        """ Convert an array of bytes to an array of timestamps
        """
        byte_array = byte_array.reshape((-1, 16))
        if endianness == "<":
            dtype = np.dtype([('second_fractions', '<u8'), ('seconds', '<i8')])
        else:
            dtype = np.dtype([('seconds', '>i8'), ('second_fractions', '>u8')])
        return TimestampArray(byte_array.view(dtype).reshape(-1))

    @classmethod
    def to_array_bytes(cls, array):
        """ Vectorized conversion of a numpy datetime64 array (or TimestampArray)
        to the packed bytes representation used in TDMS files.

        This avoids constructing a Python TimeStamp object per element, which
        is orders of magnitude slower than doing the equivalent computation
        with numpy array operations.
        """
        array = np.asarray(array).astype('datetime64[us]')
        epoch_delta_us = (array - cls._tdms_epoch).astype(np.int64)
        seconds = epoch_delta_us // 1_000_000
        microseconds = epoch_delta_us - seconds * 1_000_000
        second_fractions = (
            microseconds.astype(np.float64) * cls._fractions_per_microsecond
        ).astype(np.uint64)

        packed = np.empty(
            len(array), dtype=np.dtype([('second_fractions', '<u8'), ('seconds', '<i8')]))
        packed['second_fractions'] = second_fractions
        packed['seconds'] = seconds
        return packed.tobytes()


@tds_data_type(0x08000c, np.complex64)
class ComplexSingleFloat(TdmsType):
    size = 8


@tds_data_type(0x10000d, np.complex128)
class ComplexDoubleFloat(TdmsType):
    size = 16


@tds_data_type(0xFFFFFFFF, None)
class DaqMxRawData(TdmsType):
    pass
