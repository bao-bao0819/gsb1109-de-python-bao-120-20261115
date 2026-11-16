"""Warehouse ingestion gate for sensor readings carried over OSC.

Sensor readings arrive as OSC messages on ``/sensor/<id>/reading`` with the
arguments ``(sequence, value, boot_counter)``.  A SequenceDeduplicator sits in
front of the warehouse:

* admitted readings (new / wrapped / restart) are written to the warehouse,
* retransmissions and same-sequence conflicts are parked in a separate store
  through the quarantine callback, never overwriting warehouse data.

The feed is simulated with real OSC datagrams, so no socket is required.
"""

from pythonosc.dedup import ReadingRecord, SequenceDeduplicator
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_message_builder import OscMessageBuilder

# Small 8-bit style counter so a wrap can be demonstrated in a few messages.
WRAP_MODULUS = 16

warehouse = {}
quarantine_store = []


def park(record: ReadingRecord) -> None:
    """Transport-side fallback: store the rejected reading separately."""
    quarantine_store.append(record)


dedup = SequenceDeduplicator(
    wrap_modulus=WRAP_MODULUS, history_size=8, quarantine_cb=park
)
dispatcher = Dispatcher()


def handle_reading(address, sequence, value, boot_counter):
    # The OSC address /sensor/<id>/reading carries the stream identity.
    stream = address.split("/")[2]
    record = dedup.admit(stream, sequence, value, restart_evidence=boot_counter)
    if dedup.is_warehouse_ready(record):
        warehouse[(record.stream, record.epoch, record.wrap, record.sequence)] = (
            record.payload
        )
    return None


dispatcher.map("/sensor/*/reading", handle_reading)


def feed(stream, sequence, value, boot_counter):
    builder = OscMessageBuilder(address=f"/sensor/{stream}/reading")
    builder.add_arg(sequence)
    builder.add_arg(value)
    builder.add_arg(boot_counter)
    packet = builder.build().dgram
    dispatcher.call_handlers_for_packet(packet, ("127.0.0.1", 0))


def main():
    # Normal stream, including two consecutive identical sampled values.
    feed("temp-1", 0, 21.5, boot_counter=1)
    feed("temp-1", 1, 21.5, boot_counter=1)
    # Retry: same sequence, same payload.
    feed("temp-1", 1, 21.5, boot_counter=1)
    # Same sequence reused with a different payload: conflict, isolated.
    feed("temp-1", 1, 22.0, boot_counter=1)
    # Advance the counter to its maximum value.
    for sequence in range(2, WRAP_MODULUS):
        feed("temp-1", sequence, 20.0 + sequence, boot_counter=1)
    # Counter wraps 15 -> 0, the new reading must not be suppressed.
    feed("temp-1", 0, 24.0, boot_counter=1)
    # A pre-wrap reading replayed after the wrap is still a duplicate.
    feed("temp-1", 15, 35.0, boot_counter=1)
    # Device reboots and restarts its sequence number at 0: new epoch.
    feed("temp-1", 0, 18.0, boot_counter=2)

    print("warehouse:")
    for key, payload in sorted(warehouse.items()):
        print(f"  {key} -> {payload}")

    print("quarantine (stored separately):")
    for record in quarantine_store:
        print(
            f"  {record.status.value:9s} stream={record.stream} "
            f"epoch={record.epoch} wrap={record.wrap} "
            f"seq={record.sequence} payload={record.payload}"
        )


if __name__ == "__main__":
    main()
