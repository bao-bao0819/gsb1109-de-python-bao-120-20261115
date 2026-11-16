.. _ledger:

Settlement Ledger
=================

OFX investment transactions carry a trade date (``DTTRADE``) and, optionally,
a settlement date (``DTSETTLE``).  ``ofxtools.ledger`` replays them into three
linked books:

* equity holdings -- units owned as of the trade date;
* available securities -- units deliverable as of the settlement date;
* cash -- ``pending`` amounts committed to open settlements plus ``settled``
  cash that has already changed hands.

Settlement rules
----------------
* A trade moves equity holdings and commits cash on the trade date.  Available
  securities and settled cash move on the settlement date.  An explicit
  ``DTSETTLE`` wins; otherwise the date comes from
  :func:`ofxtools.utils.settleDate` using the configured rule table
  (:class:`~ofxtools.ledger.SettlementRules`): T+1 by default, T+2 for debt.
  Replace the table to model other instruments or markets.
* A trade with no settlement confirmation auto-settles in full on its
  contractual settlement date.
* A trade may settle in several pieces.  Every piece references the original
  ``FITID`` and is reported separately, so partial settlements stay linked to
  the trade that spawned them.  Pieces may not exceed the original quantity.
* A failed settlement is an attempt, not a disposition.  It leaves holdings,
  available units and pending cash untouched and keeps the obligation open
  for a retry.  A cancellation, by contrast, reverses the trade on all books.
* A late confirmation keeps both its effective date and the date the ledger
  first learned of it.  Queries with a knowledge cutoff before the receipt
  date exclude it.
* Each book is evaluated at its own time point: equity against the trade
  date, available securities and cash against the settlement date, and
  visibility against the knowledge date.

Building a ledger from OFX
--------------------------
.. code-block:: python

    import datetime
    from ofxtools.Parser import OFXTree
    from ofxtools.ledger import SettlementLedger

    tree = OFXTree()
    tree.parse("invstmtrs.ofx")
    ofx = tree.convert()

    # FITIDs of confirmations received late can be given their receipt date.
    ledger = SettlementLedger.from_ofx(
        ofx, known_dates={"23321": datetime.date(2005, 9, 1)}
    )

    trade_day = ledger.as_of(datetime.date(2005, 8, 25))
    trade_day.held("999988", "123456789")       # Decimal('100')
    trade_day.available("999988", "123456789")  # Decimal('0')
    trade_day.pending_cash("999988", "USD")     # Decimal('-5025.00')

    settle_day = ledger.as_of(datetime.date(2005, 8, 28))
    settle_day.available("999988", "123456789") # Decimal('100')
    settle_day.settled_cash("999988", "USD")    # Decimal('-4025.00')

    # Reproduce the books as they were known on an earlier date.
    ledger.as_of(datetime.date(2005, 8, 28), datetime.date(2005, 8, 28))

Recording settlement facts directly
-----------------------------------
.. code-block:: python

    import datetime
    from decimal import Decimal
    from ofxtools.ledger import EventType, SettlementEvent, SettlementLedger

    ledger = SettlementLedger()
    ledger.record(
        SettlementEvent(
            fitid="T1",
            effective_date=datetime.date(2024, 6, 3),
            event_type=EventType.TRADE,
            account="A",
            currency="USD",
            security_id="123456789",
            security_type="CUSIP",
            units=Decimal("100"),
            amount=Decimal("-5025"),
        )
    )
    # Half the position settles two days late.
    ledger.record(
        SettlementEvent(
            fitid="P1",
            effective_date=datetime.date(2024, 6, 4),
            event_type=EventType.PARTIAL,
            reference="T1",
            account="A",
            currency="USD",
            security_id="123456789",
            units=Decimal("40"),
            amount=Decimal("-2010"),
        )
    )

    record = ledger.trade_records(datetime.date(2024, 6, 4))[0]
    record.status          # SettlementStatus.PARTIAL
    record.settled_units   # Decimal('40')
    record.pieces          # ('P1',)

A settlement that fails uses ``EventType.FAIL`` and the same ``reference``.
It is recorded against the trade (``status`` becomes ``FAILED``) but does not
reduce ``remaining_units``, so a later successful piece can still settle it.
Use ``EventType.CANCEL`` to reverse a trade entirely.

Confirmations that OFX does not carry (partial deliveries, failed deliveries,
late receipts, broker cancellations) can be supplied as JSON.  Each object
takes the :class:`~ofxtools.ledger.SettlementEvent` fields::

    [
      {"fitid": "P1", "effective_date": "2024-06-04", "event_type": "PARTIAL",
       "reference": "T1", "account": "A", "currency": "USD",
       "security_id": "123456789", "units": "40", "amount": "-2010"},
      {"fitid": "F1", "effective_date": "2024-06-05", "event_type": "FAIL",
       "reference": "T1", "account": "A", "currency": "USD",
       "security_id": "123456789", "units": "60", "amount": "-3015",
       "known_date": "2024-06-07"}
    ]

Command line
------------
``ofxledger`` prints the three books for an OFX file::

    ofxledger invstmtrs.ofx asof --date 20050825
    ofxledger invstmtrs.ofx trades --date 20050828 --json
    ofxledger invstmtrs.ofx pending --date 20050826
    ofxledger invstmtrs.ofx events --known-fitid 23321=20050901
    ofxledger invstmtrs.ofx asof --confirmations confirmations.json
    ofxledger invstmtrs.ofx asof --rules cycle-rules.json

``--rules`` takes a JSON object mapping an instrument or security type to
business days; insertion order decides precedence and the last rule is the
fallback::

    {"MUNICIPAL": 2, "DEBT": 3, "1": 1}

``--known-fitid FITID=DATE`` and ``known_date`` in a confirmation record the
date the ledger first learned of a late receipt.  Query with ``--known DATE``
to see the books as they stood before it arrived.
