"""Restore the segment group structure of a message from a message directory.

The EDIFACT message directories bundled in ``pydifact.syntax`` (e.g. ``d24a``)
describe, for each message type, the ordered sequence of segments and segment
groups (``SG…``) a message consists of. This module matches the segments of a
message against such a structure definition and rebuilds the group hierarchy,
keeping track of the path and the occurrence order of every group (e.g. the
second ``LIN`` item group of an ORDERS message is ``SG29[2]``).

Matching is strictly sequential: segments are consumed in their original
order and are never reordered to make a message fit its structure. A segment
that cannot be placed raises `MessageStructureError` naming the offending
segment. Optional parts of the structure (e.g. the ``PRI`` price group
``SG33`` of an ORDERS item) are only filled by segments that actually occur
inside the current group occurrence - a missing optional segment never
"borrows" a segment from a following group occurrence.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # nosec B405 - parses only bundled XML
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydifact.constants import EDI_DEFAULT_DIRECTORY
from pydifact.exceptions import MessageStructureError
from pydifact.segments import Segment


@dataclass(frozen=True)
class SegmentDef:
    """A segment position within a message structure definition.

    Args:
        tag: The segment tag (e.g. ``LIN``).
        required: Whether the segment is mandatory at this position.
        maxrepeat: How often the segment may repeat at this position.
    """

    tag: str
    required: bool = False
    maxrepeat: int = 1


@dataclass(frozen=True)
class GroupDef:
    """A segment group (``SG…``) within a message structure definition.

    Args:
        group_id: The group identifier (e.g. ``SG29``).
        required: Whether at least one occurrence of the group is mandatory.
        maxrepeat: How often the group may repeat at this position.
        children: The ordered segment/group definitions the group consists of.
        first_tags: The segment tags that can start an occurrence of this
            group. Computed automatically from `children`.
    """

    group_id: str
    required: bool = False
    maxrepeat: int = 1
    children: tuple[StructureDef, ...] = ()
    first_tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "first_tags", _first_tags(self.children))


StructureDef = SegmentDef | GroupDef


def _first_tags(nodes: tuple[StructureDef, ...]) -> frozenset[str]:
    """Return the segment tags that can start the given node sequence."""
    tags: set[str] = set()
    for node in nodes:
        if isinstance(node, SegmentDef):
            tags.add(node.tag)
        else:
            tags |= node.first_tags
        if node.required:
            break
    return frozenset(tags)


_MESSAGE_TYPE_RE = re.compile(r"[A-Za-z0-9]+")

# tags of the message envelope, handled by `pydifact.segmentcollection.Message`
_ENVELOPE_TAGS = ("UNH", "UNT")


@lru_cache(maxsize=64)
def _load_message_xml(directory: str, message_type: str) -> ET.Element:
    """Load and cache a message structure XML from the given directory.

    Args:
        directory: The directory name under pydifact.syntax (e.g. ``d24a``).
        message_type: The message type (e.g. ``ORDERS``).

    Raises:
        ValueError: If `directory` or `message_type` are invalid.
        FileNotFoundError: If the message type is not defined in the directory.
    """
    syntax_root = Path(__file__).parent / "syntax"
    data_path = (syntax_root / directory / "data" / "messages").resolve()
    if not data_path.is_relative_to(syntax_root.resolve()):
        raise ValueError(
            f"Invalid directory '{directory}': must be a name below pydifact.syntax."
        )
    if not _MESSAGE_TYPE_RE.fullmatch(message_type):
        raise ValueError(f"Invalid message type '{message_type}'.")
    path = data_path / f"{message_type.lower()}.xml"
    if not path.exists():
        raise FileNotFoundError(
            f"No message structure definition for message type "
            f"'{message_type}' in directory '{directory}'."
        )
    return ET.parse(path).getroot()  # nosec B314 - bundled package XML


def _parse_nodes(element: ET.Element) -> tuple[StructureDef, ...]:
    """Parse the children of a message/group XML element into definitions."""
    nodes: list[StructureDef] = []
    for child in element:
        if child.tag == "segment":
            nodes.append(
                SegmentDef(
                    tag=child.get("id", ""),
                    required=child.get("required", "false").lower() == "true",
                    maxrepeat=int(child.get("maxrepeat", "1")),
                )
            )
        elif child.tag == "group":
            nodes.append(
                GroupDef(
                    group_id=child.get("id", ""),
                    required=child.get("required", "false").lower() == "true",
                    maxrepeat=int(child.get("maxrepeat", "1")),
                    children=_parse_nodes(child),
                )
            )
    return tuple(nodes)


@dataclass
class MatchedSegment:
    """A segment placed into the message structure.

    Attributes:
        segment: The matched segment.
        position: The 1-based position of the segment in the matched
            segment list (i.e. its original order in the message).
        occurrence: The 1-based repeat index of the segment at its position
            in the structure (e.g. the second ``QTY`` of an item group).
    """

    segment: Segment
    position: int
    occurrence: int

    def iter_segments(self) -> Iterator[MatchedSegment]:
        yield self


@dataclass
class MatchedGroup:
    """One occurrence of a segment group in a message.

    Attributes:
        group_id: The group identifier (e.g. ``SG29``).
        occurrence: The 1-based occurrence order of this group among the
            occurrences of the same group definition under the same parent.
        path: The group path including occurrence order, e.g.
            ``SG29[2]/SG33[1]``.
        children: The matched segments and groups inside this group
            occurrence, in their original order.
    """

    group_id: str
    occurrence: int
    path: str
    children: list[MatchedSegment | MatchedGroup] = field(default_factory=list)

    def iter_groups(self) -> Iterator[MatchedGroup]:
        """Yield this group and all contained groups, in order of appearance."""
        yield self
        for child in self.children:
            if isinstance(child, MatchedGroup):
                yield from child.iter_groups()

    def iter_segments(self) -> Iterator[MatchedSegment]:
        """Yield all matched segments of this group, in order of appearance."""
        for child in self.children:
            yield from child.iter_segments()


MatchedNode = MatchedSegment | MatchedGroup


class MatchResult:
    """The result of matching segments against a `MessageStructure`.

    Attributes:
        structure: The message structure the segments were matched against.
        children: The matched segments and top-level group occurrences, in
            their original order.
    """

    def __init__(self, structure: MessageStructure, children: list[MatchedNode]):
        self.structure = structure
        self.children = children

    def iter_groups(self) -> Iterator[MatchedGroup]:
        """Yield all group occurrences, in order of appearance."""
        for child in self.children:
            if isinstance(child, MatchedGroup):
                yield from child.iter_groups()

    def iter_segments(self) -> Iterator[MatchedSegment]:
        """Yield all matched segments, in order of appearance."""
        for child in self.children:
            yield from child.iter_segments()

    def get_groups(self, group_id: str) -> Iterator[MatchedGroup]:
        """Yield all occurrences of the given group, in order of appearance.

        Args:
            group_id: The group identifier to look for (e.g. ``SG29``).
        """
        for group in self.iter_groups():
            if group.group_id == group_id:
                yield group

    def __str__(self) -> str:
        lines: list[str] = []

        def render(children: list[MatchedNode], indent: int) -> None:
            for child in children:
                if isinstance(child, MatchedGroup):
                    lines.append(f"{'  ' * indent}{child.path}")
                    render(child.children, indent + 1)
                else:
                    lines.append(
                        f"{'  ' * indent}{child.segment.tag} "
                        f"(segment #{child.position})"
                    )

        render(self.children, 0)
        return "\n".join(lines)


def _raise_missing(
    structure: MessageStructure,
    node: StructureDef,
    path: str,
    segments: list[Segment],
    pos: int,
) -> None:
    """Raise an error for a required structure node that has no segment."""
    kind, name = (
        ("segment", node.tag)
        if isinstance(node, SegmentDef)
        else ("group", node.group_id)
    )
    where = f"Group {path}" if path else f"The {structure.message_type} message"
    if pos < len(segments):
        found = f"segment #{pos + 1} ({segments[pos]})"
    else:
        found = "the end of the message"
    raise MessageStructureError(
        f"{where} requires {kind} '{name}', but found {found}. "
        "Segments are not reordered to fit the structure."
    )


def _match_nodes(
    structure: MessageStructure,
    nodes: tuple[StructureDef, ...],
    segments: list[Segment],
    pos: int,
    path: str,
) -> tuple[list[MatchedNode], int]:
    """Match segments against a sequence of structure nodes, in order.

    Args:
        structure: The message structure being matched (for error messages).
        nodes: The structure nodes to match, in definition order.
        segments: The full segment list of the message.
        pos: The index of the next unmatched segment.
        path: The group path of the enclosing group occurrence ("" at root).

    Returns:
        The matched nodes and the index of the next unmatched segment.
    """
    children: list[MatchedNode] = []
    for node in nodes:
        occurrence = 0
        if isinstance(node, SegmentDef):
            while (
                pos < len(segments)
                and occurrence < node.maxrepeat
                and segments[pos].tag == node.tag
            ):
                occurrence += 1
                children.append(MatchedSegment(segments[pos], pos + 1, occurrence))
                pos += 1
        else:
            while (
                pos < len(segments)
                and occurrence < node.maxrepeat
                and segments[pos].tag in node.first_tags
            ):
                occurrence += 1
                group_path = (
                    f"{path}/{node.group_id}[{occurrence}]"
                    if path
                    else f"{node.group_id}[{occurrence}]"
                )
                sub_children, new_pos = _match_nodes(
                    structure, node.children, segments, pos, group_path
                )
                if new_pos == pos:
                    # a group occurrence must consume at least its trigger
                    # segment; guard against malformed structure definitions
                    raise MessageStructureError(
                        f"Group {group_path} of the {structure.message_type} "
                        "message structure did not consume any segment."
                    )
                pos = new_pos
                children.append(
                    MatchedGroup(node.group_id, occurrence, group_path, sub_children)
                )
        if occurrence == 0 and node.required:
            _raise_missing(structure, node, path, segments, pos)
    return children, pos


class MessageStructure:
    """The structure definition of a message type of an EDIFACT directory.

    The UNH/UNT envelope segments of the definition are stripped: match the
    content segments of a message (e.g. `Message.segments`) against it.
    """

    def __init__(
        self, message_type: str, directory: str, nodes: tuple[StructureDef, ...]
    ):
        self.message_type = message_type
        self.directory = directory
        self.nodes = nodes

    @classmethod
    def from_directory(
        cls, message_type: str, directory: str = EDI_DEFAULT_DIRECTORY
    ) -> MessageStructure:
        """Load the structure of a message type from a bundled directory.

        Args:
            message_type: The message type (e.g. ``ORDERS``).
            directory: The directory name under pydifact.syntax (e.g.
                ``d24a``); defaults to `pydifact.constants.EDI_DEFAULT_DIRECTORY`.

        Raises:
            ValueError: If `directory` or `message_type` are invalid.
            FileNotFoundError: If the message type is not defined in the
                directory.
        """
        root = _load_message_xml(directory, message_type)
        nodes = tuple(
            node
            for node in _parse_nodes(root)
            if not (isinstance(node, SegmentDef) and node.tag in _ENVELOPE_TAGS)
        )
        return cls(message_type.upper(), directory, nodes)

    def match(self, segments: Iterable[Segment]) -> MatchResult:
        """Match segments against this structure, in their original order.

        Args:
            segments: The content segments of a message (without UNH/UNT),
                in their original order.

        Returns:
            A `MatchResult` holding the restored group hierarchy.

        Raises:
            MessageStructureError: If a segment does not fit the structure at
                its position (segments are never reordered to fit), or if a
                required segment or group is missing.
        """
        segments = list(segments)
        children, pos = _match_nodes(self, self.nodes, segments, 0, "")
        if pos < len(segments):
            raise MessageStructureError(
                f"Segment #{pos + 1} ({segments[pos]}) does not fit the "
                f"{self.message_type} message structure at this position. "
                "Segments are not reordered to fit the structure."
            )
        return MatchResult(self, children)
