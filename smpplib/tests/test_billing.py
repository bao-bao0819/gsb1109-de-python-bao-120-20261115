# -*- coding: utf8 -*-
from decimal import Decimal

import pytest

from smpplib import consts, gsm
from smpplib.billing import (
    BillingError,
    BillingLedger,
    ChargeReceipt,
    DeliveryReceipt,
    MessageBilling,
    SegmentBilling,
    SubmitAttempt,
    parse_udh,
)
from smpplib.command import DeliverSM
from smpplib.client import Client


RECEIPT_BODY = (
    b'rid:0489708364 sub:001 dlvrd:001 submit date:1810151907 '
    b'done date:1810151907 stat:DELIVRD err:000 text:hello'
)


def make_attempt(sequence, part, attempt_number, status=consts.SMPP_ESME_ROK,
                 message_id=None):
    return SubmitAttempt(
        sequence=sequence,
        part_number=part,
        attempt_number=attempt_number,
        status=status,
        message_id=message_id,
    )


def test_parse_udh_from_encoded_part():
    parts, _, esm_class = gsm.make_parts(u'@' * consts.SEVENBIT_PART_SIZE * 3)
    assert esm_class == consts.SMPP_GSMFEAT_UDHI
    assert len(parts) == 3
    assert parse_udh(parts[0]) == (parse_udh(parts[0])[0], 3, 1)
    assert parse_udh(parts[1])[1:] == (3, 2)
    assert parse_udh(parts[2])[1:] == (3, 3)


def test_parse_udh_rejects_plain_part():
    assert parse_udh(b'plain') is None
    assert parse_udh(b'') is None


def test_message_billing_from_parts_uses_udh_reference():
    parts, encoding, _ = gsm.make_parts(u'@' * consts.SEVENBIT_PART_SIZE * 2)
    message = MessageBilling.from_parts('biz-1', parts, encoding=encoding)
    assert message.encoding_segment_count == 2
    assert list(message.segments) == [1, 2]
    assert message.segment(1).reference == message.segment(2).reference


def test_delivery_receipt_from_text():
    receipt = DeliveryReceipt.from_text(RECEIPT_BODY)
    assert receipt.receipted_message_id == '0489708364'
    assert receipt.stat == 'DELIVRD'
    assert receipt.delivered is True
    assert receipt.error_code == '000'
    assert receipt.message_state == consts.SMPP_MESSAGE_STATE_DELIVERED


def test_delivery_receipt_from_real_deliver_pdu():
    client = Client('localhost', 5679)
    pdu = DeliverSM('deliver_sm', client=client)
    pdu.parse(
        b"\x00\x00\x00\xcb\x00\x00\x00\x05\x00\x00\x00\x00\x00\x00\x00\x01\x00"
        b"\x01\x0131600000000\x00\x05\x00XXX YYYY\x00\x04\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x0e\x00\x01\x01\x00\x06\x00\x01\x01\x00\x1e\x00"
        b"\t1d305b4c\x00\x04'\x00\x01\x02\x04$\x00rid:0489708364 sub:001"
        b" dlvrd:001 submit date:1810151907 done date:1810151907 stat:DELIVRD"
        b" err:000 text:\x04\x1f\x04@\x048\x042\x045\x04B\x04&\x00\x01\x01"
    )
    receipt = DeliveryReceipt.from_pdu(pdu)
    assert receipt.receipted_message_id == '1d305b4c'
    assert receipt.delivered is True


def test_charge_receipt_requires_amount():
    with pytest.raises(BillingError):
        ChargeReceipt(message_id='m1', amount=None, currency='CNY')
    with pytest.raises(BillingError):
        ChargeReceipt(message_id='m1', amount='not-a-number', currency='CNY')


def test_submit_success_without_charge_receipt_is_not_guessable():
    message = MessageBilling('biz-single', segment_count=1)
    ledger = BillingLedger(currency='CNY')
    ledger.add_message(message)
    ledger.record_attempt('biz-single', make_attempt(1, 1, 1, message_id='m1'))

    row = message.row()
    assert row['business_notification_count'] == 1
    assert row['encoding_segment_count'] == 1
    assert row['gateway_charge_count'] == 0
    assert row['charged_amount'] is None
    assert message.status == 'partial'
    assert message.segment(1).status == 'submitted_pending'


def test_delivered_but_unbilled_segment_has_no_amount():
    message = MessageBilling('biz-dlr', segment_count=1)
    ledger = BillingLedger(currency='CNY')
    ledger.add_message(message)
    ledger.record_attempt('biz-dlr', make_attempt(1, 1, 1, message_id='m1'))
    ledger.attach_delivery_receipt(DeliveryReceipt(
        'm1', stat='DELIVRD',
        message_state=consts.SMPP_MESSAGE_STATE_DELIVERED))

    assert message.delivery_confirmed_count == 1
    assert message.gateway_charge_count == 0
    assert message.charged_amount is None
    assert message.segment(1).status == 'delivered_unbilled'


def build_partial_failure_ledger():
    """Long SMS of 3 parts: part1 charged, part2 retried then delivered unbilled,
    part3 fails on every attempt."""
    message = MessageBilling('biz-long', segment_count=3)
    ledger = BillingLedger(currency='CNY')
    ledger.add_message(message)

    # Part 1: accepted, delivered, vendor confirms the charge.
    ledger.record_attempt('biz-long', make_attempt(10, 1, 1, message_id='id-1'))
    ledger.attach_delivery_receipt(DeliveryReceipt(
        'id-1', stat='DELIVRD', message_state=consts.SMPP_MESSAGE_STATE_DELIVERED))
    ledger.attach_charge_receipt(ChargeReceipt('id-1', '0.045', 'CNY', bill_id='BILL-1'))

    # Part 2: first attempt throttled, retry accepted and delivered, but the
    # vendor has not issued a charge receipt yet.
    ledger.record_attempt('biz-long', make_attempt(
        11, 2, 1, status=consts.SMPP_ESME_RTHROTTLED))
    ledger.record_attempt('biz-long', make_attempt(12, 2, 2, message_id='id-2'))
    ledger.attach_delivery_receipt(DeliveryReceipt(
        'id-2', stat='DELIVRD', message_state=consts.SMPP_MESSAGE_STATE_DELIVERED))

    # Part 3: throttled twice, never accepted.
    ledger.record_attempt('biz-long', make_attempt(
        13, 3, 1, status=consts.SMPP_ESME_RTHROTTLED))
    ledger.record_attempt('biz-long', make_attempt(
        14, 3, 2, status=consts.SMPP_ESME_RSUBMITFAIL))

    return ledger, message


def test_partial_failure_keeps_three_calibres_separate():
    ledger, message = build_partial_failure_ledger()

    assert message.business_notification_count == 1
    assert message.encoding_segment_count == 3
    assert message.gateway_charge_count == 1
    assert message.status == 'partial'

    assert message.submit_accepted_count == 2
    assert message.submit_failed_count == 1
    assert message.delivery_confirmed_count == 2


def test_partial_failure_segments_are_booked_individually():
    _, message = build_partial_failure_ledger()

    part1, part2, part3 = (message.segment(n) for n in (1, 2, 3))
    assert part1.status == 'charged'
    assert part1.charged_amount == Decimal('0.045')

    assert part2.attempt_count == 2
    assert part2.submitted is True
    assert part2.delivery_confirmed is True
    assert part2.charged is False
    assert part2.charged_amount is None
    assert part2.status == 'delivered_unbilled'

    assert part3.attempt_count == 2
    assert part3.submitted is False
    assert part3.status == 'submit_failed'
    assert part3.last_attempt.error_description == consts.DESCRIPTIONS[
        consts.SMPP_ESME_RSUBMITFAIL]


def test_partial_failure_amount_comes_only_from_receipts():
    _, message = build_partial_failure_ledger()

    # Only part 1's receipt counts; parts 2 and 3 must not be guessed.
    assert message.charged_amount == Decimal('0.045')
    breakdown = message.caliber_breakdown()
    assert breakdown['business_notification']['count'] == 1
    assert breakdown['encoding_segment']['count'] == 3
    assert breakdown['encoding_segment']['failed_parts'] == [3]
    assert breakdown['gateway_charge']['count'] == 1
    assert breakdown['gateway_charge']['amount'] == '0.045'
    assert breakdown['gateway_charge']['unbilled_parts'] == [2]


def test_fully_failed_message_is_not_booked_all_zero_or_all_success():
    message = MessageBilling('biz-failed', segment_count=3)
    ledger = BillingLedger(currency='CNY')
    ledger.add_message(message)
    for part in (1, 2, 3):
        ledger.record_attempt('biz-failed', make_attempt(
            part, part, 1, status=consts.SMPP_ESME_RSUBMITFAIL))

    row = message.row()
    assert row['business_notification_count'] == 1
    assert row['encoding_segment_count'] == 3
    assert row['submit_accepted_count'] == 0
    assert row['submit_failed_count'] == 3
    assert row['gateway_charge_count'] == 0
    assert row['charged_amount'] is None
    assert message.status == 'failed'


def test_ledger_totals_and_rendered_table():
    ledger, long_message = build_partial_failure_ledger()

    single = MessageBilling('biz-ok', segment_count=1)
    ledger.add_message(single)
    ledger.record_attempt('biz-ok', make_attempt(20, 1, 1, message_id='id-9'))
    ledger.attach_charge_receipt(ChargeReceipt('id-9', '0.015', 'CNY', bill_id='BILL-1'))

    totals = ledger.totals()
    assert totals['business_notification_count'] == 2
    assert totals['encoding_segment_count'] == 4
    assert totals['gateway_charge_count'] == 2
    assert totals['charged_amount'] == '0.060'
    assert totals['submit_failed_count'] == 1

    table = ledger.render()
    assert '业务通知数' in table
    assert '编码分片数' in table
    assert '网关确认收费数' in table
    assert 'biz-long' in table and 'biz-ok' in table


def test_receipt_correlation_requires_accepted_attempt():
    ledger, _ = build_partial_failure_ledger()
    with pytest.raises(BillingError):
        ledger.attach_delivery_receipt(DeliveryReceipt('unknown-id', stat='DELIVRD'))
    with pytest.raises(BillingError):
        ledger.attach_charge_receipt(
            ChargeReceipt('unknown-id', '0.01', 'CNY', bill_id='BILL-X'))


def test_duplicate_message_id_is_rejected():
    ledger = BillingLedger(currency='CNY')
    ledger.add_message(MessageBilling('a', segment_count=1))
    ledger.add_message(MessageBilling('b', segment_count=1))
    ledger.record_attempt('a', make_attempt(1, 1, 1, message_id='dup'))
    with pytest.raises(BillingError):
        ledger.record_attempt('b', make_attempt(2, 1, 1, message_id='dup'))


def test_currency_mismatch_is_rejected():
    ledger = BillingLedger(currency='CNY')
    ledger.add_message(MessageBilling('a', segment_count=1))
    ledger.record_attempt('a', make_attempt(1, 1, 1, message_id='m'))
    with pytest.raises(BillingError):
        ledger.attach_charge_receipt(ChargeReceipt('m', '0.01', 'USD', bill_id='B'))


def test_charge_receipt_can_correlate_by_reference_and_part():
    ledger = BillingLedger(currency='CNY')
    ledger.add_message(MessageBilling('biz-ref', segment_count=2))
    ledger.record_attempt('biz-ref', make_attempt(1, 1, 1, message_id='m1'))
    ledger.record_attempt('biz-ref', make_attempt(2, 2, 1, message_id='m2'))

    ledger.attach_charge_receipt(ChargeReceipt(
        None, '0.045', 'CNY', bill_id='BILL-2',
        reference='biz-ref', part_number=2))
    message = ledger.messages['biz-ref']
    assert message.gateway_charge_count == 1
    assert message.segment(2).charged is True
    assert message.segment(1).charged is False


def test_attempt_from_real_submit_pdus():
    client = Client('localhost', 5679)
    from smpplib.smpp import make_pdu

    submit = make_pdu('submit_sm', client=client, short_message=b'hello')
    submit.sequence = 77
    resp = make_pdu('submit_sm_resp', client=client, status=consts.SMPP_ESME_ROK,
                    message_id=b'gw-77')
    attempt = SubmitAttempt.from_pdu(submit, resp, part_number=1, attempt_number=1)
    assert attempt.sequence == 77
    assert attempt.ok is True
    assert attempt.message_id == 'gw-77'

    bad_resp = make_pdu('submit_sm_resp', client=client,
                        status=consts.SMPP_ESME_RTHROTTLED)
    retry = SubmitAttempt.from_pdu(submit, bad_resp, part_number=1, attempt_number=2)
    assert retry.ok is False
    assert retry.message_id is None


def test_unknown_business_reference_and_part_are_rejected():
    ledger = BillingLedger()
    ledger.add_message(MessageBilling('biz', segment_count=1))
    with pytest.raises(BillingError):
        ledger.record_attempt('missing', make_attempt(1, 1, 1, message_id='m'))
    with pytest.raises(BillingError):
        ledger.attach_charge_receipt(ChargeReceipt(
            None, '1', 'CNY', reference='biz', part_number=9))


def test_segment_attempt_mismatch_is_rejected():
    segment = SegmentBilling(reference='r', part_number=1, total_parts=2)
    with pytest.raises(BillingError):
        segment.record_attempt(make_attempt(1, 2, 1))
