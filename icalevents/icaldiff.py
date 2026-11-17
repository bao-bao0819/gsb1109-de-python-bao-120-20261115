"""
Incremental rebuilding of calendar instances inside a query window.

The functions in this module compare two calendar snapshots without touching
the network or the async cache in :mod:`icalevents.icalevents`.  A consumer can
therefore rebuild only the instances affected by an update:

>>> result = diff_calendars(old_ics, new_ics, start, end)
>>> result.added       # new instances
>>> result.removed     # withdrawn instances
>>> result.changed     # in-place updates; see Change.kind and Change.fields
>>> result.cancelled   # STATUS:CANCELLED changes (also included in removed)

Identity is ``(UID, original occurrence)``.  The original occurrence is the
RECURRENCE-ID of an exception and the generated slot of a master occurrence.
It is intentionally *not* the current start time: moving an exception must
update the existing instance rather than remove it and add a new one.

The distinction between change categories is:

``structure``
    The recurrence rule, the slot grid (DTSTART), a time zone definition used
    by the instance, or its occupied time changed.  A master rule change may
    rebuild the whole series in the window; an exception is still updated in
    place when only its own date moved.
``display``
    Only presentation fields (summary, description, location, categories,
    attendee, organizer, visibility, transparency, status text, URL) changed.
``cancellation``
    The instance became ``STATUS:CANCELLED``.  It is reported in
    ``CalendarDiff.cancelled`` with before/after events, and also appears in
    ``CalendarDiff.removed`` so a store can withdraw it while keeping an audit
    trail.

A component that simply falls outside the queried window is never used as
evidence for deleting an instance inside the window.  Such instances are
reported in ``CalendarDiff.protected`` and left in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Mapping, Sequence

from icalendar import Calendar

from .icalparser import Component, Event, component_uid, parse_events

DISPLAY_FIELDS = (
    "summary",
    "description",
    "location",
    "categories",
    "attendee",
    "organizer",
    "private",
    "transparent",
    "status",
    "url",
)

TIME_FIELDS = ("start", "end", "all_day", "floating", "tzid")


class ChangeKind(str, Enum):
    """Category of an instance change."""

    STRUCTURE = "structure"
    DISPLAY = "display"
    CANCELLATION = "cancellation"


class OccurrenceKind(str, Enum):
    """Role of a VEVENT inside a UID's component set."""

    SINGLE = "single"
    MASTER = "master"
    EXCEPTION = "exception"


@dataclass(frozen=True)
class InstanceKey:
    """
    Stable identity of one calendar instance.

    ``occurrence`` is the original slot in the series (RECURRENCE-ID for an
    exception, the generated slot for a master occurrence).  It must not be
    replaced by the current start when an exception is moved.
    """

    uid: str
    occurrence: date | datetime | None = None

    @classmethod
    def from_event(cls, event: Event) -> InstanceKey:
        occurrence = event.occurrence_start
        if occurrence is None:
            occurrence = event.recurrence_id
        if occurrence is None:
            occurrence = event.start
        return cls(uid=event.uid, occurrence=occurrence)

    def as_tuple(self) -> tuple[str, date | datetime | None]:
        return self.uid, self.occurrence


@dataclass
class Change:
    """One changed instance, with enough detail to log or audit it."""

    key: InstanceKey
    before: Event
    after: Event
    kind: ChangeKind
    fields: tuple[str, ...] = ()
    structural: tuple[str, ...] = ()
    display: tuple[str, ...] = ()
    time_zone: tuple[str, ...] = ()
    old_start: date | datetime | None = None
    new_start: date | datetime | None = None
    old_recurrence_id: date | datetime | None = None
    new_recurrence_id: date | datetime | None = None
    occurrence_kind: OccurrenceKind = OccurrenceKind.SINGLE
    reason: str | None = None

    @property
    def attribute_only(self) -> bool:
        """True when the instance stays at its slot and only data changed."""
        return self.kind is ChangeKind.DISPLAY

    @property
    def moved(self) -> bool:
        return self.old_start != self.new_start

    @property
    def cancelled(self) -> bool:
        return _event_status(self.after) == "CANCELLED"

    def as_dict(self) -> dict:
        """JSON-friendly representation; events are kept as objects."""
        return {
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "kind": self.kind.value,
            "fields": list(self.fields),
            "structural": list(self.structural),
            "display": list(self.display),
            "time_zone": list(self.time_zone),
            "old_start": self.old_start,
            "new_start": self.new_start,
            "old_recurrence_id": self.old_recurrence_id,
            "new_recurrence_id": self.new_recurrence_id,
            "occurrence_kind": self.occurrence_kind.value,
            "reason": self.reason,
            "moved": self.moved,
            "attribute_only": self.attribute_only,
            "cancelled": self.cancelled,
        }


@dataclass
class SeriesChange:
    """A change to a whole recurrence series inside the window."""

    uid: str
    kind: str
    old_rrule: str | None = None
    new_rrule: str | None = None
    old_exdates: tuple[str, ...] = ()
    new_exdates: tuple[str, ...] = ()
    old_start: date | datetime | None = None
    new_start: date | datetime | None = None
    events: list[Event] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "kind": self.kind,
            "old_rrule": self.old_rrule,
            "new_rrule": self.new_rrule,
            "old_exdates": list(self.old_exdates),
            "new_exdates": list(self.new_exdates),
            "old_start": self.old_start,
            "new_start": self.new_start,
            "events": list(self.events),
            "reason": self.reason,
        }


@dataclass
class TimeZoneChange:
    """A changed (or added/removed) VTIMEZONE definition."""

    tzid: str
    before: str | None = None
    after: str | None = None
    uids: tuple[str, ...] = ()
    events: list[Event] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "tzid": self.tzid,
            "before": self.before,
            "after": self.after,
            "uids": list(self.uids),
            "events": list(self.events),
        }


@dataclass
class CalendarDiff:
    """Result of comparing two snapshots for one window."""

    start: datetime | date | None = None
    end: datetime | date | None = None
    added: list[Event] = field(default_factory=list)
    removed: list[Event] = field(default_factory=list)
    changed: list[Change] = field(default_factory=list)
    cancelled: list[Change] = field(default_factory=list)
    unchanged: list[Event] = field(default_factory=list)
    protected: list[Event] = field(default_factory=list)
    series_changes: list[SeriesChange] = field(default_factory=list)
    timezone_changes: list[TimeZoneChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def updated(self) -> list[Change]:
        """Alias for stores that call a changed instance an update."""
        return self.changed

    @property
    def all_changes(self) -> list[Change]:
        """All per-instance changes, including cancellations."""
        return self.changed + self.cancelled

    @property
    def added_keys(self) -> list[InstanceKey]:
        return [InstanceKey.from_event(event) for event in self.added]

    @property
    def removed_keys(self) -> list[InstanceKey]:
        return [InstanceKey.from_event(event) for event in self.removed]

    @property
    def attribute_changes(self) -> list[Change]:
        return [change for change in self.changed if change.attribute_only]

    @property
    def structural_changes(self) -> list[Change]:
        return [
            change for change in self.changed if change.kind is ChangeKind.STRUCTURE
        ]

    @property
    def cancellation_changes(self) -> list[Change]:
        return list(self.cancelled)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.added
            or self.removed
            or self.changed
            or self.cancelled
            or self.series_changes
            or self.timezone_changes
        )

    def apply(
        self, existing: Iterable[Event], *, replace_uid: Iterable[str] | None = None
    ) -> list[Event]:
        """
        Apply the diff to a caller-owned collection of instances.

        Removal uses :class:`InstanceKey`, so a moved exception replaces its old
        row instead of leaving a duplicate.  ``replace_uid`` can be supplied by
        a source-level caller after a master rule was rebuilt wholesale.
        """
        remove_keys = {key.as_tuple() for key in self.removed_keys}
        remove_keys.update(change.key.as_tuple() for change in self.cancelled)
        changes = {change.key.as_tuple(): change for change in self.changed}
        replaced = set(replace_uid or ())

        result: list[Event] = []
        for event in existing:
            key = InstanceKey.from_event(event)
            if event.uid in replaced or key.as_tuple() in remove_keys:
                continue
            change = changes.get(key.as_tuple())
            result.append(change.after if change else event)

        additions = {
            InstanceKey.from_event(event).as_tuple(): event for event in self.added
        }
        known = {InstanceKey.from_event(event).as_tuple() for event in result}
        for key, event in additions.items():
            if key not in known:
                result.append(event)

        return result

    def as_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": [change.as_dict() for change in self.changed],
            "cancelled": [change.as_dict() for change in self.cancelled],
            "unchanged": list(self.unchanged),
            "protected": list(self.protected),
            "series_changes": [change.as_dict() for change in self.series_changes],
            "timezone_changes": [change.as_dict() for change in self.timezone_changes],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Source component inspection
# ---------------------------------------------------------------------------


@dataclass
class _ComponentInfo:
    component: Component
    uid: str
    kind: OccurrenceKind
    occurrence: date | datetime | None
    start: date | datetime | None
    end: date | datetime | None
    rrule: str | None
    exdates: tuple[str, ...]
    status: str | None
    sequence: str | None
    last_modified: datetime | None
    tzids: tuple[str, ...]


def _as_ical(value: object | None) -> str | None:
    if value is None:
        return None
    serialized = value.to_ical() if hasattr(value, "to_ical") else str(value).encode()
    if isinstance(serialized, bytes):
        return serialized.decode("utf-8", "backslashreplace")
    return str(serialized)


def _values(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _rrule_fingerprint(component: Component) -> str | None:
    value = component.get("rrule")
    if value is None:
        return None
    rules: list[str] = []
    for rule in _values(value):
        # UNTIL is a cut-off; a change to it is still structural, but keeping
        # the complete rule text makes that distinction explicit.
        rules.append(_as_ical(rule) or "")
    return "|".join(sorted(rules))


def _exdates(component: Component) -> tuple[str, ...]:
    value = component.get("exdate")
    if value is None:
        return ()
    result: list[str] = []
    for group in _values(value):
        dts = getattr(group, "dts", None)
        if dts is None:
            result.append(_as_ical(group) or "")
            continue
        for item in dts:
            # to_ical() on the value deliberately omits TZID; _tzids() covers
            # the parameter, so compare the local slot value here.
            result.append(_as_ical(getattr(item, "dt", item)) or "")
    return tuple(sorted(result))


def _tzids(component: Component) -> tuple[str, ...]:
    found: set[str] = set()
    for name in ("dtstart", "dtend", "recurrence-id", "exdate"):
        value = component.get(name)
        if value is None:
            continue
        for item in _values(value):
            params = getattr(item, "params", {})
            if params.get("TZID"):
                found.add(str(params["TZID"]))
            for nested in getattr(item, "dts", ()):
                nested_params = getattr(nested, "params", {})
                if nested_params.get("TZID"):
                    found.add(str(nested_params["TZID"]))
    return tuple(sorted(found))


def _component_status(component: Component) -> str | None:
    value = component.get("status")
    return str(value).upper() if value is not None else None


def _sequence(component: Component) -> str | None:
    value = component.get("sequence")
    return str(value) if value is not None else None


def _last_modified(component: Component) -> datetime | None:
    value = component.get("last-modified")
    return getattr(value, "dt", None) if value is not None else None


def _inspect(component: Component) -> _ComponentInfo:
    uid = component_uid(component)
    dtstart = component.get("dtstart")
    dtend = component.get("dtend")
    duration = component.get("duration")
    start = dtstart.dt if dtstart is not None else None
    if dtend is not None:
        end = dtend.dt
    elif duration is not None and start is not None:
        end = start + duration.dt
    else:
        end = start

    rid = component.get("recurrence-id")
    occurrence = rid.dt if rid is not None else start
    if rid is not None and type(start) is date:
        # Match parse_events(): a date-valued DTSTART makes RECURRENCE-ID a
        # date even if the producer emitted a datetime.
        occurrence = date(occurrence.year, occurrence.month, occurrence.day)

    if rid is not None:
        kind = OccurrenceKind.EXCEPTION
    elif component.get("rrule") is not None:
        kind = OccurrenceKind.MASTER
    else:
        kind = OccurrenceKind.SINGLE

    return _ComponentInfo(
        component=component,
        uid=uid,
        kind=kind,
        occurrence=occurrence,
        start=start,
        end=end,
        rrule=_rrule_fingerprint(component),
        exdates=_exdates(component),
        status=_component_status(component),
        sequence=_sequence(component),
        last_modified=_last_modified(component),
        tzids=_tzids(component),
    )


def _components(content: str) -> dict[str, list[_ComponentInfo]]:
    grouped: dict[str, list[_ComponentInfo]] = {}
    if not content or not content.strip():
        return grouped
    calendar = Calendar.from_ical(content)
    for component in calendar.walk("VEVENT"):
        info = _inspect(component)
        grouped.setdefault(info.uid, []).append(info)
    return grouped


def _timezone_definitions(content: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if not content or not content.strip():
        return result
    calendar = Calendar.from_ical(content)
    for component in calendar.walk("VTIMEZONE"):
        result[str(component.get("tzid") or "")] = _as_ical(component) or ""
    return result


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def _bound(value: datetime | date, like: datetime | date) -> datetime | date:
    """Project a window bound onto the value type of an occurrence."""
    if type(value) is datetime and type(like) is date:
        return date(value.year, value.month, value.day)
    if type(value) is date and type(like) is datetime:
        return datetime(value.year, value.month, value.day, tzinfo=like.tzinfo)
    if type(value) is datetime and type(like) is datetime:
        if like.tzinfo is None:
            return value.replace(tzinfo=None)
        if value.tzinfo is None:
            return value.replace(tzinfo=like.tzinfo)
        return value.astimezone(like.tzinfo)
    return value


def _overlaps(
    value_start: date | datetime | None,
    value_end: date | datetime | None,
    start: datetime | date,
    end: datetime | date,
) -> bool:
    """Overlap test mirroring ``parse_events``' inclusive window semantics."""
    if value_start is None or value_end is None:
        return False
    low = _bound(start, value_start)
    high = _bound(end, value_start)
    return value_end >= low and value_start <= high


def _slot_in_window(
    occurrence: date | datetime | None,
    start: datetime | date,
    end: datetime | date,
) -> bool:
    if occurrence is None:
        return False
    low = _bound(start, occurrence)
    high = _bound(end, occurrence)
    return low <= occurrence <= high


def _recurring_window_relevant(
    info: _ComponentInfo, start: datetime | date, end: datetime | date
) -> bool:
    """
    Test a master rule against the window without expanding the whole series.

    A conservative answer is sufficient here: true means parsing may yield a
    relevant slot.  parse_events() remains the authority for the exact set.
    """
    if info.start is None:
        return False
    low = _bound(start, info.start)
    high = _bound(end, info.start)
    if info.end is not None and type(info.end) is not date:
        high -= info.end - info.start
    # A very small rule-independent probe: if DTSTART is already after the
    # window and the rule cannot move backwards, nothing can be in the window.
    return info.start <= high


def _component_relevant(
    info: _ComponentInfo, start: datetime | date, end: datetime | date
) -> bool:
    if info.kind is OccurrenceKind.EXCEPTION:
        return _slot_in_window(info.occurrence, start, end) or _overlaps(
            info.start, info.end, start, end
        )
    if info.kind is OccurrenceKind.SINGLE:
        return _overlaps(info.start, info.end, start, end)
    return _recurring_window_relevant(info, start, end)


def _status_cancelled(info: _ComponentInfo | None) -> bool:
    return info is not None and info.status == "CANCELLED"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def diff_calendars(
    previous_content: str,
    current_content: str,
    start: datetime | date,
    end: datetime | date,
    *,
    tzinfo=None,
    strict: bool = False,
) -> CalendarDiff:
    """
    Compare two iCalendar snapshots for a single query window.

    Both snapshots are parsed through the same code path as
    :func:`icalevents.icalevents.events`, so returned events are directly
    consumable by existing callers.  The result contains only window-relevant
    instances.

    :param previous_content: snapshot before the update
    :param current_content: snapshot after the update
    :param start: start of the window to rebuild
    :param end: end of the window to rebuild
    :param tzinfo: return values in this timezone (as in ``events``)
    :param strict: keep date/datetime semantics from the iCalendar source
    :return: structured diff
    """
    previous_components = _components(previous_content)
    current_components = _components(current_content)

    previous_events = (
        parse_events(
            previous_content,
            start=start,
            end=end,
            tzinfo=tzinfo,
            strict=strict,
            sort=True,
        )
        if previous_content and previous_content.strip()
        else []
    )
    current_events = (
        parse_events(
            current_content,
            start=start,
            end=end,
            tzinfo=tzinfo,
            strict=strict,
            sort=True,
        )
        if current_content and current_content.strip()
        else []
    )

    result = diff_events(previous_events, current_events, start=start, end=end)
    result.warnings = []
    current_covers_window = _covers_window(current_components, start, end)

    _classify_source_changes(
        result,
        previous_components,
        current_components,
        previous_content,
        current_content,
    )
    _partition_cancellations(result)
    _collect_series_and_timezone_changes(
        result,
        previous_components,
        current_components,
        previous_content,
        current_content,
        previous_events,
        current_events,
    )
    _protect_window_instances(
        result,
        previous_components,
        current_components,
        previous_events,
        current_events,
        start,
        end,
        current_covers_window=current_covers_window,
    )
    return result


def _find_info(
    infos: list[_ComponentInfo],
    *,
    occurrence: date | datetime | None = None,
    kind: OccurrenceKind | None = None,
) -> _ComponentInfo | None:
    candidates = infos
    if kind is not None:
        candidates = [info for info in candidates if info.kind is kind]
    if occurrence is not None:
        exact = [info for info in candidates if info.occurrence == occurrence]
        if exact:
            candidates = exact
    return candidates[0] if candidates else None


def _covers_window(
    components: Mapping[str, list[_ComponentInfo]],
    start: datetime | date,
    end: datetime | date,
) -> bool:
    """
    Best-effort test for whether a snapshot describes the entire window.

    An empty or one-slot incremental feed cannot justify deleting rows that
    are already confirmed inside the window.  A snapshot that describes a
    recurring series reaching into the window, or at least one event inside
    the window, is treated as covering it.
    """
    if not components:
        return False
    for infos in components.values():
        for info in infos:
            if info.kind is OccurrenceKind.MASTER:
                if _recurring_window_relevant(info, start, end):
                    return True
            elif _component_relevant(info, start, end):
                return True
    return False


def _collect_series_and_timezone_changes(
    result: CalendarDiff,
    previous: Mapping[str, list[_ComponentInfo]],
    current: Mapping[str, list[_ComponentInfo]],
    previous_content: str,
    current_content: str,
    previous_events: Sequence[Event],
    current_events: Sequence[Event],
) -> None:
    """Record series/zone edits which actually affect this window's instances."""
    previous_tz = _timezone_definitions(previous_content)
    current_tz = _timezone_definitions(current_content)
    window_events = list(previous_events) + list(current_events)
    window_uids = {event.uid for event in window_events}

    changed_tzids = {
        tzid
        for tzid in set(previous_tz) | set(current_tz)
        if previous_tz.get(tzid) != current_tz.get(tzid)
    }
    if changed_tzids:
        affected_uids: dict[str, set[str]] = {tzid: set() for tzid in changed_tzids}
        for uid in window_uids:
            for info in previous.get(uid, []) + current.get(uid, []):
                for tzid in info.tzids:
                    if tzid in affected_uids:
                        affected_uids[tzid].add(uid)
        for tzid in sorted(changed_tzids):
            events = [
                event
                for event in current_events
                if event.uid in affected_uids[tzid]
            ]
            result.timezone_changes.append(
                TimeZoneChange(
                    tzid=tzid,
                    before=previous_tz.get(tzid),
                    after=current_tz.get(tzid),
                    uids=tuple(sorted(affected_uids[tzid])),
                    events=events,
                )
            )

    for uid in sorted(window_uids):
        old_master = _find_info(previous.get(uid, []), kind=OccurrenceKind.MASTER)
        new_master = _find_info(current.get(uid, []), kind=OccurrenceKind.MASTER)
        if old_master is None and new_master is None:
            continue

        kind: str | None = None
        if old_master is None:
            kind = "added"
        elif new_master is None:
            kind = "removed"
        elif old_master.rrule != new_master.rrule:
            kind = "rrule"
        elif old_master.exdates != new_master.exdates:
            kind = "exdate"
        elif _as_ical(old_master.start) != _as_ical(new_master.start):
            kind = "dtstart"
        if kind is None:
            continue

        events = [
            event
            for event in result.added + result.removed
            if event.uid == uid
        ] + [
            change.after
            for change in result.changed
            if change.key.uid == uid
        ]
        if not events:
            # The rule changed but no instance in this window changed; do not
            # claim a window-scoped rebuild.
            continue
        result.series_changes.append(
            SeriesChange(
                uid=uid,
                kind=kind,
                old_rrule=old_master.rrule if old_master else None,
                new_rrule=new_master.rrule if new_master else None,
                old_exdates=old_master.exdates if old_master else (),
                new_exdates=new_master.exdates if new_master else (),
                old_start=old_master.start if old_master else None,
                new_start=new_master.start if new_master else None,
                events=events,
            )
        )


def _classify_source_changes(
    result: CalendarDiff,
    previous: Mapping[str, list[_ComponentInfo]],
    current: Mapping[str, list[_ComponentInfo]],
    previous_content: str,
    current_content: str,
) -> None:
    """
    Refine change kinds using component metadata and timezone definitions.

    Event comparison alone cannot tell a rule edit from an exception edit, nor
    a renamed display field from a changed VTIMEZONE.  This pass supplies that
    source-level context while leaving the event instances untouched.
    """
    previous_tz = _timezone_definitions(previous_content)
    current_tz = _timezone_definitions(current_content)

    for change in result.changed:
        old_infos = previous.get(change.key.uid, [])
        new_infos = current.get(change.key.uid, [])
        old_info = _find_info(old_infos, occurrence=change.key.occurrence)
        new_info = _find_info(new_infos, occurrence=change.key.occurrence)

        if old_info is not None and new_info is not None:
            old_rid = (
                old_info.occurrence
                if old_info.kind is OccurrenceKind.EXCEPTION
                else None
            )
            new_rid = (
                new_info.occurrence
                if new_info.kind is OccurrenceKind.EXCEPTION
                else None
            )
            change.old_recurrence_id = old_rid
            change.new_recurrence_id = new_rid
            change.occurrence_kind = new_info.kind

            if _status_cancelled(new_info) and not _status_cancelled(old_info):
                change.kind = ChangeKind.CANCELLATION
            elif old_info.rrule != new_info.rrule:
                change.kind = ChangeKind.STRUCTURE
                change.structural = tuple(sorted(set(change.structural) | {"rrule"}))
            elif old_info.kind is not new_info.kind:
                change.kind = ChangeKind.STRUCTURE
                change.structural = tuple(
                    sorted(set(change.structural) | {"occurrence_kind"})
                )

            touched_tzids = set(old_info.tzids) | set(new_info.tzids)
            changed_tzids = {
                tzid
                for tzid in touched_tzids
                if previous_tz.get(tzid) != current_tz.get(tzid)
            }
            if old_info.tzids != new_info.tzids:
                changed_tzids |= touched_tzids
            if changed_tzids:
                change.time_zone = tuple(sorted(changed_tzids))
                change.kind = ChangeKind.STRUCTURE
                change.structural = tuple(
                    sorted(set(change.structural) | {"tzid"})
                )

        if change.old_start != change.new_start and change.kind is ChangeKind.DISPLAY:
            change.kind = ChangeKind.STRUCTURE
            change.structural = tuple(
                sorted(set(change.structural) | {"start", "end"})
            )

        master_old = _find_info(
            previous.get(change.key.uid, []), kind=OccurrenceKind.MASTER
        )
        master_new = _find_info(
            current.get(change.key.uid, []), kind=OccurrenceKind.MASTER
        )
        if master_old is not None and master_new is not None:
            if master_old.rrule != master_new.rrule:
                change.kind = ChangeKind.STRUCTURE
                change.structural = tuple(
                    sorted(set(change.structural) | {"rrule"})
                )

        # Cancellation is terminal: it takes precedence over a simultaneous
        # time or rule change, because the instance must disappear.
        if _event_status(change.after) == "CANCELLED":
            change.kind = ChangeKind.CANCELLATION
            change.reason = "status changed to CANCELLED"


def _partition_cancellations(result: CalendarDiff) -> None:
    """Report STATUS:CANCELLED as a removal with an audit trail."""
    kept: list[Change] = []
    for change in result.changed:
        if change.kind is ChangeKind.CANCELLATION:
            change.reason = "status changed to CANCELLED"
            result.cancelled.append(change)
            result.removed.append(change.before)
        else:
            kept.append(change)
    result.changed = kept


def _protect_window_instances(
    result: CalendarDiff,
    previous: Mapping[str, list[_ComponentInfo]],
    current: Mapping[str, list[_ComponentInfo]],
    previous_events: Sequence[Event],
    current_events: Sequence[Event],
    start: datetime | date,
    end: datetime | date,
    *,
    current_covers_window: bool,
) -> None:
    """
    Remove only instances for which the new snapshot provides evidence.

    Evidence is one of:

    * the instance became ``STATUS:CANCELLED`` (handled as a change),
    * its occurrence is listed in the master's EXDATE,
    * the master rule (or the whole series) demonstrably no longer produces
      the slot,
    * a non-recurring component that was inside the window is absent from a
      snapshot known to cover the window.

    A component outside the window, or a snapshot that is visibly partial for
    the UID, is not evidence.  Those rows are moved to ``protected``.
    """
    confirmed_keys = {
        InstanceKey.from_event(event).as_tuple() for event in current_events
    }
    cancelled_keys = {change.key.as_tuple() for change in result.cancelled}
    protected: list[Event] = []
    removed: list[Event] = []

    for event in result.removed:
        key = InstanceKey.from_event(event)
        old_infos = previous.get(key.uid, [])
        new_infos = current.get(key.uid, [])

        if key.as_tuple() in cancelled_keys:
            removed.append(event)
            continue

        if key.as_tuple() in confirmed_keys:
            # The same instance is present in the new snapshot; it must have
            # been paired (or changed) rather than removed.
            protected.append(event)
            continue

        if _removal_is_authoritative(
            key,
            old_infos,
            new_infos,
            start,
            end,
            current_covers_window=current_covers_window,
        ):
            removed.append(event)
        else:
            protected.append(event)

    result.removed = removed
    result.protected.extend(protected)
    if protected:
        result.warnings.append(
            "kept %d instance(s) because the update did not contain "
            "authoritative evidence for their removal" % len(protected)
        )


def _removal_is_authoritative(
    key: InstanceKey,
    old_infos: list[_ComponentInfo],
    new_infos: list[_ComponentInfo],
    start: datetime | date,
    end: datetime | date,
    *,
    current_covers_window: bool,
) -> bool:
    if not old_infos:
        return False

    masters_old = [info for info in old_infos if info.kind is OccurrenceKind.MASTER]
    masters_new = [info for info in new_infos if info.kind is OccurrenceKind.MASTER]

    if masters_old:
        master_old = masters_old[0]
        if not masters_new:
            # The master vanished.  Only a snapshot that covers the window can
            # prove the series disappeared; a partial feed cannot.
            return current_covers_window

        master_new = masters_new[0]
        if master_old.rrule != master_new.rrule:
            return True
        if key.occurrence is not None:
            slot = _as_ical(key.occurrence)
            if slot in master_new.exdates:
                return True
        # The new master still covers the window, but this slot is no longer
        # produced.  parse_events() performed the expansion, so the slot is
        # genuinely excluded by the current rule/EXDATE set.
        return _recurring_window_relevant(master_new, start, end)

    old_single = _find_info(
        old_infos, occurrence=key.occurrence, kind=OccurrenceKind.SINGLE
    )
    if old_single is None:
        # An exception disappeared without a master.  Without a rule there is
        # no way to distinguish a deletion from a filtered feed.
        return False
    if not _overlaps(old_single.start, old_single.end, start, end):
        return False
    # A non-recurring component inside a complete new window is deleted.
    return current_covers_window


def _deduplicate(events: Iterable[Event]) -> dict[tuple, Event]:
    """Later components win, matching iCalendar duplicate-component rules."""
    result: dict[tuple, Event] = {}
    for event in events:
        result[InstanceKey.from_event(event).as_tuple()] = event
    return result


def _value(value: object) -> object:
    if isinstance(value, list):
        return tuple(_value(item) for item in value)
    if hasattr(value, "address"):
        return str(value)
    return value


def _diff_fields(before: Event, after: Event) -> tuple[str, ...]:
    fields: list[str] = []
    for name in TIME_FIELDS + DISPLAY_FIELDS:
        if _value(getattr(before, name, None)) != _value(getattr(after, name, None)):
            fields.append(name)
    return tuple(fields)


def _occurrence_kind(event: Event) -> OccurrenceKind:
    if event.recurrence_id is not None:
        return OccurrenceKind.EXCEPTION
    return OccurrenceKind.MASTER if event.recurring else OccurrenceKind.SINGLE


def _make_change(
    key: InstanceKey, before: Event, after: Event, fields: tuple[str, ...]
) -> Change:
    structural = tuple(name for name in fields if name in TIME_FIELDS)
    display = tuple(name for name in fields if name in DISPLAY_FIELDS)
    if structural or "recurrence_id" in fields:
        kind = ChangeKind.STRUCTURE
    elif (
        _event_status(after) == "CANCELLED"
        and _event_status(before) != "CANCELLED"
    ):
        kind = ChangeKind.CANCELLATION
    else:
        kind = ChangeKind.DISPLAY

    return Change(
        key=key,
        before=before,
        after=after,
        kind=kind,
        fields=fields,
        structural=structural,
        display=display,
        old_start=before.start,
        new_start=after.start,
        old_recurrence_id=before.recurrence_id,
        new_recurrence_id=after.recurrence_id,
        occurrence_kind=_occurrence_kind(after),
    )


def diff_events(
    previous: Sequence[Event],
    current: Sequence[Event],
    *,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
) -> CalendarDiff:
    """
    Compare two already parsed instance lists.

    This is the low-level counterpart to :func:`diff_calendars`.  Identity is
    ``(UID, original occurrence)``.  A moved exception keeps the same key when
    its RECURRENCE-ID is preserved.  If a producer also changes the
    RECURRENCE-ID while moving the occurrence, a unique UID match still pairs
    the two rows before falling back to add/remove.

    This function compares the supplied lists literally; it cannot inspect
    source components and therefore makes no assertion about partial feeds.
    Use :func:`diff_calendars` when source evidence is available.
    """
    old = _deduplicate(previous)
    new = _deduplicate(current)
    result = CalendarDiff(start=start, end=end)

    remaining_old = dict(old)
    remaining_new = dict(new)

    # Pair identical identities first.
    for key in list(old):
        if key not in new:
            continue
        before, after = old[key], new[key]
        remaining_old.pop(key, None)
        remaining_new.pop(key, None)
        fields = _diff_fields(before, after)
        if fields:
            result.changed.append(
                _make_change(
                    InstanceKey(uid=key[0], occurrence=key[1]),
                    before,
                    after,
                    fields,
                )
            )
        else:
            result.unchanged.append(after)

    # Pair by UID only when unambiguous.  This covers producers which rewrite
    # RECURRENCE-ID on a moved exception, and also keeps a single non-recurring
    # event paired across a DTSTART edit.
    old_by_uid: dict[str, list[tuple]] = {}
    new_by_uid: dict[str, list[tuple]] = {}
    for key in remaining_old:
        old_by_uid.setdefault(key[0], []).append(key)
    for key in remaining_new:
        new_by_uid.setdefault(key[0], []).append(key)

    for uid, old_keys in old_by_uid.items():
        new_keys = new_by_uid.get(uid, [])
        if len(old_keys) != 1 or len(new_keys) != 1:
            continue
        old_key, new_key = old_keys[0], new_keys[0]
        before = remaining_old.pop(old_key)
        after = remaining_new.pop(new_key)
        fields = _diff_fields(before, after)
        if not fields and before.recurrence_id == after.recurrence_id:
            result.unchanged.append(after)
            continue
        if not fields:
            fields = ("recurrence_id",)
        key = InstanceKey(uid=uid, occurrence=old_key[1])
        change = _make_change(key, before, after, fields)
        change.old_recurrence_id = before.recurrence_id
        change.new_recurrence_id = after.recurrence_id
        result.changed.append(change)

    result.removed = list(remaining_old.values())
    result.added = list(remaining_new.values())
    _partition_cancellations(result)
    return result


def _event_status(event: Event) -> str | None:
    """Return an event's STATUS in upper case, if present."""
    status = getattr(event, "status", None)
    return str(status).upper() if status is not None else None


def sync_window(
    previous: Sequence[Event],
    current: Sequence[Event],
    *,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
) -> list[Event]:
    """
    Return the next window content after applying an event list update.

    Display/structure updates replace rows in place, cancellations disappear,
    and new instances are appended.  This function has no source-level
    evidence available, so it follows :func:`diff_events` literally; use
    :func:`diff_calendars` when a partial source feed must be protected.
    """
    return diff_events(previous, current, start=start, end=end).apply(previous)
