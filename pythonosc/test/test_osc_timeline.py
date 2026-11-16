import struct
import unittest

from pythonosc import osc_bundle_builder
from pythonosc import osc_message_builder
from pythonosc import osc_timeline
from pythonosc.parsing import ntp
from pythonosc.parsing import osc_types

# An absolute system-epoch time used as an ordinary, non-special time-tag.
_T1 = 1_000_000.0
_T2 = 2_000_000.0
_T3 = 3_000_000.0


def _message(address="/a", value=1.0):
    builder = osc_message_builder.OscMessageBuilder(address=address)
    builder.add_arg(value)
    return builder.build()


def _bundle(timestamp, *contents):
    builder = osc_bundle_builder.OscBundleBuilder(timestamp)
    for content in contents:
        builder.add_content(content)
    return builder.build()


def _element(dgram):
    """Wrap a raw element datagram with its leading int32 size, as in a bundle."""
    return osc_types.write_int(len(dgram)) + dgram


class TestResolveTime(unittest.TestCase):
    def test_immediate_without_base_is_undetermined(self):
        self.assertIsNone(osc_timeline._resolve_time(osc_types.IMMEDIATELY, None))

    def test_immediate_inherits_parent_time(self):
        self.assertEqual(_T1, osc_timeline._resolve_time(osc_types.IMMEDIATELY, _T1))

    def test_absolute_time_without_base_is_kept(self):
        self.assertEqual(_T2, osc_timeline._resolve_time(_T2, None))

    def test_inner_time_clamped_to_parent(self):
        # Inner schedule earlier than the enclosing bundle must not win.
        self.assertEqual(_T2, osc_timeline._resolve_time(_T1, _T2))
        # Inner schedule later than the parent is allowed.
        self.assertEqual(_T3, osc_timeline._resolve_time(_T3, _T2))


class TestBuildTimeline(unittest.TestCase):
    def test_bare_message_is_undetermined_not_today(self):
        timeline = osc_timeline.build_timeline(_message("/solo").dgram)
        self.assertEqual(1, len(timeline.messages))
        self.assertIsNone(timeline.messages[0].time)
        self.assertEqual([], timeline.errors)

    def test_immediate_bundle_messages_are_undetermined(self):
        packet = _bundle(osc_types.IMMEDIATELY, _message("/a"), _message("/b")).dgram
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual(2, len(timeline.messages))
        self.assertTrue(all(entry.time is None for entry in timeline.messages))

    def test_absolute_bundle_stamps_all_messages(self):
        packet = _bundle(_T1, _message("/a"), _message("/b")).dgram
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual([_T1, _T1], [entry.time for entry in timeline.messages])

    def test_nested_immediate_inherits_absolute_parent(self):
        inner = _bundle(osc_types.IMMEDIATELY, _message("/inner"))
        packet = _bundle(_T1, _message("/outer"), inner).dgram
        timeline = osc_timeline.build_timeline(packet)
        times = {entry.message.address: entry.time for entry in timeline.messages}
        self.assertEqual(_T1, times["/outer"])
        self.assertEqual(_T1, times["/inner"])

    def test_nested_absolute_is_clamped_by_parent(self):
        # Parent at _T2, inner claims the earlier _T1: parent must win.
        inner = _bundle(_T1, _message("/early"))
        packet = _bundle(_T2, inner, _message("/late")).dgram
        timeline = osc_timeline.build_timeline(packet)
        times = {entry.message.address: entry.time for entry in timeline.messages}
        self.assertEqual(_T2, times["/early"])
        self.assertEqual(_T2, times["/late"])

    def test_nested_absolute_later_than_parent_is_kept(self):
        inner = _bundle(_T3, _message("/later"))
        packet = _bundle(_T1, inner).dgram
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual(_T3, timeline.messages[0].time)

    def test_messages_are_sorted_by_time(self):
        late = _bundle(_T3, _message("/c"))
        mid = _bundle(_T2, _message("/b"))
        packet = _bundle(osc_types.IMMEDIATELY, late, mid, _message("/a")).dgram
        timeline = osc_timeline.build_timeline(packet)
        addresses = [entry.message.address for entry in timeline.messages]
        self.assertEqual(["/b", "/c", "/a"], addresses)
        self.assertEqual([_T2, _T3, None], [entry.time for entry in timeline.messages])

    def test_same_instant_keeps_packet_order(self):
        # Five messages all resolve to the same instant; document order must
        # survive the sort instead of being reordered.
        addresses = [f"/m{i}" for i in range(5)]
        packet = _bundle(_T1, *(_message(a) for a in addresses)).dgram
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual(
            addresses, [entry.message.address for entry in timeline.messages]
        )

    def test_undetermined_entries_keep_packet_order_and_sort_last(self):
        packet = _bundle(
            osc_types.IMMEDIATELY,
            _message("/i0"),
            _bundle(_T1, _message("/t")),
            _message("/i1"),
        ).dgram
        timeline = osc_timeline.build_timeline(packet)
        addresses = [entry.message.address for entry in timeline.messages]
        self.assertEqual(["/t", "/i0", "/i1"], addresses)

    def test_empty_bundle(self):
        packet = _bundle(osc_types.IMMEDIATELY).dgram
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual([], timeline.messages)
        self.assertEqual([], timeline.errors)

    def test_not_a_packet_raises(self):
        with self.assertRaises(osc_timeline.TimelineError):
            osc_timeline.build_timeline(b"junk")


class TestFaultIsolation(unittest.TestCase):
    def _bundle_with_raw_element(self, timestamp, raw_element):
        dgram = b"#bundle\x00" + osc_types.write_date(timestamp) + raw_element
        return dgram

    def test_unknown_element_type_is_reported_not_dropped_silently(self):
        good = _message("/good")
        packet = self._bundle_with_raw_element(
            osc_types.IMMEDIATELY,
            _element(good.dgram) + _element(b"iamnotaslash"),
        )
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual(1, len(timeline.messages))
        self.assertEqual("/good", timeline.messages[0].message.address)
        self.assertEqual(1, len(timeline.errors))

    def test_bad_message_keeps_siblings(self):
        good_before = _message("/before")
        good_after = _message("/after")
        # Starts with '/' and has a valid size, so it is identified as a
        # message, but its ",i" type tag promises an int that is missing.
        broken = b"/x\x00\x00,i\x00\x00"
        packet = self._bundle_with_raw_element(
            osc_types.IMMEDIATELY,
            _element(good_before.dgram) + _element(broken) + _element(good_after.dgram),
        )
        timeline = osc_timeline.build_timeline(packet)
        addresses = [entry.message.address for entry in timeline.messages]
        self.assertEqual(["/before", "/after"], addresses)
        self.assertEqual(1, len(timeline.errors))

    def test_bad_nested_bundle_keeps_outer_siblings(self):
        sibling = _message("/sibling")
        # A nested bundle whose time-tag region is truncated: only 4 bytes
        # where an 8-byte NTP time-tag is expected.
        broken_inner = b"#bundle\x00\x00\x00\x00\x00"
        packet = self._bundle_with_raw_element(
            osc_types.IMMEDIATELY,
            _element(broken_inner) + _element(sibling.dgram),
        )
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual(["/sibling"], [m.message.address for m in timeline.messages])
        self.assertEqual(1, len(timeline.errors))

    def test_invalid_element_size_is_reported_and_stops_bundle(self):
        good = _message("/good")
        packet = (
            b"#bundle\x00"
            + osc_types.write_date(osc_types.IMMEDIATELY)
            + _element(good.dgram)
            + osc_types.write_int(0)
        )
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual(["/good"], [m.message.address for m in timeline.messages])
        self.assertEqual(1, len(timeline.errors))

    def test_one_bad_element_does_not_swallow_other_bundle(self):
        # Two nested bundles: a broken one and a healthy one.
        healthy = _bundle(_T1, _message("/healthy"))
        broken_inner = b"#bundle\x00\x00\x00\x00\x00"
        packet = self._bundle_with_raw_element(
            osc_types.IMMEDIATELY,
            _element(broken_inner) + _element(healthy.dgram),
        )
        timeline = osc_timeline.build_timeline(packet)
        self.assertEqual(["/healthy"], [m.message.address for m in timeline.messages])
        self.assertEqual(1, len(timeline.errors))


class TestNtpEncoding(unittest.TestCase):
    def test_uses_packet_timetag_not_wall_clock(self):
        # Encode a fixed NTP time-tag and confirm it survives verbatim with no
        # dependence on "today" or time.time().
        ntp_seconds = ntp.system_time_to_ntp_epoch(_T1)
        timetag = struct.pack(">Q", int(ntp_seconds * 2.0**32))
        dgram = b"#bundle\x00" + timetag + _element(_message("/a").dgram)
        timeline = osc_timeline.build_timeline(dgram)
        self.assertEqual(1, len(timeline.messages))
        self.assertAlmostEqual(_T1, timeline.messages[0].time, places=4)


if __name__ == "__main__":
    unittest.main()
