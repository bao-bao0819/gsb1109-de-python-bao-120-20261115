"""Split OSC bundles into packets that respect a maximum byte size.

Downstream consumers (e.g. UDP senders) often require every OSC packet to
stay within a fixed byte budget. This module regroups the contents of an
OscBundle into as few packets as needed so that each packet's encoded
datagram does not exceed that budget, without ever splitting a message.

Guarantees:
  - Messages are atomic: a message is never split and blobs are never
    truncated.
  - The original timetag of each (nested) bundle is preserved verbatim on
    every packet produced from it, and contents keep their original order.
  - Sizes are computed from the actual encoded datagrams, which already
    include the 4-byte alignment padding of strings, blobs and elements.
"""

from typing import List, Union

from pythonosc import osc_bundle
from pythonosc import osc_message
from pythonosc.parsing import osc_types

# Length in bytes of the bundle header: the b"#bundle\x00" prefix followed
# by the 8-byte OSC timetag.
_BUNDLE_PREFIX_LEN = 8
_TIMETAG_LEN = 8
_BUNDLE_HEADER_LEN = _BUNDLE_PREFIX_LEN + _TIMETAG_LEN
# Length in bytes of the int32 size prefix preceding every bundle element.
_ELEMENT_SIZE_LEN = 4

_BundleContent = Union[osc_message.OscMessage, osc_bundle.OscBundle]


class BundleSplitError(Exception):
    """Raised when a bundle cannot be split within the given size limit."""


def split_bundle(
    bundle: osc_bundle.OscBundle, max_size: int
) -> List[osc_bundle.OscBundle]:
    """Regroup a bundle's contents into packets of at most max_size bytes.

    Args:
      - bundle: the OscBundle whose contents should be regrouped.
      - max_size: maximum allowed length in bytes of each returned packet's
                  datagram, must at least fit an empty bundle (16 bytes).

    Returns:
      - the list of OscBundle packets, in the original content order, each
        carrying the original bundle's timetag. A bundle that already fits
        is returned as a single packet.

    Raises:
      - ValueError: if max_size cannot even hold an empty bundle.
      - BundleSplitError: if a single message exceeds the limit (messages
        are never split and blobs never truncated), or if a nested bundle
        cannot be regrouped without breaking its nesting semantics.
    """
    if max_size < _BUNDLE_HEADER_LEN:
        raise ValueError(
            f"max_size must be at least {_BUNDLE_HEADER_LEN} bytes "
            f"(empty bundle), got {max_size}"
        )
    if bundle.num_contents == 0:
        if bundle.size > max_size:
            raise BundleSplitError(
                f"Empty bundle of {bundle.size} bytes exceeds the {max_size} "
                "bytes limit"
            )
        return [bundle]
    timetag = bundle.dgram[_BUNDLE_PREFIX_LEN:_BUNDLE_HEADER_LEN]
    return _split_contents(list(bundle), timetag, max_size)


def _split_contents(
    contents: List[_BundleContent], timetag: bytes, max_size: int
) -> List[osc_bundle.OscBundle]:
    """Greedily pack contents in order into packets of at most max_size bytes."""
    packets = []  # type: List[osc_bundle.OscBundle]
    current = []  # type: List[_BundleContent]
    current_len = _BUNDLE_HEADER_LEN
    for content in contents:
        for element in _fitting_elements(content, max_size):
            cost = _ELEMENT_SIZE_LEN + element.size
            if current and current_len + cost > max_size:
                packets.append(_build_packet(timetag, current))
                current = []
                current_len = _BUNDLE_HEADER_LEN
            current.append(element)
            current_len += cost
    if current:
        packets.append(_build_packet(timetag, current))
    return packets


def _fitting_elements(content: _BundleContent, max_size: int) -> List[_BundleContent]:
    """Return the element(s) through which content joins a parent packet.

    Each returned element fits, together with its int32 size prefix, inside
    a packet of max_size bytes. Nested bundles are kept whole whenever
    possible; otherwise they are split recursively at their own nesting
    level so their timetag semantics are preserved.
    """
    if _BUNDLE_HEADER_LEN + _ELEMENT_SIZE_LEN + content.size <= max_size:
        return [content]
    if isinstance(content, osc_message.OscMessage):
        raise BundleSplitError(
            f"Message {content.address!r} is {content.size} bytes and does "
            f"not fit in a {max_size} bytes packet; messages are never "
            "split and blobs are never truncated"
        )
    if isinstance(content, osc_bundle.OscBundle):
        if content.num_contents == 0:
            raise BundleSplitError(
                f"Empty nested bundle of {content.size} bytes does not fit "
                f"in a {max_size} bytes packet; refusing to drop it"
            )
        # Split the nested bundle at its own level, keeping its timetag, so
        # that each resulting sub-bundle fits as a single element of an
        # otherwise empty parent packet.
        sub_timetag = content.dgram[_BUNDLE_PREFIX_LEN:_BUNDLE_HEADER_LEN]
        sub_limit = max_size - _BUNDLE_HEADER_LEN - _ELEMENT_SIZE_LEN
        return [
            packet for packet in _split_contents(list(content), sub_timetag, sub_limit)
        ]
    raise BundleSplitError(f"Unsupported bundle content type: {type(content)}")


def _build_packet(
    timetag: bytes, elements: List[_BundleContent]
) -> osc_bundle.OscBundle:
    """Build one packet from the verbatim timetag and the given elements."""
    dgram = b"#bundle\x00" + timetag
    for element in elements:
        dgram += osc_types.write_int(element.size)
        dgram += element.dgram
    return osc_bundle.OscBundle(dgram)
