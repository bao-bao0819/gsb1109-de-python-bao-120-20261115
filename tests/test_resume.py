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

import pytest

from pydifact.exceptions import EDISyntaxError
from pydifact.resume import ResumableReader

UNB = "UNB+UNOC:1+1234+3333+200102:2212+42'"
MESSAGE_1 = "UNH+1+PAORES:93:1:IA'BGM+381+INV001'UNT+3+1'"
MESSAGE_2 = "UNH+2+PAORES:93:1:IA'BGM+381+INV002'UNT+3+2'"
UNZ = "UNZ+2+42'"
INTERCHANGE = UNB + MESSAGE_1 + MESSAGE_2 + UNZ


@pytest.fixture
def reader():
    return ResumableReader()


def test_feed_whole_interchange_at_once(reader):
    committed = reader.feed(INTERCHANGE)
    assert [m.reference_number for m in committed] == ["1", "2"]
    assert [m.reference_number for m in reader.messages] == ["1", "2"]
    assert reader.last_complete_message.reference_number == "2"
    assert reader.unconfirmed_tail == ""
    assert reader.closed
    assert not reader.unclosed


def test_feed_character_by_character(reader):
    committed = []
    for char in INTERCHANGE:
        committed += reader.feed(char)
    assert [m.reference_number for m in committed] == ["1", "2"]
    assert reader.closed
    assert reader.unconfirmed_tail == ""


def test_committed_message_contents(reader):
    reader.feed(INTERCHANGE)
    message = reader.messages[0]
    assert message.type == "PAORES"
    assert [s.tag for s in message.segments] == ["BGM"]
    assert message.get_footer_segment().elements == ["3", "1"]


def test_disconnect_reports_last_complete_message_and_tail(reader):
    # the connection breaks in the middle of the second message
    reader.feed(UNB + MESSAGE_1 + "UNH+2+PAORES:93:1:IA'BGM+38")
    assert reader.last_complete_message.reference_number == "1"
    assert reader.unconfirmed_tail == "UNH+2+PAORES:93:1:IA'BGM+38"
    assert not reader.closed


def test_tail_is_empty_right_after_unb(reader):
    # the envelope header is consumed by the reader, it is not a message
    assert reader.feed(UNB) == []
    assert reader.last_complete_message is None
    assert reader.unconfirmed_tail == ""


def test_unt_count_mismatch_is_not_committed(reader):
    # UNT claims 5 segments, but UNH + BGM + UNT are only 3
    with pytest.raises(EDISyntaxError, match="segment count"):
        reader.feed(UNB + "UNH+1+PAORES:93:1:IA'BGM+381'UNT+5+1'")
    assert reader.messages == []
    assert reader.last_complete_message is None


def test_unt_reference_mismatch_is_not_committed(reader):
    with pytest.raises(EDISyntaxError, match="reference number"):
        reader.feed(UNB + "UNH+1+PAORES:93:1:IA'BGM+381'UNT+3+WRONG'")
    assert reader.messages == []


def test_unt_without_unh_raises(reader):
    with pytest.raises(EDISyntaxError, match="without matching UNH"):
        reader.feed(UNB + "UNT+2+1'")


def test_missing_unz_records_unclosed_batch(reader):
    reader.feed(UNB + MESSAGE_1 + MESSAGE_2)
    reader.finish()
    assert not reader.closed
    assert reader.unclosed
    # committed messages stay committed
    assert [m.reference_number for m in reader.messages] == ["1", "2"]


def test_closed_batch_is_not_unclosed(reader):
    reader.feed(INTERCHANGE)
    reader.finish()
    assert reader.closed
    assert not reader.unclosed


def test_resume_resent_tail_does_not_duplicate_messages(reader):
    reader.feed(UNB + MESSAGE_1 + "UNH+2+PAORES:93:1:IA'BGM+38")
    tail = reader.unconfirmed_tail
    # the sender resends the unconfirmed tail, followed by the rest
    committed = reader.resume(tail + "1+INV002'UNT+3+2'" + UNZ)
    # only the newly completed message is committed, message 1 is not rewritten
    assert [m.reference_number for m in committed] == ["2"]
    assert [m.reference_number for m in reader.messages] == ["1", "2"]
    assert reader.unconfirmed_tail == ""
    assert reader.closed


def test_resume_after_disconnect_before_any_message(reader):
    reader.feed(UNB + "UNH+1+PAO")
    tail = reader.unconfirmed_tail
    committed = reader.resume(tail + "RES:93:1:IA'BGM+381+INV001'UNT+3+1'" + UNZ[:4])
    assert [m.reference_number for m in committed] == ["1"]
    assert reader.unconfirmed_tail == UNZ[:4]
    reader.feed(UNZ[4:])
    assert reader.closed


def test_resume_requires_the_unconfirmed_tail(reader):
    reader.feed(UNB + MESSAGE_1 + "UNH+2+PAO")
    with pytest.raises(EDISyntaxError, match="unconfirmed tail"):
        reader.resume("UNH+9+PAORES:93:1:IA'UNT+2+9'")


def test_feed_after_finish_raises(reader):
    reader.feed(UNB)
    reader.finish()
    with pytest.raises(ValueError, match="finished"):
        reader.feed(MESSAGE_1)


def test_una_segment_sets_control_characters(reader):
    reader.feed("UNA:+.? '" + INTERCHANGE)
    assert reader.characters.decimal_point == "."
    assert [m.reference_number for m in reader.messages] == ["1", "2"]
    assert reader.closed


def test_una_split_across_chunks(reader):
    committed = []
    for char in "UNA:+,? '" + INTERCHANGE:
        committed += reader.feed(char)
    assert [m.reference_number for m in committed] == ["1", "2"]


def test_escaped_terminator_split_across_chunks(reader):
    stream = UNB + "UNH+1+PAORES:93:1:IA'FTX+AAI+1+escaped?'text'UNT+3+1'"
    # the chunk boundary is right between the escape character and the
    # escaped segment terminator
    cut = stream.index("?'")
    reader.feed(stream[: cut + 1])
    committed = reader.feed(stream[cut + 1 :] + "UNZ+1+42'")
    assert [m.reference_number for m in committed] == ["1"]
    assert reader.last_complete_message.segments[0].elements[2] == "escaped'text"
    assert reader.closed


def test_interchange_must_start_with_unb(reader):
    with pytest.raises(EDISyntaxError, match="must start with UNB"):
        reader.feed("UNH+1+PAORES:93:1:IA'")


def test_segment_outside_message_raises(reader):
    with pytest.raises(EDISyntaxError, match="not enclosed in UNH"):
        reader.feed(UNB + "BGM+381'")


def test_content_after_unz_raises(reader):
    reader.feed(INTERCHANGE)
    with pytest.raises(EDISyntaxError, match="after the UNZ"):
        reader.feed("UNH+3+PAORES:93:1:IA'")
