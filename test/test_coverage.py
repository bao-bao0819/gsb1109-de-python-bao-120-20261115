import unittest
from datetime import date, datetime, timedelta

from icalevents.coverage import (
    CALENDAR_DATA_VERSION,
    CoverageResult,
    CoverageStatus,
    RecurrenceFailure,
    complete_coverage,
    failed_coverage,
    partial_coverage,
)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.start = date(2021, 1, 1)
        self.end = date(2021, 2, 1)

    def test_complete_coverage_covers_whole_window(self):
        coverage = complete_coverage(self.start, self.end)

        self.assertIs(coverage.status, CoverageStatus.COMPLETE)
        self.assertEqual(coverage.version, CALENDAR_DATA_VERSION)
        self.assertEqual(coverage.covered_intervals, ((self.start, self.end),))
        self.assertEqual(coverage.failures, ())
        self.assertTrue(coverage.can_confirm_empty())

    def test_only_complete_non_stale_confirms_empty(self):
        coverage = complete_coverage(self.start, self.end).as_stale()

        self.assertIs(coverage.status, CoverageStatus.COMPLETE)
        self.assertTrue(coverage.stale)
        self.assertFalse(coverage.can_confirm_empty())

    def test_partial_coverage_lists_affected_range(self):
        failure = RecurrenceFailure(
            uid="recurring-1",
            summary="Broken rule",
            start=date(2021, 1, 15),
            end=self.end,
            reason="invalid 'FREQ': BOGUS",
        )

        coverage = partial_coverage(self.start, self.end, [failure])

        self.assertIs(coverage.status, CoverageStatus.PARTIAL)
        self.assertEqual(len(coverage.failures), 1)
        self.assertEqual(coverage.failures[0].uid, "recurring-1")
        self.assertEqual(coverage.failures[0].start, date(2021, 1, 15))
        self.assertFalse(coverage.can_confirm_empty())
        # the part of the window before DTSTART is still fully covered
        self.assertEqual(
            coverage.covered_intervals, ((date(2021, 1, 1), date(2021, 1, 15)),)
        )

    def test_partial_coverage_never_marks_whole_window_free(self):
        failure = RecurrenceFailure(
            uid="recurring-1",
            summary=None,
            start=date(2020, 12, 20),
            end=date(2021, 3, 1),
            reason="broken",
        )

        coverage = partial_coverage(self.start, self.end, [failure])

        self.assertIs(coverage.status, CoverageStatus.PARTIAL)
        # the failure is clamped to the query window, not silently expanded
        self.assertEqual(coverage.failures[0].start, self.start)
        self.assertEqual(coverage.failures[0].end, self.end)
        self.assertEqual(coverage.covered_intervals, ())
        self.assertFalse(coverage.can_confirm_empty())

    def test_failed_coverage_does_not_confirm_empty(self):
        coverage = failed_coverage(self.start, self.end, "connection lost")

        self.assertIs(coverage.status, CoverageStatus.FAILED)
        self.assertEqual(coverage.covered_intervals, ())
        self.assertEqual(len(coverage.failures), 1)
        self.assertEqual(coverage.failures[0].reason, "connection lost")
        self.assertFalse(coverage.can_confirm_empty())

    def test_merge_takes_worst_status(self):
        complete = complete_coverage(self.start, self.end)
        failure = RecurrenceFailure(
            uid="x", summary=None, start=date(2021, 1, 10), end=self.end, reason="r"
        )
        partial = partial_coverage(self.start, self.end, [failure])
        failed = failed_coverage(self.start, self.end, "boom")

        self.assertIs(
            CoverageResult.merge([complete, partial]).status,
            CoverageStatus.PARTIAL,
        )
        self.assertIs(
            CoverageResult.merge([partial, failed]).status,
            CoverageStatus.FAILED,
        )
        self.assertIs(
            CoverageResult.merge([complete, complete]).status,
            CoverageStatus.COMPLETE,
        )

    def test_merge_combines_failures_and_covered_intervals(self):
        first = partial_coverage(
            self.start,
            self.end,
            [
                RecurrenceFailure(
                    uid="a", summary=None, start=date(2021, 1, 10),
                    end=date(2021, 1, 12), reason="r1",
                )
            ],
        )
        second = partial_coverage(
            self.start,
            self.end,
            [
                RecurrenceFailure(
                    uid="b", summary=None, start=date(2021, 1, 20),
                    end=date(2021, 1, 22), reason="r2",
                )
            ],
        )

        merged = CoverageResult.merge([first, second])

        self.assertEqual({failure.uid for failure in merged.failures}, {"a", "b"})
        self.assertEqual(merged.status, CoverageStatus.PARTIAL)
        self.assertEqual(merged.start, self.start)
        self.assertEqual(merged.end, self.end)

    def test_merge_keeps_version_only_when_consistent(self):
        first = complete_coverage(self.start, self.end)
        second = complete_coverage(self.start, self.end)

        self.assertEqual(
            CoverageResult.merge([first, second]).version, CALENDAR_DATA_VERSION
        )

        old = CoverageResult(
            status=CoverageStatus.COMPLETE,
            start=self.start,
            end=self.end,
            version=CALENDAR_DATA_VERSION + 1,
        )
        self.assertIsNone(CoverageResult.merge([first, old]).version)
        self.assertTrue(CoverageResult.merge([first, old]).stale)

    def test_merge_requires_at_least_one_result(self):
        with self.assertRaises(ValueError):
            CoverageResult.merge([])

    def test_mixed_date_and_datetime_points_are_handled(self):
        failure = RecurrenceFailure(
            uid="dt",
            summary=None,
            start=datetime(2021, 1, 15, 10, 0),
            end=datetime(2021, 1, 20, 10, 0),
            reason="r",
        )

        coverage = partial_coverage(self.start, self.end, [failure])

        self.assertEqual(coverage.status, CoverageStatus.PARTIAL)
        self.assertEqual(coverage.failures[0].start, datetime(2021, 1, 15, 10, 0))
        self.assertTrue(coverage.covered_intervals)

        merged = CoverageResult.merge(
            [coverage, complete_coverage(self.start, self.end)]
        )
        self.assertEqual(merged.status, CoverageStatus.PARTIAL)

    def test_overlapping_covered_intervals_are_unioned(self):
        first = CoverageResult(
            status=CoverageStatus.COMPLETE,
            start=self.start,
            end=date(2021, 1, 15),
            version=CALENDAR_DATA_VERSION,
            covered_intervals=((self.start, date(2021, 1, 15)),),
        )
        second = CoverageResult(
            status=CoverageStatus.COMPLETE,
            start=date(2021, 1, 10),
            end=self.end,
            version=CALENDAR_DATA_VERSION,
            covered_intervals=((date(2021, 1, 10), self.end),),
        )

        merged = CoverageResult.merge([first, second])

        self.assertEqual(merged.covered_intervals, ((self.start, self.end),))
        self.assertEqual(merged.start, self.start)
        self.assertEqual(merged.end, self.end)

    def test_stale_propagates_through_merge(self):
        fresh = complete_coverage(self.start, self.end)
        stale = complete_coverage(self.start, self.end).as_stale()

        self.assertTrue(CoverageResult.merge([fresh, stale]).stale)
        self.assertFalse(CoverageResult.merge([fresh, fresh]).stale)

    def test_timedelta_window_is_supported(self):
        start = datetime(2021, 1, 1, 9, 0)
        end = start + timedelta(hours=2)
        coverage = complete_coverage(start, end)

        self.assertEqual(coverage.covered_intervals, ((start, end),))
        self.assertTrue(coverage.can_confirm_empty())


if __name__ == "__main__":
    unittest.main()
