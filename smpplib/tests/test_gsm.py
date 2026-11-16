# -*- coding: utf8 -*-

import mock
from pytest import mark, raises

from smpplib import consts, exceptions
from smpplib.gsm import (
    gsm_encode,
    make_parts,
    make_parts_encoded,
    make_send_plan,
)


@mark.parametrize('plaintext, encoded_text', [
    (u'@', b'\x00'),
    (u'^', b'\x1B\x14'),
])
def test_gsm_encode(plaintext, encoded_text):
    assert gsm_encode(plaintext) == encoded_text


@mark.parametrize('plaintext', [
    (u'Ая',),
])
def test_gsm_encode_unicode_error(plaintext):
    with raises(UnicodeError):
        gsm_encode(plaintext)


@mark.parametrize('plaintext, encoding, expected_parts, expected_encoding', [
    (u'@', consts.SMPP_ENCODING_DEFAULT, [b'\x00'], consts.SMPP_ENCODING_DEFAULT),
    (u'Ая', consts.SMPP_ENCODING_DEFAULT, [b'\x04\x10\x04O'], consts.SMPP_ENCODING_ISO10646),
    (u'é', consts.SMPP_ENCODING_ISO88591, [b'\xe9'], consts.SMPP_ENCODING_ISO88591),
])
def test_make_parts_single(plaintext, encoding, expected_parts, expected_encoding):
    assert make_parts(plaintext, encoding) == (expected_parts, expected_encoding, consts.SMPP_MSGTYPE_DEFAULT)


@mark.parametrize('plaintext, expected', [
    (u'@' * consts.SEVENBIT_PART_SIZE * 2, [
        b'\x05\x00\x03\x42\x02\x01' + b'\x00' * consts.SEVENBIT_PART_SIZE,
        b'\x05\x00\x03\x42\x02\x02' + b'\x00' * consts.SEVENBIT_PART_SIZE,
    ]),
])
def test_make_parts_multiple(plaintext, expected):
    with mock.patch('random.randint') as randint:
        randint.return_value = 0x42
        assert make_parts(plaintext) == (expected, consts.SMPP_ENCODING_DEFAULT, consts.SMPP_GSMFEAT_UDHI)


@mark.parametrize('encoded_text, part_size, expected', [
    (b'12345', 5, [b'\x05\x00\x03\x42\x01\x0112345']),
    (b'12345', 2, [b'\x05\x00\x03\x42\x03\x0112', b'\x05\x00\x03\x42\x03\x0234', b'\x05\x00\x03\x42\x03\x035']),
])
def test_make_parts_encoded(encoded_text, part_size, expected):
    with mock.patch('random.randint') as randint:
        randint.return_value = 0x42
        assert make_parts_encoded(encoded_text, part_size) == expected


@mark.parametrize('text, expected', [
    (u'Привет мир!\n' * 10, 2),
])
def test_part_number(text, expected):
    parts, _, _ = make_parts(text)
    assert len(parts) == expected


@mark.parametrize('text', [
    u'^' * (consts.SEVENBIT_PART_SIZE - 2),
    u'^' * (consts.SEVENBIT_PART_SIZE + 1),
])
def test_gsm_extension_characters_count_as_two_units(text):
    plan = make_send_plan(text)
    assert u''.join(segment.text for segment in plan) == text
    assert all(len(segment.body) <= consts.SEVENBIT_PART_SIZE
               for segment in plan)
    assert all(segment.body.decode('latin-1') for segment in plan)


@mark.parametrize('text, expected_parts', [
    (u'^' * 80, 1),
    (u'^' * 81, 2),
])
def test_gsm_extension_characters_reduce_effective_capacity(text,
                                                             expected_parts):
    assert len(make_send_plan(text)) == expected_parts


def test_gsm_extension_character_is_not_split_across_parts():
    text = u'a' * consts.SEVENBIT_PART_SIZE + u'^' * 4 + u'b'
    plan = make_send_plan(text)
    assert [segment.text for segment in plan] == [
        u'a' * consts.SEVENBIT_PART_SIZE,
        u'^' * 4 + u'b',
    ]
    assert [segment.character_range for segment in plan] == [
        (0, consts.SEVENBIT_PART_SIZE),
        (consts.SEVENBIT_PART_SIZE, consts.SEVENBIT_PART_SIZE + 5),
    ]
    assert all(not segment.body.endswith(b'\x1b') for segment in plan)


def test_ucs2_surrogate_pair_is_not_split_across_parts():
    text = u'😀' * 36
    plan = make_send_plan(text)
    assert plan.encoding == consts.SMPP_ENCODING_ISO10646
    assert u''.join(segment.text for segment in plan) == text
    assert all(segment.body.decode('utf-16-be') == segment.text
               for segment in plan)
    assert all(len(segment.body) <= consts.UCS2_PART_SIZE
               for segment in plan)
    assert all(len(segment.body) % 2 == 0 for segment in plan)


def test_ucs2_surrogate_pair_not_split_when_segment_boundary_is_odd():
    # 66 BMP characters fill 132 of 134 available units, leaving too little
    # room for either half of the next character's surrogate pair.
    text = u'Ж' * 66 + u'😀' + u'!' + u'Ж' * 2
    plan = make_send_plan(text)
    assert [segment.text for segment in plan] == [
        u'Ж' * 66,
        u'😀!ЖЖ',
    ]
    assert [segment.character_range for segment in plan] == [
        (0, 66),
        (66, 70),
    ]


def test_fallback_to_ucs2_recalculates_capacity():
    text = u'a' * consts.SEVENBIT_LENGTH + u'Ж'
    plan = make_send_plan(text)
    assert plan.encoding == consts.SMPP_ENCODING_ISO10646
    assert plan.part_capacity == consts.UCS2_PART_SIZE
    assert len(plan) == 3
    assert all(len(segment.body) <= consts.UCS2_PART_SIZE
               for segment in plan)
    assert u''.join(segment.text for segment in plan) == text


@mark.parametrize('text', [
    u'abc\ud800def',
    u'\udc00abc',
])
def test_unencodable_characters_report_original_positions(text):
    with raises(exceptions.UnencodableCharacters) as error:
        make_send_plan(text)

    assert error.value.positions == [
        position for position, character in enumerate(text)
        if 0xD800 <= ord(character) <= 0xDFFF
    ]
    assert error.value.encoding == consts.SMPP_ENCODING_ISO10646
    assert error.value.text == text


def test_unencodable_characters_are_not_replaced():
    text = u'Hello \ud800 world'
    with raises(exceptions.UnencodableCharacters) as error:
        make_send_plan(text)
    assert error.value.positions == [6]
    assert error.value.characters == (u'\ud800',)


def test_explicit_surrogate_pair_is_treated_as_one_character():
    pair = u'😀'
    plan = make_send_plan(pair * 36, consts.SMPP_ENCODING_ISO10646)
    assert plan.total_segments == 2
    assert all(len(segment.body) % 4 == 0 for segment in plan)
    assert u''.join(segment.text for segment in plan) == pair * 36
    assert all(segment.text.encode('utf-16-be') == segment.body
               for segment in plan)
    assert all(len(segment.body) <= consts.UCS2_PART_SIZE
               for segment in plan)


def test_manually_constructed_surrogate_pair_stays_together():
    pair = u'😀'
    text = u'Ж' * 65 + pair + u'!' * 5
    plan = make_send_plan(text, consts.SMPP_ENCODING_ISO10646)
    assert [segment.text for segment in plan] == [
        u'Ж' * 65 + pair,
        u'!' * 5,
    ]
    assert all(segment.body.decode('utf-16-be') == segment.text
               for segment in plan)


def test_send_plan_headers_and_original_ranges():
    text = u'0123456789' * 40
    with mock.patch('random.randint') as randint:
        randint.return_value = 0x42
        plan = make_send_plan(text)

    assert plan.total_segments == 3
    assert plan.esm_class == consts.SMPP_GSMFEAT_UDHI
    assert plan.parts == [
        b'\x05\x00\x03\x42\x03\x01' + text[0:153].encode('ascii'),
        b'\x05\x00\x03\x42\x03\x02' + text[153:306].encode('ascii'),
        b'\x05\x00\x03\x42\x03\x03' + text[306:400].encode('ascii'),
    ]
    assert [segment.character_range for segment in plan] == [
        (0, 153),
        (153, 306),
        (306, 400),
    ]


def test_send_plan_single_part_has_no_header():
    plan = make_send_plan(u'hello')
    assert plan.parts == [b'hello']
    assert plan.segments[0].udh == b''
    assert plan.segments[0].character_range == (0, 5)


def test_backtick_is_encoded_as_itself_not_a_placeholder():
    assert gsm_encode(u'`') == b'\x5f'


@mark.parametrize('text, encoding, expected_capacity', [
    (u'a' * (consts.SEVENBIT_LENGTH + 1),
     consts.SMPP_ENCODING_DEFAULT, consts.SEVENBIT_LENGTH),
    (u'é' * (consts.EIGHTBIT_LENGTH + 1),
     consts.SMPP_ENCODING_ISO88591, consts.EIGHTBIT_LENGTH),
])
def test_no_udh_uses_full_payload_capacity(text, encoding,
                                           expected_capacity):
    plan = make_send_plan(text, encoding, use_udhi=False)
    assert plan.part_capacity == expected_capacity
    assert [segment.character_range for segment in plan] == [
        (0, expected_capacity),
        (expected_capacity, len(text)),
    ]
    assert all(segment.udh == b'' for segment in plan)


def test_make_parts_remains_a_three_tuple_with_plan_metadata():
    result = make_parts(u'a' * (consts.SEVENBIT_LENGTH + 1))
    parts, encoding, esm_class = result
    assert parts is result.parts
    assert encoding == result.encoding
    assert esm_class == result.esm_class
    assert result.plan.total_segments == 2
    assert result.character_ranges == [(0, 153), (153, 161)]
