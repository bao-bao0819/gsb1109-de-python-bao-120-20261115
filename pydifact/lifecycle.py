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

"""Reconciliation of business messages across retransmissions, corrections and
cancellations.

Business documents (ORDERS, INVOIC, ...) are not always sent exactly once. A
message may be retransmitted under a new interchange reference while carrying
the same business identity, it may be corrected in place under the same document
number, and it may be cancelled. This module links all transmissions that refer
to the same *business key* into one :class:`LifecycleRecord` chain.

The conventions follow UN/EDIFACT:

* the business key is the document identifier (data element 1004, composite
  C106) of the beginning-of-message segment (``BGM``);
* the message function is code 1225 of the same ``BGM`` segment, interpreted
  with :data:`MESSAGE_FUNCTION_CODES`;
* optional version (1056) / revision (1060) identifiers inside C106 order
  successive corrections.

Design rules implemented here, per the requirements:

* a retransmission that arrives under a new interchange reference is linked to
  the chain by its business key, while an exact re-delivery (same business key
  *and* same interchange reference) is parked as a duplicate, never applied
  twice;
* a cancellation leaves a tombstone, so a late original can never resurrect a
  cancelled business document;
* two corrections that cannot be ordered (same or missing revision) are both
  retained in a conflict set instead of one being silently dropped.
"""

import warnings
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pydifact.exceptions import CorrectionConflictWarning, MessageLifecycleError

# UN/EDIFACT code list 1225 "Message function code". Only the codes needed to
# drive a document lifecycle are modelled; unknown codes fall back to ORIGINAL.
MESSAGE_FUNCTION_CODES: dict[str, str] = {
    "1": "cancel",  # Cancellation
    "3": "original",  # Original transmission (declared)
    "4": "replace",  # Replace
    "5": "replace",  # Replace (widely used as "correction")
    "7": "duplicate",  # Duplicate
    "9": "original",  # Original (the default code used by most senders)
    "22": "original",  # Add
    "23": "cancel",  # Remove
    "24": "replace",  # Modify/update
    "25": "replace",  # Replace
    "26": "original",  # Confirm
    "27": "cancel",  # Reject
    "29": "original",  # Accepted without amendment
    "31": "duplicate",  # Copy / duplicate
    "42": "original",  # Confirmed via third party
    "43": "original",  # Additional transmission
    "45": "replace",  # Revised
    "46": "cancel",  # Reversal of a debit/credit
}

# Canonical lifecycle phases an incoming message is normalised to.
ORIGINAL = "original"
CORRECTION = "correction"
CANCELLATION = "cancellation"
RETRANSMISSION = "retransmission"
DUPLICATE = "duplicate"

#: Mapping of the coarse 1225 semantics onto the lifecycle phases above.
_PHASE_BY_FUNCTION: dict[str, str] = {
    "original": ORIGINAL,
    "replace": CORRECTION,
    "cancel": CANCELLATION,
    "duplicate": RETRANSMISSION,
}

#: The ``BGM`` tag used by the default business-key extractor.
BGM_TAG = "BGM"


@dataclass(frozen=True)
class BusinessKey:
    """The identity of a business document, independent of its transmission.

    The document identifier alone is only unique within one sender and message
    type, so all three components form the key. Two transmissions with the same
    ``BusinessKey`` describe the same business document, even if they travel in
    different interchanges.
    """

    document_id: str
    message_type: str = ""
    sender: str = ""

    def __str__(self) -> str:
        return "/".join(
            part for part in (self.message_type, self.sender, self.document_id) if part
        )


@dataclass(frozen=True)
class MessageFunction:
    """The lifecycle-relevant interpretation of a ``BGM`` segment."""

    code: str | None
    semantic: str | None
    phase: str
    document_id: str
    version: str | None = None
    revision: str | None = None


@dataclass
class MessageEvent:
    """One observed transmission of a business document."""

    message: Any
    function: MessageFunction
    transmission_ref: str | None
    sequence: int
    phase: str

    @property
    def revision_key(self) -> tuple[Any, ...]:
        """Ordering key built from the C106 version/revision identifiers."""
        return _revision_order(self.function.version), _revision_order(
            self.function.revision
        )


@dataclass
class LifecycleRecord:
    """The complete chain of transmissions for one :class:`BusinessKey`.

    Attributes:
        key: The business key this chain is keyed on.
        events: Every transmission belonging to the chain, in arrival order.
        current: The event that currently represents the document, or `None`
            while only a cancellation has been seen (tombstone state).
        cancelled: `True` once a cancellation has been observed. Stays `True`
            forever; a late original cannot clear it.
        conflicts: Corrections that could not be ordered against each other,
            retained together so the ambiguity is visible rather than hidden.
    """

    key: BusinessKey
    events: list[MessageEvent] = field(default_factory=list)
    current: MessageEvent | None = None
    cancelled: bool = False
    conflicts: list[MessageEvent] = field(default_factory=list)
    _transmission_refs: set[str] = field(default_factory=set)
    _applied_correction: MessageEvent | None = None

    @property
    def original(self) -> MessageEvent | None:
        """The first original transmission, if one has been observed."""
        return next((event for event in self.events if event.phase == ORIGINAL), None)

    @property
    def corrections(self) -> list[MessageEvent]:
        """All correction events, in arrival order."""
        return [event for event in self.events if event.phase == CORRECTION]

    @property
    def is_conflicted(self) -> bool:
        """Whether unorderable corrections are currently held."""
        return bool(self.conflicts)

    def has_transmission(self, transmission_ref: str | None) -> bool:
        """Whether a transmission with this reference was already ingested."""
        return (
            transmission_ref is not None and transmission_ref in self._transmission_refs
        )


def _revision_order(value: str | None) -> tuple[Any, ...]:
    """Turn a dotted version/revision string into a comparable ordering key.

    Numeric components compare numerically (``"2" < "10"``); non-numeric ones
    fall back to lexicographic ordering. An absent value sorts before any
    present one.
    """
    if not value:
        return ()
    ordered: list[tuple[int, Any]] = []
    for part in value.split("."):
        try:
            ordered.append((0, int(part)))
        except ValueError:
            ordered.append((1, part))
    return tuple(ordered)


def _element_text(element: Any) -> str | None:
    """Return a non-empty scalar from a simple or composite element."""
    if element is None:
        return None
    if isinstance(element, list):
        for component in element:
            if component not in (None, ""):
                return str(component)
        return None
    text = str(element)
    return text if text else None


def default_key_extractor(message: Any) -> MessageFunction:
    """Extract business key and function from a message's ``BGM`` segment.

    Handles both layouts found in the wild: a scalar C106 (``BGM+220+PO1+9``)
    and a composite C106 with version/revision (``BGM+220::9+PO1:V:R+9``).

    Raises:
        MessageLifecycleError: if the message has no ``BGM`` segment or no
            document identifier (1004) to use as business key.
    """
    bgm = message.get_segment(BGM_TAG)
    if bgm is None:
        raise MessageLifecycleError(
            f"Message has no {BGM_TAG} segment, cannot determine its business key "
            f"and function code."
        )

    identification = bgm.elements[1] if len(bgm.elements) > 1 else None
    document_id = None
    version = None
    revision = None
    if isinstance(identification, list):
        # C106: 1004 document identifier, 1056 version, 1060 revision
        document_id = _element_text(identification[0]) if identification else None
        version = _element_text(identification[1]) if len(identification) > 1 else None
        revision = _element_text(identification[2]) if len(identification) > 2 else None
    else:
        document_id = _element_text(identification)

    if not document_id:
        raise MessageLifecycleError(
            f"{BGM_TAG} segment carries no document identifier (C106/1004), cannot "
            f"build a business key: {bgm.elements}"
        )

    code = _element_text(bgm.elements[2]) if len(bgm.elements) > 2 else None
    semantic = MESSAGE_FUNCTION_CODES.get(code) if code is not None else None
    phase = _PHASE_BY_FUNCTION.get(semantic or "", ORIGINAL)
    return MessageFunction(
        code=code,
        semantic=semantic,
        phase=phase,
        document_id=document_id,
        version=version,
        revision=revision,
    )


KeyExtractor = Callable[[Any], MessageFunction]


class LifecycleRegistry:
    """Accumulates business messages and reconciles their lifecycle chains.

    Feed every incoming message (a `Message`, `RawSegmentCollection` or any
    object exposing ``get_segment``) into :meth:`register`. The registry groups
    them by :class:`BusinessKey` and keeps one :class:`LifecycleRecord` per
    business document.

    Args:
        key_extractor: Callable returning a :class:`MessageFunction` for a
            message. Defaults to :func:`default_key_extractor` reading ``BGM``.
        strict: When `True`, unresolvable correction conflicts raise
            :class:`MessageLifecycleError` instead of warning. Either way the
            conflicting corrections are retained on the record.
    """

    def __init__(
        self,
        key_extractor: KeyExtractor = default_key_extractor,
        strict: bool = False,
    ) -> None:
        self._key_extractor = key_extractor
        self.strict = strict
        self._records: dict[BusinessKey, LifecycleRecord] = {}
        self._duplicates: list[MessageEvent] = []
        self._sequence = 0

    # -- construction helpers -------------------------------------------------

    @classmethod
    def from_messages(
        cls,
        messages: Iterable[Any],
        transmission_ref: str | None = None,
        key_extractor: KeyExtractor = default_key_extractor,
        strict: bool = False,
    ) -> "LifecycleRegistry":
        """Build a registry from an iterable of messages."""
        registry = cls(key_extractor=key_extractor, strict=strict)
        for message in messages:
            registry.register(message, transmission_ref=transmission_ref)
        return registry

    @classmethod
    def from_interchange(
        cls,
        interchange: Any,
        key_extractor: KeyExtractor = default_key_extractor,
        strict: bool = False,
    ) -> "LifecycleRegistry":
        """Build a registry from all messages of an :class:`Interchange`.

        The interchange control reference (UNB element 5) is used as the
        transmission reference, which is what makes retransmission detection
        distinguish a re-sent interchange from an in-place correction.
        """
        registry = cls(key_extractor=key_extractor, strict=strict)
        transmission_ref = _element_text(
            getattr(interchange, "control_reference", None)
        )
        sender = _element_text(getattr(interchange, "sender", None)) or ""
        for message in interchange.get_messages():
            registry.register(message, transmission_ref=transmission_ref, sender=sender)
        return registry

    # -- access ---------------------------------------------------------------

    def get(self, key: BusinessKey) -> LifecycleRecord | None:
        """Return the chain for a business key, if known."""
        return self._records.get(key)

    def records(self) -> Iterator[LifecycleRecord]:
        """Iterate over all known lifecycle chains."""
        return iter(self._records.values())

    @property
    def duplicates(self) -> list[MessageEvent]:
        """Transmissions parked because they were already delivered."""
        return list(self._duplicates)

    def active_documents(self) -> list[LifecycleRecord]:
        """Chains that currently represent a live (non-cancelled) document."""
        return [
            record
            for record in self._records.values()
            if record.current is not None and not record.cancelled
        ]

    # -- core reconciliation --------------------------------------------------

    def _business_key(
        self, message: Any, function: MessageFunction, sender: str
    ) -> BusinessKey:
        message_type = getattr(message, "type", "") or ""
        return BusinessKey(
            document_id=function.document_id,
            message_type=message_type,
            sender=sender,
        )

    def register(
        self,
        message: Any,
        transmission_ref: str | None = None,
        sender: str | None = None,
    ) -> LifecycleRecord:
        """Ingest one message and reconcile it into its lifecycle chain.

        Args:
            message: The incoming business message.
            transmission_ref: Reference of the transporting interchange (UNB
                control reference). Same business key plus same reference means
                a re-delivery of an already-seen transmission.
            sender: Optional sender identifier for the business key. When not
                given, it is taken from the message itself if available.

        Returns:
            The updated :class:`LifecycleRecord` for the message's business key.
        """
        function = self._key_extractor(message)
        sender_text = sender or _element_text(getattr(message, "sender", None)) or ""
        key = self._business_key(message, function, sender_text)
        record = self._records.get(key)

        # First time we ever see this business document.
        if record is None:
            record = LifecycleRecord(key=key)
            self._records[key] = record
            return self._apply_first(record, message, function, transmission_ref)

        # The chain already exists: decide whether this is a re-delivery of the
        # same transmission or a genuinely new lifecycle step.
        event = self._make_event(message, function, transmission_ref)
        if record.has_transmission(transmission_ref):
            event.phase = DUPLICATE
            self._duplicates.append(event)
            record.events.append(event)
            return record

        return self._apply_next(record, event)

    def _make_event(
        self,
        message: Any,
        function: MessageFunction,
        transmission_ref: str | None,
    ) -> MessageEvent:
        self._sequence += 1
        return MessageEvent(
            message=message,
            function=function,
            transmission_ref=transmission_ref,
            sequence=self._sequence,
            phase=function.phase,
        )

    def _apply_first(
        self,
        record: LifecycleRecord,
        message: Any,
        function: MessageFunction,
        transmission_ref: str | None,
    ) -> LifecycleRecord:
        event = self._make_event(message, function, transmission_ref)
        record.events.append(event)
        if transmission_ref is not None:
            record._transmission_refs.add(transmission_ref)

        if event.phase == CANCELLATION:
            # Cancellation before any original: plant the tombstone. A later
            # original must not bring the document back to life.
            record.cancelled = True
            record.current = None
        elif event.phase == CORRECTION:
            # A correction arriving before its original is accepted as the
            # current state; no conflict exists until a second correction shows up.
            record.current = event
            record._applied_correction = event
        else:
            record.current = event
        return record

    def _apply_next(
        self, record: LifecycleRecord, event: MessageEvent
    ) -> LifecycleRecord:
        if event.transmission_ref is not None:
            record._transmission_refs.add(event.transmission_ref)
        record.events.append(event)

        if event.phase == DUPLICATE or event.phase == RETRANSMISSION:
            # A copy/retransmission under a new interchange reference carries no
            # new business state: keep it linked in the chain, leave current as is.
            return record

        if event.phase == CANCELLATION:
            record.cancelled = True
            record.current = None
            record.conflicts.clear()
            record._applied_correction = None
            return record

        if event.phase == ORIGINAL:
            if record.cancelled:
                # Tombstone holds: the late original is recorded but ignored,
                # it must not resurrect the cancelled document.
                return record
            if record.original is not None:
                # An original re-sent under a new interchange reference: link it
                # into the chain as a retransmission without changing state.
                event.phase = RETRANSMISSION
                return record
            if record.current is None:
                record.current = event
            # An original arriving after a correction does not roll it back.
            return record

        # Correction of an existing business document.
        if record.cancelled:
            # Corrections cannot revive a cancelled document either.
            return record

        return self._apply_correction(record, event)

    def _apply_correction(
        self, record: LifecycleRecord, event: MessageEvent
    ) -> LifecycleRecord:
        previous = record._applied_correction

        # First correction of the document: nothing to compare against, so it
        # simply becomes the current state. A conflict needs two corrections.
        if previous is None:
            record._applied_correction = event
            record.current = event
            return record

        incoming_key = event.revision_key
        previous_key = previous.revision_key

        # A correction without version/revision cannot be ordered, and neither
        # can two corrections carrying identical revision information.
        if incoming_key == ((), ()) or incoming_key == previous_key:
            self._register_conflict(record, previous, event)
            return record

        # Both carry distinct revision information: the higher one supersedes.
        if incoming_key > previous_key:
            record._applied_correction = event
            record.current = event
            record.conflicts.clear()
            return record

        # Stale correction, superseded by a newer one already applied.
        return record

    def _register_conflict(
        self,
        record: LifecycleRecord,
        previous: MessageEvent,
        event: MessageEvent,
    ) -> None:
        if previous not in record.conflicts:
            record.conflicts.append(previous)
        if event not in record.conflicts:
            record.conflicts.append(event)
        message = (
            f"Unorderable corrections for business document {record.key}: "
            f"revisions {[e.function.revision for e in record.conflicts]} cannot be "
            f"sequenced. Both are retained."
        )
        if self.strict:
            raise MessageLifecycleError(message)
        warnings.warn(message, category=CorrectionConflictWarning, stacklevel=3)
