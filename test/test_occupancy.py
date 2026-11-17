import unittest
from datetime import date, datetime, timedelta

from dateutil.tz import gettz

from icalevents.icalparser import Event
from icalevents.occupancy import (
    ResourceRule,
    daily_occupancy,
    expand_instances,
    merge_intervals,
)


TZ = gettz("Europe/Berlin")


def event(
    uid,
    start,
    end,
    summary="booking",
    location="studio",
    status=None,
    recurrence_id=None,
):
    item = Event()
    item.uid = uid
    item.start = start
    item.end = end
    item.summary = summary
    item.location = location
    item.status = status
    item.recurrence_id = recurrence_id
    item.all_day = False
    return item


def at(hour, minute=0, day=1, month=1, year=2024):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


class ExpandInstancesTests(unittest.TestCase):
    def test_recurring_occurrences_are_kept_individually(self):
        first = event("weekly", at(9), at(10))
        first.recurring = True
        second = event("weekly", at(9, day=8), at(10, day=8))
        second.recurring = True

        instances = expand_instances(
            [first, second],
            {"studio": (timedelta(minutes=15), timedelta(minutes=10))},
        )

        self.assertEqual(len(instances), 2, "one instance per occurrence")
        self.assertEqual(instances[0].preparation, timedelta(minutes=15))
        self.assertEqual(instances[0].cleanup, timedelta(minutes=10))
        self.assertEqual(instances[1].meeting_start, at(9, day=8))

    def test_buffers_are_per_resource(self):
        studio = event("a", at(9), at(10), location="studio")
        van = event("b", at(9), at(10), location="van")

        instances = expand_instances(
            [studio, van],
            {
                "studio": ResourceRule(preparation=timedelta(minutes=30), cleanup=0),
                "van": ResourceRule(preparation=0, cleanup=timedelta(minutes=5)),
            },
        )

        self.assertEqual(instances[0].occupied_start, at(8, 30))
        self.assertEqual(instances[0].occupied_end, at(10))
        self.assertEqual(instances[1].occupied_start, at(9))
        self.assertEqual(instances[1].occupied_end, at(10, 5))

    def test_cancelled_instance_leaves_no_occupancy(self):
        cancelled = event("u", at(9), at(10), status="CANCELLED")
        instances = expand_instances([cancelled], {"studio": (30, 30)})

        self.assertEqual(instances, [], "cancelled occurrence is skipped")

    def test_cancelled_override_suppresses_generated_instance(self):
        generated = event("u", at(9), at(10))
        generated.recurring = True
        override = event(
            "u",
            at(9),
            at(10),
            status="CANCELLED",
            recurrence_id=at(9),
        )

        instances = expand_instances([generated, override], {"studio": (30, 30)})

        self.assertEqual(instances, [], "override cancels its occurrence")

    def test_cancelled_master_suppresses_all_instances(self):
        master = event("u", at(9), at(10), status="CANCELLED")
        master.recurring = True
        generated = event("u", at(9, day=8), at(10, day=8))
        generated.recurring = True

        instances = expand_instances([master, generated], {"studio": (30, 30)})

        self.assertEqual(instances, [], "cancelled recurrence suppresses instances")

    def test_resource_can_come_from_a_custom_selector(self):
        item = event("a", at(9), at(10), summary="Kiln A")
        instances = expand_instances(
            [item],
            {"Kiln A": (10, 5)},
            resource_of=lambda occurrence: occurrence.summary,
        )

        self.assertEqual(instances[0].resource, "Kiln A")
        self.assertEqual(instances[0].preparation, timedelta(minutes=10))

    def test_number_values_are_minutes(self):
        instances = expand_instances([event("a", at(9), at(10))], {"studio": (15, 5)})

        self.assertEqual(instances[0].preparation, timedelta(minutes=15))
        self.assertEqual(instances[0].cleanup, timedelta(minutes=5))

    def test_buffer_map_alias(self):
        instances = expand_instances(
            [event("a", at(9), at(10))],
            buffers={"studio": {"before": 10, "after": 20}},
        )

        self.assertEqual(instances[0].preparation, timedelta(minutes=10))
        self.assertEqual(instances[0].cleanup, timedelta(minutes=20))

    def test_negative_buffers_are_rejected(self):
        with self.assertRaises(ValueError):
            ResourceRule("studio", timedelta(minutes=-1), timedelta())


class MergeIntervalsTests(unittest.TestCase):
    def test_overlapping_and_touching_intervals_are_merged(self):
        merged = merge_intervals(
            [
                (at(9), at(10)),
                (at(9, 30), at(11)),
                (at(11), at(12)),
                (at(14), at(15)),
            ]
        )

        self.assertEqual(merged, [(at(9), at(12)), (at(14), at(15))])

    def test_empty_interval_is_ignored(self):
        self.assertEqual(merge_intervals([(at(9), at(9))]), [])


class DailyOccupancyTests(unittest.TestCase):
    def test_adjacent_buffers_are_counted_once(self):
        first = event("a", at(10), at(11))
        second = event("b", at(11, 10), at(12))

        report = daily_occupancy(
            [first, second],
            {"studio": ResourceRule(preparation=15, cleanup=15)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )

        [day] = report.days
        self.assertEqual(day.occupied_duration, timedelta(hours=2, minutes=30))
        self.assertEqual(day.meeting_duration, timedelta(hours=1, minutes=50))
        self.assertEqual(day.buffer_increment, timedelta(minutes=40))
        self.assertEqual(
            day.raw_buffer_duration,
            timedelta(hours=1),
            "the raw per-occurrence buffer total still counts both buffers",
        )

    def test_buffer_overlapping_a_neighbour_meeting_counts_once(self):
        first = event("a", at(9), at(10))
        second = event("b", at(10, 10), at(11))

        report = daily_occupancy(
            [first, second],
            {"studio": (30, 15)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )

        [day] = report.days
        self.assertEqual(day.intervals, [(at(8, 30), at(11, 15))])
        self.assertEqual(day.meeting_duration, timedelta(minutes=110))
        self.assertEqual(day.buffer_increment, timedelta(minutes=55))

    def test_buffer_crossing_midnight_is_split_by_calendar_date(self):
        late = event("late", at(23, 30), at(0, 30, day=2))

        report = daily_occupancy(
            [late],
            {"studio": (0, 60)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
        )

        first, second = report.days
        self.assertEqual(first.day, date(2024, 1, 1))
        self.assertEqual(first.intervals, [(at(23, 30), at(0, day=2))])
        self.assertEqual(first.meeting_duration, timedelta(minutes=30))
        self.assertEqual(first.buffer_increment, timedelta())

        self.assertEqual(second.day, date(2024, 1, 2))
        self.assertEqual(second.intervals, [(at(0, day=2), at(1, 30, day=2))])
        self.assertEqual(second.meeting_duration, timedelta(minutes=30))
        self.assertEqual(second.buffer_increment, timedelta(hours=1))

    def test_preparation_crossing_midnight_is_split_by_calendar_date(self):
        early = event("early", at(0, 15, day=2), at(1, day=2))

        report = daily_occupancy(
            [early],
            {"studio": (60, 0)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 3),
        )

        first, second = report.days
        self.assertEqual(first.intervals, [(at(23, 15), at(0, day=2))])
        self.assertEqual(first.meeting_duration, timedelta())
        self.assertEqual(first.buffer_increment, timedelta(minutes=45))

        self.assertEqual(second.intervals, [(at(0, day=2), at(1, day=2))])
        self.assertEqual(second.meeting_duration, timedelta(minutes=45))
        self.assertEqual(second.buffer_increment, timedelta(minutes=15))

    def test_cancelled_booking_is_absent_from_the_report(self):
        active = event("a", at(9), at(10))
        cancelled = event("b", at(14), at(15), status="CANCELLED")

        report = daily_occupancy(
            [active, cancelled],
            {"studio": (30, 30)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )

        [day] = report.days
        self.assertEqual(day.intervals, [(at(8, 30), at(10, 30))])
        self.assertEqual([item.uid for item in day.instances], ["a"])

    def test_resources_are_reported_separately(self):
        studio = event("a", at(9), at(10), location="studio")
        van = event("b", at(9), at(10), location="van")

        report = daily_occupancy(
            [studio, van],
            {"studio": (30, 0), "van": (0, 15)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )

        by_resource = {day.resource: day for day in report.days}
        self.assertEqual(by_resource["studio"].intervals, [(at(8, 30), at(10))])
        self.assertEqual(by_resource["van"].intervals, [(at(9), at(10, 15))])

    def test_original_meeting_and_buffer_increment_are_reported_per_day(self):
        report = daily_occupancy(
            [event("a", at(9), at(11))],
            {"studio": ResourceRule(preparation=20, cleanup=10)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )

        [day] = report.days
        self.assertEqual(day.meeting_duration, timedelta(hours=2))
        self.assertEqual(day.occupied_duration, timedelta(hours=2, minutes=30))
        self.assertEqual(day.buffer_increment, timedelta(minutes=30))

        [occurrence] = day.instances
        self.assertEqual(occurrence.meeting_duration, timedelta(hours=2))
        self.assertEqual(occurrence.preparation_duration, timedelta(minutes=20))
        self.assertEqual(occurrence.cleanup_duration, timedelta(minutes=10))
        self.assertEqual(occurrence.buffer_duration, timedelta(minutes=30))

    def test_parsed_calendar_expands_recurrence_and_honours_cancellation(self):
        from icalevents.occupancy import calendar_occupancy

        report = calendar_occupancy(
            file="test/test_data/occupancy.ics",
            rules={"Studio A": (15, 15), "Van 1": (0, 60)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 12),
        )

        studio = report.for_resource("Studio A")
        self.assertEqual(
            [row.day for row in studio],
            [date(2024, 1, 1)],
            "Jan 8 is cancelled and Jan 15 is outside the requested range",
        )
        self.assertEqual(len(studio[0].instances), 1, "one actual occurrence")
        self.assertEqual(studio[0].buffer_increment, timedelta(minutes=30))

        van = report.for_resource("Van 1")
        self.assertEqual(
            [row.day for row in van], [date(2024, 1, 10), date(2024, 1, 11)]
        )
        self.assertEqual(van[0].meeting_duration, timedelta(minutes=30))
        self.assertEqual(van[0].buffer_increment, timedelta())
        self.assertEqual(van[1].meeting_duration, timedelta(minutes=30))
        self.assertEqual(van[1].buffer_increment, timedelta(hours=1))

    def test_double_booked_meeting_is_counted_once_in_the_union(self):
        first = event("a", at(9), at(10, 30))
        second = event("b", at(9), at(10, 30))

        report = daily_occupancy(
            [first, second],
            {"studio": (30, 0)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 1),
        )

        [day] = report.days
        self.assertEqual(day.meeting_duration, timedelta(minutes=90))
        self.assertEqual(day.raw_meeting_duration, timedelta(hours=3))
        self.assertEqual(day.occupied_duration, timedelta(hours=2))
        self.assertEqual(day.buffer_increment, timedelta(minutes=30))
        self.assertEqual(
            day.meeting_duration + day.buffer_increment, day.occupied_duration
        )

    def test_report_is_restricted_to_the_requested_dates(self):
        report = daily_occupancy(
            [event("a", at(9), at(10))],
            {"studio": (30, 30)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 1),
        )

        self.assertEqual([day.day for day in report.days], [date(2024, 1, 1)])

        later = daily_occupancy(
            [event("a", at(9), at(10))],
            {"studio": (30, 30)},
            start=date(2024, 1, 5),
            end=date(2024, 1, 6),
        )
        self.assertEqual(later.days, [], "dates outside the booking are not reported")

    def test_end_before_start_is_rejected(self):
        with self.assertRaises(ValueError):
            daily_occupancy(
                [event("a", at(9), at(10))],
                {},
                start=date(2024, 1, 2),
                end=date(2024, 1, 1),
            )

    def test_report_defaults_cover_touched_dates_before_and_after(self):
        late = event("late", at(23, 30), at(0, 30, day=2))

        report = daily_occupancy([late], {"studio": (0, 60)})

        self.assertEqual(
            [day.day for day in report.days], [date(2024, 1, 1), date(2024, 1, 2)]
        )

    def test_totals_by_day_sum_resources(self):
        report = daily_occupancy(
            [
                event("a", at(9), at(10), location="studio"),
                event("b", at(9), at(10), location="van"),
            ],
            {"studio": (0, 0), "van": (0, 0)},
        )

        self.assertEqual(
            report.totals_by_day(),
            {date(2024, 1, 1): timedelta(hours=2)},
        )

    def test_to_dict_contains_seconds_and_intervals(self):
        report = daily_occupancy(
            [event("a", at(9), at(10))],
            {"studio": (30, 30)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
        )

        row = report.to_dict()["days"][0]
        self.assertEqual(row["occupied_seconds"], 7200.0)
        self.assertEqual(row["meeting_seconds"], 3600.0)
        self.assertEqual(row["buffer_increment_seconds"], 3600.0)
        self.assertEqual(row["occurrences"][0]["uid"], "a")

    def test_all_day_event_uses_calendar_dates(self):
        item = Event()
        item.uid = "all-day"
        item.start = date(2024, 1, 2)
        item.end = date(2024, 1, 3)
        item.summary = "closed"
        item.location = "studio"
        item.all_day = True

        report = daily_occupancy(
            [item],
            {"studio": (30, 30)},
            start=date(2024, 1, 1),
            end=date(2024, 1, 4),
        )

        before, during, after = report.days
        self.assertEqual(before.day, date(2024, 1, 1))
        self.assertEqual(before.buffer_increment, timedelta(minutes=30))

        self.assertEqual(during.day, date(2024, 1, 2))
        self.assertEqual(during.occupied_duration, timedelta(days=1))
        self.assertEqual(during.meeting_duration, timedelta(days=1))
        self.assertEqual(during.buffer_increment, timedelta())

        self.assertEqual(after.day, date(2024, 1, 3))
        self.assertEqual(after.buffer_increment, timedelta(minutes=30))


if __name__ == "__main__":
    unittest.main()
