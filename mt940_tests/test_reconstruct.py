"""Reassemble paginated MT940 statements into complete periods."""

from __future__ import annotations

import itertools

import mt940
from mt940 import models
from mt940.reconstruct import BalanceBreak, StatementPeriod


def _page(
    *,
    account: str = 'DE00BANK0123456789',
    number: str = '7',
    sequence: int = 1,
    opening: str = 'C240101EUR0,00',
    closing: str = 'C240101EUR0,00',
    final_opening: bool = False,
    final_closing: bool = False,
    reference: str | None = None,
    entries: tuple[str, ...] = (),
) -> str:
    open_tag = '60F' if final_opening else '60M'
    close_tag = '62F' if final_closing else '62M'
    lines = [
        f':20:{reference or f"REF-{number}-{sequence}"}',
        f':25:{account}',
        f':28C:{number}/{sequence}',
        f':{open_tag}:{opening}',
    ]
    lines.extend(f':61:{entry}' for entry in entries)
    lines.append(f':{close_tag}:{closing}')
    return '\n'.join(lines) + '\n'


def _chain(
    balances: list[tuple[str, str]],
    *,
    account: str = 'DE00BANK0123456789',
    number: str = '7',
) -> str:
    """Build a page sequence whose balances chain, then shuffle nothing."""
    pages = []
    for index, (opening, closing) in enumerate(balances, start=1):
        pages.append(
            _page(
                account=account,
                number=number,
                sequence=index,
                opening=opening,
                closing=closing,
                final_opening=index == 1,
                final_closing=index == len(balances),
                entries=(f'24010{index}010{index}C1,00NTRFTX{index}',),
            )
        )
    return ''.join(pages)


def _summary(periods: list[StatementPeriod]) -> list[tuple[object, ...]]:
    return [
        (
            period.account_identification,
            period.statement_number,
            [page.data.get('sequence_number') for page in period.pages],
            period.missing_pages,
            period.duplicate_pages,
            [
                (gap.from_sequence, gap.to_sequence)
                for gap in period.balance_breaks
            ],
            period.is_closed,
            period.is_complete,
            [
                transaction.data.get('customer_reference')
                for transaction in period.transactions
            ],
        )
        for period in periods
    ]


def test_empty_input_gives_no_periods() -> None:
    assert mt940.reconstruct([]) == []


def test_scrambled_pages_chain_into_a_complete_period() -> None:
    text = _chain(
        [
            ('C240101EUR100,00', 'C240101EUR150,00'),
            ('C240101EUR150,00', 'C240102EUR90,00'),
            ('C240102EUR90,00', 'C240103EUR90,00'),
        ]
    )
    blocks = text.split(':20:')
    shuffled = ':20:'.join([blocks[0], blocks[3], blocks[1], blocks[2]])
    (period,) = mt940.reconstruct(mt940.parse_statements(shuffled))

    assert period.account_identification == 'DE00BANK0123456789'
    assert period.statement_number == '7'
    assert [page.data['sequence_number'] for page in period.pages] == [
        '1',
        '2',
        '3',
    ]
    assert period.opening_balance == models.Balance(
        'C', '100,00', None, currency='EUR'
    )
    assert period.closing_balance == models.Balance(
        'C', '90,00', None, currency='EUR'
    )
    assert [
        transaction.data['customer_reference']
        for transaction in period.transactions
    ] == ['TX1', 'TX2', 'TX3']
    assert period.is_closed
    assert period.is_complete
    assert not period.has_issues
    assert period.missing_pages == []
    assert period.duplicate_pages == []
    assert period.balance_breaks == []


def test_missing_page_is_reported_without_a_balance_break() -> None:
    text = _chain(
        [
            ('C240101EUR100,00', 'C240101EUR150,00'),
            ('C240101EUR150,00', 'C240102EUR90,00'),
            ('C240102EUR90,00', 'C240103EUR40,00'),
        ]
    )
    blocks = text.split(':20:')
    del blocks[2]  # drop the second page entirely
    (period,) = mt940.reconstruct(mt940.parse_statements(':20:'.join(blocks)))

    assert period.missing_pages == [2]
    # The gap explains the balance mismatch, so no break is reported.
    assert period.balance_breaks == []
    assert period.is_closed
    assert not period.is_complete
    assert period.has_issues


def test_unclosed_period_is_not_complete() -> None:
    text = _chain(
        [
            ('C240101EUR100,00', 'C240101EUR150,00'),
            ('C240101EUR150,00', 'C240102EUR90,00'),
        ]
    )
    blocks = text.split(':20:')
    # The bank sent an intermediate closing balance on the last page.
    blocks[2] = blocks[2].replace(':62F:', ':62M:')
    (period,) = mt940.reconstruct(mt940.parse_statements(':20:'.join(blocks)))

    assert period.missing_pages == []
    assert period.balance_breaks == []
    assert not period.is_closed
    assert not period.is_complete


def test_duplicate_page_is_reported_and_dropped() -> None:
    one = _page(
        sequence=1,
        opening='C240101EUR100,00',
        closing='C240101EUR100,00',
        final_opening=True,
        final_closing=True,
    )
    (period,) = mt940.reconstruct(mt940.parse_statements(one + one))

    assert period.duplicate_pages == [1]
    assert len(period.pages) == 1
    assert period.is_complete
    assert period.has_issues


def test_conflicting_duplicates_resolve_deterministically() -> None:
    first = _page(
        sequence=1, reference='COPY-A', entries=('2401010101C1,00NTRFAAA',)
    )
    other = _page(
        sequence=1, reference='COPY-B', entries=('2401010101C2,00NTRFBBB',)
    )
    forward = mt940.reconstruct(mt940.parse_statements(first + other))
    backward = mt940.reconstruct(mt940.parse_statements(other + first))

    assert _summary(forward) == _summary(backward)
    (period,) = forward
    assert period.duplicate_pages == [1]
    (kept,) = period.pages
    assert kept.data['transaction_reference'] in {'COPY-A', 'COPY-B'}


def test_balance_break_is_reported_between_chained_pages() -> None:
    text = _page(
        sequence=1,
        opening='C240101EUR100,00',
        closing='C240101EUR150,00',
        final_opening=True,
    ) + _page(
        sequence=2,
        opening='C240102EUR140,00',
        closing='C240102EUR140,00',
        final_closing=True,
    )
    (period,) = mt940.reconstruct(mt940.parse_statements(text))

    assert period.missing_pages == []
    assert len(period.balance_breaks) == 1
    gap = period.balance_breaks[0]
    assert isinstance(gap, BalanceBreak)
    assert (gap.from_sequence, gap.to_sequence) == (1, 2)
    assert gap.closing == models.Balance('C', '150,00', None, currency='EUR')
    assert gap.opening == models.Balance('C', '140,00', None, currency='EUR')
    assert not period.is_complete


def test_missing_closing_balance_breaks_the_chain() -> None:
    text = (
        ':20:ONLY-OPEN\n'
        ':25:DE00BANK0123456789\n'
        ':28C:7/1\n'
        ':60F:C240101EUR100,00\n'
    ) + _page(
        sequence=2,
        opening='C240101EUR100,00',
        closing='C240102EUR100,00',
        final_closing=True,
    )
    (period,) = mt940.reconstruct(mt940.parse_statements(text))

    (gap,) = period.balance_breaks
    assert gap.closing is None
    assert gap.opening is not None
    assert not period.is_complete


def test_missing_opening_balance_breaks_the_chain() -> None:
    text = _page(
        sequence=1,
        opening='C240101EUR100,00',
        closing='C240101EUR100,00',
        final_opening=True,
    ) + (
        ':20:ONLY-CLOSE\n'
        ':25:DE00BANK0123456789\n'
        ':28C:7/2\n'
        ':62F:C240102EUR100,00\n'
    )
    (period,) = mt940.reconstruct(mt940.parse_statements(text))

    (gap,) = period.balance_breaks
    assert gap.closing is not None
    assert gap.opening is None
    assert not period.is_complete


def test_unconverted_balance_amount_breaks_the_chain() -> None:
    first = mt940.parse(
        ':20:A\n:28C:1/1\n:60F:C240101EUR100,00\n:62M:C240101EUR100,00\n'
    )
    second = mt940.parse(
        ':20:B\n:28C:1/2\n:60M:C240101EUR100,00\n:62F:C240101EUR100,00\n'
    )
    first.data['intermediate_closing_balance'] = models.Balance()
    (period,) = mt940.reconstruct([first, second])

    (gap,) = period.balance_breaks
    assert gap.closing is not None
    assert gap.closing.amount is None
    assert not period.is_complete


def test_other_statements_of_the_account_are_not_borrowed() -> None:
    # Statement 7 misses page 2. Statement 8 opens exactly where statement
    # 7 page 1 closed, but a different statement number must not fill the
    # gap.
    gap_text = _page(
        number='7',
        sequence=1,
        opening='C240101EUR100,00',
        closing='C240101EUR150,00',
        final_opening=True,
    ) + _page(
        number='7',
        sequence=3,
        opening='C240103EUR90,00',
        closing='C240103EUR90,00',
        final_closing=True,
    )
    next_text = _page(
        number='8',
        sequence=1,
        opening='C240101EUR150,00',
        closing='C240102EUR150,00',
        final_opening=True,
        final_closing=True,
    )
    periods = mt940.reconstruct(mt940.parse_statements(next_text + gap_text))

    assert len(periods) == 2
    incomplete, complete = periods
    assert incomplete.statement_number == '7'
    assert incomplete.missing_pages == [2]
    assert not incomplete.is_complete
    assert all(
        page.data['statement_number'] == '7' for page in incomplete.pages
    )
    assert complete.statement_number == '8'
    assert complete.is_complete


def test_accounts_form_separate_periods() -> None:
    first = _page(
        account='DE00BANK0000000002',
        final_opening=True,
        final_closing=True,
    )
    second = _page(
        account='DE00BANK0000000001',
        final_opening=True,
        final_closing=True,
    )
    periods = mt940.reconstruct(mt940.parse_statements(first + second))

    assert [period.account_identification for period in periods] == [
        'DE00BANK0000000001',
        'DE00BANK0000000002',
    ]
    assert all(period.is_complete for period in periods)


def test_input_order_does_not_change_the_result() -> None:
    pages = [
        _page(
            sequence=1,
            opening='C240101EUR100,00',
            closing='C240101EUR150,00',
            final_opening=True,
            entries=('2401010101C1,00NTRFTX1',),
        ),
        _page(
            sequence=2,
            opening='C240101EUR150,00',
            closing='C240102EUR90,00',
            entries=('2401020102C1,00NTRFTX2',),
        ),
        # A duplicate of page 2 with a different reference.
        _page(
            sequence=2,
            opening='C240101EUR150,00',
            closing='C240102EUR90,00',
            reference='COPY-OF-2',
            entries=('2401020102C1,00NTRFTX2',),
        ),
        # Page 3 is missing; page 4 closes the statement.
        _page(
            sequence=4,
            opening='C240104EUR40,00',
            closing='C240104EUR40,00',
            final_closing=True,
            entries=('2401040104C1,00NTRFTX4',),
        ),
    ]
    summaries = set()
    for ordering in itertools.permutations(pages):
        periods = mt940.reconstruct(mt940.parse_statements(''.join(ordering)))
        summaries.add(repr(_summary(periods)))

    assert len(summaries) == 1
    (period,) = mt940.reconstruct(mt940.parse_statements(''.join(pages)))
    assert period.missing_pages == [3]
    assert period.duplicate_pages == [2]
    assert not period.is_complete


def test_statement_without_sequence_number() -> None:
    text = (
        ':20:NO-SEQ\n'
        ':25:DE00BANK0123456789\n'
        ':60F:C240101EUR100,00\n'
        ':62F:C240101EUR100,00\n'
    )
    (period,) = mt940.reconstruct(mt940.parse_statements(text))

    assert period.statement_number is None
    assert period.missing_pages == []
    assert period.is_complete


def test_merge_combines_the_retained_pages() -> None:
    text = _chain(
        [
            ('C240101EUR100,00', 'C240101EUR150,00'),
            ('C240101EUR150,00', 'C240102EUR90,00'),
        ]
    )
    (period,) = mt940.reconstruct(mt940.parse_statements(text))
    merged = period.merge()

    assert isinstance(merged, models.Transactions)
    assert merged.data['final_opening_balance'] == period.opening_balance
    assert merged.data['final_closing_balance'] == period.closing_balance
    assert 'intermediate_closing_balance' not in merged.data
    assert merged.data['sequence_number'] == '2'
    assert merged.data['statement_number'] == '7'
    assert len(merged) == 2
    assert list(merged) == period.transactions
