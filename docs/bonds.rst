.. _bonds:

Bond Transaction Accounting
===========================
OFX represents bond trades as ``BUYDEBT`` / ``SELLDEBT`` wrapping ``INVBUY`` /
``INVSELL``.  The aggregates carry a **clean price** (``UNITPRICE``, a
percentage of par), an optional **accrued interest** element (``ACCRDINT``)
that is separate from price, transaction fees, and a signed cash ``TOTAL``.

``ofxtools.bonds`` turns those trades into auditable accounting entries.  It
is deliberately separate from ``ofxtools.models``: models describe the wire
format, while this module fixes the accounting conventions that OFX leaves
open.

Two ledgers, never mixed
------------------------
Security cost and interest income are posted to different journals:

* ``SecurityCostLedger`` — clean amount plus capitalized fees, in original
  currency.  It knows nothing about coupon accrual.
* ``InterestLedger`` — accrued interest paid/received and coupon cash, also
  in original currency.  It never contains price or fees.

``BondBook`` wires the two together and is the recommended entry point.

Cost convention
---------------
Every convention starts from the traded clean amount::

    clean_amount = par_value * units * unit_price / 100

``unit_price`` is the OFX ``UNITPRICE``: a percent of par, not a cash price.
``par_value`` comes from ``DEBTINFO.PARVALUE`` (OFX does not repeat par on the
trade), so it must be supplied when posting.

``CostConvention`` then selects which fees are capitalized:

======================  =========================================
Convention              Capitalized into security cost
======================  =========================================
``CLEAN_PRICE``         nothing (price only)
``CLEAN_PLUS_COMMISSION``  ``COMMISSION``
``ALL_FEES``            commission, markup/markdown, taxes, fees, load
======================  =========================================

Accrued interest is **never** capitalized, under any convention.  On a sale,
withholdings and penalties are always period expenses; they reduce cash but do
not alter cost.

Original-currency reconciliation
--------------------------------
Before anything is booked, the trade is reconciled to the cent in its own
currency::

    BUY:   TOTAL == -(clean_amount + cash_fees + accrued_interest)
    SELL:  TOTAL == +(clean_amount + accrued_interest - cash_fees)

If ``ACCRDINT`` is present but disagrees with the configured coupon calendar,
or the components disagree with ``TOTAL``, ``ReconciliationError`` is raised.

If ``ACCRDINT`` is **absent**, the trade is reconciled *without* it.  When the
reported ``TOTAL`` cannot then be explained by clean amount and fees alone,
``MissingAccruedInterestError`` is raised.  The module will not back-solve the
interest as ``TOTAL - clean - fees``; a missing field stays visible.  Set
``require_accrued=True`` to reject trades that omit it outright.

Example
-------
.. code-block:: python

    import datetime
    from decimal import Decimal
    from ofxtools.bonds import (
        BondBook, CouponTerms, CouponFrequency, CouponSchedule,
        DayCount, CostConvention,
    )

    terms = CouponTerms(
        coupon_rate=Decimal("5"),
        frequency=CouponFrequency.SEMIANNUAL,
        first_coupon=datetime.date(2024, 9, 1),
        issue_date=datetime.date(2024, 3, 1),
        maturity=datetime.date(2026, 3, 1),
        day_count=DayCount.ACT_ACT_ICMA,
    )
    book = BondBook.from_currency(
        "USD",
        schedule=CouponSchedule.from_terms(terms),
        coupon_rate=Decimal("5"),
    )

    result = book.post_buydebt(
        buydebt,                       # BUYDEBT aggregate
        par_value=Decimal("1000"),     # DEBTINFO.PARVALUE
        convention=CostConvention.ALL_FEES,
    )
    result.cost_entry.total_cost       # capitalized security cost
    result.interest_entry.amount       # accrued interest paid (negative)

Coupon calendar and accrual
---------------------------
``CouponSchedule`` generates periods from ``CouponTerms`` or accepts explicit
dates for irregular stubs.  Supported day counts are:

* ``ACT/ACT`` (ISDA)
* ``ACT/ACT ICMA`` (default; bond basis)
* ``ACT/365`` and ``ACT/360``
* ``30/360`` US, ``30E/360``, ``30E/360 ISDA``

Business-day adjustment supports unadjusted, following, modified following,
preceding, and modified preceding, using the NYSE calendar already shipped in
``ofxtools.utils``.  Coupons can optionally trade ex-dividend for a number of
days before the coupon date, in which case accrued interest is zero.

Each period carries a *notional* end date: the date a full regular period would
have ended.  ICMA accrual uses the notional period's actual days as its
denominator, and a short or long stub pays only its own accrued fraction rather
than a full coupon.  Regular periods are unaffected.

Accrual across a coupon date is split explicitly::

    schedule.accrual_period(start, end)   # [(period, seg_start, seg_end), ...]
    schedule.accrued_between(start, end, par_value=..., units=..., coupon_rate=...)

Partial transfers and quantity allocation
-----------------------------------------
A partial transfer, split, or partial sale allocates cost **by quantity**
(units), not by market value.  ``SecurityCostLedger.transfer`` moves
``cost * units / held`` and leaves the exact complement behind, so the two
lots always sum to the original cost.  ``split`` extends this to several
destination lots: the rounding residual is assigned deterministically, and the
allocated costs sum exactly to the amount being split.

``InterestLedger.transfer_accrued`` / ``release_accrued`` do the same for
accrued interest paid, so each lot's accrued balance stays proportional to its
remaining quantity.

Corrections and reversals
-------------------------
Entries are immutable and carry their own allocations.  ``reverse`` takes an
entry ID and posts the exact negation of that entry's allocations; it does not
recompute anything and cannot touch another trade's lots.  Reversal is
rejected if the entry was already reversed, if it is itself a reversal, or if
undoing it would drive a lot quantity or cost negative.

``BondBook.reverse_trade(original_fitid=..., correction_fitid=...)`` reverses
all not-yet-reversed entries from one trade, newest first.  Each entry is
unwound from its own recorded allocation, so a correction of trade A leaves
trade B untouched.

Example of a correction:

.. code-block:: python

    book.reverse_trade(
        original_fitid="TRADE-123",
        correction_fitid="CORRECTION-456",
        trade_date=datetime.date(2024, 7, 1),
    )

Working with OFX statements
---------------------------
Parse an ``INVSTMTRS`` as usual, then locate debt trades in
``INVTRANLIST`` (``buydebt`` / ``selldebt``) and the matching ``DEBTINFO`` in
``SECLIST`` for ``PARVALUE``.  Pass the ``BUYDEBT`` / ``SELLDEBT`` aggregate
directly to ``BondBook``; ``parse_debt_trade`` is available when only the
normalized fields are needed.

Limitations
-----------
* No FX translation: each book is single-currency.
* Coupon cash must be posted with ``post_coupon`` (or the ledger directly);
  the module does not scan ``INCOME`` transactions.
* ``INCOME`` / ``REINVEST`` are not auto-linked; post those to the interest
  ledger explicitly if they represent coupon cash.
