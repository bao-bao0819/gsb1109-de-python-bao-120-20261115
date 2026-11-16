"""
Source locations for OFX elements.

The parser records where each markup element started (body offset, line,
column and structural path).  ``Aggregate.from_etree()`` copies that metadata
onto the corresponding model instance, so a statement response can always be
traced back to the exact block of OFX that produced it.
"""

from __future__ import annotations

__all__ = [
    "SourceLocation",
    "clear_source_locations",
    "copy_source_locations",
    "get_source_location",
    "index_paths",
    "set_source_location",
]


import weakref
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceLocation:
    """Location of an element in the original OFX document.

    ``offset`` is a zero-based character offset into the OFX message body
    (the header is not included).  ``line`` and ``column`` are one-based and
    refer to the same body.  ``path`` is a structural XPath-like path, e.g.
    ``OFX/BANKMSGSRSV1/STMTTRNRS[2]/STMTRS``.
    """

    document: str | None = None
    path: str | None = None
    line: int | None = None
    column: int | None = None
    offset: int | None = None

    def __str__(self) -> str:
        location = self.document or "<stream>"
        if self.path:
            location += f":{self.path}"
        if self.line is not None:
            location += f" (line {self.line}"
            if self.column is not None:
                location += f", column {self.column}"
            location += ")"
        elif self.offset is not None:
            location += f" (offset {self.offset})"
        return location

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "offset": self.offset,
        }


# ``xml.etree.ElementTree.Element`` cannot carry arbitrary attributes.  Keep a
# side table keyed by the element itself, so entries disappear with the tree.
# ``SourceLocation`` also mirrors onto the model instance after conversion, so
# this table only has to survive until ``OFXTree.convert()``.
_LOCATIONS: weakref.WeakKeyDictionary[Any, SourceLocation] = weakref.WeakKeyDictionary()


def set_source_location(element: object, location: SourceLocation) -> None:
    _LOCATIONS[element] = location


def get_source_location(element: object) -> SourceLocation | None:
    try:
        return _LOCATIONS.get(element)
    except TypeError:
        # Unhashable stand-in objects carry their own ``source_location``.
        return None


def clear_source_locations() -> None:
    _LOCATIONS.clear()


def copy_source_locations(
    original: ET.Element, copied: ET.Element, *, document: str | None = None
) -> None:
    """Copy location metadata onto a structurally equivalent element tree.

    Used when user code deep-copies ``ET.Element`` trees before conversion; the
    copies are not the objects the parser indexed, so locations would otherwise
    be lost.  Paths are recomputed for *copied*; only offsets/lines are carried.
    """
    positions: dict[Any, tuple[int | None, int | None, int | None]] = {}

    def collect(source: ET.Element, target: ET.Element) -> None:
        location = get_source_location(source)
        if location is not None:
            positions[target] = (location.offset, location.line, location.column)
        for source_child, target_child in zip(source, target):
            collect(source_child, target_child)

    collect(original, copied)
    index_paths(copied, document, positions)


def _children_with_paths(element: ET.Element, path: str) -> list[tuple[ET.Element, str]]:
    """Pair each child with its path, numbering only repeated tags."""
    total: dict[str, int] = {}
    for child in element:
        total[child.tag] = total.get(child.tag, 0) + 1

    seen: dict[str, int] = {}
    result = []
    for child in element:
        seen[child.tag] = seen.get(child.tag, 0) + 1
        child_path = f"{path}/{child.tag}"
        if total[child.tag] > 1:
            child_path += f"[{seen[child.tag]}]"
        result.append((child, child_path))
    return result


def index_paths(
    root: ET.Element,
    document: str | None = None,
    positions: Mapping[Any, tuple[int | None, int | None, int | None]] | None = None,
) -> None:
    """Attach a ``SourceLocation`` to *root* and every descendant.

    ``positions`` maps each element to its ``(offset, line, column)`` as
    recorded by the parser.  Paths include a 1-based index only where a tag repeats
    among its siblings, e.g. ``OFX/BANKMSGSRSV1/STMTTRNRS[2]/STMTRS``.
    """
    positions = positions or {}

    def visit(element: ET.Element, path: str) -> None:
        offset, line, column = positions.get(element, (None, None, None))
        set_source_location(
            element,
            SourceLocation(
                document=document,
                path=path,
                line=line,
                column=column,
                offset=offset,
            ),
        )
        for child, child_path in _children_with_paths(element, path):
            visit(child, child_path)

    visit(root, root.tag)
