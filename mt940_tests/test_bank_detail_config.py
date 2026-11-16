"""Explicit bank configurations for structured ``:86:`` details.

These tests pin the config-driven splitter separately from the legacy mapping:
only configured codes open a subfield, unrecognised text and repeats keep their
original position, continuation values never gain a space, and switching to a
bank configuration never reuses another collection's split.
"""

# pyright: reportPrivateUsage=false
# The private split/assemble helpers are the units under test here.

from __future__ import annotations

import pickle
import typing

import mt940
import pytest

if typing.TYPE_CHECKING:
    from mt940.processors import BankDetailConfig

_PREAMBLE = ':20:REF\n:25:ACC\n:28C:1\n'
_OPENING = ':60F:C231229EUR0,00\n'
_HEADER = _PREAMBLE + _OPENING
_FOOTER = ':62F:C231229EUR10,00\n'


def _config() -> BankDetailConfig:
    """Build a small bank layout with a direct, a joined and a custom field."""
    return mt940.processors.BankDetailConfig(
        fields=(
            mt940.processors.DetailField(
                'purpose',
                ('20', '21', '22', '23', '24', '25', '26', '27', '28', '29'),
            ),
            mt940.processors.DetailField('applicant_iban', ('31',)),
            mt940.processors.DetailField('applicant_name', ('32', '33'),
                                         joined=True),
            mt940.processors.DetailField('bank_specific', ('70', '71')),
        ),
    )


def _statement(details: str) -> str:
    """Wrap structured ``:86:`` text in a minimal parseable statement."""
    return (
        _HEADER
        + ':61:2312290101C10,00NTRFNONREF\n'
        + f':86:{details}\n'
        + _FOOTER
    )


def test_legacy_config_reapplies_continuation_codes() -> None:
    config = mt940.processors._legacy_detail_config(
        mt940.processors.DETAIL_KEYS, space=False
    )
    mapping = config.code_to_key
    # Primary codes keep their output keys.
    assert mapping['00'] == 'posting_text'
    # Historical continuations are explicit in the self-contained layout.
    assert mapping['21'] == mapping['29'] == 'purpose'
    assert mapping['33'] == 'applicant_name'
    assert mapping['61'] == mapping['65'] == 'additional_purpose'


def test_legacy_config_skips_codes_already_present() -> None:
    # A mapping that already lists a continuation code under the same field
    # must not duplicate it while still adding the remaining continuations.
    custom = {**mt940.processors.DETAIL_KEYS, '21': 'purpose'}
    config = mt940.processors._legacy_detail_config(custom, space=False)
    purpose_codes = next(
        field.codes
        for field in config.fields
        if field.key == 'purpose'
    )
    assert purpose_codes.count('21') == 1
    assert '29' in purpose_codes


def test_default_config_matches_builtin_detail_keys() -> None:
    default = mt940.processors.DEFAULT_DETAIL_CONFIG
    assert default.code_to_key['31'] == 'applicant_name'
    iban = mt940.processors._legacy_detail_config(
        mt940.processors.DETAIL_KEYS_APPLICANT_IBAN, space=False
    )
    # The applicant_iban variant only relocates the 31 code.
    assert iban.code_to_key['31'] == 'applicant_iban'
    assert iban.code_to_key['32'] == 'applicant_name'


def test_code_to_key_first_field_wins_for_shared_codes() -> None:
    config = mt940.processors.BankDetailConfig(
        fields=(
            mt940.processors.DetailField('first', ('20',)),
            mt940.processors.DetailField('second', ('20', '21')),
        )
    )
    assert config.code_to_key['20'] == 'first'
    assert config.code_to_key['21'] == 'second'


def test_split_only_opens_configured_codes() -> None:
    parts = mt940.processors._split_configured_details(
        '166?20DE00?211234567890?x1 normal ?text?70KEEP?99TAIL',
        _config(),
    )
    # The leading digits, recognised fields, repeats and unrecognised text all
    # keep their position, and unrecognised fragments keep the '?' marker.
    assert [part.code for part in parts] == [
        '', '20', '21', None, None, '70', None
    ]
    assert [part.key for part in parts] == [
        '',
        'purpose',
        'purpose',
        None,
        None,
        'bank_specific',
        None,
    ]
    assert parts[3].value == '?x1 normal '
    assert parts[4].value == '?text'
    assert parts[6].value == '?99TAIL'


def test_split_retains_repeats_empty_and_lone_question_mark() -> None:
    parts = mt940.processors._split_configured_details(
        '105?20FIRST?20?70X?',
        _config(),
    )
    # Two 20 occurrences both survive, the empty second one included, and the
    # lone trailing '?' stays literal text inside the current fragment rather
    # than being read as a truncated delimiter.
    assert [(part.code, part.value) for part in parts] == [
        ('', '105'),
        ('20', 'FIRST'),
        ('20', ''),
        ('70', 'X?'),
    ]


def test_split_is_lossless_when_markers_are_readded() -> None:
    source = '166?20DE00?211234?x1 text?70KEEP?99TAIL'
    parts = mt940.processors._split_configured_details(source, _config())
    rebuilt = ''.join(
        part.value
        if part.key is None or part.code == ''
        else f'?{part.code}{part.value}'
        for part in parts
    )
    assert rebuilt == source


def test_assemble_joins_per_field_and_keeps_unrecognised() -> None:
    parts = mt940.processors._split_configured_details(
        '166?20DE00?211234567890?32Renate?33Richter?20DUP?70A?70B?99LOST',
        _config(),
    )
    assembled = mt940.processors._assemble_configured_details(parts, _config())
    # Purpose concatenates directly so the account number keeps its digits.
    assert assembled['purpose'] == 'DE001234567890DUP'
    # The name field is configured to join with a space.
    assert assembled['applicant_name'] == 'Renate Richter'
    assert assembled['bank_specific'] == 'AB'
    assert assembled['applicant_iban'] is None
    assert assembled['transaction_code'] == '166'
    assert assembled['transaction_details'] == '?99LOST'


def test_split_empty_string_produces_no_parts() -> None:
    assert mt940.processors._split_configured_details('', _config()) == []


def test_assemble_omits_unrecognised_key_when_nothing_is_unknown() -> None:
    parts = mt940.processors._split_configured_details(
        '105?20PURPOSE', _config()
    )
    assembled = mt940.processors._assemble_configured_details(parts, _config())
    assert 'transaction_details' not in assembled
    assert assembled['purpose'] == 'PURPOSE'


def test_detail_config_of_is_per_collection_and_fresh() -> None:
    transactions = mt940.models.Transactions(detail_config=_config())
    explicit = mt940.processors._detail_config_of(
        transactions, mt940.processors.DETAIL_KEYS, space=False
    )
    assert explicit is transactions.detail_config

    plain = mt940.models.Transactions()
    first = mt940.processors._detail_config_of(
        plain, mt940.processors.DETAIL_KEYS, space=False
    )
    second = mt940.processors._detail_config_of(
        plain, mt940.processors.DETAIL_KEYS, space=False
    )
    # Fallback layouts are rebuilt per call instead of sharing one split.
    assert first is not second
    assert first == second


def test_end_to_end_bank_config_preserves_fragments_and_position() -> None:
    data = _statement(
        '166?20DE00?211234?22567890?32Renate?33Richter?70KEEP?20DUP?99UNKNOWN'
    )
    transaction = mt940.parse(data, detail_config=_config())[0].data
    # Continuation lines never insert a space into the account-like value.
    assert transaction['purpose'] == 'DE001234567890DUP'
    assert transaction['applicant_name'] == 'Renate Richter'
    assert transaction['bank_specific'] == 'KEEP'
    assert transaction['transaction_code'] == '166'
    # Unrecognised text survives instead of being silently dropped.
    assert transaction['transaction_details'] == '?99UNKNOWN'


def test_end_to_end_separator_like_text_is_not_split() -> None:
    # '?x1' and '?wo' look like delimiters but are not configured codes.
    data = _statement('105?20hello?world?x1 leftover?20real')
    transaction = mt940.parse(data, detail_config=_config())[0].data
    assert transaction['purpose'] == 'helloreal'
    assert transaction['transaction_details'] == '?world?x1 leftover'


def test_end_to_end_multiline_continuation_keeps_account_digits() -> None:
    data = _statement('166?20DE00?211234\n?22567890')
    transaction = mt940.parse(data, detail_config=_config())[0].data
    assert transaction['purpose'] == 'DE001234567890'


def test_explicit_config_ignores_the_space_processor_argument() -> None:
    data = _statement('166?20DE00?211234567890')
    transactions = mt940.models.Transactions(
        processors={
            'post_transaction_details': [
                mt940.processors.transaction_details_post_processor_with_space,
            ],
        },
        detail_config=_config(),
    )
    transaction = transactions.parse(data)[0].data
    # The field's own joined=False wins over the partial's space=True.
    assert transaction['purpose'] == 'DE001234567890'


def test_without_config_the_legacy_mapping_remains_in_force() -> None:
    # The unknown 99 code is dropped and a repeated 20 keeps the last value,
    # exactly as the 5.0.0 mapping behaved.
    data = _statement('105?20FIRST?20LAST?99DROP')
    transaction = mt940.parse(data)[0].data
    assert transaction['purpose'] == 'LAST'
    assert 'transaction_details' not in transaction
    assert 'bank_specific' not in transaction


def test_parse_statements_forwards_the_bank_config() -> None:
    data = _statement('105?20PURPOSE?70KEEP').replace(
        ':20:REF\n', ':20:A\n', 1
    )
    statements = mt940.parse_statements(data, detail_config=_config())
    assert len(statements) == 1
    transaction = statements[0][0].data
    assert transaction['purpose'] == 'PURPOSE'
    assert transaction['bank_specific'] == 'KEEP'


def test_config_works_together_with_options() -> None:
    data = _statement('105?31DE89?20PURPOSE')
    transaction = mt940.parse(
        data,
        options=mt940.Options(applicant_iban=True),
        detail_config=_config(),
    )[0].data
    # The explicit layout owns the 31 mapping; the option is still accepted.
    assert transaction['applicant_iban'] == 'DE89'
    assert transaction['purpose'] == 'PURPOSE'


def test_bank_config_survives_pickle_and_old_pickle_defaults_to_none() -> None:
    transactions = mt940.models.Transactions(detail_config=_config())
    restored = pickle.loads(pickle.dumps(transactions))
    assert restored.detail_config == transactions.detail_config

    # Simulate a pickle written before detail_config existed: remove the field
    # from the saved state and confirm unpickling restores the legacy default.
    legacy = mt940.models.Transactions()
    state = legacy.__getstate__()
    del state['detail_config']
    rehydrated = mt940.models.Transactions.__new__(mt940.models.Transactions)
    rehydrated.__setstate__(state)
    assert rehydrated.detail_config is None


def test_switching_bank_config_never_reuses_a_previous_split() -> None:
    # Two layouts disagree on what ?70 means. They are parsed back to back via
    # the same shared post-processor, so any cross-call state would leak one
    # bank's split into the other.
    details = '105?20PURPOSE?70KEEP?99LOST'
    tag_dict = {'transaction_details': details}
    processor = mt940.processors.transaction_details_post_processor

    alpha_config = mt940.processors.BankDetailConfig(
        fields=(
            mt940.processors.DetailField('purpose', ('20',)),
            mt940.processors.DetailField('alpha_field', ('70',)),
        ),
    )
    beta_config = mt940.processors.BankDetailConfig(
        fields=(
            mt940.processors.DetailField('purpose', ('20',)),
            mt940.processors.DetailField('beta_field', ('70',)),
        ),
    )

    alpha = mt940.models.Transactions(detail_config=alpha_config)
    beta = mt940.models.Transactions(detail_config=beta_config)

    alpha_result = processor(alpha, mt940.tags.TransactionDetails(),
                             tag_dict, dict(tag_dict))
    beta_result = processor(beta, mt940.tags.TransactionDetails(),
                            tag_dict, dict(tag_dict))

    assert 'alpha_field' in alpha_result
    assert 'beta_field' not in alpha_result
    assert alpha_result['alpha_field'] == 'KEEP'

    assert 'beta_field' in beta_result
    assert 'alpha_field' not in beta_result
    assert beta_result['beta_field'] == 'KEEP'

    # Re-running the first bank after the second still uses its own layout.
    again = processor(alpha, mt940.tags.TransactionDetails(),
                      tag_dict, dict(tag_dict))
    assert again['alpha_field'] == 'KEEP'
    assert 'beta_field' not in again


def test_unstructured_details_are_left_untouched_with_a_config() -> None:
    data = (
        _HEADER
        + ':61:2312290101C10,00NTRFNONREF\n'
        + ':86:plain free text without subfields\n'
        + _FOOTER
    )
    transaction = mt940.parse(data, detail_config=_config())[0].data
    expected = 'plain free text without subfields'
    assert transaction['transaction_details'] == expected


@pytest.mark.parametrize(
    ('details', 'expected_purpose', 'expected_eref'),
    [
        # GVC fragments keep the exact spacing the legacy decoder produces.
        ('105?20SVWZ+Invoice EREF+ABC', 'Invoice ', 'ABC'),
        ('105?20EREF+ABC SVWZ+Invoice', 'Invoice', 'ABC '),
    ],
)
def test_gvc_keywords_still_decode_under_a_bank_config(
    details: str, expected_purpose: str, expected_eref: str
) -> None:
    transaction = mt940.parse(
        _statement(details), detail_config=_config()
    )[0].data
    assert transaction['end_to_end_reference'] == expected_eref
    assert transaction['purpose'] == expected_purpose
