"""Auditing tools for HL7 batch protocol archives.

An HL7 archive is layered as an optional :py:class:`~hl7.File`
(``FHS``/``FTS``) containing one or more :py:class:`~hl7.Batch` objects
(``BHS``/``BTS``), each of which carries the actual
:py:class:`~hl7.Message` objects. The ``BTS`` and ``FTS`` trailer segments
declare how many messages and batches respectively were transmitted.

These helpers reconcile those declared counts against the content actually
received, export a table mapping every message to the batch that carries it,
and isolate the individual batches whose trailers do not reconcile. A batch
with a mismatching trailer is rejected on its own, without affecting the
other batches. A batch that has a ``BHS`` header but no ``BTS`` trailer can
never be counted as a successful zero-message batch.
"""

from dataclasses import dataclass
from typing import List, Optional

from .containers import Batch, File, Message
from .exceptions import (
    BatchTrailerMismatchException,
    MalformedFileException,
    MissingBatchTrailerException,
)
from .parser import parse_hl7

#: The batch/file trailer reconciled with the content received.
STATUS_ACCEPTED = "accepted"

#: The batch/file trailer is missing or declares a count that does not
#: match the content received, so the batch/file is isolated.
STATUS_REJECTED = "rejected"


@dataclass
class MessageRecord:
    """One row of the exported message table.

    Each message received in the archive is recorded exactly once, tagged
    with the 1-based position of its batch within the file and of the
    message within that batch.
    """

    #: 1-based position of the owning batch within the file
    batch_index: int
    #: 1-based position of the message within its batch
    message_index: int
    #: The parsed HL7 message
    message: Message
    #: ``MSH.10`` message control id (empty string when absent)
    control_id: str
    #: Full ``MSH.9`` message type field (e.g. ``ADT^A04^ADT_A01``)
    message_type: str


@dataclass
class BatchAudit:
    """Reconciliation result for a single :py:class:`~hl7.Batch`."""

    #: 1-based position of the batch within the file
    index: int
    #: ``BHS.11`` batch control id, or None for an unwrapped batch
    batch_id: Optional[str]
    #: Whether a ``BHS`` header segment was present
    has_header: bool
    #: Whether a ``BTS`` trailer segment carrying a count was present
    has_trailer: bool
    #: Message count declared by ``BTS.1``, or None when not declared
    declared_count: Optional[int]
    #: Number of messages actually received in the batch
    actual_count: int
    #: The messages received in the batch, in transmission order
    messages: List[Message]
    #: The exception that caused the batch to be isolated, if any
    error: Optional[Exception]

    @property
    def is_empty(self) -> bool:
        """True when the batch contains no messages.

        A complete empty batch (``BHS`` plus ``BTS|0``) is a valid batch
        that is retained on record rather than discarded.
        """
        return self.actual_count == 0

    @property
    def accepted(self) -> bool:
        """True when the batch trailer reconciles with the messages."""
        return self.error is None

    @property
    def status(self) -> str:
        return STATUS_ACCEPTED if self.accepted else STATUS_REJECTED


@dataclass
class FileAudit:
    """Reconciliation result for a whole :py:class:`~hl7.File` archive."""

    #: Whether an ``FHS`` header segment was present
    has_header: bool
    #: Whether an ``FTS`` trailer segment carrying a count was present
    has_trailer: bool
    #: Batch count declared by ``FTS.1``, or None when not declared
    declared_count: Optional[int]
    #: Number of batches actually received in the file
    actual_count: int
    #: Audit result for each batch, in transmission order (all retained)
    batches: List[BatchAudit]
    #: Every message received, tagged with its owning batch
    records: List[MessageRecord]
    #: The exceptions raised at the file (``FHS``/``FTS``) layer, if any
    errors: List[Exception]

    @property
    def accepted_batches(self) -> List[BatchAudit]:
        """Batches whose trailers reconciled."""
        return [batch for batch in self.batches if batch.accepted]

    @property
    def rejected_batches(self) -> List[BatchAudit]:
        """Batches that were isolated due to a trailer problem."""
        return [batch for batch in self.batches if not batch.accepted]

    @property
    def accepted_messages(self) -> List[Message]:
        """Messages belonging to accepted batches, in transmission order."""
        return [
            message for batch in self.accepted_batches for message in batch.messages
        ]

    @property
    def accepted(self) -> bool:
        """True when the file trailer reconciles and every batch is accepted."""
        return not self.errors and all(batch.accepted for batch in self.batches)


def _segment_field(segment, num):
    """Return 1-based field *num* of *segment* as a stripped string.

    Returns an empty string when the segment or field is not present.
    """
    if segment is not None and len(segment) > num:
        return str(segment(num)).strip()
    return ""


def _trailer_count(trailer, missing_label, mismatch_exception):
    """Extract the integer count declared by a ``BTS``/``FTS`` trailer.

    Returns None when the trailer or its count field is absent. A present
    but non-integer count cannot reconcile and raises *mismatch_exception*.
    """
    if trailer is None or len(trailer) < 2:
        return None
    raw = str(trailer(1)).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise mismatch_exception(
            "{} declares a non-integer count {!r}".format(missing_label, raw)
        )


def _message_record(batch_index, message_index, message):
    msh = message.segment("MSH")
    control_id = message["MSH.10"] if len(msh) > 10 else ""
    message_type = str(msh(9)) if len(msh) > 9 else ""
    return MessageRecord(
        batch_index=batch_index,
        message_index=message_index,
        message=message,
        control_id=control_id,
        message_type=message_type,
    )


def audit_batch(batch: Batch) -> BatchAudit:
    """Reconcile one :py:class:`~hl7.Batch`.

    A batch wrapped in ``BHS``/``BTS`` must have a trailer whose declared
    message count equals the number of messages actually received. A
    missing trailer is reported as
    :class:`~hl7.MissingBatchTrailerException` and is never treated as a
    successful zero-message batch; a count that is present but wrong is
    reported as :class:`~hl7.BatchTrailerMismatchException`. Either failure
    isolates only this batch. An unwrapped batch (no ``BHS``/``BTS``) has
    no declaration to check and is always accepted.
    """
    header = batch.header
    trailer = batch.trailer
    actual_count = len(batch)
    # BHS.11 is the batch control id (BHS.10 is the batch comment)
    batch_id = _segment_field(header, 11) if header is not None else None
    error = None
    declared_count = None
    has_trailer = False
    try:
        declared_count = _trailer_count(
            trailer, "BTS trailer", BatchTrailerMismatchException
        )
        has_trailer = trailer is not None and declared_count is not None
    except BatchTrailerMismatchException as exc:
        # A present but unparseable count isolates just this batch
        error = exc
    if header is not None and error is None:
        label = "batch {!r}".format(batch_id) if batch_id else "a batch"
        if not has_trailer:
            error = MissingBatchTrailerException(
                "{} has a BHS header but no BTS trailer count; the {} "
                "message(s) received cannot be confirmed".format(label, actual_count)
            )
        elif declared_count != actual_count:
            error = BatchTrailerMismatchException(
                "{} BTS declares {} message(s) but {} received".format(
                    label, declared_count, actual_count
                )
            )
    return BatchAudit(
        index=1,
        batch_id=batch_id,
        has_header=header is not None,
        has_trailer=has_trailer,
        declared_count=declared_count,
        actual_count=actual_count,
        messages=list(batch),
        error=error,
    )


def audit_file(file: File) -> FileAudit:
    """Reconcile a whole :py:class:`~hl7.File` archive.

    Every batch is audited independently via :func:`audit_batch`, so a
    mismatching trailer isolates only that batch while the remaining
    batches stay accepted. The ``FTS`` trailer is then checked at the file
    layer: when an ``FHS`` header is present the ``FTS`` must declare the
    number of batches actually received. All batches -- including complete
    empty batches and isolated batches -- are retained in the result.
    """
    batch_audits = []
    records = []
    for index, batch in enumerate(file, start=1):
        batch_audit = audit_batch(batch)
        batch_audit.index = index
        batch_audits.append(batch_audit)
        for message_index, message in enumerate(batch_audit.messages, start=1):
            records.append(_message_record(index, message_index, message))

    header = file.header
    trailer = file.trailer
    declared_count = None
    has_trailer = False
    errors = []
    try:
        declared_count = _trailer_count(trailer, "FTS trailer", MalformedFileException)
        has_trailer = trailer is not None and declared_count is not None
    except MalformedFileException as exc:
        # A present but unparseable FTS count is a file-layer error
        errors.append(exc)
    if header is not None and not errors:
        if not has_trailer:
            errors.append(
                MalformedFileException(
                    "file has an FHS header but no FTS trailer count; the "
                    "{} batch(es) received cannot be confirmed".format(
                        len(batch_audits)
                    )
                )
            )
        elif declared_count != len(batch_audits):
            errors.append(
                MalformedFileException(
                    "FTS declares {} batch(es) but {} received".format(
                        declared_count, len(batch_audits)
                    )
                )
            )

    return FileAudit(
        has_header=header is not None,
        has_trailer=has_trailer,
        declared_count=declared_count,
        actual_count=len(batch_audits),
        batches=batch_audits,
        records=records,
        errors=errors,
    )


def message_table(auditable) -> List[MessageRecord]:
    """Export the message table for a :py:class:`~hl7.Batch` or
    :py:class:`~hl7.File`.

    Every message is returned once, tagged with the 1-based index of the
    batch that contains it and the 1-based index of the message within that
    batch. For a standalone batch the batch index is always 1.
    """
    if isinstance(auditable, File):
        return audit_file(auditable).records
    batch_audit = audit_batch(auditable)
    return [
        _message_record(1, message_index, message)
        for message_index, message in enumerate(batch_audit.messages, start=1)
    ]


def audit(auditable):
    """Audit a raw HL7 string or a parsed :py:class:`~hl7.Batch` /
    :py:class:`~hl7.File`.

    Raw strings are parsed with :func:`hl7.parse_hl7` first. Returns a
    :class:`BatchAudit` or :class:`FileAudit` accordingly.
    """
    if isinstance(auditable, str):
        auditable = parse_hl7(auditable)
    if isinstance(auditable, File):
        return audit_file(auditable)
    if isinstance(auditable, Batch):
        return audit_batch(auditable)
    raise TypeError(
        "audit expects an hl7.Batch or hl7.File, got {}".format(
            type(auditable).__name__
        )
    )
