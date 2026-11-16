Applying Incremental Messages
=============================

HL7 v2 messages frequently act as updates: an ``ADT^A08`` message carries the
patient details that were corrected, not necessarily a complete record.  The
:py:class:`hl7.PatientRecord` class merges those messages with field level
rules and records where every change came from.

Field rules
-----------

The default policy matches the usual HL7 update semantics:

* a field that is absent from the message is left untouched;
* a field present but empty is also left untouched;
* the HL7 null value (``""``) clears the value at that repetition; a lone null
  addresses the whole field;
* repetitions are independent, so clearing one repetition does not remove the
  others in the same field.

.. doctest::

    >>> import hl7
    >>> base = 'MSH|^~\\&|HIS|HOSP|LAB|LABF|20240101080000||ADT^A08|BASE|P|2.5\r'
    >>> base += 'PID|1||P123^^^MRN~ALT99^^^OLD||SMITH^JOHN^Q\r'
    >>> record = hl7.PatientRecord(base)

An update that only supplies the fields it corrects:

.. doctest::

    >>> update = 'MSH|^~\\&|HIS|HOSP|LAB|LABF|20240102090000||ADT^A08|UPD|P|2.5\r'
    >>> update += 'PID|1||P123^^^MRN~""~X9^^^NEW||SMITH^JANE^Q\r'
    >>> changes = record.apply(update)
    >>> record.value('PID.3.1')
    'P123^^^MRN'

The explicit null cleared only the second repetition; the first and third
repetitions remain addressable at their original positions:

.. doctest::

    >>> record.value('PID.3.2')
    ''
    >>> record.value('PID.3.3')
    'X9^^^NEW'

A supplied repetition is taken as the complete new value for that repetition.
A null inside a component position, such as ``SMITH^""^Q``, clears just that
component; components omitted from the end of the value are not inherited.
.. doctest::

    >>> record.value('PID.5')
    'SMITH^JANE^Q'

Changing the policy
-------------------

:py:class:`hl7.FieldPolicy` controls the three cases separately.  Pass a
:py:class:`hl7.RecordPolicy` to apply a default or per-field overrides:

.. doctest::

    >>> policy = hl7.RecordPolicy(
    ...     default=hl7.FieldPolicy(empty='preserve'),
    ...     fields={'PID.10': hl7.FieldPolicy(empty='clear')},
    ... )

The actions accept ``preserve`` (or ``keep``) and ``clear`` (or ``delete``).

Late messages
-------------

Each repetition tracks the timestamp of the message that supplied its value.
MSH-7 is the authoritative order; the order in which messages were applied
breaks ties.  A message older than the stored value is ignored instead of
overwriting newer data:

.. doctest::

    >>> late = 'MSH|^~\\&|HIS|HOSP|LAB|LABF|20231231090000||ADT^A08|LATE|P|2.5\r'
    >>> late += 'PID|1||P123^^^MRN~Z1^^^LATE~""~Z3||SMITH^LATE^Q\r'
    >>> ignored = record.apply(late)
    >>> record.value('PID.3.1')
    'P123^^^MRN'
    >>> record.value('PID.3.3')
    'X9^^^NEW'
    >>> record.value('PID.5')
    'SMITH^JANE^Q'

A cleared repetition also keeps its clear time, so an older message cannot
resurrect the value that a newer message cleared.

Change provenance
-----------------

Every accepted change is available from
:py:attr:`hl7.PatientRecord.changes`.  Each :py:class:`hl7.FieldChange` records
the old and new values plus the original position in the source message:
message control id, message index, segment occurrence, field and repetition.

.. doctest::

    >>> change = next(c for c in changes if c.path == 'PID.5.1')
    >>> change.old_value, change.new_value
    ('SMITH^JOHN^Q', 'SMITH^JANE^Q')
    >>> change.location.message_control_id
    'UPD'
    >>> change.location.message_index
    2

:py:meth:`hl7.PatientRecord.provenance` returns the location of the value that
is currently stored, while :py:meth:`hl7.PatientRecord.history_for` returns the
accepted changes below a path.

Reconstructing the record
-------------------------

:py:meth:`hl7.PatientRecord.to_message` builds a normal
:py:class:`hl7.Message`, and ``str(record)`` returns the HL7 text:

.. doctest::

    >>> str(record).startswith('MSH|^~\\&|HIS|HOSP|LAB|LABF|20240102090000')
    True
