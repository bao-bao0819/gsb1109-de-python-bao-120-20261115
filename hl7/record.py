"""Apply incremental HL7 messages to a patient record.

HL7 v2 update messages commonly carry only the values that changed.  This
module keeps a record (an ``ADT`` style patient record for example) and applies
subsequent messages to it using field level rules:

* a field that is absent from the update is left untouched;
* an empty field is also left untouched;
* the HL7 null marker (``""``) clears the value at that repetition; a lone
  null addresses the whole field;
* every repetition has its own value, so clearing one repetition does not
  remove its siblings;
* an update that is older than the value already stored for a repetition is
  ignored, even when it arrives late.

Every accepted change keeps the location of the value in the source message.
The location includes the message control id, the segment occurrence, the
field, the repetition and, for a composite path, the component or
sub-component.
"""

import datetime

from .accessor import Accessor
from .containers import Batch, Factory, File, Message
from .datatypes import parse_datetime
from .parser import parse_hl7

#: The HL7 null value.  It is distinct from a field that was not supplied.
NULL_VALUE = '""'

PRESERVE = "preserve"
CLEAR = "clear"
_ACTIONS = (PRESERVE, CLEAR)
_ACTION_ALIASES = {
    "preserve": PRESERVE,
    "keep": PRESERVE,
    "ignore": PRESERVE,
    "unchanged": PRESERVE,
    "no_change": PRESERVE,
    "none": PRESERVE,
    "clear": CLEAR,
    "null": CLEAR,
    "empty": CLEAR,
    "delete": CLEAR,
    "remove": CLEAR,
}


def _as_action(value, default):
    if value is None:
        return default
    if isinstance(value, FieldPolicy):
        return value
    if isinstance(value, str):
        action = value.strip().lower()
        action = _ACTION_ALIASES.get(action, action)
        if action not in _ACTIONS:
            raise ValueError("Unknown field policy action: {0}".format(value))
        return action
    raise TypeError("Field policy action must be a string")


class FieldPolicy:
    """Rules used while applying one field.

    ``empty`` controls a field/repetition that is present in the message but
    has no value.  ``null`` controls the explicit HL7 null marker.  Both default
    to leaving the stored value alone / clearing respectively, matching the
    usual HL7 update semantics.
    """

    __slots__ = ("absent", "empty", "null")

    def __init__(self, absent=PRESERVE, empty=PRESERVE, null=CLEAR):
        self.absent = _as_action(absent, PRESERVE)
        self.empty = _as_action(empty, PRESERVE)
        self.null = _as_action(null, CLEAR)

    def action_for(self, value):
        """Return the action for an incoming field or repetition *value*.

        ``None`` is used when the field or repetition is absent from the
        message.  Callers normally do not reach this path because absent
        values are never iterated, but treating it as ``preserve`` keeps the
        rule explicit.
        """
        if value is None:
            return self.absent
        if value == "":
            return self.empty
        if value == NULL_VALUE:
            return self.null
        return "set"

    def __eq__(self, other):
        if isinstance(other, FieldPolicy):
            return (
                self.absent == other.absent
                and self.empty == other.empty
                and self.null == other.null
            )
        return NotImplemented

    def __hash__(self):
        return hash((self.absent, self.empty, self.null))

    def __repr__(self):
        return "FieldPolicy(absent={0!r}, empty={1!r}, null={2!r})".format(
            self.absent, self.empty, self.null
        )


class RecordPolicy:
    """A default :class:`FieldPolicy` plus optional per-field overrides."""

    def __init__(self, default=None, fields=None):
        self.default = _coerce_field_policy(default)
        self.fields = {}
        if fields:
            for key, value in dict(fields).items():
                self.fields[_policy_key(key)] = _coerce_field_policy(value)

    def for_accessor(self, accessor):
        """Return the policy for *accessor*, most specific match first."""
        candidates = [
            accessor.key,
            Accessor(accessor.segment, accessor.segment_num, accessor.field_num).key,
            Accessor(accessor.segment, accessor.segment_num).key,
        ]
        for candidate in candidates:
            if candidate in self.fields:
                return self.fields[candidate]
        return self.default

    def with_field(self, key, policy):
        """Return a copy with one more field override."""
        fields = {_policy_key(k): v for k, v in self.fields.items()}
        fields[_policy_key(key)] = _coerce_field_policy(policy)
        return RecordPolicy(default=self.default, fields=fields)


def _coerce_field_policy(value):
    if value is None:
        return FieldPolicy()
    if isinstance(value, FieldPolicy):
        return value
    if isinstance(value, dict):
        return FieldPolicy(**value)
    if isinstance(value, str):
        # A single action is most naturally applied to the empty form.  It
        # still leaves explicit nulls with their normal clearing behaviour.
        return FieldPolicy(empty=value)
    raise TypeError("Unsupported field policy: {0!r}".format(value))


def _coerce_policy(value):
    if value is None:
        return RecordPolicy()
    if isinstance(value, RecordPolicy):
        return value
    if isinstance(value, FieldPolicy):
        return RecordPolicy(default=value)
    if isinstance(value, dict):
        if set(value).issubset({"absent", "empty", "null"}):
            return RecordPolicy(default=FieldPolicy(**value))
        return RecordPolicy(fields=value)
    raise TypeError("Unsupported record policy: {0!r}".format(value))


def _policy_key(key):
    if isinstance(key, Accessor):
        return key.key
    return str(key)


class MessageVersion:
    """Ordering information for one message.

    The message timestamp (MSH-7) is authoritative.  The local apply sequence
    breaks ties and keeps messages without a timestamp deterministic.
    """

    __slots__ = ("timestamp", "sequence", "control_id", "raw_timestamp")

    def __init__(self, timestamp=None, sequence=0, control_id=None, raw_timestamp=None):
        self.timestamp = timestamp
        self.sequence = int(sequence)
        self.control_id = control_id
        self.raw_timestamp = raw_timestamp

    @staticmethod
    def _timestamp_key(value):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        # Naive values are treated as UTC only to keep them comparable with
        # timezone-aware values.  The original timestamp remains available in
        # ``raw_timestamp``.
        return value

    def _compare(self, other):
        if self.timestamp is not None and other.timestamp is not None:
            left = self._timestamp_key(self.timestamp)
            right = self._timestamp_key(other.timestamp)
            if left != right:
                return -1 if left < right else 1
        # Without a timestamp on either side, application order is the only
        # available ordering.
        if self.sequence != other.sequence:
            return -1 if self.sequence < other.sequence else 1
        return 0

    def __lt__(self, other):
        if not isinstance(other, MessageVersion):
            return NotImplemented
        return self._compare(other) < 0

    def __le__(self, other):
        if not isinstance(other, MessageVersion):
            return NotImplemented
        return self._compare(other) <= 0

    def __eq__(self, other):
        if isinstance(other, MessageVersion):
            return self._compare(other) == 0
        return NotImplemented

    def __hash__(self):
        timestamp = (
            self._timestamp_key(self.timestamp) if self.timestamp is not None else None
        )
        return hash((timestamp, self.sequence))

    def __repr__(self):
        return (
            "MessageVersion(timestamp={0!r}, sequence={1!r}, control_id={2!r})".format(
                self.timestamp, self.sequence, self.control_id
            )
        )


class MessageInfo:
    """Metadata of an applied update message."""

    __slots__ = (
        "index",
        "control_id",
        "timestamp",
        "message_type",
        "trigger_event",
        "version",
    )

    def __init__(
        self,
        index,
        control_id=None,
        timestamp=None,
        message_type=None,
        trigger_event=None,
        version=None,
    ):
        self.index = index
        self.control_id = control_id
        self.timestamp = timestamp
        self.message_type = message_type
        self.trigger_event = trigger_event
        self.version = version

    @property
    def message_control_id(self):
        return self.control_id

    @property
    def message_timestamp(self):
        return self.timestamp

    def as_dict(self):
        return {
            "index": self.index,
            "control_id": self.control_id,
            "timestamp": self.timestamp,
            "message_type": self.message_type,
            "trigger_event": self.trigger_event,
        }

    def __repr__(self):
        return "MessageInfo(index={0!r}, control_id={1!r}, timestamp={2!r})".format(
            self.index, self.control_id, self.timestamp
        )


class SourceLocation:
    """The original position of a value in an HL7 message."""

    __slots__ = (
        "segment",
        "segment_num",
        "field_num",
        "repeat_num",
        "component_num",
        "subcomponent_num",
        "message_index",
        "message_control_id",
        "message_timestamp",
        "segment_index",
        "raw",
    )

    def __init__(
        self,
        segment,
        segment_num=1,
        field_num=None,
        repeat_num=None,
        component_num=None,
        subcomponent_num=None,
        message_index=None,
        message_control_id=None,
        message_timestamp=None,
        segment_index=None,
        raw=None,
    ):
        self.segment = segment
        self.segment_num = segment_num
        self.field_num = field_num
        self.repeat_num = repeat_num
        self.component_num = component_num
        self.subcomponent_num = subcomponent_num
        self.message_index = message_index
        self.message_control_id = message_control_id
        self.message_timestamp = message_timestamp
        self.segment_index = segment_index
        self.raw = raw

    @property
    def accessor(self):
        return Accessor(
            self.segment,
            self.segment_num,
            self.field_num,
            self.repeat_num,
            self.component_num,
            self.subcomponent_num,
        )

    @property
    def key(self):
        return self.accessor.key

    def with_path(self, component_num=None, subcomponent_num=None):
        """Copy the location with a more specific component path."""
        return SourceLocation(
            segment=self.segment,
            segment_num=self.segment_num,
            field_num=self.field_num,
            repeat_num=self.repeat_num,
            component_num=component_num
            if component_num is not None
            else self.component_num,
            subcomponent_num=subcomponent_num
            if subcomponent_num is not None
            else self.subcomponent_num,
            message_index=self.message_index,
            message_control_id=self.message_control_id,
            message_timestamp=self.message_timestamp,
            segment_index=self.segment_index,
            raw=self.raw,
        )

    def as_dict(self):
        return {
            "segment": self.segment,
            "segment_num": self.segment_num,
            "field_num": self.field_num,
            "repeat_num": self.repeat_num,
            "component_num": self.component_num,
            "subcomponent_num": self.subcomponent_num,
            "message_index": self.message_index,
            "message_control_id": self.message_control_id,
            "message_timestamp": self.message_timestamp,
            "segment_index": self.segment_index,
        }

    def __str__(self):
        return self.key

    def __repr__(self):
        return (
            "SourceLocation({0!r}, message_control_id={1!r}, "
            "message_index={2!r})".format(
                self.key, self.message_control_id, self.message_index
            )
        )


class FieldChange:
    """One value change (or explicit clearing) in a patient record."""

    __slots__ = (
        "path",
        "old_value",
        "new_value",
        "location",
        "version",
        "operation",
    )

    def __init__(
        self,
        path,
        old_value,
        new_value,
        location,
        version=None,
        operation=None,
    ):
        self.path = path
        self.old_value = old_value
        self.new_value = new_value
        self.location = location
        self.version = version
        if operation is None:
            operation = "clear" if new_value is None else "set"
        self.operation = operation

    @property
    def accessor(self):
        return self.location.accessor

    @property
    def source(self):
        return self.location

    @property
    def message_control_id(self):
        return self.location.message_control_id

    @property
    def message_index(self):
        return self.location.message_index

    @property
    def key(self):
        return self.path

    @property
    def cleared(self):
        return self.operation == "clear"

    @property
    def changed(self):
        return self.old_value != self.new_value

    def as_dict(self):
        return {
            "path": self.path,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "operation": self.operation,
            "location": self.location.as_dict(),
        }

    def __str__(self):
        return "{0}: {1!r} -> {2!r}".format(
            self.location.key, self.old_value, self.new_value
        )

    def __repr__(self):
        return "FieldChange({0!r}, {1!r}, {2!r}, {3!r})".format(
            self.path, self.old_value, self.new_value, self.location
        )


class IgnoredChange:
    """A stale update that was not allowed to overwrite the record."""

    __slots__ = ("path", "incoming_value", "location", "reason")

    def __init__(self, path, incoming_value, location, reason):
        self.path = path
        self.incoming_value = incoming_value
        self.location = location
        self.reason = reason

    @property
    def source(self):
        return self.location

    def __repr__(self):
        return "IgnoredChange({0!r}, {1!r}, {2!r})".format(
            self.path, self.incoming_value, self.reason
        )


class _Node:
    """A node in the record tree.

    Only repetition nodes carry a value.  The intermediate nodes preserve the
    shape and ordering of the source message.  ``width`` records how many
    children were present in the source so cleared positions round-trip.
    """

    __slots__ = (
        "children",
        "value",
        "exists",
        "cleared",
        "version",
        "location",
        "width",
    )

    def __init__(self):
        self.children = {}
        self.value = None
        self.exists = False
        self.cleared = False
        self.version = None
        self.location = None
        self.width = 0

    def child(self, key, create=False):
        node = self.children.get(key)
        if node is None and create:
            node = _Node()
            self.children[key] = node
        return node


class PatientRecord:
    """A patient record built by applying HL7 messages in update order.

    ``base`` may be an already parsed :class:`hl7.Message` or an HL7 string.
    Subsequent calls to :meth:`apply` merge update messages into it.
    """

    def __init__(self, base=None, policy=None):
        self.policy = _coerce_policy(policy)
        self._root = _Node()
        self._segment_order = []
        self._messages = []
        self._changes = []
        self._initial_changes = []
        self._ignored_changes = []
        self._version = None
        self._attempts = 0
        self._factory = Factory
        self._esc = "\\"
        self._separators = "\r|~^&"
        if base is not None:
            self.apply(base, initial=True)

    @property
    def changes(self):
        """All accepted changes, in application order."""
        return tuple(self._changes)

    @property
    def history(self):
        return self.changes

    @property
    def initial_changes(self):
        """Values loaded from the base record, before any update was applied."""
        return tuple(self._initial_changes)

    @property
    def ignored_changes(self):
        return tuple(self._ignored_changes)

    @property
    def messages(self):
        return tuple(self._messages)

    @property
    def version(self):
        """The newest message version applied to this record."""
        return self._version

    @property
    def message(self):
        """The record represented as an :class:`hl7.Message`."""
        return self.to_message()

    def apply(self, message, version=None, initial=False):
        """Merge *message* into the record and return the changes made.

        ``version`` can override the message timestamp.  It accepts a
        :class:`MessageVersion`, ``datetime``, HL7 DTM string or an iterable of
        ``(timestamp, sequence)``.  A value older than the value currently
        stored for a repetition is ignored.
        """
        message, changes = self._apply_one(message, version=version, initial=initial)
        return changes

    update = apply
    merge = apply
    apply_message = apply

    def _apply_one(self, message, version=None, initial=False):
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        if isinstance(message, str):
            message = parse_hl7(message)

        if isinstance(message, File):
            return self._apply_file(message, version=version, initial=initial)
        if isinstance(message, Batch):
            return self._apply_batch(message, version=version, initial=initial)
        if not isinstance(message, Message):
            return self._apply_container(message, version=version, initial=initial)

        self._attempts += 1
        index = self._attempts
        info = self._message_info(message, index, version)

        self._factory = getattr(message, "factory", self._factory)
        separators = getattr(message, "separators", self._separators)
        esc = getattr(message, "esc", self._esc)
        if initial or not self._messages:
            self._esc = esc
            self._separators = separators
        elif separators != self._separators or esc != self._esc:
            raise ValueError("Update message uses different separators than the record")
        self._messages.append(info)
        if self._version is None or self._version < info.version:
            self._version = info.version

        changes = []
        occurrences = {}
        for segment_index, segment in enumerate(message, 1):
            segment_id = str(segment[0][0])
            occurrence = occurrences.get(segment_id, 0) + 1
            occurrences[segment_id] = occurrence
            segment_node = self._segment(segment_id, occurrence, create=True)
            segment_node.width = max(segment_node.width, len(segment) - 1)
            if (segment_id, occurrence) not in self._segment_order:
                self._segment_order.append((segment_id, occurrence))

            for field_num in range(1, len(segment)):
                # MSH-1 and MSH-2 describe the encoding of the message.  They
                # must not be replaced by an update message.
                if initial is False and segment_id in ("MSH", "BHS", "FHS"):
                    if field_num in (1, 2):
                        continue
                field = segment[field_num]
                raw_field = str(field)
                field_node = segment_node.child(field_num, create=True)
                field_node.width = max(field_node.width, len(field))
                if not initial and len(field) == 1 and str(field[0]) == NULL_VALUE:
                    # A lone null applies to the field as a whole.  Positional
                    # nulls such as ``A~""~C`` apply to one repetition only.
                    changes.extend(
                        self._clear_field(
                            field_node,
                            info,
                            segment_id,
                            occurrence,
                            field_num,
                            segment_index,
                            raw_field,
                        )
                    )
                    continue
                for repeat_num, repetition in enumerate(field, 1):
                    accessor = Accessor(
                        segment_id,
                        occurrence,
                        field_num,
                        repeat_num,
                        None,
                        None,
                    )
                    policy = self.policy.for_accessor(accessor)
                    value = str(repetition)
                    change = self._apply_repetition(
                        field_node,
                        repeat_num,
                        value,
                        policy,
                        info,
                        segment_id,
                        occurrence,
                        field_num,
                        segment_index,
                        raw_field,
                        initial=initial,
                    )
                    if isinstance(change, list):
                        changes.extend(change)
                    elif change is not None:
                        changes.append(change)

            if not initial:
                changes.extend(
                    self._apply_absent_field_policies(
                        segment_node,
                        segment_id,
                        occurrence,
                        segment_index,
                        info,
                        set(range(1, len(segment))),
                    )
                )

        if not initial:
            changes.extend(
                self._apply_absent_segment_policies(occurrences, segment_index, info)
            )

        if initial:
            self._initial_changes.extend(changes)
        else:
            self._changes.extend(changes)
        return message, changes

    def _apply_container(self, container, version=None, initial=False):
        """Apply a batch or file recursively."""
        changes = []
        for child in container:
            if isinstance(child, (File, Batch)):
                _, child_changes = self._apply_one(
                    child, version=version, initial=initial
                )
                changes.extend(child_changes)
            elif isinstance(child, Message):
                _, child_changes = self._apply_one(
                    child, version=version, initial=initial
                )
                changes.extend(child_changes)
        return container, changes

    def _apply_batch(self, batch, version=None, initial=False):
        changes = []
        for message in batch:
            if isinstance(message, Message):
                _, message_changes = self._apply_one(
                    message, version=version, initial=initial
                )
                changes.extend(message_changes)
        return batch, changes

    def _apply_file(self, hl7_file, version=None, initial=False):
        changes = []
        for batch in hl7_file:
            if isinstance(batch, Batch):
                _, batch_changes = self._apply_one(
                    batch, version=version, initial=initial
                )
                changes.extend(batch_changes)
            elif isinstance(batch, Message):
                _, message_changes = self._apply_one(
                    batch, version=version, initial=initial
                )
                changes.extend(message_changes)
        return hl7_file, changes

    def _message_info(self, message, index, version_override):
        control_id = None
        timestamp = None
        message_type = None
        trigger_event = None
        try:
            msh = message.segment("MSH")
        except KeyError:
            msh = None
        if msh is not None:
            control_id = self._msh_text(msh, 10)
            timestamp = self._msh_text(msh, 7)
            message_type = self._msh_component(msh, 9, 1)
            trigger_event = self._msh_component(msh, 9, 2)

        version = self._coerce_version(
            version_override,
            timestamp,
            index,
            control_id,
        )
        return MessageInfo(
            index=index,
            control_id=control_id,
            timestamp=timestamp,
            message_type=message_type,
            trigger_event=trigger_event,
            version=version,
        )

    @staticmethod
    def _msh_text(msh, field_num):
        try:
            value = str(msh[field_num])
        except (IndexError, KeyError):
            return None
        return value if value != "" else None

    @staticmethod
    def _msh_component(msh, field_num, component_num):
        try:
            field = msh[field_num]
        except (IndexError, KeyError):
            return None
        if len(field) == 0:
            return None
        repetition = field[0]
        if not hasattr(repetition, "__len__") or isinstance(repetition, str):
            return str(repetition) if component_num == 1 else None
        if len(repetition) < component_num:
            return None
        value = str(repetition[component_num - 1])
        return value if value != "" else None

    @staticmethod
    def _coerce_version(value, timestamp, sequence, control_id):
        if isinstance(value, MessageVersion):
            if value.sequence == 0:
                return MessageVersion(
                    value.timestamp,
                    sequence,
                    value.control_id or control_id,
                    value.raw_timestamp or timestamp,
                )
            return value
        if value is None:
            parsed = PatientRecord._parse_timestamp(timestamp)
            return MessageVersion(parsed, sequence, control_id, timestamp)
        if isinstance(value, datetime.datetime):
            return MessageVersion(value, sequence, control_id, timestamp)
        if isinstance(value, str):
            return MessageVersion(
                PatientRecord._parse_timestamp(value), sequence, control_id, value
            )
        try:
            parts = tuple(value)
        except TypeError:
            raise TypeError("Unsupported message version: {0!r}".format(value))
        if not parts:
            return MessageVersion(None, sequence, control_id, timestamp)
        raw_timestamp = parts[0]
        parsed = (
            raw_timestamp
            if isinstance(raw_timestamp, datetime.datetime)
            else PatientRecord._parse_timestamp(raw_timestamp)
        )
        explicit_sequence = parts[1] if len(parts) > 1 else sequence
        return MessageVersion(parsed, explicit_sequence, control_id, timestamp)

    @staticmethod
    def _parse_timestamp(value):
        if not value:
            return None
        try:
            return parse_datetime(value)
        except (TypeError, ValueError):
            return None

    def _segment(self, segment_id, occurrence, create=False):
        segment = self._root.child(segment_id, create=create)
        if segment is None:
            return None
        return segment.child(occurrence, create=create)

    def _apply_absent_field_policies(
        self,
        segment_node,
        segment_id,
        segment_occurrence,
        segment_index,
        info,
        present_fields,
    ):
        """Apply an ``absent=clear`` policy to fields missing from the update."""
        changes = []
        for field_num in sorted(segment_node.children):
            if field_num in present_fields:
                continue
            if segment_id in ("MSH", "BHS", "FHS") and field_num in (1, 2):
                continue
            accessor = Accessor(
                segment_id,
                segment_occurrence,
                field_num,
                1,
                None,
                None,
            )
            policy = self.policy.for_accessor(accessor)
            if policy.action_for(None) != CLEAR:
                continue
            field_node = segment_node.child(field_num)
            changes.extend(
                self._clear_field(
                    field_node,
                    info,
                    segment_id,
                    segment_occurrence,
                    field_num,
                    segment_index,
                    str(self._build_field(field_node)),
                )
            )
        return changes

    def _apply_absent_segment_policies(self, occurrences, segment_index, info):
        """Apply a segment-level ``absent=clear`` policy."""
        changes = []
        for segment_id, occurrence in list(self._segment_order):
            if segment_id in ("MSH", "BHS", "FHS"):
                continue
            if occurrences.get(segment_id, 0) >= occurrence:
                continue
            segment_node = self._segment(segment_id, occurrence)
            accessor = Accessor(segment_id, occurrence, None, None, None, None)
            policy = self.policy.for_accessor(accessor)
            if policy.action_for(None) != CLEAR:
                continue
            for field_num in sorted(segment_node.children):
                field_node = segment_node.child(field_num)
                changes.extend(
                    self._clear_field(
                        field_node,
                        info,
                        segment_id,
                        occurrence,
                        field_num,
                        segment_index,
                        str(self._build_field(field_node)),
                    )
                )
        return changes

    def _clear_field(
        self,
        field_node,
        info,
        segment_id,
        segment_occurrence,
        field_num,
        segment_index,
        raw_field,
    ):
        """Clear every stored repetition of a field without touching others."""
        changes = []
        repeat_nums = sorted(
            repeat_num
            for repeat_num, node in field_node.children.items()
            if node.exists
        )
        if field_node.version is not None and info.version < field_node.version:
            for repeat_num in repeat_nums:
                node = field_node.child(repeat_num)
                location = SourceLocation(
                    segment=segment_id,
                    segment_num=segment_occurrence,
                    field_num=field_num,
                    repeat_num=repeat_num,
                    message_index=info.index,
                    message_control_id=info.control_id,
                    message_timestamp=info.timestamp,
                    segment_index=segment_index,
                    raw=raw_field,
                )
                self._ignored_changes.append(
                    IgnoredChange(
                        location.key,
                        NULL_VALUE,
                        location,
                        "older than the value currently stored",
                    )
                )
            return changes

        remaining = []
        for repeat_num in repeat_nums:
            node = field_node.child(repeat_num)
            location = SourceLocation(
                segment=segment_id,
                segment_num=segment_occurrence,
                field_num=field_num,
                repeat_num=repeat_num,
                message_index=info.index,
                message_control_id=info.control_id,
                message_timestamp=info.timestamp,
                segment_index=segment_index,
                raw=raw_field,
            )
            if node.version is not None and info.version < node.version:
                self._ignored_changes.append(
                    IgnoredChange(
                        location.key,
                        NULL_VALUE,
                        location,
                        "older than the value currently stored",
                    )
                )
                remaining.append(repeat_num)
                continue
            changes.append(
                FieldChange(
                    location.key,
                    node.value,
                    None,
                    location,
                    version=info.version,
                    operation="clear",
                )
            )
            node.exists = False
            node.cleared = True
            node.value = None
            node.version = info.version
            node.location = location
        field_node.width = 0 if not remaining else max(remaining)
        if not remaining:
            field_node.version = info.version
            field_node.cleared = True
        return changes

    def _apply_repetition(
        self,
        field_node,
        repeat_num,
        value,
        policy,
        info,
        segment_id,
        segment_occurrence,
        field_num,
        segment_index,
        raw_field,
        initial=False,
    ):
        node = field_node.child(repeat_num, create=True)
        field_node.width = max(field_node.width, repeat_num)
        location = SourceLocation(
            segment=segment_id,
            segment_num=segment_occurrence,
            field_num=field_num,
            repeat_num=repeat_num,
            message_index=info.index,
            message_control_id=info.control_id,
            message_timestamp=info.timestamp,
            segment_index=segment_index,
            raw=raw_field,
        )
        action = policy.action_for(value)

        if action == PRESERVE:
            return None

        if not initial and node.version is not None and info.version < node.version:
            self._ignored_changes.append(
                IgnoredChange(
                    location.key,
                    value,
                    location,
                    "older than the value currently stored",
                )
            )
            return None
        if (
            not initial
            and node.version is None
            and field_node.cleared
            and field_node.version is not None
            and info.version < field_node.version
        ):
            # The whole field was cleared under an ``absent=clear`` policy by a
            # newer message; an older message must not repopulate it.
            self._ignored_changes.append(
                IgnoredChange(
                    location.key,
                    value,
                    location,
                    "field cleared by a newer message",
                )
            )
            return None

        old_value = node.value if node.exists else None
        if action == CLEAR:
            changed = node.exists
            node.exists = False
            node.cleared = True
            node.value = None
            node.version = info.version
            node.location = location
            if not changed:
                return None
            return FieldChange(
                location.key,
                old_value,
                None,
                location,
                version=info.version,
                operation="clear",
            )

        node.exists = True
        node.cleared = False
        node.version = info.version
        node.location = location
        field_node.cleared = False

        # A supplied repetition carries the complete value for that
        # repetition.  Components or sub-components explicitly marked with the
        # HL7 null are cleared; everything else is taken verbatim, so omitted
        # trailing components are not silently inherited from the old value.
        new_value = self._apply_explicit_nulls(value)
        node.value = new_value
        if old_value == new_value:
            return None
        return FieldChange(
            location.key,
            old_value,
            new_value,
            location,
            version=info.version,
            operation="initial" if initial else "set",
        )

    def _apply_explicit_nulls(self, value):
        """Clear components/sub-components whose value is the HL7 null."""
        component_sep = self._separators[3]
        subcomponent_sep = self._separators[4]
        if component_sep not in value and subcomponent_sep not in value:
            return value
        components = []
        for component in value.split(component_sep):
            if component == NULL_VALUE:
                components.append("")
                continue
            subcomponents = [
                "" if subcomponent == NULL_VALUE else subcomponent
                for subcomponent in component.split(subcomponent_sep)
            ]
            components.append(subcomponent_sep.join(subcomponents))
        return component_sep.join(components)

    def value(self, key, default=""):
        """Return the value at *key* (an accessor string or ``Accessor``)."""
        accessor = key if isinstance(key, Accessor) else Accessor.parse_key(key)
        segment = self._segment(accessor.segment, accessor.segment_num)
        if segment is None:
            return default
        if accessor.field_num is None:
            return default
        field = segment.child(accessor.field_num)
        if field is None:
            return default
        repeat_num = accessor.repeat_num or 1
        repetition = field.child(repeat_num)
        if repetition is None or not repetition.exists:
            return default
        value = repetition.value
        if accessor.component_num is not None:
            value = self._component(value, accessor.component_num)
            if value is None:
                return default
        if accessor.subcomponent_num is not None:
            value = self._subcomponent(value, accessor.subcomponent_num)
            if value is None:
                return default
        return value

    def get(self, key, default=""):
        return self.value(key, default=default)

    def __getitem__(self, key):
        if isinstance(key, Accessor):
            return self.value(key)
        if isinstance(key, str) and len(key) == 3:
            return self.segment(key)
        return self.value(key)

    def segment(self, segment_id, segment_num=1):
        """Return a segment occurrence as a new :class:`hl7.Segment`."""
        node = self._segment(segment_id, segment_num)
        if node is None:
            raise KeyError("No %s segments" % segment_id)
        return self._build_segment(segment_id, node)

    def history_for(self, key):
        """Return all accepted changes at *key* (or a child path of *key*)."""
        accessor = key if isinstance(key, Accessor) else Accessor.parse_key(key)
        prefix = accessor.key
        return tuple(
            change
            for change in self._changes
            if change.path == prefix
            or change.path.startswith(prefix + ".")
            or prefix.startswith(change.path + ".")
        )

    def provenance(self, key):
        """Return the :class:`SourceLocation` for the current value at *key*.

        The change log is searched newest first, so a component that was not
        touched by a later composite update keeps the message that actually
        supplied it.
        """
        accessor = key if isinstance(key, Accessor) else Accessor.parse_key(key)
        leaf = accessor.key
        for change in reversed(self._initial_changes + self._changes):
            if change.path == leaf:
                return change.location
            if leaf.startswith(change.path + "."):
                return change.location.with_path(
                    accessor.component_num, accessor.subcomponent_num
                )

        segment = self._segment(accessor.segment, accessor.segment_num)
        if segment is None or accessor.field_num is None:
            return None
        field = segment.child(accessor.field_num)
        if field is None:
            return None
        repeat_num = accessor.repeat_num or 1
        repetition = field.child(repeat_num)
        if repetition is None or not repetition.exists:
            return None
        location = repetition.location
        if location is None:
            return None
        if accessor.component_num is not None or accessor.subcomponent_num is not None:
            location = location.with_path(
                accessor.component_num, accessor.subcomponent_num
            )
        return location

    def locations(self, key=None):
        """Return provenance for every stored repetition, optionally filtered."""
        if key is not None:
            location = self.provenance(key)
            return () if location is None else (location,)
        locations = []
        for segment_id, occurrence in self._segment_order:
            segment = self._segment(segment_id, occurrence)
            for field_num in sorted(segment.children):
                field = segment.child(field_num)
                for repeat_num in sorted(field.children):
                    repetition = field.child(repeat_num)
                    if repetition.exists and repetition.location is not None:
                        locations.append(
                            self.provenance(
                                Accessor(
                                    segment_id,
                                    occurrence,
                                    field_num,
                                    repeat_num,
                                )
                            )
                            or repetition.location
                        )
        return tuple(locations)

    def _component(self, value, component_num):
        parts = value.split(self._separators[3])
        if component_num < 1 or component_num > len(parts):
            return None
        return parts[component_num - 1]

    def _subcomponent(self, value, subcomponent_num):
        if self._separators[3] in value:
            component = value.split(self._separators[3])[0]
        else:
            component = value
        subparts = component.split(self._separators[4])
        if subcomponent_num < 1 or subcomponent_num > len(subparts):
            return None
        return subparts[subcomponent_num - 1]

    def to_message(self):
        """Build a new :class:`hl7.Message` from the record."""
        segments = [
            self._build_segment(segment_id, self._segment(segment_id, occurrence))
            for segment_id, occurrence in self._segment_order
        ]
        return self._factory.create_message(
            sequence=segments,
            esc=self._esc,
            separators=self._separators,
            factory=self._factory,
        )

    def _build_segment(self, segment_id, node):
        fields = [self._build_field_value([segment_id])]
        width = node.width if node is not None else 0
        for field_num in range(1, width + 1):
            field_node = node.child(field_num)
            fields.append(
                self._build_field(field_node)
                if field_node is not None
                else self._build_field_value([""])
            )
        if segment_id in ("MSH", "BHS", "FHS"):
            while len(fields) < 3:
                if len(fields) == 1:
                    value = self._separators[1]
                else:
                    value = self._separators[2:]
                fields.append(self._build_field_value([value]))
        return self._factory.create_segment(
            sequence=fields,
            esc=self._esc,
            separators=self._separators,
            factory=self._factory,
        )

    def _build_field(self, node):
        width = node.width if node is not None else 0
        if width == 0:
            if node is not None and node.cleared:
                return self._build_field_value([NULL_VALUE])
            return self._build_field_value([""])
        values = []
        for repeat_num in range(1, width + 1):
            repetition = node.child(repeat_num)
            if repetition is None:
                values.append("")
            elif repetition.exists:
                values.append(repetition.value)
            elif repetition.cleared:
                values.append(NULL_VALUE)
            else:
                values.append("")
        # Drop trailing cleared repetitions, but keep interior tombstones so
        # the remaining repetitions retain their original identity.
        while len(values) > 1 and values[-1] in ("", NULL_VALUE):
            values.pop()
        if not any(value not in ("", NULL_VALUE) for value in values):
            if any(value == NULL_VALUE for value in values):
                return self._build_field_value([NULL_VALUE])
            return self._build_field_value([""])
        return self._build_field_value(values)

    def _build_field_value(self, values):
        return self._factory.create_field(
            sequence=values,
            esc=self._esc,
            separators=self._separators,
            factory=self._factory,
        )

    def to_hl7(self):
        """Return the record as an HL7 string."""
        return str(self.to_message())

    def __str__(self):
        return self.to_hl7()

    def __repr__(self):
        return "PatientRecord({0} changes)".format(len(self._changes))


#: Short alias for :class:`PatientRecord`.
Record = PatientRecord


def apply_incremental(base, *updates, **kwargs):
    """Apply *updates* to *base* and return a :class:`PatientRecord`.

    ``base`` may be a :class:`PatientRecord` or the first HL7 message.  The
    optional ``policy`` keyword accepts a :class:`RecordPolicy`,
    :class:`FieldPolicy` or a mapping of field paths to policies.
    """
    policy = kwargs.pop("policy", None)
    if kwargs:
        raise TypeError(
            "Unexpected keyword arguments: {0}".format(", ".join(sorted(kwargs)))
        )
    if isinstance(base, PatientRecord):
        record = base
        if policy is not None:
            record.policy = _coerce_policy(policy)
    else:
        record = PatientRecord(base, policy=policy)
    for update in updates:
        record.apply(update)
    return record


def merge_messages(messages, base=None, policy=None):
    """Merge a sequence of HL7 messages and return a :class:`PatientRecord`.

    ``messages`` may be a single message or an iterable.  If ``base`` is given
    it is applied first.
    """
    if isinstance(messages, (str, bytes, Message)):
        messages = [messages]
    record = PatientRecord(policy=policy)
    if base is not None:
        record.apply(base)
    for message in messages:
        record.apply(message)
    return record


#: Alias used by callers that prefer the shorter name.
merge = merge_messages
