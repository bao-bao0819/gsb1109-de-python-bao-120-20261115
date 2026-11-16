"""Unit tests for ``ofxtools.bonds``"""

from __future__ import annotations

import datetime
import unittest
from decimal import Decimal

from ofxtools.bonds import (
    BondBook,
    BusinessDayConvention,
    CostConvention,
    CouponFrequency,
    CouponSchedule,
    CouponTerms,
    DayCount,
    InterestLedger,
    LedgerError,
    MissingAccruedInterestError,
    MissingFieldError,
    ReconciliationError,
    ReversalError,
    SecurityCostLedger,
    allocate_by_quantity,
    money,
    parse_debt_trade,
    roll_business_day,
)
from ofxtools.models.i18n import CURRENCY
from ofxtools.models.invest.securities import SECID
from ofxtools.models.invest.transactions import (
    BUYDEBT,
    INVBUY,
    INVSELL,
    INVTRAN,
    SELLDEBT,
)
from ofxtools.utils import UTC

D = Decimal
PAR = D("1000")
TRADE_DATE = datetime.datetime(2024, 3, 1, tzinfo=UTC)
SETTLE_DATE = datetime.datetime(2024, 4, 1, tzinfo=UTC)


def make_buy(
    *,
    fitid="BUY-1",
    units="100",
    unitprice="98.25",
    total=None,
    accrdint=None,
    currency="USD",
    dttrade=TRADE_DATE,
    dtsettle=SETTLE_DATE,
    fees="5.00",
    commission="10.00",
):
    if total is None:
        clean = D(units) * D(unitprice) * PAR / D(100)
        total = -(clean + D(fees) + D(commission) + (D(accrdint) if accrdint else D(0)))
    invtran = INVTRAN(fitid=fitid, dttrade=dttrade, dtsettle=dtsettle)
    return BUYDEBT(
        invbuy=INVBUY(
            invtran=invtran,
            secid=SECID(uniqueid="123456789", uniqueidtype="CUSIP"),
            units=D(units),
            unitprice=D(unitprice),
            commission=D(commission),
            taxes=D("0"),
            fees=D(fees),
            total=D(str(total)),
            currency=CURRENCY(currate=D("1"), cursym=currency),
            subacctsec="CASH",
            subacctfund="CASH",
        ),
        accrdint=None if accrdint is None else D(str(accrdint)),
    )


def make_sell(
    *,
    fitid="SELL-1",
    units="50",
    unitprice="99.00",
    total=None,
    accrdint=None,
    currency="USD",
    dttrade=datetime.datetime(2024, 6, 3, tzinfo=UTC),
    dtsettle=datetime.datetime(2024, 6, 6, tzinfo=UTC),
):
    if total is None:
        clean = D(units) * D(unitprice) * PAR / D(100)
        total = clean + (D(accrdint) if accrdint else D(0)) - D("5.00")
    return SELLDEBT(
        invsell=INVSELL(
            invtran=INVTRAN(fitid=fitid, dttrade=dttrade, dtsettle=dtsettle),
            secid=SECID(uniqueid="123456789", uniqueidtype="CUSIP"),
            units=D(units),
            unitprice=D(unitprice),
            commission=D("5.00"),
            total=D(str(total)),
            currency=CURRENCY(currate=D("1"), cursym=currency),
            subacctsec="CASH",
            subacctfund="CASH",
        ),
        sellreason="SELL",
        accrdint=None if accrdint is None else D(str(accrdint)),
    )


# 5% semiannual bond, coupons 1 Mar / 1 Sep, regular periods from 1 Mar 2024.
def schedule():
    terms = CouponTerms(
        coupon_rate=D("5"),
        frequency=CouponFrequency.SEMIANNUAL,
        first_coupon=datetime.date(2024, 9, 1),
        day_count=DayCount.ACT_ACT_ICMA,
        maturity=datetime.date(2026, 3, 1),
        issue_date=datetime.date(2024, 3, 1),
    )
    return CouponSchedule.from_terms(terms)


# 1 Mar -> 1 Apr 2024 is 31/184 of a semiannual period on 100 bonds of par 1000:
# 100_000 * 5% / 2 * 31/184 = 421.195652... -> 421.20
ACCRUED_APR1 = D("421.20")


class MoneyTestCase(unittest.TestCase):
    def test_money_rounds_half_up(self):
        self.assertEqual(money("1.005"), D("1.01"))
        self.assertEqual(money("1.004"), D("1.00"))

    def test_money_rejects_float(self):
        with self.assertRaises(LedgerError):
            money(1.005)

    def test_allocate_sums_exactly(self):
        parts = allocate_by_quantity(D("100.00"), [D(1), D(1), D(1)])
        self.assertEqual(sum(parts), D("100.00"))

    def test_allocate_proportional(self):
        parts = allocate_by_quantity(D("100.00"), [D(1), D(3)])
        self.assertEqual(parts, [D("25.00"), D("75.00")])

    def test_allocate_rejects_bad_quantities(self):
        with self.assertRaises(LedgerError):
            allocate_by_quantity(D("1"), [D(0), D(0)])
        with self.assertRaises(LedgerError):
            allocate_by_quantity(D("1"), [D(-1), D(2)])


class CouponScheduleTestCase(unittest.TestCase):
    def test_regular_schedule(self):
        sched = schedule()
        self.assertEqual(sched.periods[0].start, datetime.date(2024, 3, 1))
        self.assertEqual(sched.periods[0].end, datetime.date(2024, 9, 1))
        self.assertEqual(sched.periods[-1].end, datetime.date(2026, 3, 1))
        self.assertTrue(sched.periods[-1].is_maturity)

    def test_non_contiguous_rejected(self):
        sched = schedule()
        with self.assertRaises(LedgerError):
            CouponSchedule([sched.periods[0], sched.periods[2]])

    def test_act_act_icma_accrual(self):
        sched = schedule()
        # 1 Mar to 1 Apr = 31 days of the 184-day period.
        accrued = sched.accrued_interest(
            datetime.date(2024, 4, 1),
            par_value=PAR,
            units=D("100"),
            coupon_rate=D("5"),
        )
        self.assertEqual(accrued, ACCRUED_APR1)

    def test_accrual_split_at_coupon(self):
        sched = schedule()
        start, end = datetime.date(2025, 2, 1), datetime.date(2025, 4, 1)
        segments = sched.accrual_period(start, end)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0][2], datetime.date(2025, 3, 1))
        self.assertEqual(segments[1][1], datetime.date(2025, 3, 1))

        total = sched.accrued_between(
            start,
            end,
            par_value=PAR,
            units=D("100"),
            coupon_rate=D("5"),
        )
        # Accrual accumulates across the reset: as if 1 Feb -> 1 Mar (28/181)
        # plus 1 Mar -> 1 Apr (31/184), never negative.
        per_segment = sum(
            (
                period.coupon_amount(PAR, D("100"), D("5"))
                * (
                    period.year_fraction(period.start, seg_end)
                    - period.year_fraction(period.start, seg_start)
                )
                for period, seg_start, seg_end in segments
            ),
            D(0),
        )
        self.assertEqual(total, money(per_segment))
        self.assertGreater(total, D(0))

        # The second piece is exactly the accrual since the 1 Mar reset.
        self.assertEqual(
            sched.accrued_interest(
                end,
                par_value=PAR,
                units=D("100"),
                coupon_rate=D("5"),
            ),
            D("421.20"),
        )

    def test_ex_dividend_zero(self):
        sched = schedule()
        accrued = sched.accrued_interest(
            datetime.date(2024, 8, 30),
            par_value=PAR,
            units=D("100"),
            coupon_rate=D("5"),
            ex_dividend_days=5,
        )
        self.assertEqual(accrued, D("0.00"))

    def test_coupon_amount(self):
        sched = schedule()
        self.assertEqual(
            sched.coupon_for(
                datetime.date(2024, 9, 1),
                par_value=PAR,
                units=D("100"),
                coupon_rate=D("5"),
            ),
            D("2500.00"),
        )

    def test_business_day_roll(self):
        # 1 Sep 2024 is Sunday; 2 Sep is Labor Day; Following -> Tue 3 Sep.
        self.assertEqual(
            roll_business_day(
                datetime.date(2024, 9, 1), BusinessDayConvention.FOLLOWING
            ),
            datetime.date(2024, 9, 3),
        )
        # Modified Following rolls into a new month -> preceding Friday.
        self.assertEqual(
            roll_business_day(
                datetime.date(2024, 8, 31), BusinessDayConvention.MODIFIED_FOLLOWING
            ),
            datetime.date(2024, 8, 30),
        )

    def test_business_day_payment(self):
        terms = CouponTerms(
            coupon_rate=D("5"),
            frequency=CouponFrequency.SEMIANNUAL,
            first_coupon=datetime.date(2024, 9, 1),
            day_count=DayCount.ACT_ACT,
            business_day=BusinessDayConvention.FOLLOWING,
            maturity=datetime.date(2026, 3, 1),
        )
        sched = CouponSchedule.from_terms(terms)
        first = sched.periods[0]
        self.assertEqual(first.end, datetime.date(2024, 9, 1))
        self.assertEqual(first.payment, datetime.date(2024, 9, 3))

    def test_thirty_360(self):
        sched = CouponSchedule.from_dates(
            [datetime.date(2024, 3, 1), datetime.date(2024, 9, 1)],
            frequency=CouponFrequency.SEMIANNUAL,
            day_count=DayCount.THIRTY_360,
            issue_date=datetime.date(2023, 9, 1),
        )
        period = sched.periods[0]
        # 1 Sep 2023 -> 1 Mar 2024 = 180 days on 30/360.
        self.assertEqual(period.year_fraction(period.start, period.end), D("0.5"))

    def test_act_365(self):
        sched = CouponSchedule.from_dates(
            [datetime.date(2024, 3, 1)],
            frequency=CouponFrequency.SEMIANNUAL,
            day_count=DayCount.ACT_365,
            issue_date=datetime.date(2023, 12, 31),
        )
        period = sched.periods[0]
        self.assertEqual(
            period.year_fraction(period.start, period.end), D("61") / D("365")
        )

    def test_outside_schedule(self):
        sched = schedule()
        with self.assertRaises(LedgerError):
            sched.period_containing(datetime.date(2020, 1, 1))


class DebtTradeTestCase(unittest.TestCase):
    def test_parse_buy(self):
        trade = parse_debt_trade(make_buy(accrdint=ACCRUED_APR1), PAR)
        self.assertEqual(trade.side, "BUY")
        self.assertEqual(trade.clean_amount, D("98250.00"))
        self.assertEqual(trade.fees, D("15.00"))
        self.assertEqual(trade.accrued_interest, ACCRUED_APR1)
        self.assertEqual(trade.total, D("-98686.20"))

    def test_missing_currency(self):
        buy = make_buy()
        buy.invbuy.currency = None
        with self.assertRaises(MissingFieldError):
            parse_debt_trade(buy, PAR)

    def test_original_currency(self):
        trade = parse_debt_trade(make_buy(currency="EUR"), PAR)
        self.assertEqual(trade.currency, "EUR")

    def test_dirty_price_is_percent_of_par(self):
        trade = parse_debt_trade(
            make_buy(units="10", unitprice="98.25", commission="0", fees="0"),
            PAR,
        )
        self.assertEqual(trade.clean_amount, D("9825.00"))

    def test_float_rejected(self):
        # OFX model descriptors coerce floats on assignment, so exercise the
        # ledger's own guard rather than the model layer.
        with self.assertRaises(LedgerError):
            money(1234.56)

    def test_default_settlement_is_flagged(self):
        buy = make_buy(dtsettle=None)
        trade = parse_debt_trade(buy, PAR)
        self.assertFalse(trade.settlement_explicit)
        self.assertEqual(trade.settlement, datetime.date(2024, 3, 5))

    def test_cost_basis_and_expense_split(self):
        trade = parse_debt_trade(make_buy(accrdint=ACCRUED_APR1), PAR)
        self.assertEqual(trade.cost_basis(CostConvention.ALL_FEES), D("98265.00"))
        self.assertEqual(trade.cost_basis(CostConvention.CLEAN_PRICE), D("98250.00"))
        self.assertEqual(trade.expensed_fees(CostConvention.ALL_FEES), D("0.00"))
        self.assertEqual(trade.expensed_fees(CostConvention.CLEAN_PRICE), D("15.00"))
        self.assertEqual(
            trade.cost_basis(CostConvention.CLEAN_PLUS_COMMISSION)
            + trade.expensed_fees(CostConvention.CLEAN_PLUS_COMMISSION),
            D("98265.00"),
        )

    def test_sell_withholdings_are_expensed(self):
        sell = make_sell(units="10", accrdint="84.24", total="9959.24")
        sell.invsell.withholding = D("3.00")
        sell.invsell.total = D("9956.24")
        trade = parse_debt_trade(sell, PAR)
        self.assertEqual(trade.withholdings, D("3.00"))
        self.assertEqual(trade.expensed_fees(CostConvention.ALL_FEES), D("8.00"))


class SecurityCostLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = SecurityCostLedger("USD")

    def _buy(self, lot="L1", units="100"):
        return self.ledger.post_buy(
            fitid="B1",
            lot_id=lot,
            units=D(units),
            par_value=PAR,
            unit_price=D("98.25"),
            commission=D("10"),
            fees=D("5"),
            trade_date=datetime.date(2024, 3, 1),
        )

    def test_buy_cost_all_fees(self):
        entry = self._buy()
        self.assertEqual(entry.total_cost, D("98265.00"))
        self.assertEqual(self.ledger.cost_of("L1"), D("98265.00"))
        self.assertEqual(self.ledger.quantity_of("L1"), D("100"))

    def test_clean_price_excludes_fees(self):
        self.ledger.post_buy(
            fitid="B1",
            lot_id="L1",
            units=D("100"),
            par_value=PAR,
            unit_price=D("98.25"),
            commission=D("10"),
            fees=D("5"),
            convention=CostConvention.CLEAN_PRICE,
            trade_date=datetime.date(2024, 3, 1),
        )
        self.assertEqual(self.ledger.cost_of("L1"), D("98250.00"))

    def test_commission_only(self):
        self.ledger.post_buy(
            fitid="B1",
            lot_id="L1",
            units=D("100"),
            par_value=PAR,
            unit_price=D("98.25"),
            commission=D("10"),
            fees=D("5"),
            convention=CostConvention.CLEAN_PLUS_COMMISSION,
            trade_date=datetime.date(2024, 3, 1),
        )
        self.assertEqual(self.ledger.cost_of("L1"), D("98260.00"))

    def test_partial_sale_releases_average_cost(self):
        self._buy()
        self.ledger.post_sell(
            fitid="S1",
            lot_id="L1",
            units=D("60"),
            trade_date=datetime.date(2024, 6, 3),
        )
        self.assertEqual(self.ledger.quantity_of("L1"), D("40"))
        self.assertEqual(self.ledger.cost_of("L1"), D("39306.00"))
        self.assertEqual(self.ledger.average_cost("L1"), D("982.65"))

    def test_oversell_rejected(self):
        self._buy(units="100")
        with self.assertRaises(ReversalError):
            self.ledger.post_sell(
                fitid="S1",
                lot_id="L1",
                units=D("200"),
                trade_date=datetime.date(2024, 3, 2),
            )

    def test_transfer_by_quantity(self):
        self._buy()
        self.ledger.transfer(
            fitid="T1",
            from_lot="L1",
            to_lot="L2",
            units=D("25"),
            trade_date=datetime.date(2024, 4, 1),
        )
        self.assertEqual(self.ledger.quantity_of("L1"), D("75"))
        self.assertEqual(self.ledger.cost_of("L2"), D("24566.25"))
        self.assertEqual(
            self.ledger.cost_of("L1") + self.ledger.cost_of("L2"), D("98265.00")
        )

    def test_split_exact_residual(self):
        self._buy()
        entry = self.ledger.split(
            fitid="SP1",
            lot_id="L1",
            parts={"L2": D("33"), "L3": D("33"), "L4": D("34")},
            trade_date=datetime.date(2024, 4, 1),
        )
        total = sum(
            self.ledger.cost_of(lot) for lot in ("L2", "L3", "L4")
        )
        self.assertEqual(total, -entry.allocations[0].cost)
        self.assertEqual(self.ledger.quantity_of("L1"), D("0"))

    def test_reverse_only_original(self):
        self._buy(lot="L1")
        self._buy(lot="L2", units="50")
        first_id = self.ledger.entries[0].entry_id
        rev = self.ledger.reverse(
            entry_id=first_id,
            fitid="C1",
            trade_date=datetime.date(2024, 3, 3),
        )
        self.assertEqual(self.ledger.quantity_of("L1"), D("0"))
        self.assertEqual(self.ledger.quantity_of("L2"), D("50"))
        self.assertEqual(self.ledger.cost_of("L2"), D("49140.00"))
        self.assertEqual(rev.allocations[0].cost, D("-98265.00"))

    def test_double_reverse_rejected(self):
        self._buy()
        entry_id = self.ledger.entries[0].entry_id
        self.ledger.reverse(entry_id=entry_id, fitid="C1")
        with self.assertRaises(ReversalError):
            self.ledger.reverse(entry_id=entry_id, fitid="C2")

    def test_reverse_after_partial_sell_rejected(self):
        self._buy()
        self.ledger.post_sell(
            fitid="S1",
            lot_id="L1",
            units=D("10"),
            trade_date=datetime.date(2024, 4, 1),
        )
        with self.assertRaises(ReversalError):
            self.ledger.reverse(
                entry_id=self.ledger.entries[0].entry_id, fitid="C1"
            )


class InterestLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.ledger = InterestLedger("USD")

    def test_accrued_buy_is_negative(self):
        entry = self.ledger.accrued_buy(
            fitid="B1",
            lot_id="L1",
            amount=ACCRUED_APR1,
            trade_date=datetime.date(2024, 4, 1),
        )
        self.assertEqual(entry.amount, -ACCRUED_APR1)
        self.assertEqual(self.ledger.accrued_paid("L1"), ACCRUED_APR1)
        self.assertEqual(self.ledger.balance("L1"), -ACCRUED_APR1)

    def test_coupon_positive(self):
        self.ledger.coupon(
            fitid="C1",
            lot_id="L1",
            amount=D("2500.00"),
            trade_date=datetime.date(2024, 9, 1),
            period_start=datetime.date(2024, 3, 1),
            period_end=datetime.date(2024, 9, 1),
        )
        self.assertEqual(self.ledger.balance("L1"), D("2500.00"))

    def test_release_proportional(self):
        self.ledger.accrued_buy(
            fitid="B1",
            lot_id="L1",
            amount=D("400.00"),
            trade_date=datetime.date(2024, 3, 1),
        )
        self.ledger.release_accrued(
            fitid="S1",
            lot_id="L1",
            units=D("40"),
            from_units=D("100"),
            trade_date=datetime.date(2024, 6, 1),
        )
        self.assertEqual(self.ledger.accrued_paid("L1"), D("240.00"))

    def test_transfer_proportional(self):
        self.ledger.accrued_buy(
            fitid="B1",
            lot_id="L1",
            amount=D("400.00"),
            trade_date=datetime.date(2024, 3, 1),
        )
        self.ledger.transfer_accrued(
            fitid="T1",
            from_lot="L1",
            to_lot="L2",
            units=D("25"),
            from_units=D("100"),
            trade_date=datetime.date(2024, 6, 1),
        )
        self.assertEqual(self.ledger.accrued_paid("L1"), D("300.00"))
        self.assertEqual(self.ledger.accrued_paid("L2"), D("100.00"))

    def test_reverse_only_own_entry(self):
        first = self.ledger.accrued_buy(
            fitid="B1",
            lot_id="L1",
            amount=D("400.00"),
            trade_date=datetime.date(2024, 3, 1),
        )
        self.ledger.accrued_sell(
            fitid="S1",
            lot_id="L1",
            amount=D("200.00"),
            trade_date=datetime.date(2024, 5, 1),
        )
        self.ledger.reverse(entry_id=first.entry_id, fitid="C1")
        self.assertEqual(self.ledger.balance("L1"), D("200.00"))

    def test_accrual_separate_from_coupon(self):
        self.ledger.accrue(
            fitid="A1",
            lot_id="L1",
            amount=D("800.00"),
            trade_date=datetime.date(2024, 6, 1),
        )
        self.ledger.coupon(
            fitid="C1",
            lot_id="L1",
            amount=D("2500.00"),
            trade_date=datetime.date(2024, 9, 1),
            period_start=datetime.date(2024, 3, 1),
            period_end=datetime.date(2024, 9, 1),
        )
        self.assertEqual(self.ledger.balance("L1"), D("3300.00"))

    def test_transfer_is_zero_sum_across_lots(self):
        # Moving accrued-paid between lots shifts the interest balance by
        # quantity but leaves the book's total net interest unchanged.
        self.ledger.accrued_buy(
            fitid="B1",
            lot_id="L1",
            amount=D("400.00"),
            trade_date=datetime.date(2024, 3, 1),
        )
        self.ledger.transfer_accrued(
            fitid="T1",
            from_lot="L1",
            to_lot="L2",
            units=D("25"),
            from_units=D("100"),
            trade_date=datetime.date(2024, 6, 1),
        )
        self.assertEqual(self.ledger.balance("L1"), D("-300.00"))
        self.assertEqual(self.ledger.balance("L2"), D("-100.00"))
        self.assertEqual(self.ledger.balance(), D("-400.00"))
        self.assertEqual(self.ledger.accrued_paid("L1"), D("300.00"))
        self.assertEqual(self.ledger.accrued_paid("L2"), D("100.00"))

    def test_reversal_of_release_restores_paid(self):
        self.ledger.accrued_buy(
            fitid="B1",
            lot_id="L1",
            amount=D("400.00"),
            trade_date=datetime.date(2024, 3, 1),
        )
        release = self.ledger.release_accrued(
            fitid="S1",
            lot_id="L1",
            units=D("40"),
            from_units=D("100"),
            trade_date=datetime.date(2024, 6, 1),
        )
        self.ledger.reverse(entry_id=release.entry_id, fitid="C1")
        self.assertEqual(self.ledger.accrued_paid("L1"), D("400.00"))
        self.assertEqual(self.ledger.balance("L1"), D("-400.00"))


class BondBookTestCase(unittest.TestCase):
    def setUp(self):
        self.book = BondBook.from_currency(
            "USD", schedule=schedule(), coupon_rate=D("5")
        )

    def _buy(self, **kwargs):
        kwargs.setdefault("accrdint", str(ACCRUED_APR1))
        return self.book.post_buydebt(
            make_buy(**kwargs), PAR, require_accrued="accrdint" in kwargs
        )

    def test_post_buydebt_splits_cost_and_interest(self):
        result = self._buy()
        self.assertEqual(result.cost_entry.total_cost, D("98265.00"))
        self.assertEqual(result.interest_entry.amount, -ACCRUED_APR1)
        self.assertEqual(self.book.cost.cost_of("BUY-1"), D("98265.00"))
        self.assertEqual(self.book.interest.balance("BUY-1"), -ACCRUED_APR1)
        self.assertEqual(self.book.position("BUY-1").units, D("100"))

    def test_cost_conventions(self):
        self.book.post_buydebt(
            make_buy(accrdint=ACCRUED_APR1),
            PAR,
            convention=CostConvention.CLEAN_PRICE,
        )
        self.assertEqual(self.book.cost.cost_of("BUY-1"), D("98250.00"))

    def test_missing_accrued_not_inferred(self):
        # TOTAL embeds 421.20 of interest but ACCRDINT is absent.  The book
        # must not back-solve that number from the difference.
        trade = make_buy(total="-98686.20", accrdint=None)
        with self.assertRaises(MissingAccruedInterestError):
            self.book.post_buydebt(trade, PAR)

    def test_missing_accrued_reconciles_when_truly_absent(self):
        trade = make_buy(total=None, accrdint=None)
        result = self.book.post_buydebt(trade, PAR)
        self.assertIsNone(result.interest_entry)
        self.assertEqual(self.book.interest.balance("BUY-1"), D("0"))

    def test_wrong_total_rejected(self):
        trade = make_buy(accrdint=ACCRUED_APR1, total="-98686.10")
        with self.assertRaises(ReconciliationError):
            self.book.post_buydebt(trade, PAR, require_accrued=True)

    def test_accrued_checked_against_calendar(self):
        # 1 Mar settlement accrues zero, so an ACCRDINT of 421.20 is wrong.
        trade = make_buy(
            dtsettle=datetime.datetime(2024, 3, 1, tzinfo=UTC),
            accrdint=ACCRUED_APR1,
        )
        with self.assertRaises(ReconciliationError):
            self.book.post_buydebt(trade, PAR, require_accrued=True)

    def test_post_sell_partial(self):
        self._buy()
        # Sell 50 of 100 units at the same 1 Apr accrual point: clean
        # 50 * 1000 * 99% = 49,500; accrued 421.20 * 50/100 = 210.60;
        # fees 5 => total 49,705.60.
        trade = make_sell(
            units="50",
            accrdint="210.60",
            total="49705.60",
            dttrade=datetime.datetime(2024, 4, 1, tzinfo=UTC),
            dtsettle=datetime.datetime(2024, 4, 1, tzinfo=UTC),
        )
        result = self.book.post_selldebt(
            trade, PAR, lot_id="BUY-1", require_accrued=True
        )
        self.assertEqual(result.cost_entry.allocations[0].quantity, D("-50"))
        self.assertEqual(self.book.cost.cost_of("BUY-1"), D("49132.50"))
        self.assertEqual(result.interest_entry.amount, D("210.60"))
        self.assertEqual(self.book.interest.accrued_paid("BUY-1"), D("210.60"))

    def test_post_sell_missing_accrued_not_inferred(self):
        self._buy()
        trade = make_sell(units="50", accrdint=None, total="49705.60")
        with self.assertRaises(MissingAccruedInterestError):
            self.book.post_selldebt(trade, PAR, lot_id="BUY-1")

    def test_transfer_keeps_ledgers_consistent(self):
        self._buy()
        self.book.post_transfer(
            fitid="T1",
            from_lot="BUY-1",
            to_lot="BUY-1-A",
            units=D("25"),
            trade_date=datetime.date(2024, 5, 1),
        )
        self.assertEqual(
            self.book.cost.cost_of("BUY-1") + self.book.cost.cost_of("BUY-1-A"),
            D("98265.00"),
        )
        self.assertEqual(
            self.book.interest.accrued_paid("BUY-1")
            + self.book.interest.accrued_paid("BUY-1-A"),
            ACCRUED_APR1,
        )

    def test_split_position_exact(self):
        self._buy()
        self.book.split_position(
            fitid="SP1",
            lot_id="BUY-1",
            parts={"A": D("33"), "B": D("33"), "C": D("34")},
            trade_date=datetime.date(2024, 5, 1),
        )
        cost_total = sum(self.book.cost.cost_of(lot) for lot in ("A", "B", "C"))
        interest_total = sum(
            self.book.interest.accrued_paid(lot) for lot in ("A", "B", "C")
        )
        self.assertEqual(cost_total, D("98265.00"))
        self.assertEqual(interest_total, ACCRUED_APR1)

    def test_coupon_across_payment_date(self):
        self._buy()
        entry = self.book.post_coupon(
            fitid="CPN1", lot_id="BUY-1", coupon_date=datetime.date(2024, 9, 1)
        )
        self.assertEqual(entry.amount, D("2500.00"))
        self.assertEqual(
            self.book.interest.balance("BUY-1"), D("2500.00") - ACCRUED_APR1
        )

    def test_accrued_interest_calculation(self):
        self._buy()
        accrued = self.book.accrued_interest("BUY-1", datetime.date(2024, 4, 1))
        self.assertEqual(accrued, ACCRUED_APR1)

    def test_short_first_coupon_stub(self):
        terms = CouponTerms(
            coupon_rate=D("5"),
            frequency=CouponFrequency.SEMIANNUAL,
            first_coupon=datetime.date(2024, 9, 1),
            issue_date=datetime.date(2024, 5, 1),
            day_count=DayCount.ACT_ACT_ICMA,
            maturity=datetime.date(2026, 3, 1),
        )
        sched = CouponSchedule.from_terms(terms)
        stub = sched.periods[0]
        self.assertTrue(stub.is_stub)
        # 1 May -> 1 Sep is 123 days against a notional 184-day period:
        # 100 bonds * 1000 par * 5% * (123 / (184 * 2)) = 1671.20.
        self.assertEqual(
            sched.coupon_for(
                datetime.date(2024, 9, 1),
                par_value=PAR,
                units=D("100"),
                coupon_rate=D("5"),
            ),
            D("1671.20"),
        )
        self.assertEqual(
            sched.accrued_interest(
                datetime.date(2024, 6, 1),
                par_value=PAR,
                units=D("100"),
                coupon_rate=D("5"),
            ),
            money(D("100000") * D("0.05") * D("31") / (D("184") * D("2"))),
        )

    def test_reverse_trade_only_own_allocation(self):
        self._buy(fitid="B1")
        second_accrued = D("210.60")
        self.book.post_buydebt(
            make_buy(
                fitid="B2",
                units="50",
                accrdint=str(second_accrued),
                dtsettle=SETTLE_DATE,
            ),
            PAR,
            require_accrued=True,
        )
        result = self.book.reverse_trade(
            original_fitid="B1", correction_fitid="C1"
        )
        self.assertTrue(result.cost_entries)
        self.assertEqual(self.book.cost.quantity_of("B1"), D("0"))
        self.assertEqual(self.book.cost.quantity_of("B2"), D("50"))
        self.assertEqual(self.book.interest.balance("B1"), D("0"))
        self.assertEqual(self.book.interest.balance("B2"), -second_accrued)

    def test_reverse_after_transfer(self):
        self._buy()
        self.book.post_transfer(
            fitid="T1",
            from_lot="BUY-1",
            to_lot="BUY-1-A",
            units=D("25"),
            trade_date=datetime.date(2024, 5, 1),
        )
        with self.assertRaises(ReversalError):
            self.book.reverse_trade(
                original_fitid="BUY-1", correction_fitid="C1"
            )

    def test_currency_is_original(self):
        with self.assertRaises(LedgerError):
            BondBook.from_currency("USD").post_buydebt(
                make_buy(currency="EUR"), PAR
            )

    def test_par_value_required(self):
        with self.assertRaises(MissingFieldError):
            BondBook.from_currency("USD").position("missing")


class CouponCalendarTestCase(unittest.TestCase):
    def test_semiannual_common_year(self):
        sched = schedule()
        # 1 Mar -> 1 Sep 2024 = 184 actual days.
        self.assertEqual(sched.periods[0].days, 184)
        self.assertEqual(
            sched.periods[0].year_fraction(
                sched.periods[0].start, sched.periods[0].end
            ),
            D("0.5"),
        )

    def test_after_coupon_crossing(self):
        sched = schedule()
        # 1 Sep 2024 is the coupon date, so accrued resets to zero.
        self.assertEqual(
            sched.accrued_interest(
                datetime.date(2024, 9, 1),
                par_value=PAR,
                units=D("100"),
                coupon_rate=D("5"),
            ),
            D("0.00"),
        )


if __name__ == "__main__":
    unittest.main()
