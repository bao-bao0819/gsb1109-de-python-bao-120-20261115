from unittest import TestCase

import hl7
from hl7.archive import (
    ROLE_COMMON,
    ROLE_CONTROL,
    ROLE_ORDER,
    ROLE_UNASSIGNED,
    ArchiveResult,
    OrderArchiver,
    ParentLocation,
    SegmentGroupRule,
    normalize_rules,
    split_orders,
)

MULTI_ORDER = "\r".join(
    [
        "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-0001|P|2.5",
        "PID|1||MRN123^^^HOSP^MR||DOE^JANE||19800101|F",
        "PV1|1|I|WARD^101^A",
        "ORC|RE|ORD-1|FIL-1",
        "OBR|1|ORD-1|FIL-1|1554-5^GLUCOSE",
        "OBX|1|NM|1554-5^GLUCOSE||100|mg/dL",
        "NTE|1||first order note",
        "ORC|RE|ORD-2|FIL-2",
        "OBR|2|ORD-2|FIL-2|2345-7^SODIUM",
        "OBX|1|NM|2345-7^SODIUM||140|mmol/L",
        "ORC|RE|ORD-3|FIL-3",
        "OBR|3|ORD-3|FIL-3|3456-8^POTASSIUM",
        "OBX|1|NM|3456-8^POTASSIUM||4.2|mmol/L\r",
    ]
)

RULES = [SegmentGroupRule("ORC", ["ORC", "OBR", "OBX", "NTE"])]


def segment_ids(message):
    return [segment[0][0] for segment in message]


class NormalizeRulesTest(TestCase):
    def test_single_rule_object(self):
        self.assertEqual(normalize_rules(RULES), RULES)

    def test_trigger_string(self):
        rules = normalize_rules("ORC")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].start, ("ORC",))
        self.assertIsNone(rules[0].members)

    def test_mapping_of_trigger_to_members(self):
        rules = normalize_rules({"ORC": ["ORC", "OBR", "OBX"]})
        self.assertEqual(rules[0].start, ("ORC",))
        self.assertEqual(rules[0].members, ("ORC", "OBR", "OBX"))

    def test_mapping_rule_form(self):
        rules = normalize_rules(
            {"start": "ORC", "members": ["ORC", "OBR"], "name": "orders"}
        )
        self.assertEqual(rules[0].name, "orders")

    def test_start_members_pair(self):
        rules = normalize_rules(("ORC", ["ORC", "OBR"]))
        self.assertEqual(rules[0].start, ("ORC",))
        self.assertEqual(rules[0].members, ("ORC", "OBR"))

    def test_list_of_triggers(self):
        rules = normalize_rules(["ORC", "OBR"])
        self.assertEqual([rule.start for rule in rules], [("ORC",), ("OBR",)])

    def test_rule_requires_start(self):
        with self.assertRaises(ValueError):
            SegmentGroupRule()

    def test_unknown_rule_option(self):
        with self.assertRaises(TypeError):
            SegmentGroupRule("ORC", typo=True)


class SplitOrdersTest(TestCase):
    def setUp(self):
        self.parent = hl7.parse(MULTI_ORDER)
        self.result = split_orders(self.parent, RULES)

    def test_returns_archive_result(self):
        self.assertIsInstance(self.result, ArchiveResult)
        self.assertEqual(len(self.result), 3)
        self.assertTrue(all(isinstance(m, hl7.Message) for m in self.result))

    def test_each_child_is_self_contained(self):
        for index, child in enumerate(self.result, 1):
            self.assertEqual(segment_ids(child)[:3], ["MSH", "PID", "PV1"])
            self.assertEqual(child["MSH.10"], str(child.segment("MSH")[10]))

    def test_each_child_has_its_own_order_segments(self):
        first, second, third = self.result
        self.assertEqual(
            segment_ids(first), ["MSH", "PID", "PV1", "ORC", "OBR", "OBX", "NTE"]
        )
        self.assertEqual(
            segment_ids(second), ["MSH", "PID", "PV1", "ORC", "OBR", "OBX"]
        )
        self.assertEqual(segment_ids(third), ["MSH", "PID", "PV1", "ORC", "OBR", "OBX"])
        self.assertEqual(second["OBX.5"], "140")
        self.assertEqual(third["OBX.5"], "4.2")

    def test_new_control_id_per_child_and_parent_preserved(self):
        control_ids = [child["MSH.10"] for child in self.result]
        self.assertEqual(len(set(control_ids)), 3)
        self.assertNotIn("PARENT-0001", control_ids)
        for child in self.result:
            self.assertEqual(child["MSH.10"], str(child.segment("MSH")[10]))
            self.assertEqual(child.parent_message_control_id, "PARENT-0001")
            self.assertEqual(child.parent_control_id, "PARENT-0001")

    def test_parent_location_saved(self):
        first, second, third = self.result
        self.assertEqual(first.parent_segment_index, 3)
        self.assertEqual(first.parent_segment_number, 4)
        self.assertEqual(first.parent_segment_range, (3, 7))
        self.assertEqual(second.parent_segment_index, 7)
        self.assertEqual(third.parent_segment_index, 10)
        self.assertIsInstance(first.parent_location, ParentLocation)
        self.assertEqual(first.parent_location.message_control_id, "PARENT-0001")
        self.assertEqual(first.parent_location.position, 3)
        self.assertEqual(first.parent_location.end_position, 7)

    def test_parent_segment_indexes_refer_to_original_message(self):
        first = self.result[0]
        original_ids = segment_ids(self.parent)
        self.assertEqual(
            [original_ids[i] for i in first.parent_order_segment_indexes],
            ["ORC", "OBR", "OBX", "NTE"],
        )
        self.assertEqual(
            [original_ids[i] for i in first.parent_common_segment_indexes],
            ["PID", "PV1"],
        )

    def test_common_segments_are_not_separate_events(self):
        self.assertEqual(self.result.event_count, 10)
        # Common prefix is copied into all three children...
        self.assertEqual(self.result.common_occurrences, 6)
        # ...but the result exposes each common segment only once.
        self.assertEqual(
            [segment[0][0] for segment in self.result.common_segments],
            ["PID", "PV1"],
        )
        for child in self.result:
            for segment in child.common_segments:
                self.assertEqual(segment.archive_role, ROLE_COMMON)
                self.assertFalse(segment.is_order_event)
            for segment in child.event_segments:
                self.assertEqual(segment.archive_role, ROLE_ORDER)
                self.assertTrue(segment.is_order_event)

    def test_order_keys(self):
        self.assertEqual(
            [child.order_key for child in self.result], ["ORD-1", "ORD-2", "ORD-3"]
        )
        self.assertEqual(self.result.by_order()["ORD-2"]["OBR.2"], "ORD-2")

    def test_source_tags(self):
        first = self.result[0]
        self.assertEqual(first.control_segments[0].archive_role, ROLE_CONTROL)
        self.assertEqual(first.control_segments[0].source_index, 0)
        self.assertEqual(first.event_segments[0].source_index, 3)

    def test_original_message_is_not_mutated(self):
        self.assertEqual(self.parent["MSH.10"], "PARENT-0001")
        self.assertEqual(len(self.parent), 13)
        self.assertFalse(hasattr(self.parent[1], "archive_role"))

    def test_round_trip_string(self):
        reparsed = hl7.parse(str(self.result[1]))
        self.assertEqual(reparsed["MSH.10"], self.result[1]["MSH.10"])
        self.assertEqual(reparsed["OBR.2"], "ORD-2")

    def test_custom_control_id_factory(self):
        created = []

        def factory(parent_id, order_number):
            value = "NEW-{}-{}".format(parent_id[-2:], order_number)
            created.append(value)
            return value

        result = split_orders(self.parent, RULES, control_id_factory=factory)
        self.assertEqual(
            [child["MSH.10"] for child in result],
            ["NEW-01-1", "NEW-01-2", "NEW-01-3"],
        )
        self.assertEqual(created, ["NEW-01-1", "NEW-01-2", "NEW-01-3"])

    def test_custom_control_id_factory_without_arguments(self):
        result = split_orders(self.parent, RULES, control_id_factory=lambda: "FIXED-ID")
        # All children may use the same caller-provided value.
        self.assertEqual([child["MSH.10"] for child in result], ["FIXED-ID"] * 3)

    def test_archiver_helper(self):
        result = OrderArchiver(self.parent, RULES).split()
        self.assertEqual(len(result), 3)


class SegmentGroupRuleTest(TestCase):
    def test_accepts_all_non_z_by_default(self):
        rule = SegmentGroupRule("ORC")
        self.assertTrue(rule.accepts("OBX"))
        self.assertTrue(rule.accepts("TQ1"))
        self.assertFalse(rule.accepts("ZDS"))

    def test_explicit_z_segments_are_claimed(self):
        rule = SegmentGroupRule("ORC", z_segments=["ZDS"])
        self.assertTrue(rule.accepts("ZDS"))
        self.assertFalse(rule.accepts("ZAB"))
        wildcard = SegmentGroupRule("ORC", z_segments=["Z*"])
        self.assertTrue(wildcard.accepts("ZAB"))

    def test_members_can_explicitly_claim_a_z_segment(self):
        rule = SegmentGroupRule("ORC", ["ORC", "ZDS"])
        self.assertTrue(rule.accepts("ZDS"))
        self.assertFalse(rule.accepts("ZXX"))

    def test_include_z_opts_in(self):
        rule = SegmentGroupRule("ORC", include_z=True)
        self.assertTrue(rule.accepts("ZANY"))


class UnknownZSegmentTest(TestCase):
    def test_unclaimed_z_segment_stays_pending(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-1|P|2.5",
                "PID|1||MRN1",
                "ORC|RE|ORD-1",
                "OBR|1|ORD-1|FIL-1|GLUCOSE",
                "ZDS|site-data-without-owner",
                "ORC|RE|ORD-2",
                "OBR|2|ORD-2|FIL-2|SODIUM",
                "ZXX|trailing-unknown\r",
            ]
        )
        result = split_orders(hl7.parse(message), RULES)
        self.assertEqual(len(result), 2)
        # The Z segment is not attached to either neighboring order.
        for child in result:
            self.assertNotIn("ZDS", segment_ids(child))
            self.assertNotIn("ZXX", segment_ids(child))
        self.assertEqual(
            [segment[0][0] for segment in result.unassigned], ["ZDS", "ZXX"]
        )
        self.assertEqual(
            [segment.source_index for segment in result.unassigned], [4, 7]
        )
        self.assertEqual(
            [segment.reason for segment in result.unassigned],
            ["unknown_z_segment", "unknown_z_segment"],
        )
        for segment in result.unassigned:
            self.assertEqual(segment.archive_role, ROLE_UNASSIGNED)
            self.assertTrue(segment.is_unassigned)
        self.assertTrue(result.has_unassigned)
        self.assertEqual(result.pending, result.unassigned)

    def test_claimed_z_segment_is_attached_to_its_group(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-2|P|2.5",
                "PID|1||MRN1",
                "ORC|RE|ORD-1",
                "ZDS|belongs-to-first",
                "ORC|RE|ORD-2",
                "ZDS|belongs-to-second\r",
            ]
        )
        result = split_orders(
            hl7.parse(message),
            [SegmentGroupRule("ORC", ["ORC", "ZDS"])],
        )
        self.assertEqual(result.unassigned, [])
        self.assertIn("ZDS", segment_ids(result[0]))
        self.assertIn("ZDS", segment_ids(result[1]))
        self.assertEqual(result[0]["ZDS.1"], "belongs-to-first")
        self.assertEqual(result[1]["ZDS.1"], "belongs-to-second")

    def test_unclaimed_z_prefix_is_not_common(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-3|P|2.5",
                "PID|1||MRN1",
                "ZPI|prefix-without-owner",
                "ORC|RE|ORD-1",
                "OBR|1|ORD-1\r",
            ]
        )
        result = split_orders(hl7.parse(message), RULES)
        self.assertNotIn("ZPI", segment_ids(result[0]))
        self.assertEqual([s[0][0] for s in result.unassigned], ["ZPI"])

    def test_explicitly_common_z_segment_is_copied(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-4|P|2.5",
                "PID|1||MRN1",
                "ZPI|facility-context",
                "ORC|RE|ORD-1",
                "ORC|RE|ORD-2\r",
            ]
        )
        result = split_orders(hl7.parse(message), RULES, common_segments=["ZPI"])
        self.assertEqual(result.unassigned, [])
        for child in result:
            self.assertIn("ZPI", segment_ids(child))
            zpi = next(segment for segment in child if segment[0][0] == "ZPI")
            self.assertEqual(zpi.archive_role, ROLE_COMMON)


class CommonSegmentTest(TestCase):
    def test_patient_and_visit_override(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-5|P|2.5",
                "PID|1||MRN1",
                "PV1|1|I|WARD",
                "ORC|RE|ORD-1",
                "ORC|RE|ORD-2\r",
            ]
        )
        result = split_orders(
            hl7.parse(message),
            RULES,
            patient_segments=["PID"],
            visit_segments=[],
        )
        for child in result:
            counts = segment_ids(child).count("PID")
            self.assertEqual(counts, 1)
            self.assertIn("PV1", segment_ids(child))
            self.assertEqual(len(child.common_segments), 2)

    def test_do_not_include_unclassified_prefix(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-6|P|2.5",
                "PID|1||MRN1",
                "ZZZ|mystery",
                "ORC|RE|ORD-1\r",
            ]
        )
        result = split_orders(
            hl7.parse(message),
            RULES,
            patient_segments=[],
            visit_segments=[],
            include_prefix=False,
        )
        self.assertNotIn("ZZZ", segment_ids(result[0]))
        self.assertNotIn("PID", segment_ids(result[0]))
        self.assertEqual([s[0][0] for s in result.unassigned], ["PID", "ZZZ"])

    def test_common_segment_inside_group_is_not_an_event(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-7|P|2.5",
                "ORC|RE|ORD-1",
                "OBX|1|NM|GLUCOSE||100",
                "PID|late-patient-segment",
                "ORC|RE|ORD-2\r",
            ]
        )
        result = split_orders(hl7.parse(message), RULES)
        self.assertEqual(result.event_count, 3)
        for child in result:
            self.assertIn("PID", segment_ids(child))
            self.assertEqual([s[0][0] for s in child.common_segments], ["PID"])

    def test_explicit_rule_membership_beats_default_patient_role(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-9|P|2.5",
                "PID|1||MRN1",
                "ORC|RE|ORD-1",
                "PID|1||ORDER-SPECIFIC",
                "ORC|RE|ORD-2\r",
            ]
        )
        result = split_orders(
            hl7.parse(message),
            [SegmentGroupRule("ORC", ["ORC", "PID"])],
        )
        first, second = result
        # The first PID is still the common patient context in both children.
        self.assertEqual([s[0][0] for s in first.common_segments], ["PID"])
        # The second PID was named by the rule, so it is an order event.
        self.assertEqual(
            [
                str(segment[3])
                for segment in first.event_segments
                if segment[0][0] == "PID"
            ],
            ["ORDER-SPECIFIC"],
        )
        self.assertNotIn(
            "ORDER-SPECIFIC",
            [str(segment[3]) for segment in second if segment[0][0] == "PID"],
        )

    def test_end_segment_terminates_group_and_leaves_remainder_pending(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-10|P|2.5",
                "ORC|RE|ORD-1",
                "OBX|1|NM|GLUCOSE||100",
                "ZEND|stop-here",
                "ORC|RE|ORD-2\r",
            ]
        )
        result = split_orders(
            hl7.parse(message),
            [SegmentGroupRule("ORC", ["ORC", "OBX"], end=["ZEND"])],
        )
        self.assertEqual(segment_ids(result[0]), ["MSH", "ORC", "OBX"])
        self.assertEqual([s[0][0] for s in result.unassigned], ["ZEND"])
        self.assertEqual(result.unassigned[0].reason, "unknown_z_segment")


class NoOrderTest(TestCase):
    def test_message_without_orders_reports_everything_unassigned(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ADT^A08|PARENT-8|P|2.5",
                "PID|1||MRN1",
                "PV1|1|I|WARD",
                "ZZZ|unknown\r",
            ]
        )
        result = split_orders(hl7.parse(message), RULES)
        self.assertEqual(len(result), 0)
        self.assertEqual(
            [segment[0][0] for segment in result.unassigned],
            ["PID", "PV1", "ZZZ"],
        )
        self.assertEqual(
            [segment.reason for segment in result.unassigned],
            ["no_order_group", "no_order_group", "unknown_z_segment"],
        )


class BatchAndFileTest(TestCase):
    def test_batch_messages_are_split_with_message_positions(self):
        message = "\r".join(
            [
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-A|P|2.5",
                "PID|1||MRN1",
                "ORC|RE|A-1",
                "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120001||ORU^R01|PARENT-B|P|2.5",
                "PID|1||MRN2",
                "ORC|RE|B-1\r",
            ]
        )
        batch = hl7.parse_batch(message)
        result = split_orders(batch, RULES)
        self.assertEqual(len(result), 2)
        self.assertEqual(
            [child.parent_message_control_id for child in result],
            ["PARENT-A", "PARENT-B"],
        )
        self.assertEqual([child.parent_message_position for child in result], [0, 1])
        self.assertEqual(result.parent_message_control_ids, ["PARENT-A", "PARENT-B"])
        self.assertEqual([child.order_key for child in result], ["A-1", "B-1"])
        self.assertNotEqual(result[0]["MSH.10"], result[1]["MSH.10"])


class AliasTest(TestCase):
    def test_archive_orders_alias(self):
        result = hl7.archive.archive_orders(hl7.parse(MULTI_ORDER), RULES)
        self.assertEqual(len(result), 3)
