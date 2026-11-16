Sensor Reading De-duplication
==============================

Sensors commonly resend a reading that was not acknowledged, in which case the
retransmission carries the same sequence number, and they may sample the same
physical value several times in a row.  The
:class:`pythonosc.dedup.SequenceDeduplicator` is an ingress gate placed in
front of the warehouse so that only genuinely new readings are inserted while
retransmissions and ambiguous data are parked separately.

Identity rules
--------------

A raw sequence number is not a unique key: counters wrap and devices restart.
The gate therefore identifies each reading by four elements:

* ``stream``: the stable device/sensor identity,
* ``epoch``: incremented every time restart evidence changes,
* ``wrap``: incremented every time the sequence counter wraps inside an epoch,
* ``sequence``: the raw sequence number emitted by the device.

Restart evidence is any boot identity the device can provide (boot counter,
uptime, reset flag, session id...).  When it changes, the sequence baseline is
reset and the reading starts a new epoch, even if its raw sequence number is
not smaller than the previous one.  A wrapped counter is detected with serial
number arithmetic and also accepted; its readings get a new ``wrap`` value so
that reusing a raw sequence number after the wrap is never confused with a
retransmission.

Payload rules
-------------

* Same stream, epoch, wrap, sequence and identical payload: ``DUPLICATE``.
  The reading is kept out of the warehouse.
* Same identity but a different payload: ``CONFLICT``.  The reading is
  quarantined and never overwrites the one that arrived first; last-writer-wins
  is deliberately not used.
* Consecutive identical sampled values still have distinct sequence numbers,
  so they are admitted as separate readings.

Duplicates and conflicts are handed to the quarantine callback, which the
transport layer uses to persist them in a store separate from the warehouse.

Example
-------

.. code-block:: python

    from pythonosc.dedup import ReadingStatus, SequenceDeduplicator

    quarantine = []
    dedup = SequenceDeduplicator(
        wrap_modulus=256,
        history_size=64,
        quarantine_cb=quarantine.append,
    )

    record = dedup.admit("sensor-1", 42, {"temperature": 21.5})
    if dedup.is_warehouse_ready(record):
        # record.status is NEW, RESTART or WRAPPED.
        warehouse_key = (record.stream, record.epoch, record.wrap, record.sequence)
        ...

    # Exact retransmission: DUPLICATE, routed to the quarantine callback.
    dedup.admit("sensor-1", 42, {"temperature": 21.5})

    # Same sequence, different payload: CONFLICT, isolated, not overwritten.
    dedup.admit("sensor-1", 42, {"temperature": 22.0})

A runnable end-to-end example feeding real OSC messages through a
:class:`pythonosc.dispatcher.Dispatcher` is available in
``examples/sensor_dedup.py``.

Module Documentation
--------------------

.. automodule:: pythonosc.dedup
  :special-members:
  :members:
  :exclude-members: __weakref__
