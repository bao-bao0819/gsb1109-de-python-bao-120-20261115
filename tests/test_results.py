from unittest import TestCase

import hl7
from hl7 import (
    DuplicateResultError,
    InvalidResultError,
    ResultChain,
    ResultError,
    ResultRegistry,
    ResultStatus as S,
    ResultVersion,
    UnknownTargetError,
)


class ResultLifecycleTest(TestCase):
    def setUp(self):
        self.registry = ResultRegistry()

    def add(self, version_id, status, value, replaces=None, received_at=None):
        return self.registry.add(
            version_id=version_id,
            status=status,
            value=value,
            order_id="ORD-1",
            observation_id="GLU",
            replaces_id=replaces,
            received_at=received_at,
        )

    def test_full_lifecycle_keeps_queryable_history(self):
        self.add("v1", S.PRELIMINARY, 180)
        final = self.add("v2", S.FINAL, 182)
        correction = self.add("v3", S.CORRECTION, 175, replaces="v2")
        self.add("v4", S.CANCELLED, None, replaces="v3")

        chain = self.registry.chain("ORD-1", "GLU")
        self.assertEqual(
            [version.version_id for version in chain.history()],
            ["v1", "v2", "v3", "v4"],
        )
        self.assertEqual(final.replaces_id, "v1")
        self.assertEqual(correction.replaces_id, "v2")
        self.assertTrue(chain.is_cancelled)
        self.assertEqual(chain.cancelled_by, "v4")
        self.assertEqual(chain.effective_versions(), ())
        self.assertIsNone(chain.effective)

    def test_explicit_source_relationship_is_honoured(self):
        self.add("v1", S.FINAL, 182)
        correction = self.add("v2", S.CORRECTION, 175, replaces="v1")
        lineage = self.registry.chain("ORD-1", "GLU").lineage("v2")
        self.assertEqual([v.version_id for v in lineage], ["v2", "v1"])
        self.assertEqual(correction.replaces_id, "v1")

    def test_effective_tracks_latest_head(self):
        self.add("v1", S.PRELIMINARY, 180)
        self.assertEqual(self.registry.effective("ORD-1", "GLU").value, 180)
        self.add("v2", S.FINAL, 182)
        self.assertEqual(self.registry.effective("ORD-1", "GLU").value, 182)
        self.add("v3", S.CORRECTION, 175, replaces="v2")
        self.assertEqual(self.registry.effective("ORD-1", "GLU").value, 175)

    def test_final_without_predecessor_starts_new_branch(self):
        final = self.add("v1", S.FINAL, 182)
        self.assertIsNone(final.replaces_id)
        self.assertIsNone(final.declared_replaces_id)
        chain = self.registry.chain("ORD-1", "GLU")
        self.assertEqual(chain.heads, ("v1",))

    def test_implicit_and_declared_predecessor_are_distinguished(self):
        self.add("v1", S.PRELIMINARY, 180)
        final = self.add("v2", S.FINAL, 182)
        self.assertIsNone(final.declared_replaces_id)
        self.assertEqual(final.replaces_id, "v1")

        explicit = self.add(
            "v3", S.FINAL, 184, replaces="v2"
        )
        self.assertEqual(explicit.declared_replaces_id, "v2")
        self.assertEqual(explicit.replaces_id, "v2")


class DuplicateDeliveryTest(TestCase):
    def setUp(self):
        self.registry = ResultRegistry()

    def add(self, version_id, status, value, replaces=None, received_at=None):
        return self.registry.add(
            version_id,
            status,
            value,
            "ORD-1",
            "GLU",
            replaces_id=replaces,
            received_at=received_at,
        )

    def test_duplicate_delivery_does_not_grow_history(self):
        first = self.add("v1", S.FINAL, 182, received_at=1)
        redelivered = self.add("v1", S.FINAL, 182, received_at=2)
        self.assertIs(first, redelivered)
        self.assertEqual(
            len(self.registry.history("ORD-1", "GLU")), 1
        )

    def test_duplicate_after_later_versions_stays_idempotent(self):
        prelim = self.add("v1", S.PRELIMINARY, 180)
        final = self.add("v2", S.FINAL, 182)
        self.assertEqual(final.replaces_id, "v1")
        self.add("v3", S.CORRECTION, 175, replaces="v2")

        redelivered_prelim = self.add("v1", S.PRELIMINARY, 180)
        redelivered_final = self.add("v2", S.FINAL, 182)
        self.assertIs(redelivered_prelim, prelim)
        self.assertIs(redelivered_final, final)
        self.assertEqual(
            [v.version_id for v in self.registry.history("ORD-1", "GLU")],
            ["v1", "v2", "v3"],
        )

    def test_reused_id_with_different_content_is_rejected(self):
        self.add("v1", S.FINAL, 182)
        self.assertRaises(
            DuplicateResultError, self.add, "v1", S.FINAL, 999
        )

    def test_old_original_cannot_resurrect_cancelled_chain(self):
        self.add("v1", S.FINAL, 182)
        self.add("v9", S.CANCELLED, None, replaces="v1")
        self.assertTrue(self.registry.chain("ORD-1", "GLU").is_cancelled)

        replayed = self.add("v1", S.FINAL, 182)
        self.assertEqual(replayed.version_id, "v1")
        chain = self.registry.chain("ORD-1", "GLU")
        self.assertEqual(len(chain.history()), 2)
        self.assertTrue(chain.is_cancelled)
        self.assertIsNone(self.registry.effective("ORD-1", "GLU"))

    def test_new_explicit_report_restores_cancelled_chain(self):
        self.add("v1", S.FINAL, 182)
        self.add("v9", S.CANCELLED, None, replaces="v1")
        self.add("v10", S.FINAL, 190)
        chain = self.registry.chain("ORD-1", "GLU")
        self.assertEqual(len(chain.history()), 3)
        self.assertFalse(chain.is_cancelled)
        self.assertEqual(chain.effective.version_id, "v10")
        self.assertEqual(chain.effective.value, 190)


class ConflictTest(TestCase):
    def setUp(self):
        self.registry = ResultRegistry()

    def add(self, version_id, status, value, replaces=None):
        return self.registry.add(
            version_id, status, value, "ORD-1", "GLU", replaces_id=replaces
        )

    def test_two_unsortable_corrections_are_both_retained(self):
        self.add("v1", S.FINAL, 182)
        first = self.add("v2", S.CORRECTION, 175, replaces="v1")
        second = self.add("v3", S.CORRECTION, 170, replaces="v1")

        chain = self.registry.chain("ORD-1", "GLU")
        self.assertEqual(
            [v.version_id for v in chain.history()], ["v1", "v2", "v3"]
        )
        self.assertEqual(chain.heads, ("v2", "v3"))
        self.assertTrue(chain.has_conflict)
        self.assertIsNone(chain.effective)
        self.assertEqual(
            {v.version_id for v in chain.effective_versions()},
            {first.version_id, second.version_id},
        )

    def test_conflict_persists_while_branches_keep_growing(self):
        self.add("v1", S.FINAL, 182)
        self.add("v2", S.CORRECTION, 175, replaces="v1")
        self.add("v3", S.CORRECTION, 170, replaces="v1")
        self.add("v4", S.CORRECTION, 168, replaces="v3")
        self.add("v5", S.CORRECTION, 165, replaces="v2")

        chain = self.registry.chain("ORD-1", "GLU")
        self.assertEqual(chain.heads, ("v4", "v5"))
        self.assertTrue(chain.has_conflict)
        self.assertIsNone(chain.effective)
        self.assertEqual(
            [v.version_id for v in chain.lineage("v5")],
            ["v5", "v2", "v1"],
        )

    def test_cancelling_one_conflicting_branch_leaves_other_effective(self):
        self.add("v1", S.FINAL, 182)
        self.add("v2", S.CORRECTION, 175, replaces="v1")
        self.add("v3", S.CORRECTION, 170, replaces="v1")
        self.add("x", S.CANCELLED, None, replaces="v2")

        chain = self.registry.chain("ORD-1", "GLU")
        self.assertFalse(chain.is_cancelled)
        self.assertFalse(chain.has_conflict)
        self.assertEqual(chain.effective.version_id, "v3")
        self.assertEqual(chain.cancelled_heads, ("x",))


class ObservationScopeTest(TestCase):
    def test_observation_id_is_scoped_per_order(self):
        registry = ResultRegistry()
        registry.add("a1", S.FINAL, 182, "ORD-A", "GLU")
        registry.add("b1", S.FINAL, 99, "ORD-B", "GLU")

        self.assertEqual(registry.effective("ORD-A", "GLU").value, 182)
        self.assertEqual(registry.effective("ORD-B", "GLU").value, 99)
        chains = {(c.order_id, c.observation_id): c for c in registry.chains()}
        self.assertEqual(set(chains), {("ORD-A", "GLU"), ("ORD-B", "GLU")})

    def test_same_version_id_in_different_orders_are_independent(self):
        registry = ResultRegistry()
        registry.add("v1", S.FINAL, 182, "ORD-A", "GLU")
        registry.add("v1", S.FINAL, 99, "ORD-B", "GLU")
        self.assertEqual(
            registry.chain("ORD-A", "GLU").get("v1").value, 182
        )
        self.assertEqual(
            registry.chain("ORD-B", "GLU").get("v1").value, 99
        )


class ValidationTest(TestCase):
    def setUp(self):
        self.registry = ResultRegistry()

    def test_correction_requires_predecessor(self):
        self.assertRaises(
            InvalidResultError,
            self.registry.add,
            "v2",
            S.CORRECTION,
            175,
            "ORD-1",
            "GLU",
        )

    def test_cancellation_requires_predecessor(self):
        self.assertRaises(
            InvalidResultError,
            self.registry.add,
            "v9",
            S.CANCELLED,
            None,
            "ORD-1",
            "GLU",
        )

    def test_unknown_predecessor_is_rejected(self):
        self.assertRaises(
            UnknownTargetError,
            self.registry.add,
            "v2",
            S.CORRECTION,
            175,
            "ORD-1",
            "GLU",
            replaces_id="missing",
        )

    def test_mandatory_identifiers(self):
        self.assertRaises(
            InvalidResultError,
            self.registry.add,
            "",
            S.FINAL,
            1,
            "ORD-1",
            "GLU",
        )
        self.assertRaises(
            InvalidResultError,
            self.registry.add,
            "v1",
            S.FINAL,
            1,
            "",
            "GLU",
        )
        self.assertRaises(
            InvalidResultError,
            self.registry.add,
            "v1",
            S.FINAL,
            1,
            "ORD-1",
            "",
        )
        self.assertRaises(
            InvalidResultError,
            self.registry.add,
            "v1",
            "FINAL",
            1,
            "ORD-1",
            "GLU",
        )

    def test_errors_are_hl7_exceptions(self):
        self.assertTrue(issubclass(ResultError, hl7.HL7Exception))


class ChainQueryTest(TestCase):
    def setUp(self):
        self.registry = ResultRegistry()
        self.registry.add("v1", S.PRELIMINARY, 180, "ORD-1", "GLU")
        self.registry.add(
            "v2", S.CORRECTION, 175, "ORD-1", "GLU", replaces_id="v1"
        )

    def test_get_and_contains(self):
        chain = self.registry.chain("ORD-1", "GLU")
        self.assertTrue(chain.contains("v1"))
        self.assertFalse(chain.contains("nope"))
        self.assertEqual(chain.get("v2").value, 175)
        self.assertRaises(KeyError, chain.get, "nope")

    def test_lineage_walks_ancestors(self):
        chain = self.registry.chain("ORD-1", "GLU")
        lineage = chain.lineage("v2")
        self.assertEqual([v.version_id for v in lineage], ["v2", "v1"])
        self.assertRaises(KeyError, chain.lineage, "nope")

    def test_lineage_detects_cycle(self):
        cyclic = ResultChain(
            order_id="ORD-1",
            observation_id="GLU",
            versions=(
                ResultVersion("a", S.CORRECTION, 1, "ORD-1", "GLU", "b"),
                ResultVersion("b", S.CORRECTION, 2, "ORD-1", "GLU", "a"),
            ),
            heads=("a", "b"),
            cancelled_heads=(),
        )
        self.assertRaises(ResultError, cyclic.lineage, "a")

    def test_empty_chain(self):
        chain = self.registry.chain("ORD-X", "GLU")
        self.assertIsInstance(chain, ResultChain)
        self.assertEqual(chain.history(), ())
        self.assertEqual(chain.effective_versions(), ())
        self.assertIsNone(chain.effective)
        self.assertFalse(chain.is_cancelled)
        self.assertFalse(chain.has_conflict)
        self.assertIsNone(chain.cancelled_by)
