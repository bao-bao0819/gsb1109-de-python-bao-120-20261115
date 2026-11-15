"""Patient identifier relationship table with reversible, time-bounded merges.

The module models an Enterprise Master Patient Index (EMPI) style identifier
registry on top of the identifiers carried in HL7 v2 ``PID-3`` (and similar)
fields.  An identifier is always scoped by its *assigning institution*
(namespace) and its *identifier type*, so the same number issued by two
different organisations is never treated as the same patient automatically.

Relationships between identifiers are only created through explicit
:class:`MergeNotification` objects.  The registry is bitemporal:

* *valid time* (``effective_at``) says when a merge is true in the real world;
* *system time* (``recorded_at`` and the reversal timestamp) says when the
  registry knew about it.

Point-in-time queries therefore interpret history using only the notifications
that were visible at the requested moment, and a reversal recorded later never
rewrites what the system used to believe.  Every notification, including
reversed ones, is retained for audit.  Encounters are additionally anchored to
the identifier they were filed under, so a new message can never move an old
encounter onto another person without an explicit, timestamped basis.

The implementation is transport agnostic.  :func:`identifiers_from_pid` bridges
parsed HL7 messages to the registry, while the registry itself works with plain
:class:`Identifier` values and :py:class:`datetime.datetime` timestamps.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .containers import Component, Repetition

__all__ = [
    "Identifier",
    "MergeNotification",
    "ReversalRecord",
    "EncounterRecord",
    "PatientIdentityRegistry",
    "IdentityError",
    "UnknownIdentifierError",
    "DuplicateIdentifierError",
    "MergeConflictError",
    "MergeNotFoundError",
    "EncounterAssignmentError",
    "identifiers_from_pid",
]

#: Horizon used when a caller does not constrain one of the two time axes.
HORIZON = datetime.max


class IdentityError(Exception):
    """Base class for identifier registry errors."""


class UnknownIdentifierError(IdentityError):
    """Raised when an identifier has never been registered."""


class DuplicateIdentifierError(IdentityError):
    """Raised when an identifier or notification is registered twice."""


class MergeConflictError(IdentityError):
    """Raised when an undo cannot separate identifiers still linked elsewhere."""


class MergeNotFoundError(IdentityError):
    """Raised when an undo references a notification that cannot be reversed."""


class EncounterAssignmentError(IdentityError):
    """Raised when an encounter cannot be attributed to the requested patient."""


def _coerce_utc(value):
    """Normalise *value* onto a naive UTC timeline.

    Aware datetimes are converted to UTC and made naive so values parsed from
    messages with and without timezone offsets stay comparable.  Naive
    datetimes are assumed to already be on the registry's reference clock.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _normalized_text(value, label):
    if value is None:
        raise ValueError("{0} must not be None".format(label))
    text = str(value).strip()
    if not text:
        raise ValueError("{0} must not be empty".format(label))
    return text


@dataclass(frozen=True)
class Identifier:
    """A patient identifier qualified by institution and identifier type.

    ``namespace`` is the assigning institution (HL7 ``PID-3.4`` assigning
    authority), ``value`` is the identifier string (``PID-3.1``) and
    ``id_type`` is the identifier type (``PID-3.5``), defaulting to an empty
    string when the source message omits it.  Two identifiers are only equal
    when all three parts match, so the same ``value`` from different
    institutions or of different types stays distinct.
    """

    namespace: str
    value: str
    id_type: str = ""

    def __post_init__(self):
        object.__setattr__(
            self, "namespace", _normalized_text(self.namespace, "namespace")
        )
        object.__setattr__(self, "value", _normalized_text(self.value, "value"))
        object.__setattr__(self, "id_type", str(self.id_type or "").strip())

    def __str__(self):
        return "{0}/{1}:{2}".format(self.namespace, self.id_type or "*", self.value)

    def to_dict(self):
        return {
            "namespace": self.namespace,
            "value": self.value,
            "id_type": self.id_type,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(data["namespace"], data["value"], data.get("id_type", ""))


@dataclass(frozen=True)
class MergeNotification:
    """An explicit instruction to treat several identifiers as one patient.

    ``identifiers`` is the ordered set of identifiers the notification claims
    belong to the same patient.  ``effective_at`` is when the claim becomes
    true (valid time); ``recorded_at`` is when the registry received it (system
    time) and defaults to ``effective_at``.
    """

    identifiers: tuple
    effective_at: datetime
    recorded_at: datetime = None
    message_id: str = ""
    note: str = ""

    def __post_init__(self):
        if not self.identifiers:
            raise ValueError(
                "merge notification must reference at least one identifier"
            )
        normalized = tuple(
            item if isinstance(item, Identifier) else Identifier(*item)
            for item in self.identifiers
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("merge notification contains duplicate identifiers")
        object.__setattr__(self, "identifiers", normalized)
        effective = _coerce_utc(self.effective_at)
        if effective is None:
            raise ValueError("effective_at is required")
        object.__setattr__(self, "effective_at", effective)
        recorded = _coerce_utc(self.recorded_at) if self.recorded_at else effective
        object.__setattr__(self, "recorded_at", recorded)
        object.__setattr__(self, "message_id", str(self.message_id or ""))
        object.__setattr__(self, "note", str(self.note or ""))

    @property
    def key(self):
        """Stable identifier used to undo the notification later."""
        return self.message_id or "merge@{0}:{1}".format(
            self.effective_at.isoformat(),
            "|".join(str(item) for item in self.identifiers),
        )

    def to_dict(self):
        return {
            "identifiers": [item.to_dict() for item in self.identifiers],
            "effective_at": self.effective_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "message_id": self.message_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            tuple(Identifier.from_dict(item) for item in data["identifiers"]),
            datetime.fromisoformat(data["effective_at"]),
            datetime.fromisoformat(data["recorded_at"])
            if data.get("recorded_at")
            else None,
            data.get("message_id", ""),
            data.get("note", ""),
        )


@dataclass(frozen=True)
class ReversalRecord:
    """Audit entry recording that a merge notification was reversed.

    ``reversed_at`` is system time: the moment the reversal became known.  It
    lets point-in-time queries reproduce the fact that, before the reversal,
    the merge was still visible even though current state treats it as undone.
    """

    notification_key: str
    reversed_at: datetime
    reason: str = ""

    def __post_init__(self):
        object.__setattr__(
            self,
            "notification_key",
            _normalized_text(self.notification_key, "notification_key"),
        )
        object.__setattr__(self, "reversed_at", _coerce_utc(self.reversed_at))
        object.__setattr__(self, "reason", str(self.reason or ""))

    def to_dict(self):
        return {
            "notification_key": self.notification_key,
            "reversed_at": self.reversed_at.isoformat(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["notification_key"],
            datetime.fromisoformat(data["reversed_at"]),
            data.get("reason", ""),
        )


@dataclass(frozen=True)
class EncounterRecord:
    """An encounter (visit) anchored to the patient known at capture time.

    ``anchored_to`` is the identifier the encounter was filed under.  It is
    never rewritten when merges are applied or undone, which keeps historical
    encounters from migrating to another person without evidence.
    """

    encounter_id: str
    anchored_to: Identifier
    at: datetime
    identifiers: tuple = field(default_factory=tuple)

    def __post_init__(self):
        object.__setattr__(
            self, "encounter_id", _normalized_text(self.encounter_id, "encounter_id")
        )
        if not isinstance(self.anchored_to, Identifier):
            object.__setattr__(self, "anchored_to", Identifier(*self.anchored_to))
        object.__setattr__(self, "at", _coerce_utc(self.at))
        normalized = tuple(
            item if isinstance(item, Identifier) else Identifier(*item)
            for item in self.identifiers
        )
        object.__setattr__(self, "identifiers", normalized)

    def to_dict(self):
        return {
            "encounter_id": self.encounter_id,
            "anchored_to": self.anchored_to.to_dict(),
            "at": self.at.isoformat(),
            "identifiers": [item.to_dict() for item in self.identifiers],
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["encounter_id"],
            Identifier.from_dict(data["anchored_to"]),
            datetime.fromisoformat(data["at"]),
            tuple(Identifier.from_dict(item) for item in data.get("identifiers", [])),
        )


class _DisjointSet:
    """Minimal union-find used to materialise a component at one instant."""

    def __init__(self, seeds=()):
        self.parent = {item: item for item in seeds}
        self.rank = {item: 0 for item in seeds}

    def add(self, item):
        if item not in self.parent:
            self.parent[item] = item
            self.rank[item] = 0

    def find(self, item):
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1

    def connected(self, left, right):
        return self.find(left) == self.find(right)

    def members(self, item):
        root = self.find(item)
        return frozenset(
            candidate for candidate in self.parent if self.find(candidate) == root
        )


def identifiers_from_pid(pid_segment):
    """Extract :class:`Identifier` objects from a parsed ``PID`` segment.

    Every repetition of ``PID-3`` is read as an HL7 CX composite: component 1
    is the identifier value, component 4 the assigning authority (institution)
    and component 5 the identifier type.  Repetitions without a value or
    without an assigning authority are skipped, because an identifier is only
    meaningful within its issuing institution.
    """
    result = []
    field = pid_segment(3)
    repetitions = [field] if isinstance(field, str) else list(field)
    for repetition in repetitions:
        if isinstance(repetition, (Repetition, Component)):
            value = str(repetition(1)) if len(repetition) >= 1 else ""
            namespace = str(repetition(4)) if len(repetition) >= 4 else ""
            id_type = str(repetition(5)) if len(repetition) >= 5 else ""
        else:
            value = str(repetition)
            namespace = ""
            id_type = ""
        value = value.strip()
        namespace = namespace.strip()
        if not value or not namespace:
            continue
        result.append(Identifier(namespace, value, id_type.strip()))
    return result


class PatientIdentityRegistry:
    """Append-only identifier relationship table with reversible merges.

    Identifiers are registered independently of any merge.  A merge is the
    only mechanism that links identifiers, recorded as a timestamped
    notification so it can be reversed later without deleting history.

    Relationship queries take two optional time arguments:

    * ``at`` constrains valid time: only notifications effective by ``at``
      count, reproducing the answer the registry would have given for that
      point in the patient's history;
    * ``as_of`` constrains system time: only notifications recorded and not
      yet reversed by ``as_of`` count, reproducing what was actually visible
      then.  Omitting ``as_of`` uses current knowledge (so reversals apply).
    """

    def __init__(self, clock=None):
        self._identifiers = {}
        self._notifications = {}
        self._reversals = {}
        self._encounters = {}
        self._sequence = 0
        self._clock = clock or datetime.utcnow

    def register_identifier(self, identifier):
        """Record that *identifier* exists.  Re-registering is a no-op."""
        if not isinstance(identifier, Identifier):
            identifier = Identifier(*identifier)
        if identifier not in self._identifiers:
            self._identifiers[identifier] = identifier
        return identifier

    def register_identifiers(self, identifiers):
        return [self.register_identifier(item) for item in identifiers]

    def apply_merge(self, notification):
        """Apply *notification*, linking its identifiers from its effective time.

        Unknown identifiers are registered automatically because a merge
        notification is evidence that they exist.  Applying the same
        notification twice raises :class:`DuplicateIdentifierError`.
        """
        if not isinstance(notification, MergeNotification):
            notification = MergeNotification(*notification)
        if notification.key in self._notifications:
            raise DuplicateIdentifierError(
                "merge notification {0!r} already recorded".format(notification.key)
            )
        for identifier in notification.identifiers:
            self.register_identifier(identifier)
        self._sequence += 1
        self._notifications[notification.key] = (self._sequence, notification)
        return notification

    def undo_merge(self, notification_key, undone_at=None, reason=""):
        """Reverse a previously applied merge while retaining the full record.

        Only the targeted notification is deactivated; every other
        notification still applies.  The undo is refused if other active
        notifications independently keep the same identifiers connected, or if
        the notification does not exist or was already reversed.  Encounters
        are never rewritten, so historical records keep their attribution.
        """
        if notification_key not in self._notifications:
            raise MergeNotFoundError(
                "no merge notification with key {0!r}".format(notification_key)
            )
        if notification_key in self._reversals:
            raise MergeNotFoundError(
                "merge notification {0!r} is already reversed".format(notification_key)
            )
        target = self._notifications[notification_key][1]
        remaining = self._graph(excluding={notification_key})
        endpoints = list(target.identifiers)
        for other in endpoints[1:]:
            if remaining.connected(endpoints[0], other):
                raise MergeConflictError(
                    "cannot undo {0!r}: other notifications still connect "
                    "{1} and {2}".format(notification_key, endpoints[0], other)
                )
        reversed_at = _coerce_utc(undone_at) or _coerce_utc(self._clock())
        self._sequence += 1
        reversal = ReversalRecord(notification_key, reversed_at, reason)
        self._reversals[notification_key] = (self._sequence, reversal)
        return target

    def record_encounter(
        self, encounter_id, patient=None, at=None, identifiers=(), recorded_at=None
    ):
        """File an encounter under *patient*, validating against the evidence.

        ``patient`` is the :class:`Identifier` the encounter is attributed to.
        When ``identifiers`` are supplied (for example the identifiers on the
        source message), each must resolve to the same patient component that
        was effective at ``at``; an unknown or divergent identifier raises
        :class:`EncounterAssignmentError` instead of being used to move the
        encounter.  Validation always uses the component visible at encounter
        time, never current state.
        """
        at = _coerce_utc(at)
        if at is None:
            raise ValueError("encounter timestamp is required")
        if patient is None:
            if not identifiers:
                raise EncounterAssignmentError(
                    "encounter {0!r} needs a patient or identifiers".format(
                        encounter_id
                    )
                )
            first = identifiers[0]
            patient = first if isinstance(first, Identifier) else Identifier(*first)
        if not isinstance(patient, Identifier):
            patient = Identifier(*patient)
        if patient not in self._identifiers:
            raise UnknownIdentifierError(
                "unknown patient identifier {0}".format(patient)
            )
        normalized = tuple(
            item if isinstance(item, Identifier) else Identifier(*item)
            for item in identifiers
        )
        graph = self._graph(valid_at=at)
        patient_root = graph.find(patient)
        for identifier in normalized:
            if identifier not in self._identifiers:
                raise EncounterAssignmentError(
                    "encounter {0!r} references unknown identifier {1}".format(
                        encounter_id, identifier
                    )
                )
            if graph.find(identifier) != patient_root:
                raise EncounterAssignmentError(
                    "encounter {0!r}: identifier {1} was not part of "
                    "patient {2} at {3}".format(
                        encounter_id, identifier, patient, at.isoformat()
                    )
                )
        if encounter_id in self._encounters:
            raise DuplicateIdentifierError(
                "encounter {0!r} already recorded".format(encounter_id)
            )
        record = EncounterRecord(encounter_id, patient, at, normalized)
        self._encounters[encounter_id] = (
            _coerce_utc(recorded_at) or at,
            record,
        )
        return record

    def component_of(self, identifier, at=None, as_of=None):
        """Return the representative identifier for *identifier*'s component.

        ``at`` filters by effective time and ``as_of`` by knowledge time, so
        the result reflects only notifications visible at the requested point.
        """
        identifier = self._require_identifier(identifier)
        return self._graph(
            valid_at=_coerce_utc(at), as_of=_coerce_utc(as_of)
        ).find(identifier)

    def patient_identifiers(self, identifier, at=None, as_of=None):
        """Return every identifier linked to *identifier* at the given point."""
        identifier = self._require_identifier(identifier)
        graph = self._graph(valid_at=_coerce_utc(at), as_of=_coerce_utc(as_of))
        return frozenset(graph.members(identifier))

    def identifiers_for(self, patient, at=None, as_of=None):
        return self.patient_identifiers(patient, at, as_of)

    def records_for(self, patient, at=None, as_of=None):
        """Return encounters attributable to *patient*.

        Encounters keep their immutable anchor; they are grouped through the
        component visible at the requested point rather than being rewritten.
        Reversing a merge therefore restores the pre-merge attribution instead
        of dragging encounters across the split.
        """
        patient = self._require_identifier(patient)
        graph = self._graph(valid_at=_coerce_utc(at), as_of=_coerce_utc(as_of))
        root = graph.find(patient)
        result = []
        for _, encounter in sorted(
            self._encounters.values(), key=lambda item: (item[0], item[1].encounter_id)
        ):
            if encounter.anchored_to not in self._identifiers:
                continue
            if graph.find(encounter.anchored_to) != root:
                continue
            result.append(encounter)
        return result

    def is_merged(self, left, right, at=None, as_of=None):
        """Report whether two identifiers denoted the same patient at a point."""
        graph = self._graph(valid_at=_coerce_utc(at), as_of=_coerce_utc(as_of))
        return graph.find(left) == graph.find(right)

    def active_notifications(self, at=None, as_of=None):
        """Return merge notifications visible at the requested point."""
        valid_at = _coerce_utc(at)
        knowledge_at = _coerce_utc(as_of)
        visible = [
            (sequence, stored)
            for key, (sequence, stored) in self._notifications.items()
            if self._is_visible(key, valid_at, knowledge_at)
        ]
        return tuple(stored for _, stored in sorted(visible, key=lambda item: item[0]))

    def merge_history(self):
        """Return every notification and reversal in ingestion (audit) order."""
        entries = [
            (sequence, ("merge", stored))
            for _, (sequence, stored) in self._notifications.items()
        ]
        entries.extend(
            (sequence, ("reversal", reversal))
            for _, (sequence, reversal) in self._reversals.items()
        )
        return tuple(entry for _, entry in sorted(entries, key=lambda item: item[0]))

    def reversals(self):
        """Return all reversal records in ingestion order."""
        return tuple(
            reversal
            for _, reversal in sorted(
                self._reversals.values(), key=lambda item: item[0]
            )
        )

    def _require_identifier(self, identifier):
        if not isinstance(identifier, Identifier):
            identifier = Identifier(*identifier)
        if identifier not in self._identifiers:
            raise UnknownIdentifierError("unknown identifier {0}".format(identifier))
        return identifier

    def _is_visible(self, key, valid_at, as_of):
        valid_cutoff = HORIZON if valid_at is None else valid_at
        knowledge_cutoff = HORIZON if as_of is None else as_of
        _, notification = self._notifications[key]
        if notification.recorded_at > knowledge_cutoff:
            return False
        if notification.effective_at > valid_cutoff:
            return False
        if key in self._reversals:
            if self._reversals[key][1].reversed_at <= knowledge_cutoff:
                return False
        return True

    def _graph(self, valid_at=None, as_of=None, excluding=frozenset()):
        valid_cutoff = HORIZON if valid_at is None else valid_at
        knowledge_cutoff = HORIZON if as_of is None else as_of
        graph = _DisjointSet(self._identifiers.values())
        for key, (_, notification) in self._notifications.items():
            if key in excluding:
                continue
            if notification.recorded_at > knowledge_cutoff:
                continue
            if notification.effective_at > valid_cutoff:
                continue
            if (
                key in self._reversals
                and self._reversals[key][1].reversed_at <= knowledge_cutoff
            ):
                continue
            identifiers = notification.identifiers
            for other in identifiers[1:]:
                graph.union(identifiers[0], other)
        return graph

    def to_dict(self):
        """Serialise the full audit state to JSON-compatible dictionaries."""
        return {
            "identifiers": [item.to_dict() for item in self._identifiers],
            "notifications": [
                stored.to_dict() for _, stored in sorted(self._notifications.values())
            ],
            "reversals": [
                reversal.to_dict() for _, reversal in sorted(self._reversals.values())
            ],
            "encounters": [
                record.to_dict() for _, record in sorted(self._encounters.values())
            ],
            "sequence": self._sequence,
        }

    @classmethod
    def from_dict(cls, data):
        registry = cls()
        for item in data.get("identifiers", []):
            registry.register_identifier(Identifier.from_dict(item))
        for item in data.get("notifications", []):
            notification = MergeNotification.from_dict(item)
            registry._sequence += 1
            registry._notifications[notification.key] = (
                registry._sequence,
                notification,
            )
        for item in data.get("reversals", []):
            reversal = ReversalRecord.from_dict(item)
            registry._sequence += 1
            registry._reversals[reversal.notification_key] = (
                registry._sequence,
                reversal,
            )
        for item in data.get("encounters", []):
            record = EncounterRecord.from_dict(item)
            registry._encounters[record.encounter_id] = (record.at, record)
        registry._sequence = data.get("sequence", registry._sequence)
        return registry
