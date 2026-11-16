from smpplib import consts, exceptions
from smpplib.client import Client
from smpplib.command import DeliverSM, OptionalParam

import pytest


def test_parse_deliver_sm():
    client = Client("localhost", 5679)
    pdu = DeliverSM('deliver_sm', client=client)
    pdu.parse(
        b"\x00\x00\x00\xcb\x00\x00\x00\x05\x00\x00\x00\x00\x00\x00\x00\x01\x00"
        b"\x01\x0131600000000\x00\x05\x00XXX YYYY\x00\x04\x00\x00\x00\x00\x00"
        b"\x00\x00\x00\x00\x00\x0e\x00\x01\x01\x00\x06\x00\x01\x01\x00\x1e\x00"
        b"\t1d305b4c\x00\x04'\x00\x01\x02\x04$\x00rid:0489708364 sub:001"
        b" dlvrd:001 submit date:1810151907 done date:1810151907 stat:DELIVRD"
        b" err:000 text:\x04\x1f\x04@\x048\x042\x045\x04B\x04&\x00\x01\x01"
    )

    assert pdu.source_addr_ton == consts.SMPP_TON_INTL
    assert pdu.source_addr_npi == consts.SMPP_NPI_ISDN
    assert pdu.source_addr == b'31600000000'
    assert pdu.destination_addr == b'XXX YYYY'
    assert pdu.receipted_message_id == b'1d305b4c'
    assert pdu.source_network_type == consts.SMPP_NETWORK_TYPE_GSM
    assert pdu.message_state == consts.SMPP_MESSAGE_STATE_DELIVERED
    assert pdu.user_message_reference is None


def test_unrecognised_optional_parameters():
    client = Client("localhost", 5679)
    pdu = DeliverSM("deliver_sm", client=client, allow_unknown_opt_params=True)
    pdu.parse(b'\x00\x00\x00\xa8\x00\x00\x00\x05\x00\x00\x00\x00/p\xc6'
              b'\x9a\x00\x00\x0022549909028\x00\x01\x00\x00\x04\x00\x00'
              b'\x00\x00\x00\x00\x00\x00iid:795920026 sub:001 dlvrd:001 '
              b'submit date:200319131913 done date:200319131913 stat:DELIVRD err:000 text:'
              b'\x14\x03\x00\x07(null)\x00\x14\x02\x00\x04612\x00'
    )

    # This is only to avoid a breaking change, at some point the other behaviour
    # should become the default.
    with pytest.raises(exceptions.UnknownCommandError):
        pdu2 = DeliverSM("deliver_sm", client=client)
        pdu2.parse(b'\x00\x00\x00\xa8\x00\x00\x00\x05\x00\x00\x00\x00/p\xc6'
                  b'\x9a\x00\x00\x0022549909028\x00\x01\x00\x00\x04\x00\x00'
                  b'\x00\x00\x00\x00\x00\x00iid:795920026 sub:001 dlvrd:001 '
                  b'submit date:200319131913 done date:200319131913 stat:DELIVRD err:000 text:'
                  b'\x14\x03\x00\x07(null)\x00\x14\x02\x00\x04612\x00'
        )


MANDATORY_PART = (
    b"\x00"                    # service_type
    b"\x01\x01"                # source_addr_ton, source_addr_npi
    b"31600000000\x00"         # source_addr
    b"\x05\x00"                # dest_addr_ton, dest_addr_npi
    b"XXX YYYY\x00"            # destination_addr
    b"\x04"                    # esm_class
    b"\x00"                    # protocol_id
    b"\x00"                    # priority_flag
    b"\x00"                    # schedule_delivery_time
    b"\x00"                    # validity_period
    b"\x00"                    # registered_delivery
    b"\x00"                    # replace_if_present_flag
    b"\x00"                    # data_coding
    b"\x00"                    # sm_default_msg_id
    b"\x00"                    # sm_length
)

VENDOR_TLV = b"\x14\x03\x00\x03ABC"  # unknown vendor tag 0x1403

OPTIONAL_PART = (
    b"\x04\x27\x00\x01\x02"        # message_state = 2
    + VENDOR_TLV
    + b"\x04\x27\x00\x01\x05"      # message_state = 5 (repeated tag)
    + b"\x00\x1e\x00\x04id1\x00"   # receipted_message_id = "id1"
)


def make_deliver_sm_pdu(body):
    header = b"\x00\x00\x00" + bytes([16 + len(body)]) \
        + b"\x00\x00\x00\x05\x00\x00\x00\x00\x00\x00\x00\x01"
    return header + body


def parse_vendor_pdu(**kwargs):
    client = Client("localhost", 5679)
    pdu = DeliverSM("deliver_sm", client=client, **kwargs)
    pdu.parse(make_deliver_sm_pdu(MANDATORY_PART + OPTIONAL_PART))
    return pdu


def test_optional_params_ordered_list():
    pdu = parse_vendor_pdu(allow_unknown_opt_params=True)

    assert [opt.name for opt in pdu.optional_params] == [
        'message_state', None, 'message_state', 'receipted_message_id',
    ]
    assert [opt.value for opt in pdu.optional_params] == [
        2, b'ABC', 5, b'id1',
    ]

    # Known items carry their decoded value and type
    assert pdu.optional_params[0].type is int
    assert pdu.optional_params[3].type is str
    assert pdu.optional_params[0] == OptionalParam(0x0427, 'message_state', 2)

    # Unknown vendor item keeps its raw bytes
    vendor = pdu.optional_params[1]
    assert vendor.code == 0x1403
    assert vendor.type is None
    assert vendor.raw == b'ABC'

    # Repeated tag: last value wins on the attribute
    assert pdu.message_state == 5


def test_optional_params_regenerate_untouched():
    raw = make_deliver_sm_pdu(MANDATORY_PART + OPTIONAL_PART)
    pdu = parse_vendor_pdu(allow_unknown_opt_params=True)

    assert pdu.generate() == raw


def test_optional_params_local_correction():
    pdu = parse_vendor_pdu(allow_unknown_opt_params=True)
    pdu.receipted_message_id = b'id2'

    corrected = OPTIONAL_PART.replace(b"id1\x00", b"id2\x00")
    assert pdu.generate() == make_deliver_sm_pdu(MANDATORY_PART + corrected)

    # Correcting a repeated tag updates all its occurrences, other
    # items (including the vendor TLV) stay untouched
    pdu = parse_vendor_pdu(allow_unknown_opt_params=True)
    pdu.message_state = 7
    corrected = OPTIONAL_PART.replace(b"\x04\x27\x00\x01\x02", b"\x04\x27\x00\x01\x07") \
        .replace(b"\x04\x27\x00\x01\x05", b"\x04\x27\x00\x01\x07")
    assert pdu.generate() == make_deliver_sm_pdu(MANDATORY_PART + corrected)


def test_optional_params_length_overrun_is_corrupt():
    # Declared length runs past the end of the body
    body = MANDATORY_PART + b"\x04\x27\x00\x10\x02"
    client = Client("localhost", 5679)
    pdu = DeliverSM("deliver_sm", client=client, allow_unknown_opt_params=True)
    with pytest.raises(exceptions.PDUError):
        pdu.parse(make_deliver_sm_pdu(body))

    # Truncated TLV header (fewer than 4 bytes left)
    body = MANDATORY_PART + b"\x04\x27\x00"
    pdu = DeliverSM("deliver_sm", client=client, allow_unknown_opt_params=True)
    with pytest.raises(exceptions.PDUError):
        pdu.parse(make_deliver_sm_pdu(body))
