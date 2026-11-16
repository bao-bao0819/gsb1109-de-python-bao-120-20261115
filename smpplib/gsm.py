# -*- coding: utf8 -*-
import random

import six

from smpplib import consts, exceptions


CONCATENATED_UDH_LENGTH = 5
CONCATENATED_UDH_SIZE = 6
CONCATENATED_UDH_IEI = 0x00


class Segment(object):
    """One SMS segment and the part of the original text it represents."""

    def __init__(self, number, total, text, start, end, body,
                 encoded_start, encoded_end, udh=b'', reference=None):
        self.number = number
        self.sequence = number
        self.sequence_number = number
        self.part_number = number
        self.total = total
        self.total_segments = total
        self.segment_count = total
        self.start = start
        self.end = end
        self.char_start = start
        self.char_end = end
        self.character_range = (start, end)
        self.original_range = self.character_range
        self.source_range = self.character_range
        self.text = text
        self.body = body
        self.payload = body
        self.encoded = body
        self.encoded_text = body
        self.encoded_start = encoded_start
        self.encoded_end = encoded_end
        self.byte_start = encoded_start
        self.byte_end = encoded_end
        self.encoded_range = (encoded_start, encoded_end)
        self.character_length = end - start
        self.byte_length = encoded_end - encoded_start
        self.length = self.byte_length
        self.udh = udh
        self.header = udh
        self.reference = reference
        self.message_reference = reference
        self.units = self.byte_length
        self.encoded_units = self.byte_length
        self.part = udh + body
        self.data = self.part
        self.short_message = self.part

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def keys(self):
        return (
            'number', 'total', 'start', 'end', 'character_range', 'text',
            'body', 'encoded', 'encoded_range', 'udh', 'header',
            'reference', 'units', 'part', 'data', 'short_message',
        )

    def to_dict(self):
        return dict((key, getattr(self, key)) for key in self.keys())

    def __repr__(self):
        return (
            'Segment(number={0!r}, total={1!r}, character_range={2!r}, '
            'encoded_range={3!r})'
        ).format(self.number, self.total, self.character_range,
                 self.encoded_range)


class SendPlan(object):
    """Result of splitting a message for sending.

    ``parts`` contains exactly the bytes to put in the ``short_message``
    field.  Each segment also records the half-open range of the *original*
    text it covers, so callers can correlate a sent segment with their input
    without decoding it again.
    """

    def __init__(self, text, encoding, esm_class, segments,
                 single_capacity, part_capacity, use_udhi, reference=None):
        self.text = text
        self.encoding = encoding
        self.data_coding = encoding
        self.esm_class = esm_class
        self.segments = segments
        self.parts = [segment.part for segment in segments]
        self.payloads = [segment.body for segment in segments]
        self.headers = [segment.udh for segment in segments]
        self.character_ranges = [segment.character_range
                                 for segment in segments]
        self.encoded_ranges = [segment.encoded_range for segment in segments]
        self.reference = reference
        self.single_capacity = single_capacity
        self.part_capacity = part_capacity
        self.segment_capacity = part_capacity
        self.use_udhi = use_udhi
        self.total_segments = len(segments)

    def __iter__(self):
        return iter(self.segments)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return self.segments[key]
        if isinstance(key, six.integer_types):
            return self.segments[key]
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __len__(self):
        return len(self.segments)

    def __repr__(self):
        return (
            'SendPlan(encoding={0!r}, esm_class={1!r}, segments={2!r})'
        ).format(self.encoding, self.esm_class, self.total_segments)


class PartsResult(tuple):
    """Backwards-compatible result of :func:`make_parts`.

    It is a three-item tuple ``(parts, encoding, esm_class)`` and also exposes
    the richer :class:`SendPlan` attributes used by callers that need segment
    headers and original character ranges.
    """

    def __new__(cls, plan):
        result = tuple.__new__(
            cls, (plan.parts, plan.encoding, plan.esm_class))
        result.plan = plan
        result.parts = plan.parts
        result.encoding = plan.encoding
        result.data_coding = plan.data_coding
        result.esm_class = plan.esm_class
        result.segments = plan.segments
        result.payloads = plan.payloads
        result.headers = plan.headers
        result.character_ranges = plan.character_ranges
        result.encoded_ranges = plan.encoded_ranges
        result.reference = plan.reference
        result.single_capacity = plan.single_capacity
        result.part_capacity = plan.part_capacity
        result.segment_capacity = plan.segment_capacity
        result.use_udhi = plan.use_udhi
        result.total_segments = plan.total_segments
        return result


def _encoding_details(encoding):
    try:
        return ENCODINGS[encoding]
    except KeyError:
        raise NotImplementedError('encoding is not supported: %s' % encoding)


def _unencodable(encoding, text, requested_encoding=None):
    encode, _, _ = _encoding_details(encoding)
    positions = []
    for position, character in enumerate(text):
        try:
            encode(character)
        except UnicodeError:
            positions.append(position)
    return exceptions.UnencodableCharacters(
        encoding, positions, text, requested_encoding=requested_encoding)


def _encode(text, encoding):
    """Encode text, falling back to UCS-2 and reporting bad positions."""
    requested_encoding = encoding
    encode, single_capacity, part_capacity = _encoding_details(encoding)
    try:
        return encode(text), encoding, single_capacity, part_capacity
    except UnicodeError:
        if encoding == consts.SMPP_ENCODING_ISO10646:
            raise _unencodable(encoding, text, requested_encoding)

        fallback = consts.SMPP_ENCODING_ISO10646
        encode, single_capacity, part_capacity = _encoding_details(fallback)
        try:
            return encode(text), fallback, single_capacity, part_capacity
        except UnicodeError:
            raise _unencodable(fallback, text, requested_encoding)


def _encoded_character(text, position, encode):
    """Return the encoding and end position of one logical character.

    Python 3 strings may contain an astral character either as one code point
    or as an explicit high/low surrogate pair.  Both must stay together, while
    an unpaired surrogate must be rejected rather than silently replaced.
    """
    character = text[position]
    code_unit = ord(character)
    if 0xD800 <= code_unit <= 0xDBFF and position + 1 < len(text):
        low = ord(text[position + 1])
        if 0xDC00 <= low <= 0xDFFF:
            pair = character + text[position + 1]
            return encode(pair), position + 2
    return encode(character), position + 1


def _character_chunks(text, encode, part_size):
    """Return (start, end, body) chunks that never split a character.

    ``part_size`` is measured in encoded bytes.  GSM extension characters and
    UTF-16 surrogate pairs therefore count for their real wire size while the
    returned ranges always point at whole characters in ``text``.
    """
    chunks = []
    start = 0
    body = []
    body_size = 0
    position = 0

    while position < len(text):
        encoded_character, next_position = _encoded_character(
            text, position, encode)

        if body and body_size + len(encoded_character) > part_size:
            chunks.append((start, position, b''.join(body)))
            start = position
            body = []
            body_size = 0

        if len(encoded_character) > part_size:
            raise ValueError(
                'a single character needs %d units, segment capacity is %d'
                % (len(encoded_character), part_size)
            )

        body.append(encoded_character)
        body_size += len(encoded_character)
        position = next_position

    chunks.append((start, len(text), b''.join(body)))
    return chunks


def _make_udh(reference, total, number):
    return b''.join((
        six.int2byte(CONCATENATED_UDH_LENGTH),
        six.int2byte(CONCATENATED_UDH_IEI),
        b'\x03',
        six.int2byte(reference),
        six.int2byte(total),
        six.int2byte(number),
    ))


def make_send_plan(text, encoding=consts.SMPP_ENCODING_DEFAULT,
                   use_udhi=True):
    """Split ``text`` into a character-aware :class:`SendPlan`.

    The number of segments is based on the encoding actually used.  If the
    requested encoding cannot represent the text it falls back to UCS-2 and
    recalculates capacity using UCS-2 limits.  Characters that UCS-2 cannot
    represent either (for example lone surrogates) are never replaced; an
    :class:`smpplib.exceptions.UnencodableCharacters` error lists their
    zero-based positions in the original text instead.
    """
    encoded_text, encoding, single_capacity, udh_part_capacity = _encode(
        text, encoding)
    encode, _, _ = _encoding_details(encoding)

    use_udhi = bool(use_udhi)
    # Without a UDH every segment can use the full payload size.  With a UDH
    # the concatenation header consumes six bytes (seven GSM septets).
    part_capacity = udh_part_capacity if use_udhi else single_capacity
    if len(encoded_text) > single_capacity:
        chunks = _character_chunks(text, encode, part_capacity)
        if len(chunks) > 255:
            raise exceptions.MessageTooLong()

        if use_udhi:
            esm_class = consts.SMPP_GSMFEAT_UDHI
            reference = random.randint(0, 255)
        else:
            esm_class = consts.SMPP_MSGTYPE_DEFAULT
            reference = None
    else:
        chunks = [(0, len(text), encoded_text)]
        esm_class = consts.SMPP_MSGTYPE_DEFAULT
        reference = None

    total = len(chunks)
    segments = []
    encoded_offset = 0
    for number, (start, end, body) in enumerate(chunks, start=1):
        if total > 1 and use_udhi:
            udh = _make_udh(reference, total, number)
        else:
            udh = b''
        encoded_end = encoded_offset + len(body)
        segments.append(Segment(
            number=number,
            total=total,
            text=text[start:end],
            start=start,
            end=end,
            body=body,
            encoded_start=encoded_offset,
            encoded_end=encoded_end,
            udh=udh,
            reference=reference,
        ))
        encoded_offset = encoded_end

    return SendPlan(
        text=text,
        encoding=encoding,
        esm_class=esm_class,
        segments=segments,
        single_capacity=single_capacity,
        part_capacity=part_capacity,
        use_udhi=use_udhi,
        reference=reference,
    )


# Readable aliases for callers that think in terms of messages rather than
# send plans.
plan_message = make_send_plan
make_segments = make_send_plan


def make_parts(text, encoding=consts.SMPP_ENCODING_DEFAULT, use_udhi=True):
    """Returns tuple(parts, encoding, esm_class)

    The returned value also carries the complete send plan as ``.plan`` and
    its segment metadata as attributes, while remaining a normal 3-tuple for
    existing callers.
    """
    return PartsResult(make_send_plan(text, encoding, use_udhi))


# Source:
# http://stackoverflow.com/questions/2452861/python-library-for-converting-plain-text-ascii-into-gsm-7-bit-character-set
GSM_CHARACTER_TABLE = (
    u"@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>"
    u"?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ`¿abcdefghijklmnopqrstuvwxyzäöñüà"
    u"````````````````````^```````````````````{}`````\\````````````[~]`"
    u"|````````````````````````````````````€``````````````````````````"
)

# The table above uses U+0060 as a placeholder for unused code points.  Build
# the actual map explicitly so a literal backtick (GSM 0x5F) cannot be
# confused with those placeholders.
GSM_ENCODING_TABLE = dict((ord(character), index)
                          for index, character in enumerate(GSM_CHARACTER_TABLE)
                          if index < 0x80 or character != u'`')


def gsm_encode(plaintext):
    """Performs default GSM 7-bit encoding. Beware it's vendor-specific and not recommended for use."""
    encoded = []
    for character in plaintext:
        try:
            index = GSM_ENCODING_TABLE[ord(character)]
        except (KeyError, TypeError):
            raise UnicodeError(plaintext)
        if index < 0x80:
            encoded.append(six.int2byte(index))
        else:
            encoded.append(b'\x1B' + six.int2byte(index - 0x80))
    return b''.join(encoded)


# Map GSM encoding into a tuple of encode function, maximum single message size and a part size.
# Add new entry here should you need to use another encoding.
ENCODINGS = {
    consts.SMPP_ENCODING_DEFAULT: (gsm_encode, consts.SEVENBIT_LENGTH, consts.SEVENBIT_PART_SIZE),
    consts.SMPP_ENCODING_ISO88591: (lambda text: text.encode('iso-8859-1'), consts.EIGHTBIT_LENGTH, consts.EIGHTBIT_PART_SIZE),
    consts.SMPP_ENCODING_ISO10646: (lambda text: text.encode('utf-16-be'), consts.UCS2_LENGTH, consts.UCS2_PART_SIZE),
}


def make_parts_encoded(encoded_text, part_size):
    """Splits encoded text into SMS parts"""
    chunks = split_sequence(encoded_text, part_size)
    if len(chunks) > 255:
        raise exceptions.MessageTooLong()

    uid = random.randint(0, 255)
    header = b''.join((b'\x05\x00\x03', six.int2byte(uid), six.int2byte(len(chunks))))

    return [b''.join((header, six.int2byte(i), chunk)) for i, chunk in enumerate(chunks, start=1)]


def split_sequence(sequence, part_size):
    """Splits the sequence into equal parts"""
    return [sequence[i:i + part_size] for i in range(0, len(sequence), part_size)]
