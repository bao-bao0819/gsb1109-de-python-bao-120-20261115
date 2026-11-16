"""Representation of an OSC message in a pythonesque way."""

import logging

from pythonosc.parsing import osc_types
from typing import Dict, List, Iterator, Any, NamedTuple


class ParseError(Exception):
    """Base exception raised when a datagram parsing error occurs."""


class ParamField(NamedTuple):
    """A decoded parameter mapped back to its bytes in the datagram.

    Attributes:
      type: The OSC type tag character of the parameter (e.g. "s", "b", "i").
      value: The decoded parameter value.
      spans: Named (start, end) byte ranges in the datagram, end exclusive.
        Strings report "content", "terminator" and "padding"; blobs report
        "length", "payload" and "padding"; fixed-size types report "value".
        Types without wire data (T, F, N) have no spans.
    """

    type: str
    value: Any
    spans: Dict[str, osc_types.Span]


class OscMessage(object):
    """Representation of a parsed datagram representing an OSC message.

    An OSC message consists of an OSC Address Pattern followed by an OSC
    Type Tag String followed by zero or more OSC Arguments.
    """

    def __init__(self, dgram: bytes) -> None:
        self._dgram = dgram
        self._parameters = []  # type: List[Any]
        self._fields = []  # type: List[ParamField]
        self._parse_datagram()

    def __str__(self):
        return f"{self.address} {' '.join(str(p) for p in self.params)}"

    def _parse_datagram(self) -> None:
        try:
            self._address_regexp, index = osc_types.get_string(self._dgram, 0)
            if not self._dgram[index:]:
                # No params is legit, just return now.
                return

            # Get the parameters types.
            type_tag, index = osc_types.get_string(self._dgram, index)
            if not type_tag.startswith(","):
                raise ParseError(
                    f"OSC Type Tag String must start with a comma, got: {type_tag}"
                )

            type_tag = type_tag[1:]

            params = []  # type: List[Any]
            fields = []  # type: List[ParamField]
            param_stack = [params]
            # Parse each parameter given its type.
            for param in type_tag:
                val = NotImplemented  # type: Any
                spans = {}  # type: Dict[str, osc_types.Span]
                start = index
                if param == "i":  # Integer.
                    val, index = osc_types.get_int(self._dgram, index)
                    spans["value"] = (start, index)
                elif param == "h":  # Int64.
                    val, index = osc_types.get_int64(self._dgram, index)
                    spans["value"] = (start, index)
                elif param == "f":  # Float.
                    val, index = osc_types.get_float(self._dgram, index)
                    spans["value"] = (start, index)
                elif param == "d":  # Double.
                    val, index = osc_types.get_double(self._dgram, index)
                    spans["value"] = (start, index)
                elif param == "s":  # String.
                    val, index, spans = osc_types.get_string_field(self._dgram, index)
                elif param == "b":  # Blob.
                    val, index, spans = osc_types.get_blob_field(self._dgram, index)
                elif param == "r":  # RGBA.
                    val, index = osc_types.get_rgba(self._dgram, index)
                    spans["value"] = (start, index)
                elif param == "m":  # MIDI.
                    val, index = osc_types.get_midi(self._dgram, index)
                    spans["value"] = (start, index)
                elif param == "t":  # osc time tag:
                    val, index = osc_types.get_timetag(self._dgram, index)
                    spans["value"] = (start, index)
                elif param == "T":  # True.
                    val = True
                elif param == "F":  # False.
                    val = False
                elif param == "N":  # Nil.
                    val = None
                elif param == "[":  # Array start.
                    array = []  # type: List[Any]
                    param_stack[-1].append(array)
                    param_stack.append(array)
                elif param == "]":  # Array stop.
                    if len(param_stack) < 2:
                        raise ParseError(
                            f"Unexpected closing bracket in type tag: {type_tag}"
                        )
                    param_stack.pop()
                # TODO: Support more exotic types as described in the specification.
                else:
                    logging.warning(f"Unhandled parameter type: {param}")
                    continue
                if param not in "[]":
                    param_stack[-1].append(val)
                    fields.append(ParamField(param, val, spans))
            if len(param_stack) != 1:
                raise ParseError(f"Missing closing bracket in type tag: {type_tag}")
            self._parameters = params
            self._fields = fields
        except osc_types.ParseError as pe:
            raise ParseError("Found incorrect datagram, ignoring it", pe)

    @property
    def address(self) -> str:
        """Returns the OSC address regular expression."""
        return self._address_regexp

    @staticmethod
    def dgram_is_message(dgram: bytes) -> bool:
        """Returns whether this datagram starts as an OSC message."""
        return dgram.startswith(b"/")

    @property
    def size(self) -> int:
        """Returns the length of the datagram for this message."""
        return len(self._dgram)

    @property
    def dgram(self) -> bytes:
        """Returns the datagram from which this message was built."""
        return self._dgram

    @property
    def params(self) -> List[Any]:
        """Convenience method for list(self) to get the list of parameters."""
        return list(self)

    @property
    def fields(self) -> List[ParamField]:
        """Returns the decoded parameters mapped back to the datagram.

        One ParamField per decoded parameter, in wire order (array elements
        included, brackets excluded), each carrying the byte ranges of its
        wire components so the original bytes can be sliced out of the
        datagram via the dgram property.
        """
        return list(self._fields)

    def __iter__(self) -> Iterator[Any]:
        """Returns an iterator over the parameters of this message."""
        return iter(self._parameters)
