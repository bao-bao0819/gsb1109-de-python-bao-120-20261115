"""Reconcile outbound HL7 messages with their acknowledgments.

Control IDs (``MSH-10``) are only unique within a sending system.  Two
systems can therefore reuse the same control ID.  A receipt is attributed to
a delivery only when its control ID and direction evidence identify a single
message.  Otherwise it is reported as a candidate and is not allowed to set a
delivery status.

Receipts are kept in two independent layers:

``receive``
    The receiving interface accepted or rejected the message.

``application``
    The receiving application accepted or rejected the message content.

A rejection in either layer is sticky: a later or earlier acceptance never
turns it into a success.  In particular, an application reject remains the
final status even when a receive accept arrived first.

Example::

    table = build_delivery_table(messages, receipts)
    print(format_delivery_table(table))

``messages`` and ``receipts`` accept parsed :class:`hl7.Message` objects,
raw HL7 strings, or mappings.  A mapping for a message uses ``sender``,
``receiver``, ``control_id`` and optional ``sent_at``, ``message_type``,
``trigger_event`` and ``raw``.  A mapping for a receipt uses ``control_id``,
``code`` and optional ``layer``, ``received_at``, ``source``,
``sending_application``, ``sending_facility``, ``receiving_application``,
``receiving_facility``, ``message_type``, ``trigger_event``, ``text`` and
``raw``.
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from .containers import Batch, File
from .datatypes import parse_datetime
from .exceptions import HL7Exception
from .parser import parse_hl7

__all__ = [
    "CandidateReceipt",
    "DeliveryAttempt",
    "DeliveryRow",
    "DeliveryTable",
    "Receipt",
    "build_delivery_table",
    "format_delivery_table",
]

ACCEPT_CODES = frozenset({"AA", "CA"})
REJECT_CODES = frozenset({"AR", "CR"})
ERROR_CODES = frozenset({"AE", "CE"})
LAYERS = ("receive", "application")

_RECEIVE_WORDS = (
    "receive",
    "received",
    "transport",
    "interface",
    "mllp",
    "commit",
)
_APPLICATION_WORDS = (
    "application",
    "app ",
    "content",
    "business",
    "processing",
)

# Enhanced-mode commit codes belong to the receive layer; original-mode
# application codes belong to the application layer.  An explicit layer or an
# explicit MSA-3 description always wins over this inference.
_RECEIVE_CODES = frozenset({"CA", "CR", "CE"})
_APPLICATION_CODES = frozenset({"AA", "AR", "AE"})


@dataclass(frozen=True)
class DeliveryAttempt:
    """One outbound transmission of a message."""

    sender: str
    receiver: str
    control_id: str
    sent_at: str = None
    message_type: str = None
    trigger_event: str = None
    sender_facility: str = None
    receiver_facility: str = None
    raw: str = None
    index: int = 0

    @property
    def key(self):
        return (self.sender, self.receiver, self.control_id)


@dataclass
class Receipt:
    """One acknowledgment receipt.

    ``control_id`` is the *acknowledged* message's control ID (``MSA-2``),
    not the ACK's own ``MSH-10``.
    """

    control_id: str
    code: str
    layer: str = None
    received_at: str = None
    source: str = None
    sending_application: str = None
    sending_facility: str = None
    receiving_application: str = None
    receiving_facility: str = None
    message_type: str = None
    trigger_event: str = None
    structure: str = None
    text: str = None
    raw: str = None
    index: int = 0
    attempt_index: int = None
    late: bool = False

    @property
    def accepted(self):
        return self.code in ACCEPT_CODES

    @property
    def rejected(self):
        return self.code in REJECT_CODES

    @property
    def failed(self):
        return self.code in REJECT_CODES or self.code in ERROR_CODES

    @property
    def status(self):
        if self.code in ACCEPT_CODES:
            return "accepted"
        if self.code in REJECT_CODES:
            return "rejected"
        if self.code in ERROR_CODES:
            return "error"
        return "unknown"

    @property
    def timestamp(self):
        return _to_datetime(self.received_at)

    @property
    def sort_key(self):
        return (_time_key(self.timestamp), self.index)


@dataclass
class CandidateReceipt:
    """A receipt that cannot be attributed to exactly one delivery."""

    receipt: Receipt
    possible_matches: list = field(default_factory=list)
    reason: str = None

    def as_row(self):
        return {
            "control_id": self.receipt.control_id,
            "code": self.receipt.code,
            "layer": self.receipt.layer or "",
            "received_at": self.receipt.received_at or "",
            "source": self.receipt.source or "",
            "possible_matches": list(self.possible_matches),
            "reason": self.reason or "",
        }


@dataclass
class DeliveryRow:
    """A logical delivery: all retransmissions plus all valid receipts."""

    sender: str
    receiver: str
    control_id: str
    attempts: list = field(default_factory=list)
    receipts: list = field(default_factory=list)

    @property
    def key(self):
        return (self.sender, self.receiver, self.control_id)

    @property
    def attempt_count(self):
        return len(self.attempts)

    @property
    def sent_at(self):
        return self.attempts[0].sent_at if self.attempts else None

    @property
    def retransmissions(self):
        return self.attempts[1:]

    def receipts_for(self, layer):
        return [receipt for receipt in self.receipts if receipt.layer == layer]

    def status(self, layer):
        """Aggregate one layer.  Failure wins over acceptance."""
        receipts = self.receipts_for(layer)
        if not receipts:
            return "pending"
        if any(receipt.rejected for receipt in receipts):
            return "rejected"
        if any(receipt.code in ERROR_CODES for receipt in receipts):
            return "error"
        if any(receipt.accepted for receipt in receipts):
            return "accepted"
        return "unknown"

    def deciding_receipt(self, layer):
        """The receipt that explains the aggregate layer status.

        A sticky rejection is represented by the latest rejection, not by a
        later acceptance that did not change the status.
        """
        receipts = self.receipts_for(layer)
        if not receipts:
            return None
        for predicate in (
            lambda item: item.rejected,
            lambda item: item.code in ERROR_CODES,
            lambda item: item.accepted,
        ):
            matching = [item for item in receipts if predicate(item)]
            if matching:
                return max(matching, key=lambda item: item.sort_key)
        return max(receipts, key=lambda item: item.sort_key)

    def code(self, layer):
        receipt = self.deciding_receipt(layer)
        return receipt.code if receipt else ""

    def received_at(self, layer):
        receipt = self.deciding_receipt(layer)
        return receipt.received_at if receipt else ""

    @property
    def final_status(self):
        """Overall delivery status.

        Application failures take precedence over receive success.  The
        ordering is intentional and does not depend on receipt arrival time.
        """
        if self.status("application") == "rejected":
            return "application_rejected"
        if self.status("application") == "error":
            return "application_error"
        if self.status("receive") == "rejected":
            return "rejected"
        if self.status("receive") == "error":
            return "receive_error"
        if self.status("application") == "accepted":
            return "delivered"
        if self.status("receive") == "accepted":
            return "received"
        if "unknown" in (self.status("receive"), self.status("application")):
            return "unknown"
        return "pending"

    @property
    def attempt_times(self):
        return [attempt.sent_at or "" for attempt in self.attempts]

    @property
    def late_receipts(self):
        return [receipt for receipt in self.receipts if receipt.late]

    @property
    def needs_attention(self):
        return self.final_status in {
            "application_rejected",
            "application_error",
            "rejected",
            "receive_error",
            "unknown",
            "pending",
        }

    def as_row(self):
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "control_id": self.control_id,
            "attempts": self.attempt_count,
            "attempt_times": self.attempt_times,
            "sent_at": self.sent_at or "",
            "receive_code": self.code("receive"),
            "receive_status": self.status("receive"),
            "receive_at": self.received_at("receive"),
            "application_code": self.code("application"),
            "application_status": self.status("application"),
            "application_at": self.received_at("application"),
            "final_status": self.final_status,
            "late_receipts": list(self.late_receipts),
            "receipts": list(self.receipts),
        }


@dataclass
class DeliveryTable:
    rows: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def as_rows(self):
        return [row.as_row() for row in self.rows]

    def as_candidates(self):
        return [candidate.as_row() for candidate in self.candidates]

    def to_dicts(self):
        return {"rows": self.as_rows(), "candidates": self.as_candidates()}


def _time_key(value):
    if value is None:
        return (0, datetime.datetime.min)
    return (1, value)


def _to_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return parse_datetime(str(value))
    except (TypeError, ValueError):
        return None


def _field(message, segment, field_num):
    try:
        return str(message.extract_field(segment, 1, field_num)).strip()
    except (KeyError, IndexError):
        return ""


def _first_field(message, segment, field_num):
    try:
        return str(message.segment(segment)(field_num)).strip()
    except (KeyError, IndexError):
        return ""


def _message_type(message):
    values = _first_field(message, "MSH", 9).split("^")
    while len(values) < 3:
        values.append("")
    return values[0], values[1], values[2]


def _is_ack(message):
    message_type, _, structure = _message_type(message)
    return message_type.upper() == "ACK" or structure.upper() == "ACK"


def _iter_messages(value):
    """Yield parsed messages from a message, batch, file or raw string."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        if "\rMSH" in value or value.lstrip().startswith(("BHS", "FHS")):
            try:
                parsed = parse_hl7(value)
            except (ValueError, HL7Exception):
                return
            for message in _iter_messages(parsed):
                yield message
            return
        try:
            value = parse_hl7(value)
        except (ValueError, HL7Exception):
            return
    if hasattr(value, "segment") and hasattr(value, "segments"):
        yield value
        return
    if isinstance(value, (list, tuple, Batch, File)):
        for child in value:
            for message in _iter_messages(child):
                yield message
        return
    yield value


def _attempt_from_message(message, index):
    if isinstance(message, dict):
        sender = message.get("sender", "")
        receiver = message.get("receiver", "")
        control_id = message.get("control_id", message.get("message_control_id", ""))
        return DeliveryAttempt(
            sender=str(sender or "").strip(),
            receiver=str(receiver or "").strip(),
            control_id=str(control_id or "").strip(),
            sent_at=message.get("sent_at"),
            message_type=message.get("message_type"),
            trigger_event=message.get("trigger_event"),
            sender_facility=message.get("sender_facility"),
            receiver_facility=message.get("receiver_facility"),
            raw=message.get("raw"),
            index=index,
        )
    return DeliveryAttempt(
        sender=_first_field(message, "MSH", 3),
        receiver=_first_field(message, "MSH", 5),
        control_id=_first_field(message, "MSH", 10),
        sent_at=_first_field(message, "MSH", 7),
        message_type=_message_type(message)[0],
        trigger_event=_message_type(message)[1],
        sender_facility=_first_field(message, "MSH", 4),
        receiver_facility=_first_field(message, "MSH", 6),
        raw=str(message),
        index=index,
    )


def _infer_layer(code, text, structure):
    """Infer a receipt layer from explicit text or the acknowledgment code."""
    text = (text or "").lower()
    if any(word in text for word in _APPLICATION_WORDS):
        return "application"
    if any(word in text for word in _RECEIVE_WORDS):
        return "receive"
    if code in _RECEIVE_CODES:
        return "receive"
    if code in _APPLICATION_CODES:
        return "application"
    return None


def _receipt_from_message(message, index):
    if not _is_ack(message):
        return None
    control_id = _first_field(message, "MSA", 2)
    if not control_id:
        return None
    code = _first_field(message, "MSA", 1).upper()
    text = _field(message, "MSA", 3)
    message_type, trigger, structure = _message_type(message)
    return Receipt(
        control_id=control_id,
        code=code,
        layer=_infer_layer(code, text, structure),
        received_at=_first_field(message, "MSH", 7),
        source=None,
        sending_application=_first_field(message, "MSH", 3),
        sending_facility=_first_field(message, "MSH", 4),
        receiving_application=_first_field(message, "MSH", 5),
        receiving_facility=_first_field(message, "MSH", 6),
        message_type=message_type,
        trigger_event=trigger,
        structure=structure,
        text=text,
        raw=str(message),
        index=index,
    )


def _receipt_from_mapping(data, index):
    control_id = data.get(
        "control_id", data.get("message_control_id", data.get("msa2", ""))
    )
    code = data.get("code", data.get("ack_code", data.get("msa1", "")))
    layer = data.get("layer", data.get("ack_layer"))
    if layer not in LAYERS:
        layer = None
    text = data.get("text", data.get("message", ""))
    structure = data.get("structure", "")
    return Receipt(
        control_id=str(control_id or "").strip(),
        code=str(code or "").strip().upper(),
        layer=layer,
        received_at=data.get("received_at", data.get("timestamp")),
        source=data.get("source"),
        sending_application=data.get("sending_application"),
        sending_facility=data.get("sending_facility"),
        receiving_application=data.get("receiving_application"),
        receiving_facility=data.get("receiving_facility"),
        message_type=data.get("message_type"),
        trigger_event=data.get("trigger_event"),
        structure=structure,
        text=text,
        raw=data.get("raw"),
        index=index,
    )


def _parse_receipts(receipts):
    parsed = []
    for index, item in enumerate(receipts):
        if isinstance(item, Receipt):
            item.index = index
            parsed.append(item)
            continue
        if isinstance(item, dict):
            receipt = _receipt_from_mapping(item, index)
            if receipt.control_id:
                parsed.append(receipt)
            continue
        if isinstance(item, (str, bytes)):
            for message in _iter_messages(item):
                receipt = _receipt_from_message(message, index)
                if receipt:
                    parsed.append(receipt)
            continue
        receipt = _receipt_from_message(item, index)
        if receipt:
            parsed.append(receipt)
    return parsed


def _strong_match(receipt, row):
    """Return the number of independent peer-identity signals that match.

    The outbound message sends from ``sender`` to ``receiver``.  An ACK swaps
    those roles, so its receiving application must equal the message sender
    and its sending application must equal the message receiver.
    """
    score = 0
    if receipt.receiving_application and receipt.receiving_application == row.sender:
        score += 2
    if receipt.sending_application and receipt.sending_application == row.receiver:
        score += 2
    if receipt.source and receipt.source in (row.sender, row.receiver):
        score += 2
    return score


def _conflicting_match(receipt, row):
    """True when a supplied identity signal points at a different peer."""
    attempt = row.attempts[0] if row.attempts else None
    if attempt is None:
        return False
    if (
        receipt.receiving_application
        and row.sender
        and receipt.receiving_application != row.sender
    ):
        return True
    if (
        receipt.sending_application
        and row.receiver
        and receipt.sending_application != row.receiver
    ):
        return True
    if (
        receipt.receiving_facility
        and attempt.sender_facility
        and receipt.receiving_facility != attempt.sender_facility
    ):
        return True
    if (
        receipt.sending_facility
        and attempt.receiver_facility
        and receipt.sending_facility != attempt.receiver_facility
    ):
        return True
    if receipt.source and receipt.source not in (row.sender, row.receiver):
        return True
    return False


def _late_threshold(value):
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return value
    return datetime.timedelta(seconds=float(value))


def _mark_late(row, threshold, first_attempt):
    if threshold is None or not row.attempts:
        return
    base_attempt = first_attempt
    if base_attempt is None:
        return
    deadline = _to_datetime(base_attempt.sent_at) + threshold
    for receipt in row.receipts:
        received = _to_datetime(receipt.received_at)
        if received is not None and received > deadline:
            receipt.late = True


def _assign_attempt(row, receipt):
    """Attach a receipt to the latest attempt sent at or before it.

    ``receipt.attempt_index`` is the position within this delivery row, so it
    can be displayed as attempt #1, #2, and so on.
    """
    attempts = row.attempts
    if not attempts:
        return
    receipt_dt = _to_datetime(receipt.received_at)
    if receipt_dt is None:
        receipt.attempt_index = len(attempts) - 1
        return
    candidates = [
        position
        for position, attempt in enumerate(attempts)
        if _to_datetime(attempt.sent_at) is not None
        and _to_datetime(attempt.sent_at) <= receipt_dt
    ]
    if candidates:
        receipt.attempt_index = max(
            candidates,
            key=lambda position: _to_datetime(attempts[position].sent_at),
        )
    else:
        receipt.attempt_index = 0


def build_delivery_table(messages, receipts, late_after=None):
    """Build a :class:`DeliveryTable`.

    ``messages`` are outbound messages (or mappings) and ``receipts`` are
    acknowledgments.  Retransmissions are messages with the same sender,
    receiver and control ID; they are retained as separate attempts on one
    row.  Receipts are never discarded.

    ``late_after`` is an optional timeout in seconds or a
    :class:`datetime.timedelta`, measured from the first attempt.  Receipts
    after that deadline are still retained and flagged ``late``.
    """
    attempts = []
    for index, item in enumerate(messages):
        for message in _iter_messages(item):
            attempts.append(_attempt_from_message(message, index))

    grouped = defaultdict(list)
    for attempt in attempts:
        grouped[attempt.key].append(attempt)

    rows = []
    for (sender, receiver, control_id), items in grouped.items():
        items.sort(
            key=lambda attempt: (
                _time_key(_to_datetime(attempt.sent_at)),
                attempt.index,
            )
        )
        rows.append(
            DeliveryRow(
                sender=sender,
                receiver=receiver,
                control_id=control_id,
                attempts=items,
            )
        )
    rows.sort(
        key=lambda row: (
            _time_key(_to_datetime(row.sent_at)),
            row.sender,
            row.control_id,
        )
    )

    table = DeliveryTable(rows=rows)
    by_control = defaultdict(list)
    for row in rows:
        by_control[row.control_id].append(row)

    threshold = _late_threshold(late_after)
    for receipt in _parse_receipts(receipts):
        control_rows = by_control.get(receipt.control_id, [])
        if not control_rows:
            table.unmatched.append(receipt)
            continue

        scores = {row.key: _strong_match(receipt, row) for row in control_rows}
        matches = [row for row in control_rows if scores[row.key] > 0]
        conflicts = [row for row in control_rows if _conflicting_match(receipt, row)]

        if len(matches) > 1:
            top_score = max(scores[row.key] for row in matches)
            matches = [row for row in matches if scores[row.key] == top_score]

        if not receipt.layer:
            possible = matches if matches else control_rows
            reason = (
                "layer_unspecified"
                if len(matches) == 1
                else "multiple_direction_matches"
                if len(matches) > 1
                else "insufficient_source_evidence"
            )
        elif len(matches) == 1:
            target = matches[0]
            target.receipts.append(receipt)
            _assign_attempt(target, receipt)
            continue
        elif len(matches) > 1:
            possible = matches
            reason = "multiple_direction_matches"
        elif len(control_rows) == 1 and not conflicts:
            target = control_rows[0]
            target.receipts.append(receipt)
            _assign_attempt(target, receipt)
            continue
        else:
            possible = control_rows
            reason = (
                "direction_conflict" if conflicts else "insufficient_source_evidence"
            )
        candidate = CandidateReceipt(
            receipt=receipt,
            possible_matches=[row.key for row in possible],
            reason=reason,
        )
        table.candidates.append(candidate)
        # Candidates live only in the candidate table.  They are deliberately
        # not attached to a delivery row so that no status can claim them.

    for row in rows:
        row.receipts.sort(key=lambda receipt: receipt.sort_key)
        first_attempt = row.attempts[0] if row.attempts else None
        _mark_late(row, threshold, first_attempt)

    return table


def _format_receipts(receipts):
    return " ".join(
        "{code}/{layer}/{at}{late}{attempt}".format(
            code=receipt.code or "?",
            layer=receipt.layer or "?",
            at=receipt.received_at or "?",
            late="!" if receipt.late else "",
            attempt=""
            if receipt.attempt_index is None
            else "#{0}".format(receipt.attempt_index + 1),
        )
        for receipt in receipts
    )


def format_delivery_table(table, delimiter="\t"):
    """Render a delivery table and its candidate receipts as text.

    The first block is one line per logical delivery.  The second block lists
    every receipt that could not be attributed to exactly one delivery.
    """
    columns = [
        "sender",
        "receiver",
        "control_id",
        "attempts",
        "attempt_times",
        "receive",
        "application",
        "final_status",
        "late",
        "application_late",
    ]
    lines = [delimiter.join(columns)]
    for row in table.rows:
        lines.append(
            delimiter.join(
                [
                    row.sender,
                    row.receiver,
                    row.control_id,
                    str(row.attempt_count),
                    " ".join(row.attempt_times),
                    "{0}/{1}".format(row.code("receive"), row.status("receive")),
                    "{0}/{1}".format(
                        row.code("application"), row.status("application")
                    ),
                    row.final_status,
                    _format_receipts(
                        [
                            receipt
                            for receipt in row.late_receipts
                            if receipt.layer != "application"
                        ]
                    ),
                    _format_receipts(
                        [
                            receipt
                            for receipt in row.late_receipts
                            if receipt.layer == "application"
                        ]
                    ),
                ]
            )
        )

    if table.candidates or table.unmatched:
        lines.append("")
        lines.append(
            delimiter.join(
                [
                    "control_id",
                    "code",
                    "layer",
                    "received_at",
                    "possible_matches",
                    "reason",
                ]
            )
        )
        for candidate in table.candidates:
            lines.append(
                delimiter.join(
                    [
                        candidate.receipt.control_id,
                        candidate.receipt.code,
                        candidate.receipt.layer or "",
                        candidate.receipt.received_at or "",
                        "|".join(
                            "{0}>{1}>{2}".format(*key)
                            for key in candidate.possible_matches
                        ),
                        candidate.reason or "",
                    ]
                )
            )
        for receipt in table.unmatched:
            lines.append(
                delimiter.join(
                    [
                        receipt.control_id,
                        receipt.code,
                        receipt.layer or "",
                        receipt.received_at or "",
                        "",
                        "no_matching_message",
                    ]
                )
            )
    return "\n".join(lines)
