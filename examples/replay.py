"""Record OSC packets and replay them from a chosen point in time.

Run two instances: one in record mode acting as a server, then a second one in
replay mode that re-sends the captured traffic to a target client.

Recording:
    python replay.py record --port 5005 --out session.oscr

Replaying at double speed starting at sampled time 10s:
    python replay.py replay --in session.oscr --ip 127.0.0.1 --port 9000 \
        --start 10 --rate 2
"""

import argparse
import socket

from pythonosc.replay import Recorder, Replayer
from pythonosc.udp_client import SimpleUDPClient


def record(ip: str, port: int, out_path: str) -> None:
    recorder = Recorder(state_predicate=lambda address: address.startswith("/state"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((ip, port))
    print(f"Recording on {sock.getsockname()}, press Ctrl+C to stop")
    try:
        while True:
            data, _ = sock.recvfrom(65535)
            recorder.record_packet(data)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        recorder.save(out_path)
        print(f"Saved {len(recorder.records)} messages to {out_path}")


def replay(in_path: str, ip: str, port: int, start: float | None, rate: float) -> None:
    records = Recorder.load(in_path)
    client = SimpleUDPClient(ip, port)
    replayer = Replayer(records, client, start_time=start, rate=rate)
    print(f"Replaying {len(records)} messages to {ip}:{port} from {start} at {rate}x")
    replayer.run()
    print("Replay finished")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("--ip", default="127.0.0.1")
    record_parser.add_argument("--port", type=int, default=5005)
    record_parser.add_argument("--out", default="session.oscr")

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--in", dest="in_path", default="session.oscr")
    replay_parser.add_argument("--ip", default="127.0.0.1")
    replay_parser.add_argument("--port", type=int, default=9000)
    replay_parser.add_argument("--start", type=float, default=None)
    replay_parser.add_argument("--rate", type=float, default=1.0)

    arguments = parser.parse_args()
    if arguments.mode == "record":
        record(arguments.ip, arguments.port, arguments.out)
    else:
        replay(
            arguments.in_path,
            arguments.ip,
            arguments.port,
            arguments.start,
            arguments.rate,
        )
