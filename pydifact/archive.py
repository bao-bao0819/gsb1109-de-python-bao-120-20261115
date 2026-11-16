"""Audit a whole archive of EDIFACT interchanges.

An archive may contain several files, and a single file may contain more than
one interchange.  Every interchange is audited on its own, so an empty
interchange is still reported (and kept with its original ``UNB``/``UNZ``
segments) and a fault in one interchange does not hide the others.
"""

from __future__ import annotations

import json
import os
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydifact.audit import (
    STRUCTURAL_CODES,
    AuditIssue,
    AuditStatus,
    InterchangeAudit,
    MessageAudit,
    _audit_segments,
    _parse_lenient,
    audit_interchange,
)
from pydifact.exceptions import EDISyntaxError
from pydifact.segmentcollection import Interchange
from pydifact.segments import Segment


@dataclass
class ArchiveAudit:
    """Audit result for several interchanges, e.g. all files of an archive."""

    interchanges: list[InterchangeAudit] = field(default_factory=list)
    names: list[str | None] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)
    status: AuditStatus = AuditStatus.OK

    @property
    def items(self) -> list[InterchangeAudit]:
        return self.interchanges

    def __iter__(self):
        return iter(self.interchanges)

    def __len__(self) -> int:
        return len(self.interchanges)

    def count(self, status: AuditStatus) -> int:
        return sum(
            1 for interchange in self.interchanges if interchange.status is status
        )

    @property
    def messages(self) -> list[MessageAudit]:
        return [
            message
            for interchange in self.interchanges
            for message in interchange.messages
        ]

    @property
    def ok(self) -> bool:
        return self.status is AuditStatus.OK

    @property
    def truncated(self) -> bool:
        return self.status is AuditStatus.TRUNCATED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "interchanges": [
                dict(interchange.as_dict(), name=name)
                for interchange, name in zip(self.interchanges, self.names)
            ],
            "issues": [issue.as_dict() for issue in self.issues],
        }

    def validate(self) -> "ArchiveAudit":
        """Raise :class:`EDISyntaxError` for any structural fault."""
        problems = [
            f"{name or 'interchange'}: {issue.message}"
            for name, interchange in zip(self.names, self.interchanges)
            for issue in interchange.issues
            if issue.code in STRUCTURAL_CODES
        ]
        if problems:
            raise EDISyntaxError(
                "Archive audit found structural errors: " + "; ".join(problems)
            )
        return self

    def summary(self) -> str:
        parts = [
            f"archive {self.status.value}",
            f"interchanges {len(self.interchanges)}",
        ]
        counts = [
            f"{status.value}={self.count(status)}"
            for status in AuditStatus
            if self.count(status)
        ]
        if counts:
            parts.append(" ".join(counts))
        return "; ".join(parts)

    def format_text(self) -> str:
        """Render a layered, human-readable report, one block per layer."""
        lines = [self.summary()]
        for name, interchange in zip(self.names, self.interchanges):
            title = name or "<string>"
            lines.append(f"{title}: {interchange.status.value}")
            lines.append(
                f"  UNB+{interchange.control_reference} "
                f"UNZ+{interchange.unz_reference} "
                f"reference={'ok' if interchange.reference_ok else 'BAD'} "
                f"messages={interchange.actual_message_count}"
                f"/{interchange.declared_message_count}"
            )
            lines.append(f"  UNB {_format_segment(interchange.unb)}")
            lines.append(f"  UNZ {_format_segment(interchange.unz)}")
            for message in interchange.messages:
                lines.append(
                    f"  message {message.index}: {message.status.value} "
                    f"UNH {message.reference_number} UNT {message.unt_reference_number} "
                    f"segments={message.actual_segment_count}"
                    f"/{message.declared_segment_count}"
                )
                lines.append(f"    UNH {_format_segment(message.unh)}")
                lines.append(f"    UNT {_format_segment(message.unt)}")
            if interchange.una is not None:
                lines.append(f"  UNA {_format_segment(interchange.una)}")
            for issue in interchange.flattened_issues:
                lines.append(
                    f"  ! {issue.code}: {issue.message}"
                    + (
                        f" (segment {_format_segment(issue.segment)})"
                        if issue.segment is not None
                        else ""
                    )
                )
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.format_text()

    def format_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, default=str)


def _format_segment(segment: Segment | None) -> str:
    """Render a segment close to its original EDI form for reports."""
    if segment is None:
        return "<absent>"
    elements = []
    for element in segment.elements:
        if isinstance(element, list):
            elements.append(":".join(str(part) for part in element))
        else:
            elements.append(str(element))
    if not elements:
        return f"{segment.tag}'"
    return f"{segment.tag}+" + "+".join(elements) + "'"


def _split_interchanges(
    segments: list[Segment],
) -> list[tuple[Segment | None, list[Segment]]]:
    """Split a flat segment list into ``(UNA, segments)`` interchange groups."""
    una: Segment | None = None
    if segments and segments[0].tag == "UNA":
        una = segments[0]
        segments = segments[1:]

    groups: list[tuple[Segment | None, list[Segment]]] = []
    current: list[Segment] = []
    for segment in segments:
        # A new UNB always starts a new interchange, even if the previous one
        # was truncated and never got its UNZ.  Waiting for a UNZ would merge
        # the two and hide the truncation.
        if segment.tag == "UNB" and any(s.tag == "UNB" for s in current):
            groups.append((una, current))
            current = []
            una = None
        current.append(segment)
    if current or not groups:
        groups.append((una, current))
    return groups


def audit_archive(
    sources: Iterable[str | os.PathLike[str] | Interchange],
    *,
    encoding: str = "iso8859-1",
    validate: bool = False,
    recursive: bool = True,
) -> ArchiveAudit:
    """Audit several files, directories, ZIP archives or interchanges.

    A source may be a path, EDI text or an already parsed
    :class:`~pydifact.segmentcollection.Interchange`.  Directories and ZIP
    archives are expanded, and every interchange keeps its own source name.
    """
    result = ArchiveAudit()

    for source in sources:
        for name, audits in _expand_source(
            source, encoding=encoding, validate=validate, recursive=recursive
        ):
            for audit in audits:
                result.interchanges.append(audit)
                result.names.append(name)
            if not audits:
                # Keep an explicit, empty record so a source that contained no
                # interchange at all is still visible in the report.
                result.interchanges.append(_audit_segments([], una=None, source=name))
                result.names.append(name)

    result.status = _archive_status(result.interchanges)
    return result


def _expand_source(
    source: str | os.PathLike[str] | Interchange,
    *,
    encoding: str,
    validate: bool,
    recursive: bool = True,
) -> Iterable[tuple[str, list[InterchangeAudit]]]:
    if isinstance(source, Interchange):
        yield "<interchange>", [audit_interchange(source)]
        return

    if isinstance(source, os.PathLike):
        path = Path(source)
    else:
        text = source
        # A plain string is only treated as a path if it exists AND looks like
        # one.  In particular, the empty string must not become "." (the
        # current directory), which would silently audit the whole repository.
        candidate = Path(text) if text.strip() else None
        if "'" in text or "\n" in text:
            yield (
                "<string>",
                _audit_text_interchanges(text, validate=validate, source="<string>"),
            )
            return
        if candidate is None or not candidate.exists():
            yield (
                "<string>",
                _audit_text_interchanges(text, validate=validate, source="<string>"),
            )
            return
        path = candidate

    if path.is_dir():
        pattern = "**/*" if recursive else "*"
        for child in sorted(path.glob(pattern)):
            if child.is_file():
                yield from _expand_source(
                    child, encoding=encoding, validate=validate, recursive=recursive
                )
        return

    if path.suffix.lower() == ".zip":
        yield from _expand_zip(path, encoding=encoding, validate=validate)
        return

    with open(path, encoding=encoding) as handle:
        content = handle.read()
    yield (
        str(path),
        _audit_text_interchanges(content, validate=validate, source=str(path)),
    )


def _expand_zip(
    path: Path, *, encoding: str, validate: bool
) -> Iterable[tuple[str, list[InterchangeAudit]]]:
    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            with archive.open(info) as handle:
                content = handle.read().decode(encoding)
            yield (
                f"{path}:{info.filename}",
                _audit_text_interchanges(content, validate=validate),
            )


def audit_archive_dict(
    sources: Mapping[str, str | os.PathLike[str] | Interchange],
    *,
    encoding: str = "iso8859-1",
    validate: bool = False,
    recursive: bool = True,
) -> ArchiveAudit:
    """Audit a mapping of names to sources and keep the names as evidence."""
    result = ArchiveAudit()
    for name, source in sources.items():
        for _, audits in _expand_source(
            source, encoding=encoding, validate=validate, recursive=recursive
        ):
            for audit in audits:
                audit.source = name
                result.interchanges.append(audit)
                result.names.append(name)
    result.status = _archive_status(result.interchanges)
    return result


def audit_files(paths: Iterable[str | os.PathLike[str]], **kwargs: Any) -> ArchiveAudit:
    """Audit a list of files as one archive."""
    return audit_archive(paths, **kwargs)


def _audit_text_interchanges(
    content: str, *, validate: bool, source: str | None = None
) -> list[InterchangeAudit]:
    segments, parse_error = _parse_lenient(content, validate=validate)
    groups = _split_interchanges(segments)
    audits: list[InterchangeAudit] = []
    for index, (una, group) in enumerate(groups):
        # Only the last group can have been cut off by a parse error.
        group_error = parse_error if index == len(groups) - 1 else None
        audits.append(
            _audit_segments(group, parse_error=group_error, una=una, source=source)
        )
    return audits


def _archive_status(interchanges: list[InterchangeAudit]) -> AuditStatus:
    statuses = [interchange.status for interchange in interchanges]
    if AuditStatus.ERROR in statuses:
        return AuditStatus.ERROR
    if AuditStatus.TRUNCATED in statuses:
        return AuditStatus.TRUNCATED
    if AuditStatus.MISMATCH in statuses:
        return AuditStatus.MISMATCH
    return AuditStatus.OK
