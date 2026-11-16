.. _parser:

Parsing OFX Data
================

``ofxtools`` parses OFX messages in two steps.

The first step parses serialized OFX data into a Python data structure.  The
``ofxtools.Parser.OFXTree`` parser parser subclasses
``xml.etree.ElementTree.ElementTree``, and follows the ElementTree API:

.. code:: python

    In [1]: from ofxtools.Parser import OFXTree
    In [2]: parser = OFXTree()
    In [3]: with open('2015-09_amtd.ofx', 'rb') as f:  # N.B. need to open file in binary mode
       ...:     parser.parse(f)
       ...:
    In [4]: parser.parse('2015-09_amtd.ofx')  # Can also use filename directly
    In [5]: type(parser._root)
    Out[5]: xml.etree.ElementTree.Element
    In [6]: parser.find('.//STATUS')[:]  # The full ElementTree API can be used, including XPath
    Out[6]:
    [<Element 'CODE' at 0x7f4cc0aa4868>,
     <Element 'SEVERITY' at 0x7f4cc0aa49f8>,
     <Element 'MESSAGE' at 0x7f4cc0aa4d68>]

At this stage, you can modify the entire Element structure arbitrarily - move
branches around the tree, add or delete elements, rewrite tags and text, etc.

The second step of parsing converts the Element structure into a hierarchy of
custom class instances, namely subclasses of ``ofxtools.models.base.Aggregate``
and ``ofxtools.models.Types.Element``, following the OFX specification's
classification of nodes into either containers ("Aggregates") or data-bearing
leaf nodes ("Elements", not to be confused with
``xml.etree.ElementTree.Element``).  This parsing step validates the
deserialized OFX data against the OFX spec, and performs type conversion
(so that, for example, an OFX element specified as a monetary quantity will
be converted to an instance of ``decimal.Decimal``, while another element
specified as date & time will be converted to an instance of
``datetime.datetime``)  The original structure of the OFX data hierarchy is
preserved through this conversion.

.. code:: python

    In [7]: ofx = parser.convert()
    In [8]: type(ofx)
    Out[8]: ofxtools.models.ofx.OFX

Following the `OFX spec`_ , you can navigate the OFX hierarchy using normal
Python dotted-attribute access, and standard slice notation for lists.

.. code:: python

    In [9]: tx = ofx.invstmtmsgsrsv1[0].invstmtrs.invtranlist[-1]
    In [10]: tx.dtposted
    Out[10]: datetime.datetime(2015, 9, 16, 17, 9, 48, tzinfo=<UTC>)
    In [11]: tx.trnamt
    Out[11]: Decimal('4.7')

While it's obvious that INTRANLIST is a list, it's perhaps less obvious that
INVSTMTMSGSRSV1 is also a list, since OFX specifies that a single statement
response wrapper can contain multiple statements.

Navigating deeply-nested OFX hierarchies to extract data can be verbose, so
subclasses of ``ofxtools.models.base.Aggregate`` provide some navigational
conveniences.

First, each ``Aggregate`` provides proxy access to the attributes of its
``SubAggregates`` (and its sub-subaggregates, and so on).  If the data you're
looking for is located in ``a.b.c.d.e.f``, you can access it more simply
as ``a.f``.  This won't work across lists, of course; you have to select
an item from the list.  So in this example, if ``c`` is a list type, you could
get your data from ``a.c[10].f``.

Second, the upper-level ``Aggregates`` define some human-friendly aliases
for the data structures you're really looking for.  Here's an example.

.. code:: python

    In [12]: stmts = ofx.statements  # All {``STMTRS``, ``CCSTMTRS``, ``INVSTMTRS``} in the response
    In [13]: txs = stmts[0].transactions  # The relevant ``*TRANLIST``
    In [14]: acct = stmts[0].account  # The relevant ``*ACCTFROM``
    In [15]: balances = stmts[0].balances  # ``INVBAL`` - use ``balance`` for bank statement ``LEDGERBAL``
    In [16]: securities = ofx.securities  # ``SECLIST``
    In [17]: len(securities)
    Out[17]: 5
    In [18]: len(txs)
    Out[18]: 6
    In [19]: tx = txs[-1]
    In [20]: tx.trnamt
    Out[20]: Decimal('4.7')
    In [21]: tx = txs[1]
    In [22]: type(tx)
    Out[22]: ofxtools.models.invest.transactions.TRANSFER
    In [23]: tx.invtran.dttrade  # Who wants to remember where to find the trade date?
    Out[23]: datetime.datetime(2015, 9, 8, 17, 14, 8, tzinfo=<UTC>)
    In [24]: tx.dttrade  # That's more like it
    Out[24]: datetime.datetime(2015, 9, 8, 17, 14, 8, tzinfo=<UTC>)
    In [25]: tx.secid.uniqueid  # Yet more layers
    Out[25]: '403829104'
    In [26]: tx.uniqueid  # Flat access is less cognitively taxing
    Out[26]: '403829104'
    In [27]: tx.uniqueidtype
    Out[27]: 'CUSIP'

The designers of the OFX spec did a good job avoiding name collisions.  However
you will need to remember that ``<UNIQUEID>`` always refers to securities; if
you're looking for a transaction unique identifier, you want ``tx.fitid``
(which is a shortcut to ``tx.invtran.fitid``).


Per-response status and partial data
------------------------------------
A single OFX download may contain many account responses, each with its own
``<STATUS>``.  A server can fail one account while succeeding others, and it
may still include a partially populated ``STMTRS`` in the failed block.

``ofx.statements`` contains only *formal* statements: responses whose status
succeeded and whose statement data was present.  A failed response is never
turned into a zero-transaction statement period, even when it contains an
empty transaction list.

Every statement response, successful or not, is available from
``ofx.responses`` (also ``ofx.response_report`` for JSON-compatible dicts):

.. code:: python

    In [27]: for response in ofx.responses:
       ....:     print(response.trusted_scope, response.status, response.location)
    complete code=0, severity=INFO, message=None OFX/BANKMSGSRSV1/STMTTRNRS[1]
    partial code=2000, severity=ERROR, message='Statement incomplete' OFX/BANKMSGSRSV1/STMTTRNRS[2]
    complete code=0, severity=INFO, message=None OFX/CREDITCARDMSGSRSV1/CCSTMTTRNRS[1]

    In [28]: response = ofx.responses[1]
    In [29]: response.partial_data  # failed data, explicitly labelled
    In [30]: response.formal_facts  # None, because the response is not formal

``trusted_scope`` is one of:

- ``complete`` - the response succeeded and its statement is formal.  A
  successful response with zero transactions is still complete: the zero is
  an explicit fact from the account.
- ``partial`` - the response failed (or could only be partly converted) but
  carried usable data (account, transactions and/or balances).  This data is
  exposed via ``partial_data`` and never counts as a formal period.
- ``none`` - the response failed and carried no usable data.

``location`` records the source of each response: document name, structural
path (``OFX/BANKMSGSRSV1/STMTTRNRS[2]``), body line, column and character
offset.  ``statement_location`` points at the contained ``*STMTRS``.

Failed blocks do not abort decoding of their siblings.  If a response
wrapper cannot be fully validated, it is retained in degraded form with its
``conversion_error`` and the original subtree, while the successful accounts
convert normally.

The same information is available from the CLI's ``ofxget stmt`` output:
failed responses are logged with their status and source location.


Deviations from the OFX specification
-------------------------------------
For handling multicurrency transactions per OFX section 5.2, ``Aggregates``
that can contain ``ORIGCURRENCY`` have an additional ``curtype`` attribute,
which is not part of the OFX spec.  ``curtype`` yields ``'CURRENCY'`` if the
money amounts have not been converted to the home currency, or yields
``'ORIGCURRENCY'`` if they have been converted.

``YIELD`` elements are renamed ``yld``, and ``FROM`` elements are renamed ``frm``,
in order to avoid name collision with Python reserved keywords.

Proprietary OFX tags (e.g. ``<INTU.BANKID>``) are stripped and dropped.


.. _OFX spec: https://financialdataexchange.org/ofx
