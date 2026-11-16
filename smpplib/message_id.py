"""Compatibility alias for :mod:`smpplib.message_ids`.

The implementation lives in ``smpplib.message_ids``; this module exists so
both singular and plural import styles work.
"""

from smpplib.message_ids import (  # noqa: F401
    DEFAULT_RULES,
    FIELD_MESSAGE_ID,
    FIELD_RECEIPT_TEXT_ID,
    FIELD_RECEIPTED_MESSAGE_ID,
    FORMAT_AUTO,
    FORMAT_DECIMAL,
    FORMAT_HEX,
    FORMAT_TEXT,
    RULES,
    CorrelationResult,
    InvalidMessageId,
    MessageId,
    MessageIdCorrelator,
    MessageIdFormat,
    MessageIdLink,
    MessageIdNormalizer,
    MessageIdRule,
    MessageIdRules,
    NormalizedId,
    NormalizedMessageId,
    correlate,
    correlate_message_ids,
    correlate_receipts,
    default_rules,
    extract_receipt_id,
    is_delivery_receipt,
    link_message_ids,
    normalize_message_id,
    normalize_receipt_text,
    parse_receipt_id,
    parse_receipt_text,
    receipt_message_ids,
    receipt_text_id,
    submit_message_ids,
)
