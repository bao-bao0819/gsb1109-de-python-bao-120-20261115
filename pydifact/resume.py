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

"""Resumable, incremental reading of an EDIFACT interchange.

When a collecting endpoint reads an EDIFACT interchange from an unreliable
stream, the connection may break at any byte. `ResumableReader` continues
reading after such a disconnect instead of starting over:

* Messages are committed one by one. A message is committed only when its
  UNT trailer checks out: the segment count (0074) must equal the actual
  number of segments from UNH to UNT (both included), and the message
  reference number (0062) must match the UNH header.
* At any point, `last_complete_message` reports the last committed message
  and `unconfirmed_tail` the raw EDI text that was received but not yet
  committed - exactly the part the sender has to resend after a reconnect.
* If the stream ends (`finish()`) without the interchange trailer UNZ, the
  whole batch is recorded as `unclosed`; already committed messages stay
  committed.
* Resending the unconfirmed tail via `resume()` cannot rewrite previously
  committed messages: only the uncommitted tail is discarded and parsed
  again, so every message is committed exactly once.
"""

import logging

from pydifact.control import Characters
from pydifact.exceptions import EDISyntaxError
from pydifact.parser import Parser
from pydifact.segmentcollection import Message
from pydifact.segments import Segment
from pydifact.tokenizer import Tokenizer

logger = logging.getLogger(__name__)


class ResumableReader:
    """Incrementally read an EDIFACT interchange across reconnects.

    Feed the stream with `feed()` in arbitrarily sized chunks. Each call
    returns the messages that were committed by that chunk (a message is
    committed when its UNT checks pass). After a disconnect, query
    `last_complete_message` and `unconfirmed_tail`, let the sender resend
    the tail, and continue with `resume()`:

    >>> reader = ResumableReader()
    >>> reader.feed("UNB+UNOC:1+1234+3333+200102:2212+42'UNH+1+PAORES:93:1:IA'UNT+2+1'UNH+2+PAO")
    [...]
    >>> reader.last_complete_message.reference_number
    '1'
    >>> reader.unconfirmed_tail
    'UNH+2+PAO'
    >>> reader.resume(reader.unconfirmed_tail + "RES:93:1:IA'UNT+2+2'UNZ+2+42'")
    [...]
    >>> reader.closed
    True

    Args:
        parser: A parser to convert tokens to segments; defaults to `Parser`.
        characters: The set of control characters to use if there is no UNA
            segment. Only used when no `parser` is given.
        validate: Whether to validate the parsed segments. Only used when no
            `parser` is given.
        warn_non_conformant: Whether to emit a `NonConformantEDIWarning` for
            accepted deviations from the EDIFACT specification. Only used
            when no `parser` is given.

    Attributes:
        characters: The control characters in effect (updated by a UNA
            segment, if present).
    """

    def __init__(
        self,
        parser: Parser | None = None,
        characters: Characters | None = None,
        validate: bool = True,
        warn_non_conformant: bool = True,
    ) -> None:
        if parser is None:
            parser = Parser(
                characters=characters,
                validate=validate,
                warn_non_conformant=warn_non_conformant,
            )
        self.parser = parser
        self.characters = parser.characters

        # raw text received but not yet parsed into segments
        self._buffer = ""
        # raw text of parsed but not yet committed segments
        self._tail_raw = ""

        self._messages: list[Message] = []
        # UNH segment and body segments of the message currently being collected
        self._unh: Segment | None = None
        self._candidate: list[Segment] = []

        self._una_checked = False
        self._started = False  # UNB seen
        self._closed = False  # UNZ seen
        self._finished = False  # finish() called
        self._unclosed = False  # finish() called without UNZ

    @property
    def messages(self) -> list[Message]:
        """The messages committed so far, in order of their UNT trailers."""
        return list(self._messages)

    @property
    def last_complete_message(self) -> Message | None:
        """The last committed message, or None if no message committed yet."""
        return self._messages[-1] if self._messages else None

    @property
    def unconfirmed_tail(self) -> str:
        """The raw EDI text received but not yet committed.

        This is exactly the part the sender has to resend after a reconnect.
        It never contains a committed message, so resending it cannot
        rewrite previous messages.
        """
        return self._tail_raw + self._buffer

    @property
    def closed(self) -> bool:
        """Whether the interchange trailer UNZ has been received."""
        return self._closed

    @property
    def unclosed(self) -> bool:
        """Whether the stream ended without the interchange trailer UNZ.

        Only set by `finish()`; committed messages stay committed.
        """
        return self._unclosed

    def feed(self, data: str) -> list[Message]:
        """Append stream data and commit every message that completes.

        Args:
            data: The next chunk of the EDI stream, continuing exactly where
                the previous chunk ended.

        Returns:
            The messages committed by this chunk (possibly empty).
        """
        if self._finished:
            raise ValueError("Cannot feed a finished reader.")
        self._buffer += data
        return self._process()

    def resume(self, data: str) -> list[Message]:
        """Continue reading after a reconnect, when the sender resends the
        unconfirmed tail.

        Args:
            data: The resent stream data. It must start with the unconfirmed
                tail exactly as reported by `unconfirmed_tail`, optionally
                followed by new data.

        Returns:
            The messages committed by this chunk (possibly empty). Previously
            committed messages are never returned again.

        Raises:
            EDISyntaxError: If the data does not start with the unconfirmed
                tail.
        """
        if self._finished:
            raise ValueError("Cannot resume a finished reader.")
        tail = self.unconfirmed_tail
        if not data.startswith(tail):
            raise EDISyntaxError(
                "Resent data does not start with the unconfirmed tail: "
                f"expected {tail!r}, got {data[: len(tail)]!r}."
            )
        # discard the uncommitted tail and parse the resent data from scratch;
        # committed messages are untouched, so nothing is written twice.
        self._unh = None
        self._candidate = []
        self._tail_raw = ""
        self._buffer = data
        return self._process()

    def finish(self) -> None:
        """Declare the end of the stream.

        If the interchange trailer UNZ is missing, the whole batch is
        recorded as `unclosed`; already committed messages stay committed.
        """
        self._finished = True
        if not self._closed:
            self._unclosed = True
            logger.warning(
                "Interchange ended without UNZ segment; "
                "recording the batch as unclosed."
            )

    def _process(self) -> list[Message]:
        """Parse and handle all complete segments currently in the buffer."""
        committed = []
        while True:
            if not self._una_checked and not self._check_una():
                break
            raw_segment = self._extract_segment()
            if raw_segment is None:
                break
            self._tail_raw += raw_segment
            segment = self._parse_segment(raw_segment)
            message = self._handle_segment(segment)
            if message is not None:
                committed.append(message)
        return committed

    def _check_una(self) -> bool:
        """Consume a leading UNA service string advice, if present.

        Returns False if more data is needed to decide.
        """
        stripped = self._buffer.lstrip("\ufeff \t\r\n")
        if len(stripped) < 3:
            return False
        if not stripped.startswith("UNA"):
            self._una_checked = True
            return True
        if len(stripped) < 9:
            return False
        if len(stripped) != len(self._buffer):
            self.parser._warn_non_conformant(
                "Leading whitespace or BOM before the UNA segment."
            )
        self.characters = Characters.from_str(stripped[:9])
        self.parser.characters = self.characters
        self._buffer = stripped[9:].lstrip("\r\n")
        self._una_checked = True
        return True

    def _extract_segment(self) -> str | None:
        """Cut the next complete segment (including its terminator) off the
        buffer, or return None if the buffer ends mid-segment."""
        buffer = self._buffer
        # skip insignificant whitespace between segments, like the Tokenizer
        pos = 0
        while pos < len(buffer) and buffer[pos] in self.characters.line_terminators:
            pos += 1
        if pos:
            self._buffer = buffer = buffer[pos:]
        escape = self.characters.escape_character
        terminator = self.characters.segment_terminator
        pos = 0
        while pos < len(buffer):
            char = buffer[pos]
            if char == escape:
                pos += 2
                continue
            if char == terminator:
                self._buffer = buffer[pos + 1 :]
                return buffer[: pos + 1]
            pos += 1
        return None

    def _parse_segment(self, raw_segment: str) -> Segment:
        """Parse one raw segment string (including its terminator)."""
        tokens = Tokenizer().get_tokens(raw_segment, self.characters)
        raw_segments = self.parser.convert_tokens_to_raw_segments(tokens)
        raw = next(raw_segments, None)
        if raw is None:
            raise EDISyntaxError(f"Empty segment: {raw_segment!r}")
        return self.parser.convert_raw_segment_to_segment(
            raw, directory=self.parser.directory
        )

    def _handle_segment(self, segment: Segment) -> Message | None:
        """Run the interchange state machine for one parsed segment.

        Returns the committed message when a UNT trailer checks out.
        """
        tag = segment.tag
        if not self._started:
            if tag != "UNB":
                raise EDISyntaxError(
                    "An interchange must start with UNB or UNA and UNB, "
                    f'not with "{segment}".'
                )
            self._started = True
            # the envelope header is consumed by the reader itself and is
            # not part of the unconfirmed tail anymore
            self._tail_raw = ""
            return None
        if self._closed:
            raise EDISyntaxError(f"Content after the UNZ segment: {segment}")
        if tag == "UNB":
            raise EDISyntaxError("Duplicate UNB segment in one interchange.")
        if tag == "UNH":
            if self._unh is not None:
                raise EDISyntaxError(f"Missing UNT segment before new UNH: {segment}")
            if len(segment.elements) < 2:
                raise EDISyntaxError(
                    f"UNH segment needs a message reference number and a "
                    f'message identifier: "{segment}"'
                )
            if not isinstance(segment.elements[0], str) or not isinstance(
                segment.elements[1], list
            ):
                raise EDISyntaxError(
                    f"UNH segment malformed: the message reference number "
                    f"must be a simple element and the message identifier a "
                    f'composite one: "{segment}"'
                )
            self._unh = segment
            self._candidate = []
            return None
        if tag == "UNT":
            if self._unh is None:
                raise EDISyntaxError(f'UNT segment without matching UNH: "{segment}"')
            return self._commit_message(segment)
        if tag == "UNZ":
            if self._unh is not None:
                raise EDISyntaxError("UNH segment was not closed with a UNT segment.")
            self._closed = True
            self._tail_raw = ""
            return None
        if self._unh is None:
            raise EDISyntaxError(f'Segment "{segment}" is not enclosed in UNH ... UNT.')
        self._candidate.append(segment)
        return None

    def _commit_message(self, unt: Segment) -> Message:
        """Commit the candidate message if its UNT trailer checks out.

        The UNT segment count (0074) must equal the actual number of
        segments from UNH to UNT (both included), and the UNT message
        reference number (0062) must match the UNH header. Only then is the
        message committed.

        Raises:
            EDISyntaxError: If the UNT checks fail; the message is not
                committed.
        """
        unh = self._unh
        if unh is None:  # pragma: no cover - guarded by the caller
            raise EDISyntaxError(f'UNT segment without matching UNH: "{unt}"')
        reference = unh.elements[0]
        identifier = unh.elements[1]
        if isinstance(reference, list) or not isinstance(identifier, list):
            raise EDISyntaxError(f"UNH segment malformed: {unh}")
        if len(unt.elements) < 2:
            raise EDISyntaxError(
                f"UNT segment needs a segment count and a message reference "
                f'number: "{unt}"'
            )
        count, unt_reference = unt.elements[0], unt.elements[1]
        expected_count = len(self._candidate) + 2  # UNH and UNT included
        count_ok = (
            isinstance(count, str)
            and count.isdecimal()
            and int(count) == expected_count
        )
        if not count_ok:
            raise EDISyntaxError(
                f"UNT segment count {count!r} does not match the actual "
                f"number of segments in the message ({expected_count}); "
                f"the message is not committed."
            )
        if unt_reference != reference:
            raise EDISyntaxError(
                f"UNT message reference number {unt_reference!r} does not "
                f"match the UNH message reference number {reference!r}; "
                f"the message is not committed."
            )
        message = Message(reference, identifier, characters=self.characters)
        message.add_segments(self._candidate)
        self._messages.append(message)
        self._unh = None
        self._candidate = []
        self._tail_raw = ""
        return message
