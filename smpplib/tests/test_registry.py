from smpplib import consts
from smpplib.client import Client
from smpplib.registry import (
    DeliveryEvent,
    MessageStateRegistry,
    SubmitEvent,
)
from smpplib.smpp import make_pdu


def make_client():
    return Client('localhost', 5679, allow_unknown_opt_params=True)


def make_submit(client, sequence):
    pdu = make_pdu('submit_sm', client=client)
    pdu.sequence = sequence
    return pdu


def make_submit_resp(sequence, message_id=None, status=consts.SMPP_ESME_ROK):
    kwargs = {'status': status}
    if message_id is not None:
        kwargs['message_id'] = message_id
    pdu = make_pdu('submit_sm_resp', client=make_client(), **kwargs)
    pdu.sequence = sequence
    return pdu


def make_receipt(sequence, message_id, message_state, esm_class=None):
    if esm_class is None:
        esm_class = consts.SMPP_MSGTYPE_SMSC_DELIVERY_RECEIPT
    pdu = make_pdu(
        'deliver_sm',
        client=make_client(),
        esm_class=esm_class,
        receipted_message_id=message_id,
        message_state=message_state,
    )
    pdu.sequence = sequence
    return pdu


def make_text_receipt(sequence, message_id, stat, state):
    short_message = (
        'id:%s sub:001 dlvrd:001 submit date:2001010000 '
        'done date:2001010001 stat:%s err:000 text:' % (
            message_id.decode(), stat)
    ).encode()
    pdu = make_pdu(
        'deliver_sm',
        client=make_client(),
        esm_class=consts.SMPP_MSGTYPE_SMSC_DELIVERY_RECEIPT,
        short_message=short_message,
    )
    pdu.sequence = sequence
    return pdu


def test_successful_submit_is_indexed_by_message_id():
    registry = MessageStateRegistry()
    submit = make_submit(make_client(), 7)
    registry.note_submit(submit)

    assert registry.get_by_sequence(7) is not None

    record = registry.record_submit_response(
        make_submit_resp(7, message_id=b'msg-1'))

    assert record.message_id == b'msg-1'
    assert registry.get_record(b'msg-1') is record
    assert record.is_submitted
    assert isinstance(record.submit, SubmitEvent)
    assert record.submit.accepted
    assert record.message_state is None
    # The sequence placeholder is consumed once the response arrives.
    assert registry.get_by_sequence(7) is None


def test_failed_submit_keeps_submit_history_without_message_id():
    registry = MessageStateRegistry()
    registry.note_submit(make_submit(make_client(), 8))

    record = registry.record_submit_response(
        make_submit_resp(8, status=consts.SMPP_ESME_RSUBMITFAIL))

    assert record.message_id is None
    assert not record.is_submitted
    assert record.submit.accepted is False
    assert len(registry.records) == 0
    # Still reachable by sequence on the same connection for inspection.
    assert registry.get_by_sequence(8) is record

    registry.reset_sequence_scope()
    assert registry.get_by_sequence(8) is None


def test_delivery_receipt_correlates_by_message_id_not_sequence():
    registry = MessageStateRegistry()
    registry.note_submit(make_submit(make_client(), 1))
    record = registry.record_submit_response(
        make_submit_resp(1, message_id=b'msg-1'))

    # The receipt carries its own deliver_sm sequence; it must be ignored.
    receipt_pdu = make_receipt(sequence=999, message_id=b'msg-1',
                               message_state=consts.SMPP_MESSAGE_STATE_DELIVERED)
    same_record = registry.record_delivery(receipt_pdu)

    assert same_record is record
    assert len(record.events) == 2
    assert [event.phase for event in record.events] == ['submit', 'delivery']
    assert isinstance(record.delivery, DeliveryEvent)
    assert record.is_delivered
    assert record.is_terminal
    # The accepted-submission fact is untouched by the later phase.
    assert record.is_submitted
    assert record.submit.accepted


def test_delivery_failure_does_not_rewrite_successful_submission():
    registry = MessageStateRegistry()
    registry.note_submit(make_submit(make_client(), 2))
    record = registry.record_submit_response(
        make_submit_resp(2, message_id=b'msg-2'))

    registry.record_delivery(make_receipt(
        200, b'msg-2', consts.SMPP_MESSAGE_STATE_UNDELIVERABLE))

    assert record.is_submitted
    assert record.submit.accepted
    assert not record.is_delivered
    assert record.is_terminal
    assert record.message_state == consts.SMPP_MESSAGE_STATE_UNDELIVERABLE
    assert record.submit_events[0].status == consts.SMPP_ESME_ROK
    assert record.delivery_events[0].message_state == \
        consts.SMPP_MESSAGE_STATE_UNDELIVERABLE


def test_late_and_duplicate_receipts_are_appended_to_history():
    registry = MessageStateRegistry()
    registry.note_submit(make_submit(make_client(), 3))
    record = registry.record_submit_response(
        make_submit_resp(3, message_id=b'msg-3'))

    registry.record_delivery(make_receipt(
        300, b'msg-3', consts.SMPP_MESSAGE_STATE_ENROUTE))
    registry.record_delivery(make_receipt(
        301, b'msg-3', consts.SMPP_MESSAGE_STATE_DELIVERED))
    # A late duplicate after terminal state is still kept.
    registry.record_delivery(make_receipt(
        302, b'msg-3', consts.SMPP_MESSAGE_STATE_DELIVERED))

    assert len(record.delivery_events) == 3
    assert [event.message_state for event in record.delivery_events] == [
        consts.SMPP_MESSAGE_STATE_ENROUTE,
        consts.SMPP_MESSAGE_STATE_DELIVERED,
        consts.SMPP_MESSAGE_STATE_DELIVERED,
    ]
    assert record.message_state == consts.SMPP_MESSAGE_STATE_DELIVERED
    assert record.is_submitted


def test_reused_sequence_across_connections_does_not_cross_link():
    registry = MessageStateRegistry()

    # First connection: sequence 1 -> msg-1, accepted.
    registry.note_submit(make_submit(make_client(), 1))
    registry.record_submit_response(make_submit_resp(1, message_id=b'msg-1'))

    # Reconnect: sequence numbers restart. Pending scope is reset, records stay.
    registry.reset_sequence_scope()
    registry.note_submit(make_submit(make_client(), 1))
    registry.record_submit_response(make_submit_resp(1, message_id=b'msg-2'))

    # A late receipt from the old connection references the old message id.
    late_record = registry.record_delivery(make_receipt(
        500, b'msg-1', consts.SMPP_MESSAGE_STATE_DELIVERED))

    assert late_record is registry.get_record(b'msg-1')
    assert late_record is not registry.get_record(b'msg-2')
    assert registry.get_record(b'msg-2').message_state is None
    assert registry.get_record(b'msg-1').is_delivered


def test_unknown_receipt_creates_standalone_record():
    registry = MessageStateRegistry()
    record = registry.record_delivery(
        make_receipt(600, b'unknown', consts.SMPP_MESSAGE_STATE_EXPIRED))

    assert record.message_id == b'unknown'
    assert not record.is_submitted
    assert record.is_terminal
    assert len(record.submit_events) == 0
    assert len(record.delivery_events) == 1


def test_text_receipt_correlates_without_explicit_message_id():
    registry = MessageStateRegistry()
    registry.note_submit(make_submit(make_client(), 4))
    record = registry.record_submit_response(
        make_submit_resp(4, message_id=b'0489708364'))

    same_record = registry.record_delivery(
        make_text_receipt(700, b'0489708364', 'DELIVRD',
                          consts.SMPP_MESSAGE_STATE_DELIVERED))

    assert same_record is record
    assert record.is_delivered
    assert record.delivery.receipt_fields['id'] == '0489708364'


def test_tlv_state_is_authoritative_over_text_receipt():
    registry = MessageStateRegistry()
    registry.note_submit(make_submit(make_client(), 5))
    registry.record_submit_response(
        make_submit_resp(5, message_id=b'msg-5'))

    short_message = (
        b'id:msg-5 sub:001 dlvrd:000 submit date:2001010000 '
        b'done date:2001010001 stat:UNDELIV err:014 text:'
    )
    pdu = make_pdu(
        'deliver_sm',
        client=make_client(),
        esm_class=consts.SMPP_MSGTYPE_SMSC_DELIVERY_RECEIPT,
        receipted_message_id=b'msg-5',
        message_state=consts.SMPP_MESSAGE_STATE_DELIVERED,
        short_message=short_message,
    )
    pdu.sequence = 800
    record = registry.record_delivery(pdu)

    assert record.message_state == consts.SMPP_MESSAGE_STATE_DELIVERED
    assert record.delivery.receipt_fields['stat'] == 'UNDELIV'
