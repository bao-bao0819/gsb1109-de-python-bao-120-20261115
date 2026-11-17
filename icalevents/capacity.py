"""
Resource capacity and overload analysis for iCal booking calendars.

A resource calendar contains bookings that each declare how many units of a
resource they occupy.  Supply is reduced during maintenance windows.  This
module computes the exact intervals in which declared demand exceeds declared
supply.

Calendar conventions
--------------------

``X-RESOURCE-QUANTITY``
    Integer number of resource units a booking occupies.  A booking without
    this property has an unknown quantity; it is never treated as zero.

``X-RESOURCE-CAPACITY``
    Absolute number of units available during the event.  A ``VEVENT`` that
    carries this property is a supply declaration, not a booking.

``X-RESOURCE-CAPACITY-DELTA``
    Number of units removed from the ambient capacity during the event.  A
    ``VEVENT`` that carries this property is a maintenance window, not a
    booking.

All intervals are half-open: ``[start, end)``.  A booking that ends at 10:00
does not occupy 10:00, so back-to-back bookings do not double count.

Only opaque bookings consume capacity.  ``TRANSP:TRANSPARENT`` and
``STATUS:CANCELLED`` events are ignored.  Recurring events are expanded to
their actual instances, honouring ``EXDATE`` and ``RECURRENCE-ID`` overrides;
each surviving instance is counted separately.

Missing declarations are reported, not guessed.  An interval with demand but
no declared capacity is ``unknown`` (reason ``capacity_undeclared``).  An
interval containing a booking without a quantity is ``unknown`` (reason
``booking_quantity_undeclared``) unless the declared quantities alone already
exceed capacity, in which case the overload is real and the unknown-quantity
bookings are listed with it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo as _tzinfo
from typing import Any, Iterable, Sequence

from dateutil.rrule import rruleset
from dateutil.tz import UTC
from icalendar import Calendar

from .icaldownload import ICalDownload
from .icalparser import Event, create_event, get_timezone

QUANTITY_PROPERTY = "X-RESOURCE-QUANTITY"
CAPACITY_PROPERTY = "X-RESOURCE-CAPACITY"
CAPACITY_DELTA_PROPERTY = "X-RESOURCE-CAPACITY-DELTA"

STATE_OK = "ok"
STATE_OVERLOAD = "overload"
STATE_UNKNOWN = "unknown"

REASON_CAPACITY_UNDECLARED = "capacity_undeclared"
REASON_QUANTITY_UNDECLARED = "booking_quantity_undeclared"

__all__ = [
    "Booking",
    "BookingRef",
    "CapacityDeclarationError",
    "CapacityReport",
    "CapacitySegment",
    "IgnoredBooking",
    "SupplyPlan",
    "SupplyWindow",
    "analyze",
    "analyze_calendar",
    "analyze_capacity",
    "bookings_from_events",
    "main",
    "split_events",
    "supply_plan_from_events",
]


class CapacityDeclarationError(ValueError):
    """Raised when capacity declarations conflict or are inconsistent."""


def _property_int(component: Any, name: str) -> int | None:
    if component is None:
        return None
    value = component.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CapacityDeclarationError(
            "%s must be an integer, got %r" % (name, value)
        ) from exc


def _normalise(value: date | datetime, tz: _tzinfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=tz)
    raise TypeError("expected date or datetime, got %r" % (value,))


def _detect_timezone(values: Iterable[Any]) -> _tzinfo:
    for value in values:
        if isinstance(value, Event):
            value = value.start
        tz = getattr(value, "tzinfo", None)
        if tz is not None:
            return tz
    return UTC


def _duration(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


@dataclass(frozen=True)
class BookingRef:
    """A booking instance as it appears in a report."""

    uid: str
    summary: str | None
    quantity: int | None
    start: datetime
    end: datetime
    recurrence_id: datetime | None = None

    @property
    def label(self) -> str:
        if self.summary:
            return "%s (%s)" % (self.summary, self.uid)
        return self.uid

    @property
    def key(self) -> tuple[Any, ...]:
        return (self.uid, self.recurrence_id, self.start, self.end, self.quantity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "quantity": self.quantity,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "recurrence_id": (
                self.recurrence_id.isoformat() if self.recurrence_id else None
            ),
        }


@dataclass(frozen=True)
class Booking:
    """One concrete occurrence of a booking."""

    uid: str
    summary: str | None
    start: datetime
    end: datetime
    quantity: int | None
    transparent: bool = False
    status: str | None = None
    recurrence_id: datetime | None = None
    sequence: int | None = None
    last_modified: datetime | None = None

    def __post_init__(self) -> None:
        if self.quantity is not None:
            if isinstance(self.quantity, bool) or not isinstance(self.quantity, int):
                raise CapacityDeclarationError(
                    "quantity for %r must be an integer, got %r"
                    % (self.uid, self.quantity)
                )
            if self.quantity < 0:
                raise CapacityDeclarationError(
                    "quantity for %r must not be negative" % (self.uid,)
                )

    @property
    def cancelled(self) -> bool:
        return (self.status or "").strip().upper() == "CANCELLED"

    @property
    def consumes(self) -> bool:
        return not self.transparent and not self.cancelled

    @property
    def instance_key(self) -> tuple[Any, ...]:
        return (
            self.uid,
            self.recurrence_id or self.start,
            self.start,
            self.end,
        )

    @property
    def ref(self) -> BookingRef:
        return BookingRef(
            uid=self.uid,
            summary=self.summary,
            quantity=self.quantity,
            start=self.start,
            end=self.end,
            recurrence_id=self.recurrence_id,
        )

    @classmethod
    def from_event(cls, event: Event, tz: _tzinfo) -> Booking:
        if event.start is None or event.end is None:
            raise CapacityDeclarationError(
                "booking %r has no start or end" % (event.uid,)
            )

        quantity = _property_int(event.component, QUANTITY_PROPERTY)
        if quantity is None:
            quantity = getattr(event, "quantity", None)
        if quantity is not None:
            if isinstance(quantity, bool) or not isinstance(quantity, int):
                raise CapacityDeclarationError(
                    "%s for %r must be an integer, got %r"
                    % (QUANTITY_PROPERTY, event.uid, quantity)
                )
            if quantity < 0:
                raise CapacityDeclarationError(
                    "%s for %r must not be negative" % (QUANTITY_PROPERTY, event.uid)
                )

        sequence: int | None = None
        if event.sequence is not None:
            try:
                sequence = int(event.sequence)
            except (TypeError, ValueError):
                sequence = None

        recurrence_id = (
            _normalise(event.recurrence_id, tz) if event.recurrence_id else None
        )

        return cls(
            uid=str(event.uid),
            summary=event.summary,
            start=_normalise(event.start, tz),
            end=_normalise(event.end, tz),
            quantity=quantity,
            transparent=bool(event.transparent),
            status=event.status,
            recurrence_id=recurrence_id,
            sequence=sequence,
            last_modified=event.last_modified,
        )


@dataclass(frozen=True)
class SupplyWindow:
    """A maintenance or capacity declaration covering ``[start, end)``.

    Exactly one of ``capacity`` and ``delta`` must be set.  ``capacity`` is an
    absolute supply level; ``delta`` is subtracted from the ambient capacity.
    """

    start: date | datetime
    end: date | datetime
    capacity: int | None = None
    delta: int | None = None

    def __post_init__(self) -> None:
        if (self.capacity is None) == (self.delta is None):
            raise CapacityDeclarationError(
                "supply window needs exactly one of capacity or delta"
            )
        if self.capacity is not None and self.capacity < 0:
            raise CapacityDeclarationError("capacity must not be negative")
        if self.delta is not None and self.delta < 0:
            raise CapacityDeclarationError("delta must not be negative")


@dataclass
class SupplyPlan:
    """Ambient capacity plus maintenance windows."""

    base_capacity: int | None = None
    windows: list[SupplyWindow] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.base_capacity is not None and self.base_capacity < 0:
            raise CapacityDeclarationError("base_capacity must not be negative")

    def capacity_for(self, active: Sequence[SupplyWindow], tz: _tzinfo) -> int | None:
        absolutes = {
            window.capacity for window in active if window.capacity is not None
        }
        if len(absolutes) > 1:
            times = ", ".join(
                "%s..%s" % (
                    _normalise(window.start, tz).isoformat(),
                    _normalise(window.end, tz).isoformat(),
                )
                for window in active
                if window.capacity is not None
            )
            raise CapacityDeclarationError(
                "conflicting absolute capacities in overlapping supply windows: %s"
                % times
            )

        capacity = absolutes.pop() if absolutes else self.base_capacity
        if capacity is None:
            return None

        reduction = sum(
            window.delta for window in active if window.delta is not None
        )
        return max(0, capacity - reduction)


@dataclass(frozen=True)
class IgnoredBooking:
    """A calendar row that does not contribute demand."""

    ref: BookingRef
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"booking": self.ref.to_dict(), "reason": self.reason}


@dataclass(frozen=True)
class CapacitySegment:
    """One maximal interval with constant capacity and constant demand.

    ``sources`` lists every booking that overlaps the interval, in start order.
    ``excess_sources`` is the subset that cannot be accommodated when capacity
    is handed out in that same start order.  That ordering is a stable
    reporting convention, not a claim about which booking has priority.
    """

    start: datetime
    end: datetime
    capacity: int | None
    demand: int
    state: str
    reasons: tuple[str, ...] = ()
    sources: tuple[BookingRef, ...] = ()
    excess_sources: tuple[BookingRef, ...] = ()
    unknown_quantity_sources: tuple[BookingRef, ...] = ()

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def overload(self) -> int:
        if self.state != STATE_OVERLOAD or self.capacity is None:
            return 0
        return self.demand - self.capacity

    @property
    def overload_units(self) -> float:
        """Overload integrated over the interval, in resource-seconds."""
        return self.overload * _duration(self.start, self.end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": _duration(self.start, self.end),
            "capacity": self.capacity,
            "demand": self.demand,
            "overload": self.overload,
            "overload_units": self.overload_units,
            "state": self.state,
            "reasons": list(self.reasons),
            "sources": [source.to_dict() for source in self.sources],
            "excess_sources": [
                source.to_dict() for source in self.excess_sources
            ],
            "unknown_quantity_sources": [
                source.to_dict() for source in self.unknown_quantity_sources
            ],
        }


@dataclass
class CapacityReport:
    """Result of a capacity analysis."""

    start: datetime
    end: datetime
    segments: list[CapacitySegment] = field(default_factory=list)
    ignored: list[IgnoredBooking] = field(default_factory=list)

    @property
    def overloads(self) -> list[CapacitySegment]:
        return [
            segment
            for segment in self.segments
            if segment.state == STATE_OVERLOAD
        ]

    @property
    def unknowns(self) -> list[CapacitySegment]:
        return [segment for segment in self.segments if segment.state == STATE_UNKNOWN]

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": {
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
            },
            "overloads": [segment.to_dict() for segment in self.overloads],
            "unknowns": [segment.to_dict() for segment in self.unknowns],
            "segments": [segment.to_dict() for segment in self.segments],
            "ignored": [ignored.to_dict() for ignored in self.ignored],
        }


def _is_supply_event(event: Event) -> bool:
    component = event.component
    if component is None:
        return False
    return (
        component.get(CAPACITY_PROPERTY) is not None
        or component.get(CAPACITY_DELTA_PROPERTY) is not None
    )


def split_events(events: Iterable[Event]) -> tuple[list[Event], list[Event]]:
    """Split calendar events into bookings and supply declarations."""

    bookings: list[Event] = []
    supply: list[Event] = []
    for event in events:
        (supply if _is_supply_event(event) else bookings).append(event)
    return bookings, supply


def bookings_from_events(
    events: Iterable[Event], *, tz: _tzinfo | None = None
) -> list[Booking]:
    """Convert events to de-duplicated booking instances."""

    event_list = list(events)
    tz = tz or _detect_timezone(event_list)
    bookings, _ = _dedupe([Booking.from_event(event, tz) for event in event_list])
    return bookings


def _recency(booking: Booking) -> tuple[int, str]:
    modified = booking.last_modified
    stamp = modified.isoformat() if isinstance(modified, datetime) else ""
    return (booking.sequence if booking.sequence is not None else -1, stamp)


def _dedupe(
    bookings: Iterable[Booking],
) -> tuple[list[Booking], list[IgnoredBooking]]:
    best: dict[tuple[Any, ...], Booking] = {}
    order: list[tuple[Any, ...]] = []
    ignored: list[IgnoredBooking] = []

    for booking in bookings:
        key = booking.instance_key
        previous = best.get(key)
        if previous is None:
            best[key] = booking
            order.append(key)
            continue
        if _recency(booking) > _recency(previous):
            ignored.append(IgnoredBooking(previous.ref, "duplicate"))
            best[key] = booking
        else:
            ignored.append(IgnoredBooking(booking.ref, "duplicate"))

    return [best[key] for key in order], ignored


def supply_plan_from_events(
    events: Iterable[Event],
    *,
    base_capacity: int | None = None,
    extra_windows: Iterable[SupplyWindow] = (),
    tz: _tzinfo | None = None,
) -> SupplyPlan:
    """Build a supply plan from capacity-carrying events and extra windows."""

    event_list = list(events)
    tz = tz or _detect_timezone(event_list)
    windows = list(extra_windows)

    best: dict[tuple[Any, ...], tuple[tuple[int, str], SupplyWindow]] = {}
    order: list[tuple[Any, ...]] = []

    for event in event_list:
        if event.start is None or event.end is None:
            raise CapacityDeclarationError(
                "supply event %r has no start or end" % (event.uid,)
            )
        if event.transparent or (event.status or "").strip().upper() == "CANCELLED":
            continue
        capacity = _property_int(event.component, CAPACITY_PROPERTY)
        delta = _property_int(event.component, CAPACITY_DELTA_PROPERTY)
        if capacity is None and delta is None:
            continue
        if capacity is not None and delta is not None:
            raise CapacityDeclarationError(
                "supply event %r declares both capacity and delta" % (event.uid,)
            )
        window = SupplyWindow(
            start=_normalise(event.start, tz),
            end=_normalise(event.end, tz),
            capacity=capacity,
            delta=delta,
        )
        if window.end <= window.start:
            continue

        key = (
            event.uid,
            _instance_key(event.recurrence_id)
            if event.recurrence_id is not None
            else window.start,
            window.start,
            window.end,
            window.capacity,
            window.delta,
        )
        try:
            sequence = int(event.sequence) if event.sequence is not None else -1
        except (TypeError, ValueError):
            sequence = -1
        modified = event.last_modified
        recency = (
            sequence,
            modified.isoformat() if isinstance(modified, datetime) else "",
        )

        previous = best.get(key)
        if previous is None:
            order.append(key)
            best[key] = (recency, window)
        elif recency > previous[0]:
            best[key] = (recency, window)

    windows.extend(best[key][1] for key in order)

    return SupplyPlan(base_capacity=base_capacity, windows=windows)


def analyze_capacity(
    events: Iterable[Event],
    *,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    base_capacity: int | None = None,
    supply_windows: Iterable[SupplyWindow] = (),
    tz: _tzinfo | None = None,
) -> CapacityReport:
    """Analyse capacity from a full calendar, splitting supply events out."""

    event_list = list(events)
    tz = tz or _detect_timezone(event_list)
    booking_events, supply_events = split_events(event_list)
    bookings, ignored = _dedupe(
        [Booking.from_event(event, tz) for event in booking_events]
    )
    plan = supply_plan_from_events(
        supply_events,
        base_capacity=base_capacity,
        extra_windows=supply_windows,
        tz=tz,
    )
    return _analyze(bookings, plan, ignored, start=start, end=end, tz=tz)


def _read_content(
    *,
    file: str | None,
    url: str | None,
    string_content: bytes | str | None,
) -> str:
    download = ICalDownload()
    if url:
        return download.data_from_url(url)
    if file:
        return download.data_from_file(file)
    if string_content:
        return download.data_from_string(string_content)
    raise ValueError("no calendar source given")


def _instance_key(value: date | datetime) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC)
    return value


def _event_zone(event: Event) -> _tzinfo | None:
    component = event.component
    if component is not None:
        prop = component.get("dtstart")
        if prop is not None:
            tzid = prop.params.get("TZID")
            if tzid:
                zone = get_timezone(str(tzid))
                if zone is not None:
                    return zone
    if isinstance(event.start, datetime):
        return event.start.tzinfo
    return None


def _localize(naive: datetime, zone: _tzinfo | None) -> datetime:
    if zone is None:
        return naive
    localize = getattr(zone, "localize", None)
    if callable(localize):
        return localize(naive)
    return naive.replace(tzinfo=zone)


def expand_calendar(
    content: str,
    *,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    tz: _tzinfo | None = None,
    strict: bool = False,
) -> list[Event]:
    """Expand a calendar into concrete occurrences overlapping the window.

    Unlike :func:`icalevents.icalparser.parse_events`, instances that start
    before the window but end inside it are kept, and exclusions match the
    exact occurrence rather than the whole day.
    """

    calendar = Calendar.from_ical(content)
    events = [
        create_event(component, strict)
        for component in calendar.walk("VEVENT")
        if component.get("dtstart") is not None
    ]

    tz = tz or _detect_timezone(events) or UTC

    def duration(event: Event) -> timedelta:
        if event.start is None or event.end is None:
            return timedelta(0)
        value = event.end - event.start
        if isinstance(value, timedelta) and value > timedelta(0):
            return value
        return timedelta(0)

    lookback = max((duration(event) for event in events), default=timedelta(0))

    window_start = _normalise(start, tz) if start is not None else None
    window_end = _normalise(end, tz) if end is not None else None
    if window_start is None or window_end is None:
        fixed_starts = [
            _normalise(event.start, tz)
            for event in events
            if event.start is not None and not event.recurring
        ]
        fixed_ends = [
            _normalise(event.end, tz)
            for event in events
            if event.end is not None and not event.recurring
        ]
        derived_start = min(fixed_starts, default=datetime.now(UTC))
        derived_end = max(fixed_ends, default=derived_start + timedelta(days=7))
        window_start = window_start or derived_start
        window_end = window_end or (
            derived_end
            if derived_end > window_start
            else window_start + timedelta(days=7)
        )
    if window_end <= window_start:
        raise CapacityDeclarationError("analysis window must have a positive duration")

    overrides: dict[str, dict[Any, Event]] = {}
    for event in events:
        if event.recurrence_id is not None:
            overrides.setdefault(event.uid, {})[
                _instance_key(event.recurrence_id)
            ] = event

    found: list[Event] = []
    for event in events:
        if event.start is None:
            continue
        if event.recurrence_id is not None:
            if _overlaps(event, window_start, window_end, tz):
                found.append(event)
            continue
        if not event.recurring:
            if _overlaps(event, window_start, window_end, tz):
                found.append(event)
            continue

        rule = _parse_rrule(event)
        if rule is None:
            continue
        replacements = overrides.get(event.uid, {})
        zone = _event_zone(event)

        if isinstance(event.start, date) and not isinstance(event.start, datetime):
            first = datetime.combine((window_start - lookback).date(), time.min)
            last = datetime.combine(window_end.date(), time.max)
        else:
            rrule_zone = getattr(event.start, "tzinfo", None)
            first = (window_start - lookback).replace(tzinfo=None)
            last = window_end.replace(tzinfo=None)
            if rrule_zone is not None:
                first = first.replace(tzinfo=rrule_zone)
                last = last.replace(tzinfo=rrule_zone)

        all_day = isinstance(event.start, date) and not isinstance(
            event.start, datetime
        )
        for occurrence in rule.between(first, last, inc=True):
            local = occurrence.replace(tzinfo=None)
            if all_day:
                occurrence_key: Any = occurrence.date()
            else:
                occurrence_key = _instance_key(_localize(local, zone))
            if occurrence_key in replacements:
                continue
            if all_day:
                occurrence = occurrence.date()
            else:
                occurrence = _localize(local, zone)
            instance = event.copy_to(occurrence, event.uid)
            instance.recurring = True
            if _overlaps(instance, window_start, window_end, tz):
                found.append(instance)

    found.sort(key=lambda event: (_normalise(event.start, tz), event.uid))
    return found


def _parse_rrule(event: Event) -> rruleset | None:
    if event.component is None:
        return None
    from .icalparser import parse_rrule

    return parse_rrule(event.component)


def _overlaps(
    event: Event, window_start: datetime, window_end: datetime, tz: _tzinfo
) -> bool:
    if event.start is None or event.end is None:
        return False
    start = _normalise(event.start, tz)
    end = _normalise(event.end, tz)
    return end > window_start and start < window_end


def analyze_calendar(
    file: str | None = None,
    url: str | None = None,
    string_content: bytes | str | None = None,
    *,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    base_capacity: int | None = None,
    supply_windows: Iterable[SupplyWindow] = (),
    tz: _tzinfo | None = None,
    strict: bool = False,
) -> CapacityReport:
    """Load an iCal source and analyse its resource capacity.

    ``start`` and ``end`` are required: recurrence rules have no natural end,
    so deriving a window from the feed would silently truncate instances.
    """

    if start is None or end is None:
        raise CapacityDeclarationError(
            "analyze_calendar needs an explicit start and end"
        )

    content = _read_content(file=file, url=url, string_content=string_content)
    return analyze_capacity(
        expand_calendar(content, start=start, end=end, tz=tz, strict=strict),
        start=start,
        end=end,
        base_capacity=base_capacity,
        supply_windows=supply_windows,
        tz=tz,
    )


def analyze(
    bookings: Iterable[Booking],
    supply: SupplyPlan | None = None,
    *,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    tz: _tzinfo | None = None,
) -> CapacityReport:
    """Analyse explicit bookings against an explicit supply plan."""

    booking_list = list(bookings)
    plan = supply or SupplyPlan()
    values: list[Any] = []
    for booking in booking_list:
        values.extend([booking.start, booking.end])
    for window in plan.windows:
        values.extend([window.start, window.end])
    tz = tz or _detect_timezone(values)
    deduped, ignored = _dedupe(booking_list)
    return _analyze(deduped, plan, ignored, start=start, end=end, tz=tz)


def _analyze(
    bookings: Sequence[Booking],
    plan: SupplyPlan,
    ignored: list[IgnoredBooking],
    *,
    start: date | datetime | None,
    end: date | datetime | None,
    tz: _tzinfo,
) -> CapacityReport:
    windows = [
        SupplyWindow(
            start=_normalise(window.start, tz),
            end=_normalise(window.end, tz),
            capacity=window.capacity,
            delta=window.delta,
        )
        for window in plan.windows
    ]

    for window in windows:
        if window.end <= window.start:
            raise CapacityDeclarationError(
                "supply window must have a positive duration"
            )

    consuming: list[Booking] = []
    for booking in bookings:
        if booking.end < booking.start:
            raise CapacityDeclarationError(
                "booking %r ends before it starts" % (booking.uid,)
            )
        if booking.end == booking.start:
            ignored.append(IgnoredBooking(booking.ref, "zero_length"))
        elif not booking.consumes:
            ignored.append(
                IgnoredBooking(
                    booking.ref,
                    "transparent" if booking.transparent else "cancelled",
                )
            )
        else:
            consuming.append(booking)

    derived_start = min(
        [booking.start for booking in bookings]
        + [window.start for window in windows],
        default=None,
    )
    derived_end = max(
        [booking.end for booking in bookings] + [window.end for window in windows],
        default=None,
    )

    explicit = start is not None and end is not None
    if start is None:
        window_start = derived_start
    else:
        window_start = _normalise(start, tz)
    if end is None:
        window_end = derived_end
    else:
        window_end = _normalise(end, tz)

    if window_start is None or window_end is None:
        raise CapacityDeclarationError(
            "cannot derive an analysis window from empty input"
        )
    if window_end < window_start or (window_end == window_start and explicit):
        raise CapacityDeclarationError("analysis window must have a positive duration")
    if window_end == window_start:
        ignored.sort(key=lambda item: (item.ref.start, item.ref.uid, item.reason))
        return CapacityReport(
            start=window_start, end=window_end, segments=[], ignored=ignored
        )

    clipped: list[Booking] = []
    for booking in consuming:
        clipped_start = max(booking.start, window_start)
        clipped_end = min(booking.end, window_end)
        if clipped_end <= clipped_start:
            ignored.append(IgnoredBooking(booking.ref, "outside_window"))
            continue
        clipped.append(
            Booking(
                uid=booking.uid,
                summary=booking.summary,
                start=clipped_start,
                end=clipped_end,
                quantity=booking.quantity,
                transparent=booking.transparent,
                status=booking.status,
                recurrence_id=booking.recurrence_id,
                sequence=booking.sequence,
                last_modified=booking.last_modified,
            )
        )

    clipped_windows: list[SupplyWindow] = []
    for window in windows:
        clipped_start = max(window.start, window_start)
        clipped_end = min(window.end, window_end)
        if clipped_end <= clipped_start:
            continue
        clipped_windows.append(
            SupplyWindow(
                start=clipped_start,
                end=clipped_end,
                capacity=window.capacity,
                delta=window.delta,
            )
        )

    segments = _sweep(clipped, clipped_windows, plan, window_start, window_end, tz)
    ignored.sort(key=lambda item: (item.ref.start, item.ref.uid, item.reason))
    return CapacityReport(
        start=window_start,
        end=window_end,
        segments=segments,
        ignored=ignored,
    )


def _sweep(
    bookings: Sequence[Booking],
    windows: Sequence[SupplyWindow],
    plan: SupplyPlan,
    start: datetime,
    end: datetime,
    tz: _tzinfo,
) -> list[CapacitySegment]:
    points = {start, end}
    starts: dict[datetime, list[Booking]] = {}
    ends: dict[datetime, list[Booking]] = {}
    for booking in bookings:
        starts.setdefault(booking.start, []).append(booking)
        ends.setdefault(booking.end, []).append(booking)
        points.add(booking.start)
        points.add(booking.end)

    supplies_starting: dict[datetime, list[SupplyWindow]] = {}
    supplies_ending: dict[datetime, list[SupplyWindow]] = {}
    for window in windows:
        supplies_starting.setdefault(window.start, []).append(window)
        supplies_ending.setdefault(window.end, []).append(window)
        points.add(window.start)
        points.add(window.end)

    ordered = sorted(points)
    active_bookings: list[Booking] = []
    active_supply: list[SupplyWindow] = []
    segments: list[CapacitySegment] = []

    for index, point in enumerate(ordered[:-1]):
        for booking in ends.get(point, ()):
            active_bookings.remove(booking)
        for window in supplies_ending.get(point, ()):
            active_supply.remove(window)
        for booking in starts.get(point, ()):
            active_bookings.append(booking)
        for window in supplies_starting.get(point, ()):
            active_supply.append(window)

        next_point = ordered[index + 1]
        if next_point <= point:
            continue

        waiting = sorted(
            (booking for booking in active_bookings if booking.quantity is None),
            key=lambda booking: (booking.start, booking.uid),
        )
        known = sorted(
            (booking for booking in active_bookings if booking.quantity is not None),
            key=lambda booking: (booking.start, booking.uid),
        )
        demand = sum(booking.quantity or 0 for booking in known)
        sources = tuple(booking.ref for booking in known + waiting)
        unknown_sources = tuple(booking.ref for booking in waiting)
        capacity = plan.capacity_for(active_supply, tz)
        excess = _excess_sources(known, capacity)

        reasons: list[str] = []
        if capacity is None and active_bookings:
            reasons.append(REASON_CAPACITY_UNDECLARED)
        if waiting:
            reasons.append(REASON_QUANTITY_UNDECLARED)

        if not active_bookings:
            state = STATE_OK
        elif capacity is not None and demand > capacity:
            state = STATE_OVERLOAD
        elif capacity is None or waiting:
            state = STATE_UNKNOWN
        else:
            state = STATE_OK

        segment = CapacitySegment(
            start=point,
            end=next_point,
            capacity=capacity,
            demand=demand,
            state=state,
            reasons=tuple(reasons),
            sources=sources,
            excess_sources=excess,
            unknown_quantity_sources=unknown_sources,
        )
        segments.append(segment)

    return _merge(segments)


def _excess_sources(
    known: Sequence[Booking], capacity: int | None
) -> tuple[BookingRef, ...]:
    if capacity is None:
        return ()
    running = 0
    excess: list[BookingRef] = []
    for booking in known:
        running += booking.quantity or 0
        if running > capacity:
            excess.append(booking.ref)
    return tuple(excess)


def _merge(segments: Sequence[CapacitySegment]) -> list[CapacitySegment]:
    merged: list[CapacitySegment] = []
    for segment in segments:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        if (
            previous.end == segment.start
            and previous.capacity == segment.capacity
            and previous.demand == segment.demand
            and previous.state == segment.state
            and previous.reasons == segment.reasons
            and tuple(source.key for source in previous.sources)
            == tuple(source.key for source in segment.sources)
            and tuple(source.key for source in previous.excess_sources)
            == tuple(source.key for source in segment.excess_sources)
            and tuple(source.key for source in previous.unknown_quantity_sources)
            == tuple(source.key for source in segment.unknown_quantity_sources)
        ):
            merged[-1] = CapacitySegment(
                start=previous.start,
                end=segment.end,
                capacity=segment.capacity,
                demand=segment.demand,
                state=segment.state,
                reasons=segment.reasons,
                sources=segment.sources,
                excess_sources=segment.excess_sources,
                unknown_quantity_sources=segment.unknown_quantity_sources,
            )
        else:
            merged.append(segment)
    return merged


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "invalid ISO 8601 date or datetime: %r" % (value,)
        ) from exc


def _parse_maintenance(value: str) -> SupplyWindow:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "maintenance must be START,END,DELTA"
        )
    try:
        delta = int(parts[2])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("DELTA must be an integer") from exc
    return SupplyWindow(
        start=_parse_datetime(parts[0]),
        end=_parse_datetime(parts[1]),
        delta=delta,
    )


def _format_duration(value: timedelta) -> str:
    total = int(value.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append("%dd" % days)
    if hours:
        parts.append("%dh" % hours)
    if minutes:
        parts.append("%dm" % minutes)
    if seconds or not parts:
        parts.append("%ds" % seconds)
    return sign + "".join(parts)


def _print_report(report: CapacityReport, show_all: bool) -> None:
    print("window: %s .. %s" % (report.start.isoformat(), report.end.isoformat()))
    print("overloads: %d" % len(report.overloads))
    for segment in report.overloads:
        print(
            "  %s .. %s  demand %d > capacity %d  over %d  (%s)"
            % (
                segment.start.isoformat(),
                segment.end.isoformat(),
                segment.demand,
                segment.capacity,
                segment.overload,
                _format_duration(segment.duration),
            )
        )
        for source in segment.sources:
            print(
                "    - %s: %s .. %s, quantity %s"
                % (
                    source.label,
                    source.start.isoformat(),
                    source.end.isoformat(),
                    "unknown" if source.quantity is None else source.quantity,
                )
            )
        if segment.excess_sources:
            print(
                "    excess in start order: %s"
                % ", ".join(source.label for source in segment.excess_sources)
            )
        for source in segment.unknown_quantity_sources:
            print("    - %s: quantity unknown" % source.label)

    print("unknown: %d" % len(report.unknowns))
    for segment in report.unknowns:
        print(
            "  %s .. %s  demand %s, capacity %s  (%s)"
            % (
                segment.start.isoformat(),
                segment.end.isoformat(),
                segment.demand,
                "unknown" if segment.capacity is None else segment.capacity,
                ", ".join(segment.reasons),
            )
        )
        for source in segment.unknown_quantity_sources:
            print("    - %s: quantity unknown" % source.label)

    if show_all:
        print("segments: %d" % len(report.segments))
        for segment in report.segments:
            print(
                "  %s .. %s  %s  demand %d, capacity %s"
                % (
                    segment.start.isoformat(),
                    segment.end.isoformat(),
                    segment.state,
                    segment.demand,
                    "unknown" if segment.capacity is None else segment.capacity,
                )
            )

    if report.ignored:
        print("ignored: %d" % len(report.ignored))
        for ignored in report.ignored:
            print(
                "  %s: %s (%s .. %s)"
                % (
                    ignored.reason,
                    ignored.ref.label,
                    ignored.ref.start.isoformat(),
                    ignored.ref.end.isoformat(),
                )
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point for ``ical-capacity``."""

    parser = argparse.ArgumentParser(
        prog="ical-capacity",
        description="Report resource overload intervals from an iCal calendar.",
    )
    parser.add_argument("calendar", help="iCal file, or - to read standard input")
    parser.add_argument("--start", type=_parse_datetime, help="analysis window start")
    parser.add_argument("--end", type=_parse_datetime, help="analysis window end")
    parser.add_argument(
        "--capacity",
        type=int,
        default=None,
        help="ambient capacity when the calendar does not declare it",
    )
    parser.add_argument(
        "--maintenance",
        type=_parse_maintenance,
        action="append",
        default=[],
        metavar="START,END,DELTA",
        help="extra maintenance window removing DELTA units (repeatable)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a JSON report instead of text"
    )
    parser.add_argument(
        "--all", action="store_true", help="include non-overload segments in text"
    )
    args = parser.parse_args(argv)

    try:
        if args.calendar == "-":
            content = sys.stdin.read()
            report = analyze_calendar(
                string_content=content,
                start=args.start,
                end=args.end,
                base_capacity=args.capacity,
                supply_windows=args.maintenance,
            )
        else:
            report = analyze_calendar(
                file=args.calendar,
                start=args.start,
                end=args.end,
                base_capacity=args.capacity,
                supply_windows=args.maintenance,
            )
    except (CapacityDeclarationError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.json:
        json.dump(report.to_dict(), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _print_report(report, args.all)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
