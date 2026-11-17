import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from dateutil.tz import gettz

from icalevents.rosterdiff import (
    ORGANIZER_ROLE,
    build_notifications,
    diff_rosters,
    normalize_address,
    parse_roster,
)

SH = gettz("Asia/Shanghai")
WINDOW_START = datetime(2026, 11, 22, 16, 0, 0, tzinfo=gettz("UTC"))
WINDOW_END = datetime(2026, 11, 29, 16, 0, 0, tzinfo=gettz("UTC"))


def read_fixture(name):
    with open("test/test_data/%s" % name, encoding="utf-8") as handle:
        return handle.read()


class RosterDiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = read_fixture("roster_old.ics")
        cls.new = read_fixture("roster_new.ics")

    def _notifications(self, **kwargs):
        return build_notifications(
            self.old, self.new, WINDOW_START, WINDOW_END, SH, **kwargs
        )

    def _by_key(self):
        return {item.person.key: item for item in self._notifications()}

    def test_finite_window_only_contains_in_range_instances(self):
        narrow_start = datetime(2026, 11, 23, 16, 0, tzinfo=gettz("UTC"))
        narrow_end = datetime(2026, 11, 24, 16, 0, tzinfo=gettz("UTC"))
        notifications = build_notifications(
            self.old, self.new, narrow_start, narrow_end, SH
        )
        keys = {item.person.key for item in notifications}
        # The 11/24 08:00 customer shift is unchanged in this window, so the
        # only affected people come from the 11/24 security reschedule.
        self.assertEqual(keys, {"mailto:ivan@corp", "mailto:judy@corp"})
        ivan = next(
            item
            for item in notifications
            if item.person.key == "mailto:ivan@corp"
        )
        self.assertEqual(len(ivan.time_changed), 1)
        self.assertEqual(
            ivan.time_changed[0].new_start.astimezone(ZoneInfo("Asia/Shanghai")).hour,
            19,
        )

    def test_window_past_the_roster_has_no_changes(self):
        start = datetime(2026, 12, 1, tzinfo=gettz("UTC"))
        end = datetime(2026, 12, 8, tzinfo=gettz("UTC"))
        self.assertEqual(
            build_notifications(self.old, self.new, start, end, SH), []
        )

    def test_rescheduled_single_event_is_a_time_change(self):
        heidi = self._by_key()["mailto:heidi@corp"]
        self.assertEqual(heidi.added, [])
        self.assertEqual(heidi.removed, [])
        self.assertEqual(len(heidi.time_changed), 1)
        change = heidi.time_changed[0]
        self.assertEqual(
            change.old_start.astimezone(ZoneInfo("Asia/Shanghai")).hour, 14
        )
        self.assertEqual(
            change.new_start.astimezone(ZoneInfo("Asia/Shanghai")).hour, 16
        )

    def test_added_and_removed_people(self):
        by_key = self._by_key()
        self.assertEqual(
            [change.uid for change in by_key["mailto:frank@corp"].added],
            ["roster-b-20261127"],
        )
        grace = by_key["mailto:grace@corp"]
        self.assertEqual(len(grace.removed), 1)
        self.assertEqual(grace.removed[0].uid, "roster-c")

    def test_summary_only_uses_its_own_bucket(self):
        ivan = self._by_key()["mailto:ivan@corp"]
        self.assertEqual(len(ivan.summary_only), 1)
        summary_change = ivan.summary_only[0]
        self.assertEqual(summary_change.old_summary, "安全值班-巡检")
        self.assertEqual(summary_change.new_summary, "安全值班-应急巡检")
        self.assertEqual(summary_change.old_start, summary_change.new_start)
        self.assertTrue(
            all(
                change.new_start != summary_change.new_start
                for change in ivan.time_changed
            )
        )

    def test_delegation_is_attributed_on_both_sides(self):
        by_key = self._by_key()
        bob = by_key["mailto:bob@corp"]
        delegated_adds = [
            change
            for change in bob.added
            if "mailto:erin@corp" in change.delegates_to
        ]
        self.assertEqual(len(delegated_adds), 1)
        erin = by_key["mailto:erin@corp"]
        delegated_from = [
            change
            for change in erin.added
            if "mailto:bob@corp" in change.delegates_from
        ]
        self.assertEqual(len(delegated_from), 1)

    def test_one_person_with_multiple_roles_gets_one_notification(self):
        notifications = self._notifications()
        bob_notes = [
            item for item in notifications if item.person.key == "mailto:bob@corp"
        ]
        self.assertEqual(len(bob_notes), 1)
        roles = {change.role for change in bob_notes[0].added + bob_notes[0].removed}
        self.assertIn("REQ-PARTICIPANT", roles)
        self.assertIn("OPT-PARTICIPANT", roles)

    def test_unidentified_attendee_is_not_merged(self):
        notifications = self._notifications()
        unidentified = [item for item in notifications if not item.person.identified]
        self.assertEqual(len(unidentified), 1)
        self.assertTrue(unidentified[0].person.key.startswith("urn:uuid:"))
        self.assertEqual(len(unidentified[0].time_changed), 1)
        self.assertNotIn(
            unidentified[0].person.key,
            [item.person.key for item in notifications if item.person.identified],
        )

    def test_organizer_is_excluded_by_default_but_can_be_included(self):
        default_keys = {item.person.key for item in self._notifications()}
        self.assertNotIn("mailto:alice@corp", default_keys)
        included = self._notifications(include_organizer=True)
        alice = next(
            item for item in included if item.person.key == "mailto:alice@corp"
        )
        self.assertTrue(
            all(change.role == ORGANIZER_ROLE for change in alice.time_changed)
        )

    def test_normalize_address(self):
        self.assertEqual(
            normalize_address("mailto:Bob@Corp"), "mailto:bob@corp"
        )
        self.assertEqual(
            normalize_address("urn:uuid:abc"), "urn:uuid:abc"
        )

    def test_two_unidentified_urns_are_never_merged(self):
        old = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:x
SUMMARY:值班
DTSTART;TZID=Asia/Shanghai:20261123T080000
DTEND;TZID=Asia/Shanghai:20261123T090000
ATTENDEE;CN=待定:urn:uuid:aaa
ATTENDEE;CN=待定:urn:uuid:bbb
END:VEVENT
END:VCALENDAR"""
        new = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:x
SUMMARY:值班
DTSTART;TZID=Asia/Shanghai:20261123T100000
DTEND;TZID=Asia/Shanghai:20261123T110000
ATTENDEE;CN=待定:urn:uuid:aaa
ATTENDEE;CN=待定:urn:uuid:bbb
END:VEVENT
END:VCALENDAR"""
        notifications = build_notifications(
            old, new, WINDOW_START, WINDOW_END, SH
        )
        urn_keys = sorted(
            item.person.key for item in notifications if not item.person.identified
        )
        self.assertEqual(urn_keys, ["urn:uuid:aaa", "urn:uuid:bbb"])
        for item in notifications:
            self.assertEqual(len(item.time_changed), 1)

    def test_slots_carry_participation_roles(self):
        slots = parse_roster(self.old, WINDOW_START, WINDOW_END, SH)
        night_shift = next(
            slot for slot in slots.values() if slot.uid == "roster-b-20261123"
        )
        roles = {
            (participation.person.key, participation.role)
            for participation in night_shift.participations
        }
        self.assertIn(("mailto:dave@corp", "REQ-PARTICIPANT"), roles)
        self.assertIn(("mailto:bob@corp", "OPT-PARTICIPANT"), roles)
        self.assertIn(("mailto:alice@corp", ORGANIZER_ROLE), roles)

    def test_diff_rosters_returns_flat_changes(self):
        changes = diff_rosters(self.old, self.new, WINDOW_START, WINDOW_END, SH)
        kinds = {change.kind for change in changes}
        self.assertEqual(
            kinds, {"added", "removed", "time_changed", "summary_only"}
        )


if __name__ == "__main__":
    unittest.main()
