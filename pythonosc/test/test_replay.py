import threading

import pytest

from pythonosc import osc_bundle_builder
from pythonosc import osc_message_builder
from pythonosc import replay
from pythonosc.parsing import osc_types


class FakeClient:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message.address)


class VirtualClock:
    """Deterministic clock whose time only advances via sleep."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


def make_message(address):
    return osc_message_builder.build_msg(address, (1,))


def make_recorder():
    return replay.Recorder(state_predicate=lambda address: address.startswith("/state"))


def test_sampled_and_scheduled_times_are_kept_separate():
    recorder = make_recorder()
    message = make_message("/event")
    recorder.record_message(message, sampled_time=10.0, scheduled_time=12.5)

    record = recorder.records[0]
    assert record.sampled_time == 10.0
    assert record.scheduled_time == 12.5

    client = FakeClient()
    clock = VirtualClock()
    replayer = replay.Replayer(
        recorder, client, start_time=10.0, clock=clock.time, sleep=clock.sleep
    )
    replayer.run()

    assert client.sent == ["/event"]
    assert recorder.records[0].scheduled_time == 12.5
    assert recorder.records[0].sampled_time == 10.0


def test_replay_starts_at_arbitrary_time_with_rate_scaling():
    recorder = make_recorder()
    recorder.record_message(make_message("/a"), sampled_time=0.0)
    recorder.record_message(make_message("/b"), sampled_time=10.0)
    recorder.record_message(make_message("/c"), sampled_time=30.0)

    client = FakeClient()
    clock = VirtualClock()
    replayer = replay.Replayer(
        recorder,
        client,
        start_time=10.0,
        rate=2.0,
        clock=clock.time,
        sleep=clock.sleep,
    )
    replayer.run()

    assert client.sent == ["/b", "/c"]
    assert clock.now == 1000.0 + (30.0 - 10.0) / 2.0


def test_state_before_start_builds_initial_state_events_are_dropped():
    recorder = make_recorder()
    recorder.record_message(make_message("/state/x"), sampled_time=1.0)
    recorder.record_message(make_message("/event/early"), sampled_time=2.0)
    recorder.record_message(make_message("/state/x"), sampled_time=5.0)
    recorder.record_message(make_message("/state/y"), sampled_time=6.0)
    recorder.record_message(make_message("/event/later"), sampled_time=20.0)

    client = FakeClient()
    clock = VirtualClock()
    replayer = replay.Replayer(
        recorder, client, start_time=10.0, clock=clock.time, sleep=clock.sleep
    )

    snapshot = replayer.initial_state
    assert [record.message.address for record in snapshot] == [
        "/state/x",
        "/state/y",
    ]
    assert snapshot[0].sampled_time == 5.0

    replayer.run()
    assert client.sent == ["/state/x", "/state/y", "/event/later"]


def test_nested_bundle_preserves_message_order_and_timetags():
    inner = osc_bundle_builder.OscBundleBuilder(200.0)
    inner.add_content(make_message("/inner"))
    outer = osc_bundle_builder.OscBundleBuilder(100.0)
    outer.add_content(make_message("/outer-a"))
    outer.add_content(inner.build())
    outer.add_content(make_message("/outer-b"))
    bundle = outer.build()

    recorder = make_recorder()
    recorder.record_packet(bundle.dgram, received_at=50.0)

    records = recorder.records
    assert [record.order for record in records] == [0, 1, 2]
    assert [record.message.address for record in records] == [
        "/outer-a",
        "/inner",
        "/outer-b",
    ]
    assert records[0].scheduled_time == 100.0
    assert records[1].scheduled_time == 200.0
    assert records[2].scheduled_time == 100.0
    assert all(record.sampled_time == 50.0 for record in records)


def test_immediately_bundle_uses_sampled_time_as_scheduled_time():
    builder = osc_bundle_builder.OscBundleBuilder(osc_types.IMMEDIATELY)
    builder.add_content(make_message("/now"))
    recorder = make_recorder()
    recorder.record_packet(builder.build().dgram, received_at=42.0)

    record = recorder.records[0]
    assert record.scheduled_time == 42.0
    assert record.sampled_time == 42.0


def test_pause_and_resume_never_resends_confirmed_messages():
    recorder = make_recorder()
    for index in range(4):
        recorder.record_message(
            make_message(f"/msg{index}"), sampled_time=float(index * 10)
        )

    client = FakeClient()
    clock = VirtualClock()
    replayer = replay.Replayer(
        recorder,
        client,
        start_time=0.0,
        clock=clock.time,
        sleep=lambda delay: clock.sleep(delay),
    )

    original_sleep = VirtualClock.sleep

    def pause_after_first(self, delay):
        original_sleep(self, delay)
        if client.sent == ["/msg0"]:
            replayer.pause()

    VirtualClock.sleep = pause_after_first
    thread = threading.Thread(target=replayer.run)
    thread.start()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert client.sent == ["/msg0"]

    replayer.ack(0)
    VirtualClock.sleep = original_sleep
    replayer.resume()

    assert client.sent == ["/msg0", "/msg1", "/msg2", "/msg3"]
    assert replayer.confirmed_orders == {0}


def test_resume_with_new_rate_reanchors_remaining_messages():
    recorder = make_recorder()
    recorder.record_message(make_message("/a"), sampled_time=0.0)
    recorder.record_message(make_message("/b"), sampled_time=10.0)
    recorder.record_message(make_message("/c"), sampled_time=30.0)

    client = FakeClient()
    clock = VirtualClock()
    replayer = replay.Replayer(
        recorder,
        client,
        start_time=0.0,
        rate=1.0,
        clock=clock.time,
        sleep=lambda delay: clock.sleep(delay),
    )

    original_sleep = VirtualClock.sleep

    def pause_after_first(self, delay):
        original_sleep(self, delay)
        if client.sent == ["/a"]:
            replayer.pause()

    VirtualClock.sleep = pause_after_first
    thread = threading.Thread(target=replayer.run)
    thread.start()
    thread.join(timeout=2.0)

    replayer.ack(0)
    VirtualClock.sleep = original_sleep
    paused_at = clock.now
    replayer.resume(rate=2.0)
    thread.join(timeout=2.0)

    assert client.sent == ["/a", "/b", "/c"]
    assert clock.now == paused_at + (30.0 - 10.0) / 2.0


def test_invalid_rate_is_rejected():
    recorder = make_recorder()
    with pytest.raises(replay.ReplayError):
        replay.Replayer(recorder, FakeClient(), rate=0.0)


def test_pause_before_run_then_resume_runs_everything():
    recorder = make_recorder()
    recorder.record_message(make_message("/a"), sampled_time=0.0)
    recorder.record_message(make_message("/b"), sampled_time=5.0)

    client = FakeClient()
    clock = VirtualClock()
    replayer = replay.Replayer(
        recorder,
        client,
        start_time=0.0,
        clock=clock.time,
        sleep=lambda delay: clock.sleep(delay),
    )

    replayer.pause()
    thread = threading.Thread(target=replayer.run)
    thread.start()
    thread.join(timeout=2.0)
    assert client.sent == []

    replayer.resume()
    assert client.sent == ["/a", "/b"]


def test_start_time_boundary_is_inclusive():
    recorder = make_recorder()
    recorder.record_message(make_message("/state/x"), sampled_time=10.0)
    recorder.record_message(make_message("/event/on"), sampled_time=10.0)
    recorder.record_message(make_message("/event/early"), sampled_time=9.99)

    client = FakeClient()
    clock = VirtualClock()
    replayer = replay.Replayer(
        recorder,
        client,
        start_time=10.0,
        clock=clock.time,
        sleep=lambda delay: clock.sleep(delay),
    )
    replayer.run()

    # State exactly at the start seeds the snapshot; the event exactly at the
    # start is still delivered, only strictly earlier events are dropped.
    assert client.sent == ["/state/x", "/event/on"]


def test_recording_round_trip(tmp_path):
    recorder = make_recorder()
    recorder.record_message(
        make_message("/state/x"), sampled_time=1.0, scheduled_time=2.0
    )
    recorder.record_message(make_message("/event/y"), sampled_time=3.0)

    path = tmp_path / "recording.bin"
    recorder.save(str(path))
    loaded = replay.Recorder.load(str(path))

    assert len(loaded) == 2
    assert loaded[0].message.address == "/state/x"
    assert loaded[0].kind is replay.MessageKind.STATE
    assert loaded[0].sampled_time == 1.0
    assert loaded[0].scheduled_time == 2.0
    assert loaded[1].kind is replay.MessageKind.EVENT
