"""
Settlement ledger for OFX investment transactions.

OFX investment transactions carry both a trade date (``DTTRADE``) and,
optionally, a settlement date (``DTSETTLE``).  A useful bookkeeping view needs
more than one running position: securities bought yesterday are owned but not
yet deliverable, and the cash to pay for them is committed but not yet spent.
:class:`SettlementLedger` therefore keeps three linked books per account:

* **equity holdings** (``held``) -- units owned as of the trade date;
* **available securities** (``available``) -- units deliverable as of the
  settlement date;
* **cash** -- ``pending`` cash committed by open settlements, plus ``settled``
  cash that has already changed hands.

The books are derived from an append-only stream of :class:`SettlementEvent`
objects.  Each event carries an *effective* date (when it changes the books)
and a *knowledge* date (when the ledger first learned of it).  Queries take
both an as-of date and a knowledge cutoff, so a back-dated confirmation can be
reproduced as it looked before it arrived.

Settlement rules
----------------
* **Trade date vs settlement date.**  A trade moves equity holdings and
  commits cash on the trade date.  Available securities and settled cash move
  on the settlement date.  ``DTSETTLE`` is authoritative when present;
  otherwise the settlement date is derived with
  :func:`ofxtools.utils.settleDate` using the instrument's cycle (T+1 by
  default, T+2 for debt).
* **Automatic settlement.**  A trade for which no settlement confirmation was
  ever received is deemed to settle in full on its contractual settlement
  date.  This is the normal case for plain OFX statements.
* **Partial settlement.**  A trade may settle in several pieces.  Every piece
  references the original transaction by ``FITID``; the ledger enforces that
  pieces never exceed the original quantity and reports each piece separately,
  so the link back to the original trade stays explicit.  Once any settlement
  confirmation exists, settlement becomes confirmation-driven: the ledger no
  longer assumes the residual settled on the contractual date.
* **Failed settlement.**  A failure is an attempt, not a disposition.  It
  leaves the trade standing: equity holdings are unchanged, no units become
  available, and the cash obligation stays pending.  The failed attempt is
  recorded against the trade, which remains open for a later successful
  piece.  A failure therefore never looks like a cancellation.
* **Cancellation.**  ``CANCEL`` reverses a trade on both the trade-date and
  settlement-date books: holdings, available units, settled cash and pending
  cash are rolled back as of the cancellation date.  A cancelled trade is
  terminal and may not receive further settlement pieces.
* **Late confirmations.**  A confirmation may arrive after its effective
  date.  Both dates are retained, and the earliest knowledge date wins if the
  same fact is reported again.  A query whose knowledge cutoff precedes the
  receipt date excludes the event even when its effective date is older.
* **Cross-day queries.**  Equity holdings are evaluated against the trade
  date, available securities and cash against the settlement date, and
  visibility against the knowledge date.  :meth:`SettlementLedger.as_of`
  applies each date to the book it governs.

Cash posted directly to the account (``INVBANKTRAN`` deposits and
withdrawals) is settled on its posting date and never passes through the
pending book.  Internal journal entries between subaccounts are ignored,
because the books are not partitioned by subaccount.

The module is independent of the OFX models; use
:func:`transactions_from_statement` or :meth:`SettlementLedger.from_ofx` to
feed it from parsed OFX data.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import Enum
from typing import Any

from ofxtools.utils import settleDate

__all__ = [
    "EventType",
    "SettlementStatus",
    "SettlementEvent",
    "SettlementError",
    "DuplicateEvent",
    "UnknownReference",
    "OverSettlement",
    "IncompleteSettlement",
    "TerminalSettlement",
    "SettlementRule",
    "SettlementRules",
    "DEFAULT_SETTLEMENT_RULES",
    "SettlementLedger",
    "SecurityPosition",
    "CashPosition",
    "TradeRecord",
    "Snapshot",
    "transactions_from_statement",
    "transactions_from_statements",
    "event_from_dict",
]

ZERO = Decimal("0")


class SettlementError(ValueError):
    """Base class for settlement ledger errors."""


class DuplicateEvent(SettlementError):
    """An event conflicts with one already recorded."""


class UnknownReference(SettlementError):
    """An event references a transaction the ledger does not know."""


class OverSettlement(SettlementError):
    """Settlement or failure pieces exceed the original transaction."""


class IncompleteSettlement(SettlementError):
    """A ``SETTLE`` event did not cover the remaining obligation."""


class TerminalSettlement(SettlementError):
    """An event targets a trade that has been cancelled."""


@dataclass(frozen=True)
class SettlementRule:
    """
    One settlement rule.

    ``business_days`` is the number of NYSE business days between trade and
    settlement.  ``applies_to`` lists the security types (``UNIQUEIDTYPE``
    values) and/or instrument names the rule governs; an empty tuple matches
    anything and acts as the fallback.
    """

    business_days: int
    applies_to: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.business_days < 0:
            raise SettlementError("business_days must be >= 0")
        object.__setattr__(
            self, "applies_to", tuple(item.upper() for item in self.applies_to)
        )

    def matches(self, *keys: str | None) -> bool:
        if not self.applies_to:
            return True
        wanted = {key.upper() for key in keys if key}
        for token in self.applies_to:
            for key in wanted:
                # Tokens match a security type exactly or appear within the
                # instrument name, so "DEBT" covers BUYDEBT/SELLDEBT.
                if key == token or token in key:
                    return True
        return False


DEFAULT_SETTLEMENT_RULES: tuple[SettlementRule, ...] = (
    SettlementRule(2, ("DEBT", "BOND", "MUNICIPAL", "TREASURY")),
    SettlementRule(1, ()),
)


class SettlementRules:
    """
    Ordered settlement-rule table.

    The first matching rule wins; ``DEFAULT_SETTLEMENT_RULES`` settles
    equities and funds at T+1 and debt at T+2.  Rules can be replaced per
    deployment without touching the ledger itself.
    """

    def __init__(self, rules: Iterable[SettlementRule] | None = None) -> None:
        self._rules = tuple(rules) if rules is not None else DEFAULT_SETTLEMENT_RULES
        if not self._rules:
            raise SettlementError("at least one settlement rule is required")

    def __iter__(self) -> Iterator[SettlementRule]:
        return iter(self._rules)

    def cycle(self, *keys: str | None) -> int:
        for rule in self._rules:
            if rule.matches(*keys):
                return rule.business_days
        raise SettlementError(f"no settlement rule matches {keys!r}")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, int], default: int = 1) -> SettlementRules:
        """
        Build rules from ``{"security type or instrument": business_days}``.

        Insertion order decides precedence; ``default`` is the fallback.
        """
        rules = [
            SettlementRule(int(days), (str(key).upper(),))
            for key, days in mapping.items()
        ]
        rules.append(SettlementRule(int(default)))
        return cls(rules)


class EventType(str, Enum):
    """What happened to a settlement obligation."""

    TRADE = "TRADE"
    SETTLE = "SETTLE"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"
    CANCEL = "CANCEL"
    CASH = "CASH"


class SettlementStatus(str, Enum):
    """Lifecycle status of a settlement obligation."""

    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"


# Replay ordering within one effective date: book the trade first, then its
# settlement or cash movement, and finally any cancellation of that trade.
_REPLAY_ORDER = {
    EventType.TRADE: 0,
    EventType.SETTLE: 1,
    EventType.PARTIAL: 1,
    EventType.FAIL: 2,
    EventType.CASH: 2,
    EventType.CANCEL: 3,
}


@dataclass(frozen=True)
class SettlementEvent:
    """
    One settlement fact.

    Parameters
    ----------
    fitid:
        Identifier of the event itself.  For an original trade this is the
        OFX ``FITID``; settlement pieces and cancellations need identifiers
        of their own.
    effective_date:
        Date on which the event takes effect: the trade date for ``TRADE``,
        the settlement date for ``SETTLE``/``PARTIAL``/``FAIL``, the
        cancellation date for ``CANCEL`` and the posting date for ``CASH``.
    event_type:
        See :class:`EventType`.
    known_date:
        Date on which the ledger first learned of the event.  Defaults to
        ``effective_date`` (same-day confirmation).
    trade_date:
        Original trade date; defaults to ``effective_date`` for trades.
    settlement_date:
        Contractual settlement date.  For trades this defaults to
        ``settleDate(trade_date, settlement_cycle)``.
    reference:
        ``FITID`` of the original trade, required for ``SETTLE``,
        ``PARTIAL``, ``FAIL`` and ``CANCEL``.
    security_id / security_type:
        OFX ``UNIQUEID`` / ``UNIQUEIDTYPE``; ``None`` for pure cash events.
    account / currency:
        Partition keys for the books.
    units:
        Signed security quantity: positive for a buy or transfer in,
        negative for a sell or transfer out.
    amount:
        Signed cash amount: negative for a buy (cash paid), positive for a
        sell.
    settlement_cycle:
        Business days used when ``settlement_date`` must be derived.
    """

    fitid: str
    effective_date: datetime.date
    event_type: EventType
    known_date: datetime.date | None = None
    trade_date: datetime.date | None = None
    settlement_date: datetime.date | None = None
    reference: str | None = None
    security_id: str | None = None
    security_type: str | None = None
    account: str = ""
    currency: str = ""
    units: Decimal = ZERO
    amount: Decimal = ZERO
    settlement_cycle: int = 1
    memo: str | None = None

    def __post_init__(self) -> None:
        if not self.fitid:
            raise SettlementError("fitid is required")
        if isinstance(self.event_type, str):
            object.__setattr__(self, "event_type", EventType(self.event_type))
        object.__setattr__(
            self, "effective_date", _check_date(self.effective_date, "effective_date")
        )
        if self.known_date is None:
            object.__setattr__(self, "known_date", self.effective_date)
        else:
            object.__setattr__(
                self, "known_date", _check_date(self.known_date, "known_date")
            )
        if self.trade_date is not None:
            object.__setattr__(
                self, "trade_date", _check_date(self.trade_date, "trade_date")
            )
        if self.settlement_date is not None:
            object.__setattr__(
                self,
                "settlement_date",
                _check_date(self.settlement_date, "settlement_date"),
            )
        object.__setattr__(self, "units", Decimal(self.units))
        object.__setattr__(self, "amount", Decimal(self.amount))
        if self.event_type is EventType.TRADE:
            trade_date = self.trade_date or self.effective_date
            object.__setattr__(self, "trade_date", trade_date)
            if self.settlement_date is None:
                object.__setattr__(
                    self,
                    "settlement_date",
                    settleDate(trade_date, n=self.settlement_cycle),
                )
            if self.units == ZERO and self.amount == ZERO:
                raise SettlementError(f"TRADE event {self.fitid!r} moves nothing")
        elif self.event_type is EventType.CASH:
            if self.units != ZERO:
                raise SettlementError(
                    f"CASH event {self.fitid!r} must not move securities"
                )
            if self.amount == ZERO:
                raise SettlementError(f"CASH event {self.fitid!r} moves nothing")
        elif self.event_type is EventType.CANCEL:
            if self.reference is None:
                raise SettlementError(
                    f"CANCEL event {self.fitid!r} "
                    "requires reference=<original FITID>"
                )
        else:
            if self.reference is None:
                raise SettlementError(
                    f"{self.event_type.value} event {self.fitid!r} "
                    "requires reference=<original FITID>"
                )
            if self.units == ZERO and self.amount == ZERO:
                raise SettlementError(
                    f"{self.event_type.value} event {self.fitid!r} moves nothing"
                )

    @property
    def is_piece(self) -> bool:
        return self.event_type in (
            EventType.SETTLE,
            EventType.PARTIAL,
            EventType.FAIL,
        )

    @property
    def is_successful_piece(self) -> bool:
        return self.event_type in (EventType.SETTLE, EventType.PARTIAL)

    @property
    def is_debit(self) -> bool:
        return self.amount < ZERO

    def content(self) -> tuple[Any, ...]:
        """Fields that determine the effect of an event."""
        return (
            self.event_type,
            self.reference,
            self.account,
            self.security_id,
            self.effective_date,
            self.settlement_date,
            self.units,
            self.amount,
        )

    def with_known_date(self, known_date: datetime.date) -> SettlementEvent:
        """Return a copy first known on ``known_date``."""
        return replace(self, known_date=known_date)


@dataclass(frozen=True)
class SecurityPosition:
    """
    Quantity of one security in one account across the three books.

    ``receivable`` is the settled-buy quantity still to be delivered in;
    ``deliverable_pending`` is the settled-sell quantity still to be
    delivered out.  ``unsettled`` nets them, and ``available`` is the
    deliverable quantity after reserving open sells.
    """

    account: str
    security_id: str
    security_type: str | None
    held: Decimal = ZERO
    available: Decimal = ZERO
    unsettled: Decimal = ZERO
    receivable: Decimal = ZERO
    deliverable_pending: Decimal = ZERO

    @property
    def pending(self) -> Decimal:
        """Signed units still to be received (positive) or delivered."""
        return self.receivable - self.deliverable_pending


@dataclass(frozen=True)
class CashPosition:
    """Cash in one account/currency."""

    account: str
    currency: str
    settled: Decimal = ZERO
    pending: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        """Settled cash plus outstanding settlement cash."""
        return self.settled + self.pending


@dataclass(frozen=True)
class TradeRecord:
    """Settlement progress of one original trade."""

    fitid: str
    account: str
    security_id: str | None
    units: Decimal
    amount: Decimal
    trade_date: datetime.date
    settlement_date: datetime.date
    status: SettlementStatus
    settled_units: Decimal
    failed_units: Decimal
    settled_amount: Decimal
    failed_amount: Decimal
    pieces: tuple[str, ...]
    cancelled: bool = False

    @property
    def remaining_units(self) -> Decimal:
        if self.cancelled:
            return ZERO
        return abs(self.units) - self.settled_units

    @property
    def remaining_amount(self) -> Decimal:
        if self.cancelled:
            return ZERO
        return abs(self.amount) - self.settled_amount


@dataclass(frozen=True)
class Snapshot:
    """
    The three books for one point in time.

    ``as_of`` is the effective-date cutoff and ``known_as_of`` the knowledge
    cutoff.  ``equity`` is the trade-date quantity; ``securities[*].available``
    is the settlement-date quantity; ``cash[*].pending`` is the cash committed
    to open settlements.
    """

    as_of: datetime.date
    known_as_of: datetime.date
    securities: Mapping[tuple[str, str], SecurityPosition]
    cash: Mapping[tuple[str, str], CashPosition]
    equity: Mapping[tuple[str, str], Decimal]

    def held(self, account: str, security_id: str) -> Decimal:
        position = self.securities.get((account, security_id))
        return ZERO if position is None else position.held

    def available(self, account: str, security_id: str) -> Decimal:
        position = self.securities.get((account, security_id))
        return ZERO if position is None else position.available

    def pending_units(self, account: str, security_id: str) -> Decimal:
        position = self.securities.get((account, security_id))
        return ZERO if position is None else position.unsettled

    def settled_cash(self, account: str, currency: str) -> Decimal:
        position = self.cash.get((account, currency))
        return ZERO if position is None else position.settled

    def pending_cash(self, account: str, currency: str) -> Decimal:
        position = self.cash.get((account, currency))
        return ZERO if position is None else position.pending


def _check_date(value: Any, label: str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    raise SettlementError(f"{label} must be a date, got {type(value).__name__}")


def _abs(value: Decimal) -> Decimal:
    return -value if value < ZERO else value


@dataclass
class _TradeState:
    event: SettlementEvent
    settled_units: Decimal = ZERO
    settled_amount: Decimal = ZERO
    failed_units: Decimal = ZERO
    failed_amount: Decimal = ZERO
    pieces: list[SettlementEvent] = field(default_factory=list)
    cancelled_on: datetime.date | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancelled_on is not None

    @property
    def remaining_units(self) -> Decimal:
        if self.cancelled:
            return ZERO
        return abs(self.event.units) - self.settled_units

    @property
    def remaining_amount(self) -> Decimal:
        if self.cancelled:
            return ZERO
        return abs(self.event.amount) - self.settled_amount

    @property
    def status(self) -> SettlementStatus:
        if self.cancelled:
            return SettlementStatus.CANCELLED
        if self.remaining_units <= ZERO and self.remaining_amount <= ZERO:
            return SettlementStatus.SETTLED
        if self.failed_units or self.failed_amount:
            return SettlementStatus.FAILED
        if self.settled_units or self.settled_amount:
            return SettlementStatus.PARTIAL
        return SettlementStatus.PENDING


@dataclass
class _RunState:
    held: Decimal = ZERO
    receivable: Decimal = ZERO
    deliverable_pending: Decimal = ZERO
    settled_cash: Decimal = ZERO
    pending_cash: Decimal = ZERO

    @property
    def available(self) -> Decimal:
        # Sold units leave ``held`` on the trade date but stay deliverable
        # until they are handed over; only unsettled buys are unavailable.
        return self.held - self.receivable


class SettlementLedger:
    """
    Append-only settlement ledger with equity, available and cash books.

    Events are stored in arrival order.  :meth:`as_of` replays only events
    known by the requested knowledge cutoff, which makes late confirmations
    reproducible without rewriting history.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self._events: list[SettlementEvent] = []
        self._by_key: dict[tuple[str, str], SettlementEvent] = {}
        self._trades: dict[tuple[str, str], _TradeState] = {}
        self._strict = strict

    # ------------------------------------------------------------------
    #  Ingest
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[SettlementEvent]:
        return iter(self._events)

    @property
    def events(self) -> tuple[SettlementEvent, ...]:
        return tuple(self._events)

    def record(self, event: SettlementEvent) -> SettlementEvent:
        """
        Record one event.

        Re-recording identical content (as happens when overlapping
        statements repeat a confirmation) is a no-op, and the earlier
        knowledge date is kept.  A different event with the same account and
        ``FITID`` raises :class:`DuplicateEvent` when ``strict`` is set and
        replaces the old event otherwise.
        """
        key = (event.account, event.fitid)
        existing = self._by_key.get(key)
        if existing is not None:
            if existing.content() == event.content():
                if (
                    existing.known_date
                    and event.known_date
                    and event.known_date < existing.known_date
                ):
                    self._events[self._events.index(existing)] = replace(
                        existing, known_date=event.known_date
                    )
                    self._reindex()
                return existing
            if self._strict:
                raise DuplicateEvent(
                    f"FITID {event.fitid!r} already recorded with different content"
                )
            if existing.known_date and event.known_date:
                event = event.with_known_date(
                    min(existing.known_date, event.known_date)
                )
            self._events.remove(existing)

        self._events.append(event)
        try:
            self._reindex()
        except Exception:
            self._events.remove(event)
            if existing is not None:
                self._events.append(existing)
            self._reindex()
            raise
        return event

    def extend(self, events: Iterable[SettlementEvent]) -> None:
        for event in events:
            self.record(event)

    def _reindex(self) -> None:
        """Rebuild trade states and validate the whole event stream."""
        self._by_key = {}
        self._trades = {}
        for event in self._events:
            key = (event.account, event.fitid)
            if key in self._by_key:
                raise DuplicateEvent(
                    f"duplicate FITID {event.fitid!r} in account "
                    f"{event.account!r}"
                )
            self._by_key[key] = event
            if event.event_type is EventType.TRADE:
                self._trades[key] = _TradeState(event=event)

        pieces: dict[tuple[str, str], list[SettlementEvent]] = {}
        cancels: dict[tuple[str, str], list[SettlementEvent]] = {}
        for event in self._events:
            if event.event_type in (EventType.TRADE, EventType.CASH):
                continue
            key = (event.account, event.reference or "")
            if event.event_type is EventType.CANCEL:
                cancels.setdefault(key, []).append(event)
            else:
                pieces.setdefault(key, []).append(event)

        for key in list(pieces) + list(cancels):
            if key not in self._trades:
                group = pieces.get(key, []) + cancels.get(key, [])
                event = group[0]
                raise UnknownReference(
                    f"event {event.fitid!r} references unknown trade "
                    f"{event.reference!r} in account {event.account!r}"
                )

        for key, trade in self._trades.items():
            cancel_dates = sorted(
                event.effective_date for event in cancels.get(key, [])
            )
            if len(cancel_dates) > 1:
                raise TerminalSettlement(
                    f"trade {trade.event.fitid!r} is cancelled more than once"
                )
            cancelled_on = cancel_dates[0] if cancel_dates else None
            trade_pieces = sorted(
                pieces.get(key, []),
                key=lambda e: (e.effective_date, e.known_date or e.effective_date),
            )
            for event in trade_pieces:
                if cancelled_on is not None and event.effective_date >= cancelled_on:
                    raise TerminalSettlement(
                        f"event {event.fitid!r} targets trade "
                        f"{event.reference!r} cancelled on {cancelled_on}"
                    )
                self._apply_piece_to_state(event, trade)
            if cancelled_on is not None:
                if cancelled_on < (
                    trade.event.trade_date or trade.event.effective_date
                ):
                    event = cancels[key][0]
                    raise SettlementError(
                        f"cancellation {event.fitid!r} predates its trade"
                    )
                trade.cancelled_on = cancelled_on
                trade.pieces.extend(cancels[key])

    def _apply_piece_to_state(
        self, event: SettlementEvent, trade: _TradeState
    ) -> None:
        units = _abs(event.units)
        amount = _abs(event.amount)
        trade.pieces.append(event)
        if event.event_type is EventType.FAIL:
            # A failed attempt is not a disposition: it consumes no quantity,
            # so the obligation stays open for retry.
            if units > trade.remaining_units:
                raise OverSettlement(
                    f"failed units {units} exceed open obligation "
                    f"{trade.remaining_units} for {trade.event.fitid!r}"
                )
            if amount > trade.remaining_amount:
                raise OverSettlement(
                    f"failed amount {amount} exceeds open obligation "
                    f"{trade.remaining_amount} for {trade.event.fitid!r}"
                )
            trade.failed_units += units
            trade.failed_amount += amount
            return

        if event.event_type is EventType.SETTLE:
            if units != trade.remaining_units or amount != trade.remaining_amount:
                raise IncompleteSettlement(
                    f"SETTLE {event.fitid!r} covers {units} units / {amount} cash "
                    f"but {trade.remaining_units} / {trade.remaining_amount} "
                    f"remain for {trade.event.fitid!r}; use PARTIAL for pieces"
                )
        elif units > trade.remaining_units:
            raise OverSettlement(
                f"settled units {units} exceed remaining "
                f"{trade.remaining_units} for {trade.event.fitid!r}"
            )
        elif amount > trade.remaining_amount:
            raise OverSettlement(
                f"settled amount {amount} exceeds remaining "
                f"{trade.remaining_amount} for {trade.event.fitid!r}"
            )
        trade.settled_units += units
        trade.settled_amount += amount

    # ------------------------------------------------------------------
    #  Query
    # ------------------------------------------------------------------
    def as_of(
        self,
        date: datetime.date,
        known_as_of: datetime.date | None = None,
    ) -> Snapshot:
        """
        Return the three books as of ``date``.

        ``known_as_of`` limits events to those first known on or before that
        date.  It defaults to ``date``; pass a later date to reproduce the
        books after a late confirmation arrived.  The effective date is
        applied per book: equity against the trade date, available securities
        and cash against the settlement date.
        """
        as_of = _check_date(date, "date")
        known = (
            as_of
            if known_as_of is None
            else _check_date(known_as_of, "known_as_of")
        )

        visible = [
            event
            for event in self._events
            if event.known_date is not None and event.known_date <= known
        ]
        visible.sort(
            key=lambda event: (
                event.effective_date,
                event.known_date or event.effective_date,
                _REPLAY_ORDER[event.event_type],
            )
        )
        pieces_by_trade = self._pieces_by_trade(visible)

        states: dict[tuple[str, str], _RunState] = {}

        def state(account: str, key: str) -> _RunState:
            return states.setdefault((account, key), _RunState())

        for event in visible:
            if event.effective_date > as_of:
                continue
            if event.event_type is EventType.TRADE:
                trade_pieces = pieces_by_trade.get(
                    (event.account, event.fitid), []
                )
                self._apply_trade(event, as_of, trade_pieces, state)
            elif event.event_type is EventType.CASH:
                if event.currency:
                    state(
                        event.account, self._cash_key(event.currency)
                    ).settled_cash += event.amount
            elif event.is_successful_piece:
                self._apply_settlement(event, state)
            elif event.event_type is EventType.FAIL:
                continue
            elif event.event_type is EventType.CANCEL:
                trade = self._trades.get((event.account, event.reference or ""))
                if trade is not None:
                    trade_pieces = pieces_by_trade.get(
                        (event.account, event.reference or ""), []
                    )
                    self._apply_cancel(
                        trade.event, as_of, trade_pieces, state
                    )

        securities: dict[tuple[str, str], SecurityPosition] = {}
        equity: dict[tuple[str, str], Decimal] = {}
        for (account, key), run in states.items():
            if key.startswith("CASH:"):
                continue
            securities[(account, key)] = SecurityPosition(
                account=account,
                security_id=key,
                security_type=self._security_type(key, visible),
                held=run.held,
                available=run.available,
                unsettled=run.receivable - run.deliverable_pending,
                receivable=run.receivable,
                deliverable_pending=run.deliverable_pending,
            )
            equity[(account, key)] = run.held

        cash: dict[tuple[str, str], CashPosition] = {}
        for (account, key), run in states.items():
            if not key.startswith("CASH:"):
                continue
            currency = key[len("CASH:") :]
            cash[(account, currency)] = CashPosition(
                account=account,
                currency=currency,
                settled=run.settled_cash,
                pending=run.pending_cash,
            )

        return Snapshot(
            as_of=as_of,
            known_as_of=known,
            securities=securities,
            cash=cash,
            equity=equity,
        )

    def holdings(
        self, date: datetime.date, known_as_of: datetime.date | None = None
    ) -> dict[tuple[str, str], Decimal]:
        """Trade-date equity holdings only."""
        return dict(self.as_of(date, known_as_of).equity)

    def available(
        self, date: datetime.date, known_as_of: datetime.date | None = None
    ) -> dict[tuple[str, str], Decimal]:
        """Settlement-date deliverable securities only."""
        return {
            key: position.available
            for key, position in self.as_of(date, known_as_of).securities.items()
        }

    def pending_cash(
        self, date: datetime.date, known_as_of: datetime.date | None = None
    ) -> dict[tuple[str, str], Decimal]:
        """Cash still committed to open settlements only."""
        return {
            key: position.pending
            for key, position in self.as_of(date, known_as_of).cash.items()
        }

    def cash(
        self, date: datetime.date, known_as_of: datetime.date | None = None
    ) -> dict[tuple[str, str], CashPosition]:
        """Settled and pending cash."""
        return dict(self.as_of(date, known_as_of).cash)

    # ------------------------------------------------------------------
    #  Replay helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _cash_key(currency: str) -> str:
        return f"CASH:{currency}"

    def _security_type(
        self, security_id: str, events: Sequence[SettlementEvent]
    ) -> str | None:
        for event in events:
            if event.security_id == security_id and event.security_type:
                return event.security_type
        return None

    def _pieces_by_trade(
        self, events: Sequence[SettlementEvent]
    ) -> dict[tuple[str, str], list[SettlementEvent]]:
        pieces: dict[tuple[str, str], list[SettlementEvent]] = {}
        for event in events:
            if event.is_piece:
                pieces.setdefault(
                    (event.account, event.reference or ""), []
                ).append(event)
        return pieces

    def _cancels_by_trade(
        self, events: Sequence[SettlementEvent]
    ) -> dict[tuple[str, str], list[SettlementEvent]]:
        cancels: dict[tuple[str, str], list[SettlementEvent]] = {}
        for event in events:
            if event.event_type is EventType.CANCEL:
                cancels.setdefault(
                    (event.account, event.reference or ""), []
                ).append(event)
        return cancels

    @staticmethod
    def _settled_by(
        trade: SettlementEvent,
        pieces: Sequence[SettlementEvent],
        as_of: datetime.date,
    ) -> tuple[Decimal, Decimal]:
        """
        Units and cash settled for a trade by ``as_of``.

        With no settlement confirmation visible at all, the trade
        auto-settles in full on its contractual settlement date.  Once any
        confirmation exists, settlement is confirmation-driven: only
        successful pieces effective by ``as_of`` count, and a failure
        suppresses the automatic assumption entirely.
        """
        if pieces:
            units = sum(
                (
                    piece.units
                    for piece in pieces
                    if piece.is_successful_piece
                    and piece.effective_date <= as_of
                ),
                ZERO,
            )
            amount = sum(
                (
                    piece.amount
                    for piece in pieces
                    if piece.is_successful_piece
                    and piece.effective_date <= as_of
                ),
                ZERO,
            )
            return units, amount
        settlement_date = trade.settlement_date or trade.effective_date
        if settlement_date <= as_of:
            return trade.units, trade.amount
        return ZERO, ZERO

    def _apply_trade(
        self,
        event: SettlementEvent,
        as_of: datetime.date,
        pieces: Sequence[SettlementEvent],
        state: Any,
    ) -> None:
        if event.security_id is not None:
            run = state(event.account, event.security_id)
            run.held += event.units
            if event.units > ZERO:
                run.receivable += event.units
            elif event.units < ZERO:
                run.deliverable_pending += -event.units
        if event.currency:
            state(event.account, self._cash_key(event.currency)).pending_cash += (
                event.amount
            )

        # Explicit settlement pieces are applied when their own event is
        # replayed.  Only the automatic (no-confirmation) case is booked here.
        if pieces:
            return
        settled_units, settled_amount = self._settled_by(event, pieces, as_of)
        if settled_units == ZERO and settled_amount == ZERO:
            return
        if event.security_id is not None:
            self._settle_units(state, event, settled_units)
        if event.currency:
            run = state(event.account, self._cash_key(event.currency))
            run.pending_cash -= settled_amount
            run.settled_cash += settled_amount

    def _settle_units(
        self, state: Any, event: SettlementEvent, units: Decimal
    ) -> None:
        """Move ``units`` from the pending side of a trade into availability."""
        run = state(event.account, event.security_id)
        if units > ZERO:
            run.receivable -= units
        elif units < ZERO:
            run.deliverable_pending -= -units

    def _apply_settlement(self, event: SettlementEvent, state: Any) -> None:
        if event.security_id is not None:
            self._settle_units(state, event, event.units)
        if event.currency:
            run = state(event.account, self._cash_key(event.currency))
            run.pending_cash -= event.amount
            run.settled_cash += event.amount

    def _apply_cancel(
        self,
        trade: SettlementEvent,
        as_of: datetime.date,
        pieces: Sequence[SettlementEvent],
        state: Any,
    ) -> None:
        settled_units, settled_amount = self._settled_by(trade, pieces, as_of)
        if trade.security_id is not None:
            run = state(trade.account, trade.security_id)
            run.held -= trade.units
            unsettled = trade.units - settled_units
            if unsettled > ZERO:
                run.receivable -= unsettled
            elif unsettled < ZERO:
                run.deliverable_pending -= -unsettled
        if trade.currency:
            run = state(trade.account, self._cash_key(trade.currency))
            run.pending_cash -= trade.amount - settled_amount
            run.settled_cash -= settled_amount

    # ------------------------------------------------------------------
    #  Reporting
    # ------------------------------------------------------------------
    def _trade_record_at(
        self,
        trade: _TradeState,
        pieces: Sequence[SettlementEvent],
        as_of: datetime.date,
        cancels: Sequence[SettlementEvent] = (),
    ) -> TradeRecord:
        """Build the record for one trade as of ``as_of``."""
        event = trade.event
        effective_pieces = [
            piece
            for piece in pieces
            if piece.effective_date <= as_of
        ]
        settled_units, settled_amount = self._settled_by(
            event, pieces, as_of
        )
        settled_units = _abs(settled_units)
        settled_amount = _abs(settled_amount)
        failed_units = sum(
            (
                _abs(piece.units)
                for piece in effective_pieces
                if piece.event_type is EventType.FAIL
            ),
            ZERO,
        )
        failed_amount = sum(
            (
                _abs(piece.amount)
                for piece in effective_pieces
                if piece.event_type is EventType.FAIL
            ),
            ZERO,
        )
        remaining_units = _abs(event.units) - settled_units
        remaining_amount = _abs(event.amount) - settled_amount
        cancelled = any(
            cancel.effective_date <= as_of for cancel in cancels
        )
        if cancelled:
            status = SettlementStatus.CANCELLED
        elif remaining_units <= ZERO and remaining_amount <= ZERO:
            status = SettlementStatus.SETTLED
        elif failed_units or failed_amount:
            status = SettlementStatus.FAILED
        elif settled_units > ZERO or settled_amount > ZERO:
            status = SettlementStatus.PARTIAL
        else:
            status = SettlementStatus.PENDING
        return TradeRecord(
            fitid=event.fitid,
            account=event.account,
            security_id=event.security_id,
            units=event.units,
            amount=event.amount,
            trade_date=event.trade_date or event.effective_date,
            settlement_date=event.settlement_date or event.effective_date,
            status=status,
            settled_units=settled_units,
            failed_units=failed_units,
            failed_amount=failed_amount,
            settled_amount=settled_amount,
            pieces=tuple(piece.fitid for piece in effective_pieces),
            cancelled=cancelled,
        )

    def _visible_trades(
        self,
        as_of: datetime.date,
        known: datetime.date,
    ) -> tuple[
        dict[tuple[str, str], list[SettlementEvent]],
        dict[tuple[str, str], list[SettlementEvent]],
        list[_TradeState],
    ]:
        visible = [
            event
            for event in self._events
            if event.known_date is not None and event.known_date <= known
        ]
        pieces_by_trade = self._pieces_by_trade(visible)
        cancels_by_trade = self._cancels_by_trade(visible)
        trades = [
            trade
            for trade in self._trades.values()
            if trade.event.effective_date <= as_of
            and trade.event.known_date is not None
            and trade.event.known_date <= known
        ]
        return pieces_by_trade, cancels_by_trade, trades

    def trade_records(
        self,
        date: datetime.date | None = None,
        known_as_of: datetime.date | None = None,
    ) -> tuple[TradeRecord, ...]:
        """
        One record per original trade, linked to its settlement pieces.

        ``date`` and ``known_as_of`` choose the effective and knowledge
        cutoffs; both default to the latest known date in the ledger.
        """
        as_of, known = self.default_cutoffs(date, known_as_of)
        pieces_by_trade, cancels_by_trade, trades = self._visible_trades(
            as_of, known
        )
        records = []
        for trade in trades:
            key = (trade.event.account, trade.event.fitid)
            records.append(
                self._trade_record_at(
                    trade,
                    pieces_by_trade.get(key, []),
                    as_of,
                    cancels_by_trade.get(key, []),
                )
            )
        return tuple(records)

    def trades(
        self,
        status: SettlementStatus | str | None = None,
        date: datetime.date | None = None,
        known_as_of: datetime.date | None = None,
    ) -> list[TradeRecord]:
        """Trade records, optionally filtered by settlement status."""
        if isinstance(status, str):
            status = SettlementStatus(status)
        return [
            record
            for record in self.trade_records(date, known_as_of)
            if status is None or record.status is status
        ]

    def settlement_pieces(
        self, fitid: str, account: str | None = None
    ) -> list[SettlementEvent]:
        """Settlement pieces linked to the original trade ``fitid``."""
        return [
            event
            for event in self._events
            if event.reference == fitid
            and event.is_piece
            and (account is None or event.account == account)
        ]

    def pending_trades(
        self, date: datetime.date, known_as_of: datetime.date | None = None
    ) -> list[TradeRecord]:
        """Trades with an open settlement obligation as of ``date``."""
        return [
            record
            for record in self.trade_records(date, known_as_of)
            if record.status
            in (
                SettlementStatus.PENDING,
                SettlementStatus.PARTIAL,
                SettlementStatus.FAILED,
            )
        ]

    def default_cutoffs(
        self, date: datetime.date | None, known_as_of: datetime.date | None
    ) -> tuple[datetime.date, datetime.date]:
        if date is None:
            dates = []
            for event in self._events:
                dates.append(event.effective_date)
                if event.known_date is not None:
                    dates.append(event.known_date)
                if event.settlement_date is not None:
                    dates.append(event.settlement_date)
            date = max(dates) if dates else datetime.date.today()
        as_of = _check_date(date, "date")
        if known_as_of is None:
            return as_of, as_of
        return as_of, _check_date(known_as_of, "known_as_of")

    # ------------------------------------------------------------------
    #  OFX integration
    # ------------------------------------------------------------------
    @classmethod
    def from_statements(
        cls,
        statements: Iterable[Any],
        *,
        known_dates: Mapping[str, datetime.date] | None = None,
        default_known_date: datetime.date | None = None,
        rules: SettlementRules | None = None,
        strict: bool = False,
    ) -> SettlementLedger:
        """Build a ledger from OFX statement aggregates."""
        ledger = cls(strict=strict)
        ledger.extend(
            transactions_from_statements(
                statements,
                known_dates=known_dates,
                default_known_date=default_known_date,
                rules=rules,
            )
        )
        return ledger

    @classmethod
    def from_ofx(cls, ofx: Any, **kwargs: Any) -> SettlementLedger:
        """
        Build a ledger from a parsed :class:`ofxtools.models.ofx.OFX`.

        ``known_dates`` maps ``FITID`` to the date a confirmation was
        actually received; ``default_known_date`` applies to FITIDs missing
        from the mapping.
        """
        return cls.from_statements(ofx.statements, **kwargs)


# ----------------------------------------------------------------------
#  OFX model translation
# ----------------------------------------------------------------------
_TRADE_AGGREGATES = (
    "buydebt",
    "buymf",
    "buyopt",
    "buyother",
    "buystock",
    "selldebt",
    "sellmf",
    "sellopt",
    "sellother",
    "sellstock",
)
_CASH_AGGREGATES = (
    "income",
    "invexpense",
    "margininterest",
    "reinvest",
    "retofcap",
)
_SECURITY_AGGREGATES = ("closureopt", "split", "transfer")


def _as_date(value: Any) -> datetime.date | None:
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    raise TypeError(f"expected date/datetime, got {type(value).__name__}")


def _security_of(aggregate: Any) -> tuple[str | None, str | None]:
    secid = getattr(aggregate, "secid", None)
    if secid is None:
        return None, None
    return secid.uniqueid, secid.uniqueidtype


def _settlement_cycle(aggregate: Any, rules: SettlementRules | None = None) -> int:
    """Look up the settlement cycle for an OFX transaction aggregate."""
    table = rules or DEFAULT_SETTLEMENT_RULES
    _, security_type = _security_of(aggregate)
    keys = (type(aggregate).__name__.upper(), security_type)
    if isinstance(table, SettlementRules):
        return table.cycle(*keys)
    for rule in table:
        if rule.matches(*keys):
            return rule.business_days
    raise SettlementError(
        f"no settlement rule matches {type(aggregate).__name__}"
    )


def _cash_amount(aggregate: Any) -> Decimal:
    total = getattr(aggregate, "total", None)
    return ZERO if total is None else Decimal(total)


def _known_date_for(
    fitid: str,
    known_dates: Mapping[str, datetime.date] | None,
    default_known_date: datetime.date | None,
) -> datetime.date | None:
    if known_dates and fitid in known_dates:
        return _as_date(known_dates[fitid])
    if default_known_date is not None:
        return _as_date(default_known_date)
    return None


def _base_event(
    invtran: Any,
    *,
    account: str,
    currency: str,
    security_id: str | None,
    security_type: str | None,
    units: Decimal,
    amount: Decimal,
    cycle: int,
    known_dates: Mapping[str, datetime.date] | None,
    default_known_date: datetime.date | None,
) -> SettlementEvent | None:
    trade_date = _as_date(invtran.dttrade)
    if trade_date is None:
        return None
    reversal = getattr(invtran, "reversalfitid", None)
    if reversal:
        # OFX expresses a cancellation as a new transaction whose INVTRAN
        # carries REVERSALFITID.  Its own date is the cancellation date.
        return SettlementEvent(
            fitid=invtran.fitid,
            effective_date=trade_date,
            event_type=EventType.CANCEL,
            known_date=_known_date_for(
                invtran.fitid, known_dates, default_known_date
            ),
            reference=reversal,
            security_id=security_id,
            security_type=security_type,
            account=account,
            currency=currency,
            units=units,
            amount=amount,
            memo=getattr(invtran, "memo", None),
        )
    return SettlementEvent(
        fitid=invtran.fitid,
        effective_date=trade_date,
        event_type=EventType.TRADE,
        known_date=_known_date_for(
            invtran.fitid, known_dates, default_known_date
        ),
        trade_date=trade_date,
        settlement_date=_as_date(invtran.dtsettle),
        security_id=security_id,
        security_type=security_type,
        account=account,
        currency=currency,
        units=units,
        amount=amount,
        settlement_cycle=cycle,
        memo=getattr(invtran, "memo", None),
    )


def _trade_event(
    aggregate: Any,
    account: str,
    currency: str,
    known_dates: Mapping[str, datetime.date] | None,
    default_known_date: datetime.date | None,
    rules: SettlementRules | None = None,
) -> SettlementEvent | None:
    name = type(aggregate).__name__.lower()
    if name.startswith("buy"):
        inner = aggregate.invbuy
    elif name.startswith("sell"):
        inner = aggregate.invsell
    else:
        return None
    security_id, security_type = _security_of(inner)
    # OFX quantities are already signed: buys positive, sells negative.
    units = ZERO if inner.units is None else Decimal(inner.units)
    return _base_event(
        inner.invtran,
        account=account,
        currency=currency,
        security_id=security_id,
        security_type=security_type,
        units=units,
        amount=_cash_amount(inner),
        cycle=_settlement_cycle(aggregate, rules),
        known_dates=known_dates,
        default_known_date=default_known_date,
    )


def _cash_event(
    aggregate: Any,
    account: str,
    currency: str,
    known_dates: Mapping[str, datetime.date] | None,
    default_known_date: datetime.date | None,
) -> SettlementEvent | None:
    """
    Income, expenses, fees and return of capital are cash-basis items.

    OFX gives them no ``DTSETTLE``, so they post straight to settled cash on
    the transaction date rather than creating a settlement obligation.  A
    reinvestment is different: it buys shares with an already-credited
    distribution, so only the security leg moves and the cash leg is zero.
    """
    invtran = getattr(aggregate, "invtran", None)
    if invtran is None:
        return None
    trade_date = _as_date(invtran.dttrade)
    if trade_date is None:
        return None
    security_id, security_type = _security_of(aggregate)
    known = _known_date_for(
        invtran.fitid, known_dates, default_known_date
    )
    if type(aggregate).__name__.lower() == "reinvest":
        raw = getattr(aggregate, "units", None)
        units = ZERO if raw is None else Decimal(raw)
        return SettlementEvent(
            fitid=invtran.fitid,
            effective_date=trade_date,
            event_type=EventType.TRADE,
            known_date=known,
            trade_date=trade_date,
            settlement_date=trade_date,
            security_id=security_id,
            security_type=security_type,
            account=account,
            currency=currency,
            units=units,
            amount=ZERO,
            memo=getattr(invtran, "memo", None),
        )
    return SettlementEvent(
        fitid=invtran.fitid,
        effective_date=trade_date,
        event_type=EventType.CASH,
        known_date=known,
        settlement_date=trade_date,
        security_id=security_id,
        security_type=security_type,
        account=account,
        currency=currency,
        amount=_cash_amount(aggregate),
        memo=getattr(invtran, "memo", None),
    )


def _security_event(
    aggregate: Any,
    account: str,
    currency: str,
    known_dates: Mapping[str, datetime.date] | None,
    default_known_date: datetime.date | None,
    rules: SettlementRules | None = None,
) -> SettlementEvent | None:
    invtran = getattr(aggregate, "invtran", None)
    if invtran is None:
        return None
    name = type(aggregate).__name__.lower()
    units = ZERO
    if name == "split":
        units = Decimal(aggregate.newunits) - Decimal(aggregate.oldunits)
    elif name == "transfer":
        units = Decimal(aggregate.units)
        if aggregate.tferaction == "OUT":
            units = -units
    elif name == "closureopt":
        units = -Decimal(aggregate.units)
    security_id, security_type = _security_of(aggregate)
    trade_date = _as_date(invtran.dttrade)
    if trade_date is None:
        return None
    reversal = getattr(invtran, "reversalfitid", None)
    if reversal:
        return SettlementEvent(
            fitid=invtran.fitid,
            effective_date=trade_date,
            event_type=EventType.CANCEL,
            known_date=_known_date_for(
                invtran.fitid, known_dates, default_known_date
            ),
            reference=reversal,
            security_id=security_id,
            security_type=security_type,
            account=account,
            currency=currency,
            units=units,
            amount=_cash_amount(aggregate),
            memo=getattr(invtran, "memo", None),
        )
    return _base_event(
        invtran,
        account=account,
        currency=currency,
        security_id=security_id,
        security_type=security_type,
        units=units,
        amount=_cash_amount(aggregate),
        cycle=_settlement_cycle(aggregate, rules),
        known_dates=known_dates,
        default_known_date=default_known_date,
    )


def _bank_transfer_event(
    aggregate: Any,
    account: str,
    currency: str,
    known_dates: Mapping[str, datetime.date] | None,
    default_known_date: datetime.date | None,
) -> SettlementEvent | None:
    """INVBANKTRAN cash sweeps are settled cash, not a settlement obligation."""
    stmttrn = getattr(aggregate, "stmttrn", None)
    if stmttrn is None:
        return None
    posted = _as_date(stmttrn.dtposted)
    if posted is None:
        return None
    return SettlementEvent(
        fitid=stmttrn.fitid,
        effective_date=posted,
        event_type=EventType.CASH,
        known_date=_known_date_for(
            stmttrn.fitid, known_dates, default_known_date
        ),
        trade_date=None,
        settlement_date=posted,
        account=account,
        currency=currency,
        amount=Decimal(stmttrn.trnamt),
        memo=getattr(stmttrn, "memo", None),
    )


def _statement_account(statement: Any) -> str:
    account = getattr(statement, "account", None)
    if account is None:
        return ""
    return str(getattr(account, "acctid", ""))


def transactions_from_statement(
    statement: Any,
    *,
    known_dates: Mapping[str, datetime.date] | None = None,
    default_known_date: datetime.date | None = None,
    rules: SettlementRules | None = None,
) -> list[SettlementEvent]:
    """
    Convert one OFX investment statement into settlement events.

    Bank statements and statements without ``INVTRANLIST`` yield no events.
    Internal journals (``JRNLFUND``/``JRNLSEC``) are skipped because they do
    not change account-wide holdings or cash.
    """
    events: list[SettlementEvent] = []
    tranlist = getattr(statement, "invtranlist", None)
    if tranlist is None:
        return events
    account = _statement_account(statement)
    currency = str(getattr(statement, "curdef", "") or "")
    for aggregate in tranlist:
        name = type(aggregate).__name__.lower()
        event = None
        if name in _TRADE_AGGREGATES:
            event = _trade_event(
                aggregate,
                account,
                currency,
                known_dates,
                default_known_date,
                rules,
            )
        elif name in _CASH_AGGREGATES:
            event = _cash_event(
                aggregate, account, currency, known_dates, default_known_date
            )
        elif name in _SECURITY_AGGREGATES:
            event = _security_event(
                aggregate,
                account,
                currency,
                known_dates,
                default_known_date,
                rules,
            )
        elif name == "invbanktran":
            event = _bank_transfer_event(
                aggregate, account, currency, known_dates, default_known_date
            )
        if event is not None:
            events.append(event)
    return events


def transactions_from_statements(
    statements: Iterable[Any],
    *,
    known_dates: Mapping[str, datetime.date] | None = None,
    default_known_date: datetime.date | None = None,
    rules: SettlementRules | None = None,
) -> Iterator[SettlementEvent]:
    """Yield events for every statement in ``statements``."""
    for statement in statements:
        yield from transactions_from_statement(
            statement,
            known_dates=known_dates,
            default_known_date=default_known_date,
            rules=rules,
        )


def event_from_dict(payload: Mapping[str, Any]) -> SettlementEvent:
    """
    Build a :class:`SettlementEvent` from a JSON-friendly mapping.

    Required keys are ``fitid``, ``effective_date`` and ``event_type``;
    dates accept ``YYYY-MM-DD`` or ``YYYYMMDD``.  ``units`` and ``amount``
    are converted to ``Decimal``.  The mapping is the interchange format
    used by ``ofxledger --confirmations``.
    """
    data = dict(payload)
    for key in ("effective_date", "known_date", "trade_date", "settlement_date"):
        if key in data and data[key] is not None:
            data[key] = _parse_iso_date(data[key], key)
    if "event_type" in data:
        data["event_type"] = EventType(str(data["event_type"]).upper())
    for key in ("units", "amount"):
        if key in data and data[key] is not None:
            data[key] = Decimal(str(data[key]))
    unknown = set(data) - {
        field_name for field_name in SettlementEvent.__dataclass_fields__
    }
    if unknown:
        raise SettlementError(f"unknown settlement event fields: {sorted(unknown)}")
    return SettlementEvent(**data)


def _parse_iso_date(value: Any, label: str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value)
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise SettlementError(f"{label} must be YYYY-MM-DD or YYYYMMDD, got {value!r}")
