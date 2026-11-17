"""
Resource occupancy for calendar bookings with per-resource buffers.

Bookings are calendar events.  A recurring event is first expanded into its
actual occurrences; each occurrence is then treated as an independent booking.
Every resource may reserve preparation time before a booking and cleanup time
after it.  These buffers are configured per resource, for example::

    rules = {
        "studio": ResourceRule(preparation=timedelta(minutes=15),
                               cleanup=timedelta(minutes=10)),
        "van": (timedelta(minutes=5), timedelta()),
    }
    report = daily_occupancy(events, rules, start=date(2024, 5, 1),
                             end=date(2024, 5, 8), resource_of="location")

The report contains, for each resource and calendar day, the union of the
buffered occupancy intervals, the original meeting time and the buffer
increment (the part of the union not covered by a meeting).  Buffers that
overlap each other are counted once.  A ``CANCELLED`` occurrence contributes no
occupancy.  Buffers crossing midnight are split and accounted to the calendar
date on which they actually fall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Callable, Iterable, Mapping, Sequence

from dateutil.tz import UTC

from icalevents.icalparser import Event

Interval = tuple[datetime, datetime]
ResourceInput = str | Callable[[Event], str] | Mapping[str, str] | None

__all__ = [
    "BufferedInstance",
    "BufferRule",
    "DayInstance",
    "DayOccupancy",
    "OccupancyInterval",
    "OccupancyReport",
    "ResourceBuffer",
    "ResourceInput",
    "ResourceRule",
    "calendar_occupancy",
    "daily_occupancy",
    "daily_totals",
    "expand_instances",
    "expand_occurrences",
    "merge_intervals",
    "normalize_rules",
    "occupancy",
    "occupancy_report",
]


class ResourceRule:
    """
    Preparation and cleanup time reserved for one resource.

    Both ``ResourceRule("studio", 15, 10)`` and ``ResourceRule(15, 10)`` are
    accepted; plain numbers are minutes.  ``before``/``after``, ``setup``/
    ``teardown`` and ``prep``/``clean`` are accepted as keyword aliases for
    ``preparation``/``cleanup``.
    """

    __slots__ = ("resource", "preparation", "cleanup")

    def __init__(
        self,
        resource: str | timedelta | int | float | None = None,
        preparation: timedelta | int | float | None = None,
        cleanup: timedelta | int | float | None = None,
        *,
        before: timedelta | int | float | None = None,
        after: timedelta | int | float | None = None,
        setup: timedelta | int | float | None = None,
        teardown: timedelta | int | float | None = None,
        prep: timedelta | int | float | None = None,
        clean: timedelta | int | float | None = None,
    ) -> None:
        if isinstance(resource, (timedelta, int, float)):
            # Called as ResourceRule(preparation, cleanup).
            if isinstance(preparation, (timedelta, int, float)):
                cleanup = preparation
            preparation = resource
            resource = None
        elif isinstance(preparation, (tuple, list)) and cleanup is None:
            # Called as ResourceRule("studio", (preparation, cleanup)).
            if len(preparation) != 2:
                raise ValueError("a buffer tuple must be (preparation, cleanup)")
            cleanup = preparation[1]
            preparation = preparation[0]

        self.resource = str(resource) if resource is not None else None

        before = _first_not_none(before, setup, prep, preparation)
        after = _first_not_none(after, teardown, clean, cleanup)

        self.preparation = _as_duration(before, "preparation")
        self.cleanup = _as_duration(after, "cleanup")

        if self.preparation < timedelta():
            raise ValueError("preparation must not be negative")
        if self.cleanup < timedelta():
            raise ValueError("cleanup must not be negative")

    @property
    def before(self) -> timedelta:
        """Alias for :attr:`preparation`."""
        return self.preparation

    @property
    def after(self) -> timedelta:
        """Alias for :attr:`cleanup`."""
        return self.cleanup

    def __repr__(self) -> str:
        return "ResourceRule(resource=%r, preparation=%r, cleanup=%r)" % (
            self.resource,
            self.preparation,
            self.cleanup,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResourceRule):
            return NotImplemented
        return (self.resource, self.preparation, self.cleanup) == (
            other.resource,
            other.preparation,
            other.cleanup,
        )

    def __hash__(self) -> int:
        return hash((self.resource, self.preparation, self.cleanup))


#: Alternative name for :class:`ResourceRule`.
ResourceBuffer = ResourceRule
BufferRule = ResourceRule


def _first_not_none(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _attr(obj: object, name: str, default: object = None) -> object:
    """Read an attribute, tolerating event-like objects without all fields."""
    return getattr(obj, name, default)


def _as_duration(value: object, name: str) -> timedelta:
    """
    Convert a duration to :class:`datetime.timedelta`.

    Accepts a timedelta, a number of minutes, an ISO 8601 duration
    (``PT30M``), ``HH:MM[:SS]``, or a short form such as ``90m``, ``1h30m``
    or ``1.5h``.
    """
    if value is None:
        return timedelta()
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return timedelta(minutes=value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return timedelta()

        try:
            from icalendar.prop import vDuration

            return vDuration.from_ical(text)
        except (ValueError, TypeError):
            pass

        try:
            return timedelta(minutes=float(text))
        except ValueError:
            pass

        if ":" in text and not text.upper().startswith("P"):
            parts = text.split(":")
            if 2 <= len(parts) <= 3:
                try:
                    numbers = [float(part) for part in parts]
                except ValueError:
                    numbers = []
                if numbers:
                    hours, minutes = numbers[0], numbers[1]
                    seconds = numbers[2] if len(numbers) == 3 else 0
                    sign = -1 if text.startswith("-") else 1
                    return sign * timedelta(
                        hours=abs(hours), minutes=minutes, seconds=seconds
                    )

        import re

        match = re.fullmatch(
            r"(?P<sign>[+-])?\s*"
            r"(?:(?P<hours>\d+(?:\.\d+)?)\s*h)?\s*"
            r"(?:(?P<minutes>\d+(?:\.\d+)?)\s*m)?\s*"
            r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*s)?\s*",
            text,
            re.IGNORECASE,
        )
        if match and any(match.group(name) for name in ("hours", "minutes", "seconds")):
            duration = timedelta(
                hours=float(match.group("hours") or 0),
                minutes=float(match.group("minutes") or 0),
                seconds=float(match.group("seconds") or 0),
            )
            return -duration if match.group("sign") == "-" else duration

    raise TypeError(
        "%s must be a timedelta, a number of minutes or a duration string" % name
    )


def _mapping_rule(value: Mapping[str, Any], resource: str | None) -> ResourceRule:
    def lookup(*names: str) -> object:
        return _first_not_none(*(value.get(name) for name in names))

    preparation = lookup(
        "preparation",
        "setup",
        "before",
        "prep",
        "setup_time",
        "prep_time",
        "prepare",
    )
    cleanup = lookup(
        "cleanup",
        "teardown",
        "after",
        "clean",
        "clear",
        "clearance",
        "reset",
        "cleanup_time",
        "clean_time",
        "teardown_time",
        "turnaround",
    )

    if preparation is None:
        preparation = lookup(
            "preparation_minutes",
            "setup_minutes",
            "before_minutes",
            "prep_minutes",
        )
        if preparation is not None:
            preparation = timedelta(minutes=float(preparation))
    if cleanup is None:
        cleanup = lookup(
            "cleanup_minutes",
            "teardown_minutes",
            "after_minutes",
            "clean_minutes",
        )
        if cleanup is not None:
            cleanup = timedelta(minutes=float(cleanup))

    return ResourceRule(resource, preparation, cleanup)


def _rule_from_value(value: object, resource: str | None = None) -> ResourceRule:
    if isinstance(value, ResourceRule):
        if value.resource is None and resource is not None:
            return ResourceRule(resource, value.preparation, value.cleanup)
        return value
    if isinstance(value, Mapping):
        return _mapping_rule(value, resource)
    if isinstance(value, (tuple, list)):
        if len(value) != 2:
            raise ValueError("a buffer tuple must be (preparation, cleanup)")
        return ResourceRule(resource, value[0], value[1])
    return ResourceRule(resource, value, value)


def normalize_rules(
    rules: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    default_rule: object | None = None,
) -> tuple[dict[str, ResourceRule], ResourceRule]:
    """
    Normalize a per-resource buffer configuration.

    :param rules: mapping of resource name to a rule, ``(prep, cleanup)`` pair,
        mapping, or duration; or an iterable of named :class:`ResourceRule`
    :param default_rule: buffers for resources without an explicit rule
    :return: ``(rules_by_resource, default_rule)``
    """
    normalized: dict[str, ResourceRule] = {}

    if isinstance(rules, Mapping):
        for resource, value in rules.items():
            normalized[str(resource)] = _rule_from_value(value, str(resource))
    elif rules is not None:
        for rule in rules:
            if isinstance(rule, ResourceRule):
                if rule.resource is None:
                    raise TypeError("a ResourceRule in a sequence needs a resource")
                normalized[rule.resource] = rule
            elif isinstance(rule, Mapping) and rule.get("resource"):
                resource = str(rule["resource"])
                normalized[resource] = _rule_from_value(rule, resource)
            else:
                raise TypeError(
                    "a rule sequence must contain named ResourceRule or mapping values"
                )

    default = ResourceRule() if default_rule is None else _rule_from_value(default_rule)
    return normalized, default


def _tz_for(event: Event, fallback: tzinfo | None) -> tzinfo | None:
    start = _attr(event, "start")
    if isinstance(start, datetime) and start.tzinfo is not None:
        return start.tzinfo
    return fallback


def _localize(day: date, tz: tzinfo | None) -> datetime:
    """Midnight at the start of ``day``; naive when ``tz`` is None."""
    naive = datetime.combine(day, time.min)
    if tz is None:
        return naive
    localize = getattr(tz, "localize", None)
    if callable(localize):
        try:
            return localize(naive)
        except (ValueError, OverflowError):
            pass
    return naive.replace(tzinfo=tz)


def _day_bounds(day: date, tz: tzinfo | None) -> Interval:
    return _localize(day, tz), _localize(day + timedelta(days=1), tz)


def _is_date_only(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _as_date(value: object) -> object:
    """Coerce an ISO date/datetime string or datetime to a date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        text = value.strip()
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return value
    return value


def _event_bounds(event: Event, tz: tzinfo | None) -> Interval | None:
    """
    Meeting interval of an occurrence.

    All-day events keep their calendar dates.  When ``tz`` is given, timed
    values are converted to it and naive values are interpreted in it; when it
    is None, naive values stay naive.
    """
    start, end = _attr(event, "start"), _attr(event, "end")
    if start is None or end is None:
        return None

    if _attr(event, "all_day", False) or _is_date_only(start) or _is_date_only(end):
        start_day = start if _is_date_only(start) else start.date()  # type: ignore[union-attr]
        end_day = end if _is_date_only(end) else end.date()  # type: ignore[union-attr]
        if end_day < start_day:
            start_day, end_day = end_day, start_day
        return _localize(start_day, tz), _localize(end_day, tz)

    if tz is not None:
        start = start if start.tzinfo is None else start.astimezone(tz)
        end = end if end.tzinfo is None else end.astimezone(tz)
        start = start.replace(tzinfo=tz)
        end = end.replace(tzinfo=tz)
    if end < start:
        start, end = end, start
    return start, end


def _is_cancelled(event: Event) -> bool:
    status = _attr(event, "status")
    return str(status or "").strip().upper() in {"CANCELLED", "CANCELED"}


def _recurrence_key(value: object, tz: tzinfo | None = None) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz or UTC)
        return value.astimezone(UTC)
    return value


def _instance_key(event: Event, tz: tzinfo | None = None) -> tuple[object, ...]:
    recurrence = _attr(event, "recurrence_id")
    if recurrence is None:
        recurrence = _attr(event, "start")
    if _attr(event, "all_day", False) and isinstance(recurrence, datetime):
        recurrence = recurrence.date()
    return _attr(event, "uid", ""), _recurrence_key(recurrence, tz)


def _select_occurrences(events: Sequence[Event], tz: tzinfo | None) -> list[Event]:
    """
    Expand-independent cleanup: drop cancelled and duplicate occurrences.

    An override cancels a generated occurrence through its ``RECURRENCE-ID``.
    A cancelled recurring master suppresses all instances with that UID.  This
    protects the report if the parser returns both a master instance and its
    override.
    """
    events = list(events)

    cancelled_keys = {
        _instance_key(event, tz) for event in events if _is_cancelled(event)
    }
    cancelled_uids = {
        _attr(event, "uid", "")
        for event in events
        if _is_cancelled(event)
        and _attr(event, "recurring", False)
        and _attr(event, "recurrence_id") is None
    }
    replaced_keys = {
        _instance_key(event, tz)
        for event in events
        if _attr(event, "recurrence_id") is not None
    }

    selected: list[Event] = []
    seen: set[tuple[object, ...]] = set()

    for event in events:
        if (
            _attr(event, "start") is None
            or _attr(event, "end") is None
            or _is_cancelled(event)
        ):
            continue
        if (
            _attr(event, "uid", "") in cancelled_uids
            or _instance_key(event, tz) in cancelled_keys
        ):
            continue
        if (
            _attr(event, "recurrence_id") is None
            and _instance_key(event, tz) in replaced_keys
        ):
            # A rescheduled or otherwise replaced occurrence; keep only the
            # override so the original slot is not counted twice.
            continue

        bounds = _event_bounds(event, tz)
        if bounds is None:
            continue

        # The same occurrence can be present both as the generated instance and
        # as an override; identify it by UID and actual time, not by whether
        # RECURRENCE-ID happened to be set.
        key = (_attr(event, "uid", ""), bounds[0], bounds[1])
        if key in seen:
            continue
        seen.add(key)
        selected.append(event)

    def sort_key(event: Event) -> tuple[int, float]:
        bounds = _event_bounds(event, tz)
        if bounds is None:
            return (2, 0.0)
        start = bounds[0]
        if start.tzinfo is None:
            return (1, start.timestamp())
        return (0, start.timestamp())

    selected.sort(key=sort_key)
    return selected


def _default_resource(event: Event) -> str:
    resource = _attr(event, "resource")
    if resource:
        return str(resource)
    location = _attr(event, "location")
    if location:
        return str(location)
    categories = [value for value in (_attr(event, "categories") or []) if value]
    if categories:
        return str(categories[0])
    return str(_attr(event, "summary") or "default")


def _resource_selector(
    resource_of: ResourceInput,
    resource: str | None = None,
) -> Callable[[Event], str]:
    if resource is not None:
        return lambda event: resource
    if callable(resource_of):
        return resource_of
    if isinstance(resource_of, Mapping):
        mapping = resource_of

        def select_from_mapping(event: Event) -> str:
            for candidate in (
                _attr(event, "resource"),
                _attr(event, "location"),
                _attr(event, "summary"),
            ):
                if candidate and str(candidate) in mapping:
                    return str(mapping[str(candidate)])
            return _default_resource(event)

        return select_from_mapping
    if isinstance(resource_of, str):

        def select(event: Event) -> str:
            value = _attr(event, resource_of)
            return str(value) if value else _default_resource(event)

        return select
    return _default_resource


def _overlap(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> timedelta:
    start = max(start_a, start_b)
    end = min(end_a, end_b)
    return end - start if end > start else timedelta()


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """
    Union of half-open intervals, sorted and non-overlapping.

    Touching intervals (one ends when the next begins) are merged.  The total
    duration is the same either way; merging keeps the union explicit.
    """
    merged: list[Interval] = []
    for start, end in sorted((start, end) for start, end in intervals if start < end):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _sum_intervals(intervals: Iterable[Interval]) -> timedelta:
    return sum((end - start for start, end in intervals), timedelta())


@dataclass(frozen=True)
class BufferedInstance:
    """
    One actual booking occurrence with its resource and buffers.

    ``occupied_start``/``occupied_end`` span preparation, meeting and cleanup.
    ``preparation_start``/``cleanup_end`` expose the buffer boundaries.
    """

    event: Event
    resource: str
    meeting_start: datetime
    meeting_end: datetime
    preparation_start: datetime
    cleanup_end: datetime
    rule: ResourceRule = field(default_factory=ResourceRule, repr=False)

    @property
    def occupied_start(self) -> datetime:
        return self.preparation_start

    @property
    def occupied_end(self) -> datetime:
        return self.cleanup_end

    @property
    def preparation(self) -> timedelta:
        return self.meeting_start - self.preparation_start

    @property
    def cleanup(self) -> timedelta:
        return self.cleanup_end - self.meeting_end

    @property
    def meeting_duration(self) -> timedelta:
        return self.meeting_end - self.meeting_start

    @property
    def occupied_duration(self) -> timedelta:
        return self.cleanup_end - self.preparation_start

    @property
    def buffer_duration(self) -> timedelta:
        return self.preparation + self.cleanup

    @property
    def uid(self) -> str:
        return str(_attr(self.event, "uid", ""))

    @property
    def summary(self) -> str | None:
        value = _attr(self.event, "summary")
        return value if value is None else str(value)


def expand_instances(
    events: Sequence[Event],
    rules: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    resource_of: ResourceInput = None,
    tz: tzinfo | None = None,
    default_rule: object | None = None,
    *,
    resource: str | None = None,
    buffers: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    config: Mapping[str, object] | Iterable[ResourceRule] | None = None,
) -> list[BufferedInstance]:
    """
    Expand calendar occurrences and attach each one's buffered window.

    Recurring events must already have been expanded (see
    :func:`icalevents.icalevents.events`); this function handles cancellation,
    duplicate overrides and all-day values, and never merges occurrences.

    :param events: parsed occurrences
    :param rules: preparation/cleanup per resource
    :param resource_of: callable or event attribute name selecting the resource;
        defaults to ``event.resource``, then location, first category, summary
    :param tz: timezone for naive values and all-day boundaries; defaults to
        each event's own timezone, leaving naive values naive
    :param default_rule: buffers for resources without an explicit rule
    :param resource: force all occurrences onto this resource
    :param buffers: alias for ``rules``
    :param config: alias for ``rules``
    :return: one :class:`BufferedInstance` per actual occurrence
    """
    if rules is None:
        rules = buffers if buffers is not None else config
    elif buffers is not None or config is not None:
        raise TypeError("use rules, buffers or config, not several")

    normalized, default = normalize_rules(rules, default_rule)
    selector = _resource_selector(resource_of, resource)

    instances: list[BufferedInstance] = []
    for event in _select_occurrences(events, tz):
        event_tz = tz if tz is not None else _tz_for(event, None)
        meeting = _event_bounds(event, event_tz)
        if meeting is None:
            continue
        resource = selector(event)
        rule = normalized.get(resource, default)
        instances.append(
            BufferedInstance(
                event=event,
                resource=resource,
                meeting_start=meeting[0],
                meeting_end=meeting[1],
                preparation_start=meeting[0] - rule.preparation,
                cleanup_end=meeting[1] + rule.cleanup,
                rule=rule,
            )
        )
    return instances


#: Alias for :func:`expand_instances`.
expand_occurrences = expand_instances


@dataclass(frozen=True)
class DayInstance:
    """One buffered occurrence as it falls on one calendar day."""

    instance: BufferedInstance
    day: date
    occupied_start: datetime
    occupied_end: datetime
    meeting: timedelta
    preparation: timedelta
    cleanup: timedelta

    @property
    def event(self) -> Event:
        return self.instance.event

    @property
    def resource(self) -> str:
        return self.instance.resource

    @property
    def meeting_duration(self) -> timedelta:
        return self.meeting

    @property
    def full_meeting_duration(self) -> timedelta:
        """Original meeting duration of the occurrence, before day clipping."""
        return self.instance.meeting_duration

    @property
    def preparation_duration(self) -> timedelta:
        return self.preparation

    @property
    def cleanup_duration(self) -> timedelta:
        return self.cleanup

    @property
    def buffer_duration(self) -> timedelta:
        """This occurrence's own buffers on this day, before union merging."""
        return self.preparation + self.cleanup

    @property
    def total_duration(self) -> timedelta:
        return self.meeting + self.buffer_duration

    @property
    def summary(self) -> str | None:
        return self.instance.summary

    @property
    def uid(self) -> str:
        return self.instance.uid

    def to_dict(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "summary": self.summary,
            "status": _attr(self.event, "status"),
            "meeting_start": self.instance.meeting_start.isoformat(),
            "meeting_end": self.instance.meeting_end.isoformat(),
            "meeting_seconds": self.meeting.total_seconds(),
            "full_meeting_seconds": self.full_meeting_duration.total_seconds(),
            "meeting_on_day_seconds": self.meeting.total_seconds(),
            "preparation_seconds": self.preparation.total_seconds(),
            "cleanup_seconds": self.cleanup.total_seconds(),
            "buffer_seconds": self.buffer_duration.total_seconds(),
        }


@dataclass(frozen=True)
class OccupancyInterval:
    """One merged occupancy interval clipped to a calendar day."""

    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


@dataclass
class DayOccupancy:
    """
    Daily occupancy union for one resource.

    ``meeting_duration`` is the original meeting time on this date, merged so
    that double-booked meetings are not counted twice.  Together with
    ``buffer_increment`` it reconciles to ``occupied_duration``.
    ``raw_meeting_duration`` keeps the unmerged sum for callers that want to
    see double booking.
    """

    resource: str
    day: date
    segments: list[OccupancyInterval]
    instances: list[DayInstance]
    meeting_duration: timedelta
    meeting_union_duration: timedelta
    buffer_increment: timedelta
    raw_buffer_duration: timedelta = timedelta()
    raw_meeting_duration: timedelta = timedelta()
    full_meeting_duration: timedelta = timedelta()

    @property
    def date(self) -> date:
        return self.day

    @property
    def occupied_duration(self) -> timedelta:
        """Length of the merged daily occupancy union."""
        return _sum_intervals((segment.start, segment.end) for segment in self.segments)

    @property
    def total_duration(self) -> timedelta:
        """Alias for :attr:`occupied_duration`."""
        return self.occupied_duration

    @property
    def original_duration(self) -> timedelta:
        """Original meeting time on this date (before adding buffers)."""
        return self.meeting_duration

    @property
    def original_meeting_duration(self) -> timedelta:
        return self.meeting_duration

    @property
    def raw_meeting_total(self) -> timedelta:
        """Unmerged sum of per-occurrence meeting time on this date."""
        return self.raw_meeting_duration

    @property
    def buffer_duration(self) -> timedelta:
        """Alias for :attr:`buffer_increment` after union merging."""
        return self.buffer_increment

    @property
    def intervals(self) -> list[Interval]:
        return [(segment.start, segment.end) for segment in self.segments]

    @property
    def events(self) -> list[Event]:
        return [item.event for item in self.instances]

    @property
    def occurrences(self) -> list[DayInstance]:
        return self.instances

    def touches(self, moment: datetime) -> bool:
        return any(segment.contains(moment) for segment in self.segments)

    def __getitem__(self, key: str) -> object:
        data = self.to_dict()
        if key not in data:
            raise KeyError(key)
        return data[key]

    def keys(self) -> list[str]:
        return list(self.to_dict())

    def as_dict(self) -> dict[str, object]:
        """Alias for :meth:`to_dict`."""
        return self.to_dict()

    def to_dict(self) -> dict[str, object]:
        return {
            "resource": self.resource,
            "date": self.day.isoformat(),
            "intervals": [
                {"start": segment.start.isoformat(), "end": segment.end.isoformat()}
                for segment in self.segments
            ],
            "occupied_seconds": self.occupied_duration.total_seconds(),
            "total_seconds": self.occupied_duration.total_seconds(),
            "meeting_seconds": self.meeting_duration.total_seconds(),
            "meeting_on_day_seconds": self.meeting_duration.total_seconds(),
            "original_meeting_seconds": self.meeting_duration.total_seconds(),
            "raw_meeting_seconds": self.raw_meeting_duration.total_seconds(),
            "full_meeting_seconds": self.full_meeting_duration.total_seconds(),
            "meeting_union_seconds": self.meeting_union_duration.total_seconds(),
            "buffer_increment_seconds": self.buffer_increment.total_seconds(),
            "raw_buffer_seconds": self.raw_buffer_duration.total_seconds(),
            "occurrences": [item.to_dict() for item in self.instances],
        }


@dataclass
class OccupancyReport:
    """Daily occupancy rows for all resources."""

    days: list[DayOccupancy] = field(default_factory=list)

    @property
    def rows(self) -> list[DayOccupancy]:
        return self.days

    def __iter__(self):
        return iter(self.days)

    def __len__(self) -> int:
        return len(self.days)

    def __getitem__(self, index):
        return self.days[index]

    def __bool__(self) -> bool:
        return bool(self.days)

    @property
    def resources(self) -> list[str]:
        return sorted({row.resource for row in self.days})

    @property
    def dates(self) -> list[date]:
        return sorted({row.day for row in self.days})

    def for_resource(self, resource: str) -> list[DayOccupancy]:
        return [row for row in self.days if row.resource == resource]

    def for_day(self, day: date) -> list[DayOccupancy]:
        return [row for row in self.days if row.day == day]

    def totals_by_day(self) -> dict[date, timedelta]:
        """Merged occupancy per date, summed across resources."""
        totals: dict[date, timedelta] = {}
        for row in self.days:
            totals[row.day] = totals.get(row.day, timedelta()) + row.occupied_duration
        return totals

    def to_dict(self) -> dict[str, object]:
        return {
            "resources": self.resources,
            "days": [row.to_dict() for row in self.days],
            "totals_by_day": {
                day.isoformat(): duration.total_seconds()
                for day, duration in sorted(self.totals_by_day().items())
            },
        }

    def to_rows(self) -> list[dict[str, object]]:
        """JSON-friendly rows, one per resource and date."""
        return [row.to_dict() for row in self.days]


def daily_occupancy(
    events: Sequence[Event],
    rules: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    start: date | None = None,
    end: date | None = None,
    resource_of: ResourceInput = None,
    tz: tzinfo | None = None,
    default_rule: object | None = None,
    *,
    resource: str | None = None,
    buffers: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    config: Mapping[str, object] | Iterable[ResourceRule] | None = None,
) -> OccupancyReport:
    """
    Compute the daily occupancy union per resource.

    :param events: parsed occurrences (recurring events already expanded)
    :param rules: preparation/cleanup per resource.  Values may be
        :class:`ResourceRule`, ``(preparation, cleanup)`` tuples, mappings, or
        durations; numbers are minutes.
    :param start: first calendar date to report (inclusive); defaults to the
        first date touched by a buffered occurrence
    :param end: last calendar date to report (inclusive); defaults to the last
        date touched by a buffered occurrence
    :param resource_of: callable or event attribute name selecting the
        resource; defaults to ``resource``, location, first category, summary
    :param tz: timezone defining calendar dates; defaults to the timezone of
        the first timed event so every row shares one calendar, leaving fully
        naive input naive
    :param default_rule: buffers for resources without an explicit rule
    :param resource: force all occurrences onto this resource
    :param buffers: alias for ``rules``
    :param config: alias for ``rules``
    :return: an :class:`OccupancyReport` with one row per resource and date
    :raises ValueError: if ``end`` is before ``start``
    """
    if rules is None:
        rules = buffers if buffers is not None else config
    elif buffers is not None or config is not None:
        raise TypeError("use rules, buffers or config, not several")

    events = list(events)
    start = _as_date(start)
    end = _as_date(end)
    if start is not None and end is not None and end < start:
        raise ValueError("end must not be before start")

    # Use one timezone for all report boundaries so that resources and dates
    # stay comparable even when a strict parse preserved per-event zones.
    report_tz = tz
    if report_tz is None:
        for event in events:
            event_start = _attr(event, "start")
            if (
                isinstance(event_start, datetime)
                and event_start.tzinfo is not None
                and not _attr(event, "all_day", False)
            ):
                report_tz = event_start.tzinfo
                break

    instances = expand_instances(
        events,
        rules,
        resource_of=resource_of,
        tz=report_tz,
        default_rule=default_rule,
        resource=resource,
    )
    base_tz = report_tz

    touched = [(item.occupied_start, item.occupied_end) for item in instances]
    if start is None:
        start = min((interval[0].date() for interval in touched), default=date.today())
    if end is None:
        end = max(
            ((interval[1] - timedelta(microseconds=1)).date() for interval in touched),
            default=start,
        )

    grouped: dict[tuple[str, date], list[BufferedInstance]] = {}
    for instance in instances:
        first = max(instance.occupied_start.date(), start)
        last_candidate = (instance.occupied_end - timedelta(microseconds=1)).date()
        last = min(last_candidate, end)
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            day_start, day_end = _day_bounds(
                day, instance.occupied_start.tzinfo or base_tz
            )
            if max(instance.occupied_start, day_start) < min(
                instance.occupied_end, day_end
            ):
                grouped.setdefault((instance.resource, day), []).append(instance)

    days: list[DayOccupancy] = []

    for resource, day in sorted(grouped, key=lambda key: (key[1], key[0])):
        entries = grouped[(resource, day)]
        reference_tz = entries[0].occupied_start.tzinfo or base_tz
        day_start, day_end = _day_bounds(day, reference_tz)

        occupied = merge_intervals(
            (max(item.occupied_start, day_start), min(item.occupied_end, day_end))
            for item in entries
        )
        meetings = merge_intervals(
            (max(item.meeting_start, day_start), min(item.meeting_end, day_end))
            for item in entries
        )

        items: list[DayInstance] = []
        meeting_total = timedelta()
        full_meeting_total = timedelta()
        raw_buffer_total = timedelta()

        for instance in sorted(entries, key=lambda item: item.meeting_start):
            meeting = _overlap(
                instance.meeting_start, instance.meeting_end, day_start, day_end
            )
            preparation = _overlap(
                instance.preparation_start, instance.meeting_start, day_start, day_end
            )
            cleanup = _overlap(
                instance.meeting_end, instance.cleanup_end, day_start, day_end
            )
            meeting_total += meeting
            full_meeting_total += instance.meeting_duration
            raw_buffer_total += preparation + cleanup
            items.append(
                DayInstance(
                    instance=instance,
                    day=day,
                    occupied_start=max(instance.occupied_start, day_start),
                    occupied_end=min(instance.occupied_end, day_end),
                    meeting=meeting,
                    preparation=preparation,
                    cleanup=cleanup,
                )
            )

        meeting_union = _sum_intervals(meetings)
        occupied_total = _sum_intervals(occupied)

        days.append(
            DayOccupancy(
                resource=resource,
                day=day,
                segments=[OccupancyInterval(*interval) for interval in occupied],
                instances=items,
                meeting_duration=meeting_union,
                meeting_union_duration=meeting_union,
                buffer_increment=occupied_total - meeting_union,
                raw_buffer_duration=raw_buffer_total,
                raw_meeting_duration=meeting_total,
                full_meeting_duration=full_meeting_total,
            )
        )

    return OccupancyReport(days=days)


def occupancy(
    events: Sequence[Event],
    rules: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    start: date | None = None,
    end: date | None = None,
    resource_of: ResourceInput = None,
    tz: tzinfo | None = None,
    default_rule: object | None = None,
    **kwargs: object,
) -> OccupancyReport:
    """Alias for :func:`daily_occupancy`."""
    return daily_occupancy(
        events,
        rules=rules,
        start=start,
        end=end,
        resource_of=resource_of,
        tz=tz,
        default_rule=default_rule,
        **kwargs,  # type: ignore[arg-type]
    )


def calendar_occupancy(
    url: str | None = None,
    file: str | None = None,
    string_content: bytes | str | None = None,
    rules: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    start: date | None = None,
    end: date | None = None,
    resource_of: ResourceInput = None,
    tz: tzinfo | None = None,
    default_rule: object | None = None,
    fix_apple: bool = False,
    strict: bool = False,
    **kwargs: object,
) -> OccupancyReport:
    """
    Parse an iCalendar source and compute its daily resource occupancy.

    The source is queried with a margin equal to the widest buffer plus one
    day on each side, so an occurrence just outside the requested dates whose
    preparation or cleanup reaches into them is still included.  Other
    parameters match :func:`daily_occupancy`.
    """
    from icalevents.icalevents import events as load_events

    start_date = _as_date(start)
    end_date = _as_date(end)

    # Widen the query by the largest buffer so a booking just outside the
    # requested dates whose preparation or cleanup reaches into them is found.
    normalized, default = normalize_rules(rules, default_rule)
    rules_list = list(normalized.values()) + [default]
    widest = max((rule.preparation for rule in rules_list), default=timedelta()) + max(
        (rule.cleanup for rule in rules_list), default=timedelta()
    )
    margin = widest + timedelta(days=1)

    occurrences = load_events(
        url=url,
        file=file,
        string_content=string_content,
        start=start_date - margin if isinstance(start_date, date) else None,
        end=end_date + margin if isinstance(end_date, date) else None,
        fix_apple=fix_apple,
        strict=strict,
    )
    return daily_occupancy(
        occurrences,
        rules=rules,
        start=start_date,
        end=end_date,
        resource_of=resource_of,
        tz=tz,
        default_rule=default_rule,
        **kwargs,  # type: ignore[arg-type]
    )


def daily_totals(
    report: OccupancyReport | Sequence[DayOccupancy],
) -> dict[date, timedelta]:
    """Merged occupancy per date, summed across resources."""
    if isinstance(report, OccupancyReport):
        return report.totals_by_day()
    totals: dict[date, timedelta] = {}
    for row in report:
        totals[row.day] = totals.get(row.day, timedelta()) + row.occupied_duration
    return totals


def occupancy_report(
    events: Sequence[Event],
    rules: Mapping[str, object] | Iterable[ResourceRule] | None = None,
    start: date | None = None,
    end: date | None = None,
    resource_of: ResourceInput = None,
    tz: tzinfo | None = None,
    default_rule: object | None = None,
    **kwargs: object,
) -> OccupancyReport:
    """Alias for :func:`daily_occupancy` with a descriptive name."""
    return daily_occupancy(
        events,
        rules=rules,
        start=start,
        end=end,
        resource_of=resource_of,
        tz=tz,
        default_rule=default_rule,
        **kwargs,  # type: ignore[arg-type]
    )
