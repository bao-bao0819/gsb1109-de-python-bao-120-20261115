"""
peff - PSI Extended FASTA Format
================================

PEFF is a forth-coming standard from PSI-HUPO formalizing and extending the
encoding of protein features and annotations for building search spaces for
proteomics. See `The PEFF specification <http://www.psidev.info/peff>`_ for
more up-to-date information on the standard.

Data manipulation
-----------------

Classes
.......

The PEFF parser inherits several properties from implementation in the :mod:`~.fasta` module,
building on top of the :class:`~.TwoLayerIndexedFASTA` reader.

Available classes:

  :py:class:`IndexedPEFF` - Parse a PEFF format file in binary-mode, supporting
  direct indexing by header string or by tag.

  :py:class:`PEFFCatalog` - Normalize one or more PEFF sources into separate
  protein entity, sequence version and annotation occurrence tables.

  :py:class:`ProteinEntity` - Stable protein identity, independent of sequence
  version.

  :py:class:`SequenceVersion` - One sequence at a particular ``SV``.

  :py:class:`ModificationOccurrence` - One declared annotation occurrence,
  retaining its original coordinates and affected residues.

Catalog semantics
.................

The catalog keeps the declared coordinates verbatim. Range annotations use
1-based inclusive positions and their replacement length is read from the
declared replacement field. In particular, an empty ``VariantComplex``
replacement is a zero-length replacement (a deletion), not a missing value.

Annotations are attached to the sequence version on whose description line
they were declared. When a new sequence version is registered, annotations of
older versions are marked ``pending_remap``; they are never silently applied
to the new sequence using old indices. Use
:py:meth:`PEFFCatalog.remap_annotations` to establish an explicit
correspondence. Occurrences for which no unambiguous correspondence can be
established remain ``pending_remap``.

"""

#   Copyright 2018 Joshua Klein, Lev Levitsky
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import re
import os
from dataclasses import dataclass, field as dataclass_field, replace as dataclass_replace
try:
    from collections.abc import Sequence as SequenceABC, Mapping
except ImportError:
    from collections import Sequence as SequenceABC, Mapping
from collections import OrderedDict, defaultdict

from .fasta import TwoLayerIndexedFASTA
from .auxiliary import PyteomicsError


STATUS_CURRENT = "current"
STATUS_MAPPED = "mapped"
STATUS_PENDING_REMAP = "pending_remap"
STATUS_SUPERSEDED = "superseded"

CURRENT = STATUS_CURRENT
MAPPED = STATUS_MAPPED
PENDING_REMAP = STATUS_PENDING_REMAP
SUPERSEDED = STATUS_SUPERSEDED

ANNOTATION_TYPES = (
    "ModResPsi",
    "ModRes",
    "ModResUnimod",
    "VariantSimple",
    "VariantComplex",
    "Processed",
    "Domain",
    "Conflict",
    "DisulfideBond",
    "Proteoform",
)

CORRECTION_TYPES = (
    "SequenceCorrection",
    "SeqCorrection",
    "Correction",
)

MODIFICATION_TYPES = (
    "ModResPsi",
    "ModRes",
    "ModResUnimod",
)


@dataclass(frozen=True)
class ProteinEntity:
    """A protein entity in a PEFF catalog.

    An entity is the stable protein identity, independent of any particular
    sequence version. Its attributes mirror the entry description line.
    """
    accession: str
    prefix: str
    name: str = None
    gene: str = None
    ncbi_tax_id: int = None
    tax_name: str = None
    db_unique_id: str = None
    entry_version: str = None
    protein_evidence: str = None
    extra: Mapping = dataclass_field(default_factory=dict, compare=False, repr=False)

    @property
    def id(self):
        return self.accession

    @property
    def entity_id(self):
        return self.accession

    def as_dict(self):
        return {
            "accession": self.accession,
            "prefix": self.prefix,
            "name": self.name,
            "gene": self.gene,
            "ncbi_tax_id": self.ncbi_tax_id,
            "tax_name": self.tax_name,
            "db_unique_id": self.db_unique_id,
            "entry_version": self.entry_version,
            "protein_evidence": self.protein_evidence,
        }


@dataclass(frozen=True)
class SequenceVersion:
    """A particular sequence version of a :class:`ProteinEntity`.

    ``sequence_version`` corresponds to the PEFF ``SV`` tag and
    ``entry_version`` to ``EV``. ``sequence`` is the sequence exactly as
    declared by this version, with the terminal ``*`` removed in the same way
    as in other Pyteomics FASTA readers. ``length`` is the length of the
    parsed sequence; ``declared_length`` is the ``Length`` field as declared
    in PEFF (which may include a terminal stop symbol).
"""
    entity_id: str
    sequence_version: str
    sequence: str
    entry_version: str = None
    length: int = None
    declared_length: int = None
    sequence_type: str = None
    status: str = STATUS_CURRENT
    previous_sequence: str = None
    previous_sequence_version: str = None
    source: str = None

    @property
    def id(self):
        return (self.entity_id, self.sequence_version)

    def residue(self, position):
        """Return the residue at 1-based ``position`` in this version."""
        if position < 1 or position > len(self.sequence):
            raise IndexError(position)
        return self.sequence[position - 1]

    def as_dict(self):
        return {
            "entity_id": self.entity_id,
            "sequence_version": self.sequence_version,
            "sequence": self.sequence,
            "entry_version": self.entry_version,
            "length": self.length,
            "declared_length": self.declared_length,
            "sequence_type": self.sequence_type,
            "status": self.status,
            "previous_sequence": self.previous_sequence,
            "previous_sequence_version": self.previous_sequence_version,
        }


@dataclass(frozen=True)
class ModificationOccurrence:
    """One occurrence of an annotation on a particular sequence version.

    The original declaration is kept verbatim:

    ``start`` and ``end`` are 1-based, inclusive positions *as declared in the
    source*. For a single-position annotation, ``end == start``.

    ``replacement`` and ``replacement_length`` describe the declared
    replacement string. They are ``None`` when the annotation does not declare
    a replacement (for example, a point modification or ``Processed``).
    ``declared_length`` is the inclusive span length derived from ``start`` and
    ``end``, independent of any replacement.

    ``affected_residues`` records the source residues at the *declared*
    coordinates. It is intentionally not recomputed after a correction, since
    doing so would silently apply old indices to a new sequence.

    After a correction, ``position``/``start``/``end`` remain the declared old
    coordinates. ``current_start`` and ``current_end`` are filled in only by an
    explicit remap; until then ``status`` is ``pending_remap``.
    """
    entity_id: str
    sequence_version: str
    annotation_type: str
    start: int = None
    end: int = None
    end_declared: bool = False
    replacement: str = None
    replacement_length: int = None
    accession: str = None
    name: str = None
    comment: str = None
    annotation_id: int = None
    raw: str = None
    affected_residues: str = None
    status: str = STATUS_CURRENT
    current_start: int = None
    current_end: int = None
    current_affected_residues: str = None
    target_sequence_version: str = None
    remap_note: str = None

    @property
    def position(self):
        return self.start

    @property
    def original_position(self):
        return self.start

    @property
    def original_start(self):
        return self.start

    @property
    def original_end(self):
        return self.end

    @property
    def original_range(self):
        return (self.start, self.end)

    @property
    def declared_start(self):
        return self.start

    @property
    def declared_end(self):
        return self.end

    @property
    def range(self):
        return (self.start, self.end)

    @property
    def is_range(self):
        return (self.start is not None and self.end is not None
                and (self.end_declared or self.end != self.start))

    @property
    def declared_length(self):
        """Declared span length (inclusive), independent of any replacement."""
        if self.start is None:
            return None
        if self.end is None:
            return 1
        return self.end - self.start + 1

    @property
    def length(self):
        return self.declared_length

    @property
    def mapped_range(self):
        if self.status == STATUS_MAPPED:
            return (self.current_start, self.current_end)
        return None

    @property
    def pending_remap(self):
        return self.status == STATUS_PENDING_REMAP

    @property
    def is_pending(self):
        return self.pending_remap

    @property
    def remapped(self):
        return self.status == STATUS_MAPPED

    @property
    def unmodified(self):
        return self.status == STATUS_CURRENT

    def as_dict(self):
        """Return a plain-dict view, useful for tabular export."""
        return {
            "entity_id": self.entity_id,
            "sequence_version": self.sequence_version,
            "annotation_type": self.annotation_type,
            "start": self.start,
            "end": self.end,
            "end_declared": self.end_declared,
            "replacement": self.replacement,
            "replacement_length": self.replacement_length,
            "accession": self.accession,
            "name": self.name,
            "comment": self.comment,
            "annotation_id": self.annotation_id,
            "raw": self.raw,
            "affected_residues": self.affected_residues,
            "status": self.status,
            "current_start": self.current_start,
            "current_end": self.current_end,
            "current_affected_residues": self.current_affected_residues,
            "target_sequence_version": self.target_sequence_version,
            "remap_note": self.remap_note,
        }


@dataclass(frozen=True)
class SequenceCorrection:
    """A declared correction between two sequence versions.

    ``position`` is the 1-based position in the *previous* sequence at which
    the corrected block starts. ``old`` is the replaced substring and ``new``
    is the replacement. ``declared_length`` is ``len(old)`` and
    ``replacement_length`` is ``len(new)``. An empty ``old`` declares an
    insertion; an empty ``new`` declares a deletion.

    A correction affects annotations *only* through an explicit remapping; it
    never changes their recorded indices by itself.
    """
    entity_id: str
    from_version: str
    to_version: str = None
    position: int = None
    old: str = ""
    new: str = ""
    annotation_id: int = None
    raw: str = None

    @property
    def start(self):
        return self.position

    @property
    def end(self):
        return self.position + self.declared_length - 1

    @property
    def range(self):
        return (self.start, self.end)

    @property
    def declared_length(self):
        return len(self.old)

    @property
    def replacement_length(self):
        return len(self.new)


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _version_sort_key(version):
    """Sort version labels numerically when possible, lexically otherwise."""
    try:
        return (0, int(version), "")
    except (TypeError, ValueError):
        return (1, 0, str(version))


def _map_occurrence(occurrence, start, end, delta, new, target_sequence):
    """Decide the target range for one occurrence.

    ``start``/``end`` describe the corrected block in the source sequence;
    ``delta`` is ``len(new) - block_length``. Returns
    ``((new_start, new_end), None)`` on success or ``(None, reason)`` when no
    unambiguous correspondence exists.
    """
    if occurrence.start is None:
        if occurrence.annotation_type == "DisulfideBond":
            return None, (occurrence.remap_note or
                          "bond partner annotations must be remapped first")
        return None, "annotation has no declared position"

    block_length = end - start + 1 if end >= start else 0
    if block_length == 0:
        if occurrence.end < start:
            return (occurrence.start, occurrence.end), None
        if occurrence.start >= start:
            return (occurrence.start + delta, occurrence.end + delta), None
        return (occurrence.start, occurrence.end + delta), None

    if occurrence.end < start or occurrence.start > end:
        shift = delta if occurrence.start > end else 0
        return (occurrence.start + shift, occurrence.end + shift), None

    if occurrence.start >= start and occurrence.end <= end:
        if occurrence.is_range and occurrence.start == start and occurrence.end == end:
            if len(new) == 0:
                return None, ("annotation is the deletion applied by the "
                              "correction and has no target residues")
            return (start, start + len(new) - 1), None
        if block_length == len(new):
            if occurrence.is_range:
                return (occurrence.start, occurrence.end), None
            if occurrence.affected_residues == target_sequence[occurrence.start - 1]:
                return (occurrence.start, occurrence.end), None
            return None, ("point annotation covers a changed residue "
                          "and has no explicit correspondence")
        if len(new) == 0:
            return None, "annotation falls inside a deleted block"
        return None, "annotation falls inside a length-changing corrected block"

    if occurrence.is_range and occurrence.start <= start and occurrence.end >= end:
        return (occurrence.start, occurrence.end + delta), None
    return None, "annotation overlaps the corrected block boundary"


def _parse_annotation(annotation_type, feature):
    """Interpret the declared fields of a PEFF annotation.

    The field layout depends on the annotation type:

    * ``ModResPsi`` / ``ModRes`` / ``ModResUnimod``: ``position|accession|name``
    * ``VariantSimple``: ``position|replacement``
    * ``VariantComplex``: ``start|end|replacement``
    * ``Processed`` / ``Domain``: ``start|end|name``

    Positions are used exactly as declared. ``replacement_length`` is the
    length of the declared replacement, so an empty ``VariantComplex``
    replacement is a deletion of length 0.
    """
    fields = list(feature)
    if not fields:
        return None

    if annotation_type in ("DisulfideBond", "Proteoform"):
        # These values are annotation identifiers, not positions.
        return {
            "start": None,
            "end": None,
            "end_declared": False,
            "replacement": None,
            "replacement_length": None,
            "accession": None,
            "name": None,
            "comment": "|".join(map(str, fields)),
        }

    start = _int_or_none(fields[0])
    if start is None:
        return None

    result = {
        "start": start,
        "end": start,
        "end_declared": False,
        "replacement": None,
        "replacement_length": None,
        "accession": None,
        "name": None,
        "comment": None,
    }

    if annotation_type in MODIFICATION_TYPES:
        result["accession"] = fields[1] if len(fields) > 1 and fields[1] else None
        if len(fields) > 2:
            result["name"] = fields[2] if fields[2] else None
        return result

    if annotation_type == "VariantSimple":
        replacement = fields[1] if len(fields) > 1 else None
        result["replacement"] = replacement
        result["replacement_length"] = None if replacement is None else len(str(replacement))
        return result

    if annotation_type in ("VariantComplex", "Variant"):
        if len(fields) > 1 and _int_or_none(fields[1]) is not None:
            result["end"] = _int_or_none(fields[1])
            result["end_declared"] = True
            replacement = fields[2] if len(fields) > 2 else ""
        else:
            replacement = fields[1] if len(fields) > 1 else ""
        result["replacement"] = replacement if replacement is not None else ""
        result["replacement_length"] = len(result["replacement"])
        if len(fields) > 3:
            result["comment"] = fields[3] if fields[3] else None
        return result

    if annotation_type in ("Processed", "Domain", "SignalPeptide", "TransitPeptide",
                           "Propeptide", "MatureProtein", "InitiatorMethionine"):
        if len(fields) > 1 and _int_or_none(fields[1]) is not None:
            result["end"] = _int_or_none(fields[1])
            result["end_declared"] = True
            if len(fields) > 2:
                result["name"] = fields[2] if fields[2] else None
        else:
            result["name"] = fields[1] if len(fields) > 1 and fields[1] else None
        return result

    if annotation_type == "Conflict":
        if len(fields) > 1 and _int_or_none(fields[1]) is not None:
            result["end"] = _int_or_none(fields[1])
            result["end_declared"] = True
            if len(fields) > 2:
                result["comment"] = fields[2] if fields[2] else None
        elif len(fields) > 1:
            result["comment"] = fields[1] if fields[1] else None
        return result

    if len(fields) > 1:
        if _int_or_none(fields[1]) is not None:
            result["end"] = _int_or_none(fields[1])
            if len(fields) > 2:
                result["comment"] = fields[2] if fields[2] else None
        else:
            result["replacement"] = fields[1]
            result["replacement_length"] = len(str(fields[1]))
    return result


class _QueryDescriptor:
    """Descriptor that supports both attribute-style and call-style access."""

    def __init__(self, factory):
        self.factory = factory
        self.__doc__ = factory.__doc__

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        return self.factory(instance)


class _OccurrenceQuery:
    """List-like view over annotation occurrences, also callable with filters.

    This lets ``catalog.occurrences`` be used both as a table (iterate, len,
    indexing) and as a query method (``catalog.occurrences(entity_id=...)``).
    """

    def __init__(self, catalog, entity_id=None, sequence_version=None,
                 annotation_type=None, include_pending=True):
        self.catalog = catalog
        self.entity_id = entity_id
        self.sequence_version = sequence_version
        self.annotation_type = annotation_type
        self.include_pending = include_pending

    def _items(self):
        return list(self.catalog.iter_occurrences(
            self.entity_id, self.sequence_version,
            self.annotation_type, self.include_pending))

    def __iter__(self):
        return self.catalog.iter_occurrences(
            self.entity_id, self.sequence_version,
            self.annotation_type, self.include_pending)

    def __len__(self):
        return sum(1 for _ in self)

    def __getitem__(self, index):
        return self._items()[index]

    def __bool__(self):
        return len(self) > 0

    def __call__(self, entity_id=None, sequence_version=None,
                 annotation_type=None, include_pending=True):
        return _OccurrenceQuery(
            self.catalog,
            self.entity_id if entity_id is None else entity_id,
            self.sequence_version if sequence_version is None else sequence_version,
            self.annotation_type if annotation_type is None else annotation_type,
            include_pending)

    def __repr__(self):
        return "{self.__class__.__name__}({items})".format(
            self=self, items=self._items())


class _PendingQuery:
    """List-like view over occurrences waiting for an explicit remap."""

    def __init__(self, catalog, entity_id=None):
        self.catalog = catalog
        self.entity_id = entity_id

    def _items(self):
        return [a for a in self.catalog.pending_remap
                if self.entity_id is None or a.entity_id == self.entity_id]

    def __iter__(self):
        return iter(self._items())

    def __len__(self):
        return len(self._items())

    def __getitem__(self, index):
        return self._items()[index]

    def __bool__(self):
        return len(self) > 0

    def __call__(self, entity_id=None):
        return _PendingQuery(
            self.catalog,
            self.entity_id if entity_id is None else entity_id)

    def __repr__(self):
        return "{self.__class__.__name__}({items})".format(
            self=self, items=self._items())


class PEFFCatalog:
    """Normalized view over one or more PEFF readers.

    The catalog separates three things that a PEFF description line mixes
    together:

    * :class:`ProteinEntity` objects (protein identity, in :attr:`entities`),
    * :class:`SequenceVersion` objects (one sequence per ``SV``, in
      :attr:`sequence_versions`),
    * :class:`ModificationOccurrence` objects (every annotation occurrence,
      with its declared coordinates, in :attr:`annotations`).

    Annotations are always attached to the sequence version whose description
    line declared them. When that sequence version is corrected to a newer
    one, the old annotations are *not* silently reinterpreted using the old
    indices. They are either explicitly remapped with
    :meth:`remap_annotations` or marked ``pending_remap``.

    Parameters
    ----------
    source : str, file or iterable
        A PEFF file, or an iterable of parsed PEFF entries.
    """

    def __init__(self, source=None):
        self.entities = OrderedDict()
        self.sequence_versions = OrderedDict()
        self.annotations = []
        self.corrections = []
        self.pending_remap = []
        self.sequence_type = None
        if source is not None:
            self.add(source)

    def add(self, source):
        """Add entries from a PEFF reader, file name, or entry iterable."""
        if isinstance(source, PEFFCatalog):
            for entity in source.entities.values():
                self.entities.setdefault(entity.accession, entity)
            for version in source.sequence_versions.values():
                self.sequence_versions.setdefault(version.id, version)
            self.annotations.extend(source.annotations)
            self.corrections.extend(source.corrections)
            self.pending_remap = [a for a in self.annotations if a.pending_remap]
            if self.sequence_type is None:
                self.sequence_type = source.sequence_type
        elif isinstance(source, (str, bytes, os.PathLike)):
            source = os.fspath(source)
            with IndexedPEFF(source) as reader:
                self.add_reader(reader)
        elif hasattr(source, "get_entry") and hasattr(source, "header_blocks"):
            self.add_reader(source)
        elif hasattr(source, "description") and hasattr(source, "sequence"):
            self.add_entry(source)
        elif hasattr(source, "read"):
            with IndexedPEFF(source) as reader:
                self.add_reader(reader)
        else:
            for entry in source:
                self.add_entry(entry)
        return self

    def add_reader(self, reader):
        if self.sequence_type is None:
            blocks = getattr(reader, "header_blocks", None)
            if blocks:
                self.sequence_type = blocks[0].get("SequenceType")
        for entry in reader:
            self.add_entry(entry)
        return self

    def add_entry(self, entry):
        description = entry.description
        prefix = description.get("Prefix")
        db_unique_id = description.get("DbUniqueId")
        tag = description.get("Tag")
        entity_id = db_unique_id or tag
        name = description.get("PName")
        sequence_version = str(description.get("SV")) if description.get("SV") is not None else "1"
        self.entities.setdefault(entity_id, ProteinEntity(
            accession=entity_id,
            prefix=prefix,
            name=name,
            gene=description.get("GName"),
            ncbi_tax_id=description.get("NcbiTaxId"),
            tax_name=description.get("TaxName"),
            db_unique_id=db_unique_id,
            entry_version=str(description.get("EV")) if description.get("EV") is not None else None,
            protein_evidence=description.get("PE"),
            extra={k: v for k, v in description.items()
                   if k not in {"Prefix", "Tag", "DbUniqueId", "PName", "GName",
                                "NcbiTaxId", "TaxName", "EV", "PE", "SV", "Length",
                                "SequenceType"} and k not in ANNOTATION_TYPES}
        ))

        length = description.get("Length")
        sequence = entry.sequence
        declared_length = None
        if length is not None:
            declared_length = int(length)
            if declared_length not in (len(sequence), len(sequence) + 1):
                raise PyteomicsError(
                    "Declared Length ({}) does not match the sequence length ({}) "
                    "for {}".format(length, len(sequence), entity_id))
        version = SequenceVersion(
            entity_id=entity_id,
            sequence_version=sequence_version,
            sequence=sequence,
            entry_version=str(description.get("EV")) if description.get("EV") is not None else None,
            length=len(sequence),
            declared_length=declared_length,
            sequence_type=self.sequence_type,
            status=STATUS_CURRENT,
        )
        key = version.id
        if key in self.sequence_versions:
            raise PyteomicsError(
                "Duplicate sequence version {} for {}".format(sequence_version, entity_id))
        self.sequence_versions[key] = version

        self._add_annotations(entry, entity_id, sequence_version)
        self._supersede_older_versions(entity_id, sequence_version)
        return self

    def _supersede_older_versions(self, entity_id, current_version):
        current_key = _version_sort_key(current_version)
        for version in list(self.sequence_versions.values()):
            if version.entity_id != entity_id:
                continue
            if version.sequence_version == str(current_version):
                continue
            if _version_sort_key(version.sequence_version) < current_key:
                self.supersede(entity_id, version.sequence_version, current_version)

    def _add_annotations(self, entry, entity_id, sequence_version):
        description = entry.description
        sequence = entry.sequence
        for annotation_type in ANNOTATION_TYPES + CORRECTION_TYPES:
            value = description.get(annotation_type)
            if value is None:
                continue
            features = value if isinstance(value, list) else [value]
            for feature in features:
                if not isinstance(feature, IndexedPEFF._PEFFFeature):
                    continue
                if annotation_type in CORRECTION_TYPES:
                    self._add_correction(entity_id, sequence_version, feature)
                    continue
                parsed = _parse_annotation(annotation_type, feature)
                if parsed is None:
                    continue
                start = parsed["start"]
                end = parsed["end"]
                affected = None
                if start is not None and end is not None and 1 <= start <= end <= len(sequence):
                    affected = sequence[start - 1:end]
                self.annotations.append(ModificationOccurrence(
                    entity_id=entity_id,
                    sequence_version=sequence_version,
                    annotation_type=annotation_type,
                    start=start,
                    end=end,
                    end_declared=parsed["end_declared"],
                    replacement=parsed["replacement"],
                    replacement_length=parsed["replacement_length"],
                    accession=parsed["accession"],
                    name=parsed["name"],
                    comment=parsed["comment"],
                    annotation_id=feature.id,
                    raw=feature.raw,
                    affected_residues=affected,
                    status=STATUS_CURRENT,
                ))
        self.pending_remap = [a for a in self.annotations if a.pending_remap]

    def _add_correction(self, entity_id, sequence_version, feature):
        fields = list(feature)
        if len(fields) < 3:
            raise PyteomicsError("SequenceCorrection requires position, old and new")
        self.corrections.append(SequenceCorrection(
            entity_id=entity_id,
            from_version=sequence_version,
            to_version=None,
            position=int(fields[0]),
            old=str(fields[1]),
            new=str(fields[2]),
            annotation_id=feature.id,
            raw=feature.raw,
        ))

    def get_entity(self, entity_id):
        return self.entities[entity_id]

    def get_sequence(self, entity_id, sequence_version=None):
        """Return the requested :class:`SequenceVersion`."""
        if sequence_version is None:
            versions = [v for v in self.sequence_versions.values() if v.entity_id == entity_id]
            if not versions:
                raise KeyError(entity_id)
            return versions[-1]
        return self.sequence_versions[(entity_id, str(sequence_version))]

    def versions_of(self, entity_id):
        """Return all sequence versions of ``entity_id`` in insertion order."""
        return [v for v in self.sequence_versions.values() if v.entity_id == entity_id]

    def current_version(self, entity_id):
        """Return the latest registered sequence version of ``entity_id``."""
        return self.get_sequence(entity_id)

    def iter_occurrences(self, entity_id=None, sequence_version=None, annotation_type=None,
                         include_pending=True):
        """Iterate over annotation occurrences, optionally filtered."""
        for occurrence in self.annotations:
            if entity_id is not None and occurrence.entity_id != entity_id:
                continue
            if sequence_version is not None and occurrence.sequence_version != str(sequence_version):
                continue
            if annotation_type is not None and occurrence.annotation_type != annotation_type:
                continue
            if not include_pending and occurrence.pending_remap:
                continue
            yield occurrence

    @_QueryDescriptor
    def occurrences(self):
        """List-like view over annotation occurrences.

        Can be iterated directly (``for a in catalog.occurrences``) or called
        with filters (``catalog.occurrences(annotation_type='ModResPsi')``).
        """
        return _OccurrenceQuery(self)

    @_QueryDescriptor
    def pending(self):
        """List-like view over occurrences without an established mapping."""
        return _PendingQuery(self)

    @property
    def proteins(self):
        """Mapping of accession to :class:`ProteinEntity`."""
        return self.entities

    @property
    def versions(self):
        """Mapping of ``(entity_id, sequence_version)`` to :class:`SequenceVersion`."""
        return self.sequence_versions

    @property
    def occurrences_table(self):
        """All annotation occurrences as a list of plain dicts."""
        return [a.as_dict() for a in self.annotations]

    def occurrences_for(self, entity_id, sequence_version=None, annotation_type=None):
        return list(self.iter_occurrences(entity_id, sequence_version, annotation_type))

    def corrected_sequence(self, entity_id, from_version, correction=None):
        """Apply a declared correction to ``from_version`` and return the new sequence.

        ``correction`` may be a :class:`SequenceCorrection` or a
        ``(position, old, new)`` tuple. The correction is validated against the
        source sequence: the declared ``old`` substring must occur at the
        declared 1-based position. No positional shifting is guessed.
        """
        version = self.get_sequence(entity_id, from_version)
        correction = correction or self.corrections_for(entity_id, from_version)[-1]
        if isinstance(correction, tuple):
            position, old, new = correction
        else:
            position, old, new = correction.position, correction.old, correction.new
        start = position - 1
        if start < 0 or version.sequence[start:start + len(old)] != old:
            raise PyteomicsError(
                "Sequence correction does not match {} version {}: expected {!r} "
                "at position {}".format(entity_id, from_version, old, position))
        sequence = version.sequence[:start] + new + version.sequence[start + len(old):]
        return sequence

    def infer_correction(self, entity_id, from_version, to_version):
        """Derive the single contiguous correction block between two versions.

        Returns ``(position, old, new)`` with 1-based ``position`` in
        ``from_version``. A :py:class:`PyteomicsError` is raised if the two
        sequences differ in more than one contiguous block, because such a
        change has no unambiguous declared range to use for remapping.
        """
        import difflib
        source = self.get_sequence(entity_id, from_version).sequence
        target = self.get_sequence(entity_id, to_version).sequence
        matcher = difflib.SequenceMatcher(a=source, b=target, autojunk=False)
        blocks = [op for op in matcher.get_opcodes() if op[0] != "equal"]
        if not blocks:
            return 1, "", ""
        if len(blocks) > 1:
            raise PyteomicsError(
                "Sequences of {} versions {} and {} differ in {} blocks; "
                "a single declared correction is required".format(
                    entity_id, from_version, to_version, len(blocks)))
        _, i1, i2, j1, j2 = blocks[0]
        return i1 + 1, source[i1:i2], target[j1:j2]

    def corrections_for(self, entity_id, from_version=None):
        return [c for c in self.corrections
                if c.entity_id == entity_id
                and (from_version is None or c.from_version == str(from_version))]

    def add_version(self, entity_id, sequence_version, sequence, entry_version=None,
                    previous_sequence=None, previous_sequence_version=None):
        """Register a new sequence version of an existing entity.

        Annotations declared for the previous version are attached to that old
        version and are *not* copied onto the new sequence. They become visible
        as ``pending_remap`` until :meth:`remap_annotations` establishes a
        mapping; use that method to carry them over explicitly.
        """
        if entity_id not in self.entities:
            raise KeyError(entity_id)
        if (entity_id, str(sequence_version)) in self.sequence_versions:
            raise PyteomicsError(
                "Duplicate sequence version {} for {}".format(
                    sequence_version, entity_id))
        version = SequenceVersion(
            entity_id=entity_id,
            sequence_version=str(sequence_version),
            sequence=sequence,
            entry_version=entry_version,
            length=len(sequence),
            sequence_type=self.sequence_type,
            status=STATUS_CURRENT,
            previous_sequence=previous_sequence,
            previous_sequence_version=(
                str(previous_sequence_version)
                if previous_sequence_version is not None else None),
        )
        self.sequence_versions[version.id] = version
        if previous_sequence_version is not None:
            self.supersede(entity_id, previous_sequence_version, sequence_version)
        else:
            self._supersede_older_versions(entity_id, sequence_version)
        return version

    def supersede(self, entity_id, from_version, to_version):
        """Mark unchanged annotations of ``from_version`` as waiting for remapping.

        This does not move any coordinate. It only records the fact that the
        annotations were declared against an older sequence, so callers cannot
        mistake the old indices for current ones.
        """
        updated = []
        for index, occurrence in enumerate(self.annotations):
            if (occurrence.entity_id == entity_id
                    and occurrence.sequence_version == str(from_version)
                    and occurrence.status == STATUS_CURRENT):
                occurrence = dataclass_replace(
                    occurrence,
                    status=STATUS_PENDING_REMAP,
                    target_sequence_version=str(to_version),
                    remap_note="sequence version superseded; explicit remap required")
                self.annotations[index] = occurrence
                updated.append(occurrence)
        for index, version in enumerate(self.sequence_versions.values()):
            if (version.entity_id == entity_id
                    and version.sequence_version == str(from_version)
                    and version.status == STATUS_CURRENT):
                self.sequence_versions[version.id] = dataclass_replace(
                    version, status=STATUS_SUPERSEDED)
        self.pending_remap = [a for a in self.annotations if a.pending_remap]
        return updated

    def remap_annotations(self, entity_id, from_version, to_version, correction=None,
                          annotations=None, mark_unmapped=True):
        """Explicitly remap annotations from one sequence version to another.

        The declared coordinates of every occurrence are preserved. A new
        coordinate is established only when the correction leaves no ambiguity:

        * a point annotation outside the corrected block shifts by the declared
          length difference; inside a same-length block it keeps its position
          only if the annotated residue is unchanged;
        * a range annotation entirely before or after the corrected block shifts
          as a whole; inside a same-length block it keeps its range only if the
          affected residues are unchanged; a range that exactly covers the
          corrected block maps onto the declared replacement block;
        * an insertion (empty ``old``) shifts all annotations at or after its
          declared position.

        Anything else (point annotation inside a length-changing block, a range
        crossing the block boundary, a different-length replacement range) is
        marked ``pending_remap`` instead of being assigned a guessed position.

        The returned tuple is ``(remapped, pending)``, both lists of updated
        :class:`ModificationOccurrence` objects. The catalog's list is updated
        in place. If ``mark_unmapped`` is :py:const:`False`, ambiguous
        occurrences are left untouched instead of being marked.
        """
        target = self.get_sequence(entity_id, to_version)
        if correction is None:
            corrections = self.corrections_for(entity_id, from_version)
            if not corrections:
                raise PyteomicsError(
                    "No sequence correction declared for {} version {}".format(
                        entity_id, from_version))
            correction = corrections[-1]
        if isinstance(correction, tuple):
            position, old, new = correction
            declared_length = len(old)
        else:
            position, old, new = correction.position, correction.old, correction.new
            declared_length = correction.declared_length
        expected = self.corrected_sequence(
            entity_id, from_version, (position, old, new))
        if expected != target.sequence:
            raise PyteomicsError(
                "Correction does not reproduce target version {} of {}".format(
                    to_version, entity_id))
        if declared_length == 0:
            start = position
            end = position - 1
        else:
            start = position
            end = position + declared_length - 1
        delta = len(new) - declared_length

        selected = list(annotations) if annotations is not None else list(
            self.iter_occurrences(entity_id, from_version))
        remapped = []
        pending = []
        updated_by_index = {}
        for occurrence in selected:
            index = self._annotation_index(occurrence)
            new_range, failure = _map_occurrence(
                occurrence, start, end, delta, new, target.sequence)
            if failure:
                if not mark_unmapped:
                    pending.append(occurrence)
                    continue
                updated = dataclass_replace(
                    occurrence,
                    status=STATUS_PENDING_REMAP,
                    current_start=None,
                    current_end=None,
                    current_affected_residues=None,
                    target_sequence_version=str(to_version),
                    remap_note=failure)
                pending.append(updated)
            else:
                new_start, new_end = new_range
                current_affected = None
                if new_start is not None and 1 <= new_start <= new_end <= len(target.sequence):
                    current_affected = target.sequence[new_start - 1:new_end]
                updated = dataclass_replace(
                    occurrence,
                    status=STATUS_MAPPED,
                    current_start=new_start,
                    current_end=new_end,
                    current_affected_residues=current_affected,
                    target_sequence_version=str(to_version),
                    remap_note=None)
                remapped.append(updated)
            updated_by_index[index] = updated

        for index, updated in updated_by_index.items():
            self.annotations[index] = updated
        self.pending_remap = [a for a in self.annotations if a.pending_remap]
        return remapped, pending

    def _annotation_index(self, occurrence):
        for index, candidate in enumerate(self.annotations):
            if candidate is occurrence or candidate == occurrence:
                return index
        raise ValueError("{!r} is not an occurrence of this catalog".format(occurrence))


def read(source=None, **kwargs):
    """Open a PEFF file and return an :class:`IndexedPEFF` reader.

    This mirrors the functional interface of :mod:`pyteomics.fasta`.
    """
    return IndexedPEFF(source, **kwargs)


def read_catalog(source=None, **kwargs):
    """Open a PEFF source and return a normalized :class:`PEFFCatalog`."""
    return PEFFCatalog(source)


class Header(Mapping):
    """Hold parsed properties of a key-value pair like a sequence's
    definition line.

    This object supports the :class:`Mapping` interface, and
    keys may be accessed by attribute access notation.
    """
    def __init__(self, mapping, original=None):
        self._mapping = mapping
        self.original = original

    def __getitem__(self, key):
        return self._mapping[key]

    def __iter__(self):
        return iter(self._mapping)

    def items(self):
        return self._mapping.items()

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def __len__(self):
        return len(self._mapping)

    def __contains__(self, key):
        return key in self._mapping

    def __getattr__(self, key):
        if key == "_mapping":
            raise AttributeError(key)
        try:
            return self._mapping[key]
        except KeyError:
            raise AttributeError(key)

    def __repr__(self):
        return "{self.__class__.__name__}({mapping})".format(self=self, mapping=dict(self._mapping))

    def __hash__(self):
        return hash(self.defline)

    def __eq__(self, other):
        try:
            return self._mapping == other._mapping
        except AttributeError:
            return str(self) == str(other)

    def __ne__(self, other):
        return not (self == other)

    def __dir__(self):
        base = set(dir(super(Header, self)))
        keys = set(self._mapping.keys())
        return list(base | keys)


class IndexedPEFF(TwoLayerIndexedFASTA):
    """Creates an :py:class:`IndexedPEFF` object.

    Parameters
    ----------
    source : str or file
        The file to read. If a file object, it needs to be in *rb* mode.
    parse : bool, optional
        Defines whether the descriptions should be parsed in the produced tuples.
        Default is :py:const:`True`.
    kwargs : passed to the :py:class:`TwoLayerIndexedFASTA` constructor.
    """

    kv_pattern = re.compile(r"\\(?P<key>\S+)=(?P<value>.+?)(?:\s(?=\\)|$)")
    header_pattern = re.compile(r"^>?(\S+):(\S+)")
    has_feature_index = re.compile(r"^\(?(\d+):")
    header_group = 2

    class _PEFFFeature(SequenceABC):
        def __init__(self, *fields, **kwargs):
            self.fields = tuple(fields)
            self.id = kwargs.get('id')
            self.feature_type = kwargs.get("feature_type")
            self.raw = kwargs.get("raw")

        def __eq__(self, other):
            return tuple(self) == tuple(other)

        def __ne__(self, other):
            return not (self == other)

        def __getitem__(self, i):
            return self.fields[i]

        def __len__(self):
            return len(self.fields)

        def __repr__(self):
            return repr(tuple(self))

        def __str__(self):
            return "(%s%s)" % (
                '%r:' % self.id if self.id is not None else '',
                '|'.join(map(str, self)), )

    def __init__(self, source, ignore_comments=False, **kwargs):
        super(IndexedPEFF, self).__init__(
            source, ignore_comments=ignore_comments, parser=self.parser,
            header_pattern=self.header_pattern, **kwargs)
        self.header_blocks = []
        self.comments = []
        self.version = None
        self.number_of_entries = 0
        self._parse_header()

    def _parse_header(self):
        self.seek(0)
        line = self.readline().decode("ascii")
        if not line.startswith("# PEFF"):
            raise ValueError("Not a PEFF File")
        self.version = tuple(map(int, line.strip()[7:].split(".")))
        current_block = defaultdict(list)
        in_header = True
        while in_header:
            line = self.readline().decode("ascii")
            if not line.startswith("#"):
                in_header = False
            line = line.strip()[2:]
            if '=' in line:
                key, value = line.split("=", 1)
                if key == "GeneralComment":
                    self.comments.append(value)
                else:
                    current_block[key].append(value)
            if line.startswith("//"):
                if current_block:
                    self.header_blocks.append(
                        Header(OrderedDict((k, v if len(v) > 1 else v[0])
                                           for k, v in current_block.items())))
                current_block = defaultdict(list)
        number_of_entries = 0
        for block in self.header_blocks:
            try:
                number_of_entries += int(block['NumberOfEntries'])
            except KeyError:
                pass
        self.number_of_entries = number_of_entries

    def _extract_parenthesis_list(self, text):
        chunks = []
        chunk = []
        paren_level = 0
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            i += 1
            if c == "(":
                if paren_level > 0:
                    chunk.append(c)
                paren_level += 1
            elif c == ")":
                if paren_level > 1:
                    chunk.append(c)
                paren_level -= 1
                if paren_level == 0:
                    if chunk:
                        chunks.append(chunk)
                    chunk = []
            else:
                chunk.append(c)
        chunks = list(map(''.join, chunks))
        return chunks

    def _split_pipe_separated_tuple(self, text):
        parts = text.split("|")
        return parts

    def _coerce_types(self, key, value):
        value = value.strip()
        feature_id_match = self.has_feature_index.search(value)
        if feature_id_match:
            feature_id = int(feature_id_match.group(1))
            value = self.has_feature_index.sub('', value)
        else:
            feature_id = None
        if "|" in value:
            value = self._split_pipe_separated_tuple(value)
            result = []
            for i, v in enumerate(value):
                result.append(self._coerce_value(key, v, i))
            return self._PEFFFeature(*result, feature_type=key, id=feature_id,
                                     raw='|'.join(value))
        else:
            return self._coerce_value(key, value, 0)

    def _coerce_value(self, key, value, index):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        return str(value)

    def parser(self, line):
        original = line
        match = self.header_pattern.match(line)
        if not match:
            raise ValueError(
                "Failed to parse {!r} using {!r}".format(
                    line, self))
        storage = OrderedDict()
        prefix = None
        db_uid = None
        if line.startswith(">"):
            line = line[1:]
        prefix, line = line.split(":", 1)
        db_uid, line = line.split(" ", 1)
        storage['Prefix'] = prefix
        storage['Tag'] = db_uid
        kv_pattern = re.compile(r"\\(?P<key>\S+)=(?P<value>.+?)(?:\s(?=\\)|$)")
        for key, value in kv_pattern.findall(line):
            if not (value.startswith("(") or " (" in value):
                storage[key] = self._coerce_types(key, value)
            else:
                # multi-value
                storage[key] = [self._coerce_types(key, v) for v in self._extract_parenthesis_list(value)]
        return Header(storage, original=original)
