"""
Unit tests for the local authorization ledger - ofxtools.authorization
"""

# stdlib imports
import datetime
import unittest
from decimal import Decimal

# local imports
from ofxtools.authorization import (
    Authorization,
    AuthorizationConsumption,
    AuthorizationLedger,
    AuthorizationNotInForceError,
    ConflictingDuplicateError,
    CurrencyMismatchError,
    DuplicateFlowError,
    FindingLevel,
    Flow,
    FlowStatus,
    FlowType,
    MissingAuthorizationError,
    OrphanReversalError,
    PeriodGranularity,
    PeriodKey,
    PeriodLimitExceededError,
    PerTransactionLimitExceededError,
    RegistrationError,
    ReversalAmountExceededError,
    ReversalAuthorizationMismatchError,
    ReversalCurrencyMismatchError,
    UnknownAuthorizationError,
    flow_from_stmttrn,
)
from ofxtools.models.bank.stmt import STMTTRN
from ofxtools.models.i18n import CURRENCY
from ofxtools.utils import UTC


def _dt(year: int, month: int, day: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, tzinfo=UTC)


def _auth(
    auth_id: str = "AUTH-1",
    *,
    currency: str = "USD",
    per_transaction_limit: str = "100.00",
    period_limit: str = "250.00",
    valid_from: datetime.date = datetime.date(2026, 1, 1),
    period: PeriodGranularity = PeriodGranularity.MONTH,
    valid_to: datetime.date | None = None,
) -> Authorization:
    return Authorization(
        auth_id=auth_id,
        currency=currency,
        per_transaction_limit=Decimal(per_transaction_limit),
        period_limit=Decimal(period_limit),
        valid_from=valid_from,
        period=period,
        valid_to=valid_to,
    )


def _flow(
    flow_id: str = "F1",
    *,
    day: tuple[int, int, int] = (2026, 1, 10),
    amount: str = "100.00",
    currency: str = "USD",
    auth_ref: str | None = "AUTH-1",
    kind: FlowType = FlowType.DEBIT,
    reversal_of: str | None = None,
) -> Flow:
    return Flow(
        flow_id=flow_id,
        posted=_dt(*day),
        amount=Decimal(amount),
        currency=currency,
        auth_ref=auth_ref,
        kind=kind,
        reversal_of=reversal_of,
    )


class AuthorizationTestCase(unittest.TestCase):
    def test_normalization(self) -> None:
        authorization = _auth(currency="usd", auth_id=" AUTH-1 ")
        self.assertEqual(authorization.auth_id, "AUTH-1")
        self.assertEqual(authorization.currency, "USD")
        self.assertIsInstance(authorization.per_transaction_limit, Decimal)

    def test_blank_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _auth(auth_id="  ")

    def test_non_positive_limits_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _auth(per_transaction_limit="0")
        with self.assertRaises(ValueError):
            _auth(period_limit="-1")

    def test_period_limit_below_transaction_limit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _auth(per_transaction_limit="100", period_limit="50")

    def test_validity_window_reversed_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _auth(
                valid_from=datetime.date(2026, 2, 1),
                valid_to=datetime.date(2026, 1, 1),
            )

    def test_in_force_on_inclusive_boundaries(self) -> None:
        authorization = _auth(
            valid_from=datetime.date(2026, 1, 1),
            valid_to=datetime.date(2026, 1, 31),
        )
        self.assertTrue(authorization.in_force_on(datetime.date(2026, 1, 1)))
        self.assertTrue(authorization.in_force_on(datetime.date(2026, 1, 31)))
        self.assertFalse(authorization.in_force_on(datetime.date(2025, 12, 31)))
        self.assertFalse(authorization.in_force_on(datetime.date(2026, 2, 1)))

    def test_open_ended_validity(self) -> None:
        authorization = _auth()
        self.assertTrue(authorization.in_force_on(datetime.date(2099, 1, 1)))

    def test_frozen(self) -> None:
        with self.assertRaises(Exception):
            _auth().currency = "EUR"  # type: ignore[misc]


class FlowTestCase(unittest.TestCase):
    def test_blank_reference_normalized_to_none(self) -> None:
        flow = _flow(auth_ref="   ")
        self.assertIsNone(flow.auth_ref)

    def test_amount_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            _flow(amount="0")
        with self.assertRaises(ValueError):
            _flow(amount="-10")

    def test_reversal_requires_original(self) -> None:
        with self.assertRaises(ValueError):
            Flow(
                flow_id="R1",
                posted=_dt(2026, 1, 11),
                amount=Decimal("10"),
                currency="USD",
                kind=FlowType.REVERSAL,
            )

    def test_debit_must_not_reference_original(self) -> None:
        with self.assertRaises(ValueError):
            _flow(reversal_of="F0")

    def test_equality_by_content(self) -> None:
        self.assertEqual(_flow(), _flow())
        self.assertNotEqual(_flow(amount="99"), _flow(amount="100"))


class PeriodKeyTestCase(unittest.TestCase):
    def test_day_key(self) -> None:
        from ofxtools.authorization import _period_key

        key = _period_key(
            PeriodGranularity.DAY, datetime.date(2026, 1, 15)
        )
        self.assertEqual(
            key, PeriodKey(PeriodGranularity.DAY, 2026, 1, 15)
        )

    def test_week_key(self) -> None:
        from ofxtools.authorization import _period_key

        key = _period_key(
            PeriodGranularity.WEEK, datetime.date(2026, 1, 15)
        )
        iso = datetime.date(2026, 1, 15).isocalendar()
        self.assertEqual(key.granularity, PeriodGranularity.WEEK)
        self.assertEqual((key.year, key.week), (iso.year, iso.week))

    def test_month_key(self) -> None:
        from ofxtools.authorization import _period_key

        key = _period_key(
            PeriodGranularity.MONTH, datetime.date(2026, 1, 31)
        )
        self.assertEqual(key, PeriodKey(PeriodGranularity.MONTH, 2026, 1))

    def test_year_key(self) -> None:
        from ofxtools.authorization import _period_key

        key = _period_key(
            PeriodGranularity.YEAR, datetime.date(2026, 12, 31)
        )
        self.assertEqual(key, PeriodKey(PeriodGranularity.YEAR, 2026))


class RegistrationTestCase(unittest.TestCase):
    def test_duplicate_effective_date_rejected(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        with self.assertRaises(RegistrationError):
            ledger.register_authorization(_auth(period_limit="300"))

    def test_out_of_order_version_rejected(self) -> None:
        ledger = AuthorizationLedger()
        ledger.register_authorization(
            _auth(valid_from=datetime.date(2026, 2, 1))
        )
        with self.assertRaises(RegistrationError):
            ledger.register_authorization(
                _auth(valid_from=datetime.date(2026, 1, 1))
            )

    def test_open_ended_predecessor_blocks_new_version(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        with self.assertRaises(RegistrationError):
            ledger.register_authorization(
                _auth(valid_from=datetime.date(2026, 2, 1))
            )

    def test_version_gap_rejected(self) -> None:
        ledger = AuthorizationLedger(
            [_auth(valid_to=datetime.date(2026, 1, 31))]
        )
        with self.assertRaises(RegistrationError):
            ledger.register_authorization(
                _auth(valid_from=datetime.date(2026, 2, 5))
            )

    def test_contiguous_versions_accepted(self) -> None:
        ledger = AuthorizationLedger(
            [_auth(valid_to=datetime.date(2026, 1, 31))]
        )
        ledger.register_authorization(
            _auth(
                valid_from=datetime.date(2026, 2, 1),
                period_limit="500",
            )
        )
        self.assertEqual(len(ledger.versions("AUTH-1")), 2)

    def test_amend_closes_previous_version_at_boundary(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        amended = ledger.amend_authorization(
            "AUTH-1",
            datetime.date(2026, 2, 1),
            period_limit=Decimal("500"),
            per_transaction_limit=Decimal("200"),
        )
        old, new = ledger.versions("AUTH-1")
        self.assertEqual(old.valid_to, datetime.date(2026, 1, 31))
        self.assertIs(amended, new)
        self.assertEqual(new.period_limit, Decimal("500"))
        self.assertEqual(new.per_transaction_limit, Decimal("200"))
        self.assertEqual(new.valid_from, datetime.date(2026, 2, 1))

    def test_amend_unknown_authorization(self) -> None:
        with self.assertRaises(RegistrationError):
            AuthorizationLedger().amend_authorization(
                "NOPE", datetime.date(2026, 2, 1)
            )

    def test_amend_boundary_must_follow_chain(self) -> None:
        ledger = AuthorizationLedger(
            [_auth(valid_to=datetime.date(2026, 1, 31))]
        )
        with self.assertRaises(RegistrationError):
            ledger.amend_authorization(
                "AUTH-1", datetime.date(2026, 3, 1)
            )


class DebitProcessingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = AuthorizationLedger([_auth()])

    def test_happy_path_consumes_period_capacity(self) -> None:
        result = self.ledger.process(_flow(amount="100"))
        self.assertEqual(result.status, FlowStatus.POSTED)
        self.assertEqual(result.period_consumed, Decimal("100.00"))
        self.assertEqual(result.available, Decimal("150.00"))
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 20)),
            Decimal("100.00"),
        )

    def test_missing_reference_rejected_not_rerouted(self) -> None:
        flow = _flow(auth_ref=None)
        with self.assertRaises(MissingAuthorizationError) as cm:
            self.ledger.process(flow)
        self.assertEqual(cm.exception.code, "MISSING_AUTH_REF")
        # Nothing booked against any authorization
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 10)),
            Decimal("0"),
        )

    def test_unknown_reference_rejected(self) -> None:
        with self.assertRaises(UnknownAuthorizationError):
            self.ledger.process(_flow(auth_ref="AUTH-OTHER"))

    def test_not_yet_effective(self) -> None:
        ledger = AuthorizationLedger(
            [_auth(valid_from=datetime.date(2026, 2, 1))]
        )
        with self.assertRaises(AuthorizationNotInForceError):
            ledger.process(_flow(day=(2026, 1, 31)))

    def test_expired_authorization(self) -> None:
        ledger = AuthorizationLedger(
            [_auth(valid_to=datetime.date(2026, 1, 31))]
        )
        with self.assertRaises(AuthorizationNotInForceError):
            ledger.process(_flow(day=(2026, 2, 1)))

    def test_currency_mismatch(self) -> None:
        with self.assertRaises(CurrencyMismatchError):
            self.ledger.process(_flow(currency="EUR"))

    def test_per_transaction_limit(self) -> None:
        with self.assertRaises(PerTransactionLimitExceededError):
            self.ledger.process(_flow(amount="100.01"))

    def test_period_cumulative_limit(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        self.ledger.process(_flow("F2", amount="100"))
        with self.assertRaises(PeriodLimitExceededError):
            self.ledger.process(_flow("F3", amount="50.01"))
        # Exactly at the cap is allowed
        result = self.ledger.process(_flow("F3", amount="50.00"))
        self.assertEqual(result.available, Decimal("0.00"))

    def test_period_rollover_resets_accumulation(self) -> None:
        ledger = AuthorizationLedger(
            [
                _auth(
                    period=PeriodGranularity.MONTH,
                    per_transaction_limit="250",
                    period_limit="250",
                )
            ]
        )
        ledger.process(_flow("F1", day=(2026, 1, 31), amount="250"))
        result = ledger.process(_flow("F2", day=(2026, 2, 1), amount="100"))
        self.assertEqual(result.period_consumed, Decimal("100.00"))
        self.assertEqual(
            ledger.consumed("AUTH-1", datetime.date(2026, 1, 31)),
            Decimal("250.00"),
        )

    def test_day_granularity(self) -> None:
        ledger = AuthorizationLedger(
            [_auth(period=PeriodGranularity.DAY, period_limit="100")]
        )
        ledger.process(_flow("F1", day=(2026, 1, 10), amount="100"))
        with self.assertRaises(PeriodLimitExceededError):
            ledger.process(_flow("F2", day=(2026, 1, 10), amount="0.01"))
        result = ledger.process(
            _flow("F3", day=(2026, 1, 11), amount="100")
        )
        self.assertEqual(result.period_consumed, Decimal("100.00"))


class DuplicateFlowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = AuthorizationLedger([_auth()])

    def test_duplicate_raises_and_consumes_nothing_more(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        with self.assertRaises(DuplicateFlowError):
            self.ledger.process(_flow("F1", amount="100"))
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 10)),
            Decimal("100.00"),
        )

    def test_conflicting_duplicate_raises(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        with self.assertRaises(ConflictingDuplicateError):
            self.ledger.process(_flow("F1", amount="99"))

    def test_reconcile_duplicate_is_warning_not_rejection(self) -> None:
        report = self.ledger.reconcile(
            [_flow("F1", amount="100"), _flow("F1", amount="100")]
        )
        statuses = {result.flow_id: result.status for result in report.results}
        self.assertEqual(statuses["F1"], FlowStatus.DUPLICATE)
        self.assertEqual(len(report.warnings), 1)
        self.assertEqual(report.warnings[0].code, "DUPLICATE_FLOW")
        self.assertTrue(report.is_clean)
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 10)),
            Decimal("100.00"),
        )

    def test_conflicting_duplicate_is_error_in_reconcile(self) -> None:
        report = self.ledger.reconcile(
            [_flow("F1", amount="100"), _flow("F1", amount="99")]
        )
        self.assertFalse(report.is_clean)
        self.assertEqual(report.errors[0].code, "DUPLICATE_CONFLICT")


class ReversalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = AuthorizationLedger(
            [
                _auth(
                    period=PeriodGranularity.MONTH,
                    per_transaction_limit="100",
                    period_limit="250",
                )
            ]
        )

    def _reversal(
        self,
        flow_id: str = "R1",
        *,
        day: tuple[int, int, int] = (2026, 1, 12),
        amount: str = "100.00",
        auth_ref: str | None = "AUTH-1",
        reversal_of: str = "F1",
    ) -> Flow:
        return _flow(
            flow_id,
            day=day,
            amount=amount,
            auth_ref=auth_ref,
            kind=FlowType.REVERSAL,
            reversal_of=reversal_of,
        )

    def test_full_reversal_releases_original_period_and_amount(self) -> None:
        self.ledger.process(_flow("F1", day=(2026, 1, 10), amount="100"))
        result = self.ledger.process(self._reversal())
        self.assertEqual(result.status, FlowStatus.REVERSAL)
        self.assertEqual(result.released, Decimal("100.00"))
        self.assertEqual(result.period_consumed, Decimal("0.00"))
        self.assertEqual(result.available, Decimal("250.00"))
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 10)),
            Decimal("0.00"),
        )

    def test_reversal_in_later_period_releases_original_period(self) -> None:
        self.ledger.process(_flow("F1", day=(2026, 1, 31), amount="100"))
        # February flow first consumes the new period
        self.ledger.process(_flow("F2", day=(2026, 2, 2), amount="80"))
        reversal = self._reversal(
            "R1", day=(2026, 2, 3), amount="100"
        )
        result = self.ledger.process(reversal)
        # Release lands on the January period key, not February
        jan_key = PeriodKey(PeriodGranularity.MONTH, 2026, 1)
        feb_key = PeriodKey(PeriodGranularity.MONTH, 2026, 2)
        self.assertEqual(result.period, jan_key)
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 31)),
            Decimal("0.00"),
        )
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 2, 2)),
            Decimal("80.00"),
        )
        self.assertNotEqual(result.period, feb_key)

    def test_partial_reversal_then_full_status(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        self.ledger.process(self._reversal("R1", amount="40"))
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 10)),
            Decimal("60.00"),
        )
        self.ledger.process(self._reversal("R2", amount="60"))
        self.assertEqual(
            self.ledger.consumed("AUTH-1", datetime.date(2026, 1, 10)),
            Decimal("0.00"),
        )

    def test_reversal_cannot_exceed_original_amount(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        self.ledger.process(self._reversal("R1", amount="60"))
        with self.assertRaises(ReversalAmountExceededError):
            self.ledger.process(self._reversal("R2", amount="40.01"))

    def test_orphan_reversal_rejected(self) -> None:
        with self.assertRaises(OrphanReversalError):
            self.ledger.process(self._reversal(reversal_of="GHOST"))

    def test_reversal_currency_must_match(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        reversal = _flow(
            "R1",
            currency="EUR",
            kind=FlowType.REVERSAL,
            reversal_of="F1",
        )
        with self.assertRaises(ReversalCurrencyMismatchError):
            self.ledger.process(reversal)

    def test_reversal_wrong_authorization_rejected(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        with self.assertRaises(ReversalAuthorizationMismatchError):
            self.ledger.process(
                self._reversal(auth_ref="AUTH-OTHER")
            )

    def test_reversal_without_ref_uses_original_authorization(self) -> None:
        self.ledger.process(_flow("F1", amount="100"))
        result = self.ledger.process(
            self._reversal(auth_ref=None)
        )
        self.assertEqual(result.auth_id, "AUTH-1")

    def test_reversal_then_capacity_reusable(self) -> None:
        ledger = AuthorizationLedger(
            [
                _auth(
                    period=PeriodGranularity.MONTH,
                    per_transaction_limit="250",
                    period_limit="250",
                )
            ]
        )
        self.ledger = ledger
        self.ledger.process(_flow("F1", amount="250"))
        with self.assertRaises(PeriodLimitExceededError):
            self.ledger.process(_flow("F2", amount="10"))
        self.ledger.process(self._reversal(amount="250"))
        result = self.ledger.process(_flow("F2", amount="10"))
        self.assertEqual(result.period_consumed, Decimal("10.00"))


class AmendmentBoundaryTestCase(unittest.TestCase):
    def test_consumption_stays_with_version_in_force(self) -> None:
        ledger = AuthorizationLedger([_auth(period_limit="250")])
        ledger.process(_flow("F1", day=(2026, 1, 31), amount="100"))
        ledger.amend_authorization(
            "AUTH-1",
            datetime.date(2026, 2, 1),
            period_limit=Decimal("1000"),
            per_transaction_limit=Decimal("500"),
        )
        # Old period/version keeps its own cap and consumption
        old = ledger.consumed("AUTH-1", datetime.date(2026, 1, 31))
        self.assertEqual(old, Decimal("100.00"))
        result = ledger.process(
            _flow("F2", day=(2026, 2, 1), amount="500")
        )
        self.assertEqual(result.version_seq, 1)
        self.assertEqual(result.period_limit, Decimal("1000"))
        self.assertEqual(result.available, Decimal("500.00"))
        # A flow that the old per-transaction cap would reject is valid now
        self.assertEqual(result.status, FlowStatus.POSTED)

    def test_reversal_after_amendment_releases_old_version(self) -> None:
        ledger = AuthorizationLedger([_auth(period_limit="250")])
        ledger.process(_flow("F1", day=(2026, 1, 31), amount="100"))
        ledger.amend_authorization(
            "AUTH-1",
            datetime.date(2026, 2, 1),
            period_limit=Decimal("1000"),
            per_transaction_limit=Decimal("500"),
        )
        ledger.process(_flow("F2", day=(2026, 2, 5), amount="300"))
        result = ledger.process(
            _flow(
                "R1",
                day=(2026, 2, 6),
                amount="100",
                kind=FlowType.REVERSAL,
                reversal_of="F1",
            )
        )
        self.assertEqual(result.version_seq, 0)
        self.assertEqual(
            result.period, PeriodKey(PeriodGranularity.MONTH, 2026, 1)
        )
        self.assertEqual(
            ledger.consumed("AUTH-1", datetime.date(2026, 1, 31)),
            Decimal("0.00"),
        )
        # New version consumption untouched
        self.assertEqual(
            ledger.consumed("AUTH-1", datetime.date(2026, 2, 5)),
            Decimal("300.00"),
        )

    def test_amendment_cannot_apply_retroactively(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        with self.assertRaises(RegistrationError):
            ledger.amend_authorization(
                "AUTH-1",
                datetime.date(2025, 12, 31),
                period_limit=Decimal("999"),
            )


class ReconcileReportTestCase(unittest.TestCase):
    def test_batch_collects_findings_and_kept_booking(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        flows = [
            _flow("F1", day=(2026, 1, 10), amount="100"),
            _flow("F2", day=(2026, 1, 11), amount="100"),
            _flow("F3", day=(2026, 1, 12), amount="100", auth_ref=None),
            _flow("F4", day=(2026, 1, 13), amount="100", currency="EUR"),
        ]
        report = ledger.reconcile(flows)
        self.assertFalse(report.is_clean)
        codes = {finding.code for finding in report.errors}
        self.assertEqual(
            codes, {"MISSING_AUTH_REF", "CURRENCY_MISMATCH"}
        )
        rejected = {
            result.flow_id: result.status
            for result in report.results
            if result.status is FlowStatus.REJECTED
        }
        self.assertEqual(set(rejected), {"F3", "F4"})
        # Valid flows still booked
        self.assertEqual(
            ledger.consumed("AUTH-1", datetime.date(2026, 1, 11)),
            Decimal("200.00"),
        )

    def test_report_consumption_snapshot_grouped_by_version_period(self) -> None:
        ledger = AuthorizationLedger([_auth(period_limit="250")])
        ledger.amend_authorization(
            "AUTH-1",
            datetime.date(2026, 2, 1),
            period_limit=Decimal("500"),
            per_transaction_limit=Decimal("500"),
        )
        ledger.process(_flow("F1", day=(2026, 1, 31), amount="100"))
        ledger.process(_flow("F2", day=(2026, 2, 1), amount="200"))
        report = ledger.reconcile([])
        self.assertIsInstance(report.consumption[0], AuthorizationConsumption)
        self.assertEqual(len(report.consumption), 2)
        item = report.consumption_for(
            "AUTH-1", datetime.date(2026, 2, 15)
        )
        assert item is not None
        self.assertEqual(item.version_seq, 1)
        self.assertEqual(item.consumed, Decimal("200.00"))
        self.assertEqual(item.available, Decimal("300.00"))
        self.assertEqual(
            report.available_for("AUTH-1", datetime.date(2026, 1, 15)),
            Decimal("150.00"),
        )

    def test_result_for_lookup(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        report = ledger.reconcile([_flow("F1")])
        self.assertEqual(report.result_for("F1").flow_id, "F1")
        with self.assertRaises(KeyError):
            report.result_for("MISSING")

    def test_finding_levels(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        report = ledger.reconcile(
            [_flow("F1"), _flow("F1"), _flow("F2", auth_ref=None)]
        )
        self.assertTrue(
            all(
                finding.level is FindingLevel.ERROR
                for finding in report.errors
            )
        )
        self.assertTrue(
            all(
                finding.level is FindingLevel.WARNING
                for finding in report.warnings
            )
        )

    def test_processing_order_sorted_by_posted_then_id(self) -> None:
        ledger = AuthorizationLedger([_auth()])
        # Submitted out of order; chronological booking still applies
        report = ledger.reconcile(
            [
                _flow("F2", day=(2026, 1, 11), amount="100"),
                _flow("F1", day=(2026, 1, 10), amount="100"),
            ]
        )
        self.assertTrue(report.is_clean)
        self.assertEqual(
            ledger.consumed("AUTH-1", datetime.date(2026, 1, 11)),
            Decimal("200.00"),
        )


class NoSubstitutionTestCase(unittest.TestCase):
    def test_missing_ref_never_consumes_another_authorization(self) -> None:
        ledger = AuthorizationLedger(
            [
                _auth("AUTH-1"),
                _auth(
                    "AUTH-2",
                    period_limit="9999",
                    per_transaction_limit="9999",
                    valid_from=datetime.date(2026, 1, 1),
                ),
            ]
        )
        report = ledger.reconcile([_flow("F1", auth_ref=None, amount="50")])
        self.assertEqual(report.errors[0].code, "MISSING_AUTH_REF")
        for auth_id in ("AUTH-1", "AUTH-2"):
            self.assertEqual(
                ledger.consumed(auth_id, datetime.date(2026, 1, 10)),
                Decimal("0"),
            )


class FlowFromStmttrnTestCase(unittest.TestCase):
    def _stmttrn(self, **kwargs: object) -> STMTTRN:
        defaults: dict[str, object] = dict(
            trntype="DEBIT",
            dtposted=_dt(2026, 1, 10),
            trnamt=Decimal("-433.25"),
            fitid="FIT-1",
            refnum="AUTH-1",
            currency=CURRENCY(currate=Decimal("1"), cursym="USD"),
        )
        defaults.update(kwargs)
        return STMTTRN(**defaults)  # type: ignore[arg-type]

    def test_adapt_debit(self) -> None:
        flow = flow_from_stmttrn(self._stmttrn())
        self.assertEqual(flow.flow_id, "FIT-1")
        self.assertEqual(flow.amount, Decimal("433.25"))
        self.assertEqual(flow.currency, "USD")
        self.assertEqual(flow.auth_ref, "AUTH-1")
        self.assertIs(flow.kind, FlowType.DEBIT)
        self.assertEqual(flow.posted, _dt(2026, 1, 10))

    def test_explicit_auth_ref_overrides_refnum(self) -> None:
        flow = flow_from_stmttrn(self._stmttrn(), auth_ref="AUTH-9")
        self.assertEqual(flow.auth_ref, "AUTH-9")

    def test_reversal_adaptation(self) -> None:
        flow = flow_from_stmttrn(
            self._stmttrn(), reversal_of="FIT-0"
        )
        self.assertIs(flow.kind, FlowType.REVERSAL)
        self.assertEqual(flow.reversal_of, "FIT-0")

    def test_credit_transaction_rejected(self) -> None:
        with self.assertRaises(ValueError):
            flow_from_stmttrn(self._stmttrn(trnamt=Decimal("10")))

    def test_missing_currency_requires_fallback(self) -> None:
        stmttrn = self._stmttrn(currency=None, origcurrency=None)
        with self.assertRaises(ValueError):
            flow_from_stmttrn(stmttrn)
        flow = flow_from_stmttrn(stmttrn, fallback_currency="USD")
        self.assertEqual(flow.currency, "USD")

    def test_origcurrency_used_when_currency_absent(self) -> None:
        from ofxtools.models.i18n import ORIGCURRENCY

        stmttrn = self._stmttrn(
            currency=None,
            origcurrency=ORIGCURRENCY(
                currate=Decimal("0.9"), cursym="EUR"
            ),
        )
        flow = flow_from_stmttrn(stmttrn)
        self.assertEqual(flow.currency, "EUR")

    def test_blank_refnum_becomes_missing_reference(self) -> None:
        flow = flow_from_stmttrn(self._stmttrn(refnum=None))
        self.assertIsNone(flow.auth_ref)

    def test_end_to_end_with_ledger(self) -> None:
        ledger = AuthorizationLedger(
            [
                Authorization(
                    auth_id="AUTH-1",
                    currency="USD",
                    per_transaction_limit=Decimal("500"),
                    period_limit=Decimal("1000"),
                    valid_from=datetime.date(2026, 1, 1),
                )
            ]
        )
        flow = flow_from_stmttrn(self._stmttrn())
        result = ledger.process(flow)
        self.assertEqual(result.status, FlowStatus.POSTED)
        self.assertEqual(
            ledger.consumed("AUTH-1", datetime.date(2026, 1, 10)),
            Decimal("433.25"),
        )


if __name__ == "__main__":
    unittest.main()
