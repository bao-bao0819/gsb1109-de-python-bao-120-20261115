import struct
import unittest
import time

from pythonosc.parsing import ntp

# System time of the first era wraparound (2036-02-07T06:28:16Z).
_ERA_BOUNDARY = ntp._NTP_ERA_SECONDS - ntp._NTP_DELTA


class TestNTP(unittest.TestCase):
    """TODO: Write real tests for this when I get time..."""

    def test_nto_to_system_time(self):
        unix_time = time.time()
        timestamp = ntp.system_time_to_ntp(unix_time)
        unix_time2 = ntp.ntp_to_system_time(timestamp)
        self.assertTrue(type(unix_time) is float)
        self.assertTrue(type(timestamp) is bytes)
        self.assertTrue(type(unix_time2) is float)
        self.assertAlmostEqual(unix_time, unix_time2, places=5)


class TestInferNtpEraCandidates(unittest.TestCase):
    def test_unique_era(self):
        unix_time = 1700000000.0  # 2023-11-14, era 0.
        raw = struct.unpack(">Q", ntp.system_time_to_ntp(unix_time))[0]
        candidates = ntp.infer_ntp_era_candidates(
            raw, unix_time - 3600.0, unix_time + 3600.0
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(0, candidates[0].era)
        self.assertAlmostEqual(unix_time, candidates[0].system_time, places=5)

    def test_accepts_bytes_and_keeps_original_counts(self):
        raw = (3912365441 << 32) | 123456789
        timestamp = ntp.parse_timestamp(raw)
        system_time = raw * ntp._NTP_TIMESTAMP_TO_SECONDS - ntp._NTP_DELTA
        candidates = ntp.infer_ntp_era_candidates(
            struct.pack(">Q", raw), system_time - 1.0, system_time + 1.0
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(timestamp, candidates[0].timestamp)
        self.assertEqual(3912365441, candidates[0].timestamp.seconds)
        self.assertEqual(123456789, candidates[0].timestamp.fraction)

    def test_window_spanning_wraparound_resolves_after_wrap(self):
        # Seconds field wrapped to 5: belongs to era 1 even though the
        # window starts in era 0.
        raw = 5 << 32
        candidates = ntp.infer_ntp_era_candidates(
            raw, _ERA_BOUNDARY - 100.0, _ERA_BOUNDARY + 100.0
        )
        self.assertEqual([1], [c.era for c in candidates])
        self.assertAlmostEqual(_ERA_BOUNDARY + 5.0, candidates[0].system_time)

    def test_window_spanning_wraparound_resolves_before_wrap(self):
        # Seconds field near 2**32: belongs to era 0 even though the
        # window ends in era 1.
        raw = (ntp._NTP_ERA_SECONDS - 10) << 32
        candidates = ntp.infer_ntp_era_candidates(
            raw, _ERA_BOUNDARY - 100.0, _ERA_BOUNDARY + 100.0
        )
        self.assertEqual([0], [c.era for c in candidates])
        self.assertAlmostEqual(_ERA_BOUNDARY - 10.0, candidates[0].system_time)

    def test_ambiguous_window_lists_candidates(self):
        raw = 5 << 32
        candidates = ntp.infer_ntp_era_candidates(
            raw,
            _ERA_BOUNDARY - 100.0,
            _ERA_BOUNDARY + ntp._NTP_ERA_SECONDS + 100.0,
        )
        self.assertEqual([1, 2], [c.era for c in candidates])

    def test_no_era_fits_window(self):
        raw = 5 << 32
        self.assertEqual([], ntp.infer_ntp_era_candidates(raw, 0.0, 1000.0))

    def test_immediately_is_not_an_absolute_time(self):
        with self.assertRaises(ntp.NtpError):
            ntp.infer_ntp_era_candidates(1, 0.0, 10.0**10)
        with self.assertRaises(ntp.NtpError):
            ntp.infer_ntp_era_candidates(ntp.IMMEDIATELY, 0.0, 10.0**10)

    def test_invalid_window(self):
        with self.assertRaises(ntp.NtpError):
            ntp.infer_ntp_era_candidates(0, 1000.0, 0.0)


if __name__ == "__main__":
    unittest.main()
