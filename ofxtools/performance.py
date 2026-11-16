"""
Portfolio performance attribution from OFX investment statements.

This module separates the two forces that change the value of a portfolio:

* market movement (investment performance), and
* external cash movements (contributions and withdrawals), which are not
  performance and must be excluded from return calculations.

It builds on the OFX investment statement model (``INVSTMTRS``), which carries
both the cash flow history (``INVTRANLIST``) and valuation observation points
(``INVPOSLIST`` plus the cash balance in ``INVBAL``).

Segmented return formula
------------------------
External cash flows split the reporting period into segments bounded by
valuation observation points.  For segment *i*, delimited by valuations
``V_{i-1}`` at ``t_{i-1}`` and ``V_i`` at ``t_i`` and containing external
flows ``C_{i,t}`` (positive = contribution, negative = withdrawal) each posted
at time *t*, the modified Dietz segment return is

    w_t = (t_i - t) / (t_i - t_{i-1})            # time weight
    R_i = (V_i - V_{i-1} - C_i) / (V_{i-1} + sum_t(w_t * C_{i,t}))

The numerator is the segment's investment gain/loss (the change in value net
of external money); the denominator is average capital exposed to the market.
The full-period return chains the segment returns geometrically:

    R_full = product_i(1 + R_i) - 1

Classification rules
--------------------
* ``INVBANKTRAN`` bank transactions are external cash movements.  The sign of
  ``STMTTRN.TRNAMT`` decides direction (positive = money in, negative = money
  out); ``TRNTYPE`` is retained on the event for reference.
* ``TRANSFER`` aggregates that do not name an ``INVACCTFROM`` reference an
  account outside the statement, so they cross the portfolio boundary and are
  external.  Transfers that name another tracked account are internal: the two
  legs of a position move stay inside the combined portfolio and must never be
  counted as contributions/withdrawals.
* ``JRNLSEC`` and ``JRNLFUND`` are always internal journal entries between
  subaccounts of the same account and never produce external cash flow.
* Security buys, sells, income, expenses and reinvestments exchange one asset
  for another inside the account, so they do not move external capital either.

When several flows share a timestamp, their ordering is determined by the
explicit :class:`CashFlowPriority` rule, never by the order the transactions
happen to appear in the OFX file.
"""

from __future__ import annotations

__all__ = [
    "CashFlowKind",
    "CashFlow",
    "CashFlowPriority",
    "ValuationPoint",
    "SegmentReturn",
    "PerformanceReport",
    "PerformanceError",
    "MissingValuationError",
    "extract_cash_flows",
    "extract_valuation_points",
    "build_valuation_point",
    "segment_returns",
    "performance_report",
]

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from ofxtools.models.base import Aggregate
from ofxtools.models.invest.positions import INVPOS
from ofxtools.models.invest.stmt import INVSTMTRS
from ofxtools.models.invest.transactions import (
    INVBANKTRAN,
    JRNLFUND,
    JRNLSEC,
    TRANSFER,
)

CashFlowKind = Literal[
    "EXTERNAL_IN",
    "EXTERNAL_OUT",
    "TRANSFER_IN",
    "TRANSFER_OUT",
]

class PerformanceError(ValueError):
    """Base class for portfolio performance calculation errors."""


class MissingValuationError(PerformanceError):
    """
    A return cannot be computed because a segment lacks a valuation endpoint.

    Cash flows alone cannot establish portfolio value; each segment boundary
    needs an observation point (``INVPOSLIST`` positions plus ``INVBAL`` cash).
    """


@dataclass(frozen=True)
class CashFlow:
    """
    A single cash movement affecting (or merely passing through) the portfolio.

    Attributes
    ----------
    dtposted
        Settlement/posting timestamp of the movement.
    amount
        Signed amount in the statement currency.  Positive values put money
        into the portfolio, negative values take money out.
    kind
        Direction/external nature of the movement.
    fitid
        OFX transaction identifier used for traceability.
    source
        OFX aggregate type that produced the event (e.g. ``INVBANKTRAN``).
    trntype
        Bank ``TRNTYPE`` when the event comes from an ``INVBANKTRAN``.
    external
        Whether the movement crosses the boundary of the combined portfolio.
        Internal transfers and journal entries are ``False`` and are excluded
        from performance math.
    """

    dtposted: datetime.datetime
    amount: Decimal
    kind: CashFlowKind
    fitid: str
    source: str
    trntype: str | None = None
    external: bool = True

    @property
    def is_inflow(self) -> bool:
        return self.amount > 0


@dataclass(frozen=True)
class CashFlowPriority:
    """
    Deterministic ordering rule for cash flows sharing one timestamp.

    Ties are never resolved by OFX file order.  At a given timestamp, all
    outflows (withdrawals/transfers out) are applied before all inflows
    (contributions/transfers in) when ``outflows_first`` is True (the
    default); otherwise inflows are applied first.  Within each side, events
    are ordered by ``fitid`` so the result is fully deterministic.
    """

    outflows_first: bool = True

    def sort_key(self, cash_flow: CashFlow) -> tuple[int, str]:
        side = 0 if self._comes_first(cash_flow) else 1
        return side, cash_flow.fitid

    def _comes_first(self, cash_flow: CashFlow) -> bool:
        is_outflow = cash_flow.amount < 0
        return is_outflow if self.outflows_first else not is_outflow

    def order(self, cash_flows: Sequence[CashFlow]) -> list[CashFlow]:
        return sorted(cash_flows, key=lambda cf: (cf.dtposted, self.sort_key(cf)))


@dataclass(frozen=True)
class ValuationPoint:
    """
    Total portfolio value observed at one timestamp.

    The value is the sum of every position's ``MKTVAL`` plus the available cash
    balance reported by ``INVBAL``; margin/short balances are already reflected
    in the signed position market values and cash figures.
    """

    dtasof: datetime.datetime
    market_value: Decimal
    cash_balance: Decimal
    positions: tuple[INVPOS, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SegmentReturn:
    """
    Attributed result for one segment between two valuation observation points.

    Attributes
    ----------
    dtstart / dtend
        Timestamps of the bounding valuation points.
    beginning_value / ending_value
        Portfolio market value at the segment boundaries.
    net_external_flow
        Sum of signed external flows within the segment (internal transfers and
        journal entries are excluded).
    contributions / withdrawals
        Gross external money in / out over the segment (positive magnitudes).
    cash_flows
        External flow events attributed to the segment, ordered per the
        configured :class:`CashFlowPriority`.
    gain
        Investment gain/loss: ``ending_value - beginning_value
        - net_external_flow``.
    return_rate
        Modified Dietz segment return, or ``None`` when the capital base
        (denominator) is zero and a rate is undefined.
    """

    dtstart: datetime.datetime
    dtend: datetime.datetime
    beginning_value: Decimal
    ending_value: Decimal
    net_external_flow: Decimal
    contributions: Decimal
    withdrawals: Decimal
    cash_flows: tuple[CashFlow, ...]
    gain: Decimal
    return_rate: Decimal | None

    @property
    def length(self) -> datetime.timedelta:
        return self.dtend - self.dtstart


@dataclass(frozen=True)
class PerformanceReport:
    """Full segmented performance attribution for one statement."""

    account_id: str
    account_ids: tuple[str, ...]
    currency: str
    valuation_points: tuple[ValuationPoint, ...]
    segments: tuple[SegmentReturn, ...]
    total_contributions: Decimal
    total_withdrawals: Decimal
    total_external_flow: Decimal
    total_gain: Decimal
    beginning_value: Decimal
    ending_value: Decimal
    total_return: Decimal | None
    internal_cash_flows: tuple[CashFlow, ...] = field(default_factory=tuple)

    @property
    def internal_flows_excluded(self) -> int:
        """Number of cash flow events ignored as internal transfers/journals."""
        return len(self.internal_cash_flows)


def _position_list(invstmtrs: INVSTMTRS) -> list[Aggregate]:
    invposlist = invstmtrs.invposlist
    if invposlist is None:
        return []
    return list(invposlist)


def _flatten_positions(invstmtrs: INVSTMTRS) -> list[INVPOS]:
    positions: list[INVPOS] = []
    for wrapper in _position_list(invstmtrs):
        invpos = getattr(wrapper, "invpos", None)
        if isinstance(invpos, INVPOS):
            positions.append(invpos)
    return positions


def build_valuation_point(invstmtrs: INVSTMTRS) -> ValuationPoint:
    """
    Construct the valuation observation point carried by a statement.

    Aggregates the ``INVPOS`` market values and the ``INVBAL.AVAILCASH`` cash
    balance as of the statement's ``DTASOF``.
    """
    positions = _flatten_positions(invstmtrs)
    market_value = sum((position.mktval for position in positions), Decimal())
    invbal = invstmtrs.invbal
    cash_balance = invbal.availcash if invbal is not None else Decimal()
    return ValuationPoint(
        dtasof=invstmtrs.dtasof,
        market_value=market_value + cash_balance,
        cash_balance=cash_balance,
        positions=tuple(positions),
    )


def extract_valuation_points(
    statements: Sequence[INVSTMTRS],
) -> list[ValuationPoint]:
    """
    Collect the valuation point of each statement, merging by timestamp.

    Several accounts of one combined portfolio typically report separate
    statements with the same ``DTASOF``; their values are summed into a single
    observation rather than de-duplicated.  Results are chronological.
    """
    merged: dict[datetime.datetime, ValuationPoint] = {}
    for statement in statements:
        point = build_valuation_point(statement)
        existing = merged.get(point.dtasof)
        if existing is None:
            merged[point.dtasof] = point
        else:
            merged[point.dtasof] = ValuationPoint(
                dtasof=point.dtasof,
                market_value=existing.market_value + point.market_value,
                cash_balance=existing.cash_balance + point.cash_balance,
                positions=existing.positions + point.positions,
            )
    return [merged[dtasof] for dtasof in sorted(merged)]


def _bank_cash_flow(invbanktran: INVBANKTRAN) -> CashFlow:
    stmttrn = invbanktran.stmttrn
    amount = stmttrn.trnamt
    kind: CashFlowKind = "EXTERNAL_IN" if amount >= 0 else "EXTERNAL_OUT"
    return CashFlow(
        dtposted=stmttrn.dtposted,
        amount=amount,
        kind=kind,
        fitid=stmttrn.fitid,
        source=type(invbanktran).__name__,
        trntype=stmttrn.trntype,
        external=True,
    )


def _transfer_cash_flow(transfer: TRANSFER) -> CashFlow:
    # A TRANSFER naming another account is an internal position move; a
    # TRANSFER without an account reference crosses the portfolio boundary.
    internal = transfer.invacctfrom is not None
    if transfer.tferaction == "IN":
        kind: CashFlowKind = "TRANSFER_IN" if internal else "EXTERNAL_IN"
    else:
        kind = "TRANSFER_OUT" if internal else "EXTERNAL_OUT"

    unitprice = transfer.unitprice
    if unitprice is None and not internal:
        raise PerformanceError(
            f"external TRANSFER {transfer.invtran.fitid} has no UNITPRICE; "
            "its cash value cannot be determined"
        )
    amount = transfer.units * (unitprice or Decimal())
    if transfer.tferaction == "OUT":
        amount = -amount
    return CashFlow(
        dtposted=transfer.invtran.dttrade,
        amount=amount,
        kind=kind,
        fitid=transfer.invtran.fitid,
        source=type(transfer).__name__,
        external=not internal,
    )


def extract_cash_flows(invstmtrs: INVSTMTRS) -> list[CashFlow]:
    """
    Extract every cash movement recorded in a statement's transaction list.

    External bank transactions and cross-account transfers are returned
    alongside internal transfers and journal entries, each flagged via
    :attr:`CashFlow.external`.  Security trades, income, expenses and
    reinvestments are internal conversions between cash and securities and do
    not appear here.  The result is ordered chronologically using
    :class:`CashFlowPriority` at shared timestamps, independent of file order.
    """
    invtranlist = invstmtrs.invtranlist
    cash_flows: list[CashFlow] = []
    if invtranlist is None:
        return cash_flows

    for transaction in invtranlist:
        if isinstance(transaction, INVBANKTRAN):
            cash_flows.append(_bank_cash_flow(transaction))
        elif isinstance(transaction, TRANSFER):
            cash_flows.append(_transfer_cash_flow(transaction))
        elif isinstance(transaction, (JRNLSEC, JRNLFUND)):
            total = getattr(transaction, "total", None)
            amount = Decimal() if total is None else total
            kind: CashFlowKind = (
                "TRANSFER_OUT" if amount < 0 else "TRANSFER_IN"
            )
            cash_flows.append(
                CashFlow(
                    dtposted=transaction.invtran.dttrade,
                    amount=amount,
                    kind=kind,
                    fitid=transaction.invtran.fitid,
                    source=type(transaction).__name__,
                    external=False,
                )
            )

    return CashFlowPriority().order(cash_flows)


def _require_endpoints(
    valuation_points: Sequence[ValuationPoint],
) -> None:
    if len(valuation_points) < 2:
        raise MissingValuationError(
            "segmented performance requires valuation points at both ends of "
            f"the reporting period; got {len(valuation_points)}"
        )
    timestamps = [point.dtasof for point in valuation_points]
    if timestamps != sorted(timestamps):
        raise MissingValuationError("valuation points must be chronological")


def _segment_cash_flows(
    cash_flows: Sequence[CashFlow],
    dtstart: datetime.datetime,
    dtend: datetime.datetime,
    priority: CashFlowPriority,
    include_end: bool = False,
) -> list[CashFlow]:
    # A flow landing exactly on a boundary belongs to the following segment,
    # where it earns a full-period time weight of 1.  The final segment also
    # absorbs flows stamped at the closing valuation (weight 0).
    in_segment = [
        cash_flow
        for cash_flow in cash_flows
        if cash_flow.external
        and (
            dtstart <= cash_flow.dtposted < dtend
            or (include_end and cash_flow.dtposted == dtend)
        )
    ]
    return priority.order(in_segment)


def _total_microseconds(delta: datetime.timedelta) -> int:
    return (
        delta.days * 24 * 60 * 60 + delta.seconds
    ) * 1_000_000 + delta.microseconds


def _time_weight(
    dtposted: datetime.datetime,
    dtstart: datetime.datetime,
    dtend: datetime.datetime,
) -> Decimal:
    span = _total_microseconds(dtend - dtstart)
    if span == 0:
        return Decimal()
    elapsed = _total_microseconds(dtend - dtposted)
    # Exact integer ratio keeps the calculation inside Decimal arithmetic,
    # avoiding float rounding in the time weight.
    return Decimal(elapsed) / Decimal(span)


def _compute_segment(
    beginning: ValuationPoint,
    ending: ValuationPoint,
    cash_flows: Sequence[CashFlow],
    priority: CashFlowPriority,
    include_end: bool = False,
) -> SegmentReturn:
    segment_flows = _segment_cash_flows(
        cash_flows,
        beginning.dtasof,
        ending.dtasof,
        priority,
        include_end=include_end,
    )
    net_flow = sum((cf.amount for cf in segment_flows), Decimal())
    contributions = sum(
        (cf.amount for cf in segment_flows if cf.amount > 0), Decimal()
    )
    withdrawals = sum(
        (-cf.amount for cf in segment_flows if cf.amount < 0), Decimal()
    )
    gain = ending.market_value - beginning.market_value - net_flow

    weighted_flow = sum(
        (
            _time_weight(cf.dtposted, beginning.dtasof, ending.dtasof) * cf.amount
            for cf in segment_flows
        ),
        Decimal(),
    )
    capital_base = beginning.market_value + weighted_flow
    if capital_base == 0:
        return_rate: Decimal | None = None
    else:
        return_rate = gain / capital_base

    return SegmentReturn(
        dtstart=beginning.dtasof,
        dtend=ending.dtasof,
        beginning_value=beginning.market_value,
        ending_value=ending.market_value,
        net_external_flow=net_flow,
        contributions=contributions,
        withdrawals=withdrawals,
        cash_flows=tuple(segment_flows),
        gain=gain,
        return_rate=return_rate,
    )


def segment_returns(
    valuation_points: Sequence[ValuationPoint],
    cash_flows: Sequence[CashFlow],
    priority: CashFlowPriority | None = None,
) -> list[SegmentReturn]:
    """
    Compute the modified Dietz return for every segment between valuations.

    See the module docstring for the segment formula.  A segment whose capital
    base is zero has an undefined rate; its :attr:`SegmentReturn.return_rate`
    is ``None`` rather than raising.
    """
    _require_endpoints(valuation_points)
    priority = priority or CashFlowPriority()
    segments: list[SegmentReturn] = []
    last_index = len(valuation_points) - 1
    for index, (beginning, ending) in enumerate(
        zip(valuation_points, valuation_points[1:])
    ):
        segments.append(
            _compute_segment(
                beginning,
                ending,
                cash_flows,
                priority,
                include_end=index == last_index - 1,
            )
        )
    return segments


def _chain_returns(segments: Sequence[SegmentReturn]) -> Decimal | None:
    chained = Decimal(1)
    saw_rate = False
    for segment in segments:
        if segment.return_rate is None:
            continue
        saw_rate = True
        chained *= Decimal(1) + segment.return_rate
    if not saw_rate:
        return None
    return chained - Decimal(1)


def performance_report(
    statements: Sequence[INVSTMTRS],
    priority: CashFlowPriority | None = None,
) -> PerformanceReport:
    """
    Generate the full combined performance report for a set of statements.

    Statements may cover multiple accounts of one combined portfolio.  All
    external flows are pooled; internal transfers between tracked accounts and
    subaccount journal entries are excluded from performance math.  Raises
    :class:`MissingValuationError` when the statements do not provide valuation
    observation points bracketing the period.
    """
    if not statements:
        raise MissingValuationError(
            "cannot build a performance report without statements"
        )

    currencies = {statement.curdef for statement in statements}
    if len(currencies) > 1:
        raise PerformanceError(
            "cannot combine statements denominated in multiple currencies: "
            f"{sorted(currencies)}"
        )

    valuation_points = extract_valuation_points(statements)
    _require_endpoints(valuation_points)

    all_flows: list[CashFlow] = []
    for statement in statements:
        all_flows.extend(extract_cash_flows(statement))
    priority = priority or CashFlowPriority()
    all_flows = priority.order(all_flows)

    window_start = valuation_points[0].dtasof
    window_end = valuation_points[-1].dtasof
    out_of_window = [
        cash_flow
        for cash_flow in all_flows
        if cash_flow.external
        and not (window_start <= cash_flow.dtposted <= window_end)
    ]
    if out_of_window:
        offending = ", ".join(
            f"{cf.fitid}@{cf.dtposted.isoformat()}" for cf in out_of_window
        )
        raise MissingValuationError(
            "external cash flows fall outside the valuation window "
            f"[{window_start.isoformat()}, {window_end.isoformat()}]: {offending}"
        )

    external_flows = [cf for cf in all_flows if cf.external]
    internal_flows = [cf for cf in all_flows if not cf.external]

    segments = segment_returns(valuation_points, external_flows, priority)

    total_contributions = sum(
        (segment.contributions for segment in segments), Decimal()
    )
    total_withdrawals = sum(
        (segment.withdrawals for segment in segments), Decimal()
    )
    total_external_flow = sum(
        (segment.net_external_flow for segment in segments), Decimal()
    )
    total_gain = sum((segment.gain for segment in segments), Decimal())
    total_return = _chain_returns(segments)

    reference = statements[0]
    account_ids = tuple(
        dict.fromkeys(statement.invacctfrom.acctid for statement in statements)
    )
    return PerformanceReport(
        account_id=reference.invacctfrom.acctid,
        account_ids=account_ids,
        currency=reference.curdef,
        valuation_points=tuple(valuation_points),
        segments=tuple(segments),
        total_contributions=total_contributions,
        total_withdrawals=total_withdrawals,
        total_external_flow=total_external_flow,
        total_gain=total_gain,
        beginning_value=valuation_points[0].market_value,
        ending_value=valuation_points[-1].market_value,
        total_return=total_return,
        internal_cash_flows=tuple(internal_flows),
    )
