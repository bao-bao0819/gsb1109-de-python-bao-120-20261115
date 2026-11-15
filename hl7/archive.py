"""Split multi-order HL7 messages into independent order archives.

An HL7 result message (for example an ``ORU^R01``) often carries several
orders in one message::

    MSH
    PID
    PV1
    ORC / OBR / OBX ...   order 1
    ORC / OBR / OBX ...   order 2

Archiving such a message means creating one self-contained message per order.
The caller describes how orders are laid out in its messages with
:class:`SegmentGroupRule` instances (or the equivalent plain dictionaries)::

    import hl7
    from hl7.archive import SegmentGroupRule, split_orders

    result = split_orders(
        message,
        [SegmentGroupRule("ORC", ["ORC", "OBR", "OBX", "NTE"])],
    )

Each child archive receives:

* a fresh ``MSH-10`` message control ID,
* the patient, visit and common prefix segments it needs,
* its own order segments,
* metadata pointing back to the parent message and the source segment range.

Common segments are copied into every child so each archive is self-contained,
but they are tagged ``archive_role == "common"``.  They are therefore not
counted as separate clinical events; only order segments are "real events"
(``archive_role == "order"``).

Unknown ``Z`` segments are handled conservatively.  Unless a rule explicitly
claims a ``Z`` segment (through ``z_segments``, ``include_z`` or ``members``) or
the caller declares it common, the segment is kept in
:attr:`ArchiveResult.unassigned` together with its source position.  It is never
attached to the nearest order merely because it happened to appear before the
next group started.
"""

from __future__ import annotations

import copy
import fnmatch
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .containers import Batch, File, Message
from .util import generate_message_control_id

#: Role tags attached to every segment copied into an archive.
ROLE_CONTROL = "control"
ROLE_COMMON = "common"
ROLE_ORDER = "order"
ROLE_UNASSIGNED = "unassigned"

DEFAULT_PATIENT_SEGMENTS = ("PID", "PD1", "NK1")
DEFAULT_VISIT_SEGMENTS = ("PV1", "PV2")

_START_KEYS = (
    "start",
    "starts",
    "start_segments",
    "trigger",
    "triggers",
    "trigger_segments",
    "group_start",
    "order",
    "orders",
    "order_segments",
)
_MEMBER_KEYS = (
    "members",
    "member_segments",
    "segments",
    "contains",
    "details",
    "children",
    "include",
)
_END_KEYS = ("end", "ends", "stop", "stops", "stop_at", "terminators")
_Z_KEYS = ("z_segments", "zs", "z")
_NAME_KEYS = ("name", "id", "label")


def _as_tuple(value: Optional[Iterable[str] | str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _segment_id(segment: Sequence[Any]) -> str:
    try:
        return str(segment[0][0])
    except (IndexError, KeyError, TypeError):
        return ""


def _matches(segment_id: str, patterns: Iterable[str]) -> bool:
    upper = segment_id.upper()
    return any(fnmatch.fnmatchcase(upper, pattern.upper()) for pattern in patterns)


def _is_z_segment(segment_id: str) -> bool:
    return segment_id.upper().startswith("Z")


def _field_value(segment: Sequence[Any], field_num: int) -> str:
    try:
        return str(segment.extract_field(1, field_num))
    except (IndexError, KeyError):
        return ""


class SegmentGroupRule:
    """Description of one order segment group.

    Parameters
    ----------
    start:
        Segment ID(s) that begin a new order (for example ``"ORC"`` or
        ``"OBR"``).  ``fnmatch`` patterns such as ``"ORC"``/``"OBR"`` are
        accepted.
    members:
        Segment IDs belonging to the group.  ``None`` means "every segment
        until the next order starts", except that ``Z`` segments remain
        unassigned unless explicitly claimed.
    end:
        Optional segment ID(s) that terminate the group early.
    z_segments:
        Site-defined ``Z`` segment IDs (or patterns) that this rule explicitly
        claims.  A ``Z`` segment not named here is never attached implicitly.
    include_z:
        Set to true to explicitly opt into all ``Z`` segments for this group.
    name:
        Optional human-readable group name.
    """

    def __init__(
        self,
        start: Optional[Iterable[str] | str] = None,
        members: Optional[Iterable[str] | str] = None,
        *,
        trigger: Optional[Iterable[str] | str] = None,
        triggers: Optional[Iterable[str] | str] = None,
        segments: Optional[Iterable[str] | str] = None,
        member_segments: Optional[Iterable[str] | str] = None,
        end: Optional[Iterable[str] | str] = None,
        z_segments: Optional[Iterable[str] | str] = None,
        include_z: bool = False,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise TypeError("Unknown segment group rule option(s): {}".format(unknown))

        starts = _as_tuple(start) + _as_tuple(trigger) + _as_tuple(triggers)
        if not starts:
            raise ValueError("A segment group rule needs a start/trigger segment")

        if members is None:
            members = segments if segments is not None else member_segments

        self.start = starts
        self.start_ids = starts
        self.triggers = starts
        self.members = None if members is None else _as_tuple(members)
        self.member_ids = self.members
        self.end = _as_tuple(end)
        self.z_segments = _as_tuple(z_segments)
        self.include_z = bool(include_z)
        self.name = name or "/".join(starts)

    def starts_with(self, segment_id: str) -> bool:
        return _matches(segment_id, self.start)

    def claims_z(self, segment_id: str) -> bool:
        if not _is_z_segment(segment_id):
            return False
        if self.include_z or _matches(segment_id, self.z_segments):
            return True
        return self.members is not None and _matches(segment_id, self.members)

    def accepts(self, segment_id: str) -> bool:
        """Return whether *segment_id* belongs to this order group.

        ``Z`` segments are only accepted when this rule claims them
        explicitly.  This is what prevents an unattributed site-defined segment
        from being attached to whichever order happens to precede it.
        """
        if _is_z_segment(segment_id) and not self.claims_z(segment_id):
            return False
        if self.members is None:
            return True
        return _matches(segment_id, self.members)

    def explicitly_accepts(self, segment_id: str) -> bool:
        """Whether *segment_id* was named in ``members`` (not implied)."""
        if self.members is None:
            return False
        if _is_z_segment(segment_id) and not self.claims_z(segment_id):
            return False
        return _matches(segment_id, self.members)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> SegmentGroupRule:
        data = dict(mapping)

        start = None
        for key in _START_KEYS:
            if key in data:
                start = data.pop(key)
                break
        if start is None:
            raise ValueError("Segment group rule mapping needs a start/trigger")

        members = None
        for key in _MEMBER_KEYS:
            if key in data:
                members = data.pop(key)
                break

        end = None
        for key in _END_KEYS:
            if key in data:
                end = data.pop(key)
                break

        z_segments = None
        for key in _Z_KEYS:
            if key in data:
                z_segments = data.pop(key)
                break

        name = None
        for key in _NAME_KEYS:
            if key in data:
                name = data.pop(key)
                break

        include_z = bool(data.pop("include_z", False))
        if data:
            unknown = ", ".join(sorted(data))
            raise ValueError("Unknown segment group rule key(s): {}".format(unknown))
        return cls(
            start,
            members,
            end=end,
            z_segments=z_segments,
            include_z=include_z,
            name=name,
        )

    def __repr__(self) -> str:
        return (
            "SegmentGroupRule(start={!r}, members={!r}, end={!r}, "
            "z_segments={!r}, include_z={!r}, name={!r})"
        ).format(
            self.start,
            self.members,
            self.end,
            self.z_segments,
            self.include_z,
            self.name,
        )


def _looks_like_rule_mapping(mapping: Mapping[str, Any]) -> bool:
    return any(key in mapping for key in _START_KEYS)


def normalize_rules(rules: Any) -> list[SegmentGroupRule]:
    """Return *rules* as a list of :class:`SegmentGroupRule` instances.

    Accepted forms include:

    * a :class:`SegmentGroupRule`,
    * a segment ID (all non-``Z`` segments until the next trigger belong to it),
    * a mapping from trigger to members, e.g.
      ``{"ORC": ["ORC", "OBR", "OBX"]}``,
    * a rule mapping, e.g. ``{"start": "ORC", "members": [...]}``,
    * a ``(start, members)`` pair, e.g. ``("ORC", ["ORC", "OBR"])``,
    * a list/tuple of any of the above.  A list of plain strings is a list of
      independent triggers.
    """
    if rules is None:
        raise ValueError("Segment group rules are required")
    if isinstance(rules, SegmentGroupRule):
        return [rules]
    if isinstance(rules, str):
        return [SegmentGroupRule(rules)]
    if isinstance(rules, Mapping):
        if _looks_like_rule_mapping(rules):
            return [SegmentGroupRule.from_mapping(rules)]
        normalized = []
        for trigger, specification in rules.items():
            if isinstance(specification, Mapping):
                data = dict(specification)
                if not _looks_like_rule_mapping(data):
                    data.setdefault("start", trigger)
                data.setdefault("name", str(trigger))
                normalized.append(SegmentGroupRule.from_mapping(data))
            elif specification is None:
                normalized.append(SegmentGroupRule(trigger))
            elif isinstance(specification, str):
                normalized.append(SegmentGroupRule(trigger, [specification]))
            else:
                normalized.append(SegmentGroupRule(trigger, specification))
        return normalized
    if isinstance(rules, (list, tuple)):
        if (
            len(rules) == 2
            and isinstance(rules[0], str)
            and (rules[1] is None or isinstance(rules[1], (list, tuple, set)))
        ):
            return [SegmentGroupRule(rules[0], rules[1])]
        normalized = []
        for item in rules:
            normalized.extend(normalize_rules(item))
        return normalized
    raise TypeError("Unsupported segment group rules: {!r}".format(rules))


@dataclass(frozen=True)
class ParentLocation:
    """Location of an archived order in its parent message.

    ``position`` is the 0-based index of the first order segment in the parent;
    ``segment_number`` is the equivalent 1-based HL7 position.  ``end_position``
    is exclusive.  ``message_position`` identifies the message inside a
    :class:`~hl7.containers.Batch` or :class:`~hl7.containers.File`.
    """

    message_control_id: str
    position: Optional[int] = None
    end_position: Optional[int] = None
    segment_indexes: tuple[int, ...] = field(default_factory=tuple)
    message_position: Optional[int] = None

    @property
    def control_id(self) -> str:
        return self.message_control_id

    @property
    def segment_number(self) -> Optional[int]:
        return None if self.position is None else self.position + 1

    @property
    def segment_range(self) -> tuple[Optional[int], Optional[int]]:
        return (self.position, self.end_position)

    @property
    def message_index(self) -> Optional[int]:
        return self.message_position


class ArchiveResult(list):
    """List of archived order :class:`~hl7.containers.Message` instances.

    The object behaves like a list of child messages while also exposing the
    parent message, the unique common segments and any segments which could
    not be assigned safely.
    """

    def __init__(
        self,
        children: Iterable[Message] = (),
        *,
        parent_message: Optional[Message | Batch | File] = None,
        parent_message_control_id: str = "",
        parent_message_control_ids: Iterable[str] = (),
        parent_position: Optional[int] = None,
        unassigned: Iterable[Any] = (),
        common_segments: Iterable[Any] = (),
        rules: Iterable[SegmentGroupRule] = (),
    ) -> None:
        super().__init__(children)
        self.parent_message = parent_message
        self.parent_message_control_id = parent_message_control_id
        self.parent_control_id = parent_message_control_id
        self.parent_message_control_ids = list(parent_message_control_ids)
        self.parent_position = parent_position
        self.unassigned = list(unassigned)
        self.common_segments = list(common_segments)
        self.rules = list(rules)

    @property
    def children(self) -> list[Message]:
        return list(self)

    @property
    def orders(self) -> list[Message]:
        return list(self)

    @property
    def messages(self) -> list[Message]:
        return list(self)

    @property
    def archives(self) -> list[Message]:
        return list(self)

    @property
    def pending(self) -> list[Any]:
        return self.unassigned

    @property
    def unassigned_segments(self) -> list[Any]:
        return self.unassigned

    @property
    def has_unassigned(self) -> bool:
        return bool(self.unassigned)

    @property
    def event_segments(self) -> list[Any]:
        """Order segments across all children (common segments excluded)."""
        return [
            segment
            for child in self
            for segment in getattr(child, "event_segments", [])
        ]

    @property
    def event_count(self) -> int:
        """Number of real order events.

        Common/patient/visit segments are carried into every child but are not
        counted here, so copying a common prefix does not multiply events.
        """
        return sum(getattr(child, "event_segment_count", 0) for child in self)

    @property
    def common_occurrences(self) -> int:
        """How many times common segments were physically copied into children."""
        return sum(getattr(child, "common_segment_count", 0) for child in self)

    def by_order(self) -> dict[str, Message]:
        """Map each order key to its archived message."""
        return {
            getattr(child, "order_key", str(index)): child
            for index, child in enumerate(self, 1)
        }

    def __repr__(self) -> str:
        return "ArchiveResult(orders={}, unassigned={})".format(
            len(self), len(self.unassigned)
        )


def _tag_segment(
    segment: Any,
    role: str,
    source_index: int,
    parent_message_control_id: str,
    reason: Optional[str] = None,
) -> Any:
    segment.archive_role = role
    segment.source_index = source_index
    segment.source_segment_index = source_index
    segment.source_position = source_index
    segment.source_message_control_id = parent_message_control_id
    segment.parent_message_control_id = parent_message_control_id
    segment.is_common = role == ROLE_COMMON
    segment.is_order_event = role == ROLE_ORDER
    segment.is_unassigned = role == ROLE_UNASSIGNED
    segment.unassigned_reason = reason
    segment.reason = reason
    return segment


def _new_control_id(
    factory: Optional[Callable[..., str]],
    used: set[str],
    parent_message_control_id: str,
    order_index: int,
) -> str:
    if factory is None:
        for _ in range(100):
            candidate = generate_message_control_id()
            if candidate not in used and candidate != parent_message_control_id:
                return candidate
        return generate_message_control_id()

    try:
        parameters = inspect.signature(factory).parameters.values()
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        has_varargs = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        )
    except (TypeError, ValueError):
        positional = []
        has_varargs = False

    if has_varargs or len(positional) >= 2:
        return str(factory(parent_message_control_id, order_index))
    if len(positional) == 1:
        return str(factory(order_index))
    return str(factory())


def _order_key(segment: Any, fallback: int) -> str:
    value = _field_value(segment, 2)
    if not value:
        value = _field_value(segment, 3)
    return value or str(fallback)


def _message_messages(
    message: Message | Batch | File,
) -> list[tuple[Message, Optional[int]]]:
    """Return ``(message, message_position)`` pairs for supported inputs."""
    if isinstance(message, Message):
        return [(message, 0)]
    if isinstance(message, Batch):
        return [(child, index) for index, child in enumerate(message)]
    if isinstance(message, File):
        pairs: list[tuple[Message, Optional[int]]] = []
        position = 0
        for batch in message:
            for child in batch:
                pairs.append((child, position))
                position += 1
        return pairs
    raise TypeError(
        "Expected an hl7.Message, Batch or File, got {!r}".format(type(message))
    )


def _split_message(
    message: Message,
    rules: Sequence[SegmentGroupRule],
    *,
    patient_patterns: Sequence[str],
    visit_patterns: Sequence[str],
    common_patterns: Sequence[str],
    include_prefix: bool,
    control_id_factory: Optional[Callable[..., str]],
    message_position: Optional[int],
    used_control_ids: set[str],
    order_index_offset: int,
) -> ArchiveResult:
    segments = list(message)
    if not segments or _segment_id(segments[0]) != "MSH":
        raise ValueError("An HL7 message must start with an MSH segment")

    parent_control_id = _field_value(segments[0], 10)
    used_control_ids.add(parent_control_id)

    starts: list[tuple[int, SegmentGroupRule]] = []
    for index, segment in enumerate(segments[1:], 1):
        segment_id = _segment_id(segment)
        for rule in rules:
            if rule.starts_with(segment_id):
                starts.append((index, rule))
                break

    if not starts:
        unassigned = []
        for index, segment in enumerate(segments[1:], 1):
            segment_id = _segment_id(segment)
            reason = (
                "unknown_z_segment" if _is_z_segment(segment_id) else "no_order_group"
            )
            copied = copy.deepcopy(segment)
            _tag_segment(
                copied, ROLE_UNASSIGNED, index, parent_control_id, reason=reason
            )
            unassigned.append(copied)
        return ArchiveResult(
            [],
            parent_message=message,
            parent_message_control_id=parent_control_id,
            parent_message_control_ids=[parent_control_id],
            parent_position=message_position,
            unassigned=unassigned,
            common_segments=(),
            rules=rules,
        )

    first_order_index = starts[0][0]
    common_indexes: set[int] = set()
    unassigned: list[Any] = []

    def mark_unassigned(index: int, reason: str) -> None:
        copied = copy.deepcopy(segments[index])
        _tag_segment(copied, ROLE_UNASSIGNED, index, parent_control_id, reason=reason)
        copied.parent_message_position = message_position
        copied.parent_message_index = message_position
        unassigned.append(copied)

    # Explicit rule membership beats the default patient/visit classification
    # (a caller that puts PID into an order rule gets it in that order).  A Z
    # segment is only common if the caller explicitly lists it in
    # common_patterns; otherwise it stays pending.
    def common_candidate(segment_id: str) -> bool:
        if _matches(segment_id, common_patterns):
            return True
        return not _is_z_segment(segment_id) and _matches(
            segment_id, patient_patterns + visit_patterns
        )

    # Everything before the first order is common prefix material.  A Z segment
    # is only common if the caller explicitly listed it; otherwise it stays
    # pending so it cannot silently become part of every order.
    for index in range(1, first_order_index):
        segment_id = _segment_id(segments[index])
        if common_candidate(segment_id):
            common_indexes.add(index)
        elif _is_z_segment(segment_id):
            mark_unassigned(index, "unknown_z_segment")
        elif include_prefix:
            common_indexes.add(index)
        else:
            mark_unassigned(index, "segment_before_first_order")

    groups: list[dict[str, Any]] = []
    for position, (start_index, rule) in enumerate(starts):
        end_limit = (
            starts[position + 1][0] if position + 1 < len(starts) else len(segments)
        )
        order_indexes = [start_index]
        group_common: list[int] = []
        group_unassigned: list[tuple[int, str]] = []
        stopped_at: Optional[int] = None

        for index in range(start_index + 1, end_limit):
            segment_id = _segment_id(segments[index])

            if rule.end and _matches(segment_id, rule.end):
                stopped_at = index
                for remaining in range(index, end_limit):
                    remaining_id = _segment_id(segments[remaining])
                    reason = (
                        "unknown_z_segment"
                        if _is_z_segment(remaining_id)
                        else "after_group_end"
                    )
                    group_unassigned.append((remaining, reason))
                break

            if rule.explicitly_accepts(segment_id):
                order_indexes.append(index)
            elif common_candidate(segment_id):
                group_common.append(index)
            elif rule.accepts(segment_id):
                order_indexes.append(index)
            else:
                reason = (
                    "unknown_z_segment"
                    if _is_z_segment(segment_id)
                    else "segment_not_in_rule"
                )
                group_unassigned.append((index, reason))

        groups.append(
            {
                "start": start_index,
                "end": end_limit,
                "content_end": stopped_at if stopped_at is not None else end_limit,
                "rule": rule,
                "order_indexes": order_indexes,
                "common_in_group": group_common,
                "unassigned": group_unassigned,
            }
        )

    # A genuine data-level common segment (for example a late PID) is common in
    # every child, matching its role as shared context rather than an event.
    for group in groups:
        common_indexes.update(group["common_in_group"])
        for index, reason in group["unassigned"]:
            mark_unassigned(index, reason)

    children: list[Message] = []
    for offset, group in enumerate(groups):
        order_index = order_index_offset + offset + 1
        start_index = group["start"]
        end_position = group["content_end"]

        new_control_id = _new_control_id(
            control_id_factory,
            used_control_ids,
            parent_control_id,
            order_index,
        )
        used_control_ids.add(new_control_id)

        msh = copy.deepcopy(segments[0])
        msh.assign_field(new_control_id, 10)
        _tag_segment(msh, ROLE_CONTROL, 0, parent_control_id)

        child_segments = [msh]
        for index in sorted(common_indexes):
            copied = copy.deepcopy(segments[index])
            _tag_segment(copied, ROLE_COMMON, index, parent_control_id)
            child_segments.append(copied)
        for index in group["order_indexes"]:
            copied = copy.deepcopy(segments[index])
            _tag_segment(copied, ROLE_ORDER, index, parent_control_id)
            child_segments.append(copied)

        child = message.create_message(child_segments)
        child_msh = child[0]

        child.parent_message_control_id = parent_control_id
        child.parent_control_id = parent_control_id
        child.parent_msh10 = parent_control_id
        child.parent_message_id = parent_control_id
        child.source_message_control_id = parent_control_id
        child.parent_position = start_index
        child.parent_segment_index = start_index
        child.parent_segment_position = start_index
        child.parent_segment_number = start_index + 1
        child.parent_segment_range = (start_index, end_position)
        child.parent_segment_indexes = tuple(segment.source_index for segment in child)
        child.parent_order_segment_indexes = tuple(
            segment.source_index
            for segment in child
            if getattr(segment, "archive_role", None) == ROLE_ORDER
        )
        child.parent_common_segment_indexes = tuple(
            segment.source_index
            for segment in child
            if getattr(segment, "archive_role", None) == ROLE_COMMON
        )
        child.parent_message_position = message_position
        child.parent_message_index = message_position
        child.parent_location = ParentLocation(
            message_control_id=parent_control_id,
            position=start_index,
            end_position=end_position,
            segment_indexes=child.parent_segment_indexes,
            message_position=message_position,
        )

        child.order_index = order_index
        child.order_trigger = _segment_id(segments[start_index])
        child.order_key = _order_key(segments[start_index], order_index)
        child.order_id = child.order_key
        child.is_order_archive = True
        child.archive_role = ROLE_ORDER

        child.control_segments = [
            segment
            for segment in child
            if getattr(segment, "archive_role", None) == ROLE_CONTROL
        ]
        child.common_segments = [
            segment
            for segment in child
            if getattr(segment, "archive_role", None) == ROLE_COMMON
        ]
        child.event_segments = [
            segment
            for segment in child
            if getattr(segment, "archive_role", None) == ROLE_ORDER
        ]
        child.common_segment_count = len(child.common_segments)
        child.event_segment_count = len(child.event_segments)
        child.msh = child_msh
        children.append(child)

    common_segments = []
    for index in sorted(common_indexes):
        copied = copy.deepcopy(segments[index])
        _tag_segment(copied, ROLE_COMMON, index, parent_control_id)
        common_segments.append(copied)

    return ArchiveResult(
        children,
        parent_message=message,
        parent_message_control_id=parent_control_id,
        parent_message_control_ids=[parent_control_id],
        parent_position=message_position,
        unassigned=unassigned,
        common_segments=common_segments,
        rules=rules,
    )


def split_orders(
    message: Message | Batch | File,
    rules: Any = None,
    *,
    segment_groups: Any = None,
    patient_segments: Optional[Iterable[str] | str] = DEFAULT_PATIENT_SEGMENTS,
    visit_segments: Optional[Iterable[str] | str] = DEFAULT_VISIT_SEGMENTS,
    common_segments: Optional[Iterable[str] | str] = (),
    include_prefix: bool = True,
    control_id_factory: Optional[Callable[..., str]] = None,
    parent_position: Optional[int] = None,
) -> ArchiveResult:
    """Split *message* into independent per-order archive messages.

    ``rules`` describes the caller's segment groups.  See
    :func:`normalize_rules` for accepted forms.  ``segment_groups`` is an alias
    for callers that prefer that name.

    Each child receives a copy of the MSH with a newly generated ``MSH-10``.
    Patient (``PID``/``PD1``/``NK1``), visit (``PV1``/``PV2``) and the common
    prefix before the first order are included in every child.  These common
    segments are tagged ``archive_role == "common"`` and are not counted as
    order events.

    A ``Z`` segment is only assigned to a group when the matching rule claims
    it (``z_segments``, ``include_z`` or an explicit ``members`` entry) or when
    it is explicitly declared common.  Otherwise it is returned in
    :attr:`ArchiveResult.unassigned` with its source index, never attached to
    the nearest order.

    For a :class:`~hl7.containers.Batch` or :class:`~hl7.containers.File`, every
    contained message is split and the results are concatenated.  Each child's
    ``parent_message_position`` identifies the message in the container.

    :rtype: :class:`ArchiveResult`
    """
    if rules is None:
        rules = segment_groups
    elif segment_groups is not None:
        raise TypeError("Pass only one of rules or segment_groups")

    patient_patterns = _as_tuple(patient_segments)
    visit_patterns = _as_tuple(visit_segments)
    common_patterns = _as_tuple(common_segments)
    normalized_rules = normalize_rules(rules)

    parents = _message_messages(message)
    all_children: list[Message] = []
    all_unassigned: list[Any] = []
    common_by_location: dict[tuple[Optional[int], int], Any] = {}
    parent_control_ids: list[str] = []
    used_control_ids: set[str] = set()
    order_offset = 0

    for child_message, message_position in parents:
        effective_position = (
            parent_position if parent_position is not None else message_position
        )
        result = _split_message(
            child_message,
            normalized_rules,
            patient_patterns=patient_patterns,
            visit_patterns=visit_patterns,
            common_patterns=common_patterns,
            include_prefix=include_prefix,
            control_id_factory=control_id_factory,
            message_position=effective_position,
            used_control_ids=used_control_ids,
            order_index_offset=order_offset,
        )
        parent_control_ids.extend(result.parent_message_control_ids)
        all_children.extend(result)
        all_unassigned.extend(result.unassigned)
        for segment in result.common_segments:
            key = (effective_position, segment.source_index)
            if key not in common_by_location:
                common_by_location[key] = segment
        order_offset += len(result)

    return ArchiveResult(
        all_children,
        parent_message=message,
        parent_message_control_id=parent_control_ids[0] if parent_control_ids else "",
        parent_message_control_ids=parent_control_ids,
        parent_position=parent_position,
        unassigned=all_unassigned,
        common_segments=list(common_by_location.values()),
        rules=normalized_rules,
    )


class OrderArchiver:
    """Reusable splitter for a fixed set of rules and options."""

    def __init__(self, message: Message | Batch | File, rules: Any, **options: Any):
        self.message = message
        self.rules = normalize_rules(rules)
        self.options = options

    def split(self) -> ArchiveResult:
        return split_orders(self.message, self.rules, **self.options)

    def archive(self) -> ArchiveResult:
        return self.split()

    def __call__(self) -> ArchiveResult:
        return self.split()


SegmentGroup = SegmentGroupRule
ArchiveSplitter = OrderArchiver
archive_orders = split_orders

__all__ = [
    "ROLE_CONTROL",
    "ROLE_COMMON",
    "ROLE_ORDER",
    "ROLE_UNASSIGNED",
    "DEFAULT_PATIENT_SEGMENTS",
    "DEFAULT_VISIT_SEGMENTS",
    "SegmentGroupRule",
    "SegmentGroup",
    "normalize_rules",
    "ParentLocation",
    "ArchiveResult",
    "OrderArchiver",
    "ArchiveSplitter",
    "split_orders",
    "archive_orders",
]
