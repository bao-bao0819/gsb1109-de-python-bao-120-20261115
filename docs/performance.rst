.. _performance:

Portfolio Performance Reporting
===============================

``ofxtools.performance`` turns one or more OFX investment statements
(``INVSTMTRS``) into a segmented performance report.  It keeps the two forces
that move a portfolio's value strictly separate:

* **market movement** — investment gain/loss and rate of return; and
* **external cash movements** — contributions and withdrawals, which are not
  performance and never enter the return numerator.

The module works from two parts of the OFX statement:

* cash flows from ``INVTRANLIST`` (bank transactions and cross-account
  transfers), and
* valuation observation points from ``INVPOSLIST`` plus the ``INVBAL`` cash
  balance.

Segmented modified Dietz formula
---------------------------------
External flows split the period into segments bounded by valuation points.
For segment *i* with bounding valuations ``V_{i-1}`` at ``t_{i-1}`` and
``V_i`` at ``t_i`` and external flows ``C_{i,t}`` (positive = contribution,
negative = withdrawal) posted at time *t*:

.. code-block:: text

    w_t = (t_i - t) / (t_i - t_{i-1})
    R_i = (V_i - V_{i-1} - C_i) / (V_{i-1} + sum_t(w_t * C_{i,t}))

The numerator is investment gain/loss (value change net of external money);
the denominator is average capital exposed to the market.  The full-period
return chains the segment returns geometrically:
``R_full = product_i(1 + R_i) - 1``.  Time weights are computed from an exact
integer time ratio; all money math uses :class:`decimal.Decimal`.

Internal vs. external money
---------------------------
* ``INVBANKTRAN`` bank transactions are external; the sign of ``TRNAMT``
  decides in vs. out (``TRNTYPE`` is kept on the event for reference).
* A ``TRANSFER`` without an ``INVACCTFROM`` references an account outside the
  statement and is external; a ``TRANSFER`` naming another tracked account is
  an internal position move whose two legs cancel in the combined portfolio.
* ``JRNLSEC`` and ``JRNLFUND`` are always internal subaccount journals.
* Security buys/sells, income, expenses, and reinvestments swap one asset for
  another inside the account and are not external capital.

Internal events are flagged on each :class:`~ofxtools.performance.CashFlow`
and excluded from the return math.  An external ``TRANSFER`` without a
``UNITPRICE`` cannot be valued and raises ``PerformanceError``.

Same-timestamp ordering
-----------------------
When flows share a timestamp, ordering follows an explicit
:class:`~ofxtools.performance.CashFlowPriority` rule — by default outflows
before inflows, with ``fitid`` as a deterministic tiebreaker — never the order
transactions happen to appear in the OFX file.  A flow landing exactly on a
segment boundary is attributed to the following segment (full time weight);
flows stamped at the closing valuation are absorbed by the final segment
(zero time weight).

Missing valuation endpoints
---------------------------
Cash flows alone cannot establish portfolio value.  A report needs valuation
points bracketing the period; otherwise
:class:`~ofxtools.performance.MissingValuationError` is raised.  The same error
is raised when an external flow falls outside the valuation window.  A segment
whose capital base is zero has an undefined rate and reports
``return_rate=None`` rather than dividing by zero.

Usage
-----
.. code-block:: python

    from ofxtools.Parser import OFXTree
    from ofxtools.performance import performance_report

    tree = OFXTree()
    tree.parse("invstmtrs.ofx")
    ofx = tree.convert()

    # Pass statements from every account of one combined portfolio; same-day
    # statements are merged into a single valuation observation.
    statements = ofx.invstmtmsgsrsv1.statements
    report = performance_report(statements)

    report.beginning_value        # value at the first observation point
    report.ending_value           # value at the last observation point
    report.total_contributions    # gross external money in
    report.total_withdrawals      # gross external money out
    report.total_external_flow    # net external movement
    report.total_gain             # market-driven gain/loss over all segments
    report.total_return           # geometrically chained segment return
    report.internal_flows_excluded

    for segment in report.segments:
        segment.dtstart, segment.dtend
        segment.net_external_flow, segment.gain, segment.return_rate

Reference
---------
* :class:`~ofxtools.performance.CashFlow` — one movement, with ``amount``,
  ``kind``, ``fitid``, ``source``, ``trntype`` and an ``external`` flag.
* :class:`~ofxtools.performance.CashFlowPriority` — deterministic rule for
  flows sharing a timestamp (outflows first by default, ``fitid`` tiebreak).
* :class:`~ofxtools.performance.ValuationPoint` — total value at one
  ``DTASOF``: position ``MKTVAL`` sum plus ``INVBAL.AVAILCASH``.
* :class:`~ofxtools.performance.SegmentReturn` — per-segment flows,
  contribution/withdrawal totals, ``gain`` and modified Dietz ``return_rate``.
* :class:`~ofxtools.performance.PerformanceReport` — combined result with
  segment tuple, full-period totals, chained ``total_return`` and the count of
  excluded internal flows.
* ``extract_cash_flows``, ``build_valuation_point``,
  ``extract_valuation_points``, ``segment_returns`` and
  ``performance_report`` — the staged pipeline, each usable on its own.
* :class:`~ofxtools.performance.MissingValuationError` (a subclass of
  :class:`~ofxtools.performance.PerformanceError`) — raised when valuation
  endpoints are missing or external flows fall outside the valuation window.
