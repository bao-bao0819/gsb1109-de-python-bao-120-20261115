"""
Bond transaction accounting for OFX debt trades.

OFX reports bond trades as ``BUYDEBT`` / ``SELLDEBT`` wrapping ``INVBUY`` /
``INVSELL``.  Those aggregates carry a clean price (``UNITPRICE``), an optional
``ACCRDINT`` that is separate from the price, transaction fees, and a signed
``TOTAL``.  OFX itself does not prescribe how to turn that into a cost basis,
how to accrue coupon interest, or how to apportion either when only part of a
position is transferred out.

This module fixes those conventions explicitly:

* **Separate ledgers.**  :class:`SecurityCostLedger` records only security
  cost; :class:`InterestLedger` records only coupon/accrued-interest amounts.
  Accrued interest is never rolled into security cost.
* **Original currency only.**  Each ledger is bound to one ISO-4217 currency.
  Every trade is reconciled in that currency, to the cent, against the OFX
  ``TOTAL``.  No FX translation is performed.
* **Missing fields stay missing.**  If ``ACCRDINT`` is absent, the trade is
  reconciled without it.  If the reported ``TOTAL`` then disagrees, the
  posting fails; accrued interest is never derived from the total difference.
* **Quantity-based allocation.**  Partial transfers and splits allocate cost
  (and accrued interest paid) in proportion to units.  A deterministic
  rounding residual guarantees that allocated pieces sum exactly to the
  amount being split.
* **Corrections are additive.**  Entries are immutable.  Reversing an entry
  posts the negation of *that entry's* allocations; it never recomputes or
  touches allocations made by any other entry.

Money is always :class:`decimal.Decimal`; binary floats are rejected.
"""

from __future__ import annotations

__all__ = [
    "DayCount",
    "BusinessDayConvention",
    "CouponFrequency",
    "CouponTerms",
    "CouponPeriod",
    "CouponSchedule",
    "CostConvention",
    "CostAllocation",
    "CostEntry",
    "CostPosition",
    "InterestAllocation",
    "InterestEntry",
    "SecurityCostLedger",
    "InterestLedger",
    "DebtTrade",
    "PostingResult",
    "ReversalResult",
    "BondBook",
    "BondPosition",
    "LedgerError",
    "MissingFieldError",
    "MissingAccruedInterestError",
    "ReconciliationError",
    "ReversalError",
    "parse_debt_trade",
    "money",
]

# stdlib imports
import calendar
import datetime
import decimal
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

# local imports
from ofxtools.utils import NYSEcalendar, settleDate

ZERO = Decimal("0")
HUNDRED = Decimal("100")
CENT = Decimal("0.01")


class LedgerError(ValueError):
    """Base class for bond accounting errors."""


class MissingFieldError(LedgerError):
    """A field needed for accounting is absent from the OFX data."""


class ReconciliationError(LedgerError):
    """Trade components do not add up to the reported ``TOTAL``."""


class MissingAccruedInterestError(MissingFieldError, ReconciliationError):
    """``ACCRDINT`` is absent and the total cannot be reconciled without it.

    This is deliberately an error rather than a residual calculation: the
    accounting rule is that missing interest is never inferred from the
    difference between ``TOTAL`` and the other components.
    """


class ReversalError(LedgerError):
    """A correction/reversal is not valid for the ledger's current state."""


def money(value: Any, places: int = 2) -> Decimal:
    """Coerce to ``Decimal`` and round half-up to a fixed number of places."""
    if isinstance(value, bool):
        raise LedgerError("Boolean is not a monetary amount")
    if isinstance(value, float):
        raise LedgerError(
            "Binary floats are not accepted for money; pass str or Decimal"
        )
    if not isinstance(value, Decimal):
        try:
            value = Decimal(value)
        except (decimal.InvalidOperation, TypeError) as exc:
            raise LedgerError(f"{value!r} is not a Decimal-compatible number") from exc
    quantum = Decimal(1).scaleb(-places)
    return Decimal(value).quantize(quantum, rounding=decimal.ROUND_HALF_UP)


def _require_decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, float):
        raise LedgerError(f"{name} must be Decimal or str, not float")
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except (decimal.InvalidOperation, TypeError, ValueError) as exc:
            raise LedgerError(f"{name} is not a Decimal-compatible number") from exc
    if positive and result <= ZERO:
        raise LedgerError(f"{name} must be positive, got {result}")
    return result


def _validate_currency(code: str) -> str:
    if not isinstance(code, str) or len(code) != 3 or not code.isalpha():
        raise LedgerError(f"Invalid ISO-4217 currency code {code!r}")
    return code.upper()


def _date_of(value: Any, name: str) -> datetime.date:
    if value is None:
        raise MissingFieldError(f"{name} is required")
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    raise LedgerError(f"{name} must be a date/datetime, got {value!r}")


###############################################################################
# Coupon calendar
###############################################################################
class DayCount(str, Enum):
    """Interest accrual day-count conventions."""

    #: Actual/Actual (ISDA): actual days split across calendar years, each
    #: divided by 365 or 366.
    ACT_ACT = "ACT/ACT"
    #: Actual/Actual (ICMA bond basis): actual days accrued divided by the
    #: actual days in the coupon period, scaled by coupon frequency.
    ACT_ACT_ICMA = "ACT/ACT ICMA"
    #: Actual days over a 365-day year.
    ACT_365 = "ACT/365"
    #: Actual days over a 360-day year (money-market basis).
    ACT_360 = "ACT/360"
    #: 30/360 US (NASD): 30-day months and a 360-day year.
    THIRTY_360 = "30/360"
    #: 30E/360 (Eurobond): every month counts as 30 days.
    THIRTY_E_360 = "30E/360"
    #: 30E/360 ISDA: end-of-month dates move to the 30th, except at maturity.
    THIRTY_E_360_ISDA = "30E/360 ISDA"


class BusinessDayConvention(str, Enum):
    """How a scheduled coupon date is rolled onto a business day."""

    UNADJUSTED = "UNADJUSTED"
    FOLLOWING = "FOLLOWING"
    MODIFIED_FOLLOWING = "MODIFIED_FOLLOWING"
    PRECEDING = "PRECEDING"
    MODIFIED_PRECEDING = "MODIFIED_PRECEDING"


class CouponFrequency(str, Enum):
    """Coupon frequency and its month spacing."""

    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMIANNUAL = "SEMIANNUAL"
    ANNUAL = "ANNUAL"

    @property
    def months(self) -> int:
        return {
            CouponFrequency.MONTHLY: 1,
            CouponFrequency.QUARTERLY: 3,
            CouponFrequency.SEMIANNUAL: 6,
            CouponFrequency.ANNUAL: 12,
        }[self]

    @property
    def payments_per_year(self) -> Decimal:
        return Decimal(12) / Decimal(self.months)

    @classmethod
    def from_ofx(cls, value: str) -> CouponFrequency:
        """Map an OFX ``COUPONFREQ`` value to a frequency.

        OFX also permits ``OTHER``, which has no month spacing.  Callers must
        then construct a schedule with explicit dates via
        :meth:`CouponSchedule.from_dates`.
        """
        try:
            return cls(value)
        except ValueError:
            raise LedgerError(
                f"OFX COUPONFREQ {value!r} has no defined coupon spacing; "
                "supply explicit coupon dates"
            ) from None


def _is_leap(year: int) -> bool:
    return calendar.isleap(year)


def _actual_actual_isda(start: datetime.date, end: datetime.date) -> Decimal:
    """ACT/ACT (ISDA) year fraction between two dates."""
    if end <= start:
        return ZERO
    total = ZERO
    cursor = start
    while cursor < end:
        year_end = datetime.date(cursor.year + 1, 1, 1)
        segment_end = min(end, year_end)
        denominator = Decimal(366 if _is_leap(cursor.year) else 365)
        total += Decimal((segment_end - cursor).days) / denominator
        cursor = segment_end
    return total


def _thirty_360_us(start: datetime.date, end: datetime.date) -> int:
    """Days between dates on the 30/360 US (NASD) basis."""
    d1, m1, y1 = start.day, start.month, start.year
    d2, m2, y2 = end.day, end.month, end.year

    if start.month == 2 and d1 == calendar.monthrange(y1, 2)[1]:
        d1 = 30
    if d1 == 31:
        d1 = 30
    if end.month == 2 and d2 == calendar.monthrange(y2, 2)[1] and d1 == 30:
        d2 = 30
    if d2 == 31 and d1 == 30:
        d2 = 30

    return 360 * (y2 - y1) + 30 * (m2 - m1) + (d2 - d1)


def _thirty_e_360(start: datetime.date, end: datetime.date) -> int:
    d1 = min(start.day, 30)
    d2 = min(end.day, 30)
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def _thirty_e_360_isda(
    start: datetime.date, end: datetime.date, *, is_maturity: bool
) -> int:
    d1 = 30 if start.day == calendar.monthrange(start.year, start.month)[1] else start.day
    if end.day == calendar.monthrange(end.year, end.month)[1] and not is_maturity:
        d2 = 30
    else:
        d2 = end.day
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def _clamp_date(year: int, month: int, day: int) -> datetime.date:
    return datetime.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _is_business_day(date: datetime.date) -> bool:
    return date.weekday() < 5 and date not in NYSEcalendar.holidays(date.year)


def _next_business_day(date: datetime.date) -> datetime.date:
    date += datetime.timedelta(days=1)
    while not _is_business_day(date):
        date += datetime.timedelta(days=1)
    return date


def _previous_business_day(date: datetime.date) -> datetime.date:
    date -= datetime.timedelta(days=1)
    while not _is_business_day(date):
        date -= datetime.timedelta(days=1)
    return date


def roll_business_day(
    date: datetime.date,
    convention: BusinessDayConvention,
    *,
    is_maturity: bool = False,
) -> datetime.date:
    """
    Roll ``date`` onto a NYSE business day per ``convention``.

    The NYSE calendar is the one used elsewhere in ``ofxtools``.  For bonds
    paying on another market's calendar, pass explicit adjusted payment dates
    to :meth:`CouponSchedule.from_dates` instead.
    """
    if convention is BusinessDayConvention.UNADJUSTED or _is_business_day(date):
        return date

    if convention in (
        BusinessDayConvention.FOLLOWING,
        BusinessDayConvention.MODIFIED_FOLLOWING,
    ):
        adjusted = _next_business_day(date)
        if (
            convention is BusinessDayConvention.MODIFIED_FOLLOWING
            and adjusted.month != date.month
        ):
            return _previous_business_day(date)
        return adjusted

    adjusted = _previous_business_day(date)
    if (
        convention is BusinessDayConvention.MODIFIED_PRECEDING
        and adjusted.month != date.month
    ):
        return _next_business_day(date)
    return adjusted


@dataclass(frozen=True)
class CouponTerms:
    """
    Static coupon terms used to generate a schedule.

    ``first_coupon`` is the first coupon date after issue/settlement.
    ``maturity`` (optional) bounds the schedule and identifies the final
    period for 30E/360 ISDA.  ``issue_date`` (optional) provides a stub start;
    without it, the period before ``first_coupon`` is one regular period.
    """

    coupon_rate: Decimal
    frequency: CouponFrequency
    first_coupon: datetime.date
    day_count: DayCount = DayCount.ACT_ACT_ICMA
    business_day: BusinessDayConvention = BusinessDayConvention.UNADJUSTED
    maturity: datetime.date | None = None
    issue_date: datetime.date | None = None
    #: Days before a coupon date during which the bond trades ex-coupon.
    ex_dividend_days: int = 0
    #: If True, accrual restarts on the adjusted payment date; if False
    #: (default), it restarts on the scheduled coupon date.
    accrual_on_adjusted_dates: bool = False

    def __post_init__(self) -> None:
        if _require_decimal(self.coupon_rate, "coupon_rate") < ZERO:
            raise LedgerError("coupon_rate cannot be negative")
        if not isinstance(self.frequency, CouponFrequency):
            object.__setattr__(self, "frequency", CouponFrequency(self.frequency))
        if not isinstance(self.day_count, DayCount):
            object.__setattr__(self, "day_count", DayCount(self.day_count))
        if not isinstance(self.business_day, BusinessDayConvention):
            object.__setattr__(
                self, "business_day", BusinessDayConvention(self.business_day)
            )
        if self.ex_dividend_days < 0:
            raise LedgerError("ex_dividend_days cannot be negative")
        if self.maturity is not None and self.maturity < self.first_coupon:
            raise LedgerError("maturity precedes first_coupon")
        if self.issue_date is not None and self.issue_date >= self.first_coupon:
            raise LedgerError("issue_date must precede first_coupon")


@dataclass(frozen=True)
class CouponPeriod:
    """One coupon period, with scheduled start/end and payment date."""

    start: datetime.date
    end: datetime.date
    payment: datetime.date
    frequency: CouponFrequency
    day_count: DayCount
    is_maturity: bool = False
    #: End of the notional regular period for ICMA.  For a regular period this
    #: equals ``end``; for a stub it is the date a full period would have ended.
    notional_end: datetime.date | None = None

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def notional_days(self) -> int:
        """Actual days in the notional regular period (ICMA denominator)."""
        if self.notional_end is None:
            return self.days
        return (self.notional_end - self.start).days

    def year_fraction(self, start: datetime.date, end: datetime.date) -> Decimal:
        """Year fraction from ``start`` to ``end`` inside this period."""
        if not (self.start <= start <= end <= self.end):
            raise LedgerError(
                f"{start}..{end} falls outside coupon period "
                f"{self.start}..{self.end}"
            )
        if start == end:
            return ZERO

        dc = self.day_count
        if dc is DayCount.ACT_ACT:
            return _actual_actual_isda(start, end)
        if dc is DayCount.ACT_ACT_ICMA:
            if self.notional_days == 0:  # pragma: no cover - schedule validates
                raise LedgerError("Zero-length notional coupon period")
            return Decimal((end - start).days) / (
                Decimal(self.notional_days) * self.frequency.payments_per_year
            )
        if dc is DayCount.ACT_365:
            return Decimal((end - start).days) / Decimal(365)
        if dc is DayCount.ACT_360:
            return Decimal((end - start).days) / Decimal(360)
        if dc is DayCount.THIRTY_360:
            return Decimal(_thirty_360_us(start, end)) / Decimal(360)
        if dc is DayCount.THIRTY_E_360:
            return Decimal(_thirty_e_360(start, end)) / Decimal(360)
        if dc is DayCount.THIRTY_E_360_ISDA:
            return Decimal(
                _thirty_e_360_isda(start, end, is_maturity=self.is_maturity)
            ) / Decimal(360)
        raise LedgerError(f"Unsupported day count {dc!r}")  # pragma: no cover

    def coupon_amount(self, par_value: Decimal, units: Decimal, coupon_rate: Decimal) -> Decimal:
        """
        Cash coupon paid for this period, before rounding to cents.

        A regular period pays ``par * units * rate / payments_per_year`` —
        coupon cash is a declared amount, so ACT/ACT's year-by-year split does
        not change it.  A short or long stub pays only its own accrual:
        ``par * units * rate * yearfraction(period)``.
        """
        regular = (
            par_value
            * units
            * coupon_rate
            / HUNDRED
            / self.frequency.payments_per_year
        )
        if not self.is_stub:
            return regular
        fraction = self.year_fraction(self.start, self.end)
        return par_value * units * coupon_rate / HUNDRED * fraction

    @property
    def is_stub(self) -> bool:
        """True when the period is not exactly one notional regular interval."""
        return self.notional_end is not None and self.notional_end != self.end


class CouponSchedule:
    """Ordered, contiguous coupon periods."""

    def __init__(self, periods: Sequence[CouponPeriod]) -> None:
        periods = tuple(sorted(periods, key=lambda p: p.end))
        if not periods:
            raise LedgerError("Coupon schedule is empty")

        previous_end = periods[0].start
        for period in periods:
            if period.end <= period.start:
                raise LedgerError(f"Coupon period {period} ends before it starts")
            if period.start != previous_end:
                raise LedgerError(
                    f"Coupon periods are not contiguous at {period.start}"
                )
            previous_end = period.end
        self.periods = periods

    @classmethod
    def from_terms(
        cls,
        terms: CouponTerms,
        *,
        horizon: datetime.date | None = None,
    ) -> CouponSchedule:
        """
        Generate a schedule from ``terms``.

        ``horizon`` caps the schedule; it defaults to maturity, or to 30 years
        after the first coupon when no maturity is given.
        """
        if horizon is None:
            horizon = terms.maturity
        if horizon is None:
            horizon = _clamp_date(
                terms.first_coupon.year + 30,
                terms.first_coupon.month,
                terms.first_coupon.day,
            )

        months = terms.frequency.months
        previous_start = terms.issue_date
        if previous_start is None:
            y, m = _add_months(
                terms.first_coupon.year, terms.first_coupon.month, -months
            )
            previous_start = _clamp_date(y, m, terms.first_coupon.day)

        periods: list[CouponPeriod] = []
        coupon = terms.first_coupon
        while coupon <= horizon:
            is_maturity = terms.maturity is not None and coupon == terms.maturity
            payment = roll_business_day(
                coupon, terms.business_day, is_maturity=is_maturity
            )
            ny, nm = _add_months(previous_start.year, previous_start.month, months)
            notional_end = _clamp_date(ny, nm, previous_start.day)
            periods.append(
                CouponPeriod(
                    start=previous_start,
                    end=coupon,
                    payment=payment,
                    frequency=terms.frequency,
                    day_count=terms.day_count,
                    is_maturity=is_maturity,
                    notional_end=notional_end,
                )
            )
            previous_start = (
                payment if terms.accrual_on_adjusted_dates else coupon
            )
            y, m = _add_months(coupon.year, coupon.month, months)
            coupon = _clamp_date(y, m, terms.first_coupon.day)

        return cls(periods)

    @classmethod
    def from_dates(
        cls,
        coupon_dates: Iterable[datetime.date],
        *,
        frequency: CouponFrequency,
        day_count: DayCount,
        issue_date: datetime.date | None = None,
        payment_dates: Iterable[datetime.date] | None = None,
        maturity: datetime.date | None = None,
    ) -> CouponSchedule:
        """Build a schedule from explicit coupon dates (irregular stubs)."""
        coupons = tuple(sorted(set(coupon_dates)))
        if not coupons:
            raise LedgerError("No coupon dates supplied")

        start_points = (issue_date,) if issue_date is not None else ()
        points = start_points + coupons
        payments = tuple(payment_dates) if payment_dates is not None else coupons
        if len(payments) != len(coupons):
            raise LedgerError("payment_dates must align one-for-one with coupon dates")
        if any(p < c for p, c in zip(payments, coupons)):
            raise LedgerError("payment date cannot precede its coupon date")

        periods = []
        for i in range(len(points) - 1):
            start = points[i]
            end = points[i + 1]
            ny, nm = _add_months(start.year, start.month, frequency.months)
            periods.append(
                CouponPeriod(
                    start=start,
                    end=end,
                    payment=payments[i],
                    frequency=frequency,
                    day_count=day_count,
                    is_maturity=(maturity is not None and end == maturity),
                    notional_end=_clamp_date(ny, nm, start.day),
                )
            )
        return cls(periods)

    def period_containing(self, date: datetime.date) -> CouponPeriod:
        """Return the period containing ``date`` (start-inclusive, end-inclusive)."""
        for period in self.periods:
            if period.start <= date <= period.end:
                return period
        raise LedgerError(f"{date} falls outside the coupon schedule")

    def period_ending(self, coupon_date: datetime.date) -> CouponPeriod:
        for period in self.periods:
            if period.end == coupon_date:
                return period
        raise LedgerError(f"No coupon period ends on {coupon_date}")

    def coupons_between(
        self, start: datetime.date, end: datetime.date
    ) -> list[CouponPeriod]:
        """Periods whose coupon date is after ``start`` and on/before ``end``."""
        return [period for period in self.periods if start < period.end <= end]

    def accrued_interest(
        self,
        settlement: datetime.date,
        *,
        par_value: Decimal,
        units: Decimal,
        coupon_rate: Decimal,
        ex_dividend_days: int = 0,
    ) -> Decimal:
        """
        Accrued interest for ``units`` bonds at ``settlement``, rounded to cents.

        If settlement falls in the ex-coupon window (``ex_dividend_days``
        before the coupon date), the buyer will not receive the coupon, so
        accrued interest is zero.
        """
        period = self.period_containing(settlement)
        if period.end - settlement <= datetime.timedelta(days=ex_dividend_days):
            return ZERO
        fraction = period.year_fraction(period.start, settlement)
        amount = (
            _require_decimal(par_value, "par_value")
            * _require_decimal(units, "units")
            * _require_decimal(coupon_rate, "coupon_rate")
            / HUNDRED
            * fraction
        )
        return money(amount)

    def coupon_for(
        self,
        coupon_date: datetime.date,
        *,
        par_value: Decimal,
        units: Decimal,
        coupon_rate: Decimal,
    ) -> Decimal:
        period = self.period_ending(coupon_date)
        return money(period.coupon_amount(par_value, units, coupon_rate))

    def accrual_period(
        self, start: datetime.date, end: datetime.date
    ) -> list[tuple[CouponPeriod, datetime.date, datetime.date]]:
        """
        Split an accrual interval at coupon dates.

        Returns ``(period, segment_start, segment_end)`` tuples, making coupon
        crossings explicit for reporting or day-count checks.
        """
        if end < start:
            raise LedgerError("Accrual end precedes accrual start")
        segments = []
        cursor = start
        for period in self.periods:
            if period.end <= cursor:
                continue
            if period.start >= end:
                break
            segment_start = max(cursor, period.start)
            segment_end = min(end, period.end)
            if segment_start < segment_end:
                segments.append((period, segment_start, segment_end))
            cursor = segment_end
            if cursor >= end:
                break
        return segments

    def accrued_between(
        self,
        start: datetime.date,
        end: datetime.date,
        *,
        par_value: Decimal,
        units: Decimal,
        coupon_rate: Decimal,
    ) -> Decimal:
        """
        Accrued interest for an interval, accumulated segment by segment across
        every coupon date.

        Each segment contributes ``coupon per period * (yf(seg_end) -
        yf(seg_start))``, which is additive and unaffected by the accrual reset
        at a coupon date.  Ex-dividend handling applies to a settlement point,
        not to an interval, so it is not applied here; use
        :meth:`accrued_interest` for a settlement-date amount.
        """
        par_value = _require_decimal(par_value, "par_value")
        units = _require_decimal(units, "units")
        coupon_rate = _require_decimal(coupon_rate, "coupon_rate")

        total = ZERO
        for period, seg_start, seg_end in self.accrual_period(start, end):
            coupon = period.coupon_amount(par_value, units, coupon_rate)
            fraction = period.year_fraction(
                period.start, seg_end
            ) - period.year_fraction(period.start, seg_start)
            total += coupon * fraction
        return money(total)


###############################################################################
# Trade parsing
###############################################################################
class CostConvention(str, Enum):
    """
    Which purchase fees are capitalized into security cost.

    Every convention starts from the traded clean amount
    ``par_value * units * unit_price / 100``.  Accrued interest is never
    included; it belongs in the interest ledger.
    """

    #: Clean price only; all fees are period expenses.
    CLEAN_PRICE = "CLEAN_PRICE"
    #: Clean price plus commission; other fees are period expenses.
    CLEAN_PLUS_COMMISSION = "CLEAN_PLUS_COMMISSION"
    #: Clean price plus commission, markup/markdown, taxes, fees, and load.
    ALL_FEES = "ALL_FEES"


@dataclass(frozen=True)
class DebtTrade:
    """Normalized fields of an OFX debt purchase or sale, in original currency."""

    fitid: str
    side: str  # "BUY" or "SELL"
    units: Decimal  # positive quantity traded
    unit_price: Decimal  # percent of par
    par_value: Decimal  # par per bond
    clean_amount: Decimal
    commission: Decimal
    markup_markdown: Decimal
    taxes: Decimal
    other_fees: Decimal
    load: Decimal
    withholding: Decimal
    penalty: Decimal
    state_withholding: Decimal
    accrued_interest: Decimal | None
    total: Decimal  # signed cash effect reported by OFX
    currency: str
    trade_date: datetime.date
    settlement: datetime.date
    settlement_explicit: bool = True

    @property
    def fees(self) -> Decimal:
        """Transaction fees before withholdings/penalties."""
        return money(
            self.commission
            + self.markup_markdown
            + self.taxes
            + self.other_fees
            + self.load
        )

    @property
    def withholdings(self) -> Decimal:
        return money(self.withholding + self.penalty + self.state_withholding)

    @property
    def cash_fees(self) -> Decimal:
        return money(self.fees + self.withholdings)

    def cost_fees(self, convention: CostConvention) -> Decimal:
        """Fees capitalized into purchase cost under ``convention``."""
        if convention is CostConvention.CLEAN_PRICE:
            return ZERO
        if convention is CostConvention.CLEAN_PLUS_COMMISSION:
            return money(self.commission)
        if convention is CostConvention.ALL_FEES:
            return self.fees
        raise LedgerError(f"Unknown cost convention {convention!r}")

    def expensed_fees(self, convention: CostConvention) -> Decimal:
        """
        Fees treated as period expense under ``convention``.

        Withholdings and penalties are never capitalized; they are always
        expensed (and they reduce cash on a sale rather than adding to cost).
        """
        if self.side == "SELL":
            return money(self.fees + self.withholdings)
        return money(self.fees - self.cost_fees(convention) + self.withholdings)

    def cost_basis(self, convention: CostConvention) -> Decimal:
        """Total purchase cost basis in original currency."""
        return money(self.clean_amount + self.cost_fees(convention))

    def cash_total(self, *, accrued: Decimal | None = None) -> Decimal:
        """Alias for ``expected_total`` with an explicit cash-oriented name."""
        return self.expected_total(accrued=accrued)

    def expected_total(self, *, accrued: Decimal | None = None) -> Decimal:
        """
        Signed cash total implied by the trade's components.

        Purchases are negative cash; sales are positive cash.
        """
        accrued = self.accrued_interest if accrued is None else accrued
        accrued = ZERO if accrued is None else accrued

        if self.side == "BUY":
            return money(-(self.clean_amount + self.cash_fees + accrued))
        return money(self.clean_amount + accrued - self.cash_fees)


def _currency_of(aggregate: Any) -> str | None:
    currency = getattr(aggregate, "currency", None)
    if currency is not None:
        return str(currency.cursym)
    origcurrency = getattr(aggregate, "origcurrency", None)
    if origcurrency is not None:
        return str(origcurrency.cursym)
    return None


def _fee(inv: Any, name: str) -> Decimal:
    value = getattr(inv, name, None)
    return ZERO if value is None else _require_decimal(value, name.upper())


def parse_debt_trade(
    trade: Any,
    par_value: Decimal,
    *,
    side: str | None = None,
    default_settlement_days: int = 2,
) -> DebtTrade:
    """
    Normalize a ``BUYDEBT`` / ``SELLDEBT`` (or bare ``INVBUY``/``INVSELL``).

    ``par_value`` is required because OFX carries par on security information
    (``DEBTINFO``), not on the trade aggregate.  ``ACCRDINT`` remains ``None``
    when absent; it is never inferred.

    If ``DTSETTLE`` is absent, settlement is taken as the trade date plus
    ``default_settlement_days`` business days and ``settlement_explicit`` is
    set to ``False`` so callers can see the assumption.
    """
    clsname = type(trade).__name__
    if clsname == "BUYDEBT":
        side = "BUY"
        inv = trade.invbuy
    elif clsname == "SELLDEBT":
        side = "SELL"
        inv = trade.invsell
    elif clsname in ("INVBUY", "INVSELL"):
        side = "BUY" if clsname == "INVBUY" else "SELL"
        inv = trade
    elif side in ("BUY", "SELL") and hasattr(trade, "invtran"):
        inv = trade
    else:
        raise LedgerError(f"{clsname} is not a debt trade")

    if side not in ("BUY", "SELL"):
        raise LedgerError("side must be BUY or SELL")
    if inv is None:
        raise MissingFieldError(f"{clsname} has no underlying INVBUY/INVSELL")

    currency = _currency_of(inv)
    if currency is None:
        raise MissingFieldError(
            f"{clsname} {inv.invtran.fitid}: CURRENCY/ORIGCURRENCY is required "
            "to keep cost in original currency"
        )

    trade_date = _date_of(inv.dttrade, "DTTRADE")
    if inv.dtsettle is not None:
        settlement = _date_of(inv.dtsettle, "DTSETTLE")
        settlement_explicit = True
    else:
        settlement = settleDate(trade_date, n=default_settlement_days)
        settlement_explicit = False

    units = abs(_require_decimal(inv.units, "UNITS"))
    if units == ZERO:
        raise LedgerError("UNITS cannot be zero")
    unit_price = _require_decimal(inv.unitprice, "UNITPRICE")
    par_value = _require_decimal(par_value, "par_value", positive=True)
    clean_amount = money(par_value * units * unit_price / HUNDRED)

    if side == "BUY":
        commission = _fee(inv, "commission")
        markup_markdown = _fee(inv, "markup")
        taxes = _fee(inv, "taxes")
        other_fees = _fee(inv, "fees")
        load = _fee(inv, "load")
        withholding = penalty = state_withholding = ZERO
        accrdint = getattr(trade, "accrdint", None)
    else:
        commission = _fee(inv, "commission")
        markup_markdown = _fee(inv, "markdown")
        taxes = _fee(inv, "taxes")
        other_fees = _fee(inv, "fees")
        load = _fee(inv, "load")
        withholding = _fee(inv, "withholding")
        penalty = _fee(inv, "penalty")
        state_withholding = _fee(inv, "statewithholding")
        accrdint = getattr(trade, "accrdint", None)

    return DebtTrade(
        fitid=inv.invtran.fitid,
        side=side,
        units=units,
        unit_price=unit_price,
        par_value=par_value,
        clean_amount=clean_amount,
        commission=commission,
        markup_markdown=markup_markdown,
        taxes=taxes,
        other_fees=other_fees,
        load=load,
        withholding=withholding,
        penalty=penalty,
        state_withholding=state_withholding,
        accrued_interest=(
            None if accrdint is None else _require_decimal(accrdint, "ACCRDINT")
        ),
        total=_require_decimal(inv.total, "TOTAL"),
        currency=_validate_currency(currency),
        trade_date=trade_date,
        settlement=settlement,
        settlement_explicit=settlement_explicit,
    )


###############################################################################
# Quantity allocation
###############################################################################
def allocate_by_quantity(total: Decimal, quantities: Sequence[Decimal]) -> list[Decimal]:
    """
    Split ``total`` in proportion to ``quantities``, rounded to cents.

    The rounding residual is assigned to the largest fractional remainder
    (ties broken by order), so the returned amounts sum exactly to ``total``.
    """
    total = money(total)
    quantities = [_require_decimal(q, "quantity") for q in quantities]
    if any(q < ZERO for q in quantities):
        raise LedgerError("Allocation quantities cannot be negative")
    quantity = sum(quantities, ZERO)
    if quantity <= ZERO:
        raise LedgerError("Allocation quantities must sum to a positive value")
    if len(quantities) == 1:
        return [total]

    exact = [total * q / quantity for q in quantities]
    rounded = [money(value) for value in exact]
    residual = total - sum(rounded, ZERO)
    if residual != ZERO:
        # Allocate the residual to the largest fractional part; deterministic.
        candidates = sorted(
            ((-(value - rounded_value), index) for index, (value, rounded_value) in enumerate(zip(exact, rounded))),
            reverse=False,
        )
        # candidates sorts by most negative remainder first = largest remainder.
        _remainder, index = candidates[0]
        rounded[index] += residual
    return rounded


###############################################################################
# Security cost ledger
###############################################################################
@dataclass(frozen=True)
class CostAllocation:
    """One leg of a cost entry: signed quantity and signed cost for a lot."""

    lot_id: str
    quantity: Decimal
    cost: Decimal


@dataclass(frozen=True)
class CostEntry:
    """Immutable journal entry in the security cost ledger."""

    entry_id: str
    fitid: str
    group_id: str
    kind: str  # BUY, SELL, TRANSFER, REVERSAL
    trade_date: datetime.date
    currency: str
    allocations: tuple[CostAllocation, ...]
    reverses: str | None = None
    memo: str = ""

    @property
    def total_cost(self) -> Decimal:
        return money(sum((a.cost for a in self.allocations), ZERO))


@dataclass(frozen=True)
class CostPosition:
    """Current quantity and cost for one lot."""

    lot_id: str
    quantity: Decimal
    cost: Decimal
    currency: str


class SecurityCostLedger:
    """
    Event-sourced security cost journal, in one original currency.

    Entries are immutable.  Balances are derived by folding all allocations;
    there is no mutable lot state to corrupt.  ``reverse`` books the exact
    negation of one entry's allocations.
    """

    def __init__(self, currency: str) -> None:
        self.currency = _validate_currency(currency)
        self._entries: list[CostEntry] = []
        self._counter = 0

    @property
    def entries(self) -> tuple[CostEntry, ...]:
        return tuple(self._entries)

    def _next_id(self, kind: str, fitid: str) -> str:
        self._counter += 1
        return f"SC-{self._counter:04d}-{kind}-{fitid}"

    def _append(
        self,
        *,
        fitid: str,
        group_id: str | None,
        kind: str,
        trade_date: datetime.date,
        allocations: Sequence[CostAllocation],
        reverses: str | None = None,
        memo: str = "",
    ) -> CostEntry:
        entry = CostEntry(
            entry_id=self._next_id(kind, fitid),
            fitid=fitid,
            group_id=group_id or fitid,
            kind=kind,
            trade_date=trade_date,
            currency=self.currency,
            allocations=tuple(allocations),
            reverses=reverses,
            memo=memo,
        )
        self._entries.append(entry)
        return entry

    def _fold(self, lot_id: str | None = None) -> dict[str, CostPosition]:
        quantities: dict[str, Decimal] = defaultdict(lambda: ZERO)
        costs: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for entry in self._entries:
            for allocation in entry.allocations:
                if lot_id is not None and allocation.lot_id != lot_id:
                    continue
                quantities[allocation.lot_id] += allocation.quantity
                costs[allocation.lot_id] += allocation.cost
        return {
            lot: CostPosition(
                lot_id=lot,
                quantity=quantities[lot],
                cost=money(costs[lot]),
                currency=self.currency,
            )
            for lot in quantities
        }

    def positions(self, *, include_closed: bool = False) -> dict[str, CostPosition]:
        positions = self._fold()
        if include_closed:
            return positions
        return {
            lot: position
            for lot, position in positions.items()
            if position.quantity != ZERO or position.cost != ZERO
        }

    def quantity_of(self, lot_id: str) -> Decimal:
        return self._fold(lot_id).get(
            lot_id, CostPosition(lot_id, ZERO, ZERO, self.currency)
        ).quantity

    def cost_of(self, lot_id: str) -> Decimal:
        return self._fold(lot_id).get(
            lot_id, CostPosition(lot_id, ZERO, ZERO, self.currency)
        ).cost

    def average_cost(self, lot_id: str) -> Decimal:
        quantity = self.quantity_of(lot_id)
        if quantity == ZERO:
            raise LedgerError(f"Lot {lot_id!r} is closed")
        return self.cost_of(lot_id) / quantity

    def post_buy(
        self,
        *,
        fitid: str,
        lot_id: str,
        units: Decimal,
        par_value: Decimal,
        unit_price: Decimal,
        convention: CostConvention = CostConvention.ALL_FEES,
        commission: Decimal = ZERO,
        markup: Decimal = ZERO,
        taxes: Decimal = ZERO,
        fees: Decimal = ZERO,
        load: Decimal = ZERO,
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> CostEntry:
        """
        Open or add to a lot from a purchase.

        Cost is the clean amount plus capitalized fees per ``convention``;
        accrued interest must be posted to :class:`InterestLedger` separately.
        """
        units = _require_decimal(units, "units", positive=True)
        par_value = _require_decimal(par_value, "par_value", positive=True)
        unit_price = _require_decimal(unit_price, "unit_price", positive=True)
        clean = money(par_value * units * unit_price / HUNDRED)
        trade = DebtTrade(
            fitid=fitid,
            side="BUY",
            units=units,
            unit_price=unit_price,
            par_value=par_value,
            clean_amount=clean,
            commission=_require_decimal(commission, "commission"),
            markup_markdown=_require_decimal(markup, "markup"),
            taxes=_require_decimal(taxes, "taxes"),
            other_fees=_require_decimal(fees, "fees"),
            load=_require_decimal(load, "load"),
            withholding=ZERO,
            penalty=ZERO,
            state_withholding=ZERO,
            accrued_interest=None,
            total=-clean,
            currency=self.currency,
            trade_date=trade_date,
            settlement=trade_date,
        )
        capitalized = trade.cost_fees(CostConvention(convention))
        cost = money(clean + capitalized)
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="BUY",
            trade_date=trade_date,
            allocations=(
                CostAllocation(lot_id=lot_id, quantity=units, cost=cost),
            ),
        )

    def post_sell(
        self,
        *,
        fitid: str,
        lot_id: str,
        units: Decimal,
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> CostEntry:
        """
        Remove ``units`` from a lot and release average cost.

        Partial sales do not change the remaining lot's per-unit cost.
        """
        units = _require_decimal(units, "units", positive=True)
        held = self.quantity_of(lot_id)
        if units > held:
            raise ReversalError(
                f"Sale of {units} exceeds lot {lot_id!r} quantity {held}"
            )
        released = money(self.cost_of(lot_id) * units / held)
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="SELL",
            trade_date=trade_date,
            allocations=(
                CostAllocation(lot_id=lot_id, quantity=-units, cost=-released),
            ),
        )

    def transfer(
        self,
        *,
        fitid: str,
        from_lot: str,
        to_lot: str,
        units: Decimal,
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> CostEntry:
        """
        Move ``units`` from one lot to another, allocating cost by quantity.

        The source lot keeps the exact complement of the moved cost, so the
        pair always sums to the source's pre-transfer cost.
        """
        units = _require_decimal(units, "units", positive=True)
        if from_lot == to_lot:
            raise LedgerError("from_lot and to_lot must differ")
        held = self.quantity_of(from_lot)
        if units > held:
            raise ReversalError(
                f"Transfer of {units} exceeds lot {from_lot!r} quantity {held}"
            )
        if self.quantity_of(to_lot) != ZERO:
            raise LedgerError(f"Destination lot {to_lot!r} already has a quantity")
        moved = money(self.cost_of(from_lot) * units / held)
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="TRANSFER",
            trade_date=trade_date,
            allocations=(
                CostAllocation(lot_id=from_lot, quantity=-units, cost=-moved),
                CostAllocation(lot_id=to_lot, quantity=units, cost=moved),
            ),
        )

    def split(
        self,
        *,
        fitid: str,
        lot_id: str,
        parts: Mapping[str, Decimal],
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> CostEntry:
        """
        Split off several lots from one lot by quantity.

        ``parts`` maps each new lot ID to the quantity moved.  The moved cost
        is allocated across parts with an exact residual; the source lot keeps
        any remainder.  The returned entry has one negative source leg and one
        positive leg per new lot.
        """
        held = self.quantity_of(lot_id)
        quantities = [_require_decimal(q, "quantity", positive=True) for q in parts.values()]
        total_units = sum(quantities, ZERO)
        if total_units > held:
            raise ReversalError(
                f"Split quantity {total_units} exceeds lot {lot_id!r} quantity {held}"
            )
        for new_lot in parts:
            if new_lot == lot_id:
                raise LedgerError("Split destination cannot be the source lot")
            if self.quantity_of(new_lot) != ZERO:
                raise LedgerError(f"Destination lot {new_lot!r} already has a quantity")

        moved_cost = money(self.cost_of(lot_id) * total_units / held)
        allocated = allocate_by_quantity(moved_cost, quantities)
        allocations = [
            CostAllocation(lot_id=lot_id, quantity=-total_units, cost=-moved_cost)
        ]
        allocations.extend(
            CostAllocation(lot_id=new_lot, quantity=quantity, cost=cost)
            for (new_lot, quantity), cost in zip(parts.items(), allocated)
        )
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="SPLIT",
            trade_date=trade_date,
            allocations=tuple(allocations),
        )

    def reversed_entry_ids(self) -> set[str]:
        return {entry.reverses for entry in self._entries if entry.reverses}

    def reversal_entries(self, entry_id: str) -> list[CostEntry]:
        return [entry for entry in self._entries if entry.reverses == entry_id]

    def find_entry(self, entry_id: str) -> CostEntry:
        for entry in self._entries:
            if entry.entry_id == entry_id:
                return entry
        raise ReversalError(f"Unknown cost entry {entry_id!r}")

    def _later_entries_touching(self, original: CostEntry) -> list[CostEntry]:
        """
        Entries booked after ``original`` that touch any lot it allocates to.

        Reversing an entry whose lots have since moved would restore cost to a
        lot that no longer holds it, so such reversals are rejected.
        """
        lots = {allocation.lot_id for allocation in original.allocations}
        index = self._entries.index(original)
        later = []
        for entry in self._entries[index + 1 :]:
            if entry.kind == "REVERSAL" and entry.reverses == original.entry_id:
                continue
            if any(allocation.lot_id in lots for allocation in entry.allocations):
                later.append(entry)
        return later

    def reverse(
        self,
        *,
        entry_id: str,
        fitid: str,
        trade_date: datetime.date | None = None,
        group_id: str | None = None,
    ) -> CostEntry:
        """
        Reverse exactly one cost entry.

        The reversal posts negated allocations of the original only.  It is
        rejected if that would drive any lot quantity or cost negative, if the
        entry has already been reversed, or if a later entry has touched the
        same lot (which would make the original allocation stale).
        """
        original = self.find_entry(entry_id)
        if original.kind == "REVERSAL":
            raise ReversalError("Cannot reverse a reversal")
        if self.reversal_entries(entry_id):
            raise ReversalError(f"Entry {entry_id} has already been reversed")
        later = self._later_entries_touching(original)
        if later:
            raise ReversalError(
                f"Entry {entry_id} is stale: later entries "
                f"{[entry.entry_id for entry in later]} have touched its lots; "
                "reverse those first"
            )

        positions = self._fold()
        for allocation in original.allocations:
            position = positions.get(allocation.lot_id)
            if position is None:
                raise ReversalError(
                    f"Lot {allocation.lot_id!r} no longer exists"
                )
            new_quantity = position.quantity - allocation.quantity
            new_cost = position.cost - allocation.cost
            if new_quantity < ZERO or new_cost < ZERO:
                raise ReversalError(
                    f"Reversing {entry_id} would make lot {allocation.lot_id!r} "
                    f"negative (quantity {new_quantity}, cost {new_cost})"
                )

        return self._append(
            fitid=fitid,
            group_id=group_id or original.group_id,
            kind="REVERSAL",
            trade_date=trade_date or original.trade_date,
            allocations=tuple(
                CostAllocation(
                    lot_id=allocation.lot_id,
                    quantity=-allocation.quantity,
                    cost=-allocation.cost,
                )
                for allocation in original.allocations
            ),
            reverses=entry_id,
        )

    def reverse_group(
        self,
        *,
        group_id: str,
        fitid: str,
        trade_date: datetime.date | None = None,
    ) -> list[CostEntry]:
        """
        Reverse all not-yet-reversed entries in a trade group, newest first.

        Newest-first ordering avoids transient negative positions when a group
        contains multiple allocations to the same lot.
        """
        reversed_ids = self.reversed_entry_ids()
        targets = [
            entry
            for entry in self._entries
            if entry.group_id == group_id
            and entry.kind != "REVERSAL"
            and entry.entry_id not in reversed_ids
        ]
        reversals = []
        for entry in reversed(targets):
            reversals.append(
                self.reverse(
                    entry_id=entry.entry_id,
                    fitid=fitid,
                    trade_date=trade_date,
                    group_id=group_id,
                )
            )
        return reversals


###############################################################################
# Interest ledger
###############################################################################
@dataclass(frozen=True)
class InterestAllocation:
    """One leg of an interest entry: signed amount for a lot.

    Positive means interest income/benefit to the holder; negative means
    interest cost paid (for example accrued interest paid to a seller).
    """

    lot_id: str
    amount: Decimal


@dataclass(frozen=True)
class InterestEntry:
    """Immutable journal entry in the interest ledger."""

    entry_id: str
    fitid: str
    group_id: str
    kind: str  # ACCRUED_BUY, ACCRUED_SELL, COUPON, ACCRUAL, TRANSFER, RELEASE, REVERSAL
    trade_date: datetime.date
    currency: str
    allocations: tuple[InterestAllocation, ...]
    period_start: datetime.date | None = None
    period_end: datetime.date | None = None
    reverses: str | None = None
    memo: str = ""

    @property
    def amount(self) -> Decimal:
        return money(sum((a.amount for a in self.allocations), ZERO))


class InterestLedger:
    """
    Event-sourced coupon/accrued-interest journal, separate from cost.

    Sign convention: positive is interest income to the holder, negative is
    interest cost.  Thus accrued interest paid on a purchase is negative;
    accrued interest received on a sale and coupon cash are positive.
    """

    _ACCRUED_PAID_KINDS = frozenset({"ACCRUED_BUY", "TRANSFER", "RELEASE"})

    def __init__(self, currency: str) -> None:
        self.currency = _validate_currency(currency)
        self._entries: list[InterestEntry] = []
        self._counter = 0

    @property
    def entries(self) -> tuple[InterestEntry, ...]:
        return tuple(self._entries)

    def _next_id(self, kind: str, fitid: str) -> str:
        self._counter += 1
        return f"IL-{self._counter:04d}-{kind}-{fitid}"

    def _append(
        self,
        *,
        fitid: str,
        group_id: str | None,
        kind: str,
        trade_date: datetime.date,
        allocations: Sequence[InterestAllocation],
        period_start: datetime.date | None = None,
        period_end: datetime.date | None = None,
        reverses: str | None = None,
        memo: str = "",
    ) -> InterestEntry:
        entry = InterestEntry(
            entry_id=self._next_id(kind, fitid),
            fitid=fitid,
            group_id=group_id or fitid,
            kind=kind,
            trade_date=trade_date,
            currency=self.currency,
            allocations=tuple(allocations),
            period_start=period_start,
            period_end=period_end,
            reverses=reverses,
            memo=memo,
        )
        self._entries.append(entry)
        return entry

    def _fold(
        self,
        lot_id: str | None = None,
        *,
        kinds: Iterable[str] | None = None,
    ) -> dict[str, Decimal]:
        allowed = None if kinds is None else frozenset(kinds)
        balances: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for entry in self._entries:
            kind = self._effective_kind(entry)
            if allowed is not None and kind not in allowed:
                continue
            for allocation in entry.allocations:
                if lot_id is not None and allocation.lot_id != lot_id:
                    continue
                balances[allocation.lot_id] += allocation.amount
        return {lot: money(amount) for lot, amount in balances.items()}

    def _effective_kind(self, entry: InterestEntry) -> str:
        """
        Kind used when folding balances.

        A reversal inherits the kind of the entry it reverses, so filtering by
        kind (for example ``_ACCRUED_PAID_KINDS``) treats an original entry and
        its reversal symmetrically.
        """
        if entry.kind != "REVERSAL" or entry.reverses is None:
            return entry.kind
        for candidate in self._entries:
            if candidate.entry_id == entry.reverses:
                return candidate.kind
        return entry.kind

    def balance(self, lot_id: str | None = None) -> Decimal:
        """
        Net interest amount for a lot (or all lots).

        This nets every interest entry, including transfers and releases of
        accrued-paid.  Transfers are zero-sum across lots, so a per-lot balance
        follows the units that carry the accrued interest; a release is
        offset by the accrued interest received on the same sale, leaving the
        trade's net interest effect intact.
        """
        return money(sum(self._fold(lot_id).values(), ZERO))

    def accrued_paid(self, lot_id: str) -> Decimal:
        """
        Accrued interest paid that is still attached to ``lot_id``.

        Positive value; transfer and sale releases reduce it proportionally.
        """
        balances = self._fold(lot_id, kinds=self._ACCRUED_PAID_KINDS)
        return money(-balances.get(lot_id, ZERO))

    def accrued_sell(self, *, fitid: str, lot_id: str, amount: Decimal,
                     trade_date: datetime.date,
                     period_start: datetime.date | None = None,
                     period_end: datetime.date | None = None,
                     group_id: str | None = None) -> InterestEntry:
        amount = _require_decimal(amount, "amount")
        if amount < ZERO:
            raise LedgerError("Accrued interest cannot be negative")
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="ACCRUED_SELL",
            trade_date=trade_date,
            allocations=(InterestAllocation(lot_id, amount),),
            period_start=period_start,
            period_end=period_end,
        )

    def accrued_buy(self, *, fitid: str, lot_id: str, amount: Decimal,
                    trade_date: datetime.date,
                    period_start: datetime.date | None = None,
                    period_end: datetime.date | None = None,
                    group_id: str | None = None) -> InterestEntry:
        amount = _require_decimal(amount, "amount")
        if amount < ZERO:
            raise LedgerError("Accrued interest cannot be negative")
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="ACCRUED_BUY",
            trade_date=trade_date,
            allocations=(InterestAllocation(lot_id, -amount),),
            period_start=period_start,
            period_end=period_end,
        )

    def coupon(self, *, fitid: str, lot_id: str, amount: Decimal,
               trade_date: datetime.date,
               period_start: datetime.date,
               period_end: datetime.date,
               group_id: str | None = None) -> InterestEntry:
        amount = _require_decimal(amount, "amount")
        if amount < ZERO:
            raise LedgerError("Coupon cannot be negative")
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="COUPON",
            trade_date=trade_date,
            allocations=(InterestAllocation(lot_id, amount),),
            period_start=period_start,
            period_end=period_end,
        )

    def accrue(self, *, fitid: str, lot_id: str, amount: Decimal,
               trade_date: datetime.date,
               period_start: datetime.date | None = None,
               period_end: datetime.date | None = None,
               group_id: str | None = None) -> InterestEntry:
        """
        Book an accrual for reporting.

        ``amount`` is positive earned interest.  This does not replace a
        coupon entry: reverse or net the accrual when the coupon is paid, so
        the same interest is not counted twice.
        """
        amount = _require_decimal(amount, "amount")
        if amount < ZERO:
            raise LedgerError("Accrual cannot be negative")
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="ACCRUAL",
            trade_date=trade_date,
            allocations=(InterestAllocation(lot_id, amount),),
            period_start=period_start,
            period_end=period_end,
        )

    def transfer_accrued(
        self,
        *,
        fitid: str,
        from_lot: str,
        to_lot: str,
        units: Decimal,
        from_units: Decimal,
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> InterestEntry:
        """
        Move the proportional accrued interest paid from one lot to another.

        The amount is based on the source lot's remaining accrued-paid balance
        and allocated by quantity.  A zero balance simply books a zero entry.
        """
        units = _require_decimal(units, "units", positive=True)
        from_units = _require_decimal(from_units, "from_units", positive=True)
        if units > from_units:
            raise ReversalError(
                f"Transfer of {units} exceeds source quantity {from_units}"
            )
        if from_lot == to_lot:
            raise LedgerError("from_lot and to_lot must differ")

        paid = self.accrued_paid(from_lot)
        moved = money(paid * units / from_units)
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="TRANSFER",
            trade_date=trade_date,
            allocations=(
                # Source's negative accrued-paid balance shrinks (less negative).
                InterestAllocation(lot_id=from_lot, amount=moved),
                # Destination takes on the paid balance (more negative).
                InterestAllocation(lot_id=to_lot, amount=-moved),
            ),
        )

    def release_accrued(
        self,
        *,
        fitid: str,
        lot_id: str,
        units: Decimal,
        from_units: Decimal,
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> InterestEntry:
        """
        Remove the accrued-paid share belonging to units sold from a lot.

        This keeps the remaining lot's accrued-paid balance proportional to
        its remaining quantity.
        """
        units = _require_decimal(units, "units", positive=True)
        from_units = _require_decimal(from_units, "from_units", positive=True)
        if units > from_units:
            raise ReversalError(f"Release of {units} exceeds quantity {from_units}")
        if units == from_units:
            released = self.accrued_paid(lot_id)
        else:
            released = money(self.accrued_paid(lot_id) * units / from_units)
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="RELEASE",
            trade_date=trade_date,
            allocations=(InterestAllocation(lot_id=lot_id, amount=released),),
        )

    def split_accrued(
        self,
        *,
        fitid: str,
        lot_id: str,
        parts: Mapping[str, Decimal],
        from_units: Decimal,
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> InterestEntry:
        """Split accrued-paid across several new lots by quantity."""
        from_units = _require_decimal(from_units, "from_units", positive=True)
        quantities = [_require_decimal(q, "quantity", positive=True) for q in parts.values()]
        total_units = sum(quantities, ZERO)
        if total_units > from_units:
            raise ReversalError(
                f"Split quantity {total_units} exceeds source quantity {from_units}"
            )
        paid = self.accrued_paid(lot_id)
        moved = money(paid * total_units / from_units)
        allocated = allocate_by_quantity(moved, quantities)
        allocations = [InterestAllocation(lot_id=lot_id, amount=moved)]
        allocations.extend(
            InterestAllocation(lot_id=new_lot, amount=-cost)
            for (new_lot, _), cost in zip(parts.items(), allocated)
        )
        return self._append(
            fitid=fitid,
            group_id=group_id,
            kind="TRANSFER",
            trade_date=trade_date,
            allocations=tuple(allocations),
        )

    def reversed_entry_ids(self) -> set[str]:
        return {entry.reverses for entry in self._entries if entry.reverses}

    def reversal_entries(self, entry_id: str) -> list[InterestEntry]:
        return [entry for entry in self._entries if entry.reverses == entry_id]

    def find_entry(self, entry_id: str) -> InterestEntry:
        for entry in self._entries:
            if entry.entry_id == entry_id:
                return entry
        raise ReversalError(f"Unknown interest entry {entry_id!r}")

    def reverse(
        self,
        *,
        entry_id: str,
        fitid: str,
        trade_date: datetime.date | None = None,
        group_id: str | None = None,
    ) -> InterestEntry:
        """Reverse exactly one interest entry with negated allocations."""
        original = self.find_entry(entry_id)
        if original.kind == "REVERSAL":
            raise ReversalError("Cannot reverse a reversal")
        if self.reversal_entries(entry_id):
            raise ReversalError(f"Entry {entry_id} has already been reversed")
        return self._append(
            fitid=fitid,
            group_id=group_id or original.group_id,
            kind="REVERSAL",
            trade_date=trade_date or original.trade_date,
            allocations=tuple(
                InterestAllocation(lot_id=a.lot_id, amount=-a.amount)
                for a in original.allocations
            ),
            period_start=original.period_start,
            period_end=original.period_end,
            reverses=entry_id,
        )

    def reverse_group(
        self,
        *,
        group_id: str,
        fitid: str,
        trade_date: datetime.date | None = None,
    ) -> list[InterestEntry]:
        reversed_ids = self.reversed_entry_ids()
        targets = [
            entry
            for entry in self._entries
            if entry.group_id == group_id
            and entry.kind != "REVERSAL"
            and entry.entry_id not in reversed_ids
        ]
        return [
            self.reverse(
                entry_id=entry.entry_id,
                fitid=fitid,
                trade_date=trade_date,
                group_id=group_id,
            )
            for entry in reversed(targets)
        ]


###############################################################################
# Bond book
###############################################################################
@dataclass(frozen=True)
class PostingResult:
    """Result of posting a trade into both ledgers."""

    trade: DebtTrade
    cost_entry: CostEntry
    interest_entry: InterestEntry | None


@dataclass(frozen=True)
class ReversalResult:
    """Result of reversing a trade group."""

    group_id: str
    cost_entries: tuple[CostEntry, ...]
    interest_entries: tuple[InterestEntry, ...]


@dataclass(frozen=True)
class BondPosition:
    """Combined view of one bond lot across both ledgers."""

    lot_id: str
    units: Decimal
    par_value: Decimal
    cost: Decimal
    accrued_interest_paid: Decimal
    net_interest: Decimal
    currency: str


class BondBook:
    """
    Facade that posts OFX debt trades into a cost ledger and an interest ledger.

    ``schedule`` and ``coupon_rate`` are optional but strongly recommended:
    when supplied, every ``ACCRDINT`` is checked against the schedule and every
    coupon/accrual calculation uses the documented day count.  Without them,
    the OFX ``ACCRDINT`` is still reconciled to ``TOTAL`` but cannot be
    independently validated.
    """

    def __init__(
        self,
        cost: SecurityCostLedger,
        interest: InterestLedger,
        *,
        schedule: CouponSchedule | None = None,
        coupon_rate: Decimal | None = None,
        ex_dividend_days: int = 0,
    ) -> None:
        if cost.currency != interest.currency:
            raise LedgerError(
                f"Cost ledger {cost.currency} and interest ledger "
                f"{interest.currency} must share a currency"
            )
        if schedule is not None and coupon_rate is None:
            raise LedgerError("coupon_rate is required with a coupon schedule")
        if coupon_rate is not None:
            coupon_rate = _require_decimal(coupon_rate, "coupon_rate")
        if ex_dividend_days < 0:
            raise LedgerError("ex_dividend_days cannot be negative")
        self.cost = cost
        self.interest = interest
        self.schedule = schedule
        self.coupon_rate = coupon_rate
        self.ex_dividend_days = ex_dividend_days
        self._par_values: dict[str, Decimal] = {}

    @classmethod
    def from_currency(
        cls,
        currency: str,
        *,
        schedule: CouponSchedule | None = None,
        coupon_rate: Decimal | None = None,
        ex_dividend_days: int = 0,
    ) -> BondBook:
        return cls(
            SecurityCostLedger(currency),
            InterestLedger(currency),
            schedule=schedule,
            coupon_rate=coupon_rate,
            ex_dividend_days=ex_dividend_days,
        )

    @property
    def currency(self) -> str:
        return self.cost.currency

    def _register_par(self, lot_id: str, par_value: Decimal) -> None:
        existing = self._par_values.get(lot_id)
        if existing is not None and existing != par_value:
            raise LedgerError(
                f"Lot {lot_id!r} already has par_value {existing}, not {par_value}"
            )
        self._par_values[lot_id] = par_value

    def par_value(self, lot_id: str) -> Decimal:
        try:
            return self._par_values[lot_id]
        except KeyError:
            raise MissingFieldError(
                f"par_value for lot {lot_id!r} is unknown; pass it when posting the buy"
            ) from None

    def _verify_accrued(
        self, trade: DebtTrade, par_value: Decimal, units: Decimal
    ) -> Decimal:
        if trade.accrued_interest is None:
            raise MissingAccruedInterestError(
                f"{trade.fitid}: ACCRDINT is missing; accrued interest cannot be "
                "computed without a schedule and is never inferred from TOTAL"
            )

        if self.schedule is not None and self.coupon_rate is not None:
            expected = self.schedule.accrued_interest(
                trade.settlement,
                par_value=par_value,
                units=units,
                coupon_rate=self.coupon_rate,
                ex_dividend_days=self.ex_dividend_days,
            )
            if expected != money(trade.accrued_interest):
                raise ReconciliationError(
                    f"{trade.fitid}: ACCRDINT {trade.accrued_interest} disagrees "
                    f"with coupon calendar ({expected}) at settlement "
                    f"{trade.settlement}"
                )
        return trade.accrued_interest

    def _reconcile(self, trade: DebtTrade, expected: Decimal) -> None:
        expected = money(expected)
        reported = money(trade.total)
        if expected != reported:
            raise ReconciliationError(
                f"{trade.fitid}: components imply TOTAL {expected}, OFX reports "
                f"{reported} (difference {money(reported - expected)}); "
                "missing fields are not inferred from the total"
            )

    def post_buydebt(
        self,
        trade: Any,
        par_value: Decimal,
        *,
        convention: CostConvention = CostConvention.ALL_FEES,
        lot_id: str | None = None,
        require_accrued: bool = False,
        group_id: str | None = None,
    ) -> PostingResult:
        """
        Post a ``BUYDEBT`` (or ``INVBUY``) into both ledgers.

        The trade's ``TOTAL`` must equal ``-(clean + fees + accrued)``.  When
        ``ACCRDINT`` is absent, it must equal ``-(clean + fees)``; otherwise
        :class:`MissingAccruedInterestError` is raised.  It is never solved
        for as a plug.
        """
        normalized = parse_debt_trade(trade, par_value)
        if normalized.currency != self.currency:
            raise LedgerError(
                f"Trade currency {normalized.currency} != book currency {self.currency}"
            )
        if normalized.side != "BUY":
            raise LedgerError(f"{normalized.fitid}: expected a BUY trade")

        if normalized.accrued_interest is None:
            if require_accrued:
                raise MissingAccruedInterestError(
                    f"{normalized.fitid}: ACCRDINT is required for this posting"
                )
            expected = normalized.expected_total(accrued=ZERO)
            if money(expected) != money(normalized.total):
                raise MissingAccruedInterestError(
                    f"{normalized.fitid}: ACCRDINT is missing and TOTAL "
                    f"{normalized.total} cannot be reconciled from clean amount "
                    f"+ fees alone (implied {money(expected)}); accrued interest "
                    "is not inferred from the difference"
                )
            accrued = ZERO
            period_start = period_end = None
            interest_entry = None
        else:
            accrued = self._verify_accrued(
                normalized, par_value, normalized.units
            )
            self._reconcile(normalized, normalized.expected_total(accrued=accrued))
            period = (
                self.schedule.period_containing(normalized.settlement)
                if self.schedule is not None
                else None
            )
            period_start = period.start if period is not None else None
            period_end = period.end if period is not None else None
            interest_entry = None

        lot_id = lot_id or normalized.fitid
        if self.cost.quantity_of(lot_id) != ZERO:
            raise LedgerError(f"Lot {lot_id!r} already exists")

        cost_entry = self.cost.post_buy(
            fitid=normalized.fitid,
            lot_id=lot_id,
            units=normalized.units,
            par_value=par_value,
            unit_price=normalized.unit_price,
            convention=convention,
            commission=normalized.commission,
            markup=normalized.markup_markdown,
            taxes=normalized.taxes,
            fees=normalized.other_fees,
            load=normalized.load,
            trade_date=normalized.trade_date,
            group_id=group_id,
        )
        self._register_par(lot_id, par_value)

        if normalized.accrued_interest is not None:
            interest_entry = self.interest.accrued_buy(
                fitid=normalized.fitid,
                lot_id=lot_id,
                amount=accrued,
                trade_date=normalized.trade_date,
                period_start=period_start,
                period_end=period_end,
                group_id=group_id,
            )

        return PostingResult(
            trade=normalized,
            cost_entry=cost_entry,
            interest_entry=interest_entry,
        )

    def post_selldebt(
        self,
        trade: Any,
        par_value: Decimal,
        *,
        lot_id: str,
        require_accrued: bool = False,
        group_id: str | None = None,
    ) -> PostingResult:
        """
        Post a ``SELLDEBT`` (or ``INVSELL``) against a lot.

        ``UNITS`` on the trade is the quantity sold, which may be less than the
        lot's holding; the released security cost comes from that lot's average
        cost and is allocated by quantity.  Accrued interest received goes only
        to the interest ledger.
        """
        normalized = parse_debt_trade(trade, par_value)
        if normalized.currency != self.currency:
            raise LedgerError(
                f"Trade currency {normalized.currency} != book currency {self.currency}"
            )
        if normalized.side != "SELL":
            raise LedgerError(f"{normalized.fitid}: expected a SELL trade")

        units = normalized.units
        held = self.cost.quantity_of(lot_id)
        if units > held:
            raise ReversalError(
                f"Sale of {units} exceeds lot {lot_id!r} quantity {held}"
            )
        if self._par_values.get(lot_id) is None:
            self._par_values[lot_id] = par_value

        if normalized.accrued_interest is None:
            if require_accrued:
                raise MissingAccruedInterestError(
                    f"{normalized.fitid}: ACCRDINT is required for this posting"
                )
            expected = normalized.expected_total(accrued=ZERO)
            if money(expected) != money(normalized.total):
                raise MissingAccruedInterestError(
                    f"{normalized.fitid}: ACCRDINT is missing and TOTAL "
                    f"{normalized.total} cannot be reconciled from clean amount "
                    f"- fees alone (implied {money(expected)}); accrued interest "
                    "is not inferred from the difference"
                )
            accrued = ZERO
            period_start = period_end = None
            interest_entry = None
        else:
            accrued = self._verify_accrued(normalized, par_value, units)
            self._reconcile(normalized, normalized.expected_total(accrued=accrued))
            period = (
                self.schedule.period_containing(normalized.settlement)
                if self.schedule is not None
                else None
            )
            period_start = period.start if period is not None else None
            period_end = period.end if period is not None else None
            interest_entry = None

        cost_entry = self.cost.post_sell(
            fitid=normalized.fitid,
            lot_id=lot_id,
            units=units,
            trade_date=normalized.trade_date,
            group_id=group_id,
        )

        # The sold units' share of accrued interest paid is released by
        # quantity, independent of whether the trade reported ACCRDINT.  This
        # keeps the remaining lot proportional without inferring any amount
        # from TOTAL.
        self.interest.release_accrued(
            fitid=normalized.fitid,
            lot_id=lot_id,
            units=units,
            from_units=held,
            trade_date=normalized.trade_date,
            group_id=group_id,
        )

        if normalized.accrued_interest is not None:
            interest_entry = self.interest.accrued_sell(
                fitid=normalized.fitid,
                lot_id=lot_id,
                amount=accrued,
                trade_date=normalized.trade_date,
                period_start=period_start,
                period_end=period_end,
                group_id=group_id,
            )

        return PostingResult(
            trade=normalized,
            cost_entry=cost_entry,
            interest_entry=interest_entry,
        )

    def post_transfer(
        self,
        *,
        fitid: str,
        from_lot: str,
        to_lot: str,
        units: Decimal,
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> tuple[CostEntry, InterestEntry]:
        """
        Transfer part of a lot out, allocating cost and accrued interest paid
        by quantity.
        """
        units = _require_decimal(units, "units", positive=True)
        held = self.cost.quantity_of(from_lot)
        if units > held:
            raise ReversalError(
                f"Transfer of {units} exceeds lot {from_lot!r} quantity {held}"
            )
        par = self.par_value(from_lot)
        cost_entry = self.cost.transfer(
            fitid=fitid,
            from_lot=from_lot,
            to_lot=to_lot,
            units=units,
            trade_date=trade_date,
            group_id=group_id,
        )
        interest_entry = self.interest.transfer_accrued(
            fitid=fitid,
            from_lot=from_lot,
            to_lot=to_lot,
            units=units,
            from_units=held,
            trade_date=trade_date,
            group_id=group_id,
        )
        self._par_values[to_lot] = par
        return cost_entry, interest_entry

    def split_position(
        self,
        *,
        fitid: str,
        lot_id: str,
        parts: Mapping[str, Decimal],
        trade_date: datetime.date,
        group_id: str | None = None,
    ) -> tuple[CostEntry, InterestEntry]:
        """
        Split one lot into several by quantity, allocating cost and accrued
        interest paid with an exact residual.
        """
        held = self.cost.quantity_of(lot_id)
        par = self.par_value(lot_id)
        cost_entry = self.cost.split(
            fitid=fitid,
            lot_id=lot_id,
            parts=parts,
            trade_date=trade_date,
            group_id=group_id,
        )
        interest_entry = self.interest.split_accrued(
            fitid=fitid,
            lot_id=lot_id,
            parts=parts,
            from_units=held,
            trade_date=trade_date,
            group_id=group_id,
        )
        for new_lot in parts:
            self._par_values[new_lot] = par
        return cost_entry, interest_entry

    def post_coupon(
        self,
        *,
        fitid: str,
        lot_id: str,
        coupon_date: datetime.date,
        payment_date: datetime.date | None = None,
        units: Decimal | None = None,
        group_id: str | None = None,
    ) -> InterestEntry:
        """
        Book a coupon receipt for a lot (or a quantity within it).

        Requires ``schedule`` and ``coupon_rate``.
        """
        if self.schedule is None or self.coupon_rate is None:
            raise MissingFieldError(
                "Coupon posting requires a coupon schedule and coupon_rate"
            )
        period = self.schedule.period_ending(coupon_date)
        held = self.cost.quantity_of(lot_id)
        units = held if units is None else _require_decimal(units, "units")
        if units <= ZERO or units > held:
            raise LedgerError(
                f"Coupon units {units} must be positive and not exceed lot {lot_id!r} "
                f"quantity {held}"
            )
        amount = period.coupon_amount(
            self.par_value(lot_id), units, self.coupon_rate
        )
        return self.interest.coupon(
            fitid=fitid,
            lot_id=lot_id,
            amount=money(amount),
            trade_date=payment_date or period.payment,
            period_start=period.start,
            period_end=period.end,
            group_id=group_id,
        )

    def accrued_interest(
        self, lot_id: str, as_of: datetime.date, *, units: Decimal | None = None
    ) -> Decimal:
        """
        Accrued interest for a lot at ``as_of`` using the configured calendar.

        This is a calculation only; it does not book an entry.  It returns zero
        inside the ex-coupon window.
        """
        if self.schedule is None or self.coupon_rate is None:
            raise MissingFieldError(
                "Accrued interest requires a coupon schedule and coupon_rate"
            )
        held = self.cost.quantity_of(lot_id)
        units = held if units is None else _require_decimal(units, "units")
        return self.schedule.accrued_interest(
            as_of,
            par_value=self.par_value(lot_id),
            units=units,
            coupon_rate=self.coupon_rate,
            ex_dividend_days=self.ex_dividend_days,
        )

    def position(self, lot_id: str) -> BondPosition:
        return BondPosition(
            lot_id=lot_id,
            units=self.cost.quantity_of(lot_id),
            par_value=self.par_value(lot_id),
            cost=self.cost.cost_of(lot_id),
            accrued_interest_paid=self.interest.accrued_paid(lot_id),
            net_interest=self.interest.balance(lot_id),
            currency=self.currency,
        )

    def positions(self) -> dict[str, BondPosition]:
        return {
            lot_id: self.position(lot_id) for lot_id in self.cost.positions()
        }

    def reverse_trade(
        self,
        *,
        original_fitid: str,
        correction_fitid: str,
        trade_date: datetime.date | None = None,
    ) -> ReversalResult:
        """
        Reverse every not-yet-reversed entry posted under ``original_fitid``.

        Each entry is reversed using its own stored allocations; no other
        trade's lots, costs, or interest are recomputed or touched.  Entries
        are processed newest-first, and the operation is rejected if a
        reversal would make a lot negative.
        """
        cost_entries = self.cost.reverse_group(
            group_id=original_fitid,
            fitid=correction_fitid,
            trade_date=trade_date,
        )
        interest_entries = self.interest.reverse_group(
            group_id=original_fitid,
            fitid=correction_fitid,
            trade_date=trade_date,
        )
        return ReversalResult(
            group_id=original_fitid,
            cost_entries=tuple(cost_entries),
            interest_entries=tuple(interest_entries),
        )
