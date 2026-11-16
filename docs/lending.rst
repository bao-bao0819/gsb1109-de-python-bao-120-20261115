.. _lending:

Securities Lending Reconciliation
=================================

``ofxtools.lending`` reconciles securities on loan from OFX investment records
plus an explicit lending/return correspondence table.

The correspondence table is authoritative.  OFX transactions are evidence:
they never create, merge or cancel a loan on their own, and a record without an
explicit table link stays *undetermined* rather than being matched by security,
counterparty or amount.

The module depends only on the standard library at import time; helper
functions translate parsed ``INVSTMTRS`` objects lazily.

Why a Separate Table?
---------------------
OFX section 13 has no securities-lending message set.  A lend and a borrow of
the same security look identical in ``JRNLSEC``/``TRANSFER`` records apart from
a sign or a transfer action, and a return is indistinguishable from an ordinary
sale.  Inferring loans from transaction streams therefore guesses at facts the
data does not contain.  The table supplies exactly those facts - loan identity,
direction, counterparty and the FITIDs that belong to each loan - and the
reconciler checks that OFX evidence and the table agree without either side
overriding the other.

Table Rows
----------
Rows are plain dataclasses; they can also be loaded from JSON with
:func:`ofxtools.lending.table_from_dict`:

``Lend(loan_id, security, counterparty, units, date, fitid=None)``
    Units lent out under ``loan_id``.  ``units`` must be positive.
``Borrow(loan_id, security, counterparty, units, date, fitid=None)``
    Units borrowed in under a *separate* ``loan_id``.  A borrow never nets
    against a lend, even for the same security and counterparty.
``Return(loan_id, units, date, fitid=None)``
    Units returned against a loan.  Partial returns are normal.
``Recall(loan_id, units, date, fitid=None)``
    Early recall.  Reduces the on-loan balance from ``date`` with no cash
    settlement; ``units=None`` recalls everything open.
``Split(security, date, numerator, denominator)``
    Split or counter-split; open units of ``security`` scale by
    ``numerator/denominator``.
``CorporateAction(security, date, ratio, action="CORPORATEACTION")``
    Any other unit-multiplying event (stock dividend, bonus issue, reverse
    split).

``security`` must be the same stable key used for OFX ``SECID`` values;
:func:`ofxtools.lending.security_key` produces ``UNIQUEIDTYPE:UNIQUEID`` for a
parsed ``SECID``.

JSON Shape
~~~~~~~~~~
.. code:: json

    {
      "asof": "2024-12-31",
      "loans": [
        {"type": "lend", "loan_id": "L1", "security": "CUSIP:123456789",
         "counterparty": "CPT", "units": "1000", "date": "2024-01-01",
         "fitid": "F-OPEN"},
        {"type": "borrow", "loan_id": "B1", "security": "CUSIP:123456789",
         "counterparty": "CPT", "units": "500", "date": "2024-01-05",
         "fitid": "F-BORROW"}
      ],
      "returns": [
        {"loan_id": "L1", "units": "400", "date": "2024-02-01",
         "fitid": "F-RET1"}
      ],
      "recalls": [
        {"loan_id": "L1", "units": "100", "date": "2024-03-01"}
      ],
      "actions": [
        {"type": "split", "security": "CUSIP:123456789", "date": "2024-05-01",
         "numerator": "2", "denominator": "1"}
      ],
      "correspondence": {"F-OPEN": "L1", "F-RET1": "L1"},
      "expected": {"L1": "1000"}
    }

``correspondence`` maps OFX ``FITID`` values to table ``loan_id`` values.  A
``Lend``/``Borrow``/``Return``/``Recall`` row may also carry its own ``fitid``;
the two sources must agree, and a FITID linked to two different loans is a
``TableError``.

Reconciliation Semantics
------------------------
Batches
~~~~~~~
Each loan keeps one open batch per still-unsettled slice.  A partial return
settles only the units it covers and leaves the remainder as a separate open
batch; several partial returns therefore leave several settled slices and a
reduced open remainder.  Each :class:`OpenBatch` and :class:`SettledBatch`
carries a stable ``(loan_id, batch)`` address - batch numbers run consecutively
per loan across settled and open slices - plus a ``records`` tuple naming the
original table/OFX records that produced it.  :func:`result_batches` returns
both kinds together, and :meth:`Reconciliation.batch` resolves an address.

Recalls
~~~~~~~
A recall settles units like a return but with ``settlement="RECALL"`` and no
cash record.  It can be partial (``units=...``) or complete (``units=None``).
A recall may cite a FITID, in which case the OFX record must exist; a recall
without a FITID is an instruction and is not flagged as missing evidence.

Corporate Actions
~~~~~~~~~~~~~~~~~
Actions apply to every loan of the same security that is open *before* the
action date.  Units are multiplied in both directions; the action is recorded
on the affected batches in ``actions``.  A loan opened on or after the action
date is assumed to already be stated in post-action units and is left alone, as
is a loan already fully settled.

Direction Isolation
~~~~~~~~~~~~~~~~~~~
``Lend`` and ``Borrow`` loans live in separate directions.  A borrow record can
never close a lend, a return linked to a borrow never reduces a lend, and two
identical lends remain two loans.  :meth:`Reconciliation.on_loan` reports units
per security for one direction only.

Undetermined Records
~~~~~~~~~~~~~~~~~~~~
A record that cannot be tied to a loan is reported in ``unmatched`` with a
machine-readable ``reason``:

``NO_LOAN_ID``
    No explicit ``loan_id`` or FITID correspondence.
``UNKNOWN_LOAN``
    The link names a loan absent from the table.
``WRONG_SECURITY``
    The record's security differs from the loan's.
``WRONG_DIRECTION``
    A borrow record linked to a lend (or vice versa).
``WRONG_LOAN``
    An OFX return/recall linked to a different loan than its table row.
``NO_TABLE_RECORD``
    An OFX return linked to a loan with no matching return/recall row.
``NO_EVIDENCE``
    A table loan or return has no OFX record, or none was supplied.

Pass ``require_evidence=False`` when reconciling a table on its own; this
suppresses ``NO_EVIDENCE`` but leaves structural and direction checks intact.

Quantity Conservation
---------------------
:class:`QuantityMismatch` reports failed arithmetic with ``kind``:

``CONSERVATION``
    Settlements exceed the units opened (over-return or over-recall).
``EXPECTED``
    The computed open quantity differs from the table's stated ``expected``.
``EVIDENCE``
    OFX evidence disagrees with the table's opening or settlement quantities.

``records`` names the original rows involved, for example
``loan:L1@2024-01-01``, ``return:F-RET1``, ``recall:L1@2024-03-01``,
``ofx:F-RET1`` or ``action:SPLIT:2/1@2024-05-01``.  ``opening``, ``delta`` and
``closing`` give the arithmetic, and ``expected`` holds the table figure when
one exists.  Over-settlement still emits the units it could settle; the
negative remainder appears in ``closing`` and no phantom batch is created.

Usage
-----
.. code:: python

    from datetime import date

    from ofxtools.lending import Evidence, Lend, Return, reconcile

    result = reconcile(
        [Lend("L1", "CUSIP:123456789", "CPT", "1000", "2024-01-01",
              fitid="F-OPEN")],
        returns=[Return("L1", "400", "2024-02-01", fitid="F-RET1")],
        evidence=[
            Evidence("F-OPEN", "CUSIP:123456789", "1000", date(2024, 1, 1),
                     "LEND", "L1"),
            Evidence("F-RET1", "CUSIP:123456789", "400", date(2024, 2, 1),
                     "RETURN", "L1"),
        ],
    )
    result.on_loan("LEND")          # {'CUSIP:123456789': Decimal('600')}
    result.open_batches             # one batch of 600, with record trail
    result.balanced                 # True

Reconciling a parsed statement or an OFX file:

.. code:: python

    from ofxtools.lending import reconcile_statements, reconcile_file

    result = reconcile_statements(
        loans, ofx.statements,
        returns=returns,
        correspondence={"F-OPEN": "L1", "F-RET1": "L1"},
    )
    result = reconcile_file("2024-12.ofx", "lending.json")

``reconciliation_to_dict(result)`` produces JSON-compatible output including
open/settled batches, undetermined records and mismatches with their record
trails.

Command Line
------------
.. code:: bash

    python -m ofxtools.lending 2024-12.ofx lending.json

Prints the same JSON as ``reconciliation_to_dict()``.  Exit status is ``0``
when balanced, ``1`` when undetermined records or quantity mismatches remain,
and ``2`` for malformed input.  Pass ``--no-require-evidence`` to reconcile a
table on its own.

Helper Functions
----------------
``evidence_from_transaction(tx, loan_for_fitid=None)``
    Translate a parsed ``JRNLSEC`` (negative units = LEND, positive = RETURN),
    ``TRANSFER`` (``OUT`` = LEND, ``IN`` = RETURN) or ``SELLSTOCK`` (= LEND)
    into :class:`Evidence`.  Returns ``None`` for ``SPLIT``, which is a
    table-side corporate action rather than loan evidence.
``evidence_from_statement(statement, loan_for_fitid=None)``
    Translate all evidenced transactions in an ``INVSTMTRS``.
``actions_from_statement(statement)``
    Extract ``SPLIT`` records as table actions.  These are advisory: check that
    the split applies to the lending programme before passing them to
    ``reconcile()``.
``security_key(secid)``
    ``UNIQUEIDTYPE:UNIQUEID`` string for a parsed ``SECID``.
