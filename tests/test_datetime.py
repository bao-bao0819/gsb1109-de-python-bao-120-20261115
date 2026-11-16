from datetime import datetime, timedelta, timezone
from unittest import TestCase

from hl7.datatypes import (
    _UTCOffset,
    datetime_range_report,
    describe_datetime_ranges,
    filter_datetime_ranges,
    parse_datetime,
    parse_datetime_range,
    sort_datetime_ranges,
)


class DatetimeTest(TestCase):
    def test_parse_date(self):
        self.assertEqual(datetime(1901, 2, 13), parse_datetime("19010213"))

    def test_parse_datetime(self):
        self.assertEqual(
            datetime(2014, 3, 11, 14, 25, 33), parse_datetime("20140311142533")
        )

    def test_parse_datetime_frac(self):
        self.assertEqual(
            datetime(2014, 3, 11, 14, 25, 33, 100000),
            parse_datetime("20140311142533.1"),
        )
        self.assertEqual(
            datetime(2014, 3, 11, 14, 25, 33, 10000),
            parse_datetime("20140311142533.01"),
        )
        self.assertEqual(
            datetime(2014, 3, 11, 14, 25, 33, 1000),
            parse_datetime("20140311142533.001"),
        )
        self.assertEqual(
            datetime(2014, 3, 11, 14, 25, 33, 100),
            parse_datetime("20140311142533.0001"),
        )

    def test_parse_tz(self):
        self.assertEqual(
            datetime(2014, 3, 11, 14, 12, tzinfo=_UTCOffset(330)),
            parse_datetime("201403111412+0530"),
        )
        self.assertEqual(
            datetime(2014, 3, 11, 14, 12, 20, tzinfo=_UTCOffset(-300)),
            parse_datetime("20140311141220-0500"),
        )

    def test_tz(self):
        self.assertEqual("+0205", _UTCOffset(125).tzname(datetime.utcnow()))
        self.assertEqual("-0410", _UTCOffset(-250).tzname(datetime.utcnow()))

    def test_parse_tzname(self):
        dt = parse_datetime("201403111412-0500")
        self.assertEqual("-0500", dt.tzname())
        dt = parse_datetime("201403111412+0530")
        self.assertEqual("+0530", dt.tzname())

    def test_utc_offset_float(self):
        self.assertEqual("-0500", _UTCOffset(-300.0).tzname(datetime.utcnow()))
        self.assertEqual("+0530", _UTCOffset(330.0).tzname(datetime.utcnow()))

    def test_parse_negative_zero_offset(self):
        dt = parse_datetime("201403111412-0030")
        self.assertEqual(dt.tzinfo, _UTCOffset(-30))

    def test_utcoffset_equality(self):
        self.assertEqual(_UTCOffset(60), _UTCOffset(60))
        self.assertNotEqual(_UTCOffset(60), _UTCOffset(-60))
        self.assertNotEqual(_UTCOffset(60), 60)

    def test_utcoffset_hash(self):
        a = _UTCOffset(45)
        b = _UTCOffset(45)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(len({a, b}), 1)


class DatetimeRangeTest(TestCase):
    def test_preserves_original_and_precision(self):
        parsed = parse_datetime_range("202403")
        self.assertEqual("202403", parsed.raw)
        self.assertEqual("202403", parsed.original)
        self.assertEqual(6, parsed.precision)
        self.assertEqual(6, parsed.precision_digits)
        self.assertEqual(6, parsed.precision_code)
        self.assertEqual(6, parsed.precision_level)
        self.assertEqual(6, parsed.precision_length)

    def test_month_range_covers_whole_month(self):
        parsed = parse_datetime_range("202402")
        self.assertEqual(datetime(2024, 2, 1), parsed.start)
        self.assertEqual(datetime(2024, 3, 1), parsed.end)
        self.assertEqual((datetime(2024, 2, 1), datetime(2024, 3, 1)), parsed.range)
        self.assertEqual(datetime(2024, 2, 1), parsed.range_start)
        self.assertEqual(datetime(2024, 3, 1), parsed.range_end)
        self.assertEqual(timedelta(days=29), parsed.granularity)

    def test_day_range_handles_leap_day(self):
        parsed = parse_datetime_range("20240229")
        self.assertEqual(datetime(2024, 2, 29), parsed.start)
        self.assertEqual(datetime(2024, 3, 1), parsed.end)

    def test_year_range(self):
        parsed = parse_datetime_range("2024")
        self.assertEqual(datetime(2024, 1, 1), parsed.start)
        self.assertEqual(datetime(2025, 1, 1), parsed.end)

    def test_fraction_quantizes_range_end(self):
        parsed = parse_datetime_range("20240315123045.123")
        self.assertEqual(datetime(2024, 3, 15, 12, 30, 45, 123000), parsed.start)
        self.assertEqual(datetime(2024, 3, 15, 12, 30, 45, 124000), parsed.end)
        self.assertEqual(17, parsed.precision)
        self.assertEqual("millisecond", parsed.precision_name)
        self.assertEqual(17, parsed.precision_digits)

    def test_fraction_with_offset(self):
        parsed = parse_datetime_range("20240315123045.123+0530")
        self.assertEqual(_UTCOffset(330), parsed.start.tzinfo)
        self.assertEqual(
            datetime(2024, 3, 15, 12, 30, 45, 123000),
            parsed.start.replace(tzinfo=None),
        )
        self.assertTrue(parsed.has_offset)
        self.assertEqual("declared", parsed.timezone_source)
        self.assertEqual("+05:30", parsed.offset)
        self.assertEqual(timedelta(hours=5, minutes=30), parsed.utc_offset)

    def test_source_timezone_used_for_missing_offset(self):
        parsed = parse_datetime_range("20240315123045", source_tz="+08:00")
        self.assertEqual(_UTCOffset(480), parsed.start.tzinfo)
        self.assertEqual("source", parsed.timezone_source)
        self.assertEqual(timedelta(hours=8), parsed.start.utcoffset())

    def test_source_timezone_alias(self):
        parsed = parse_datetime_range("20240315", source_timezone="+08:00")
        self.assertEqual(_UTCOffset(480), parsed.start.tzinfo)

    def test_range_unpacks_and_indexes(self):
        parsed = parse_datetime_range("202403")
        start, end = parsed
        self.assertEqual(parsed.start, start)
        self.assertEqual(parsed.end, end)
        self.assertEqual(parsed.end, parsed[1])
        self.assertEqual((parsed.start, parsed.end), parsed)

    def test_describe_includes_declaration_and_precision(self):
        described = describe_datetime_ranges("202403")
        self.assertEqual("202403", described["raw"])
        self.assertEqual(6, described["precision"])
        self.assertEqual("month", described["precision_name"])
        self.assertEqual(
            ["2024-03-01T00:00:00", "2024-04-01T00:00:00"], described["range"]
        )

    def test_declared_offset_wins_over_source_timezone(self):
        parsed = parse_datetime_range("20240315123045-0500", source_tz="Asia/Shanghai")
        self.assertEqual(_UTCOffset(-300), parsed.start.tzinfo)

    def test_iana_timezone_handles_dst_gap(self):
        parsed = parse_datetime_range("202403100230", source_tz="America/New_York")
        self.assertIsNotNone(parsed.start.tzinfo)
        self.assertEqual(timedelta(minutes=1), parsed.granularity)

    def test_naive_without_source_timezone(self):
        parsed = parse_datetime_range("20240315")
        self.assertIsNone(parsed.start.tzinfo)
        self.assertEqual("naive", parsed.timezone_source)

    def test_explicit_utc_marker(self):
        parsed = parse_datetime_range("20240315123045Z")
        self.assertEqual(timezone.utc.utcoffset(None), parsed.start.utcoffset())

    def test_aware_and_naive_comparison_rejected(self):
        aware = parse_datetime_range("20240315", source_tz="+08:00")
        naive = parse_datetime_range("20240315")
        with self.assertRaises(ValueError):
            aware.overlaps(naive)

    def test_invalid_inputs(self):
        for value in ["202", "2024031", "20240230", "2024031512.5", "20240315+25"]:
            with self.assertRaises(ValueError, msg=value):
                parse_datetime_range(value)

    def test_old_parse_datetime_still_returns_start(self):
        self.assertEqual(datetime(2024, 3, 1), parse_datetime("202403"))
        self.assertIsNone(parse_datetime(""))


class DatetimeFilterTest(TestCase):
    def test_cross_day_overlap_is_separate(self):
        result = filter_datetime_ranges(
            ["202403", "20240315", "202403150900", "20240316"],
            "20240315",
            "20240316",
        )
        self.assertEqual(
            ["20240315", "202403150900"], [x.raw for x in result.contained]
        )
        self.assertEqual(["202403"], [x.raw for x in result.overlapping])
        self.assertEqual(["20240316"], [x.raw for x in result.outside])
        self.assertEqual(
            ["20240315", "202403150900", "202403"], [x.raw for x in result.matched]
        )

    def test_end_boundary_is_exclusive(self):
        result = filter_datetime_ranges(
            ["20240316000000", "20240315235959"], "20240315", "20240316"
        )
        self.assertEqual(["20240315235959"], [x.raw for x in result.contained])
        self.assertEqual(["20240316000000"], [x.raw for x in result.outside])

    def test_partial_day_overlap_is_not_padded(self):
        result = filter_datetime_ranges(
            ["20240315", "202403150900"],
            "202403151000",
            "202403152300",
        )
        self.assertEqual([], list(result.contained))
        self.assertEqual(["20240315"], [x.raw for x in result.overlapping])
        self.assertEqual(["202403150900"], [x.raw for x in result.outside])

    def test_source_timezone_for_filter_and_values(self):
        result = filter_datetime_ranges(
            ["20240315120000", "20240315120000+0000"],
            "20240315110000",
            "20240315130000",
            source_tz="+08:00",
        )
        self.assertEqual(["20240315120000"], [x.raw for x in result.contained])
        self.assertEqual([], list(result.overlapping))
        self.assertEqual(["20240315120000+0000"], [x.raw for x in result.outside])

    def test_order_is_preserved(self):
        result = filter_datetime_ranges(
            ["202403150900", "202403150700", "202403150800"],
            "20240315",
            "20240316",
        )
        self.assertEqual(
            ["202403150900", "202403150700", "202403150800"],
            [x.raw for x in result.contained],
        )

    def test_result_serialization(self):
        result = filter_datetime_ranges(["202403", "20240315"], "20240315", "20240316")
        payload = result.to_dict()
        self.assertEqual(["202403"], [x["raw"] for x in payload["overlapping"]])
        self.assertEqual(["20240315"], [x["raw"] for x in payload["contained"]])

    def test_filter_accepts_a_single_day_boundary(self):
        result = filter_datetime_ranges(["202403150900", "20240316"], "20240315")
        self.assertEqual(["202403150900"], [x.raw for x in result.contained])
        self.assertEqual(["20240316"], [x.raw for x in result.outside])

    def test_empty_filter_range_rejected(self):
        with self.assertRaises(ValueError):
            filter_datetime_ranges(["20240315"], "20240315", "20240315")

    def test_sort_keeps_partial_dates_as_ranges(self):
        ordered = sort_datetime_ranges(["202403", "20240315", "2024", "20240315000000"])
        self.assertEqual(
            ["2024", "202403", "20240315000000", "20240315"],
            [item.raw for item in ordered],
        )
        # Same start: the one-second instant ends before the whole-day range.
        self.assertEqual(
            [timedelta(seconds=1), timedelta(days=1)],
            [item.granularity for item in ordered[2:]],
        )

    def test_report_keeps_every_declaration_and_overlap_group(self):
        report = datetime_range_report(
            ["202403", "20240315", "20240316"],
            "20240315",
            "20240316",
        )
        self.assertEqual(
            ["202403", "20240315", "20240316"],
            [item["raw"] for item in report["declarations"]],
        )
        self.assertEqual(["20240315"], report["contained"])
        self.assertEqual(["202403"], report["overlapping"])
        self.assertEqual(["20240316"], report["outside"])

    def test_day_helper_uses_declaration_end(self):
        from hl7.datatypes import filter_by_day

        result = filter_by_day(["202403", "20240315", "20240316"], "20240315")
        self.assertEqual(["20240315"], [x.raw for x in result.contained])
        self.assertEqual(["202403"], [x.raw for x in result.overlapping])
        self.assertEqual(["20240316"], [x.raw for x in result.outside])

    def test_declaration_info_has_three_core_fields(self):
        info = parse_datetime_range("202403").declaration_info
        self.assertEqual(
            {
                "declaration": "202403",
                "precision": 6,
                "comparable_range": [
                    "2024-03-01T00:00:00",
                    "2024-04-01T00:00:00",
                ],
            },
            info,
        )
