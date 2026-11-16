# Pydifact - a python edifact library
#
# Copyright (c) 2017-2024 Christian González
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""Trace decoded EDI text back to the original byte stream.

When a downstream consumer finds a bad field, it must be able to locate the
original EDI bytes it came from. This module decodes a byte stream while
recording which bytes produced which characters, so every parsed component
keeps its source range through the encoding conversion.
"""

import codecs
from bisect import bisect_right
from typing import NamedTuple

# Placeholder inserted for bytes that cannot be decoded. It is a private-use
# character, so it cannot collide with regular EDI content, and it never
# reaches callers: its presence always raises an `EDIEncodingError`.
DECODE_ERROR_SENTINEL = "\ue000"


class DecodeError(NamedTuple):
    """A sequence of bytes that could not be decoded."""

    byte_start: int
    byte_end: int
    reason: str
    # index of the sentinel character that marks this error in the decoded text
    char_pos: int


class DecodedMessage(NamedTuple):
    """The result of `decode_with_source_map`."""

    text: str
    source_map: "SourceMap"
    errors: list[DecodeError]


# A run maps the characters [char_start, char_end) to the bytes
# [byte_start, byte_end). In a linear run every character corresponds to
# exactly one byte; in an atomic run the whole byte span belongs to the
# whole character span (e.g. one multi-byte character).
_Run = tuple[int, int, int, int, bool]


class SourceMap:
    """Maps character offsets in a decoded message to byte offsets in the
    original byte stream.

    The map is a list of runs. A *linear* run covers single-byte characters
    (``byte = byte_start + (char - char_start)``). An *atomic* run covers one
    decode unit of several bytes that turned into one or more characters
    (e.g. a UTF-8 multi-byte character); since the codec does not reveal how
    the bytes distribute over the characters, every character of an atomic
    run maps to the whole byte span of the run - no per-character
    correspondence is fabricated.
    """

    def __init__(self, runs: list[_Run], char_length: int, byte_length: int) -> None:
        self._runs = runs
        self._char_starts = [run[0] for run in runs]
        self.char_length = char_length
        self.byte_length = byte_length

    def byte_span(self, char_start: int, char_end: int) -> tuple[int, int]:
        """Return the byte range ``(byte_start, byte_end)`` that produced the
        characters ``[char_start, char_end)``.

        An empty character range returns a zero-length byte range at the
        corresponding position.
        """
        if not 0 <= char_start <= char_end <= self.char_length:
            raise ValueError(
                f"Character range {char_start}..{char_end} is outside the "
                f"decoded message of {self.char_length} characters."
            )
        if char_start == char_end:
            position = self._byte_at(char_start)
            return (position, position)
        return (self._byte_at(char_start), self._byte_end_at(char_end - 1))

    def _run_for(self, char_index: int) -> _Run | None:
        index = bisect_right(self._char_starts, char_index) - 1
        if index >= 0:
            run = self._runs[index]
            if char_index < run[1]:
                return run
        return None

    def _byte_at(self, char_index: int) -> int:
        """The byte offset at which the given character starts."""
        if char_index >= self.char_length:
            return self.byte_length
        run = self._run_for(char_index)
        if run is None:
            # A character without recorded bytes (e.g. right after a stripped
            # BOM): point at the start of the next run.
            index = bisect_right(self._char_starts, char_index)
            if index < len(self._runs):
                return self._runs[index][2]
            return self.byte_length
        char_start, _, byte_start, _, atomic = run
        if atomic:
            return byte_start
        return byte_start + (char_index - char_start)

    def _byte_end_at(self, char_index: int) -> int:
        """The byte offset just after the given character."""
        run = self._run_for(char_index)
        if run is None:
            return self._byte_at(char_index + 1)
        char_start, _, byte_start, byte_end, atomic = run
        if atomic:
            return byte_end
        return byte_start + (char_index - char_start) + 1


def decode_with_source_map(data: bytes, encoding: str) -> DecodedMessage:
    """Decode bytes, recording where each character came from.

    Returns the decoded text, a `SourceMap` relating character offsets to
    byte offsets, and a list of `DecodeError`. Each undecodable byte
    sequence is replaced by exactly one `DECODE_ERROR_SENTINEL` character -
    never by U+FFFD or another replacement character - so the error stays
    traceable and callers can isolate the affected field instead of silently
    working on with altered values.

    Raises:
        LookupError: If the encoding is not recognized.
    """
    codecs.lookup(encoding)

    # Fast path: if the whole input decodes strictly and every byte that
    # occurs in it maps to exactly one character on its own, characters and
    # bytes correspond 1:1 and the map is the identity.
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        text = None
    if (
        text is not None
        and len(text) == len(data)
        and _is_bytewise_decodable(data, encoding)
    ):
        runs: list[_Run] = [(0, len(text), 0, len(data), False)] if text else []
        return DecodedMessage(text, SourceMap(runs, len(text), len(data)), [])

    return _decode_incrementally(data, encoding)


def _is_bytewise_decodable(data: bytes, encoding: str) -> bool:
    """Check whether every byte occurring in `data` decodes to exactly one
    character on its own (i.e. the encoding is single-byte for this input)."""
    for byte_value in set(data):
        try:
            if len(bytes((byte_value,)).decode(encoding)) != 1:
                return False
        except UnicodeDecodeError:
            return False
    return True


def _decode_incrementally(data: bytes, encoding: str) -> DecodedMessage:
    """Decode byte by byte, recording one source run per decode unit."""
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    text_parts: list[str] = []
    runs: list[_Run] = []
    errors: list[DecodeError] = []
    char_pos = 0

    def emit(text: str, byte_start: int, byte_end: int) -> None:
        nonlocal char_pos
        if not text:
            return
        char_end = char_pos + len(text)
        if len(text) == 1 and byte_end - byte_start == 1:
            # a 1:1 character extends a linear run
            last = runs[-1] if runs else None
            if last is not None and not last[4] and last[3] == byte_start:
                runs[-1] = (last[0], char_end, last[2], byte_end, False)
            else:
                runs.append((char_pos, char_end, byte_start, byte_end, False))
        else:
            # several bytes for one or more characters: an atomic run
            runs.append((char_pos, char_end, byte_start, byte_end, True))
        text_parts.append(text)
        char_pos = char_end

    # Bytes fed to the decoder that it has not turned into output yet.
    pending = bytearray()
    # Bytes to feed again after a decode error, as (byte, position) pairs.
    refeed: list[tuple[int, int]] = []
    index = 0
    size = len(data)
    while index < size or refeed:
        if refeed:
            byte_value, position = refeed.pop()
        else:
            byte_value = data[index]
            position = index
            index += 1
        pending.append(byte_value)
        try:
            output = decoder.decode(bytes((byte_value,)))
        except UnicodeDecodeError as exc:
            # exc.start/end are relative to the decoder's internal buffer,
            # which is exactly `pending`.
            base = position + 1 - len(pending)
            byte_start = base + exc.start
            byte_end = base + exc.end
            errors.append(DecodeError(byte_start, byte_end, exc.reason, char_pos))
            emit(DECODE_ERROR_SENTINEL, byte_start, byte_end)
            remainder = pending[exc.end :]
            decoder.reset()
            pending.clear()
            # bytes after the bad unit were not consumed: feed them again
            for k in range(len(remainder) - 1, -1, -1):
                refeed.append((remainder[k], base + exc.end + k))
            continue
        if output:
            emit(output, position + 1 - len(pending), position + 1)
            pending.clear()

    # Flush the decoder's internal buffer. Whatever is still pending at this
    # point is a truncated unit at the end of the input.
    try:
        output = decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        base = size - len(pending)
        errors.append(
            DecodeError(base + exc.start, base + exc.end, exc.reason, char_pos)
        )
        emit(DECODE_ERROR_SENTINEL, base + exc.start, base + exc.end)
        output = ""
    if output:
        emit(output, size - len(pending), size)

    return DecodedMessage("".join(text_parts), SourceMap(runs, char_pos, size), errors)
