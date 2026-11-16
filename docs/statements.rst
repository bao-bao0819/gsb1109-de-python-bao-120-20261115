Keep separate statements and group transactions
===============================================

A file can contain several bank statements. If their account numbers,
references and balances must survive separately, use
:func:`mt940.parse_statements`. It returns one
:class:`mt940.models.Transactions` per ``:20:`` block in source order.

Two statements in one file
--------------------------

This fixture contains two fictional statement references with their own
opening and closing balances:

.. literalinclude:: ../examples/fixtures/statements.sta
   :language: text

Run ``uv run python -m examples.statements`` from the checkout:

.. literalinclude:: ../examples/statements.py
   :language: python

The recorded output shows both collections and then the merged form:

.. literalinclude:: ../examples/output/statements.txt
   :language: text

Separate parsing retains the opening balance of ``100.00`` on the first
statement and ``87.50`` on the second. ``parse`` puts both transactions in one
collection and keeps the final value for each repeated metadata key, including
``EXAMPLE-002`` as its reference. A metadata key absent from the final block
can still retain an earlier value.

What creates a statement
------------------------

Only ``:20:`` at the beginning of a physical line splits the input. A header
before the first usable block is discarded. Empty input or input without a
usable ``:20:`` block returns an empty list. A block with balances and no
transactions still produces a collection:

.. doctest::

   >>> import mt940
   >>> blocks: list[mt940.models.Transactions] = mt940.parse_statements(
   ...     ':20:BALANCES-ONLY\n:60F:C260101EUR100,00\n:62F:C260102EUR100,00'
   ... )
   >>> len(blocks), len(blocks[0])
   (1, 0)
   >>> print(blocks[0].data['final_closing_balance'])
   100.00 EUR @ 2026-01-02

``:28C:`` statement and sequence numbers do not split collections. A file with
one ``:20:`` and several pages remains one statement. Repeated ``:60M:`` and
``:62M:`` values overwrite the earlier intermediate balances under their
respective keys. The first-intermediate-balance regression test remains a
strict expected failure. There is no option that retains every page balance.
When each page arrives as its own ``:20:`` block,
:func:`mt940.reconstruct` reassembles the period as described below.

Reassembling paginated statements
---------------------------------

Banks that split a long statement into pages repeat the ``:28C:`` statement
number with a consecutive ``/`` sequence number on every page. Two pages
only chain when the closing balance of one page equals the opening balance
of the next page of the same statement. :func:`mt940.reconstruct` groups
parsed pages by account and statement number, orders them by sequence number
and reports missing pages, duplicate pages and balance breaks separately:

.. doctest::

   >>> pages: list[mt940.models.Transactions] = mt940.parse_statements(
   ...     ':20:PAGE-2\n'
   ...     ':25:DE00BANK0123456789\n'
   ...     ':28C:7/2\n'
   ...     ':60M:C240102EUR150,00\n'
   ...     ':62F:C240102EUR100,00\n'
   ...     ':20:PAGE-1\n'
   ...     ':25:DE00BANK0123456789\n'
   ...     ':28C:7/1\n'
   ...     ':60F:C240101EUR100,00\n'
   ...     ':62M:C240101EUR150,00\n'
   ... )
   >>> (period,) = mt940.reconstruct(pages)
   >>> period.is_complete
   True
   >>> print(period.opening_balance.amount, period.closing_balance.amount)
   100.00 EUR 100.00 EUR

The input page order does not change the result, and a page from another
statement of the same account never fills a gap. A period is only complete
when a final closing balance closes it and no page is missing or misaligned.
``missing_pages``, ``duplicate_pages`` and ``balance_breaks`` on
:class:`mt940.reconstruct.StatementPeriod` describe what was found
otherwise, and its ``merge`` method combines the retained pages into one
collection.

When ``:20:`` marks a transaction
---------------------------------

Some exports use a reference tag to begin a transaction block inside one
statement. Pass tag slugs through ``transaction_boundary`` when that is the
bank's convention:

.. doctest::

   >>> source: str = ':20:FIRST\n:86:First detail\n:20:SECOND\n:86:Second detail'
   >>> grouped: mt940.models.Transactions = mt940.parse(
   ...     source, transaction_boundary={'transaction_reference_number'}
   ... )
   >>> [item.data['transaction_reference'] for item in grouped]
   ['FIRST', 'SECOND']
   >>> grouped[0].data['transaction_details']
   'First detail'

Each configured slug creates a placeholder transaction. A following ``:61:``
can fill that placeholder. The reference also remains in statement metadata
because its tag is statement-scoped. A bare string is accepted as one slug,
and other iterables are materialised once into a frozen set.

Use ``parse`` for this convention. Combining ``parse_statements`` with the
reference boundary also creates a placeholder at every statement opening,
which may be unwanted when the block has no ``:61:`` line.

Default ``:61:`` grouping has another compatibility limit: a trailing
transaction with a missing or false ``id`` is reused. Two ``:61:`` lines without
transaction-type IDs can therefore merge. The parser does not promise one
transaction per physical ``:61:`` under all inputs. Inspect ``id`` and use a
bank-specific adaptation when necessary. See
:meth:`mt940.models.Transactions._process_statement_tag` for the exact rule.
