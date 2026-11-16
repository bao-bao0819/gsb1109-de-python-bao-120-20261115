"""Record OSC traffic and replay it from an arbitrary point in time.

The recording keeps two distinct time concepts for every captured message:

* the **sampled time** (``sampled_time``): when the message was originally
  observed on the wire, in wall-clock seconds since the epoch;
* the **scheduled time** (``scheduled_time``): the OSC bundle timetag at which
  the message was originally requested to be dispatched.  Messages captured
  without an explicit timetag (standalone messages or ``IMMEDIATELY`` bundles)
  inherit their sampled time.

Only the sampled time is used to space out a replay; the scheduled time is
retained as metadata and never overwritten, so the original timing information
survives the replay unchanged.

Messages are additionally classified as either :attr:`MessageKind.STATE` or
:attr:`MessageKind.EVENT`.  This drives the rules applied when a replay starts
in the middle of a recording:

* state messages sampled before the start point establish the initial state.
  For every OSC address only the most recently sampled state message is kept
  (last-writer-wins, recording order breaks ties) and it is emitted once before
  the timed replay begins;
* event messages sampled before the start point are never re-emitted.
"""

import enum
import struct
import threading
import time
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Protocol

from pythonosc import osc_bundle
from pythonosc import osc_message
from pythonosc import osc_packet
from pythonosc.parsing import osc_types


class SendClient(Protocol):
    """Anything able to send a parsed OSC message, e.g. an OSC client."""

    def send(self, message: osc_message.OscMessage) -> None: ...


class MessageKind(enum.Enum):
    """Classification of a recorded message."""

    STATE = "state"
    EVENT = "event"


class RecordedMessage(NamedTuple):
    """One captured OSC message.

    Attributes:
        order: Monotonic, gap-free sequence number assigned at capture time.
            It reflects the depth-first order in which messages appeared,
            including messages nested inside bundles, and therefore defines
            the canonical replay ordering.
        kind: Whether the message carries state or a one-shot event.
        sampled_time: Wall-clock seconds since the epoch when the message was
            observed.
        scheduled_time: Original OSC dispatch time (bundle timetag) in
            wall-clock seconds since the epoch; equals ``sampled_time`` when
            the message carried no explicit timetag.
        message: The parsed OSC message.
    """

    order: int
    kind: MessageKind
    sampled_time: float
    scheduled_time: float
    message: osc_message.OscMessage


StatePredicate = Callable[[str], bool]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]

_RECORDING_MAGIC = b"OSCREC1\n"
_KIND_TO_BYTE = {MessageKind.STATE: 1, MessageKind.EVENT: 0}
_BYTE_TO_KIND = {value: key for key, value in _KIND_TO_BYTE.items()}
_HEADER = struct.Struct("<BiddI")


class Recorder:
    """Captures OSC packets as an ordered list of :class:`RecordedMessage`.

    Args:
        state_predicate: Optional callable receiving an OSC address and
            returning ``True`` when messages on that address are state
            messages.  When omitted every message is recorded as an event.
    """

    def __init__(self, state_predicate: Optional[StatePredicate] = None) -> None:
        self._state_predicate = state_predicate
        self._records: List[RecordedMessage] = []

    @property
    def records(self) -> List[RecordedMessage]:
        """Returns the messages captured so far, in recording order."""
        return list(self._records)

    def _classify(self, address: str) -> MessageKind:
        if self._state_predicate is not None and self._state_predicate(address):
            return MessageKind.STATE
        return MessageKind.EVENT

    def record_message(
        self,
        message: osc_message.OscMessage,
        sampled_time: float,
        scheduled_time: Optional[float] = None,
        kind: Optional[MessageKind] = None,
    ) -> RecordedMessage:
        """Records a single, already parsed message.

        Args:
            message: The OSC message to record.
            sampled_time: When the message was observed.
            scheduled_time: Original dispatch time; defaults to the sampled
                time when omitted.
            kind: Explicit classification; defaults to the recorder predicate.
        """
        if scheduled_time is None:
            scheduled_time = sampled_time
        if kind is None:
            kind = self._classify(message.address)
        recorded = RecordedMessage(
            order=len(self._records),
            kind=kind,
            sampled_time=sampled_time,
            scheduled_time=scheduled_time,
            message=message,
        )
        self._records.append(recorded)
        return recorded

    def record_packet(self, data: bytes, received_at: Optional[float] = None) -> None:
        """Captures every message contained in a raw OSC packet.

        Bundles are walked depth-first so that nested bundles keep their
        relative message ordering, which is reflected in the assigned
        sequence numbers.

        Args:
            data: Raw OSC packet bytes (a standalone message or a bundle).
            received_at: Sampled time to stamp the messages with; defaults to
                the current wall-clock time.
        """
        if received_at is None:
            received_at = time.time()
        if osc_bundle.OscBundle.dgram_is_bundle(data):
            self._record_bundle(osc_bundle.OscBundle(data), received_at)
        elif osc_message.OscMessage.dgram_is_message(data):
            self.record_message(osc_message.OscMessage(data), received_at)
        else:
            raise osc_packet.ParseError(
                "OSC Packet should at least contain an OscMessage or an OscBundle."
            )

    def _record_bundle(self, bundle: osc_bundle.OscBundle, received_at: float) -> None:
        scheduled_time = (
            received_at
            if bundle.timestamp == osc_types.IMMEDIATELY
            else bundle.timestamp
        )
        for content in bundle:
            if isinstance(content, osc_bundle.OscBundle):
                self._record_bundle(content, received_at)
            else:
                self.record_message(content, received_at, scheduled_time)

    def save(self, path: str) -> None:
        """Writes the recording to a binary file.

        The sampled time, scheduled time and raw message datagram are all
        persisted so a :class:`Replayer` can be reconstructed later.
        """
        with open(path, "wb") as handle:
            handle.write(_RECORDING_MAGIC)
            for record in self._records:
                dgram = record.message.dgram
                handle.write(
                    _HEADER.pack(
                        _KIND_TO_BYTE[record.kind],
                        record.order,
                        record.sampled_time,
                        record.scheduled_time,
                        len(dgram),
                    )
                )
                handle.write(dgram)

    @staticmethod
    def load(path: str) -> List[RecordedMessage]:
        """Loads a recording previously written with :meth:`save`."""
        records: List[RecordedMessage] = []
        with open(path, "rb") as handle:
            magic = handle.read(len(_RECORDING_MAGIC))
            if magic != _RECORDING_MAGIC:
                raise ValueError(f"Not a python-osc recording: {path}")
            while True:
                header = handle.read(_HEADER.size)
                if not header:
                    break
                if len(header) != _HEADER.size:
                    raise ValueError("Truncated recording header")
                kind_byte, order, sampled_time, scheduled_time, size = _HEADER.unpack(
                    header
                )
                dgram = handle.read(size)
                if len(dgram) != size:
                    raise ValueError("Truncated recording entry")
                if kind_byte not in _BYTE_TO_KIND:
                    raise ValueError(f"Unknown message kind byte: {kind_byte}")
                records.append(
                    RecordedMessage(
                        order=order,
                        kind=_BYTE_TO_KIND[kind_byte],
                        sampled_time=sampled_time,
                        scheduled_time=scheduled_time,
                        message=osc_message.OscMessage(dgram),
                    )
                )
        records.sort(key=lambda record: record.order)
        return records


class ReplayError(Exception):
    """Raised when a replay is configured or driven incorrectly."""


class Replayer:
    """Replays recorded messages from an arbitrary sampled-time position.

    Replay spacing is derived exclusively from the recorded **sampled times**
    and scaled by ``rate``; the original scheduled times are left untouched.
    State messages sampled before ``start_time`` are collapsed into a single
    initial-state snapshot (last-writer-wins per address), while event messages
    sampled before the start are dropped.

    The replay can be paused and resumed, including from another thread.
    Messages passed to :meth:`ack` are treated as confirmed: they are skipped
    if still queued and are never sent again, even across a pause/resume
    cycle.  Pausing preserves the current position, so resuming continues with
    the next undelivered message and never re-sends an already dispatched one.

    Args:
        records: A :class:`Recorder` or an iterable of recorded messages.
        client: Object exposing a ``send(message)`` method, e.g. a
            :class:`pythonosc.udp_client.UDPClient`.
        start_time: Sampled time to start replay at, in the same time base as
            the recording.  ``None`` replays from the first recorded message.
        rate: Time scaling factor; ``2.0`` runs twice as fast as recorded.
        clock: Monotonic clock used for scheduling (injectable for tests).
        sleep: One-argument callable blocking for the given delay.  Defaults
            to an interruptible wait so :meth:`pause` takes effect immediately
            (injectable for tests).
    """

    def __init__(
        self,
        records: Iterable[RecordedMessage] | Recorder,
        client: SendClient,
        start_time: Optional[float] = None,
        rate: float = 1.0,
        clock: Clock = time.monotonic,
        sleep: Optional[Sleeper] = None,
    ) -> None:
        if rate <= 0:
            raise ReplayError("Replay rate must be strictly positive")
        self._records = (
            records.records if isinstance(records, Recorder) else list(records)
        )
        self._client = client
        self._rate = rate
        self._clock = clock
        self._confirmed: set[int] = set()
        self._paused = threading.Event()
        # An Event wait is interruptible by pause()/stop(), unlike time.sleep.
        self._sleep: Sleeper = sleep if sleep is not None else self._interruptible_wait
        self._started = False
        self._stopped = False
        self._anchor_wall = 0.0
        if self._records:
            first_sampled = self._records[0].sampled_time
        else:
            first_sampled = 0.0
        self._anchor_sampled = start_time if start_time is not None else first_sampled
        self._start_time = start_time
        self._initial_state = self._build_initial_state()
        self._queue: List[RecordedMessage] = []
        self._index = 0

    def _build_initial_state(self) -> List[RecordedMessage]:
        """Returns the pre-start state snapshot in recording order.

        Rule: among state messages with a sampled time at or before the start
        point, keep only the latest one per OSC address.  Equal sampled times
        are resolved by recording order (the later sequence wins).
        """
        if self._start_time is None:
            return []
        latest: Dict[str, RecordedMessage] = {}
        for record in self._records:
            if (
                record.kind is MessageKind.STATE
                and record.sampled_time <= self._start_time
            ):
                previous = latest.get(record.message.address)
                if previous is None or record.order > previous.order:
                    latest[record.message.address] = record
        return sorted(latest.values(), key=lambda item: item.order)

    @property
    def initial_state(self) -> List[RecordedMessage]:
        """Messages emitted once to establish the state at the start point."""
        return list(self._initial_state)

    def _build_queue(self) -> List[RecordedMessage]:
        """Builds the full ordered dispatch queue.

        The initial-state snapshot comes first (due immediately), followed by
        every non-snapshot message with a sampled time at or after the start
        point, ordered by sampled time then recording order.  Events sampled
        before the start point are excluded here and therefore never
        back-filled.
        """
        snapshot_orders = {record.order for record in self._initial_state}
        timed = [
            record
            for record in self._records
            if record.order not in snapshot_orders
            and record.sampled_time >= self._anchor_sampled
        ]
        timed.sort(key=lambda record: (record.sampled_time, record.order))
        return [*self._initial_state, *timed]

    def run(self) -> None:
        """Runs the replay to completion, blocking the calling thread."""
        if self._stopped:
            raise ReplayError("Replay has been stopped")
        if self._started:
            raise ReplayError("Replay is already running")
        self._started = True
        self._anchor_wall = self._clock()
        self._queue = self._build_queue()
        self._index = 0
        self._consume()

    def _interruptible_wait(self, delay: float) -> None:
        """Waits for ``delay`` seconds but returns early when paused/stopped."""
        self._paused.wait(delay)

    def _consume(self) -> None:
        snapshot_orders = {record.order for record in self._initial_state}
        while self._index < len(self._queue):
            if self._paused.is_set() or self._stopped:
                return
            record = self._queue[self._index]
            if record.order in self._confirmed:
                self._index += 1
                continue
            if record.order in snapshot_orders:
                delay = 0.0
            else:
                target = (
                    self._anchor_wall
                    + (record.sampled_time - self._anchor_sampled) / self._rate
                )
                delay = target - self._clock()
            if delay > 0:
                self._sleep(delay)
            if self._paused.is_set() or self._stopped:
                return
            self._index += 1
            self._dispatch(record)

    def _dispatch(self, record: RecordedMessage) -> None:
        self._client.send(record.message)

    def pause(self) -> None:
        """Suspends the replay without losing the current position."""
        self._paused.set()

    def resume(self, rate: Optional[float] = None) -> None:
        """Resumes a paused replay, optionally with a new rate.

        The next unconfirmed message is re-anchored to the current clock so
        that the pause itself introduces no delay, and a rate change applies
        to everything still queued.  Confirmed messages are skipped and never
        re-sent.
        """
        if self._stopped:
            raise ReplayError("Replay has been stopped")
        if not self._paused.is_set():
            raise ReplayError("Replay is not paused")
        if rate is not None:
            if rate <= 0:
                raise ReplayError("Replay rate must be strictly positive")
            self._rate = rate
        self._paused.clear()
        if not self._started:
            # Allow pause() before run(): start cleanly when resumed.
            self.run()
            return
        snapshot_orders = {record.order for record in self._initial_state}
        while self._index < len(self._queue):
            record = self._queue[self._index]
            if record.order not in self._confirmed:
                break
            self._index += 1
        if self._index >= len(self._queue):
            return
        next_record = self._queue[self._index]
        self._anchor_wall = self._clock()
        if next_record.order not in snapshot_orders:
            self._anchor_sampled = next_record.sampled_time
        self._consume()

    def stop(self) -> None:
        """Permanently ends the replay; further resumes are rejected."""
        self._stopped = True
        self._paused.set()

    def ack(self, order: int) -> None:
        """Marks a message as confirmed so it is never sent again."""
        self._confirmed.add(order)

    @property
    def confirmed_orders(self) -> set[int]:
        """Sequence numbers confirmed via :meth:`ack`."""
        return set(self._confirmed)
