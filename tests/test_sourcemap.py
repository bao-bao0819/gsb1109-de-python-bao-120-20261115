#    pydifact - a python edifact library
#    Copyright (C) 2017-2024  Christian González
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Source ranges: a bad field found downstream must be traceable back to
the original EDI bytes - through encoding conversion and release-character
processing - and undecodable bytes must isolate their field instead of
silently altering values with replacement characters."""

import pytest

from pydifact.exceptions import EDIEncodingError
from pydifact.segmentcollection import Interchange
from pydifact.sourcemap import DECODE_ERROR_SENTINEL, decode_with_source_map

EDI = (
    "UNB+UNOA:4+SENDER+RECIPIENT+20240101:1200+REF1'"
    "UNH+1+ORDERS:D:96A:UN'"
    "BGM+220+B10001'"
    "UNT+3+1'"
    "UNZ+1+REF1'"
)


def test_element_ranges_point_into_message():
    interchange = Interchange.from_str(EDI, validate=False)
    bgm = interchange.get_segment("BGM")
    assert bgm.element_ranges is not None
    for element, element_range in zip(bgm.elements, bgm.element_ranges):
        assert EDI[element_range[0] : element_range[1]] == element


def test_segment_source_range_covers_whole_segment():
    interchange = Interchange.from_str(EDI, validate=False)
    bgm = interchange.get_segment("BGM")
    assert EDI[bgm.source_range[0] : bgm.source_range[1]] == "BGM+220+B10001'"


def test_composite_component_ranges():
    edi = "UNB+UNOA:4+S+R+20240101:1200+X'RFF+PD:50515'UNZ+0+X'"
    interchange = Interchange.from_str(edi, validate=False)
    rff = interchange.get_segment("RFF")
    component_ranges = rff.element_ranges[0]
    assert edi[component_ranges[0][0] : component_ranges[0][1]] == "PD"
    assert edi[component_ranges[1][0] : component_ranges[1][1]] == "50515"


def test_empty_components_have_point_ranges():
    edi = "UNB+UNOA:4+S+R+20240101:1200+X'IMD+::A'UNZ+0+X'"
    interchange = Interchange.from_str(edi, validate=False)
    imd = interchange.get_segment("IMD")
    assert imd.elements[0] == ["", "", "A"]
    component_ranges = imd.element_ranges[0]
    assert component_ranges[0] == component_ranges[1]
    assert component_ranges[0][0] == component_ranges[0][1]
    assert edi[component_ranges[2][0] : component_ranges[2][1]] == "A"


def test_release_character_range_covers_escape_sequence():
    """A released (escaped) character keeps the range of its escape
    sequence; the range is not squeezed to the released character count."""
    edi = "UNB+UNOA:4+S+R+20240101:1200+X'ERC+10:Craig?'s'UNZ+0+X'"
    interchange = Interchange.from_str(edi, validate=False)
    erc = interchange.get_segment("ERC")
    assert erc.elements[0] == ["10", "Craig's"]
    component_ranges = erc.element_ranges[0]
    # "Craig's" is 7 characters, but its source "Craig?'s" is 8
    assert edi[component_ranges[1][0] : component_ranges[1][1]] == "Craig?'s"


def test_ranges_with_una_header():
    edi = "UNA:+,? '" + EDI
    interchange = Interchange.from_str(edi, validate=False)
    bgm = interchange.get_segment("BGM")
    start, end = bgm.element_ranges[1]
    assert edi[start:end] == "B10001"


def test_from_bytes_byte_ranges_match_original_bytes():
    data = EDI.encode("iso8859-1")
    interchange = Interchange.from_bytes(data, validate=False)
    bgm = interchange.get_segment("BGM")
    assert bgm.byte_ranges is not None
    for element, byte_range in zip(bgm.elements, bgm.byte_ranges):
        assert data[byte_range[0] : byte_range[1]] == element.encode("iso8859-1")
    # the order number field can be located in the original bytes
    start, end = bgm.byte_ranges[1]
    assert data[start:end] == b"B10001"


def test_from_bytes_with_una_header():
    data = ("UNA:+,? '" + EDI).encode("iso8859-1")
    interchange = Interchange.from_bytes(data, validate=False)
    bgm = interchange.get_segment("BGM")
    start, end = bgm.byte_ranges[1]
    assert data[start:end] == b"B10001"


def test_from_bytes_utf8_multibyte_span():
    """A multi-byte character maps to its whole byte span; pydifact does not
    fabricate a per-character correspondence."""
    edi = "UNB+UNOA:4+S+R+20240101:1200+X'FTX+AAI+++Müller'UNZ+0+X'"
    data = edi.encode("utf-8")
    interchange = Interchange.from_bytes(data, encoding="utf-8", validate=False)
    ftx = interchange.get_segment("FTX")
    start, end = ftx.byte_ranges[3]
    # "Müller" is 6 characters but 7 bytes
    assert ftx.elements[3] == "Müller"
    assert data[start:end] == "Müller".encode("utf-8")
    assert end - start == 7


def test_from_bytes_release_character_byte_range():
    edi = "UNB+UNOA:4+S+R+20240101:1200+X'ERC+10:Craig?'s'UNZ+0+X'"
    data = edi.encode("iso8859-1")
    interchange = Interchange.from_bytes(data, validate=False)
    erc = interchange.get_segment("ERC")
    start, end = erc.byte_ranges[0][1]
    assert data[start:end] == b"Craig?'s"


def test_from_file_tracks_byte_ranges(tmp_path):
    edi_file = tmp_path / "order.edi"
    edi_file.write_bytes(EDI.encode("iso8859-1"))
    interchange = Interchange.from_file(str(edi_file), validate=False)
    assert interchange.source_map is not None
    assert interchange.source_encoding == "iso8859-1"
    bgm = interchange.get_segment("BGM")
    start, end = bgm.byte_ranges[1]
    assert EDI.encode("iso8859-1")[start:end] == b"B10001"


def test_invalid_encoding_isolates_the_field():
    """Undecodable bytes must isolate their field, not silently change the
    order number with replacement characters."""
    data = EDI.encode("utf-8").replace(b"B10001", b"B1000\xc3")
    with pytest.raises(EDIEncodingError) as excinfo:
        Interchange.from_bytes(data, encoding="utf-8", validate=False)
    exc = excinfo.value
    assert exc.segment_tag == "BGM"
    assert exc.element_index == 1
    assert exc.component_index is None
    bad_byte = data.index(b"\xc3")
    assert exc.byte_range == (bad_byte, bad_byte + 1)
    assert "BGM" in str(exc)
    # no replacement character anywhere
    assert "�" not in str(exc)


def test_invalid_encoding_in_composite_component():
    edi = "UNB+UNOA:4+S+R+20240101:1200+X'RFF+PD:50515'UNZ+0+X'"
    data = edi.encode("utf-8").replace(b"50515", b"50\xc3\x2815")
    with pytest.raises(EDIEncodingError) as excinfo:
        Interchange.from_bytes(data, encoding="utf-8", validate=False)
    exc = excinfo.value
    assert exc.segment_tag == "RFF"
    assert exc.element_index == 0
    assert exc.component_index == 1
    # only the lone lead byte is the bad unit, the "(" is content again
    bad_byte = data.index(b"\xc3")
    assert exc.byte_range == (bad_byte, bad_byte + 1)


def test_invalid_encoding_via_from_file(tmp_path):
    edi_file = tmp_path / "broken.edi"
    edi_file.write_bytes(EDI.encode("utf-8").replace(b"B10001", b"B1000\xc3"))
    with pytest.raises(EDIEncodingError):
        Interchange.from_file(str(edi_file), encoding="utf-8", validate=False)


def test_decode_with_source_map_multibyte():
    decoded = decode_with_source_map("aäb".encode("utf-8"), "utf-8")
    assert decoded.text == "aäb"
    assert decoded.errors == []
    assert decoded.source_map.byte_span(0, 1) == (0, 1)
    # "ä" is one character but two bytes
    assert decoded.source_map.byte_span(1, 2) == (1, 3)
    assert decoded.source_map.byte_span(2, 3) == (3, 4)
    assert decoded.source_map.byte_span(3, 3) == (4, 4)


def test_decode_error_uses_sentinel_not_replacement_char():
    decoded = decode_with_source_map(b"ab\xffcd", "utf-8")
    assert decoded.text == "ab" + DECODE_ERROR_SENTINEL + "cd"
    assert "�" not in decoded.text
    assert len(decoded.errors) == 1
    error = decoded.errors[0]
    assert error.byte_start == 2
    assert error.byte_end == 3
    assert error.char_pos == 2
    # characters after the error still map to their bytes
    assert decoded.source_map.byte_span(3, 4) == (3, 4)


def test_decode_error_truncated_sequence_at_end():
    decoded = decode_with_source_map(b"ab\xc3", "utf-8")
    assert decoded.text == "ab" + DECODE_ERROR_SENTINEL
    assert len(decoded.errors) == 1
    assert decoded.errors[0].byte_start == 2
    assert decoded.errors[0].byte_end == 3


def test_decode_error_does_not_swallow_following_bytes():
    # \xc3 is a lone lead byte; the "(" behind it is valid content again
    decoded = decode_with_source_map(b"a\xc3(b", "utf-8")
    assert decoded.text == "a" + DECODE_ERROR_SENTINEL + "(b"
    assert decoded.errors[0].byte_start == 1
    assert decoded.errors[0].byte_end == 2
    assert decoded.source_map.byte_span(2, 3) == (2, 3)
