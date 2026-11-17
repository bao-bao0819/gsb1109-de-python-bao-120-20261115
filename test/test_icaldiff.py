import unittest
from datetime import date, datetime

from dateutil.tz import UTC

from icalevents.icaldiff import (
    ChangeKind,
    InstanceKey,
    OccurrenceKind,
    diff_calendars,
    diff_events,
    sync_window,
)
from icalevents.icalevents import events, update_events


def calendar(*components: str) -> str:
    return (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//icalevents tests//EN\n"
        + "\n".join(components)
        + "\nEND:VCALENDAR\n"
    )


def vevent(
    uid: str,
    dtstart: str,
    *,
    dtend: str | None = None,
    rrule: str | None = None,
    exdate: str | None = None,
    recurrence_id: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    location: str | None = None,
    status: str | None = None,
    sequence: int | None = None,
) -> str:
    lines = ["BEGIN:VEVENT", "UID:" + uid, "DTSTART:" + dtstart]
    if dtend:
        lines.append("DTEND:" + dtend)
    if rrule:
        lines.append("RRULE:" + rrule)
    if exdate:
        lines.append("EXDATE:" + exdate)
    if recurrence_id:
        lines.append("RECURRENCE-ID:" + recurrence_id)
    if summary is not None:
        lines.append("SUMMARY:" + summary)
    if description is not None:
        lines.append("DESCRIPTION:" + description)
    if location is not None:
        lines.append("LOCATION:" + location)
    if status is not None:
        lines.append("STATUS:" + status)
    if sequence is not None:
        lines.append("SEQUENCE:%d" % sequence)
    lines.append("END:VEVENT")
    return "\n".join(lines)


class CalendarDiffTests(unittest.TestCase):
    def test_display_change_is_attribute_only(self):
        old = calendar(vevent("a", "20240101T100000Z", summary="Old"))
        new = calendar(vevent("a", "20240101T100000Z", summary="New"))

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 2))

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.changed), 1)
        change = result.changed[0]
        self.assertIs(change.kind, ChangeKind.DISPLAY)
        self.assertTrue(change.attribute_only)
        self.assertEqual(change.fields, ("summary",))
        self.assertEqual(change.display, ("summary",))

    def test_multiple_display_fields_are_one_change(self):
        old = calendar(vevent("a", "20240101T100000Z", summary="A"))
        new = calendar(
            vevent(
                "a",
                "20240101T100000Z",
                summary="B",
                description="details",
                location="Room 1",
            )
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 2))

        self.assertEqual(len(result.changed), 1)
        self.assertEqual(
            set(result.changed[0].fields), {"summary", "description", "location"}
        )

    def test_moved_exception_keeps_original_identity(self):
        old = calendar(
            vevent(
                "s",
                "20240101T100000Z",
                rrule="FREQ=WEEKLY;COUNT=3",
                summary="Series",
            )
        )
        new = calendar(
            vevent(
                "s",
                "20240101T100000Z",
                rrule="FREQ=WEEKLY;COUNT=3",
                summary="Series",
            ),
            vevent(
                "s",
                "20240109T120000Z",
                recurrence_id="20240108T100000Z",
                summary="Series",
            ),
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 20))

        self.assertEqual(result.added, [], "a move is not a new instance")
        self.assertEqual(result.removed, [], "a move is not a deletion")
        self.assertEqual(len(result.changed), 1)
        change = result.changed[0]
        self.assertIs(change.kind, ChangeKind.STRUCTURE)
        self.assertEqual(change.key.occurrence, datetime(2024, 1, 8, 10, tzinfo=UTC))
        self.assertEqual(change.new_recurrence_id, datetime(2024, 1, 8, 10, tzinfo=UTC))
        self.assertTrue(change.moved)

    def test_moved_exception_display_change_is_still_structural(self):
        old = calendar(
            vevent("s", "20240101T100000Z", rrule="FREQ=WEEKLY;COUNT=3"),
            vevent(
                "s",
                "20240108T100000Z",
                recurrence_id="20240108T100000Z",
                summary="Before",
            ),
        )
        new = calendar(
            vevent("s", "20240101T100000Z", rrule="FREQ=WEEKLY;COUNT=3"),
            vevent(
                "s",
                "20240109T120000Z",
                recurrence_id="20240108T100000Z",
                summary="After",
            ),
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 20))

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        change = result.changed[0]
        self.assertIs(change.kind, ChangeKind.STRUCTURE)
        self.assertIn("start", change.fields)
        self.assertIn("summary", change.fields)
        self.assertTrue(change.attribute_only is False)

    def test_unique_uid_move_with_rewritten_recid_is_an_update(self):
        old = calendar(vevent("a", "20240108T100000Z", summary="Meeting"))
        new = calendar(vevent("a", "20240109T120000Z", summary="Meeting"))

        result = diff_events(
            events(string_content=old, start=date(2024, 1, 1), end=date(2024, 1, 20)),
            events(string_content=new, start=date(2024, 1, 1), end=date(2024, 1, 20)),
        )

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.changed), 1)
        self.assertEqual(
            result.changed[0].key.occurrence, datetime(2024, 1, 8, 10, tzinfo=UTC)
        )
        self.assertIs(result.changed[0].kind, ChangeKind.STRUCTURE)

    def test_all_day_exception_uses_date_identity(self):
        old = calendar(
            vevent(
                "s",
                "20240101",
                rrule="FREQ=DAILY;COUNT=3",
                summary="All day",
            )
        )
        new = calendar(
            vevent(
                "s",
                "20240101",
                rrule="FREQ=DAILY;COUNT=3",
                summary="All day",
            ),
            vevent(
                "s",
                "20240103",
                recurrence_id="20240102",
                summary="Moved all day",
            ),
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 5))

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.changed), 1)
        self.assertEqual(result.changed[0].key.occurrence, date(2024, 1, 2))
        self.assertEqual(result.changed[0].occurrence_kind, OccurrenceKind.EXCEPTION)

    def test_exdate_is_a_cancellation(self):
        old = calendar(
            vevent("s", "20240101T100000Z", rrule="FREQ=WEEKLY;COUNT=3")
        )
        new = calendar(
            vevent(
                "s",
                "20240101T100000Z",
                rrule="FREQ=WEEKLY;COUNT=3",
                exdate="20240108T100000Z",
            )
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 20))

        self.assertEqual(len(result.removed), 1)
        self.assertEqual(
            InstanceKey.from_event(result.removed[0]).occurrence,
            datetime(2024, 1, 8, 10, tzinfo=UTC),
        )
        self.assertEqual(result.series_changes[0].kind, "exdate")

    def test_cancelled_instance_is_reported_and_removed(self):
        old = calendar(vevent("a", "20240101T100000Z", summary="Meeting"))
        new = calendar(
            vevent("a", "20240101T100000Z", summary="Meeting", status="CANCELLED")
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 2))

        self.assertEqual(result.changed, [])
        self.assertEqual(len(result.cancelled), 1)
        self.assertEqual(len(result.removed), 1)
        self.assertIs(result.cancelled[0].kind, ChangeKind.CANCELLATION)
        self.assertIs(result.cancelled[0].cancelled, True)

    def test_rrule_change_rebuilds_series_with_only_affected_instances(self):
        old = calendar(
            vevent("s", "20240101T100000Z", rrule="FREQ=WEEKLY;COUNT=3")
        )
        new = calendar(
            vevent("s", "20240101T100000Z", rrule="FREQ=WEEKLY;COUNT=5")
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 2, 1))

        self.assertEqual(len(result.added), 2)
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.series_changes), 1)
        self.assertEqual(result.series_changes[0].kind, "rrule")

    def test_dtstart_change_is_a_series_change(self):
        old = calendar(vevent("s", "20240101T100000Z", rrule="FREQ=WEEKLY;COUNT=2"))
        new = calendar(vevent("s", "20240102T100000Z", rrule="FREQ=WEEKLY;COUNT=2"))

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 2, 1))

        self.assertTrue(result.series_changes)
        self.assertEqual(result.series_changes[0].kind, "dtstart")

    def test_rrule_change_wins_over_display_change(self):
        old = calendar(
            vevent(
                "s",
                "20240101T100000Z",
                rrule="FREQ=WEEKLY;COUNT=3",
                summary="Before",
            )
        )
        new = calendar(
            vevent(
                "s",
                "20240101T100000Z",
                rrule="FREQ=WEEKLY;COUNT=5",
                summary="After",
            )
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 2, 1))

        change = next(
            change
            for change in result.changed
            if change.key.occurrence == datetime(2024, 1, 1, 10, tzinfo=UTC)
        )
        self.assertIs(change.kind, ChangeKind.STRUCTURE)
        self.assertIn("rrule", change.structural)
        self.assertFalse(change.attribute_only)

    def test_timezone_definition_change_is_reported_separately(self):
        old_tz = (
            "BEGIN:VTIMEZONE\n"
            "TZID:Europe/Berlin\n"
            "BEGIN:STANDARD\n"
            "DTSTART:19701025T030000\n"
            "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\n"
            "TZOFFSETFROM:+0200\n"
            "TZOFFSETTO:+0100\n"
            "END:STANDARD\n"
            "END:VTIMEZONE"
        )
        new_tz = old_tz.replace("TZOFFSETTO:+0100", "TZOFFSETTO:+0101")
        event = vevent("a", "20240115T100000", summary="Berlin")
        event = event.replace(
            "DTSTART:20240115T100000",
            "DTSTART;TZID=Europe/Berlin:20240115T100000",
        )

        old = calendar(old_tz, event)
        new = calendar(new_tz, event)

        result = diff_calendars(
            old, new, date(2024, 1, 1), date(2024, 2, 1), strict=True
        )

        self.assertEqual(len(result.timezone_changes), 1)
        self.assertEqual(result.timezone_changes[0].tzid, "Europe/Berlin")
        self.assertIn("a", result.timezone_changes[0].uids)
        self.assertEqual(len(result.timezone_changes[0].events), 1)

    def test_empty_snapshot_does_not_erase_window(self):
        old = calendar(
            vevent(
                "s",
                "20240101T100000Z",
                rrule="FREQ=WEEKLY;COUNT=3",
                summary="Series",
            )
        )

        result = diff_calendars(old, "", date(2024, 1, 1), date(2024, 1, 20))

        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.protected), 3)
        self.assertTrue(result.warnings)

    def test_change_outside_window_cannot_remove_inside_instance(self):
        old = calendar(vevent("inside", "20240110T100000Z", dtend="20240110T110000Z"))
        new = calendar(vevent("outside", "20250110T100000Z", dtend="20250110T110000Z"))

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 31))

        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.protected), 1)
        self.assertEqual(result.protected[0].uid, "inside")
        self.assertEqual(result.added, [], "outside changes are not in this window")

    def test_complete_snapshot_may_remove_missing_single_event(self):
        old = calendar(
            vevent("a", "20240110T100000Z", dtend="20240110T110000Z"),
            vevent("b", "20240111T100000Z", dtend="20240111T110000Z"),
        )
        new = calendar(vevent("b", "20240111T100000Z", dtend="20240111T110000Z"))

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 31))

        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.removed[0].uid, "a")

    def test_no_uid_identity_is_stable_across_display_update(self):
        old = calendar(
            "BEGIN:VEVENT\n"
            "DTSTART:20240101T100000Z\n"
            "SUMMARY:Before\n"
            "END:VEVENT"
        )
        new = calendar(
            "BEGIN:VEVENT\n"
            "DTSTART:20240101T100000Z\n"
            "SUMMARY:After\n"
            "END:VEVENT"
        )

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 2))

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.changed), 1)

    def test_non_ascii_uid_identity_is_stable_across_display_update(self):
        old = calendar(vevent("🙉🙈👀", "20240101T100000Z", summary="Before"))
        new = calendar(vevent("🙉🙈👀", "20240101T100000Z", summary="After"))

        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 2))

        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.changed), 1)

    def test_apply_replaces_moved_instance_without_duplicate(self):
        old = calendar(vevent("a", "20240101T100000Z", summary="Meeting"))
        new = calendar(vevent("a", "20240102T100000Z", summary="Meeting"))
        previous = events(
            string_content=old, start=date(2024, 1, 1), end=date(2024, 1, 3)
        )
        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 3))

        applied = result.apply(previous)

        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].start, datetime(2024, 1, 2, 10, tzinfo=UTC))

    def test_apply_removes_cancelled_instance(self):
        old = calendar(vevent("a", "20240101T100000Z", summary="Meeting"))
        new = calendar(
            vevent("a", "20240101T100000Z", summary="Meeting", status="CANCELLED")
        )
        previous = events(
            string_content=old, start=date(2024, 1, 1), end=date(2024, 1, 2)
        )
        result = diff_calendars(old, new, date(2024, 1, 1), date(2024, 1, 2))

        self.assertEqual(result.apply(previous), [])

    def test_sync_window_updates_in_place(self):
        old = calendar(vevent("a", "20240101T100000Z", summary="Before"))
        new = calendar(vevent("a", "20240101T100000Z", summary="After"))
        previous = events(
            string_content=old, start=date(2024, 1, 1), end=date(2024, 1, 2)
        )
        current = events(
            string_content=new, start=date(2024, 1, 1), end=date(2024, 1, 2)
        )

        result = sync_window(
            previous, current, start=date(2024, 1, 1), end=date(2024, 1, 2)
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].summary, "After")

    def test_update_events_only_rebuilds_window(self):
        key = "window-test"
        inside = events(
            string_content=calendar(
                vevent("a", "20240110T100000Z", dtend="20240110T110000Z")
            ),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        outside = events(
            string_content=calendar(
                vevent("b", "20240310T100000Z", dtend="20240310T110000Z")
            ),
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
        )
        update_events(key, inside + outside)

        new_inside = events(
            string_content=calendar(
                vevent(
                    "a",
                    "20240110T100000Z",
                    dtend="20240110T110000Z",
                    summary="Updated",
                )
            ),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        update_events(
            key, new_inside, start=date(2024, 1, 1), end=date(2024, 1, 31)
        )

        from icalevents import icalevents

        result = icalevents.latest_events(key)
        self.assertEqual(len(result), 2)
        self.assertEqual({event.uid for event in result}, {"a", "b"})
        self.assertEqual(
            next(event for event in result if event.uid == "a").summary, "Updated"
        )

    def test_update_events_replaces_moved_instance_by_identity(self):
        key = "window-move-test"
        old = events(
            string_content=calendar(
                vevent("a", "20240110T100000Z", dtend="20240110T110000Z")
            ),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        update_events(key, old)

        new = events(
            string_content=calendar(
                vevent("a", "20240112T100000Z", dtend="20240112T110000Z")
            ),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        update_events(key, new, start=date(2024, 1, 1), end=date(2024, 1, 31))

        from icalevents import icalevents

        result = icalevents.latest_events(key)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].start, datetime(2024, 1, 12, 10, tzinfo=UTC))

    def test_update_events_removes_cancelled_instance(self):
        key = "window-cancel-test"
        old = events(
            string_content=calendar(
                vevent("a", "20240110T100000Z", dtend="20240110T110000Z")
            ),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        update_events(key, old)

        new = events(
            string_content=calendar(
                vevent(
                    "a",
                    "20240110T100000Z",
                    dtend="20240110T110000Z",
                    status="CANCELLED",
                )
            ),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        update_events(key, new, start=date(2024, 1, 1), end=date(2024, 1, 31))

        from icalevents import icalevents

        self.assertEqual(icalevents.latest_events(key), [])

    def test_update_events_keeps_confirmed_rows_on_empty_result(self):
        key = "window-empty-test"
        old = events(
            string_content=calendar(
                vevent("a", "20240110T100000Z", dtend="20240110T110000Z")
            ),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
        )
        update_events(key, old)

        update_events(key, [], start=date(2024, 1, 1), end=date(2024, 1, 31))

        from icalevents import icalevents

        self.assertEqual(len(icalevents.latest_events(key)), 1)
