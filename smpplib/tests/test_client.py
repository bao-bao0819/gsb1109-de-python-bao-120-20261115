import warnings
import pytest
from mock import Mock, call

from smpplib.client import Client
from smpplib.smpp import make_pdu
from smpplib import smpp
from smpplib import consts
from smpplib import exceptions


def make_client():
    return Client("localhost", 5679, allow_unknown_opt_params=True)


def make_receipt_pdu(client, sequence, message_id, message_state):
    pdu = make_pdu(
        'deliver_sm',
        client=client,
        esm_class=consts.SMPP_MSGTYPE_SMSC_DELIVERY_RECEIPT,
        receipted_message_id=message_id,
        message_state=message_state,
    )
    pdu.sequence = sequence
    return pdu


def submit_record(client, sequence, message_id):
    submit = make_pdu('submit_sm', client=client)
    submit.sequence = sequence
    client.message_registry.note_submit(submit)
    resp = make_pdu('submit_sm_resp', client=client, message_id=message_id)
    resp.sequence = sequence
    return client.message_registry.record_submit_response(resp)


def test_client_construction_allow_unknown_opt_params_warning():
    with warnings.catch_warnings(record=True) as w:
        client = Client("localhost", 5679)

    assert len(w) == 1
    assert "optional parameters" in str(w[0].message)
    assert not client.allow_unknown_opt_params


def test_client_error_pdu_default():
    client = Client("localhost", 5679)
    error_pdu = make_pdu("submit_sm_resp")
    error_pdu.status = consts.SMPP_ESME_RINVMSGLEN
    client.read_pdu = Mock(return_value=error_pdu)

    with pytest.raises(exceptions.PDUError) as exec_info:
        client.read_once()

    assert exec_info.value.args[1] == consts.SMPP_ESME_RINVMSGLEN

    # Should not raise
    client.read_once(ignore_error_codes=[consts.SMPP_ESME_RINVMSGLEN])


def test_client_error_pdu_custom_handler():
    client = Client("localhost", 5679)
    error_pdu = make_pdu("submit_sm_resp")
    error_pdu.status = consts.SMPP_ESME_RINVMSGLEN
    client.read_pdu = Mock(return_value=error_pdu)

    mock_error_pdu_handler = Mock()
    client.set_error_pdu_handler(mock_error_pdu_handler)

    client.read_once()

    assert mock_error_pdu_handler.mock_calls == [call(error_pdu)]


def test_send_message_registers_pending_sequence():
    client = make_client()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TX
    client.send_pdu = Mock()

    ssm = client.send_message(
        source_addr_ton=consts.SMPP_TON_INTL,
        source_addr='SENDER',
        dest_addr_ton=consts.SMPP_TON_INTL,
        destination_addr='RECIPIENT',
        short_message=b'hello',
    )

    assert client.message_registry.get_by_sequence(ssm.sequence) is not None
    client.send_pdu.assert_called_once_with(ssm)


def test_read_once_records_successful_submit_by_message_id():
    client = make_client()
    resp = make_pdu('submit_sm_resp', client=client, message_id=b'gateway-1')
    resp.sequence = 5
    client.read_pdu = Mock(return_value=resp)
    pending = make_pdu('submit_sm', client=client)
    pending.sequence = 5
    client.message_registry.note_submit(pending)

    client.read_once()

    record = client.message_registry.get_record(b'gateway-1')
    assert record is not None
    assert record.is_submitted
    assert record.sequence == 5
    assert len(record.submit_events) == 1
    assert client.message_registry.get_by_sequence(5) is None


def test_read_once_records_failed_submit_without_message_id():
    client = make_client()
    client.set_error_pdu_handler(Mock())
    resp = make_pdu('submit_sm_resp', client=client,
                    status=consts.SMPP_ESME_RSUBMITFAIL)
    resp.sequence = 6
    client.read_pdu = Mock(return_value=resp)
    pending = make_pdu('submit_sm', client=client)
    pending.sequence = 6
    client.message_registry.note_submit(pending)

    client.read_once()

    record = client.message_registry.get_by_sequence(6)
    assert record is not None
    assert not record.is_submitted
    assert record.submit.status == consts.SMPP_ESME_RSUBMITFAIL
    assert len(client.message_registry.records) == 0


def test_read_once_records_delivery_receipt_in_second_phase():
    client = make_client()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TRX
    client.send_pdu = Mock()
    client.set_message_received_handler(Mock(return_value=consts.SMPP_ESME_ROK))
    submit_record(client, sequence=7, message_id=b'gateway-2')

    receipt_pdu = make_receipt_pdu(
        client, sequence=4242, message_id=b'gateway-2',
        message_state=consts.SMPP_MESSAGE_STATE_DELIVERED)
    client.read_pdu = Mock(return_value=receipt_pdu)

    client.read_once()

    record = client.message_registry.get_record(b'gateway-2')
    assert [event.phase for event in record.events] == ['submit', 'delivery']
    assert record.is_submitted
    assert record.is_delivered
    # The response to deliver_sm echoes the deliver_sm sequence, not the
    # original submit_sm sequence.
    sent = client.send_pdu.call_args[0][0]
    assert sent.command == 'deliver_sm_resp'
    assert sent.sequence == 4242


def test_delivery_failure_preserves_successful_submission():
    client = make_client()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TRX
    client.send_pdu = Mock()
    client.set_message_received_handler(Mock())
    submit_record(client, sequence=8, message_id=b'gateway-3')

    failure = make_receipt_pdu(
        client, sequence=4243, message_id=b'gateway-3',
        message_state=consts.SMPP_MESSAGE_STATE_UNDELIVERABLE)
    client.read_pdu = Mock(return_value=failure)
    client.read_once()

    record = client.message_registry.get_record(b'gateway-3')
    assert record.is_submitted
    assert record.submit.status == consts.SMPP_ESME_ROK
    assert record.message_state == consts.SMPP_MESSAGE_STATE_UNDELIVERABLE
    assert not record.is_delivered


def test_reused_sequence_after_reconnect_does_not_cross_link():
    client = make_client()

    submit_record(client, sequence=1, message_id=b'old-msg')
    client.message_registry.reset_sequence_scope()
    submit_record(client, sequence=1, message_id=b'new-msg')

    late = make_receipt_pdu(
        client, sequence=4244, message_id=b'old-msg',
        message_state=consts.SMPP_MESSAGE_STATE_DELIVERED)
    client.message_registry.record_delivery(late)

    assert client.message_registry.get_record(b'old-msg').is_delivered
    assert client.message_registry.get_record(b'new-msg').message_state is None


def test_plain_mobile_message_is_not_treated_as_receipt():
    client = make_client()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TRX
    client.send_pdu = Mock()
    client.set_message_received_handler(Mock())

    mo_message = make_pdu(
        'deliver_sm', client=client, esm_class=0,
        short_message=b'see you at 5pm')
    mo_message.sequence = 9
    client.read_pdu = Mock(return_value=mo_message)
    client.read_once()

    assert len(client.message_registry.records) == 0
    client.send_pdu.assert_called_once()


def test_connect_resets_sequence_scope_but_keeps_message_records():
    client = make_client()
    submit_record(client, sequence=10, message_id=b'gateway-4')
    client.message_registry._pending[11] = object()

    client._socket = Mock()
    client._socket.connect = Mock()
    client.connect()

    assert client.message_registry.get_record(b'gateway-4') is not None
    assert client.message_registry.get_by_sequence(11) is None


def test_read_once_correlates_real_binary_receipt_by_message_id():
    client = make_client()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TRX
    client.send_pdu = Mock()
    client.set_message_received_handler(Mock())
    submit_record(client, sequence=11, message_id=b'1d305b4c')

    raw_deliver_sm = (
        b"\x00\x00\x00\xcb\x00\x00\x00\x05\x00\x00\x00\x00\x00\x00\x00\x01\x00"
        b"\x01\x0131600000000\x00\x05\x00XXX YYYY\x00\x04\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x0e\x00\x01\x01\x00\x06\x00\x01\x01\x00\x1e\x00"
        b"\t1d305b4c\x00\x04'\x00\x01\x02\x04$\x00rid:0489708364 sub:001"
        b" dlvrd:001 submit date:1810151907 done date:1810151907 stat:DELIVRD"
        b" err:000 text:\x04\x1f\x04@\x048\x042\x045\x04B\x04&\x00\x01\x01"
    )
    deliver = smpp.parse_pdu(
        raw_deliver_sm, client=client, allow_unknown_opt_params=True)
    client.read_pdu = Mock(return_value=deliver)
    client.read_once()

    record = client.message_registry.get_record(b'1d305b4c')
    assert record is not None
    assert record.is_submitted
    assert record.is_delivered
    assert record.is_terminal
    assert [event.phase for event in record.events] == ['submit', 'delivery']
    assert record.delivery.message_state == consts.SMPP_MESSAGE_STATE_DELIVERED


