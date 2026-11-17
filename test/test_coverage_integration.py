import unittest
from datetime import date, datetime
from time import sleep

from dateutil.tz import gettz

from icalevents import icalevents
from icalevents.coverage import CALENDAR_DATA_VERSION, CoverageStatus
from icalevents.icalparser import parse_events


BASIC_ICS = "test/test_data/basic.ics"


def calendar(*vevents: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        + "".join(vevents)
        + "END:VCALENDAR\r\n"
    )


BROKEN_ALL_DAY = """BEGIN:VEVENT\r
UID:broken-all-day\r
DTSTART;VALUE=DATE:20210115\r
DTEND;VALUE=DATE:20210116\r
SUMMARY:Broken all day\r
RRULE:FREQ=BOGUS\r
END:VEVENT\r
"""

HEALTHY_ALL_DAY = """BEGIN:VEVENT\r
UID:healthy-all-day\r
DTSTART;VALUE=DATE:20210120\r
DTEND;VALUE=DATE:20210121\r
SUMMARY:Healthy all day\r
END:VEVENT\r
"""

BROKEN_TIMED = """BEGIN:VTIMEZONE\r
TZID:Europe/Berlin\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:broken-timed\r
DTSTART;TZID=Europe/Berlin:20210115T100000\r
DTEND;TZID=Europe/Berlin:20210115T110000\r
SUMMARY:Broken timed\r
RRULE:FREQ=BOGUS\r
END:VEVENT\r
"""


class ParseEventsCoverageTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2021, 1, 1)
        self.end = date(2021, 2, 1)

    def test_healthy_calendar_is_complete(self):
        result = parse_events(
            calendar(HEALTHY_ALL_DAY),
            start=self.start,
            end=self.end,
            coverage=True,
        )

        self.assertEqual(len(result.events), 1)
        self.assertIs(result.coverage.status, CoverageStatus.COMPLETE)
        self.assertEqual(result.coverage.version, CALENDAR_DATA_VERSION)
        self.assertTrue(result.coverage.can_confirm_empty())

    def test_complete_coverage_allows_confirmed_empty_window(self):
        result = parse_events(
            calendar(HEALTHY_ALL_DAY),
            start=date(2022, 1, 1),
            end=date(2022, 2, 1),
            coverage=True,
        )

        self.assertEqual(result.events, [])
        self.assertIs(result.coverage.status, CoverageStatus.COMPLETE)
        self.assertTrue(result.coverage.can_confirm_empty())

    def test_broken_rrule_degrades_to_partial_and_keeps_other_events(self):
        result = parse_events(
            calendar(BROKEN_ALL_DAY, HEALTHY_ALL_DAY),
            start=self.start,
            end=self.end,
            coverage=True,
        )

        self.assertIs(result.coverage.status, CoverageStatus.PARTIAL)
        self.assertEqual(
            [event.summary for event in result.events], ["Healthy all day"]
        )
        self.assertEqual(len(result.coverage.failures), 1)

        failure = result.coverage.failures[0]
        self.assertEqual(failure.uid, "broken-all-day")
        self.assertEqual(failure.summary, "Broken all day")
        self.assertIn("FREQ", failure.reason)
        # the affected range starts at DTSTART, not at the window start
        self.assertEqual(failure.start, date(2021, 1, 15))
        self.assertEqual(failure.end, self.end)
        self.assertFalse(result.coverage.can_confirm_empty())

    def test_broken_rrule_does_not_register_whole_window_as_free(self):
        result = parse_events(
            calendar(BROKEN_ALL_DAY),
            start=self.start,
            end=self.end,
            coverage=True,
        )

        covered = result.coverage.covered_intervals
        self.assertEqual(covered, ((self.start, date(2021, 1, 15)),))
        self.assertEqual(result.events, [])
        self.assertIs(result.coverage.status, CoverageStatus.PARTIAL)

    def test_broken_timed_rrule_reports_datetime_range(self):
        result = parse_events(
            calendar(BROKEN_TIMED),
            start=self.start,
            end=self.end,
            coverage=True,
        )

        self.assertIs(result.coverage.status, CoverageStatus.PARTIAL)
        failure = result.coverage.failures[0]
        self.assertEqual(failure.uid, "broken-timed")
        self.assertEqual(
            failure.start,
            datetime(2021, 1, 15, 10, 0, tzinfo=gettz("Europe/Berlin")),
        )

    def test_multiple_broken_rules_record_multiple_ranges(self):
        result = parse_events(
            calendar(BROKEN_ALL_DAY, BROKEN_TIMED),
            start=self.start,
            end=self.end,
            coverage=True,
        )

        self.assertIs(result.coverage.status, CoverageStatus.PARTIAL)
        self.assertEqual(
            {failure.uid for failure in result.coverage.failures},
            {"broken-all-day", "broken-timed"},
        )

    def test_coverage_mode_swallows_rule_error_legacy_mode_raises(self):
        content = calendar(BROKEN_ALL_DAY)

        result = parse_events(
            content, start=self.start, end=self.end, coverage=True
        )
        self.assertIs(result.coverage.status, CoverageStatus.PARTIAL)

        with self.assertRaises(ValueError):
            parse_events(content, start=self.start, end=self.end)

    def test_unparseable_calendar_is_failed(self):
        result = parse_events(
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nmissing end",
            start=self.start,
            end=self.end,
            coverage=True,
        )

        self.assertIs(result.coverage.status, CoverageStatus.FAILED)
        self.assertEqual(result.events, [])
        self.assertFalse(result.coverage.can_confirm_empty())
        self.assertEqual(len(result.coverage.failures), 1)

    def test_empty_content_is_failed_in_coverage_mode(self):
        result = parse_events(
            "", start=self.start, end=self.end, coverage=True
        )

        self.assertIs(result.coverage.status, CoverageStatus.FAILED)
        self.assertFalse(result.coverage.can_confirm_empty())

        with self.assertRaises(ValueError):
            parse_events("", start=self.start, end=self.end)

    def test_existing_calendars_still_parse_with_coverage(self):
        with open(BASIC_ICS, mode="r", encoding="utf-8") as handle:
            content = handle.read()

        result = parse_events(
            content,
            start=date(2017, 5, 18),
            end=date(2017, 5, 19),
            coverage=True,
        )

        self.assertEqual(len(result.events), 2)
        self.assertIs(result.coverage.status, CoverageStatus.COMPLETE)


class CachedCoverageTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2017, 5, 18)
        self.end = date(2017, 5, 19)
        icalevents.event_store.clear()
        icalevents.result_store.clear()
        icalevents.threads.clear()

    def test_async_request_stores_versioned_complete_result(self):
        icalevents.events_async(
            "basic",
            url=None,
            file=BASIC_ICS,
            start=self.start,
            end=self.end,
        )

        sleep(1)
        self.assertTrue(icalevents.all_done("basic"))

        cached = icalevents.latest_result("basic")
        self.assertEqual(len(cached.events), 2)
        self.assertIs(cached.coverage.status, CoverageStatus.COMPLETE)
        self.assertEqual(cached.version, CALENDAR_DATA_VERSION)
        self.assertFalse(cached.stale)
        self.assertTrue(cached.coverage.can_confirm_empty())
        # legacy cache accessor stays in sync
        self.assertEqual(len(icalevents.latest_events("basic")), 2)

    def test_old_cache_version_is_reported_stale(self):
        successful = icalevents.events_with_coverage(
            file=BASIC_ICS, start=self.start, end=self.end
        )

        icalevents.update_result(
            "old",
            icalevents.CachedResult(
                events=successful.events,
                coverage=successful.coverage,
                version=CALENDAR_DATA_VERSION - 1,
                fetched_at=successful.coverage.start,
            ),
        )

        cached = icalevents.latest_result("old")
        self.assertTrue(cached.stale)
        self.assertFalse(cached.coverage.can_confirm_empty())

    def test_failed_request_without_cache_is_failed(self):
        icalevents.request_data(
            "missing",
            url=None,
            file=None,
            string_content=None,
            start=self.start,
            end=self.end,
            fix_apple=False,
        )

        cached = icalevents.latest_result("missing")
        self.assertIs(cached.coverage.status, CoverageStatus.FAILED)
        self.assertEqual(cached.events, [])
        self.assertFalse(cached.coverage.can_confirm_empty())

    def test_failed_refresh_keeps_previous_events_but_marks_stale(self):
        icalevents.request_data(
            "keep",
            url=None,
            file=BASIC_ICS,
            string_content=None,
            start=self.start,
            end=self.end,
            fix_apple=False,
        )
        previous = icalevents.latest_result("keep")
        self.assertEqual(len(previous.events), 2)

        icalevents.request_data(
            "keep",
            url=None,
            file=None,
            string_content=None,
            start=self.start,
            end=self.end,
            fix_apple=False,
        )

        refreshed = icalevents.latest_result("keep")
        self.assertIs(refreshed.coverage.status, CoverageStatus.FAILED)
        self.assertEqual(len(refreshed.events), 2)
        self.assertTrue(refreshed.stale)
        self.assertFalse(refreshed.coverage.can_confirm_empty())

    def test_events_with_coverage_never_raises_on_bad_content(self):
        result = icalevents.events_with_coverage(
            string_content="not an ical calendar",
            start=self.start,
            end=self.end,
        )

        self.assertIs(result.coverage.status, CoverageStatus.FAILED)
        self.assertEqual(result.events, [])

        with self.assertRaises(Exception):
            icalevents.events(
                string_content="not an ical calendar",
                start=self.start,
                end=self.end,
            )

    def test_events_with_coverage_from_file(self):
        result = icalevents.events_with_coverage(
            file=BASIC_ICS, start=self.start, end=self.end
        )

        self.assertIs(result.coverage.status, CoverageStatus.COMPLETE)
        self.assertEqual(len(result.events), 2)
        self.assertTrue(result.coverage.can_confirm_empty())


if __name__ == "__main__":
    unittest.main()
