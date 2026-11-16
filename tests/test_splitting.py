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
import datetime

import pytest

from pydifact.control.splitting import InterchangeSplitter, MessageTooLargeError
from pydifact.segmentcollection import Interchange, Message
from pydifact.segments import Segment

TIMESTAMP = datetime.datetime(2020, 1, 2, 22, 12)


def make_message(reference: str, body_segments: list[Segment] | None = None) -> Message:
    message = Message(reference, ("PAORES", 93, 1, "IA"))
    for segment in body_segments or []:
        message.add_segment(segment)
    return message


def make_interchange(
    messages: list[Message], control_reference: str = "42"
) -> Interchange:
    interchange = Interchange(
        sender="1234",
        recipient="3333",
        timestamp=TIMESTAMP,
        control_reference=control_reference,
        syntax_identifier=("UNOC", 1),
    )
    for message in messages:
        interchange.add_message(message)
    return interchange


def message_refs(interchange: Interchange) -> list[str]:
    return [message.reference_number for message in interchange.get_messages()]


def test_no_messages_returns_empty_list():
    interchange = make_interchange([])
    assert InterchangeSplitter(max_bytes=1000).split(interchange) == []


def test_single_interchange_when_everything_fits():
    messages = [make_message("1"), make_message("2")]
    interchange = make_interchange(messages)
    full_size = len(interchange.serialize().encode("iso8859-1"))

    results = InterchangeSplitter(max_bytes=full_size).split(interchange)

    assert len(results) == 1
    assert results[0].serialize() == interchange.serialize()
    assert message_refs(results[0]) == ["1", "2"]


def test_messages_are_distributed_whole_and_in_order():
    messages = [
        make_message("1", [Segment("BGM", "220", "one")]),
        make_message("2", [Segment("BGM", "220", "two")]),
        make_message("3", [Segment("BGM", "220", "three")]),
        make_message("4", [Segment("BGM", "220", "four")]),
    ]
    interchange = make_interchange(messages)

    # A limit that lets roughly one message per interchange.
    results = InterchangeSplitter(max_bytes=110).split(interchange)

    assert len(results) > 1
    # every message survives, intact and in the original order
    recovered = [ref for part in results for ref in message_refs(part)]
    assert recovered == ["1", "2", "3", "4"]
    # no part exceeds the limit (measured on the real encoded output)
    for part in results:
        assert len(part.serialize().encode("iso8859-1")) <= 110


def test_control_references_are_unique_and_gap_free():
    messages = [make_message(str(i)) for i in range(1, 6)]
    interchange = make_interchange(messages, control_reference="100")

    results = InterchangeSplitter(max_bytes=95).split(interchange)

    references = [str(part.control_reference) for part in results]
    assert len(references) == len(set(references))
    assert references == [str(100 + i) for i in range(len(results))]


def test_custom_first_reference_and_step():
    messages = [make_message(str(i)) for i in range(1, 5)]
    interchange = make_interchange(messages)

    results = InterchangeSplitter(
        max_bytes=95, first_control_reference=500, control_reference_step=10
    ).split(interchange)

    references = [str(part.control_reference) for part in results]
    assert references == [str(500 + 10 * i) for i in range(len(results))]


def test_unz_message_count_is_recalculated_per_interchange():
    messages = [make_message(str(i)) for i in range(1, 7)]
    interchange = make_interchange(messages)

    results = InterchangeSplitter(max_bytes=140).split(interchange)

    for part in results:
        unz = part.get_footer_segment()
        assert unz.tag == "UNZ"
        assert unz.elements[0] == str(len(list(part.get_messages())))
        assert unz.elements[1] == part.control_reference
        # UNB and UNZ carry the same control reference
        assert part.get_header_segment().elements[4] == part.control_reference


def test_retry_reproduces_the_same_plan_without_control_number_collision():
    messages = [make_message(str(i)) for i in range(1, 6)]
    interchange = make_interchange(messages, control_reference="7")

    splitter = InterchangeSplitter(max_bytes=100)
    first_run = splitter.split(interchange)
    # re-running on the unchanged source must yield the very same plan
    second_run = splitter.split(interchange)

    first_output = [part.serialize() for part in first_run]
    second_output = [part.serialize() for part in second_run]
    assert first_output == second_output

    first_refs = [str(part.control_reference) for part in first_run]
    second_refs = [str(part.control_reference) for part in second_run]
    assert first_refs == second_refs
    # references stay unique inside each run, so retries never collide
    assert len(set(first_refs)) == len(first_refs)


def test_source_interchange_is_not_modified():
    messages = [make_message("1"), make_message("2"), make_message("3")]
    interchange = make_interchange(messages)
    original = interchange.serialize()

    InterchangeSplitter(max_bytes=95).split(interchange)

    assert interchange.serialize() == original
    assert message_refs(interchange) == ["1", "2", "3"]


def test_oversized_single_message_is_reported_separately():
    huge = make_message("99", [Segment("FTX", "x" * 200)])
    interchange = make_interchange([make_message("1"), huge])

    with pytest.raises(MessageTooLargeError) as exc_info:
        InterchangeSplitter(max_bytes=120).split(interchange)

    error = exc_info.value
    assert error.message.reference_number == "99"
    assert error.max_bytes == 120
    assert error.required_bytes > 120
    # the small message took reference "42", so the failing interchange
    # reports the next, still unique control reference for reconciliation
    assert error.control_reference == "43"


def test_message_that_alone_exceeds_limit_raises_even_as_first_message():
    huge = make_message("99", [Segment("FTX", "x" * 300)])
    interchange = make_interchange([huge])

    with pytest.raises(MessageTooLargeError) as exc_info:
        InterchangeSplitter(max_bytes=100).split(interchange)

    assert exc_info.value.message.reference_number == "99"
    assert exc_info.value.required_bytes > 100


def test_bytes_are_measured_after_escaping():
    # the control characters in the text must be escaped on the wire, adding
    # bytes; splitting must count the escaped, encoded size
    message = make_message("1", [Segment("FTX", "a+b?c'd:e")])
    interchange = make_interchange([message])
    serialized = interchange.serialize()
    real_size = len(serialized.encode("iso8859-1"))

    # the raw control characters are escaped on the wire
    assert "?+" in serialized and "??" in serialized
    assert "?'" in serialized and "?:" in serialized
    with pytest.raises(MessageTooLargeError):
        InterchangeSplitter(max_bytes=real_size - 1).split(interchange)
    # exactly the real size still fits
    assert (
        len(
            InterchangeSplitter(max_bytes=real_size)
            .split(interchange)[0]
            .serialize()
            .encode("iso8859-1")
        )
        == real_size
    )


def test_bytes_are_measured_after_encoding():
    # "Müller" occupies 7 bytes in UTF-8 but only 6 characters
    message = make_message("1", [Segment("FTX", "Müller")])
    interchange = make_interchange([message])
    utf8_size = len(interchange.serialize().encode("utf-8"))
    latin1_size = len(interchange.serialize().encode("iso8859-1"))
    assert utf8_size > latin1_size

    results = InterchangeSplitter(max_bytes=utf8_size, encoding="utf-8").split(
        interchange
    )
    assert len(results) == 1
    assert len(results[0].serialize().encode("utf-8")) <= utf8_size


def test_una_service_string_advice_is_preserved_and_counted():
    interchange = Interchange.from_segments(
        [
            Segment("UNA", ":+,? '"),
            Segment("UNB", ["UNOC", "1"], "1234", "3333", ["200102", "2212"], "42"),
            *[
                segment
                for message in [make_message("1"), make_message("2"), make_message("3")]
                for segment in (
                    [message.get_header_segment()]
                    + message.segments
                    + [message.get_footer_segment()]
                )
            ],
            Segment("UNZ", "3", "42"),
        ]
    )

    results = InterchangeSplitter(max_bytes=110).split(interchange)

    assert len(results) > 1
    for part in results:
        serialized = part.serialize()
        assert serialized.startswith("UNA:+,? '")
        assert len(serialized.encode("iso8859-1")) <= 110


def test_envelope_elements_are_copied_onto_each_interchange():
    messages = [make_message(str(i)) for i in range(1, 5)]
    interchange = Interchange(
        sender=["1234", "ZZ"],
        recipient="3333",
        timestamp=TIMESTAMP,
        control_reference="42",
        syntax_identifier=("UNOC", 4),
        extra_header_elements=["TL"],
    )
    for message in messages:
        interchange.add_message(message)

    results = InterchangeSplitter(max_bytes=120).split(interchange)

    for part in results:
        unb = part.get_header_segment()
        assert unb.elements[1] == ["1234", "ZZ"]
        assert unb.elements[-1] == "TL"
        # syntax version 4 keeps the 8-digit preparation date
        assert unb.elements[3] == ["20200102", "2212"]


def test_generated_interchanges_parse_back_and_round_trip():
    messages = [
        make_message(str(i), [Segment("BGM", "220", f"order-{i}")]) for i in range(1, 5)
    ]
    interchange = make_interchange(messages)

    results = InterchangeSplitter(max_bytes=120).split(interchange)
    recovered = []
    for part in results:
        parsed = Interchange.from_str(part.serialize())
        recovered.extend(message_refs(parsed))

    assert recovered == ["1", "2", "3", "4"]


def test_invalid_arguments_raise_value_error():
    with pytest.raises(ValueError):
        InterchangeSplitter(max_bytes=0)
    with pytest.raises(ValueError):
        InterchangeSplitter(max_bytes=-1)
    with pytest.raises(ValueError):
        InterchangeSplitter(max_bytes=100, control_reference_step=0)


def test_non_numeric_control_reference_must_be_overridden():
    interchange = make_interchange([make_message("1"), make_message("2")], "ABC")
    with pytest.raises(ValueError):
        InterchangeSplitter(max_bytes=95).split(interchange)

    results = InterchangeSplitter(max_bytes=95, first_control_reference="1").split(
        interchange
    )
    assert str(results[0].control_reference) == "1"


def test_interchange_convenience_method_matches_splitter():
    messages = [make_message(str(i)) for i in range(1, 5)]
    interchange = make_interchange(messages)

    via_method = interchange.split(max_bytes=110, first_control_reference="9")
    via_class = InterchangeSplitter(max_bytes=110, first_control_reference="9").split(
        interchange
    )

    assert [part.serialize() for part in via_method] == [
        part.serialize() for part in via_class
    ]
