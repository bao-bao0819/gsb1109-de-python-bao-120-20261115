# Pydifact - a python edifact library
#
# Copyright (c) 2017-2024 Christian González
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
"""Cleaned export of coded message values, resolved against EDIFACT directories.

The same code can mean different things in different EDIFACT directories, so
every resolved value is bound to

- the **message version** (from the UNH message identifier),
- the **element path** (segment tag, element position, component position), and
- the **code list version** (the directory the code list was taken from).

Resolution never falls back to a newer (or any other) directory: if the
message's own directory is not available locally, values are exported
unresolved, keeping their original declaration. The same applies to codes that
are unknown to the matched directory's code list. Qualifiers (code list
identification 1131 / responsible agency 3055) are always taken from the same
composite occurrence as the code they qualify, never from a sibling composite.
"""

import xml.etree.ElementTree as ET  # nosec B405 - parses only bundled XML
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydifact.constants import Element

if TYPE_CHECKING:
    from pydifact.segmentcollection import Message
    from pydifact.segments import Segment

#: data element id of the code list identification qualifier
CODE_LIST_ID_ELEMENT = "1131"
#: data element id of the code list responsible agency qualifier
RESPONSIBLE_AGENCY_ELEMENT = "3055"


@lru_cache(maxsize=32)
def _load_directory_xml(directory: str, filename: str) -> ET.Element:
    """Load and cache an XML data file from the specified directory.

    Args:
        directory: The directory name under pydifact.syntax (e.g., 'd00a', 'd96a')
        filename: The XML file to load (e.g., 'segments.xml', 'codes.xml')

    Raises:
        ValueError: If `directory` points outside of pydifact.syntax
        FileNotFoundError: If the file cannot be found in the directory
        ET.ParseError: If the XML file cannot be parsed
    """
    syntax_root = Path(__file__).parent / "syntax"
    data_path = (syntax_root / directory / "data").resolve()
    if not data_path.is_relative_to(syntax_root.resolve()):
        raise ValueError(
            f"Invalid directory '{directory}': must be a name below pydifact.syntax."
        )
    xml_path = data_path / filename
    if not xml_path.exists():
        raise FileNotFoundError(f"{filename} not found in directory: {directory}")
    tree = ET.parse(xml_path)  # nosec B314 - bundled package XML
    return tree.getroot()


def directory_for_message(message: "Message") -> str | None:
    """Derive the EDIFACT directory name from a message's UNH identifier.

    E.g., the message identifier ``["APERAK", "D", "23A", "UN"]`` maps to
    directory ``"d23a"``. Returns `None` if the identifier does not carry a
    version and release.
    """
    identifier = message.identifier
    if len(identifier) < 3 or not identifier[1] or not identifier[2]:
        return None
    return f"{identifier[1]}{identifier[2]}".lower()


@dataclass(frozen=True)
class CodeResolution:
    """One coded value of a message, resolved against an EDIFACT directory.

    The original declared value is always kept in `value`, even when the code
    is unknown to the matched code list or no directory matched at all.
    """

    #: message version from the UNH identifier, e.g. "D.23A"
    message_version: str
    #: code list version (directory) actually used, e.g. "d23a";
    #: None if no matching directory is available locally
    directory: str | None
    segment_tag: str
    #: 0-based position of the element within the segment
    element_index: int
    #: 0-based position within the composite, None for simple elements
    component_index: int | None
    #: composite data element id, e.g. "C002"; None for simple elements
    composite_id: str | None
    #: data element id, e.g. "1001"; None if the position is not defined
    data_element_id: str | None
    #: the code as declared in the message
    value: str
    #: code list entry title; None if the code could not be resolved
    title: str | None = None
    #: code list entry description; None if the code could not be resolved
    description: str | None = None
    #: code list identification (1131) from the same composite occurrence
    code_list_id: str | None = None
    #: responsible agency (3055) from the same composite occurrence
    responsible_agency: str | None = None

    @property
    def resolved(self) -> bool:
        """Whether the code was found in the matched directory's code list."""
        return self.title is not None

    @property
    def path(self) -> str:
        """Element path, 1-based, e.g. ``"BGM:3"`` or ``"CPI:1.1"``."""
        path = f"{self.segment_tag}:{self.element_index + 1}"
        if self.component_index is not None:
            path += f".{self.component_index + 1}"
        return path


@dataclass(frozen=True)
class CleanedMessage:
    """The cleaned export of a message: its coded values with resolutions."""

    message_type: str
    #: message version from the UNH identifier, e.g. "D.23A"
    message_version: str
    #: code list version (directory) used for resolution; None if the
    #: message's directory is not available locally (no fallback happens)
    directory: str | None
    entries: tuple[CodeResolution, ...] = field(default_factory=tuple)

    @property
    def resolved(self) -> list[CodeResolution]:
        """The entries whose code was found in the matched code list."""
        return [entry for entry in self.entries if entry.resolved]

    @property
    def unresolved(self) -> list[CodeResolution]:
        """The entries keeping their original, unresolved declaration."""
        return [entry for entry in self.entries if not entry.resolved]


def _segment_def(segments_root: ET.Element | None, tag: str) -> ET.Element | None:
    if segments_root is None:
        return None
    return segments_root.find(f".//segment[@id='{tag}']")


def _child_at(element: ET.Element | None, index: int) -> ET.Element | None:
    if element is None:
        return None
    children = list(element)
    return children[index] if index < len(children) else None


def _composite_qualifiers(
    composite_def: ET.Element, components: list[str]
) -> dict[str, str | None]:
    """Read the 1131/3055 qualifiers from this composite occurrence only."""
    component_ids = [de.get("id") for de in composite_def.findall("data_element")]

    def value_of(element_id: str) -> str | None:
        try:
            position = component_ids.index(element_id)
        except ValueError:
            return None
        if position < len(components) and components[position]:
            return components[position]
        return None

    return {
        "code_list_id": value_of(CODE_LIST_ID_ELEMENT),
        "responsible_agency": value_of(RESPONSIBLE_AGENCY_ELEMENT),
    }


def _iter_segment_resolutions(
    segment: "Segment",
    segments_root: ET.Element | None,
    code_lists: dict[str, dict[str, ET.Element]],
    directory: str | None,
    message_version: str,
) -> Iterator[CodeResolution]:
    segment_def = _segment_def(segments_root, segment.tag)
    for element_index, element in enumerate(segment.elements):
        xml_element = _child_at(segment_def, element_index)
        if isinstance(element, list):
            yield from _iter_composite_resolutions(
                segment.tag,
                element_index,
                element,
                xml_element,
                code_lists,
                directory,
                message_version,
            )
        else:
            data_element_id = (
                xml_element.get("id")
                if xml_element is not None and xml_element.tag == "data_element"
                else None
            )
            resolution = _resolve_value(
                code_lists,
                message_version=message_version,
                directory=directory,
                segment_tag=segment.tag,
                element_index=element_index,
                component_index=None,
                composite_id=None,
                data_element_id=data_element_id,
                value=element,
            )
            if resolution is not None:
                yield resolution


def _iter_composite_resolutions(
    segment_tag: str,
    element_index: int,
    components: list[str],
    xml_element: ET.Element | None,
    code_lists: dict[str, dict[str, ET.Element]],
    directory: str | None,
    message_version: str,
) -> Iterator[CodeResolution]:
    composite_def = (
        xml_element
        if xml_element is not None and xml_element.tag == "composite_data_element"
        else None
    )
    composite_id = composite_def.get("id") if composite_def is not None else None
    component_ids = (
        [de.get("id") for de in composite_def.findall("data_element")]
        if composite_def is not None
        else []
    )
    qualifiers = (
        _composite_qualifiers(composite_def, components)
        if composite_def is not None
        else {}
    )
    for component_index, value in enumerate(components):
        data_element_id = (
            component_ids[component_index]
            if component_index < len(component_ids)
            else None
        )
        resolution = _resolve_value(
            code_lists,
            message_version=message_version,
            directory=directory,
            segment_tag=segment_tag,
            element_index=element_index,
            component_index=component_index,
            composite_id=composite_id,
            data_element_id=data_element_id,
            value=value,
            **qualifiers,
        )
        if resolution is not None:
            yield resolution


def _resolve_value(
    code_lists: dict[str, dict[str, ET.Element]],
    *,
    message_version: str,
    directory: str | None,
    segment_tag: str,
    element_index: int,
    component_index: int | None,
    composite_id: str | None,
    data_element_id: str | None,
    value: Element,
    code_list_id: str | None = None,
    responsible_agency: str | None = None,
) -> CodeResolution | None:
    """Build a resolution entry for a single value.

    Returns None for values that are neither coded nor in doubt: empty values,
    and elements the matched directory defines without a code list. Whenever
    the code list is unknown (no directory, undefined position) or the code is
    not in the list, the original declaration is kept in an unresolved entry.
    """
    if not isinstance(value, str) or not value:
        return None
    code_list = code_lists.get(data_element_id) if data_element_id else None
    if directory is not None and data_element_id is not None and code_list is None:
        # the matched directory defines this element without a code list
        return None
    entry = code_list.get(value) if code_list else None
    return CodeResolution(
        message_version=message_version,
        directory=directory,
        segment_tag=segment_tag,
        element_index=element_index,
        component_index=component_index,
        composite_id=composite_id,
        data_element_id=data_element_id,
        value=value,
        title=entry.get("title") if entry is not None else None,
        description=entry.get("desc") if entry is not None else None,
        code_list_id=code_list_id,
        responsible_agency=responsible_agency,
    )


def _message_version(message: "Message") -> str:
    identifier = message.identifier
    return f"{identifier[1]}.{identifier[2]}" if len(identifier) >= 3 else ""


def _resolve_directory(
    message: "Message", directory: str | None
) -> tuple[str | None, ET.Element | None, dict[str, dict[str, ET.Element]]]:
    """Load the segment and code list definitions for a directory.

    Returns the directory actually usable (None if it is not available
    locally), the segments.xml root and the code lists. Never falls back to
    another (e.g. the latest) directory.
    """
    if directory is None:
        directory = directory_for_message(message)
    if not directory:
        return None, None, {}
    try:
        segments_root = _load_directory_xml(directory, "segments.xml")
        codes_root = _load_directory_xml(directory, "codes.xml")
    except FileNotFoundError:
        # the message's directory is not available locally: do not
        # silently apply another (e.g. the latest) directory
        return None, None, {}
    code_lists: dict[str, dict[str, ET.Element]] = {}
    for data_element in codes_root.findall("data_element"):
        element_id = data_element.get("id")
        if element_id is None:
            continue
        code_lists[element_id] = {
            code_id: code
            for code in data_element.findall("code")
            if (code_id := code.get("id")) is not None
        }
    return directory, segments_root, code_lists


def iter_code_resolutions(
    message: "Message", directory: str | None = None
) -> Iterator[CodeResolution]:
    """Iterate the coded values of a message, resolved against its directory.

    Args:
        message: The message to export.
        directory: The EDIFACT directory to resolve against (e.g. "d23a").
            Defaults to the directory derived from the message's UNH
            identifier. If that directory is not available locally, no other
            (e.g. newer) directory is used; values are yielded unresolved.
    """
    message_version = _message_version(message)
    directory, segments_root, code_lists = _resolve_directory(message, directory)
    for segment in message.segments:
        yield from _iter_segment_resolutions(
            segment, segments_root, code_lists, directory, message_version
        )


def clean_message(message: "Message", directory: str | None = None) -> CleanedMessage:
    """Export a message's coded values, resolved against its EDIFACT directory.

    Each entry is bound to the message version, its element path and the code
    list version (directory) actually used. Unknown codes keep their original
    declaration; if the message's directory is not available locally, no other
    directory is applied and all values are exported unresolved.
    """
    identifier = message.identifier
    directory, segments_root, code_lists = _resolve_directory(message, directory)
    message_version = _message_version(message)
    entries = tuple(
        entry
        for segment in message.segments
        for entry in _iter_segment_resolutions(
            segment, segments_root, code_lists, directory, message_version
        )
    )
    return CleanedMessage(
        message_type=identifier[0] if identifier else "",
        message_version=message_version,
        directory=directory,
        entries=entries,
    )
