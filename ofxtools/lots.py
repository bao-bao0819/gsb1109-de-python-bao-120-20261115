"""
Cost-basis lot migration driven by broker corporate-action instructions.

A "lot" is a tax lot of an OFX investment position: the units held together
with their original cost basis.  Brokers document reorganizations (mergers,
splits, spin-offs, redemptions, ...) in action sheets that say, for each
old security:

  * how many units of each new security one old unit converts into;
  * the cash received in lieu of a fractional ("odd-lot") remainder; and
  * how the original cost basis is to be apportioned among the proceeds.

This module consumes those instructions and migrates a collection of old
:class:`Lot` instances into new lots:

  * every new lot carries a back-reference (``source_lot_id``) to the lot
    whose cost basis it inherited, plus the ordered ``provenance`` of
    action IDs through which that basis flowed;
  * cost basis is allocated strictly according to the broker's allocation
    fractions - the same basis can never be allocated twice;
  * accounts lacking action evidence are passed through untouched and
    reported separately as *unevidenced*;
  * multiple actions sharing a predecessor are applied in explicit order,
    each consuming only the basis the preceding action left behind.
"""

from __future__ import annotations

__all__ = [
    "Lot",
    "LotAction",
    "SecurityProceeds",
    "CashProceeds",
    "MigrationResult",
    "LotMigrator",
    "migrate_lots",
]


# stdlib imports
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

logger = logging.getLogger(__name__)

SecID: TypeAlias = str
LotID: TypeAlias = str
AccountID: TypeAlias = str

_ZERO = Decimal(0)
_ONE = Decimal(1)
# Rounding grid for units and money; fine enough to preserve broker precision
# while keeping rounding residuals deterministic.
DEFAULT_QUANTUM = Decimal("0.00000001")
# Broker worksheets routinely state allocation fractions rounded to a few
# decimals (e.g. three times 1/3); accept anything summing to 1 on that grid.
FRACTION_TOLERANCE = DEFAULT_QUANTUM


@dataclass(frozen=True, slots=True)
class Lot:
    """
    A single cost-basis lot (``units`` of ``secid`` carrying ``cost`` basis).

    ``account`` scopes corporate-action evidence; lots whose account has no
    applicable action are reported as unevidenced rather than migrated.
    ``source_lot_id``/``provenance`` are populated on lots produced by a
    migration, so every new cost can be traced back to an original lot.
    """

    lot_id: LotID
    secid: SecID
    units: Decimal
    cost: Decimal
    account: AccountID | None = None
    memo: str | None = None
    source_lot_id: LotID | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.lot_id:
            raise ValueError("Lot.lot_id must be a non-empty string")
        if not self.secid:
            raise ValueError("Lot.secid must be a non-empty string")
        if self.units <= 0:
            raise ValueError(f"Lot {self.lot_id!r} units must be positive")
        if self.cost < 0:
            raise ValueError(f"Lot {self.lot_id!r} cost must be non-negative")


@dataclass(frozen=True, slots=True)
class SecurityProceeds:
    """
    New securities received from a corporate action.

    ``ratio`` is the conversion ratio (new units per old unit);
    ``cost_fraction`` is the share of the predecessor's cost basis allocated
    to this security.
    """

    secid: SecID
    ratio: Decimal
    cost_fraction: Decimal
    memo: str | None = None

    def __post_init__(self) -> None:
        if not self.secid:
            raise ValueError("SecurityProceeds.secid must be a non-empty string")
        if self.ratio <= 0:
            raise ValueError(
                f"SecurityProceeds {self.secid!r} ratio must be positive"
            )
        if not _ZERO <= self.cost_fraction <= _ONE:
            raise ValueError(
                f"SecurityProceeds {self.secid!r} cost_fraction must lie in [0, 1]"
            )


@dataclass(frozen=True, slots=True)
class CashProceeds:
    """
    Cash received in lieu of fractional units (or a full cash-out).

    ``amount`` is the broker-stated cash.  When ``per_unit`` is False (the
    default) it is the flat cash attached to one application of the action;
    when True it is a per-unit rate, so the cash due on a lot scales with
    that lot's units (the same action rule can then span many lots).
    ``cost_fraction`` is the share of the predecessor's cost basis disposed
    of against that cash (it is not carried into any new lot).
    """

    amount: Decimal
    cost_fraction: Decimal
    per_unit: bool = False
    memo: str | None = None

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("CashProceeds.amount must be non-negative")
        if not _ZERO <= self.cost_fraction <= _ONE:
            raise ValueError("CashProceeds.cost_fraction must lie in [0, 1]")


@dataclass(frozen=True, slots=True)
class LotAction:
    """
    One broker-documented corporate action against predecessor ``secid``.

    ``order`` gives the explicit application sequence for actions sharing a
    predecessor (lower runs first).  ``securities``/``cash`` describe the
    proceeds; their ``cost_fraction`` values must sum to 1.  ``accounts``
    optionally restricts the action to named accounts (``None`` = all
    accounts); lots outside scope are treated as lacking evidence.
    """

    action_id: str
    secid: SecID
    order: int
    securities: Sequence[SecurityProceeds] = ()
    cash: CashProceeds | None = None
    accounts: frozenset[AccountID] | None = None
    memo: str | None = None

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("LotAction.action_id must be a non-empty string")
        if not self.secid:
            raise ValueError("LotAction.secid must be a non-empty string")
        if not self.securities and self.cash is None:
            raise ValueError(
                f"Action {self.action_id!r} has neither security nor cash proceeds"
            )
        secids = [proceed.secid for proceed in self.securities]
        if len(secids) != len(set(secids)):
            raise ValueError(
                f"Action {self.action_id!r} lists duplicate security proceeds"
            )
        fractions = [proceed.cost_fraction for proceed in self.securities]
        if self.cash is not None:
            fractions.append(self.cash.cost_fraction)
        total = sum(fractions, _ZERO)
        if abs(total - _ONE) > FRACTION_TOLERANCE:
            raise ValueError(
                f"Action {self.action_id!r} cost_fraction values must sum to 1, "
                f"not {total}"
            )


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Outcome of a :class:`LotMigrator` run."""

    new_lots: tuple[Lot, ...]
    unevidenced_lots: tuple[Lot, ...]
    cash: dict[LotID, Decimal]
    actions_applied: tuple[LotAction, ...]


@dataclass(slots=True)
class _Piece:
    """Mutable working fragment of a lot flowing through successive actions."""

    secid: SecID
    units: Decimal
    cost: Decimal
    source_lot_id: LotID
    provenance: list[str]


class LotMigrator:
    """
    Migrate cost-basis lots according to broker corporate-action evidence.

    Actions are validated up front and indexed by predecessor security.  At
    migration time each lot walks the ordered action chain applicable to its
    account; an action only ever consumes basis still held in its predecessor
    security, so basis can never be apportioned twice.  Lots without any
    applicable action are returned unchanged in ``unevidenced_lots``.
    """

    def __init__(
        self,
        actions: Iterable[LotAction],
        quantum: Decimal = DEFAULT_QUANTUM,
    ) -> None:
        action_list = tuple(actions)
        self._validate(action_list)
        self._quantum = quantum
        self._index = self._build_index(action_list)

    @staticmethod
    def _validate(actions: Sequence[LotAction]) -> None:
        seen_ids: set[str] = set()
        order_keys: set[tuple[SecID, int]] = set()
        for action in actions:
            if action.action_id in seen_ids:
                raise ValueError(f"Duplicate LotAction.action_id {action.action_id!r}")
            seen_ids.add(action.action_id)
            key = (action.secid, action.order)
            if key in order_keys:
                raise ValueError(
                    f"Actions on {action.secid!r} must have distinct orders; "
                    f"order {action.order} is repeated"
                )
            order_keys.add(key)

    @staticmethod
    def _build_index(
        actions: Sequence[LotAction],
    ) -> dict[SecID, list[LotAction]]:
        index: dict[SecID, list[LotAction]] = {}
        for action in actions:
            index.setdefault(action.secid, []).append(action)
        for chain in index.values():
            chain.sort(key=lambda action: (action.order, action.action_id))
        return index

    def _applies(self, action: LotAction, account: AccountID | None) -> bool:
        if action.accounts is None:
            return True
        return account is not None and account in action.accounts

    def _quantize(self, value: Decimal) -> Decimal:
        return value.quantize(self._quantum)

    def migrate(self, lots: Iterable[Lot]) -> MigrationResult:
        """Return the new lots, unevidenced lots, and cash for ``lots``."""
        new_lots: list[Lot] = []
        unevidenced: list[Lot] = []
        cash: dict[LotID, Decimal] = {}
        applied: list[LotAction] = []

        for lot in lots:
            applicable = [
                action
                for action in self._index.get(lot.secid, [])
                if self._applies(action, lot.account)
            ]
            if not applicable:
                logger.info(
                    "No corporate-action evidence for lot %s (secid=%s account=%s); "
                    "carrying it forward unevidenced",
                    lot.lot_id,
                    lot.secid,
                    lot.account,
                )
                unevidenced.append(lot)
                continue

            pieces = [
                _Piece(
                    secid=lot.secid,
                    units=lot.units,
                    cost=lot.cost,
                    source_lot_id=lot.lot_id,
                    provenance=[],
                )
            ]
            lot_cash = _ZERO
            applied_ids: set[str] = set()

            # Walk the action graph: at every step apply the lowest-order
            # action whose predecessor security still holds basis from this
            # lot.  Actions on securities produced by earlier steps become
            # reachable automatically; already-applied actions never fire
            # again, so basis can't be apportioned twice.
            while True:
                candidates = [
                    action
                    for secid in {piece.secid for piece in pieces}
                    for action in self._index.get(secid, [])
                    if self._applies(action, lot.account)
                    and action.action_id not in applied_ids
                ]
                if not candidates:
                    break
                action = min(
                    candidates, key=lambda item: (item.order, item.action_id)
                )
                incoming = [
                    piece for piece in pieces if piece.secid == action.secid
                ]
                outgoing, action_cash = self._apply_action(action, incoming)
                pieces = [
                    piece for piece in pieces if piece.secid != action.secid
                ] + outgoing
                if action_cash:
                    lot_cash += action_cash
                applied_ids.add(action.action_id)
                if action not in applied:
                    applied.append(action)

            if lot_cash:
                cash[lot.lot_id] = self._quantize(lot_cash)
            new_lots.extend(self._materialize(lot, pieces))

        return MigrationResult(
            new_lots=tuple(new_lots),
            unevidenced_lots=tuple(unevidenced),
            cash=cash,
            actions_applied=tuple(applied),
        )

    def _apply_action(
        self, action: LotAction, incoming: Sequence[_Piece]
    ) -> tuple[list[_Piece], Decimal]:
        outgoing: list[_Piece] = []
        total_units = sum((piece.units for piece in incoming), _ZERO)
        cash_allocations = self._allocate_cash(action, incoming, total_units)

        for piece in incoming:
            # Split this piece's basis across every proceed, quantizing each
            # allocation.  The rounding residual goes to the bucket carrying
            # the most basis so the piece's basis is fully accounted for
            # without ever creating basis in a zero-fraction proceed.
            allocated = _ZERO
            piece_outgoing: list[_Piece] = []
            for proceed in action.securities:
                cost = self._quantize(piece.cost * proceed.cost_fraction)
                allocated += cost
                piece_outgoing.append(
                    _Piece(
                        secid=proceed.secid,
                        units=self._quantize(piece.units * proceed.ratio),
                        cost=cost,
                        source_lot_id=piece.source_lot_id,
                        provenance=piece.provenance + [action.action_id],
                    )
                )

            cash_cost = (
                self._quantize(piece.cost * action.cash.cost_fraction)
                if action.cash is not None
                else _ZERO
            )
            allocated += cash_cost
            residual = piece.cost - allocated
            if residual:
                target = (
                    max(piece_outgoing, key=lambda item: item.cost)
                    if piece_outgoing
                    else None
                )
                if target is not None and (
                    target.cost > 0 or action.cash is None
                ):
                    target.cost += residual
                elif action.cash is not None:
                    # Cash-only action, or every security fraction is zero:
                    # the residual settles into the cash disposition.
                    cash_cost += residual
            outgoing.extend(piece_outgoing)

        return outgoing, sum(cash_allocations, _ZERO)

    def _allocate_cash(
        self,
        action: LotAction,
        incoming: Sequence[_Piece],
        total_units: Decimal,
    ) -> list[Decimal]:
        if action.cash is None or action.cash.amount == 0:
            return [_ZERO for _ in incoming]
        amount = action.cash.amount
        if action.cash.per_unit:
            # Per-unit rate: each lot's cash scales with its own units, so a
            # single action rule can cover lots of different sizes.
            return [self._quantize(amount * piece.units) for piece in incoming]
        # Flat cash: apportion by units across the lots this action touches.
        allocations = [
            self._quantize(amount * (piece.units / total_units)) for piece in incoming
        ]
        # Hand the rounding residual to the largest piece so the broker-stated
        # cash total is preserved exactly.
        residual = amount - sum(allocations, _ZERO)
        if residual:
            largest = max(range(len(incoming)), key=lambda idx: incoming[idx].units)
            allocations[largest] += residual
        return allocations

    def _materialize(
        self, lot: Lot, pieces: Sequence[_Piece]
    ) -> list[Lot]:
        # Merge terminal pieces of the same security so each new security
        # becomes one lot per original lot, while retaining the back-reference.
        merged: dict[SecID, _Piece] = {}
        for piece in pieces:
            if piece.units <= 0:
                continue
            existing = merged.get(piece.secid)
            if existing is None:
                merged[piece.secid] = piece
            else:
                existing.units += piece.units
                existing.cost += piece.cost
                existing.provenance = sorted(
                    set(existing.provenance) | set(piece.provenance)
                )

        return [
            Lot(
                lot_id=f"{lot.lot_id}->{piece.secid}",
                secid=piece.secid,
                units=self._quantize(piece.units),
                cost=self._quantize(piece.cost),
                account=lot.account,
                source_lot_id=piece.source_lot_id,
                provenance=tuple(piece.provenance),
            )
            for piece in merged.values()
        ]


def migrate_lots(
    lots: Iterable[Lot],
    actions: Iterable[LotAction],
    *,
    quantum: Decimal = DEFAULT_QUANTUM,
) -> MigrationResult:
    """
    Convenience wrapper: build a :class:`LotMigrator` from ``actions`` and
    migrate ``lots`` in one call.
    """
    return LotMigrator(actions, quantum=quantum).migrate(lots)
