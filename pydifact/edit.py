"""Scoped, surgical edits of raw EDIFACT interchange text.

Sometimes only a single component of a single segment is authorized to
change - e.g. a supplier allows correcting the receipt note of one line
item, and nothing else. Re-serializing a parsed interchange is not
acceptable then, because it would rewrite every segment (normalizing
explicit empty components, trailing omissions, escaping, ...).

`replace_component()` therefore works directly on the raw text: it
locates the one segment identified by its content as it appears in the
original, splices the escaped new value into exactly the addressed
component, and leaves every other byte untouched. If the identity does
not match exactly one segment - because the source changed since the
edit was authorized, or because the identity is not unique - the edit
is refused by raising `EditConflictError`.
"""

from collections.abc import Iterator, Mapping

from pydifact.control.characters import Characters
from pydifact.exceptions import EditConflictError
from pydifact.serializer import Serializer

# An identity key addresses either a whole data element (its index) or a
# single component within it (element index, component index). Indexes
# are 0-based; element 0 is the first data element behind the tag.
IdentityKey = int | tuple[int, int]

# Absolute (start, end) offsets into the edited message.
_Span = tuple[int, int]


def replace_component(
    message: str,
    tag: str,
    identity: Mapping[IdentityKey, str],
    element: int,
    component: int | None = None,
    *,
    new_value: str,
    characters: Characters | None = None,
) -> str:
    """Replace one component of one uniquely identified segment, in place.

    Parameters:
        message: The raw EDIFACT text (with or without a UNA header).
        tag: The 3-letter tag of the segment to modify.
        identity: Mapping selecting the segment by its content as it
            appears in the original segment. Keys are element indexes
            (compared against the whole raw element text) or
            (element, component) tuples (compared against the raw
            component text). Values must be given exactly as they appear
            in the segment, i.e. with release characters in place.
        element: 0-based index of the data element to modify.
        component: 0-based component index within that element, or None
            to replace the whole element.
        new_value: The literal new content. Service characters in it are
            released (escaped) with the release character automatically.
        characters: The control characters to use. If omitted, they are
            read from the UNA header, falling back to the defaults.

    Returns:
        The full message with only the addressed component replaced.
        Every other segment, and the separator skeleton of the modified
        segment itself (explicit empty components vs. trailing
        omission), is preserved byte for byte.

    Raises:
        EditConflictError: If the identity matches anything but exactly
            one segment, or if the addressed component is not present in
            the identified segment (writing there would require turning
            trailing omission into explicit components, which is
            deliberately refused).
    """
    if not isinstance(new_value, str):
        raise TypeError(f"new_value must be a str, not {type(new_value)}")
    if element < 0 or (component is not None and component < 0):
        raise ValueError("element and component indexes must be >= 0")
    for key, expected in identity.items():
        if isinstance(key, tuple):
            if len(key) != 2 or not all(isinstance(i, int) for i in key):
                raise ValueError(f"Invalid identity key: {key!r}")
            if key[0] < 0 or key[1] < 0:
                raise ValueError("identity indexes must be >= 0")
        elif not isinstance(key, int) or key < 0:
            raise ValueError(f"Invalid identity key: {key!r}")
        if not isinstance(expected, str):
            raise TypeError(f"identity value for {key!r} must be a str")

    start = 0
    if message.startswith("UNA"):
        if characters is None:
            characters = Characters.from_str(message[:9])
        start = 9
    if characters is None:
        characters = Characters()

    matches: list[list[list[_Span]]] = []
    for seg_start, seg_end in _segment_spans(message, characters, start):
        seg_tag, elements = _split_segment(message, seg_start, seg_end, characters)
        if seg_tag == tag and _identity_matches(message, elements, identity):
            matches.append(elements)

    if len(matches) != 1:
        raise EditConflictError(
            f"Edit refused: identity {dict(identity)!r} matches "
            f"{len(matches)} '{tag}' segments, exactly 1 required. The "
            f"source may have changed since the edit was authorized, or "
            f"the identity is not unique."
        )

    elements = matches[0]
    if element >= len(elements):
        raise EditConflictError(
            f"Edit refused: '{tag}' segment has no element {element} "
            f"(only {len(elements)} present). Writing there would turn "
            f"trailing omission into explicit components."
        )
    components = elements[element]
    if component is None:
        span = (components[0][0], components[-1][1])
    elif component >= len(components):
        raise EditConflictError(
            f"Edit refused: element {element} of the '{tag}' segment has "
            f"no component {component} (only {len(components)} present). "
            f"Writing there would turn trailing omission into explicit "
            f"components."
        )
    else:
        span = components[component]

    escaped = Serializer(characters).escape(new_value)
    return message[: span[0]] + escaped + message[span[1] :]


def _segment_spans(
    message: str, characters: Characters, start: int
) -> Iterator[tuple[int, int]]:
    """Yield (start, end) spans of all terminated segments in the message.

    `end` is the offset just behind the segment terminator. Characters
    between segments (e.g. line breaks) do not belong to any span and
    are left untouched. Unterminated trailing content is not a segment.
    """
    pos = start
    while pos < len(message):
        if message[pos] in characters.line_terminators:
            pos += 1
            continue
        seg_start = pos
        while pos < len(message):
            char = message[pos]
            if char == characters.escape_character:
                pos += 2
                continue
            if char == characters.segment_terminator:
                break
            pos += 1
        if pos >= len(message):
            return
        yield seg_start, pos + 1
        pos += 1


def _split_segment(
    message: str, seg_start: int, seg_end: int, characters: Characters
) -> tuple[str, list[list[_Span]]]:
    """Split a raw segment into its tag and its element/component spans.

    Returns (tag, elements), where each element is a list of (start,
    end) spans of its components (absolute offsets into `message`,
    excluding the separating control characters). An explicitly empty
    component yields an empty span (start == end); an omitted trailing
    component yields no span at all, so the two are never interchanged.
    """
    content_end = seg_end - 1  # exclude the segment terminator
    escape = characters.escape_character
    data_separator = characters.data_separator
    component_separator = characters.component_separator

    pos = seg_start
    while pos < content_end and message[pos] != data_separator:
        pos += 1
    tag = message[seg_start:pos]

    elements: list[list[_Span]] = []
    if pos >= content_end:
        return tag, elements

    pos += 1
    component_start = pos
    components: list[_Span] = []
    while pos < content_end:
        char = message[pos]
        if char == escape:
            pos += 2
            continue
        if char == data_separator:
            components.append((component_start, pos))
            elements.append(components)
            components = []
            pos += 1
            component_start = pos
            continue
        if char == component_separator:
            components.append((component_start, pos))
            pos += 1
            component_start = pos
            continue
        pos += 1
    components.append((component_start, content_end))
    elements.append(components)
    return tag, elements


def _identity_matches(
    message: str,
    elements: list[list[_Span]],
    identity: Mapping[IdentityKey, str],
) -> bool:
    """Check whether all identity coordinates match the raw segment text."""
    for key, expected in identity.items():
        if isinstance(key, tuple):
            element_index, component_index = key
            if element_index >= len(elements):
                return False
            components = elements[element_index]
            if component_index >= len(components):
                return False
            start, end = components[component_index]
        else:
            if key >= len(elements):
                return False
            components = elements[key]
            start, end = components[0][0], components[-1][1]
        if message[start:end] != expected:
            return False
    return True
