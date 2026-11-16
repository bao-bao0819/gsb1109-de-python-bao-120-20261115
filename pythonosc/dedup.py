"""Ingestion-side de-duplication for sensor readings.

Sensors resend readings with the same sequence number (retry) and may sample
the same value several times in a row.  This module provides an ingress gate
that decides, per data stream, whether a reading is new and may enter the
warehouse, or has already been seen and must be parked elsewhere.

Design rules
------------

* Sequence numbers alone are not unique after a counter wrap or a device
  restart, so every reading is keyed by its *stream* (device/sensor identity),
  its boot epoch, its wrap cycle and its sequence number.
* Restart evidence (an uptime/boot counter, reset flag, session id...) proves
  that sequence numbers were reset, even when the new sequence is not smaller
  than the previous one.  The first reading of a new boot epoch always passes.
* After a wrap the new reading is accepted: a smaller raw sequence is treated
  as newer instead of being rejected as "older", and the wrap cycle is
  incremented so that reusing the raw sequence number after a wrap cannot be
  mistaken for a retransmission.
* Two readings sharing a sequence number but carrying different payloads are
  kept apart.  Later ones are quarantined, they never overwrite the first and
  "last writer wins" is never used.
* Exact retransmissions (same sequence and identical payload) are pure
  duplicates and are kept out of the warehouse path.  Every duplicate or
  conflict is surfaced through the quarantine callback so the transport layer
  can store it separately.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, NamedTuple, Optional, Tuple


class ReadingStatus(Enum):
    """Outcome of inspecting one reading."""

    NEW = "new"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    RESTART = "restart"
    WRAPPED = "wrapped"


#: Statuses that authorize insertion into the warehouse.
WAREHOUSE_STATUSES = frozenset(
    (ReadingStatus.NEW, ReadingStatus.RESTART, ReadingStatus.WRAPPED)
)


class ReadingRecord(NamedTuple):
    """What happened to one reading handed to the gate."""

    status: ReadingStatus
    stream: Any
    sequence: int
    epoch: int
    wrap: int
    payload: Any
    detail: str


# A quarantine callback receives the record of every reading that must not be
# inserted into the warehouse (duplicate retransmission or same-sequence
# conflict).  It typically writes the reading to a separate store.
QuarantineCallback = Callable[[ReadingRecord], None]


class SequenceDedupError(ValueError):
    """Raised when a reading cannot be interpreted by the de-duplication gate."""


class _StreamState(NamedTuple):
    epoch: int
    # How many times the sequence counter has wrapped inside this epoch.
    wrap: int
    sequence: int
    # Boot evidence attached to the current epoch; ``None`` when the device
    # never provided any.
    epoch_marker: Any


class SequenceDeduplicator:
    """Gate sensor readings before they enter the warehouse.

    The gate is stream-aware: each device/sensor gets an independent sequence
    counter, so sequence numbers from different devices never collide.  The
    instance is thread-safe and can be shared by threaded OSC servers.

    Args:
        wrap_modulus: Value at which the sequence counter wraps back to zero.
            Must be greater than one; common choices are 256 for an 8-bit
            counter or 65536 for a 16-bit counter.
        history_size: Number of recent sequence entries remembered per stream.
            An entry is a distinct ``(epoch, wrap, sequence)`` combination.
            It bounds memory for out-of-order or late retransmissions; it
            should be at least as large as the biggest plausible retry window.
            Once an entry is evicted, a retransmission of it can no longer be
            recognized.
        quarantine_cb: Optional callback invoked for every reading that is not
            admitted to the warehouse (duplicates and same-sequence conflicts).
            Use it to persist the reading in a separate store.
    """

    def __init__(
        self,
        wrap_modulus: int = 65536,
        history_size: int = 1024,
        quarantine_cb: Optional[QuarantineCallback] = None,
    ) -> None:
        if wrap_modulus <= 1:
            raise ValueError("wrap_modulus must be greater than one")
        if history_size < 1:
            raise ValueError("history_size must be at least one")
        self._modulus = wrap_modulus
        self._half_range = wrap_modulus // 2
        self._history_size = history_size
        self._quarantine_cb = quarantine_cb
        self._lock = threading.RLock()
        # stream -> OrderedDict[(epoch, wrap, sequence) -> payloads seen],
        # oldest first
        self._streams: Dict[Any, "OrderedDict[Tuple[int, int, int], List[Any]]"] = {}
        # stream -> state of the latest admitted reading
        self._latest: Dict[Any, _StreamState] = {}
        # Diagnostics counters, one entry per processed reading.
        self.stats: Dict[ReadingStatus, int] = {status: 0 for status in ReadingStatus}
        self._rejected: Deque[ReadingRecord] = deque(maxlen=history_size)

    def _quarantine(self, record: ReadingRecord) -> None:
        self.stats[record.status] += 1
        self._rejected.append(record)
        if self._quarantine_cb is not None:
            self._quarantine_cb(record)

    def _remember(
        self,
        stream: Any,
        epoch: int,
        wrap: int,
        sequence: int,
        payload: Any,
    ) -> None:
        history = self._streams.get(stream)
        if history is None:
            history = OrderedDict()
            self._streams[stream] = history
        key = (epoch, wrap, sequence)
        payloads = history.get(key)
        if payloads is None:
            history[key] = [payload]
        elif payload not in payloads:
            # Exact payload comparison: same sequence, distinct payloads are
            # retained side by side instead of overwriting each other.
            payloads.append(payload)
        while len(history) > self._history_size:
            history.popitem(last=False)

    def admit(
        self,
        stream: Any,
        sequence: int,
        payload: Any,
        restart_evidence: Any = None,
    ) -> ReadingRecord:
        """Inspect one reading and decide whether it may enter the warehouse.

        Args:
            stream: Stable identity of the device/sensor producing readings.
            sequence: Reading sequence number as emitted by the device.
            payload: Full reading payload.  Only identical payloads count as
                duplicates; the same sequence with a different payload is a
                conflict and is quarantined rather than overwriting anything.
            restart_evidence: Optional boot identity (boot counter, uptime,
                reset flag, session id...).  Any change versus the previously
                seen value for the stream proves a device restart and resets
                the sequence baseline.  ``None`` means "no evidence
                available", which never triggers a restart.

        Returns:
            A :class:`ReadingRecord` describing the decision.  Readings whose
            status is :data:`ReadingStatus.NEW`,
            :data:`ReadingStatus.RESTART` or :data:`ReadingStatus.WRAPPED`
            are admitted to the warehouse;
            :data:`ReadingStatus.DUPLICATE` and
            :data:`ReadingStatus.CONFLICT` readings are only handed to the
            quarantine callback and must be stored separately.  The record's
            ``epoch`` and ``wrap`` fields give a collision-free identity when
            combined with ``stream`` and ``sequence``.

        Raises:
            SequenceDedupError: If the sequence number is not an integer or is
                outside the counter range ``0 .. wrap_modulus - 1``.
        """
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise SequenceDedupError(
                f"sequence must be an integer, got {type(sequence).__name__}"
            )
        if not 0 <= sequence < self._modulus:
            raise SequenceDedupError(
                f"sequence {sequence} is outside the counter range "
                f"[0, {self._modulus - 1}]"
            )

        with self._lock:
            previous = self._latest.get(stream)

            if previous is None:
                epoch = 0
                wrap = 0
                marker = restart_evidence
                status = ReadingStatus.NEW
                detail = "first reading for this stream"
            elif (
                restart_evidence is not None
                and restart_evidence != previous.epoch_marker
            ):
                # Restart evidence wins: the counter was reset on the device,
                # so an old-looking sequence number is a genuinely new reading.
                epoch = previous.epoch + 1
                wrap = 0
                marker = restart_evidence
                status = ReadingStatus.RESTART
                detail = (
                    f"restart evidence changed {previous.epoch_marker!r} -> "
                    f"{restart_evidence!r}; sequence baseline reset"
                )
            else:
                epoch = previous.epoch
                marker = previous.epoch_marker

                # Serial-number arithmetic (RFC 1982 style): the forward
                # distance decides ordering regardless of the raw values, so a
                # wrapped counter is read as newer while a backwards jump is
                # rejected.  Repeated identical sampled values are irrelevant
                # because only sequence numbers are compared.
                forward = (sequence - previous.sequence) % self._modulus
                wrapped = (
                    sequence < previous.sequence and 0 < forward <= self._half_range
                )
                wrap = previous.wrap + 1 if wrapped else previous.wrap

                # Look the reading up in the wrap cycle it belongs to.  A
                # reading that starts a new cycle is only matched against that
                # new cycle, so an old raw sequence number can never suppress
                # it.  A backwards-looking reading (a late retransmission
                # emitted before the most recent wrap) is additionally matched
                # against the previous cycle.  Forward-progress readings never
                # consult older cycles.
                history = self._streams.get(stream)
                lookup_wraps: Tuple[int, ...]
                if wrapped:
                    lookup_wraps = (wrap,)
                elif forward > self._half_range and previous.wrap >= 1:
                    lookup_wraps = (wrap, wrap - 1)
                else:
                    lookup_wraps = (wrap,)
                seen_payloads = None
                seen_wrap = wrap
                if history is not None:
                    for candidate_wrap in lookup_wraps:
                        if candidate_wrap < 0:
                            continue
                        payloads = history.get((epoch, candidate_wrap, sequence))
                        if payloads is not None:
                            seen_payloads = payloads
                            seen_wrap = candidate_wrap
                            break
                if seen_payloads is not None:
                    if payload in seen_payloads:
                        record = ReadingRecord(
                            ReadingStatus.DUPLICATE,
                            stream,
                            sequence,
                            epoch,
                            seen_wrap,
                            payload,
                            "retransmission of an already seen reading",
                        )
                        self._quarantine(record)
                        return record
                    record = ReadingRecord(
                        ReadingStatus.CONFLICT,
                        stream,
                        sequence,
                        epoch,
                        seen_wrap,
                        payload,
                        "same sequence number carries a different payload; "
                        "kept apart instead of overwriting",
                    )
                    self._quarantine(record)
                    self._remember(stream, epoch, seen_wrap, sequence, payload)
                    return record

                # An unseen sequence still has to obey ordering: a backwards
                # jump without wrap evidence (or an implausibly large forward
                # jump, which serial arithmetic reads as backwards) is stale.
                if forward == 0 or forward > self._half_range:
                    record = ReadingRecord(
                        ReadingStatus.CONFLICT,
                        stream,
                        sequence,
                        epoch,
                        wrap,
                        payload,
                        f"sequence {sequence} is older than the latest "
                        f"{previous.sequence} without wrap or restart evidence",
                    )
                    self._quarantine(record)
                    self._remember(stream, epoch, wrap, sequence, payload)
                    return record

                if wrapped:
                    status = ReadingStatus.WRAPPED
                    detail = (
                        f"sequence wrapped from {previous.sequence} to "
                        f"{sequence}; new reading accepted"
                    )
                else:
                    status = ReadingStatus.NEW
                    detail = "next reading in sequence order"

            record = ReadingRecord(
                status, stream, sequence, epoch, wrap, payload, detail
            )
            self.stats[status] += 1
            self._remember(stream, epoch, wrap, sequence, payload)
            self._latest[stream] = _StreamState(epoch, wrap, sequence, marker)
            return record

    def is_warehouse_ready(self, record: ReadingRecord) -> bool:
        """Return True for readings that may be inserted into the warehouse."""
        return record.status in WAREHOUSE_STATUSES

    @property
    def rejected(self) -> Tuple[ReadingRecord, ...]:
        """Most recent quarantined readings (oldest first, bounded)."""
        with self._lock:
            return tuple(self._rejected)

    def reset(self) -> None:
        """Forget every stream (mainly useful in tests)."""
        with self._lock:
            self._streams.clear()
            self._latest.clear()
            self._rejected.clear()
            self.stats = {status: 0 for status in ReadingStatus}
