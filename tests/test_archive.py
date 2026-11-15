from unittest import TestCase

import hl7
from hl7 import archive
from hl7.archive import (
    MOTHER_CONTROL_ID_ATTR,
    MOTHER_LOCATION_ATTR,
    SegmentGroupRule,
    archive_message,
)


def _multi_order_message():
    # OML laboratory order carrying two independent orders.
    return (
        "\r".join(
            [
                "MSH|^~\\&|SENDING|SENDFAC|RECV|RECVFAC|20261116100000||OML^O21^OML_O21|MCTRL-001|P|2.5.1",
                "PID|1||PAT-123^^^MR||DOE^JOHN||19800101|M",
                "PV1|1|O",
                "ORC|NW|ORD-1001",
                "OBR|1||ORD-1001|GLU^Glucose",
                "OBX|1|ST|GLU^Glucose||100",
                "NTE|1||note one",
                "ZPD|unknown custom segment",
                "ORC|NW|ORD-1002",
                "OBR|1||ORD-1002|HGB^Hemoglobin",
                "OBX|1|ST|HGB^Hemoglobin||13",
                "NTE|1||note two",
            ]
        )
        + "\r"
    )


def _rule():
    return SegmentGroupRule(
        trigger="ORC",
        group_segments=["OBR", "OBX"],
    )


class ArchiveMessageTest(TestCase):
    def setUp(self):
        self.message = hl7.parse(_multi_order_message())
        self.result = archive_message(
            self.message, _rule(), mother_location="archive/2026/MCTRL-001.hl7"
        )

    def test_one_child_per_order(self):
        self.assertEqual(len(self.result), 2)
        self.assertEqual([o.order_index for o in self.result], [0, 1])
        self.assertEqual([o.mother_segment_index for o in self.result], [3, 8])

    def test_child_contains_only_its_own_order(self):
        first, second = self.result.orders
        self.assertEqual(
            [str(s[0][0]) for s in first.segments],
            ["ORC", "OBR", "OBX", "NTE"],
        )
        self.assertEqual(str(first.message.segment("ORC")(2)), "ORD-1001")
        self.assertEqual(str(second.message.segment("ORC")(2)), "ORD-1002")
        # The second order must not leak into the first child.
        self.assertNotIn("ORD-1002", str(first.message))
        self.assertNotIn("ORD-1001", str(second.message))

    def test_child_carries_patient_visit_and_public_prefix(self):
        for order in self.result.orders:
            child = order.message
            self.assertEqual([str(s[0][0]) for s in child[:3]], ["MSH", "PID", "PV1"])
            self.assertEqual(str(child.segment("PID")(3)), "PAT-123^^^MR")
            self.assertEqual(str(child.segment("PV1")(2)), "O")

    def test_public_segments_are_copied_not_duplicated(self):
        for order in self.result.orders:
            child = order.message
            # One real event per child: the shared prefix appears exactly once.
            self.assertEqual(len(child.segments("MSH")), 1)
            self.assertEqual(len(child.segments("PID")), 1)
            self.assertEqual(len(child.segments("PV1")), 1)
        first, second = self.result.orders
        # Both children share identical prefix content.
        self.assertEqual(
            str(first.message.segment("PID")), str(second.message.segment("PID"))
        )

    def test_each_child_gets_a_new_control_id(self):
        first, second = self.result.orders
        first_id = str(first.message.segment("MSH")(10))
        second_id = str(second.message.segment("MSH")(10))
        self.assertNotEqual(first_id, "MCTRL-001")
        self.assertNotEqual(second_id, "MCTRL-001")
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(len(first_id), 20)

    def test_mother_control_id_and_location_are_recorded(self):
        for order in self.result.orders:
            self.assertEqual(
                getattr(order.message, MOTHER_LOCATION_ATTR),
                "archive/2026/MCTRL-001.hl7",
            )
            self.assertEqual(
                getattr(order.message, MOTHER_CONTROL_ID_ATTR), "MCTRL-001"
            )

    def test_mother_message_is_not_modified(self):
        self.assertEqual(str(self.message), _multi_order_message())
        self.assertEqual(str(self.message.segment("MSH")(10)), "MCTRL-001")

    def test_children_are_independent_deep_copies(self):
        first, second = self.result.orders
        first.message["PID.3"] = "MUTATED"
        self.assertEqual(str(second.message.segment("PID")(3)), "PAT-123^^^MR")
        self.assertEqual(str(self.message.segment("PID")(3)), "PAT-123^^^MR")

    def test_unknown_z_segment_stays_unassigned(self):
        unassigned = self.result.unassigned_segments
        self.assertEqual(len(unassigned), 1)
        self.assertEqual(str(unassigned[0][0][0]), "ZPD")
        for order in self.result.orders:
            self.assertNotIn("ZPD", str(order.message))

    def test_known_z_segment_is_routed_to_the_order(self):
        rule = SegmentGroupRule(trigger="ORC", group_segments=["OBR", "OBX", "ZCL"])
        message = hl7.parse(
            "\r".join(
                [
                    "MSH|^~\\&|S||R||20261116100000||OML^O21|MCTRL-Z|P|2.5.1",
                    "PID|1||PAT-1",
                    "ORC|NW|ORD-1",
                    "OBR|1||ORD-1|GLU",
                    "ZCL|known custom segment",
                ]
            )
            + "\r"
        )
        result = archive_message(message, rule)
        self.assertEqual(result.unassigned_segments, [])
        self.assertIn("ZCL", str(result.orders[0].message))

    def test_z_segment_before_first_order_is_unassigned(self):
        message = hl7.parse(
            "\r".join(
                [
                    "MSH|^~\\&|S||R||20261116100000||OML^O21|MCTRL-E|P|2.5.1",
                    "PID|1||PAT-1",
                    "ZPD|orphan before orders",
                    "ORC|NW|ORD-1",
                    "OBR|1||ORD-1|GLU",
                ]
            )
            + "\r"
        )
        result = archive_message(message, _rule())
        self.assertEqual(len(result.unassigned_segments), 1)
        self.assertEqual(str(result.unassigned_segments[0][0][0]), "ZPD")
        self.assertNotIn("ZPD", str(result.orders[0].message))

    def test_custom_control_id_factory(self):
        ids = iter(["NEW-1", "NEW-2"])
        result = archive_message(
            self.message, _rule(), control_id_factory=lambda: next(ids)
        )
        self.assertEqual(str(result.orders[0].message.segment("MSH")(10)), "NEW-1")
        self.assertEqual(str(result.orders[1].message.segment("MSH")(10)), "NEW-2")

    def test_obr_trigger_splits_on_obr(self):
        message = hl7.parse(
            "\r".join(
                [
                    "MSH|^~\\&|S||R||20261116100000||ORU^R01|MCTRL-O|P|2.5.1",
                    "PID|1||PAT-1",
                    "OBR|1||ORD-A|GLU",
                    "OBX|1|ST|GLU||1",
                    "OBR|2||ORD-B|HGB",
                    "OBX|1|ST|HGB||2",
                ]
            )
            + "\r"
        )
        rule = SegmentGroupRule(trigger="OBR", group_segments=["OBX"])
        result = archive_message(message, rule)
        self.assertEqual(len(result), 2)
        self.assertEqual([o.mother_segment_index for o in result.orders], [2, 4])
        self.assertIn("ORD-A", str(result.orders[0].message))
        self.assertNotIn("ORD-B", str(result.orders[0].message))

    def test_missing_trigger_raises(self):
        message = hl7.parse(
            "\r".join(
                [
                    "MSH|^~\\&|S||R||20261116100000||ADT^A04|MCTRL-N|P|2.5.1",
                    "PID|1||PAT-1",
                ]
            )
            + "\r"
        )
        with self.assertRaises(ValueError):
            archive_message(message, _rule())

    def test_children_round_trip_through_parser(self):
        for order in self.result.orders:
            reparsed = hl7.parse(str(order.message))
            self.assertEqual(
                str(reparsed.segment("MSH")(10)),
                str(order.message.segment("MSH")(10)),
            )
            self.assertEqual(len(reparsed.segments("ORC")), 1)

    def test_result_is_iterable(self):
        indices = [order.order_index for order in self.result]
        self.assertEqual(indices, [0, 1])

    def test_custom_prefix_segments(self):
        rule = SegmentGroupRule(
            trigger="ORC",
            group_segments=["OBR"],
            prefix_segments=["MSH", "PID"],
        )
        message = hl7.parse(
            "\r".join(
                [
                    "MSH|^~\\&|S||R||20261116100000||OML^O21|MCTRL-C|P|2.5.1",
                    "PID|1||PAT-1",
                    "ORC|NW|ORD-1",
                    "OBR|1||ORD-1|GLU",
                ]
            )
            + "\r"
        )
        result = archive_message(message, rule)
        child = result.orders[0].message
        self.assertEqual([str(s[0][0]) for s in child], ["MSH", "PID", "ORC", "OBR"])

    def test_module_exports(self):
        self.assertIs(hl7.archive_message, archive.archive_message)
        self.assertIs(hl7.SegmentGroupRule, SegmentGroupRule)
