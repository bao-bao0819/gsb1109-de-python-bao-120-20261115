"""
Statement synchronization coverage tracking.

OFX statement requests name a date range, but a server may return a narrower
transaction list and/or attach a warning.  A coverage table keeps those facts
separate:

* the range the client requested;
* the range the server *declared* via ``*TRANLIST/DTSTART`` and ``DTEND``;
* the transaction records that were actually present.

The distinction matters for incremental synchronization.  An empty
transaction list is valid evidence for closing a gap only when the server
declared coverage of the whole requested range.  A transaction response with
an error status contributes no coverage, and therefore cannot advance a
checkpoint.
"""

from __future__ import annotations

__all__ = [
    "AccountCoverage",
    "CoverageError",
    "CoverageLedger",
    "CoverageRow",
    "CoverageTable",
    "DateRange",
    "LedgerUpdate",
    "ReceivedRecord",
    "ResponseStatus",
    "StatementSynchronizer",
    "SyncBatch",
    "Synchronizer",
    "build_coverage_table",
    "coverage_from_response",
    "inspect_response",
    "month_range",
]


# stdlib imports
import csv
import datetime
import io
import json
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO, overload

# local imports
from ofxtools import models
from ofxtools.models.bank.stmt import CCSTMTTRNRS, STMTTRNRS
from ofxtools.models.bank.stmtend import CCSTMTENDTRNRS, STMTENDTRNRS
from ofxtools.models.invest.stmt import INVSTMTTRNRS
from ofxtools.models.ofx import OFX
from ofxtools.Parser import OFXTree
from ofxtools.utils import UTC

_ONE_MICROSECOND = datetime.timedelta(microseconds=1)
_DATE_ATTRS = ("dtposted", "dttrade", "dtsettle", "dtuser", "dtavail")


class CoverageError(ValueError):
    """Raised when coverage cannot be determined from an OFX response."""


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Give naive datetimes the same meaning as OFX datetimes (UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _as_datetime(value: datetime.datetime | str | None) -> datetime.datetime | None:
    match value:
        case None:
            return None
        case datetime.datetime():
            return _as_utc(value)
        case str():
            parsed = datetime.datetime.fromisoformat(value)
            return _as_utc(parsed)
        case _:
            raise TypeError(f"Expected datetime/str/None, got {type(value).__name__}")


def _is_date_precision(value: datetime.datetime) -> bool:
    """True when a datetime carries no sub-day information."""
    return (
        value.hour == 0
        and value.minute == 0
        and value.second == 0
        and value.microsecond == 0
    )


def _adjacent(left: DateRange, right: DateRange) -> bool:
    """
    Whether *right* starts immediately after *left* with no uncovered instant.

    Date-only OFX intervals are inclusive calendar dates, so August 31 and
    September 1 are adjacent even though their midnight timestamps are 24 hours
    apart.  For time-bearing endpoints, only a one-microsecond gap is allowed.
    """
    if right.start <= left.end + _ONE_MICROSECOND:
        return True
    if _is_date_precision(left.end) and _is_date_precision(right.start):
        return right.start.date() - left.end.date() == datetime.timedelta(days=1)
    return False


@dataclass(frozen=True, slots=True)
class DateRange:
    """
    An inclusive date/time interval.

    OFX ``DTSTART``/``DTEND`` values are inclusive endpoints, so this type uses
    closed-interval semantics.  Datetimes without a timezone are interpreted
    as UTC, matching OFX's normalized datetime representation.
    """

    start: datetime.datetime
    end: datetime.datetime

    def __post_init__(self) -> None:
        start = _as_utc(self.start)
        end = _as_utc(self.end)
        if end < start:
            raise ValueError(f"DateRange end {end!r} precedes start {start!r}")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    # OFX-flavoured aliases, useful when reading rows from responses.
    @property
    def dtstart(self) -> datetime.datetime:
        return self.start

    @property
    def dtend(self) -> datetime.datetime:
        return self.end

    @property
    def duration(self) -> datetime.timedelta:
        return self.end - self.start

    def __bool__(self) -> bool:
        return True

    def __contains__(self, value: datetime.datetime) -> bool:
        return self.contains(value)

    @property
    def _coverage_bounds(self) -> tuple[datetime.datetime, datetime.datetime]:
        """
        Bounds used for coverage comparison.

        A date-only interval (both endpoints at midnight) covers its whole
        final calendar day.  OFX servers commonly answer a request ending at
        ``20260930`` for the full month with exactly ``DTEND=20260930``; that
        declaration must cover a request ending later that same day.
        """
        start, end = self.start, self.end
        if _is_date_precision(start) and _is_date_precision(end):
            end = end + datetime.timedelta(days=1) - _ONE_MICROSECOND
        return start, end

    def contains(self, value: datetime.datetime) -> bool:
        value = _as_utc(value)
        start, end = self._coverage_bounds
        return start <= value <= end

    def covers(self, other: DateRange) -> bool:
        """True if *other* lies wholly within this interval."""
        start, end = self._coverage_bounds
        other_start, other_end = other._coverage_bounds
        return start <= other_start and end >= other_end

    def overlaps(self, other: DateRange) -> bool:
        start, end = self._coverage_bounds
        other_start, other_end = other._coverage_bounds
        return start <= other_end and other_start <= end

    def intersection(self, other: DateRange) -> DateRange | None:
        # Preserve original endpoints when one interval wholly contains the
        # other, so a date-only declaration clipped to a date-only request
        # does not acquire a fabricated end-of-day timestamp.
        if self.covers(other):
            return other
        if other.covers(self):
            return self

        start, end = self._coverage_bounds
        other_start, other_end = other._coverage_bounds
        overlap_start = max(start, other_start)
        overlap_end = min(end, other_end)
        if overlap_end < overlap_start:
            return None
        return DateRange(overlap_start, overlap_end)

    def union(self, other: DateRange) -> DateRange:
        return DateRange(min(self.start, other.start), max(self.end, other.end))

    def subtract(self, other: DateRange) -> tuple[DateRange, ...]:
        """Return the portions of this interval not covered by *other*."""
        start, end = self._coverage_bounds
        other_start, other_end = other._coverage_bounds
        overlap_start = max(start, other_start)
        overlap_end = min(end, other_end)
        if overlap_end < overlap_start:
            return (self,)

        pieces: list[DateRange] = []
        if start < overlap_start:
            pieces.append(DateRange(self.start, overlap_start - _ONE_MICROSECOND))
        if overlap_end < end:
            pieces.append(DateRange(overlap_end + _ONE_MICROSECOND, self.end))
        return tuple(pieces)

    def to_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_dict(cls, value: Mapping[str, str]) -> DateRange:
        return cls(
            _as_datetime(value["start"]),  # type: ignore[arg-type]
            _as_datetime(value["end"]),  # type: ignore[arg-type]
        )

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


@dataclass(frozen=True, slots=True)
class ResponseStatus:
    """STATUS aggregate, plus derived success/warning/error flags."""

    code: int | None = None
    severity: str | None = None
    message: str | None = None

    @classmethod
    def from_model(cls, status: Any) -> ResponseStatus | None:
        if status is None:
            return None
        return cls(
            code=getattr(status, "code", None),
            severity=getattr(status, "severity", None),
            message=getattr(status, "message", None),
        )

    @classmethod
    def success(cls) -> ResponseStatus:
        return cls(code=0, severity="INFO")

    @classmethod
    def from_error(cls, error: BaseException | str) -> ResponseStatus:
        return cls(code=None, severity="ERROR", message=str(error))

    @property
    def normalized_severity(self) -> str | None:
        return self.severity.upper() if self.severity else None

    @property
    def is_error(self) -> bool:
        """
        An error status blocks coverage/checkpoint updates.

        OFX uses severity to distinguish warnings from errors; a nonzero code
        with severity ``WARN`` remains a warning and is surfaced separately.
        """
        severity = self.normalized_severity
        if severity == "ERROR":
            return True
        # WARN explicitly denotes a non-fatal status; everything else with a
        # nonzero CODE is treated as an error so a checkpoint can't advance.
        if severity == "WARN":
            return False
        return self.code not in (None, 0)

    @property
    def is_warning(self) -> bool:
        return self.normalized_severity == "WARN"

    @property
    def ok(self) -> bool:
        return not self.is_error

    @property
    def warning_message(self) -> str | None:
        if self.is_warning:
            return self.message or "OFX response included a warning"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ReceivedRecord:
    """A transaction record actually present in the response."""

    fitid: str | None
    date: datetime.datetime | None
    trntype: str | None = None
    amount: Decimal | None = None

    def __post_init__(self) -> None:
        if self.date is not None:
            object.__setattr__(self, "date", _as_utc(self.date))

    @property
    def dtposted(self) -> datetime.datetime | None:
        return self.date

    @property
    def dttrade(self) -> datetime.datetime | None:
        return self.date

    def to_dict(self) -> dict[str, Any]:
        return {
            "fitid": self.fitid,
            "date": self.date.isoformat() if self.date else None,
            "trntype": self.trntype,
            "amount": str(self.amount) if self.amount is not None else None,
        }


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """
    One requested statement, and the evidence returned for it.

    ``declared`` is exactly the range stated by the response's transaction
    list (or closing statement).  It is *not* inferred from the earliest and
    latest transaction records.  ``records`` summarizes what was actually
    received.
    """

    account: str | None
    requested: DateRange | None
    declared: DateRange | None
    records: tuple[ReceivedRecord, ...] = ()
    status: ResponseStatus = field(default_factory=ResponseStatus.success)
    warnings: tuple[str, ...] = ()
    request_kind: str | None = None
    error: str | None = None
    coverage_blocked: bool = False

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def received_range(self) -> DateRange | None:
        dates = [record.date for record in self.records if record.date is not None]
        if not dates:
            return None
        return DateRange(min(dates), max(dates))

    @property
    def fitids(self) -> tuple[str, ...]:
        return tuple(
            record.fitid for record in self.records if record.fitid is not None
        )

    @property
    def unique_fitids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.fitids))

    @property
    def duplicate_fitids(self) -> tuple[str, ...]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for fitid in self.fitids:
            if fitid in seen and fitid not in duplicates:
                duplicates.append(fitid)
            seen.add(fitid)
        return tuple(duplicates)

    @property
    def is_error(self) -> bool:
        return self.status.is_error or self.error is not None

    @property
    def is_warning(self) -> bool:
        return self.status.is_warning or bool(self.warnings)

    @property
    def error_message(self) -> str | None:
        if self.error is not None:
            return self.error
        if self.status.is_error:
            return self.status.message or f"OFX status code {self.status.code}"
        return None

    @property
    def accepted_coverage(self) -> DateRange | None:
        """Coverage usable for the ledger (clipped to the request)."""
        if self.is_error or self.coverage_blocked or self.declared is None:
            return None
        if self.requested is None:
            return self.declared
        return self.declared.intersection(self.requested)

    @property
    def declared_complete(self) -> bool:
        """
        Whether the response's declared envelope covers the whole request.

        This is purely a statement about what the server declared.  An error
        response may still contain a complete envelope, but that envelope is
        not usable evidence (see `is_complete` and `closes_gap`).
        """
        if self.requested is None or self.declared is None:
            return False
        return self.declared.covers(self.requested)

    @property
    def is_complete(self) -> bool:
        """Whether usable (non-error) coverage spans the requested range."""
        if self.is_error or self.coverage_blocked:
            return False
        return self.declared_complete

    @property
    def closes_gap(self) -> bool:
        """
        An empty transaction list is sufficient only when coverage is complete.

        This holds for any record count: coverage is established by the
        server's declaration, not by the presence of records.  The explicit
        ``record_count`` guard documents the empty-list rule and prevents a
        future refactor from silently changing it.
        """
        if self.record_count == 0 and self.declared is None:
            return False
        return self.is_complete

    @property
    def gaps(self) -> tuple[DateRange, ...]:
        """Requested portions not backed by acceptable declared coverage."""
        if self.requested is None:
            return ()
        coverage = self.accepted_coverage
        if coverage is None:
            return (self.requested,)
        return self.requested.subtract(coverage)

    @property
    def gap(self) -> DateRange | None:
        """The first uncovered interval, or None when the request is covered."""
        gaps = self.gaps
        if gaps:
            return gaps[0]
        return None

    def to_dict(self) -> dict[str, Any]:
        received = self.received_range
        return {
            "account": self.account,
            "request_kind": self.request_kind,
            "requested_start": self.requested.start.isoformat()
            if self.requested
            else None,
            "requested_end": self.requested.end.isoformat() if self.requested else None,
            "declared_start": self.declared.start.isoformat() if self.declared else None,
            "declared_end": self.declared.end.isoformat() if self.declared else None,
            "record_count": self.record_count,
            "received_start": received.start.isoformat() if received else None,
            "received_end": received.end.isoformat() if received else None,
            "status_code": self.status.code,
            "severity": self.status.severity,
            "message": self.status.message,
            "warning": "; ".join(self.warnings) or None,
            "coverage_blocked": self.coverage_blocked,
            "gap_start": self.gap.start.isoformat() if self.gap else None,
            "gap_end": self.gap.end.isoformat() if self.gap else None,
            "closes_gap": self.closes_gap,
        }

    @classmethod
    def from_response(
        cls,
        response: Any,
        requested: DateRange | Any | None = None,
        *,
        account: str | None = None,
    ) -> CoverageRow:
        return coverage_from_response(response, requested, account=account)


@dataclass(frozen=True, slots=True)
class LedgerUpdate:
    """Result of applying one coverage row to a `CoverageLedger`."""

    row: CoverageRow
    account: str
    accepted: bool
    added: tuple[DateRange, ...] = ()
    checkpoint_before: datetime.datetime | None = None
    checkpoint_after: datetime.datetime | None = None
    gaps: tuple[DateRange, ...] = ()
    reason: str | None = None

    @property
    def checkpoint_advanced(self) -> bool:
        return (
            self.checkpoint_after is not None
            and self.checkpoint_after != self.checkpoint_before
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "accepted": self.accepted,
            "added": [interval.to_dict() for interval in self.added],
            "checkpoint_before": self.checkpoint_before.isoformat()
            if self.checkpoint_before
            else None,
            "checkpoint_after": self.checkpoint_after.isoformat()
            if self.checkpoint_after
            else None,
            "checkpoint_advanced": self.checkpoint_advanced,
            "gaps": [interval.to_dict() for interval in self.gaps],
            "reason": self.reason,
        }


class CoverageTable(Sequence[CoverageRow]):
    """A renderable collection of `CoverageRow` objects."""

    def __init__(self, rows: Iterable[CoverageRow] = ()) -> None:
        self._rows = list(rows)

    @overload
    def __getitem__(self, index: int) -> CoverageRow: ...

    @overload
    def __getitem__(self, index: slice) -> list[CoverageRow]: ...

    def __getitem__(self, index: int | slice) -> CoverageRow | list[CoverageRow]:
        return self._rows[index]

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self) -> Iterator[CoverageRow]:
        return iter(self._rows)

    def append(self, row: CoverageRow) -> None:
        self._rows.append(row)

    def extend(self, rows: Iterable[CoverageRow]) -> None:
        self._rows.extend(rows)

    @property
    def rows(self) -> tuple[CoverageRow, ...]:
        return tuple(self._rows)

    @property
    def complete(self) -> bool:
        """True when every row closes its requested gap."""
        return bool(self._rows) and all(row.closes_gap for row in self._rows)

    @property
    def gaps(self) -> tuple[DateRange, ...]:
        return tuple(gap for row in self._rows for gap in row.gaps)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [row.to_dict() for row in self._rows]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.to_dicts(),
            "complete": self.complete,
            "row_count": len(self),
        }

    _CSV_FIELDS = (
        "account",
        "request_kind",
        "requested_start",
        "requested_end",
        "declared_start",
        "declared_end",
        "record_count",
        "received_start",
        "received_end",
        "status_code",
        "severity",
        "message",
        "warning",
        "coverage_blocked",
        "gap_start",
        "gap_end",
        "closes_gap",
    )

    def to_csv(self, destination: str | os.PathLike[str] | TextIO | None = None) -> str:
        """Write CSV to a path/file, or return it as a string."""
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=self._CSV_FIELDS)
        writer.writeheader()
        writer.writerows(self.to_dicts())
        text = output.getvalue()

        if destination is None:
            return text
        if hasattr(destination, "write"):
            destination.write(text)
        else:
            Path(destination).write_text(text, encoding="utf-8")
        return text

    def to_markdown(self) -> str:
        headers = (
            "Account",
            "Requested",
            "Declared",
            "Records",
            "Received",
            "Status",
            "Gap",
            "Closes",
        )
        rows = [
            (
                row.account or "-",
                str(row.requested) if row.requested else "-",
                str(row.declared) if row.declared else "-",
                str(row.record_count),
                str(row.received_range) if row.received_range else "-",
                self._status_text(row),
                str(row.gap) if row.gap else "-",
                "yes" if row.closes_gap else "no",
            )
            for row in self._rows
        ]
        widths = [len(header) for header in headers]
        for row in rows:
            widths = [max(width, len(value)) for width, value in zip(widths, row)]

        def format_row(values: Sequence[str]) -> str:
            return "| " + " | ".join(
                value.ljust(width) for value, width in zip(values, widths)
            ) + " |"

        separator = "| " + " | ".join("-" * width for width in widths) + " |"
        lines = [format_row(headers), separator]
        lines.extend(format_row(row) for row in rows)
        return "\n".join(lines)

    @staticmethod
    def _status_text(row: CoverageRow) -> str:
        status = row.status
        severity = status.severity or ("ERROR" if row.is_error else "INFO")
        code = "" if status.code is None else f" {status.code}"
        text = f"{severity}{code}"
        if row.error_message and row.error_message not in text:
            text += f": {row.error_message}"
        return text

    def __str__(self) -> str:
        return self.to_markdown()

    @classmethod
    def from_response(
        cls,
        response: Any,
        requests: Iterable[Any] | Any = (),
    ) -> CoverageTable:
        return build_coverage_table(response, requests)


@dataclass(slots=True)
class AccountCoverage:
    """Mutable per-account interval set used by `CoverageLedger`."""

    account: str
    intervals: list[DateRange] = field(default_factory=list)
    checkpoint: datetime.datetime | None = None

    def add(self, interval: DateRange) -> tuple[DateRange, ...]:
        before = tuple(self.intervals)
        merged = self._merged([*self.intervals, interval])
        self.intervals = list(merged)
        return tuple(i for i in merged if i not in before)

    @staticmethod
    def _merged(intervals: Iterable[DateRange]) -> tuple[DateRange, ...]:
        ordered = sorted(intervals, key=lambda interval: interval.start)
        if not ordered:
            return ()
        merged = [ordered[0]]
        for interval in ordered[1:]:
            last = merged[-1]
            if _adjacent(last, interval):
                merged[-1] = DateRange(
                    last.start, max(last.end, interval.end)
                )
            else:
                merged.append(interval)
        return tuple(merged)

    def gaps(self, requested: DateRange) -> tuple[DateRange, ...]:
        gaps: list[DateRange] = [requested]
        for interval in self.intervals:
            next_gaps: list[DateRange] = []
            for gap in gaps:
                next_gaps.extend(gap.subtract(interval))
            gaps = next_gaps
        return tuple(gaps)

    def advance_checkpoint(
        self, requested: DateRange | None
    ) -> datetime.datetime | None:
        """
        Advance the checkpoint only across a fully covered requested window.

        A partial declaration is still recorded for later gap analysis, but it
        cannot move the checkpoint.  Otherwise a later request starting from
        the checkpoint would skip the uncovered remainder.
        """
        if requested is None:
            return self.checkpoint

        # Every part of the requested window through its end must be covered.
        if self.gaps(requested):
            return self.checkpoint

        base = self.checkpoint if self.checkpoint is not None else requested.start
        position = base
        for interval in self.intervals:
            if interval.start <= position + _ONE_MICROSECOND or _adjacent(
                DateRange(position, position), interval
            ):
                if interval.end > position:
                    position = interval.end
            elif interval.start > position:
                break

        if position > base:
            self.checkpoint = position
        return self.checkpoint


class CoverageLedger:
    """
    Persistent coverage intervals and checkpoints, grouped by account.

    Ledger rules:

    * Error responses are ignored entirely (no coverage, no checkpoint).
    * Successful responses add only their declared coverage.
    * Declared coverage is evidence even when the transaction list is empty.
    * Checkpoints advance only along contiguous covered intervals.
    """

    def __init__(self) -> None:
        self._accounts: dict[str, AccountCoverage] = {}

    def _account(self, account: str) -> AccountCoverage:
        if account not in self._accounts:
            self._accounts[account] = AccountCoverage(account)
        return self._accounts[account]

    @property
    def accounts(self) -> tuple[str, ...]:
        return tuple(sorted(self._accounts))

    def checkpoint(self, account: str) -> datetime.datetime | None:
        state = self._accounts.get(account)
        return state.checkpoint if state else None

    def coverage(self, account: str) -> tuple[DateRange, ...]:
        state = self._accounts.get(account)
        return tuple(state.intervals) if state else ()

    def gaps(self, account: str, requested: DateRange) -> tuple[DateRange, ...]:
        state = self._accounts.get(account)
        if state is None:
            return (requested,)
        return state.gaps(requested)

    def apply(
        self,
        row: CoverageRow,
        account: str | None = None,
        *,
        requested: DateRange | None = None,
    ) -> LedgerUpdate:
        key = account or row.account or "default"
        state = self._account(key)
        before = state.checkpoint
        request_range = requested or row.requested

        if row.is_error:
            return LedgerUpdate(
                row=row,
                account=key,
                accepted=False,
                checkpoint_before=before,
                checkpoint_after=before,
                gaps=state.gaps(request_range) if request_range else (),
                reason=row.error_message or "OFX response has an error status",
            )

        accepted = row.accepted_coverage
        if accepted is not None and request_range is not None:
            accepted = accepted.intersection(request_range)
        if accepted is None:
            reason = (
                "coverage blocked by signon status"
                if row.coverage_blocked
                else "response declared no coverage"
            )
            return LedgerUpdate(
                row=row,
                account=key,
                accepted=False,
                checkpoint_before=before,
                checkpoint_after=before,
                gaps=state.gaps(request_range) if request_range else (),
                reason=reason,
            )

        added = state.add(accepted)
        after = state.advance_checkpoint(request_range)
        return LedgerUpdate(
            row=row,
            account=key,
            accepted=True,
            added=added,
            checkpoint_before=before,
            checkpoint_after=after,
            gaps=state.gaps(request_range) if request_range else (),
            reason=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "accounts": {
                account: {
                    "coverage": [interval.to_dict() for interval in state.intervals],
                    "checkpoint": state.checkpoint.isoformat()
                    if state.checkpoint
                    else None,
                }
                for account, state in sorted(self._accounts.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoverageLedger:
        ledger = cls()
        accounts = value.get("accounts", {})
        for account, payload in accounts.items():
            state = ledger._account(account)
            state.intervals = [
                DateRange.from_dict(interval)
                for interval in payload.get("coverage", ())
            ]
            checkpoint = payload.get("checkpoint")
            state.checkpoint = _as_datetime(checkpoint) if checkpoint else None
        return ledger

    def save(self, path: str | os.PathLike[str]) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> CoverageLedger:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def __len__(self) -> int:
        return len(self._accounts)


@dataclass(frozen=True, slots=True)
class SyncBatch:
    """Response plus its coverage table and ledger updates."""

    table: CoverageTable
    updates: tuple[LedgerUpdate, ...] = ()
    response: bytes | None = None

    @property
    def rows(self) -> tuple[CoverageRow, ...]:
        return self.table.rows

    @property
    def complete(self) -> bool:
        return self.table.complete

    def __iter__(self) -> Iterator[CoverageRow]:
        return iter(self.table)


def month_range(
    year: int,
    month: int,
    *,
    tz: datetime.tzinfo = UTC,
) -> DateRange:
    """
    The inclusive date range spanning a calendar month.

    OFX statement requests routinely use date-only values (``YYYYMMDD``).
    The end is therefore the last calendar day at midnight, matching the
    precision at which such requests are expressed (and not an artificially
    precise ``23:59:59.999999`` that no date-only response could cover).
    """
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid month: {month}")
    start = datetime.datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end = datetime.datetime(year, 12, 31, tzinfo=tz)
    else:
        end = datetime.datetime(year, month + 1, 1, tzinfo=tz) - datetime.timedelta(
            days=1
        )
    return DateRange(start, end)


def _date_range_from_request(request: Any) -> DateRange | None:
    dtstart = getattr(request, "dtstart", None)
    dtend = getattr(request, "dtend", None)
    if dtstart is None or dtend is None:
        return None
    return DateRange(_as_utc(dtstart), _as_utc(dtend))


def _account_from_request(request: Any) -> str | None:
    return getattr(request, "acctid", None)


def _request_kind(request: Any) -> str | None:
    return type(request).__name__ if request is not None else None


def _record_from_transaction(transaction: Any) -> ReceivedRecord | None:
    """Extract a record descriptor from a bank, card, or investment item."""
    if transaction is None:
        return None

    # Investment bank transactions wrap a normal STMTTRN.
    nested = _getattr(transaction, "stmttrn")
    if nested is not None and nested is not transaction:
        return _record_from_transaction(nested)

    invtran = _getattr(transaction, "invtran")
    if invtran is not None:
        fitid = _getattr(invtran, "fitid")
        date = next(
            (
                value
                for value in (
                    _getattr(invtran, "dttrade"),
                    _getattr(invtran, "dtsettle"),
                )
                if value is not None
            ),
            None,
        )
        amount = _getattr(transaction, "total")
        trntype = type(transaction).__name__
        return ReceivedRecord(fitid=fitid, date=date, trntype=trntype, amount=amount)

    fitid = _getattr(transaction, "fitid")
    date = next(
        (
            value
            for attr in _DATE_ATTRS
            if (value := _getattr(transaction, attr)) is not None
        ),
        None,
    )
    amount = _getattr(transaction, "trnamt")
    trntype = _getattr(transaction, "trntype")
    if fitid is None and date is None:
        return None
    return ReceivedRecord(fitid=fitid, date=date, trntype=trntype, amount=amount)


def _records_from_statement(statement: Any) -> tuple[ReceivedRecord, ...]:
    if statement is None:
        return ()

    tranlist = _getattr(statement, "banktranlist")
    if tranlist is None:
        tranlist = _getattr(statement, "invtranlist")
    if tranlist is None:
        return ()

    records: list[ReceivedRecord] = []
    for transaction in tranlist:
        record = _record_from_transaction(transaction)
        if record is not None:
            records.append(record)
    return tuple(records)


def _declared_from_statement(statement: Any) -> DateRange | None:
    if statement is None:
        return None

    tranlist = _getattr(statement, "banktranlist")
    if tranlist is None:
        tranlist = _getattr(statement, "invtranlist")
    if tranlist is not None:
        dtstart = _getattr(tranlist, "dtstart")
        dtend = _getattr(tranlist, "dtend")
        if dtstart is not None and dtend is not None:
            return DateRange(dtstart, dtend)
        return None

    # Closing statement aggregates (STMTENDRS/CCSTMTENDRS) store their
    # CLOSING/CCCLOSING items directly in the aggregate's list.  Each item
    # declares the posting window it covers.
    intervals: list[DateRange] = []
    for closing in statement:
        dtstart = _getattr(closing, "dtpoststart")
        dtend = _getattr(closing, "dtpostend")
        if dtstart is not None and dtend is not None:
            intervals.append(DateRange(dtstart, dtend))
    if not intervals:
        return None
    result = intervals[0]
    for interval in intervals[1:]:
        result = result.union(interval)
    return result


def _getattr(obj: Any, name: str, default: Any = None) -> Any:
    """
    Direct attribute lookup that bypasses ``Aggregate.__getattr__``.

    Model aggregates proxy missing attributes across nested subaggregates; for
    response inspection that can recurse when probing optional wrapper fields.
    """
    try:
        return object.__getattribute__(obj, name)
    except AttributeError:
        return default


def _account_from_statement(statement: Any) -> str | None:
    account = _getattr(statement, "account")
    if account is not None:
        acctid: str | None = _getattr(account, "acctid")
        return acctid
    for attr in ("bankacctfrom", "ccacctfrom", "invacctfrom"):
        aggregate = _getattr(statement, attr)
        if aggregate is not None:
            acctid = _getattr(aggregate, "acctid")
            return acctid
    return None


def _status_from_wrapper(wrapper: Any) -> ResponseStatus | None:
    return ResponseStatus.from_model(_getattr(wrapper, "status"))


def _signon_status(ofx: Any) -> ResponseStatus | None:
    signon = _getattr(ofx, "signonmsgsrsv1")
    if signon is None:
        return None
    sonrs = _getattr(signon, "sonrs")
    return ResponseStatus.from_model(_getattr(sonrs, "status"))


def _parse_response(response: Any) -> Any:
    """Coerce raw OFX or a model aggregate into an OFX model."""
    if isinstance(response, OFX):
        return response
    if isinstance(response, (bytes, bytearray, str)):
        data = response.encode("utf-8") if isinstance(response, str) else response
        parser = OFXTree()
        parser.parse(io.BytesIO(data))
        return parser.convert()
    if hasattr(response, "read"):
        data = response.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        parser = OFXTree()
        parser.parse(io.BytesIO(data))
        return parser.convert()
    if hasattr(response, "to_etree"):
        tree = response.to_etree()
        return models.Aggregate.from_etree(tree)
    raise TypeError(f"Can't parse OFX response of type {type(response).__name__}")


def _statement_from_wrapper(wrapper: Any) -> Any:
    """Return the statement aggregate, including closing-statement responses."""
    for attr in ("stmtendrs", "ccstmtendrs", "statement"):
        value = _getattr(wrapper, attr)
        if value is not None:
            return value
    # Regular *STMTTRNRS wrappers expose a `statement` property, but fall
    # back to their concrete aggregate attribute for defensive compatibility.
    for attr in ("stmtrs", "ccstmtrs", "invstmtrs"):
        value = _getattr(wrapper, attr)
        if value is not None:
            return value
    return None


_STATEMENT_WRAPPER_TYPES = (
    STMTTRNRS,
    STMTENDTRNRS,
    CCSTMTTRNRS,
    CCSTMTENDTRNRS,
    INVSTMTTRNRS,
)


def _iter_statement_wrappers(ofx: Any) -> Iterator[tuple[Any, Any]]:
    for msgs_name in (
        "bankmsgsrsv1",
        "creditcardmsgsrsv1",
        "invstmtmsgsrsv1",
    ):
        msgs = _getattr(ofx, msgs_name)
        if not msgs:
            continue
        for wrapper in msgs:
            # *MSGSRSV1 aggregates also contain transfers, stop-checks,
            # mail, and other transaction wrappers.  Only statement wrappers
            # contribute coverage rows.
            if isinstance(wrapper, _STATEMENT_WRAPPER_TYPES):
                yield wrapper, _statement_from_wrapper(wrapper)


def _make_row(
    *,
    account: str | None,
    requested: DateRange | None,
    declared: DateRange | None,
    records: tuple[ReceivedRecord, ...] = (),
    status: ResponseStatus | None = None,
    warnings: Iterable[str] = (),
    request_kind: str | None = None,
    error: str | None = None,
    coverage_blocked: bool = False,
) -> CoverageRow:
    return CoverageRow(
        account=account,
        requested=requested,
        declared=declared,
        records=records,
        status=status or ResponseStatus.success(),
        warnings=tuple(warnings),
        request_kind=request_kind,
        error=error,
        coverage_blocked=coverage_blocked,
    )


def build_coverage_table(
    response: Any,
    requests: Iterable[Any] | Any = (),
) -> CoverageTable:
    """
    Build a coverage table from an OFX response and optional request objects.

    ``requests`` may be a single ``StmtRq``/``CcStmtRq``/``InvStmtRq`` (or
    closing equivalent) or an iterable of them.  When omitted, rows still
    report declared coverage and received records, but cannot mark a gap as
    closed because the requested range is unknown.
    """
    request_list = _coerce_requests(requests)
    ofx = _parse_response(response)

    if not isinstance(ofx, OFX):
        # A bare statement/wrapper was supplied.
        wrapper = ofx
        statement = _statement_from_wrapper(wrapper)
        if statement is None:
            statement = wrapper
        status = _status_from_wrapper(wrapper) or ResponseStatus.success()
        request = request_list[0] if len(request_list) == 1 else None
        row = _make_row(
            account=_account_from_statement(statement)
            or _account_from_request(request),
            requested=_date_range_from_request(request),
            declared=_declared_from_statement(statement),
            records=_records_from_statement(statement),
            status=status,
            warnings=(status.warning_message,) if status.warning_message else (),
            request_kind=_request_kind(request),
        )
        return CoverageTable([row])

    signon = _signon_status(ofx)
    # A SIGNON warning means the session itself was qualified (e.g. a partial
    # signon).  The response is reported in the table, but its statement data
    # is not treated as authoritative coverage.
    signon_blocking = bool(signon and (signon.is_error or signon.is_warning))
    signon_warnings = [signon.warning_message] if signon and signon.warning_message else []

    pairs = list(_iter_statement_wrappers(ofx))
    rows: list[CoverageRow] = []
    unmatched_pairs = list(pairs)

    # Match response statements to requests by account number.
    for request in request_list:
        requested = _date_range_from_request(request)
        request_account = _account_from_request(request)
        match_index: int | None = None
        for index, (_, statement) in enumerate(unmatched_pairs):
            response_account = _account_from_statement(statement)
            if (
                request_account is not None
                and response_account is not None
                and request_account == response_account
            ):
                match_index = index
                break

        # Error responses may omit the statement aggregate.  Pair those
        # accountless responses in order, but never fall back for a response
        # that explicitly names a different account.
        if match_index is None:
            for index, (_, statement) in enumerate(unmatched_pairs):
                if _account_from_statement(statement) is None:
                    match_index = index
                    break

        # A request without an account can only be paired positionally when
        # exactly one response remains.
        if (
            match_index is None
            and request_account is None
            and len(unmatched_pairs) == 1
        ):
            match_index = 0

        if match_index is None:
            status = signon if signon_blocking and signon else ResponseStatus(
                code=None,
                severity="ERROR",
                message=f"No statement response for account {request_account!r}",
            )
            rows.append(
                _make_row(
                    account=request_account,
                    requested=requested,
                    declared=None,
                    status=status,
                    warnings=signon_warnings,
                    request_kind=_request_kind(request),
                    error=None if status.is_error else status.message,
                    coverage_blocked=signon_blocking,
                )
            )
            continue

        wrapper, statement = unmatched_pairs.pop(match_index)
        trn_status = _status_from_wrapper(wrapper) or ResponseStatus.success()
        status = signon if signon_blocking and signon else trn_status
        warnings = list(signon_warnings)
        if trn_status.warning_message:
            warnings.append(trn_status.warning_message)

        rows.append(
            _make_row(
                account=_account_from_statement(statement)
                or _account_from_request(request),
                requested=requested,
                declared=_declared_from_statement(statement),
                records=_records_from_statement(statement),
                status=status,
                warnings=warnings,
                request_kind=_request_kind(request),
                coverage_blocked=signon_blocking,
            )
        )

    # Include statements that arrived without a corresponding request.
    for wrapper, statement in unmatched_pairs:
        status = _status_from_wrapper(wrapper) or ResponseStatus.success()
        rows.append(
            _make_row(
                account=_account_from_statement(statement),
                requested=None,
                declared=_declared_from_statement(statement),
                records=_records_from_statement(statement),
                status=status,
                warnings=signon_warnings
                + ([status.warning_message] if status.warning_message else []),
                coverage_blocked=signon_blocking,
            )
        )

    if not rows:
        # An error response with no statement wrapper still yields a row so
        # callers can see that the checkpoint was not advanced.
        status = signon or ResponseStatus(
            code=None, severity="ERROR", message="OFX response contained no statement"
        )
        request = request_list[0] if len(request_list) == 1 else None
        rows.append(
            _make_row(
                account=_account_from_request(request),
                requested=_date_range_from_request(request),
                declared=None,
                status=status,
                warnings=signon_warnings,
                request_kind=_request_kind(request),
                coverage_blocked=signon_blocking,
            )
        )

    return CoverageTable(rows)


def _coerce_requests(requests: Iterable[Any] | Any) -> list[Any]:
    if requests is None:
        return []
    if isinstance(requests, (str, bytes)):
        return []
    if isinstance(requests, Iterable) and not hasattr(requests, "dtstart"):
        return list(requests)
    if hasattr(requests, "dtstart"):
        return [requests]
    return []


def coverage_from_response(
    response: Any,
    requested: DateRange | Any | None = None,
    *,
    account: str | None = None,
) -> CoverageRow:
    """
    Return the single coverage row for a response.

    ``requested`` may be a request object (``StmtRq`` etc.) or a `DateRange`.
    """
    request: Any = requested
    if isinstance(requested, DateRange):
        request = None
        requested_range: DateRange | None = requested
    else:
        requested_range = _date_range_from_request(requested)

    table = build_coverage_table(response, [request] if request is not None else [])
    rows = list(table)
    if account is not None:
        rows = [row for row in rows if row.account == account]
    if len(rows) != 1:
        raise CoverageError(
            f"Expected exactly one statement row, got {len(rows)}; "
            "use build_coverage_table() for multi-statement responses"
        )
    row = rows[0]
    if requested_range is not None and row.requested is None:
        row = _make_row(
            account=row.account,
            requested=requested_range,
            declared=row.declared,
            records=row.records,
            status=row.status,
            warnings=row.warnings,
            request_kind=row.request_kind,
            error=row.error,
            coverage_blocked=row.coverage_blocked,
        )
    return row


inspect_response = coverage_from_response


class Synchronizer:
    """
    Small adapter that requests statements, records coverage, and applies the
    ledger rules.  Network use is optional; `ingest()` works on saved OFX.
    """

    def __init__(
        self,
        client: Any | None = None,
        ledger: CoverageLedger | None = None,
        *,
        account: str | None = None,
    ) -> None:
        self.client = client
        self.ledger = ledger or CoverageLedger()
        self.account = account

    def ingest(
        self,
        response: Any,
        requests: Iterable[Any] | Any = (),
    ) -> SyncBatch:
        table = build_coverage_table(response, requests)
        updates = tuple(
            self.ledger.apply(row, row.account or self.account)
            for row in table
        )
        raw = response if isinstance(response, bytes) else None
        return SyncBatch(table=table, updates=updates, response=raw)

    def request(
        self,
        password: str,
        *requests: Any,
        **kwargs: Any,
    ) -> SyncBatch:
        if self.client is None:
            raise ValueError("Synchronizer requires an OFXClient to make requests")

        try:
            with self.client.request_statements(password, *requests, **kwargs) as f:
                raw = f.read()
        except Exception as exc:  # Network/HTTP failure: no checkpoint movement.
            table = self._error_table(requests, exc)
            updates = tuple(
                self.ledger.apply(row, row.account or self.account) for row in table
            )
            return SyncBatch(table=table, updates=updates, response=None)

        result = self.ingest(raw, requests)
        return SyncBatch(
            table=result.table, updates=result.updates, response=raw
        )

    sync = request

    def request_month(
        self,
        password: str,
        request: Any,
        year: int,
        month: int,
        **kwargs: Any,
    ) -> SyncBatch:
        """Request a calendar month and build its coverage table."""
        return self.request(
            password,
            _with_date_range(request, month_range(year, month)),
            **kwargs,
        )

    @staticmethod
    def _error_table(requests: Iterable[Any], error: BaseException) -> CoverageTable:
        rows = [
            _make_row(
                account=_account_from_request(request),
                requested=_date_range_from_request(request),
                declared=None,
                status=ResponseStatus.from_error(error),
                request_kind=_request_kind(request),
            )
            for request in _coerce_requests(requests)
        ]
        if not rows:
            rows.append(
                _make_row(
                    account=None,
                    requested=None,
                    declared=None,
                    status=ResponseStatus.from_error(error),
                )
            )
        return CoverageTable(rows)


StatementSynchronizer = Synchronizer


def _with_date_range(request: Any, interval: DateRange) -> Any:
    if hasattr(request, "_replace"):
        return request._replace(dtstart=interval.start, dtend=interval.end)
    raise TypeError(f"Can't set date range on {type(request).__name__}")
