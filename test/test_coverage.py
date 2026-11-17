import unittest
from datetime import date, datetime, time, timedelta

from dateutil.tz import UTC, gettz
from pytz import timezone as pytz_timezone

from icalevents import icalparser
from icalevents.icalevents import events
from icalevents.coverage import (
    BusinessWindow,
    business_calendar,
    count_covered_business_days,
    coverage_by_uid,
    covered_business_days,
    event_coverage,
    is_counted,
    resolve_timezone,
)


BERLIN = gettz("Europe/Berlin")
SHANGHAI = gettz("Asia/Shanghai")


def make_event(
    uid,
    start,
    end,
    status=None,
    transparent=False,
    all_day=False,
):
    event = icalparser.Event()
    event.uid = uid
    event.start = start
    event.end = end
    event.status = status
    event.transparent = transparent
    event.all_day = all_day
    return event


class BusinessCalendarTests(unittest.TestCase):
    def test_windows_are_aware_in_venue_timezone(self):
        windows = business_calendar(
            [date(2024, 1, 1), date(2024, 1, 2)], time(9, 0), time(18, 0), "UTC"
        )

        self.assertEqual(len(windows), 2)
        self.assertEqual(
            windows[0].start, datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
        )
        self.assertEqual(
            windows[0].end, datetime(2024, 1, 1, 18, 0, tzinfo=UTC)
        )
        self.assertEqual(windows[0].day, date(2024, 1, 1))

    def test_close_before_open_runs_past_midnight(self):
        windows = business_calendar(
            [date(2024, 1, 1)], time(22, 0), time(2, 0), BERLIN
        )

        self.assertEqual(
            windows[0].start,
            datetime(2024, 1, 1, 22, 0, tzinfo=BERLIN),
        )
        self.assertEqual(
            windows[0].end,
            datetime(2024, 1, 2, 2, 0, tzinfo=BERLIN),
        )

    def test_pytz_timezone_is_supported(self):
        windows = business_calendar(
            [date(2024, 1, 1)], time(9, 0), time(18, 0), pytz_timezone("UTC")
        )
        self.assertEqual(
            windows[0].start, datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
        )

    def test_window_rejects_naive_bounds(self):
        with self.assertRaises(ValueError):
            BusinessWindow(
                day=date(2024, 1, 1),
                start=datetime(2024, 1, 1, 9, 0),
                end=datetime(2024, 1, 1, 18, 0),
            )

    def test_resolve_timezone_failures(self):
        with self.assertRaises(ValueError):
            resolve_timezone("Not/AZone")
        with self.assertRaises(ValueError):
            resolve_timezone(None)
        self.assertIs(resolve_timezone(UTC), UTC)


class CoveredDaysTests(unittest.TestCase):
    def setUp(self):
        self.windows = business_calendar(
            [date(2024, 1, 1), date(2024, 1, 2)],
            time(9, 0),
            time(18, 0),
            UTC,
        )

    def test_overlap_covers_the_day(self):
        event = make_event(
            "e1",
            datetime(2024, 1, 1, 10, 0, tzinfo=UTC),
            datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            covered_business_days(event, self.windows), {date(2024, 1, 1)}
        )

    def test_boundary_touch_does_not_cover(self):
        ends_at_open = make_event(
            "e1",
            datetime(2024, 1, 1, 8, 0, tzinfo=UTC),
            datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        )
        starts_at_close = make_event(
            "e2",
            datetime(2024, 1, 1, 18, 0, tzinfo=UTC),
            datetime(2024, 1, 1, 19, 0, tzinfo=UTC),
        )
        self.assertEqual(covered_business_days(ends_at_open, self.windows), set())
        self.assertEqual(
            covered_business_days(starts_at_close, self.windows), set()
        )

    def test_partial_overlap_covers(self):
        event = make_event(
            "e1",
            datetime(2024, 1, 1, 8, 30, tzinfo=UTC),
            datetime(2024, 1, 1, 9, 30, tzinfo=UTC),
        )
        self.assertEqual(
            covered_business_days(event, self.windows), {date(2024, 1, 1)}
        )

    def test_late_night_event_only_hits_real_window(self):
        windows = business_calendar(
            [date(2024, 1, 1), date(2024, 1, 2)],
            time(22, 0),
            time(2, 0),
            UTC,
        )

        inside_first_window = make_event(
            "e1",
            datetime(2024, 1, 2, 1, 0, tzinfo=UTC),
            datetime(2024, 1, 2, 1, 30, tzinfo=UTC),
        )
        self.assertEqual(
            covered_business_days(inside_first_window, windows),
            {date(2024, 1, 1)},
        )

        starts_when_window_ends = make_event(
            "e2",
            datetime(2024, 1, 2, 2, 0, tzinfo=UTC),
            datetime(2024, 1, 2, 3, 0, tzinfo=UTC),
        )
        self.assertEqual(
            covered_business_days(starts_when_window_ends, windows), set()
        )

    def test_overnight_event_can_cover_two_labelled_days(self):
        windows = business_calendar(
            [date(2024, 1, 1), date(2024, 1, 2)],
            time(22, 0),
            time(2, 0),
            UTC,
        )
        event = make_event(
            "e1",
            datetime(2024, 1, 2, 1, 0, tzinfo=UTC),
            datetime(2024, 1, 2, 23, 0, tzinfo=UTC),
        )
        self.assertEqual(
            covered_business_days(event, windows),
            {date(2024, 1, 1), date(2024, 1, 2)},
        )

    def test_overnight_event_against_day_windows(self):
        event = make_event(
            "e1",
            datetime(2024, 1, 1, 17, 0, tzinfo=UTC),
            datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
        )
        self.assertEqual(
            covered_business_days(event, self.windows),
            {date(2024, 1, 1), date(2024, 1, 2)},
        )

    def test_all_day_event_covers_its_day(self):
        event = make_event(
            "e1",
            date(2024, 1, 1),
            date(2024, 1, 2),
            all_day=True,
        )
        self.assertEqual(
            covered_business_days(event, self.windows), {date(2024, 1, 1)}
        )

    def test_floating_event_uses_venue_timezone(self):
        event = make_event(
            "e1",
            datetime(2024, 1, 1, 17, 0),
            datetime(2024, 1, 1, 18, 30),
        )
        windows = business_calendar(
            [date(2024, 1, 1)], time(9, 0), time(18, 0), SHANGHAI
        )
        self.assertEqual(
            covered_business_days(event, windows), {date(2024, 1, 1)}
        )

    def test_timezone_conversion_is_respected(self):
        windows = business_calendar(
            [date(2024, 1, 1)], time(9, 0), time(18, 0), BERLIN
        )
        event = make_event(
            "e1",
            datetime(2024, 1, 1, 8, 0, tzinfo=UTC),
            datetime(2024, 1, 1, 8, 30, tzinfo=UTC),
        )
        self.assertEqual(
            covered_business_days(event, windows), {date(2024, 1, 1)}
        )

    def test_dst_transition_window_is_built(self):
        windows = business_calendar(
            [date(2023, 3, 25)], time(22, 0), time(6, 0), BERLIN
        )
        self.assertEqual(windows[0].end - windows[0].start, timedelta(hours=8))


class StatusAndTransparencyTests(unittest.TestCase):
    def test_cancelled_never_counts(self):
        event = make_event(
            "e1",
            datetime(2024, 1, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 1, 11, tzinfo=UTC),
            status="CANCELLED",
        )
        self.assertFalse(is_counted(event))
        self.assertFalse(is_counted(event, include_transparent=True))

    def test_transparent_only_counts_with_flag(self):
        event = make_event(
            "e1",
            datetime(2024, 1, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 1, 11, tzinfo=UTC),
            transparent=True,
        )
        self.assertFalse(is_counted(event))
        self.assertTrue(is_counted(event, include_transparent=True))

    def test_tentative_and_confirmed_count(self):
        for status in ("CONFIRMED", "TENTATIVE", None):
            event = make_event(
                "e1",
                datetime(2024, 1, 1, 10, tzinfo=UTC),
                datetime(2024, 1, 1, 11, tzinfo=UTC),
                status=status,
            )
            self.assertTrue(is_counted(event))


class CoverageAggregationTests(unittest.TestCase):
    def setUp(self):
        self.windows = business_calendar(
            [date(2024, 1, 1), date(2024, 1, 2)],
            time(9, 0),
            time(18, 0),
            UTC,
        )

    def test_overlapping_instances_same_uid_count_once(self):
        first = make_event(
            "activity",
            datetime(2024, 1, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 1, 12, tzinfo=UTC),
        )
        overlapping = make_event(
            "activity",
            datetime(2024, 1, 1, 11, tzinfo=UTC),
            datetime(2024, 1, 1, 13, tzinfo=UTC),
        )
        second_day = make_event(
            "activity",
            datetime(2024, 1, 2, 10, tzinfo=UTC),
            datetime(2024, 1, 2, 12, tzinfo=UTC),
        )

        result = coverage_by_uid(
            [first, overlapping, second_day], self.windows
        )
        self.assertEqual(result, {"activity": 2})

    def test_different_uids_are_separate(self):
        first = make_event(
            "a",
            datetime(2024, 1, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 1, 12, tzinfo=UTC),
        )
        second = make_event(
            "b",
            datetime(2024, 1, 1, 14, tzinfo=UTC),
            datetime(2024, 1, 1, 16, tzinfo=UTC),
        )
        self.assertEqual(
            coverage_by_uid([first, second], self.windows),
            {"a": 1, "b": 1},
        )
        self.assertEqual(
            count_covered_business_days([first, second], self.windows), 1
        )

    def test_cancelled_and_transparent_are_skipped(self):
        good = make_event(
            "a",
            datetime(2024, 1, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 1, 12, tzinfo=UTC),
        )
        cancelled = make_event(
            "b",
            datetime(2024, 1, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 1, 12, tzinfo=UTC),
            status="CANCELLED",
        )
        transparent = make_event(
            "c",
            datetime(2024, 1, 2, 10, tzinfo=UTC),
            datetime(2024, 1, 2, 12, tzinfo=UTC),
            transparent=True,
        )

        self.assertEqual(
            coverage_by_uid([good, cancelled, transparent], self.windows),
            {"a": 1},
        )
        self.assertEqual(
            coverage_by_uid(
                [good, cancelled, transparent],
                self.windows,
                include_transparent=True,
            ),
            {"a": 1, "c": 1},
        )

    def test_events_without_window_overlap_are_absent(self):
        event = make_event(
            "a",
            datetime(2024, 1, 3, 10, tzinfo=UTC),
            datetime(2024, 1, 3, 12, tzinfo=UTC),
        )
        self.assertEqual(coverage_by_uid([event], self.windows), {})
        self.assertEqual(
            count_covered_business_days([event], self.windows), 0
        )


class EndToEndTests(unittest.TestCase):
    def test_moved_recurrence_counts_new_day_only(self):
        parsed = events(
            file="test/test_data/recurrenceid_ms.ics",
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            strict=True,
            sort=True,
        )
        windows = business_calendar(
            [date(2021, 1, 1) + timedelta(days=i) for i in range(365)],
            time(0, 0),
            time(23, 59, 59),
            BERLIN,
        )

        result = coverage_by_uid(parsed, windows)
        self.assertEqual(len(result), 1)
        uid = next(iter(result))
        self.assertEqual(result[uid], 42)

        covered = set()
        for event in parsed:
            covered.update(covered_business_days(event, windows))
        self.assertIn(date(2021, 4, 10), covered)
        self.assertNotIn(date(2021, 4, 9), covered)

    def test_small_window_intersection(self):
        start = datetime(2022, 1, 11, 0, 0, tzinfo=BERLIN)
        end = datetime(2022, 1, 11, 12, 0, tzinfo=BERLIN)
        parsed = events(
            file="test/test_data/recurring_small_window.ics",
            start=start,
            end=end,
            strict=True,
        )
        self.assertEqual(len(parsed), 1)

        outside = business_calendar(
            [date(2022, 1, 11)], time(9, 0), time(18, 0), BERLIN
        )
        inside = business_calendar(
            [date(2022, 1, 11)], time(7, 0), time(8, 30), BERLIN
        )
        self.assertEqual(
            count_covered_business_days(parsed, outside), 0
        )
        self.assertEqual(
            count_covered_business_days(parsed, inside), 1
        )

    def test_transparent_ical_is_excluded_by_default(self):
        parsed = events(
            file="test/test_data/transparent.ics",
            start=date(2021, 5, 19),
            end=date(2021, 5, 22),
            strict=True,
        )
        windows = business_calendar(
            [date(2021, 5, 20), date(2021, 5, 21)],
            time(0, 0),
            time(23, 59, 59),
            BERLIN,
        )

        self.assertEqual(
            count_covered_business_days(parsed, windows), 1
        )
        self.assertEqual(
            count_covered_business_days(
                parsed, windows, include_transparent=True
            ),
            2,
        )

    def test_cancelled_ical_is_excluded(self):
        parsed = events(
            file="test/test_data/status_and_url.ics",
            start=date(2018, 10, 29),
            end=date(2018, 10, 31),
            strict=True,
        )
        windows = business_calendar(
            [date(2018, 10, 30)], time(0, 0), time(23, 59, 59), UTC
        )

        per_uid = coverage_by_uid(parsed, windows)
        cancelled = [event for event in parsed if event.status == "CANCELLED"]
        self.assertEqual(len(cancelled), 2)
        for event in cancelled:
            self.assertFalse(is_counted(event))
            self.assertNotIn(event.uid, per_uid)
        self.assertEqual(len(per_uid), 3)
        self.assertEqual(
            count_covered_business_days(parsed, windows), 1
        )

    def test_event_coverage_one_shot(self):
        result = event_coverage(
            business_days=[
                date(2021, 5, 20),
                date(2021, 5, 21),
                date(2021, 5, 22),
            ],
            open_time=time(0, 0),
            close_time=time(23, 59, 59),
            tz=BERLIN,
            start=date(2021, 5, 19),
            end=date(2021, 5, 23),
            file="test/test_data/transparent.ics",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(next(iter(result.values())), 1)

        with_transparent = event_coverage(
            business_days=[date(2021, 5, 20), date(2021, 5, 21)],
            open_time=time(0, 0),
            close_time=time(23, 59, 59),
            tz="Europe/Berlin",
            start=date(2021, 5, 19),
            end=date(2021, 5, 23),
            file="test/test_data/transparent.ics",
            include_transparent=True,
        )
        self.assertEqual(sum(with_transparent.values()), 2)


if __name__ == "__main__":
    unittest.main()
