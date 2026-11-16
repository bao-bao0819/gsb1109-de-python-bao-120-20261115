"""Parsing and conversion of NTP dates contained in datagrams."""

import datetime
import math
import struct
import time

from typing import List, NamedTuple, Union

# 63 zero bits followed by a one in the least signifigant bit is a special
# case meaning "immediately."
IMMEDIATELY = struct.pack(">Q", 1)
_IMMEDIATELY_RAW = 1

# timetag * (1 / 2 ** 32) == l32bits + (r32bits / 1 ** 32)
_NTP_TIMESTAMP_TO_SECONDS = 1.0 / 2.0**32.0
_SECONDS_TO_NTP_TIMESTAMP = 2.0**32.0

# From NTP lib.
_SYSTEM_EPOCH = datetime.date(*time.gmtime(0)[0:3])
_NTP_EPOCH = datetime.date(1900, 1, 1)
# _NTP_DELTA is 2208988800
_NTP_DELTA = (_SYSTEM_EPOCH - _NTP_EPOCH).days * 24 * 3600

# The 32-bit seconds field of a timetag wraps around every 2**32 seconds
# (~136 years); each wrap starts a new NTP era, era 0 beginning 1900-01-01.
_NTP_ERA_SECONDS = 2**32


Timestamp = NamedTuple(
    "Timestamp",
    [
        ("seconds", int),
        ("fraction", int),
    ],
)

EraCandidate = NamedTuple(
    "EraCandidate",
    [
        ("era", int),
        ("timestamp", Timestamp),
        ("system_time", float),
    ],
)


class NtpError(Exception):
    """Base class for ntp module errors."""


def parse_timestamp(timestamp: int) -> Timestamp:
    """Parse NTP timestamp as Timetag."""
    seconds = timestamp >> 32
    fraction = timestamp & 0xFFFFFFFF
    return Timestamp(seconds, fraction)


def ntp_to_system_time(timestamp: bytes) -> float:
    """Convert a NTP timestamp to system time in seconds."""
    try:
        ts = struct.unpack(">Q", timestamp)[0]
    except Exception as e:
        raise NtpError(e)
    return ts * _NTP_TIMESTAMP_TO_SECONDS - _NTP_DELTA


def system_time_to_ntp(seconds: float) -> bytes:
    """Convert a system time in seconds to NTP timestamp."""
    try:
        seconds = seconds + _NTP_DELTA
    except TypeError as e:
        raise NtpError(e)
    return struct.pack(">Q", int(seconds * _SECONDS_TO_NTP_TIMESTAMP))


def ntp_time_to_system_epoch(seconds: float) -> float:
    """Convert a NTP time in seconds to system time in seconds."""
    return seconds - _NTP_DELTA


def system_time_to_ntp_epoch(seconds: float) -> float:
    """Convert a system time in seconds to NTP time in seconds."""
    return seconds + _NTP_DELTA


def infer_ntp_era_candidates(
    timestamp: Union[int, bytes], window_start: float, window_end: float
) -> List[EraCandidate]:
    """Infer which NTP era(s) a raw timetag may belong to.

    Old recordings often only keep the raw 64-bit OSC timetag counts together
    with a known recording time window.  Because the 32-bit seconds field
    wraps every 2**32 seconds, the era cannot be recovered from the counts
    alone; this returns every era whose absolute time falls inside the
    window.  The inference relies only on the window, never on the current
    system time, so windows spanning an era wraparound are resolved from
    the window itself.

    Args:
      timestamp: Raw 64-bit timetag as an int count or 8-byte big-endian
        bytes, as found in the recording.
      window_start: Start of the recording window, system time in seconds.
      window_end: End of the recording window, system time in seconds.

    Returns:
      A list of EraCandidate, oldest era first, each keeping the original
      seconds/fraction counts.  Empty if no era fits the window; more than
      one entry means the era cannot be uniquely determined.

    Raises:
      NtpError: If the timetag is the "immediately" special value (which is
        not an absolute time), is out of range, or the window is invalid.
    """
    if isinstance(timestamp, bytes):
        try:
            timestamp = struct.unpack(">Q", timestamp)[0]
        except Exception as e:
            raise NtpError(e)
    if timestamp == _IMMEDIATELY_RAW:
        raise NtpError("timetag of 1 means 'immediately', not an absolute time")
    if not 0 <= timestamp < 2**64:
        raise NtpError("timetag {!r} is not a 64-bit count".format(timestamp))
    if window_end < window_start:
        raise NtpError("window end is before window start")

    ts = parse_timestamp(timestamp)
    base = ts.seconds + ts.fraction * _NTP_TIMESTAMP_TO_SECONDS - _NTP_DELTA
    # Absolute system time for era k is base + k * _NTP_ERA_SECONDS; scan a
    # slightly wider era range and filter exactly to stay robust at window
    # boundaries despite floating point rounding.
    era_start = max(0, math.floor((window_start - base) / _NTP_ERA_SECONDS) - 1)
    era_end = max(0, math.ceil((window_end - base) / _NTP_ERA_SECONDS) + 1)
    return [
        EraCandidate(era, ts, base + era * _NTP_ERA_SECONDS)
        for era in range(era_start, era_end + 1)
        if window_start <= base + era * _NTP_ERA_SECONDS <= window_end
    ]
