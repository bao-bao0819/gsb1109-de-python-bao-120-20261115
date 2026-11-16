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

from collections.abc import Iterable, Iterator

from pydifact.control.characters import Characters
from pydifact.exceptions import EDISyntaxError

# characters ignored between segments and between interchanges
_WHITESPACE = " \t\r\n\ufeff"


def _iter_chars(source: str | Iterable[str]) -> Iterator[str]:
    """Yield the characters of the source, chunk by chunk if it is chunked."""
    if isinstance(source, str):
        yield from source
    else:
        for chunk in source:
            yield from chunk


class InterchangeSplitter:
    """Split a stream of concatenated EDIFACT interchanges into single interchanges.

    Each interchange in the stream may carry its own UNA service string advice
    with different control characters. The splitter re-reads the UNA header of
    every interchange and applies those characters only until the matching UNZ
    segment; afterwards the control characters are reset to the defaults, so
    the settings of one interchange never leak into the next one.

    A segment terminator directly behind a release (escape) character is part
    of the data element value and never ends a segment. The source may be given
    in chunks of any size - the escape state is kept across chunk boundaries,
    so a chunk may even end right behind a release character.

    Parameters:
        characters: The default control characters to use for interchanges
            without a UNA header. (default: Characters())
    """

    def __init__(self, characters: Characters | None = None) -> None:
        self.characters = characters or Characters()

    def split(self, source: str | Iterable[str]) -> Iterator[tuple[str, list[int]]]:
        """Split the source into single interchanges.

        Args:
            source: The concatenated EDI interchanges, either as one string or
                as an iterable of string chunks (e.g. read from a file).

        Yields:
            A tuple (interchange, offsets) per interchange, where
            ``interchange`` is the raw interchange string (including a
            leading UNA header, if present) and ``offsets`` holds the
            absolute character positions of all its segments (UNA included)
            within the source.

        Raises:
            EDISyntaxError: If the source ends within an unterminated
                interchange.
        """
        # the control characters of the interchange currently being read
        characters = self.characters

        buffer: list[str] = []
        offsets: list[int] = []
        tag: list[str] = []
        escaped = False
        in_segment = False
        una_remaining = 0
        pos = 0

        for char in _iter_chars(source):
            if not in_segment:
                if char in _WHITESPACE:
                    pos += 1
                    continue
                # a new segment starts here
                in_segment = True
                offsets.append(pos)
                tag = []

            buffer.append(char)

            if una_remaining:
                # the 6 characters behind "UNA" are taken unconditionally
                una_remaining -= 1
                if not una_remaining:
                    characters = Characters.from_str("UNA" + "".join(buffer[-6:]))
                    # the UNA header is not followed by a segment terminator
                    in_segment = False
            elif len(tag) < 3:
                tag.append(char)
                if len(tag) == 3 and len(offsets) == 1 and "".join(tag) == "UNA":
                    una_remaining = 6
            elif escaped:
                # a character behind the release character is plain content,
                # even if it is the segment terminator
                escaped = False
            elif char == characters.escape_character:
                escaped = True
            elif char == characters.segment_terminator:
                in_segment = False
                if "".join(tag) == "UNZ":
                    yield "".join(buffer), offsets
                    buffer = []
                    offsets = []
                    # reset the characters, they must not leak into the
                    # next interchange
                    characters = self.characters

            pos += 1

        if buffer:
            raise EDISyntaxError(
                "Unexpected end of input: the last interchange is not "
                "terminated by a UNZ segment."
            )
