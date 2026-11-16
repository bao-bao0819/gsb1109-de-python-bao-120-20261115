"""
Reconcile securities on loan using OFX investment records together with an
explicit lending/return correspondence table.

The correspondence table is authoritative.  It names each loan with a stable
``loan_id``, states the security, counterparty, direction and units, and links
repayments, early recalls and corporate actions to the loan they belong to.
OFX investment records are evidence only: they never create, merge or cancel a
loan on their own.

Reconciliation keeps one open batch per still-unsettled slice of a loan:

* a partial return (or recall) settles only the units it covers and leaves the
  remainder open as its own batch;
* a recall removes units from the loan from the recall date without making a
  cash settlement;
* a split or other corporate action scales the units of every batch of the
  affected security that is open *before* the action date (a loan opened on the
  action date is assumed to already be stated in post-action units);
* loans of the same security in opposite directions never offset each other,
  even where security, counterparty, units and date all match.

Evidence that cannot be tied to a loan by an explicit table link is reported as
undetermined (``Unmatched``) instead of being guessed or silently dropped.
When quantities do not conserve, the result carries a ``QuantityMismatch``
whose ``records`` tuple names the original table and OFX records plus the
arithmetic that failed.

The module is independent of ``ofxtools.models`` at import time; helper
functions translate parsed ``INVSTMTRS`` objects into ``Evidence`` rows lazily.
"""

from __future__ import annotations

__all__ = [
    "LendingError",
    "TableError",
    "EvidenceError",
    "Lend",
    "Borrow",
    "Return",
    "Recall",
    "Split",
    "CorporateAction",
    "Evidence",
    "OpenBatch",
    "SettledBatch",
    "Unmatched",
    "QuantityMismatch",
    "Reconciliation",
    "Reconciler",
    "reconcile",
    "reconcile_statements",
    "result_batches",
    "evidence_from_transaction",
    "evidence_from_statement",
    "actions_from_statement",
    "security_key",
    "table_from_dict",
    "reconciliation_to_dict",
    "reconcile_file",
]


import datetime
import decimal
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")


class LendingError(ValueError):
    """Base class for securities-lending reconciliation errors."""


class TableError(LendingError):
    """The lending/return correspondence table is malformed."""


class EvidenceError(LendingError):
    """An OFX evidence record is malformed or ambiguous."""


def _as_date(value: Any, label: str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    raise TableError(f"{label} must be a date, got {value!r}")


def _as_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise TableError(f"{label} must be a number, got {value!r}")
    try:
        result = Decimal(value)
    except (decimal.InvalidOperation, ValueError) as exc:
        raise TableError(f"{label} must be a number, got {value!r}") from exc
    if not result.is_finite():
        raise TableError(f"{label} must be finite, got {value!r}")
    return result


def _positive(value: Any, label: str) -> Decimal:
    result = _as_decimal(value, label)
    if result <= ZERO:
        raise TableError(f"{label} must be positive, got {result}")
    return result


def _nonempty(value: str, label: str) -> str:
    if not value:
        raise TableError(f"{label} must not be empty")
    return value


@dataclass(frozen=True)
class Lend:
    """Units of ``security`` lent out under ``loan_id``, effective ``date``.

    ``fitid`` optionally names the OFX transaction that opened the loan, making
    the correspondence to evidence explicit.
    """

    loan_id: str
    security: str
    counterparty: str
    units: Decimal
    date: datetime.date
    fitid: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _as_date(self.date, "lend.date"))
        object.__setattr__(self, "units", _positive(self.units, "lend.units"))
        _nonempty(self.loan_id, "lend.loan_id")
        _nonempty(self.security, "lend.security")
        _nonempty(self.counterparty, "lend.counterparty")


@dataclass(frozen=True)
class Borrow:
    """Units of ``security`` borrowed in under ``loan_id``, effective ``date``.

    Held in a separate direction from :class:`Lend`; a borrow never nets
    against a lend, even for the same security and counterparty.
    """

    loan_id: str
    security: str
    counterparty: str
    units: Decimal
    date: datetime.date
    fitid: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _as_date(self.date, "borrow.date"))
        object.__setattr__(self, "units", _positive(self.units, "borrow.units"))
        _nonempty(self.loan_id, "borrow.loan_id")
        _nonempty(self.security, "borrow.security")
        _nonempty(self.counterparty, "borrow.counterparty")


@dataclass(frozen=True)
class Return:
    """Units returned against ``loan_id`` on ``date``; partial returns allowed.

    ``fitid`` should be the OFX transaction that evidences the return.
    """

    loan_id: str
    units: Decimal
    date: datetime.date
    fitid: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _as_date(self.date, "return.date"))
        object.__setattr__(self, "units", _positive(self.units, "return.units"))
        _nonempty(self.loan_id, "return.loan_id")


@dataclass(frozen=True)
class Recall:
    """Early recall of ``units`` from ``loan_id`` effective ``date``.

    A recall reduces the on-loan balance from the recall date and makes no cash
    settlement; recalled slices appear in ``settled_batches`` with settlement
    ``RECALL``.  ``units=None`` recalls everything open on that date.
    """

    loan_id: str
    units: Decimal | None
    date: datetime.date
    fitid: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _as_date(self.date, "recall.date"))
        if self.units is not None:
            object.__setattr__(self, "units", _positive(self.units, "recall.units"))
        _nonempty(self.loan_id, "recall.loan_id")


@dataclass(frozen=True)
class Split:
    """Split/counter-split; units of ``security`` open before ``date`` scale."""

    security: str
    date: datetime.date
    numerator: Decimal
    denominator: Decimal
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _as_date(self.date, "split.date"))
        object.__setattr__(
            self, "numerator", _positive(self.numerator, "split.numerator")
        )
        object.__setattr__(
            self, "denominator", _positive(self.denominator, "split.denominator")
        )
        _nonempty(self.security, "split.security")

    @property
    def ratio(self) -> Decimal:
        return self.numerator / self.denominator


@dataclass(frozen=True)
class CorporateAction:
    """Corporate action scaling open units of ``security`` by ``ratio``.

    Suitable for stock dividends, bonus issues, reverse splits and similar
    unit-multiplying events.  Cash-only events do not change the on-loan unit
    count and should not be modelled here.
    """

    security: str
    date: datetime.date
    ratio: Decimal
    action: str = "CORPORATEACTION"
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _as_date(self.date, "corporateaction.date"))
        object.__setattr__(
            self, "ratio", _positive(self.ratio, "corporateaction.ratio")
        )
        _nonempty(self.security, "corporateaction.security")
        _nonempty(self.action, "corporateaction.action")


@dataclass(frozen=True)
class Evidence:
    """An OFX investment record offered as evidence of a loan movement.

    ``direction`` is ``"LEND"`` or ``"BORROW"`` for a record that opens a loan
    in that direction, or ``"RETURN"`` for a record that closes units.  A
    ``BORROW`` record can never be used against a ``LEND`` loan, even for the
    same security and counterparty.

    ``loan_id`` is the explicit link from the correspondence table; without it
    (and without a FITID mapping) the record stays undetermined.
    """

    fitid: str
    security: str
    units: Decimal
    date: datetime.date
    direction: str
    loan_id: str | None = None
    kind: str = "OFX"
    account: str | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", _as_date(self.date, "ofx.date"))
        object.__setattr__(self, "units", _positive(self.units, "ofx.units"))
        direction = str(self.direction).upper()
        if direction not in ("LEND", "BORROW", "RETURN"):
            raise EvidenceError(
                "OFX direction must be LEND, BORROW or RETURN, "
                f"got {direction!r}"
            )
        object.__setattr__(self, "direction", direction)
        if not self.fitid:
            raise EvidenceError("OFX record must have a FITID")
        if not self.security:
            raise EvidenceError(f"OFX record {self.fitid} must name a security")


@dataclass(frozen=True)
class OpenBatch:
    """A still-unsettled slice of a loan.

    ``records`` names the original table/OFX records that produced the slice
    (the opening loan row plus any corporate actions applied to it), so a later
    mismatch can be traced back without re-deriving the timeline.
    """

    loan_id: str
    security: str
    counterparty: str
    direction: str
    units: Decimal
    opened: datetime.date
    batch: int
    actions: tuple[str, ...] = ()
    records: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettledBatch:
    """A slice of a loan closed by a return or recall.

    ``records`` names the original records that produced the slice: the
    opening loan row, any corporate actions, and the settlement row.
    """

    loan_id: str
    security: str
    counterparty: str
    direction: str
    units: Decimal
    opened: datetime.date
    closed: datetime.date
    batch: int
    settlement: str
    fitid: str | None = None
    actions: tuple[str, ...] = ()
    records: tuple[str, ...] = ()


@dataclass(frozen=True)
class Unmatched:
    """A record that could not be tied to a loan by explicit evidence.

    ``reason`` is one of ``NO_LOAN_ID``, ``UNKNOWN_LOAN``, ``WRONG_SECURITY``,
    ``WRONG_DIRECTION``, ``NO_TABLE_RECORD`` or ``NO_EVIDENCE``.
    """

    source: str
    fitid: str | None
    security: str | None
    units: Decimal | None
    date: datetime.date | None
    direction: str | None
    reason: str
    detail: str
    loan_id: str | None = None


@dataclass(frozen=True)
class QuantityMismatch:
    """Quantities do not conserve units.

    ``kind`` is ``CONSERVATION`` for table-side over-settlement, ``EXPECTED``
    when the computed open quantity differs from the table's stated closing
    figure, and ``EVIDENCE`` when OFX evidence disagrees with the table.

    ``records`` names the original records that form the failed arithmetic,
    e.g. ``loan:L1@2024-01-02``, ``return:OFX-3``, ``ofx:OFX-3`` or
    ``action:SPLIT:2/1@2024-05-01``.  ``delta`` is the signed change applied
    to ``opening`` to obtain ``closing``.
    """

    loan_id: str
    security: str
    direction: str
    opening: Decimal
    delta: Decimal
    closing: Decimal
    expected: Decimal | None
    records: tuple[str, ...]
    detail: str
    kind: str = "CONSERVATION"


@dataclass
class Reconciliation:
    """Result of reconciling a correspondence table against OFX evidence."""

    asof: datetime.date
    open_batches: list[OpenBatch] = field(default_factory=list)
    settled_batches: list[SettledBatch] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)
    mismatches: list[QuantityMismatch] = field(default_factory=list)

    @property
    def balanced(self) -> bool:
        """True when every loan is fully evidenced and quantities conserve."""
        return not (self.unmatched or self.mismatches)

    def on_loan(self, direction: str = "LEND") -> dict[str, Decimal]:
        """Per-security open units for one direction only."""
        totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for batch in self.open_batches:
            if batch.direction == direction:
                totals[batch.security] += batch.units
        return dict(totals)

    def open_for(self, loan_id: str) -> list[OpenBatch]:
        return [batch for batch in self.open_batches if batch.loan_id == loan_id]

    def batch(self, loan_id: str, batch: int) -> OpenBatch | SettledBatch | None:
        """Look up one batch by its stable ``(loan_id, batch)`` address.

        Batch numbers run consecutively per loan across both open and settled
        slices, so an address is unique within a reconciliation.
        """
        for item in result_batches(self):
            if item.loan_id == loan_id and item.batch == batch:
                return item
        return None


@dataclass
class _Batch:
    units: Decimal
    opened: datetime.date
    actions: list[str] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)


class Reconciler:
    """Build a :class:`Reconciliation` from a table plus OFX evidence."""

    def __init__(
        self,
        loans: Sequence[Lend | Borrow],
        returns: Sequence[Return] = (),
        recalls: Sequence[Recall] = (),
        actions: Sequence[Split | CorporateAction] = (),
        evidence: Sequence[Evidence] = (),
        *,
        correspondence: Mapping[str, str] | None = None,
        asof: datetime.date | None = None,
        expected: Mapping[str, Decimal | None] | None = None,
        require_evidence: bool = True,
    ) -> None:
        self.loans = list(loans)
        self.returns = list(returns)
        self.recalls = list(recalls)
        self.actions = list(actions)
        self.evidence = list(evidence)
        self.correspondence = dict(correspondence or {})
        self.asof = _as_date(asof, "asof") if asof is not None else None
        self.expected = {
            key: (None if value is None else _as_decimal(value, "expected"))
            for key, value in (expected or {}).items()
        }
        self.require_evidence = require_evidence

    # -- table validation -------------------------------------------------
    def _loan_map(self) -> dict[str, Lend | Borrow]:
        loans: dict[str, Lend | Borrow] = {}
        for loan in self.loans:
            if loan.loan_id in loans:
                raise TableError(f"duplicate loan_id {loan.loan_id!r}")
            loans[loan.loan_id] = loan
        settlements: list[Return | Recall] = [*self.returns, *self.recalls]
        for settlement in settlements:
            if settlement.loan_id not in loans:
                raise TableError(
                    f"settlement references unknown loan {settlement.loan_id!r}"
                )
        for fitid, loan_id in self.correspondence.items():
            if loan_id not in loans:
                raise TableError(
                    f"correspondence maps FITID {fitid!r} to unknown loan {loan_id!r}"
                )
        for loan_id in self.expected:
            if loan_id not in loans:
                raise TableError(f"expected quantity for unknown loan {loan_id!r}")
        return loans

    def _table_links(self, loans: Mapping[str, Lend | Borrow]) -> dict[str, str]:
        """Explicit FITID -> loan links contributed by the table itself."""
        links: dict[str, str] = {}
        for loan in self.loans:
            if loan.fitid:
                self._add_link(links, loan.fitid, loan.loan_id, "loan")
        for returned in self.returns:
            if returned.fitid:
                self._add_link(
                    links, returned.fitid, returned.loan_id, "return"
                )
        for recall in self.recalls:
            if recall.fitid:
                self._add_link(links, recall.fitid, recall.loan_id, "recall")
        for fitid, loan_id in self.correspondence.items():
            self._add_link(links, fitid, loan_id, "correspondence")
        return links

    @staticmethod
    def _add_link(
        links: dict[str, str], fitid: str, loan_id: str, source: str
    ) -> None:
        existing = links.get(fitid)
        if existing is not None and existing != loan_id:
            raise TableError(
                f"FITID {fitid!r} from {source} is linked to both "
                f"{existing!r} and {loan_id!r}"
            )
        links[fitid] = loan_id

    def _asof(self) -> datetime.date:
        if self.asof is not None:
            return self.asof
        dates = [loan.date for loan in self.loans]
        dates += [item.date for item in self.returns]
        dates += [item.date for item in self.recalls]
        dates += [item.date for item in self.actions]
        dates += [item.date for item in self.evidence]
        return max(dates) if dates else datetime.date.today()

    # -- reconciliation ---------------------------------------------------
    def run(self) -> Reconciliation:
        loans = self._loan_map()
        links = self._table_links(loans)
        asof = self._asof()
        result = Reconciliation(asof=asof)

        # Actions first on a given date, then openings, then settlements, so a
        # return on the action date settles post-action units while a loan
        # opened on the action date is already in post-action units.
        events: list[tuple[datetime.date, int, int, Any]] = []
        for seq, action in enumerate(self.actions):
            events.append((action.date, 0, seq, action))
        for seq, loan in enumerate(self.loans):
            events.append((loan.date, 1, seq, loan))
        settlements: list[Return | Recall] = [*self.recalls, *self.returns]
        for seq, settlement in enumerate(settlements):
            events.append((settlement.date, 2, seq, settlement))
        events.sort(key=lambda item: (item[0], item[1], item[2]))

        batches: dict[str, list[_Batch]] = defaultdict(list)
        records: dict[str, list[str]] = defaultdict(list)
        adjusted_opening: dict[str, Decimal] = {
            loan.loan_id: loan.units for loan in self.loans
        }
        batch_counter: dict[str, int] = defaultdict(int)

        for date, _priority, _seq, event in events:
            if date > asof:
                continue
            if isinstance(event, (Split, CorporateAction)):
                self._apply_action(
                    event, loans, batches, records, adjusted_opening, date
                )
            elif isinstance(event, (Lend, Borrow)):
                batch = _Batch(
                    event.units,
                    event.date,
                    record_ids=[_loan_record(event)],
                )
                if event.fitid:
                    batch.record_ids.append(f"ofx:{event.fitid}")
                batches[event.loan_id].append(batch)
                records[event.loan_id].append(_loan_record(event))
            elif isinstance(event, (Return, Recall)):
                self._settle(
                    event,
                    loans[event.loan_id],
                    batches,
                    records,
                    batch_counter,
                    result,
                )
            else:  # pragma: no cover - guarded by construction
                raise TableError(f"unexpected event {event!r}")

        self._emit_batches(loans, batches, batch_counter, result)
        self._check_expected(loans, adjusted_opening, records, result)
        self._check_evidence(loans, links, result)
        self._sort(result)
        return result

    def _apply_action(
        self,
        action: Split | CorporateAction,
        loans: Mapping[str, Lend | Borrow],
        batches: Mapping[str, list[_Batch]],
        records: dict[str, list[str]],
        adjusted_opening: dict[str, Decimal],
        date: datetime.date,
    ) -> None:
        if isinstance(action, Split):
            ratio = action.ratio
            label = f"SPLIT:{action.numerator}/{action.denominator}"
        else:
            ratio = action.ratio
            label = f"ACTION:{action.action}:{action.ratio}"
        for loan in loans.values():
            if loan.security != action.security:
                continue
            if loan.date >= date:
                # Opened on/after the action date: already post-action units.
                continue
            loan_batches = batches.get(loan.loan_id, [])
            if not loan_batches:
                continue
            open_units = sum((batch.units for batch in loan_batches), ZERO)
            if open_units <= ZERO:
                # Fully settled before the action; nothing left to scale.
                continue
            for batch in loan_batches:
                if batch.units <= ZERO:
                    continue
                batch.units *= ratio
                batch.actions.append(label)
                batch.record_ids.append(f"action:{label}@{date.isoformat()}")
            adjusted_opening[loan.loan_id] *= ratio
            records[loan.loan_id].append(f"action:{label}@{date.isoformat()}")

    def _settle(
        self,
        event: Return | Recall,
        loan: Lend | Borrow,
        batches: Mapping[str, list[_Batch]],
        records: Mapping[str, list[str]],
        batch_counter: dict[str, int],
        result: Reconciliation,
    ) -> None:
        loan_batches = batches[event.loan_id]
        open_units = sum((batch.units for batch in loan_batches), ZERO)
        requested = open_units if event.units is None else event.units
        units = min(requested, open_units)
        source = "RETURN" if isinstance(event, Return) else "RECALL"
        settlement_record = _settlement_record(event)

        if requested > open_units:
            result.mismatches.append(
                QuantityMismatch(
                    loan_id=loan.loan_id,
                    security=loan.security,
                    direction=_direction(loan),
                    opening=open_units,
                    delta=-requested,
                    closing=open_units - requested,
                    expected=ZERO,
                    records=_mismatch_records(loan, records[loan.loan_id], settlement_record),
                    detail=(
                        f"loan {loan.loan_id}: {source} of {requested} on "
                        f"{event.date} exceeds the {open_units} units open"
                    ),
                )
            )

        remaining = units
        for batch in loan_batches:
            if remaining <= ZERO:
                break
            if batch.units <= ZERO:
                continue
            take = min(batch.units, remaining)
            batch.units -= take
            remaining -= take
            result.settled_batches.append(
                SettledBatch(
                    loan_id=loan.loan_id,
                    security=loan.security,
                    counterparty=loan.counterparty,
                    direction=_direction(loan),
                    units=take,
                    opened=batch.opened,
                    closed=event.date,
                    batch=batch_counter[loan.loan_id],
                    settlement=source,
                    fitid=event.fitid,
                    actions=tuple(batch.actions),
                    records=tuple([*batch.record_ids, settlement_record]),
                )
            )
            batch_counter[loan.loan_id] += 1
            batch.record_ids.append(settlement_record)

        if units > ZERO:
            records[loan.loan_id].append(settlement_record)

    def _emit_batches(
        self,
        loans: Mapping[str, Lend | Borrow],
        batches: Mapping[str, list[_Batch]],
        batch_counter: dict[str, int],
        result: Reconciliation,
    ) -> None:
        for loan in loans.values():
            for batch in batches.get(loan.loan_id, []):
                if batch.units <= ZERO:
                    continue
                result.open_batches.append(
                    OpenBatch(
                        loan_id=loan.loan_id,
                        security=loan.security,
                        counterparty=loan.counterparty,
                        direction=_direction(loan),
                        units=batch.units,
                        opened=batch.opened,
                        batch=batch_counter[loan.loan_id],
                        actions=tuple(batch.actions),
                        records=tuple(batch.record_ids),
                    )
                )
                batch_counter[loan.loan_id] += 1

    def _check_expected(
        self,
        loans: Mapping[str, Lend | Borrow],
        adjusted_opening: Mapping[str, Decimal],
        records: Mapping[str, list[str]],
        result: Reconciliation,
    ) -> None:
        for loan_id, expected in self.expected.items():
            if expected is None:
                continue
            loan = loans[loan_id]
            closing = sum(
                (
                    batch.units
                    for batch in result.open_batches
                    if batch.loan_id == loan_id
                ),
                ZERO,
            )
            if closing == expected:
                continue
            opening = adjusted_opening[loan_id]
            result.mismatches.append(
                QuantityMismatch(
                    loan_id=loan_id,
                    security=loan.security,
                    direction=_direction(loan),
                    opening=opening,
                    delta=closing - opening,
                    closing=closing,
                    expected=expected,
                    records=tuple(records.get(loan_id, ())),
                    detail=(
                        f"loan {loan_id}: adjusted opening {opening}, "
                        f"open {closing} at {result.asof}, expected {expected}"
                    ),
                    kind="EXPECTED",
                )
            )

    def _check_evidence(
        self,
        loans: Mapping[str, Lend | Borrow],
        links: Mapping[str, str],
        result: Reconciliation,
    ) -> None:
        evidence_by_fitid: dict[str, Evidence] = {}
        for record in self.evidence:
            existing = evidence_by_fitid.get(record.fitid)
            if existing is None:
                evidence_by_fitid[record.fitid] = record
                continue
            # Re-downloading a statement yields the same FITID again.  Identical
            # records are collapsed; conflicting ones are an evidence error.
            if existing != record:
                raise EvidenceError(
                    f"conflicting OFX evidence for FITID {record.fitid!r}: "
                    f"{existing!r} vs {record!r}"
                )
        evidence_records = list(evidence_by_fitid.values())

        linked: dict[str, list[Evidence]] = defaultdict(list)
        table_returns = {
            returned.fitid: returned
            for returned in self.returns
            if returned.fitid is not None
        }
        table_recalls = {
            recall.fitid: recall
            for recall in self.recalls
            if recall.fitid is not None
        }

        for record in evidence_records:
            if record.date > result.asof:
                continue
            loan_id = record.loan_id or links.get(record.fitid)
            if loan_id is None:
                result.unmatched.append(
                    Unmatched(
                        source=record.kind,
                        fitid=record.fitid,
                        security=record.security,
                        units=record.units,
                        date=record.date,
                        direction=record.direction,
                        reason="NO_LOAN_ID",
                        detail=(
                            "no explicit loan_id or FITID correspondence; "
                            "matching on security alone is not evidence"
                        ),
                    )
                )
                continue
            loan = loans.get(loan_id)
            if loan is None:
                result.unmatched.append(
                    Unmatched(
                        source=record.kind,
                        fitid=record.fitid,
                        security=record.security,
                        units=record.units,
                        date=record.date,
                        direction=record.direction,
                        reason="UNKNOWN_LOAN",
                        detail=f"loan_id {loan_id!r} is not in the table",
                        loan_id=loan_id,
                    )
                )
                continue
            if record.security != loan.security:
                result.unmatched.append(
                    Unmatched(
                        source=record.kind,
                        fitid=record.fitid,
                        security=record.security,
                        units=record.units,
                        date=record.date,
                        direction=record.direction,
                        reason="WRONG_SECURITY",
                        detail=(
                            f"record security {record.security!r} does not match "
                            f"loan {loan_id} security {loan.security!r}"
                        ),
                        loan_id=loan_id,
                    )
                )
                continue
            if record.direction == "RETURN":
                self._check_return_evidence(
                    record, loan, table_returns, table_recalls, result
                )
            elif record.direction != _direction(loan):
                result.unmatched.append(
                    Unmatched(
                        source=record.kind,
                        fitid=record.fitid,
                        security=record.security,
                        units=record.units,
                        date=record.date,
                        direction=record.direction,
                        reason="WRONG_DIRECTION",
                        detail=(
                            f"record opens {record.direction} but loan {loan_id} "
                            f"is {_direction(loan)}; opposite directions never "
                            "offset"
                        ),
                        loan_id=loan_id,
                    )
                )
                continue
            linked[loan_id].append(record)

        for loan_id, loan in loans.items():
            loan_evidence = linked.get(loan_id, [])
            opening_evidence = [
                record for record in loan_evidence if record.direction != "RETURN"
            ]
            self._check_opening(loan, opening_evidence, result)
            opening_fitid_present = bool(
                loan.fitid and loan.fitid in evidence_by_fitid
            )
            if self.require_evidence and not (
                opening_evidence or opening_fitid_present
            ):
                result.unmatched.append(
                    Unmatched(
                        source="loan",
                        fitid=loan.fitid,
                        security=loan.security,
                        units=loan.units,
                        date=loan.date,
                        direction=_direction(loan),
                        reason="NO_EVIDENCE",
                        detail=(
                            f"loan {loan_id} has no matched OFX evidence for its "
                            "opening lend/borrow"
                            + (
                                f" (FITID {loan.fitid})"
                                if loan.fitid
                                else ""
                            )
                        ),
                        loan_id=loan_id,
                    )
                )

        if not self.require_evidence:
            return

        for returned in self.returns:
            if returned.fitid is None:
                result.unmatched.append(
                    Unmatched(
                        source="return",
                        fitid=None,
                        security=loans[returned.loan_id].security,
                        units=returned.units,
                        date=returned.date,
                        direction="RETURN",
                        reason="NO_EVIDENCE",
                        detail=(
                            f"return for loan {returned.loan_id} has no FITID, "
                            "so it cannot be tied to an OFX record"
                        ),
                        loan_id=returned.loan_id,
                    )
                )
            elif returned.fitid not in evidence_by_fitid:
                result.unmatched.append(
                    Unmatched(
                        source="return",
                        fitid=returned.fitid,
                        security=loans[returned.loan_id].security,
                        units=returned.units,
                        date=returned.date,
                        direction="RETURN",
                        reason="NO_EVIDENCE",
                        detail=(
                            f"return for loan {returned.loan_id} cites FITID "
                            f"{returned.fitid}, but no such OFX record was supplied"
                        ),
                        loan_id=returned.loan_id,
                    )
                )
            elif evidence_by_fitid[returned.fitid].direction != "RETURN":
                # The row cites an OFX record that opens a loan instead of
                # closing one, so it cannot evidence the return.
                result.unmatched.append(
                    Unmatched(
                        source="return",
                        fitid=returned.fitid,
                        security=loans[returned.loan_id].security,
                        units=returned.units,
                        date=returned.date,
                        direction="RETURN",
                        reason="NO_EVIDENCE",
                        detail=(
                            f"return for loan {returned.loan_id} cites FITID "
                            f"{returned.fitid}, but that OFX record is a "
                            f"{evidence_by_fitid[returned.fitid].direction}, "
                            "not a return"
                        ),
                        loan_id=returned.loan_id,
                    )
                )
        for recall in self.recalls:
            # A recall is an instruction, not necessarily an OFX movement, so
            # it is allowed to cite no FITID.  When it does cite one, the
            # record must exist.
            if recall.fitid is None:
                continue
            if recall.fitid not in evidence_by_fitid:
                result.unmatched.append(
                    Unmatched(
                        source="recall",
                        fitid=recall.fitid,
                        security=loans[recall.loan_id].security,
                        units=recall.units,
                        date=recall.date,
                        direction="RETURN",
                        reason="NO_EVIDENCE",
                        detail=(
                            f"recall for loan {recall.loan_id} cites FITID "
                            f"{recall.fitid}, but no such OFX record was supplied"
                        ),
                        loan_id=recall.loan_id,
                    )
                )
            elif evidence_by_fitid[recall.fitid].direction != "RETURN":
                result.unmatched.append(
                    Unmatched(
                        source="recall",
                        fitid=recall.fitid,
                        security=loans[recall.loan_id].security,
                        units=recall.units,
                        date=recall.date,
                        direction="RETURN",
                        reason="NO_EVIDENCE",
                        detail=(
                            f"recall for loan {recall.loan_id} cites FITID "
                            f"{recall.fitid}, but that OFX record is a "
                            f"{evidence_by_fitid[recall.fitid].direction}, "
                            "not a return"
                        ),
                        loan_id=recall.loan_id,
                    )
                )

    def _check_opening(
        self,
        loan: Lend | Borrow,
        opening_evidence: Sequence[Evidence],
        result: Reconciliation,
    ) -> None:
        if not opening_evidence:
            return
        total = sum((record.units for record in opening_evidence), ZERO)
        if total == loan.units:
            return
        # Only the opening row and its own evidence belong to this arithmetic;
        # later returns/actions are irrelevant to whether the loan opened at
        # the stated quantity.
        record_ids = [_loan_record(loan)]
        record_ids += [f"ofx:{record.fitid}" for record in opening_evidence]
        result.mismatches.append(
            QuantityMismatch(
                loan_id=loan.loan_id,
                security=loan.security,
                direction=_direction(loan),
                opening=loan.units,
                delta=total - loan.units,
                closing=total,
                expected=loan.units,
                records=tuple(dict.fromkeys(record_ids)),
                detail=(
                    f"loan {loan.loan_id}: OFX opening evidence totals {total} "
                    f"units but the table states {loan.units}"
                ),
                kind="EVIDENCE",
            )
        )

    def _check_return_evidence(
        self,
        record: Evidence,
        loan: Lend | Borrow,
        table_returns: Mapping[str, Return],
        table_recalls: Mapping[str, Recall],
        result: Reconciliation,
    ) -> None:
        returned = table_returns.get(record.fitid)
        recalled = table_recalls.get(record.fitid)
        if returned is None and recalled is None:
            result.unmatched.append(
                Unmatched(
                    source=record.kind,
                    fitid=record.fitid,
                    security=record.security,
                    units=record.units,
                    date=record.date,
                    direction=record.direction,
                    reason="NO_TABLE_RECORD",
                    detail=(
                        f"OFX return {record.fitid} is linked to loan "
                        f"{loan.loan_id} but no return or recall row cites it"
                    ),
                    loan_id=loan.loan_id,
                )
            )
            return
        if returned is not None:
            if returned.loan_id != loan.loan_id:
                result.unmatched.append(
                    Unmatched(
                        source=record.kind,
                        fitid=record.fitid,
                        security=record.security,
                        units=record.units,
                        date=record.date,
                        direction=record.direction,
                        reason="WRONG_LOAN",
                        detail=(
                            f"OFX return {record.fitid} was linked to loan "
                            f"{loan.loan_id}, but its table row belongs to "
                            f"{returned.loan_id}"
                        ),
                        loan_id=loan.loan_id,
                    )
                )
                return
            expected_units = returned.units
            table_record = _return_record(returned)
        else:
            assert recalled is not None
            if recalled.loan_id != loan.loan_id:
                result.unmatched.append(
                    Unmatched(
                        source=record.kind,
                        fitid=record.fitid,
                        security=record.security,
                        units=record.units,
                        date=record.date,
                        direction=record.direction,
                        reason="WRONG_LOAN",
                        detail=(
                            f"OFX recall {record.fitid} was linked to loan "
                            f"{loan.loan_id}, but its table row belongs to "
                            f"{recalled.loan_id}"
                        ),
                        loan_id=loan.loan_id,
                    )
                )
                return
            if recalled.units is None:
                return
            expected_units = recalled.units
            table_record = _recall_record(recalled)
        if record.units == expected_units:
            return
        result.mismatches.append(
            QuantityMismatch(
                loan_id=loan.loan_id,
                security=loan.security,
                direction=_direction(loan),
                opening=expected_units,
                delta=record.units - expected_units,
                closing=record.units,
                expected=expected_units,
                records=(_loan_record(loan), table_record, f"ofx:{record.fitid}"),
                detail=(
                    f"loan {loan.loan_id}: OFX return {record.fitid} is "
                    f"{record.units} units but the table row says {expected_units}"
                ),
                kind="EVIDENCE",
            )
        )

    @staticmethod
    def _sort(result: Reconciliation) -> None:
        result.open_batches.sort(
            key=lambda batch: (
                batch.direction,
                batch.security,
                batch.loan_id,
                batch.opened,
                batch.batch,
            )
        )
        result.settled_batches.sort(
            key=lambda batch: (
                batch.closed,
                batch.loan_id,
                batch.batch,
            )
        )
        result.unmatched.sort(
            key=lambda item: (
                item.reason,
                item.loan_id or "",
                item.fitid or "",
                item.date or datetime.date.min,
            )
        )
        result.mismatches.sort(
            key=lambda item: (
                item.loan_id,
                item.kind,
                item.detail,
            )
        )


def result_batches(
    result: Reconciliation,
) -> list[OpenBatch | SettledBatch]:
    """All batches of a reconciliation, settled first then open."""
    return [*result.settled_batches, *result.open_batches]


def _direction(loan: Lend | Borrow) -> str:
    return "LEND" if isinstance(loan, Lend) else "BORROW"


def _loan_record(loan: Lend | Borrow) -> str:
    return f"loan:{loan.loan_id}@{loan.date.isoformat()}"


def _mismatch_records(
    loan: Lend | Borrow, seen: Sequence[str], settlement: str
) -> tuple[str, ...]:
    """Original records for a failed arithmetic, without duplicates."""
    records = list(seen)
    if _loan_record(loan) not in records:
        records.insert(0, _loan_record(loan))
    if settlement not in records:
        records.append(settlement)
    return tuple(dict.fromkeys(records))


def _settlement_record(event: Return | Recall) -> str:
    source = "return" if isinstance(event, Return) else "recall"
    if event.fitid:
        return f"{source}:{event.fitid}"
    return f"{source}:{event.loan_id}@{event.date.isoformat()}"


def _return_record(returned: Return) -> str:
    return _settlement_record(returned)


def _recall_record(recall: Recall) -> str:
    return _settlement_record(recall)


def reconcile(
    loans: Sequence[Lend | Borrow],
    returns: Sequence[Return] = (),
    recalls: Sequence[Recall] = (),
    actions: Sequence[Split | CorporateAction] = (),
    evidence: Sequence[Evidence] = (),
    *,
    correspondence: Mapping[str, str] | None = None,
    asof: datetime.date | None = None,
    expected: Mapping[str, Decimal | None] | None = None,
    require_evidence: bool = True,
) -> Reconciliation:
    """Convenience wrapper around :class:`Reconciler`."""
    return Reconciler(
        loans,
        returns,
        recalls,
        actions,
        evidence,
        correspondence=correspondence,
        asof=asof,
        expected=expected,
        require_evidence=require_evidence,
    ).run()


def security_key(secid: Any) -> str:
    """Stable ``UNIQUEIDTYPE:UNIQUEID`` key for an OFX ``SECID``.

    Use the same form in ``Lend.security`` / ``Borrow.security`` so that OFX
    evidence and table rows can be compared without guessing.
    """
    uniqueid = secid.uniqueid
    uniqueidtype = getattr(secid, "uniqueidtype", None)
    if uniqueidtype:
        return f"{uniqueidtype}:{uniqueid}"
    return str(uniqueid)


def evidence_from_transaction(
    tx: Any, loan_for_fitid: Mapping[str, str] | None = None
) -> Evidence | None:
    """Translate a parsed OFX investment transaction into evidence.

    ``JRNLSEC`` maps to LEND for negative units and RETURN for positive units;
    ``TRANSFER`` maps to LEND for ``OUT`` and RETURN for ``IN``;
    ``SELLSTOCK`` is exposed as LEND.  Split records are table-side corporate
    actions, not evidence for a single loan, so ``None`` is returned.

    The returned record is still undetermined unless ``loan_for_fitid`` (or the
    transaction itself) supplies an explicit ``loan_id``.
    """
    from ofxtools.models.invest.transactions import (
        JRNLSEC,
        SELLSTOCK,
        SPLIT,
        TRANSFER,
    )

    mapping = loan_for_fitid or {}
    if isinstance(tx, JRNLSEC):
        units = tx.units
        return Evidence(
            fitid=tx.fitid,
            security=security_key(tx.secid),
            units=abs(units),
            date=tx.dttrade,
            direction="LEND" if units < ZERO else "RETURN",
            loan_id=mapping.get(tx.fitid),
            kind="JRNLSEC",
            account=None,
            note=tx.memo,
        )
    if isinstance(tx, TRANSFER):
        return Evidence(
            fitid=tx.fitid,
            security=security_key(tx.secid),
            units=abs(tx.units),
            date=tx.dttrade,
            direction="LEND" if tx.tferaction == "OUT" else "RETURN",
            loan_id=mapping.get(tx.fitid),
            kind="TRANSFER",
            account=None,
            note=tx.memo,
        )
    if isinstance(tx, SELLSTOCK):
        return Evidence(
            fitid=tx.fitid,
            security=security_key(tx.secid),
            units=abs(tx.units),
            date=tx.dttrade,
            direction="LEND",
            loan_id=mapping.get(tx.fitid),
            kind="SELLSTOCK",
            account=None,
            note=tx.memo,
        )
    if isinstance(tx, SPLIT):
        return None
    return None


def evidence_from_statement(
    statement: Any, loan_for_fitid: Mapping[str, str] | None = None
) -> list[Evidence]:
    """Translate evidenced transactions in a parsed ``INVSTMTRS``."""
    account = None
    if statement.invacctfrom is not None:
        account = f"{statement.invacctfrom.brokerid}/{statement.invacctfrom.acctid}"
    evidence: list[Evidence] = []
    tranlist = statement.invtranlist
    if tranlist is None:
        return evidence
    for tx in tranlist:
        record = evidence_from_transaction(tx, loan_for_fitid)
        if record is None:
            continue
        if account is not None:
            record = Evidence(
                fitid=record.fitid,
                security=record.security,
                units=record.units,
                date=record.date,
                direction=record.direction,
                loan_id=record.loan_id,
                kind=record.kind,
                account=account,
                note=record.note,
            )
        evidence.append(record)
    return evidence


def reconcile_statements(
    loans: Sequence[Lend | Borrow],
    statements: Iterable[Any],
    returns: Sequence[Return] = (),
    recalls: Sequence[Recall] = (),
    actions: Sequence[Split | CorporateAction] = (),
    *,
    correspondence: Mapping[str, str] | None = None,
    asof: datetime.date | None = None,
    expected: Mapping[str, Decimal | None] | None = None,
    require_evidence: bool = True,
) -> Reconciliation:
    """Reconcile one or more parsed ``INVSTMTRS`` statements.

    ``correspondence`` maps OFX ``FITID`` values to table ``loan_id`` values;
    records absent from it stay undetermined.
    """
    evidence: list[Evidence] = []
    for statement in statements:
        evidence.extend(evidence_from_statement(statement, correspondence))
    return reconcile(
        loans,
        returns,
        recalls,
        actions,
        evidence,
        correspondence=correspondence,
        asof=asof,
        expected=expected,
        require_evidence=require_evidence,
    )


def actions_from_statement(statement: Any) -> list[Split]:
    """Translate ``SPLIT`` records in an ``INVSTMTRS`` into table actions.

    The result is advisory: pass it to :func:`reconcile` only after checking
    that the split applies to the lending programme, since applying it changes
    the on-loan unit count.  OFX splits are reported per position, not per
    loan, and the correspondence table remains authoritative.
    """
    from ofxtools.models.invest.transactions import SPLIT as OFXSPLIT

    actions: list[Split] = []
    tranlist = statement.invtranlist
    if tranlist is None:
        return actions
    for tx in tranlist:
        if not isinstance(tx, OFXSPLIT):
            continue
        actions.append(
            Split(
                security=security_key(tx.secid),
                date=tx.dttrade,
                numerator=tx.numerator,
                denominator=tx.denominator,
                note=f"OFX SPLIT {tx.fitid}",
            )
        )
    return actions


def table_from_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a JSON-style correspondence table into ``reconcile()`` args.

    Expected shape::

        {
          "asof": "2024-12-31",
          "loans": [
            {"type": "lend", "loan_id": "L1", "security": "CUSIP:123",
             "counterparty": "CPT", "units": "1000", "date": "2024-01-01",
             "fitid": "F-OPEN"},
            {"type": "borrow", "loan_id": "B1", ...}
          ],
          "returns": [
            {"loan_id": "L1", "units": "400", "date": "2024-02-01",
             "fitid": "F-RET1"}
          ],
          "recalls": [
            {"loan_id": "L1", "units": "200", "date": "2024-03-01"}
          ],
          "actions": [
            {"type": "split", "security": "CUSIP:123", "date": "2024-05-01",
             "numerator": "2", "denominator": "1"},
            {"type": "action", "security": "CUSIP:123", "date": "2024-06-01",
             "ratio": "1.1", "action": "STOCKDIVIDEND"}
          ],
          "correspondence": {"F-OPEN": "L1", "F-RET1": "L1"},
          "expected": {"L1": "0"}
        }

    Records without an explicit type are rejected rather than guessed.
    """
    loans: list[Lend | Borrow] = []
    for row in data.get("loans", ()):
        kind = str(row.get("type", "")).lower()
        if kind == "lend":
            loans.append(
                Lend(
                    loan_id=row["loan_id"],
                    security=row["security"],
                    counterparty=row["counterparty"],
                    units=row["units"],
                    date=row["date"],
                    fitid=row.get("fitid"),
                    note=row.get("note"),
                )
            )
        elif kind == "borrow":
            loans.append(
                Borrow(
                    loan_id=row["loan_id"],
                    security=row["security"],
                    counterparty=row["counterparty"],
                    units=row["units"],
                    date=row["date"],
                    fitid=row.get("fitid"),
                    note=row.get("note"),
                )
            )
        else:
            raise TableError(
                f"loan row {row.get('loan_id')!r} has type {row.get('type')!r}; "
                "must be 'lend' or 'borrow'"
            )

    returns = [
        Return(
            loan_id=row["loan_id"],
            units=row["units"],
            date=row["date"],
            fitid=row.get("fitid"),
            note=row.get("note"),
        )
        for row in data.get("returns", ())
    ]
    recalls = [
        Recall(
            loan_id=row["loan_id"],
            units=row.get("units"),
            date=row["date"],
            fitid=row.get("fitid"),
            note=row.get("note"),
        )
        for row in data.get("recalls", ())
    ]

    actions: list[Split | CorporateAction] = []
    for row in data.get("actions", ()):
        kind = str(row.get("type", "")).lower()
        if kind == "split":
            actions.append(
                Split(
                    security=row["security"],
                    date=row["date"],
                    numerator=row["numerator"],
                    denominator=row["denominator"],
                    note=row.get("note"),
                )
            )
        elif kind in ("action", "corporate_action", "corporateaction"):
            actions.append(
                CorporateAction(
                    security=row["security"],
                    date=row["date"],
                    ratio=row["ratio"],
                    action=row.get("action", "CORPORATEACTION"),
                    note=row.get("note"),
                )
            )
        else:
            raise TableError(
                f"action row for {row.get('security')!r} has type {row.get('type')!r}; "
                "must be 'split' or 'action'"
            )

    asof = data.get("asof")
    return {
        "loans": loans,
        "returns": returns,
        "recalls": recalls,
        "actions": actions,
        "correspondence": data.get("correspondence"),
        "asof": asof,
        "expected": data.get("expected"),
        "require_evidence": data.get("require_evidence", True),
    }


def reconciliation_to_dict(result: Reconciliation) -> dict[str, Any]:
    """Serialize a :class:`Reconciliation` to JSON-compatible primitives."""
    return {
        "asof": result.asof.isoformat(),
        "balanced": result.balanced,
        "on_loan": {
            direction: {
                security: str(units) for security, units in result.on_loan(direction).items()
            }
            for direction in ("LEND", "BORROW")
        },
        "open_batches": [
            {
                "loan_id": batch.loan_id,
                "security": batch.security,
                "counterparty": batch.counterparty,
                "direction": batch.direction,
                "units": str(batch.units),
                "opened": batch.opened.isoformat(),
                "batch": batch.batch,
                "actions": list(batch.actions),
                "records": list(batch.records),
            }
            for batch in result.open_batches
        ],
        "settled_batches": [
            {
                "loan_id": batch.loan_id,
                "security": batch.security,
                "counterparty": batch.counterparty,
                "direction": batch.direction,
                "units": str(batch.units),
                "opened": batch.opened.isoformat(),
                "closed": batch.closed.isoformat(),
                "batch": batch.batch,
                "settlement": batch.settlement,
                "fitid": batch.fitid,
                "actions": list(batch.actions),
                "records": list(batch.records),
            }
            for batch in result.settled_batches
        ],
        "unmatched": [
            {
                "source": item.source,
                "fitid": item.fitid,
                "security": item.security,
                "units": None if item.units is None else str(item.units),
                "date": None if item.date is None else item.date.isoformat(),
                "direction": item.direction,
                "reason": item.reason,
                "detail": item.detail,
                "loan_id": item.loan_id,
            }
            for item in result.unmatched
        ],
        "mismatches": [
            {
                "loan_id": item.loan_id,
                "security": item.security,
                "direction": item.direction,
                "opening": str(item.opening),
                "delta": str(item.delta),
                "closing": str(item.closing),
                "expected": None if item.expected is None else str(item.expected),
                "records": list(item.records),
                "detail": item.detail,
                "kind": item.kind,
            }
            for item in result.mismatches
        ],
    }


def reconcile_file(
    ofx_source: Any,
    table: Mapping[str, Any] | str,
    *,
    require_evidence: bool = True,
) -> Reconciliation:
    """Reconcile an OFX file (or file-like object) against a JSON table.

    ``table`` may be a mapping (already parsed) or a path to a JSON file.  Any
    ``asof``/``expected``/``require_evidence`` keys in the table override the
    keyword default.
    """
    import json

    from ofxtools.Parser import OFXTree

    if isinstance(table, str):
        with open(table, encoding="utf-8") as handle:
            data: Mapping[str, Any] = json.load(handle)
    else:
        data = table
    options = table_from_dict(data)
    effective_require = options.pop("require_evidence", require_evidence)

    tree = OFXTree()
    tree.parse(ofx_source)
    ofx = tree.convert()
    statements = [stmt for stmt in ofx.statements if hasattr(stmt, "invtranlist")]
    return reconcile_statements(
        options.pop("loans"),
        statements,
        options.pop("returns"),
        options.pop("recalls"),
        options.pop("actions"),
        correspondence=options.pop("correspondence"),
        asof=options.pop("asof"),
        expected=options.pop("expected"),
        require_evidence=effective_require,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point: ``python -m ofxtools.lending OFX TABLE``.

    Prints ``reconciliation_to_dict()`` as JSON.  Exit status is 0 when the
    reconciliation is balanced, 1 when undetermined records or quantity
    mismatches remain, and 2 for malformed input.
    """
    import argparse
    import json
    import sys

    from ofxtools.header import OFXHeaderError
    from ofxtools.Parser import ParseError

    parser = argparse.ArgumentParser(
        prog="python -m ofxtools.lending",
        description=(
            "Reconcile securities on loan from an OFX investment statement "
            "and an explicit lending/return correspondence table"
        ),
    )
    parser.add_argument("ofx", help="OFX statement file")
    parser.add_argument("table", help="correspondence table (JSON)")
    parser.add_argument(
        "--no-require-evidence",
        dest="require_evidence",
        action="store_false",
        default=True,
        help="Reconcile the table alone, without demanding OFX evidence",
    )
    args = parser.parse_args(argv)

    try:
        result = reconcile_file(
            args.ofx, args.table, require_evidence=args.require_evidence
        )
    except (LendingError, OFXHeaderError, ParseError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    json.dump(reconciliation_to_dict(result), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if result.balanced else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
