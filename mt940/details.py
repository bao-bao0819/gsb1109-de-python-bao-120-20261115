"""Ambiguity-aware decoding of structured ``:86:`` transaction details.

A structured ``:86:`` field marks subfields with ``?`` and two identifier
digits, for example ``?20`` followed by purpose text. Banks wrap long fields
over several physical lines and an MT940 detail line holds at most
:data:`WRAP_WIDTH` characters. A wrapped continuation line can therefore
begin with marker-shaped characters such as ``?30`` that are really text.
The legacy decoder in :mod:`mt940.processors` flattens the physical lines
first, so such a continuation is indistinguishable from a genuine marker.

:func:`analyze_details` keeps the physical lines and compares the readings
each marker-shaped position admits:

* a *marker* reading opens a subfield and is accepted only when the selected
  grammar allows the code in this position and context;
* a *text* reading keeps the characters as continuation of the current
  subfield, preserving the original line.

A marker at the start of a physical line following a full
:data:`WRAP_WIDTH` line may be a mechanical wrap, so both readings are kept
when each is grammar-consistent. A marker after a shorter line is a
deliberate line break and therefore a marker alone. Readings that violate
the grammar are discarded.

A single surviving reading is unique evidence that a caller may store
automatically. Competing readings are returned as structural candidates
together with the original lines, while missing required fields and unknown
legal extensions are reported separately. The analyzer never reorders
source lines to complete a statement.

The module has no top-level package imports, so
:mod:`mt940.processors` and :mod:`mt940.models` can import it without a
cycle. Access to the subfield mapping and the shared segment helpers happens
inside the functions that need them.
"""

from __future__ import annotations

import enum
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Maximum characters of one MT940 detail physical line. A line of exactly
#: this width is treated as mechanically wrapped.
WRAP_WIDTH: Final[int] = 65

#: Two identifier digits follow every ``?`` marker.
_MARKER_WIDTH: Final[int] = 2

#: Structured details open with three digits, a marker and two digit codes.
_STRUCTURED_RE: Final[re.Pattern[str]] = re.compile(r'^\d{3}\?\d{2}')

#: Marker-shaped token anywhere in the flattened text.
_MARKER_RE: Final[re.Pattern[str]] = re.compile(r'\?\d{2}')

#: Output fields that a complete structured ``:86:`` block is expected to
#: supply. Missing ones are reported, never silently invented.
REQUIRED_DETAIL_FIELDS: Final[tuple[str, ...]] = (
    'transaction_code',
    'posting_text',
    'purpose',
)


class MarkerStatus(enum.Enum):
    """Grammar classification of a marker in a surviving reading."""

    #: Code listed directly in the configured subfield mapping.
    KNOWN = 'known'

    #: Code that continues an earlier subfield in the current context.
    CONTINUATION = 'continuation'

    #: Well-formed marker whose code is a legal but unmapped extension.
    UNKNOWN = 'unknown'


class IssueKind(enum.Enum):
    """Category of a separately reported detail problem."""

    #: A configured required output field is absent from the reading.
    MISSING_REQUIRED = 'missing_required'

    #: A well-formed marker uses a code the selected grammar does not map.
    UNKNOWN_EXTENSION = 'unknown_extension'

    #: More than one grammar-consistent reading survives.
    AMBIGUOUS_STRUCTURE = 'ambiguous_structure'


@dataclass(frozen=True, slots=True)
class MarkerOccurrence:
    """A marker accepted while building one reading.

    Attributes:
        line_number: One-based physical line number inside the raw value.
        code: Two-digit marker code as written in the source.
        status: Grammar classification of ``code`` in its position.
    """

    line_number: int
    code: str
    status: MarkerStatus


@dataclass(frozen=True, slots=True)
class DetailIssue:
    """A reported problem that does not invalidate the whole reading.

    Attributes:
        kind: Problem category.
        message: Human-readable explanation of the problem.
        code: Marker code for a marker-related problem, otherwise ``None``.
        field_name: Output field name for a missing requirement, otherwise
            ``None``.
        line_number: Physical line carrying the marker, or ``None``.
    """

    kind: IssueKind
    message: str
    code: str | None = None
    field_name: str | None = None
    line_number: int | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        """Return a JSON-safe dictionary describing the issue.

        Returns:
            A plain dictionary with primitive values, suitable for storage
            on a transaction and for :mod:`mt940.json` serialisation.
        """
        return {
            'kind': self.kind.value,
            'message': self.message,
            'code': self.code,
            'field_name': self.field_name,
            'line_number': self.line_number,
        }


@dataclass(frozen=True, slots=True)
class DetailReading:
    """One grammar-consistent interpretation of the detail lines.

    Attributes:
        pairs: Ordered ``(code, text)`` subfield pairs, including repeats.
            The leading transaction code uses the empty string code.
        markers: Markers accepted in source order while building the reading.
    """

    pairs: tuple[tuple[str, str], ...]
    markers: tuple[MarkerOccurrence, ...]

    @property
    def segments(self) -> OrderedDict[str, str]:
        """Return the pairs as an ordered mapping, keeping repeated codes.

        Returns:
            An :class:`~collections.OrderedDict` of code to text. Repeated
            codes keep their first position and their last text, matching
            :func:`mt940.processors._parse_segments`.
        """
        segments: OrderedDict[str, str] = OrderedDict()
        for code, text in self.pairs:
            segments[code] = text
        return segments

    def decode(
        self,
        detail_keys: Mapping[str, str] | None = None,
        *,
        space: bool = False,
    ) -> dict[str, str | None]:
        """Decode the reading into named output fields.

        Args:
            detail_keys: Subfield mapping. ``None`` lazily uses
                :data:`mt940.processors.DETAIL_KEYS`.
            space: Insert one space between fragments of the same field.

        Returns:
            The joined output mapping with ``None`` for absent fields.
        """
        from . import processors

        mapping = (
            processors.DETAIL_KEYS if detail_keys is None else detail_keys
        )
        grouped: defaultdict[str, list[str]] = defaultdict(list)
        grouped.update(
            processors._process_segments(self.segments, dict(mapping))
        )
        return processors._join_result(grouped, space, dict(mapping))


@dataclass(frozen=True, slots=True)
class DetailAnalysis:
    """Result of analyzing one raw structured ``:86:`` value.

    Attributes:
        raw_lines: Original physical lines, never reordered or rewritten.
        structured: Whether the value opens as a structured detail block.
        readings: Every grammar-consistent reading. A unique reading is the
            only one eligible for automatic storage.
        issues: Missing requirements, unknown extensions and ambiguity.
    """

    raw_lines: tuple[str, ...]
    structured: bool
    readings: tuple[DetailReading, ...]
    issues: tuple[DetailIssue, ...] = field(default=())

    @property
    def unique(self) -> bool:
        """Whether exactly one distinct reading survives."""
        return len(self.readings) == 1

    @property
    def unique_reading(self) -> DetailReading | None:
        """Return the sole reading, or ``None`` when evidence is ambiguous."""
        return self.readings[0] if self.unique else None

    @property
    def candidates(self) -> tuple[Mapping[str, str], ...]:
        """Return the raw segment mapping of every surviving reading.

        Returns:
            One ordered mapping per reading so a caller can compare the
            structural alternatives without rerunning the analysis.
        """
        return tuple(reading.segments for reading in self.readings)


def _marker_status(
    code: str,
    seen_codes: frozenset[str],
    detail_keys: Mapping[str, str],
) -> MarkerStatus | None:
    """Classify a marker code in the context of segments already opened.

    Args:
        code: Two-digit marker code to classify.
        seen_codes: Marker codes accepted earlier in the same reading.
        detail_keys: Configured direct subfield mapping.

    Returns:
        The marker status, or ``None`` when the code cannot open a segment
        here because a continuation's base field was never opened.
    """
    if code in detail_keys:
        return MarkerStatus.KNOWN
    if code == '33':
        return (
            MarkerStatus.CONTINUATION if '32' in seen_codes else None
        )
    if code[0] == '2' and code != '20':
        return (
            MarkerStatus.CONTINUATION if '20' in seen_codes else None
        )
    if code in {'61', '62', '63', '64', '65'}:
        return (
            MarkerStatus.CONTINUATION if '60' in seen_codes else None
        )
    return MarkerStatus.UNKNOWN


@dataclass(slots=True)
class _State:
    """Partial reading grown while scanning marker-shaped positions."""

    #: Ordered ``(code, text)`` pairs built so far, starting with the code.
    pairs: list[tuple[str, str]]
    #: Markers accepted while building this partial reading.
    markers: list[MarkerOccurrence]
    #: Marker codes already opened, used to validate continuations.
    seen: frozenset[str]

    def append_text(self, text: str) -> None:
        """Append flattened text to the currently open subfield."""
        if not text:
            return
        code, existing = self.pairs[-1]
        self.pairs[-1] = (code, existing + text)

    def open_marker(
        self,
        code: str,
        status: MarkerStatus,
        line_number: int,
    ) -> None:
        """Open the subfield introduced by an accepted marker."""
        self.markers.append(MarkerOccurrence(line_number, code, status))
        self.pairs.append((code, ''))
        self.seen = self.seen | {code}

    def key(self) -> tuple[tuple[str, str], ...]:
        """Return the pair sequence used to merge equivalent states."""
        return tuple(self.pairs)


def _line_starts(raw_lines: Sequence[str]) -> tuple[int, ...]:
    """Return the flattened-text offset at which each physical line starts."""
    starts: list[int] = []
    offset = 0
    for line in raw_lines:
        starts.append(offset)
        offset += len(line)
    return tuple(starts)


def _line_index_of(
    offset: int,
    line_starts: Sequence[int],
    raw_lines: Sequence[str],
) -> int:
    """Return the zero-based physical line containing a flattened offset."""
    line_index = 0
    for index, line_start in enumerate(line_starts):
        if line_start <= offset < line_start + len(raw_lines[index]):
            line_index = index
        elif line_start > offset:
            break
    return line_index


def _expand_state(
    state: _State,
    code: str,
    token: str,
    line_number: int,
    *,
    allow_text: bool,
    detail_keys: Mapping[str, str],
) -> list[_State]:
    """Return the readings one partial state admits at a marker-shaped token.

    Args:
        state: Partial reading before the token.
        code: Two-digit marker code carried by the token.
        token: The matched marker text, including its ``?`` prefix.
        line_number: One-based physical line of the token.
        allow_text: Whether a text reading is permitted alongside a legal
            marker reading. A grammar-invalid marker always becomes text.
        detail_keys: Selected subfield mapping.

    Returns:
        The marker reading when the grammar accepts it, followed by the text
        reading when permitted or required.
    """
    status = _marker_status(code, state.seen, detail_keys)
    expanded: list[_State] = []

    if status is not None:
        marker_state = _State(
            pairs=[*state.pairs],
            markers=[*state.markers],
            seen=state.seen,
        )
        marker_state.open_marker(code, status, line_number)
        expanded.append(marker_state)

    if status is None or allow_text:
        # Illegal markers are content, and a wrapped line may carry
        # marker-shaped text instead of opening a subfield.
        text_state = _State(
            pairs=[*state.pairs],
            markers=[*state.markers],
            seen=state.seen,
        )
        text_state.append_text(token)
        expanded.append(text_state)

    return expanded


def _dedup_states(states: list[_State]) -> list[_State]:
    """Merge states that built the same ordered pair sequence."""
    deduped: dict[tuple[tuple[str, str], ...], _State] = {}
    for state in states:
        deduped.setdefault(state.key(), state)
    return list(deduped.values())


def _incomplete_delimiter_length(text: str) -> int:
    """Count a trailing ``?`` delimiter that has fewer than two code chars.

    Args:
        text: Flattened detail text.

    Returns:
        The number of trailing characters to ignore: zero for a complete
        ending, one for a trailing bare ``?``, or two when the last two
        characters both begin incomplete delimiters. This mirrors the
        truncation in :func:`mt940.processors._parse_segments`.
    """
    if len(text) >= _MARKER_WIDTH and text[-2] == '?':
        return _MARKER_WIDTH
    if text and text[-1] == '?':
        return 1
    return 0


def _missing_field_issues(
    reading: DetailReading,
    detail_keys: Mapping[str, str],
) -> list[DetailIssue]:
    """Report required output fields absent from a unique reading."""
    decoded = reading.decode(detail_keys)
    issues: list[DetailIssue] = []
    for field_name in REQUIRED_DETAIL_FIELDS:
        if not decoded.get(field_name):
            issues.append(
                DetailIssue(
                    kind=IssueKind.MISSING_REQUIRED,
                    message=(
                        'structured :86: is missing required field '
                        f'{field_name!r}'
                    ),
                    field_name=field_name,
                )
            )
    return issues


def _unknown_extension_issues(
    reading: DetailReading,
) -> list[DetailIssue]:
    """Report well-formed markers whose code has no configured mapping."""
    return [
        DetailIssue(
            kind=IssueKind.UNKNOWN_EXTENSION,
            message=(
                f'marker ?{marker.code} on line {marker.line_number} is an '
                'unmapped legal extension'
            ),
            code=marker.code,
            line_number=marker.line_number,
        )
        for marker in reading.markers
        if marker.status is MarkerStatus.UNKNOWN
    ]


def analyze_details(
    raw_details: str,
    detail_keys: Mapping[str, str] | None = None,
) -> DetailAnalysis:
    """Analyze raw structured details while preserving physical lines.

    Args:
        raw_details: Raw ``:86:`` value with its original line breaks.
        detail_keys: Selected subfield syntax. ``None`` lazily uses
            :data:`mt940.processors.DETAIL_KEYS`. Pass the applicant-IBAN
            variant to interpret ``?31`` as an account identifier.

    Returns:
        The analysis with original lines, every grammar-consistent reading
        and separate issues. Unstructured input returns no readings and no
        issues.
    """
    from . import processors

    mapping = (
        processors.DETAIL_KEYS if detail_keys is None else detail_keys
    )
    raw_lines = tuple(raw_details.splitlines())
    if not _STRUCTURED_RE.match(raw_details):
        return DetailAnalysis(
            raw_lines=raw_lines,
            structured=False,
            readings=(),
        )

    flattened = ''.join(raw_lines)
    scan_end = len(flattened) - _incomplete_delimiter_length(flattened)
    line_starts = _line_starts(raw_lines)
    states: list[_State] = [
        _State(pairs=[('', '')], markers=[], seen=frozenset())
    ]
    cursor = 0

    for match in _MARKER_RE.finditer(flattened, endpos=scan_end):
        start = match.start()
        code = flattened[start + 1 : start + 1 + _MARKER_WIDTH]
        line_index = _line_index_of(start, line_starts, raw_lines)
        line_number = line_index + 1
        allow_text = (
            line_index > 0
            and start == line_starts[line_index]
            and len(raw_lines[line_index - 1]) >= WRAP_WIDTH
        )

        next_states: list[_State] = []
        for state in states:
            state.append_text(flattened[cursor:start])
            next_states.extend(
                _expand_state(
                    state,
                    code,
                    match.group(0),
                    line_number,
                    allow_text=allow_text,
                    detail_keys=mapping,
                )
            )

        cursor = match.end()
        states = _dedup_states(next_states)

    for state in states:
        state.append_text(flattened[cursor:scan_end])

    readings = tuple(
        DetailReading(
            pairs=tuple(state.pairs),
            markers=tuple(state.markers),
        )
        for state in states
    )

    issues: list[DetailIssue] = []
    if len(readings) > 1:
        issues.append(
            DetailIssue(
                kind=IssueKind.AMBIGUOUS_STRUCTURE,
                message=(
                    'marker-shaped continuation lines admit more than one '
                    'grammar-consistent reading'
                ),
            )
        )
    else:
        issues.extend(_unknown_extension_issues(readings[0]))
        issues.extend(_missing_field_issues(readings[0], mapping))

    return DetailAnalysis(
        raw_lines=raw_lines,
        structured=True,
        readings=readings,
        issues=tuple(issues),
    )
