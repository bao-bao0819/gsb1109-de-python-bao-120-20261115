Order Archiving
===============

A single HL7 result message can contain several orders.  Archiving such a
message means splitting it into one self-contained message per order, while
keeping enough information to trace each child back to the parent.

The caller supplies the segment group rules, because the layout of order
groups is site- and message-type-specific.  A rule names the segment that
starts an order and the segments that belong to it::

    >>> import hl7
    >>> from hl7.archive import SegmentGroupRule, split_orders

.. testsetup:: *

    import hl7
    from hl7.archive import SegmentGroupRule, split_orders
    message = "\r".join([
        "MSH|^~\\&|LAB|FAC|EMR|HOSP|20260101120000||ORU^R01|PARENT-1|P|2.5",
        "PID|1||MRN1^^^HOSP^MR||DOE^JANE||19800101|F",
        "PV1|1|I|WARD^101^A",
        "ORC|RE|ORD-1|FIL-1",
        "OBR|1|ORD-1|FIL-1|GLUCOSE",
        "OBX|1|NM|GLUCOSE||100|mg/dL",
        "ZDS|no-clear-owner",
        "ORC|RE|ORD-2|FIL-2",
        "OBR|2|ORD-2|FIL-2|SODIUM",
        "OBX|1|NM|SODIUM||140|mmol/L",
    ])
    parsed = hl7.parse(message)
    rules = [SegmentGroupRule("ORC", ["ORC", "OBR", "OBX", "NTE"])]

.. doctest::

    >>> result = split_orders(parsed, rules)
    >>> len(result)
    2
    >>> [child.order_key for child in result]
    ['ORD-1', 'ORD-2']

Each child is a normal :py:class:`hl7.Message` with:

* a newly generated ``MSH-10`` control ID (the parent's ID is retained in
  metadata instead),
* the patient and visit segments it needs,
* the common prefix segments before the first order,
* only the segments belonging to that order.

.. doctest::

    >>> first, second = result
    >>> [segment[0][0] for segment in first]
    ['MSH', 'PID', 'PV1', 'ORC', 'OBR', 'OBX']
    >>> first.parent_message_control_id
    'PARENT-1'
    >>> first["MSH.10"] == "PARENT-1"
    False
    >>> first.parent_segment_number
    4

The patient and visit segments are copied into every child so each archive is
self-contained, but they are tagged as common.  Copying them does not create
additional clinical events, and the result exposes only one copy of each unique
common segment:

.. doctest::

    >>> result.event_count
    6
    >>> [segment[0][0] for segment in result.common_segments]
    ['PID', 'PV1']
    >>> result.common_occurrences
    4

Unknown ``Z`` segments are deliberately conservative.  A ``Z`` segment is only
attached to an order when a rule claims it explicitly, for example::

    SegmentGroupRule("ORC", ["ORC", "OBR"], z_segments=["ZDS"])

Otherwise it is returned as pending, together with its position in the parent
message.  It is never attached to the nearest preceding order merely because of
its position:

.. doctest::

    >>> [(segment[0][0], segment.source_index, segment.reason)
    ...  for segment in result.unassigned]
    [('ZDS', 6, 'unknown_z_segment')]

Parent positions
----------------

Every child records where it came from:

``parent_message_control_id``
    The parent's ``MSH-10``.

``parent_segment_index`` / ``parent_segment_number``
    0-based / 1-based index of the first order segment in the parent.

``parent_segment_range``
    Half-open ``(start, end)`` range of the order's source segments.

``parent_order_segment_indexes`` / ``parent_common_segment_indexes``
    Source indexes of the copied order and common segments.

``parent_location``
    A :py:class:`hl7.archive.ParentLocation` with the same information.

For a :py:class:`hl7.Batch` or :py:class:`hl7.File`, each contained message is
split and ``parent_message_position`` identifies which message the child came
from.  Control IDs are unique across the whole result.

Caller-supplied control IDs
---------------------------

A custom generator can be passed with ``control_id_factory``.  It is called
with ``(parent_control_id, order_number)`` when it accepts two positional
arguments, with ``(order_number)`` when it accepts one, or with no arguments::

    >>> result = split_orders(
    ...     parsed,
    ...     rules,
    ...     control_id_factory=lambda parent_id, n: "{}-{}".format(parent_id, n),
    ... )
    >>> [child["MSH.10"] for child in result]
    ['PARENT-1-1', 'PARENT-1-2']
