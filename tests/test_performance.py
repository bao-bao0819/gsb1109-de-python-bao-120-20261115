"""Unit tests for ofxtools.performance"""

# stdlib imports
import datetime
import unittest
from decimal import Decimal
from pathlib import Path

from ofxtools.models.bank.stmt import STMTTRN
from ofxtools.models.invest.acct import INVACCTFROM
from ofxtools.models.invest.positions import INVPOS, INVPOSLIST, POSSTOCK
from ofxtools.models.invest.securities import SECID
from ofxtools.models.invest.stmt import INVBAL, INVSTMTRS
from ofxtools.models.invest.transactions import (
    BUYSTOCK,
    INVBANKTRAN,
    INVBUY,
    INVTRAN,
    INVTRANLIST,
    JRNLFUND,
    JRNLSEC,
    TRANSFER,
)

# local imports
from ofxtools.Parser import OFXTree
from ofxtools.performance import (
    CashFlow,
    CashFlowPriority,
    MissingValuationError,
    PerformanceError,
    ValuationPoint,
    build_valuation_point,
    extract_cash_flows,
    extract_valuation_points,
    performance_report,
    segment_returns,
)
from ofxtools.utils import UTC

DATA = Path(__file__).parent / "data" / "invstmtrs.ofx"


def _dt(year: int, month: int, day: int) -> datetime.datetime:
    return datetime.datetime(year, month, day, tzinfo=UTC)


def _secid() -> SECID:
    return SECID(uniqueid="123456789", uniqueidtype="CUSIP")


def _bank_tran(
    fitid: str,
    amount: str,
    dtposted: datetime.datetime,
    trntype: str = "CREDIT",
) -> INVBANKTRAN:
    stmttrn = STMTTRN(
        trntype=trntype,
        dtposted=dtposted,
        trnamt=Decimal(amount),
        fitid=fitid,
    )
    return INVBANKTRAN(stmttrn=stmttrn, subacctfund="CASH")


def _position(
    mktval: str,
    dtpriceasof: datetime.datetime,
    unitprice: str = "100",
) -> POSSTOCK:
    invpos = INVPOS(
        secid=_secid(),
        heldinacct="CASH",
        postype="LONG",
        units=Decimal("100"),
        unitprice=Decimal(unitprice),
        mktval=Decimal(mktval),
        dtpriceasof=dtpriceasof,
    )
    return POSSTOCK(invpos=invpos)


def _statement(
    acctid: str,
    dtasof: datetime.datetime,
    mktval: str,
    cash: str,
    transactions: list | None = None,
    currency: str = "USD",
) -> INVSTMTRS:
    invtranlist = INVTRANLIST(
        dtstart=_dt(2025, 1, 1),
        dtend=dtasof,
        *(transactions or []),
    )
    invposlist = INVPOSLIST(_position(mktval, dtasof))
    invbal = INVBAL(
        availcash=Decimal(cash),
        marginbalance=Decimal("0"),
        shortbalance=Decimal("0"),
    )
    return INVSTMTRS(
        dtasof=dtasof,
        curdef=currency,
        invacctfrom=INVACCTFROM(brokerid="121099999", acctid=acctid),
        invtranlist=invtranlist,
        invposlist=invposlist,
        invbal=invbal,
    )


class CashFlowExtractionTestCase(unittest.TestCase):
    def test_bank_transactions_classify_by_sign(self) -> None:
        statement = _statement(
            "acct1",
            _dt(2026, 1, 31),
            "10000",
            "0",
            transactions=[
                _bank_tran("dep", "1000.00", _dt(2026, 1, 10)),
                _bank_tran(
                    "wd", "-400.00", _dt(2026, 1, 20), trntype="DEBIT"
                ),
            ],
        )
        cash_flows = extract_cash_flows(statement)
        self.assertEqual(["dep", "wd"], [cf.fitid for cf in cash_flows])
        self.assertEqual(
            ["EXTERNAL_IN", "EXTERNAL_OUT"], [cf.kind for cf in cash_flows]
        )
        self.assertTrue(all(cf.external for cf in cash_flows))
        self.assertEqual(cash_flows[1].trntype, "DEBIT")

    def test_security_trades_are_not_external_cash_flows(self) -> None:
        buy = BUYSTOCK(
            invbuy=INVBUY(
                invtran=INVTRAN(fitid="buy1", dttrade=_dt(2026, 1, 5)),
                secid=_secid(),
                units=Decimal("10"),
                unitprice=Decimal("50"),
                total=Decimal("-500"),
                subacctsec="CASH",
                subacctfund="CASH",
            ),
            buytype="BUY",
        )
        statement = _statement(
            "acct1",
            _dt(2026, 1, 31),
            "10000",
            "0",
            transactions=[buy, _bank_tran("dep", "1000", _dt(2026, 1, 10))],
        )
        cash_flows = extract_cash_flows(statement)
        self.assertEqual(["dep"], [cf.fitid for cf in cash_flows])

    def test_journal_entries_are_internal(self) -> None:
        jrnlsec = JRNLSEC(
            invtran=INVTRAN(fitid="js1", dttrade=_dt(2026, 1, 11)),
            secid=_secid(),
            subacctto="MARGIN",
            subacctfrom="CASH",
            units=Decimal("5"),
        )
        jrnlfund = JRNLFUND(
            invtran=INVTRAN(fitid="jf1", dttrade=_dt(2026, 1, 12)),
            subacctto="MARGIN",
            subacctfrom="CASH",
            total=Decimal("-500"),
        )
        statement = _statement(
            "acct1",
            _dt(2026, 1, 31),
            "10000",
            "0",
            transactions=[jrnlsec, jrnlfund],
        )
        cash_flows = extract_cash_flows(statement)
        self.assertEqual({"js1", "jf1"}, {cf.fitid for cf in cash_flows})
        self.assertFalse(any(cf.external for cf in cash_flows))
        self.assertEqual(
            {cf.kind for cf in cash_flows}, {"TRANSFER_IN", "TRANSFER_OUT"}
        )

    def test_transfer_account_reference_marks_internal_legs(self) -> None:
        other_account = INVACCTFROM(brokerid="121099999", acctid="acct2")
        internal_in = TRANSFER(
            invtran=INVTRAN(fitid="tin", dttrade=_dt(2026, 1, 13)),
            secid=_secid(),
            subacctsec="CASH",
            units=Decimal("10"),
            tferaction="IN",
            postype="LONG",
            invacctfrom=other_account,
            unitprice=Decimal("5"),
        )
        external_out = TRANSFER(
            invtran=INVTRAN(fitid="tout", dttrade=_dt(2026, 1, 14)),
            secid=_secid(),
            subacctsec="CASH",
            units=Decimal("10"),
            tferaction="OUT",
            postype="LONG",
            unitprice=Decimal("5"),
        )
        statement = _statement(
            "acct1",
            _dt(2026, 1, 31),
            "10000",
            "0",
            transactions=[external_out, internal_in],
        )
        cash_flows = {cf.fitid: cf for cf in extract_cash_flows(statement)}
        self.assertFalse(cash_flows["tin"].external)
        self.assertTrue(cash_flows["tout"].external)
        self.assertEqual(cash_flows["tin"].amount, Decimal("50"))
        self.assertEqual(cash_flows["tout"].amount, Decimal("-50"))

    def test_external_transfer_without_price_rejected(self) -> None:
        transfer = TRANSFER(
            invtran=INVTRAN(fitid="tin", dttrade=_dt(2026, 1, 13)),
            secid=_secid(),
            subacctsec="CASH",
            units=Decimal("10"),
            tferaction="IN",
            postype="LONG",
        )
        statement = _statement(
            "acct1",
            _dt(2026, 1, 31),
            "10000",
            "0",
            transactions=[transfer],
        )
        with self.assertRaises(ValueError):
            extract_cash_flows(statement)


class PriorityTestCase(unittest.TestCase):
    def test_same_timestamp_uses_rule_not_file_order(self) -> None:
        # File order is inflow then outflow; the default rule must reverse it.
        inflow = CashFlow(
            dtposted=_dt(2026, 1, 15),
            amount=Decimal("100"),
            kind="EXTERNAL_IN",
            fitid="ZIN",
            source="INVBANKTRAN",
        )
        outflow = CashFlow(
            dtposted=_dt(2026, 1, 15),
            amount=Decimal("-100"),
            kind="EXTERNAL_OUT",
            fitid="AOUT",
            source="INVBANKTRAN",
        )
        ordered = CashFlowPriority().order([inflow, outflow])
        self.assertEqual(["AOUT", "ZIN"], [cf.fitid for cf in ordered])

        inflows_first = CashFlowPriority(outflows_first=False)
        ordered = inflows_first.order([outflow, inflow])
        self.assertEqual(["ZIN", "AOUT"], [cf.fitid for cf in ordered])

    def test_ties_within_side_resolve_by_fitid(self) -> None:
        first = CashFlow(
            dtposted=_dt(2026, 1, 15),
            amount=Decimal("-5"),
            kind="EXTERNAL_OUT",
            fitid="B",
            source="INVBANKTRAN",
        )
        second = CashFlow(
            dtposted=_dt(2026, 1, 15),
            amount=Decimal("-9"),
            kind="EXTERNAL_OUT",
            fitid="A",
            source="INVBANKTRAN",
        )
        ordered = CashFlowPriority().order([first, second])
        self.assertEqual(["A", "B"], [cf.fitid for cf in ordered])


class ValuationPointTestCase(unittest.TestCase):
    def test_valuation_sums_positions_and_cash(self) -> None:
        statement = _statement("acct1", _dt(2026, 1, 1), "9900", "200")
        point = build_valuation_point(statement)
        self.assertEqual(point.market_value, Decimal("10100"))
        self.assertEqual(point.cash_balance, Decimal("200"))
        self.assertEqual(len(point.positions), 1)

    def test_same_day_statements_merge_into_one_point(self) -> None:
        statements = [
            _statement("acct1", _dt(2026, 1, 1), "1000", "100"),
            _statement("acct2", _dt(2026, 1, 1), "2000", "200"),
        ]
        points = extract_valuation_points(statements)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].market_value, Decimal("3300"))
        self.assertEqual(points[0].cash_balance, Decimal("300"))

    def test_points_sort_chronologically(self) -> None:
        statements = [
            _statement("acct1", _dt(2026, 2, 1), "2000", "0"),
            _statement("acct1", _dt(2026, 1, 1), "1000", "0"),
        ]
        points = extract_valuation_points(statements)
        self.assertEqual(
            [p.dtasof for p in points],
            [_dt(2026, 1, 1), _dt(2026, 2, 1)],
        )


class SegmentReturnTestCase(unittest.TestCase):
    def _points(self) -> list:

        return [
            ValuationPoint(
                dtasof=_dt(2026, 1, 1),
                market_value=Decimal("10000"),
                cash_balance=Decimal("0"),
            ),
            ValuationPoint(
                dtasof=_dt(2026, 1, 31),
                market_value=Decimal("13000"),
                cash_balance=Decimal("0"),
            ),
        ]

    def test_modified_dietz_formula_single_segment(self) -> None:
        # Contribution at the midpoint (15 of 30 days) carries weight 0.5.
        flow = CashFlow(
            dtposted=_dt(2026, 1, 16),
            amount=Decimal("2000"),
            kind="EXTERNAL_IN",
            fitid="dep",
            source="INVBANKTRAN",
        )
        segments = segment_returns(self._points(), [flow])
        self.assertEqual(len(segments), 1)
        segment = segments[0]
        self.assertEqual(segment.length, _dt(2026, 1, 31) - _dt(2026, 1, 1))
        self.assertEqual(segment.net_external_flow, Decimal("2000"))
        self.assertEqual(segment.contributions, Decimal("2000"))
        self.assertEqual(segment.withdrawals, Decimal("0"))
        self.assertEqual(segment.gain, Decimal("1000"))
        # base = 10000 + 0.5 * 2000 = 11000
        self.assertEqual(segment.return_rate, Decimal("1000") / Decimal("11000"))

    def test_withdrawal_offsets_value_change(self) -> None:

        points = [
            ValuationPoint(
                dtasof=_dt(2026, 1, 1),
                market_value=Decimal("10000"),
                cash_balance=Decimal("0"),
            ),
            ValuationPoint(
                dtasof=_dt(2026, 1, 31),
                market_value=Decimal("9500"),
                cash_balance=Decimal("0"),
            ),
        ]
        flow = CashFlow(
            dtposted=_dt(2026, 1, 16),
            amount=Decimal("-1000"),
            kind="EXTERNAL_OUT",
            fitid="wd",
            source="INVBANKTRAN",
        )
        segment = segment_returns(points, [flow])[0]
        self.assertEqual(segment.withdrawals, Decimal("1000"))
        # gain = 9500 - 10000 - (-1000) = 500
        self.assertEqual(segment.gain, Decimal("500"))

    def test_boundary_flows_follow_next_segment(self) -> None:

        points = [
            ValuationPoint(
                dtasof=_dt(2026, 1, 1),
                market_value=Decimal("10000"),
                cash_balance=Decimal("0"),
            ),
            ValuationPoint(
                dtasof=_dt(2026, 2, 1),
                market_value=Decimal("12000"),
                cash_balance=Decimal("0"),
            ),
            ValuationPoint(
                dtasof=_dt(2026, 3, 1),
                market_value=Decimal("12000"),
                cash_balance=Decimal("0"),
            ),
        ]
        boundary_flow = CashFlow(
            dtposted=_dt(2026, 2, 1),
            amount=Decimal("1000"),
            kind="EXTERNAL_IN",
            fitid="boundary",
            source="INVBANKTRAN",
        )
        closing_flow = CashFlow(
            dtposted=_dt(2026, 3, 1),
            amount=Decimal("500"),
            kind="EXTERNAL_IN",
            fitid="closing",
            source="INVBANKTRAN",
        )
        segments = segment_returns(points, [boundary_flow, closing_flow])
        self.assertEqual(
            [cf.fitid for cf in segments[0].cash_flows], []
        )
        self.assertEqual(
            [cf.fitid for cf in segments[1].cash_flows],
            ["boundary", "closing"],
        )
        # Closing flow lands at segment end: time weight 0, so it only enters
        # the gain identity, not the capital base.
        last = segments[1]
        self.assertEqual(last.net_external_flow, Decimal("1500"))
        self.assertEqual(last.gain, Decimal("12000") - Decimal("12000") - Decimal("1500"))

    def test_zero_capital_base_has_undefined_rate(self) -> None:

        points = [
            ValuationPoint(
                dtasof=_dt(2026, 1, 1),
                market_value=Decimal("0"),
                cash_balance=Decimal("0"),
            ),
            ValuationPoint(
                dtasof=_dt(2026, 1, 31),
                market_value=Decimal("0"),
                cash_balance=Decimal("0"),
            ),
        ]
        segment = segment_returns(points, [])[0]
        self.assertIsNone(segment.return_rate)
        self.assertEqual(segment.gain, Decimal("0"))

    def test_missing_valuation_endpoint_raises(self) -> None:

        point = ValuationPoint(
            dtasof=_dt(2026, 1, 1),
            market_value=Decimal("100"),
            cash_balance=Decimal("0"),
        )
        with self.assertRaises(MissingValuationError):
            segment_returns([point], [])


class PerformanceReportTestCase(unittest.TestCase):
    def test_full_report_reconciles_value_change(self) -> None:
        opening = _statement("acct1", _dt(2026, 1, 1), "10000", "0")
        middle = _statement(
            "acct1",
            _dt(2026, 1, 16),
            "11000",
            "0",
            transactions=[
                _bank_tran("dep", "2000", _dt(2026, 1, 10)),
                _bank_tran("wd", "-500", _dt(2026, 1, 12), trntype="DEBIT"),
            ],
        )
        closing = _statement(
            "acct1",
            _dt(2026, 1, 31),
            "13000",
            "0",
            transactions=[
                _bank_tran("dep2", "1000", _dt(2026, 1, 20)),
            ],
        )
        report = performance_report([opening, middle, closing])
        self.assertEqual(len(report.segments), 2)
        self.assertEqual(report.total_contributions, Decimal("3000"))
        self.assertEqual(report.total_withdrawals, Decimal("500"))
        self.assertEqual(report.total_external_flow, Decimal("2500"))
        # ending - beginning - net flows = 13000 - 10000 - 2500
        self.assertEqual(report.total_gain, Decimal("500"))
        self.assertEqual(
            report.ending_value - report.beginning_value,
            report.total_external_flow + report.total_gain,
        )
        self.assertIsNotNone(report.total_return)

    def test_internal_legs_excluded_from_combined_report(self) -> None:
        transfer_out = TRANSFER(
            invtran=INVTRAN(fitid="legout", dttrade=_dt(2026, 1, 10)),
            secid=_secid(),
            subacctsec="CASH",
            units=Decimal("10"),
            tferaction="OUT",
            postype="LONG",
            invacctfrom=INVACCTFROM(brokerid="121099999", acctid="acct2"),
            unitprice=Decimal("5"),
        )
        transfer_in = TRANSFER(
            invtran=INVTRAN(fitid="legin", dttrade=_dt(2026, 1, 10)),
            secid=_secid(),
            subacctsec="CASH",
            units=Decimal("10"),
            tferaction="IN",
            postype="LONG",
            invacctfrom=INVACCTFROM(brokerid="121099999", acctid="acct1"),
            unitprice=Decimal("5"),
        )
        opening = [
            _statement("acct1", _dt(2026, 1, 1), "1000", "0"),
            _statement("acct2", _dt(2026, 1, 1), "2000", "0"),
        ]
        closing = [
            _statement(
                "acct1",
                _dt(2026, 1, 31),
                "1500",
                "0",
                transactions=[transfer_in],
            ),
            _statement(
                "acct2",
                _dt(2026, 1, 31),
                "1500",
                "0",
                transactions=[transfer_out],
            ),
        ]
        report = performance_report(opening + closing)
        self.assertEqual(report.account_ids, ("acct1", "acct2"))
        self.assertEqual(report.total_external_flow, Decimal("0"))
        self.assertEqual(report.internal_flows_excluded, 2)
        # No external money moved: all value change is market movement.
        self.assertEqual(report.total_gain, Decimal("3000") - Decimal("3000"))

    def test_flow_outside_valuation_window_raises(self) -> None:
        opening = _statement("acct1", _dt(2026, 1, 1), "1000", "0")
        closing = _statement(
            "acct1",
            _dt(2026, 1, 31),
            "2000",
            "0",
            transactions=[
                _bank_tran("late", "100", _dt(2025, 12, 31)),
            ],
        )
        with self.assertRaises(MissingValuationError):
            performance_report([opening, closing])

    def test_no_statements_raises(self) -> None:
        with self.assertRaises(MissingValuationError):
            performance_report([])

    def test_mixed_currency_statements_rejected(self) -> None:
        opening = [
            _statement("acct1", _dt(2026, 1, 1), "1000", "0", currency="USD"),
            _statement("acct2", _dt(2026, 1, 1), "1000", "0", currency="EUR"),
        ]
        closing = [
            _statement("acct1", _dt(2026, 1, 31), "1100", "0", currency="USD"),
            _statement("acct2", _dt(2026, 1, 31), "1100", "0", currency="EUR"),
        ]
        with self.assertRaises(PerformanceError):
            performance_report(opening + closing)


class ParserIntegrationTestCase(unittest.TestCase):
    def test_sample_ofx_extracts_deposit_and_valuation(self) -> None:
        tree = OFXTree()
        tree.parse(str(DATA))
        ofx = tree.convert()
        statement = ofx.invstmtmsgsrsv1.statements[0]

        cash_flows = extract_cash_flows(statement)
        self.assertEqual(len(cash_flows), 1)
        self.assertEqual(cash_flows[0].fitid, "12345")
        self.assertEqual(cash_flows[0].amount, Decimal("1000.00"))
        self.assertEqual(cash_flows[0].kind, "EXTERNAL_IN")
        # The BUYSTOCK leg must not be treated as an external flow.
        self.assertEqual(
            [cf.source for cf in cash_flows], ["INVBANKTRAN"]
        )

        point = build_valuation_point(statement)
        # 9900 (stock) + 500 (option) + 200 (avail cash)
        self.assertEqual(point.market_value, Decimal("10600.00"))

        with self.assertRaises(MissingValuationError):
            performance_report([statement])


if __name__ == "__main__":
    unittest.main()
