import unittest
from datetime import date, datetime, timedelta

from dateutil.tz import UTC, gettz
from icalendar import Calendar

from icalevents import icalevents
from icalevents.icalparser import Event, PublicParseError, parse_events
from icalevents.public import (
    DEFAULT_POLICY,
    PublicExportError,
    PublicExportPolicy,
    busy_events,
    export_feed,
    occupies_resource,
    public_events,
    redact_event,
    to_ical,
)


SECRET_SUMMARY = "Private therapy appointment"
SECRET_DESCRIPTION = "Bank PIN 4242, do not share"
SECRET_LOCATION = "Clinic room 7"
SECRET_ATTENDEE = "mailto:private.person@example.com"
SECRET_ORGANIZER = "mailto:secret.organizer@example.com"
SECRET_URL = "https://private.example.com/notes"
SECRET_CATEGORY = "health"


def make_event(
    start: datetime | None = None,
    end: datetime | None = None,
    **values,
) -> Event:
    event = Event()
    event.uid = values.pop("uid", "series-1@example.com")
    event.start = start or datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    event.end = end or (event.start + timedelta(hours=1))
    event.summary = values.pop("summary", SECRET_SUMMARY)
    event.description = values.pop("description", SECRET_DESCRIPTION)
    event.location = values.pop("location", SECRET_LOCATION)
    event.attendee = values.pop("attendee", [SECRET_ATTENDEE])
    event.organizer = values.pop("organizer", SECRET_ORGANIZER)
    event.categories = values.pop("categories", [SECRET_CATEGORY])
    event.url = values.pop("url", SECRET_URL)
    for name in (
        "all_day",
        "transparent",
        "recurring",
        "private",
        "recurrence_id",
        "status",
        "sequence",
    ):
        if name in values:
            setattr(event, name, values.pop(name))
    return event


class PublicExportTests(unittest.TestCase):
    def test_private_event_is_kept_as_busy_occupancy(self):
        event = make_event(private=True)

        [public] = public_events([event])

        self.assertTrue(public.occupies_resource(), "private events still block time")
        self.assertEqual(public.summary, "Busy")
        self.assertTrue(public.private, "the private flag itself stays visible")

    def test_private_content_is_removed(self):
        event = make_event(private=True)

        public = redact_event(event)

        self.assertNotEqual(public.summary, SECRET_SUMMARY)
        self.assertIsNone(public.description)
        self.assertIsNone(public.location)
        self.assertIsNone(public.attendee)
        self.assertIsNone(public.organizer)
        self.assertIsNone(public.url)
        self.assertEqual(public.categories, [])
        self.assertIsNone(public.component)

    def test_redaction_does_not_mutate_the_source_event(self):
        event = make_event(private=True)

        redact_event(event)

        self.assertEqual(event.summary, SECRET_SUMMARY)
        self.assertEqual(event.description, SECRET_DESCRIPTION)
        self.assertEqual(event.location, SECRET_LOCATION)
        self.assertEqual(event.attendee, [SECRET_ATTENDEE])
        self.assertEqual(event.organizer, SECRET_ORGANIZER)
        self.assertEqual(event.categories, [SECRET_CATEGORY])
        self.assertEqual(event.url, SECRET_URL)

    def test_public_event_can_reveal_non_private_fields(self):
        event = make_event(private=False, summary="Team standup")
        policy = PublicExportPolicy(
            reveal_public_summary=True,
            reveal_public_description=True,
            reveal_public_location=True,
            reveal_public_attendees=True,
            reveal_public_organizer=True,
            reveal_public_categories=True,
            reveal_public_url=True,
        )

        [public] = public_events([event], policy)

        self.assertEqual(public.summary, "Team standup")
        self.assertEqual(public.description, SECRET_DESCRIPTION)
        self.assertEqual(public.location, SECRET_LOCATION)
        self.assertEqual(public.attendee, [SECRET_ATTENDEE])
        self.assertEqual(public.organizer, SECRET_ORGANIZER)
        self.assertEqual(public.categories, [SECRET_CATEGORY])
        self.assertEqual(public.url, SECRET_URL)

    def test_reveal_switches_never_expose_private_events(self):
        event = make_event(private=True, summary="Board meeting")
        policy = PublicExportPolicy(
            reveal_public_summary=True,
            reveal_public_description=True,
            reveal_public_location=True,
            reveal_public_attendees=True,
            reveal_public_organizer=True,
            reveal_public_categories=True,
            reveal_public_url=True,
        )

        [public] = public_events([event], policy)

        self.assertEqual(public.summary, "Busy")
        self.assertIsNone(public.description)
        self.assertIsNone(public.location)
        self.assertIsNone(public.attendee)
        self.assertIsNone(public.organizer)
        self.assertEqual(public.categories, [])
        self.assertIsNone(public.url)

    def test_transparent_event_is_kept_but_does_not_occupy(self):
        event = make_event(transparent=True)

        [public] = public_events([event])

        self.assertTrue(public.transparent)
        self.assertFalse(occupies_resource(public))
        self.assertEqual(public.summary, "Free")
        self.assertEqual(public.status, event.status)

    def test_transparent_events_can_be_excluded(self):
        policy = PublicExportPolicy(include_transparent=False)

        public = public_events([make_event(transparent=True)], policy)

        self.assertEqual(public, [])

    def test_cancelled_event_does_not_occupy(self):
        [public] = public_events([make_event(status="CANCELLED")])

        self.assertFalse(public.occupies_resource())
        self.assertEqual(public.summary, "Free")

    def test_busy_events_only_returns_occupancy(self):
        busy = make_event()
        free = make_event(transparent=True)
        private = make_event(private=True)

        result = busy_events([busy, free, private])

        self.assertEqual(len(result), 2, "the free entry is filtered out")
        self.assertTrue(all(event.occupies_resource() for event in result))

    def test_recurring_instance_relation_is_preserved(self):
        first = make_event(
            start=datetime(2024, 1, 1, 9, tzinfo=UTC),
            recurring=True,
            uid="series-uid",
        )
        override = make_event(
            start=datetime(2024, 1, 8, 10, tzinfo=UTC),
            recurring=True,
            uid="series-uid",
            recurrence_id=datetime(2024, 1, 8, 9, tzinfo=UTC),
        )

        [public_first, public_override] = public_events([first, override])

        self.assertEqual(public_first.uid, public_override.uid)
        self.assertIsNone(public_first.recurrence_id)
        self.assertEqual(
            public_override.recurrence_id, datetime(2024, 1, 8, 9, tzinfo=UTC)
        )
        self.assertTrue(public_first.recurring)
        self.assertTrue(public_override.recurring)

    def test_anonymized_uids_keep_series_relation(self):
        policy = PublicExportPolicy(anonymize_uid=True)
        first = make_event(uid="series-uid")
        second = make_event(
            start=datetime(2024, 1, 2, 9, tzinfo=UTC),
            uid="series-uid",
            recurrence_id=datetime(2024, 1, 2, 9, tzinfo=UTC),
        )
        other = make_event(uid="other-uid")

        first_public, second_public, other_public = public_events(
            [first, second, other], policy
        )

        self.assertEqual(first_public.uid, second_public.uid, "same series keeps UID")
        self.assertNotEqual(first_public.uid, "series-uid", "original UID is hidden")
        self.assertNotEqual(first_public.uid, other_public.uid)
        self.assertEqual(len(first_public.uid), 64)

    def test_time_range_is_preserved(self):
        taper = gettz("Europe/Zurich")
        start = datetime(2024, 3, 31, 9, 30, tzinfo=taper)
        end = datetime(2024, 3, 31, 11, 0, tzinfo=taper)

        [public] = public_events([make_event(start=start, end=end)])

        self.assertEqual(public.start, start)
        self.assertEqual(public.end, end)

    def test_all_day_flag_and_times_are_preserved(self):
        start = date(2024, 5, 1)
        end = date(2024, 5, 2)
        event = make_event()
        event.start = start
        event.end = end
        event.all_day = True

        [public] = public_events([event])

        self.assertEqual(public.start, start)
        self.assertEqual(public.end, end)
        self.assertTrue(public.all_day)

    def test_event_without_time_range_is_rejected(self):
        event = make_event()
        event.start = None

        with self.assertRaises(PublicExportError) as raised:
            redact_event(event)

        self.assertNotIn(SECRET_SUMMARY, str(raised.exception))
        self.assertNotIn(SECRET_DESCRIPTION, str(raised.exception))

    def test_to_ical_contains_no_private_content(self):
        raw = to_ical([make_event(private=True)])

        self.assertNotIn(SECRET_SUMMARY.encode(), raw)
        self.assertNotIn(SECRET_DESCRIPTION.encode(), raw)
        self.assertNotIn(SECRET_LOCATION.encode(), raw)
        self.assertNotIn(SECRET_ATTENDEE.encode(), raw)
        self.assertNotIn(SECRET_ORGANIZER.encode(), raw)
        self.assertNotIn(SECRET_URL.encode(), raw)
        self.assertNotIn(b"DESCRIPTION", raw)
        self.assertNotIn(b"ATTENDEE", raw)
        self.assertNotIn(b"ORGANIZER", raw)
        self.assertIn(b"TRANSP:OPAQUE", raw)

    def test_to_ical_keeps_occupancy_and_series_links(self):
        first = make_event(uid="series", recurring=True)
        second = make_event(
            start=datetime(2024, 1, 2, 9, tzinfo=UTC),
            uid="series",
            recurring=True,
            recurrence_id=datetime(2024, 1, 2, 9, tzinfo=UTC),
        )

        calendar = Calendar.from_ical(to_ical([first, second]))
        components = calendar.walk("VEVENT")

        self.assertEqual(len(components), 2)
        self.assertEqual(str(components[0]["uid"]), "series")
        self.assertEqual(str(components[1]["uid"]), "series")
        self.assertIn("RECURRENCE-ID", components[1])
        self.assertEqual(str(components[0]["summary"]), "Busy")


class PublicFeedExportTests(unittest.TestCase):
    def test_transparent_fixture_keeps_both_entries(self):
        raw = export_feed(open("test/test_data/transparent.ics", "rb").read())
        calendar = Calendar.from_ical(raw)
        events = calendar.walk("VEVENT")

        self.assertEqual(len(events), 2, "both entries stay in the feed")
        summaries = {str(event["summary"]) for event in events}
        self.assertEqual(summaries, {"Busy", "Free"})
        self.assertNotIn(b"CLASS", raw)

    def test_feed_export_preserves_recurrence_structure(self):
        raw = export_feed(open("test/test_data/recurrenceid_ms.ics", "rb").read())

        self.assertIn(b"RRULE", raw)
        self.assertIn(b"RECURRENCE-ID", raw)
        self.assertIn(b"UID:", raw)
        self.assertIn(b"TRANSP:OPAQUE", raw)

    def test_feed_export_removes_private_properties(self):
        raw = export_feed(open("test/test_data/response.ics", "rb").read())

        for name in (b"DESCRIPTION", b"ATTENDEE", b"ORGANIZER", b"LOCATION"):
            self.assertNotIn(name, raw)

        calendar = Calendar.from_ical(raw)
        [event] = calendar.walk("VEVENT")
        self.assertIn(str(event["summary"]), ("Busy", "Free"))

    def test_feed_export_never_copies_class(self):
        raw = export_feed(open("test/test_data/recurring_override.ics", "rb").read())

        self.assertNotIn(b"CLASS:PRIVATE", raw)
        self.assertNotIn(b"CLASS:CONFIDENTIAL", raw)

    def test_feed_export_rejects_empty_content(self):
        with self.assertRaises(PublicExportError):
            export_feed(b"")

    def test_feed_export_error_contains_no_private_content(self):
        content = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "DESCRIPTION:" + SECRET_DESCRIPTION + "\r\n"
        )

        with self.assertRaises(PublicExportError) as raised:
            export_feed(content)

        self.assertNotIn(SECRET_DESCRIPTION, str(raised.exception))


class PublicEventsApiTests(unittest.TestCase):
    def test_events_public_flag_redacts(self):
        events = icalevents.events(
            file="test/test_data/transparent.ics",
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            public=True,
        )

        self.assertEqual(len(events), 2)
        self.assertEqual([event.summary for event in events], ["Free", "Busy"])
        self.assertEqual([event.occupies_resource() for event in events], [False, True])

    def test_events_without_public_flag_is_unchanged(self):
        events = icalevents.events(
            file="test/test_data/transparent.ics",
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
        )

        self.assertEqual([event.summary for event in events], ["Free", "Busy"])

    def test_events_policy_argument_implies_export(self):
        events = icalevents.events(
            file="test/test_data/response.ics",
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            policy=PublicExportPolicy(),
        )

        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].attendee)

    def test_events_public_export_keeps_recurring_instances(self):
        events = icalevents.events(
            file="test/test_data/recurrenceid_ms.ics",
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            public=True,
            sort=True,
        )

        self.assertEqual(len(events), 42, "the moved instance is still one entry")
        self.assertEqual(len({event.uid for event in events}), 1)
        recurrence_ids = [event.recurrence_id for event in events]
        self.assertTrue(any(value is not None for value in recurrence_ids))


class DescriptionLeakTests(unittest.TestCase):
    def test_parse_error_does_not_contain_description(self):
        content = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            "UID:x\r\nDTSTART:BADDATE\r\n"
            "DESCRIPTION:" + SECRET_DESCRIPTION + "\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        with self.assertRaises(PublicParseError) as raised:
            parse_events(content)

        self.assertNotIn(SECRET_DESCRIPTION, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_calendar_error_does_not_contain_description(self):
        content = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\n"
            "DTSTART:20240101T090000Z\r\n"
            "DESCRIPTION:" + SECRET_DESCRIPTION
        )

        with self.assertRaises(PublicParseError) as raised:
            parse_events(content)

        self.assertNotIn(SECRET_DESCRIPTION, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_valid_description_is_still_available(self):
        content = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\n"
            "DTSTART:20240101T090000Z\r\nDTEND:20240101T100000Z\r\n"
            "DESCRIPTION:" + SECRET_DESCRIPTION + "\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        [event] = parse_events(content, start=date(2024, 1, 1), end=date(2024, 1, 2))

        self.assertEqual(event.description, SECRET_DESCRIPTION)

    def test_public_export_suppresses_valid_description(self):
        content = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\n"
            "DTSTART:20240101T090000Z\r\nDTEND:20240101T100000Z\r\n"
            "DESCRIPTION:" + SECRET_DESCRIPTION + "\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )

        [event] = icalevents.events(
            string_content=content,
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            public=True,
        )

        self.assertIsNone(event.description)
        self.assertEqual(event.summary, "Busy")


class OccupancyTests(unittest.TestCase):
    def test_default_policy_is_redaction_first(self):
        self.assertFalse(DEFAULT_POLICY.reveal_public_summary)
        self.assertFalse(DEFAULT_POLICY.reveal_public_description)
        self.assertTrue(DEFAULT_POLICY.include_transparent)
        self.assertTrue(DEFAULT_POLICY.include_cancelled)
        self.assertFalse(DEFAULT_POLICY.anonymize_uid)

    def test_event_busy_and_occupancy_helpers(self):
        busy = make_event()
        free = make_event(transparent=True)

        self.assertTrue(busy.busy)
        self.assertEqual(busy.occupancy, "BUSY")
        self.assertFalse(free.busy)
        self.assertEqual(free.occupancy, "FREE")
