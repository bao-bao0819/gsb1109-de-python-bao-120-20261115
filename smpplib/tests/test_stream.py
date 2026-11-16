import struct
from io import BytesIO

import pytest

from smpplib import exceptions
from smpplib.stream import (
    PDUStreamError,
    PDUStreamReader,
    RawPDU,
    iter_file,
)


def make_raw(command_id, sequence, body=b''):
    return struct.pack('>4L', 16 + len(body), command_id, 0, sequence) + body


def test_feed_multiple_pdus_single_chunk():
    pdu1 = make_raw(0x15, 1)
    pdu2 = make_raw(0x21, 2, b'\x00\x01\x02')
    pdu3 = make_raw(0x04, 3)
    reader = PDUStreamReader()

    result = reader.feed(pdu1 + pdu2 + pdu3)

    assert result == [
        RawPDU(0, pdu1),
        RawPDU(len(pdu1), pdu2),
        RawPDU(len(pdu1) + len(pdu2), pdu3),
    ]
    assert result[0].end == len(pdu1)
    assert result[1].end == len(pdu1) + len(pdu2)
    assert reader.pending == 0


def test_length_header_split_across_chunks():
    pdu = make_raw(0x15, 1)
    reader = PDUStreamReader()

    assert reader.feed(pdu[:2]) == []
    assert reader.pending == 2
    assert reader.feed(pdu[2:4]) == []
    assert reader.pending == 4
    assert reader.feed(pdu[4:]) == [RawPDU(0, pdu)]
    assert reader.pending == 0


def test_byte_by_byte_feeding():
    pdu1 = make_raw(0x15, 1)
    pdu2 = make_raw(0x04, 2, b'abc')
    reader = PDUStreamReader()

    result = []
    for byte in pdu1 + pdu2:
        result.extend(reader.feed(bytes([byte])))

    assert result == [RawPDU(0, pdu1), RawPDU(len(pdu1), pdu2)]


def test_declared_length_above_max_reported_immediately():
    reader = PDUStreamReader(max_pdu_length=32)
    header = struct.pack('>L', 100)

    assert reader.feed(header[:2]) == []
    with pytest.raises(PDUStreamError) as exc_info:
        reader.feed(header[2:])

    assert exc_info.value.offset == 0
    assert exc_info.value.declared_length == 100
    assert isinstance(exc_info.value, exceptions.PDUError)


def test_declared_length_below_header_reported():
    reader = PDUStreamReader()

    with pytest.raises(PDUStreamError) as exc_info:
        reader.feed(struct.pack('>L', 8))

    assert exc_info.value.declared_length == 8


def test_error_offset_accounts_for_consumed_pdus():
    pdu = make_raw(0x15, 1)
    reader = PDUStreamReader()
    assert reader.feed(pdu) == [RawPDU(0, pdu)]

    with pytest.raises(PDUStreamError) as exc_info:
        reader.feed(struct.pack('>L', 10))

    assert exc_info.value.offset == len(pdu)


def test_incomplete_tail_can_be_continued():
    pdu1 = make_raw(0x15, 1)
    pdu2 = make_raw(0x04, 2, b'hello')
    reader = PDUStreamReader()

    assert reader.feed(pdu1 + pdu2[:10]) == [RawPDU(0, pdu1)]
    assert reader.pending == 10
    assert reader.offset == len(pdu1)

    assert reader.feed(pdu2[10:]) == [RawPDU(len(pdu1), pdu2)]
    assert reader.pending == 0


def test_header_like_bytes_in_payload_not_treated_as_pdu_start():
    payload = make_raw(0x15, 7) + b'junk'
    pdu1 = make_raw(0x04, 1, payload)
    pdu2 = make_raw(0x21, 2)
    reader = PDUStreamReader()

    result = reader.feed(pdu1 + pdu2)

    assert result == [RawPDU(0, pdu1), RawPDU(len(pdu1), pdu2)]
    assert payload in result[0].data


def test_iter_file_reads_all_pdus():
    pdu1 = make_raw(0x15, 1)
    pdu2 = make_raw(0x04, 2, b'xy')
    pdu3 = make_raw(0x21, 3)
    stream = BytesIO(pdu1 + pdu2 + pdu3)

    result = list(iter_file(stream, chunk_size=5))

    assert result == [
        RawPDU(0, pdu1),
        RawPDU(len(pdu1), pdu2),
        RawPDU(len(pdu1) + len(pdu2), pdu3),
    ]


def test_iter_file_truncated_tail_raises():
    pdu = make_raw(0x15, 1)
    stream = BytesIO(pdu + make_raw(0x04, 2)[:10])

    with pytest.raises(PDUStreamError) as exc_info:
        list(iter_file(stream, chunk_size=4))

    assert exc_info.value.offset == len(pdu)


def test_max_pdu_length_validation():
    with pytest.raises(ValueError):
        PDUStreamReader(max_pdu_length=15)
