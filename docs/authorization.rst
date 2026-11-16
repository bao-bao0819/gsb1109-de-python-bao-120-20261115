.. _authorization:

Authorization Ledger
====================

``ofxtools.authorization`` reconciles bank debit flows against a local ledger
of standing authorizations (mandates).  Each authorization fixes a currency, a
per-transaction cap, a per-period cumulative cap, and a validity window.

The ledger enforces strict matching:

* A debit flow must carry an explicit authorization reference.  A flow without
  a reference is rejected - the ledger never substitutes another
  authorization that happens to have remaining capacity.
* The referenced authorization must be in force on the posting date, be
  denominated in the same currency, accept the flow under its per-transaction
  cap, and retain enough period capacity.
* Reversals release only the period and amount originally consumed, even when
  the reversal posts in a later period or after the authorization is amended.
* Amendments take effect at their boundary; earlier consumption stays bound
  to the version in force when the original flow posted.
* A repeated flow identifier is treated as a duplicate and never consumes
  capacity a second time.

Defining Authorizations and Flows
---------------------------------

.. code-block:: python

    import datetime
    from decimal import Decimal
    from ofxtools.authorization import (
        Authorization,
        AuthorizationLedger,
        Flow,
        PeriodGranularity,
    )

    authorization = Authorization(
        auth_id="AUTH-1",
        currency="USD",
        per_transaction_limit=Decimal("100.00"),
        period_limit=Decimal("250.00"),
        valid_from=datetime.date(2026, 1, 1),
        period=PeriodGranularity.MONTH,  # DAY / WEEK / MONTH / YEAR
        valid_to=None,                    # inclusive; None means open-ended
    )

    ledger = AuthorizationLedger([authorization])

    flow = Flow(
        flow_id="F1",
        posted=datetime.datetime(2026, 1, 10, tzinfo=datetime.timezone.utc),
        amount=Decimal("100.00"),
        currency="USD",
        auth_ref="AUTH-1",
    )
    result = ledger.process(flow)
    result.period_consumed  # Decimal("100.00")
    result.available         # Decimal("150.00")

Strict processing raises a subclass of ``LedgerError`` for the first rule
violation, for example ``MissingAuthorizationError``,
``UnknownAuthorizationError``, ``AuthorizationNotInForceError``,
``CurrencyMismatchError``, ``PerTransactionLimitExceededError`` or
``PeriodLimitExceededError``.

Batch Reconciliation
--------------------

``reconcile`` processes a batch in chronological order and collects
violations as findings instead of raising.  Valid flows are still booked, and
duplicates appear as warnings rather than rejections.

.. code-block:: python

    report = ledger.reconcile([flow1, flow2, flow3])
    report.is_clean            # False if any ERROR-level finding exists
    report.errors              # tuple of Finding (rule violations)
    report.warnings            # tuple of Finding (e.g. duplicate flows)
    report.consumption         # capacity snapshot per version and period
    report.result_for("F1")    # FlowResult lookup

Reversals
---------

A reversal names the original ``flow_id``.  It releases capacity in the
original flow's period, against the authorization version that was in force at
the time - regardless of when the reversal posts.

.. code-block:: python

    from ofxtools.authorization import FlowType

    reversal = Flow(
        flow_id="R1",
        posted=datetime.datetime(2026, 2, 3, tzinfo=datetime.timezone.utc),
        amount=Decimal("100.00"),
        currency="USD",
        auth_ref="AUTH-1",
        kind=FlowType.REVERSAL,
        reversal_of="F1",
    )
    result = ledger.process(reversal)
    result.released            # Decimal("100.00")

Partial reversals are supported; cumulative reversals may not exceed the
original amount (``ReversalAmountExceededError``).  Orphan, wrong-currency and
wrong-authorization reversals are rejected.

Amendments
----------

``amend_authorization`` closes the current version at ``effective - 1`` and
opens a new version at ``effective``.  The version chain must remain
contiguous.

.. code-block:: python

    ledger.amend_authorization(
        "AUTH-1",
        datetime.date(2026, 2, 1),
        period_limit=Decimal("1000.00"),
        per_transaction_limit=Decimal("500.00"),
    )

Flows posted before the boundary keep consuming the old version's caps; a
later reversal of such a flow still releases the old version and period.

OFX Integration
---------------

``flow_from_stmttrn`` adapts an OFX ``STMTTRN`` debit (negative ``trnamt``)
into a ``Flow``, using ``REFNUM`` as the authorization reference when no
explicit reference is supplied.

.. code-block:: python

    from ofxtools.authorization import flow_from_stmttrn

    flow = flow_from_stmttrn(stmttrn)                     # refnum -> auth_ref
    flow = flow_from_stmttrn(stmttrn, auth_ref="AUTH-9")  # explicit reference
    flow = flow_from_stmttrn(stmttrn, reversal_of="FIT-0")
