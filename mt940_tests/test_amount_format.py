"""Explicit bank amount conventions and lossless source text.

The amount grammar must come from the import configuration, never from the
literal's magnitude. These tests pin strict rejection, the exact original text
and the guarantee that switching configuration never rescales an old result.
"""

from __future__ import annotations

import decimal
import pickle
from collections.abc import Callable
from typing import Any

import mt940
import pytest


def _statement(
    amount: str, currency: str = 'EUR', zero: str = '0,00'
) -> str:
    return (
        ':20:AMOUNTS\n'
        f':60F:C260101{currency}{zero}\n'
        f':61:260102C{amount}NTRFREF\n'
        f':62F:C260102{currency}{amount}\n'
    )


def test_configured_comma_format_reads_grouping_and_raw_text() -> None:
    amount_format = mt940.AmountFormat(decimal=',', thousands='.')
    statement = mt940.parse(
        _statement('1.234,56'),
        options=mt940.Options(amount_format=amount_format),
    )
    amount: mt940.models.Amount = statement[0].data['amount']
    assert amount.amount == decimal.Decimal('1234.56')
    assert amount.raw == '1.234,56'
    assert amount.original == amount.source == amount.literal == '1.234,56'
    assert amount.amount_format == amount_format
    assert statement.data['final_closing_balance'].amount.raw == '1.234,56'


def test_configured_dot_format_reads_grouping_and_raw_text() -> None:
    statement = mt940.parse(
        _statement('1,234.56', zero='0.00'),
        options=mt940.Options(
            amount_format=mt940.AmountFormat(decimal='.', thousands=',')
        ),
    )
    amount: mt940.models.Amount = statement[0].data['amount']
    assert amount.amount == decimal.Decimal('1234.56')
    assert amount.raw == '1,234.56'


@pytest.mark.parametrize(
    ('amount_format', 'literal'),
    [
        (mt940.AmountFormat(decimal=',', thousands='.'), '1,234.56'),
        (mt940.AmountFormat(decimal='.', thousands=','), '1.234,56'),
        (mt940.AmountFormat(decimal=','), '1.234'),
        (mt940.AmountFormat(decimal='.'), '1,234'),
        (mt940.AmountFormat(decimal=','), '1.234,56'),
        (mt940.AmountFormat(decimal=','), '1 234,56'),
    ],
)
def test_wrong_separator_is_rejected_not_guessed(
    amount_format: mt940.AmountFormat, literal: str
) -> None:
    with pytest.raises(mt940.AmountFormatError) as error:
        _ = amount_format.parse(literal)
    assert literal in str(error.value)
    assert 'configured amount format' in str(error.value)


def test_wrong_separator_in_source_is_rejected_by_the_import() -> None:
    options = mt940.Options(amount_format=mt940.AmountFormat(',', '.'))
    with pytest.raises(mt940.AmountFormatError):
        _ = mt940.parse(_statement('1,234.56'), options=options)


def test_decimal_places_are_enforced_when_configured() -> None:
    exact = mt940.AmountFormat(decimal=',', decimal_places=2)
    assert exact.parse('12,30') == decimal.Decimal('12.30')
    with pytest.raises(mt940.AmountFormatError, match='expected 2'):
        _ = exact.parse('12,3')


def test_empty_fraction_remains_accepted() -> None:
    # MT940's 15d allows a trailing decimal separator with no fraction.
    assert mt940.AmountFormat(decimal=',').parse('10,') == decimal.Decimal(
        '10'
    )
    assert mt940.parse(_statement('10,')).data['final_closing_balance']


def test_default_without_configuration_remains_permissive() -> None:
    # Existing integrations did not state a bank convention, so the legacy
    # comma-or-point handling remains the default.
    assert mt940.models.Amount('1,00', 'C', 'EUR').amount == decimal.Decimal(
        '1.00'
    )
    assert mt940.models.Amount('1.00', 'C', 'EUR').amount == decimal.Decimal(
        '1.00'
    )


def test_switching_options_cannot_rescale_an_existing_result() -> None:
    comma_options = mt940.Options(
        amount_format=mt940.AmountFormat(decimal=',', thousands='.')
    )
    original = mt940.parse(_statement('1.234,56'), options=comma_options)
    first: mt940.models.Amount = original[0].data['amount']
    assert first.amount == decimal.Decimal('1234.56')

    # A different bank convention applied to the same text would mean 1.23456,
    # but an existing parsed result keeps its own version and must not be
    # cleaned again by the new configuration.
    dot_options = mt940.Options(
        amount_format=mt940.AmountFormat(decimal='.', thousands=',')
    )
    copied = mt940.models.Amount(first, 'C', 'EUR', options=dot_options)
    assert copied.amount == decimal.Decimal('1234.56')
    assert copied.raw == '1.234,56'
    assert copied.amount_format == first.amount_format

    # Reconstructing a model from the already-parsed amount also retains it.
    balance = mt940.models.Balance(
        'C', first, amount_format=dot_options.amount_format
    )
    amount = balance.amount
    assert isinstance(amount, mt940.models.Amount)
    assert amount.amount == decimal.Decimal('1234.56')


def test_canonical_decimal_is_not_reparsed_as_grouped_text() -> None:
    thousands_dot = mt940.AmountFormat(decimal=',', thousands='.')
    amount = mt940.models.Amount(
        decimal.Decimal('1234.56'),
        'C',
        'EUR',
        amount_format=thousands_dot,
    )
    assert amount.amount == decimal.Decimal('1234.56')
    assert amount.raw == '1234.56'
    assert amount.canonical == decimal.Decimal('1234.56')


def test_decimal_input_still_applies_its_status_mark() -> None:
    amount = mt940.models.Amount(decimal.Decimal('12.34'), 'D', 'EUR')
    assert amount.amount == decimal.Decimal('-12.34')
    assert amount.raw == '12.34'


def test_format_is_snapshotted_and_pickled_with_the_result() -> None:
    options = mt940.Options(amount_format=mt940.AmountFormat(',', '.'))
    amount: mt940.models.Amount = mt940.parse(
        _statement('2.500,00'), options=options
    )[0].data['amount']
    restored: mt940.models.Amount = pickle.loads(pickle.dumps(amount))
    assert restored.raw == '2.500,00'
    assert restored.amount == decimal.Decimal('2500.00')
    assert restored.amount_format == amount.amount_format


def test_old_pickled_amount_state_is_upgraded_losslessly() -> None:
    # State written before source metadata existed has only amount/currency.
    amount = mt940.models.Amount.__new__(mt940.models.Amount)
    amount.__setstate__({
        'amount': decimal.Decimal('-12.34'),
        'currency': 'EUR',
    })
    assert amount.amount == decimal.Decimal('-12.34')
    assert amount.currency == 'EUR'
    assert amount.raw == '-12.34'
    assert amount.amount_format is None


def test_old_options_state_defaults_the_new_configuration() -> None:
    options = mt940.Options.__new__(mt940.Options)
    options.__setstate__(tuple([False] * 10))
    assert options.amount_format is None
    assert not options.reversal_sign
    assert len(options.names()) == 10
    assert len(options.fields()) == 11


def test_options_all_enables_only_boolean_switches() -> None:
    options = mt940.Options.all()
    assert options.amount_format is None
    assert all(getattr(options, name) for name in mt940.Options.names())
    assert len(mt940.Options.names()) == 10
    assert mt940.Options.switches() == mt940.Options.names()


@pytest.mark.parametrize(
    ('kwargs', 'expected_decimal', 'expected_thousands'),
    [
        ({'decimal_separator': ','}, ',', None),
        ({'decimal': ','}, ',', None),
        ({'thousands_separator': '.'}, ',', '.'),
        ({'thousands': '.'}, ',', '.'),
        ({'grouping_separator': '.'}, ',', '.'),
    ],
    ids=[
        'decimal_separator',
        'decimal',
        'thousands_separator',
        'thousands',
        'grouping',
    ],
)
def test_amount_format_constructor_aliases(
    kwargs: dict[str, str],
    expected_decimal: str,
    expected_thousands: str | None,
) -> None:
    amount_format = mt940.AmountFormat(**kwargs)  # type: ignore[arg-type]
    assert amount_format.decimal_separator == expected_decimal
    assert amount_format.thousands_separator == expected_thousands


@pytest.mark.parametrize(
    'kwargs',
    [
        {'decimal': ',', 'decimal_mark': ','},
        {'decimal_separator': ',', 'thousands_separator': ','},
        {'decimal_separator': ',,'},
        {'decimal_separator': '1'},
        {'decimal_places': -1},
        {'unknown': True},
    ],
    ids=[
        'duplicate-alias',
        'identical-separators',
        'long-separator',
        'digit-separator',
        'negative-places',
        'unknown-option',
    ],
)
def test_invalid_amount_format_configuration(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises((mt940.AmountFormatError, TypeError)):
        _ = mt940.AmountFormat(**kwargs)  # type: ignore[arg-type]


def test_options_accepts_explicit_amount_format() -> None:
    amount_format = mt940.AmountFormat(decimal=',', thousands='.')
    options = mt940.Options(amount_format=amount_format)
    assert options.amount_format is amount_format


def test_options_rejects_other_amount_format_types() -> None:
    with pytest.raises(
        TypeError, match='amount_format must be an AmountFormat'
    ):
        _ = mt940.Options(amount_format='.')  # type: ignore[arg-type]
    with pytest.raises(
        TypeError, match='amount_format must be an AmountFormat'
    ):
        _ = mt940.Options(
            amount_format={',': '.'}  # type: ignore[arg-type]
        )
    with pytest.raises(
        TypeError, match='amount_format must be an AmountFormat'
    ):
        _ = mt940.Options(
            amount_format=(',', '.')  # type: ignore[arg-type]
        )


def test_json_keeps_canonical_value_and_source_text_separate() -> None:
    import json

    amount: mt940.models.Amount = mt940.parse(
        _statement('1.234,56'),
        options=mt940.Options(amount_format=mt940.AmountFormat(',', '.')),
    )[0].data['amount']
    decoded: dict[str, object] = json.loads(
        json.dumps(amount, cls=mt940.JSONEncoder)
    )
    # JSON remains the historical amount/currency shape for compatibility;
    # the source text stays on the Python model.
    assert decoded == {'amount': '1234.56', 'currency': 'EUR'}


@pytest.mark.parametrize(
    ('amount_format', 'literal', 'reason'),
    [
        (mt940.AmountFormat(','), '', 'it is empty'),
        (mt940.AmountFormat(','), '+', 'it has no digits'),
        (mt940.AmountFormat(','), '1,2,3', 'it repeats'),
        (mt940.AmountFormat(None), '1,23', 'format has no decimal separator'),
        (mt940.AmountFormat(','), '1,2a', 'not a digit sequence'),
        (mt940.AmountFormat(','), ',', 'it has no digits'),
        (mt940.AmountFormat('.'), '1a.5', 'disallowed separator'),
        (mt940.AmountFormat('.', ','), '1a.5', 'disallowed separator'),
        (mt940.AmountFormat('.', ','), ',123', 'has an empty group'),
        (
            mt940.AmountFormat('.', ','),
            '1234,567.89',
            'not one to three digits',
        ),
        (mt940.AmountFormat('.', ','), '1,234,56', 'not exactly three digits'),
        (mt940.AmountFormat('.', ','), '1,2a4.56', 'not a digit sequence'),
    ],
)
def test_strict_grammar_reports_the_exact_failure(
    amount_format: mt940.AmountFormat, literal: str, reason: str
) -> None:
    with pytest.raises(mt940.AmountFormatError, match=reason):
        _ = amount_format.parse(literal)


def test_amount_format_parse_rejects_non_text() -> None:
    with pytest.raises(TypeError, match='amount literal must be str'):
        _ = mt940.AmountFormat(',').parse(12)  # type: ignore[arg-type]


def test_amount_format_call_alias_parses() -> None:
    assert mt940.AmountFormat(',').__call__('12,30') == decimal.Decimal(
        '12.30'
    )


def test_legacy_invalid_text_raises_amount_format_error() -> None:
    with pytest.raises(mt940.AmountFormatError, match='legacy decimal text'):
        _ = mt940.AmountFormat.legacy().parse('not money')


def test_amount_format_alias_must_be_text() -> None:
    with pytest.raises(TypeError, match='decimal separator must be text'):
        _ = mt940.AmountFormat(decimal=1)  # type: ignore[arg-type]


def test_options_rejects_unsupported_amount_format_type() -> None:
    with pytest.raises(TypeError, match='amount_format must be'):
        _ = mt940.Options(amount_format=1)  # type: ignore[arg-type]


def test_options_state_without_non_boolean_field_backfills_it() -> None:
    options = mt940.Options.__new__(mt940.Options)
    options.__setstate__(tuple([False] * (len(mt940.Options.names()) - 1)))
    assert options.amount_format is None


def test_options_state_missing_boolean_switches_backfills_them() -> None:
    options = mt940.Options.__new__(mt940.Options)
    options.__setstate__(())
    assert options == mt940.Options()


def test_options_state_with_non_boolean_field_before_the_end() -> None:
    # Covers the non-boolean fallback in __setstate__ itself.
    options = mt940.Options.__new__(mt940.Options)
    options.__setstate__(tuple([False] * (len(mt940.Options.names()) - 2)))
    assert options.amount_format is None


def test_amount_state_without_source_status_upgrades() -> None:
    amount = mt940.models.Amount.__new__(mt940.models.Amount)
    amount.__setstate__({'amount': decimal.Decimal('1.00')})
    assert amount.currency is None
    assert amount.raw == '1.00'


def test_sum_amount_number_is_in_getstate() -> None:
    amount = mt940.models.SumAmount(
        '1,00', 'C', 'EUR', number='2'  # type: ignore[arg-type]
    )
    assert amount.__getstate__()['number'] == '2'


def test_no_decimal_separator_rejects_any_separator() -> None:
    # A format with no decimal separator has no fractional part at all.
    assert mt940.AmountFormat(None).parse('123') == decimal.Decimal('123')
    with pytest.raises(
        mt940.AmountFormatError, match='format has no decimal separator'
    ):
        _ = mt940.AmountFormat(None).parse('1.23')


def test_sum_amount_without_number_state_upgrades_losslessly() -> None:
    amount = mt940.models.SumAmount.__new__(mt940.models.SumAmount)
    amount.__setstate__({
        'amount': decimal.Decimal('-1.00'),
        'currency': 'EUR',
    })
    assert amount.number == ''  # type: ignore[comparison-overlap]
    assert amount.raw == '-1.00'
    assert amount.amount_format is None


def test_options_state_round_trip_preserves_amount_format() -> None:
    options = mt940.Options(amount_format=mt940.AmountFormat(',', '.'))
    restored = mt940.Options.__new__(mt940.Options)
    restored.__setstate__(options.__getstate__())
    assert restored == options


def test_parse_accepts_an_explicit_amount_format() -> None:
    statement = mt940.parse(
        _statement('1.234,56'),
        amount_format=mt940.AmountFormat(',', '.'),
    )
    amount: mt940.models.Amount = statement[0].data['amount']
    assert amount.amount == decimal.Decimal('1234.56')
    assert statement.options.amount_format == mt940.AmountFormat(',', '.')


def test_parse_statements_accepts_an_explicit_amount_format() -> None:
    source = _statement('1.234,56') + _statement('2.000,00')
    statements = mt940.parse_statements(
        source, amount_format=mt940.AmountFormat(',', '.')
    )
    assert len(statements) == 2
    assert statements[1].data['final_closing_balance'].amount.amount == (
        decimal.Decimal('2000.00')
    )


def test_transactions_accepts_an_explicit_amount_format() -> None:
    transactions = mt940.models.Transactions(
        amount_format=mt940.AmountFormat(',', '.')
    )
    _ = transactions.parse(_statement('1.234,56'))
    amount: mt940.models.Amount = transactions[0].data['amount']
    assert amount.amount == decimal.Decimal('1234.56')


@pytest.mark.parametrize(
    'call',
    [
        lambda: mt940.parse(
            _statement('1,00'), amount_format='comma'  # type: ignore[arg-type]
        ),
        lambda: mt940.parse_statements(
            _statement('1,00'), amount_format='comma'  # type: ignore[arg-type]
        ),
        lambda: mt940.models.Transactions(
            amount_format='comma'  # type: ignore[arg-type]
        ),
    ],
    ids=['parse', 'parse_statements', 'transactions'],
)
def test_import_level_amount_format_requires_the_type(
    call: Callable[[], object],
) -> None:
    with pytest.raises(TypeError, match='must be an mt940.AmountFormat'):
        _ = call()
