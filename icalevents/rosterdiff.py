"""
Compare two versions of a roster calendar and build per-person notifications.

Only the instances falling inside a finite query window are compared. Every
change is attributed per role (organizer / participant role) and per
delegation link, then merged into a single notification per identified
person. Only a ``mailto:`` address is a deliverable identity; attendees
with URN or other non-mail addresses are kept separate, never merged, and
their identity is never guessed from a display name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from typing import Any

from dateutil.tz import UTC

from icalevents.icalparser import Attendee, Event, parse_events

NON_PARTICIPANT_CUTYPES = {"RESOURCE", "ROOM", "UNKNOWN"}
NON_PARTICIPANT_ROLES = {"NON-PARTICIPANT"}
PARTICIPANT_DEFAULT_ROLE = "REQ-PARTICIPANT"
ORGANIZER_ROLE = "ORGANIZER"


@dataclass(frozen=True)
class Person:
    """A notification target.

    ``key`` is the merge identity (normalized mail address or the raw
    calendar address). ``identified`` is False when the calendar entry
    does not name a person or give a mail address.
    """

    key: str
    name: str | None
    address: str
    identified: bool


@dataclass(frozen=True)
class Participation:
    """One person's role on one roster slot."""

    person: Person
    role: str
    delegates_to: frozenset[str] = frozenset()
    delegates_from: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Slot:
    """One concrete roster instance inside the query window."""

    key: tuple[str, datetime]
    uid: str
    summary: str | None
    start: datetime
    end: datetime | None
    participations: frozenset[Participation]
    event: Event


@dataclass(frozen=True)
class Change:
    kind: str
    person: Person
    uid: str
    summary: str | None
    role: str
    old_start: datetime | None = None
    new_start: datetime | None = None
    old_end: datetime | None = None
    new_end: datetime | None = None
    old_summary: str | None = None
    new_summary: str | None = None
    delegates_to: frozenset[str] = frozenset()
    delegates_from: frozenset[str] = frozenset()


@dataclass
class PersonNotification:
    person: Person
    added: list[Change] = field(default_factory=list)
    removed: list[Change] = field(default_factory=list)
    time_changed: list[Change] = field(default_factory=list)
    summary_only: list[Change] = field(default_factory=list)

    @property
    def affected(self) -> bool:
        return bool(
            self.added or self.removed or self.time_changed or self.summary_only
        )


def normalize_address(address: str) -> str:
    """Return the merge identity of a calendar address."""

    raw = str(address).strip()
    if raw.lower().startswith("mailto:"):
        return "mailto:" + raw[len("mailto:") :].strip().lower()
    return raw


def _to_utc(value: datetime | date, reference: tzinfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=reference).astimezone(UTC)
        return value.astimezone(UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _person_from(address: str, params: dict[str, Any]) -> Person:
    name = params.get("CN")
    name = str(name).strip() if name else None
    normalized = normalize_address(address)
    # Only a mail address is a deliverable identity. URNs and other
    # addresses are kept separate and never merged into a person.
    identified = normalized.lower().startswith("mailto:")
    return Person(
        key=normalized,
        name=name,
        address=str(address),
        identified=identified,
    )


def _split_params(value: Any) -> tuple[str, dict[str, Any]]:
    params = getattr(value, "params", None)
    if params is None:
        return str(value), {}
    return str(value), dict(params)


def _address_list(param_value: Any) -> list[str]:
    if not param_value:
        return []
    values = (
        param_value if isinstance(param_value, (list, tuple)) else [param_value]
    )
    return [normalize_address(item) for item in values if item]


def _attendee_participations(attendee: Any) -> list[Participation]:
    if isinstance(attendee, Attendee):
        raw, params = str(attendee), dict(attendee.params)
    else:
        raw, params = _split_params(attendee)

    cutype = str(params.get("CUTYPE", "INDIVIDUAL")).upper()
    role = str(params.get("ROLE", PARTICIPANT_DEFAULT_ROLE)).upper()
    if cutype in NON_PARTICIPANT_CUTYPES or role in NON_PARTICIPANT_ROLES:
        return []

    return [
        Participation(
            person=_person_from(raw, params),
            role=role,
            delegates_to=frozenset(_address_list(params.get("DELEGATED-TO"))),
            delegates_from=frozenset(_address_list(params.get("DELEGATED-FROM"))),
        )
    ]


def _organizer_participation(event: Event) -> Participation | None:
    component = event.component
    if component is None or component.get("organizer") is None:
        return None
    raw, params = _split_params(component.get("organizer"))
    return Participation(person=_person_from(raw, params), role=ORGANIZER_ROLE)


def extract_participations(event: Event) -> frozenset[Participation]:
    participations: list[Participation] = []
    organizer = _organizer_participation(event)
    if organizer is not None:
        participations.append(organizer)

    if event.attendee is not None:
        attendees = (
            event.attendee if isinstance(event.attendee, list) else [event.attendee]
        )
        for attendee in attendees:
            participations.extend(_attendee_participations(attendee))

    deduped: dict[tuple[Any, ...], Participation] = {}
    for participation in participations:
        key = (
            participation.person.key,
            participation.role,
            participation.delegates_to,
            participation.delegates_from,
        )
        deduped[key] = participation
    return frozenset(deduped.values())


def slot_key(event: Event, reference: tzinfo) -> tuple[str, datetime]:
    anchor = event.recurrence_id or event.start
    return event.uid, _to_utc(anchor, reference)


def build_slots(
    events: list[Event], reference: tzinfo
) -> dict[tuple[str, datetime], Slot]:
    slots: dict[tuple[str, datetime], Slot] = {}
    for event in events:
        key = slot_key(event, reference)
        slots[key] = Slot(
            key=key,
            uid=event.uid,
            summary=event.summary,
            start=_to_utc(event.start, reference),
            end=_to_utc(event.end, reference) if event.end else None,
            participations=extract_participations(event),
            event=event,
        )
    return slots


def parse_roster(
    content: str,
    start: datetime,
    end: datetime,
    reference: tzinfo,
) -> dict[tuple[str, datetime], Slot]:
    # parse_events rebuilds the bounds using their wall-clock components in
    # the event timezone, so convert the instants to the reference zone
    # first to keep the window semantics intuitive.
    window_start = start.astimezone(reference)
    window_end = end.astimezone(reference)
    events = parse_events(
        content, window_start, window_end, strict=True, tzinfo=reference
    )
    return build_slots(events, reference)


def pair_slots(
    old_slots: dict[tuple[str, datetime], Slot],
    new_slots: dict[tuple[str, datetime], Slot],
) -> list[tuple[Slot | None, Slot | None]]:
    """Match old and new slots.

    Exact ``(uid, recurrence anchor)`` matches pair first. Remaining slots
    with the same uid are paired one-to-one by the closest start time so a
    rescheduled single event is reported as a time change rather than a
    cancel plus an add. Everything left over is a pure add or removal.
    """

    pairs: list[tuple[Slot | None, Slot | None]] = []
    exact = set(old_slots) & set(new_slots)
    for key in sorted(exact):
        pairs.append((old_slots[key], new_slots[key]))

    old_left = [slot for key, slot in old_slots.items() if key not in exact]
    new_left = [slot for key, slot in new_slots.items() if key not in exact]

    uids = {slot.uid for slot in old_left} | {slot.uid for slot in new_left}
    for uid in sorted(uids):
        old_group = [slot for slot in old_left if slot.uid == uid]
        new_group = [slot for slot in new_left if slot.uid == uid]
        new_available = set(range(len(new_group)))
        for old_slot in sorted(old_group, key=lambda slot: slot.start):
            if not new_available:
                pairs.append((old_slot, None))
                continue
            closest = min(
                new_available,
                key=lambda index: abs(
                    (new_group[index].start - old_slot.start).total_seconds()
                ),
            )
            pairs.append((old_slot, new_group[closest]))
            new_available.remove(closest)
        for index in sorted(new_available):
            pairs.append((None, new_group[index]))

    return pairs


def _participation_index(
    slot: Slot,
) -> dict[tuple[Any, ...], Participation]:
    return {
        (
            participation.person.key,
            participation.role,
            participation.delegates_to,
            participation.delegates_from,
        ): participation
        for participation in slot.participations
    }


def diff_slot(old: Slot | None, new: Slot | None) -> list[Change]:
    if old is None and new is None:
        return []
    if old is None:
        return [
            Change(
                kind="added",
                person=participation.person,
                uid=new.uid,
                summary=new.summary,
                role=participation.role,
                new_start=new.start,
                new_end=new.end,
                delegates_to=participation.delegates_to,
                delegates_from=participation.delegates_from,
            )
            for participation in new.participations
        ]
    if new is None:
        return [
            Change(
                kind="removed",
                person=participation.person,
                uid=old.uid,
                summary=old.summary,
                role=participation.role,
                old_start=old.start,
                old_end=old.end,
                delegates_to=participation.delegates_to,
                delegates_from=participation.delegates_from,
            )
            for participation in old.participations
        ]

    changes: list[Change] = []
    old_index = _participation_index(old)
    new_index = _participation_index(new)

    for identity, participation in new_index.items():
        if identity not in old_index:
            changes.append(
                Change(
                    kind="added",
                    person=participation.person,
                    uid=new.uid,
                    summary=new.summary,
                    role=participation.role,
                    new_start=new.start,
                    new_end=new.end,
                    delegates_to=participation.delegates_to,
                    delegates_from=participation.delegates_from,
                )
            )

    for identity, participation in old_index.items():
        if identity not in new_index:
            changes.append(
                Change(
                    kind="removed",
                    person=participation.person,
                    uid=old.uid,
                    summary=old.summary,
                    role=participation.role,
                    old_start=old.start,
                    old_end=old.end,
                    delegates_to=participation.delegates_to,
                    delegates_from=participation.delegates_from,
                )
            )

    time_changed = old.start != new.start or old.end != new.end
    for identity, new_participation in new_index.items():
        if identity not in old_index:
            continue
        if time_changed:
            changes.append(
                Change(
                    kind="time_changed",
                    person=new_participation.person,
                    uid=old.uid,
                    summary=new.summary,
                    role=new_participation.role,
                    old_start=old.start,
                    new_start=new.start,
                    old_end=old.end,
                    new_end=new.end,
                )
            )
        elif old.summary != new.summary:
            changes.append(
                Change(
                    kind="summary_only",
                    person=new_participation.person,
                    uid=old.uid,
                    summary=new.summary,
                    role=new_participation.role,
                    old_summary=old.summary,
                    new_summary=new.summary,
                    old_start=old.start,
                    new_start=new.start,
                    old_end=old.end,
                    new_end=new.end,
                )
            )

    return changes


def diff_rosters(
    old_content: str,
    new_content: str,
    start: datetime,
    end: datetime,
    reference: tzinfo,
) -> list[Change]:
    old_slots = parse_roster(old_content, start, end, reference)
    new_slots = parse_roster(new_content, start, end, reference)

    changes: list[Change] = []
    for old_slot, new_slot in pair_slots(old_slots, new_slots):
        changes.extend(diff_slot(old_slot, new_slot))
    return changes


def build_notifications(
    old_content: str,
    new_content: str,
    start: datetime,
    end: datetime,
    reference: tzinfo,
    include_organizer: bool = False,
) -> list[PersonNotification]:
    """Build one merged notification per affected person.

    The organizer is the party sending the notifications, so it is excluded
    unless ``include_organizer`` is True.
    """

    old_slots = parse_roster(old_content, start, end, reference)
    new_slots = parse_roster(new_content, start, end, reference)

    notifications: dict[str, PersonNotification] = {}

    def ensure(person: Person) -> PersonNotification:
        notification = notifications.get(person.key)
        if notification is None:
            notification = PersonNotification(person=person)
            notifications[person.key] = notification
        return notification

    for old_slot, new_slot in pair_slots(old_slots, new_slots):
        for change in diff_slot(old_slot, new_slot):
            if not include_organizer and change.role == ORGANIZER_ROLE:
                continue
            getattr(ensure(change.person), change.kind).append(change)

    return sorted(
        (
            notification
            for notification in notifications.values()
            if notification.affected
        ),
        key=lambda item: (item.person.identified is False, item.person.key),
    )
