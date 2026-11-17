import json
import unittest
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import mock

from dateutil.tz import gettz

from icalevents import capacity
from icalevents.capacity import (
    Booking,
    CapacityDeclarationError,
    SupplyPlan,
    SupplyWindow,
    analyze,
    analyze_calendar,
    bookings_from_events,
    expand_calendar,
    split_events,
    supply_plan_from_events,
)

DATA = Path(__file__).parent / "test_data" / "capacity_bookings.ics"
TZ = gettz("Europe/Berlin")


def at(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


class CapacityAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.calendar = DATA.read_text()

    def test_half_open_boundaries_do_not_overlap(self):
        bookings = [
            Booking("a", "A", at(3, 2, 9), at(3, 2, 10), 4),
            Booking("b", "B", at(3, 2, 10), at(3, 2, 11), 4),
        ]
        report = analyze(bookings, SupplyPlan(base_capacity=4), tz=TZ)

        self.assertEqual(report.overloads, [])
        self.assertEqual(
            [
                (segment.start, segment.end, segment.demand)
                for segment in report.segments
            ],
            [
                (at(3, 2, 9), at(3, 2, 10), 4),
                (at(3, 2, 10), at(3, 2, 11), 4),
            ],
        )

    def test_zero_duration_booking_is_ignored(self):
        bookings = [Booking("a", "A", at(3, 2, 9), at(3, 2, 9), 9)]
        report = analyze(bookings, SupplyPlan(base_capacity=1), tz=TZ)

        self.assertEqual(report.overloads, [])
        self.assertEqual([(item.ref.uid, item.reason) for item in report.ignored], [
            ("a", "zero_length")
        ])

    def test_maintenance_creates_exact_overload_intervals(self):
        report = analyze_calendar(
            file=str(DATA), start=date(2026, 3, 2), end=date(2026, 3, 3), tz=TZ
        )

        self.assertEqual(
            [
                (
                    segment.start,
                    segment.end,
                    segment.demand,
                    segment.capacity,
                    segment.overload,
                )
                for segment in report.overloads
            ],
            [
                (at(3, 2, 10, 30), at(3, 2, 10, 45), 10, 8, 2),
                (at(3, 2, 10, 45), at(3, 2, 11), 10, 5, 5),
                (at(3, 2, 11), at(3, 2, 11, 15), 9, 5, 4),
                (at(3, 2, 11, 15), at(3, 2, 11, 30), 9, 8, 1),
            ],
        )

    def test_overload_sources_are_reported(self):
        report = analyze_calendar(
            file=str(DATA), start=date(2026, 3, 2), end=date(2026, 3, 3), tz=TZ
        )
        segment = report.overloads[1]

        self.assertEqual(
            [source.summary for source in segment.sources],
            ["Alpha", "Beta", "Gamma"],
        )
        self.assertEqual(
            [source.summary for source in segment.excess_sources],
            ["Beta", "Gamma"],
        )
        self.assertEqual(segment.overload_units, 5 * 15 * 60)

    def test_transparent_cancelled_and_duplicate_rows_are_not_counted(self):
        report = analyze_calendar(
            file=str(DATA), start=date(2026, 3, 2), end=date(2026, 3, 3), tz=TZ
        )
        reasons = {item.ref.uid: item.reason for item in report.ignored}

        self.assertEqual(reasons["b-transparent"], "transparent")
        self.assertEqual(reasons["b-cancelled"], "cancelled")
        self.assertEqual(reasons["b-alpha"], "duplicate")
        self.assertEqual(len(report.ignored), 4)

        peak = max(segment.demand for segment in report.segments)
        self.assertEqual(peak, 10)

    def test_recurring_instances_use_exdate_and_override(self):
        report = analyze_calendar(
            file=str(DATA), start=date(2026, 3, 4), end=date(2026, 3, 8), tz=TZ
        )
        by_start = {
            segment.start: segment for segment in report.segments if segment.demand
        }

        self.assertIn(at(3, 4, 9, 30), by_start)
        self.assertEqual(by_start[at(3, 4, 9, 30)].demand, 9)
        self.assertIn(at(3, 5, 9), by_start)
        self.assertEqual(
            [source.uid for source in by_start[at(3, 5, 9)].sources],
            ["b-fill-2"],
        )
        self.assertIn(at(3, 6, 14), by_start)
        self.assertNotIn(at(3, 6, 9), by_start)
        self.assertEqual(by_start[at(3, 6, 14)].demand, 9)
        self.assertIn(at(3, 7, 9), by_start)
        self.assertEqual(by_start[at(3, 7, 9)].demand, 5)

    def test_cancelled_override_removes_the_instance(self):
        report = analyze_calendar(
            file=str(DATA), start=date(2026, 3, 9), end=date(2026, 3, 12), tz=TZ
        )
        by_start = {
            segment.start: segment for segment in report.segments if segment.demand
        }

        self.assertIn(at(3, 9, 8), by_start)
        self.assertIn(at(3, 11, 8), by_start)
        self.assertIn(
            ("b-series-2", "cancelled"),
            [(item.ref.uid, item.reason) for item in report.ignored],
        )
        self.assertEqual(
            [source.uid for source in by_start[at(3, 10, 8)].sources],
            ["b-fill-4"],
        )
        self.assertEqual(by_start[at(3, 10, 8)].demand, 7)

    def test_missing_capacity_is_unknown_not_zero(self):
        bookings = [Booking("a", "A", at(3, 2, 9), at(3, 2, 10), 3)]
        report = analyze(bookings, SupplyPlan(), tz=TZ)

        [segment] = report.segments
        self.assertEqual(segment.state, "unknown")
        self.assertIsNone(segment.capacity)
        self.assertEqual(segment.reasons, ("capacity_undeclared",))
        self.assertEqual([source.uid for source in segment.sources], ["a"])

    def test_missing_quantity_is_unknown(self):
        bookings = [
            Booking("a", "A", at(3, 2, 9), at(3, 2, 10), 2),
            Booking("b", "B", at(3, 2, 9), at(3, 2, 10), None),
        ]
        report = analyze(bookings, SupplyPlan(base_capacity=4), tz=TZ)

        [segment] = report.segments
        self.assertEqual(segment.state, "unknown")
        self.assertEqual(segment.demand, 2)
        self.assertEqual(segment.reasons, ("booking_quantity_undeclared",))
        self.assertEqual(
            [source.uid for source in segment.unknown_quantity_sources], ["b"]
        )
        self.assertEqual(
            [source.uid for source in segment.sources], ["a", "b"]
        )

    def test_known_demand_over_capacity_is_overload_despite_unknown_quantity(self):
        bookings = [
            Booking("a", "A", at(3, 2, 9), at(3, 2, 10), 6),
            Booking("b", "B", at(3, 2, 9), at(3, 2, 10), None),
        ]
        report = analyze(bookings, SupplyPlan(base_capacity=4), tz=TZ)

        [segment] = report.segments
        self.assertEqual(segment.state, "overload")
        self.assertEqual(segment.overload, 2)
        self.assertEqual(
            [source.uid for source in segment.unknown_quantity_sources], ["b"]
        )

    def test_short_peak_is_not_hidden_by_daily_average(self):
        bookings = [
            Booking("a", "A", at(3, 3, 0), at(3, 3, 1), 10),
        ]
        report = analyze(
            bookings,
            SupplyPlan(base_capacity=5),
            start=date(2026, 3, 3),
            end=date(2026, 3, 4),
            tz=TZ,
        )

        self.assertEqual(len(report.overloads), 1)
        self.assertEqual(report.overloads[0].start, at(3, 3, 0))
        self.assertEqual(report.overloads[0].end, at(3, 3, 1))
        self.assertEqual(report.overloads[0].overload, 5)

    def test_bookings_are_clipped_to_the_window(self):
        bookings = [Booking("a", "A", at(3, 1, 22), at(3, 2, 2), 9)]
        report = analyze(
            bookings,
            SupplyPlan(base_capacity=4),
            start=date(2026, 3, 2),
            end=date(2026, 3, 3),
            tz=TZ,
        )

        [segment] = report.overloads
        self.assertEqual(segment.start, at(3, 2, 0))
        self.assertEqual(segment.end, at(3, 2, 2))
        self.assertEqual(segment.overload, 5)

    def test_absolute_capacity_window_overrides_base(self):
        plan = SupplyPlan(
            base_capacity=8,
            windows=[
                SupplyWindow(start=at(3, 2, 9), end=at(3, 2, 12), capacity=2)
            ],
        )
        bookings = [Booking("a", "A", at(3, 2, 9), at(3, 2, 10), 3)]
        report = analyze(bookings, plan, tz=TZ)

        [segment] = report.overloads
        self.assertEqual(segment.capacity, 2)
        self.assertEqual(segment.overload, 1)

    def test_conflicting_absolute_capacities_raise(self):
        plan = SupplyPlan(
            windows=[
                SupplyWindow(start=at(3, 2, 9), end=at(3, 2, 12), capacity=2),
                SupplyWindow(start=at(3, 2, 10), end=at(3, 2, 11), capacity=3),
            ]
        )
        bookings = [Booking("a", "A", at(3, 2, 10), at(3, 2, 11), 1)]

        with self.assertRaises(CapacityDeclarationError):
            analyze(bookings, plan, tz=TZ)

    def test_negative_quantity_is_rejected(self):
        with self.assertRaises(CapacityDeclarationError):
            Booking("a", "A", at(3, 2, 9), at(3, 2, 10), -1)

        calendar = self.calendar.replace(
            "X-RESOURCE-QUANTITY:3", "X-RESOURCE-QUANTITY:-3", 1
        )
        with self.assertRaises(CapacityDeclarationError):
            analyze_calendar(
                string_content=calendar,
                start=date(2026, 3, 2),
                end=date(2026, 3, 3),
                tz=TZ,
            )

    def test_maintenance_cannot_push_capacity_below_zero(self):
        plan = SupplyPlan(
            base_capacity=2,
            windows=[SupplyWindow(start=at(3, 2, 9), end=at(3, 2, 10), delta=5)],
        )
        report = analyze(
            [Booking("a", "A", at(3, 2, 9), at(3, 2, 10), 1)], plan, tz=TZ
        )

        [segment] = report.overloads
        self.assertEqual(segment.capacity, 0)
        self.assertEqual(segment.overload, 1)

    def test_segments_partition_the_whole_window(self):
        report = analyze_calendar(
            file=str(DATA), start=date(2026, 3, 1), end=date(2026, 3, 20), tz=TZ
        )

        self.assertEqual(report.segments[0].start, report.start)
        self.assertEqual(report.segments[-1].end, report.end)
        for previous, current in zip(report.segments, report.segments[1:]):
            self.assertEqual(previous.end, current.start)

    def test_split_events_separates_supply_from_bookings(self):
        events = expand_calendar(
            self.calendar, start=date(2026, 3, 1), end=date(2026, 3, 20), tz=TZ
        )
        bookings, supply = split_events(events)

        self.assertEqual(
            sorted(event.uid for event in supply),
            ["maintenance-0302", "supply-window"],
        )
        self.assertTrue(all(event.uid not in {"supply-window"} for event in bookings))

        plan = supply_plan_from_events(supply, tz=TZ)
        self.assertEqual(plan.base_capacity, None)
        self.assertEqual(len(plan.windows), 2)
        self.assertEqual(
            plan.capacity_for([plan.windows[0]], TZ), 8
        )

    def test_bookings_from_events_deduplicates(self):
        events = expand_calendar(
            self.calendar, start=date(2026, 3, 2), end=date(2026, 3, 3), tz=TZ
        )
        bookings, _ = split_events(events)
        deduped = bookings_from_events(bookings, tz=TZ)

        self.assertEqual(
            len([booking for booking in deduped if booking.uid == "b-alpha"]), 1
        )

    def test_analyze_capacity_accepts_extra_windows(self):
        report = analyze_calendar(
            file=str(DATA),
            start=date(2026, 3, 2),
            end=date(2026, 3, 3),
            tz=TZ,
            supply_windows=[
                SupplyWindow(start=at(3, 2, 12), end=at(3, 2, 13), delta=6)
            ],
        )
        starts = {(segment.start, segment.capacity) for segment in report.segments}
        self.assertIn((at(3, 2, 12), 2), starts)

    def test_empty_input_is_an_error(self):
        with self.assertRaises(CapacityDeclarationError):
            analyze([], SupplyPlan(base_capacity=2), tz=TZ)

    def test_duplicate_supply_rows_are_not_double_counted(self):
        content = (
            "BEGIN:VCALENDAR\nVERSION:2.0\n"
            "BEGIN:VEVENT\nUID:m\nDTSTART:20260302T100000Z\n"
            "DTEND:20260302T120000Z\nX-RESOURCE-CAPACITY-DELTA:3\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:m\nDTSTART:20260302T100000Z\n"
            "DTEND:20260302T120000Z\nX-RESOURCE-CAPACITY-DELTA:3\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:b\nDTSTART:20260302T100000Z\n"
            "DTEND:20260302T110000Z\nX-RESOURCE-QUANTITY:6\nEND:VEVENT\n"
            "END:VCALENDAR\n"
        )
        report = analyze_calendar(
            string_content=content,
            start=date(2026, 3, 2),
            end=date(2026, 3, 3),
            base_capacity=8,
            tz=capacity.UTC,
        )

        [segment] = report.overloads
        self.assertEqual(segment.capacity, 5)
        self.assertEqual(segment.overload, 1)

    def test_all_day_recurrence_honours_exdate_and_override(self):
        content = (
            "BEGIN:VCALENDAR\nVERSION:2.0\n"
            "BEGIN:VEVENT\nUID:ad\nDTSTART;VALUE=DATE:20260302\n"
            "DTEND;VALUE=DATE:20260303\nRRULE:FREQ=DAILY;COUNT=3\n"
            "EXDATE;VALUE=DATE:20260303\nX-RESOURCE-QUANTITY:5\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:ad\nRECURRENCE-ID;VALUE=DATE:20260304\n"
            "DTSTART;VALUE=DATE:20260304\nDTEND;VALUE=DATE:20260305\n"
            "X-RESOURCE-QUANTITY:5\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        report = analyze_calendar(
            string_content=content,
            start=date(2026, 3, 2),
            end=date(2026, 3, 6),
            base_capacity=4,
            tz=capacity.UTC,
        )

        self.assertEqual(
            [
                (segment.start.date(), segment.end.date())
                for segment in report.overloads
            ],
            [
                (date(2026, 3, 2), date(2026, 3, 3)),
                (date(2026, 3, 4), date(2026, 3, 5)),
            ],
        )


class CapacityCliTests(unittest.TestCase):
    def test_json_output(self):
        stream = StringIO()
        with mock.patch("sys.stdout", stream):
            code = capacity.main(
                [
                    str(DATA),
                    "--start",
                    "2026-03-02T00:00:00+01:00",
                    "--end",
                    "2026-03-03T00:00:00+01:00",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(len(payload["overloads"]), 4)
        self.assertEqual(payload["overloads"][1]["overload"], 5)
        self.assertEqual(
            [source["summary"] for source in payload["overloads"][1]["sources"]],
            ["Alpha", "Beta", "Gamma"],
        )

    def test_text_output_lists_sources(self):
        stream = StringIO()
        with mock.patch("sys.stdout", stream):
            code = capacity.main(
                [
                    str(DATA),
                    "--start",
                    "2026-03-02T00:00:00+01:00",
                    "--end",
                    "2026-03-03T00:00:00+01:00",
                ]
            )

        output = stream.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("overloads: 4", output)
        self.assertIn("demand 10 > capacity 5", output)
        self.assertIn("Beta", output)

    def test_missing_capacity_is_reported_as_unknown(self):
        content = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART:20260302T090000Z\nDTEND:20260302T100000Z\n"
            "X-RESOURCE-QUANTITY:3\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        with NamedTemporaryFile("w", suffix=".ics", delete=False) as handle:
            handle.write(content)
            name = handle.name

        stream = StringIO()
        with mock.patch("sys.stdout", stream):
            code = capacity.main(
                [
                    name,
                    "--start",
                    "2026-03-01T00:00:00Z",
                    "--end",
                    "2026-03-04T00:00:00Z",
                    "--json",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["overloads"], [])
        self.assertEqual(payload["unknowns"][0]["reasons"], ["capacity_undeclared"])
        Path(name).unlink()

    def test_bad_input_returns_error(self):
        stream = StringIO()
        errors = StringIO()
        with mock.patch("sys.stdout", stream), mock.patch("sys.stderr", errors):
            code = capacity.main(["/does/not/exist.ics"])

        self.assertEqual(code, 2)
        self.assertIn("error:", errors.getvalue())


class CapacityTrapTests(unittest.TestCase):
    def test_recurrence_override_moves_booking(self):
        content = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:s\n"
            "DTSTART;TZID=Europe/Berlin:20260302T090000\n"
            "DTEND;TZID=Europe/Berlin:20260302T100000\n"
            "RRULE:FREQ=DAILY;COUNT=2\nX-RESOURCE-QUANTITY:5\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:s\n"
            "RECURRENCE-ID;TZID=Europe/Berlin:20260303T090000\n"
            "DTSTART;TZID=Europe/Berlin:20260303T150000\n"
            "DTEND;TZID=Europe/Berlin:20260303T160000\n"
            "X-RESOURCE-QUANTITY:5\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        report = analyze_calendar(
            string_content=content,
            start=date(2026, 3, 2),
            end=date(2026, 3, 4),
            base_capacity=4,
            tz=TZ,
        )

        self.assertEqual(
            [(segment.start, segment.end) for segment in report.overloads],
            [
                (at(3, 2, 9), at(3, 2, 10)),
                (at(3, 3, 15), at(3, 3, 16)),
            ],
        )

    def test_exdate_on_one_instance_only(self):
        content = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:s\n"
            "DTSTART;TZID=Europe/Berlin:20260302T090000\n"
            "DTEND;TZID=Europe/Berlin:20260302T100000\n"
            "RRULE:FREQ=DAILY;COUNT=3\n"
            "EXDATE;TZID=Europe/Berlin:20260303T090000\n"
            "X-RESOURCE-QUANTITY:5\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        report = analyze_calendar(
            string_content=content,
            start=date(2026, 3, 2),
            end=date(2026, 3, 5),
            base_capacity=4,
            tz=TZ,
        )

        self.assertEqual(
            [segment.start for segment in report.overloads],
            [at(3, 2, 9), at(3, 4, 9)],
        )

    def test_booking_spanning_window_start_is_counted(self):
        content = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:a\n"
            "DTSTART:20260301T220000Z\nDTEND:20260302T020000Z\n"
            "X-RESOURCE-QUANTITY:9\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        report = analyze_calendar(
            string_content=content,
            start=datetime(2026, 3, 2, 0, 0, tzinfo=TZ),
            end=datetime(2026, 3, 2, 12, 0, tzinfo=TZ),
            base_capacity=4,
            tz=TZ,
        )

        [segment] = report.overloads
        self.assertEqual(segment.start, datetime(2026, 3, 2, 0, 0, tzinfo=TZ))
        self.assertEqual(
            segment.end, datetime(2026, 3, 2, 3, 0, tzinfo=TZ)
        )
        self.assertEqual(segment.overload, 5)


if __name__ == "__main__":
    unittest.main()
