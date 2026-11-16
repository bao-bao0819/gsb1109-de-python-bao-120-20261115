"""Verify the balance chain across statements and page continuations.

One file can hold opening (``:60F:``/``:60M:``), closing
(``:62F:``/``:62M:``) and available (``:64:``) balances, each with its
own currency. The chain is checked per page, across the pages of one
statement and across consecutive statement numbers. A link is only
compared when it is actually comparable: numbering gaps, missing book
balances and currency changes without FX evidence are reported as
gaps, never as amount errors. The available balance is informational
and never substitutes a book balance.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from pathlib import Path

import mt940
from mt940.models import Amount, Balance, Transactions

SOURCE: Path = Path(__file__).parent / 'fixtures' / 'balance_chain.sta'

OK = 'ok'
MISMATCH = 'mismatch'
GAP = 'gap'
CURRENCY_CHANGE = 'currency-change'
MISSING_BALANCE = 'missing-balance'


@dataclasses.dataclass
class Page:
    """Book and available balances of one parsed statement block.

    Attributes:
        label: Display label ``<statement>/<sequence>`` with the raw
            numbers, leading zeroes preserved.
        statement_number: Statement number as an integer for ordering.
        sequence_number: Page sequence as an integer for ordering.
        opening: Opening book balance from ``:60F:`` or ``:60M:``.
        closing: Closing book balance from ``:62F:`` or ``:62M:``.
        available: Available balance from ``:64:``, informational only.
        turnover: Signed sum of the page's transaction amounts.
    """

    label: str
    statement_number: int
    sequence_number: int
    opening: Balance | None
    closing: Balance | None
    available: Balance | None
    turnover: Decimal


@dataclasses.dataclass
class Check:
    """One checked link in the balance chain.

    Attributes:
        description: Which page or link is checked.
        status: One of ``OK``, ``MISMATCH``, ``GAP``,
            ``CURRENCY_CHANGE`` or ``MISSING_BALANCE``.
        detail: Human-readable explanation of the outcome.
    """

    description: str
    status: str
    detail: str


def _book_balance(
    statement: Transactions, *keys: str
) -> Balance | None:
    """Return the first book balance with a usable amount.

    Only the opening and closing tags qualify. The available balance
    (``:64:``) is never a substitute for a missing book balance.
    """
    for key in keys:
        balance: object = statement.data.get(key)
        if isinstance(balance, Balance) and isinstance(
            balance.amount, Amount
        ):
            return balance
    return None


def collect_pages(statements: list[Transactions]) -> list[Page]:
    """Build pages sorted by statement and sequence number."""
    pages: list[Page] = []
    for statement in statements:
        data = statement.data
        raw_number: str = data.get('statement_number', '0')
        raw_sequence: str = data.get('sequence_number') or '1'
        available: object = data.get('available_balance')
        pages.append(
            Page(
                label=f'{raw_number}/{raw_sequence}',
                statement_number=int(raw_number),
                sequence_number=int(raw_sequence),
                opening=_book_balance(
                    statement,
                    'final_opening_balance',
                    'intermediate_opening_balance',
                ),
                closing=_book_balance(
                    statement,
                    'final_closing_balance',
                    'intermediate_closing_balance',
                ),
                available=(
                    available
                    if isinstance(available, Balance)
                    and isinstance(available.amount, Amount)
                    else None
                ),
                turnover=sum(
                    (
                        transaction.data['amount'].amount
                        for transaction in statement
                    ),
                    Decimal('0'),
                ),
            )
        )
    return sorted(
        pages, key=lambda page: (page.statement_number, page.sequence_number)
    )


def check_page(page: Page) -> Check:
    """Check that opening plus turnover equals the closing balance."""
    description = f'Statement {page.label}'
    if page.opening is None or page.closing is None:
        return Check(
            description,
            MISSING_BALANCE,
            'book balance missing; available balance is not a substitute',
        )
    opening: Amount = page.opening.amount  # type: ignore[assignment]
    closing: Amount = page.closing.amount  # type: ignore[assignment]
    if opening.currency != closing.currency:
        return Check(
            description,
            CURRENCY_CHANGE,
            f'currency changes {opening.currency} -> {closing.currency}'
            ' within one page, no FX basis to compare',
        )
    expected: Decimal = opening.amount + page.turnover
    if expected == closing.amount:
        return Check(
            description,
            OK,
            f'{opening.amount} {opening.currency}'
            f' + turnover {page.turnover}'
            f' = {closing.amount} {closing.currency}',
        )
    return Check(
        description,
        MISMATCH,
        f'{opening.amount} {opening.currency} + turnover {page.turnover}'
        f' = {expected}, but closing is {closing.amount} {closing.currency}',
    )


def _compare(
    description: str, left: Balance | None, right: Balance | None
) -> Check:
    """Compare two book balances that should be equal.

    A currency change without explicit FX evidence makes the link
    incomparable; the difference is not computed as an error.
    """
    if left is None or right is None:
        return Check(
            description,
            MISSING_BALANCE,
            'book balance missing; available balance is not a substitute',
        )
    left_amount: Amount = left.amount  # type: ignore[assignment]
    right_amount: Amount = right.amount  # type: ignore[assignment]
    if left_amount.currency != right_amount.currency:
        return Check(
            description,
            CURRENCY_CHANGE,
            f'currency changes {left_amount.currency}'
            f' -> {right_amount.currency}'
            ' without FX evidence; not comparable',
        )
    if left_amount.amount == right_amount.amount:
        return Check(
            description,
            OK,
            f'{left_amount.amount} {left_amount.currency}'
            f' == {right_amount.amount} {right_amount.currency}',
        )
    return Check(
        description,
        MISMATCH,
        f'{left_amount.amount} {left_amount.currency}'
        f' != {right_amount.amount} {right_amount.currency}',
    )


def check_balance_chain(statements: list[Transactions]) -> list[Check]:
    """Check pages, page continuations and the statement chain.

    Pages of one statement link closing to opening when their sequence
    numbers are consecutive. Statements link the last page's closing to
    the next statement's first page opening when their numbers are
    consecutive. Anything else is a gap, not an error.
    """
    pages: list[Page] = collect_pages(statements)
    checks: list[Check] = [check_page(page) for page in pages]

    previous: Page
    page: Page
    for previous, page in zip(pages, pages[1:]):
        if previous.statement_number != page.statement_number:
            continue
        description = f'Page link {previous.label} -> {page.label}'
        if page.sequence_number != previous.sequence_number + 1:
            checks.append(
                Check(
                    description,
                    GAP,
                    'page sequence not consecutive; not comparable',
                )
            )
        else:
            checks.append(
                _compare(description, previous.closing, page.opening)
            )

    first_pages: dict[int, Page] = {}
    last_pages: dict[int, Page] = {}
    for page in pages:
        first_pages.setdefault(page.statement_number, page)
        last_pages[page.statement_number] = page
    numbers: list[int] = sorted(first_pages)
    previous_number: int
    number: int
    for previous_number, number in zip(numbers, numbers[1:]):
        description = f'Statement link {previous_number} -> {number}'
        if number != previous_number + 1:
            checks.append(
                Check(
                    description,
                    GAP,
                    'statement numbers not consecutive; gap, not comparable',
                )
            )
        else:
            checks.append(
                _compare(
                    description,
                    last_pages[previous_number].closing,
                    first_pages[number].opening,
                )
            )
    return checks


def available_lines(pages: list[Page]) -> list[str]:
    """Report available balances as informational, never as book balances."""
    lines: list[str] = []
    for page in pages:
        if page.available is not None:
            amount: Amount = page.available.amount  # type: ignore[assignment]
            lines.append(
                f'Statement {page.label}: available'
                f' {amount.amount} {amount.currency}'
                ' (informational only, not a book balance)'
            )
    return lines


def main() -> None:
    """Print the balance-chain report for the fixture file."""
    statements: list[Transactions] = mt940.parse_statements(SOURCE)
    check: Check
    for check in check_balance_chain(statements):
        print(f'{check.description}: {check.detail} [{check.status}]')
    line: str
    for line in available_lines(collect_pages(statements)):
        print(line)


if __name__ == '__main__':
    main()
