"""Tests for source-named reference tables and explicit deduplication.

The table indexes references without merging transactions: repeated reliable
values stay separate rows, ``NOREF``/``NONREF`` placeholders are skipped and
the caller names the source domains used for a duplicate check.
"""

from __future__ import annotations

import mt940
import pytest
from mt940.references import (
    NON_REFERENCE_VALUES,
    ReferenceEntry,
    ReferenceKey,
    ReferenceSource,
    ReferenceTable,
    build_reference_table,
)


def _transaction(data: dict[str, object]) -> mt940.models.Transaction:
    """Build an owned transaction with arbitrary parsed field data."""
    return mt940.models.Transaction(mt940.models.Transactions(), data)


def test_key_equality_never_crosses_sources() -> None:
    customer = ReferenceKey(ReferenceSource.CUSTOMER, 'SAME')
    customer_again = ReferenceKey(ReferenceSource.CUSTOMER, 'SAME')
    bank = ReferenceKey(ReferenceSource.BANK, 'SAME')
    assert customer == customer_again
    assert hash(customer) == hash(customer_again)
    assert customer != bank
    assert customer != 'SAME'
    assert repr(customer) == "<CUSTOMER='SAME'>"


def test_entry_identity_uses_index_and_key() -> None:
    key = ReferenceKey(ReferenceSource.BANK, 'BANK-1')
    entry = ReferenceEntry(3, key)
    assert entry.transaction_index == 3
    assert entry.key == key
    same = ReferenceEntry(3, ReferenceKey(ReferenceSource.BANK, 'BANK-1'))
    assert entry == same
    assert entry != ReferenceEntry(4, key)
    assert entry != key
    assert hash(entry) == hash(ReferenceEntry(3, key))
    assert repr(entry) == "<3: <BANK='BANK-1'>>"


def test_table_indexes_every_source_domain_separately() -> None:
    transactions = [
        _transaction({
            'customer_reference': 'INV-1',
            'bank_reference': 'BANK-1',
            'transaction_reference': 'STMT-1',
            'related_reference': 'REL-1',
            'end_to_end_reference': 'E2E-1',
            'transaction_code': 116,
        }),
    ]
    table = build_reference_table(transactions)
    assert len(table) == 6
    assert [entry.transaction_index for entry in table] == [0] * 6
    keys = {entry.key for entry in table}
    assert keys == {
        ReferenceKey(ReferenceSource.CUSTOMER, 'INV-1'),
        ReferenceKey(ReferenceSource.BANK, 'BANK-1'),
        ReferenceKey(ReferenceSource.STATEMENT, 'STMT-1'),
        ReferenceKey(ReferenceSource.RELATED, 'REL-1'),
        ReferenceKey(ReferenceSource.END_TO_END, 'E2E-1'),
        ReferenceKey(ReferenceSource.TRANSACTION_CODE, '116'),
    }
    assert ReferenceKey(ReferenceSource.BANK, 'BANK-1') in table
    assert ReferenceKey(ReferenceSource.BANK, 'MISSING') not in table
    assert 'BANK-1' not in table
    bank_rows = table.lookup(ReferenceSource.BANK, 'BANK-1')
    assert bank_rows[0].transaction_index == 0
    assert table.lookup(ReferenceSource.BANK, 'MISSING') == ()
    assert table.unreferenced == frozenset()


def test_same_value_in_different_sources_stays_distinct() -> None:
    transactions = [
        _transaction({'customer_reference': 'X', 'bank_reference': 'X'}),
    ]
    table = build_reference_table(transactions)
    customer_rows = table.lookup(ReferenceSource.CUSTOMER, 'X')
    bank_rows = table.lookup(ReferenceSource.BANK, 'X')
    assert len(customer_rows) == len(bank_rows) == 1
    assert customer_rows[0].key != bank_rows[0].key
    assert len(table.duplicates()) == 0


def test_repeated_reliable_reference_keeps_every_transaction() -> None:
    transactions = [
        _transaction({'end_to_end_reference': 'E2E-SHARED'}),
        _transaction({'end_to_end_reference': 'E2E-SHARED'}),
        _transaction({'end_to_end_reference': 'E2E-OTHER'}),
    ]
    table = build_reference_table(transactions)
    duplicates = table.duplicates(ReferenceSource.END_TO_END)
    (key, rows) = next(iter(duplicates.items()))
    assert key == ReferenceKey(ReferenceSource.END_TO_END, 'E2E-SHARED')
    assert [row.transaction_index for row in rows] == [0, 1]
    assert len(table) == 3
    assert table.unreferenced == frozenset()


def test_noref_batch_is_never_merged_into_one_transaction() -> None:
    transactions = [
        _transaction({'customer_reference': 'NONREF'}),
        _transaction({'customer_reference': 'NOREF'}),
        _transaction({'customer_reference': 'NOREF'}),
    ]
    table = build_reference_table(transactions)
    assert len(table) == 0
    assert table.unreferenced == frozenset({0, 1, 2})
    assert table.duplicates() == {}
    assert table.groups() == {}


def test_placeholders_can_be_kept_as_per_transaction_entries() -> None:
    transactions = [
        _transaction({'customer_reference': 'NOREF'}),
        _transaction({'customer_reference': 'NOREF'}),
    ]
    table = build_reference_table(transactions, include_placeholders=True)
    rows = table.lookup(ReferenceSource.CUSTOMER, 'NOREF')
    assert [row.transaction_index for row in rows] == [0, 1]
    key = ReferenceKey(ReferenceSource.CUSTOMER, 'NOREF')
    assert table.duplicates(ReferenceSource.CUSTOMER) == {
        key: (rows[0], rows[1]),
    }
    assert table.unreferenced == frozenset({0, 1})


def test_custom_placeholder_values_are_case_sensitive() -> None:
    transactions = [
        _transaction({'customer_reference': 'n/a'}),
        _transaction({'customer_reference': 'N/A'}),
    ]
    table = build_reference_table(transactions, placeholder_values={'n/a'})
    assert table.unreferenced == frozenset({0})
    assert [
        row.transaction_index
        for row in table.lookup(ReferenceSource.CUSTOMER, 'N/A')
    ] == [1]
    assert isinstance(NON_REFERENCE_VALUES, frozenset)
    assert set(NON_REFERENCE_VALUES) == {'NOREF', 'NONREF'}


def test_blank_missing_and_unsupported_values_are_skipped() -> None:
    transactions = [
        _transaction({
            'customer_reference': '   ',
            'bank_reference': None,
            'end_to_end_reference': mt940.models.Amount('1', 'C', 'EUR'),
            'transaction_code': True,
        }),
        _transaction({}),
    ]
    table = build_reference_table(transactions)
    assert len(table) == 0
    assert table.unreferenced == frozenset({0, 1})


def test_integer_transaction_code_is_rendered_as_text() -> None:
    table = build_reference_table([
        _transaction({'transaction_code': 116}),
    ])
    entry = next(iter(table))
    assert entry.key == ReferenceKey(ReferenceSource.TRANSACTION_CODE, '116')


def test_sources_argument_restricts_extracted_domains() -> None:
    transactions = [
        _transaction({
            'customer_reference': 'INV-1',
            'bank_reference': 'BANK-1',
        }),
    ]
    table = build_reference_table(transactions, sources=[ReferenceSource.BANK])
    assert len(table) == 1
    assert table.lookup(ReferenceSource.CUSTOMER, 'INV-1') == ()
    assert table.groups() == table.groups(ReferenceSource.BANK)
    assert build_reference_table(transactions, sources=()).unreferenced == (
        frozenset({0})
    )


def test_invalid_source_is_rejected() -> None:
    with pytest.raises(ValueError, match='Not a ReferenceSource'):
        build_reference_table(
            [_transaction({})],
            sources=['not-a-source'],  # type: ignore[list-item]
        )


def test_groups_filter_combines_domains_and_preserves_first_order() -> None:
    transactions = [
        _transaction({'customer_reference': 'A', 'bank_reference': 'B'}),
        _transaction({'customer_reference': 'A'}),
        _transaction({'bank_reference': 'B'}),
    ]
    table = build_reference_table(transactions)
    groups = table.groups(ReferenceSource.CUSTOMER, ReferenceSource.BANK)
    assert list(groups) == [
        ReferenceKey(ReferenceSource.CUSTOMER, 'A'),
        ReferenceKey(ReferenceSource.BANK, 'B'),
    ]
    assert [
        row.transaction_index
        for row in groups[ReferenceKey(ReferenceSource.CUSTOMER, 'A')]
    ] == [0, 1]
    assert table.duplicates(ReferenceSource.STATEMENT) == {}


def test_accepts_generator_input_and_parsed_statements() -> None:
    source = (
        ':20:STMT-1\n'
        ':25:NL00BANK0123456789\n'
        ':28C:1/1\n'
        ':60F:C260101EUR100,00\n'
        ':61:260102D233,15NTRFINV-9//BANK-9\n'
        ':86:116?20EREF+E2E-9?31PayPal\n'
        ':61:260103C10,00NTRFNONREF\n'
        ':62F:C260103EUR100,00\n'
    )
    parsed = mt940.parse(source)
    table = build_reference_table(transaction for transaction in parsed)
    # The :20: reference is copied onto every transaction by the default
    # processor, so a statement-domain lookup returns both rows.
    assert [
        row.transaction_index
        for row in table.lookup(ReferenceSource.STATEMENT, 'STMT-1')
    ] == [0, 1]
    bank9 = table.lookup(ReferenceSource.BANK, 'BANK-9')
    assert bank9[0].transaction_index == 0
    e2e9 = table.lookup(ReferenceSource.END_TO_END, 'E2E-9')
    assert e2e9[0].transaction_index == 0
    code116 = table.lookup(ReferenceSource.TRANSACTION_CODE, '116')
    assert code116[0].transaction_index == 0
    business_sources = [
        ReferenceSource.CUSTOMER,
        ReferenceSource.BANK,
        ReferenceSource.END_TO_END,
        ReferenceSource.TRANSACTION_CODE,
    ]
    assert build_reference_table(
        parsed, sources=business_sources
    ).unreferenced == frozenset({1})


def test_direct_table_construction_indexes_entries() -> None:
    entries = [
        ReferenceEntry(0, ReferenceKey(ReferenceSource.RELATED, 'R')),
        ReferenceEntry(2, ReferenceKey(ReferenceSource.RELATED, 'R')),
    ]
    table = ReferenceTable(entries, [2, 3])
    assert len(table) == 2
    assert list(table) == entries
    assert table.unreferenced == frozenset({2, 3})
    assert [
        row.transaction_index
        for row in table.lookup(ReferenceSource.RELATED, 'R')
    ] == [0, 2]
