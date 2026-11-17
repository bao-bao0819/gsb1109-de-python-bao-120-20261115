"""Tests for the opt-in processor field-change audit trail.

The ``process_sources`` option records what custom and built-in processors
change without altering the parsed result. These tests cover in-place and
replacement processors, added, changed and removed fields, the no-false-report
rule, processor naming and pickle compatibility.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import pickle
import types
from typing import Any, cast

import mt940
import pytest
from mt940.processors import (
    _MISSING,
    FieldChange,
    _MissingMarker,
    _processor_name,
    diff_mappings,
    field_changes,
)

_STATEMENT = (
    ':20:REF\n'
    ':25:ACC\n'
    ':28C:1\n'
    ':60F:C240101EUR0,00\n'
    ':61:240101C1,25NTRFNONREF\n'
    ':86:Example transaction\n'
    ':62F:C240101EUR1,25\n'
)


def _in_place_currency(
    transactions: mt940.models.Transactions,
    tag: mt940.tags.Tag,
    tag_dict: dict[str, Any],
) -> dict[str, Any]:
    """Return the received mapping after setting a currency in place."""
    del transactions, tag
    tag_dict['currency'] = 'GBP'
    tag_dict['untouched'] = 'kept'
    return tag_dict


def _replacement_currency(
    transactions: mt940.models.Transactions,
    tag: mt940.tags.Tag,
    tag_dict: dict[str, Any],
) -> dict[str, Any]:
    """Return a replacement mapping with one changed field."""
    del transactions, tag
    return {**tag_dict, 'currency': 'USD'}


def _noop_pre(
    transactions: mt940.models.Transactions,
    tag: mt940.tags.Tag,
    tag_dict: dict[str, Any],
) -> dict[str, Any]:
    """Return the mapping unchanged."""
    del transactions, tag
    return tag_dict


def _same_value_pre(
    transactions: mt940.models.Transactions,
    tag: mt940.tags.Tag,
    tag_dict: dict[str, Any],
) -> dict[str, Any]:
    """Return the mapping after reassigning a key to its same value."""
    del transactions, tag
    tag_dict['currency'] = tag_dict['currency']
    return tag_dict


def _post_edit(
    transactions: mt940.models.Transactions,
    tag: mt940.tags.Tag,
    tag_dict: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Return the result with one field changed and another removed."""
    del transactions, tag, tag_dict
    result['customer_reference'] = 'CHANGED'
    del result['extra_details']
    return result


def _add_meta(
    transactions: mt940.models.Transactions,
    tag: mt940.tags.Tag,
    tag_dict: dict[str, Any],
) -> dict[str, Any]:
    """Return the mapping after adding a nested mapping."""
    del transactions, tag
    tag_dict['meta'] = {'x': 1}
    return tag_dict


def _mutate_meta(
    transactions: mt940.models.Transactions,
    tag: mt940.tags.Tag,
    tag_dict: dict[str, Any],
) -> dict[str, Any]:
    """Return the mapping after mutating a nested value in place."""
    del transactions, tag
    tag_dict['meta']['x'] = 2
    return tag_dict


def _payload(transactions: mt940.models.Transactions) -> str:
    """Return parsed data as JSON, which never includes the audit list."""
    return json.dumps(transactions, cls=mt940.JSONEncoder, sort_keys=True)


def test_process_sources_is_empty_by_default() -> None:
    transactions = mt940.parse(_STATEMENT)
    assert transactions.process_sources == []
    assert mt940.Options().process_sources is False


def test_enabling_tracking_keeps_parsed_results_identical() -> None:
    plain = mt940.parse(_STATEMENT)
    tracked = mt940.parse(
        _STATEMENT, options=mt940.Options(process_sources=True)
    )
    all_on = mt940.parse(_STATEMENT, options=mt940.Options.all())

    assert _payload(plain) == _payload(tracked) == _payload(all_on)
    assert plain.data == tracked.data
    assert [item.data for item in plain] == [item.data for item in tracked]
    assert tracked.process_sources
    assert all_on.process_sources


def test_in_place_pre_processor_change_is_traced() -> None:
    transactions = mt940.models.Transactions(
        processors={'pre_statement': [_in_place_currency]},
        options=mt940.Options(process_sources=True),
    )
    _ = transactions.parse(_STATEMENT)

    changes = [
        item
        for item in transactions.process_sources
        if item.processor.endswith('_in_place_currency')
    ]
    assert len(changes) == 2
    currency = next(item for item in changes if item.field == 'currency')
    assert currency.tag == 'statement'
    assert currency.phase == 'pre'
    assert currency.added is True
    assert currency.removed is False
    assert currency.before is _MISSING
    assert currency.after == 'GBP'
    assert repr(currency.before) == '<missing>'


def test_replacement_pre_processor_change_is_traced() -> None:
    transactions = mt940.models.Transactions(
        processors={'pre_statement': [_replacement_currency]},
        options=mt940.Options(process_sources=True),
    )
    _ = transactions.parse(_STATEMENT)

    currency = next(
        item
        for item in transactions.process_sources
        if item.field == 'currency'
    )
    assert currency.after == 'USD'
    assert currency.added is True


def test_unchanged_fields_are_never_reported() -> None:
    transactions = mt940.models.Transactions(
        processors={
            'pre_statement': [
                _in_place_currency,
                _noop_pre,
                _same_value_pre,
            ],
        },
        options=mt940.Options(process_sources=True),
    )
    _ = transactions.parse(_STATEMENT)

    noop = [
        item
        for item in transactions.process_sources
        if item.processor.endswith('_noop_pre')
    ]
    same_value = [
        item
        for item in transactions.process_sources
        if item.processor.endswith('_same_value_pre')
    ]
    assert noop == []
    assert same_value == []
    assert not any(
        item.field == 'untouched' and item.phase == 'pre' and not item.added
        for item in transactions.process_sources
    )


def test_post_processor_change_and_removal_are_traced() -> None:
    transactions = mt940.models.Transactions(
        processors={'post_statement': [_post_edit]},
        options=mt940.Options(process_sources=True),
    )
    _ = transactions.parse(_STATEMENT)

    by_field = {
        item.field: item
        for item in transactions.process_sources
        if item.processor.endswith('_post_edit')
    }
    assert set(by_field) == {'customer_reference', 'extra_details'}
    assert by_field['customer_reference'].phase == 'post'
    assert by_field['customer_reference'].before == 'NONREF'
    assert by_field['customer_reference'].after == 'CHANGED'
    removed = by_field['extra_details']
    assert removed.removed is True
    assert removed.added is False
    assert removed.after is _MISSING
    assert repr(removed.after) == '<missing>'


def test_nested_in_place_mutation_needs_the_deep_copy() -> None:
    transactions = mt940.models.Transactions(
        processors={'pre_statement': [_add_meta, _mutate_meta]},
        options=mt940.Options(process_sources=True),
    )
    _ = transactions.parse(_STATEMENT)

    records = {
        item.processor.rsplit('.', 1)[-1]: item
        for item in transactions.process_sources
        if item.field == 'meta'
    }
    added = records['_add_meta']
    nested = records['_mutate_meta']
    assert added.before == _MISSING
    assert added.after == {'x': 1}
    assert nested.before == {'x': 1}
    assert nested.after == {'x': 2}
    # A later in-place mutation must not rewrite the earlier record.
    assert added.after == {'x': 1}


def test_default_processors_are_traced_with_phase_and_tag_labels() -> None:
    transactions = mt940.parse(
        _STATEMENT, options=mt940.Options(process_sources=True)
    )
    records = transactions.process_sources

    cleanup = [
        item
        for item in records
        if item.processor.endswith('date_cleanup_post_processor')
    ]
    assert {item.tag for item in cleanup} == {'statement'}
    assert {item.phase for item in cleanup} == {'post'}
    assert all(item.removed for item in cleanup)
    assert {item.field for item in cleanup} == {
        'day',
        'month',
        'year',
        'entry_day',
        'entry_month',
    }

    reference = next(
        item for item in records if item.field == 'transaction_reference'
    )
    assert reference.added is True
    assert reference.after == 'REF'
    assert 'transactions_to_transaction' in reference.processor

    assert {item.phase for item in records} <= {'pre', 'post'}


def test_balance_slot_records_its_own_tag_slug() -> None:
    transactions = mt940.models.Transactions(
        processors={
            'pre_final_closing_balance': [
                mt940.processors.add_currency_pre_processor('USD'),
            ],
        },
        options=mt940.Options(process_sources=True),
    )
    _ = transactions.parse(_STATEMENT)

    change = next(
        item
        for item in transactions.process_sources
        if item.tag == 'final_closing_balance' and item.field == 'currency'
    )
    assert change.phase == 'pre'
    assert change.before == 'EUR'
    assert change.after == 'USD'


def test_records_accumulate_across_repeated_parse_calls() -> None:
    transactions = mt940.models.Transactions(
        options=mt940.Options(process_sources=True)
    )
    first = len(transactions.parse(_STATEMENT))
    first_count = len(transactions.process_sources)
    _ = transactions.parse(_STATEMENT)

    assert first == 1
    assert first_count > 0
    assert len(transactions.process_sources) == 2 * first_count


def test_pickle_roundtrip_preserves_and_backfills_sources() -> None:
    tracked = mt940.parse(
        _STATEMENT, options=mt940.Options(process_sources=True)
    )
    restored = pickle.loads(pickle.dumps(tracked))

    assert restored.process_sources == tracked.process_sources
    _ = restored.parse(_STATEMENT)
    assert len(restored.process_sources) > len(tracked.process_sources)

    state = tracked.__getstate__()
    del state['process_sources']
    legacy = mt940.models.Transactions.__new__(mt940.models.Transactions)
    legacy.__setstate__(state)
    assert legacy.process_sources == []


def test_missing_marker_compares_equal_and_hashes_together() -> None:
    fresh = _MissingMarker()
    assert fresh == _MISSING
    assert fresh != object()
    assert hash(_MISSING) == hash(fresh)
    assert len({_MISSING, fresh}) == 1
    assert repr(fresh) == '<missing>'


def test_diff_mappings_reports_added_removed_changed_only() -> None:
    changed = diff_mappings(
        {'a': 1, 'same': 0},
        {'a': 2, 'added': 3, 'same': 0},
    )
    assert changed == {'a': (1, 2), 'added': (_MISSING, 3)}

    removed = diff_mappings({'gone': 1}, {})
    assert removed == {'gone': (1, _MISSING)}
    assert diff_mappings({'a': 1}, {'a': 1}) == {}


def test_field_changes_builds_one_record_per_field() -> None:
    records = field_changes(
        tag='statement',
        phase='post',
        processor=_post_edit,
        before={'customer_reference': 'NONREF', 'extra_details': None},
        after={'customer_reference': 'CHANGED'},
    )
    assert len(records) == 2
    assert all(isinstance(item, FieldChange) for item in records)
    assert {item.field for item in records} == {
        'customer_reference',
        'extra_details',
    }
    assert all(item.tag == 'statement' for item in records)
    assert all(item.phase == 'post' for item in records)


def test_processor_name_handles_functions_partials_and_objects() -> None:
    function_name = _processor_name(_noop_pre)
    assert function_name.endswith('_noop_pre')

    partial = functools.partial(_noop_pre)
    assert _processor_name(partial) == f'partial({function_name})'

    obj = types.SimpleNamespace()
    assert _processor_name(cast('Any', obj)) == 'SimpleNamespace'


def test_field_change_is_frozen_and_slotted() -> None:
    change = FieldChange(
        tag='statement',
        field='currency',
        phase='pre',
        processor='module.processor',
        before=_MISSING,
        after='EUR',
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast('Any', change).after = 'USD'
    assert not hasattr(change, '__dict__')


def test_non_mapping_post_processor_output_passes_through_untracked() -> None:
    transactions = mt940.models.Transactions(
        options=mt940.Options(process_sources=True)
    )
    tag = mt940.tags.Statement()

    def returns_string(
        collection: mt940.models.Transactions,
        selected_tag: mt940.tags.Tag,
        tag_dict: dict[str, Any],
        result: dict[str, Any],
    ) -> str:
        del collection, selected_tag, tag_dict, result
        return 'not-a-mapping'

    # These callbacks intentionally break the mapping return contract to
    # prove the audit trail ignores non-mapping output rather than crashing.
    output = transactions._run_post_processor(
        tag, cast('Any', returns_string), {}, {'customer_reference': 'NONREF'}
    )
    assert output == 'not-a-mapping'
    assert transactions.process_sources == []

    echoed = transactions._run_post_processor(
        tag, cast('Any', lambda *args: args[-1]), {}, 42
    )
    assert echoed == 42
    assert transactions.process_sources == []


def test_pre_processor_helper_without_the_option_takes_no_snapshot() -> None:
    transactions = mt940.models.Transactions()
    tag = mt940.tags.Statement()

    result = transactions._run_pre_processor(tag, _noop_pre, {'a': 1})

    assert result == {'a': 1}
    assert transactions.process_sources == []


def test_process_sources_is_a_declared_option() -> None:
    names = mt940.Options.names()
    assert 'process_sources' in names
    assert len(names) == 11
    assert all(getattr(mt940.Options.all(), name) is True for name in names)
    assert mt940.Options.all().process_sources is True
