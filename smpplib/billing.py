# -*- coding: utf8 -*-
"""Billing reconciliation built on SMPP evidence.

A business notification (one long SMS may be split into several encoded
segments) is reconciled through three independent calibres (口径):

  * business notification count (业务通知数):
    one per business message, regardless of segmentation or delivery outcome;
  * encoding segment count (编码分片数):
    number of SMS segments produced by ``smpplib.gsm.make_parts``, i.e. the
    units that could be charged if the gateway accepts every segment;
  * gateway confirmed charge count (网关确认收费数):
    segments backed by an explicit vendor charge receipt.  A successful
    ``submit_sm_resp`` is acceptance, not payment: without a charge receipt
    the charged amount stays ``None`` and is never guessed.

Evidence is correlated by SMPP identifiers:

  * ``submit_sm.sequence`` identifies a submit attempt;
  * ``submit_sm_resp.message_id`` identifies the gateway-accepted segment and
    is matched against ``receipted_message_id`` on delivery receipts and the
    ``message_id`` quoted on vendor bill lines;
  * the UDH concatenation header (reference / total / part number) groups the
    segments of a single long SMS.

Retries are append-only evidence: a segment keeps every attempt, but it is
counted once in the encoding segment calibre.  When a long SMS partially
fails, each segment is reported on its own merits -- the message is neither
booked as fully successful nor as fully zero.
"""

import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from smpplib import consts


# Standard SMPP delivery receipt body, e.g.
# "id:1d305b4c sub:001 dlvrd:001 submit date:1810151907 done date:1810151907
#  stat:DELIVRD err:000 text:..."
_RECEIPT_RE = re.compile(
    r'id:(?P<id>\S+)'
    r'.*?sub:(?P<sub>\d+)'
    r'.*?dlvrd:(?P<dlvrd>\d+)'
    r'.*?done date:(?P<done>\d+)'
    r'.*?stat:(?P<stat>[A-Za-z]+)'
    r'.*?err:(?P<err>\S+)',
    re.DOTALL,
)

_STAT_MESSAGE_STATES = {
    'DELIVRD': consts.SMPP_MESSAGE_STATE_DELIVERED,
    'EXPIRED': consts.SMPP_MESSAGE_STATE_EXPIRED,
    'DELETED': consts.SMPP_MESSAGE_STATE_DELETED,
    'UNDELIV': consts.SMPP_MESSAGE_STATE_UNDELIVERABLE,
    'ACCEPTD': consts.SMPP_MESSAGE_STATE_ACCEPTED,
    'UNKNOWN': consts.SMPP_MESSAGE_STATE_UNKNOWN,
    'REJECTD': consts.SMPP_MESSAGE_STATE_REJECTED,
}

# Segment lifecycle states, ordered from strongest evidence down.
SEGMENT_CHARGED = 'charged'
SEGMENT_DELIVERED_UNBILLED = 'delivered_unbilled'
SEGMENT_SUBMITTED_PENDING = 'submitted_pending'
SEGMENT_SUBMIT_FAILED = 'submit_failed'
SEGMENT_NOT_SUBMITTED = 'not_submitted'

MESSAGE_FULLY_CHARGED = 'fully_charged'
MESSAGE_PARTIAL = 'partial'
MESSAGE_FAILED = 'failed'
MESSAGE_PENDING = 'pending'


class BillingError(ValueError):
    """Raised when billing evidence is missing, inconsistent or uncorrelated."""


def _text(value):
    """Normalize PDU octet strings (bytes) into text for correlation."""
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8')
    return value


def _as_decimal(amount):
    try:
        return Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise BillingError('charge receipt amount is not numeric: %r' % (amount,))


def parse_udh(part):
    """Return ``(reference, total, part_number)`` from a UDHI part or ``None``.

    Only the 8-bit concatenation reference (IEI 0x00) produced by
    :func:`smpplib.gsm.make_parts_encoded` is understood.
    """
    if not part or len(part) < 6:
        return None
    header = bytearray(part[:6])
    if header[0] != 0x05 or header[1] != 0x00 or header[2] != 0x03:
        return None
    return header[3], header[4], header[5]


class SubmitAttempt(object):
    """One ``submit_sm`` / ``submit_sm_resp`` exchange for a segment.

    A rejected attempt may be followed by a retry carrying a new sequence;
    every attempt is retained as evidence.
    """

    def __init__(self, sequence, part_number, attempt_number, status, message_id=None):
        self.sequence = sequence
        self.part_number = part_number
        self.attempt_number = attempt_number
        self.status = status
        self.message_id = _text(message_id)

    @classmethod
    def from_pdu(cls, submit_pdu, resp_pdu, part_number, attempt_number):
        """Build an attempt from duck-typed submit/response PDU objects."""
        return cls(
            sequence=submit_pdu.sequence,
            part_number=part_number,
            attempt_number=attempt_number,
            status=resp_pdu.status,
            message_id=getattr(resp_pdu, 'message_id', None),
        )

    @property
    def ok(self):
        return self.status == consts.SMPP_ESME_ROK

    @property
    def error_description(self):
        return consts.DESCRIPTIONS.get(self.status, 'Unknown status 0x%x' % self.status)

    def __repr__(self):
        return 'SubmitAttempt(seq=%s part=%s attempt=%s status=0x%x ok=%s)' % (
            self.sequence, self.part_number, self.attempt_number, self.status, self.ok,
        )


class DeliveryReceipt(object):
    """Gateway delivery receipt (``deliver_sm``) for one ``message_id``.

    A delivery receipt proves handset/SMSC outcome, not payment.
    """

    def __init__(self, receipted_message_id, message_state=None, stat=None,
                 error_code=None, done_date=None):
        self.receipted_message_id = _text(receipted_message_id)
        self.message_state = message_state
        self.stat = stat
        self.error_code = error_code
        self.done_date = done_date

    @classmethod
    def from_text(cls, text):
        """Parse the standard receipt body carried in ``short_message``."""
        if isinstance(text, bytes):
            text = text.decode('utf-8')
        match = _RECEIPT_RE.search(text)
        if match is None:
            raise BillingError('unparseable delivery receipt: %r' % (text,))
        stat = match.group('stat').upper()
        return cls(
            receipted_message_id=match.group('id'),
            message_state=_STAT_MESSAGE_STATES.get(stat),
            stat=stat,
            error_code=match.group('err'),
            done_date=match.group('done'),
        )

    @classmethod
    def from_pdu(cls, pdu):
        """Build a receipt from a parsed ``deliver_sm`` PDU.

        The TLV ``receipted_message_id`` / ``message_state`` are preferred;
        the textual receipt body, when present, fills the remaining fields.
        """
        text = getattr(pdu, 'short_message', None)
        parsed = cls.from_text(text) if text else None
        receipted_message_id = getattr(pdu, 'receipted_message_id', None)
        if receipted_message_id is None and parsed is None:
            raise BillingError('deliver_sm PDU carries no receipted_message_id')
        if parsed is None:
            return cls(
                receipted_message_id=receipted_message_id,
                message_state=getattr(pdu, 'message_state', None),
            )
        return cls(
            receipted_message_id=receipted_message_id or parsed.receipted_message_id,
            message_state=getattr(pdu, 'message_state', None) or parsed.message_state,
            stat=parsed.stat,
            error_code=parsed.error_code,
            done_date=parsed.done_date,
        )

    @property
    def delivered(self):
        if self.message_state is not None:
            return self.message_state == consts.SMPP_MESSAGE_STATE_DELIVERED
        return self.stat == 'DELIVRD'

    def __repr__(self):
        return 'DeliveryReceipt(message_id=%s stat=%s delivered=%s)' % (
            self.receipted_message_id, self.stat, self.delivered,
        )


class ChargeReceipt(object):
    """Vendor bill line confirming a charge for one gateway ``message_id``.

    This is the only acceptable evidence for an amount.  ``amount`` is
    mandatory and must be numeric; a receipt without an amount cannot be used
    to book revenue.
    """

    def __init__(self, message_id, amount, currency, bill_id=None,
                 reference=None, part_number=None, charged_at=None):
        if amount is None:
            raise BillingError(
                'charge receipt for message_id=%r lacks an amount; a submit '
                'success must not be used to guess the charge' % (_text(message_id),)
            )
        self.message_id = _text(message_id)
        self.amount = _as_decimal(amount)
        self.currency = currency
        self.bill_id = bill_id
        self.reference = reference
        self.part_number = part_number
        self.charged_at = charged_at

    def __repr__(self):
        return 'ChargeReceipt(message_id=%s amount=%s %s bill=%s)' % (
            self.message_id, self.amount, self.currency, self.bill_id,
        )


class SegmentBilling(object):
    """Reconciled evidence for one encoded segment of a business message."""

    def __init__(self, reference, part_number, total_parts):
        self.reference = reference
        self.part_number = part_number
        self.total_parts = total_parts
        self.attempts = []
        self.delivery_receipts = []
        self.charge_receipts = []

    def record_attempt(self, attempt):
        if attempt.part_number != self.part_number:
            raise BillingError(
                'attempt part_number %s does not belong to segment %s' % (
                    attempt.part_number, self.part_number)
            )
        self.attempts.append(attempt)

    @property
    def attempt_count(self):
        """Total submit attempts, including retries (always >= 1 once sent)."""
        return len(self.attempts)

    @property
    def last_attempt(self):
        return self.attempts[-1] if self.attempts else None

    @property
    def message_ids(self):
        """Every message_id ever assigned, including ids from failed retries."""
        return [attempt.message_id for attempt in self.attempts if attempt.message_id]

    @property
    def submitted(self):
        """True only when the latest submit attempt was accepted (ROK)."""
        return bool(self.attempts) and self.last_attempt.ok

    @property
    def delivery_confirmed(self):
        return any(receipt.delivered for receipt in self.delivery_receipts)

    @property
    def charged(self):
        return bool(self.charge_receipts)

    @property
    def charged_amount(self):
        """Sum of vendor receipt amounts, or ``None`` without any receipt."""
        if not self.charge_receipts:
            return None
        return sum((receipt.amount for receipt in self.charge_receipts), Decimal('0'))

    @property
    def currency(self):
        return self.charge_receipts[0].currency if self.charge_receipts else None

    @property
    def status(self):
        if self.charged:
            return SEGMENT_CHARGED
        if self.delivery_confirmed:
            return SEGMENT_DELIVERED_UNBILLED
        if self.submitted:
            return SEGMENT_SUBMITTED_PENDING
        if self.attempts:
            return SEGMENT_SUBMIT_FAILED
        return SEGMENT_NOT_SUBMITTED

    def __repr__(self):
        return 'SegmentBilling(ref=%s part=%s/%s status=%s attempts=%s)' % (
            self.reference, self.part_number, self.total_parts,
            self.status, self.attempt_count,
        )


class MessageBilling(object):
    """All evidence for one business notification (a possible long SMS)."""

    def __init__(self, business_reference, segment_count, encoding=None):
        self.business_reference = business_reference
        self.encoding = encoding
        self.segments = OrderedDict(
            (part_number, SegmentBilling(reference=business_reference,
                                         part_number=part_number,
                                         total_parts=segment_count))
            for part_number in range(1, segment_count + 1)
        )

    @classmethod
    def from_parts(cls, business_reference, parts, encoding=None):
        """Build the record from the parts returned by ``make_parts``.

        UDHI parts contribute their concatenation reference; plain SaR parts
        are numbered sequentially under the business reference.
        """
        segment_count = len(parts)
        message = cls(business_reference, segment_count, encoding=encoding)
        udh_reference = business_reference
        for index, part in enumerate(parts, start=1):
            udh = parse_udh(part)
            if udh is not None:
                udh_reference, total, part_number = udh
                if total != segment_count or part_number != index:
                    raise BillingError(
                        'UDH header of %r does not match its part position' % (business_reference,)
                    )
            segment = message.segments[index]
            segment.reference = udh_reference
        return message

    def segment(self, part_number):
        try:
            return self.segments[part_number]
        except KeyError:
            raise BillingError(
                'message %r has no segment %s (has %s segments)' % (
                    self.business_reference, part_number, len(self.segments))
            )

    def record_attempt(self, attempt):
        self.segment(attempt.part_number).record_attempt(attempt)

    def segment_for_message_id(self, message_id):
        message_id = _text(message_id)
        for segment in self.segments.values():
            if message_id in segment.message_ids:
                return segment
        return None

    @property
    def business_notification_count(self):
        """业务通知数: a business message is one notification in every outcome."""
        return 1

    @property
    def encoding_segment_count(self):
        """编码分片数: segments produced by encoding, counted once despite retries."""
        return len(self.segments)

    @property
    def submit_accepted_count(self):
        return sum(1 for segment in self.segments.values() if segment.submitted)

    @property
    def submit_failed_count(self):
        return sum(
            1 for segment in self.segments.values()
            if segment.attempts and not segment.submitted
        )

    @property
    def delivery_confirmed_count(self):
        return sum(1 for segment in self.segments.values() if segment.delivery_confirmed)

    @property
    def gateway_charge_count(self):
        """网关确认收费数: only segments carrying a vendor charge receipt."""
        return sum(1 for segment in self.segments.values() if segment.charged)

    @property
    def charged_amount(self):
        """Receipt-backed amount, or ``None`` when no receipt exists at all."""
        amounts = [segment.charged_amount for segment in self.segments.values()]
        amounts = [amount for amount in amounts if amount is not None]
        if not amounts:
            return None
        return sum(amounts, Decimal('0'))

    @property
    def currency(self):
        for segment in self.segments.values():
            if segment.currency is not None:
                return segment.currency
        return None

    @property
    def status(self):
        charged = self.gateway_charge_count
        total = self.encoding_segment_count
        if charged == total:
            return MESSAGE_FULLY_CHARGED
        if charged or self.submit_accepted_count or self.delivery_confirmed_count:
            return MESSAGE_PARTIAL
        if self.submit_failed_count == total:
            return MESSAGE_FAILED
        return MESSAGE_PENDING

    def caliber_breakdown(self):
        """Explain each calibre separately, segment by segment."""
        failed_parts = [
            segment.part_number for segment in self.segments.values()
            if segment.status == SEGMENT_SUBMIT_FAILED
        ]
        unbilled_parts = [
            segment.part_number for segment in self.segments.values()
            if segment.charged is False and segment.submitted
        ]
        amount = self.charged_amount
        return OrderedDict((
            ('business_notification', OrderedDict((
                ('count', self.business_notification_count),
                ('note', '1 business notification regardless of segment outcome'),
            ))),
            ('encoding_segment', OrderedDict((
                ('count', self.encoding_segment_count),
                ('submit_accepted', self.submit_accepted_count),
                ('submit_failed', self.submit_failed_count),
                ('failed_parts', failed_parts),
            ))),
            ('gateway_charge', OrderedDict((
                ('count', self.gateway_charge_count),
                ('amount', None if amount is None else str(amount)),
                ('currency', self.currency),
                ('unbilled_parts', unbilled_parts),
                ('note', 'amount sourced exclusively from vendor charge receipts'),
            ))),
        ))

    def _note(self):
        notes = []
        for segment in self.segments.values():
            notes.append('part%s=%s' % (segment.part_number, segment.status))
        return '; '.join(notes)

    def row(self):
        """Flat table row for this business notification."""
        amount = self.charged_amount
        return OrderedDict((
            ('business_reference', self.business_reference),
            ('business_notification_count', self.business_notification_count),
            ('encoding_segment_count', self.encoding_segment_count),
            ('submit_accepted_count', self.submit_accepted_count),
            ('submit_failed_count', self.submit_failed_count),
            ('delivery_confirmed_count', self.delivery_confirmed_count),
            ('gateway_charge_count', self.gateway_charge_count),
            ('charged_amount', None if amount is None else str(amount)),
            ('currency', self.currency),
            ('status', self.status),
            ('segment_detail', self._note()),
        ))


class BillingLedger(object):
    """Correlates submit attempts, receipts and vendor bills into a table."""

    ROW_HEADERS = (
        ('business_reference', '业务引用'),
        ('business_notification_count', '业务通知数'),
        ('encoding_segment_count', '编码分片数'),
        ('submit_accepted_count', '提交接受数'),
        ('submit_failed_count', '提交失败数'),
        ('delivery_confirmed_count', '网关回执数'),
        ('gateway_charge_count', '网关确认收费数'),
        ('charged_amount', '收费金额'),
        ('currency', '币种'),
        ('status', '状态'),
        ('segment_detail', '分片明细'),
    )

    def __init__(self, currency=None):
        self.currency = currency
        self.messages = OrderedDict()
        self._message_id_index = {}

    def add_message(self, message):
        if message.business_reference in self.messages:
            raise BillingError(
                'duplicate business reference: %r' % (message.business_reference,)
            )
        self.messages[message.business_reference] = message
        return message

    def record_attempt(self, business_reference, attempt):
        message = self._message(business_reference)
        message.record_attempt(attempt)
        if attempt.ok and attempt.message_id:
            if attempt.message_id in self._message_id_index:
                raise BillingError(
                    'message_id %r is already correlated to another segment' % (
                        attempt.message_id,)
                )
            self._message_id_index[attempt.message_id] = (
                business_reference, attempt.part_number)

    def attach_delivery_receipt(self, receipt):
        segment = self._segment_for_receipt(receipt.receipted_message_id)
        segment.delivery_receipts.append(receipt)

    def attach_charge_receipt(self, receipt):
        if self.currency is None:
            self.currency = receipt.currency
        elif receipt.currency != self.currency:
            raise BillingError(
                'currency mismatch on bill %s: receipt uses %s, ledger uses %s' % (
                    receipt.bill_id, receipt.currency, self.currency)
            )
        segment = None
        if receipt.message_id:
            segment = self._segment_for_receipt(receipt.message_id)
        elif receipt.reference is not None and receipt.part_number is not None:
            segment = self._message(receipt.reference).segment(receipt.part_number)
        else:
            raise BillingError(
                'charge receipt %r has neither message_id nor reference/part_number' % (
                    receipt.bill_id,)
            )
        segment.charge_receipts.append(receipt)

    def _message(self, business_reference):
        try:
            return self.messages[business_reference]
        except KeyError:
            raise BillingError('unknown business reference: %r' % (business_reference,))

    def _segment_for_receipt(self, message_id):
        message_id = _text(message_id)
        try:
            business_reference, part_number = self._message_id_index[message_id]
        except KeyError:
            raise BillingError(
                'receipt for message_id=%r matches no accepted submit attempt' % (message_id,)
            )
        return self._message(business_reference).segment(part_number)

    def rows(self):
        return [message.row() for message in self.messages.values()]

    def totals(self):
        """Aggregate the three calibres over every business notification."""
        amount = sum(
            (message.charged_amount for message in self.messages.values()
             if message.charged_amount is not None),
            Decimal('0'),
        )
        has_any_receipt = any(
            message.charged_amount is not None for message in self.messages.values()
        )
        return OrderedDict((
            ('business_notification_count',
             sum(m.business_notification_count for m in self.messages.values())),
            ('encoding_segment_count',
             sum(m.encoding_segment_count for m in self.messages.values())),
            ('submit_accepted_count',
             sum(m.submit_accepted_count for m in self.messages.values())),
            ('submit_failed_count',
             sum(m.submit_failed_count for m in self.messages.values())),
            ('delivery_confirmed_count',
             sum(m.delivery_confirmed_count for m in self.messages.values())),
            ('gateway_charge_count',
             sum(m.gateway_charge_count for m in self.messages.values())),
            ('charged_amount', str(amount) if has_any_receipt else None),
            ('currency', self.currency),
        ))

    def render(self):
        """Plain-text table listing all three calibres side by side."""
        rows = self.rows()
        headers = [label for key, label in self.ROW_HEADERS]
        keys = [key for key, label in self.ROW_HEADERS]
        string_rows = [
            ['' if row[key] is None else str(row[key]) for key in keys]
            for row in rows
        ]
        widths = [
            max([len(header)] + [len(row[index]) for row in string_rows])
            for index, header in enumerate(headers)
        ]
        lines = [
            ' | '.join(header.ljust(widths[index]) for index, header in enumerate(headers)),
            '-+-'.join('-' * width for width in widths),
        ]
        for row in string_rows:
            lines.append(
                ' | '.join(cell.ljust(widths[index]) for index, cell in enumerate(row))
            )
        totals = self.totals()
        lines.append(
            'TOTAL: business_notification=%(business_notification_count)s '
            'encoding_segment=%(encoding_segment_count)s '
            'gateway_charge=%(gateway_charge_count)s '
            'amount=%(charged_amount)s %(currency)s' % dict(
                totals, currency=totals['currency'] or '')
        )
        return '\n'.join(lines)
