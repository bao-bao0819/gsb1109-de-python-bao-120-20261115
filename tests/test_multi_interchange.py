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
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#    Lesser General Public License for more details.
#
#    You should have received a copy of the GNU Lesser General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import pytest

from pydifact.control.characters import Characters
from pydifact.exceptions import EDISyntaxError
from pydifact.parser import Parser
from pydifact.segmentcollection import Interchange

# interchange with default control characters
I1 = (
    "UNA:+.? '"
    "UNB+UNOC:1+AAA+BBB+200102:2212+1'"
    "UNH+42z42+PAORES:93:1:IA'"
    "UNT+2+42z42'"
    "UNZ+1+1'"
)
# interchange with differing UNA service characters (";" and "!")
I2 = (
    "UNA;+.? !"
    "UNB+UNOC;1+CCC+DDD+200102;2212+2!"
    "UNH+23x23+PAORES;93;1;IA!"
    "UNT+2+23x23!"
    "UNZ+1+2!"
)
# interchange without UNA header (default control characters)
I3 = "UNB+UNOC:1+EEE+FFF+200102:2212+3'UNH+777+PAORES:93:1:IA'UNT+2+777'UNZ+1+3'"


def test_iter_interchanges_with_different_una():
    interchanges = list(Interchange.iter_from_str(I1 + I2))

    assert len(interchanges) == 2
    first, second = interchanges
    assert first.sender == "AAA"
    assert first.characters.segment_terminator == "'"
    assert second.sender == "CCC"
    assert second.characters.segment_terminator == "!"
    assert second.characters.component_separator == ";"
    assert [s.tag for s in second.segments] == ["UNH", "UNT"]


def test_interchanges_separated_by_whitespace():
    interchanges = list(Interchange.iter_from_str(I1 + "\r\n" + I2 + "\n"))
    assert len(interchanges) == 2


def test_characters_do_not_leak_into_next_interchange():
    # I2 uses "!" as segment terminator, I3 has no UNA and must be
    # tokenized with the default characters again
    interchanges = list(Interchange.iter_from_str(I2 + I3))

    assert len(interchanges) == 2
    assert interchanges[1].sender == "EEE"
    assert interchanges[1].characters == Characters()
    assert [s.tag for s in interchanges[1].segments] == ["UNH", "UNT"]


def test_parser_defaults_are_not_mutable_by_una():
    parser = Parser()
    list(parser.parse_interchanges(I2 + I3))
    assert parser.characters == Characters()


def test_escaped_terminator_belongs_to_value():
    source = (
        "UNA:+.? '"
        "UNB+UNOC:1+AA?'A+BBB+200102:2212+1'"
        "UNH+1+PAORES:93:1:IA'"
        "UNT+2+1'"
        "UNZ+1+1'"
    )
    interchanges = list(Interchange.iter_from_str(source + I2))

    assert len(interchanges) == 2
    assert interchanges[0].sender == "AA'A"


def test_escaped_terminator_in_unz_does_not_end_interchange():
    source = (
        "UNB+UNOC:1+AAA+BBB+200102:2212+1'"
        "UNH+1+PAORES:93:1:IA'"
        "UNT+2+1'"
        "UNZ+1+1?''"  # control reference "1'", then the real terminator
    )
    interchanges = list(Interchange.iter_from_str(source + I2))

    assert len(interchanges) == 2
    assert interchanges[1].sender == "CCC"


def test_chunk_boundary_behind_release_character():
    source = "UNB+UNOC:1+AA?'A+BBB+200102:2212+1'UNH+1+PAORES:93:1:IA'UNT+2+1'UNZ+1+1'"
    # cut the source right behind the release character
    cut = source.index("?'") + 1
    chunks = [source[:cut], source[cut:]]
    interchanges = list(Interchange.iter_from_str(chunks))

    assert len(interchanges) == 1
    assert interchanges[0].sender == "AA'A"


def test_single_character_chunks():
    interchanges = list(Interchange.iter_from_str(list(I1 + I2)))

    assert len(interchanges) == 2
    assert interchanges[0].sender == "AAA"
    assert interchanges[1].sender == "CCC"


def test_segment_positions_are_traceable():
    source = I1 + I2
    segments_per_interchange = list(Parser().parse_interchanges(source))

    for segments in segments_per_interchange:
        for segment in segments:
            assert segment.position is not None
            assert source[segment.position :].startswith(segment.tag)

    # the second interchange starts right behind the first one
    assert segments_per_interchange[1][0].tag == "UNA"
    assert segments_per_interchange[1][0].position == len(I1)


def test_interchange_segment_positions():
    source = I1 + I2
    interchanges = list(Interchange.iter_from_str(source))

    unh = interchanges[1].segments[0]
    assert unh.tag == "UNH"
    assert unh.position == len(I1) + I2.index("UNH")


def test_iter_from_file_reads_in_chunks(tmp_path):
    edi_file = tmp_path / "multi.edi"
    edi_file.write_text(I1 + I2, encoding="iso8859-1")

    interchanges = list(Interchange.iter_from_file(str(edi_file), chunk_size=7))

    assert len(interchanges) == 2
    assert interchanges[0].sender == "AAA"
    assert interchanges[1].sender == "CCC"


def test_unterminated_interchange_raises():
    with pytest.raises(EDISyntaxError, match="not terminated by a UNZ"):
        list(Interchange.iter_from_str(I1 + "UNB+UNOC:1+EEE"))


def test_empty_source_yields_nothing():
    assert list(Interchange.iter_from_str("")) == []
    assert list(Interchange.iter_from_str("  \r\n")) == []
