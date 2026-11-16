#
# smpplib -- SMPP Library for Python
# Copyright (c) 2005 Martynas Jocius <mjoc@akl.lt>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""Two-phase message state registry

A submitted short message goes through two independent phases:

1. **Submission** -- the SMSC answers ``submit_sm`` with ``submit_sm_resp``.
   On success the response carries the gateway-assigned ``message_id`` which
   is the only valid correlation key from this point on.
2. **Delivery** -- the SMSC later sends ``deliver_sm`` delivery receipts
   (possibly several, including intermediate and late ones) referencing that
   same ``message_id``.

The PDU sequence number is per-connection and may be reused on a new
connection, so it is never used to correlate a delivery receipt with a
submission. It is only remembered while waiting for the matching
``submit_sm_resp`` on the same connection.

All state changes are appended to an event history: a failed or successful
terminal delivery receipt never overwrites the fact that the SMSC previously
accepted the message.
"""

import time

from smpplib import consts, receipt


# Terminal message states, SMPP 3.4 section 5.2.28. Other states
# (ENROUTE, ACCEPTED, UNKNOWN) are intermediate notifications.
FINAL_MESSAGE_STATES = frozenset((
    consts.SMPP_MESSAGE_STATE_DELIVERED,
    consts.SMPP_MESSAGE_STATE_EXPIRED,
    consts.SMPP_MESSAGE_STATE_DELETED,
    consts.SMPP_MESSAGE_STATE_UNDELIVERABLE,
    consts.SMPP_MESSAGE_STATE_REJECTED,
))


class MessageEvent(object):
    """Base class for an entry in a message's history."""

    phase = None

    def __init__(self, pdu, message_id=None, timestamp=None):
        self.pdu = pdu
        self.command = pdu.command
        self.status = pdu.status
        self.sequence = pdu.sequence
        self.message_id = message_id
        self.timestamp = time.time() if timestamp is None else timestamp

    def __repr__(self):
        return '{}(command={!r}, sequence={!r}, message_id={!r})'.format(
            self.__class__.__name__, self.command, self.sequence, self.message_id)


class SubmitEvent(MessageEvent):
    """A ``submit_sm_resp`` received from the SMSC (phase one)."""

    phase = 'submit'

    def __init__(self, pdu, message_id, timestamp=None):
        super(SubmitEvent, self).__init__(pdu, message_id=message_id, timestamp=timestamp)
        self.accepted = not pdu.is_error()


class DeliveryEvent(MessageEvent):
    """A ``deliver_sm`` delivery receipt from the SMSC (phase two)."""

    phase = 'delivery'

    def __init__(self, pdu, message_id, message_state, receipt_fields, timestamp=None):
        super(DeliveryEvent, self).__init__(pdu, message_id=message_id, timestamp=timestamp)
        self.message_state = message_state
        self.receipt_fields = receipt_fields
        self.final = message_state in FINAL_MESSAGE_STATES

    @property
    def delivered(self):
        return self.message_state == consts.SMPP_MESSAGE_STATE_DELIVERED


class MessageRecord(object):
    """Full known history of one gateway-assigned message id."""

    def __init__(self, message_id):
        self.message_id = message_id
        # Sequence of the submit_sm on the connection that created the record.
        self.sequence = None
        self.events = []
        self.submit = None
        self.delivery = None

    def add_event(self, event):
        """Append an event. Existing phase state is never removed."""
        self.events.append(event)
        if event.phase == 'submit':
            self.submit = event
        else:
            self.delivery = event

    @property
    def submit_events(self):
        return [event for event in self.events if event.phase == 'submit']

    @property
    def delivery_events(self):
        return [event for event in self.events if event.phase == 'delivery']

    @property
    def is_submitted(self):
        """True if the SMSC accepted the message (a successful submit resp)."""
        return self.submit is not None and self.submit.accepted

    @property
    def message_state(self):
        """State from the latest delivery receipt, or None if none arrived."""
        if self.delivery is None:
            return None
        return self.delivery.message_state

    @property
    def is_terminal(self):
        return self.delivery is not None and self.delivery.final

    @property
    def is_delivered(self):
        return self.delivery is not None and self.delivery.delivered

    def __repr__(self):
        return '{}(message_id={!r}, events={})'.format(
            self.__class__.__name__, self.message_id, len(self.events))


class MessageStateRegistry(object):
    """Append-only, message-id keyed store of message state."""

    def __init__(self):
        self._records = {}
        # Sequence number -> MessageRecord placeholder, valid for the current
        # connection only. Cleared by reset_sequence_scope() on (re)connect so
        # that a reused sequence number can never be matched to an old message.
        self._pending = {}

    def reset_sequence_scope(self):
        """Forget sequence numbers of the previous connection.

        Message records keyed by gateway message id are preserved.
        """
        self._pending.clear()

    def note_submit(self, pdu):
        """Remember an outgoing submit_sm while waiting for its response."""
        record = MessageRecord(message_id=None)
        record.sequence = pdu.sequence
        self._pending[pdu.sequence] = record
        return record

    def record_submit_response(self, pdu):
        """Record a submit_sm_resp and return the affected record.

        A successful response promotes the per-connection placeholder to a
        record indexed by the gateway-assigned message id. An error response
        has no message id and stays attached to the sequence placeholder.
        """
        placeholder = self._pending.pop(pdu.sequence, None)

        if pdu.is_error():
            message_id = None
        else:
            message_id = pdu.message_id

        if message_id is not None:
            record = self._records.get(message_id)
            if record is not None:
                # The gateway reused an id or the response arrived without a
                # tracked submit on this connection: extend the known record.
                pass
            elif placeholder is not None:
                record = placeholder
                record.message_id = message_id
            else:
                record = MessageRecord(message_id)
            self._records[message_id] = record
            record.sequence = pdu.sequence
        elif placeholder is not None:
            record = placeholder
            # Keep the failed submission reachable by sequence until reconnect.
            self._pending[pdu.sequence] = record
        else:
            record = MessageRecord(message_id=None)
            record.sequence = pdu.sequence

        record.add_event(SubmitEvent(pdu, message_id=message_id))
        return record

    def record_delivery(self, pdu, message_id=None):
        """Record a deliver_sm delivery receipt and return its record.

        Correlation uses the gateway-assigned message id only; it is taken
        from ``message_id`` when given or extracted from the receipt
        otherwise. A receipt that arrives without a known submission creates a
        standalone record instead of being linked to an unrelated message that
        happens to reuse a sequence number.
        """
        if message_id is None:
            message_id = receipt.get_receipt_message_id(pdu)

        record = self._records.get(message_id)
        if record is None:
            record = MessageRecord(message_id)
            self._records[message_id] = record

        message_state = getattr(pdu, 'message_state', None)
        receipt_fields = None
        if getattr(pdu, 'short_message', None):
            try:
                receipt_fields = receipt.parse_receipt(pdu.short_message)
            except ValueError:
                receipt_fields = None
        # The optional message_state parameter is authoritative when present;
        # otherwise derive the state from the legacy text receipt.
        if message_state is None and receipt_fields is not None:
            message_state = receipt_fields.get('message_state')

        record.add_event(DeliveryEvent(
            pdu,
            message_id=message_id,
            message_state=message_state,
            receipt_fields=receipt_fields,
        ))
        return record

    def get_record(self, message_id):
        """Return the record for a gateway message id, or None."""
        return self._records.get(message_id)

    def get_by_sequence(self, sequence):
        """Return the record tied to a sequence on this connection, or None."""
        return self._pending.get(sequence)

    @property
    def records(self):
        return dict(self._records)

    def __iter__(self):
        return iter(self._records)

    def __len__(self):
        return len(self._records)
