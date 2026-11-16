import unittest

from pythonosc import osc_bundle
from pythonosc import osc_bundle_builder
from pythonosc import osc_bundle_splitter
from pythonosc import osc_message
from pythonosc import osc_message_builder
from pythonosc.parsing import ntp


def _make_message(address: str, *args) -> osc_message.OscMessage:
    builder = osc_message_builder.OscMessageBuilder(address=address)
    for value in args:
        builder.add_arg(value)
    return builder.build()


def _make_bundle(timestamp, *contents) -> osc_bundle.OscBundle:
    builder = osc_bundle_builder.OscBundleBuilder(timestamp)
    for content in contents:
        builder.add_content(content)
    return builder.build()


def _flatten(packets):
    """Return all messages of the given packets, traversing nested bundles."""
    messages = []

    def collect(bundle):
        for content in bundle:
            if isinstance(content, osc_bundle.OscBundle):
                collect(content)
            else:
                messages.append(content)

    for packet in packets:
        collect(packet)
    return messages


class TestSplitBundle(unittest.TestCase):
    def test_bundle_already_fitting_is_returned_as_is(self):
        bundle = _make_bundle(
            osc_bundle_builder.IMMEDIATELY, _make_message("/a", 1), _make_message("/b")
        )
        packets = osc_bundle_splitter.split_bundle(bundle, bundle.size)
        self.assertEqual(1, len(packets))
        self.assertEqual(bundle.dgram, packets[0].dgram)

    def test_empty_bundle(self):
        bundle = _make_bundle(osc_bundle_builder.IMMEDIATELY)
        packets = osc_bundle_splitter.split_bundle(bundle, 16)
        self.assertEqual(1, len(packets))
        self.assertEqual(0, packets[0].num_contents)

    def test_split_preserves_order_and_timetag(self):
        timestamp = 1700000000.5
        messages = [_make_message(f"/addr/{i}", i, f"value-{i}") for i in range(10)]
        bundle = _make_bundle(timestamp, *messages)
        max_size = 64
        self.assertGreater(bundle.size, max_size)

        packets = osc_bundle_splitter.split_bundle(bundle, max_size)
        self.assertGreater(len(packets), 1)
        for packet in packets:
            self.assertLessEqual(packet.size, max_size)
            # The original timetag bytes are preserved verbatim.
            self.assertEqual(bundle.dgram[8:16], packet.dgram[8:16])
            self.assertEqual(bundle.timestamp, packet.timestamp)
        # Order and content of messages are preserved across packets.
        self.assertEqual(
            [m.dgram for m in messages], [m.dgram for m in _flatten(packets)]
        )

    def test_split_with_immediately_timetag(self):
        messages = [_make_message("/m", i) for i in range(5)]
        bundle = _make_bundle(osc_bundle_builder.IMMEDIATELY, *messages)
        packets = osc_bundle_splitter.split_bundle(bundle, 32)
        for packet in packets:
            self.assertEqual(ntp.IMMEDIATELY, packet.dgram[8:16])
            self.assertLessEqual(packet.size, 32)
        self.assertEqual(
            [m.dgram for m in messages], [m.dgram for m in _flatten(packets)]
        )

    def test_sizes_account_for_alignment_padding(self):
        # Odd-length strings and blobs exercise the 4-byte alignment padding.
        messages = [
            _make_message("/pad", "abc"),
            _make_message("/pad", b"\x01\x02\x03"),
            _make_message("/pads", "abcde"),
            _make_message("/pad", b"\x01"),
        ]
        bundle = _make_bundle(osc_bundle_builder.IMMEDIATELY, *messages)
        # Tightest limit that still fits the largest message.
        largest = max(m.size for m in messages)
        max_size = 16 + 4 + largest
        packets = osc_bundle_splitter.split_bundle(bundle, max_size)
        self.assertEqual(len(messages), len(packets))
        for packet, message in zip(packets, messages):
            self.assertLessEqual(packet.size, max_size)
            self.assertEqual(message.dgram, packet.content(0).dgram)

    def test_single_message_exceeding_limit_raises(self):
        blob = bytes(range(256))
        message = _make_message("/blob", blob)
        bundle = _make_bundle(osc_bundle_builder.IMMEDIATELY, message)
        limit = 16 + 4 + message.size - 1  # One byte short.
        with self.assertRaises(osc_bundle_splitter.BundleSplitError):
            osc_bundle_splitter.split_bundle(bundle, limit)
        # The message itself fits: no truncation of the blob happens.
        packets = osc_bundle_splitter.split_bundle(bundle, limit + 1)
        self.assertEqual(blob, packets[0].content(0).params[0])

    def test_invalid_max_size_raises(self):
        bundle = _make_bundle(osc_bundle_builder.IMMEDIATELY, _make_message("/a"))
        for bad_limit in (0, 8, 15):
            with self.assertRaises(ValueError):
                osc_bundle_splitter.split_bundle(bundle, bad_limit)

    def test_nested_bundle_kept_whole_when_it_fits(self):
        nested = _make_bundle(
            osc_bundle_builder.IMMEDIATELY,
            _make_message("/in", 1),
            _make_message("/in", 2),
        )
        bundle = _make_bundle(
            osc_bundle_builder.IMMEDIATELY, _make_message("/out"), nested
        )
        packets = osc_bundle_splitter.split_bundle(bundle, bundle.size)
        self.assertEqual(1, len(packets))
        self.assertIsInstance(packets[0].content(1), osc_bundle.OscBundle)
        self.assertEqual(nested.dgram, packets[0].content(1).dgram)

    def test_nested_bundle_split_preserves_nesting_and_timetag(self):
        nested_timestamp = 1700000000.25
        nested_messages = [_make_message("/nested", i) for i in range(6)]
        nested = _make_bundle(nested_timestamp, *nested_messages)
        bundle = _make_bundle(
            osc_bundle_builder.IMMEDIATELY, _make_message("/head"), nested
        )
        # Room for the head message plus one nested element per packet only.
        max_size = (
            16 + 4 + _make_message("/head").size + 4 + 16 + 4 + nested_messages[0].size
        )
        packets = osc_bundle_splitter.split_bundle(bundle, max_size)
        self.assertGreater(len(packets), 1)
        for packet in packets:
            self.assertLessEqual(packet.size, max_size)
        # Nested messages stay nested, in order, with their own timetag.
        sub_bundles = [
            content
            for packet in packets
            for content in packet
            if isinstance(content, osc_bundle.OscBundle)
        ]
        self.assertGreater(len(sub_bundles), 1)
        for sub_bundle in sub_bundles:
            self.assertEqual(nested.dgram[8:16], sub_bundle.dgram[8:16])
        self.assertEqual(
            [m.dgram for m in nested_messages],
            [m.dgram for m in _flatten(sub_bundles)],
        )
        # Global order is preserved: /head comes before every nested message.
        self.assertEqual("/head", _flatten(packets)[0].address)

    def test_nested_bundle_with_oversized_message_is_rejected(self):
        nested = _make_bundle(
            osc_bundle_builder.IMMEDIATELY, _make_message("/big", bytes(128))
        )
        bundle = _make_bundle(osc_bundle_builder.IMMEDIATELY, nested)
        with self.assertRaises(osc_bundle_splitter.BundleSplitError):
            osc_bundle_splitter.split_bundle(bundle, 64)

    def test_empty_nested_bundle_that_cannot_fit_is_rejected(self):
        nested = _make_bundle(osc_bundle_builder.IMMEDIATELY)
        bundle = _make_bundle(osc_bundle_builder.IMMEDIATELY, nested)
        # An empty nested bundle needs 16 + 4 + 16 = 36 bytes.
        packets = osc_bundle_splitter.split_bundle(bundle, 36)
        self.assertEqual(nested.dgram, packets[0].content(0).dgram)
        with self.assertRaises(osc_bundle_splitter.BundleSplitError):
            osc_bundle_splitter.split_bundle(bundle, 35)


if __name__ == "__main__":
    unittest.main()
