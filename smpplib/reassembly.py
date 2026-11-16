# -*- coding: utf-8 -*-
"""Reassembly of concatenated short messages.

The SMPP protocol can carry a long short message either as a User Data Header
(UDH) concatenation header inside ``short_message`` or through the optional
``sar_msg_ref_num`` / ``sar_total_segments`` / ``sar_segment_seqnum``
parameters.  Reference numbers are only meaningful together with the
originator and destination, and senders may reuse a reference number for a
later message.

This module keeps every received part as an occurrence.  It never overwrites a
part with the same sequence number, never moves a later part into the position
of a missing earlier part, and decodes text only after the complete payload
sequence has been joined.

Example::

    from smpplib.reassembly import reassemble

    for message in reassemble(received_pdus):
        print(message.source, message.destination, message.body)
"""

from __future__ import unicode_literals

import collections
import struct

import six

from smpplib import consts

try:
    long_types = (int, long)
except NameError:
    long_types = (int,)


UDH_CONCAT_8 = consts.SMPP_UDHIEIE_CONCATENATED
UDH_CONCAT_16 = consts.SMPP_UDHIEIE_CONCATENATED16
UDH_INDICATOR = consts.SMPP_GSMFEAT_UDHI

# U+FFFD is used to keep a missing part in place instead of moving the next
# part forward.  Callers that want a different archive representation can
# pass ``gap=...`` to the reassembler.
DEFAULT_GAP = u'�'


class FragmentEvidence(object):
    """Normalized concatenation evidence for one received part."""

    __slots__ = (
        'reference', 'total', 'sequence', 'kind', 'udh', 'sar',
        'source', 'destination', 'source_ton', 'source_npi',
        'destination_ton', 'destination_npi', 'errors',
    )

    def __init__(self, reference=None, total=None, sequence=None, kind=None,
                 udh=None, sar=None, source=None, destination=None,
                 source_ton=None, source_npi=None,
                 destination_ton=None, destination_npi=None,
                 errors=None):
        self.reference = reference
        self.total = total
        self.sequence = sequence
        self.kind = kind
        self.udh = udh
        self.sar = sar
        self.source = source
        self.destination = destination
        self.source_ton = source_ton
        self.source_npi = source_npi
        self.destination_ton = destination_ton
        self.destination_npi = destination_npi
        self.errors = list(errors or ())

    @property
    def present(self):
        return self.kind != 'single' and self.reference is not None \
            and self.total is not None and self.sequence is not None

    @property
    def multipart(self):
        return self.kind != 'single'

    @property
    def fragment_key(self):
        return (self.source, self.source_ton, self.source_npi,
                self.destination, self.destination_ton, self.destination_npi,
                self.kind, self.reference, self.total)

    @property
    def valid(self):
        return not self.errors

    def as_dict(self):
        return {
            'reference': self.reference,
            'total': self.total,
            'sequence': self.sequence,
            'kind': self.kind,
            'udh': self.udh,
            'sar': self.sar,
            'source': self.source,
            'destination': self.destination,
            'source_ton': self.source_ton,
            'source_npi': self.source_npi,
            'destination_ton': self.destination_ton,
            'destination_npi': self.destination_npi,
            'errors': list(self.errors),
        }

    def __repr__(self):
        return 'FragmentEvidence(reference=%r, total=%r, sequence=%r, kind=%r)' % (
            self.reference, self.total, self.sequence, self.kind)


class PartOccurrence(object):
    """One physical receipt of a multipart segment.

    Duplicate segments are kept as separate occurrences.  ``payload`` is the
    user data after a UDH (when present), while ``raw_payload`` is the complete
    short message/message payload as received.
    """

    __slots__ = (
        'sequence', 'payload', 'raw_payload', 'encoding', 'evidence',
        'pdu', 'order', 'text',
    )

    def __init__(self, sequence, payload, raw_payload=None, encoding=None,
                 evidence=None, pdu=None, order=None, text=None):
        self.sequence = sequence
        self.payload = payload
        self.raw_payload = raw_payload if raw_payload is not None else payload
        self.encoding = encoding
        self.evidence = evidence
        self.pdu = pdu
        self.order = order
        self.text = text

    def as_dict(self):
        return {
            'sequence': self.sequence,
            'payload': self.payload,
            'raw_payload': self.raw_payload,
            'encoding': self.encoding,
            'order': self.order,
            'text': self.text,
            'evidence': self.evidence.as_dict() if self.evidence else None,
        }

    def __repr__(self):
        return 'PartOccurrence(sequence=%r, payload=%r, order=%r)' % (
            self.sequence, self.payload, self.order)


class ReassembledMessage(object):
    """A group of parts sharing source, destination and fragmentation data."""

    def __init__(self, source, destination, evidence=None, gap=DEFAULT_GAP,
                 message_id=None, source_addr=None, destination_addr=None):
        self.source = source
        self.destination = destination
        self.source_addr = source if source_addr is None else source_addr
        self.destination_addr = \
            destination if destination_addr is None else destination_addr
        self.evidence = evidence
        self.reference = evidence.reference if evidence is not None else None
        self.total = evidence.total if evidence is not None else None
        self.kind = evidence.kind if evidence is not None else None
        self.gap = gap
        self.message_id = message_id
        self.occurrences = []
        self._by_sequence = collections.OrderedDict()
        self._encodings = []

    @property
    def first_sequence(self):
        return self.sequences[0] if self.sequences else None

    @property
    def last_sequence(self):
        return self.sequences[-1] if self.sequences else None

    def add_occurrence(self, occurrence):
        self.occurrences.append(occurrence)
        self._by_sequence.setdefault(occurrence.sequence, []).append(occurrence)
        if occurrence.encoding is not None and occurrence.encoding not in self._encodings:
            self._encodings.append(occurrence.encoding)

    @property
    def sequences(self):
        return sorted(self._by_sequence)

    @property
    def present_sequences(self):
        return self.sequences

    @property
    def missing_sequences(self):
        if not self.total:
            return []
        return [number for number in range(1, self.total + 1)
                if number not in self._by_sequence]

    @property
    def missing(self):
        return self.missing_sequences

    @property
    def invalid_sequences(self):
        if not self.total:
            return []
        return [number for number in self.sequences
                if number < 1 or number > self.total]

    @property
    def conflicts(self):
        """Return ``{sequence: [distinct payloads]}`` for contradictory parts.

        More than one distinct payload at one sequence number is retained as a
        conflict.  The first occurrence is the one used for rendering; the
        other values remain available here for audit.
        """
        result = collections.OrderedDict()
        for sequence in self.sequences:
            payloads = []
            for occurrence in self._by_sequence[sequence]:
                if occurrence.payload not in payloads:
                    payloads.append(occurrence.payload)
            if len(payloads) > 1:
                result[sequence] = payloads
        return result

    @property
    def has_conflicts(self):
        return bool(self.conflicts)

    @property
    def duplicates(self):
        """Return ``{sequence: [occurrence, ...]}`` of repeated payloads.

        Only receipts equal to the first payload at that position are
        duplicates.  A different payload at the same position is a conflict
        and is reported by :attr:`conflicts` instead.
        """
        result = collections.OrderedDict()
        for sequence in self.sequences:
            occurrences = self._by_sequence[sequence]
            first = occurrences[0].payload
            repeated = [occurrence for occurrence in occurrences[1:]
                        if occurrence.payload == first]
            if repeated:
                result[sequence] = repeated
        return result

    @property
    def duplicate_sequences(self):
        return sorted(self.duplicates)

    @property
    def duplicate_occurrences(self):
        """Every repeated occurrence in arrival order."""
        return [occurrence
                for occurrences in self.duplicates.values()
                for occurrence in occurrences]

    @property
    def conflict_sequences(self):
        return sorted(self.conflicts)

    @property
    def conflict_payloads(self):
        return [payload
                for payloads in self.conflicts.values()
                for payload in payloads]

    @property
    def occurrence_count(self):
        return len(self.occurrences)

    @property
    def payloads(self):
        """All payload occurrences grouped by sequence number."""
        return collections.OrderedDict(
            (sequence, [occurrence.payload
                        for occurrence in self._by_sequence[sequence]])
            for sequence in self.sequences
        )

    @property
    def positional_segments(self):
        """Sequence slots from 1..total, using ``None`` for missing parts."""
        if not self.total:
            return [self._by_sequence[sequence][0].payload
                    for sequence in self.sequences]
        return [
            self._by_sequence[sequence][0].payload
            if sequence in self._by_sequence else None
            for sequence in range(1, self.total + 1)
        ]

    @property
    def segments(self):
        """First payload received for each sequence number."""
        return collections.OrderedDict(
            (sequence, self._by_sequence[sequence][0].payload)
            for sequence in self.sequences
        )

    @property
    def parts(self):
        """First payload received, ordered by sequence number."""
        return [self._by_sequence[sequence][0].payload
                for sequence in self.sequences]

    @property
    def parts_by_sequence(self):
        return self.segments

    @property
    def occurrence_payloads(self):
        return [occurrence.payload for occurrence in self.occurrences]

    unique_payloads = segments

    @property
    def complete(self):
        return bool(self.total) and not self.missing_sequences \
            and not self.invalid_sequences

    is_complete = complete

    @property
    def valid(self):
        if not self.evidence.valid:
            return False
        return all(occurrence.evidence.valid for occurrence in self.occurrences) \
            and not self.conflicts

    @property
    def data_coding(self):
        for occurrence in self.occurrences:
            if occurrence.encoding is not None:
                return occurrence.encoding
        return None

    @property
    def encodings(self):
        return list(self._encodings)

    @property
    def body(self):
        return self.render()

    payload = body
    content = body
    decoded = body

    @property
    def occurrence_texts(self):
        return [occurrence.text for occurrence in self.occurrences]

    @property
    def text(self):
        return self.render()

    @property
    def message(self):
        return self.render()

    def render(self, gap=None):
        """Return the decoded archive body.

        Missing sequence numbers are represented by ``gap`` (one marker per
        missing part) so a later part is never shifted into an earlier
        position.  When the whole sequence is present, the bytes of all parts
        are joined before decoding; this is what keeps a multi-byte character
        that crosses a part boundary intact.
        """
        if gap is None:
            gap = self.gap

        if not self.total:
            sequence_numbers = self.sequences
            if not sequence_numbers:
                return u''
            return self._decode_sequence(sequence_numbers)

        if self.complete:
            body = self._decode_sequence(range(1, self.total + 1))
            for occurrence in self.occurrences:
                occurrence.text = body
            return body

        chunks = []
        sequence = 1
        while sequence <= self.total:
            if sequence in self._by_sequence:
                run_end = sequence
                while run_end <= self.total and run_end in self._by_sequence:
                    run_end += 1
                chunks.append(self._decode_sequence(range(sequence, run_end)))
                sequence = run_end
            else:
                chunks.append(gap)
                sequence += 1

        # Parts outside 1..total cannot be placed without guessing.  Keep
        # their bytes after the declared sequence instead of dropping them,
        # and leave ``invalid_sequences`` for the caller to inspect.
        invalid = [number for number in self.sequences
                   if number < 1 or number > self.total]
        if invalid:
            chunks.append(self._decode_sequence(invalid))

        return u''.join(chunks)

    def _decode_sequence(self, sequence_numbers):
        data = b''.join(
            self._by_sequence[sequence][0].payload
            for sequence in sequence_numbers
        )
        return _decode_bytes(data, self.data_coding)

    def as_dict(self):
        return {
            'message_id': self.message_id,
            'source': self.source,
            'destination': self.destination,
            'reference': self.reference,
            'total': self.total,
            'kind': self.kind,
            'complete': self.complete,
            'body': self.body,
            'missing': self.missing_sequences,
            'invalid_sequences': self.invalid_sequences,
            'conflicts': self.conflicts,
            'duplicates': self.duplicates,
            'duplicate_sequences': self.duplicate_sequences,
            'occurrences': [occurrence.as_dict() for occurrence in self.occurrences],
            'segments': self.segments,
            'data_coding': self.data_coding,
            'encodings': self.encodings,
        }

    def keys(self):
        return list(self.as_dict())

    def __getitem__(self, name):
        return self.as_dict()[name]

    def __contains__(self, name):
        return name in self.as_dict()

    def get(self, name, default=None):
        return self.as_dict().get(name, default)

    def __repr__(self):
        return 'ReassembledMessage(source=%r, destination=%r, reference=%r, ' \
               'total=%r, received=%r, complete=%r)' % (
                   self.source, self.destination, self.reference, self.total,
                   len(self.occurrences), self.complete)


class ReassemblyResult(list):
    """List of reassembled messages with single-message convenience access.

    ``reassemble`` always returns a collection because one archive can contain
    several messages.  When exactly one message is present, attribute access
    such as ``result.body`` or ``result.complete`` is forwarded to that
    message, so simple callers do not need to index the result.
    """

    @property
    def messages(self):
        return list(self)

    @property
    def groups(self):
        return list(self)

    @property
    def bodies(self):
        return [message.body for message in self]

    @property
    def texts(self):
        return self.bodies

    def __getattr__(self, name):
        if name.startswith('__') or len(self) != 1:
            raise AttributeError(name)
        return getattr(self[0], name)


class MultipartReassembler(object):
    """Stateful collector for streamed or offline PDU archives.

    ``add`` returns the group the part belongs to.  Reference numbers are
    scoped by source and destination, so two senders can use the same
    reference number without mixing their messages.  The fragment evidence
    (UDH header, SAR parameters or explicit archive fields) is preserved on
    every occurrence and group.  A completed group does not absorb a later
    reuse of its reference; the later part starts a new group instead.
    """

    def __init__(self, gap=DEFAULT_GAP):
        self.gap = gap
        self._groups = []
        self._buckets = collections.OrderedDict()
        self._next_order = 0
        self._next_message_id = 1

    @property
    def messages(self):
        return list(self._groups)

    @property
    def groups(self):
        return self.messages

    @property
    def complete_messages(self):
        return [message for message in self._groups if message.complete]

    @property
    def incomplete_messages(self):
        return [message for message in self._groups if not message.complete]

    def add(self, pdu, new_message=False):
        """Add one PDU/dict and return its :class:`ReassembledMessage`."""
        normalized = _normalize(pdu)
        evidence = normalized['evidence']
        occurrence = normalized['occurrence']
        occurrence.order = self._next_order
        self._next_order += 1

        if not evidence.multipart:
            message = self._new_message(normalized, evidence)
            message.add_occurrence(occurrence)
            return message

        # UDH-8, UDH-16, SAR and explicit records use different header
        # conventions.  They must not cross-merge even when the numeric
        # reference, source and destination happen to match.
        bucket = self._buckets.setdefault(evidence.fragment_key, [])

        if new_message:
            message = None
        else:
            message = self._find_group(bucket, occurrence)

        if message is None:
            message = self._new_message(normalized, evidence, bucket=bucket)

        message.add_occurrence(occurrence)
        return message

    # Common spellings used by integrations.
    add_pdu = add
    push = add

    def add_many(self, pdus, new_message=False):
        return [self.add(pdu, new_message=new_message) for pdu in pdus]

    def extend(self, pdus):
        return self.add_many(pdus)

    def feed(self, pdu, new_message=False):
        return self.add(pdu, new_message=new_message)

    def finish(self):
        """Return all groups seen so far in first-seen order."""
        return list(self._groups)

    def clear(self):
        self._groups = []
        self._buckets = collections.OrderedDict()
        self._next_order = 0
        self._next_message_id = 1

    def _new_message(self, normalized, evidence, bucket=None):
        message = ReassembledMessage(
            source=evidence.source,
            destination=evidence.destination,
            evidence=evidence,
            gap=self.gap,
            message_id=self._next_message_id,
        )
        self._next_message_id += 1
        self._groups.append(message)
        if bucket is not None:
            bucket.append(message)
        return message

    def _find_group(self, bucket, occurrence):
        sequence = occurrence.sequence
        payload = occurrence.payload

        # An identical receipt of the same position is a duplicate occurrence
        # of the most recent matching generation, not a new message.
        for message in reversed(bucket):
            for existing in message._by_sequence.get(sequence, ()):
                if existing.payload == payload:
                    return message

        # Fill the oldest incomplete group that still has this position open.
        # FIFO keeps interleaved generations deterministic; completion is the
        # boundary at which a reused reference can start a new generation.
        for message in bucket:
            if not message.complete and sequence not in message._by_sequence:
                return message

        # The position exists with a different payload.  Preserve it in the
        # incomplete group as an explicit conflict instead of overwriting the
        # first occurrence.  A completed group is closed; later parts with the
        # same reference start a new message.
        for message in reversed(bucket):
            if not message.complete:
                return message

        return None


# Short, conventional aliases.
Reassembler = MultipartReassembler
MultipartMessage = ReassembledMessage


def reassemble(pdus, gap=DEFAULT_GAP, only_complete=False):
    """Reassemble an iterable of PDUs/dicts/raw parts into messages."""
    reassembler = MultipartReassembler(gap=gap)
    for pdu in pdus:
        if isinstance(pdu, bytes):
            pdu = {'short_message': pdu, 'esm_class': consts.SMPP_GSMFEAT_UDHI}
        reassembler.add(pdu)
    messages = ReassemblyResult(reassembler.messages)
    if only_complete:
        messages = ReassemblyResult(
            message for message in messages if message.complete)
    return messages


def reassemble_message(pdus, **kwargs):
    """Return the first group produced from ``pdus``."""
    messages = reassemble(pdus, **kwargs)
    return messages[0] if messages else None


def reassemble_messages(pdus, **kwargs):
    return reassemble(pdus, **kwargs)


def reassemble_text(pdus, **kwargs):
    """Return the rendered body of every reassembled group."""
    return [message.body for message in reassemble(pdus, **kwargs)]


def reassemble_joined(pdus, **kwargs):
    """Return the rendered bodies joined with a newline."""
    return u'\n'.join(reassemble_text(pdus, **kwargs))


def reassemble_one(pdus, **kwargs):
    """Return a single group, raising if the archive yields zero or many."""
    messages = reassemble(pdus, **kwargs)
    if len(messages) != 1:
        raise ValueError('expected exactly one message, got %d' % len(messages))
    return messages[0]


def reassemble_parts(parts, encoding=consts.SMPP_ENCODING_DEFAULT,
                     source=None, destination=None, gap=DEFAULT_GAP,
                     only_complete=False):
    """Reassemble raw short-message payloads (for example ``make_parts``).

    Payloads that already contain a concatenation UDH are detected
    automatically.  Bare parts can be paired with explicit ``reference``,
    ``total`` and ``sequence`` values by passing dictionaries instead.
    """
    pdus = []
    for part in parts:
        if isinstance(part, dict):
            record = dict(part)
            record.setdefault('data_coding', encoding)
            record.setdefault('esm_class', 0)
            pdus.append(record)
            continue
        pdus.append({
            'source_addr': source or b'',
            'destination_addr': destination or b'',
            'short_message': part,
            'esm_class': 0,
            'data_coding': encoding,
        })
    return reassemble(pdus, gap=gap, only_complete=only_complete)


def join_parts(parts, **kwargs):
    """Return the decoded bodies for raw short-message payloads."""
    return [message.body for message in reassemble_parts(parts, **kwargs)]


def _normalize(pdu):
    source = _address(pdu, (
        'source_addr', 'source', 'src', 'from', 'originator', 'sender',
    ))
    destination = _address(pdu, (
        'destination_addr', 'destination', 'dest', 'dst', 'to', 'recipient',
        'receiver',
    ))
    source_ton = _integer(_first(pdu, ('source_addr_ton', 'source_ton')))
    source_npi = _integer(_first(pdu, ('source_addr_npi', 'source_npi')))
    destination_ton = _integer(_first(pdu, (
        'dest_addr_ton', 'destination_ton', 'destination_addr_ton',
    )))
    destination_npi = _integer(_first(pdu, (
        'dest_addr_npi', 'destination_npi', 'destination_addr_npi',
    )))
    encoding = _encoding(pdu)
    esm_class = _integer(_first(pdu, ('esm_class', 'esm_class_flag'))) or 0

    short_message = _payload_bytes(
        _first(pdu, ('short_message', 'payload', 'user_data', 'content')),
        encoding,
    )
    message_payload = _first(pdu, ('message_payload', 'message', 'body', 'text'))
    if message_payload:
        payload = _payload_bytes(message_payload, encoding)
    else:
        payload = short_message

    explicit_udh = _first(pdu, ('udh', 'user_data_header'))
    if explicit_udh is not None:
        explicit_udh = _octets(explicit_udh)
        payload = explicit_udh + payload

    udh, user_data, udh_evidence, udh_errors = _parse_udh(
        payload, bool(esm_class & UDH_INDICATOR))

    sar = _sar_evidence(pdu)
    evidence = _choose_evidence(
        source, destination, udh_evidence, sar, udh_errors)
    evidence.source_ton = source_ton
    evidence.source_npi = source_npi
    evidence.destination_ton = destination_ton
    evidence.destination_npi = destination_npi
    evidence = _explicit_evidence(pdu, evidence)

    occurrence = PartOccurrence(
        sequence=evidence.sequence,
        payload=user_data,
        raw_payload=payload,
        encoding=encoding,
        evidence=evidence,
        pdu=pdu,
        order=0,
    )
    return {'evidence': evidence, 'occurrence': occurrence}


def _choose_evidence(source, destination, udh_evidence, sar, udh_errors):
    errors = list(udh_errors)

    if udh_evidence is not None:
        evidence = FragmentEvidence(
            reference=udh_evidence[0],
            total=udh_evidence[1],
            sequence=udh_evidence[2],
            kind=udh_evidence[3],
            udh=udh_evidence[4],
            sar=sar,
            source=source,
            destination=destination,
            errors=errors,
        )
        if sar is not None and (
                sar[0] != evidence.reference
                or sar[1] != evidence.total
                or sar[2] != evidence.sequence):
            evidence.errors.append('udh_sar_mismatch')
        _validate_evidence(evidence)
        return evidence

    if sar is not None:
        evidence = FragmentEvidence(
            reference=sar[0],
            total=sar[1],
            sequence=sar[2],
            kind='sar',
            udh=None,
            sar=sar,
            source=source,
            destination=destination,
            errors=errors,
        )
        _validate_evidence(evidence)
        return evidence

    return FragmentEvidence(
        reference=None,
        total=1,
        sequence=1,
        kind='single',
        source=source,
        destination=destination,
        errors=errors,
    )


def _validate_evidence(evidence):
    if evidence.total is None or evidence.total < 1:
        evidence.errors.append('invalid_total')
    if evidence.sequence is None or evidence.sequence < 1:
        evidence.errors.append('invalid_sequence')
    elif evidence.total is not None and evidence.sequence > evidence.total:
        evidence.errors.append('invalid_sequence')


def _explicit_evidence(pdu, evidence):
    """Allow callers to supply fragment metadata for archive records.

    Some offline archives store the decoded header fields instead of the raw
    UDH.  These names are intentionally permissive so both SMPP and GSM
    terminology work.
    """
    if evidence.multipart:
        return evidence

    reference = _first(pdu, ('reference', 'ref', 'concat_ref',
                             'concat_reference', 'reference_number',
                             'ref_number'))
    total = _first(pdu, ('total', 'total_parts', 'part_count', 'parts',
                         'total_segments', 'segment_count',
                         'total_parts_count'))
    sequence = _first(pdu, ('sequence', 'seq', 'part', 'part_number', 'part_no',
                            'segment', 'segment_number', 'sequence_number'))
    kind = _first(pdu, ('fragment_kind', 'fragment_type', 'kind'))

    explicit_udh = _first(pdu, ('udh', 'user_data_header'))
    if explicit_udh is not None and reference is None:
        parsed_udh, _user_data, parsed, errors = _parse_udh(
            _octets(explicit_udh), True)
        if parsed is not None:
            reference, total, sequence = parsed[0], parsed[1], parsed[2]
            evidence.errors.extend(errors)

    reference = _integer(reference)
    total = _integer(total)
    sequence = _integer(sequence)
    if reference is None and total is None and sequence is None:
        return evidence

    if kind not in ('udh8', 'udh16', 'sar', 'explicit'):
        kind = 'explicit'

    evidence = FragmentEvidence(
        reference=reference,
        total=total if total is not None else 1,
        sequence=sequence if sequence is not None else 1,
        kind=kind,
        source=evidence.source,
        destination=evidence.destination,
        source_ton=evidence.source_ton,
        source_npi=evidence.source_npi,
        destination_ton=evidence.destination_ton,
        destination_npi=evidence.destination_npi,
        errors=list(evidence.errors),
    )
    _validate_evidence(evidence)
    return evidence


def _parse_udh(payload, has_udhi):
    """Return ``(header, user_data, concat_evidence, errors)``."""
    if not payload:
        return b'', payload, None, []

    if not has_udhi and not _looks_like_concat_udh(payload):
        return b'', payload, None, []

    header_length = six.indexbytes(payload, 0)
    if header_length == 0:
        return b'', payload, None, []

    header_end = header_length + 1
    errors = []
    if header_end > len(payload):
        errors.append('truncated_udh')
        header_end = len(payload)
    header = payload[:header_end]
    user_data = payload[header_end:]

    evidence = None
    position = 1
    while position < header_end:
        if position >= len(header):
            errors.append('truncated_udh_iei')
            break
        iei = six.indexbytes(header, position)
        position += 1
        if position >= len(header):
            errors.append('truncated_udh_length')
            break
        length = six.indexbytes(header, position)
        position += 1
        if position + length > len(header):
            errors.append('truncated_udh_value')
            value = header[position:]
        else:
            value = header[position:position + length]
        position += length

        if iei == UDH_CONCAT_8 and len(value) >= 3:
            if evidence is not None:
                errors.append('multiple_concat_ies')
                continue
            evidence = (
                six.indexbytes(value, 0),
                six.indexbytes(value, 1),
                six.indexbytes(value, 2),
                'udh8',
                header,
            )
        elif iei == UDH_CONCAT_16 and len(value) >= 4:
            if evidence is not None:
                errors.append('multiple_concat_ies')
                continue
            evidence = (
                struct.unpack('>H', value[:2])[0],
                six.indexbytes(value, 2),
                six.indexbytes(value, 3),
                'udh16',
                header,
            )

    return header, user_data, evidence, errors


def _looks_like_concat_udh(payload):
    """Detect a UDH even when a sender omitted the UDHI esm_class bit."""
    if len(payload) < 2:
        return False
    header_length = six.indexbytes(payload, 0)
    header_end = header_length + 1
    if header_end > len(payload):
        return False
    position = 1
    found = False
    while position < header_end:
        if position + 1 >= header_end:
            return False
        iei = six.indexbytes(payload, position)
        length = six.indexbytes(payload, position + 1)
        position += 2
        if position + length > header_end:
            return False
        if (iei == UDH_CONCAT_8 and length >= 3) or \
                (iei == UDH_CONCAT_16 and length >= 4):
            found = True
        position += length
    return found


def _sar_evidence(pdu):
    reference = _integer(_value(pdu, 'sar_msg_ref_num'))
    total = _integer(_value(pdu, 'sar_total_segments'))
    sequence = _integer(_value(pdu, 'sar_segment_seqnum'))
    if reference is None or total is None or sequence is None:
        return None
    return reference, total, sequence


def _decode_bytes(data, encoding):
    if encoding == consts.SMPP_ENCODING_DEFAULT:
        from smpplib.gsm import gsm_decode
        return gsm_decode(data)
    if encoding == consts.SMPP_ENCODING_IA5:
        return data.decode('ascii', 'replace')
    if encoding == consts.SMPP_ENCODING_ISO88591:
        return data.decode('iso-8859-1', 'replace')
    if encoding == consts.SMPP_ENCODING_ISO10646:
        # Join first, decode once: a UTF-16 code unit or surrogate pair may
        # straddle two parts.
        return data.decode('utf-16-be', 'replace')
    if encoding in (consts.SMPP_ENCODING_BINARY, consts.SMPP_ENCODING_BINARY2):
        return data.decode('latin-1', 'replace')
    return data.decode('utf-8', 'replace')


def _value(pdu, name, default=None):
    if isinstance(pdu, dict):
        return pdu.get(name, default)
    return getattr(pdu, name, default)


def _first(pdu, names):
    for name in names:
        value = _value(pdu, name)
        if value is not None:
            return value
    return None


def _address(pdu, names):
    value = _first(pdu, names)
    if value is None:
        return u''
    if isinstance(value, six.text_type):
        return value
    if isinstance(value, (int, long_types)):
        return six.text_type(value)
    return bytes(value).decode('utf-8', 'replace')


def _encoding(pdu):
    value = _first(pdu, ('data_coding', 'encoding', 'encoding_flag'))
    if isinstance(value, six.string_types):
        return _ENCODING_NAMES.get(value.lower(), value)
    return _integer(value)


_ENCODING_NAMES = {
    'default': consts.SMPP_ENCODING_DEFAULT,
    'gsm': consts.SMPP_ENCODING_DEFAULT,
    'gsm7': consts.SMPP_ENCODING_DEFAULT,
    'ascii': consts.SMPP_ENCODING_IA5,
    'ia5': consts.SMPP_ENCODING_IA5,
    'latin1': consts.SMPP_ENCODING_ISO88591,
    'latin-1': consts.SMPP_ENCODING_ISO88591,
    'iso-8859-1': consts.SMPP_ENCODING_ISO88591,
    'iso88591': consts.SMPP_ENCODING_ISO88591,
    'ucs2': consts.SMPP_ENCODING_ISO10646,
    'ucs-2': consts.SMPP_ENCODING_ISO10646,
    'utf-16-be': consts.SMPP_ENCODING_ISO10646,
    'utf16be': consts.SMPP_ENCODING_ISO10646,
    'iso-10646': consts.SMPP_ENCODING_ISO10646,
    'binary': consts.SMPP_ENCODING_BINARY,
}


def _octets(value):
    if value is None:
        return b''
    if isinstance(value, six.text_type):
        try:
            return value.encode('latin-1')
        except UnicodeError:
            return value.encode('utf-8')
    if isinstance(value, memoryview):
        return value.tobytes()
    return bytes(value)


def _payload_bytes(value, encoding):
    """Turn a short-message value into octets.

    Text archives sometimes store the already-decoded body.  For UCS-2 the
    SMPP representation is UTF-16BE; for other text encodings the codec's
    byte representation is the most faithful archive form.
    """
    if value is None:
        return b''
    if not isinstance(value, six.text_type):
        return _octets(value)

    if encoding == consts.SMPP_ENCODING_ISO10646:
        return value.encode('utf-16-be')
    if encoding == consts.SMPP_ENCODING_ISO88591:
        return value.encode('iso-8859-1', 'replace')
    if encoding == consts.SMPP_ENCODING_IA5:
        return value.encode('ascii', 'replace')
    if encoding in (consts.SMPP_ENCODING_BINARY, consts.SMPP_ENCODING_BINARY2):
        return _octets(value)
    return _octets(value)


def _integer(value):
    if value is None or value is True or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
