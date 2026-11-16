#
# smpplib -- SMPP Library for Python
# Copyright (c) 2005 Martynas Jocius <mjoc@akl.lt>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""Incremental reading of concatenated SMPP PDUs from recorded streams"""

import struct

from smpplib import exceptions

HEADER_LENGTH = 16
LENGTH_FIELD_SIZE = 4
DEFAULT_MAX_PDU_LENGTH = 64 * 1024


class PDUStreamError(exceptions.PDUError):
    """Raised when a recorded PDU stream declares an invalid PDU length"""

    def __init__(self, message, offset, declared_length=None):
        super(PDUStreamError, self).__init__(message)
        self.offset = offset
        self.declared_length = declared_length


class RawPDU(object):
    """Raw PDU bytes together with their original byte range in the stream"""

    def __init__(self, offset, data):
        self.offset = offset
        self.data = data

    @property
    def end(self):
        """Offset one past the last byte of this PDU in the stream"""
        return self.offset + len(self.data)

    def __len__(self):
        return len(self.data)

    def __eq__(self, other):
        if not isinstance(other, RawPDU):
            return NotImplemented
        return self.offset == other.offset and self.data == other.data

    def __repr__(self):
        return 'RawPDU(offset={}, length={})'.format(self.offset, len(self.data))


class PDUStreamReader(object):
    """Incrementally splits a stream of concatenated SMPP PDUs.

    Splitting is purely length-driven: once a length header is read,
    exactly that many bytes are consumed as the PDU. Bytes inside a PDU
    payload that happen to look like a command header are never treated
    as the start of the next PDU.
    """

    def __init__(self, max_pdu_length=DEFAULT_MAX_PDU_LENGTH):
        if max_pdu_length < HEADER_LENGTH:
            raise ValueError('max_pdu_length must be at least {}'.format(HEADER_LENGTH))
        self.max_pdu_length = max_pdu_length
        self._buffer = bytearray()
        self._offset = 0
        self._declared = None

    @property
    def offset(self):
        """Absolute stream offset of the first buffered byte"""
        return self._offset

    @property
    def pending(self):
        """Number of buffered bytes belonging to an incomplete trailing PDU"""
        return len(self._buffer)

    def feed(self, data):
        """Feed a chunk of bytes, return a list of completed RawPDUs.

        Raises PDUStreamError as soon as a complete length header declares
        a length outside [HEADER_LENGTH, max_pdu_length], without waiting
        for the rest of the PDU. The reader should be discarded after an
        error since the stream position can no longer be trusted.
        """
        self._buffer += data
        return list(self._extract())

    def _extract(self):
        while True:
            if self._declared is None:
                if len(self._buffer) < LENGTH_FIELD_SIZE:
                    return
                declared = struct.unpack('>L', bytes(self._buffer[:LENGTH_FIELD_SIZE]))[0]
                if declared < HEADER_LENGTH or declared > self.max_pdu_length:
                    raise PDUStreamError(
                        'Invalid PDU length {} declared at offset {}'.format(
                            declared, self._offset),
                        offset=self._offset,
                        declared_length=declared,
                    )
                self._declared = declared
            if len(self._buffer) < self._declared:
                return
            raw = bytes(self._buffer[:self._declared])
            del self._buffer[:self._declared]
            offset = self._offset
            self._offset += self._declared
            self._declared = None
            yield RawPDU(offset, raw)


def iter_file(fileobj, chunk_size=4096, max_pdu_length=DEFAULT_MAX_PDU_LENGTH):
    """Yield RawPDUs from a file-like object holding concatenated PDUs.

    Raises PDUStreamError if the stream ends with a truncated PDU.
    """
    reader = PDUStreamReader(max_pdu_length=max_pdu_length)
    while True:
        chunk = fileobj.read(chunk_size)
        if not chunk:
            break
        for raw in reader.feed(chunk):
            yield raw
    if reader.pending:
        raise PDUStreamError(
            'Truncated PDU at end of stream: {} byte(s) pending at offset {}'.format(
                reader.pending, reader.offset),
            offset=reader.offset,
            declared_length=reader._declared,
        )
