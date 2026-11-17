"""Entry-date resolution against coverage windows and bank rules."""

import datetime as dt

import pytest

import mt940
from mt940 import dates


def _statement(
    transactions: mt940.models.Transactions, **kwargs: object
) -> dict[str, object]:
    statement = mt940.tags.Statement()
    data: dict[str, object] = {'amount': '123', 'status': 'D', **kwargs}
    return statement(transactions, data)


def test_entry_dates_wrapping_years() -> None:
    transactions = mt940.models.Transactions()

    # Regular statement without an entry date: only the value date is set.
    regular = _statement(transactions, year=2000, month=1, day=1)
    assert regular['date'] == dt.date(2000, 1, 1)
    assert 'entry_date' not in regular

    # Entry date in the same year as the value date: no correction applied.
    same_year = _statement(
        transactions, year=2000, month=6, day=15, entry_month=6, entry_day=10
    )
    assert same_year['date'] == dt.date(2000, 6, 15)
    assert same_year['entry_date'] == dt.date(2000, 6, 10)
    assert same_year['guessed_entry_date'] == same_year['entry_date']

    # Entry date wraps into the next year (value date Dec, entry date Jan).
    forward = _statement(
        transactions, year=2000, month=12, day=31, entry_month=1, entry_day=1
    )
    assert forward['date'] == dt.date(2000, 12, 31)
    assert forward['entry_date'] == dt.date(2001, 1, 1)
    assert forward['guessed_entry_date'] == forward['entry_date']

    # Entry date wraps into the previous year (value date Jan, entry date
    # Dec), issue #121. `entry_date` must be resolved, not left in the value
    # date's year.
    backward = _statement(
        transactions, year=2000, month=1, day=1, entry_month=12, entry_day=31
    )
    assert backward['date'] == dt.date(2000, 1, 1)
    assert backward['entry_date'] == dt.date(1999, 12, 31)
    assert backward['guessed_entry_date'] == backward['entry_date']


def test_same_month_day_is_not_ambiguous() -> None:
    # A gap of a few days cannot cross a year, so the value date decides.
    resolution = dates.resolve_entry_date('0610', dt.date(2000, 6, 15))
    assert resolution.status == 'resolved'
    assert resolution.basis == 'value_date_year'
    assert resolution.date == dt.date(2000, 6, 10)


def test_year_boundary_without_window_uses_value_date() -> None:
    forward = dates.resolve_entry_date('0101', dt.date(2000, 12, 31))
    backward = dates.resolve_entry_date('1231', dt.date(2000, 1, 1))
    assert forward.date == dt.date(2001, 1, 1)
    assert backward.date == dt.date(1999, 12, 31)


def test_mid_year_literal_lists_cross_year_candidates() -> None:
    # January 1 is equally plausible in 2024 and 2025 for a June value date;
    # no window or bank rule distinguishes them.
    resolution = dates.resolve_entry_date('0101', dt.date(2024, 6, 1))
    assert resolution.status == 'ambiguous'
    assert resolution.basis == 'ambiguous'
    assert resolution.date is None
    assert resolution.candidates == (dt.date(2024, 1, 1), dt.date(2025, 1, 1))
    assert '2024-01-01, 2025-01-01' in resolution.explanation


def test_invalid_month_day_is_not_rolled_forward() -> None:
    resolution = dates.resolve_entry_date('0230', dt.date(2024, 6, 1))
    assert resolution.status == 'invalid'
    assert resolution.basis == 'invalid_month_day'
    assert resolution.date is None
    assert resolution.raw == '0230'
    assert resolution.candidates == ()


def test_invalid_literal_is_preserved() -> None:
    resolution = dates.resolve_entry_date('02AB', dt.date(2024, 6, 1))
    assert resolution.status == 'invalid'
    assert resolution.basis == 'invalid_literal'
    assert resolution.raw == '02AB'


def test_leap_day_only_valid_in_leap_years() -> None:
    resolution = dates.resolve_entry_date(
        '0229',
        dt.date(2024, 1, 15),
        window=(dt.date(2024, 1, 1), dt.date(2024, 3, 1)),
    )
    assert resolution.date == dt.date(2024, 2, 29)
    invalid = dates.resolve_entry_date('0229', dt.date(2023, 1, 15))
    assert invalid.date is None
    assert invalid.raw == '0229'
    assert invalid.candidates == ()


def test_window_resolves_the_year() -> None:
    resolution = dates.resolve_entry_date(
        '1231',
        dt.date(2001, 1, 1),
        window=(dt.date(2000, 12, 30), dt.date(2001, 1, 2)),
    )
    assert resolution.status == 'resolved'
    assert resolution.basis == 'statement_window'
    assert resolution.date == dt.date(2000, 12, 31)


def test_window_is_inclusive() -> None:
    resolution = dates.resolve_entry_date(
        '0101',
        dt.date(2000, 1, 2),
        window=(dt.date(2000, 1, 1), dt.date(2000, 1, 2)),
    )
    assert resolution.date == dt.date(2000, 1, 1)


def test_literal_outside_window_is_reported() -> None:
    resolution = dates.resolve_entry_date(
        '0630',
        dt.date(2024, 6, 15),
        window=(dt.date(2024, 7, 1), dt.date(2024, 7, 31)),
    )
    assert resolution.status == 'outside_window'
    assert resolution.date is None
    assert resolution.candidates == ()


def test_bank_rule_narrows_candidates() -> None:
    rules = mt940.EntryDateRules(max_days_from_value=200)
    resolution = dates.resolve_entry_date(
        '0101',
        dt.date(2024, 6, 1),
        rules=rules,
    )
    assert resolution.status == 'resolved'
    assert resolution.basis == 'bank_rule'
    assert resolution.date == dt.date(2024, 1, 1)


def test_bank_rule_can_reject_both_years() -> None:
    rules = mt940.EntryDateRules(max_days_from_value=100)
    resolution = dates.resolve_entry_date(
        '0101',
        dt.date(2024, 6, 1),
        rules=rules,
    )
    assert resolution.status == 'outside_bank_rule'
    assert resolution.date is None


def test_bank_rule_direction_is_independent() -> None:
    # Entry dates at this bank may follow the value date by at most 10 days,
    # but may precede it without that bound. Only the earlier January
    # candidate is a legal posting delay, so the date resolves.
    rules = mt940.EntryDateRules(max_days_after_value=10)
    resolution = dates.resolve_entry_date(
        '0101', dt.date(2024, 6, 1), rules=rules
    )
    assert resolution.candidates == (dt.date(2024, 1, 1),)
    assert resolution.status == 'resolved'


def test_rules_window_overrides_statement_window() -> None:
    rules = mt940.EntryDateRules(
        window_start=dt.date(2025, 1, 1),
        window_end=dt.date(2025, 1, 31),
    )
    resolution = dates.resolve_entry_date(
        '0102',
        dt.date(2025, 1, 1),
        window=(dt.date(2024, 12, 1), dt.date(2026, 1, 1)),
        rules=rules,
    )
    assert resolution.date == dt.date(2025, 1, 2)


def test_require_window_rejects_missing_coverage() -> None:
    rules = mt940.EntryDateRules(require_window=True)
    resolution = dates.resolve_entry_date(
        '0102', dt.date(2024, 1, 1), rules=rules
    )
    assert resolution.status == 'window_required'
    assert resolution.date is None


def test_rules_validate_inputs() -> None:
    with pytest.raises(ValueError, match='max_days_after_value'):
        _ = mt940.EntryDateRules(max_days_after_value=-1)
    with pytest.raises(ValueError, match='window_start'):
        _ = mt940.EntryDateRules(
            window_start=dt.date(2024, 2, 1),
            window_end=dt.date(2024, 1, 1),
        )


def test_balance_window_uses_surrounding_balances() -> None:
    balances = [
        ('final_opening_balance', dt.date(2024, 1, 1)),
        ('final_closing_balance', dt.date(2024, 1, 31)),
        ('final_opening_balance', dt.date(2024, 2, 1)),
        ('final_closing_balance', dt.date(2024, 2, 29)),
    ]
    assert dates.balance_window(balances, dt.date(2024, 1, 15)) == (
        dt.date(2024, 1, 1),
        dt.date(2024, 1, 31),
    )
    assert dates.balance_window(balances, dt.date(2024, 2, 10)) == (
        dt.date(2024, 2, 1),
        dt.date(2024, 2, 29),
    )


def test_balance_window_rejects_implausible_span() -> None:
    balances = [
        ('final_opening_balance', dt.date(2003, 10, 2)),
        ('final_closing_balance', dt.date(2002, 3, 25)),
    ]
    assert dates.balance_window(balances, dt.date(2003, 10, 20)) == (
        dt.date(2003, 10, 2),
        None,
    )
    # Here the closing precedes the value date, so only the opening side is
    # known and the inconsistent closing is not used.


def test_statement_window_falls_back_to_balance_dates() -> None:
    transactions = mt940.parse(
        ':20:REF\n'
        ':60F:C240101EUR1,00\n'
        ':61:2401010101C1,00NTRFREF\n'
        ':62F:C240102EUR2,00\n'
    )
    assert dates.statement_window(transactions.data) == (
        dt.date(2024, 1, 1),
        dt.date(2024, 1, 2),
    )


def test_parse_records_literal_basis_and_status() -> None:
    transactions = mt940.parse(
        ':20:REF\n'
        ':60F:C240101EUR10,00\n'
        ':61:2401050104C1,00NTRFREF\n'
        ':62F:C240106EUR11,00\n'
    )
    data = transactions[0].data
    assert data['entry_date'] == dt.date(2024, 1, 4)
    assert data['entry_date_raw'] == '0104'
    assert data['entry_date_basis'] == 'statement_window'
    assert data['entry_date_status'] == 'resolved'


def test_parse_keeps_ambiguous_candidates() -> None:
    # An opening balance alone leaves the upper bound open. The January
    # literal is plausible in both the past year and the current one.
    transactions = mt940.parse(
        ':20:REF\n:60F:C230101EUR10,00\n:61:2406010101C1,00NTRFREF\n'
    )
    data = transactions[0].data
    assert data['entry_date'] is None
    assert data['guessed_entry_date'] is None
    assert data['entry_date_status'] == 'ambiguous'
    assert data['entry_date_candidates'] == [
        dt.date(2024, 1, 1),
        dt.date(2025, 1, 1),
    ]
    assert 'entry_date_explanation' in data


def test_parse_keeps_invalid_literal() -> None:
    transactions = mt940.parse(
        ':20:REF\n'
        ':60F:C240101EUR10,00\n'
        ':61:2401020230C1,00NTRFREF\n'
        ':62F:C240103EUR11,00\n'
    )
    data = transactions[0].data
    assert data['entry_date'] is None
    assert data['entry_date_raw'] == '0230'
    assert data['entry_date_status'] == 'invalid'
    assert data['entry_date_basis'] == 'invalid_month_day'


def test_parse_accepts_bank_rules() -> None:
    transactions = mt940.parse(
        ':20:REF\n'
        ':60F:C240101EUR10,00\n'
        ':61:2401010101C1,00NTRFREF\n'
        ':62F:C240602EUR11,00\n',
        entry_date_rules=mt940.EntryDateRules(max_days_from_value=200),
    )
    data = transactions[0].data
    assert data['entry_date'] == dt.date(2024, 1, 1)
    assert data['entry_date_basis'] == 'statement_window+bank_rule'


def test_collection_without_rules_pickles_and_unpickles() -> None:
    transactions = mt940.models.Transactions()
    restored = mt940.models.Transactions.__new__(mt940.models.Transactions)
    restored.__setstate__(transactions.__getstate__())
    assert restored.entry_date_rules is None
    restored.parse(
        ':20:REF\n'
        ':60F:C240101EUR10,00\n'
        ':61:2401010101C1,00NTRFREF\n'
        ':62F:C240102EUR11,00\n'
    )
    assert restored[0].data['entry_date'] == dt.date(2024, 1, 1)


def test_window_before_and_after_rules_are_separate() -> None:
    before = mt940.EntryDateRules(max_days_before_value=2)
    after = mt940.EntryDateRules(max_days_after_value=2)
    assert (
        dates.resolve_entry_date(
            '0101', dt.date(2024, 1, 5), rules=before
        ).status
        == 'outside_bank_rule'
    )
    assert dates.resolve_entry_date(
        '0103', dt.date(2024, 1, 1), rules=after
    ).date == dt.date(2024, 1, 3)


def test_basis_ignores_window_without_rule() -> None:
    resolution = dates.resolve_entry_date(
        '0102',
        dt.date(2024, 1, 1),
        window=(dt.date(2024, 1, 1), dt.date(2024, 1, 2)),
    )
    assert resolution.basis == 'statement_window'


def test_basis_without_any_filter_is_value_date_year() -> None:
    resolution = dates.resolve_entry_date('0102', dt.date(2024, 1, 1))
    assert resolution.basis == 'value_date_year'


def test_window_metadata_without_opening_or_closing_keys() -> None:
    transactions = mt940.parse(
        ':20:REF\n:64:C240101EUR10,00\n:61:2401010101C1,00NTRFREF\n'
    )
    assert dates.statement_window(transactions.data) == (
        dt.date(2024, 1, 1),
        dt.date(2024, 1, 1),
    )


def test_window_metadata_with_non_date_balance() -> None:
    assert dates.statement_window({'closing_balance': object()}) == (
        None,
        None,
    )


def test_statement_window_ignores_other_metadata() -> None:
    assert dates.statement_window({'transaction_reference': 'REF'}) == (
        None,
        None,
    )


def test_one_sided_window_keeps_plausible_year() -> None:
    resolution = dates.resolve_entry_date(
        '0608',
        dt.date(2012, 6, 7),
        window=(None, dt.date(2012, 6, 8)),
    )
    assert resolution.date == dt.date(2012, 6, 8)
    assert resolution.basis == 'statement_window'


def test_missing_value_date_is_invalid_but_keeps_literal() -> None:
    resolution = dates.resolve_entry_date('0102', None)
    assert resolution.status == 'invalid'
    assert resolution.basis == 'missing_value_date'
    assert resolution.raw == '0102'


def test_direct_tag_call_without_balance_index_uses_metadata() -> None:
    transactions = mt940.models.Transactions()
    transactions.data['closing_balance'] = mt940.models.Balance(
        status='C',
        amount=mt940.models.Amount('10,00', 'C', 'EUR'),
        date=mt940.models.Date(2024, 1, 1),
    )
    transactions.__dict__.pop('_balance_dates')
    statement = mt940.tags.Statement()
    result = statement(
        transactions,
        {
            'amount': '1,00',
            'status': 'C',
            'year': 2024,
            'month': 1,
            'day': 1,
            'entry_month': 1,
            'entry_day': 1,
        },
    )
    assert result['entry_date'] == dt.date(2024, 1, 1)
    assert result['entry_date_basis'] == 'statement_window'


def test_blank_entry_components_clear_previous_evidence() -> None:
    transactions = mt940.models.Transactions()
    statement = mt940.tags.Statement()
    initial = statement(
        transactions,
        {
            'amount': '1,00',
            'status': 'C',
            'year': 2024,
            'month': 1,
            'day': 1,
            'entry_month': 1,
            'entry_day': 2,
        },
    )
    assert 'entry_date_raw' in initial
    cleared = statement(
        transactions,
        {
            'amount': '1,00',
            'status': 'C',
            'year': 2024,
            'month': 1,
            'day': 1,
            'entry_date_raw': '0102',
            'entry_month': '  ',
            'entry_day': '  ',
        },
    )
    assert 'entry_date' not in cleared
    assert 'entry_date_raw' not in cleared


def test_rules_window_one_sided_overrides_statement_bound() -> None:
    rules = mt940.EntryDateRules(window_start=dt.date(2024, 2, 1))
    resolution = dates.resolve_entry_date(
        '0105',
        dt.date(2024, 1, 1),
        window=(dt.date(2024, 1, 1), dt.date(2024, 3, 1)),
        rules=rules,
    )
    assert resolution.status == 'outside_window'


def test_rules_window_end_only() -> None:
    rules = mt940.EntryDateRules(window_end=dt.date(2024, 1, 31))
    resolution = dates.resolve_entry_date(
        '0120', dt.date(2024, 1, 1), rules=rules
    )
    assert resolution.date == dt.date(2024, 1, 20)


def test_no_balance_dates_in_direct_tag_call() -> None:
    transactions = mt940.models.Transactions()
    statement = mt940.tags.Statement()
    result = statement(
        transactions,
        {
            'amount': '1,00',
            'status': 'C',
            'year': '2024',
            'month': '1',
            'day': '1',
            'entry_month': '01',
            'entry_day': '01',
        },
    )
    assert result['entry_date'] == dt.date(2024, 1, 1)
    assert result['entry_date_basis'] == 'value_date_year'


def test_ambiguous_legacy_resolution_is_reported() -> None:
    # Two nearby January dates fit for a June value date, but no window or
    # rule narrows them: the result must list both instead of choosing one.
    resolution = dates.resolve_entry_date('0101', dt.date(2024, 6, 1))
    assert resolution.status == 'ambiguous'
    assert resolution.basis == 'ambiguous'
    assert resolution.candidates == (
        dt.date(2024, 1, 1),
        dt.date(2025, 1, 1),
    )


def test_balance_scan_records_parseable_balances() -> None:
    transactions = mt940.parse(
        ':20:REF\n'
        ':60F:C240101EUR10,00\n'
        ':61:2401010101C1,00NTRFREF\n'
        ':62F:C240102EUR11,00\n'
    )
    assert transactions._balance_dates == [
        ('final_opening_balance', dt.date(2024, 1, 1)),
        ('final_closing_balance', dt.date(2024, 1, 2)),
    ]


class _RejectingBalance(mt940.tags.OpeningBalance):
    """Balance tag whose capture always fails, as an unparsable value would."""

    def parse(
        self, transactions: mt940.models.Transactions, value: str
    ) -> dict[str, str | None]:
        del transactions, value
        msg = 'rejected by the test'
        raise RuntimeError(msg)


def test_balance_scan_skips_unparseable_balance() -> None:
    transactions = mt940.models.Transactions(tags={'60F': _RejectingBalance()})
    with pytest.raises(RuntimeError, match='rejected by the test'):
        transactions.parse(':20:REF\n:60F:C240101EUR10,00\n')
    # The pre-scan ignored the failing balance before conversion started.
    assert transactions._balance_dates == []


def test_entry_date_rules_survive_pickle_round_trip() -> None:
    import pickle

    rules = mt940.EntryDateRules(max_days_from_value=10)
    transactions = mt940.models.Transactions(entry_date_rules=rules)
    restored = pickle.loads(pickle.dumps(transactions))
    assert restored.entry_date_rules == rules


def test_parse_statements_passes_rules_to_every_block() -> None:
    source = (
        ':20:FIRST\n'
        ':60F:C240101EUR10,00\n'
        ':61:2401010101C1,00NTRFREF\n'
        ':62F:C240102EUR11,00\n'
        ':20:SECOND\n'
        ':60F:C240201EUR10,00\n'
        ':61:2402010101C1,00NTRFREF\n'
        ':62F:C240202EUR11,00\n'
    )
    rules = mt940.EntryDateRules(max_days_from_value=10)
    statements = mt940.parse_statements(source, entry_date_rules=rules)
    assert [statement.entry_date_rules for statement in statements] == [
        rules,
        rules,
    ]
    assert statements[0][0].data['entry_date_status'] == 'resolved'
    assert (
        statements[0][0].data['entry_date_basis']
        == 'statement_window+bank_rule'
    )
