"""Unit tests for ofxtools.lending"""

import datetime
import unittest
from decimal import Decimal

from ofxtools.lending import (
    Borrow,
    CorporateAction,
    Evidence,
    EvidenceError,
    Lend,
    QuantityMismatch,
    Recall,
    Return,
    Split,
    TableError,
    actions_from_statement,
    evidence_from_transaction,
    reconcile,
    reconcile_file,
    reconcile_statements,
    reconciliation_to_dict,
    security_key,
    table_from_dict,
)
from ofxtools.lending import main as lending_main

# test imports
from ofxtools.models.i18n import CURRENCY_CODES  # noqa: F401  (ensures module import)
from ofxtools.models.invest import (
    INVACCTFROM,
    INVSTMTRS,
    INVTRAN,
    INVTRANLIST,
    JRNLSEC,
    SECID,
)
from ofxtools.models.invest import (
    SPLIT as OFXSPLIT,
)

D = Decimal
SEC = "CUSIP:123456789"
OTHER_SEC = "CUSIP:987654321"


def dt(year, month, day):
    return datetime.date(year, month, day)


def make_secid(uniqueid="123456789", uniqueidtype="CUSIP"):
    return SECID(uniqueid=uniqueid, uniqueidtype=uniqueidtype)


class LendTestCase(unittest.TestCase):
    def test_requires_positive_units(self):
        with self.assertRaises(TableError):
            Lend("L1", SEC, "CPT", D("0"), dt(2024, 1, 1))

    def test_requires_loan_id(self):
        with self.assertRaises(TableError):
            Lend("", SEC, "CPT", D("10"), dt(2024, 1, 1))

    def test_accepts_string_dates_and_numbers(self):
        loan = Lend("L1", SEC, "CPT", "100", "20240102")
        self.assertEqual(loan.units, D("100"))
        self.assertEqual(loan.date, dt(2024, 1, 2))


class ReturnTestCase(unittest.TestCase):
    def test_rejects_non_positive(self):
        with self.assertRaises(TableError):
            Return("L1", D("-1"), dt(2024, 1, 2))


class PartialReturnTestCase(unittest.TestCase):
    def setUp(self):
        self.loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-OPEN")

    def test_partial_return_leaves_open_batch(self):
        result = reconcile(
            [self.loan],
            returns=[Return("L1", D("400"), dt(2024, 2, 1), fitid="F-RET1")],
            evidence=[
                Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-RET1", SEC, D("400"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(result.balanced, result)
        self.assertEqual([b.units for b in result.open_batches], [D("600")])
        self.assertEqual(len(result.settled_batches), 1)
        self.assertEqual(result.settled_batches[0].units, D("400"))
        self.assertEqual(result.on_loan("LEND"), {SEC: D("600")})

    def test_multiple_partial_returns_keep_each_batch(self):
        result = reconcile(
            [self.loan],
            returns=[
                Return("L1", D("250"), dt(2024, 2, 1), fitid="F-R1"),
                Return("L1", D("300"), dt(2024, 3, 1), fitid="F-R2"),
            ],
            evidence=[
                Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-R1", SEC, D("250"), dt(2024, 2, 1), "RETURN", "L1"),
                Evidence("F-R2", SEC, D("300"), dt(2024, 3, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(result.balanced, result)
        self.assertEqual(len(result.settled_batches), 2)
        self.assertEqual([b.units for b in result.open_batches], [D("450")])

    def test_over_return_reports_original_records(self):
        result = reconcile(
            [self.loan],
            returns=[Return("L1", D("1200"), dt(2024, 2, 1), fitid="F-RET1")],
            evidence=[
                Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-RET1", SEC, D("1200"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertEqual(result.open_batches, [])
        self.assertTrue(result.mismatches)
        mismatch = result.mismatches[0]
        self.assertIsInstance(mismatch, QuantityMismatch)
        self.assertEqual(mismatch.kind, "CONSERVATION")
        self.assertIn("loan:L1@2024-01-01", mismatch.records)
        self.assertIn("return:F-RET1", mismatch.records)
        self.assertEqual(mismatch.opening, D("1000"))
        self.assertEqual(mismatch.closing, D("-200"))

    def test_open_batch_records_trace_back_to_original(self):
        result = reconcile(
            [self.loan],
            returns=[Return("L1", D("400"), dt(2024, 2, 1), fitid="F-RET1")],
            evidence=[
                Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-RET1", SEC, D("400"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        open_batch = result.open_batches[0]
        self.assertEqual(
            open_batch.records,
            ("loan:L1@2024-01-01", "ofx:F-OPEN", "return:F-RET1"),
        )
        settled = result.settled_batches[0]
        self.assertEqual(
            settled.records,
            ("loan:L1@2024-01-01", "ofx:F-OPEN", "return:F-RET1"),
        )

    def test_two_lend_rows_stay_separate_batches(self):
        first = Lend("L1", SEC, "CPT-A", D("1000"), dt(2024, 1, 1), fitid="F-A")
        second = Lend("L2", SEC, "CPT-B", D("500"), dt(2024, 1, 15), fitid="F-B")
        result = reconcile(
            [first, second],
            returns=[Return("L1", D("1000"), dt(2024, 2, 1), fitid="F-RET")],
            evidence=[
                Evidence("F-A", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-B", SEC, D("500"), dt(2024, 1, 15), "LEND", "L2"),
                Evidence("F-RET", SEC, D("1000"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(result.balanced, result)
        self.assertEqual(
            [(b.loan_id, b.units) for b in result.open_batches],
            [("L2", D("500"))],
        )
        self.assertEqual(result.on_loan("LEND"), {SEC: D("500")})


class RecallTestCase(unittest.TestCase):
    def setUp(self):
        self.loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-OPEN")
        self.evidence = [
            Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1")
        ]

    def test_partial_recall_leaves_remainder(self):
        result = reconcile(
            [self.loan],
            recalls=[Recall("L1", D("600"), dt(2024, 2, 1))],
            evidence=self.evidence,
        )
        self.assertEqual([b.units for b in result.open_batches], [D("400")])
        self.assertEqual(result.settled_batches[0].settlement, "RECALL")
        self.assertEqual(result.settled_batches[0].units, D("600"))

    def test_full_recall_without_units(self):
        result = reconcile(
            [self.loan],
            recalls=[Recall("L1", None, dt(2024, 2, 1))],
            evidence=self.evidence,
        )
        self.assertEqual(result.open_batches, [])
        self.assertEqual(result.settled_batches[0].units, D("1000"))

    def test_recall_exceeding_open_is_flagged(self):
        result = reconcile(
            [self.loan],
            recalls=[Recall("L1", D("1500"), dt(2024, 2, 1))],
            evidence=self.evidence,
        )
        self.assertTrue(result.mismatches)
        self.assertEqual(result.mismatches[0].kind, "CONSERVATION")
        self.assertIn("recall:L1@2024-02-01", result.mismatches[0].records)

    def test_recall_with_evidence_must_match_table_units(self):
        result = reconcile(
            [self.loan],
            recalls=[Recall("L1", D("600"), dt(2024, 2, 1), fitid="F-RCL")],
            evidence=[
                *self.evidence,
                Evidence("F-RCL", SEC, D("500"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(
            any(m.kind == "EVIDENCE" for m in result.mismatches), result.mismatches
        )
        evidence_mismatch = next(
            m for m in result.mismatches if m.kind == "EVIDENCE"
        )
        self.assertIn("ofx:F-RCL", evidence_mismatch.records)
        self.assertIn("recall:F-RCL", evidence_mismatch.records)


class CorporateActionTestCase(unittest.TestCase):
    def setUp(self):
        self.loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-OPEN")
        self.evidence = [
            Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1")
        ]

    def test_split_scales_open_units(self):
        result = reconcile(
            [self.loan], actions=[Split(SEC, dt(2024, 5, 1), D("2"), D("1"))],
            evidence=self.evidence,
        )
        self.assertEqual([b.units for b in result.open_batches], [D("2000")])
        self.assertEqual(result.open_batches[0].actions, ("SPLIT:2/1",))
        self.assertTrue(result.balanced, result)

    def test_counter_split(self):
        result = reconcile(
            [self.loan],
            actions=[CorporateAction(SEC, dt(2024, 5, 1), D("0.5"), "REVERSE")],
            evidence=self.evidence,
        )
        self.assertEqual([b.units for b in result.open_batches], [D("500")])

    def test_action_does_not_touch_other_security(self):
        result = reconcile(
            [self.loan],
            actions=[Split(OTHER_SEC, dt(2024, 5, 1), D("2"), D("1"))],
            evidence=self.evidence,
        )
        self.assertEqual([b.units for b in result.open_batches], [D("1000")])

    def test_partial_return_then_split(self):
        result = reconcile(
            [self.loan],
            returns=[Return("L1", D("400"), dt(2024, 2, 1), fitid="F-RET")],
            actions=[Split(SEC, dt(2024, 5, 1), D("2"), D("1"))],
            evidence=[
                *self.evidence,
                Evidence("F-RET", SEC, D("400"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        # 400 returned pre-split; remaining 600 scales to 1200.
        self.assertEqual([b.units for b in result.open_batches], [D("1200")])
        self.assertEqual(result.settled_batches[0].units, D("400"))
        self.assertEqual(result.settled_batches[0].actions, ())

    def test_split_then_return_settles_post_action_units(self):
        result = reconcile(
            [self.loan],
            actions=[Split(SEC, dt(2024, 5, 1), D("2"), D("1"))],
            returns=[Return("L1", D("1200"), dt(2024, 6, 1), fitid="F-RET")],
            evidence=[
                *self.evidence,
                Evidence("F-RET", SEC, D("1200"), dt(2024, 6, 1), "RETURN", "L1"),
            ],
        )
        self.assertEqual([b.units for b in result.open_batches], [D("800")])
        self.assertEqual(result.settled_batches[0].units, D("1200"))
        self.assertEqual(result.settled_batches[0].actions, ("SPLIT:2/1",))

    def test_loan_opened_on_action_date_not_rescaled(self):
        loan = Lend("L2", SEC, "CPT", D("500"), dt(2024, 5, 1), fitid="F-NEW")
        result = reconcile(
            [loan],
            actions=[Split(SEC, dt(2024, 5, 1), D("2"), D("1"))],
            evidence=[Evidence("F-NEW", SEC, D("500"), dt(2024, 5, 1), "LEND", "L2")],
        )
        self.assertEqual([b.units for b in result.open_batches], [D("500")])

    def test_action_after_full_settlement_is_noop(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-OPEN")
        result = reconcile(
            [loan],
            returns=[Return("L1", D("1000"), dt(2024, 2, 1), fitid="F-RET")],
            actions=[Split(SEC, dt(2024, 5, 1), D("2"), D("1"))],
            evidence=[
                Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-RET", SEC, D("1000"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertEqual(result.open_batches, [])
        self.assertEqual(result.settled_batches[0].units, D("1000"))
        self.assertEqual(result.settled_batches[0].actions, ())
        self.assertTrue(result.balanced, result)

    def test_action_applies_to_each_lend_loan_not_borrow(self):
        lend = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-L")
        borrow = Borrow("B1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-B")
        result = reconcile(
            [lend, borrow],
            actions=[Split(SEC, dt(2024, 5, 1), D("2"), D("1"))],
            evidence=[
                Evidence("F-L", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-B", SEC, D("1000"), dt(2024, 1, 1), "BORROW", "B1"),
            ],
        )
        # Corporate actions scale on-loan units in both directions; direction
        # isolation means the two results are reported separately.
        self.assertEqual(result.on_loan("LEND"), {SEC: D("2000")})
        self.assertEqual(result.on_loan("BORROW"), {SEC: D("2000")})


class DirectionIsolationTestCase(unittest.TestCase):
    """A separate borrow of the same security must not net a lend."""

    def test_borrow_does_not_offset_lend(self):
        lend = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        borrow = Borrow("B1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-BORROW")
        result = reconcile(
            [lend, borrow],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-BORROW", SEC, D("1000"), dt(2024, 1, 1), "BORROW", "B1"),
            ],
        )
        self.assertTrue(result.balanced, result)
        self.assertEqual(result.on_loan("LEND"), {SEC: D("1000")})
        self.assertEqual(result.on_loan("BORROW"), {SEC: D("1000")})

    def test_borrow_evidence_cannot_close_lend(self):
        lend = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [lend],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-BORROW", SEC, D("1000"), dt(2024, 1, 1), "BORROW", "L1"),
            ],
        )
        self.assertEqual([b.units for b in result.open_batches], [D("1000")])
        reasons = [u.reason for u in result.unmatched]
        self.assertIn("WRONG_DIRECTION", reasons)
        wrong = next(u for u in result.unmatched if u.reason == "WRONG_DIRECTION")
        self.assertEqual(wrong.fitid, "F-BORROW")

    def test_borrow_return_does_not_close_lend(self):
        lend = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        borrow = Borrow("B1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-BORROW")
        result = reconcile(
            [lend, borrow],
            returns=[Return("B1", D("1000"), dt(2024, 2, 1), fitid="F-RET")],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-BORROW", SEC, D("1000"), dt(2024, 1, 1), "BORROW", "B1"),
                Evidence("F-RET", SEC, D("1000"), dt(2024, 2, 1), "RETURN", "B1"),
            ],
        )
        self.assertEqual([b.units for b in result.open_batches], [D("1000")])
        self.assertEqual([b.loan_id for b in result.settled_batches], ["B1"])

    def test_return_of_one_loan_does_not_close_identical_other_loan(self):
        """Same security, counterparty, units and dates: still separate loans."""
        first = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-A")
        second = Lend("L2", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-B")
        result = reconcile(
            [first, second],
            returns=[Return("L1", D("1000"), dt(2024, 2, 1), fitid="F-RET")],
            evidence=[
                Evidence("F-A", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-B", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L2"),
                Evidence("F-RET", SEC, D("1000"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(result.balanced, result)
        self.assertEqual(
            [(b.loan_id, b.units) for b in result.open_batches],
            [("L2", D("1000"))],
        )
        self.assertEqual([b.loan_id for b in result.settled_batches], ["L1"])

    def test_return_linked_to_wrong_loan_is_undetermined(self):
        first = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-A")
        second = Lend("L2", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-B")
        result = reconcile(
            [first, second],
            returns=[Return("L1", D("1000"), dt(2024, 2, 1), fitid="F-RET")],
            evidence=[
                Evidence("F-A", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-B", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L2"),
                # Explicitly linked to L2, while the table row says L1.
                Evidence("F-RET", SEC, D("1000"), dt(2024, 2, 1), "RETURN", "L2"),
            ],
        )
        self.assertTrue(any(u.reason == "WRONG_LOAN" for u in result.unmatched))
        self.assertEqual(
            [(b.loan_id, b.units) for b in result.open_batches],
            [("L2", D("1000"))],
        )


class UndeterminedTestCase(unittest.TestCase):
    def setUp(self):
        self.loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")

    def test_evidence_without_link_is_undetermined(self):
        result = reconcile(
            [self.loan],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-OTHER", SEC, D("500"), dt(2024, 2, 1), "RETURN"),
            ],
        )
        self.assertFalse(result.balanced)
        reasons = {(u.fitid, u.reason) for u in result.unmatched}
        self.assertIn(("F-OTHER", "NO_LOAN_ID"), reasons)

    def test_evidence_with_unknown_loan(self):
        result = reconcile(
            [self.loan],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-X", SEC, D("10"), dt(2024, 2, 1), "RETURN", "NOPE"),
            ],
        )
        self.assertTrue(any(u.reason == "UNKNOWN_LOAN" for u in result.unmatched))

    def test_evidence_with_wrong_security(self):
        result = reconcile(
            [self.loan],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-X", OTHER_SEC, D("10"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(any(u.reason == "WRONG_SECURITY" for u in result.unmatched))

    def test_return_evidence_without_table_row(self):
        result = reconcile(
            [self.loan],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-RET", SEC, D("100"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(
            any(u.reason == "NO_TABLE_RECORD" for u in result.unmatched)
        )

    def test_table_return_without_evidence(self):
        result = reconcile(
            [self.loan],
            returns=[Return("L1", D("100"), dt(2024, 2, 1), fitid="F-RET")],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1")
            ],
        )
        self.assertTrue(
            any(u.reason == "NO_EVIDENCE" for u in result.unmatched)
        )

    def test_loan_with_no_evidence(self):
        result = reconcile([self.loan])
        self.assertTrue(any(u.reason == "NO_EVIDENCE" for u in result.unmatched))

    def test_require_evidence_false_allows_no_evidence(self):
        result = reconcile([self.loan], require_evidence=False)
        self.assertTrue(result.balanced)


class QuantityConservationTestCase(unittest.TestCase):
    def test_expected_closing_mismatch_points_at_records(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [loan],
            returns=[Return("L1", D("400"), dt(2024, 2, 1), fitid="F-R1")],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-R1", SEC, D("400"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
            expected={"L1": D("700")},
        )
        self.assertTrue(result.mismatches)
        mismatch = result.mismatches[0]
        self.assertEqual(mismatch.kind, "EXPECTED")
        self.assertEqual(mismatch.closing, D("600"))
        self.assertIn("loan:L1@2024-01-01", mismatch.records)
        self.assertIn("return:F-R1", mismatch.records)

    def test_evidence_opening_quantity_mismatch(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [loan],
            evidence=[
                Evidence("F-LEND", SEC, D("900"), dt(2024, 1, 1), "LEND", "L1")
            ],
        )
        self.assertTrue(result.mismatches)
        mismatch = result.mismatches[0]
        self.assertEqual(mismatch.kind, "EVIDENCE")
        self.assertEqual(mismatch.opening, D("1000"))
        self.assertEqual(mismatch.closing, D("900"))
        self.assertIn("ofx:F-LEND", mismatch.records)

    def test_table_internal_over_settlement(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [loan],
            returns=[Return("L1", D("800"), dt(2024, 2, 1), fitid="F-R1")],
            recalls=[Recall("L1", D("500"), dt(2024, 3, 1))],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-R1", SEC, D("800"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        self.assertTrue(any(m.kind == "CONSERVATION" for m in result.mismatches))
        mismatch = next(m for m in result.mismatches if m.kind == "CONSERVATION")
        self.assertIn("loan:L1@2024-01-01", mismatch.records)
        self.assertIn("recall:L1@2024-03-01", mismatch.records)

    def test_conservation_after_split_uses_post_action_units(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [loan],
            actions=[Split(SEC, dt(2024, 5, 1), D("2"), D("1"))],
            returns=[Return("L1", D("3000"), dt(2024, 6, 1), fitid="F-R")],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-R", SEC, D("3000"), dt(2024, 6, 1), "RETURN", "L1"),
            ],
        )
        mismatch = next(m for m in result.mismatches if m.kind == "CONSERVATION")
        # Opening here is the post-action 2000 units, not the original 1000.
        self.assertEqual(mismatch.opening, D("2000"))
        self.assertEqual(mismatch.closing, D("-1000"))
        self.assertIn("action:SPLIT:2/1@2024-05-01", mismatch.records)

    def test_opening_evidence_mismatch_only_lists_opening_records(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [loan],
            returns=[Return("L1", D("400"), dt(2024, 2, 1), fitid="F-R")],
            evidence=[
                Evidence("F-LEND", SEC, D("900"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-R", SEC, D("400"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        mismatch = next(m for m in result.mismatches if m.kind == "EVIDENCE")
        self.assertEqual(
            set(mismatch.records), {"loan:L1@2024-01-01", "ofx:F-LEND"}
        )

    def test_batch_address_is_unique_across_open_and_settled(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [loan],
            returns=[Return("L1", D("400"), dt(2024, 2, 1), fitid="F-R")],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-R", SEC, D("400"), dt(2024, 2, 1), "RETURN", "L1"),
            ],
        )
        settled = result.batch("L1", 0)
        opened = result.batch("L1", 1)
        self.assertIsNotNone(settled)
        self.assertIsNotNone(opened)
        assert settled is not None and opened is not None
        self.assertEqual(settled.units, D("400"))
        self.assertEqual(opened.units, D("600"))
        self.assertIsNone(result.batch("L1", 99))


class TableValidationTestCase(unittest.TestCase):
    def test_duplicate_loan_ids(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1))
        with self.assertRaises(TableError):
            reconcile([loan, loan], require_evidence=False)

    def test_unknown_settlement_loan(self):
        with self.assertRaises(TableError):
            reconcile(
                [],
                returns=[Return("NOPE", D("1"), dt(2024, 1, 2))],
                require_evidence=False,
            )

    def test_conflicting_fitid_links(self):
        loan1 = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F1")
        loan2 = Lend("L2", OTHER_SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F1")
        with self.assertRaises(TableError):
            reconcile([loan1, loan2], require_evidence=False)

    def test_asof_excludes_future_events(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-LEND")
        result = reconcile(
            [loan],
            returns=[Return("L1", D("400"), dt(2024, 6, 1), fitid="F-R1")],
            evidence=[
                Evidence("F-LEND", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1"),
                Evidence("F-R1", SEC, D("400"), dt(2024, 6, 1), "RETURN", "L1"),
            ],
            asof=dt(2024, 3, 1),
        )
        self.assertEqual([b.units for b in result.open_batches], [D("1000")])
        self.assertEqual(result.settled_batches, [])


class OfxIntegrationTestCase(unittest.TestCase):
    def make_jrnlsec(self, fitid, units, date, uniqueid="123456789"):
        invtran = INVTRAN(fitid=fitid, dttrade=date)
        return JRNLSEC(
            invtran=invtran,
            secid=make_secid(uniqueid),
            subacctto="CASH",
            subacctfrom="MARGIN",
            units=units,
        )

    def test_evidence_from_jrnlsec_sign(self):
        date = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        lend_record = evidence_from_transaction(
            self.make_jrnlsec("F-OUT", D("-1000"), date)
        )
        return_record = evidence_from_transaction(
            self.make_jrnlsec("F-IN", D("400"), date)
        )
        self.assertIsNotNone(lend_record)
        self.assertIsNotNone(return_record)
        assert lend_record is not None and return_record is not None
        self.assertEqual(lend_record.direction, "LEND")
        self.assertEqual(lend_record.units, D("1000"))
        self.assertEqual(return_record.direction, "RETURN")
        self.assertEqual(return_record.units, D("400"))

    def test_reconcile_statement_flow(self):
        date_open = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
        date_ret = datetime.datetime(2024, 2, 1, tzinfo=datetime.timezone.utc)
        opening = self.make_jrnlsec("F-OPEN", D("-1000"), date_open)
        returned = self.make_jrnlsec("F-RET", D("400"), date_ret)
        tranlist = INVTRANLIST(opening, returned, dtstart=date_open, dtend=date_ret)
        statement = INVSTMTRS(
            dtasof=date_ret,
            curdef="USD",
            invacctfrom=INVACCTFROM(brokerid="111000614", acctid="999988"),
            invtranlist=tranlist,
        )
        loans = [Lend("L1", SEC, "CPT", D("1000"), date_open)]
        result = reconcile_statements(
            loans,
            [statement],
            returns=[Return("L1", D("400"), date_ret, fitid="F-RET")],
            recalls=[],
            correspondence={"F-OPEN": "L1", "F-RET": "L1"},
        )
        self.assertEqual([b.units for b in result.open_batches], [D("600")])
        self.assertEqual(result.settled_batches[0].units, D("400"))
        self.assertTrue(result.balanced, result)

    def test_split_from_statement(self):
        date_split = datetime.datetime(2024, 5, 1, tzinfo=datetime.timezone.utc)
        split = OFXSPLIT(
            invtran=INVTRAN(fitid="F-SPLIT", dttrade=date_split),
            secid=make_secid(),
            subacctsec="CASH",
            oldunits=D("600"),
            newunits=D("1200"),
            numerator=D("2"),
            denominator=D("1"),
        )
        tranlist = INVTRANLIST(
            split,
            dtstart=date_split,
            dtend=date_split,
        )
        statement = INVSTMTRS(
            dtasof=date_split,
            curdef="USD",
            invacctfrom=INVACCTFROM(brokerid="111000614", acctid="999988"),
            invtranlist=tranlist,
        )
        actions = actions_from_statement(statement)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].security, SEC)
        self.assertEqual(actions[0].ratio, D("2"))

    def test_security_key(self):
        secid = make_secid()
        self.assertEqual(security_key(secid), SEC)


class TableDictTestCase(unittest.TestCase):
    def table(self):
        return {
            "asof": "2024-12-31",
            "loans": [
                {
                    "type": "lend",
                    "loan_id": "L1",
                    "security": SEC,
                    "counterparty": "CPT",
                    "units": "1000",
                    "date": "2024-01-01",
                    "fitid": "F-OPEN",
                },
                {
                    "type": "borrow",
                    "loan_id": "B1",
                    "security": SEC,
                    "counterparty": "CPT",
                    "units": "1000",
                    "date": "2024-01-01",
                    "fitid": "F-BORROW",
                },
            ],
            "returns": [
                {
                    "loan_id": "L1",
                    "units": "400",
                    "date": "2024-02-01",
                    "fitid": "F-RET1",
                }
            ],
            "recalls": [
                {"loan_id": "L1", "units": "100", "date": "2024-03-01"}
            ],
            "actions": [
                {
                    "type": "split",
                    "security": SEC,
                    "date": "2024-05-01",
                    "numerator": "2",
                    "denominator": "1",
                }
            ],
            "correspondence": {"F-OPEN": "L1", "F-RET1": "L1"},
            "expected": {"L1": "1000"},
            "require_evidence": False,
        }

    def test_table_from_dict(self):
        options = table_from_dict(self.table())
        self.assertEqual(len(options["loans"]), 2)
        self.assertIsInstance(options["loans"][0], Lend)
        self.assertIsInstance(options["loans"][1], Borrow)
        self.assertEqual(options["returns"][0].units, D("400"))
        self.assertEqual(options["recalls"][0].units, D("100"))
        self.assertEqual(options["actions"][0].ratio, D("2"))
        self.assertEqual(options["asof"], "2024-12-31")

    def test_unknown_loan_type_rejected(self):
        table = self.table()
        table["loans"][0]["type"] = "short"
        with self.assertRaises(TableError):
            table_from_dict(table)

    def test_reconcile_from_dict(self):
        options = table_from_dict(self.table())
        result = reconcile(**options)
        # L1: 1000 - 400 - 100 = 500, split 2:1 -> 1000 open.
        # B1 is untouched by settlements but the same split applies: 2000.
        self.assertEqual(result.on_loan("LEND"), {SEC: D("1000")})
        self.assertEqual(result.on_loan("BORROW"), {SEC: D("2000")})
        self.assertTrue(result.balanced, result)

    def test_reconciliation_to_dict(self):
        options = table_from_dict(self.table())
        result = reconcile(**options)
        data = reconciliation_to_dict(result)
        self.assertEqual(data["asof"], "2024-12-31")
        self.assertTrue(data["balanced"])
        self.assertEqual(data["on_loan"]["LEND"], {SEC: "1000"})
        self.assertEqual(data["on_loan"]["BORROW"], {SEC: "2000"})
        lend_batch = next(
            batch for batch in data["open_batches"] if batch["loan_id"] == "L1"
        )
        self.assertEqual(lend_batch["units"], "1000")
        self.assertEqual(data["settled_batches"][0]["units"], "400")
        self.assertEqual(lend_batch["actions"], ["SPLIT:2/1"])


class ReconcileFileTestCase(unittest.TestCase):
    """End-to-end: parsed OFX file plus JSON-style correspondence table."""

    def make_ofx(self):
        import xml.etree.ElementTree as ET

        from ofxtools.header import make_header
        from ofxtools.models import (
            INVSTMTMSGSRSV1,
            INVSTMTRS,
            INVSTMTTRNRS,
            INVTRAN,
            INVTRANLIST,
            JRNLSEC,
            OFX,
            SECID,
            SIGNONMSGSRSV1,
            SONRS,
            STATUS,
        )
        from ofxtools.utils import UTC

        secid = SECID(uniqueid="123456789", uniqueidtype="CUSIP")

        def journal(fitid, date, units):
            return JRNLSEC(
                invtran=INVTRAN(fitid=fitid, dttrade=date),
                secid=secid,
                subacctto="CASH",
                subacctfrom="MARGIN",
                units=D(units),
            )

        start = datetime.datetime(2024, 1, 1, tzinfo=UTC)
        end = datetime.datetime(2024, 2, 1, tzinfo=UTC)
        tranlist = INVTRANLIST(
            journal("F-OPEN", start, "-1000"),
            journal("F-RET", end, "400"),
            dtstart=start,
            dtend=end,
        )
        stmt = INVSTMTRS(
            dtasof=end,
            curdef="USD",
            invacctfrom=INVACCTFROM(brokerid="111000614", acctid="999988"),
            invtranlist=tranlist,
        )
        ofx = OFX(
            signonmsgsrsv1=SIGNONMSGSRSV1(
                sonrs=SONRS(
                    status=STATUS(code=0, severity="INFO"),
                    dtserver=end,
                    language="ENG",
                )
            ),
            invstmtmsgsrsv1=INVSTMTMSGSRSV1(
                INVSTMTTRNRS(
                    trnuid="1",
                    status=STATUS(code=0, severity="INFO"),
                    invstmtrs=stmt,
                )
            ),
        )
        body = ET.tostring(ofx.to_etree()).decode()
        return (str(make_header(version=220)) + body).encode()

    def table(self):
        return {
            "asof": "2024-12-31",
            "loans": [
                {
                    "type": "lend",
                    "loan_id": "L1",
                    "security": SEC,
                    "counterparty": "CPT",
                    "units": "1000",
                    "date": "2024-01-01",
                    "fitid": "F-OPEN",
                }
            ],
            "returns": [
                {
                    "loan_id": "L1",
                    "units": "400",
                    "date": "2024-02-01",
                    "fitid": "F-RET",
                }
            ],
            "correspondence": {"F-OPEN": "L1", "F-RET": "L1"},
        }

    def test_reconcile_file_bytesio(self):
        import io

        result = reconcile_file(io.BytesIO(self.make_ofx()), self.table())
        self.assertTrue(result.balanced, result)
        self.assertEqual(result.on_loan("LEND"), {SEC: D("600")})
        self.assertEqual([b.units for b in result.settled_batches], [D("400")])

    def test_reconcile_file_json_path(self):
        import io
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "table.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.table(), handle)
            result = reconcile_file(io.BytesIO(self.make_ofx()), path)
        self.assertTrue(result.balanced, result)
        self.assertEqual(result.on_loan("LEND"), {SEC: D("600")})

    def test_reconcile_file_unlinked_journal_is_undetermined(self):
        import io

        table = self.table()
        table["correspondence"] = {"F-OPEN": "L1"}  # F-RET deliberately missing
        table["returns"][0].pop("fitid")  # table row also cites no OFX record
        result = reconcile_file(io.BytesIO(self.make_ofx()), table)
        self.assertTrue(
            any(
                u.source == "return" and u.reason == "NO_EVIDENCE"
                for u in result.unmatched
            ),
            result.unmatched,
        )
        # The table row still settles its units; the missing link is reported,
        # not silently assumed.
        self.assertEqual(result.on_loan("LEND"), {SEC: D("600")})

    def test_cli_balanced_exit_zero(self):
        import contextlib
        import io
        import json
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ofx_path = os.path.join(tmp, "stmt.ofx")
            table_path = os.path.join(tmp, "table.json")
            with open(ofx_path, "wb") as handle:
                handle.write(self.make_ofx())
            with open(table_path, "w", encoding="utf-8") as handle:
                json.dump(self.table(), handle)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = lending_main([ofx_path, table_path])
        self.assertEqual(code, 0)
        data = json.loads(buffer.getvalue())
        self.assertTrue(data["balanced"])
        self.assertEqual(data["on_loan"]["LEND"], {SEC: "600"})

    def test_cli_unbalanced_exit_one(self):
        import contextlib
        import io
        import json
        import os
        import tempfile

        table = self.table()
        table["expected"] = {"L1": "999"}
        with tempfile.TemporaryDirectory() as tmp:
            ofx_path = os.path.join(tmp, "stmt.ofx")
            table_path = os.path.join(tmp, "table.json")
            with open(ofx_path, "wb") as handle:
                handle.write(self.make_ofx())
            with open(table_path, "w", encoding="utf-8") as handle:
                json.dump(table, handle)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = lending_main([ofx_path, table_path])
        self.assertEqual(code, 1)
        data = json.loads(buffer.getvalue())
        self.assertFalse(data["balanced"])

    def test_cli_bad_table_exit_two(self):
        import contextlib
        import io
        import json
        import os
        import tempfile

        table = self.table()
        table["loans"][0]["type"] = "short"
        with tempfile.TemporaryDirectory() as tmp:
            ofx_path = os.path.join(tmp, "stmt.ofx")
            table_path = os.path.join(tmp, "table.json")
            with open(ofx_path, "wb") as handle:
                handle.write(self.make_ofx())
            with open(table_path, "w", encoding="utf-8") as handle:
                json.dump(table, handle)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = lending_main([ofx_path, table_path])
        self.assertEqual(code, 2)
        self.assertIn("error:", stderr.getvalue())


class EvidenceValidationTestCase(unittest.TestCase):
    def test_bad_direction(self):
        with self.assertRaises(EvidenceError):
            Evidence("F1", SEC, D("1"), dt(2024, 1, 1), "SIDEWAYS")

    def test_missing_fitid(self):
        with self.assertRaises(EvidenceError):
            Evidence("", SEC, D("1"), dt(2024, 1, 1), "LEND")

    def test_return_evidence_must_be_a_return_direction(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-OPEN")
        result = reconcile(
            [loan],
            returns=[Return("L1", D("400"), dt(2024, 2, 1), fitid="F-OPEN")],
            evidence=[
                Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1")
            ],
        )
        self.assertTrue(
            any(
                u.source == "return"
                and u.fitid == "F-OPEN"
                and u.reason == "NO_EVIDENCE"
                for u in result.unmatched
            ),
            result.unmatched,
        )

    def test_recall_evidence_must_be_a_return_direction(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1), fitid="F-OPEN")
        result = reconcile(
            [loan],
            recalls=[Recall("L1", D("100"), dt(2024, 2, 1), fitid="F-OPEN")],
            evidence=[
                Evidence("F-OPEN", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1")
            ],
        )
        self.assertTrue(
            any(
                u.source == "recall"
                and u.fitid == "F-OPEN"
                and u.reason == "NO_EVIDENCE"
                for u in result.unmatched
            ),
            result.unmatched,
        )

    def test_duplicate_evidence_fitid_is_collapsed(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1))
        record = Evidence("F1", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1")
        result = reconcile([loan], evidence=[record, record])
        self.assertTrue(result.balanced, result)
        self.assertEqual([b.units for b in result.open_batches], [D("1000")])

    def test_conflicting_duplicate_evidence_fitid(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1))
        first = Evidence("F1", SEC, D("1000"), dt(2024, 1, 1), "LEND", "L1")
        second = Evidence("F1", SEC, D("900"), dt(2024, 1, 1), "LEND", "L1")
        with self.assertRaises(EvidenceError):
            reconcile([loan], evidence=[first, second])


class NoMutationTestCase(unittest.TestCase):
    def test_inputs_are_not_modified(self):
        loan = Lend("L1", SEC, "CPT", D("1000"), dt(2024, 1, 1))
        returned = Return("L1", D("400"), dt(2024, 2, 1))
        reconcile([loan], returns=[returned], require_evidence=False)
        self.assertEqual(loan.units, D("1000"))
        self.assertEqual(returned.units, D("400"))


if __name__ == "__main__":
    unittest.main()
