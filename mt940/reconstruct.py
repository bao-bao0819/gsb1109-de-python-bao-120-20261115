r"""Reassemble paginated MT940 statements into complete statement periods.

Banks split a long statement into pages that share one ``:28C:`` statement
number and carry consecutive ``/`` sequence numbers. Two pages only chain
when the closing balance of the earlier page equals the opening balance of
the later page under the same statement number. :func:`reconstruct` groups
parsed pages by account and statement number, orders them by sequence number
and reports missing pages, duplicate pages and balance breaks separately.
Pages from another statement of the same account are never borrowed to close
a gap, and the result does not depend on the input page order. A period is
only complete when a final closing balance closes it and no page is missing
or misaligned.

Example:
    >>> import mt940
    >>> data = (
    ...     ':20:PAGE-2\n'
    ...     ':25:DE00BANK0123456789\n'
    ...     ':28C:7/2\n'
    ...     ':60M:C240102EUR150,00\n'
    ...     ':61:2401020102D50,00NTRFREF-2\n'
    ...     ':62F:C240102EUR100,00\n'
    ...     ':20:PAGE-1\n'
    ...     ':25:DE00BANK0123456789\n'
    ...     ':28C:7/1\n'
    ...     ':60F:C240101EUR100,00\n'
    ...     ':61:2401010101C50,00NTRFREF-1\n'
    ...     ':62M:C240101EUR150,00\n'
    ... )
    >>> pages = mt940.parse_statements(data)
    >>> (period,) = mt940.reconstruct(pages)
    >>> period.is_complete
    True
    >>> period.opening_balance.amount
    <100.00 EUR>
    >>> period.closing_balance.amount
    <100.00 EUR>
    >>> [t.data['customer_reference'] for t in period.transactions]
    ['REF-1', 'REF-2']
"""

from __future__ import annotations

import dataclasses
import itertools
from typing import TYPE_CHECKING

from . import models, utils

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: Balance tag output keys that open a statement page, in priority order.
_OPENING_KEYS = (
    'final_opening_balance',
    'opening_balance',
    'intermediate_opening_balance',
)

#: Balance tag output keys that close a statement page, in priority order.
_CLOSING_KEYS = (
    'final_closing_balance',
    'closing_balance',
    'intermediate_closing_balance',
)

#: Closing balance keys that mark a statement period as closed.
_FINAL_CLOSING_KEYS = ('final_closing_balance', 'closing_balance')


def _balance(
    page: models.Transactions, keys: tuple[str, ...]
) -> models.Balance | None:
    """Return the page's first present balance under ``keys``.

    Anything stored that is not a :class:`~mt940.models.Balance` counts as
    absent, so a malformed value never chains two pages.
    """
    balance: object = utils.coalesce(*(page.data.get(key) for key in keys))
    return balance if isinstance(balance, models.Balance) else None


def _sequence_number(page: models.Transactions) -> int:
    """Return the page's sequence number, defaulting to one.

    A page without a usable ``sequence_number`` counts as the first page of
    its statement.
    """
    raw: object = page.data.get('sequence_number')
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 1


def _number_key(value: str | None) -> tuple[int, int, str]:
    """Return a deterministic sort key for an optional numeric string."""
    if value and value.isdigit():
        return (0, int(value), value)
    return (1, 0, value or '')


def _group_key(
    key: tuple[str | None, str | None],
) -> tuple[str, tuple[int, int, str]]:
    """Return the period ordering key for an account/statement pair."""
    account, statement_number = key
    return (account or '', _number_key(statement_number))


def _freeze(data: Mapping[str, object]) -> str:
    """Return a deterministic text form of statement or transaction data."""
    return repr(sorted(data.items(), key=lambda item: item[0]))


def _fingerprint(page: models.Transactions) -> str:
    """Return a stable content fingerprint used to order duplicate pages."""
    parts = [_freeze(page.data)]
    parts.extend(
        _freeze(transaction.data) for transaction in page.transactions
    )
    return '\n'.join(parts)


def _balances_chain(
    closing: models.Balance | None,
    opening: models.Balance | None,
) -> bool:
    """Return whether a closing balance continues as the next opening.

    Both sides need a converted amount; anything unverifiable counts as a
    broken chain. The comparison uses the signed amount and currency, so the
    balance date does not break a chain.
    """
    if closing is None or opening is None:
        return False
    closing_amount = closing.amount
    if not isinstance(closing_amount, models.Amount):
        return False
    return closing_amount == opening.amount


def _is_closed(page: models.Transactions) -> bool:
    """Return whether the page carries a final closing balance."""
    return any(
        isinstance(page.data.get(key), models.Balance)
        for key in _FINAL_CLOSING_KEYS
    )


@dataclasses.dataclass(frozen=True)
class BalanceBreak:
    """A broken balance chain between two adjacent retained pages.

    Attributes:
        from_sequence: Sequence number of the earlier page.
        to_sequence: Sequence number of the later page.
        closing: Closing balance of the earlier page, or ``None`` when the
            page has none.
        opening: Opening balance of the later page, or ``None`` when the
            page has none.
    """

    #: Sequence number of the earlier page.
    from_sequence: int
    #: Sequence number of the later page.
    to_sequence: int
    #: Closing balance of the earlier page, or ``None``.
    closing: models.Balance | None
    #: Opening balance of the later page, or ``None``.
    opening: models.Balance | None


@dataclasses.dataclass
class StatementPeriod:
    """One statement period reassembled from its pages.

    Pages are retained in sequence order. Extra copies of a duplicate page
    are dropped after a deterministic content comparison and reported under
    ``duplicate_pages``. The period is complete only when a final closing
    balance closes it and it has neither missing pages nor balance breaks.

    Attributes:
        account_identification: Shared ``:25:`` account identifier.
        statement_number: Shared ``:28C:`` statement number.
        pages: Retained pages in sequence order.
        missing_pages: Absent sequence numbers between one and the highest
            present sequence number, ascending.
        duplicate_pages: Sequence numbers supplied by more than one page,
            ascending.
        balance_breaks: Broken chains between adjacent retained pages.
    """

    #: Shared ``:25:`` account identifier, or ``None`` when absent.
    account_identification: str | None
    #: Shared ``:28C:`` statement number, or ``None`` when the pages have
    #: none.
    statement_number: str | None
    #: Retained pages in sequence order; never empty.
    pages: list[models.Transactions]
    #: Absent sequence numbers in the covered range, ascending.
    missing_pages: list[int]
    #: Sequence numbers supplied by more than one page, ascending.
    duplicate_pages: list[int]
    #: Broken balance chains in page order.
    balance_breaks: list[BalanceBreak]

    @property
    def transactions(self) -> list[models.Transaction]:
        """Return all transactions of the retained pages in statement order.

        The transaction objects are shared with the source pages.
        """
        return [
            transaction
            for page in self.pages
            for transaction in page.transactions
        ]

    @property
    def opening_balance(self) -> models.Balance | None:
        """Return the opening balance of the first retained page."""
        return _balance(self.pages[0], _OPENING_KEYS)

    @property
    def closing_balance(self) -> models.Balance | None:
        """Return the closing balance of the last retained page."""
        return _balance(self.pages[-1], _CLOSING_KEYS)

    @property
    def is_closed(self) -> bool:
        """Return whether the last retained page closes the period finally.

        An intermediate closing balance (``:62M:``) does not close a period,
        so a period whose final pages are missing is not closed.
        """
        return _is_closed(self.pages[-1])

    @property
    def is_complete(self) -> bool:
        """Return whether the period is closed, gapless and well chained.

        Duplicate pages are reported but do not block completeness: the
        retained copy is chosen deterministically and chains on its own.
        """
        return (
            self.is_closed
            and not self.missing_pages
            and not self.balance_breaks
        )

    @property
    def has_issues(self) -> bool:
        """Return whether any anomaly was found while reassembling."""
        return bool(
            self.missing_pages or self.duplicate_pages or self.balance_breaks
        )

    def merge(self) -> models.Transactions:
        """Combine the retained pages into a single transaction collection.

        Statement metadata comes from the first page, except the closing
        balances and the sequence number, which come from the last page. The
        transaction objects are shared with the source pages; the merged
        collection does not re-parse anything.
        """
        first = self.pages[0]
        last = self.pages[-1]
        merged = models.Transactions(
            first.processors,
            dict(first.tags),
            transaction_boundary=first.transaction_boundary,
            options=first.options,
        )
        merged.data.update(first.data)
        for key in _CLOSING_KEYS:
            merged.data.pop(key, None)
        for key in (*_CLOSING_KEYS, 'sequence_number'):
            if key in last.data:
                merged.data[key] = last.data[key]
        merged.transactions.extend(self.transactions)
        return merged


def _build_period(
    account: str | None,
    statement_number: str | None,
    pages: list[models.Transactions],
) -> StatementPeriod:
    """Order one account/statement group and classify its anomalies.

    Duplicate sequence numbers resolve to a single retained page through the
    content fingerprint, so the input order never changes the outcome.
    """
    by_sequence: dict[int, list[models.Transactions]] = {}
    for page in pages:
        by_sequence.setdefault(_sequence_number(page), []).append(page)

    retained: list[models.Transactions] = []
    duplicate_pages: list[int] = []
    for sequence in sorted(by_sequence):
        candidates = sorted(by_sequence[sequence], key=_fingerprint)
        retained.append(candidates[0])
        if len(candidates) > 1:
            duplicate_pages.append(sequence)

    present = set(by_sequence)
    missing_pages = [
        sequence
        for sequence in range(1, max(present) + 1)
        if sequence not in present
    ]

    balance_breaks: list[BalanceBreak] = []
    for earlier, later in itertools.pairwise(retained):
        from_sequence = _sequence_number(earlier)
        to_sequence = _sequence_number(later)
        if to_sequence != from_sequence + 1:
            # A sequence gap is already reported as missing pages.
            continue
        closing = _balance(earlier, _CLOSING_KEYS)
        opening = _balance(later, _OPENING_KEYS)
        if not _balances_chain(closing, opening):
            balance_breaks.append(
                BalanceBreak(from_sequence, to_sequence, closing, opening)
            )

    return StatementPeriod(
        account_identification=account,
        statement_number=statement_number,
        pages=retained,
        missing_pages=missing_pages,
        duplicate_pages=duplicate_pages,
        balance_breaks=balance_breaks,
    )


def reconstruct(
    pages: Iterable[models.Transactions],
) -> list[StatementPeriod]:
    """Group statement pages and reassemble their statement periods.

    Args:
        pages: Parsed pages, normally the result of
            :func:`mt940.parse_statements`. Consumed once. Only the account
            identification, statement and sequence numbers and the balance
            tags of each page are inspected.

    Returns:
        One period per account and statement number combination, sorted by
        account identification and statement number. Pages within a period
        are sorted by sequence number, so the input order does not change
        the result. Pages from a different statement number of the same
        account form their own period and never fill another period's gap.
    """
    groups: dict[tuple[str | None, str | None], list[models.Transactions]] = {}
    for page in pages:
        key = (
            page.data.get('account_identification'),
            page.data.get('statement_number'),
        )
        groups.setdefault(key, []).append(page)

    periods: list[StatementPeriod] = []
    for key in sorted(groups, key=_group_key):
        account, statement_number = key
        periods.append(_build_period(account, statement_number, groups[key]))
    return periods
