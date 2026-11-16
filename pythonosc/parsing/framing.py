"""OSC message codecs for different framing strategies."""

import copy
from abc import ABCMeta, abstractmethod
from struct import pack, unpack_from
from typing import BinaryIO, Iterable, Iterator, Union

from pythonosc.parsing.slip import (
    END,
    decode as slip_decode,
    encode as slip_encode,
)

DEFAULT_MAX_FRAME_SIZE = 1048576
_READ_CHUNK_SIZE = 16384


class FramingError(ValueError):
    """Raised when a framed OSC byte stream cannot be decoded."""


class FrameSizeError(FramingError):
    """Raised when a frame claims a length that exceeds the configured limit."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.complete: list[bytes] = []


class Framing(metaclass=ABCMeta):
    """Base class for OSC stream framing strategies.

    Args:
        max_frame_size: Maximum size of a single OSC datagram.  Frames that
            declare a larger size are rejected as soon as their length is
            known, rather than being buffered indefinitely.  ``None`` disables
            the limit.
    """

    max_frame_size: int | None = None
    """Maximum datagram size, or ``None`` for no limit."""

    frame_overhead: int = 0
    """Fixed number of framing bytes stored alongside each datagram."""

    def __init__(self, max_frame_size: int | None = DEFAULT_MAX_FRAME_SIZE) -> None:
        self.max_frame_size = max_frame_size

    def _check_frame_size(self, size: int) -> None:
        if self.max_frame_size is not None and size > self.max_frame_size:
            raise FrameSizeError(
                f"OSC frame is too large: {size} > {self.max_frame_size}"
            )

    @abstractmethod
    def encode(self, dgram: bytes) -> bytes:
        """Encode a single OSC datagram for transmission on a byte stream.

        Args:
            dgram: Raw OSC datagram bytes.

        Returns:
            The framed byte sequence to write to the stream.
        """

    @abstractmethod
    def decode_stream(
        self, buf: bytes | bytearray, start: int = 0
    ) -> tuple[list[bytes], int]:
        """Decode complete OSC datagrams from a byte buffer.

        Args:
            buf: Bytes received from a stream.
            start: The index in ``buf`` to start decoding from.

        Returns:
            A tuple ``(messages, bytes consumed)``.  Bytes belonging to an
            incomplete trailing frame are not consumed and therefore remain
            available to the caller.
        """

    def check_pending(
        self, buf: bytes | bytearray, start: int, max_frame_size: int | None
    ) -> None:
        """Validate a pending frame before decoding it.

        Framing strategies with an explicit length header override this hook so
        that an invalid size is rejected without waiting for the payload.
        """

    def reader(
        self,
        max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
        max_frame_size: int | None = None,
    ) -> "FramingReader":
        """Create an incremental reader for this framing strategy."""

        return FramingReader(self, max_buffer_size, max_frame_size)

    def read(
        self,
        source: "ByteSource",
        max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
        max_frame_size: int | None = None,
    ) -> list[bytes]:
        """Decode all complete datagrams from an offline byte source.

        The final incomplete frame, if any, is validated by size and discarded
        rather than returned.  Use :meth:`reader` directly to inspect it.
        """

        return list(self.iter_frames(source, max_buffer_size, max_frame_size))

    def iter_frames(
        self,
        source: "ByteSource",
        max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
        max_frame_size: int | None = None,
    ) -> "Iterator[bytes]":
        """Iterate over complete datagrams from an offline byte source."""

        reader = self.reader(max_buffer_size, max_frame_size)
        yield from reader.iter_frames(source)


class SLIPFraming(Framing):
    """SLIP (RFC 1055) framing for OSC 1.1.

    See the "OSC Delivery Specification 1.1" section from the 2009 NIME paper for details.

    For this framing ``max_frame_size`` bounds the encoded packet content
    (between the two ``END`` delimiters), so it also bounds the decoded
    datagram and the bytes retained while waiting for the closing delimiter.
    """

    frame_overhead = 2

    def encode(self, dgram: bytes) -> bytes:
        return slip_encode(dgram)

    def check_pending(
        self, buf: bytes | bytearray, start: int, max_frame_size: int | None
    ) -> None:
        # Leading END markers are delimiters, not packet contents.
        pos = start
        while pos < len(buf) and buf[pos : pos + 1] == END:
            pos += 1

        end = buf.find(END, pos)
        if end < 0:
            end = len(buf)
        if end == pos:
            return
        if max_frame_size is not None and end - pos > max_frame_size:
            raise FrameSizeError(
                f"OSC frame is too large: {end - pos} > {max_frame_size}"
            )

    def decode_stream(
        self, buf: bytes | bytearray, start: int = 0
    ) -> tuple[list[bytes], int]:
        pos = start
        messages: list[bytes] = []
        limit = self.max_frame_size

        scan = start
        while (end := buf.find(END, scan)) >= 0:
            if end > scan and limit is not None and end - scan > limit:
                # Leave the oversized packet pending; the reader reports it
                # after returning any packets confirmed before it.
                break
            if end > scan:
                messages.append(slip_decode(bytes(buf[scan:end])))
                pos = end + len(END)
            scan = end + len(END)

        # Keep an incomplete packet, but consume trailing delimiters.
        if scan == len(buf):
            pos = scan

        return messages, pos - start


class BinaryLengthFraming(Framing):
    """Length-prefixed framing for OSC 1.0.

    See the "OSC Packets" section from OSC 1.0 Specification for details.
    """

    frame_overhead = 4

    def encode(self, dgram: bytes) -> bytes:
        if dgram:
            return pack("!I", len(dgram)) + dgram
        return b""

    def _validate_length(self, length: int, max_frame_size: int | None) -> None:
        if max_frame_size is not None and length > max_frame_size:
            raise FrameSizeError(f"OSC frame is too large: {length} > {max_frame_size}")

    def check_pending(
        self, buf: bytes | bytearray, start: int, max_frame_size: int | None
    ) -> None:
        if len(buf) - start < 4:
            return

        (length,) = unpack_from("!I", buf, start)
        self._validate_length(length, max_frame_size)

    def decode_stream(
        self, buf: bytes | bytearray, start: int = 0
    ) -> tuple[list[bytes], int]:
        messages = []
        pos = start

        while len(buf) - pos >= 4:
            (length,) = unpack_from("!I", buf, pos)
            if self.max_frame_size is not None and length > self.max_frame_size:
                # Leave the header pending; the reader reports the error after
                # returning any frames confirmed before it.
                break

            data_pos = pos + 4
            end = data_pos + length

            if end > len(buf):
                # The length header is valid but its payload is incomplete.
                # Leave header and payload pending for the next chunk.
                break

            if length:
                messages.append(bytes(buf[data_pos:end]))

            # A zero-length frame (an empty datagram) is consumed without
            # producing a message, matching the encoder, which omits it.
            pos = end

        return messages, pos - start


class FramingReader:
    """Incremental reader for framed OSC datagrams from a byte stream.

    The reader is suitable for offline streams (``bytes``, file-like objects
    or iterables of chunks) as well as live socket reads.  Not safe for
    concurrent use.

    Args:
        framing: Strategy used to decode buffered stream data.
        max_buffer_size: Maximum number of bytes held while waiting for a
            frame to become complete.
        max_frame_size: Maximum size of a single OSC datagram.  Defaults to
            the framing strategy's limit, capped by ``max_buffer_size``.
    """

    __slots__ = ("buf", "start", "framing", "max_buffer_size", "max_frame_size")

    def __init__(
        self,
        framing: Framing,
        max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
        max_frame_size: int | None = None,
    ) -> None:
        self.buf = bytearray()
        self.start = 0
        self.max_buffer_size = max_buffer_size

        limits = [
            limit
            for limit in (
                max_frame_size,
                getattr(framing, "max_frame_size", None),
                max_buffer_size,
            )
            if limit is not None
        ]
        self.max_frame_size = min(limits) if limits else None

        # Use a private strategy instance so the reader's effective limit
        # applies to decoding as well as to pending-header validation.
        self.framing = copy.copy(framing)
        self.framing.max_frame_size = self.max_frame_size

    @property
    def _capacity(self) -> int:
        # Allow the framing header/delimiters of a max-sized datagram.
        return self.max_buffer_size + self.framing.frame_overhead

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[bytes]:
        """Append stream data to the internal buffer and return complete datagrams.

        Args:
            chunk: Newly received bytes from the stream.

        Returns:
            A list of complete, decoded OSC datagrams.  Any incomplete trailing
            frame is retained and can be inspected through :attr:`remaining`.
        """

        new_buf_len = len(self.buf) + len(chunk)

        if (pending := new_buf_len - self.start) > self._capacity:
            raise ValueError(f"Buffer is too large: {pending} > {self._capacity}")

        if new_buf_len > self._capacity:
            del self.buf[: self.start]
            self.start = 0

        self.buf.extend(chunk)

        messages, consumed = self.framing.decode_stream(self.buf, self.start)
        available = len(self.buf) - self.start

        assert 0 <= consumed <= available, (
            f"misbehaving framing decoder: {consumed=} {available=}"
        )

        self.start += consumed

        # Reject an oversized length header as soon as it is complete, even if
        # it directly follows frames decoded in this same feed.  Confirmed
        # datagrams remain available on the exception.
        if self.start < len(self.buf):
            try:
                self.framing.check_pending(self.buf, self.start, self.max_frame_size)
            except FrameSizeError as exc:
                exc.complete = messages
                raise

        return messages

    def read(self, source: "ByteSource") -> list[bytes]:
        """Feed an entire offline byte source through the reader."""

        return list(self.iter_frames(source))

    def iter_frames(self, source: "ByteSource") -> "Iterator[bytes]":
        """Feed an offline byte source and yield immediately decoded frames."""

        if isinstance(source, (bytes, bytearray, memoryview)):
            chunks: Iterable[bytes | bytearray | memoryview] = (source,)
        elif hasattr(source, "read"):
            chunks = iter(lambda: source.read(_READ_CHUNK_SIZE), b"")
        else:
            chunks = source

        for chunk in chunks:
            view = memoryview(chunk).cast("B")
            offset = 0
            while offset < len(view):
                capacity = self._capacity - (len(self.buf) - self.start)
                if capacity <= 0:
                    # Force the regular overflow check to raise.
                    yield from self.feed(view[offset : offset + 1])
                    offset += 1
                    continue

                size = min(capacity, _READ_CHUNK_SIZE, len(view) - offset)
                yield from self.feed(view[offset : offset + size])
                offset += size

        # Validate a trailing incomplete frame against the size limit.
        self.framing.check_pending(self.buf, self.start, self.max_frame_size)

    @property
    def remaining(self) -> bytes:
        """Return any unparsed bytes (the unconfirmed tail of the stream)."""

        return bytes(self.buf[self.start :])

    def reset(self) -> None:
        """Clear the internal buffer (discard unparsed data)."""

        del self.buf[:]
        self.start = 0


class FramingParser(FramingReader):
    """Backward-compatible name for :class:`FramingReader`."""


class SLIPReader(FramingReader):
    """Reader for SLIP-framed (OSC 1.1) byte streams."""

    def __init__(
        self,
        max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
        max_frame_size: int | None = None,
    ) -> None:
        super().__init__(SLIPFraming(max_frame_size), max_buffer_size, max_frame_size)


class BinaryLengthReader(FramingReader):
    """Reader for length-prefixed (OSC 1.0) byte streams."""

    def __init__(
        self,
        max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
        max_frame_size: int | None = None,
    ) -> None:
        super().__init__(
            BinaryLengthFraming(max_frame_size), max_buffer_size, max_frame_size
        )


# Descriptive aliases for the concrete readers.
SLIPFramingReader = SLIPReader
BinaryLengthFramingReader = BinaryLengthReader
LengthPrefixedReader = BinaryLengthReader

SLIP_FRAMING: Framing = SLIPFraming()
BINARY_LENGTH_FRAMING: Framing = BinaryLengthFraming()

FRAMINGS: dict[str, Framing] = {
    "1.0": BINARY_LENGTH_FRAMING,
    "1.1": SLIP_FRAMING,
}


def get_framing_for_mode(mode: str) -> Framing:
    """Return the declared framing strategy for an OSC TCP mode string."""

    try:
        return FRAMINGS[mode]
    except KeyError:
        raise ValueError(f"Unsupported OSC mode: {mode}") from None


def make_reader(
    mode: str,
    max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
    max_frame_size: int | None = None,
) -> FramingReader:
    """Create a reader for one of the declared OSC TCP framing modes."""

    return get_framing_for_mode(mode).reader(max_buffer_size, max_frame_size)


ByteSource = Union[
    bytes,
    bytearray,
    memoryview,
    Iterable[bytes],
    BinaryIO,
]


def read_frames(
    source: ByteSource,
    framing: Framing,
    max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
    max_frame_size: int | None = None,
) -> list[bytes]:
    """Read all complete OSC datagrams from an offline framed byte source.

    ``source`` may be a bytes-like object, a file-like object with ``read``, or
    an iterable of byte chunks.  An incomplete trailing frame is not returned;
    use a :class:`FramingReader` directly to inspect it via
    :attr:`FramingReader.remaining`.
    """

    return framing.read(source, max_buffer_size, max_frame_size)


def iter_frames(
    source: ByteSource,
    framing: Framing,
    max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
    max_frame_size: int | None = None,
) -> Iterator[bytes]:
    """Iterate over complete OSC datagrams from an offline framed byte source."""

    return framing.iter_frames(source, max_buffer_size, max_frame_size)


def read_datagrams(
    source: ByteSource,
    mode: str,
    max_buffer_size: int = DEFAULT_MAX_FRAME_SIZE,
    max_frame_size: int | None = None,
) -> list[bytes]:
    """Read all datagrams from a stream whose framing is named by ``mode``.

    Args:
        source: Offline byte source.
        mode: Declared OSC TCP framing mode (``"1.0"`` or ``"1.1"``).
    """

    return get_framing_for_mode(mode).read(source, max_buffer_size, max_frame_size)


read_stream = read_datagrams
