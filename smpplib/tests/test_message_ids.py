import json

import pytest

from smpplib import message_ids
from smpplib.client import Client
from smpplib.command import DeliverSM, SubmitSMResp


RECEIPT_TEXT = (
    b'id:0489708364 sub:001 dlvrd:001 submit date:1810151907 '
    b'done date:1810151907 stat:DELIVRD err:000 text:'
)


def make_deliver_sm(client=None, **kwargs):
    client = client or Client('localhost', 5679)
    return DeliverSM('deliver_sm', client=client, **kwargs)


def test_normalize_decimal_submit_id_keeps_raw():
    normalized = message_ids.normalize_message_id(
        b'0489708364', field='message_id')

    assert normalized.raw == b'0489708364'
    assert normalized.value == '489708364'
    assert normalized.format == message_ids.MessageIdFormat.DECIMAL
    assert normalized.ok


def test_normalize_hex_tlv_under_declared_rule():
    normalized = message_ids.normalize_message_id(
        b'1d305b4c', field='receipted_message_id')

    assert normalized.raw == b'1d305b4c'
    assert normalized.value == '489708364'
    assert normalized.format == message_ids.MessageIdFormat.HEX


def test_hex_looking_text_is_not_coerced_without_a_rule():
    normalized = message_ids.normalize_message_id('deadbeef')

    assert normalized.value == 'deadbeef'
    assert normalized.format == message_ids.MessageIdFormat.TEXT

    normalized = message_ids.normalize_message_id('deadbeef', field='message_id')
    assert normalized.value == 'deadbeef'
    assert normalized.format == message_ids.MessageIdFormat.TEXT


def test_explicit_hex_declaration_allows_conversion():
    for literal in ('0x1d305b4c', 'hex:1d305b4c', '1D305B4C'):
        normalized = message_ids.normalize_message_id(
            literal, format='hex')
        assert normalized.value == '489708364'


def test_unexplainable_value_is_unmatched_not_converted():
    normalized = message_ids.normalize_message_id(
        'not-a-message-id', field='receipted_message_id')

    assert not normalized.ok
    assert normalized.value is None
    assert normalized.raw == 'not-a-message-id'
    assert normalized.reason


def test_strict_mode_raises_invalid_message_id():
    with pytest.raises(message_ids.InvalidMessageId):
        message_ids.normalize_message_id(
            'not-a-message-id', field='receipted_message_id', strict=True)


def test_versioned_rule_selection():
    rules = message_ids.MessageIdRules({
        'versions': {
            '1.0': {'receipted_message_id': 'hex'},
            '2.0': {'receipted_message_id': 'decimal'},
        },
    })

    assert rules.resolve('receipted_message_id', version='1.0').format == 'hex'
    assert rules.resolve('receipted_message_id', version='1.5').format == 'hex'
    assert rules.resolve('receipted_message_id', version='2.0').format == 'decimal'
    assert rules.resolve('receipted_message_id', version='3.0').format == 'decimal'
    assert rules.resolve('receipted_message_id') is None


def test_gateway_scoped_rule_beats_generic_rule():
    rules = message_ids.MessageIdRules({
        'receipted_message_id': 'hex',
        'gateways': {
            'gateway-b': {'receipted_message_id': 'decimal'},
        },
    })

    assert rules.resolve(
        'receipted_message_id', gateway='gateway-b').format == 'decimal'
    assert rules.resolve(
        'receipted_message_id', gateway='unknown').format == 'hex'


def test_parse_receipt_text_does_not_treat_iid_as_id():
    receipt = message_ids.parse_receipt_text(
        b'iid:795920026 sub:001 dlvrd:001 submit date:200319131913 '
        b'done date:200319131913 stat:DELIVRD err:000 text:')

    assert receipt['id'] is None
    assert receipt['iid'] == '795920026'


def test_correlate_hex_tlv_with_decimal_submit():
    submit = SubmitSMResp(
        'submit_sm_resp', client=Client('localhost', 5679),
        message_id=b'0489708364')
    receipt = make_deliver_sm(
        receipted_message_id=b'1d305b4c',
        message_payload=RECEIPT_TEXT,
    )

    result = message_ids.correlate_message_ids([submit], [receipt])

    assert len(result.linked) == 1
    link = result.linked[0]
    assert link.message_id == '489708364'
    assert link.raw_submit == b'0489708364'
    assert link.raw_receipt == b'1d305b4c'
    assert result.unmatched == []


def test_correlate_text_ids_unchanged():
    result = message_ids.correlate_message_ids(
        [{'message_id': 'ABC-123'}],
        [{'receipted_message_id': 'ABC-123', 'declared_format': 'text'}],
    )

    assert len(result.linked) == 1
    assert result.linked[0].message_id == 'ABC-123'


def test_correlate_reports_unmatched_values():
    submit = SubmitSMResp(
        'submit_sm_resp', client=Client('localhost', 5679),
        message_id='abc-text')
    receipt = make_deliver_sm(receipted_message_id='deadbeef')

    result = message_ids.correlate_message_ids([submit], [receipt])

    assert result.linked == []
    assert len(result.unmatched) == 2
    assert {value.raw for value in result.unmatched} == {'abc-text', 'deadbeef'}
    # The hex-looking receipt is kept as text, not converted to 3735928559.
    assert result.unmatched[0].value == 'abc-text'


def test_conflicting_receipt_representations_are_unmatched():
    submit = SubmitSMResp(
        'submit_sm_resp', client=Client('localhost', 5679),
        message_id='489708364')
    receipt = make_deliver_sm(
        receipted_message_id=b'1d305b4c',
        message_payload=b'id:999999999 sub:001 dlvrd:001 '
                        b'stat:DELIVRD err:000 text:',
    )

    result = message_ids.correlate_message_ids([submit], [receipt])

    assert result.linked == []
    receipt_values = result.unmatched_receipt
    assert len(receipt_values) == 2
    assert all(
        'conflicting' in value.match_reason for value in receipt_values)


def test_client_configures_versioned_rules():
    rules = message_ids.MessageIdRules({
        'receipted_message_id': 'hex',
    })
    client = Client(
        'localhost', 5679,
        message_id_version='3.4',
        message_id_gateway='gateway-a',
        message_id_rules=rules,
    )
    submit = SubmitSMResp(
        'submit_sm_resp', client=client, message_id=b'0489708364')
    receipt = make_deliver_sm(
        client=client,
        receipted_message_id=b'1d305b4c',
        message_payload=RECEIPT_TEXT,
    )

    result = client.correlate_message_ids([submit], [receipt])

    assert len(result.linked) == 1
    assert result.linked[0].submit.version == '3.4'
    assert result.linked[0].submit.gateway == 'gateway-a'


def test_report_keeps_original_and_normalized_values():
    result = message_ids.correlate_message_ids(
        [{'message_id': b'0489708364'}],
        [{'receipted_message_id': b'1d305b4c'}],
    )

    report = json.loads(json.dumps(result.to_dict()))

    assert report['linked'][0]['message_id'] == '489708364'
    assert report['linked'][0]['submit']['raw'] == '0489708364'
    assert report['linked'][0]['receipt']['raw'] == '1d305b4c'
    assert report['linked'][0]['submit']['raw_hex'] == '30343839373038333634'


def test_pdu_namespace_rules_apply_to_fields():
    rules = message_ids.MessageIdRules({
        'submit_sm_resp': {'message_id': 'decimal'},
        'deliver_sm': {
            'receipted_message_id': {'format': 'hex'},
            'message_payload': {'format': 'text'},
        },
    })

    assert rules.resolve('message_id').format == 'decimal'
    assert rules.resolve('receipted_message_id').format == 'hex'
    assert rules.resolve('receipt_text_id').format == 'text'


def test_stateful_correlator_retains_raw_records():
    correlator = message_ids.MessageIdCorrelator(version='3.4')
    correlator.add_submit({'message_id': b'0489708364'})
    correlator.add_receipt({'receipted_message_id': b'1d305b4c'})

    report = correlator.report()

    assert report['linked'][0]['message_id'] == '489708364'
    assert correlator.raw_submits == [b'0489708364']
    assert correlator.raw_receipts == [b'1d305b4c']


def test_singular_module_exposes_same_api():
    from smpplib import message_id

    assert message_id.MessageIdRules is message_ids.MessageIdRules
    assert message_id.normalize_message_id(
        '1d305b4c', field='receipted_message_id').value == '489708364'


def test_cli_accepts_json_document(capsys, tmp_path):
    document = {
        'submit_responses': [{'message_id': '0489708364'}],
        'receipts': [{'receipted_message_id': '1d305b4c'}],
    }
    path = tmp_path / 'ids.json'
    path.write_text(json.dumps(document))

    assert message_ids.main([str(path)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output['linked'][0]['message_id'] == '489708364'
