"""
Migrate legacy OFXv1 (SGML) documents to OFXv2 (XML).

The migration deliberately does more than "parse and re-serialize":

* The original OFX header (version, ``DATA``, encoding, character set and
  compression) is recorded in the output ``<?OFX ...?>`` instruction as
  ``ORIG*`` attributes, so a migrated file still reports where it came from.
  The body itself is written as UTF-8, as required by OFXv2.
* An element that is present but empty (``<MEMO></MEMO>``) stays distinct
  from an element that is absent.  Empty elements are written with explicit
  start and end tags, and empty *required* elements are rejected instead of
  being turned into a default value.
* SGML permits omitting the end tag of data-bearing elements, but not of
  aggregates.  A missing aggregate end tag is therefore an illegal omission.
  ``convert()`` refuses it by default; ``repair=True`` re-attaches the
  mis-nested aggregate to the closest ancestor that accepts it and records
  the repair in the returned report.  Nothing is repaired silently.
* ``verify()`` re-reads the converted document and compares the business
  tree, field kinds, and ordered accounts / transactions / amounts so that
  repeated items correspond one-to-one.
"""

from __future__ import annotations

__all__ = [
    "MigrationError",
    "StructureError",
    "Repair",
    "MigrationReport",
    "MigrationResult",
    "VerificationResult",
    "convert",
    "convert_file",
    "verify",
    "business_tree",
    "count_business_items",
]


# stdlib imports
import copy
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast
from warnings import catch_warnings, simplefilter

# local imports
import ofxtools.models
from ofxtools import Types
from ofxtools.header import OFXHeaderType, OFXHeaderV1, OFXHeaderV2
from ofxtools.models.base import Aggregate
from ofxtools.Parser import OFXTree

logger = logging.getLogger(__name__)


class MigrationError(ValueError):
    """The document can't be migrated without losing information."""


class StructureError(MigrationError):
    """The SGML business tree is not structurally valid."""


@dataclass(frozen=True)
class Repair:
    """One structural repair applied while converting."""

    path: str
    tag: str
    from_parent: str
    to_parent: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "tag": self.tag,
            "from_parent": self.from_parent,
            "to_parent": self.to_parent,
            "reason": self.reason,
        }


@dataclass
class MigrationReport:
    """Provenance and structural facts about one migration."""

    source_version: int
    source_ofxheader: int
    source_data: str
    source_encoding: str
    source_charset: str
    source_compression: str
    target_version: int
    repairs: list[Repair] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "source_ofxheader": self.source_ofxheader,
            "source_data": self.source_data,
            "source_encoding": self.source_encoding,
            "source_charset": self.source_charset,
            "source_compression": self.source_compression,
            "target_version": self.target_version,
            "repairs": [repair.as_dict() for repair in self.repairs],
            "warnings": list(self.warnings),
            "counts": self.counts,
        }

    def __str__(self) -> str:
        lines = [
            f"OFXv{self.source_version} ({self.source_encoding}/"
            f"{self.source_charset}) -> OFXv{self.target_version} XML (UTF-8)"
        ]
        if self.repairs:
            lines.append(f"repairs: {len(self.repairs)}")
            lines.extend(f"  {repair.path}: {repair.reason}" for repair in self.repairs)
        if self.warnings:
            lines.append(f"warnings: {len(self.warnings)}")
            lines.extend(f"  {warning}" for warning in self.warnings)
        for name, items in self.counts.items():
            lines.append(f"{name}: {len(items)}")
        return "\n".join(lines)


@dataclass
class MigrationResult:
    """Converted XML bytes plus the report describing the conversion."""

    xml: bytes
    report: MigrationReport
    header: OFXHeaderV2
    tree: ET.Element

    @property
    def text(self) -> str:
        return self.xml.decode("utf_8")

    def verify(self, source: Any) -> VerificationResult:
        """Re-read the output and compare it to ``source``."""
        return verify(source, self.xml, repaired_source=self.tree)


# ---------------------------------------------------------------------------
# Model schema helpers
# ---------------------------------------------------------------------------
def _model_classes() -> dict[str, type[Aggregate]]:
    classes: dict[str, type[Aggregate]] = {}
    for name, value in vars(ofxtools.models).items():
        if (
            isinstance(value, type)
            and issubclass(value, Aggregate)
            and value is not Aggregate
        ):
            classes[name.upper()] = value
    return classes


def _container_spec(tag: str) -> dict[str, tuple[int, bool]] | None:
    """Map child tag -> (position, is_list_member) for a known aggregate.

    Returns ``None`` when ``tag`` is a data-bearing element or is unknown.
    """

    cls = _model_classes().get(tag)
    if cls is None:
        return None
    spec: dict[str, tuple[int, bool]] = {}
    for index, (attr, converter) in enumerate(cls.spec.items()):
        spec[attr.upper()] = (
            index,
            isinstance(converter, (Types.ListAggregate, Types.ListElement)),
        )
    return spec


# Model classes occasionally rename a tag because the OFX name is a Python
# keyword (``STOCKINFO``: ``YIELD`` -> ``yld``).  Structural checks use the
# model name; the serialized document keeps the OFX name.
_TAG_ALIASES = {"YIELD": "YLD"}


def _canonical_tag(tag: str) -> str:
    return _TAG_ALIASES.get(tag, tag)


def _child_spec(spec: dict[str, tuple[int, bool]], tag: str) -> tuple[int, bool] | None:
    return spec.get(_canonical_tag(tag))


# ---------------------------------------------------------------------------
# Structural validation / repair
# ---------------------------------------------------------------------------
def _path(parent_path: str, tag: str, occurrence: int) -> str:
    return f"{parent_path}/{tag}[{occurrence}]"


def _normalize_tag(elem: ET.Element) -> None:
    # ``TreeBuilder`` exposes XML self-closing syntax as ``TAG /``.
    if elem.tag.endswith(" /"):
        elem.tag = elem.tag[:-2]
    elif elem.tag.endswith("/"):
        elem.tag = elem.tag[:-1]


def _known_tags() -> set[str]:
    return set(_model_classes())


def _normalize_tree(
    root: ET.Element, *, repair: bool
) -> tuple[ET.Element, list[Repair], list[str]]:
    """Validate tree structure; optionally repair mis-nested aggregates.

    A missing aggregate end tag shows up as a known child appearing under the
    wrong parent.  In strict mode that is an error; in repair mode the child is
    moved to the closest ancestor that declares it.

    Repairs can expose further problems in the moved subtree, so validation
    repeats until the tree is stable.  Every node is therefore validated at its
    final position, and a repair can never smuggle an invalid subtree through.
    """
    tree = copy.deepcopy(root)
    repairs: list[Repair] = []
    warnings: list[str] = []

    for _ in range(100):
        _, pass_repairs = _validate_pass(tree, repair=repair)
        if not pass_repairs:
            return tree, repairs, warnings
        repairs.extend(pass_repairs)
    raise StructureError(
        "structural repair did not converge; the business tree is ambiguous"
    )


def _validate_pass(tree: ET.Element, *, repair: bool) -> tuple[bool, list[Repair]]:
    """One depth-first validation pass.  Returns (changed, repairs)."""
    repairs: list[Repair] = []
    known = _known_tags()

    def walk(elem: ET.Element, path: str) -> bool:
        _normalize_tag(elem)
        changed = False
        spec = _container_spec(elem.tag)
        if spec is None:
            if len(elem):
                # An element the model treats as a leaf has children: the
                # markup is not a valid business tree.
                raise StructureError(
                    f"{path}: <{elem.tag}> is a data element but contains child "
                    f"elements {[child.tag for child in elem]}"
                )
            return False

        # Hoist children that belong to an ancestor.  This handles an
        # aggregate whose end tag was omitted, e.g. LEDGERBAL swallowed by an
        # unclosed BANKTRANLIST.
        index = 0
        while index < len(elem):
            child = elem[index]
            _normalize_tag(child)
            if _child_spec(spec, child.tag) is not None or child.tag not in known:
                index += 1
                continue
            hoisted = False
            for ancestor in _ancestors(tree, elem):
                ancestor_spec = _container_spec(ancestor.tag)
                if (
                    ancestor_spec is not None
                    and _child_spec(ancestor_spec, child.tag) is not None
                ):
                    if not repair:
                        raise StructureError(
                            f"{path}: <{child.tag}> is not a valid child of "
                            f"<{elem.tag}>; it belongs to <{ancestor.tag}>. An "
                            f"aggregate end tag appears to be omitted. Pass "
                            f"repair=True to re-attach it."
                        )
                    elem.remove(child)
                    _insert_sorted(ancestor, child, ancestor_spec)
                    repairs.append(
                        Repair(
                            path=path,
                            tag=child.tag,
                            from_parent=elem.tag,
                            to_parent=ancestor.tag,
                            reason=(
                                f"aggregate end tag omitted: <{child.tag}> belongs "
                                f"under <{ancestor.tag}>"
                            ),
                        )
                    )
                    hoisted = changed = True
                    break
            if not hoisted:
                raise StructureError(
                    f"{path}: <{child.tag}> is not a valid child of <{elem.tag}>"
                )

        # Enforce declaration order for non-list members.  The OFX spec lets
        # different list members (e.g. BUYSTOCK / INVBANKTRAN) appear in any
        # order, and that order is business-significant, so they are never
        # sorted among themselves.
        if _order_violation(elem, spec):
            if not repair:
                raise StructureError(
                    f"{path}: children of <{elem.tag}> are out of order "
                    f"({[child.tag for child in elem]})"
                )
            _reorder(elem, spec)
            repairs.append(
                Repair(
                    path=path,
                    tag=elem.tag,
                    from_parent=elem.tag,
                    to_parent=elem.tag,
                    reason="children reordered to match the OFX schema",
                )
            )
            changed = True

        occurrences: dict[str, int] = {}
        for child in elem:
            occurrence = occurrences.get(child.tag, 0)
            occurrences[child.tag] = occurrence + 1
            if walk(child, _path(path, child.tag, occurrence)):
                changed = True
        return changed

    walk(tree, f"/{tree.tag}[0]")
    return bool(repairs), repairs


def _order_violation(elem: ET.Element, spec: dict[str, tuple[int, bool]]) -> bool:
    """True if non-list members appear out of schema order.

    List members are exempt: the spec allows them in any order and their
    relative order carries business meaning.
    """
    previous = -1
    for child in elem:
        child_spec = _child_spec(spec, child.tag)
        if child_spec is None:
            continue
        position, is_list = child_spec
        if is_list:
            continue
        if position < previous:
            return True
        previous = position
    return False


def _reorder(elem: ET.Element, spec: dict[str, tuple[int, bool]]) -> None:
    """Reorder non-list members; list members keep their relative order.

    Slots for list members are inferred from the first list child seen; all
    list children stay together at that slot, matching ``to_etree()``.
    """
    children = list(elem)
    slots = {
        child_spec[0]
        for child in children
        if (child_spec := _child_spec(spec, child.tag)) is not None and child_spec[1]
    }
    if len(slots) > 1:
        raise StructureError(
            f"<{elem.tag}> contains list members at several schema positions; "
            f"can't reorder without changing business order"
        )

    lists = [
        child
        for child in children
        if (child_spec := _child_spec(spec, child.tag)) is not None and child_spec[1]
    ]
    others = sorted(
        (child for child in children if child not in lists),
        key=lambda child: spec[_canonical_tag(child.tag)][0],
    )
    for child in children:
        elem.remove(child)
    if lists:
        position = slots.pop()
        before = [
            child for child in others if spec[_canonical_tag(child.tag)][0] < position
        ]
        after = [
            child for child in others if spec[_canonical_tag(child.tag)][0] > position
        ]
        elem.extend(before + lists + after)
    else:
        elem.extend(others)


def _ancestors(root: ET.Element, target: ET.Element) -> list[ET.Element]:
    chain: list[ET.Element] = []

    def find(elem: ET.Element) -> bool:
        if elem is target:
            return True
        for child in elem:
            if find(child):
                chain.append(elem)
                return True
        return False

    find(root)
    return chain


def _insert_sorted(
    parent: ET.Element, child: ET.Element, spec: dict[str, tuple[int, bool]]
) -> None:
    position = spec[_canonical_tag(child.tag)][0]
    for index, sibling in enumerate(parent):
        sibling_spec = _child_spec(spec, sibling.tag)
        if sibling_spec is not None and sibling_spec[0] > position:
            parent.insert(index, child)
            return
    parent.append(child)


# ---------------------------------------------------------------------------
# Business tree comparison
# ---------------------------------------------------------------------------
def _child_converter(cls: type[Aggregate] | None, tag: str) -> Types.Element | None:
    if cls is None:
        return None
    target = _canonical_tag(tag)
    for attr, converter in cls.spec.items():
        if attr.upper() == target:
            return cast(Types.Element, converter)
    return None


def _aggregate_class(converter: Types.Element | None) -> type[Aggregate] | None:
    if isinstance(converter, (Types.SubAggregate, Types.ListAggregate)):
        return converter.aggregate_type
    return None


def business_tree(root: ET.Element) -> list[dict[str, Any]]:
    """Flatten the business tree into ordered, comparable records.

    Each record identifies its path (with sibling occurrences), tag, whether
    the node is an aggregate or a leaf, the converter type from the model
    schema, and the exact text.  ``text`` is ``None`` both for aggregates and
    for empty leaves; ``kind`` keeps those two cases distinct.
    """
    records: list[dict[str, Any]] = []
    classes = _model_classes()

    def walk(
        elem: ET.Element,
        path: str,
        converter: Types.Element | None,
        parent: type[Aggregate] | None,
    ) -> None:
        kind = "aggregate" if len(elem) else "empty" if elem.text is None else "leaf"
        if converter is None:
            datatype = "aggregate" if kind == "aggregate" else "unknown"
            required = False
        elif kind == "aggregate":
            datatype = (
                converter.aggregate_type.__name__
                if isinstance(converter, (Types.SubAggregate, Types.ListAggregate))
                else "aggregate"
            )
            required = converter.required
        else:
            datatype = type(converter).__name__
            required = converter.required
        records.append(
            {
                "path": path,
                "tag": elem.tag,
                "kind": kind,
                "type": datatype,
                "required": required,
                "text": elem.text,
            }
        )
        cls = _aggregate_class(converter)
        if cls is None:
            cls = classes.get(_canonical_tag(elem.tag))
        occurrences: dict[str, int] = {}
        for child in elem:
            occurrence = occurrences.get(child.tag, 0)
            occurrences[child.tag] = occurrence + 1
            walk(
                child,
                _path(path, child.tag, occurrence),
                _child_converter(cls, child.tag),
                cls,
            )

    walk(root, f"/{root.tag}[0]", None, None)
    return records


ACCOUNT_TAGS = frozenset({"BANKACCTFROM", "CCACCTFROM", "INVACCTFROM"})
TRANSACTION_TAGS = frozenset({"STMTTRN"})


def count_business_items(root: ET.Element) -> dict[str, list[dict[str, Any]]]:
    """Ordered accounts, transactions and monetary amounts."""

    items: dict[str, list[dict[str, Any]]] = {
        "accounts": [],
        "transactions": [],
        "amounts": [],
    }
    classes = _model_classes()

    def walk(
        elem: ET.Element,
        path: str,
        converter: Types.Element | None,
        parent: type[Aggregate] | None,
    ) -> None:
        record = {
            "path": path,
            "tag": elem.tag,
            "type": (
                type(converter).__name__ if converter is not None else "aggregate"
            ),
            "text": elem.text,
        }
        if elem.tag in ACCOUNT_TAGS:
            items["accounts"].append(record)
        elif elem.tag in TRANSACTION_TAGS or (
            elem.tag in _transaction_wrappers() and elem.find("INVTRAN") is not None
        ):
            items["transactions"].append(record)
        elif (
            len(elem) == 0
            and isinstance(converter, Types.Decimal)
            and elem.text is not None
        ):
            # Amounts are recognised by their model field type, not by
            # guessing from the tag name.
            items["amounts"].append(record)
        cls = _aggregate_class(converter)
        if cls is None:
            cls = classes.get(_canonical_tag(elem.tag))
        occurrences: dict[str, int] = {}
        for child in elem:
            occurrence = occurrences.get(child.tag, 0)
            occurrences[child.tag] = occurrence + 1
            walk(
                child,
                _path(path, child.tag, occurrence),
                _child_converter(cls, child.tag),
                cls,
            )

    walk(root, f"/{root.tag}[0]", None, None)
    return items


def _transaction_wrappers() -> frozenset[str]:
    tags = set()
    for tag, cls in _model_classes().items():
        if "invtran" in cls.spec:
            tags.add(tag)
    return frozenset(tags)


@dataclass
class VerificationResult:
    """Outcome of re-reading a migrated document."""

    source_version: int
    target_version: int
    records_compared: int
    counts: dict[str, list[dict[str, Any]]]

    def __bool__(self) -> bool:
        return True


def verify(
    source: Any,
    converted: Any,
    *,
    repaired_source: ET.Element | None = None,
) -> VerificationResult:
    """Re-read ``converted`` and check it matches ``source`` exactly.

    ``source`` / ``converted`` may be paths, binary file objects, raw bytes,
    text, or already parsed roots.  When ``repaired_source`` is given (e.g.
    from :func:`convert`), it is used as the reference tree.
    """
    source_header: Any
    if isinstance(source, ET.Element):
        source_root = source
        source_header = None
    else:
        parsed_header, source_root = _parse(source)
        source_header = parsed_header
    reference_root = repaired_source if repaired_source is not None else source_root
    converted_header: Any
    if isinstance(converted, ET.Element):
        converted_root = converted
        converted_header = None
    else:
        parsed_converted_header, converted_root = _parse(converted)
        converted_header = parsed_converted_header

    reference = business_tree(reference_root)
    actual = business_tree(converted_root)
    if reference != actual:
        for index, (expected, found) in enumerate(zip(reference, actual)):
            if expected != found:
                raise MigrationError(
                    f"Business tree mismatch at record {index}: "
                    f"expected {expected!r}, got {found!r}"
                )
        raise MigrationError(
            f"Business tree length mismatch: expected {len(reference)} records, "
            f"got {len(actual)}"
        )

    expected_items = count_business_items(reference_root)
    actual_items = count_business_items(converted_root)
    for name in expected_items:
        if expected_items[name] != actual_items[name]:
            raise MigrationError(f"{name} do not correspond one-to-one")

    if source_header is not None and converted_header is not None:
        if isinstance(source_header, OFXHeaderV1):
            if not isinstance(converted_header, OFXHeaderV2):
                raise MigrationError("converted document is not OFXv2 XML")
            expected = {
                "origofxheader": str(source_header.ofxheader),
                "origdata": source_header.data,
                "origversion": str(source_header.version),
                "origencoding": source_header.encoding,
                "origcharset": source_header.charset,
                "origcompression": source_header.compression,
            }
            for attr, value in expected.items():
                if getattr(converted_header, attr, None) != value:
                    raise MigrationError(
                        f"original header field {attr.upper()} was not preserved "
                        f"(expected {value!r})"
                    )
            source_version = source_header.version
        else:
            source_version = source_header.version
    else:
        source_version = 0

    return VerificationResult(
        source_version=source_version,
        target_version=(
            converted_header.version if converted_header is not None else 0
        ),
        records_compared=len(reference),
        counts=actual_items,
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def _coerce_source(source: Any) -> Any:
    """Accept a path, raw bytes, XML/SGML text, or a binary file object."""
    if isinstance(source, bytes):
        return BytesIO(source)
    if isinstance(source, str):
        if "<" in source:
            return BytesIO(source.encode("utf_8"))
        return source
    return source


def _parse(source: Any) -> tuple[Any, ET.Element]:
    tree = OFXTree()
    tree.parse(_coerce_source(source))
    root = tree._root
    if not isinstance(root, ET.Element):  # pragma: no cover - parser contract
        raise MigrationError("parser did not return an ElementTree root")
    _normalize_tag(root)
    return tree.header, root


def _target_version(source_header: OFXHeaderType, version: int | None) -> int:
    if version is not None:
        return int(version)
    if isinstance(source_header, OFXHeaderV2):
        return int(source_header.version)
    # OFXv1 message versions below 200 have no OFXv2 header counterpart.
    # Keep them verbatim in ORIGVERSION and target OFX 2.0 by default.
    return source_header.version if source_header.version >= 200 else 200


def _build_header(source_header: OFXHeaderType, target_version: int) -> OFXHeaderV2:
    if isinstance(source_header, OFXHeaderV2):
        return OFXHeaderV2(
            target_version,
            security=source_header.security,
            oldfileuid=source_header.oldfileuid,
            newfileuid=source_header.newfileuid,
        )
    return OFXHeaderV2(
        target_version,
        security=source_header.security,
        oldfileuid=source_header.oldfileuid,
        newfileuid=source_header.newfileuid,
        origofxheader=str(source_header.ofxheader),
        origdata=source_header.data,
        origversion=str(source_header.version),
        origencoding=source_header.encoding,
        origcharset=source_header.charset,
        origcompression=source_header.compression,
    )


def _serialize(root: ET.Element, *, pretty: bool) -> str:
    if pretty:
        ET.indent(root)
    # short_empty_elements=False keeps ``<MEMO></MEMO>`` for a present but
    # empty element, so emptiness survives re-reading and human review.
    return ET.tostring(root, encoding="unicode", short_empty_elements=False)


def convert(
    source: str | Path | BinaryIO,
    *,
    version: int | None = None,
    repair: bool = False,
    pretty: bool = False,
    validate: bool = True,
) -> MigrationResult:
    """Convert an OFXv1 (SGML) document to OFXv2 (XML).

    ``repair`` explicitly authorizes re-attaching aggregates whose end tag was
    omitted; every repair is listed in ``result.report.repairs``.  Without it,
    such input raises :class:`StructureError` rather than being accepted as if
    it were legal.

    ``version`` overrides the output OFX version.  The source version is
    always preserved in ``ORIGVERSION``.
    """
    try:
        header, root = _parse(source)
    except MigrationError:
        raise
    except (ValueError, TypeError, SyntaxError) as err:
        # OFXHeaderError and ParseError subclass SyntaxError.
        raise MigrationError(str(err)) from err
    try:
        return _convert_parsed(
            header,
            root,
            version=version,
            repair=repair,
            pretty=pretty,
            validate=validate,
        )
    except MigrationError:
        raise
    except (ValueError, TypeError, SyntaxError) as err:
        # Header/type failures from the existing parsers keep their message
        # but are surfaced through this module's error type for callers.
        raise MigrationError(f"{_header_label(header)}: {err}") from err


def _header_label(header: Any) -> str:
    """Single-line header description for error messages."""
    return (
        f"OFXv{getattr(header, 'version', '?')} "
        f"({getattr(header, 'encoding', '')}/{getattr(header, 'charset', '')})"
    )


def _convert_parsed(
    header: Any,
    root: ET.Element,
    *,
    version: int | None,
    repair: bool,
    pretty: bool,
    validate: bool,
) -> MigrationResult:
    target_version = _target_version(header, version)
    normalized, repairs, warnings = _normalize_tree(root, repair=repair)

    if validate:
        # The model applies the spec's required/type constraints.  A missing
        # or empty required element raises here; it is never defaulted.
        # Model warnings (e.g. over-long account numbers that FIs really
        # send) are surfaced in the report instead of being discarded.
        with catch_warnings(record=True) as caught:
            simplefilter("always")
            try:
                Aggregate.from_etree(copy.deepcopy(normalized))
            except ValueError as err:
                raise MigrationError(f"{_header_label(header)}: {err}") from err
        warnings.extend(
            f"{warning.category.__name__}: {warning.message}" for warning in caught
        )

    out_header = _build_header(header, target_version)
    body = _serialize(normalized, pretty=pretty)
    xml = (str(out_header) + "\n" + body).encode("utf_8")

    counts = count_business_items(normalized)
    report = MigrationReport(
        source_version=header.version,
        source_ofxheader=header.ofxheader,
        source_data=getattr(header, "data", ""),
        source_encoding=getattr(header, "encoding", ""),
        source_charset=getattr(header, "charset", ""),
        source_compression=getattr(header, "compression", ""),
        target_version=target_version,
        repairs=repairs,
        warnings=warnings,
        counts=counts,
    )
    return MigrationResult(xml=xml, report=report, header=out_header, tree=normalized)


def convert_file(
    source: str | Path,
    destination: str | Path | None = None,
    **kwargs: Any,
) -> MigrationResult:
    """Convert ``source``; write XML next to it unless ``destination`` given."""

    source_path = Path(source)
    if destination is None:
        destination = source_path.with_suffix(".xml")
    result = convert(source_path, **kwargs)
    Path(destination).write_bytes(result.xml)
    logger.info(f"Wrote migrated OFX to {destination}")
    return result
