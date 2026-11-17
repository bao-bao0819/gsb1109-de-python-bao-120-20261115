r"""Normalise parsed transactions without losing their source direction.

The parser keeps each ``:61:`` debit, credit or reversal mark in
``transaction.data['status']`` and signs the stored amount according to the
active :class:`mt940.options.Options`. Cleaning re-derives the canonical flow
from the mark, so ``D`` and ``RC`` always leave the account and ``C`` and
``RD`` always enter it. The amount already stored by the parser is treated as
the unsigned magnitude: a debit parsed as ``-12.50`` is not made positive and
then negative again.

Each occurrence retains its original mark, original signed amount and currency.
Net amounts are totalled per currency with exact decimal arithmetic, so no
rounding occurs even under a low decimal context. Two entries that offset each
other are only removed from the effective view when they share a business
reference. Unassociated reversal pairs remain visible in
:attr:`Cleaning.occurrences` and in :attr:`Cleaning.effective`.

Example:
    >>> import mt940
    >>> source = (
    ...     ':20:EXAMPLE\n'
    ...     ':60F:C260101EUR100,00\n'
    ...     ':61:2601020102C10,00NTRFREF-A//BANK-A\n'
    ...     ':61:2601020102RC10,00NTRFREF-A//BANK-A\n'
    ...     ':62F:C260102EUR100,00\n'
    ... )
    >>> cleaned = mt940.clean(mt940.parse(source))
    >>> [(entry.status, entry.net_amount) for entry in cleaned.occurrences]
    [('C', Decimal('10.00')), ('RC', Decimal('-10.00'))]
    >>> cleaned.net_amount('EUR')
    Decimal('0.00')
    >>> len(cleaned.effective)
    0
"""

from __future__ import annotations

import dataclasses
import decimal
import enum
import typing
from typing import TYPE_CHECKING, Any

from . import models

if TYPE_CHECKING:
    from collections.abc import Iterable


@enum.unique
class Direction(enum.IntEnum):
    """Canonical flow direction independent of the source mark."""

    #: Money entering the account: ``C`` and ``RD``.
    CREDIT = 1
    #: Money leaving the account: ``D`` and ``RC``.
    DEBIT = -1


#: Canonical flow for every supported ``:61:`` debit, credit sequence.
#: ``RC`` reverses a credit and therefore leaves the account; ``RD`` reverses
#: a debit and therefore enters it. Case is ignored during lookup.
STATUS_DIRECTIONS: dict[str, Direction] = {
    'C': Direction.CREDIT,
    'D': Direction.DEBIT,
    'RC': Direction.DEBIT,
    'RD': Direction.CREDIT,
}

#: Direction of the original entry named by each source mark. A plain ``C`` or
#: ``D`` is its own original, while ``RC`` names a credit and ``RD`` names a
#: debit. This is the direction recorded in the source, before the reversal
#: changes the canonical flow.
STATUS_ORIGINAL_DIRECTIONS: dict[str, Direction] = {
    'C': Direction.CREDIT,
    'D': Direction.DEBIT,
    'RC': Direction.CREDIT,
    'RD': Direction.DEBIT,
}

#: Transaction fields that can associate a reversal with its original entry.
#: Statement-wide fields such as ``transaction_reference`` are excluded because
#: the default parser copies the same value into every transaction of a
#: statement. ``bank_reference`` is also excluded by default: some banks repeat
#: one value across a whole statement, which would pair unrelated entries.
#: Pass ``association_keys`` to :func:`clean` for bank-specific linking fields.
DEFAULT_ASSOCIATION_KEYS: tuple[str, ...] = (
    'end_to_end_reference',
    'customer_reference',
    'additional_position_reference',
)


@dataclasses.dataclass(frozen=True, slots=True)
class CleanedEntry:
    """One source transaction with its original and canonical direction.

    Attributes:
        transaction: The parsed transaction retained by reference.
        index: Zero-based position in the cleaned input.
        status: Source mark exactly as stored, including its original case.
        direction: Canonical flow derived from ``status``.
        original_direction: Direction recorded in the mark before reversal:
            ``C`` and ``RD`` credit, ``D`` and ``RC`` debit.
        original_amount: Signed decimal exactly as the parser stored it.
        net_amount: Amount signed once from its magnitude and ``direction``.
        currency: Source currency, or ``None`` when the source supplied none.
    """

    transaction: models.Transaction
    index: int
    status: str
    direction: Direction
    original_direction: Direction
    original_amount: decimal.Decimal
    net_amount: decimal.Decimal
    currency: str | None

    @property
    def is_reversal(self) -> bool:
        """Return whether the source mark is ``RC`` or ``RD``."""
        return self.status.upper() in {'RC', 'RD'}

    @property
    def data(self) -> dict[str, Any]:
        """Return the untouched transaction data mapping."""
        return self.transaction.data


@dataclasses.dataclass(frozen=True, slots=True)
class ReversalPair:
    """Two equal amounts with opposite canonical directions.

    Attributes:
        first: Entry that appeared first in the source.
        second: Entry that appeared second in the source.
        associated: Whether a shared business reference links the entries.
        association_key: Shared field name for an associated pair, otherwise
            ``None``.
        association_value: Shared non-empty field value for an associated
            pair, otherwise ``None``.
    """

    first: CleanedEntry
    second: CleanedEntry
    associated: bool
    association_key: str | None = None
    association_value: str | None = None


class Cleaning:
    """Canonical directions, exact net amounts and retained occurrences.

    Attributes:
        occurrences: Every cleaned entry in source order. Entries in an
            associated reversal pair remain present here.
        effective: Occurrences excluding both sides of associated reversal
            pairs. Unassociated reversal pairs remain in this view.
        reversals: Detected reversal pairs in source order, whether or not
            they share a business reference.
        net_amounts: Exact totals keyed by currency. ``None`` is a separate
            key for entries without a currency.
    """

    def __init__(
        self,
        occurrences: Iterable[CleanedEntry],
        reversals: Iterable[ReversalPair] = (),
    ) -> None:
        """Build the net totals and effective occurrence view.

        Args:
            occurrences: Cleaned entries in source order.
            reversals: Detected reversal pairs. Only associated pairs remove
                their entries from :attr:`effective`.
        """
        self.occurrences: tuple[CleanedEntry, ...] = tuple(occurrences)
        self.reversals: tuple[ReversalPair, ...] = tuple(reversals)

        amounts: dict[str | None, list[decimal.Decimal]] = {}
        for entry in self.occurrences:
            amounts.setdefault(entry.currency, []).append(entry.net_amount)
        self.net_amounts: dict[str | None, decimal.Decimal] = {
            currency: _exact_sum(values)
            for currency, values in amounts.items()
        }

        offset = {
            entry.index
            for pair in self.reversals
            if pair.associated
            for entry in (pair.first, pair.second)
        }
        self.effective: tuple[CleanedEntry, ...] = tuple(
            entry for entry in self.occurrences if entry.index not in offset
        )

    @property
    def associated_reversals(self) -> tuple[ReversalPair, ...]:
        """Return reversal pairs that share a business reference."""
        return tuple(pair for pair in self.reversals if pair.associated)

    @property
    def unassociated_reversals(self) -> tuple[ReversalPair, ...]:
        """Return offsetting pairs that lack a business reference."""
        return tuple(pair for pair in self.reversals if not pair.associated)

    def net_amount(self, currency: str | None = None) -> decimal.Decimal:
        """Return the exact net for one currency.

        Args:
            currency: Currency string, or ``None`` for entries without one.
                An unknown currency gives ``Decimal(0)``.

        Returns:
            The exact sum of canonical amounts in that currency.
        """
        return self.net_amounts.get(currency, decimal.Decimal(0))

    def occurrences_for(
        self,
        currency: str | None = None,
    ) -> tuple[CleanedEntry, ...]:
        """Return every retained occurrence for one currency.

        Args:
            currency: Currency string, or ``None`` for entries without one.

        Returns:
            Entries in source order whose currency equals ``currency``.
        """
        return tuple(
            entry for entry in self.occurrences if entry.currency == currency
        )


def _direction(status: str) -> Direction:
    """Return the canonical flow direction for a source mark.

    Args:
        status: Source debit, credit or reversal mark. Case is ignored.

    Returns:
        The canonical direction for ``C``, ``D``, ``RC`` or ``RD``.

    Raises:
        ValueError: The mark is not one of the four supported values.
    """
    try:
        return STATUS_DIRECTIONS[status.upper()]
    except KeyError:
        expected = ', '.join(STATUS_DIRECTIONS)
        msg = f'Unknown debit/credit mark {status!r}; expected {expected}'
        raise ValueError(msg) from None


def _original_direction(status: str) -> Direction:
    """Return the direction recorded by a source mark before reversal.

    Args:
        status: Source debit, credit or reversal mark. Case is ignored.

    Returns:
        ``CREDIT`` for ``C`` and ``RC``, ``DEBIT`` for ``D`` and ``RD``.
        :func:`_direction` has already rejected unknown marks.
    """
    return STATUS_ORIGINAL_DIRECTIONS[status.upper()]


def _canonical_amount(
    amount: decimal.Decimal,
    direction: Direction,
) -> decimal.Decimal:
    """Sign an amount once from its magnitude and canonical direction.

    Args:
        amount: Signed decimal stored by the parser. Its magnitude is treated
            as the source amount, so an already-negative debit is not negated
            a second time.
        direction: Canonical flow for the entry's source mark.

    Returns:
        ``abs(amount)`` for a credit, or its negation for a debit. The input
        exponent and digits are preserved.

    Raises:
        ValueError: The amount is not a finite decimal.
    """
    if not amount.is_finite():
        msg = f'Cannot clean non-finite amount {amount!r}'
        raise ValueError(msg)
    magnitude = amount.copy_abs()
    if direction is Direction.DEBIT:
        return magnitude.copy_negate()
    return magnitude


def _exact_sum(values: Iterable[decimal.Decimal]) -> decimal.Decimal:
    """Sum decimals exactly, without using the active decimal context.

    Args:
        values: Finite decimals to add.

    Returns:
        A decimal whose exponent is the smallest exponent among ``values``,
        so no digit is rounded away. An empty input gives ``Decimal(0)``.

    Raises:
        ValueError: A value is not finite.
    """
    total = 0
    exponent: int | None = None
    for value in values:
        if not value.is_finite():
            msg = f'Cannot total non-finite amount {value!r}'
            raise ValueError(msg)
        sign, digits, raw_exponent = value.as_tuple()
        value_exponent = typing.cast('int', raw_exponent)
        coefficient = int(''.join(str(digit) for digit in digits))
        if sign:
            coefficient = -coefficient
        if exponent is None:
            exponent = value_exponent
        elif value_exponent < exponent:
            total *= 10 ** (exponent - value_exponent)
            exponent = value_exponent
        elif value_exponent > exponent:
            coefficient *= 10 ** (value_exponent - exponent)
        total += coefficient

    if exponent is None:
        return decimal.Decimal(0)
    total_digits = tuple(int(digit) for digit in str(abs(total)))
    return decimal.Decimal((int(total < 0), total_digits, exponent))


def _association_values(
    entry: CleanedEntry,
    keys: Iterable[str],
) -> dict[str, str]:
    """Collect non-empty business references from an entry.

    Args:
        entry: Cleaned entry whose transaction data is inspected.
        keys: Candidate field names.

    Returns:
        Field names mapped to their non-empty string values. Missing, empty
        and non-string values are ignored.
    """
    values: dict[str, str] = {}
    for key in keys:
        value: object = entry.transaction.data.get(key)
        if isinstance(value, str) and value:
            values[key] = value
    return values


def _shared_association(
    first: dict[str, str],
    second: dict[str, str],
) -> tuple[str, str] | None:
    """Return the first field and value shared by two entries.

    Args:
        first: References collected from the first entry.
        second: References collected from the second entry.

    Returns:
        The first key in ``first`` whose value also appears under the same key
        in ``second``, or ``None`` when no reference matches.
    """
    for key, value in first.items():
        if second.get(key) == value:
            return key, value
    return None


def _is_reversal(first: CleanedEntry, second: CleanedEntry) -> bool:
    """Return whether two entries form a credit/debit reversal pair.

    Args:
        first: One entry from the same currency and magnitude group.
        second: Another entry from that group.

    Returns:
        ``True`` for an ordinary ``C`` paired with ``RC``, or an ordinary
        ``D`` paired with ``RD``. Two ordinary marks and two reversal marks do
        not form a pair.
    """
    marks = frozenset({first.status.upper(), second.status.upper()})
    return marks in {
        frozenset({'C', 'RC'}),
        frozenset({'D', 'RD'}),
    }


def _reversal_pair(
    first: CleanedEntry,
    second: CleanedEntry,
    *,
    associated: bool,
    association: tuple[str, str] | None = None,
) -> ReversalPair:
    """Build a pair with its entries in source order.

    Args:
        first: One member of the pair.
        second: The other member.
        associated: Whether a shared business reference links the entries.
        association: Shared field name and value for an associated pair.

    Returns:
        A pair whose ``first`` entry appeared no later than ``second``.
    """
    early, late = sorted((first, second), key=lambda entry: entry.index)
    key, value = association if association is not None else (None, None)
    return ReversalPair(
        early,
        late,
        associated=associated,
        association_key=key,
        association_value=value,
    )


def _magnitude(
    entry: CleanedEntry,
) -> tuple[str | None, decimal.Decimal]:
    """Return the grouping key for an entry: currency and absolute amount.

    Args:
        entry: Cleaned entry to group.

    Returns:
        The source currency and the unsigned decimal magnitude.
    """
    return entry.currency, entry.net_amount.copy_abs()


def _match_associated(
    debit: int,
    credit_indexes: list[int],
    entries: tuple[CleanedEntry, ...],
    links: list[dict[str, str]],
) -> tuple[int, tuple[str, str]] | None:
    """Find a reversal candidate that shares a business reference.

    Args:
        debit: Index of the debit-side entry being paired.
        credit_indexes: Remaining credit-side candidates in source order.
        entries: All cleaned entries.
        links: Business references collected for every entry.

    Returns:
        The matched credit index and its shared field/value, or ``None``.
    """
    for credit in credit_indexes:
        if not _is_reversal(entries[debit], entries[credit]):
            continue
        association = _shared_association(links[debit], links[credit])
        if association is not None:
            return credit, association
    return None


def _match_unassociated(
    debit: int,
    credit_indexes: list[int],
    entries: tuple[CleanedEntry, ...],
) -> int | None:
    """Find the first remaining compatible reversal candidate.

    Args:
        debit: Index of the debit-side entry being paired.
        credit_indexes: Remaining credit-side candidates in source order.
        entries: All cleaned entries.

    Returns:
        The matched credit index, or ``None`` when none is compatible.
    """
    return next(
        (
            credit
            for credit in credit_indexes
            if _is_reversal(entries[debit], entries[credit])
        ),
        None,
    )


def _pair_reversals(
    entries: tuple[CleanedEntry, ...],
    association_keys: tuple[str, ...],
) -> tuple[ReversalPair, ...]:
    """Pair offsetting entries, preferring a shared business reference.

    Args:
        entries: Cleaned entries in source order.
        association_keys: Fields that can link a reversal to its original.

    Returns:
        Pairs ordered by their first occurrence. Associated pairs are matched
        first; remaining compatible entries form unassociated pairs. Pairing
        is greedy in source order and leaves surplus entries unpaired.
    """
    links = [_association_values(entry, association_keys) for entry in entries]
    groups: dict[tuple[str | None, decimal.Decimal], list[int]] = {}
    for index, entry in enumerate(entries):
        groups.setdefault(_magnitude(entry), []).append(index)

    pairs: list[ReversalPair] = []
    for indexes in groups.values():
        debits = [
            index
            for index in indexes
            if entries[index].direction is Direction.DEBIT
        ]
        candidates = [
            index
            for index in indexes
            if entries[index].direction is Direction.CREDIT
        ]
        unmatched: list[int] = []

        for debit in debits:
            match = _match_associated(debit, candidates, entries, links)
            if match is None:
                unmatched.append(debit)
                continue
            credit, association = match
            candidates.remove(credit)
            pairs.append(
                _reversal_pair(
                    entries[debit],
                    entries[credit],
                    associated=True,
                    association=association,
                ),
            )

        for debit in unmatched:
            unassociated = _match_unassociated(debit, candidates, entries)
            if unassociated is None:
                continue
            candidates.remove(unassociated)
            pairs.append(
                _reversal_pair(
                    entries[debit],
                    entries[unassociated],
                    associated=False,
                ),
            )

    pairs.sort(key=lambda pair: min(pair.first.index, pair.second.index))
    return tuple(pairs)


def _association_keys(
    keys: Iterable[object] | None,
) -> tuple[str, ...]:
    """Validate and materialise the association field names.

    Args:
        keys: Supplied field names, or ``None`` for the defaults.

    Returns:
        The default names for ``None``, otherwise the supplied names as a
        tuple in their given order.

    Raises:
        TypeError: A supplied name is not a string.
    """
    if keys is None:
        return DEFAULT_ASSOCIATION_KEYS
    materialised: tuple[object, ...] = tuple(keys)
    for key in materialised:
        if not isinstance(key, str):
            msg = f'Association key {key!r} is not a string'
            raise TypeError(msg)
    return typing.cast('tuple[str, ...]', materialised)


def _cleaned_entries(
    transactions: Iterable[object],
) -> tuple[CleanedEntry, ...]:
    """Validate transactions and derive their canonical entries.

    Args:
        transactions: Candidate objects. Each must be a
            :class:`mt940.models.Transaction` with a finite
            :class:`mt940.models.Amount` and a source mark.

    Returns:
        Entries in input order with their original and canonical fields.

    Raises:
        TypeError: An item is not a transaction, or its amount is missing,
            not an :class:`mt940.models.Amount`, or not a
            :class:`decimal.Decimal`.
        ValueError: A transaction has no mark, or a mark is not ``C``, ``D``,
            ``RC`` or ``RD``.
    """
    entries: list[CleanedEntry] = []
    for index, transaction in enumerate(transactions):
        if not isinstance(transaction, models.Transaction):
            msg = f'Item {index} is not a Transaction'
            raise TypeError(msg)
        data = transaction.data
        amount = data.get('amount')
        if not isinstance(amount, models.Amount):
            msg = f'Transaction {index} has no Amount under "amount"'
            raise TypeError(msg)
        value: object = amount.amount
        if not isinstance(value, decimal.Decimal):
            msg = f'Transaction {index} amount is not a Decimal'
            raise TypeError(msg)
        status = data.get('status')
        if not isinstance(status, str) or not status:
            msg = f'Transaction {index} has no debit/credit mark'
            raise ValueError(msg)
        direction = _direction(status)
        entries.append(
            CleanedEntry(
                transaction=transaction,
                index=index,
                status=status,
                direction=direction,
                original_direction=_original_direction(status),
                original_amount=value,
                net_amount=_canonical_amount(value, direction),
                currency=amount.currency,
            ),
        )
    return tuple(entries)


def clean(
    transactions: Iterable[models.Transaction],
    *,
    association_keys: Iterable[str] | None = None,
) -> Cleaning:
    """Clean parsed transactions without losing original directions.

    The source mark is retained exactly while the canonical amount is signed
    once from its magnitude. Amounts are grouped by their own currency; no
    conversion or cross-currency total is performed. A reversal is offset in
    :attr:`Cleaning.effective` only when it shares a non-empty value under one
    of the association fields. Offsetting entries without such a reference
    remain in both ``occurrences`` and ``effective``.

    Args:
        transactions: Parsed transactions carrying a
            :class:`mt940.models.Amount` under ``amount`` and a source mark
            under ``status``.
        association_keys: Fields that link a reversal to its original entry.
            ``None`` uses :data:`DEFAULT_ASSOCIATION_KEYS`; an empty iterable
            disables association so every reversal pair is unassociated.

    Returns:
        A :class:`Cleaning` with occurrences, exact per-currency nets and
        detected reversal pairs.

    Raises:
        TypeError: An item is not a transaction, an amount is missing or not
            an :class:`mt940.models.Amount`, an amount value is not a
            :class:`decimal.Decimal`, or an association key is not a string.
        ValueError: A transaction has no mark, an amount is not finite, or a
            mark is not ``C``, ``D``, ``RC`` or ``RD``.
    """
    keys = _association_keys(association_keys)
    occurrences = _cleaned_entries(transactions)
    return Cleaning(occurrences, _pair_reversals(occurrences, keys))
