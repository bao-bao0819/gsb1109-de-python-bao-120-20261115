from datetime import datetime, timedelta, timezone
from unittest import TestCase

import hl7

from hl7.identity import (
    DuplicateIdentifierError,
    EncounterAssignmentError,
    Identifier,
    MergeConflictError,
    MergeNotification,
    MergeNotFoundError,
    PatientIdentityRegistry,
    ReversalRecord,
    UnknownIdentifierError,
    identifiers_from_pid,
)
from tests.samples import sample_msh


def dt(hour, day=1):
    return datetime(2026, 1, day, hour, 0, 0)


class IdentifierTest(TestCase):
    def test_scoped_equality(self):
        first = Identifier("HOSP_A", "123", "MR")
        same = Identifier("HOSP_A", "123", "MR")
        other_institution = Identifier("HOSP_B", "123", "MR")
        other_type = Identifier("HOSP_A", "123", "AN")
        self.assertEqual(first, same)
        self.assertEqual(hash(first), hash(same))
        self.assertNotEqual(first, other_institution)
        self.assertNotEqual(first, other_type)

    def test_blank_components_rejected(self):
        with self.assertRaises(ValueError):
            Identifier(" ", "123")
        with self.assertRaises(ValueError):
            Identifier("HOSP_A", "")

    def test_tuple_coercion_and_dict(self):
        identifier = Identifier(*("HOSP_A", "123", "MR"))
        self.assertEqual(identifier, Identifier.from_dict(identifier.to_dict()))


class IdentifiersFromPidTest(TestCase):
    def test_extracts_repetitions(self):
        message = hl7.parse(
            "MSH|^~\\&|APP|FAC\r"
            "PID|||123^^^HOSP_A^MR~123^^^HOSP_B^MR~456^^^HOSP_B^AN\r"
        )
        identifiers = identifiers_from_pid(message.segment("PID"))
        self.assertEqual(
            identifiers,
            [
                Identifier("HOSP_A", "123", "MR"),
                Identifier("HOSP_B", "123", "MR"),
                Identifier("HOSP_B", "456", "AN"),
            ],
        )

    def test_skips_repetitions_without_authority_or_value(self):
        message = hl7.parse("MSH|^~\\&|APP|FAC\rPID|||123^^^HOSP_A^MR~789^^^^AN\r")
        identifiers = identifiers_from_pid(message.segment("PID"))
        self.assertEqual(identifiers, [Identifier("HOSP_A", "123", "MR")])

    def test_sample_message(self):
        message = hl7.parse(sample_msh)
        identifiers = identifiers_from_pid(message.segment("PID"))
        self.assertIn(Identifier("MSH_MRN", "2148790", "MR"), identifiers)
        self.assertIn(Identifier("MSH_EMPI", "162840", "CM"), identifiers)


class RegistryTest(TestCase):
    def setUp(self):
        self.registry = PatientIdentityRegistry()
        self.a = Identifier("HOSP_A", "111", "MR")
        self.b = Identifier("HOSP_B", "222", "MR")
        self.c = Identifier("HOSP_C", "333", "MR")
        self.registry.register_identifiers([self.a, self.b, self.c])

    def test_same_number_different_institutions_not_auto_merged(self):
        shadow = Identifier("HOSP_B", "111", "MR")
        self.registry.register_identifier(shadow)
        self.assertFalse(self.registry.is_merged(self.a, shadow))
        self.assertEqual(self.registry.patient_identifiers(self.a), frozenset({self.a}))
        self.assertEqual(self.registry.patient_identifiers(shadow), frozenset({shadow}))

    def test_unknown_identifier_raises(self):
        with self.assertRaises(UnknownIdentifierError):
            self.registry.component_of(Identifier("HOSP_X", "999"))

    def test_merge_only_through_notification(self):
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        self.assertTrue(self.registry.is_merged(self.a, self.b))
        self.assertEqual(
            self.registry.patient_identifiers(self.a), frozenset({self.a, self.b})
        )
        self.assertFalse(self.registry.is_merged(self.a, self.c))

    def test_duplicate_notification_rejected(self):
        notification = MergeNotification((self.a, self.b), dt(2), message_id="M1")
        self.registry.apply_merge(notification)
        with self.assertRaises(DuplicateIdentifierError):
            self.registry.apply_merge(notification)

    def test_point_in_time_valid_time(self):
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(5), message_id="M1")
        )
        self.assertFalse(self.registry.is_merged(self.a, self.b, at=dt(4)))
        self.assertTrue(self.registry.is_merged(self.a, self.b, at=dt(5)))
        self.assertEqual(
            self.registry.patient_identifiers(self.a, at=dt(4)), frozenset({self.a})
        )
        self.assertEqual(
            self.registry.active_notifications(at=dt(4)),
            tuple(),
        )
        self.assertEqual(
            self.registry.active_notifications(at=dt(5))[0].message_id, "M1"
        )

    def test_undo_split_restores_components(self):
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        undone = self.registry.undo_merge("M1", undone_at=dt(6), reason="wrong match")
        self.assertEqual(undone.message_id, "M1")
        self.assertFalse(self.registry.is_merged(self.a, self.b))
        self.assertEqual(self.registry.patient_identifiers(self.a), frozenset({self.a}))
        self.assertEqual(self.registry.patient_identifiers(self.b), frozenset({self.b}))
        reversal = self.registry.reversals()[0]
        self.assertIsInstance(reversal, ReversalRecord)
        self.assertEqual(reversal.notification_key, "M1")
        self.assertEqual(reversal.reason, "wrong match")
        kinds = [kind for kind, _ in self.registry.merge_history()]
        self.assertEqual(kinds, ["merge", "reversal"])

    def test_undo_unknown_or_twice_rejected(self):
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        with self.assertRaises(MergeNotFoundError):
            self.registry.undo_merge("nope")
        self.registry.undo_merge("M1", undone_at=dt(6))
        with self.assertRaises(MergeNotFoundError):
            self.registry.undo_merge("M1", undone_at=dt(7))

    def test_undo_leaf_merge_leaves_other_links_intact(self):
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        self.registry.apply_merge(
            MergeNotification((self.b, self.c), dt(3), message_id="M2")
        )
        self.registry.undo_merge("M2", undone_at=dt(5))
        self.assertTrue(self.registry.is_merged(self.a, self.b))
        self.assertFalse(self.registry.is_merged(self.b, self.c))
        self.registry.undo_merge("M1", undone_at=dt(6))
        self.assertFalse(self.registry.is_merged(self.a, self.b))

    def test_undo_blocked_while_redundant_notification_active(self):
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        # A second, independent notification still asserts the same relationship.
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(3), message_id="M2")
        )
        with self.assertRaises(MergeConflictError):
            self.registry.undo_merge("M1", undone_at=dt(4))
        # A refused undo must not mutate state.
        self.assertTrue(self.registry.is_merged(self.a, self.b))
        self.assertEqual(self.registry.reversals(), tuple())

    def test_bitemporal_visibility_of_reversal(self):
        self.registry.apply_merge(
            MergeNotification(
                (self.a, self.b), dt(2), recorded_at=dt(2), message_id="M1"
            )
        )
        self.registry.undo_merge("M1", undone_at=dt(8))
        # Before the reversal was known, the merge was still visible.
        self.assertTrue(self.registry.is_merged(self.a, self.b, as_of=dt(7)))
        self.assertEqual(
            self.registry.active_notifications(as_of=dt(7))[0].message_id, "M1"
        )
        # Current knowledge (no as_of) applies the reversal.
        self.assertFalse(self.registry.is_merged(self.a, self.b))
        self.assertFalse(self.registry.is_merged(self.a, self.b, as_of=dt(9)))
        self.assertEqual(self.registry.active_notifications(as_of=dt(9)), tuple())

    def test_timezone_normalization(self):
        aware = datetime(2026, 1, 1, 3, 0, tzinfo=timezone(timedelta(hours=2)))
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), aware, message_id="M1")
        )
        # 03:00+02:00 == 01:00 UTC; at 00:00 UTC the merge is not yet effective.
        self.assertFalse(
            self.registry.is_merged(self.a, self.b, at=datetime(2026, 1, 1, 0, 0))
        )
        self.assertTrue(
            self.registry.is_merged(self.a, self.b, at=datetime(2026, 1, 1, 1, 0))
        )


class EncounterAttributionTest(TestCase):
    def setUp(self):
        self.registry = PatientIdentityRegistry()
        self.a = Identifier("HOSP_A", "111", "MR")
        self.b = Identifier("HOSP_B", "222", "MR")
        self.registry.register_identifiers([self.a, self.b])

    def test_encounter_anchor_never_rewritten(self):
        self.registry.record_encounter("VISIT-1", patient=self.a, at=dt(1))
        encounter = self.registry.records_for(self.a)[0]
        self.assertEqual(encounter.anchored_to, self.a)

        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        self.assertEqual(
            {record.encounter_id for record in self.registry.records_for(self.b)},
            {"VISIT-1"},
        )

        self.registry.undo_merge("M1", undone_at=dt(5))
        self.assertEqual(self.registry.records_for(self.b), [])
        remaining = self.registry.records_for(self.a)
        self.assertEqual([record.encounter_id for record in remaining], ["VISIT-1"])
        self.assertEqual(remaining[0].anchored_to, self.a)

    def test_historical_query_uses_then_visible_links(self):
        self.registry.record_encounter("VISIT-1", patient=self.a, at=dt(1))
        # Before the merge, the encounter could not be seen through B.
        self.assertEqual(self.registry.records_for(self.b, at=dt(1)), [])
        self.assertEqual(
            [r.encounter_id for r in self.registry.records_for(self.a, at=dt(1))],
            ["VISIT-1"],
        )
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        self.assertEqual(
            [r.encounter_id for r in self.registry.records_for(self.b, at=dt(3))],
            ["VISIT-1"],
        )

    def test_cannot_attach_encounter_without_evidence(self):
        # A and B are distinct at dt(1); claiming both on one encounter fails.
        with self.assertRaises(EncounterAssignmentError):
            self.registry.record_encounter(
                "VISIT-2", patient=self.a, at=dt(1), identifiers=[self.a, self.b]
            )
        with self.assertRaises(EncounterAssignmentError):
            self.registry.record_encounter(
                "VISIT-3",
                patient=self.a,
                at=dt(1),
                identifiers=[Identifier("HOSP_X", "999")],
            )
        # Once an explicit merge exists, the same attribution is supported.
        self.registry.apply_merge(
            MergeNotification((self.a, self.b), dt(2), message_id="M1")
        )
        record = self.registry.record_encounter(
            "VISIT-2", patient=self.a, at=dt(3), identifiers=[self.a, self.b]
        )
        self.assertEqual(record.anchored_to, self.a)

    def test_duplicate_encounter_rejected(self):
        self.registry.record_encounter("VISIT-1", patient=self.a, at=dt(1))
        with self.assertRaises(DuplicateIdentifierError):
            self.registry.record_encounter("VISIT-1", patient=self.a, at=dt(2))


class SerializationTest(TestCase):
    def test_roundtrip_preserves_audit_state(self):
        registry = PatientIdentityRegistry()
        a = Identifier("HOSP_A", "111", "MR")
        b = Identifier("HOSP_B", "222", "MR")
        registry.register_identifiers([a, b])
        registry.apply_merge(MergeNotification((a, b), dt(2), message_id="M1"))
        registry.record_encounter("VISIT-1", patient=a, at=dt(1))
        registry.undo_merge("M1", undone_at=dt(6), reason="bad merge")

        restored = PatientIdentityRegistry.from_dict(registry.to_dict())
        self.assertFalse(restored.is_merged(a, b))
        self.assertTrue(restored.is_merged(a, b, as_of=dt(5)))
        self.assertEqual(
            [r.encounter_id for r in restored.records_for(a)], ["VISIT-1"]
        )
        self.assertEqual(restored.records_for(b), [])
        self.assertEqual(restored.reversals()[0].reason, "bad merge")


class EndToEndMessageTest(TestCase):
    def test_message_identifiers_require_explicit_merge(self):
        first = hl7.parse(
            "MSH|^~\\&|APP|FAC\rPID|||123^^^HOSP_A^MR\r"
        )
        second = hl7.parse(
            "MSH|^~\\&|APP|FAC\rPID|||123^^^HOSP_B^MR\r"
        )
        left = identifiers_from_pid(first.segment("PID"))[0]
        right = identifiers_from_pid(second.segment("PID"))[0]

        registry = PatientIdentityRegistry()
        registry.register_identifiers([left, right])
        self.assertFalse(registry.is_merged(left, right))

        registry.apply_merge(
            MergeNotification((left, right), dt(2), message_id="N-1")
        )
        self.assertTrue(registry.is_merged(left, right))
        registry.undo_merge("N-1", undone_at=dt(4))
        self.assertFalse(registry.is_merged(left, right))
