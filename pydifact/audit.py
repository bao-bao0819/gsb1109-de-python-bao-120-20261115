"""Layer-by-layer auditing of EDIFACT interchange envelopes.

The audit keeps the original ``UNB``/``UNZ`` and ``UNH``/``UNT`` segments as
evidence and checks, for every layer separately:

* the control reference numbers,
* the declared message count (``UNZ``) and segment counts (``UNT``).

Messages are grouped independently, so a bad count in one message never hides
its sibling messages.  A file that simply ends before its ``UNZ`` (or before a
``UNT``) is reported as ``truncated``; a trailer that is present but carries a
wrong count/reference is reported as ``mismatch`` instead.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from pydifact.constants import Element
from pydifact.exceptions import EDISyntaxError
from pydifact.parser import Parser
from pydifact.segmentcollection import Interchange, Message
from pydifact.segments import Segment


class AuditStatus(str, Enum):
    """Outcome of an envelope layer or of the whole interchange."""

    OK = "ok"
    MISMATCH = "mismatch"
    TRUNCATED = "truncated"
    ERROR = "error"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class AuditIssue:
    """A single finding, tied to the segment that caused it (if any)."""

    code: str
    message: str
    segment: Segment | None = None
    expected: Any = None
    actual: Any = None
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "segment": _segment_dict(self.segment),
            "expected": self.expected,
            "actual": self.actual,
            "source": self.source,
        }


@dataclass
class MessageAudit:
    """Audit result for one ``UNH`` ... ``UNT`` message."""

    index: int
    unh: Segment | None
    unt: Segment | None
    body: list[Segment] = field(default_factory=list)
    message: Message | None = None
    issues: list[AuditIssue] = field(default_factory=list)
    status: AuditStatus = AuditStatus.OK

    @property
    def reference_number(self) -> str | None:
        """Message reference number from ``UNH`` (0062)."""
        if self.unh is not None and self.unh.elements:
            value = self.unh.elements[0]
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def unt_reference_number(self) -> str | None:
        """Message reference number from ``UNT`` (0062)."""
        if self.unt is not None and len(self.unt.elements) > 1:
            value = self.unt.elements[1]
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def reference_ok(self) -> bool | None:
        if self.unh is None or self.unt is None:
            return None
        if self.reference_number is None or self.unt_reference_number is None:
            # A missing reference must not accidentally compare equal.
            return False
        return self.reference_number == self.unt_reference_number

    @property
    def declared_segment_count(self) -> int | None:
        if self.unt is None or not self.unt.elements:
            return None
        return _as_count(self.unt.elements[0])

    @property
    def actual_segment_count(self) -> int:
        """Number of segments from ``UNH`` through ``UNT`` (inclusive)."""
        count = 1 + len(self.body)
        if self.unt is not None:
            count += 1
        return count

    @property
    def segment_count_ok(self) -> bool | None:
        declared = self.declared_segment_count
        if declared is None:
            return None
        return declared == self.actual_segment_count

    @property
    def segments(self) -> list[Segment]:
        """Original segments of this message, envelope included."""
        result: list[Segment] = []
        if self.unh is not None:
            result.append(self.unh)
        result.extend(self.body)
        if self.unt is not None:
            result.append(self.unt)
        return result

    # convenient alias used by report/archive style callers
    @property
    def original_segments(self) -> list[Segment]:
        return self.segments

    @property
    def ok(self) -> bool:
        return self.status is AuditStatus.OK

    def validate(self) -> "MessageAudit":
        """Raise :class:`EDISyntaxError` for structural message faults."""
        structural = [issue for issue in self.issues if issue.code in STRUCTURAL_CODES]
        if structural:
            details = "; ".join(issue.message for issue in structural)
            raise EDISyntaxError(f"Message audit found structural errors: {details}")
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "status": self.status.value,
            "reference_number": self.reference_number,
            "unt_reference_number": self.unt_reference_number,
            "reference_ok": self.reference_ok,
            "declared_segment_count": self.declared_segment_count,
            "actual_segment_count": self.actual_segment_count,
            "segment_count_ok": self.segment_count_ok,
            "unh": _segment_dict(self.unh),
            "unt": _segment_dict(self.unt),
            "segments": [_segment_dict(segment) for segment in self.segments],
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass
class InterchangeAudit:
    """Audit result for one ``UNB`` ... ``UNZ`` interchange."""

    unb: Segment | None
    unz: Segment | None
    messages: list[MessageAudit] = field(default_factory=list)
    body: list[Segment] = field(default_factory=list)
    group_segments: list[Segment] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)
    status: AuditStatus = AuditStatus.OK
    parse_error: str | None = None
    una: Segment | None = None
    source: str | None = None

    @property
    def control_reference(self) -> str | None:
        """Interchange control reference from ``UNB`` (0020)."""
        if self.unb is not None and len(self.unb.elements) > 4:
            value = self.unb.elements[4]
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def unz_reference(self) -> str | None:
        """Interchange control reference from ``UNZ`` (0020)."""
        if self.unz is not None and len(self.unz.elements) > 1:
            value = self.unz.elements[1]
            if isinstance(value, str) and value:
                return value
        return None

    @property
    def reference_ok(self) -> bool | None:
        if self.unb is None or self.unz is None:
            return None
        if self.control_reference is None or self.unz_reference is None:
            return False
        return self.control_reference == self.unz_reference

    @property
    def declared_message_count(self) -> int | None:
        if self.unz is None or not self.unz.elements:
            return None
        return _as_count(self.unz.elements[0])

    @property
    def actual_message_count(self) -> int:
        return len(self.messages)

    @property
    def message_count_ok(self) -> bool | None:
        declared = self.declared_message_count
        if declared is None:
            return None
        return declared == self.actual_message_count

    @property
    def total_segment_count(self) -> int:
        """Segments from ``UNB`` through ``UNZ``, inclusive (``UNA`` excluded)."""
        count = 0
        if self.unb is not None:
            count += 1
        count += len(self.body)
        if self.unz is not None:
            count += 1
        return count

    @property
    def message_segment_count(self) -> int:
        """Total number of segments inside all messages, envelopes included."""
        return sum(message.actual_segment_count for message in self.messages)

    @property
    def segments(self) -> list[Segment]:
        """Original interchange segments, envelope (and UNA) included."""
        result: list[Segment] = []
        if self.una is not None:
            result.append(self.una)
        if self.unb is not None:
            result.append(self.unb)
        result.extend(self.body)
        if self.unz is not None:
            result.append(self.unz)
        return result

    @property
    def original_segments(self) -> list[Segment]:
        return self.segments

    @property
    def ok(self) -> bool:
        return self.status is AuditStatus.OK

    @property
    def truncated(self) -> bool:
        return self.status is AuditStatus.TRUNCATED

    @property
    def flattened_issues(self) -> list[AuditIssue]:
        """Interchange and message issues, in report order."""
        issues = list(self.issues)
        for message in self.messages:
            for issue in message.issues:
                if issue.source is None:
                    issue = replace(issue, source=self.source)
                issues.append(issue)
        return issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "source": self.source,
            "control_reference": self.control_reference,
            "unz_reference": self.unz_reference,
            "reference_ok": self.reference_ok,
            "declared_message_count": self.declared_message_count,
            "actual_message_count": self.actual_message_count,
            "message_count_ok": self.message_count_ok,
            "total_segment_count": self.total_segment_count,
            "message_segment_count": self.message_segment_count,
            "unb": _segment_dict(self.unb),
            "unz": _segment_dict(self.unz),
            "una": _segment_dict(self.una),
            "segments": [_segment_dict(segment) for segment in self.segments],
            "messages": [message.as_dict() for message in self.messages],
            "group_segments": [
                _segment_dict(segment) for segment in self.group_segments
            ],
            "issues": [issue.as_dict() for issue in self.issues],
            "parse_error": self.parse_error,
        }

    def summary(self) -> str:
        """One-line, human readable summary of the audit."""
        parts = [f"interchange {self.status.value}"]
        if self.control_reference is not None:
            parts.append(f"UNB/UNZ reference {self.control_reference!r}")
        parts.append(
            f"messages {self.actual_message_count}"
            f" (declared {self.declared_message_count})"
        )
        if self.parse_error:
            parts.append(f"parse error: {self.parse_error}")
        return "; ".join(parts)

    def __str__(self) -> str:
        return self.summary()

    def validate(self) -> "InterchangeAudit":
        """Raise :class:`EDISyntaxError` for structural envelope faults.

        Count and reference mismatches stay as findings (they describe wrong
        data, not a broken structure); truncation is not raised either.  This
        mirrors :meth:`Message.validate` and
        :meth:`Interchange.validate`, which never raise for mere content
        errors.
        """
        structural = [
            issue
            for issue in self.flattened_issues
            if issue.code in STRUCTURAL_CODES or issue.code == "parse_error"
        ]
        if structural:
            details = "; ".join(issue.message for issue in structural)
            raise EDISyntaxError(f"Envelope audit found structural errors: {details}")
        return self


def _segment_dict(segment: Segment | None) -> dict[str, Any] | None:
    if segment is None:
        return None
    return {"tag": segment.tag, "elements": list(segment.elements)}


def _as_count(value: Element) -> int | None:
    """Parse a numeric control count, rejecting signs/whitespace and blanks."""
    if not isinstance(value, str) or not value.isdecimal():
        return None
    return int(value)


TRUNCATION_CODES = {"unexpected_end", "unz_missing", "unt_missing"}
STRUCTURAL_CODES = {
    "unb_missing",
    "multiple_unb",
    "multiple_unz",
    "malformed_unb",
    "malformed_unz",
    "malformed_unh",
    "unt_without_unh",
    "unt_missing_before_unh",
    "unt_missing_before_unz",
    "malformed_message_count",
    "malformed_segment_count",
    "malformed_unt",
    "segment_outside_message",
    "unh_missing",
    "parse_error",
}


def _status_for(issues: Iterable[AuditIssue]) -> AuditStatus:
    """Derive one status from the collected issues.

    A missing trailer (truncated file) and a trailer with a wrong value
    (explicit error) are deliberately kept apart.
    """
    codes = {issue.code for issue in issues}
    if not codes:
        return AuditStatus.OK

    truncation = codes & TRUNCATION_CODES
    structural = codes & STRUCTURAL_CODES
    mismatches = codes - TRUNCATION_CODES - STRUCTURAL_CODES

    if truncation and (mismatches or structural):
        return AuditStatus.ERROR
    if structural:
        return AuditStatus.ERROR
    if truncation:
        return AuditStatus.TRUNCATED
    return AuditStatus.MISMATCH


def _finalize_message(
    audit: MessageAudit,
    *,
    truncated: bool = False,
    followed_by: str = "UNH",
) -> None:
    issues = audit.issues

    if audit.unh is None:
        issues.append(AuditIssue("unh_missing", "Message has no UNH segment."))
    if audit.unt is None:
        if truncated:
            code = "unt_missing"
            text = "Message was not closed with an UNT segment (file truncated?)."
        else:
            code = f"unt_missing_before_{followed_by.lower()}"
            text = (
                "Message was not closed with an UNT segment before the "
                f"following {followed_by}."
            )
        issues.append(AuditIssue(code, text, segment=audit.unh))
    else:
        if audit.unh is not None and (
            audit.reference_number is None or audit.unt_reference_number is None
        ):
            issues.append(
                AuditIssue(
                    "missing_reference",
                    "UNH/UNT message reference number is missing.",
                    segment=audit.unt,
                    expected=audit.reference_number,
                    actual=audit.unt_reference_number,
                )
            )
        elif audit.unh is not None and not audit.reference_ok:
            issues.append(
                AuditIssue(
                    "message_reference_mismatch",
                    "UNH/UNT message reference numbers differ.",
                    segment=audit.unt,
                    expected=audit.reference_number,
                    actual=audit.unt_reference_number,
                )
            )
        if len(audit.unt.elements) < 2:
            issues.append(
                AuditIssue(
                    "malformed_unt",
                    "UNT needs a segment count and a message reference number.",
                    segment=audit.unt,
                )
            )
        declared = audit.declared_segment_count
        if audit.unt.elements and _as_count(audit.unt.elements[0]) is None:
            issues.append(
                AuditIssue(
                    "malformed_segment_count",
                    "UNT segment count is not numeric.",
                    segment=audit.unt,
                    actual=audit.unt.elements[0],
                )
            )
        elif declared is not None and declared != audit.actual_segment_count:
            issues.append(
                AuditIssue(
                    "message_segment_count_mismatch",
                    "UNT segment count does not match the message content.",
                    segment=audit.unt,
                    expected=declared,
                    actual=audit.actual_segment_count,
                )
            )
    audit.status = _status_for(issues)


def _aggregate_status(
    own_issues: Iterable[AuditIssue], children: Iterable[AuditStatus]
) -> AuditStatus:
    """Combine interchange-level and message-level outcomes.

    Any child error wins; otherwise truncation, then mismatch, then ok.  This
    keeps a bad count in one message visible at the interchange level without
    suppressing the healthy sibling messages.
    """
    own = _status_for(own_issues)
    child_statuses = list(children)
    if own is AuditStatus.ERROR or AuditStatus.ERROR in child_statuses:
        return AuditStatus.ERROR
    if own is AuditStatus.TRUNCATED or AuditStatus.TRUNCATED in child_statuses:
        return AuditStatus.TRUNCATED
    if own is AuditStatus.MISMATCH or AuditStatus.MISMATCH in child_statuses:
        return AuditStatus.MISMATCH
    return AuditStatus.OK


def _build_message(unh: Segment, index: int) -> MessageAudit:
    audit = MessageAudit(index=index, unh=unh, unt=None)
    elements = unh.elements
    if (
        len(elements) >= 2
        and isinstance(elements[0], str)
        and isinstance(elements[1], list)
    ):
        message = Message(elements[0], elements[1])
        message.header_segment = unh
        audit.message = message
    else:
        audit.issues.append(
            AuditIssue(
                "malformed_unh",
                "UNH must contain a message reference number and a composite "
                "message identifier.",
                segment=unh,
            )
        )
    return audit


def _group_messages(
    segments: list[Segment],
    *,
    interchange_closed: bool = False,
) -> tuple[list[MessageAudit], list[AuditIssue], int, int, list[Segment]]:
    """Group segments into messages without losing siblings.

    ``interchange_closed`` says whether an ``UNZ`` followed the body.  An open
    ``UNH`` at the end is then an explicit missing ``UNT`` (``error``); without
    an ``UNZ`` the file is treated as truncated.

    Returns ``(messages, issues, inside_count, outside_count, group_segments)``.
    """
    messages: list[MessageAudit] = []
    issues: list[AuditIssue] = []
    current: MessageAudit | None = None
    inside_count = 0
    outside_count = 0
    body_outside_groups: list[Segment] = []

    for segment in segments:
        if segment.tag == "UNH":
            if current is not None:
                # A new UNH starts while the previous message has no UNT.
                # Keep the previous message as evidence instead of dropping it.
                _finalize_message(current, followed_by="UNH")
                messages.append(current)
            current = _build_message(segment, index=len(messages) + 1)
            inside_count += 1
        elif segment.tag == "UNT":
            if current is None:
                issues.append(
                    AuditIssue(
                        "unt_without_unh",
                        "UNT segment without a matching UNH.",
                        segment=segment,
                    )
                )
                outside_count += 1
                continue
            current.unt = segment
            if current.message is not None:
                current.message.footer_segment = segment
            _finalize_message(current)
            messages.append(current)
            current = None
            inside_count += 1
        else:
            if current is None:
                if segment.tag in ("UNG", "UNE"):
                    # Functional groups are a separate envelope layer that
                    # pydifact does not model.  Keep them as evidence, but do
                    # not report the group envelope as stray message content.
                    body_outside_groups.append(segment)
                else:
                    outside_count += 1
                    issues.append(
                        AuditIssue(
                            "segment_outside_message",
                            "Segment is not enclosed in an UNH/UNT message.",
                            segment=segment,
                        )
                    )
            else:
                current.body.append(segment)
                if current.message is not None:
                    current.message.add_segment(segment)
                inside_count += 1

    if current is not None:
        # Reaching the end of the data without an UNT.  If the interchange was
        # explicitly closed by an UNZ, the UNT is genuinely missing (error);
        # otherwise the file was cut off (truncated).
        if interchange_closed:
            _finalize_message(current, followed_by="UNZ")
        else:
            _finalize_message(current, truncated=True)
        messages.append(current)

    return messages, issues, inside_count, outside_count, body_outside_groups


def _audit_segments(
    segments: list[Segment],
    *,
    parse_error: str | None = None,
    una: Segment | None = None,
    source: str | None = None,
) -> InterchangeAudit:
    unb: Segment | None = None
    unz: Segment | None = None
    body: list[Segment] = []
    issues: list[AuditIssue] = []

    # UNA is a service string advice, not part of the envelope count.
    start = 0
    if segments and segments[0].tag == "UNA":
        una = segments[0]
        start = 1

    for segment in segments[start:]:
        if segment.tag == "UNB":
            if unb is not None:
                issues.append(
                    AuditIssue(
                        "multiple_unb",
                        "More than one UNB segment found.",
                        segment=segment,
                    )
                )
            else:
                unb = segment
            continue
        if segment.tag == "UNZ":
            if unz is not None:
                issues.append(
                    AuditIssue(
                        "multiple_unz",
                        "More than one UNZ segment found.",
                        segment=segment,
                    )
                )
            else:
                unz = segment
            continue
        body.append(segment)

    messages, message_issues, _inside, _outside, group_segments = _group_messages(
        body, interchange_closed=unz is not None
    )
    issues.extend(message_issues)

    if unb is None:
        issues.append(AuditIssue("unb_missing", "Interchange has no UNB header."))
    elif len(unb.elements) < 5:
        issues.append(
            AuditIssue(
                "malformed_unb",
                "UNB is incomplete; it needs at least five elements.",
                segment=unb,
                actual=len(unb.elements),
            )
        )
    if unz is None:
        issues.append(
            AuditIssue(
                "unz_missing",
                "Interchange was not closed with an UNZ segment (file truncated?).",
                segment=unb,
            )
        )
    elif len(unz.elements) < 2:
        issues.append(
            AuditIssue(
                "malformed_unz",
                "UNZ needs a message count and an interchange control reference.",
                segment=unz,
            )
        )
    if parse_error:
        code = (
            "unexpected_end"
            if "Unexpected end of EDI messages" in parse_error
            else "parse_error"
        )
        issues.append(AuditIssue(code, parse_error))

    audit = InterchangeAudit(
        unb=unb,
        unz=unz,
        messages=messages,
        body=body,
        issues=issues,
        parse_error=parse_error,
        una=una,
        source=source,
    )
    audit.group_segments = group_segments

    # Interchange-level checks, only where both ends are available.
    if unb is not None and unz is not None and len(unz.elements) >= 2:
        if audit.control_reference is None or audit.unz_reference is None:
            issues.append(
                AuditIssue(
                    "missing_reference",
                    "UNB/UNZ interchange control reference is missing.",
                    segment=unz,
                    expected=audit.control_reference,
                    actual=audit.unz_reference,
                )
            )
        elif audit.control_reference != audit.unz_reference:
            issues.append(
                AuditIssue(
                    "interchange_reference_mismatch",
                    "UNB/UNZ interchange control references differ.",
                    segment=unz,
                    expected=audit.control_reference,
                    actual=audit.unz_reference,
                )
            )
    if unz is not None and unz.elements and audit.declared_message_count is not None:
        if audit.declared_message_count != audit.actual_message_count:
            issues.append(
                AuditIssue(
                    "message_count_mismatch",
                    "UNZ message count does not match the number of messages.",
                    segment=unz,
                    expected=audit.declared_message_count,
                    actual=audit.actual_message_count,
                )
            )
    elif unz is not None and unz.elements and _as_count(unz.elements[0]) is None:
        issues.append(
            AuditIssue(
                "malformed_message_count",
                "UNZ message count is not numeric.",
                segment=unz,
                actual=unz.elements[0],
            )
        )

    audit.status = _aggregate_status(issues, (m.status for m in messages))
    if source is not None:
        audit.issues = [_with_source(issue, source) for issue in audit.issues]
        for message in audit.messages:
            message.issues = [_with_source(issue, source) for issue in message.issues]
    return audit


def _with_source(issue: AuditIssue, source: str) -> AuditIssue:
    if issue.source == source:
        return issue
    return replace(issue, source=source)


def _parse_lenient(content: str, *, validate: bool) -> tuple[list[Segment], str | None]:
    """Parse as much as possible, keeping complete segments before an error."""
    parser = Parser(validate=validate, warn_non_conformant=False)
    segments: list[Segment] = []
    parse_error: str | None = None
    try:
        iterator = parser.parse(content)
        while True:
            try:
                segments.append(next(iterator))
            except StopIteration:
                break
    except EDISyntaxError as exc:
        parse_error = str(exc)
    return segments, parse_error


def audit_str(content: str, *, validate: bool = False) -> InterchangeAudit:
    """Audit an interchange (or a truncated one) supplied as a string.

    Parsing is lenient: complete segments before a truncated trailing segment
    are still returned as evidence, and truncation is reported through the
    audit result rather than raised as an exception.
    """
    segments, parse_error = _parse_lenient(content, validate=validate)
    return _audit_segments(segments, parse_error=parse_error)


def audit_file(
    path: str | os.PathLike[str],
    *,
    encoding: str = "iso8859-1",
    validate: bool = False,
) -> InterchangeAudit:
    """Audit an interchange file, tolerating a missing/truncated trailer."""
    with open(path, encoding=encoding) as handle:
        content = handle.read()
    return audit_str(content, validate=validate)


def audit_interchange(interchange: Interchange) -> InterchangeAudit:
    """Audit an already parsed :class:`~pydifact.segmentcollection.Interchange`.

    Original envelope segments are used when the interchange kept them.
    For an interchange built in memory (never parsed), the crafted envelope is
    used so that a complete, freshly assembled interchange audits as ``ok``.
    """
    unb = interchange.header_segment
    if unb is None:
        unb = interchange.get_header_segment()
    unz = interchange.footer_segment
    if unz is None and not interchange.is_parsed:
        unz = interchange.get_footer_segment()

    segments: list[Segment] = []
    una = interchange.una_segment
    if una is not None:
        segments.append(una)
    if unb is not None:
        segments.append(unb)
    segments.extend(interchange.segments)
    if unz is not None:
        segments.append(unz)
    return _audit_segments(segments, una=una)


def audit_message(message: Message, index: int = 1) -> MessageAudit:
    """Audit one :class:`Message`, keeping its original envelope segments."""
    unh = message.header_segment
    if unh is None:
        unh = message.get_header_segment()
    unt = message.footer_segment
    truncated = unt is None
    if unt is None and not message.is_parsed:
        unt = message.get_footer_segment()
        truncated = False
    audit = MessageAudit(index=index, unh=unh, unt=unt, message=message)
    audit.body = list(message.segments)
    _finalize_message(audit, truncated=truncated)
    return audit


def audit(source: Any, **kwargs: Any) -> InterchangeAudit:
    """Audit a string, file path or :class:`Interchange`.

    This is a small dispatcher; use :func:`audit_str`, :func:`audit_file` or
    :func:`audit_interchange` when the input kind is already known.
    """
    if isinstance(source, Interchange):
        return audit_interchange(source)
    if isinstance(source, Message):
        raise TypeError("audit() expects an Interchange, not a Message")
    if isinstance(source, os.PathLike):
        return audit_file(source, **kwargs)
    if isinstance(source, str):
        if "UNB" in source or "UNA" in source or "'" in source:
            return audit_str(source, **kwargs)
        path = Path(source)
        if path.exists():
            return audit_file(path, **kwargs)
        return audit_str(source, **kwargs)
    raise TypeError(f"Unsupported source for audit(): {type(source)!r}")
