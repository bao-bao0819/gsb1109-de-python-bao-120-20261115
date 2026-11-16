import unittest

from datetime import datetime

from pythonosc import osc_columns, osc_message
from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.parsing import osc_types

_DGRAM_ALL_STANDARD_TYPES_OF_PARAMS = (
    b"/SYNC\x00\x00\x00"
    b",ifsb\x00\x00\x00"
    b"\x00\x00\x00\x03"  # 3
    b"@\x00\x00\x00"  # 2.0
    b"ABC\x00"  # "ABC"
    b"\x00\x00\x00\x08stuff\x00\x00\x00"
)

_DGRAM_COMPLEX_ARRAY_PARAMS = (
    b"/SYNC\x00\x00\x00"
    b",[i][[ss]][[i][i[s]]]\x00\x00\x00"
    b"\x00\x00\x00\x01"  # 1
    b"ABC\x00"  # "ABC"
    b"DEF\x00"  # "DEF"
    b"\x00\x00\x00\x02"  # 2
    b"\x00\x00\x00\x03"  # 3
    b"GHI\x00"  # "GHI"
)

_DGRAM_PAYLOAD_LESS_PARAMS = b"/SYNC\x00\x00\x00,TFN\x00\x00\x00\x00"

_DGRAM_TIMETAG_PARAM = b"/SYNC\x00\x00\x00,t\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01"

_DGRAM_BAD_ARRAY_CLOSE = b"/SYNC\x00\x00\x00,[]]\x00\x00\x00\x00"

_DGRAM_MISSING_ARRAY_CLOSE = b"/SYNC\x00\x00\x00,[[]\x00\x00\x00\x00"


def _make_bundle(*elements):
    dgram = b"#bundle\x00" + b"\x00\x00\x00\x00\x00\x00\x00\x01"
    for element in elements:
        dgram += osc_types.write_int(len(element)) + element
    return dgram


class TestSplitMessage(unittest.TestCase):
    def test_splits_standard_types_with_ranges(self):
        view = osc_columns.split_message(_DGRAM_ALL_STANDARD_TYPES_OF_PARAMS)
        self.assertEqual("/SYNC", view.address)
        self.assertEqual(0, view.start)
        self.assertEqual(len(_DGRAM_ALL_STANDARD_TYPES_OF_PARAMS), view.end)
        self.assertEqual(4, len(view.columns))

        self.assertEqual(["i", "f", "s", "b"], [c.type_tag for c in view.columns])
        self.assertEqual([(0,), (1,), (2,), (3,)], [c.path for c in view.columns])
        self.assertEqual(3, view.columns[0].value)
        self.assertAlmostEqual(2.0, view.columns[1].value)
        self.assertEqual("ABC", view.columns[2].value)
        self.assertEqual(b"stuff\x00\x00\x00", view.columns[3].value)

        # Ranges cover the arguments back to back within the original packet.
        self.assertEqual(16, view.columns[0].start)
        for prev, nxt in zip(view.columns, view.columns[1:]):
            self.assertEqual(prev.end, nxt.start)
        self.assertEqual(len(_DGRAM_ALL_STANDARD_TYPES_OF_PARAMS), view.columns[-1].end)
        dgram = _DGRAM_ALL_STANDARD_TYPES_OF_PARAMS
        self.assertEqual(
            b"\x00\x00\x00\x03", dgram[view.columns[0].start : view.columns[0].end]
        )
        self.assertEqual(
            b"@\x00\x00\x00", dgram[view.columns[1].start : view.columns[1].end]
        )
        self.assertEqual(b"ABC\x00", dgram[view.columns[2].start : view.columns[2].end])
        self.assertEqual(
            b"\x00\x00\x00\x08stuff\x00\x00\x00",
            dgram[view.columns[3].start : view.columns[3].end],
        )

    def test_splits_nested_arrays_with_paths(self):
        view = osc_columns.split_message(_DGRAM_COMPLEX_ARRAY_PARAMS)
        self.assertEqual(
            [(0, 0), (1, 0, 0), (1, 0, 1), (2, 0, 0), (2, 1, 0), (2, 1, 1, 0)],
            [c.path for c in view.columns],
        )
        self.assertEqual(
            ["i", "s", "s", "i", "i", "s"], [c.type_tag for c in view.columns]
        )
        self.assertEqual(
            [1, "ABC", "DEF", 2, 3, "GHI"], [c.value for c in view.columns]
        )

    def test_payload_less_types_keep_positions(self):
        view = osc_columns.split_message(_DGRAM_PAYLOAD_LESS_PARAMS)
        self.assertEqual(3, len(view.columns))
        self.assertEqual(["T", "F", "N"], [c.type_tag for c in view.columns])
        self.assertEqual([True, False, None], [c.value for c in view.columns])
        self.assertEqual([(0,), (1,), (2,)], [c.path for c in view.columns])
        # No payload: each column sits at an empty packet range.
        for column in view.columns:
            self.assertEqual(column.start, column.end)

    def test_blob_is_not_a_string(self):
        view = osc_columns.split_message(_DGRAM_ALL_STANDARD_TYPES_OF_PARAMS)
        blob = view.columns[3]
        self.assertEqual("b", blob.type_tag)
        self.assertIsInstance(blob.value, bytes)
        self.assertNotIsInstance(blob.value, str)

    def test_empty_message_has_no_columns(self):
        view = osc_columns.split_message(b"/SYNC\x00\x00\x00")
        self.assertEqual("/SYNC", view.address)
        self.assertEqual((), view.columns)

    def test_raises_on_bad_array_close(self):
        with self.assertRaises(osc_columns.ParseError):
            osc_columns.split_message(_DGRAM_BAD_ARRAY_CLOSE)

    def test_raises_on_missing_array_close(self):
        with self.assertRaises(osc_columns.ParseError):
            osc_columns.split_message(_DGRAM_MISSING_ARRAY_CLOSE)

    def test_raises_on_incorrect_datagram(self):
        with self.assertRaises(osc_columns.ParseError):
            osc_columns.split_message(b"")
        with self.assertRaises(osc_columns.ParseError):
            osc_columns.split_message(b"foobar")


class TestSplitPacket(unittest.TestCase):
    def test_single_message_packet(self):
        views = osc_columns.split_packet(_DGRAM_COMPLEX_ARRAY_PARAMS)
        self.assertEqual(1, len(views))
        self.assertEqual(6, len(views[0].columns))

    def test_bundle_with_two_messages(self):
        good = b"/SYNC\x00\x00\x00,f\x00\x00?\x00\x00\x00"
        bundle = _make_bundle(good, good)
        views = osc_columns.split_packet(bundle)
        self.assertEqual(2, len(views))
        for view in views:
            self.assertEqual(good, bundle[view.start : view.end])
            self.assertEqual(1, len(view.columns))
            column = view.columns[0]
            self.assertEqual(b"?\x00\x00\x00", bundle[column.start : column.end])

    def test_bad_array_close_does_not_eat_following_messages(self):
        good = b"/GOOD\x00\x00\x00,i\x00\x00\x00\x00\x00*"
        bundle = _make_bundle(_DGRAM_BAD_ARRAY_CLOSE, good)
        with self.assertLogs(level="WARNING"):
            views = osc_columns.split_packet(bundle)
        self.assertEqual(1, len(views))
        self.assertEqual("/GOOD", views[0].address)
        self.assertEqual(good, bundle[views[0].start : views[0].end])
        self.assertEqual(42, views[0].columns[0].value)

    def test_nested_bundle(self):
        inner = _make_bundle(b"/A\x00\x00,i\x00\x00\x00\x00\x00\x01")
        outer = _make_bundle(inner, b"/B\x00\x00,N\x00\x00")
        views = osc_columns.split_packet(outer)
        self.assertEqual(["/A", "/B"], [v.address for v in views])
        for view in views:
            self.assertTrue(
                outer[view.start : view.end].startswith(view.address.encode())
            )

    def test_raises_on_non_packet(self):
        with self.assertRaises(osc_columns.ParseError):
            osc_columns.split_packet(b"junk")

    def test_raises_on_structurally_bad_bundle(self):
        with self.assertRaises(osc_columns.ParseError):
            osc_columns.split_packet(b"#bundle\x00\x00\x00\x00\x00")


class TestBuildMessage(unittest.TestCase):
    def _roundtrip(self, dgram):
        view = osc_columns.split_message(dgram)
        rebuilt = osc_columns.build_message(view.address, view.columns)
        self.assertEqual(dgram, rebuilt.dgram)
        self.assertEqual(osc_message.OscMessage(dgram).params, rebuilt.params)
        return rebuilt

    def test_roundtrip_standard_types(self):
        self._roundtrip(_DGRAM_ALL_STANDARD_TYPES_OF_PARAMS)

    def test_roundtrip_complex_array(self):
        rebuilt = self._roundtrip(_DGRAM_COMPLEX_ARRAY_PARAMS)
        self.assertEqual([[1], [["ABC", "DEF"]], [[2], [3, ["GHI"]]]], rebuilt.params)

    def test_roundtrip_payload_less_types(self):
        rebuilt = self._roundtrip(_DGRAM_PAYLOAD_LESS_PARAMS)
        self.assertEqual([True, False, None], rebuilt.params)

    def test_roundtrip_timetag(self):
        rebuilt = self._roundtrip(_DGRAM_TIMETAG_PARAM)
        self.assertEqual([(datetime(1900, 1, 1, 0, 0, 0), 1)], rebuilt.params)

    def test_roundtrip_empty_blob(self):
        dgram = b"/b\x00\x00,b\x00\x00" + osc_types.write_int(0)
        view = osc_columns.split_message(dgram)
        self.assertEqual(b"", view.columns[0].value)
        rebuilt = osc_columns.build_message(view.address, view.columns)
        self.assertEqual(dgram, rebuilt.dgram)
        self.assertEqual([b""], rebuilt.params)

    def test_roundtrip_from_builder(self):
        builder = OscMessageBuilder(address="/nested")
        builder.add_arg([1, [2.5, "x"], True, None, b"\x01\x02"])
        builder.add_arg(False)
        msg = builder.build()
        view = osc_columns.split_message(msg.dgram)
        rebuilt = osc_columns.build_message(view.address, view.columns)
        self.assertEqual(msg.dgram, rebuilt.dgram)
        self.assertEqual(msg.params, rebuilt.params)

    def test_rebuilt_type_sequence_matches_values(self):
        view = osc_columns.split_message(_DGRAM_COMPLEX_ARRAY_PARAMS)
        rebuilt = osc_columns.build_message(view.address, view.columns)
        _, index = osc_types.get_string(rebuilt.dgram, 0)
        type_tag, _ = osc_types.get_string(rebuilt.dgram, index)
        self.assertEqual(",[i][[ss]][[i][i[s]]]", type_tag)

    def test_raises_on_empty_path(self):
        with self.assertRaises(osc_columns.BuildError):
            osc_columns.build_message(
                "/x", [osc_columns.ArgumentColumn((), "i", 1, 0, 4)]
            )

    def test_raises_on_unsupported_type(self):
        with self.assertRaises(osc_columns.BuildError):
            osc_columns.build_message(
                "/x", [osc_columns.ArgumentColumn((0,), "z", 1, 0, 4)]
            )


if __name__ == "__main__":
    unittest.main()
