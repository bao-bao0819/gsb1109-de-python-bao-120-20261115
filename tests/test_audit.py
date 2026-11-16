#    pydifact - a python edifact library
#
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
"""Layer-by-layer envelope auditing (UNB/UNZ and per-message UNH/UNT)."""

import pytest

from pydifact.audit import (
    AuditStatus,
    audit,
    audit_file,
    audit_interchange,
    audit_message,
    audit_str,
)
from pydifact.exceptions import EDISyntaxError
from pydifact.segmentcollection import Interchange, Message
from pydifact.segments import Segment

UNB = "UNB+UNOC:1+1234+3333+200102:2212+42'"


def _msg(ref: str, body: str = "BGM+1'", unt_count: int | str = 3) -> str:
    return f"UNH+{ref}+ORDERS:D:96A:UN'{body}UNT+{unt_count}+{ref}'"


def test_complete_empty_interchange_is_ok_with_evidence():
    audit_result = audit_str(UNB + "UNZ+0+42'")

    assert audit_result.status is AuditStatus.OK
    assert audit_result.messages == []
    assert audit_result.control_reference == "42"
    assert audit_result.unz_reference == "42"
    assert audit_result.reference_ok is True
    assert audit_result.declared_message_count == 0
    assert audit_result.actual_message_count == 0
    # the empty exchange itself is evidence
    assert audit_result.segments == [
        Segment("UNB", ["UNOC", "1"], "1234", "3333", ["200102", "2212"], "42"),
        Segment("UNZ", "0", "42"),
    ]


def test_empty_interchange_with_una_keeps_una_out_of_counts():
    audit_result = audit_str("UNA:+,? '" + UNB + "UNZ+0+42'")

    assert audit_result.status is AuditStatus.OK
    assert audit_result.una == Segment("UNA", ":+,? '")
    assert [segment.tag for segment in audit_result.segments] == ["UNA", "UNB", "UNZ"]
    assert audit_result.total_segment_count == 2


def test_clean_interchange_reports_every_layer():
    content = UNB + _msg("1") + _msg("2") + "UNZ+2+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.OK
    assert audit_result.message_count_ok is True
    assert [message.status for message in audit_result.messages] == [
        AuditStatus.OK,
        AuditStatus.OK,
    ]
    assert [message.reference_number for message in audit_result.messages] == ["1", "2"]
    assert [message.actual_segment_count for message in audit_result.messages] == [3, 3]
    assert audit_result.total_segment_count == 8
    assert audit_result.message_segment_count == 6


def test_message_segments_include_original_envelope():
    content = UNB + "UNH+7+ORDERS:D:96A:UN'BGM+1'UNT+3+7'UNZ+1+42'"

    message = audit_str(content).messages[0]

    assert message.unh == Segment("UNH", "7", ["ORDERS", "D", "96A", "UN"])
    assert message.unt == Segment("UNT", "3", "7")
    assert [segment.tag for segment in message.segments] == ["UNH", "BGM", "UNT"]
    assert message.reference_ok is True
    assert message.segment_count_ok is True


def test_bad_count_in_one_message_keeps_sibling_messages():
    content = UNB + _msg("1", unt_count=99) + _msg("2") + _msg("3") + "UNZ+3+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.MISMATCH
    assert [message.reference_number for message in audit_result.messages] == [
        "1",
        "2",
        "3",
    ]
    assert [message.status for message in audit_result.messages] == [
        AuditStatus.MISMATCH,
        AuditStatus.OK,
        AuditStatus.OK,
    ]
    bad = audit_result.messages[0]
    assert bad.declared_segment_count == 99
    assert bad.actual_segment_count == 3
    assert [issue.code for issue in bad.issues] == ["message_segment_count_mismatch"]
    assert bad.issues[0].segment == bad.unt


def test_message_reference_mismatch_is_reported():
    content = UNB + "UNH+1+ORDERS:D:96A:UN'BGM+1'UNT+3+2'UNZ+1+42'"

    message = audit_str(content).messages[0]

    assert message.status is AuditStatus.MISMATCH
    assert message.reference_ok is False
    assert [issue.code for issue in message.issues] == ["message_reference_mismatch"]


def test_interchange_reference_mismatch_is_reported():
    content = UNB + _msg("1") + "UNZ+1+43'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.MISMATCH
    assert audit_result.reference_ok is False
    assert "interchange_reference_mismatch" in {
        issue.code for issue in audit_result.issues
    }
    assert audit_result.unz_reference == "43"


def test_unz_message_count_mismatch_is_reported_with_both_numbers():
    content = UNB + _msg("1") + "UNZ+5+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.MISMATCH
    assert audit_result.message_count_ok is False
    issue = next(
        issue for issue in audit_result.issues if issue.code == "message_count_mismatch"
    )
    assert issue.expected == 5
    assert issue.actual == 1
    assert issue.segment == audit_result.unz


def test_truncated_file_without_unz_is_truncated_not_error():
    content = UNB + _msg("1") + "UNH+2+ORDERS:D:96A:UN'BGM+2'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.TRUNCATED
    assert audit_result.truncated is True
    assert audit_result.unz is None
    assert [issue.code for issue in audit_result.issues] == ["unz_missing"]
    assert [message.status for message in audit_result.messages] == [
        AuditStatus.OK,
        AuditStatus.TRUNCATED,
    ]
    assert [issue.code for issue in audit_result.messages[1].issues] == ["unt_missing"]


def test_truncated_mid_segment_keeps_complete_segments():
    content = UNB + "UNH+1+ORDERS:D:96A:UN'BGM+1'UNT+3+1'UN"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.TRUNCATED
    assert audit_result.parse_error is not None
    assert "unexpected_end" in {issue.code for issue in audit_result.issues}
    # the complete message before the cut is still audited
    assert len(audit_result.messages) == 1
    assert audit_result.messages[0].status is AuditStatus.OK


def test_missing_unt_before_unz_is_explicit_error_not_truncation():
    content = UNB + _msg("1") + "UNH+2+ORDERS:D:96A:UN'BGM+2'UNZ+2+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.ERROR
    assert audit_result.status is not AuditStatus.TRUNCATED
    assert audit_result.truncated is False
    assert [message.status for message in audit_result.messages] == [
        AuditStatus.OK,
        AuditStatus.ERROR,
    ]
    assert [issue.code for issue in audit_result.messages[1].issues] == [
        "unt_missing_before_unz"
    ]


def test_missing_unt_before_next_unh_keeps_both_messages():
    content = UNB + "UNH+1+ORDERS:D:96A:UN'BGM+1'" + _msg("2") + "UNZ+2+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.ERROR
    assert [message.reference_number for message in audit_result.messages] == ["1", "2"]
    assert [message.status for message in audit_result.messages] == [
        AuditStatus.ERROR,
        AuditStatus.OK,
    ]


def test_unz_present_but_malformed_is_error_not_truncation():
    content = UNB + _msg("1") + "UNZ+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.ERROR
    assert "malformed_unz" in {issue.code for issue in audit_result.issues}


def test_non_numeric_counts_are_reported_as_malformed():
    content = UNB + _msg("1", unt_count="x") + "UNZ+one+42'"

    audit_result = audit_str(content)

    codes = {issue.code for issue in audit_result.issues}
    assert "malformed_message_count" in codes
    message_codes = {issue.code for issue in audit_result.messages[0].issues}
    assert "malformed_segment_count" in message_codes
    assert audit_result.status is AuditStatus.ERROR


def test_unt_without_unh_is_structural_error():
    content = UNB + "UNT+2+1'UNZ+1+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.ERROR
    assert "unt_without_unh" in {issue.code for issue in audit_result.issues}


def test_segment_outside_message_is_structural_error():
    content = UNB + _msg("1") + "BGM+stray'UNZ+1+42'"

    audit_result = audit_str(content)

    assert audit_result.status is AuditStatus.ERROR
    assert "segment_outside_message" in {issue.code for issue in audit_result.issues}
    assert [message.status for message in audit_result.messages] == [AuditStatus.OK]


def test_functional_groups_are_kept_as_separate_evidence():
    content = (
        UNB
        + "UNG+INFENT+SENDER+RECIPIENT+240704:1032+1+UN+D:00B:FD2401'"
        + _msg("1")
        + "UNE+1+1'"
        + "UNZ+1+42'"
    )

    audit_result = audit_str(content)

    assert [segment.tag for segment in audit_result.group_segments] == ["UNG", "UNE"]
    assert audit_result.status is AuditStatus.OK


def test_audit_dispatches_str_path_and_interchange(tmp_path):
    content = UNB + _msg("1") + "UNZ+1+42'"
    path = tmp_path / "clean.edi"
    path.write_text(content, encoding="iso8859-1")

    assert audit_str(content).status is AuditStatus.OK
    assert audit_file(path).status is AuditStatus.OK
    assert audit(path).status is AuditStatus.OK

    interchange = Interchange.from_str(content, validate=False)
    assert audit(interchange).status is AuditStatus.OK
    assert audit_interchange(interchange).status is AuditStatus.OK


def test_parsed_interchange_keeps_original_envelope():
    content = UNB + _msg("1", unt_count=99) + "UNZ+1+42'"

    interchange = Interchange.from_str(content, validate=False)
    audit_result = interchange.audit()

    assert interchange.header_segment is not None
    assert interchange.footer_segment == Segment("UNZ", "1", "42")
    assert audit_result.messages[0].unt == Segment("UNT", "99", "1")
    assert audit_result.messages[0].status is AuditStatus.MISMATCH


def test_parsed_interchange_without_unz_stays_truncated():
    interchange = Interchange.from_str(UNB + _msg("1"), validate=False)

    audit_result = interchange.audit()

    assert audit_result.status is AuditStatus.TRUNCATED
    assert audit_result.unz is None
    # serialization may still craft a footer; the audit must not pretend it existed
    assert interchange.serialize().endswith("UNZ+1+42'")


def test_in_memory_empty_interchange_audits_ok():
    interchange = Interchange(
        sender="1234",
        recipient="3333",
        control_reference="42",
        syntax_identifier=("UNOC", 1),
    )

    audit_result = interchange.audit()

    assert audit_result.status is AuditStatus.OK
    assert audit_result.declared_message_count == 0
    assert audit_result.actual_message_count == 0


def test_in_memory_message_is_audited_with_crafted_envelope():
    message = Message("1", ["ORDERS", "D", "96A", "UN"])
    message.add_segment(Segment("BGM", "1"))

    result = audit_message(message)

    assert result.status is AuditStatus.OK
    assert result.unh == Segment("UNH", "1", ["ORDERS", "D", "96A", "UN"])
    assert result.unt == Segment("UNT", "3", "1")


def test_missing_references_are_mismatch_not_silently_ok():
    content = UNB + "UNH++ORDERS:D:96A:UN'UNT+2+'UNZ+1+42'"

    audit_result = audit_str(content)

    message = audit_result.messages[0]
    assert message.reference_ok is False
    assert "missing_reference" in {issue.code for issue in message.issues}
    assert audit_result.status is AuditStatus.MISMATCH


def test_validate_raises_only_for_structural_faults():
    mismatch = audit_str(UNB + _msg("1", unt_count=99) + "UNZ+1+42'")
    truncated = audit_str(UNB + _msg("1"))
    broken = audit_str(UNB + "UNT+2+1'UNZ+1+42'")

    assert mismatch.validate() is mismatch
    assert truncated.validate() is truncated
    with pytest.raises(EDISyntaxError):
        broken.validate()


def test_validate_checks_message_level_structural_faults():
    malformed = audit_str(UNB + "UNH+1+ORDERS:D:96A:UN'UNT'UNZ+1+42'")

    with pytest.raises(EDISyntaxError):
        malformed.validate()
