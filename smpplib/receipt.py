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

"""SMSC delivery receipt parsing

Two encodings of a delivery receipt are seen on real SMSCs:

* the legacy receipt carried as text in ``deliver_sm.short_message``
  (``id:... sub:... dlvrd:... submit date:... done date:... stat:... err:... text:``);
* the SMPP 3.4 optional parameters ``receipted_message_id`` and
  ``message_state``.

Both identify the original submission by the message id the SMSC returned in
``submit_sm_resp``; the PDU sequence number is never used for correlation.
"""

import re

import six

from smpplib import consts


# Fixed, single-token fields of a text receipt. The trailing ``text`` field is
# handled separately because it may contain spaces or field-like tokens.
_RECEIPT_FIELD_RE = re.compile(
    r'(?<![^\s])'            # start of string or right after a space
    r'(id|sub|dlvrd|submit date|done date|stat|err)'
    r':(\S*)'
)
_TEXT_FIELD_RE = re.compile(r'(?<![^\s])text:')

# Final delivery outcome reported in the ``stat`` field of a text receipt.
RECEIPT_STATES = {
    'ENROUTE': consts.SMPP_MESSAGE_STATE_ENROUTE,
    'DELIVRD': consts.SMPP_MESSAGE_STATE_DELIVERED,
    'EXPIRED': consts.SMPP_MESSAGE_STATE_EXPIRED,
    'DELETED': consts.SMPP_MESSAGE_STATE_DELETED,
    'UNDELIV': consts.SMPP_MESSAGE_STATE_UNDELIVERABLE,
    'ACCEPTD': consts.SMPP_MESSAGE_STATE_ACCEPTED,
    'UNKNOWN': consts.SMPP_MESSAGE_STATE_UNKNOWN,
    'REJECTD': consts.SMPP_MESSAGE_STATE_REJECTED,
}


def _to_text(value):
    if isinstance(value, six.binary_type):
        return value.decode('utf-8', 'replace')
    return value


def _looks_like_text_receipt(text):
    """A legacy receipt always starts with its id field (SMPP 3.4).

    Some SMSCs spell the leading field ``rid:`` instead of the spec's ``id:``.
    """
    return text.startswith('id:') or text.startswith('rid:')


def parse_receipt(short_message):
    """Parse a legacy textual SMSC delivery receipt.

    Returns a dict with the receipt fields (``id``, ``sub``, ``dlvrd``,
    ``submit date``, ``done date``, ``stat``, ``err`` and ``text``) plus a
    ``message_state`` key derived from ``stat``. Missing fields are not
    present in the dict. Raises ``ValueError`` when the payload does not look
    like a delivery receipt (no leading ``id`` field).
    """
    text = _to_text(short_message).lstrip('\x00').lstrip()

    if not _looks_like_text_receipt(text):
        raise ValueError('Not a textual SMSC delivery receipt: %r' % short_message)

    # Normalize the vendor ``rid:`` spelling to the spec's ``id:``.
    if text.startswith('rid:'):
        text = 'id:' + text[len('rid:'):]

    # ``text`` is the last field and its content is free-form, so split it off
    # before parsing the fixed fields to avoid tokens inside the message body
    # being mistaken for receipt fields.
    fields = {}
    text_match = _TEXT_FIELD_RE.search(text)
    if text_match is not None:
        header = text[:text_match.start()]
        fields['text'] = text[text_match.end():]
    else:
        header = text

    for key, value in _RECEIPT_FIELD_RE.findall(header):
        fields[key] = value.strip()

    if 'id' not in fields:
        raise ValueError('Not a textual SMSC delivery receipt: %r' % short_message)

    stat = fields.get('stat')
    if stat is not None and stat in RECEIPT_STATES:
        fields['message_state'] = RECEIPT_STATES[stat]

    return fields


def is_delivery_receipt(pdu):
    """Return True if the deliver_sm PDU carries an SMSC delivery receipt.

    Detection follows two independent channels: the ``esm_class`` message type
    sub-field and the presence of receipt data (optional parameters or the
    legacy ``id:`` text payload).
    """
    message_type = (getattr(pdu, 'esm_class', None) or 0) & consts.SMPP_MSG_TYPE_MASK
    if message_type == consts.SMPP_MSGTYPE_SMSC_DELIVERY_RECEIPT:
        return True

    if getattr(pdu, 'receipted_message_id', None) is not None:
        return True

    short_message = getattr(pdu, 'short_message', None)
    if short_message:
        try:
            parse_receipt(short_message)
        except ValueError:
            return False
        return True

    return False


def get_receipt_message_id(pdu):
    """Return the gateway-assigned message id referenced by a receipt.

    The optional ``receipted_message_id`` parameter is preferred; otherwise the
    id is taken from the ``id`` field of the legacy text receipt. The returned
    id is normalized to bytes, matching the ``message_id`` type of
    ``submit_sm_resp``. Raises ``ValueError`` when no id can be found.
    """
    receipted_message_id = getattr(pdu, 'receipted_message_id', None)
    if receipted_message_id is not None:
        if isinstance(receipted_message_id, six.text_type):
            return receipted_message_id.encode('utf-8')
        return receipted_message_id

    short_message = getattr(pdu, 'short_message', None)
    if short_message:
        fields = parse_receipt(short_message)
        message_id = fields['id']
        if isinstance(message_id, six.text_type):
            return message_id.encode('utf-8')
        return message_id

    raise ValueError('Delivery receipt does not reference a message id')
