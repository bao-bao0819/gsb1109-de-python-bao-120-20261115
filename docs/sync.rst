.. _sync:

Synchronization Coverage
========================

A server can answer a monthly statement request with less than the requested
period and still return a successful response.  It may also include a
``STATUS`` warning.  Neither fact is visible if you only count transactions, so
``ofxtools.Sync`` records three independent pieces of evidence per statement:

* **Requested** - the ``DTSTART``/``DTEND`` sent by the client;
* **Declared** - the ``DTSTART``/``DTEND`` in the response's ``*TRANLIST``
  (or the posting window of a closing statement);
* **Received** - the actual records present in the response.

Declared coverage is never inferred from the first and last received record.
A transaction's ``DTPOSTED`` is not a statement about which dates the server
searched.

Building A Coverage Table
-------------------------
Parse an OFX response or pass the raw bytes directly.  Supply the same request
objects that were sent so the table can show the requested range and identify
gaps:

.. code-block:: python

    import datetime
    from ofxtools.Client import StmtRq
    from ofxtools.Sync import build_coverage_table, month_range
    from ofxtools.utils import UTC

    period = month_range(2026, 9)
    request = StmtRq(acctid="123456789", accttype="CHECKING",
                     dtstart=period.start, dtend=period.end)

    with client.request_statements(password, request) as response:
        table = build_coverage_table(response.read(), [request])

    print(table)          # aligned text table
    print(table.to_csv()) # spreadsheet-friendly export

Columns distinguish the requested range, the server's declaration, the number
of records, and the range those records actually span.

Coverage And Checkpoints
------------------------
The ``CoverageLedger`` applies rules that make incremental sync safe:

* an empty transaction list closes a gap **only** when the response declares
  complete coverage of the requested range;
* a partial declaration never closes the remainder, regardless of whether
  records were returned;
* a response whose status is an error contributes no coverage and cannot
  advance a checkpoint;
* statement-level warnings are recorded in the table but do not by themselves
  invalidate a successfully declared range;
* a warning or error in the sign-on response blocks coverage from that
  response, since the session itself was qualified;
* a checkpoint advances only once the entire requested window is covered and
  there is no gap between it and the previous checkpoint.

.. code-block:: python

    from ofxtools.Sync import CoverageLedger

    ledger = CoverageLedger()
    update = ledger.apply(table[0])
    print(update.checkpoint_advanced, update.gaps)

``CoverageLedger`` can be persisted with ``save()``/``load()``.  The
``Synchronizer`` class wraps the request/parse/ledger sequence and also offers
``request_month()`` for a calendar-month request.

Inspecting A Response
---------------------
When the request range is not available, ``coverage_from_response()`` still
reports declared coverage and received records; it simply cannot mark a gap as
closed because there is nothing to compare against.

Responses that omit the statement aggregate entirely still produce an error
row, allowing a caller to record that the checkpoint was left untouched.
