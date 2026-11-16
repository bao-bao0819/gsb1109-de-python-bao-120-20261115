from unittest import TestCase

import hl7
from hl7 import FieldPolicy, PatientRecord, RecordPolicy
from hl7.record import NULL_VALUE


def message(*segments, timestamp="20240101080000", control_id="MSG1"):
    msh = "MSH|^~\\&|HIS|HOSP|LAB|LABF|{0}||ADT^A08|{1}|P|2.5".format(
        timestamp, control_id
    )
    return "\r".join((msh,) + segments)


BASE = message(
    "PID|1||P123^^^MRN~ALT99^^^OLD||SMITH^JOHN^Q|19800101|M|||1 MAIN ST^^BOSTON^MA",
    "PV1|1|I|WARD1^ROOM1",
    timestamp="20240101080000",
    control_id="BASE",
)


class FieldPolicyTest(TestCase):
    def test_defaults(self):
        policy = FieldPolicy()
        self.assertEqual(hl7.PRESERVE, policy.absent)
        self.assertEqual(hl7.PRESERVE, policy.empty)
        self.assertEqual(hl7.CLEAR, policy.null)

    def test_actions(self):
        policy = FieldPolicy(absent="clear", empty="preserve", null="clear")
        self.assertEqual(hl7.CLEAR, policy.action_for(None))
        self.assertEqual(hl7.PRESERVE, policy.action_for(""))
        self.assertEqual(hl7.CLEAR, policy.action_for(NULL_VALUE))
        self.assertEqual("set", policy.action_for("value"))

    def test_aliases(self):
        policy = FieldPolicy(absent="keep", empty="unchanged", null="delete")
        self.assertEqual(hl7.PRESERVE, policy.absent)
        self.assertEqual(hl7.PRESERVE, policy.empty)
        self.assertEqual(hl7.CLEAR, policy.null)

    def test_invalid_action(self):
        with self.assertRaises(ValueError):
            FieldPolicy(absent="explode")

    def test_equality(self):
        self.assertEqual(FieldPolicy(), FieldPolicy())
        self.assertNotEqual(FieldPolicy(), FieldPolicy(empty="clear"))

    def test_record_policy_lookup(self):
        policy = RecordPolicy(
            default=FieldPolicy(empty="preserve"),
            fields={"PID.3": FieldPolicy(empty="clear")},
        )
        self.assertEqual(
            hl7.CLEAR, policy.for_accessor(hl7.Accessor("PID", 1, 3, 1)).empty
        )
        self.assertEqual(
            hl7.PRESERVE, policy.for_accessor(hl7.Accessor("PID", 1, 5, 1)).empty
        )

    def test_record_policy_with_field(self):
        policy = RecordPolicy()
        updated = policy.with_field("PID.3", FieldPolicy(null="preserve"))
        self.assertIs(policy, policy)
        self.assertEqual(hl7.CLEAR, policy.default.null)
        self.assertEqual(hl7.PRESERVE, updated.fields["PID.3"].null)


class MergeTest(TestCase):
    def test_absent_field_is_unchanged(self):
        record = PatientRecord(BASE)
        changes = record.apply(
            message(
                "PID|1||P123^^^MRN~ALT99^^^OLD||SMITH^JOHN^Q",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("1 MAIN ST^^BOSTON^MA", record.value("PID.10"))
        self.assertEqual("WARD1^ROOM1", record.value("PV1.3"))
        self.assertFalse([c for c in changes if c.path.startswith("PID.10.")])

    def test_empty_field_is_unchanged(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN~ALT99^^^OLD||SMITH^JOHN^Q|||",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("19800101", record.value("PID.6"))
        self.assertEqual("M", record.value("PID.7"))

    def test_null_clears_field(self):
        record = PatientRecord(BASE)
        changes = record.apply(
            message(
                'PID|1||P123^^^MRN~ALT99^^^OLD||SMITH^JOHN^Q|""||||""',
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("", record.value("PID.6"))
        self.assertEqual("", record.value("PID.10"))
        cleared = {c.path for c in changes if c.operation == "clear"}
        self.assertEqual({"PID.6.1", "PID.10.1"}, cleared)

    def test_clear_one_repetition_keeps_siblings(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                'PID|1||P123^^^MRN~""~X9^^^NEW||SMITH^JOHN^Q',
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("P123^^^MRN", record.value("PID.3.1"))
        self.assertEqual("", record.value("PID.3.2"))
        self.assertEqual("X9^^^NEW", record.value("PID.3.3"))

    def test_clear_one_repetition_does_not_delete_others(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                'PID|1||P123^^^MRN~""~X9^^^NEW||SMITH^JOHN^Q',
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        segment = record.segment("PID")
        values = [str(value) for value in segment[3]]
        # The cleared repetition keeps its position as an explicit HL7 null,
        # while the surrounding repetitions are untouched.
        self.assertEqual(["P123^^^MRN", '""', "X9^^^NEW"], values)
        self.assertEqual("P123^^^MRN", record.value("PID.3.1"))
        self.assertEqual("X9^^^NEW", record.value("PID.3.3"))

    def test_lone_null_clears_whole_repeating_field(self):
        record = PatientRecord(BASE)
        changes = record.apply(
            message(
                'PID|1||""||SMITH^JOHN^Q',
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("", record.value("PID.3"))
        self.assertEqual(
            {"PID.3.1", "PID.3.2"},
            {c.path for c in changes if c.path.startswith("PID.3.")},
        )
        self.assertEqual(
            'PID|1||""||SMITH^JOHN^Q|19800101|M|||1 MAIN ST^^BOSTON^MA',
            str(record.segment("PID")),
        )

    def test_single_value_update_targets_first_repetition(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P999^^^NEW||SMITH^JOHN^Q",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("P999^^^NEW", record.value("PID.3.1"))
        self.assertEqual("ALT99^^^OLD", record.value("PID.3.2"))

    def test_stale_repetition_cannot_restore_cleared_value(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                'PID|1||P123^^^MRN~""~X9^^^NEW||SMITH^JOHN^Q',
                timestamp="20240103090000",
                control_id="NEW",
            )
        )
        record.apply(
            message(
                "PID|||P123^^^MRN~ALT99^^^OLD||SMITH^JOHN^Q",
                timestamp="20240102090000",
                control_id="OLD",
            )
        )
        self.assertEqual("P123^^^MRN", record.value("PID.3.1"))
        self.assertEqual("", record.value("PID.3.2"))
        self.assertEqual("X9^^^NEW", record.value("PID.3.3"))

    def test_late_message_does_not_replace_new_value(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^JANE^Q",
                timestamp="20240105090000",
                control_id="NEW",
            )
        )
        changes = record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^LATE^Q",
                timestamp="20240102090000",
                control_id="OLD",
            )
        )
        self.assertEqual([], list(changes))
        self.assertEqual("SMITH^JANE^Q", record.value("PID.5"))
        self.assertTrue(record.ignored_changes)

    def test_newer_message_wins(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^JANE^Q",
                timestamp="20240105090000",
                control_id="NEW",
            )
        )
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^LATER^Q",
                timestamp="20240106090000",
                control_id="LATER",
            )
        )
        self.assertEqual("SMITH^LATER^Q", record.value("PID.5"))

    def test_version_override_orders_unstamped_messages(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^NEW^Q",
                timestamp="",
                control_id="N",
            ),
            version="20240110000000",
        )
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^OLD^Q",
                timestamp="",
                control_id="O",
            ),
            version="20240101000000",
        )
        self.assertEqual("SMITH^NEW^Q", record.value("PID.5"))

    def test_sequence_breaks_timestamp_ties(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^SECOND^Q",
                timestamp="20240102090000",
                control_id="SECOND",
            )
        )
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^FIRST^Q",
                timestamp="20240102090000",
                control_id="FIRST",
            )
        )
        self.assertEqual("SMITH^FIRST^Q", record.value("PID.5"))


class ProvenanceTest(TestCase):
    def test_change_records_source_location(self):
        record = PatientRecord(BASE)
        changes = record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^JANE^Q",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        change = next(c for c in changes if c.path == "PID.5.1")
        self.assertEqual("UPD", change.location.message_control_id)
        self.assertEqual(2, change.location.message_index)
        self.assertEqual("PID", change.location.segment)
        self.assertEqual(5, change.location.field_num)
        self.assertEqual(1, change.location.repeat_num)
        self.assertEqual("SMITH^JOHN^Q", change.old_value)
        self.assertEqual("SMITH^JANE^Q", change.new_value)

    def test_provenance_returns_current_value_location(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^JANE^Q",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        location = record.provenance("PID.5")
        self.assertEqual("UPD", location.message_control_id)
        self.assertEqual("PID.5.1", location.key)

    def test_history_for_filters_by_path(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN~ALT99^^^OLD||SMITH^JANE^Q",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        history = record.history_for("PID.5")
        self.assertTrue(history)
        self.assertTrue(all(c.path.startswith("PID.5") for c in history))

    def test_initial_changes_are_separate(self):
        record = PatientRecord(BASE)
        self.assertEqual([], list(record.changes))
        self.assertTrue(record.initial_changes)

    def test_change_as_dict(self):
        record = PatientRecord(BASE)
        changes = record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^JANE^Q",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        payload = changes[0].as_dict()
        self.assertIn("location", payload)
        self.assertIn("message_control_id", payload["location"])
        self.assertIn("path", payload)

    def test_ignored_change_records_source(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^JANE^Q",
                timestamp="20240105090000",
                control_id="NEW",
            )
        )
        record.apply(
            message(
                "PID|1||P123^^^MRN||SMITH^LATE^Q",
                timestamp="20240102090000",
                control_id="OLD",
            )
        )
        ignored = record.ignored_changes[0]
        self.assertEqual("OLD", ignored.location.message_control_id)


class RecordApiTest(TestCase):
    def test_value_and_get(self):
        record = PatientRecord(BASE)
        self.assertEqual("P123^^^MRN", record["PID.3"])
        self.assertEqual("P123", record.value("PID.3.1.1"))
        self.assertEqual("missing", record.get("PID.99", default="missing"))

    def test_to_message_round_trip(self):
        record = PatientRecord(BASE)
        record.apply(
            message(
                'PID|1||P123^^^MRN~""~X9^^^NEW||SMITH^JANE^Q',
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        reparsed = hl7.parse(str(record))
        self.assertEqual("P123", reparsed["PID.3.1.1"])
        self.assertEqual("X9", reparsed["PID.3.3.1"])
        self.assertEqual("SMITH^JANE^Q", str(reparsed["PID"][0][5][0]))
        self.assertEqual("WARD1^ROOM1", str(reparsed["PV1"][0][3][0]))

    def test_apply_incremental(self):
        record = hl7.apply_incremental(
            BASE,
            message(
                "PID|1||P123^^^MRN||SMITH^JANE^Q",
                timestamp="20240102090000",
                control_id="UPD",
            ),
        )
        self.assertIsInstance(record, PatientRecord)
        self.assertEqual("SMITH^JANE^Q", record.value("PID.5"))

    def test_merge_messages(self):
        record = hl7.merge_messages(
            [
                BASE,
                message(
                    "PID|1||P123^^^MRN||SMITH^JANE^Q",
                    timestamp="20240102090000",
                    control_id="UPD",
                ),
            ]
        )
        self.assertEqual("SMITH^JANE^Q", record.value("PID.5"))

    def test_policy_per_field(self):
        record = PatientRecord(
            BASE,
            policy=RecordPolicy(fields={"PID.10": FieldPolicy(empty="clear")}),
        )
        record.apply(
            message(
                "PID|1||P123^^^MRN~ALT99^^^OLD||SMITH^JOHN^Q|||||",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("", record.value("PID.10"))
        self.assertEqual("19800101", record.value("PID.6"))

    def test_absent_field_clear_policy(self):
        record = PatientRecord(
            BASE, policy=RecordPolicy(default=FieldPolicy(absent="clear"))
        )
        record.apply(
            message(
                "PID|1||P123^^^MRN",
                timestamp="20240102090000",
                control_id="UPD",
            )
        )
        self.assertEqual("", record.value("PID.5"))
        self.assertEqual("", record.value("PV1.3"))

    def test_batch_application(self):
        record = PatientRecord(BASE)
        batch = "\r".join(
            [
                "BHS|^~\\&|A",
                message(
                    "PID|1||P999^^^MRN",
                    timestamp="20240102090000",
                    control_id="B1",
                ),
                message(
                    "PID|1||P888^^^MRN",
                    timestamp="20240103090000",
                    control_id="B2",
                ),
                "BTS|2",
            ]
        )
        record.apply(batch)
        self.assertEqual("P888^^^MRN", record.value("PID.3"))
        self.assertEqual(["BASE", "B1", "B2"], [m.control_id for m in record.messages])

    def test_nonstandard_separators(self):
        base = (
            "MSH$%~\\&$HIS$HOSP$LAB$LABF$20240101080000$$ADT%A08$M1$P$2.5\r"
            "PID$1$$P1$$SMITH%JOHN\r"
        )
        record = PatientRecord(base)
        self.assertEqual("SMITH%JOHN", record.value("PID.5"))
        self.assertEqual("SMITH", record.value("PID.5.1.1"))
        self.assertEqual("JOHN", record.value("PID.5.1.2"))

    def test_mismatched_separators_rejected(self):
        record = PatientRecord(BASE)
        with self.assertRaises(ValueError):
            record.apply(
                "MSH$%~\\&$HIS$HOSP$LAB$LABF$20240102090000$$ADT%A08$M2$P$2.5\r"
                "PID$1$$P2\r"
            )
