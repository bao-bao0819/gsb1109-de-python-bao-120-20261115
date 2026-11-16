"""Columnar (flattened) views of OSC message arguments.

This module splits the arguments of an OSC message - including the contents
of nested OSC arrays - into a flat list of column records.  Each record
keeps the nested path of the argument, its OSC type tag and the byte range
it occupies in the original packet, so that columns can later be turned
back into an equivalent OSC message.
"""

import logging
import struct

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Sequence, Tuple

from pythonosc import osc_bundle, osc_message
from pythonosc.parsing import ntp, osc_types

_BUNDLE_PREFIX = b"#bundle\x00"


class ParseError(Exception):
    """Base exception raised when a datagram could not be split into columns."""


class BuildError(Exception):
    """Base exception raised when columns could not be rebuilt into a message."""


@dataclass(frozen=True)
class ArgumentColumn:
    """A single scalar OSC argument flattened out of a message.

    Attributes:
        path: Index tuple locating the argument inside nested arrays,
            e.g. ``(1, 0, 2)`` for the third element of the first array
            nested inside the second top-level argument.
        type_tag: The OSC type tag character of the argument.
        value: The decoded Python value.  ``True``/``False``/``None`` for
            the payload-less ``T``/``F``/``N`` tags and ``bytes`` for blobs
            (blobs are never decoded as strings).
        start: Offset of the argument's data within the original packet.
        end: Offset right after the argument's data.  Equals ``start`` for
            payload-less types, which still occupy a column position.
    """

    path: Tuple[int, ...]
    type_tag: str
    value: Any
    start: int
    end: int


@dataclass(frozen=True)
class MessageColumns:
    """The columnar view of a single OSC message.

    Attributes:
        address: The OSC address pattern of the message.
        columns: The flattened argument columns, in packet order.
        start: Offset of the message within the original packet.
        end: Offset right after the message within the original packet.
    """

    address: str
    columns: Tuple[ArgumentColumn, ...]
    start: int
    end: int


def split_message(dgram: bytes, base: int = 0) -> MessageColumns:
    """Split the arguments of a single OSC message datagram into columns.

    Args:
        dgram: The raw datagram of one OSC message.
        base: Offset of ``dgram`` within the original packet, used as the
            origin for the column ranges.

    Raises:
        ParseError: if the datagram could not be parsed, e.g. on an
            unbalanced array closing bracket.
    """
    try:
        address, index = osc_types.get_string(dgram, 0)
        columns = []  # type: List[ArgumentColumn]
        if not dgram[index:]:
            return MessageColumns(address, tuple(columns), base, base + len(dgram))

        type_tag, index = osc_types.get_string(dgram, index)
        if not type_tag.startswith(","):
            raise ParseError(
                f"OSC Type Tag String must start with a comma, got: {type_tag}"
            )

        path_prefix = []  # type: List[int]
        index_stack = [0]  # type: List[int]
        for tag in type_tag[1:]:
            if tag == "[":  # Array start.
                path_prefix.append(index_stack[-1])
                index_stack.append(0)
                continue
            if tag == "]":  # Array stop.
                if len(index_stack) < 2:
                    raise ParseError(
                        f"Unexpected closing bracket in type tag: {type_tag}"
                    )
                index_stack.pop()
                path_prefix.pop()
                index_stack[-1] += 1
                continue

            start = index
            value = None  # type: Any
            if tag == "i":  # Integer.
                value, index = osc_types.get_int(dgram, index)
            elif tag == "h":  # Int64.
                value, index = osc_types.get_int64(dgram, index)
            elif tag == "f":  # Float.
                value, index = osc_types.get_float(dgram, index)
            elif tag == "d":  # Double.
                value, index = osc_types.get_double(dgram, index)
            elif tag == "s":  # String.
                value, index = osc_types.get_string(dgram, index)
            elif tag == "b":  # Blob, kept as bytes and not as a string.
                value, index = osc_types.get_blob(dgram, index)
            elif tag == "r":  # RGBA.
                value, index = osc_types.get_rgba(dgram, index)
            elif tag == "m":  # MIDI.
                value, index = osc_types.get_midi(dgram, index)
            elif tag == "t":  # OSC time tag.
                value, index = osc_types.get_timetag(dgram, index)
            elif tag == "T":  # True, no payload but still a position.
                value = True
            elif tag == "F":  # False, no payload but still a position.
                value = False
            elif tag == "N":  # Nil, no payload but still a position.
                value = None
            # TODO: Support more exotic types as described in the specification.
            else:
                logging.warning(f"Unhandled parameter type: {tag}")
                continue
            columns.append(
                ArgumentColumn(
                    path=tuple(path_prefix) + (index_stack[-1],),
                    type_tag=tag,
                    value=value,
                    start=base + start,
                    end=base + index,
                )
            )
            index_stack[-1] += 1
        if len(index_stack) != 1:
            raise ParseError(f"Missing closing bracket in type tag: {type_tag}")
        return MessageColumns(address, tuple(columns), base, base + len(dgram))
    except osc_types.ParseError as pe:
        raise ParseError("Found incorrect datagram, ignoring it", pe)


def split_packet(dgram: bytes) -> List[MessageColumns]:
    """Split every message of an OSC packet into columnar views.

    Bundle elements are size-prefixed, so a malformed message (e.g. one
    with an unbalanced array closing bracket) is skipped with a warning
    and does not swallow the messages that follow it in the packet.

    Raises:
        ParseError: if the packet itself could not be parsed.
    """
    if osc_bundle.OscBundle.dgram_is_bundle(dgram):
        return _split_bundle(dgram, 0)
    if osc_message.OscMessage.dgram_is_message(dgram):
        return [split_message(dgram)]
    raise ParseError("Packet is neither an OSC message nor an OSC bundle")


def _split_bundle(dgram: bytes, base: int) -> List[MessageColumns]:
    """Split the elements of a bundle datagram, skipping malformed ones."""
    messages = []  # type: List[MessageColumns]
    index = len(_BUNDLE_PREFIX)
    try:
        _, index = osc_types.get_date(dgram, index)
        while dgram[index:]:
            # An OSC Bundle Element consists of its size and its contents.
            content_size, index = osc_types.get_int(dgram, index)
            if content_size <= 0 or content_size % 4:
                raise ParseError(f"Invalid bundle element size: {content_size}")
            if content_size > len(dgram) - index:
                raise ParseError("Bundle element exceeds the datagram length")
            content_dgram = dgram[index : index + content_size]
            try:
                if osc_bundle.OscBundle.dgram_is_bundle(content_dgram):
                    messages.extend(_split_bundle(content_dgram, base + index))
                elif osc_message.OscMessage.dgram_is_message(content_dgram):
                    messages.append(split_message(content_dgram, base + index))
                else:
                    logging.warning(
                        f"Could not identify content type of dgram {content_dgram!r}"
                    )
            except ParseError as pe:
                logging.warning(f"Skipping malformed OSC element: {pe}")
            index += content_size
    except osc_types.ParseError as pe:
        raise ParseError(f"Could not parse bundle: {pe}")
    return messages


def build_message(
    address: str, columns: Sequence[ArgumentColumn]
) -> osc_message.OscMessage:
    """Rebuild an OSC message from an address and columnar records.

    The rebuilt message holds the same type tag sequence and values as the
    message the columns were split from.  Note that empty arrays produce no
    columns and therefore cannot be rebuilt.

    Raises:
        BuildError: if a column path, type tag or value cannot be encoded.
    """
    tags = []  # type: List[str]
    payload = b""
    prev_chain = []  # type: List[Tuple[int, ...]]
    for column in columns:
        if not column.path:
            raise BuildError("Column path cannot be empty")
        # The ancestor arrays of this column, identified by their own paths.
        chain = [column.path[:i] for i in range(1, len(column.path))]
        common = 0
        while (
            common < len(chain)
            and common < len(prev_chain)
            and chain[common] == prev_chain[common]
        ):
            common += 1
        tags.append("]" * (len(prev_chain) - common))
        tags.append("[" * (len(chain) - common))
        tags.append(column.type_tag)
        payload += _write_value(column.type_tag, column.value)
        prev_chain = chain
    tags.append("]" * len(prev_chain))

    dgram = osc_types.write_string(address)
    dgram += osc_types.write_string("," + "".join(tags))
    dgram += payload
    try:
        return osc_message.OscMessage(dgram)
    except osc_message.ParseError as pe:
        raise BuildError(f"Could not rebuild message: {pe}")


def _write_value(type_tag: str, value: Any) -> bytes:
    """Returns the datagram payload for a single column value."""
    try:
        if type_tag == "i":
            return osc_types.write_int(value)
        if type_tag == "h":
            return osc_types.write_int64(value)
        if type_tag == "f":
            return osc_types.write_float(value)
        if type_tag == "d":
            return osc_types.write_double(value)
        if type_tag == "s":
            return osc_types.write_string(value)
        if type_tag == "b":
            # Blobs are binary, never strings; an empty blob is a zero length.
            return osc_types.write_blob(value) if value else osc_types.write_int(0)
        if type_tag == "r":
            return osc_types.write_rgba(value)
        if type_tag == "m":
            return osc_types.write_midi(value)
        if type_tag == "t":
            return _write_timetag(value)
        if type_tag in ("T", "F", "N"):
            return b""
    except osc_types.BuildError as be:
        raise BuildError(f"Could not encode column value: {be}")
    raise BuildError(f"Unsupported column type tag: {type_tag}")


def _write_timetag(value: Tuple[datetime, int]) -> bytes:
    """Returns the datagram for a timetag column value (utc, fraction)."""
    utc, fraction = value
    epoch = datetime.combine(ntp._NTP_EPOCH, datetime.min.time())
    seconds = int((utc - epoch).total_seconds())
    return struct.pack(">Q", (seconds << 32) | (fraction & 0xFFFFFFFF))
