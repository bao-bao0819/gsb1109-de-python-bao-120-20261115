"""Offline resolution of OSC bundles into an executable timeline.

Unlike :class:`pythonosc.osc_packet.OscPacket`, this module never reads the
wall clock and never assumes "today".  It walks a (possibly deeply nested)
OSC packet purely from the time-tags it carries and produces the messages in
their execution order.

OSC 1.0 time-tag rules implemented here:

* A bundle's time-tag applies to every element it directly contains.
* A nested element executes at ``max(ancestor time-tags, its own time-tag)``;
  an inner schedule can never fire before an enclosing bundle allowed it to.
* The special ``IMMEDIATELY`` time-tag (``0`` after parsing) means "as soon as
  the packet is received" and imposes no constraint of its own.
* A message whose time can only be expressed as "immediately" has no absolute
  time base, so its time is left :data:`UNDETERMINED` instead of being silently
  stamped with the current date.

Each bundle element is parsed independently.  One malformed message or nested
bundle is reported in :attr:`Timeline.errors` while every well-formed sibling
still lands in :attr:`Timeline.messages`, so a single bad element can never
make the rest of a packet disappear silently.
"""

from pythonosc import osc_bundle
from pythonosc import osc_message
from pythonosc.parsing import osc_types

from typing import List, NamedTuple, Optional

# Sentinel execution time for messages that only carry an IMMEDIATELY tag.
# They must be dispatched as soon as the packet is handled, but an offline
# resolver has no clock to read, so no absolute time can be assigned.
UNDETERMINED: Optional[float] = None

_BUNDLE_PREFIX = b"#bundle\x00"


class TimelineError(Exception):
    """One element of a packet could not be placed on the timeline.

    Raised for an unrecoverable packet, or collected in
    :attr:`Timeline.errors` when only an individual element is at fault and
    the surrounding elements can still be resolved.
    """


class TimedMessage(NamedTuple):
    """A message paired with the time at which it should be executed.

    Attributes:
      time: absolute execution time in seconds since the system epoch, or
        :data:`UNDETERMINED` when the message and all of its ancestors only
        carried the IMMEDIATELY time-tag.
      message: the parsed OSC message to dispatch.
    """

    time: Optional[float]
    message: osc_message.OscMessage


class Timeline(NamedTuple):
    """Result of resolving one OSC packet offline.

    Attributes:
      messages: messages ordered by execution time; messages sharing the same
        instant keep the order in which they were encoded in the packet.
      errors: one entry per element that had to be skipped, never silently.
    """

    messages: List[TimedMessage]
    errors: List[TimelineError]


def _resolve_time(own_timestamp: float, inherited: Optional[float]) -> Optional[float]:
    """Combine a bundle's time-tag with the constraint inherited from above.

    ``IMMEDIATELY`` imposes no constraint, so it resolves to the inherited
    time when there is one and stays :data:`UNDETERMINED` otherwise.  A real
    time-tag is clamped so it can never be earlier than its enclosing bundles.
    """
    if own_timestamp == osc_types.IMMEDIATELY:
        return inherited
    if inherited is None:
        return own_timestamp
    return max(own_timestamp, inherited)


def _resolve_bundle(
    dgram: bytes, inherited: Optional[float], errors: List[TimelineError]
) -> List[TimedMessage]:
    """Flatten one bundle datagram, tolerating individual bad elements."""
    if not osc_bundle.OscBundle.dgram_is_bundle(dgram):
        raise TimelineError(f"Not an OSC bundle: {dgram!r}")

    index = len(_BUNDLE_PREFIX)
    try:
        timestamp, index = osc_types.get_date(dgram, index)
    except osc_types.ParseError as error:
        # Without a time-tag the bundle cannot be scheduled at all.
        raise TimelineError(f"Could not read bundle time-tag: {error}") from error

    constraint = _resolve_time(timestamp, inherited)
    resolved: List[TimedMessage] = []

    while dgram[index:]:
        try:
            content_size, next_index = osc_types.get_int(dgram, index)
        except osc_types.ParseError as error:
            # No trustworthy size means we cannot locate the next element.
            errors.append(TimelineError(f"Could not read element size: {error}"))
            break

        if (
            content_size <= 0
            or content_size % 4
            or content_size > len(dgram) - next_index
        ):
            errors.append(TimelineError(f"Invalid bundle element size: {content_size}"))
            break

        content_dgram = dgram[next_index : next_index + content_size]
        index = next_index + content_size

        if osc_bundle.OscBundle.dgram_is_bundle(content_dgram):
            try:
                resolved.extend(_resolve_bundle(content_dgram, constraint, errors))
            except TimelineError as error:
                # The nested bundle itself is unusable; its siblings live on.
                errors.append(error)
        elif osc_message.OscMessage.dgram_is_message(content_dgram):
            try:
                message = osc_message.OscMessage(content_dgram)
            except osc_message.ParseError as error:
                errors.append(TimelineError(f"Could not parse message: {error}"))
                continue
            resolved.append(TimedMessage(constraint, message))
        else:
            errors.append(
                TimelineError(
                    f"Could not identify content type of dgram {content_dgram!r}"
                )
            )

    return resolved


def build_timeline(dgram: bytes) -> Timeline:
    """Build the offline execution timeline of a raw OSC packet.

    Args:
      dgram: a raw UDP datagram holding one OSC message or one (possibly
        nested) OSC bundle.

    Returns:
      A :class:`Timeline`.  Its messages are sorted by execution time, with
      :data:`UNDETERMINED` messages last; messages at the same instant keep
      their packet order.  Per-element failures are listed in ``errors`` while
      all remaining messages are still returned.

    Raises:
      TimelineError: when the top-level packet is not an OSC packet at all, or
        a bare top-level message cannot be parsed.
    """
    errors: List[TimelineError] = []

    if osc_bundle.OscBundle.dgram_is_bundle(dgram):
        messages = _resolve_bundle(dgram, UNDETERMINED, errors)
    elif osc_message.OscMessage.dgram_is_message(dgram):
        try:
            message = osc_message.OscMessage(dgram)
        except osc_message.ParseError as error:
            raise TimelineError(f"Could not parse message: {error}") from error
        # A bare message carries no time-tag, so its time is undetermined.
        messages = [TimedMessage(UNDETERMINED, message)]
    else:
        raise TimelineError(
            "OSC packet should at least contain an OscMessage or an OscBundle."
        )

    # Timed entries first, ascending; undetermined entries last.  Python's sort
    # is stable, so equal keys (equal instants, or all undetermined entries)
    # keep the document order produced by the depth-first walk.
    ordered = sorted(
        messages,
        key=lambda entry: (
            entry.time is None,
            entry.time if entry.time is not None else 0.0,
        ),
    )
    return Timeline(ordered, errors)
