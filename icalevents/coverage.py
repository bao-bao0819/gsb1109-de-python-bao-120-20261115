"""
Coverage status for calendar query windows.

A calendar collection can answer a query with a complete response, a partial
response (for example when a single recurrence rule cannot be parsed) or a
failed response. Only an explicitly complete coverage allows a caller to
confirm that a window contains no events.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum

# Bumped whenever the parsing/caching semantics change. Cached results keep the
# version they were produced with so stale data can be detected.
CALENDAR_DATA_VERSION = 1

Point = date | datetime
Interval = tuple[Point, Point]


def _coerce(first: Point, second: Point) -> tuple[Point, Point]:
    """
    Make two points comparable: when a date and a datetime meet, compare on
    the date component rather than raising a TypeError.
    """
    if type(first) is date and type(second) is datetime:
        return first, second.date()
    if type(first) is datetime and type(second) is date:
        return first.date(), second
    return first, second


def _max(first: Point, second: Point) -> Point:
    left, right = _coerce(first, second)
    return first if left >= right else second


def _min(first: Point, second: Point) -> Point:
    left, right = _coerce(first, second)
    return first if left <= right else second


def _max_all(points) -> Point:
    result = next(iter(points))
    for point in points:
        result = _max(result, point)
    return result


def _min_all(points) -> Point:
    result = next(iter(points))
    for point in points:
        result = _min(result, point)
    return result


def _sort_key(point: Point) -> tuple:
    """
    Deterministic ordering key that works for mixed date/datetime points.
    """
    if type(point) is datetime:
        offset = point.utcoffset()
        offset_seconds = offset.total_seconds() if offset is not None else 0
        return (
            point.year,
            point.month,
            point.day,
            point.hour,
            point.minute,
            point.second,
            point.microsecond,
            offset_seconds,
        )
    return (point.year, point.month, point.day, 0, 0, 0, 0, 0)


class CoverageStatus(str, Enum):
    """
    How much of a query window is actually covered by the collected data.
    """

    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def rank(self) -> int:
        return {
            CoverageStatus.COMPLETE: 0,
            CoverageStatus.PARTIAL: 1,
            CoverageStatus.FAILED: 2,
        }[self]


@dataclass(frozen=True)
class RecurrenceFailure:
    """
    A part of the query window that could not be evaluated.

    :param uid: UID of the affected component, None when the whole calendar failed
    :param summary: summary of the affected component (best effort)
    :param start: start of the affected range within the query window
    :param end: end of the affected range within the query window
    :param reason: human readable reason why the range could not be evaluated
    """

    uid: str | None
    summary: str | None
    start: Point
    end: Point
    reason: str


@dataclass(frozen=True)
class CoverageResult:
    """
    Coverage information for one query window.

    :param status: overall coverage status
    :param start: start of the query window
    :param end: end of the query window
    :param version: data format version the result was produced with
    :param failures: ranges that could not be evaluated
    :param stale: True when the result is served from an outdated cache entry
    :param covered_intervals: sub-ranges of the window that are fully covered
    """

    status: CoverageStatus
    start: Point
    end: Point
    version: int | None
    failures: tuple[RecurrenceFailure, ...] = ()
    stale: bool = False
    covered_intervals: tuple[Interval, ...] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        return self.status is CoverageStatus.COMPLETE

    def can_confirm_empty(self) -> bool:
        """
        Only a complete, non-stale response proves that the window has no events.
        """
        return self.status is CoverageStatus.COMPLETE and not self.stale

    def as_stale(self) -> CoverageResult:
        return replace(self, stale=True)

    @classmethod
    def merge(cls, results: list[CoverageResult]) -> CoverageResult:
        """
        Combine coverage information from several calendars or sub-queries.

        The worst status wins, failure ranges and covered intervals are joined
        and a version is only kept when every result shares the same one.
        """
        if not results:
            raise ValueError("at least one coverage result is required")

        status = max((result.status for result in results), key=lambda s: s.rank)
        failures = tuple(
            failure
            for result in results
            for failure in result.failures
        )
        covered = _union_intervals(
            interval
            for result in results
            for interval in result.covered_intervals
        )
        versions = {result.version for result in results}
        consistent_version = len(versions) == 1
        version = next(iter(versions)) if consistent_version else None
        # results produced by different data versions cannot be merged into an
        # authoritative view, so treat the merged result as stale
        stale = any(result.stale for result in results) or not consistent_version

        return cls(
            status=status,
            start=_min_all(result.start for result in results),
            end=_max_all(result.end for result in results),
            version=version,
            failures=failures,
            stale=stale,
            covered_intervals=tuple(covered),
        )


def complete_coverage(start: Point, end: Point) -> CoverageResult:
    return CoverageResult(
        status=CoverageStatus.COMPLETE,
        start=start,
        end=end,
        version=CALENDAR_DATA_VERSION,
        covered_intervals=((start, end),),
    )


def partial_coverage(
    start: Point,
    end: Point,
    failures: list[RecurrenceFailure],
) -> CoverageResult:
    clamped = _clamp_failures(start, end, failures)
    covered = tuple(_subtract_intervals((start, end), clamped))
    return CoverageResult(
        status=CoverageStatus.PARTIAL,
        start=start,
        end=end,
        version=CALENDAR_DATA_VERSION,
        failures=tuple(clamped),
        covered_intervals=covered,
    )


def failed_coverage(
    start: Point,
    end: Point,
    reason: str,
    uid: str | None = None,
    summary: str | None = None,
) -> CoverageResult:
    failure = RecurrenceFailure(
        uid=uid, summary=summary, start=start, end=end, reason=reason
    )
    return CoverageResult(
        status=CoverageStatus.FAILED,
        start=start,
        end=end,
        version=CALENDAR_DATA_VERSION,
        failures=(failure,),
        covered_intervals=(),
    )


def _clamp_failures(
    window_start: Point,
    window_end: Point,
    failures: list[RecurrenceFailure],
) -> list[RecurrenceFailure]:
    clamped: list[RecurrenceFailure] = []
    for failure in failures:
        start = _max(failure.start, window_start)
        end = _min(failure.end, window_end)
        left, right = _coerce(start, end)
        if left < right:
            clamped.append(
                RecurrenceFailure(
                    uid=failure.uid,
                    summary=failure.summary,
                    start=start,
                    end=end,
                    reason=failure.reason,
                )
            )
    return clamped


def _subtract_intervals(
    window: Interval, failures: list[RecurrenceFailure]
) -> list[Interval]:
    covered = [window]
    for failure in sorted(
        failures, key=lambda item: (_sort_key(item.start), _sort_key(item.end))
    ):
        remaining: list[Interval] = []
        for covered_start, covered_end in covered:
            end_start, start_covered = _coerce(failure.end, covered_start)
            start_end, end_covered = _coerce(failure.start, covered_end)
            if end_start <= start_covered or start_end >= end_covered:
                remaining.append((covered_start, covered_end))
                continue
            start_a, failure_start_cmp = _coerce(covered_start, failure.start)
            if start_a < failure_start_cmp:
                remaining.append((covered_start, failure.start))
            failure_end_cmp, end_b = _coerce(failure.end, covered_end)
            if failure_end_cmp < end_b:
                remaining.append((failure.end, covered_end))
        covered = remaining
    return covered


def _union_intervals(intervals) -> list[Interval]:
    ordered = sorted(
        intervals, key=lambda interval: (_sort_key(interval[0]), _sort_key(interval[1]))
    )
    merged: list[Interval] = []
    for start, end in ordered:
        if merged:
            start_cmp, last_end_cmp = _coerce(start, merged[-1][1])
            if start_cmp <= last_end_cmp:
                end_cmp, merged_end_cmp = _coerce(end, merged[-1][1])
                if end_cmp > merged_end_cmp:
                    merged[-1] = (merged[-1][0], end)
                continue
        merged.append((start, end))
    return merged
