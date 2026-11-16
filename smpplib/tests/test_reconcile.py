import pytest

from smpplib import consts, smpp
from smpplib.client import Client, SimpleSequenceGenerator
from smpplib.reconcile import (
    REQUEST_ANSWERED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    REQUEST_UNKNOWN,
    RequestFactTable,
)


def make_request(client, command, sequence):
    pdu = smpp.make_pdu(command, client=client)
    pdu.sequence = sequence
    return pdu


def make_response(client, command, sequence, status=consts.SMPP_ESME_ROK):
    pdu = smpp.make_pdu(command, client=client)
    pdu.sequence = sequence
    pdu.status = status
    return pdu


def new_client():
    return Client('localhost', 5679, allow_unknown_opt_params=True)


def test_pending_request_waits_for_its_response():
    client = new_client()
    table = RequestFactTable()

    request = make_request(client, 'submit_sm', 7)
    record = table.record_request(request)

    assert record.status == REQUEST_PENDING
    assert record.request_pdu is request
    assert record.response_pdu is None
    assert table.pending_requests() == [record]
    assert table.stats()['in_flight'] == 1
    assert table.stats()[REQUEST_PENDING] == 1


def test_out_of_order_responses_match_their_own_requests():
    client = new_client()
    table = RequestFactTable()

    first = table.record_request(make_request(client, 'submit_sm', 1))
    second = table.record_request(make_request(client, 'submit_sm', 2))
    third = table.record_request(make_request(client, 'query_sm', 3))

    # Responses arrive in a different order than the requests.
    table.record_response(make_response(client, 'query_sm_resp', 3))
    table.record_response(make_response(client, 'submit_sm_resp', 1))

    assert third.status == REQUEST_ANSWERED
    assert first.status == REQUEST_ANSWERED
    assert second.status == REQUEST_PENDING
    # Rows stay in request-registration order; only their status changes.
    assert [record.sequence for record in table.answered_requests()] == [1, 3]
    assert [record.sequence for record in table.pending_requests()] == [2]
    assert table.stats()['in_flight'] == 1


def test_error_status_response_marks_request_rejected():
    client = new_client()
    table = RequestFactTable()

    request = table.record_request(make_request(client, 'submit_sm', 4))
    response = make_response(
        client, 'submit_sm_resp', 4, status=consts.SMPP_ESME_RSUBMITFAIL)
    record = table.record_response(response)

    assert record is request
    assert record.status == REQUEST_REJECTED
    assert record.response_pdu is response
    assert record.response_status == consts.SMPP_ESME_RSUBMITFAIL
    assert table.rejected_requests() == [request]
    assert table.pending_requests() == []
    assert table.stats()['in_flight'] == 0


def test_response_without_in_flight_request_is_unknown_orphan():
    client = new_client()
    table = RequestFactTable()

    orphan = table.record_response(make_response(client, 'submit_sm_resp', 99))

    assert orphan.status == REQUEST_UNKNOWN
    assert orphan.request_pdu is None
    assert orphan.response_pdu is not None
    assert table.unknown_records() == [orphan]
    assert table.pending_requests() == []


def test_command_mismatch_same_sequence_is_unknown_not_matched():
    client = new_client()
    table = RequestFactTable()

    table.record_request(make_request(client, 'submit_sm', 5))

    # A query_sm_resp must not complete a same-numbered submit_sm.
    orphan = table.record_response(make_response(client, 'query_sm_resp', 5))

    assert orphan.status == REQUEST_UNKNOWN
    assert table.pending_requests()[0].sequence == 5
    assert table.stats()['in_flight'] == 1


def test_non_response_pdu_is_ignored():
    client = new_client()
    table = RequestFactTable()
    deliver = smpp.make_pdu('deliver_sm', client=client)
    deliver.sequence = 11

    assert table.record_response(deliver) is None
    assert table.build_fact_table() == []


def test_duplicate_in_flight_sequence_is_rejected():
    client = new_client()
    table = RequestFactTable()

    table.record_request(make_request(client, 'submit_sm', 8))
    with pytest.raises(ValueError):
        table.record_request(make_request(client, 'submit_sm', 8))


def test_disconnect_turns_unanswered_requests_into_unknown():
    client = new_client()
    table = RequestFactTable()

    request = table.record_request(make_request(client, 'submit_sm', 1))
    table.close_open_requests()

    assert request.status == REQUEST_UNKNOWN
    assert table.pending_requests() == []
    assert table.stats()['in_flight'] == 0

    # A response arriving after the window closed can no longer complete it.
    orphan = table.record_response(make_response(client, 'submit_sm_resp', 1))
    assert orphan.status == REQUEST_UNKNOWN
    assert request.response_pdu is None


def test_late_response_of_old_session_cannot_complete_new_session_request():
    client = new_client()
    table = RequestFactTable()
    assert table.session_id == 1

    old_request = table.record_request(make_request(client, 'submit_sm', 1))
    table.start_new_session()
    assert table.session_id == 2

    # The old request is finalized as unknown, not silently dropped.
    assert old_request.status == REQUEST_UNKNOWN

    # Sequence numbers restart from one on the rebuilt connection.
    new_request = table.record_request(make_request(client, 'submit_sm', 1))
    assert new_request.status == REQUEST_PENDING
    assert new_request.session_id == 2

    # The late response of the old session has the same number but must not
    # be matched against the new session's in-flight request.
    orphan = table.record_response(
        make_response(client, 'submit_sm_resp', 1), session_id=1)
    assert orphan.status == REQUEST_UNKNOWN
    assert orphan.session_id == 1
    assert new_request.status == REQUEST_PENDING
    assert new_request.response_pdu is None

    # The new session's own response still completes its own request.
    completed = table.record_response(
        make_response(client, 'submit_sm_resp', 1), session_id=2)
    assert completed is new_request
    assert completed.status == REQUEST_ANSWERED


def test_wrap_around_only_matches_inside_the_in_flight_window():
    client = new_client()
    table = RequestFactTable()

    # First generation of sequence 1 completes and leaves the window.
    first_generation = table.record_request(make_request(client, 'enquire_link', 1))
    table.record_response(make_response(client, 'enquire_link_resp', 1))
    assert first_generation.status == REQUEST_ANSWERED

    # Wrap-around reuse of the same sequence number opens a new in-flight row.
    second_generation = table.record_request(make_request(client, 'enquire_link', 1))
    assert second_generation is not first_generation
    assert second_generation.status == REQUEST_PENDING

    # A late response carrying the old generation's number can only match
    # the request currently in the window, and only once.
    matched = table.record_response(make_response(client, 'enquire_link_resp', 1))
    assert matched is second_generation
    assert matched.status == REQUEST_ANSWERED

    stray = table.record_response(make_response(client, 'enquire_link_resp', 1))
    assert stray.status == REQUEST_UNKNOWN
    assert first_generation.response_pdu is not second_generation.response_pdu


def test_begin_connection_keeps_first_session_when_empty():
    table = RequestFactTable()
    table.begin_connection()
    assert table.session_id == 1

    table.record_request(make_request(new_client(), 'submit_sm', 1))
    table.begin_connection()
    assert table.session_id == 2


class FakeSocket(object):
    def __init__(self):
        self.sent = []
        self.closed = False

    def settimeout(self, timeout):
        pass

    def connect(self, address):
        pass

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def test_client_records_requests_and_responses_through_socket():
    client = new_client()
    fake_socket = FakeSocket()
    client._create_socket = lambda: fake_socket

    client.connect()
    assert client.request_fact_table.session_id == 1

    client.state = consts.SMPP_CLIENT_STATE_BOUND_TX
    request = smpp.make_pdu('submit_sm', client=client)
    request.sequence = 21
    assert client.send_pdu(request) is True

    [row] = client.request_fact_table.pending_requests()
    assert row.sequence == 21
    assert fake_socket.sent

    response = make_response(client, 'submit_sm_resp', 21)
    completed = client.request_fact_table.record_response(response)
    assert completed.status == REQUEST_ANSWERED
    assert client.request_fact_table.pending_requests() == []

    client.disconnect()


def test_client_reconnect_isolates_same_numbered_requests():
    client = new_client()
    fake_socket = FakeSocket()
    client._create_socket = lambda: fake_socket

    client.connect()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TX
    old_request = smpp.make_pdu('submit_sm', client=client)
    old_request.sequence = 1
    client.send_pdu(old_request)

    client.disconnect()
    assert old_request_row(client).status == REQUEST_UNKNOWN
    assert fake_socket.closed

    # Rebuilt connection: sequence space restarts from one.
    client.connect()
    assert client.request_fact_table.session_id == 2
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TX
    client.sequence_generator = SimpleSequenceGenerator()
    new_request = smpp.make_pdu('submit_sm', client=client)
    new_request.sequence = 1
    client.send_pdu(new_request)

    orphan = client.request_fact_table.record_response(
        make_response(client, 'submit_sm_resp', 1), session_id=1)
    assert orphan.status == REQUEST_UNKNOWN
    assert old_request_row(client).response_pdu is None
    assert client.request_fact_table.pending_requests()[0].request_pdu is new_request

    client.disconnect()


def old_request_row(client):
    return client.request_fact_table.build_fact_table()[0]


def feed_pdu(client, response):
    """Make the next client.read_pdu() return the given response PDU."""
    raw = response.generate()
    chunks = [raw[:4], raw[4:]]
    client._recv_exact = lambda exact_size: chunks.pop(0)


def test_client_read_pdu_reconciles_responses_automatically():
    client = new_client()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TX

    request = smpp.make_pdu('submit_sm', client=client)
    request.sequence = 30
    client.request_fact_table.record_request(request)

    feed_pdu(client, make_response(client, 'submit_sm_resp', 30))
    pdu = client.read_pdu()

    assert pdu.command == 'submit_sm_resp'
    assert client.request_fact_table.answered_requests()[0].request_pdu is request
    assert client.request_fact_table.pending_requests() == []


def test_client_read_pdu_records_rejection_and_orphan_responses():
    client = new_client()
    client.state = consts.SMPP_CLIENT_STATE_BOUND_TX

    request = smpp.make_pdu('submit_sm', client=client)
    request.sequence = 31
    client.request_fact_table.record_request(request)

    feed_pdu(client, make_response(
        client, 'submit_sm_resp', 31, status=consts.SMPP_ESME_RTHROTTLED))
    client.read_pdu()
    assert client.request_fact_table.rejected_requests()[0].request_pdu is request

    feed_pdu(client, make_response(client, 'submit_sm_resp', 404))
    client.read_pdu()
    orphan = client.request_fact_table.unknown_records()[-1]
    assert orphan.sequence == 404
    assert orphan.request_pdu is None

