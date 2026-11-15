"""Stable references to segment occurrences and field-level issues.

A parsed :class:`hl7.Message` is an ordered collection of mutable segment
objects.  Cleaning a message (for example, inserting an extra ``PID``) changes
the positional accessor paths, but must not change which segment a previously
registered issue belongs to.

This module separates those two concepts:

* :class:`SegmentOccurrence` is an identity-based handle for one segment
  object.  Its ``occurrence_id`` is stable for the lifetime of the object.
* :class:`FieldIssue` binds an issue to that occurrence.  The current accessor
  path is recalculated from the message's current order whenever it is read.

Identity is always based on object identity.  Segments with the same name and
identical field values remain separate occurrences; issue lookup never falls
back to comparing content.
"""

from __future__ import annotations

import itertools

from .accessor import Accessor
from .exceptions import MissingOccurrenceError

_occurrence_counter = itertools.count(1)


def make_occurrence_id():
    """Return a process-unique identifier for a segment occurrence."""
    return "segment-{0}".format(next(_occurrence_counter))


def _segment_name(segment):
    try:
        return segment[0][0]
    except (IndexError, KeyError, TypeError):
        return ""


class SegmentOccurrence:
    """A stable handle for one particular segment object in *message*.

    The handle deliberately stores the segment object rather than its index.
    ``segment_num`` and :meth:`path` are derived from the current order, so
    inserting or removing other segments does not alter the association.
    """

    __slots__ = ("_message", "_segment")

    def __init__(self, message, segment):
        self._message = message
        self._segment = segment

    @property
    def occurrence_id(self):
        """Stable identifier of this occurrence."""
        return self._segment.occurrence_id

    #: Aliases for callers that name the identity differently.
    uid = occurrence_id
    stable_id = occurrence_id
    identity = occurrence_id

    # A short alias for callers that treat the occurrence itself as an id.
    id = occurrence_id

    @property
    def segment(self):
        """The referenced :class:`hl7.Segment` object."""
        return self._segment

    @property
    def segment_id(self):
        """Three character HL7 segment identifier, e.g. ``"PID"``."""
        return _segment_name(self._segment)

    @property
    def is_valid(self):
        """``True`` while the original occurrence is still in the message."""
        return self._message._contains_segment(self._segment)

    #: Alias for :attr:`is_valid`.
    valid = is_valid

    @property
    def is_deleted(self):
        """``True`` once the original occurrence has been removed."""
        return not self.is_valid

    deleted = is_deleted

    @property
    def segment_num(self):
        """Current 1-based number among segments with the same identifier."""
        if not self.is_valid:
            return None
        return self._message._segment_num(self._segment)

    def accessor(
        self,
        field_num=None,
        repeat_num=None,
        component_num=None,
        subcomponent_num=None,
    ):
        """Build an :class:`hl7.Accessor` using the *current* position.

        Returns ``None`` if the occurrence has been deleted.
        """
        segment_num = self.segment_num
        if segment_num is None:
            return None
        return Accessor(
            self.segment_id,
            segment_num,
            field_num,
            repeat_num,
            component_num,
            subcomponent_num,
        )

    def path(
        self,
        field_num=None,
        repeat_num=None,
        component_num=None,
        subcomponent_num=None,
    ):
        """Return the current accessor key, or ``None`` if deleted."""
        accessor = self.accessor(field_num, repeat_num, component_num, subcomponent_num)
        return None if accessor is None else accessor.key

    def __eq__(self, other):
        if not isinstance(other, SegmentOccurrence):
            return NotImplemented
        return self._message is other._message and self._segment is other._segment

    def __hash__(self):
        return hash((id(self._message), id(self._segment)))

    def __repr__(self):
        return "<SegmentOccurrence {0} {1}>".format(self.segment_id, self.occurrence_id)


class FieldIssue:
    """An issue registered against a field of one segment occurrence.

    ``original_path`` records the path at registration time.  ``current_path``
    is computed on demand and follows the segment when other segments are
    inserted before it.  Once the original occurrence is deleted the issue is
    stale: ``current_path`` becomes ``None`` and :meth:`resolve` raises
    :class:`hl7.MissingOccurrenceError` instead of falling through to whatever
    segment now occupies the old position.
    """

    def __init__(
        self,
        message,
        segment,
        field_num=1,
        repeat_num=None,
        component_num=None,
        subcomponent_num=None,
        description="",
        **metadata,
    ):
        self._message = message
        self._segment = segment
        self._invalidated = False

        self.occurrence_id = segment.occurrence_id
        self.field_num = self._positive_int(field_num, "field_num", default=1)
        self.repeat_num = self._optional_positive_int(repeat_num, "repeat_num")
        self.component_num = self._optional_positive_int(component_num, "component_num")
        self.subcomponent_num = self._optional_positive_int(
            subcomponent_num, "subcomponent_num"
        )
        self.description = description
        self.metadata = dict(metadata)

        self.original_segment_num = message._segment_num(segment)
        original = self._accessor_for(self.original_segment_num)
        self.original_path = None if original is None else original.key

    @staticmethod
    def _positive_int(value, name, default=None):
        if value is None:
            value = default
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("{0} must be a positive integer".format(name))
        if value < 1:
            raise ValueError("{0} must be a positive integer".format(name))
        return value

    @classmethod
    def _optional_positive_int(cls, value, name):
        if value is None:
            return None
        return cls._positive_int(value, name)

    @property
    def segment(self):
        """The original segment object, even after it has been deleted."""
        return self._segment

    @property
    def segment_id(self):
        return _segment_name(self._segment)

    @property
    def is_valid(self):
        """Whether this issue still resolves to its original occurrence."""
        return not self._invalidated and self._message._contains_segment(self._segment)

    #: Alias for :attr:`is_valid`.
    valid = is_valid

    @property
    def is_stale(self):
        return not self.is_valid

    stale = is_stale
    orphaned = is_stale

    def invalidate(self):
        """Permanently detach this issue from its original occurrence."""
        self._invalidated = True
        return self

    def _accessor_for(self, segment_num):
        if segment_num is None:
            return None
        return Accessor(
            self.segment_id,
            segment_num,
            self.field_num,
            self.repeat_num,
            self.component_num,
            self.subcomponent_num,
        )

    @property
    def original_accessor(self):
        """Accessor path captured when the issue was registered."""
        return self._accessor_for(self.original_segment_num)

    @property
    def current_segment_num(self):
        """Current 1-based segment number, or ``None`` if deleted."""
        if not self.is_valid:
            return None
        return self._message._segment_num(self._segment)

    @property
    def current_accessor(self):
        """Accessor using the segment's current position.

        Returns ``None`` for a stale issue.
        """
        return self._accessor_for(self.current_segment_num)

    @property
    def current_path(self):
        """Current accessor key, or ``None`` for a stale issue."""
        accessor = self.current_accessor
        return None if accessor is None else accessor.key

    @property
    def relocated(self):
        """``True`` when the occurrence moved to a different segment number.

        A stale issue is not considered relocated.
        """
        current = self.current_path
        return current is not None and current != self.original_path

    def resolve(self):
        """Return the original segment occurrence.

        Raises :class:`hl7.MissingOccurrenceError` if it has been deleted.
        """
        if not self.is_valid:
            raise MissingOccurrenceError(
                "segment occurrence {0} is no longer in the message".format(
                    self.occurrence_id
                )
            )
        return self._segment

    def current_value(self):
        """Read the field through the current path, if it is still valid."""
        accessor = self.current_accessor
        if accessor is None:
            return None
        try:
            return self._message[accessor]
        except (IndexError, KeyError):
            return None

    def as_dict(self):
        """Return a JSON-friendly snapshot of the issue."""
        return {
            "occurrence_id": self.occurrence_id,
            "segment_id": self.segment_id,
            "field_num": self.field_num,
            "repeat_num": self.repeat_num,
            "component_num": self.component_num,
            "subcomponent_num": self.subcomponent_num,
            "description": self.description,
            "metadata": dict(self.metadata),
            "original_path": self.original_path,
            "current_path": self.current_path,
            "valid": self.is_valid,
        }

    def __repr__(self):
        return "<FieldIssue {0} {1}>".format(
            self.occurrence_id, self.current_path or "deleted"
        )


class FieldIssueRegistry:
    """Ordered collection of :class:`FieldIssue` objects for one message."""

    def __init__(self, message):
        self._message = message
        self._issues = []

    def register(
        self,
        target,
        field_num=None,
        repeat_num=None,
        component_num=None,
        subcomponent_num=None,
        description="",
        **metadata,
    ):
        """Register a field issue against *target*.

        *target* may be a :class:`hl7.Segment`, a
        :class:`SegmentOccurrence`, an :class:`hl7.Accessor`, or an accessor
        key such as ``"PID2.3"``.  The occurrence is captured at registration
        time; later insertions only affect :attr:`FieldIssue.current_path`.
        """
        segment, accessor = self._resolve_target(target)

        if accessor is not None:
            if field_num is None:
                field_num = accessor.field_num
            if repeat_num is None:
                repeat_num = accessor.repeat_num
            if component_num is None:
                component_num = accessor.component_num
            if subcomponent_num is None:
                subcomponent_num = accessor.subcomponent_num

        issue = FieldIssue(
            self._message,
            segment,
            field_num=field_num,
            repeat_num=repeat_num,
            component_num=component_num,
            subcomponent_num=subcomponent_num,
            description=description,
            **metadata,
        )
        self._issues.append(issue)
        return issue

    #: Alias for :meth:`register`.
    add = register

    def _resolve_target(self, target):
        # Imported lazily to avoid a container <-> issues import cycle.
        from .containers import Segment

        if isinstance(target, SegmentOccurrence):
            segment = target.segment
            self._require_member(segment)
            return segment, None
        if isinstance(target, Segment):
            self._require_member(target)
            return target, None
        if isinstance(target, Accessor):
            return self._segment_for_accessor(target), target
        if isinstance(target, str):
            occurrence = self._message.find_occurrence(target)
            if occurrence is not None:
                return occurrence.segment, None
            if "." in target:
                try:
                    accessor = Accessor.parse_key(target)
                except (ValueError, IndexError):
                    raise MissingOccurrenceError(
                        "unknown accessor {0!r}".format(target)
                    )
                return self._segment_for_accessor(accessor), accessor
            raise MissingOccurrenceError(
                "unknown segment occurrence {0!r}".format(target)
            )
        raise TypeError("target must be a Segment, SegmentOccurrence, Accessor or key")

    def _require_member(self, segment):
        if not self._message._contains_segment(segment):
            raise MissingOccurrenceError(
                "segment occurrence {0} is not in the message".format(
                    segment.occurrence_id
                )
            )

    def _segment_for_accessor(self, accessor):
        try:
            return self._message.segments(accessor.segment)(accessor.segment_num)
        except (IndexError, KeyError):
            raise MissingOccurrenceError("no segment matching {0}".format(accessor.key))

    @property
    def issues(self):
        return tuple(self._issues)

    def __iter__(self):
        return iter(self._issues)

    def __len__(self):
        return len(self._issues)

    def __bool__(self):
        return bool(self._issues)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self._issues[index])
        return self._issues[index]

    def valid(self):
        """Return issues that still resolve to their original occurrence."""
        return tuple(issue for issue in self._issues if issue.is_valid)

    def orphaned(self):
        """Return issues whose original occurrence has been deleted."""
        return tuple(issue for issue in self._issues if not issue.is_valid)

    #: Alias for :meth:`orphaned`.
    stale = orphaned

    def for_segment(self, segment):
        """Return issues registered for *segment* (identity comparison)."""
        return tuple(issue for issue in self._issues if issue.segment is segment)

    def remove(self, issue):
        """Remove *issue* from the registry.

        This discards the registry entry; use :meth:`FieldIssue.invalidate` to
        keep the record but mark it stale.
        """
        for index, current in enumerate(self._issues):
            if current is issue:
                del self._issues[index]
                return True
        return False

    #: Alias for :meth:`remove`.
    discard = remove

    def clear(self):
        self._issues.clear()
