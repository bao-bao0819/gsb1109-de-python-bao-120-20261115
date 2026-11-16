"""Unit tests for ofxtools.ledger"""

# stdlib imports
import datetime
import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path

# local imports
from ofxtools.ledger import (
    DuplicateEvent,
    EventType,
    IncompleteSettlement,
    OverSettlement,
    SettlementEvent,
    SettlementLedger,
    SettlementRule,
    SettlementRules,
    SettlementStatus,
    Snapshot,
    TerminalSettlement,
    UnknownReference,
    event_from_dict,
    transactions_from_statement,
)
from ofxtools.Parser import OFXTree

D = datetime.date
DATA = Path(__file__).parent / "data"


def trade(
    fitid="T1",
    date=D(2024, 6, 3),
    units="100",
    amount="-5025",
    security="123",
    account="A",
    currency="USD",
    settlement=None,
    known=None,
    cycle=1,
):
    return SettlementEvent(
        fitid=fitid,
        effective_date=date,
        event_type=EventType.TRADE,
        known_date=known,
        security_id=security,
        security_type="CUSIP",
        account=account,
        currency=currency,
        units=Decimal(units),
        amount=Decimal(amount),
        settlement_date=settlement,
        settlement_cycle=cycle,
    )


def piece(fitid, reference, date, units, amount, kind=EventType.PARTIAL, account="A"):
    return SettlementEvent(
        fitid=fitid,
        effective_date=date,
        event_type=kind,
        reference=reference,
        security_id="123",
        account=account,
        currency="USD",
        units=Decimal(units),
        amount=Decimal(amount),
    )


class TradeDateVersusSettlementDateTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = SettlementLedger()
        self.ledger.record(trade())

    def test_trade_date_books(self):
        snap = self.ledger.as_of(D(2024, 6, 3))
        self.assertEqual(snap.held("A", "123"), Decimal("100"))
        self.assertEqual(snap.available("A", "123"), Decimal("0"))
        self.assertEqual(snap.pending_units("A", "123"), Decimal("100"))
        self.assertEqual(snap.settled_cash("A", "USD"), Decimal("0"))
        self.assertEqual(snap.pending_cash("A", "USD"), Decimal("-5025"))

    def test_settlement_date_books(self):
        snap = self.ledger.as_of(D(2024, 6, 4))
        self.assertEqual(snap.held("A", "123"), Decimal("100"))
        self.assertEqual(snap.available("A", "123"), Decimal("100"))
        self.assertEqual(snap.pending_units("A", "123"), Decimal("0"))
        self.assertEqual(snap.settled_cash("A", "USD"), Decimal("-5025"))
        self.assertEqual(snap.pending_cash("A", "USD"), Decimal("0"))

    def test_explicit_settlement_date_wins(self):
        ledger = SettlementLedger()
        ledger.record(trade(settlement=D(2024, 6, 10)))
        self.assertEqual(
            ledger.available(D(2024, 6, 4))[("A", "123")], Decimal("0")
        )
        self.assertEqual(
            ledger.available(D(2024, 6, 10))[("A", "123")], Decimal("100")
        )

    def test_debt_settles_t_plus_2(self):
        ledger = SettlementLedger()
        ledger.record(trade(cycle=2))
        self.assertEqual(
            ledger.available(D(2024, 6, 4))[("A", "123")], Decimal("0")
        )
        self.assertEqual(
            ledger.available(D(2024, 6, 5))[("A", "123")], Decimal("100")
        )

    def test_three_book_views_differ_on_same_date(self):
        snap = self.ledger.as_of(D(2024, 6, 3))
        self.assertNotEqual(
            snap.held("A", "123"), snap.available("A", "123")
        )
        self.assertEqual(snap.held("A", "123"), Decimal("100"))
        self.assertEqual(snap.available("A", "123"), Decimal("0"))


class SellTestCase(unittest.TestCase):
    def test_sell_reduces_holdings_and_available_at_trade_date(self):
        ledger = SettlementLedger()
        ledger.record(trade(fitid="B", date=D(2024, 6, 1)))
        ledger.record(
            trade(
                fitid="S",
                date=D(2024, 6, 5),
                units="-40",
                amount="2000",
            )
        )
        snap = ledger.as_of(D(2024, 6, 5))
        self.assertEqual(snap.held("A", "123"), Decimal("60"))
        self.assertEqual(snap.available("A", "123"), Decimal("60"))
        self.assertEqual(snap.pending_cash("A", "USD"), Decimal("2000"))
        self.assertEqual(
            ledger.as_of(D(2024, 6, 6)).settled_cash("A", "USD"), Decimal("-3025")
        )


class PartialSettlementTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = SettlementLedger()
        self.ledger.record(trade())

    def test_partial_piece_links_to_original_trade(self):
        self.ledger.record(piece("P1", "T1", D(2024, 6, 4), "40", "-2010"))
        record = self.ledger.trade_records(D(2024, 6, 4))[0]
        self.assertEqual(record.status, SettlementStatus.PARTIAL)
        self.assertEqual(record.settled_units, Decimal("40"))
        self.assertEqual(record.remaining_units, Decimal("60"))
        self.assertEqual(record.pieces, ("P1",))
        self.assertEqual(
            [event.fitid for event in self.ledger.settlement_pieces("T1")], ["P1"]
        )

    def test_partial_pieces_complete_trade(self):
        self.ledger.record(piece("P1", "T1", D(2024, 6, 4), "40", "-2010"))
        self.ledger.record(piece("P2", "T1", D(2024, 6, 5), "60", "-3015"))
        snap = self.ledger.as_of(D(2024, 6, 5))
        self.assertEqual(snap.available("A", "123"), Decimal("100"))
        record = self.ledger.trade_records(D(2024, 6, 5))[0]
        self.assertEqual(record.status, SettlementStatus.SETTLED)
        self.assertEqual(record.pieces, ("P1", "P2"))

    def test_automatic_settlement_suppressed_by_pieces(self):
        self.ledger.record(piece("P1", "T1", D(2024, 6, 4), "40", "-2010"))
        # The contractual settlement date has passed, but only the explicit
        # piece may settle.
        snap = self.ledger.as_of(D(2024, 6, 30))
        self.assertEqual(snap.available("A", "123"), Decimal("40"))
        self.assertEqual(snap.pending_cash("A", "USD"), Decimal("-3015"))

    def test_over_settlement_rejected(self):
        self.ledger.record(piece("P1", "T1", D(2024, 6, 4), "40", "-2010"))
        with self.assertRaises(OverSettlement):
            self.ledger.record(piece("P2", "T1", D(2024, 6, 5), "70", "-3015"))

    def test_full_settle_requires_remaining_quantity(self):
        with self.assertRaises(IncompleteSettlement):
            self.ledger.record(
                piece("S1", "T1", D(2024, 6, 4), "40", "-2010", EventType.SETTLE)
            )

    def test_unknown_reference_rejected(self):
        with self.assertRaises(UnknownReference):
            self.ledger.record(piece("P1", "MISSING", D(2024, 6, 4), "40", "-2010"))


class FailedSettlementTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = SettlementLedger()
        self.ledger.record(trade())
        self.ledger.record(piece("P1", "T1", D(2024, 6, 4), "40", "-2010"))

    def test_failure_does_not_reverse_trade(self):
        self.ledger.record(
            piece("F1", "T1", D(2024, 6, 5), "60", "-3015", EventType.FAIL)
        )
        snap = self.ledger.as_of(D(2024, 6, 5))
        self.assertEqual(snap.held("A", "123"), Decimal("100"))
        self.assertEqual(snap.available("A", "123"), Decimal("40"))
        self.assertEqual(snap.pending_units("A", "123"), Decimal("60"))
        self.assertEqual(snap.pending_cash("A", "USD"), Decimal("-3015"))

    def test_failure_keeps_obligation_open_for_retry(self):
        self.ledger.record(
            piece("F1", "T1", D(2024, 6, 5), "60", "-3015", EventType.FAIL)
        )
        record = self.ledger.trade_records(D(2024, 6, 5))[0]
        self.assertEqual(record.status, SettlementStatus.FAILED)
        self.assertEqual(record.failed_units, Decimal("60"))
        self.assertEqual(record.remaining_units, Decimal("60"))
        # A later successful piece for the same quantity is allowed.
        self.ledger.record(piece("S1", "T1", D(2024, 6, 6), "60", "-3015"))
        snap = self.ledger.as_of(D(2024, 6, 6))
        self.assertEqual(snap.held("A", "123"), Decimal("100"))
        self.assertEqual(snap.available("A", "123"), Decimal("100"))
        self.assertEqual(snap.pending_cash("A", "USD"), Decimal("0"))
        record = self.ledger.trade_records(D(2024, 6, 6))[0]
        self.assertEqual(record.status, SettlementStatus.SETTLED)
        self.assertEqual(record.failed_units, Decimal("60"))

    def test_failure_is_distinct_from_cancellation(self):
        self.ledger.record(
            piece("F1", "T1", D(2024, 6, 5), "60", "-3015", EventType.FAIL)
        )
        self.assertNotEqual(
            self.ledger.as_of(D(2024, 6, 5)).held("A", "123"), Decimal("0")
        )
        cancel = piece(
            "C1", "T1", D(2024, 6, 6), "100", "-5025", EventType.CANCEL
        )
        self.ledger.record(cancel)
        snap = self.ledger.as_of(D(2024, 6, 6))
        self.assertEqual(snap.held("A", "123"), Decimal("0"))
        self.assertEqual(snap.available("A", "123"), Decimal("0"))

    def test_failed_amount_cannot_exceed_open_obligation(self):
        with self.assertRaises(OverSettlement):
            self.ledger.record(
                piece("F1", "T1", D(2024, 6, 5), "99", "-9000", EventType.FAIL)
            )


class CancellationTestCase(unittest.TestCase):
    def test_cancel_reverses_all_books(self):
        ledger = SettlementLedger()
        ledger.record(trade())
        ledger.record(piece("P1", "T1", D(2024, 6, 4), "40", "-2010"))
        ledger.record(
            piece("C1", "T1", D(2024, 6, 5), "100", "-5025", EventType.CANCEL)
        )
        before = ledger.as_of(D(2024, 6, 4))
        self.assertEqual(before.held("A", "123"), Decimal("100"))
        self.assertEqual(before.available("A", "123"), Decimal("40"))
        after = ledger.as_of(D(2024, 6, 5))
        self.assertEqual(after.held("A", "123"), Decimal("0"))
        self.assertEqual(after.available("A", "123"), Decimal("0"))
        self.assertEqual(after.settled_cash("A", "USD"), Decimal("0"))
        self.assertEqual(after.pending_cash("A", "USD"), Decimal("0"))

    def test_piece_after_cancel_rejected(self):
        ledger = SettlementLedger()
        ledger.record(trade())
        ledger.record(
            piece("C1", "T1", D(2024, 6, 5), "100", "-5025", EventType.CANCEL)
        )
        with self.assertRaises(TerminalSettlement):
            ledger.record(piece("P1", "T1", D(2024, 6, 6), "10", "-500"))

    def test_double_cancel_rejected(self):
        ledger = SettlementLedger()
        ledger.record(trade())
        ledger.record(
            piece("C1", "T1", D(2024, 6, 5), "100", "-5025", EventType.CANCEL)
        )
        with self.assertRaises(TerminalSettlement):
            ledger.record(
                piece("C2", "T1", D(2024, 6, 6), "100", "-5025", EventType.CANCEL)
            )

    def test_cancel_before_trade_rejected(self):
        ledger = SettlementLedger()
        ledger.record(trade())
        with self.assertRaises(ValueError):
            ledger.record(
                piece("C1", "T1", D(2024, 6, 2), "100", "-5025", EventType.CANCEL)
            )


class LateConfirmationTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = SettlementLedger()
        self.ledger.record(trade(known=D(2024, 6, 10)))

    def test_late_event_hidden_before_knowledge_date(self):
        before = self.ledger.as_of(D(2024, 6, 4), D(2024, 6, 4))
        self.assertEqual(before.held("A", "123"), Decimal("0"))
        self.assertEqual(before.settled_cash("A", "USD"), Decimal("0"))

    def test_late_event_visible_after_knowledge_date(self):
        after = self.ledger.as_of(D(2024, 6, 4), D(2024, 6, 10))
        self.assertEqual(after.held("A", "123"), Decimal("100"))
        self.assertEqual(after.available("A", "123"), Decimal("100"))
        self.assertEqual(after.settled_cash("A", "USD"), Decimal("-5025"))

    def test_effective_date_preserved(self):
        event = self.ledger.events[0]
        self.assertEqual(event.effective_date, D(2024, 6, 3))
        self.assertEqual(event.known_date, D(2024, 6, 10))

    def test_earliest_knowledge_date_wins_on_repeat(self):
        self.ledger.record(trade(known=D(2024, 6, 8)))
        self.assertEqual(self.ledger.events[0].known_date, D(2024, 6, 8))

    def test_snapshot_records_both_cutoffs(self):
        snap = self.ledger.as_of(D(2024, 6, 4), D(2024, 6, 10))
        self.assertIsInstance(snap, Snapshot)
        self.assertEqual(snap.as_of, D(2024, 6, 4))
        self.assertEqual(snap.known_as_of, D(2024, 6, 10))


class CrossDayQueryTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = SettlementLedger()
        self.ledger.record(trade())

    def test_each_book_uses_its_own_time_point(self):
        # On the trade date the stock is owned but not deliverable, and the
        # cash is committed but not spent.
        trade_day = self.ledger.as_of(D(2024, 6, 3))
        self.assertEqual(trade_day.held("A", "123"), Decimal("100"))
        self.assertEqual(trade_day.available("A", "123"), Decimal("0"))
        self.assertEqual(trade_day.pending_cash("A", "USD"), Decimal("-5025"))
        # On the settlement date both settle-date books have moved.
        settle_day = self.ledger.as_of(D(2024, 6, 4))
        self.assertEqual(settle_day.available("A", "123"), Decimal("100"))
        self.assertEqual(settle_day.settled_cash("A", "USD"), Decimal("-5025"))
        self.assertEqual(settle_day.pending_cash("A", "USD"), Decimal("0"))

    def test_holdings_available_and_pending_helpers(self):
        self.assertEqual(
            self.ledger.holdings(D(2024, 6, 3))[("A", "123")], Decimal("100")
        )
        self.assertEqual(
            self.ledger.available(D(2024, 6, 3))[("A", "123")], Decimal("0")
        )
        self.assertEqual(
            self.ledger.pending_cash(D(2024, 6, 3))[("A", "USD")], Decimal("-5025")
        )


class IngestTestCase(unittest.TestCase):
    def test_duplicate_event_is_idempotent(self):
        ledger = SettlementLedger()
        ledger.record(trade())
        ledger.record(trade())
        self.assertEqual(len(ledger), 1)

    def test_conflicting_duplicate_rejected_in_strict_mode(self):
        ledger = SettlementLedger(strict=True)
        ledger.record(trade())
        with self.assertRaises(DuplicateEvent):
            ledger.record(trade(units="50", amount="-2500"))

    def test_conflicting_duplicate_replaces_by_default(self):
        ledger = SettlementLedger()
        ledger.record(trade())
        ledger.record(trade(units="50", amount="-2500"))
        self.assertEqual(len(ledger), 1)
        self.assertEqual(
            ledger.as_of(D(2024, 6, 3)).held("A", "123"), Decimal("50")
        )

    def test_datetimes_are_normalized_to_dates(self):
        event = SettlementEvent(
            fitid="X",
            effective_date=datetime.datetime(2024, 6, 3, 10, 30),
            event_type=EventType.TRADE,
            units=Decimal("1"),
            amount=Decimal("-1"),
        )
        self.assertEqual(event.effective_date, D(2024, 6, 3))
        self.assertEqual(event.known_date, D(2024, 6, 3))
        self.assertIsInstance(event.effective_date, datetime.date)
        self.assertNotIsInstance(event.effective_date, datetime.datetime)


class OfxIntegrationTestCase(unittest.TestCase):
    def setUp(self):
        tree = OFXTree()
        tree.parse(io.BytesIO((DATA / "invstmtrs.ofx").read_bytes()))
        self.ofx = tree.convert()

    def test_statement_events(self):
        statement = self.ofx.statements[0]
        events = transactions_from_statement(statement)
        self.assertEqual([event.fitid for event in events], ["23321", "12345"])
        buy = events[0]
        self.assertEqual(buy.event_type, EventType.TRADE)
        self.assertEqual(buy.trade_date, D(2005, 8, 25))
        self.assertEqual(buy.settlement_date, D(2005, 8, 28))
        self.assertEqual(buy.units, Decimal("100"))
        self.assertEqual(buy.amount, Decimal("-5025.00"))
        self.assertEqual(buy.account, "999988")

    def test_ledger_from_ofx(self):
        ledger = SettlementLedger.from_ofx(self.ofx)
        before = ledger.as_of(D(2005, 8, 25))
        self.assertEqual(before.held("999988", "123456789"), Decimal("100"))
        self.assertEqual(before.available("999988", "123456789"), Decimal("0"))
        self.assertEqual(before.settled_cash("999988", "USD"), Decimal("1000.00"))
        self.assertEqual(before.pending_cash("999988", "USD"), Decimal("-5025.00"))
        after = ledger.as_of(D(2005, 8, 28))
        self.assertEqual(after.available("999988", "123456789"), Decimal("100"))
        self.assertEqual(after.settled_cash("999988", "USD"), Decimal("-4025.00"))
        self.assertEqual(after.pending_cash("999988", "USD"), Decimal("0"))

    def test_ofx_known_dates(self):
        ledger = SettlementLedger.from_ofx(
            self.ofx, known_dates={"23321": D(2005, 9, 1)}
        )
        self.assertEqual(
            ledger.as_of(D(2005, 8, 28), D(2005, 8, 28)).held(
                "999988", "123456789"
            ),
            Decimal("0"),
        )
        self.assertEqual(
            ledger.as_of(D(2005, 8, 28), D(2005, 9, 1)).held(
                "999988", "123456789"
            ),
            Decimal("100"),
        )


class CliTestCase(unittest.TestCase):
    def _run(self, *args):
        from ofxtools.scripts.ofxledger import main

        out = io.StringIO()
        with redirect_stdout(out):
            main(list(args))
        return out.getvalue()

    def test_asof(self):
        output = self._run(
            str(DATA / "invstmtrs.ofx"), "asof", "--date", "20050825"
        )
        self.assertIn("Equity holdings", output)
        self.assertIn("to receive", output)
        self.assertIn("-5025", output)

    def test_trades_json(self):
        import json

        output = self._run(
            str(DATA / "invstmtrs.ofx"), "trades", "--date", "20050828", "--json"
        )
        records = json.loads(output)
        self.assertEqual(records[0]["status"], "SETTLED")
        self.assertEqual(records[0]["settled_units"], "100")

    def test_pending(self):
        output = self._run(
            str(DATA / "invstmtrs.ofx"), "pending", "--date", "20050826", "--json"
        )
        import json

        records = json.loads(output)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "PENDING")


class SettlementRulesTestCase(unittest.TestCase):
    def test_default_rules(self):
        rules = SettlementRules()
        self.assertEqual(rules.cycle("BUYSTOCK", "CUSIP"), 1)
        self.assertEqual(rules.cycle("BUYDEBT", "CUSIP"), 2)
        self.assertEqual(rules.cycle("SELLDEBT", None), 2)

    def test_rule_order_decides_precedence(self):
        rules = SettlementRules(
            [
                SettlementRule(3, ("MUNICIPAL",)),
                SettlementRule(2, ("CUSIP",)),
                SettlementRule(1),
            ]
        )
        self.assertEqual(rules.cycle("BUYDEBT", "MUNICIPAL"), 3)
        self.assertEqual(rules.cycle("BUYDEBT", "CUSIP"), 2)
        self.assertEqual(rules.cycle("BUYSTOCK", "ISIN"), 1)

    def test_from_mapping(self):
        rules = SettlementRules.from_mapping({"DEBT": 3}, default=2)
        self.assertEqual(rules.cycle("BUYDEBT"), 3)
        self.assertEqual(rules.cycle("BUYSTOCK"), 2)

    def test_custom_rule_changes_derived_date(self):
        statement = _buy_statement()
        ledger = SettlementLedger.from_statements(
            [statement], rules=SettlementRules.from_mapping({"CUSIP": 2})
        )
        self.assertEqual(
            ledger.available(D(2024, 6, 4))[("A", "123")], Decimal("0")
        )
        self.assertEqual(
            ledger.available(D(2024, 6, 5))[("A", "123")], Decimal("100")
        )

    def test_unknown_rule_target_raises(self):
        rules = SettlementRules([SettlementRule(1, ("AAAA",))])
        with self.assertRaises(ValueError):
            rules.cycle("BBBB")


def _buy_statement(fitid="T1"):
    from ofxtools import models
    from ofxtools.utils import UTC

    return models.INVSTMTRS(
        dtasof=datetime.datetime(2024, 6, 5, tzinfo=UTC),
        curdef="USD",
        invacctfrom=models.INVACCTFROM(brokerid="b", acctid="A"),
        invtranlist=models.INVTRANLIST(
            models.BUYSTOCK(
                invbuy=models.INVBUY(
                    invtran=models.INVTRAN(
                        fitid=fitid,
                        dttrade=datetime.datetime(2024, 6, 3, tzinfo=UTC),
                    ),
                    secid=models.SECID(uniqueid="123", uniqueidtype="CUSIP"),
                    units=Decimal("100"),
                    unitprice=Decimal("50"),
                    total=Decimal("-5025"),
                    subacctsec="CASH",
                    subacctfund="CASH",
                ),
                buytype="BUY",
            ),
            dtstart=datetime.datetime(2024, 6, 1, tzinfo=UTC),
            dtend=datetime.datetime(2024, 6, 5, tzinfo=UTC),
        ),
    )


class EventFromDictTestCase(unittest.TestCase):
    def test_dates_and_decimals(self):
        event = event_from_dict(
            {
                "fitid": "P1",
                "effective_date": "2024-06-04",
                "known_date": "20240605",
                "event_type": "partial",
                "reference": "T1",
                "units": "40",
                "amount": "-2010",
            }
        )
        self.assertEqual(event.event_type, EventType.PARTIAL)
        self.assertEqual(event.effective_date, D(2024, 6, 4))
        self.assertEqual(event.known_date, D(2024, 6, 5))
        self.assertEqual(event.units, Decimal("40"))
        self.assertEqual(event.amount, Decimal("-2010"))

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            event_from_dict(
                {
                    "fitid": "P1",
                    "effective_date": "2024-06-04",
                    "event_type": "PARTIAL",
                    "reference": "T1",
                    "units": "40",
                    "amount": "-2010",
                    "bogus": 1,
                }
            )


class OfxReversalTestCase(unittest.TestCase):
    def _statement(self, transactions, curdef="USD"):
        from ofxtools import models
        from ofxtools.utils import UTC

        return models.INVSTMTRS(
            dtasof=datetime.datetime(2024, 6, 5, tzinfo=UTC),
            curdef=curdef,
            invacctfrom=models.INVACCTFROM(brokerid="b", acctid="A"),
            invtranlist=models.INVTRANLIST(
                *transactions,
                dtstart=datetime.datetime(2024, 6, 1, tzinfo=UTC),
                dtend=datetime.datetime(2024, 6, 5, tzinfo=UTC),
            ),
        )

    def _buy(self, fitid="T1", units="100", total="-5025"):
        from ofxtools import models
        from ofxtools.utils import UTC

        return models.BUYSTOCK(
            invbuy=models.INVBUY(
                invtran=models.INVTRAN(
                    fitid=fitid,
                    dttrade=datetime.datetime(2024, 6, 3, tzinfo=UTC),
                ),
                secid=models.SECID(uniqueid="123", uniqueidtype="CUSIP"),
                units=Decimal(units),
                unitprice=Decimal("50"),
                total=Decimal(total),
                subacctsec="CASH",
                subacctfund="CASH",
            ),
            buytype="BUY",
        )

    def _reversal(self, fitid="R1", reference="T1", units="-100", total="5025"):
        from ofxtools import models
        from ofxtools.utils import UTC

        return models.SELLSTOCK(
            invsell=models.INVSELL(
                invtran=models.INVTRAN(
                    fitid=fitid,
                    dttrade=datetime.datetime(2024, 6, 5, tzinfo=UTC),
                    reversalfitid=reference,
                ),
                secid=models.SECID(uniqueid="123", uniqueidtype="CUSIP"),
                units=Decimal(units),
                unitprice=Decimal("50"),
                total=Decimal(total),
                subacctsec="CASH",
                subacctfund="CASH",
            ),
            selltype="SELL",
        )

    def test_reversalfitid_becomes_cancel(self):
        statement = self._statement([self._buy(), self._reversal()])
        events = transactions_from_statement(statement)
        self.assertEqual(events[0].event_type, EventType.TRADE)
        self.assertEqual(events[1].event_type, EventType.CANCEL)
        self.assertEqual(events[1].reference, "T1")

    def test_reversal_and_trade_are_symmetric(self):
        statement = self._statement([self._buy(), self._reversal()])
        ledger = SettlementLedger.from_statements([statement])
        before = ledger.as_of(D(2024, 6, 4))
        self.assertEqual(before.held("A", "123"), Decimal("100"))
        after = ledger.as_of(D(2024, 6, 5))
        self.assertEqual(after.held("A", "123"), Decimal("0"))
        self.assertEqual(after.settled_cash("A", "USD"), Decimal("0"))
        self.assertEqual(after.pending_cash("A", "USD"), Decimal("0"))

    def test_reversal_keeps_its_own_fitid(self):
        statement = self._statement([self._buy(), self._reversal()])
        ledger = SettlementLedger.from_statements([statement])
        self.assertEqual([event.fitid for event in ledger.events], ["T1", "R1"])


class OfxIncomeTestCase(unittest.TestCase):
    def test_income_posts_settled_cash(self):
        from ofxtools import models
        from ofxtools.utils import UTC

        income = models.INCOME(
            invtran=models.INVTRAN(
                fitid="I1", dttrade=datetime.datetime(2024, 6, 3, tzinfo=UTC)
            ),
            secid=models.SECID(uniqueid="123", uniqueidtype="CUSIP"),
            incometype="DIV",
            total=Decimal("25"),
            subacctsec="CASH",
            subacctfund="CASH",
        )
        statement = models.INVSTMTRS(
            dtasof=datetime.datetime(2024, 6, 5, tzinfo=UTC),
            curdef="USD",
            invacctfrom=models.INVACCTFROM(brokerid="b", acctid="A"),
            invtranlist=models.INVTRANLIST(
                income,
                dtstart=datetime.datetime(2024, 6, 1, tzinfo=UTC),
                dtend=datetime.datetime(2024, 6, 5, tzinfo=UTC),
            ),
        )
        ledger = SettlementLedger.from_statements([statement])
        snap = ledger.as_of(D(2024, 6, 3))
        self.assertEqual(snap.settled_cash("A", "USD"), Decimal("25"))
        self.assertEqual(snap.pending_cash("A", "USD"), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
