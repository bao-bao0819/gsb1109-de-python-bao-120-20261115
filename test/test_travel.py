import unittest
from datetime import date, datetime, time, timedelta

from dateutil.tz import UTC

from icalevents.icalparser import Event, parse_events
from icalevents.travel import (
    REASON_INSUFFICIENT_TIME,
    REASON_UNKNOWN_LEG,
    REASON_UNKNOWN_LOCATION,
    TimeRule,
    TravelTable,
    analyze_itinerary,
)


def make_event(uid, start, end, location=None, status=None):
    event = Event()
    event.uid = uid
    event.summary = uid
    event.start = start
    event.end = end
    event.location = location
    event.status = status
    event.all_day = False
    return event


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


class TravelTableTests(unittest.TestCase):
    def test_directed_travel_times(self):
        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=45))
        table.set_travel_time("Lab", "Office", timedelta(minutes=10))

        self.assertEqual(
            table.travel_time("Office", "Lab", at(16, 9)), timedelta(minutes=45)
        )
        self.assertEqual(
            table.travel_time("Lab", "Office", at(16, 9)), timedelta(minutes=10)
        )

    def test_same_location_is_zero(self):
        table = TravelTable()
        self.assertEqual(
            table.travel_time("Office", "Office", at(16, 9)), timedelta(0)
        )

    def test_unknown_leg_is_not_zero(self):
        table = TravelTable()
        self.assertIsNone(table.travel_time("Office", "Lab", at(16, 9)))

    def test_time_rule_scales_travel_time(self):
        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=20))
        table.add_time_rule(
            TimeRule(time(17, 0), time(19, 0), factor=2.0, extra=timedelta(minutes=5))
        )

        self.assertEqual(
            table.travel_time("Office", "Lab", at(16, 18, 0)),
            timedelta(minutes=45),
        )
        self.assertEqual(
            table.travel_time("Office", "Lab", at(16, 10, 0)),
            timedelta(minutes=20),
        )

    def test_time_rule_crossing_midnight(self):
        rule = TimeRule(time(22, 0), time(2, 0), factor=1.5)

        self.assertTrue(rule.applies(at(16, 23, 0)))
        self.assertFalse(rule.applies(at(16, 12, 0)))


class AnalyzeItineraryTests(unittest.TestCase):
    def test_expanded_instances_after_exceptions(self):
        with open("test/test_data/travel.ics", "rb") as file:
            content = file.read()

        events = parse_events(
            content.decode("utf-8"),
            start=date(2026, 11, 16),
            end=date(2026, 11, 19),
        )

        # EXDATE removed the Nov 18 occurrence, the RECURRENCE-ID override
        # replaced the Nov 17 occurrence with the moved instance at the Lab
        starts = sorted(event.start for event in events)
        self.assertEqual(len(events), 4)
        self.assertNotIn(datetime(2026, 11, 18, 9, 0, tzinfo=UTC), starts)
        self.assertNotIn(datetime(2026, 11, 17, 9, 0, tzinfo=UTC), starts)

        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=20))
        table.set_travel_time("Lab", "Office", timedelta(minutes=45))

        chains = analyze_itinerary(events, table)

        # moved standup ends 10:30 at Lab, sync starts 10:45 at Office:
        # gap of 15 minutes against a required 45 minutes
        self.assertEqual(len(chains), 1)
        self.assertEqual(len(chains[0].conflicts), 1)
        conflict = chains[0].conflicts[0]
        self.assertEqual(conflict.previous.uid, "standup")
        self.assertEqual(conflict.previous.location, "Lab")
        self.assertEqual(conflict.following.uid, "sync")
        self.assertEqual(conflict.reason, REASON_INSUFFICIENT_TIME)
        self.assertEqual(conflict.min_buffer, timedelta(minutes=30))
        self.assertEqual(chains[0].min_buffer, timedelta(minutes=30))

    def test_cancelled_event_rechecks_new_neighbours(self):
        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=5))
        table.set_travel_time("Lab", "Home", timedelta(minutes=5))
        table.set_travel_time("Office", "Home", timedelta(minutes=60))

        events = [
            make_event("a", at(16, 9, 0), at(16, 9, 50), "Office"),
            make_event("b", at(16, 10, 0), at(16, 10, 30), "Lab"),
            make_event("c", at(16, 10, 40), at(16, 11, 0), "Home"),
        ]

        self.assertEqual(analyze_itinerary(events, table), [])

        chains = analyze_itinerary(events, table, cancelled=["b"])

        # the new Office -> Home leg needs 60 minutes but only 50 are left
        self.assertEqual(len(chains), 1)
        conflict = chains[0].conflicts[0]
        self.assertEqual((conflict.previous.uid, conflict.following.uid), ("a", "c"))
        self.assertEqual(conflict.min_buffer, timedelta(minutes=10))

    def test_cancelled_event_drops_stale_conflicts(self):
        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=30))

        events = [
            make_event("a", at(16, 9, 0), at(16, 9, 50), "Office"),
            make_event("b", at(16, 9, 55), at(16, 10, 30), "Lab"),
            make_event("c", at(16, 11, 0), at(16, 11, 30), "Office"),
        ]

        self.assertEqual(len(analyze_itinerary(events, table)), 1)
        self.assertEqual(analyze_itinerary(events, table, cancelled=["b"]), [])

    def test_status_cancelled_events_are_skipped(self):
        table = TravelTable()
        table.set_travel_time("Office", "Home", timedelta(minutes=60))

        events = [
            make_event("a", at(16, 9, 0), at(16, 9, 50), "Office"),
            make_event(
                "b", at(16, 10, 0), at(16, 10, 30), "Lab", status="CANCELLED"
            ),
            make_event("c", at(16, 10, 40), at(16, 11, 0), "Home"),
        ]

        chains = analyze_itinerary(events, table)

        self.assertEqual(len(chains), 1)
        conflict = chains[0].conflicts[0]
        self.assertEqual((conflict.previous.uid, conflict.following.uid), ("a", "c"))

    def test_cancelled_single_instance_of_recurring_event(self):
        with open("test/test_data/travel.ics", "rb") as file:
            content = file.read()

        events = parse_events(
            content.decode("utf-8"),
            start=date(2026, 11, 16),
            end=date(2026, 11, 19),
        )

        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=20))
        table.set_travel_time("Lab", "Office", timedelta(minutes=45))

        moved = next(event for event in events if event.uid == "standup"
                     and event.start == datetime(2026, 11, 17, 10, 0, tzinfo=UTC))

        self.assertEqual(len(analyze_itinerary(events, table)), 1)
        self.assertEqual(analyze_itinerary(events, table, cancelled=[moved]), [])

    def test_unknown_location_is_not_zero_distance(self):
        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=5))

        events = [
            make_event("a", at(16, 9, 0), at(16, 9, 30), "Office"),
            make_event("b", at(16, 23, 0), at(16, 23, 30), None),
        ]

        chains = analyze_itinerary(events, table)

        self.assertEqual(len(chains), 1)
        conflict = chains[0].conflicts[0]
        self.assertEqual(conflict.reason, REASON_UNKNOWN_LOCATION)
        self.assertIsNone(conflict.min_buffer)
        self.assertIsNone(chains[0].min_buffer)

    def test_unknown_leg_is_not_zero_distance(self):
        table = TravelTable()

        events = [
            make_event("a", at(16, 9, 0), at(16, 9, 30), "Office"),
            make_event("b", at(16, 23, 0), at(16, 23, 30), "Lab"),
        ]

        chains = analyze_itinerary(events, table)

        self.assertEqual(len(chains), 1)
        self.assertEqual(chains[0].conflicts[0].reason, REASON_UNKNOWN_LEG)

    def test_conflict_chains_and_min_buffer(self):
        table = TravelTable()
        table.set_travel_time("Office", "Lab", timedelta(minutes=20))
        table.set_travel_time("Lab", "Home", timedelta(minutes=30))
        table.set_travel_time("Home", "Office", timedelta(minutes=25))

        events = [
            make_event("a", at(16, 9, 0), at(16, 9, 30), "Office"),
            make_event("b", at(16, 9, 40), at(16, 10, 0), "Lab"),
            make_event("c", at(16, 10, 10), at(16, 10, 30), "Home"),
            make_event("d", at(16, 11, 0), at(16, 11, 30), "Home"),
            make_event("e", at(16, 11, 40), at(16, 12, 0), "Office"),
        ]

        chains = analyze_itinerary(events, table)

        self.assertEqual(len(chains), 2)

        self.assertEqual(
            [event.uid for event in chains[0].events], ["a", "b", "c"]
        )
        self.assertEqual(
            chains[0].min_buffer, timedelta(minutes=10) + timedelta(minutes=20)
        )

        self.assertEqual([event.uid for event in chains[1].events], ["d", "e"])
        self.assertEqual(chains[1].min_buffer, timedelta(minutes=15))


if __name__ == "__main__":
    unittest.main()
