# -*- coding: utf-8 -*-

from smpplib import consts
from smpplib.reassembly import (
    MultipartReassembler,
    reassemble,
    reassemble_parts,
    reassemble_text,
)


def udh8(reference, total, sequence, payload):
    return b'\x05\x00\x03' + bytes(bytearray((reference, total, sequence))) + payload


def udh16(reference, total, sequence, payload):
    return (b'\x06\x08\x04'
            + bytes(bytearray(((reference >> 8) & 0xff, reference & 0xff)))
            + bytes(bytearray((total, sequence))) + payload)


def deliver(payload=None, source=b'31600000000', destination=b'31600000001',
            data_coding=consts.SMPP_ENCODING_DEFAULT, esm_class=None,
            **kwargs):
    if payload is None:
        payload = b''
    if esm_class is None:
        esm_class = (consts.SMPP_GSMFEAT_UDHI
                     if payload[:1] in (b'\x05', b'\x06') else 0)
    result = {
        'source_addr': source,
        'destination_addr': destination,
        'esm_class': esm_class,
        'data_coding': data_coding,
        'short_message': payload,
    }
    result.update(kwargs)
    return result


def test_udh8_reassembles_in_position_order():
    messages = reassemble([
        deliver(udh8(0x42, 2, 2, b'llo')),
        deliver(udh8(0x42, 2, 1, b'he')),
    ])

    assert len(messages) == 1
    assert messages[0].complete
    assert messages[0].body == u'hello'
    assert messages[0].reference == 0x42


def test_udh16_reference_and_position_order():
    messages = reassemble([
        deliver(udh16(0x1234, 3, 3, b'c')),
        deliver(udh16(0x1234, 3, 1, b'a')),
        deliver(udh16(0x1234, 3, 2, b'b')),
    ])

    assert len(messages) == 1
    assert messages[0].reference == 0x1234
    assert messages[0].body == u'abc'
    assert messages[0].complete


def test_sar_only_parts_are_grouped():
    messages = reassemble([
        deliver(b'he', esm_class=0, sar_msg_ref_num=9,
                sar_total_segments=2, sar_segment_seqnum=1),
        deliver(b'llo', esm_class=0, sar_msg_ref_num=9,
                sar_total_segments=2, sar_segment_seqnum=2),
    ])

    assert len(messages) == 1
    assert messages[0].kind == 'sar'
    assert messages[0].body == u'hello'


def test_missing_middle_part_does_not_shift_later_parts():
    messages = reassemble([
        deliver(udh8(7, 3, 1, b'A')),
        deliver(udh8(7, 3, 3, b'C')),
    ])

    message = messages[0]
    assert not message.complete
    assert message.missing_sequences == [2]
    assert message.body == u'A�C'


def test_missing_middle_with_custom_gap_marker():
    messages = reassemble([
        deliver(udh8(7, 3, 1, b'A')),
        deliver(udh8(7, 3, 3, b'C')),
    ], gap=u'[missing]')

    assert messages[0].body == u'A[missing]C'


def test_missing_first_part_keeps_its_position():
    message = reassemble([deliver(udh8(7, 3, 2, b'B')), deliver(udh8(7, 3, 3, b'C'))])[0]

    assert message.missing_sequences == [1]
    assert message.body == u'�BC'


def test_duplicate_part_is_recorded_as_separate_occurrence():
    message = reassemble([
        deliver(udh8(7, 2, 1, b'he')),
        deliver(udh8(7, 2, 1, b'he')),
        deliver(udh8(7, 2, 2, b'llo')),
    ])[0]

    assert message.body == u'hello'
    assert message.complete
    assert message.duplicate_sequences == [1]
    assert len(message.occurrences) == 3
    assert len(message.duplicates[1]) == 1
    assert not message.conflicts


def test_conflicting_payload_at_same_position_is_retained():
    message = reassemble([
        deliver(udh8(7, 2, 1, b'first')),
        deliver(udh8(7, 2, 1, b'other')),
        deliver(udh8(7, 2, 2, b'!')),
    ])[0]

    assert message.body == u'first!'
    assert message.has_conflicts
    assert message.conflicts[1] == [b'first', b'other']
    assert message.payloads[1] == [b'first', b'other']


def test_source_and_destination_scope_the_reference():
    messages = reassemble([
        deliver(udh8(7, 2, 1, b'AA'), source=b'111', destination=b'222'),
        deliver(udh8(7, 2, 1, b'BB'), source=b'333', destination=b'222'),
        deliver(udh8(7, 2, 2, b'22'), source=b'111', destination=b'222'),
        deliver(udh8(7, 2, 2, b'22'), source=b'333', destination=b'222'),
    ])

    assert len(messages) == 2
    assert messages[0].body == u'AA22'
    assert messages[0].source == u'111'
    assert messages[1].body == u'BB22'
    assert messages[1].source == u'333'


def test_reference_reuse_after_completion_creates_new_group():
    messages = reassemble([
        deliver(udh8(7, 2, 1, b'AA')),
        deliver(udh8(7, 2, 2, b'aa')),
        deliver(udh8(7, 2, 1, b'BB')),
        deliver(udh8(7, 2, 2, b'bb')),
    ])

    assert len(messages) == 2
    assert [message.body for message in messages] == [u'AAaa', u'BBbb']
    assert all(message.complete for message in messages)


def test_reference_reuse_while_previous_is_incomplete_is_retained_as_conflict():
    messages = reassemble([
        deliver(udh8(7, 2, 1, b'AA')),
        deliver(udh8(7, 2, 1, b'BB')),
        deliver(udh8(7, 2, 2, b'bb')),
    ])

    assert len(messages) == 1
    assert messages[0].complete
    assert messages[0].body == u'AAbb'
    assert messages[0].conflicts[1] == [b'AA', b'BB']


def test_after_completion_a_reused_reference_starts_a_new_group():
    messages = reassemble([
        deliver(udh8(7, 2, 1, b'A1')),
        deliver(udh8(7, 2, 1, b'B1')),
        deliver(udh8(7, 2, 2, b'A2')),
        deliver(udh8(7, 2, 2, b'B2')),
    ])

    assert len(messages) == 2
    assert messages[0].complete
    assert messages[0].body == u'A1A2'
    assert messages[0].conflicts[1] == [b'A1', b'B1']
    assert messages[1].missing_sequences == [1]
    assert messages[1].body == u'�B2'


def test_cross_part_utf16_character_decodes_on_full_sequence():
    payload_one = b'\x04'
    payload_two = b'\x1f'
    messages = reassemble([
        deliver(udh8(1, 2, 1, payload_one), data_coding=consts.SMPP_ENCODING_ISO10646),
        deliver(udh8(1, 2, 2, payload_two), data_coding=consts.SMPP_ENCODING_ISO10646),
    ])

    assert messages[0].body == u'П'


def test_cross_part_utf16_surrogate_pair():
    text = u'😀'
    encoded = text.encode('utf-16-be')
    messages = reassemble([
        deliver(udh8(1, 2, 1, encoded[:2]), data_coding=consts.SMPP_ENCODING_ISO10646),
        deliver(udh8(1, 2, 2, encoded[2:]), data_coding=consts.SMPP_ENCODING_ISO10646),
    ])

    assert messages[0].body == text


def test_complete_sequence_decodes_once_but_missing_sequence_does_not_join_across_gap():
    first = u'П'.encode('utf-16-be')[:1]
    third = u'П'.encode('utf-16-be')[1:]
    message = reassemble([
        deliver(udh8(1, 3, 1, first), data_coding=consts.SMPP_ENCODING_ISO10646),
        deliver(udh8(1, 3, 3, third), data_coding=consts.SMPP_ENCODING_ISO10646),
    ])[0]

    assert message.missing_sequences == [2]
    # The two halves never become adjacent: the missing part keeps position
    # two occupied, so each incomplete UTF-16 fragment decodes to replacement
    # characters while the gap marker keeps the positions distinct.
    assert message.body == u'�' + message.gap + u'�'


def test_non_fragmented_message_is_single_group():
    message = reassemble([deliver(b'plain', esm_class=0)])[0]

    assert message.kind == 'single'
    assert message.complete
    assert message.body == u'plain'
    assert message.missing_sequences == []


def test_invalid_sequence_is_reported_not_shifted():
    message = reassemble([deliver(udh8(1, 2, 3, b'x'))])[0]

    assert message.invalid_sequences == [3]
    assert not message.complete
    assert not message.valid
    assert message.body.endswith(u'x')


def test_udh_sar_mismatch_is_recorded_as_error():
    message = reassemble([
        deliver(udh8(1, 2, 1, b'a'), sar_msg_ref_num=2,
                sar_total_segments=2, sar_segment_seqnum=1),
    ])[0]

    assert 'udh_sar_mismatch' in message.evidence.errors
    assert not message.valid


def test_explicit_fragment_metadata_without_udh():
    messages = reassemble([
        deliver(b'he', esm_class=0, reference=5, total=2, sequence=1),
        deliver(b'llo', esm_class=0, reference=5, total=2, sequence=2),
    ])

    assert len(messages) == 1
    assert messages[0].kind == 'explicit'
    assert messages[0].body == u'hello'


def test_data_sm_message_payload_is_supported():
    messages = reassemble([
        deliver(None, esm_class=0, message_payload=udh8(3, 2, 2, b'b')),
        deliver(None, esm_class=0, message_payload=udh8(3, 2, 1, b'a')),
    ])

    assert len(messages) == 1
    assert messages[0].body == u'ab'


def test_stateful_reassembler_add_returns_group_and_finish_is_stable():
    reassembler = MultipartReassembler()
    first = reassembler.add(deliver(udh8(1, 2, 1, b'a')))
    second = reassembler.add(deliver(udh8(1, 2, 2, b'b')))

    assert first is second
    assert reassembler.finish() == [first]
    assert reassembler.complete_messages == [first]


def test_pdu_objects_are_supported():
    class FakePdu(object):
        source_addr = b'111'
        destination_addr = b'222'
        esm_class = consts.SMPP_GSMFEAT_UDHI
        data_coding = consts.SMPP_ENCODING_DEFAULT
        short_message = udh8(1, 2, 1, b'a')
        message_payload = None

    class SecondPdu(FakePdu):
        short_message = udh8(1, 2, 2, b'b')

    messages = reassemble([FakePdu(), SecondPdu()])
    assert messages[0].body == u'ab'


def test_reassemble_parts_accepts_output_of_make_parts():
    from smpplib.gsm import make_parts

    parts, encoding, _ = make_parts(u'hello ' * 40)
    messages = reassemble_parts(parts, encoding=encoding)

    assert len(messages) == 1
    assert messages[0].complete
    assert messages[0].body == u'hello ' * 40


def test_reassemble_text_returns_bodies():
    assert reassemble_text([
        deliver(udh8(1, 2, 1, b'a')),
        deliver(udh8(1, 2, 2, b'b')),
    ]) == [u'ab']
