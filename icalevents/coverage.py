"""
Count the business days actually covered by events.

Event occurrences (already expanded by :func:`icalevents.icalparser.parse_events`,
including moved recurrence exceptions) are intersected with the business
windows of a venue calendar. A business day counts when an event overlaps the
window of that day; overlapping instances of the same event (same UID) count a
day only once. Windows that run past midnight only count on the day whose
window is really touched.

Cancelled events (``STATUS:CANCELLED``) never count. Transparent events
(``TRANSP:TRANSPARENT``) do not count by default, mirroring the free/busy
semantics of :rfc:`5545`; pass ``include_transparent=True`` to count them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo as _tzinfo

from dateutil.tz import gettz

from .icalevents import events
from .icalparser import Event


@dataclass(frozen=True)
class BusinessWindow:
    """
    One business window of one business day.

    ``start`` and ``end`` are timezone-aware datetimes in the venue timezone.
    A window may cross midnight, in which case ``end`` lies on the following
    calendar day.
    """

    day: date
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("business window bounds must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("business window end must be later than start")


def resolve_timezone(tz: str | _tzinfo) -> _tzinfo:
    """Resolve a timezone name to a tzinfo object, passing tzinfo through."""
    if isinstance(tz, str):
        resolved = gettz(tz)
        if resolved is None:
            raise ValueError("unknown timezone: %s" % tz)
        return resolved
    if tz is None:
        raise ValueError("a venue timezone is required")
    return tz


def _localize(day: date, local_time: time, tz: _tzinfo) -> datetime:
    naive = datetime.combine(day, local_time)
    localize = getattr(tz, "localize", None)
    if localize is not None:  # pytz timezones must be localized explicitly
        return localize(naive)
    return naive.replace(tzinfo=tz)


def business_calendar(
    business_days: Iterable[date],
    open_time: time,
    close_time: time,
    tz: str | _tzinfo,
) -> list[BusinessWindow]:
    """
    Build the business windows of a venue calendar.

    :param business_days: dates the venue is open, each becoming one window
    :param open_time: daily opening time in the venue timezone
    :param close_time: daily closing time; a time at or before ``open_time``
        means the window runs past midnight into the following day
    :param tz: venue timezone name or tzinfo
    :return: one :class:`BusinessWindow` per business day, in input order
    """
    venue_tz = resolve_timezone(tz)
    windows: list[BusinessWindow] = []

    for day in business_days:
        start = _localize(day, open_time, venue_tz)
        end = _localize(day, close_time, venue_tz)
        if end <= start:
            end = end + timedelta(days=1)
        windows.append(BusinessWindow(day=day, start=start, end=end))

    return windows


def _event_bounds(
    event: Event, venue_tz: _tzinfo
) -> tuple[datetime, datetime] | None:
    """
    Return the event as an aware UTC-comparable interval in the venue timezone.

    All-day events (date bounds) cover the full local day; floating datetimes
    without timezone are interpreted in the venue timezone. ``None`` is
    returned for events without usable bounds.
    """
    start = event.start
    end = event.end

    if start is None or end is None:
        return None

    if isinstance(start, datetime):
        if start.tzinfo is None:
            start = _localize(start.date(), start.time(), venue_tz)
    else:  # all-day event: [day 00:00, next day 00:00) in local time
        start = _localize(start, time.min, venue_tz)

    if isinstance(end, datetime):
        if end.tzinfo is None:
            end = _localize(end.date(), end.time(), venue_tz)
    else:
        end = _localize(end, time.min, venue_tz)

    if end <= start:
        return None

    return start, end


def is_counted(event: Event, include_transparent: bool = False) -> bool:
    """
    Apply the explicit status/transparency rules.

    Cancelled events (``STATUS:CANCELLED``) never count; transparent events
    (``TRANSP:TRANSPARENT``) only count when ``include_transparent`` is set.
    """
    if event.status is not None and event.status.upper() == "CANCELLED":
        return False
    if event.transparent and not include_transparent:
        return False
    return True


def covered_business_days(
    event: Event, windows: Iterable[BusinessWindow]
) -> set[date]:
    """
    Return the business days whose windows an event really overlaps.

    Intervals are treated as half-open: an event that only touches a window
    boundary does not cover that day. Overnight events may intersect windows
    labelled with different days.
    """
    window_list = list(windows)
    if not window_list:
        return set()

    venue_tz = window_list[0].start.tzinfo
    bounds = _event_bounds(event, venue_tz)
    if bounds is None:
        return set()

    start, end = bounds
    covered: set[date] = set()
    for window in window_list:
        if start < window.end and end > window.start:
            covered.add(window.day)

    return covered


def coverage_by_uid(
    events: Iterable[Event],
    windows: Iterable[BusinessWindow],
    include_transparent: bool = False,
) -> dict[str, int]:
    """
    Count the distinct business days covered per event UID.

    Overlapping occurrences of the same event (e.g. a moved recurrence
    exception next to another instance) count a business day only once.

    :param events: expanded event occurrences, e.g. from
        :func:`icalevents.icalevents.events`
    :param windows: business windows of the venue calendar
    :param include_transparent: also count transparent events
    :return: mapping of UID to number of covered business days
    """
    window_list = list(windows)
    days_by_uid: dict[str, set[date]] = {}

    for event in events:
        if not is_counted(event, include_transparent):
            continue
        covered = covered_business_days(event, window_list)
        if not covered:
            continue
        days_by_uid.setdefault(event.uid, set()).update(covered)

    return {uid: len(days) for uid, days in days_by_uid.items()}


def count_covered_business_days(
    events: Iterable[Event],
    windows: Iterable[BusinessWindow],
    include_transparent: bool = False,
) -> int:
    """
    Count the business days covered across all events.

    Days covered by several different events are counted once. Use
    :func:`coverage_by_uid` for per-activity (per-UID) counts.
    """
    window_list = list(windows)
    all_days: set[date] = set()

    for event in events:
        if not is_counted(event, include_transparent):
            continue
        all_days.update(covered_business_days(event, window_list))

    return len(all_days)


def event_coverage(
    business_days: Iterable[date],
    open_time: time,
    close_time: time,
    tz: str | _tzinfo,
    start: datetime,
    end: datetime,
    url: str | None = None,
    file: str | None = None,
    string_content: bytes | str | None = None,
    include_transparent: bool = False,
) -> dict[str, int]:
    """
    One-shot monthly report: expand the event source and count covered days.

    The event source (file, URL or raw iCal content) is expanded in the
    reporting range, moved recurrence exceptions are applied, and each
    activity UID is intersected with the venue business windows.

    :param business_days: open dates of the venue calendar
    :param open_time: daily opening time in the venue timezone
    :param close_time: daily closing time (earlier than opening means overnight)
    :param tz: venue timezone name or tzinfo
    :param start: reporting range start
    :param end: reporting range end
    :param url: iCal URL
    :param file: iCal file path
    :param string_content: raw iCal content
    :param include_transparent: also count transparent events
    :return: mapping of UID to number of covered business days
    """
    windows = business_calendar(business_days, open_time, close_time, tz)
    parsed = events(
        url=url,
        file=file,
        string_content=string_content,
        start=start,
        end=end,
        tzinfo=resolve_timezone(tz),
        strict=True,
    )
    return coverage_by_uid(parsed, windows, include_transparent)
