"""Tests for reconciling a complete trial archive with retransmitted fragments."""

import copy

import pytest

from nptdms.trial_merge import (
    Fragment, Role, Sample, Source, merge_trial,
)


ARCHIVE = Source("archive", device="dev-a", generation=1)
FRAGMENT_1 = Source("fragment-1", device="dev-a", generation=1)
FRAGMENT_2 = Source("fragment-2", device="dev-b", generation=2)


def archive_rows(*rows):
    return [Sample(sample_id, timestamp, values)
            for (sample_id, timestamp, values) in rows]


def test_confirming_fragment_keeps_row_and_records_provenance():
    rows = archive_rows((1, "t0", {"a": 5}), (2, "t1", {"a": 6}))
    fragment = Fragment(FRAGMENT_1, archive_rows((2, "t1", {"a": 6})), coverage={2})

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert [r.sample_id for r in result.coverage] == [1, 2]
    assert result.coverage[0].role is Role.KEPT
    assert result.coverage[1].role is Role.CONFIRMED
    entry = result.provenance[1]
    assert entry.sample_id == 2
    assert [(e.source, e.role) for e in entry.evidence] == [
        (ARCHIVE, Role.CONFIRMED), (FRAGMENT_1, Role.CONFIRMED)]


def test_conflicting_values_are_kept():
    rows = archive_rows((1, "t0", {"a": 5}))
    fragment = Fragment(FRAGMENT_1, archive_rows((1, "t0", {"a": 7})), coverage={1})

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert len(result.coverage) == 2
    assert all(r.role is Role.CONFLICT for r in result.coverage)
    assert result.coverage[0].values == {"a": 5}
    assert result.coverage[0].source is ARCHIVE
    assert result.coverage[1].values == {"a": 7}
    assert result.coverage[1].source is FRAGMENT_1
    assert [(e.source, e.role) for e in result.provenance[0].evidence] == [
        (ARCHIVE, Role.CONFLICT), (FRAGMENT_1, Role.CONFLICT)]


def test_equal_values_without_correspondence_are_not_deleted():
    rows = archive_rows((1, "t0", {"a": 5}))
    # The fragment row has identical values but a different sample id,
    # so there is no explicit correspondence with the archive row.
    fragment = Fragment(FRAGMENT_1, archive_rows((2, "t0", {"a": 5})), coverage={2})

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert [(r.sample_id, r.role) for r in result.coverage] == [
        (1, Role.KEPT), (2, Role.ADDED)]
    assert [e.sample_id for e in result.provenance] == [1, 2]
    assert len(result.provenance[0].evidence) == 1


def test_fragment_only_replaces_its_declared_coverage():
    rows = archive_rows((1, "t0", {"a": 5}), (2, "t1", {"a": 6}))
    # The fragment carries rows for samples 1 and 2 but only declares
    # coverage of sample 2, so sample 1 must stay untouched.
    fragment = Fragment(
        FRAGMENT_1,
        archive_rows((1, "t0", {"a": 50}), (2, "t1", {"a": 60})),
        coverage={2})

    result = merge_trial(ARCHIVE, rows, [fragment])

    kept = [r for r in result.coverage if r.sample_id == 1]
    assert len(kept) == 1
    assert kept[0].role is Role.KEPT
    assert kept[0].values == {"a": 5}
    assert [r.sample_id for r in result.coverage].count(2) == 2
    assert all(r.role is Role.CONFLICT for r in result.coverage if r.sample_id == 2)


def test_identical_timestamps_are_not_matched():
    rows = archive_rows((1, "t0", {"a": 5}), (2, "t0", {"a": 5}))
    fragment = Fragment(FRAGMENT_1, archive_rows((2, "t0", {"a": 5})), coverage={2})

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert [(r.sample_id, r.role) for r in result.coverage] == [
        (1, Role.KEPT), (2, Role.CONFIRMED)]


def test_fragment_only_rows_extend_coverage():
    rows = archive_rows((1, "t0", {"a": 5}))
    fragment = Fragment(
        FRAGMENT_1, archive_rows((1, "t0", {"a": 5}), (3, "t2", {"a": 9})),
        coverage={1, 3})

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert [(r.sample_id, r.role) for r in result.coverage] == [
        (1, Role.CONFIRMED), (3, Role.ADDED)]
    assert result.coverage[1].source is FRAGMENT_1


def test_fragment_rows_outside_declared_coverage_are_ignored():
    rows = archive_rows((1, "t0", {"a": 5}))
    fragment = Fragment(
        FRAGMENT_1, archive_rows((1, "t0", {"a": 5}), (3, "t2", {"a": 9})),
        coverage={1})

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert [r.sample_id for r in result.coverage] == [1]
    assert result.coverage[0].role is Role.CONFIRMED


def test_device_generations_are_recorded_in_provenance():
    rows = archive_rows((1, "t0", {"a": 5}))
    fragment_1 = Fragment(FRAGMENT_1, archive_rows((1, "t0", {"a": 5})), coverage={1})
    fragment_2 = Fragment(FRAGMENT_2, archive_rows((1, "t0", {"a": 8})), coverage={1})

    result = merge_trial(ARCHIVE, rows, [fragment_1, fragment_2])

    evidence = result.provenance[0].evidence
    assert [(e.source.device, e.source.generation) for e in evidence] == [
        ("dev-a", 1), ("dev-a", 1), ("dev-b", 2)]
    assert [e.role for e in evidence] == [
        Role.CONFLICT, Role.CONFIRMED, Role.CONFLICT]
    # Both differing variants are kept in the unified coverage
    assert sorted(r.values["a"] for r in result.coverage) == [5, 8]


def test_second_fragment_can_confirm_a_conflict_variant():
    rows = archive_rows((1, "t0", {"a": 5}))
    fragment_1 = Fragment(FRAGMENT_1, archive_rows((1, "t0", {"a": 7})), coverage={1})
    fragment_2 = Fragment(FRAGMENT_2, archive_rows((1, "t0", {"a": 7})), coverage={1})

    result = merge_trial(ARCHIVE, rows, [fragment_1, fragment_2])

    # No third variant is added; the second fragment confirms the first
    assert len(result.coverage) == 2
    assert [(e.source, e.role) for e in result.provenance[0].evidence] == [
        (ARCHIVE, Role.CONFLICT),
        (FRAGMENT_1, Role.CONFLICT),
        (FRAGMENT_2, Role.CONFIRMED)]


def test_coverage_range_container():
    rows = archive_rows(*[(i, "t%d" % i, {"a": i}) for i in range(5)])
    fragment = Fragment(
        FRAGMENT_1, archive_rows((2, "t2", {"a": 2}), (3, "t3", {"a": 3})),
        coverage=range(2, 4))

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert [r.role for r in result.coverage] == [
        Role.KEPT, Role.KEPT, Role.CONFIRMED, Role.CONFIRMED, Role.KEPT]


def test_archive_without_fragments():
    rows = archive_rows((1, "t0", {"a": 5}), (2, "t1", {"a": 6}))

    result = merge_trial(ARCHIVE, rows)

    assert [(r.sample_id, r.role) for r in result.coverage] == [
        (1, Role.KEPT), (2, Role.KEPT)]
    assert all(len(e.evidence) == 1 for e in result.provenance)


def test_empty_archive():
    fragment = Fragment(FRAGMENT_1, archive_rows((1, "t0", {"a": 5})), coverage={1})

    result = merge_trial(ARCHIVE, [], [fragment])

    assert [(r.sample_id, r.role) for r in result.coverage] == [(1, Role.ADDED)]


@pytest.mark.parametrize("values", [
    {"a": [1, 2, 3]},
    {"a": (1.5, 2.5)},
])
def test_array_like_values_compare_equal(values):
    rows = archive_rows((1, "t0", copy.deepcopy(values)))
    fragment = Fragment(
        FRAGMENT_1, archive_rows((1, "t0", copy.deepcopy(values))), coverage={1})

    result = merge_trial(ARCHIVE, rows, [fragment])

    assert [r.role for r in result.coverage] == [Role.CONFIRMED]
