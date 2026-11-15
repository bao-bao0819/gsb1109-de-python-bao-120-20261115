from unittest import TestCase

import hl7
from hl7 import FieldIssue, MissingOccurrenceError

PID = "PID|1||555-44-4444||EVERYWOMAN^EVE"
PID_SAME = "PID|1||555-44-4444||EVERYWOMAN^EVE"
MSH = "MSH|^~\\&|GHH LAB|ELAB-3"


def build(*segments):
    return hl7.parse("\r".join((MSH,) + segments) + "\r")


def parse_segment(text):
    """Parse *text* as a segment attached to a throwaway MSH."""
    return hl7.parse(MSH + "\r" + text + "\r")[1]


class OccurrenceIdentityTest(TestCase):
    def test_stable_when_segment_inserted_before(self):
        msg = build(PID, "OBX|1|SN|1554-5")
        pid = msg.occurrence("PID")
        obx = msg.occurrence("OBX")
        issue = msg.issues.register(pid, field_num=3, description="bad id")

        self.assertEqual(issue.original_path, "PID.3")
        self.assertEqual(issue.current_path, "PID.3")

        msg.insert(1, parse_segment(PID))

        self.assertEqual(issue.occurrence_id, pid.occurrence_id)
        self.assertEqual(issue.current_path, "PID2.3")
        self.assertEqual(issue.original_path, "PID.3")
        self.assertEqual(pid.segment_num, 2)
        self.assertEqual(obx.path(), "OBX")
        self.assertEqual(msg["PID2.3"], "555-44-4444")

    def test_current_path_follows_removal_of_earlier_peer(self):
        msg = build(PID, PID, "OBX|1|SN|1554-5")
        first, second = msg.occurrences("PID")
        issue = msg.issues.register(second, field_num=3)

        self.assertEqual(issue.original_path, "PID2.3")
        self.assertEqual(issue.current_path, "PID2.3")

        removed = msg.remove_segment(first)

        self.assertIs(removed, first.segment)
        self.assertEqual(issue.current_path, "PID.3")
        self.assertIs(issue.segment, second.segment)
        self.assertEqual(issue.resolve(), second.segment)

    def test_deletion_invalidates_and_does_not_rebind(self):
        msg = build(PID, PID, "OBX|1|SN|1554-5")
        first, second = msg.occurrences("PID")
        issue = msg.issues.register(first, field_num=3)

        msg.remove_segment(first)

        self.assertTrue(issue.is_stale)
        self.assertIsNone(issue.current_path)
        self.assertIsNone(issue.current_segment_num)
        self.assertIsNone(issue.current_value())
        with self.assertRaises(MissingOccurrenceError):
            issue.resolve()

        # The surviving segment now occupies the old path, but the issue must
        # not silently attach to it.
        self.assertEqual(msg["PID.3"], "555-44-4444")
        self.assertEqual(msg.issues.for_segment(second.segment), ())

    def test_deleting_lookalike_does_not_revive_stale_issue(self):
        msg = build(PID, PID, "OBX|1|SN|1554-5")
        first, second = msg.occurrences("PID")
        issue = msg.issues.register(first, field_num=5)

        msg.remove_segment(first)
        self.assertIsNone(issue.current_path)

        # Deleting the look-alike that moved into the freed position must not
        # revive the stale issue.
        msg.remove_segment(second.segment)
        self.assertTrue(issue.is_stale)
        self.assertIsNone(issue.current_path)
        self.assertEqual(msg.issues.valid(), ())

    def test_same_name_and_same_content_are_distinct(self):
        msg = build(PID_SAME, PID_SAME)
        first, second = msg.occurrences("PID")

        self.assertEqual(str(first.segment), str(second.segment))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first.occurrence_id, second.occurrence_id)

        first_issue = msg.issues.register(first, field_num=3, description="A")
        second_issue = msg.issues.register(second, field_num=3, description="B")

        self.assertEqual(first_issue.occurrence_id, first.occurrence_id)
        self.assertEqual(second_issue.occurrence_id, second.occurrence_id)
        self.assertEqual(first_issue.current_path, "PID.3")
        self.assertEqual(second_issue.current_path, "PID2.3")
        self.assertEqual(msg.issues.for_segment(first.segment), (first_issue,))

        msg.remove_segment(first)
        self.assertTrue(first_issue.is_stale)
        self.assertFalse(second_issue.is_stale)
        self.assertEqual(second_issue.current_path, "PID.3")

    def test_content_is_never_used_to_match(self):
        msg = build(PID, PID)
        first, second = msg.occurrences("PID")
        issue = msg.issues.register(first, field_num=3)
        msg.remove_segment(first)

        self.assertIsNot(issue.segment, second.segment)
        self.assertIsNone(issue.current_path)
        self.assertTrue(issue.is_stale)

    def test_deleted_segment_cannot_be_registered(self):
        msg = build(PID, PID)
        first = msg.occurrences("PID")[0]
        msg.remove_segment(first)
        with self.assertRaises(MissingOccurrenceError):
            msg.issues.register(first, field_num=3)

    def test_preexisting_issue_not_affected_by_unrelated_insert(self):
        msg = build("EVN|A04|20060705000000", PID)
        evn = msg.occurrence("EVN")
        issue = msg.issues.register(evn, field_num=2)

        msg.insert(1, parse_segment("PID|9||999"))

        self.assertEqual(issue.current_path, "EVN.2")
        self.assertEqual(issue.current_value(), "20060705000000")

    def test_resolution_reads_the_bound_segment(self):
        msg = build(PID, PID)
        first, second = msg.occurrences("PID")
        issue = msg.issues.register(first, field_num=3)

        msg.remove_segment(first)
        second.segment.assign_field("NEW", 3)

        self.assertTrue(issue.is_stale)
        self.assertEqual(msg["PID.3"], "NEW")
        self.assertIsNone(issue.current_value())

    def test_metadata_is_preserved(self):
        msg = build(PID)
        pid = msg.occurrence("PID")
        issue = msg.issues.register(
            pid, field_num=3, description="invalid", code="E01", source="cleaner"
        )
        self.assertEqual(issue.description, "invalid")
        self.assertEqual(issue.metadata["code"], "E01")
        self.assertEqual(issue.as_dict()["valid"], True)

        msg.remove_segment(pid.segment)
        self.assertFalse(issue.as_dict()["valid"])
        self.assertIsNone(issue.as_dict()["current_path"])

    def test_explicit_invalidate(self):
        msg = build(PID)
        issue = msg.issues.register(msg.occurrence("PID"), field_num=3)
        issue.invalidate()
        self.assertTrue(issue.is_stale)
        self.assertIsNone(issue.current_path)
        with self.assertRaises(MissingOccurrenceError):
            issue.resolve()

    def test_registry_valid_orphaned(self):
        msg = build(PID, PID)
        first, second = msg.occurrences("PID")
        first_issue = msg.issues.register(first, field_num=3)
        second_issue = msg.issues.register(second, field_num=3)
        self.assertEqual(msg.issues.valid(), (first_issue, second_issue))
        self.assertEqual(msg.issues.stale(), ())

        msg.remove_segment(first)

        self.assertEqual(msg.issues.valid(), (second_issue,))
        self.assertEqual(msg.issues.stale(), (first_issue,))

    def test_registry_remove_discards_entry(self):
        msg = build(PID)
        issue = msg.issues.register(msg.occurrence("PID"), field_num=3)
        self.assertTrue(msg.issues.remove(issue))
        self.assertFalse(msg.issues.remove(issue))
        self.assertEqual(len(msg.issues), 0)
        self.assertTrue(issue.is_valid)

    def test_segment_occurrence_key(self):
        msg = build(PID, "OBX|1|SN|1554-5")
        pid = msg.occurrence("PID")
        self.assertEqual(msg.occurrence(pid.occurrence_id), pid)
        self.assertEqual(msg.find_occurrence("PID"), pid)
        self.assertIsNone(msg.find_occurrence("PID.3"))
        self.assertEqual(
            msg.issues.register(pid.occurrence_id, field_num=3).current_path,
            "PID.3",
        )

    def test_message_accessor_still_works(self):
        msg = build(PID)
        self.assertEqual(msg["PID.3"], "555-44-4444")
        self.assertEqual(msg["PID"], [msg.occurrence("PID").segment])

    def test_occurrence_equality_is_message_scoped(self):
        msg = build(PID)
        other = build(PID)
        self.assertNotEqual(msg.occurrence("PID"), other.occurrence("PID"))

    def test_issue_construct_directly(self):
        msg = build(PID)
        seg = msg.occurrence("PID").segment
        issue = FieldIssue(msg, seg, field_num=3)
        self.assertIsInstance(issue, FieldIssue)
        self.assertEqual(issue.current_path, "PID.3")

    def test_accessor_key_registration(self):
        msg = build(PID, PID)
        issue = msg.issues.register("PID2.3", description="second pid")
        self.assertEqual(issue.occurrence_id, msg.occurrences("PID")[1].occurrence_id)
        self.assertEqual(issue.original_path, "PID2.3")

    def test_reinserted_segment_stays_stale(self):
        msg = build(PID, PID)
        first, second = msg.occurrences("PID")
        issue = msg.issues.register(first, field_num=3)

        segment = first.segment
        msg.remove_segment(segment)
        msg.insert(1, segment)

        self.assertTrue(issue.is_stale)
        self.assertIsNone(issue.current_path)
        with self.assertRaises(MissingOccurrenceError):
            msg.occurrence(segment)

    def test_list_mutation_retires_occurrences(self):
        msg = build(PID, PID)
        first, second = msg.occurrences("PID")
        issue = msg.issues.register(first, field_num=3)

        msg.pop(1)
        self.assertTrue(issue.is_stale)
        self.assertIs(msg[1], second.segment)
        self.assertEqual(second.path(field_num=3), "PID.3")

    def test_message_remove_is_identity_based(self):
        msg = build(PID_SAME, PID_SAME)
        first, second = msg.occurrences("PID")
        msg.remove(second.segment)
        self.assertIs(msg[1], first.segment)
        self.assertFalse(second.is_valid)
        self.assertTrue(first.is_valid)

    def test_delitem_retires(self):
        msg = build(PID)
        issue = msg.issues.register(msg.occurrence("PID"), field_num=3)
        del msg[1]
        self.assertTrue(issue.is_stale)

    def test_register_issue_shorthand(self):
        msg = build(PID)
        issue = msg.register_issue("PID.3", description="x")
        self.assertEqual(issue.current_path, "PID.3")

    def test_occurrence_aliases(self):
        msg = build(PID)
        occ = msg.occurrence("PID")
        self.assertEqual(occ.uid, occ.occurrence_id)
        self.assertEqual(occ.stable_id, occ.occurrence_id)
        self.assertEqual(occ.identity, occ.occurrence_id)
        self.assertTrue(occ.valid)
        self.assertFalse(occ.deleted)

    def test_relocated_flag(self):
        msg = build(PID, "OBX|1|SN|1554-5")
        issue = msg.issues.register("PID.3")
        self.assertFalse(issue.relocated)
        msg.insert(1, parse_segment(PID))
        self.assertTrue(issue.relocated)
        msg.remove_segment(issue.resolve())
        self.assertFalse(issue.relocated)
