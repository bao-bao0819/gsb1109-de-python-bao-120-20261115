import pytest

from smpplib import consts, receipt


TEXT_RECEIPT = (
    b'id:0489708364 sub:001 dlvrd:001 submit date:1810151907 '
    b'done date:1810151907 stat:DELIVRD err:000 text:'
)


class FakePDU(object):
    def __init__(self, command='deliver_sm', status=consts.SMPP_ESME_ROK,
                 sequence=1, **attrs):
        self.command = command
        self.status = status
        self.sequence = sequence
        self.esm_class = 0
        self.short_message = None
        self.receipted_message_id = None
        self.message_state = None
        for name, value in attrs.items():
            setattr(self, name, value)

    def is_error(self):
        return self.status != consts.SMPP_ESME_ROK


def test_parse_text_receipt():
    fields = receipt.parse_receipt(TEXT_RECEIPT)

    assert fields['id'] == '0489708364'
    assert fields['sub'] == '001'
    assert fields['dlvrd'] == '001'
    assert fields['submit date'] == '1810151907'
    assert fields['done date'] == '1810151907'
    assert fields['stat'] == 'DELIVRD'
    assert fields['err'] == '000'
    assert fields['text'] == ''
    assert fields['message_state'] == consts.SMPP_MESSAGE_STATE_DELIVERED


def test_parse_text_receipt_with_text_payload():
    fields = receipt.parse_receipt(
        b'id:42 sub:001 dlvrd:000 submit date:2001010000 '
        b'done date:2001010001 stat:UNDELIV err:014 text:hello world'
    )

    assert fields['stat'] == 'UNDELIV'
    assert fields['message_state'] == consts.SMPP_MESSAGE_STATE_UNDELIVERABLE
    assert fields['text'] == 'hello world'


def test_parse_text_receipt_keeps_field_like_tokens_in_text():
    fields = receipt.parse_receipt(
        b'id:42 sub:001 dlvrd:001 submit date:2001010000 '
        b'done date:2001010001 stat:DELIVRD err:000 text:ref id:99 of 100'
    )

    assert fields['id'] == '42'
    assert fields['text'] == 'ref id:99 of 100'


def test_parse_text_receipt_accepts_unicode():
    fields = receipt.parse_receipt(TEXT_RECEIPT.decode('utf-8'))
    assert fields['id'] == '0489708364'


def test_parse_vendor_rid_receipt():
    fields = receipt.parse_receipt(
        b'rid:0489708364 sub:001 dlvrd:001 submit date:1810151907 '
        b'done date:1810151907 stat:DELIVRD err:000 text:'
    )

    assert fields['id'] == '0489708364'
    assert fields['message_state'] == consts.SMPP_MESSAGE_STATE_DELIVERED


def test_parse_receipt_rejects_plain_message():
    with pytest.raises(ValueError):
        receipt.parse_receipt(b'hello world, this is a normal mobile message')


def test_parse_receipt_requires_leading_id_field():
    # An ordinary message may contain receipt-like tokens in the middle.
    with pytest.raises(ValueError):
        receipt.parse_receipt(b'please note the id:123 in your records')
    assert not receipt.is_delivery_receipt(
        FakePDU(short_message=b'please note the id:123 in your records'))


def test_get_receipt_message_id_from_text():
    pdu = FakePDU(short_message=TEXT_RECEIPT)
    assert receipt.get_receipt_message_id(pdu) == b'0489708364'


def test_get_receipt_message_id_prefers_tlv():
    pdu = FakePDU(
        short_message=TEXT_RECEIPT,
        receipted_message_id=b'tlv-id',
    )
    assert receipt.get_receipt_message_id(pdu) == b'tlv-id'


def test_get_receipt_message_id_missing():
    with pytest.raises(ValueError):
        receipt.get_receipt_message_id(FakePDU(short_message=b'no id here'))


def test_is_delivery_receipt_by_esm_class():
    assert receipt.is_delivery_receipt(
        FakePDU(esm_class=consts.SMPP_MSGTYPE_SMSC_DELIVERY_RECEIPT))


def test_is_delivery_receipt_by_tlv():
    assert receipt.is_delivery_receipt(FakePDU(receipted_message_id=b'1'))


def test_is_delivery_receipt_by_text():
    assert receipt.is_delivery_receipt(FakePDU(short_message=TEXT_RECEIPT))


def test_is_not_delivery_receipt_for_plain_message():
    assert not receipt.is_delivery_receipt(
        FakePDU(esm_class=0, short_message=b'see you at 5pm'))
