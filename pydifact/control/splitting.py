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

"""Split an EDIFACT interchange into several interchanges respecting a byte limit.

Receiving stations often accept interchanges only up to a maximum number of
bytes. Messages (the units enclosed by ``UNH`` ... ``UNT``) must stay intact:
they are never torn apart, but whole messages can be distributed over several
interchanges. Each generated interchange gets its own control reference (so
that retries of the same splitting plan stay reconcilable and never reuse a
control number already emitted) and a recalculated ``UNZ`` message count.

The real byte size is only known after serialization and encoding, so every
segment is serialized and encoded first. A message is moved to the next
interchange once adding it would exceed the limit. A message that alone does
not fit into a single interchange (even with nothing else inside) cannot be
delivered without breaking it apart and is reported separately.
"""

from collections.abc import Sequence

from pydifact.segmentcollection import Interchange, Message
from pydifact.segments import Segment
from pydifact.serializer import Serializer


class MessageTooLargeError(Exception):
    """Raised when a single message cannot fit into any interchange.

    The message serialized on its own (with its own ``UNH``/``UNT`` envelope,
    inside an otherwise empty interchange) already needs more bytes than the
    configured limit, so it cannot be delivered without breaking it apart,
    which the EDIFACT message structure does not allow.

    Attributes:
        message: The offending `~pydifact.segmentcollection.Message`.
        required_bytes: Number of bytes the minimal interchange for this
            message needs.
        max_bytes: The configured byte limit.
        control_reference: Control reference the oversized interchange would
            have received, so the failure stays reconcilable with the plan.
    """

    def __init__(
        self,
        message: Message,
        required_bytes: int,
        max_bytes: int,
        control_reference: str,
    ) -> None:
        self.message = message
        self.required_bytes = required_bytes
        self.max_bytes = max_bytes
        self.control_reference = control_reference
        super().__init__(
            f"Message {message.reference_number!r} needs {required_bytes} bytes "
            f"on its own, which exceeds the interchange limit of {max_bytes} "
            f"bytes (interchange control reference {control_reference!r})."
        )


class InterchangeSplitter:
    """Distribute the messages of an interchange over size-limited copies.

    The source interchange is never modified: its envelope (sender, recipient,
    syntax identifier, timestamp, extra ``UNB`` elements and control
    characters) is copied onto every generated interchange, while the messages
    are redistributed whole.

    Sizes are measured on the serialized and encoded output, including escape
    sequences and the optional ``UNA`` service string advice.

    Args:
        max_bytes: Maximum number of encoded bytes allowed for one generated
            interchange, including its ``UNB``/``UNZ`` envelope.
        encoding: Character encoding used to turn the serialized EDI text into
            bytes. Defaults to ``"iso8859-1"``, the library default for EDI
            files.
        first_control_reference: Interchange control reference (``UNB``/``UNZ``
            element 0020) of the first generated interchange. Defaults to the
            source interchange's own control reference. Further references are
            derived numerically, so this must be an integer value.
        control_reference_step: Positive integer added for every further
            generated interchange, producing unique, gap-free references.

    Raises:
        ValueError: If ``max_bytes`` or ``control_reference_step`` is not
            positive.
    """

    def __init__(
        self,
        max_bytes: int,
        encoding: str = "iso8859-1",
        first_control_reference: str | int | None = None,
        control_reference_step: int = 1,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be a positive number of bytes.")
        if control_reference_step <= 0:
            raise ValueError("control_reference_step must be positive.")
        self.max_bytes = max_bytes
        self.encoding = encoding
        self.first_control_reference = (
            None if first_control_reference is None else str(first_control_reference)
        )
        self.control_reference_step = control_reference_step

    def split(self, interchange: Interchange) -> Sequence[Interchange]:
        """Split ``interchange`` into one or more size-limited interchanges.

        Messages are processed in order and kept intact; a message moves to a
        new interchange once adding it would exceed `max_bytes`. Every
        generated interchange receives a unique control reference and a
        recalculated ``UNZ`` message count. Re-running this method with the
        same arguments reproduces the same plan and the same references.

        Args:
            interchange: The source interchange. It is not modified and must
                consist of complete ``UNH`` ... ``UNT`` messages.

        Returns:
            A list of new `~pydifact.segmentcollection.Interchange` objects,
            each serialized below the byte limit. The list is empty only if
            the source contains no messages.

        Raises:
            MessageTooLargeError: If a single message already exceeds the
                limit on its own.
            ValueError: If the control reference is not an integer value.
        """
        messages = list(interchange.get_messages())
        if not messages:
            return []

        serializer = Serializer(interchange.characters)
        with_una = interchange.has_una_segment

        results: list[Interchange] = []

        # The interchange currently being filled, with exact byte bookkeeping.
        current: Interchange | None = None
        header_bytes = 0
        footer_bytes = 0
        payload_bytes = 0
        message_count = 0

        for message in messages:
            message_bytes = self._message_bytes(message, serializer)

            if current is None:
                current, header_bytes = self._open_interchange(
                    interchange, len(results), serializer, with_una
                )
                results.append(current)
                payload_bytes = 0
                message_count = 0
                footer_bytes = self._footer_bytes(
                    str(current.control_reference), 1, serializer
                )

            # Size the interchange would have if this message is added: the
            # already collected payload stays in place, messages never split.
            minimal_size = header_bytes + payload_bytes + message_bytes + footer_bytes
            if minimal_size > self.max_bytes:
                if message_count == 0:
                    # The message does not even fit alone: it can never be
                    # delivered under this limit, so report it instead of
                    # breaking the message apart.
                    raise MessageTooLargeError(
                        message,
                        minimal_size,
                        self.max_bytes,
                        str(current.control_reference),
                    )
                # The current interchange is full; open the next one and place
                # the message there.
                current, header_bytes = self._open_interchange(
                    interchange, len(results), serializer, with_una
                )
                results.append(current)
                payload_bytes = 0
                message_count = 0
                footer_bytes = self._footer_bytes(
                    str(current.control_reference), 1, serializer
                )
                minimal_size = header_bytes + message_bytes + footer_bytes
                if minimal_size > self.max_bytes:
                    raise MessageTooLargeError(
                        message,
                        minimal_size,
                        self.max_bytes,
                        str(current.control_reference),
                    )

            current.add_message(message)
            payload_bytes += message_bytes
            message_count += 1
            # The UNZ count may gain a digit (e.g. 9 -> 10); measure the
            # footer for the count the next decision would produce.
            footer_bytes = self._footer_bytes(
                str(current.control_reference),
                message_count + 1,
                serializer,
            )

        self._verify_limits(results)
        return results

    def _control_reference(self, interchange: Interchange, index: int) -> str:
        """Return the control reference for the interchange at ``index``."""
        first = (
            self.first_control_reference
            if self.first_control_reference is not None
            else str(interchange.control_reference)
        )
        # int() raises ValueError for non-numeric references, which cannot be
        # advanced deterministically.
        return str(int(first) + index * self.control_reference_step)

    def _open_interchange(
        self,
        interchange: Interchange,
        index: int,
        serializer: Serializer,
        with_una: bool,
    ) -> tuple[Interchange, int]:
        """Create a fresh interchange and measure its exact header size.

        The header is measured per interchange so that a control reference
        gaining digits (e.g. ``"9"`` -> ``"10"``) is accounted for correctly.
        """
        new_interchange = Interchange(
            sender=interchange.sender,
            recipient=interchange.recipient,
            control_reference=self._control_reference(interchange, index),
            syntax_identifier=interchange.syntax_identifier,
            timestamp=interchange.timestamp,
            characters=interchange.characters,
            extra_header_elements=interchange.extra_header_elements,
        )
        new_interchange.has_una_segment = interchange.has_una_segment
        header = new_interchange.get_header_segment()
        header_size = self._encoded_size(
            serializer.serialize([header], with_una_header=with_una)
        )
        return new_interchange, header_size

    def _footer_bytes(
        self,
        control_reference: str,
        message_count: int,
        serializer: Serializer,
    ) -> int:
        """Encoded size of the ``UNZ`` footer for a given message count."""
        footer = Segment(Interchange.FOOTER_TAG, str(message_count), control_reference)
        return self._encoded_size(serializer.serialize([footer], with_una_header=False))

    def _message_bytes(self, message: Message, serializer: Serializer) -> int:
        """Encoded size of a complete message, ``UNH``/``UNT`` included."""
        segments = (
            [message.get_header_segment()]
            + message.segments
            + [message.get_footer_segment()]
        )
        return self._encoded_size(serializer.serialize(segments, with_una_header=False))

    def _encoded_size(self, serialized: str) -> int:
        """Number of bytes a serialized EDI fragment occupies on the wire."""
        return len(serialized.encode(self.encoding))

    def _verify_limits(self, results: Sequence[Interchange]) -> None:
        """Check the real encoded output of every generated interchange.

        The byte bookkeeping during splitting is exact, but measure the final
        serialized interchanges as well to guarantee the contracted limit
        against future serializer changes.
        """
        for result in results:
            actual_size = self._encoded_size(result.serialize(break_lines=False))
            if actual_size > self.max_bytes:
                raise RuntimeError(
                    f"Internal splitting error: interchange "
                    f"{result.control_reference!r} serializes to {actual_size} "
                    f"bytes, exceeding the limit of {self.max_bytes} bytes."
                )
