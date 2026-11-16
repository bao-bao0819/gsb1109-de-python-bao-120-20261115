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
"""Audit multiple interchanges/files as one archive."""

import pytest

from pydifact.archive import audit_archive, audit_archive_dict, audit_files
from pydifact.audit import AuditStatus
from pydifact.exceptions import EDISyntaxError

UNB = "UNB+UNOC:1+1234+3333+200102:2212+42'"
UNB_43 = "UNB+UNOC:1+1234+3333+200102:2212+43'"


def _interchange(reference: str = "42", unz_count: int = 1) -> str:
    unb = UNB if reference == "42" else UNB_43
    return (
        unb + "UNH+1+ORDERS:D:96A:UN'BGM+1'UNT+3+1'" + f"UNZ+{unz_count}+{reference}'"
    )


def test_archive_keeps_empty_and_complete_interchanges():
    empty = UNB + "UNZ+0+42'"

    audit_result = audit_archive([empty, _interchange("43")])

    assert audit_result.status is AuditStatus.OK
    assert len(audit_result.interchanges) == 2
    assert audit_result.interchanges[0].messages == []
    assert (
        audit_result.interchanges[0].segments
        == audit_result.interchanges[0].original_segments
    )
    assert audit_result.interchanges[1].messages[0].status is AuditStatus.OK


def test_archive_splits_multiple_interchanges_in_one_string():
    audit_result = audit_archive([_interchange("42") + _interchange("43")])

    assert len(audit_result.interchanges) == 2
    assert [item.control_reference for item in audit_result.interchanges] == [
        "42",
        "43",
    ]


def test_archive_bad_message_count_does_not_hide_other_interchanges():
    bad = (
        UNB
        + "UNH+1+ORDERS:D:96A:UN'BGM+1'UNT+99+1'"
        + "UNH+2+ORDERS:D:96A:UN'BGM+2'UNT+3+2'"
        + "UNZ+2+42'"
    )

    audit_result = audit_archive([bad, _interchange("43")])

    assert audit_result.status is AuditStatus.MISMATCH
    assert [message.status for message in audit_result.interchanges[0].messages] == [
        AuditStatus.MISMATCH,
        AuditStatus.OK,
    ]
    assert audit_result.interchanges[1].status is AuditStatus.OK


def test_archive_truncated_tail_keeps_earlier_interchange():
    complete = _interchange("42")
    cut_off = UNB_43 + "UNH+9+ORDERS:D:96A:UN'BGM+9'UNT+3+9'UNZ"

    audit_result = audit_archive([complete + cut_off])

    assert len(audit_result.interchanges) == 2
    assert audit_result.interchanges[0].status is AuditStatus.OK
    assert audit_result.interchanges[0].messages[0].reference_number == "1"
    # the trailing partial UNZ is not a real trailer
    assert audit_result.interchanges[1].status is AuditStatus.TRUNCATED
    assert audit_result.interchanges[1].unz is None
    assert audit_result.status is AuditStatus.TRUNCATED


def test_archive_splits_truncated_interchange_before_next_unb():
    content = UNB + "UNH+1+ORDERS:D:96A:UN'BGM+1'UNT+3+1'" + UNB_43 + "UNZ+0+43'"

    audit_result = audit_archive([content])

    assert len(audit_result.interchanges) == 2
    assert audit_result.interchanges[0].status is AuditStatus.TRUNCATED
    assert audit_result.interchanges[0].messages[0].reference_number == "1"
    assert audit_result.interchanges[1].status is AuditStatus.OK
    assert audit_result.interchanges[1].declared_message_count == 0


def test_archive_mixed_truncation_and_error():
    structural = UNB + "UNT+2+1'UNZ+1+42'"

    audit_result = audit_archive([structural, UNB_43 + "UNH+1+ORDERS:D:96A:UN'BGM+1'"])

    assert audit_result.status is AuditStatus.ERROR


def test_archive_dict_keeps_names(tmp_path):
    path_a = tmp_path / "a.edi"
    path_b = tmp_path / "b.edi"
    path_a.write_text(_interchange("42"), encoding="iso8859-1")
    path_b.write_text(UNB_43 + "UNZ+0+43'", encoding="iso8859-1")

    audit_result = audit_archive_dict({"a.edi": path_a, "b.edi": path_b})

    assert audit_result.names == ["a.edi", "b.edi"]
    assert audit_result.as_dict()["interchanges"][0]["name"] == "a.edi"
    assert audit_files([path_a, path_b]).status is AuditStatus.OK


def test_archive_expands_directories_and_zip(tmp_path):
    import zipfile

    (tmp_path / "sub").mkdir()
    (tmp_path / "a.edi").write_text(_interchange("42"), encoding="iso8859-1")
    (tmp_path / "sub" / "b.edi").write_text(UNB_43 + "UNZ+0+43'", encoding="iso8859-1")
    zip_path = tmp_path / "c.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("inner/c.edi", _interchange("42"))

    report = audit_archive([tmp_path])

    names = [name or "" for name in report.names]
    assert any(name.endswith("a.edi") for name in names)
    assert any(name.endswith("sub/b.edi") for name in names)
    assert any(name.endswith("c.zip:inner/c.edi") for name in names)
    assert report.status is AuditStatus.OK


def test_archive_is_iterable_and_counts_statuses():
    report = audit_archive(
        [_interchange("42"), UNB_43 + "UNH+1+ORDERS:D:96A:UN'BGM+1'"]
    )

    assert len(report) == 2
    assert [item.status for item in report] == [
        AuditStatus.OK,
        AuditStatus.TRUNCATED,
    ]
    assert report.count(AuditStatus.TRUNCATED) == 1
    assert "truncated=1" in report.summary()


def test_archive_text_and_json_reports_keep_original_segments():
    report = audit_archive_dict({"a.edi": _interchange("42")})

    text = report.format_text()
    assert "UNB UNB+UNOC:1+1234+3333+200102:2212+42'" in text
    assert "UNZ UNZ+1+42'" in text
    assert "UNH UNH+1+ORDERS:D:96A:UN'" in text
    assert "UNT UNT+3+1'" in text

    data = report.as_dict()
    assert data["interchanges"][0]["unb"]["tag"] == "UNB"
    assert data["interchanges"][0]["messages"][0]["unt"]["elements"] == ["3", "1"]


def test_archive_validate_raises_for_structural_faults():
    broken = UNB + "UNT+2+1'UNZ+1+42'"

    audit_result = audit_archive([broken])

    with pytest.raises(EDISyntaxError):
        audit_result.validate()


def test_archive_accepts_parsed_interchanges():
    from pydifact.segmentcollection import Interchange

    interchange = Interchange.from_str(_interchange("42"), validate=False)

    audit_result = audit_archive([interchange])

    assert audit_result.status is AuditStatus.OK
    assert audit_result.interchanges[0].unb is interchange.header_segment


def test_empty_source_yields_explicit_record():
    report = audit_archive([""])

    assert len(report) == 1
    codes = {issue.code for issue in report.interchanges[0].issues}
    assert {"unb_missing", "unz_missing"} <= codes
    assert report.status is AuditStatus.ERROR
