"""Tests for the balance-chain checker in ``examples.balance_chain``."""

from __future__ import annotations

from pathlib import Path

import mt940
from examples.balance_chain import (
    CURRENCY_CHANGE,
    GAP,
    MISSING_BALANCE,
    MISMATCH,
    OK,
    check_balance_chain,
)

ROOT: Path = Path(__file__).resolve().parents[1]
FIXTURE: Path = ROOT / 'examples' / 'fixtures' / 'balance_chain.sta'


def statuses(source: Path | str) -> list[str]:
    statements: list[mt940.models.Transactions] = mt940.parse_statements(
        source
    )
    return [check.status for check in check_balance_chain(statements)]


def test_fixture_chain() -> None:
    assert statuses(FIXTURE) == [
        # Per-page arithmetic: opening + turnover == closing.
        OK,
        OK,
        OK,
        OK,
        OK,
        # Page continuation inside statement 1.
        OK,
        # Statement 1 closing == statement 2 opening.
        OK,
        # Statement 3 is missing: a gap, not an error.
        GAP,
        # EUR -> USD without FX evidence: not comparable, not an error.
        CURRENCY_CHANGE,
    ]


def test_available_balance_is_not_a_book_balance() -> None:
    source: str = (
        ':20:REF-1\n'
        ':25:ACCOUNT\n'
        ':28C:1/1\n'
        ':60F:C260201EUR100,00\n'
        ':64:C260201EUR80,00\n'
        ':20:REF-2\n'
        ':25:ACCOUNT\n'
        ':28C:2/1\n'
        ':60F:C260202EUR100,00\n'
        ':62F:C260202EUR100,00\n'
    )
    # No :62F: in statement 1: the :64: available balance must not fill
    # the gap, so both the page check and the link report a missing
    # book balance instead of comparing against 80.00 EUR.
    assert statuses(source) == [MISSING_BALANCE, OK, MISSING_BALANCE]


def test_currency_change_is_not_an_error() -> None:
    source: str = (
        ':20:REF-1\n'
        ':25:ACCOUNT\n'
        ':28C:1/1\n'
        ':60F:C260201EUR100,00\n'
        ':62F:C260201EUR100,00\n'
        ':20:REF-2\n'
        ':25:ACCOUNT\n'
        ':28C:2/1\n'
        ':60F:C260202USD50,00\n'
        ':62F:C260202USD50,00\n'
    )
    checks = check_balance_chain(mt940.parse_statements(source))
    link = checks[-1]
    assert link.status == CURRENCY_CHANGE
    assert 'not comparable' in link.detail


def test_statement_number_gap_is_not_compared() -> None:
    source: str = (
        ':20:REF-1\n'
        ':25:ACCOUNT\n'
        ':28C:1/1\n'
        ':60F:C260201EUR100,00\n'
        ':62F:C260201EUR100,00\n'
        ':20:REF-3\n'
        ':25:ACCOUNT\n'
        ':28C:3/1\n'
        ':60F:C260203EUR999,00\n'
        ':62F:C260203EUR999,00\n'
    )
    checks = check_balance_chain(mt940.parse_statements(source))
    link = checks[-1]
    assert link.status == GAP
    assert '999' not in link.detail


def test_amount_mismatch_same_currency_is_an_error() -> None:
    source: str = (
        ':20:REF-1\n'
        ':25:ACCOUNT\n'
        ':28C:1/1\n'
        ':60F:C260201EUR100,00\n'
        ':62F:C260201EUR100,00\n'
        ':20:REF-2\n'
        ':25:ACCOUNT\n'
        ':28C:2/1\n'
        ':60F:C260202EUR90,00\n'
        ':62F:C260202EUR90,00\n'
    )
    checks = check_balance_chain(mt940.parse_statements(source))
    assert checks[-1].status == MISMATCH


def test_page_sequence_gap_is_not_compared() -> None:
    source: str = (
        ':20:REF-1\n'
        ':25:ACCOUNT\n'
        ':28C:7/1\n'
        ':60F:C260201EUR100,00\n'
        ':62M:C260201EUR40,00\n'
        ':20:REF-2\n'
        ':25:ACCOUNT\n'
        ':28C:7/3\n'
        ':60M:C260201EUR40,00\n'
        ':62F:C260201EUR40,00\n'
    )
    checks = check_balance_chain(mt940.parse_statements(source))
    page_link = checks[2]
    assert page_link.status == GAP
    assert 'not comparable' in page_link.detail


def test_page_continuation_mismatch_is_an_error() -> None:
    source: str = (
        ':20:REF-1\n'
        ':25:ACCOUNT\n'
        ':28C:7/1\n'
        ':60F:C260201EUR100,00\n'
        ':62M:C260201EUR40,00\n'
        ':20:REF-2\n'
        ':25:ACCOUNT\n'
        ':28C:7/2\n'
        ':60M:C260201EUR30,00\n'
        ':62F:C260201EUR30,00\n'
    )
    checks = check_balance_chain(mt940.parse_statements(source))
    assert checks[2].status == MISMATCH
