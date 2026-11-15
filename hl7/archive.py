"""Archive a multi-order HL7 message into one independent message per order.

A single HL7 message may carry several orders (e.g. several ``ORC``/``OBR``
groups). Archiving splits that *mother message* into standalone *child
messages*, one for each order. The caller owns the segment grouping rules,
since those depend on the message type and trigger event.

Each child message receives:

* the common prefix (``MSH`` and any patient/visit/public segments) shared by
  every order, so it is a valid message on its own;
* a newly generated message control id (``MSH-10``);
* a record of where the mother message lives, so the children stay linked to
  their origin;
* exactly one real order event -- the shared, public segments are copied into
  every child rather than emitted as repeated events.

Unknown ``Z`` segments whose ownership cannot be determined are never attached
to the nearest order. They are kept aside as unassigned so the caller can route
them deliberately.
"""

import copy

from .util import generate_message_control_id

#: Name of the field that records the mother message's location.
#: Stored as a custom field on each child :py:class:`hl7.Message`.
MOTHER_LOCATION_ATTR = "mother_message_location"

#: Name of the field recording the mother message's original control id.
MOTHER_CONTROL_ID_ATTR = "mother_message_control_id"

#: Common public prefix: message header plus patient and visit segments.
#: Callers may override via :py:class:`SegmentGroupRule`.
DEFAULT_PREFIX_SEGMENTS = (
    "MSH",
    "EVN",
    "PID",
    "PD1",
    "NK1",
    "PV1",
    "PV2",
    "GT1",
    "IN1",
    "IN2",
    "IN3",
)


class SegmentGroupRule:
    """Rules describing how orders and their segments are grouped.

    :param str trigger: segment id that starts a new order group (e.g. ``ORC``
        or ``OBR``). Every occurrence begins a new archived child message.
    :param iterable(str) group_segments: segment ids that belong to the order
        group started by *trigger*. The trigger itself is always included.
        Segments are collected into the current order until another trigger is
        encountered. List any site-specific ``Z`` segments here when their
        ownership is known; unlisted ``Z`` segments stay unassigned.
    :param iterable(str) prefix_segments: segment ids that form the common
        prefix shared by every order (e.g. ``MSH``, ``PID``, ``PV1``). These are
        copied into every child rather than treated as order content.
    :param iterable(str) z_segment_prefixes: segment id prefixes that mark
        site-specific (``Z``) segments. Defaults to ``("Z",)``. A ``Z`` segment
        whose id is listed in *group_segments* or *prefix_segments* is still
        routed explicitly; any other ``Z`` segment is held as unassigned.
    """

    def __init__(
        self,
        trigger,
        group_segments=(),
        prefix_segments=DEFAULT_PREFIX_SEGMENTS,
        z_segment_prefixes=("Z",),
    ):
        self.trigger = trigger
        self.group_segments = set(group_segments)
        # The trigger always belongs to the group it starts.
        self.group_segments.add(trigger)
        self.prefix_segments = set(prefix_segments)
        self.z_segment_prefixes = tuple(z_segment_prefixes)

    def is_trigger(self, segment):
        return self._segment_id(segment) == self.trigger

    def is_prefix(self, segment):
        return self._segment_id(segment) in self.prefix_segments

    def is_group_segment(self, segment):
        return self._segment_id(segment) in self.group_segments

    def is_z_segment(self, segment):
        segment_id = self._segment_id(segment)
        return any(segment_id.startswith(prefix) for prefix in self.z_segment_prefixes)

    @staticmethod
    def _segment_id(segment):
        return segment[0][0]


class ArchivedOrder:
    """One order extracted from the mother message.

    :ivar hl7.Message message: standalone child message containing the shared
        prefix plus this order's segments, with a new control id.
    :ivar int order_index: zero-based position of this order among the mother's
        orders.
    :ivar list(hl7.Segment) segments: the order's own segments as they appeared
        in the mother message (shared prefix excluded).
    :ivar int mother_segment_index: zero-based index of the order's trigger
        segment within the mother message.
    """

    def __init__(self, message, order_index, segments, mother_segment_index):
        self.message = message
        self.order_index = order_index
        self.segments = segments
        self.mother_segment_index = mother_segment_index

    def __repr__(self):
        return "ArchivedOrder(order_index={0}, mother_segment_index={1})".format(
            self.order_index, self.mother_segment_index
        )


class ArchiveResult:
    """Outcome of archiving a multi-order message.

    :ivar list(ArchivedOrder) orders: one entry per order, in mother-message
        order.
    :ivar list(hl7.Segment) unassigned_segments: segments (typically unknown
        ``Z`` segments) whose ownership could not be determined. They are kept
        here for the caller to assign and are not attached to any order.
    :ivar hl7.Message mother: the original, unmodified mother message.
    """

    def __init__(self, orders, unassigned_segments, mother):
        self.orders = orders
        self.unassigned_segments = unassigned_segments
        self.mother = mother

    def __iter__(self):
        return iter(self.orders)

    def __len__(self):
        return len(self.orders)


def archive_message(message, rule, mother_location=None, control_id_factory=None):
    """Split a multi-order *message* into one child message per order.

    :param hl7.Message message: the mother message containing one or more
        orders. It is not modified.
    :param SegmentGroupRule rule: caller-supplied grouping rules identifying the
        order trigger, the segments owned by an order, and the common prefix.
    :param str mother_location: location of the mother message (e.g. a file
        path, queue name or archive key). Recorded on every child so the link
        back to the origin is preserved. May be ``None`` if unknown.
    :param callable control_id_factory: zero-argument callable returning a
        unique message control id. Defaults to
        :py:func:`hl7.generate_message_control_id`.
    :rtype: ArchiveResult
    :raises ValueError: if *message* contains no order trigger segment.
    """
    if control_id_factory is None:
        control_id_factory = generate_message_control_id

    prefix_segments = []
    orders = []  # list of [mother_segment_index, [segments]]
    unassigned_segments = []

    current_order = None
    for index, segment in enumerate(message):
        if rule.is_trigger(segment):
            current_order = [index, [segment]]
            orders.append(current_order)
        elif current_order is not None and rule.is_group_segment(segment):
            # A known order segment belongs to the open order.
            current_order[1].append(segment)
        elif rule.is_prefix(segment):
            # Prefix segments are shared; collected once and copied per child.
            prefix_segments.append(segment)
        elif current_order is not None and rule.is_z_segment(segment):
            # Unknown Z segment: ownership unclear, so keep it unassigned
            # rather than hanging it on the nearest order.
            unassigned_segments.append(segment)
        elif rule.is_z_segment(segment):
            # Z segment before any order starts also stays unassigned.
            unassigned_segments.append(segment)
        elif current_order is not None:
            # An unrecognised non-Z segment inside an order: treat as order
            # content so standard segments are not silently dropped.
            current_order[1].append(segment)
        else:
            # Anything before the first trigger that is not a known prefix or
            # Z segment joins the shared prefix.
            prefix_segments.append(segment)

    if not orders:
        raise ValueError(
            "No order trigger segment {0!r} found in message".format(rule.trigger)
        )

    try:
        mother_control_id = str(message.segment("MSH")(10))
    except (KeyError, IndexError):
        mother_control_id = None

    archived_orders = []
    for order_index, (mother_segment_index, order_segments) in enumerate(orders):
        child = _build_child(
            message,
            prefix_segments,
            order_segments,
            control_id_factory,
        )
        setattr(child, MOTHER_LOCATION_ATTR, mother_location)
        setattr(child, MOTHER_CONTROL_ID_ATTR, mother_control_id)
        archived_orders.append(
            ArchivedOrder(
                message=child,
                order_index=order_index,
                segments=list(order_segments),
                mother_segment_index=mother_segment_index,
            )
        )

    return ArchiveResult(
        orders=archived_orders,
        unassigned_segments=unassigned_segments,
        mother=message,
    )


def _build_child(message, prefix_segments, order_segments, control_id_factory):
    """Construct an independent child message for one order."""
    child = message.create_message([])
    child_segments = [copy.deepcopy(segment) for segment in prefix_segments]
    child_segments.extend(copy.deepcopy(segment) for segment in order_segments)
    child.extend(child_segments)

    # Give the child its own control id. The public MSH is a copied prefix,
    # so this does not mutate the mother message or any sibling.
    try:
        msh = child.segment("MSH")
    except KeyError:
        msh = None
    if msh is not None:
        msh.assign_field(str(control_id_factory()), 10)
    return child
