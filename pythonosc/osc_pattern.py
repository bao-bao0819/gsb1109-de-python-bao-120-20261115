"""OSC address patterns.

Implements the address pattern language from the OSC 1.0 specification:

* ``?`` matches a single character other than ``/``.
* ``*`` matches zero or more characters other than ``/``.
* ``[abc]`` matches one character from the class; ``[a-z]`` ranges and
  ``[!abc]`` negation are supported.
* ``{foo,bar}`` matches one of the comma separated alternatives.  Each
  alternative is a literal candidate string, as defined by the specification.

A pattern is compiled once and can then be matched against any number of
concrete addresses.  Patterns that do not follow the grammar raise
:class:`InvalidAddressPattern`, which carries the offending pattern so a
caller can report the rule that is wrong instead of treating it as a plain
address.
"""

import functools
import re
from typing import List, Union

_PATTERN_CHARACTERS = frozenset("*?[]{},")
_ALTERNATIVE_STOPS = frozenset(",}")


class InvalidAddressPattern(ValueError):
    """Raised when an OSC address pattern is malformed.

    Attributes:
        pattern: The exact pattern text supplied by the caller.
        reason: A human readable explanation of the syntax error.
    """

    def __init__(self, pattern: object, reason: str) -> None:
        self.pattern = pattern
        self.reason = reason
        super().__init__(f"Invalid OSC address pattern {pattern!r}: {reason}")


class AddressPattern(object):
    """A compiled OSC address pattern."""

    __slots__ = ("_pattern", "_regexp")

    def __init__(self, pattern: str) -> None:
        if not isinstance(pattern, str):
            raise InvalidAddressPattern(pattern, "pattern must be a string")
        self._pattern = pattern
        self._regexp = _PatternCompiler(pattern).compile()

    @property
    def pattern(self) -> str:
        """The original pattern text, without any normalisation."""
        return self._pattern

    @property
    def regular_expression(self) -> str:
        """The Python regular expression used to match concrete addresses."""
        return self._regexp.pattern

    def matches(self, address: str) -> bool:
        """Return whether a concrete OSC address matches this pattern."""
        return isinstance(address, str) and self._regexp.match(address) is not None

    def __repr__(self) -> str:
        return f"AddressPattern({self._pattern!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AddressPattern) and self._pattern == other._pattern

    def __hash__(self) -> int:
        return hash(self._pattern)


def is_address_pattern(value: str) -> bool:
    """Return whether *value* contains OSC address pattern syntax.

    Concrete OSC addresses never contain ``*``, ``?``, ``,``, ``[``, ``]``,
    ``{`` or ``}``; a value containing one of them has to be interpreted as a
    pattern, and therefore has to be validated as one.
    """
    return isinstance(value, str) and any(char in _PATTERN_CHARACTERS for char in value)


def compile_address_pattern(pattern: str) -> AddressPattern:
    """Compile *pattern* and return an :class:`AddressPattern`.

    Raises:
        InvalidAddressPattern: if the pattern is not valid OSC address
            pattern syntax.  The exception names the offending pattern.
    """
    if not isinstance(pattern, str):
        raise InvalidAddressPattern(pattern, "pattern must be a string")
    return _compile_address_pattern(pattern)


@functools.lru_cache(maxsize=512)
def _compile_address_pattern(pattern: str) -> AddressPattern:
    return AddressPattern(pattern)


class _PatternCompiler(object):
    def __init__(self, pattern: str) -> None:
        self._pattern = pattern
        self._position = 0

    def compile(self) -> "re.Pattern[str]":
        if not self._pattern:
            self._fail("pattern must not be empty")
        if not self._pattern.startswith("/"):
            self._fail("pattern must start with '/'")
        expression = self._sequence()
        if self._position != len(self._pattern):
            self._fail(f"unexpected {self._pattern[self._position]!r}")
        return re.compile("^" + expression + "$")

    def _fail(self, reason: str) -> None:
        raise InvalidAddressPattern(
            self._pattern, f"{reason} at position {self._position}"
        )

    def _sequence(self) -> str:
        parts: List[str] = []
        while self._position < len(self._pattern):
            char = self._pattern[self._position]
            if char == "*":
                parts.append("[^/]*")
                self._position += 1
            elif char == "?":
                parts.append("[^/]")
                self._position += 1
            elif char == "[":
                parts.append(self._character_class())
            elif char == "{":
                parts.append(self._alternatives())
            elif char in "]}":
                self._fail(f"unmatched {char!r}")
            elif char == ",":
                self._fail("unexpected ',' outside a brace alternation")
            else:
                parts.append(re.escape(char))
                self._position += 1
        return "".join(parts)

    def _alternatives(self) -> str:
        start = self._position
        self._position += 1  # Consume '{'.
        alternatives: List[str] = []
        while True:
            alternative = self._alternative()
            if not alternative:
                self._position = start
                self._fail("empty alternative in brace expression")
            alternatives.append(alternative)
            if self._position >= len(self._pattern):
                self._position = start
                self._fail("unterminated '{'")
            separator = self._pattern[self._position]
            if separator == ",":
                self._position += 1
                continue
            # The alternative can only stop at '}' when it was not at end of input.
            self._position += 1  # Consume '}'.
            break
        return "(?:" + "|".join(alternatives) + ")"

    def _alternative(self) -> str:
        # Per the specification a brace alternative is a plain string, so
        # pattern characters inside it are literal characters.
        parts: List[str] = []
        while self._position < len(self._pattern):
            char = self._pattern[self._position]
            if char in _ALTERNATIVE_STOPS:
                break
            if char == "{":
                self._fail("nested '{' is not allowed")
            parts.append(re.escape(char))
            self._position += 1
        return "".join(parts)

    def _character_class(self) -> str:
        self._position += 1  # Consume '['.
        negated = False
        if self._position < len(self._pattern) and self._pattern[self._position] == "!":
            negated = True
            self._position += 1
        if self._position >= len(self._pattern):
            self._fail("unterminated character class")
        if self._pattern[self._position] == "]":
            self._fail("empty character class")

        items: List[Union[str, tuple]] = []
        while True:
            if self._position >= len(self._pattern):
                self._fail("unterminated character class")
            char = self._pattern[self._position]
            if char == "]":
                self._position += 1
                break
            if (
                char != "-"
                and self._position + 2 < len(self._pattern)
                and self._pattern[self._position + 1] == "-"
                and self._pattern[self._position + 2] != "]"
            ):
                end = self._pattern[self._position + 2]
                if ord(char) > ord(end):
                    self._fail(f"invalid range {char!r}-{end!r}")
                items.append((char, end))
                self._position += 3
                continue
            items.append(char)
            self._position += 1

        content = "".join(
            re.escape(item[0]) + "-" + re.escape(item[1])
            if isinstance(item, tuple)
            else re.escape(item)
            for item in items
        )
        if negated:
            # Never let a character class cross a path separator.
            return "[^" + content + "/]"
        return "(?!/)[" + content + "]"
