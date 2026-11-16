""" Reconcile a complete trial archive with retransmitted fragments

A trial may be recorded as one complete archive plus a number of
retransmitted fragments that arrive later. Timestamps are not reliable
for matching rows between sources, because distinct samples can share
identical timestamps. Reconciliation is therefore driven by the explicit
sample correspondence declared for each fragment, and every source
carries its device and generation so the provenance of each row in the
unified coverage is preserved.

Merge rules:

* A fragment only replaces the sample range it declares to cover.
  Archive rows outside that range are left untouched, even when the
  fragment carries rows with matching sample ids.
* Where a covered fragment row corresponds to an archive row and the
  values agree, the archive row is kept and the fragment is recorded
  as confirming evidence.
* Where corresponding rows disagree, the conflict is kept: all
  differing variants remain in the unified coverage and the provenance
  table records every contributor.
* Rows are never deleted on value equality alone. Without explicit
  corresponding evidence, an archive row stays even when a fragment
  happens to contain an identical row.
* Fragment rows without archive correspondence extend the coverage
  when they fall inside the declared range.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class Role(Enum):
    """ The part a source played for a row of the unified coverage
    """
    #: Row retained from the complete archive without fragment evidence
    KEPT = "kept"
    #: Row corroborated by fragment evidence with equal values
    CONFIRMED = "confirmed"
    #: Row whose evidence disagrees with other evidence for the sample
    CONFLICT = "conflict"
    #: Row only present in a fragment, within its declared coverage
    ADDED = "added"


@dataclass(frozen=True)
class Source:
    """ A data source taking part in a merge

    :param name: Identifier of the source.
    :param device: Device that produced the data.
    :param generation: Generation of the device.
    """
    name: str
    device: str
    generation: int


@dataclass
class Sample:
    """ A single trial row

    :param sample_id: Explicit sample correspondence key. Rows are only
        ever matched through this key, never through the timestamp,
        because timestamps of distinct samples may be identical.
    :param timestamp: Timestamp of the sample. Not used for matching.
    :param values: Mapping of channel name to measured value.
    """
    sample_id: object
    timestamp: object
    values: Mapping


@dataclass
class Fragment:
    """ A retransmitted fragment of a trial

    :param source: Where the fragment came from.
    :param rows: Rows carried by the fragment.
    :param coverage: The sample ids the fragment declares to cover, as
        any container supporting ``in`` (for example a ``set`` or a
        ``range``). Only rows within this declared range take part in
        the merge.
    """
    source: Source
    rows: list
    coverage: object

    def covers(self, sample_id):
        """ Whether the fragment declares to cover the given sample id
        """
        return sample_id in self.coverage


@dataclass
class CoverageRow:
    """ One row of the unified coverage

    :ivar sample_id: Explicit sample correspondence key.
    :ivar timestamp: Timestamp of the sample.
    :ivar values: Mapping of channel name to measured value.
    :ivar source: Source the row was taken from.
    :ivar role: The part the source played for this row.
    """
    sample_id: object
    timestamp: object
    values: Mapping
    source: Source
    role: Role


@dataclass
class Evidence:
    """ One source's contribution to a sample in the provenance table

    :ivar source: The contributing source, with its device and generation.
    :ivar role: The part the source played for the sample.
    :ivar values: The values the source provided for the sample.
    """
    source: Source
    role: Role
    values: Mapping


@dataclass
class ProvenanceEntry:
    """ Provenance of one sample in the unified coverage

    :ivar sample_id: Explicit sample correspondence key.
    :ivar evidence: Contributions of every source for the sample, in
        the order they were merged.
    """
    sample_id: object
    evidence: list = field(default_factory=list)


@dataclass
class MergeResult:
    """ The result of reconciling an archive with its fragments

    :ivar coverage: The unified coverage. Archive rows keep their
        original order; rows added from fragments follow in the order
        the fragments were merged. Samples with conflicting evidence
        have one row per differing variant.
    :ivar provenance: The provenance table, with one entry per sample
        of the unified coverage.
    """
    coverage: list = field(default_factory=list)
    provenance: list = field(default_factory=list)


def merge_trial(archive, archive_rows, fragments=()):
    """ Merge a complete trial archive with retransmitted fragments

    :param archive: :class:`Source` describing the complete archive.
    :param archive_rows: Ordered :class:`Sample` rows of the archive.
    :param fragments: :class:`Fragment` instances to reconcile, merged
        in the order given.
    :returns: A :class:`MergeResult` with the unified coverage and the
        provenance table.
    """
    result = MergeResult()
    # Sample id -> merge state for the sample. The first archive row
    # seen for an id is the primary variant that evidence is matched
    # against.
    states = {}

    for row in archive_rows:
        if row.sample_id in states:
            continue
        coverage_row = CoverageRow(
            row.sample_id, row.timestamp, dict(row.values), archive, Role.KEPT)
        entry = ProvenanceEntry(row.sample_id, [
            Evidence(archive, Role.KEPT, dict(row.values))])
        result.coverage.append(coverage_row)
        result.provenance.append(entry)
        states[row.sample_id] = _SampleState(coverage_row, entry)

    for fragment in fragments:
        for row in fragment.rows:
            if not fragment.covers(row.sample_id):
                # A fragment only replaces the range it declares to cover
                continue
            state = states.get(row.sample_id)
            if state is None:
                coverage_row = CoverageRow(
                    row.sample_id, row.timestamp, dict(row.values),
                    fragment.source, Role.ADDED)
                entry = ProvenanceEntry(row.sample_id, [
                    Evidence(fragment.source, Role.ADDED, dict(row.values))])
                result.coverage.append(coverage_row)
                result.provenance.append(entry)
                states[row.sample_id] = _SampleState(coverage_row, entry)
            else:
                state.add_fragment_row(fragment.source, row, result)
    return result


class _SampleState(object):
    """ Merge state for one sample id
    """

    def __init__(self, primary_row, entry):
        self._primary_row = primary_row
        self._primary_evidence = entry.evidence[0]
        self._entry = entry
        # All coverage rows for the sample, starting with the primary row
        self._variants = [primary_row]

    def add_fragment_row(self, source, row, result):
        for variant in self._variants:
            if _values_equal(variant.values, row.values):
                # Confirming evidence for an existing variant. The row is
                # kept and not duplicated, and equal valued rows without
                # this explicit correspondence are never deleted.
                self._confirm(variant, source, row.values)
                return
        # Values disagree with every known variant: keep the conflict
        self._primary_row.role = Role.CONFLICT
        self._primary_evidence.role = Role.CONFLICT
        self._entry.evidence.append(
            Evidence(source, Role.CONFLICT, dict(row.values)))
        conflict_row = CoverageRow(
            row.sample_id, row.timestamp, dict(row.values), source, Role.CONFLICT)
        result.coverage.append(conflict_row)
        self._variants.append(conflict_row)

    def _confirm(self, variant, source, values):
        if variant is self._primary_row and variant.role is Role.KEPT:
            variant.role = Role.CONFIRMED
            self._primary_evidence.role = Role.CONFIRMED
        self._entry.evidence.append(
            Evidence(source, Role.CONFIRMED, dict(values)))


def _values_equal(left, right):
    """ Compare two value mappings, tolerating array-like values
    """
    if left is right:
        return True
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return False
        return all(_values_equal(left[key], right[key]) for key in left)
    try:
        comparison = left == right
    except Exception:
        return False
    if isinstance(comparison, bool):
        return comparison
    reduce_all = getattr(comparison, "all", None)
    if callable(reduce_all):
        return bool(reduce_all())
    return bool(comparison)
