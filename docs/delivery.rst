Delivery Status
===============

HL7 control IDs (``MSH-10``) are unique per sending system, not globally.
When two systems reuse a control ID, a receipt must be matched using both the
control ID and direction evidence before it can change a delivery status.

:func:`hl7.build_delivery_table` reconciles outbound messages with their
acknowledgments.  It accepts :py:class:`hl7.Message` objects, raw HL7 strings,
batches, or mappings and returns a :class:`hl7.DeliveryTable`.

Receipt layers
--------------

Each receipt is kept in one of two independent layers:

``receive``
    The receiving interface or transport accepted or rejected the message.
    Typical codes are ``AA`` and ``AR``; ``CA``, ``CR`` and ``CE`` are
    treated as receive-layer commit acknowledgments.

``application``
    The receiving application accepted or rejected the message content.
    Typical codes are ``AA``, ``AR`` and ``AE``.

A receipt with an explicit ``layer`` (in a mapping or an ``MSA-3`` description)
is assigned directly.  Otherwise the layer is inferred from the
acknowledgment code: commit codes (``CA``, ``CR``, ``CE``) are receive-layer
and original-mode codes (``AA``, ``AR``, ``AE``) are application-layer.  A
receipt with no code that identifies its layer remains a candidate.

The aggregate status of each layer is computed independently:

* ``accepted`` - at least one acceptance and no failure.
* ``rejected`` - ``AR`` or ``CR`` in that layer.
* ``error`` - ``AE`` or ``CE`` in that layer.
* ``pending`` - no receipt in that layer.
* ``unknown`` - a receipt exists but its code is not recognized.

A failure is sticky within a layer.  A later acceptance never overrides an
earlier rejection.  The final status is ``application_rejected`` or
``application_error`` whenever the application layer failed, even if the
receive layer accepted first.

Retransmissions and late receipts
---------------------------------

Messages with the same sender, receiver and control ID are grouped into one
row.  Every transmission is retained as an attempt, so ``attempts`` counts the
original plus all retransmissions, and ``attempt_times`` lists each attempt.
Receipts are never deleted.  When ``late_after`` is supplied, receipts received
after the first attempt plus the timeout are flagged ``late`` (``!`` in the
formatted table) and still counted in the layer status.  The layer status
columns show the deciding code and outcome; ``late`` and ``application_late``
list the corresponding late receipts with their attempt number.

Ambiguous receipts
------------------

A receipt is assigned only when its control ID and direction evidence identify
exactly one delivery.  Direction evidence comes from ``MSH-3``/``MSH-5`` on the
ACK, or an explicit ``source``.  If several deliveries have the same control
ID and the receipt does not distinguish them, the receipt is listed in the
candidate table with every possible match and a reason such as
``insufficient_source_evidence``, ``multiple_direction_matches`` or
``direction_conflict``.  Candidates are not attached to a delivery row and
never change a status, so one receipt is never claimed by two messages.

Example
-------

.. code-block:: python

    from hl7.delivery import build_delivery_table, format_delivery_table

    table = build_delivery_table(messages, receipts, late_after=3600)
    print(format_delivery_table(table))
    for row in table:
        print(row.key, row.attempt_count, row.final_status)
    for candidate in table.as_candidates():
        print(candidate["control_id"], candidate["reason"])
