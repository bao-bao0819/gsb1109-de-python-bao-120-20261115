Stable Segment Occurrences and Field Issues
==========================================

Accessor paths such as ``PID.3`` or ``PID2.3`` describe a *position* in the
message.  They are convenient for reading and writing, but they are not a
stable identity: cleaning a message may insert a segment (shifting later
positions) or delete one (letting a different segment occupy the old path).

Registering a field-level issue against an accessor path alone would therefore
mis-associate the issue after such edits.  The library keeps the two concepts
apart:

* :class:`hl7.SegmentOccurrence` is a stable handle for one segment **object**.
  Its ``occurrence_id`` never changes while that object exists.
* :class:`hl7.FieldIssue` binds an issue to an occurrence.  ``original_path``
  records where the field was at registration time; ``current_path`` is
  recalculated from the message's current order.

Identity is always object identity.  Same-named segments with identical field
values remain distinct occurrences, and an issue is never re-attached by
comparing text.

Registering an issue
--------------------

.. doctest::

    >>> import hl7
    >>> message = 'MSH|^~\\&|LAB\rPID|1||MISSING\r'
    >>> h = hl7.parse(message)

    >>> original_pid = h.occurrence('PID')
    >>> issue = h.issues.register(
    ...     original_pid, field_num=3, description='patient id missing'
    ... )
    >>> issue.original_path
    'PID.3'
    >>> issue.current_path
    'PID.3'

The target may be a :class:`hl7.Segment`, a
:class:`hl7.SegmentOccurrence`, an :class:`hl7.Accessor`, an accessor key such
as ``"PID2.3"``, or an occurrence id.

Segments inserted during cleaning
---------------------------------

Insert a segment before the original one.  The issue still resolves to the
original segment; only the current path moves:

.. doctest::

    >>> inserted = hl7.parse('MSH|^~\\&|LAB\rPID|1||NEW\r')[1]
    >>> h.insert(1, inserted)

    >>> issue.original_path
    'PID.3'
    >>> issue.current_path
    'PID2.3'
    >>> issue.resolve() is original_pid.segment
    True

Note that the message itself now has two ``PID`` segments, and ``PID.3`` refers
to the newly inserted one.  The issue does not follow it.

Deleting the original occurrence
--------------------------------

Deleting the occurrence invalidates the association.  The issue is not moved to
the segment that takes over the old position:

.. doctest::

    >>> removed = h.remove_segment(original_pid)
    >>> removed is original_pid.segment
    True
    >>> issue.is_stale
    True
    >>> issue.current_path is None
    True
    >>> h['PID.3']
    'NEW'
    >>> issue.resolve()  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    hl7.exceptions.MissingOccurrenceError: segment occurrence ... is no longer in the message

A removed occurrence stays stale even if the same object is inserted again.
The registry can be queried for live and stale entries:

.. doctest::

    >>> h.issues.valid()
    ()
    >>> len(h.issues.orphaned())
    1

Same-named segments with identical content
------------------------------------------

Content is never used to pick an occurrence.  Both the identity and the path
remain exact even when two segments are byte-for-byte identical:

.. doctest::

    >>> h = hl7.parse('MSH|^~\\&|LAB\rPID|1||A\rPID|1||A\r')
    >>> first, second = h.occurrences('PID')
    >>> first == second
    False
    >>> first.occurrence_id == second.occurrence_id
    False

    >>> issue = h.issues.register(second, field_num=3)
    >>> issue.original_path
    'PID2.3'

    >>> removed = h.remove_segment(first)
    >>> issue.current_path
    'PID.3'

Deleting the original second segment makes the issue stale rather than letting
it match the remaining look-alike:

.. doctest::

    >>> removed = h.remove_segment(second)
    >>> issue.is_stale
    True
    >>> issue.current_path is None
    True

API summary
-----------

* :meth:`hl7.Message.occurrence` returns a :class:`hl7.SegmentOccurrence` by
  segment name, key, occurrence id, or segment object.
* :meth:`hl7.Message.occurrences` lists occurrences, optionally filtered by
  segment identifier.
* :meth:`hl7.Message.remove_segment` removes by object identity.
  ``Message.remove``/``pop``/``del``/``clear`` also retire the affected
  occurrences.
* :attr:`hl7.Message.issues` is the message's
  :class:`hl7.FieldIssueRegistry`.
* :class:`hl7.FieldIssue` exposes ``occurrence_id``, ``original_path``,
  ``current_path``, ``is_valid``/``is_stale``, ``resolve()``, and
  ``current_value()``.
* :class:`hl7.MissingOccurrenceError` is raised when a stale issue is resolved.
