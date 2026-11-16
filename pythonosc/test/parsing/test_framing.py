from pytest import FixtureRequest, fixture, mark, raises

from pythonosc.parsing.framing import (
    BINARY_LENGTH_FRAMING,
    SLIP_FRAMING,
    BinaryLengthFraming,
    BinaryLengthReader,
    FrameSizeError,
    Framing,
    FramingParser,
    FramingReader,
    SLIPFraming,
    SLIPReader,
    get_framing_for_mode,
    iter_frames,
    make_reader,
    read_datagrams,
    read_frames,
)


class SquareFraming(Framing):
    """Framing strategy for ease of testing, wraps everything in [] brackets."""

    def encode(self, data: bytes) -> bytes:
        if data:
            return b"[" + data + b"]"
        return b""

    def decode_stream(
        self, chunk: bytes | bytearray, start: int = 0
    ) -> tuple[list[bytes], int]:
        out = []
        pos = start

        while (a := chunk.find(b"[", pos)) >= 0:
            a += 1

            if (b := chunk.find(b"]", a)) >= 0:
                out.append(bytes(chunk[a:b]))
                pos = b + 1
            else:
                break

        return out, pos - start


class CommonFramingTest:
    def test_encode_decode(self, framing: Framing) -> None:
        messages = [b"test1", b"", b"test2", b"test3"]
        expected = [m for m in messages if m]
        data = b"".join(framing.encode(message) for message in messages)

        assert framing.decode_stream(data) == (expected, len(data))

    @mark.parametrize("n,m", [(1, 1), (4, 1), (64, 0)])
    def test_incomplete(self, framing: Framing, n: int, m: int) -> None:
        message_a = framing.encode(b"test")
        message_b = framing.encode(b"cut here")
        data = (message_a + message_b)[:-n]
        messages, consumed = framing.decode_stream(data)

        assert messages == messages[:m]
        assert data[consumed:] == data[len(message_a) :]

    def test_decode_with_start(self, framing: Framing) -> None:
        garbage = b"garbage"
        data = garbage + framing.encode(b"test")

        assert framing.decode_stream(data, len(garbage)) == (
            [b"test"],
            len(data) - len(garbage),
        )


class TestSLIPFraming(CommonFramingTest):
    @fixture
    def framing(self) -> Framing:
        return SLIPFraming()

    def test_encode(self, framing: Framing) -> None:
        assert framing.encode(b"te\xc0st") == b"\xc0te\xdb\xdcst\xc0"

    @mark.parametrize("prefix", [b"", b"\xc0"])
    def test_single_end_separates_packets(
        self, framing: Framing, prefix: bytes
    ) -> None:
        data = prefix + b"one\xc0two\xc0"
        assert framing.decode_stream(data) == ([b"one", b"two"], len(data))

    @mark.parametrize("count", [1, 2, 3, 5])
    def test_end_markers_do_not_emit_empty_packets(
        self, framing: Framing, count: int
    ) -> None:
        data = b"\xc0" * count
        assert framing.decode_stream(data) == ([], len(data))

    @mark.parametrize("leading_end", [False, True])
    def test_every_stream_split_preserves_packets(
        self, framing: Framing, leading_end: bool
    ) -> None:
        expected = [b"one", b"two\xc0\xdb", b"three"]
        data = b"".join(framing.encode(value)[not leading_end :] for value in expected)
        for split in range(len(data) + 1):
            parser = FramingParser(framing)
            actual = parser.feed(data[:split]) + parser.feed(data[split:])
            assert actual == expected, split
            assert parser.remaining == b""

    def test_bytewise_stream_preserves_packets(self, framing: Framing) -> None:
        parser = FramingParser(framing)
        expected = [b"one", b"two\xc0\xdb"]
        data = b"".join(framing.encode(value) for value in expected)
        actual = []
        for byte in data:
            actual.extend(parser.feed(bytes([byte])))
        assert actual == expected
        assert parser.remaining == b""


class TestBinaryLengthFraming(CommonFramingTest):
    @fixture
    def framing(self) -> Framing:
        return BinaryLengthFraming()

    def test_encode(self, framing: Framing) -> None:
        assert framing.encode(b"test") == b"\x00\x00\x00\x04test"


class TestSquareFraming(CommonFramingTest):
    @fixture
    def framing(self) -> Framing:
        return SquareFraming()

    def test_encode(self, framing: Framing) -> None:
        assert framing.encode(b"test") == b"[test]"


class TestFramingParser:
    @fixture
    def parser(self) -> FramingParser:
        return FramingParser(SquareFraming(), max_buffer_size=16)

    def test_cold(self, parser: FramingParser) -> None:
        assert parser.feed(b"[test]") == [b"test"]

    def test_hot(self, parser: FramingParser) -> None:
        assert parser.feed(b"[test") == []
        assert parser.feed(b"123][xxx]") == [b"test123", b"xxx"]

    def test_large_buffer(self, parser: FramingParser) -> None:
        parser.feed(b"[test]")

        with raises(ValueError, match="Buffer is too large: 18 > 16"):
            parser.feed(b"[test]" * 3)

    def test_compaction(self, parser: FramingParser) -> None:
        for _ in range(4):
            assert parser.feed(b"[tests]") == [b"tests"]

    def test_reset(self, parser: FramingParser) -> None:
        parser.feed(b"[test]")
        parser.feed(b"[xxx")

        assert parser.remaining == b"[xxx"

        parser.reset()

        assert parser.remaining == b""


class TestReaders:
    @fixture(params=[SLIP_FRAMING, BINARY_LENGTH_FRAMING], ids=["1.1", "1.0"])
    def framing(self, request: FixtureRequest) -> Framing:
        return request.param

    def test_bytewise_chunks(self, framing: Framing) -> None:
        expected = [b"one", b"two\xc0\xdb", b"three"]
        data = b"".join(framing.encode(value) for value in expected)
        reader = framing.reader()

        actual = []
        for byte in data:
            actual.extend(reader.feed(bytes([byte])))

        assert actual == expected
        assert reader.remaining == b""

    @mark.parametrize("split", [1, 2, 3, 4])
    def test_header_and_escape_split(self, framing: Framing, split: int) -> None:
        expected = [b"te\xc0\xdbst", b"tail"]
        data = b"".join(framing.encode(value) for value in expected)
        reader = framing.reader()

        assert reader.feed(data[:split]) == []
        assert reader.feed(data[split:]) == expected
        assert reader.remaining == b""

    def test_incomplete_tail_is_unconsumed(self, framing: Framing) -> None:
        complete = framing.encode(b"first")
        partial = framing.encode(b"second")
        data = complete + partial[:-1]
        reader = framing.reader()

        assert reader.feed(data) == [b"first"]
        assert reader.remaining == data[len(complete) :]

    def test_read_returns_only_complete_datagrams(self, framing: Framing) -> None:
        complete = framing.encode(b"first")
        data = complete + framing.encode(b"second")[:-1]

        assert framing.read(data) == [b"first"]

    def test_iter_frames_accepts_chunk_iterables(self, framing: Framing) -> None:
        expected = [b"one", b"two"]
        data = b"".join(framing.encode(value) for value in expected)
        chunks = [data[:1], data[1:3], data[3:]]

        assert list(framing.iter_frames(chunks)) == expected

    def test_oversized_frame_keeps_confirmed_packets(self, framing: Framing) -> None:
        reader = framing.reader(max_frame_size=8)
        confirmed = framing.encode(b"ok")
        oversized = framing.encode(b"x" * 9)

        with raises(FrameSizeError) as exc_info:
            reader.feed(confirmed + oversized)

        assert exc_info.value.complete == [b"ok"]
        assert reader.remaining == oversized


class TestBinaryLengthReader:
    def test_header_split_across_chunks(self) -> None:
        reader = BinaryLengthReader()
        data = b"\x00\x00\x00\x04test\x00\x00\x00\x04next"

        assert reader.feed(data[:2]) == []
        assert reader.feed(data[2:5]) == []
        assert reader.feed(data[5:]) == [b"test", b"next"]
        assert reader.remaining == b""

    def test_zero_length_frame_is_consumed_without_message(self) -> None:
        reader = BinaryLengthReader()

        assert reader.feed(b"\x00\x00\x00\x00") == []
        assert reader.remaining == b""
        assert reader.feed(b"\x00\x00\x00\x02ok") == [b"ok"]
        assert reader.remaining == b""

    def test_oversized_length_rejected_before_payload(self) -> None:
        reader = BinaryLengthReader(max_frame_size=8)

        with raises(FrameSizeError, match="too large: 9 > 8"):
            reader.feed(b"\x00\x00\x00\x09")
        assert reader.remaining == b"\x00\x00\x00\x09"

    def test_oversized_length_after_complete_frame(self) -> None:
        reader = BinaryLengthReader(max_frame_size=8)
        data = b"\x00\x00\x00\x02ok\xff\xff\xff\xff"

        with raises(FrameSizeError) as exc_info:
            reader.feed(data)
        assert exc_info.value.complete == [b"ok"]
        assert reader.remaining == b"\xff\xff\xff\xff"

    def test_oversized_length_with_partial_payload_is_unconsumed(self) -> None:
        reader = BinaryLengthReader(max_frame_size=8)

        with raises(FrameSizeError):
            reader.feed(b"\x00\x00\x00\x09abc")
        assert reader.remaining == b"\x00\x00\x00\x09abc"

    def test_max_buffer_size_caps_declared_length(self) -> None:
        reader = BinaryLengthReader(max_buffer_size=16)

        with raises(FrameSizeError):
            reader.feed(b"\x00\x00\x00\x11")


class TestModeReaders:
    @mark.parametrize(
        ["mode", "data"],
        [
            ("1.0", b"\x00\x00\x00\x03one\x00\x00\x00\x03two"),
            ("1.1", b"\xc0one\xc0\xc0two\xc0"),
        ],
    )
    def test_make_reader(self, mode: str, data: bytes) -> None:
        reader = make_reader(mode)

        assert reader.feed(data) == [b"one", b"two"]
        assert reader.remaining == b""
        assert get_framing_for_mode(mode) is (
            BINARY_LENGTH_FRAMING if mode == "1.0" else SLIP_FRAMING
        )
        assert read_datagrams(data, mode) == [b"one", b"two"]

    def test_unknown_mode(self) -> None:
        with raises(ValueError, match="Unsupported OSC mode: 2.0"):
            get_framing_for_mode("2.0")


class TestOfflineSources:
    def test_read_frames_from_bytes(self) -> None:
        data = b"".join(
            BINARY_LENGTH_FRAMING.encode(value) for value in (b"one", b"two")
        )

        assert read_frames(data[:4], BINARY_LENGTH_FRAMING) == []
        assert read_frames(data, BINARY_LENGTH_FRAMING) == [b"one", b"two"]
        with raises(FrameSizeError):
            read_frames(data, BINARY_LENGTH_FRAMING, max_frame_size=2)

    def test_read_frames_from_file_like(self) -> None:
        from io import BytesIO

        data = b"".join(SLIP_FRAMING.encode(value) for value in (b"a", b"b"))

        assert read_frames(BytesIO(data), SLIP_FRAMING) == [b"a", b"b"]

    def test_large_offline_stream_respects_small_buffer(self) -> None:
        from io import BytesIO

        expected = [b"x" * 10] * 200
        data = b"".join(BINARY_LENGTH_FRAMING.encode(value) for value in expected)

        reader = BinaryLengthReader(max_buffer_size=32)
        assert reader.read(BytesIO(data)) == expected
        assert reader.remaining == b""

    def test_iter_frames_helper(self) -> None:
        data = b"".join(BINARY_LENGTH_FRAMING.encode(value) for value in (b"a", b"b"))

        assert list(iter_frames(data, BINARY_LENGTH_FRAMING)) == [b"a", b"b"]

    def test_concrete_reader_classes(self) -> None:
        assert SLIPReader().feed(b"\xc0x\xc0") == [b"x"]
        assert BinaryLengthReader().feed(b"\x00\x00\x00\x01x") == [b"x"]
        assert isinstance(SLIPReader(), FramingReader)
        assert isinstance(BinaryLengthReader(), FramingReader)
        assert SLIPReader().framing is not None
