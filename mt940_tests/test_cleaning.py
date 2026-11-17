"""Cleaning keeps each source direction and an exact, queryable net.

The cleaning layer never re-signs the parsed ``Amount``. It derives the
canonical direction from the ``:61:`` mark, retains the original signed amount
for each occurrence, totals each currency exactly, and leaves an offsetting
pair visible unless the two entries share a business reference.
"""

import decimal
from decimal import Decimal, localcontext

import pytest

import mt940

_HEADER = ':20:REF\n:25:ACC\n'


def _transaction(
    mark: str,
    amount: str,
    reference: str = 'SHARED',
) -> str:
    return f':61:2601020101{mark}{amount}NTRF{reference}//BANK-{reference}\n'


def _statement(*lines: str, closing: str = '0,00') -> str:
    return (
        _HEADER
        + f':60F:C260101EUR{closing}\n'
        + ''.join(lines)
        + f':62F:C260102EUR{closing}\n'
    )


@pytest.mark.parametrize(
    ('mark', 'original', 'canonical', 'expected'),
    [
        ('C', 1, 1, '5.00'),
        ('D', -1, -1, '-5.00'),
        # A reversed credit leaves the account, but the source mark still
        # names a credit.
        ('RC', 1, -1, '-5.00'),
        # A reversed debit enters the account, but the source mark still
        # names a debit.
        ('RD', -1, 1, '5.00'),
        # The parser does not sign lowercase marks by default. Cleaning reads
        # them case-insensitively without altering the stored status.
        ('rc', 1, -1, '-5.00'),
        ('rd', -1, 1, '5.00'),
    ],
)
def test_each_mark_keeps_original_and_canonical_direction(
    mark: str,
    original: int,
    canonical: int,
    expected: str,
) -> None:
    transactions = mt940.parse(_statement(_transaction(mark, '5,00')))
    cleaned = mt940.clean(transactions)
    assert len(cleaned.occurrences) == 1
    entry = cleaned.occurrences[0]
    assert entry.status == mark
    assert entry.original_direction == original
    assert entry.direction == canonical
    assert entry.net_amount == Decimal(expected)
    assert entry.original_amount == transactions[0].data['amount'].amount
    assert entry.transaction is transactions[0]


@pytest.mark.parametrize(
    ('options', 'stored'),
    [
        (mt940.Options(), '5.00'),
        (mt940.Options(reversal_sign=True), '-5.00'),
        (mt940.Options.all(), '-5.00'),
    ],
)
def test_parsed_amount_is_not_negated_twice(
    options: mt940.Options, stored: str
) -> None:
    # ``reversal_sign`` can store an RC as -5.00. Cleaning must still yield a
    # single debit of -5.00 rather than flipping it back to +5.00.
    transactions = mt940.parse(
        _statement(_transaction('RC', '5,00')), options=options
    )
    assert transactions[0].data['amount'].amount == Decimal(stored)
    entry = mt940.clean(transactions).occurrences[0]
    assert entry.original_amount == Decimal(stored)
    assert entry.net_amount == Decimal('-5.00')


def test_plain_debit_is_not_negated_twice() -> None:
    transactions = mt940.parse(_statement(_transaction('D', '12,50')))
    assert transactions[0].data['amount'].amount == Decimal('-12.50')
    entry = mt940.clean(transactions).occurrences[0]
    assert entry.original_amount == Decimal('-12.50')
    assert entry.net_amount == Decimal('-12.50')


@pytest.mark.parametrize('mark', ['C', 'D', 'RC', 'RD'])
def test_amounts_keep_every_input_digit(mark: str) -> None:
    transactions = mt940.parse(
        _statement(_transaction(mark, '1,2345')),
        options=mt940.Options.all(),
    )
    entry = mt940.clean(transactions).occurrences[0]
    assert entry.net_amount.as_tuple().exponent == -4
    assert abs(entry.net_amount) == Decimal('1.2345')


def test_net_total_ignores_the_decimal_context() -> None:
    # Negation in ``Amount`` uses the active context and can round; cleaning
    # reads the stored digits and totals them exactly.
    transactions = mt940.parse(
        _statement(
            _transaction('D', '12345,67'),
            _transaction('C', '0,03'),
        )
    )
    with localcontext() as context:
        context.prec = 4
        cleaned = mt940.clean(transactions)
    assert cleaned.net_amount('EUR') == Decimal('-12345.64')
    assert str(cleaned.net_amount('EUR')) == '-12345.64'


def _collection(
    *entries: tuple[str, str, str | None],
) -> mt940.models.Transactions:
    """Build transactions directly when a fixture needs several currencies.

    Returns:
        A collection holding one transaction per supplied mark, amount and
        currency tuple.
    """
    transactions = mt940.models.Transactions()
    for status, amount, currency in entries:
        transactions.transactions.append(
            mt940.models.Transaction(
                transactions,
                {
                    'amount': mt940.models.Amount(amount, status, currency),
                    'status': status,
                },
            )
        )
    return transactions


def test_currencies_are_totalled_separately() -> None:
    cleaned = mt940.clean(
        _collection(
            ('C', '10,00', 'USD'),
            ('D', '3,00', 'EUR'),
            ('RC', '1,00', 'EUR'),
        )
    )
    assert cleaned.net_amounts == {
        'USD': Decimal('10.00'),
        'EUR': Decimal('-4.00'),
    }
    assert cleaned.net_amount('USD') == Decimal('10.00')
    assert cleaned.net_amount('EUR') == Decimal('-4.00')
    assert cleaned.net_amount('GBP') == Decimal(0)
    assert [entry.currency for entry in cleaned.occurrences_for('USD')] == [
        'USD'
    ]


def test_association_keys_are_compared_per_currency_and_magnitude() -> None:
    transactions = _collection(
        ('C', '10,00', 'USD'),
        ('RC', '10,00', 'EUR'),
    )
    for transaction in transactions:
        transaction.data['customer_reference'] = 'SHARED'
    # The shared customer reference must not pair two different currencies.
    cleaned = mt940.clean(transactions)
    assert cleaned.reversals == ()
    assert cleaned.net_amounts == {
        'USD': Decimal('10.00'),
        'EUR': Decimal('-10.00'),
    }


def test_associated_pair_is_offset_and_still_queryable() -> None:
    transactions = mt940.parse(
        _statement(
            _transaction('C', '10,00'),
            _transaction('RC', '10,00'),
        ),
        options=mt940.Options.all(),
    )
    cleaned = mt940.clean(transactions)
    assert [entry.status for entry in cleaned.occurrences] == ['C', 'RC']
    assert cleaned.effective == ()
    assert cleaned.net_amount('EUR') == Decimal('0.00')
    assert len(cleaned.associated_reversals) == 1
    assert cleaned.unassociated_reversals == ()
    pair = cleaned.associated_reversals[0]
    assert pair.associated
    assert pair.first.status == 'C'
    assert pair.second.status == 'RC'
    assert pair.association_key == 'customer_reference'
    assert pair.association_value == 'SHARED'


def test_missing_business_link_keeps_both_entries() -> None:
    source = _statement(
        ':61:2601020101C10,00NTRF\n',
        ':61:2601020101RC10,00NTRF\n',
    )
    transactions = mt940.parse(source, options=mt940.Options.all())
    cleaned = mt940.clean(transactions)
    assert [entry.status for entry in cleaned.occurrences] == ['C', 'RC']
    assert [entry.status for entry in cleaned.effective] == ['C', 'RC']
    assert cleaned.net_amount('EUR') == Decimal('0.00')
    assert cleaned.associated_reversals == ()
    assert len(cleaned.unassociated_reversals) == 1
    pair = cleaned.unassociated_reversals[0]
    assert pair.first.status == 'C'
    assert pair.second.status == 'RC'
    assert pair.association_key is None
    assert pair.association_value is None


def test_plain_debit_and_reversed_debit_without_link_stay_visible() -> None:
    source = _statement(
        ':61:2601020101D7,25NTRF\n',
        ':61:2601020101RD7,25NTRF\n',
    )
    cleaned = mt940.clean(mt940.parse(source, options=mt940.Options.all()))
    assert len(cleaned.unassociated_reversals) == 1
    assert [entry.net_amount for entry in cleaned.effective] == [
        Decimal('-7.25'),
        Decimal('7.25'),
    ]


def test_associated_debit_and_reversed_debit_are_offset() -> None:
    source = _statement(
        ':61:2601020101D7,25NTRF\n',
        ':86:105?20SVWZ+Charge?21LINK-1\n',
        ':61:2601020101RD7,25NTRF\n',
        ':86:105?20SVWZ+Charge?21LINK-1\n',
    )
    transactions = mt940.parse(source, options=mt940.Options.all())
    # Structured ``:86:`` content is decoded, so the shared purpose text is
    # available under ``purpose`` rather than the raw details.
    assert transactions[0].data['purpose'] == 'ChargeLINK-1'
    cleaned = mt940.clean(
        transactions,
        association_keys=('purpose',),
    )
    assert cleaned.associated_reversals[0].association_key == 'purpose'
    assert cleaned.effective == ()
    assert cleaned.net_amount('EUR') == Decimal('0.00')


def test_two_ordinary_marks_never_form_a_reversal_pair() -> None:
    source = _statement(
        _transaction('C', '4,00'),
        _transaction('C', '4,00'),
    )
    cleaned = mt940.clean(mt940.parse(source, options=mt940.Options.all()))
    assert cleaned.reversals == ()
    assert cleaned.net_amount('EUR') == Decimal('8.00')


def test_pairing_prefers_an_associated_candidate() -> None:
    source = _statement(
        ':61:2601020101C10,00NTRFOTHER//BANK-OTHER\n',
        ':61:2601020101C10,00NTRFLINKED//BANK-LINKED\n',
        ':61:2601020101RC10,00NTRFLINKED//BANK-LINKED\n',
    )
    cleaned = mt940.clean(mt940.parse(source, options=mt940.Options.all()))
    assert len(cleaned.associated_reversals) == 1
    pair = cleaned.associated_reversals[0]
    assert pair.first.index == 1
    assert pair.second.index == 2
    # Only the linked pair is offset; the unrelated credit remains.
    assert [entry.index for entry in cleaned.effective] == [0]


def test_unassociated_pair_is_used_after_associated_matching() -> None:
    source = _statement(
        ':61:2601020101C10,00NTRFA//BANK-A\n',
        ':61:2601020101C10,00NTRFB//BANK-B\n',
        ':61:2601020101RC10,00NTRFA//BANK-A\n',
        ':61:2601020101RC10,00NTRFC//BANK-C\n',
    )
    cleaned = mt940.clean(mt940.parse(source, options=mt940.Options.all()))
    assert len(cleaned.associated_reversals) == 1
    assert len(cleaned.unassociated_reversals) == 1
    assert [entry.index for entry in cleaned.effective] == [1, 3]


def test_empty_association_keys_disable_linking() -> None:
    source = _statement(_transaction('C', '2,00'), _transaction('RC', '2,00'))
    cleaned = mt940.clean(
        mt940.parse(source, options=mt940.Options.all()),
        association_keys=(),
    )
    assert cleaned.associated_reversals == ()
    assert len(cleaned.unassociated_reversals) == 1


def test_transactions_clean_method_matches_the_function() -> None:
    source = _statement(_transaction('C', '3,00'), _transaction('RC', '3,00'))
    transactions = mt940.parse(source)
    assert (
        transactions.clean().net_amounts
        == mt940.clean(transactions).net_amounts
    )


def test_source_objects_are_left_untouched() -> None:
    source = _statement(_transaction('D', '9,99'))
    transactions = mt940.parse(source)
    before = dict(transactions[0].data)
    cleaned = mt940.clean(transactions)
    assert transactions[0].data == before
    assert cleaned.occurrences[0].transaction is transactions[0]


@pytest.mark.parametrize(
    ('status', 'message'),
    [('X', 'Unknown debit/credit mark'), ('', 'no debit/credit mark')],
)
def test_unknown_or_missing_mark_is_rejected(
    status: str, message: str
) -> None:
    transaction = mt940.models.Transaction(
        mt940.models.Transactions(),
        {
            'amount': mt940.models.Amount('1,00', 'C', 'EUR'),
            'status': status,
        },
    )
    with pytest.raises(ValueError, match=message):
        mt940.clean([transaction])


def test_non_transaction_is_rejected() -> None:
    with pytest.raises(TypeError, match='not a Transaction'):
        mt940.clean([object()])  # type: ignore[list-item]


def test_transaction_without_an_amount_is_rejected() -> None:
    transaction = mt940.models.Transaction(
        mt940.models.Transactions(), {'status': 'C'}
    )
    with pytest.raises(TypeError, match='no Amount'):
        mt940.clean([transaction])


def test_non_decimal_amount_is_rejected() -> None:
    amount = mt940.models.Amount('1,00', 'C', 'EUR')
    amount.amount = '1.00'  # type: ignore[assignment]
    transaction = mt940.models.Transaction(
        mt940.models.Transactions(),
        {'amount': amount, 'status': 'C'},
    )
    with pytest.raises(TypeError, match='not a Decimal'):
        mt940.clean([transaction])


def test_non_finite_amount_is_rejected() -> None:
    amount = mt940.models.Amount('1,00', 'C', 'EUR')
    amount.amount = Decimal('NaN')
    transaction = mt940.models.Transaction(
        mt940.models.Transactions(),
        {'amount': amount, 'status': 'C'},
    )
    with pytest.raises(ValueError, match='non-finite'):
        mt940.clean([transaction])


def test_non_string_association_key_is_rejected() -> None:
    with pytest.raises(TypeError, match='not a string'):
        mt940.clean([], association_keys=(1,))  # type: ignore[arg-type]


def test_empty_input_has_a_zero_net() -> None:
    cleaned = mt940.clean([])
    assert cleaned.occurrences == ()
    assert cleaned.effective == ()
    assert cleaned.net_amounts == {}
    assert cleaned.net_amount('EUR') == Decimal(0)


def test_cleaning_is_exposed_on_the_package() -> None:
    assert mt940.clean is mt940.cleaning.clean
    assert mt940.Cleaning is mt940.cleaning.Cleaning
    assert mt940.Direction.CREDIT == 1
    assert mt940.Direction.DEBIT == -1


def test_signaling_nan_is_rejected_without_context_rounding() -> None:
    # A signaling NaN must be reported as non-finite rather than parsed or
    # rounded through the active decimal context.
    amount = mt940.models.Amount('0,00', 'C', 'EUR')
    amount.amount = Decimal('sNaN')
    transaction = mt940.models.Transaction(
        mt940.models.Transactions(),
        {'amount': amount, 'status': 'C'},
    )
    with pytest.raises(ValueError, match='non-finite'):
        mt940.clean([transaction])


def test_cleaned_entry_exposes_reversal_flag_and_data() -> None:
    transactions = mt940.parse(
        _statement(_transaction('C', '1,00'), _transaction('RD', '1,00'))
    )
    credit, reversed_debit = mt940.clean(transactions).occurrences
    assert credit.is_reversal is False
    assert reversed_debit.is_reversal is True
    assert credit.data is credit.transaction.data


def test_exact_sum_preserves_the_smallest_exponent() -> None:
    exact_sum = mt940.cleaning._exact_sum
    # A lower exponent arriving second widens the running total.
    assert exact_sum([Decimal('1.5'), Decimal('2.25')]) == Decimal('3.75')
    # A higher exponent arriving second is scaled to the running exponent.
    assert exact_sum([Decimal('1.25'), Decimal('2.5')]) == Decimal('3.75')
    assert exact_sum([]) == Decimal(0)


def test_exact_sum_rejects_non_finite_values_directly() -> None:
    with pytest.raises(ValueError, match='non-finite'):
        mt940.cleaning._exact_sum([Decimal('Infinity')])


def test_association_search_skips_a_non_reversal_candidate() -> None:
    # The first credit in the magnitude group is an ordinary credit, so the
    # associated search must continue to the RC behind it.
    source = _statement(
        ':61:2601020101C10,00NTRFOTHER//BANK-OTHER\n',
        ':61:2601020101C10,00NTRFLINK//BANK-LINK\n',
        ':61:2601020101RC10,00NTRFLINK//BANK-LINK\n',
    )
    cleaned = mt940.clean(mt940.parse(source, options=mt940.Options.all()))
    pair = cleaned.associated_reversals[0]
    assert pair.first.index == 1
    assert [entry.index for entry in cleaned.effective] == [0]


def test_associated_search_returns_none_without_a_reversal_candidate() -> None:
    # A plain debit and a plain credit of the same magnitude are not a
    # reversal pair, so the search reaches the end and reports no match.
    source = _statement(
        ':61:2601020101D6,00NTRFONE//BANK-ONE\n',
        ':61:2601020101C6,00NTRFTWO//BANK-TWO\n',
    )
    cleaned = mt940.clean(mt940.parse(source, options=mt940.Options.all()))
    assert cleaned.reversals == ()
    assert len(cleaned.effective) == 2


def test_decimal_infinity_is_rejected_when_totalling() -> None:
    transactions = mt940.models.Transactions()
    transaction = mt940.models.Transaction(
        transactions,
        {
            'amount': mt940.models.Amount('1,00', 'C', None),
            'status': 'C',
        },
    )
    transactions.transactions.append(transaction)
    # Mutate after construction so only the summing path sees the value.
    amount = transaction.data['amount']
    assert isinstance(amount, mt940.models.Amount)
    amount.amount = decimal.Decimal('Infinity')
    with pytest.raises(ValueError, match='non-finite'):
        mt940.clean(transactions)
