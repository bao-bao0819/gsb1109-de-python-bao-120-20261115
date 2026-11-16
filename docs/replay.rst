Record & Replay
===============

The :mod:`pythonosc.replay` module records raw OSC traffic and replays it from
any point in the recording at a configurable speed.

Two time concepts are stored separately for every message:

* the **sampled time** is when the message was observed on the wire and is the
  only time used to space out a replay;
* the **scheduled time** is the original OSC bundle timetag, kept as metadata
  and never overwritten.

Messages are classified as *state* or *event* via a predicate on their address.
When a replay starts in the middle of a recording, state messages sampled before
the start point establish the initial state (only the latest message per address
is emitted, once), while earlier event messages are not re-sent.

Recording
---------

.. code-block:: python

    from pythonosc.replay import Recorder

    # Addresses starting with /state carry durable state, the rest are events.
    recorder = Recorder(state_predicate=lambda address: address.startswith("/state"))

    # Feed raw packets as they arrive, e.g. from a server callback.
    recorder.record_packet(raw_packet_bytes)

    # Recordings can be persisted and reloaded later.
    recorder.save("session.oscr")
    records = Recorder.load("session.oscr")

Replaying
---------

.. code-block:: python

    from pythonosc.replay import Replayer
    from pythonosc.udp_client import SimpleUDPClient

    client = SimpleUDPClient("127.0.0.1", 9000)
    replayer = Replayer(
        records,
        client,
        start_time=42.0,  # replay from sampled time 42s; None means the start
        rate=2.0,         # run twice as fast as the original capture
    )
    replayer.run()

Pause, resume and confirmation
-------------------------------

The replay can be paused from another thread and resumed, optionally with a new
rate.  Confirmed messages are passed to :meth:`Replayer.ack` and are never sent
again, even across a pause/resume cycle:

.. code-block:: python

    replayer.pause()
    replayer.ack(order)       # order comes from RecordedMessage.order
    replayer.resume(rate=4.0)

Replay Module Documentation
----------------------------

.. automodule:: pythonosc.replay
  :special-members:
  :members:
  :exclude-members: __weakref__
