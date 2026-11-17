"""Tests for ambiguity-aware structured ``:86:`` analysis.

The analyzer keeps physical lines so a marker-shaped continuation after a
full 65-character line stays distinguishable from a deliberate marker. These
tests drive both the low-level :mod:`mt940.details` analyzer and the
``Options.detail_candidates`` integration in the details post-processor.
"""

from __future__ import annotations

import mt940
from mt940 import details, processors


def _statement(detail: str) -> str:
    return (
        ':20:REF\n'
        ':25:123456789\n'
        ':28C:0\n'
        ':60F:C200101EUR100,00\n'
        ':61:2001010101C10,00NTRFNONREF\n'
        f':86:{detail}\n'
        ':62F:C200101EUR110,00\n'
    )


def test_unstructured_input_is_not_analyzed() -> None:
    analysis = details.analyze_details('free text without markers')
    assert analysis.structured is False
    assert analysis.readings == ()
    assert analysis.issues == ()
    assert analysis.unique is False
    assert analysis.unique_reading is None
    assert analysis.candidates == ()
    assert analysis.raw_lines == ('free text without markers',)


def test_unique_reading_matches_the_flattened_legacy_pipeline() -> None:
    raw = (
        '105?00Basislastschrift?10931?20Invoice?21number\n'
        '?30BYLADEMM?31DE99?32Finanzamt?33Erhebung'
    )
    analysis = details.analyze_details(raw)
    assert analysis.structured is True
    assert analysis.unique is True
    reading = analysis.unique_reading
    assert reading is analysis.readings[0]

    flattened = ''.join(raw.splitlines())
    legacy = processors._parse_mt940_details(flattened)
    assert reading.decode() == legacy
    assert reading.decode(processors.DETAIL_KEYS_APPLICANT_IBAN)[
        'applicant_iban'
    ] == 'DE99'


def test_raw_lines_are_preserved_and_never_reordered() -> None:
    raw = '105?00Text?20Purpose\n?21continued'
    analysis = details.analyze_details(raw)
    assert analysis.raw_lines == (
        '105?00Text?20Purpose',
        '?21continued',
    )
    assert analysis.readings[0].segments['21'] == 'continued'


def test_short_line_marker_is_accepted_even_when_it_looks_like_text() -> None:
    # The previous line is shorter than the wrap width, so the line-start
    # marker is a deliberate break and yields exactly one reading.
    raw = '105?00Text?20Purpose\n?21continued'
    analysis = details.analyze_details(raw)
    assert len(analysis.readings) == 1
    assert [m.code for m in analysis.readings[0].markers] == [
        '00',
        '20',
        '21',
    ]


def test_wrapped_marker_shaped_line_produces_two_candidates() -> None:
    first = '105?00Text?20' + 'A' * 52
    assert len(first) == details.WRAP_WIDTH
    raw = f'{first}\n?30BIC?31IBAN'
    analysis = details.analyze_details(raw)
    assert len(analysis.readings) == 2
    assert analysis.unique is False
    assert analysis.unique_reading is None
    kinds = [issue.kind for issue in analysis.issues]
    assert details.IssueKind.AMBIGUOUS_STRUCTURE in kinds
    # One candidate opens ?30, the other keeps '?30...' inside the purpose.
    assert any('30' in candidate for candidate in analysis.candidates)
    purpose_only = [
        candidate for candidate in analysis.candidates if '30' not in candidate
    ]
    assert purpose_only and '?30BIC' in purpose_only[0]['20']


def test_illegal_continuation_without_base_field_is_text() -> None:
    # ?33 continues ?32 and ?61 continues ?60; with neither base opened the
    # marker-shaped tokens are content of the only legal reading.
    raw = '105?00Text?20Purpose?33orphan?61orphan'
    analysis = details.analyze_details(raw)
    assert analysis.unique is True
    segments = analysis.readings[0].segments
    assert '33' not in segments
    assert '61' not in segments
    assert segments['20'].endswith('?33orphan?61orphan')


def test_continuation_codes_are_classified_in_context() -> None:
    raw = (
        '105?00Text?20Purpose?21more?32Name?33line?60Note?61again'
    )
    analysis = details.analyze_details(raw)
    statuses = {
        marker.code: marker.status
        for marker in analysis.readings[0].markers
    }
    assert statuses['21'] is details.MarkerStatus.CONTINUATION
    assert statuses['33'] is details.MarkerStatus.CONTINUATION
    assert statuses['61'] is details.MarkerStatus.CONTINUATION
    assert statuses['00'] is details.MarkerStatus.KNOWN


def test_unknown_legal_extension_is_reported_not_dropped() -> None:
    raw = '105?00Text?20Purpose?38IBAN2'
    analysis = details.analyze_details(raw)
    assert analysis.unique is True
    issue = next(
        issue
        for issue in analysis.issues
        if issue.kind is details.IssueKind.UNKNOWN_EXTENSION
    )
    assert issue.code == '38'
    assert issue.line_number == 1
    assert issue.as_dict() == {
        'kind': 'unknown_extension',
        'message': issue.message,
        'code': '38',
        'field_name': None,
        'line_number': 1,
    }


def test_missing_required_field_is_reported_separately() -> None:
    # No ?00 posting text and no ?20 purpose.
    analysis = details.analyze_details('105?10931')
    names = {
        issue.field_name
        for issue in analysis.issues
        if issue.kind is details.IssueKind.MISSING_REQUIRED
    }
    assert names == {'posting_text', 'purpose'}


def test_trailing_incomplete_delimiter_is_ignored() -> None:
    with_one = details.analyze_details('105?00Text?20Purpose?')
    with_two = details.analyze_details('105?00Text?20Purpose??')
    expected = details.analyze_details('105?00Text?20Purpose')
    assert with_one.readings[0].pairs == expected.readings[0].pairs
    assert with_two.readings[0].pairs == expected.readings[0].pairs


def test_adjacent_markers_leave_every_subfield_present() -> None:
    analysis = details.analyze_details('105?00?20?21tail')
    segments = analysis.readings[0].segments
    assert set(segments) >= {'', '00', '20', '21'}
    assert segments['21'] == 'tail'


def test_marker_line_numbers_follow_physical_lines() -> None:
    raw = '105?00Text?20Purpose\n?21second\n?22third'
    lines = {
        marker.code: marker.line_number
        for marker in details.analyze_details(raw).readings[0].markers
    }
    assert lines['00'] == 1
    assert lines['21'] == 2
    assert lines['22'] == 3


def test_processor_unique_reading_is_stored_like_the_legacy_output() -> None:
    detail = '105?00Basislastschrift?10931?20EREF+X SVWZ+Invoice'
    legacy = mt940.parse(_statement(detail))[0].data
    enabled = mt940.parse(
        _statement(detail),
        options=mt940.Options(detail_candidates=True),
    )[0].data
    for key, value in legacy.items():
        assert enabled[key] == value
    assert 'detail_issues' not in enabled
    assert 'detail_candidates' not in enabled
    assert 'transaction_details' not in enabled


def test_processor_ambiguous_reading_keeps_raw_lines_and_candidates() -> None:
    first = '105?00Text?20' + 'A' * 56
    detail = f'{first}\n?30BIC?31IBAN'
    transaction = mt940.parse(
        _statement(detail),
        options=mt940.Options(detail_candidates=True),
    )[0]
    data = transaction.data
    assert data['transaction_details'] == detail
    assert len(data['detail_candidates']) == 2
    assert data['detail_issues'][0]['kind'] == 'ambiguous_structure'
    # Forced legacy storage would have removed the raw details entirely.
    assert mt940.parse(_statement(detail))[0].data.get(
        'transaction_details'
    ) is None


def test_processor_reports_unknown_extension_end_to_end() -> None:
    transaction = mt940.parse(
        _statement('105?00Text?20Purpose?38IBAN2'),
        options=mt940.Options(detail_candidates=True),
    )[0]
    assert transaction.data['detail_issues'] == [
        details.analyze_details(
            '105?00Text?20Purpose?38IBAN2'
        ).issues[0].as_dict()
    ]


def test_processor_unstructured_details_ignore_the_option() -> None:
    transaction = mt940.parse(
        _statement('free text only'),
        options=mt940.Options(detail_candidates=True),
    )[0]
    assert transaction.data['transaction_details'] == 'free text only'
    assert 'detail_issues' not in transaction.data


def test_processor_honors_applicant_iban_mapping() -> None:
    detail = '105?00Text?20Purpose?31DE99?32Name'
    transaction = mt940.parse(
        _statement(detail),
        options=mt940.Options(
            detail_candidates=True, applicant_iban=True
        ),
    )[0]
    assert transaction.data['applicant_iban'] == 'DE99'
    assert transaction.data['applicant_name'] == 'Name'


def test_processor_with_space_joins_fragments_with_spaces() -> None:
    transactions = mt940.models.Transactions(
        processors={
            'post_transaction_details': [
                processors.transaction_details_post_processor_with_space,
            ],
        },
        options=mt940.Options(detail_candidates=True),
    )
    transaction = transactions.parse(
        _statement('105?00Text?20One?21Two')
    )[0]
    assert transaction.data['purpose'] == 'One Two'
