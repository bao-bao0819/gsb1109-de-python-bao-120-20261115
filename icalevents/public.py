"""
Policy driven export of events for public subscriptions.

A public subscription is only allowed to show resource occupancy.  Event
summaries, descriptions, locations, categories, URLs, organizers and
attendees are private data and must not be published.  The export therefore
returns redacted copies of the original events which still carry the time
range, the busy/free state and the identity of recurring instances.

The original :class:`~icalevents.icalparser.Event` objects are never
modified.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Iterable

from icalendar import Calendar
from icalendar import Event as ICalEvent

from .icalparser import Event

BUSY_SUMMARY = "Busy"
FREE_SUMMARY = "Free"


class PublicExportError(ValueError):
    """
    Raised when an event cannot be exported for a public subscription.

    Error messages deliberately contain no event content.  In particular a
    description, summary, attendee or organizer is never included, so an
    invalid event cannot leak private data through an exception message.
    """


@dataclass(frozen=True)
class PublicExportPolicy:
    """
    Rules used to build a public representation of calendar events.

    By default every descriptive field is removed and only occupancy is
    published.  ``reveal_public_*`` switches may be enabled for events which
    are not marked as private.  Private and confidential events are always
    redacted, regardless of these switches.

    :param label_summary: replace the summary with a busy/free label
    :param busy_summary: label used for events which occupy the resource
    :param free_summary: label used for free or transparent events
    :param reveal_public_summary: keep summaries of non private events
    :param reveal_public_description: keep descriptions of non private events
    :param reveal_public_location: keep locations of non private events
    :param reveal_public_attendees: keep attendees of non private events
    :param reveal_public_organizer: keep organizers of non private events
    :param reveal_public_categories: keep categories of non private events
    :param reveal_public_url: keep URLs of non private events
    :param include_transparent: include transparent events as free entries
    :param include_cancelled: include cancelled events as free entries
    :param anonymize_uid: replace UIDs with stable hashes (keeps series links)
    """

    label_summary: bool = True
    busy_summary: str = BUSY_SUMMARY
    free_summary: str = FREE_SUMMARY

    reveal_public_summary: bool = False
    reveal_public_description: bool = False
    reveal_public_location: bool = False
    reveal_public_attendees: bool = False
    reveal_public_organizer: bool = False
    reveal_public_categories: bool = False
    reveal_public_url: bool = False

    include_transparent: bool = True
    include_cancelled: bool = True
    anonymize_uid: bool = False


DEFAULT_POLICY = PublicExportPolicy()


def coerce_policy(policy: PublicExportPolicy | Mapping | None) -> PublicExportPolicy:
    """
    Normalize a policy argument.

    :param policy: policy, mapping of policy fields or None
    :return: policy instance
    :raises PublicExportError: if the policy is invalid
    """
    if policy is None:
        return DEFAULT_POLICY
    if isinstance(policy, PublicExportPolicy):
        return policy
    if isinstance(policy, Mapping):
        known = {field.name for field in fields(PublicExportPolicy)}
        unknown = set(policy) - known
        if unknown:
            raise PublicExportError(
                "Unknown policy setting: %s" % ", ".join(sorted(unknown))
            )
        return PublicExportPolicy(**dict(policy))
    raise PublicExportError("Policy must be a PublicExportPolicy or a mapping")


# iCalendar properties which describe an event but never define occupancy.
PRIVATE_PROPERTIES = (
    "SUMMARY",
    "DESCRIPTION",
    "LOCATION",
    "GEO",
    "ATTENDEE",
    "ORGANIZER",
    "CATEGORIES",
    "URL",
    "COMMENT",
    "CONTACT",
    "RELATED-TO",
    "RESOURCES",
    "X-ALT-DESC",
)

# Properties which have to survive on a public event: identity, timing,
# occupancy and recurrence structure.
STRUCTURAL_PROPERTIES = (
    "UID",
    "RECURRENCE-ID",
    "DTSTART",
    "DTEND",
    "DURATION",
    "RRULE",
    "RDATE",
    "EXDATE",
    "EXRULE",
    "TRANSP",
    "STATUS",
    "SEQUENCE",
    "DTSTAMP",
    "CREATED",
    "LAST-MODIFIED",
    "CLASS",
)


def _validate(event: Event) -> None:
    if not isinstance(event, Event):
        raise PublicExportError("Only events can be exported")
    if event.start is None or event.end is None:
        raise PublicExportError("Event has no usable time range")


def _occupies(event: Event) -> bool:
    return event.occupies_resource()


def _public_uid(event: Event, policy: PublicExportPolicy) -> str:
    if not policy.anonymize_uid:
        return event.uid
    # A stable, non reversible identifier keeps the relation between the
    # instances of one recurring series without exposing the original UID.
    from hashlib import sha256

    return sha256(str(event.uid).encode("utf-8")).hexdigest()


def _component_is_private(component: ICalEvent) -> bool:
    return str(component.get("class", "")).upper() in ("PRIVATE", "CONFIDENTIAL")


def _component_is_busy(component: ICalEvent) -> bool:
    if str(component.get("transp", "")).upper() == "TRANSPARENT":
        return False
    if str(component.get("status", "")).upper() == "CANCELLED":
        return False
    return True


def _component_label(component: ICalEvent, policy: PublicExportPolicy) -> str:
    return policy.busy_summary if _component_is_busy(component) else policy.free_summary


def _component_label(component: ICalEvent, policy: PublicExportPolicy) -> str:
    return policy.busy_summary if _component_is_busy(component) else policy.free_summary


def _feed_component(
    component: ICalEvent, policy: PublicExportPolicy
) -> ICalEvent | None:
    """
    Build the public variant of a VEVENT, keeping only structural properties.

    :param component: source event
    :param policy: export policy
    :return: public component or None if it must be skipped
    """
    private = _component_is_private(component)
    transparent = str(component.get("transp", "")).upper() == "TRANSPARENT"
    cancelled = str(component.get("status", "")).upper() == "CANCELLED"

    if transparent and not policy.include_transparent:
        return None
    if cancelled and not policy.include_cancelled:
        return None

    public = ICalEvent()

    for name in STRUCTURAL_PROPERTIES:
        if name == "CLASS":
            continue
        if name in component:
            public.add(name, component[name], encode=False)

    summary = None
    if not private and policy.reveal_public_summary:
        summary = component.get("summary")
    if summary is None and policy.label_summary:
        summary = _component_label(component, policy)
    if summary is not None:
        public.add("summary", str(summary))

    if not private:
        reveal = (
            ("description", policy.reveal_public_description),
            ("location", policy.reveal_public_location),
            ("attendee", policy.reveal_public_attendees),
            ("organizer", policy.reveal_public_organizer),
            ("categories", policy.reveal_public_categories),
            ("url", policy.reveal_public_url),
        )
        for name, enabled in reveal:
            if enabled and name in component:
                public.add(name, component[name], encode=False)

    return public


def export_feed(
    content: bytes | str, policy: PublicExportPolicy | None = None
) -> bytes:
    """
    Export a raw iCalendar feed for a public subscription.

    Recurrence information (``RRULE``, ``RDATE``, ``EXDATE``) and recurring
    instance relations (``RECURRENCE-ID``) are preserved, as are the times
    and the busy/free state.  Subject, description, location, attendees,
    organizer, categories and URL are removed unless the policy explicitly
    reveals them for a non private event.

    :param content: iCalendar content as bytes or string
    :param policy: export policy, defaults to :data:`DEFAULT_POLICY`
    :return: redacted iCalendar bytes
    :raises PublicExportError: if the feed cannot be exported
    """
    policy = coerce_policy(policy)

    try:
        source = Calendar.from_ical(content)
    except Exception:
        raise PublicExportError("Unable to parse calendar content") from None

    calendar = Calendar()
    for name in ("version", "calscale", "method"):
        if name.upper() in source:
            calendar.add(name, source[name.upper()], encode=False)
    calendar["prodid"] = b"-//icalevents//public subscription//EN"
    calendar["version"] = b"2.0"

    try:
        for component in source.walk("VTIMEZONE"):
            calendar.add_component(component)

        for component in source.walk("VEVENT"):
            public = _feed_component(component, policy)
            if public is not None:
                calendar.add_component(public)
    except Exception:
        # Never chain: the original exception may contain private content.
        raise PublicExportError("Unable to export calendar content") from None

    try:
        return calendar.to_ical()
    except Exception:
        raise PublicExportError("Unable to create public calendar") from None


def _label(event: Event, policy: PublicExportPolicy) -> str | None:
    if not policy.label_summary:
        return None
    return policy.busy_summary if _occupies(event) else policy.free_summary


def redact_event(event: Event, policy: PublicExportPolicy | None = None) -> Event:
    """
    Create a public copy of an event.

    The returned event always keeps its time range, transparency, recurring
    instance identity (``uid`` and ``recurrence_id``) and status.  Private
    content is removed, including the original iCalendar component which
    would otherwise expose every property.

    :param event: event to redact
    :param policy: export policy, defaults to :data:`DEFAULT_POLICY`
    :return: redacted event
    :raises PublicExportError: if the event cannot be exported
    """
    policy = coerce_policy(policy)
    _validate(event)

    public = Event()
    public.uid = _public_uid(event, policy)
    public.start = event.start
    public.end = event.end
    public.all_day = event.all_day
    public.transparent = event.transparent
    public.recurring = event.recurring
    public.private = event.private
    public.created = event.created
    public.last_modified = event.last_modified
    public.sequence = event.sequence
    public.recurrence_id = event.recurrence_id
    public.floating = event.floating
    public.status = event.status

    # The original component contains all private properties, so it is never
    # copied to a public event.
    public.component = None

    if not event.private and policy.reveal_public_summary:
        public.summary = event.summary
    else:
        public.summary = _label(event, policy)

    if not event.private and policy.reveal_public_description:
        public.description = event.description
    else:
        public.description = None

    if not event.private and policy.reveal_public_location:
        public.location = event.location
    else:
        public.location = None

    if not event.private and policy.reveal_public_attendees:
        public.attendee = event.attendee
    else:
        public.attendee = None

    if not event.private and policy.reveal_public_organizer:
        public.organizer = event.organizer
    else:
        public.organizer = None

    if not event.private and policy.reveal_public_categories:
        public.categories = list(event.categories)
    else:
        public.categories = []

    if not event.private and policy.reveal_public_url:
        public.url = event.url
    else:
        public.url = None

    return public


def public_events(
    events: Iterable[Event], policy: PublicExportPolicy | None = None
) -> list[Event]:
    """
    Redact a collection of events for a public subscription.

    Private events are kept as anonymized occupancy entries; they are not
    removed.  Transparent and cancelled events are kept as free entries by
    default so that the busy/free state of every instance remains visible.

    :param events: events to export
    :param policy: export policy, defaults to :data:`DEFAULT_POLICY`
    :return: redacted events
    """
    policy = coerce_policy(policy)
    result: list[Event] = []

    for event in events:
        _validate(event)
        if event.transparent and not policy.include_transparent:
            continue
        if not _occupies(event) and event.status:
            if (
                str(event.status).upper() == "CANCELLED"
                and not policy.include_cancelled
            ):
                continue
        result.append(redact_event(event, policy))

    return result


def to_ical(events: Iterable[Event], policy: PublicExportPolicy | None = None) -> bytes:
    """
    Serialize redacted events as a public iCalendar document.

    The output is a flattened snapshot of the supplied occurrences.  Only the
    properties needed for occupancy and recurring instance identity are
    written; private properties are never copied from the input.

    :param events: events to export
    :param policy: export policy, defaults to :data:`DEFAULT_POLICY`
    :return: iCalendar bytes
    :raises PublicExportError: if the public calendar cannot be created
    """
    calendar = Calendar()
    calendar.add("prodid", "-//icalevents//public subscription//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")

    try:
        for public in public_events(events, policy):
            component = ICalEvent()
            component.add("uid", public.uid)
            component.add("dtstart", public.start)
            component.add("dtend", public.end)
            component.add("transp", "TRANSPARENT" if public.transparent else "OPAQUE")
            if public.summary is not None:
                component.add("summary", public.summary)
            if public.status is not None:
                component.add("status", str(public.status))
            if public.recurrence_id is not None:
                component.add("recurrence-id", public.recurrence_id)
            if public.sequence is not None:
                component.add("sequence", public.sequence)
            if public.created is not None:
                component.add("created", public.created)
            if public.last_modified is not None:
                component.add("last-modified", public.last_modified)
            calendar.add_component(component)

        return calendar.to_ical()
    except PublicExportError:
        raise
    except Exception:
        # Do not chain the original exception: it may contain event content.
        raise PublicExportError("Unable to create public calendar") from None


def occupies_resource(event: Event) -> bool:
    """
    Check whether an event occupies the calendar resource.

    Transparent events and cancelled events never occupy a resource.

    :param event: event to check
    :return: True if the event blocks the resource
    """
    _validate(event)
    return _occupies(event)


def busy_events(
    events: Iterable[Event], policy: PublicExportPolicy | None = None
) -> list[Event]:
    """
    Return only the redacted events which occupy the resource.

    :param events: events to export
    :param policy: export policy, defaults to :data:`DEFAULT_POLICY`
    :return: redacted busy events
    """
    return [
        event for event in public_events(events, policy) if event.occupies_resource()
    ]


# Descriptive aliases for callers which prefer an "export" spelling.
export_public_events = public_events
redact_events = public_events
export_ical = to_ical
