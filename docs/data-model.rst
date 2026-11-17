Read transactions, amounts and balances
=======================================

After parsing, distinguish the collection's statement metadata from each
transaction's fields. :class:`mt940.models.Transactions` is a sequence. Its
``data`` dictionary contains references, account information and balances.
Its ``transactions`` list holds :class:`mt940.models.Transaction` objects,
each with another ``data`` dictionary and a reference to its collection.

.. list-table:: Collection and model attributes
   :header-rows: 1
   :widths: 25 35 40

   * - Object
     - Attribute
     - Meaning
   * - ``Transactions``
     - ``data``, ``transactions``
     - Mutable metadata and the ordered transaction list.
   * - ``Transactions``
     - ``currency``
     - Currency inferred from eligible balances or floor limits.
   * - ``Transactions``
     - ``options``, ``tags``, ``processors``, ``transaction_boundary``
     - Configuration used while parsing. See :doc:`customising`.
   * - ``Transaction``
     - ``data``, ``transactions``
     - Parsed transaction fields and the owning collection.
   * - ``Amount``
     - ``amount``, ``currency``
     - Signed ``Decimal`` and currency string or ``None``.
   * - ``Balance``
     - ``status``, ``amount``, ``date``
     - Original debit/credit mark, an ``Amount`` and a date for ordinary parsed
       balances. Direct constructors also permit empty values.
   * - ``SumAmount``
     - ``number`` plus ``Amount`` attributes
     - Informational entry count. Built-in summary tags retain a string.

Indexing returns the stored transaction. Slicing returns a new list containing
the same transaction objects. These are mutable objects, so editing a sliced
transaction also edits the collection's transaction.

What a statement line produces
------------------------------

With the default ``:61:`` parser and processors, fields include:

.. list-table:: Transaction fields
   :header-rows: 1
   :widths: 30 25 45

   * - Field
     - Typical value type
     - Source and limits
   * - ``date``
     - ``Date``
     - Value date from ``YYMMDD``.
   * - ``entry_date``, ``guessed_entry_date``
     - ``Date``
     - Optional entry month/day, with inferred year. Both keys refer to the
       same resolved date when present.
   * - ``amount``
     - ``Amount``
     - Signed amount. ``amount.amount`` is a ``Decimal``.
   * - ``currency``
     - ``str`` or ``None``
     - Normally inherited from statement data available at that point.
   * - ``status``
     - ``str``
     - Source debit, credit or reversal mark, retained in its original case.
   * - ``funds_code``
     - ``str`` or ``None``
     - Optional source funds code. It is not a complete currency code.
   * - ``id``
     - ``str`` or ``None``
     - Transaction type such as ``NTRF``. It also affects grouping.
   * - ``customer_reference``, ``bank_reference``
     - ``str`` or ``None``
     - References on ``:61:``. Later structured details can replace them.
   * - ``extra_details``
     - ``str``
     - Extra ``:61:`` content, commonly empty.
   * - ``transaction_reference``
     - ``str`` or ``None``
     - Copied from collection metadata by a default post-processor.
   * - ``transaction_details``
     - ``str``
     - Unstructured ``:86:`` text. Structured decoding removes this key and
       emits the fields in :doc:`bank-formats`.

Field availability is driven by input and processors. An optional regex group
may produce ``None``. A tag that never appeared may leave a key absent.
Structured details emit multiple mapped keys with ``None`` values. Use
membership tests when absence and ``None`` have different application meanings.

Money and signs
---------------

Amounts retain decimal digits and do not impose a currency scale:

.. doctest::

   >>> from decimal import Decimal, localcontext
   >>> import mt940
   >>> debit: mt940.models.Amount = mt940.models.Amount('12,50', 'D', 'EUR')
   >>> debit.amount, debit.currency
   (Decimal('-12.50'), 'EUR')
   >>> debit.amount + Decimal('25.00')
   Decimal('12.50')
   >>> mt940.models.Amount('1,2345', 'C', 'EUR').amount
   Decimal('1.2345')

Only exact ``D`` negates by default. ``reversal_sign`` also negates ``RC``.
``case_insensitive_marks`` recognises lowercase marks. ``C`` and ``RD`` leave
the numeric value unchanged. The original status is retained separately.
There is no currency conversion or validation of currency codes.

Decimal construction preserves input digits, but debit negation uses the active
decimal context and can round long values:

.. doctest::

   >>> with localcontext() as context:
   ...     context.prec = 4
   ...     print(mt940.models.Amount('12345,67', 'D', 'EUR').amount)
   -1.235E+4

Choose sufficient precision before parsing if your application changes the
default decimal context or processes very long amounts. Avoid converting money
to ``float`` for calculations or export.

Clean directions and reversals
------------------------------

:func:`mt940.clean` keeps every parsed occurrence and adds the canonical flow
implied by its mark. ``C`` and ``RD`` credit the account; ``D`` and ``RC``
debit it. Cleaning never negates the parsed ``Amount`` again: it reads the
stored magnitude and signs it once, so an ``RC`` parsed as ``-10.00`` by
``reversal_sign`` stays a debit of ``10.00`` rather than returning to credit.

.. doctest::

   >>> import mt940
   >>> source: str = """:20:EXAMPLE
   ... :60F:C260101EUR100,00
   ... :61:2601020101C10,00NTRFREF-A
   ... :61:2601020101RC10,00NTRFREF-B
   ... :62F:C260102EUR100,00
   ... """
   >>> cleaned: mt940.Cleaning = mt940.clean(mt940.parse(source))
   >>> [(entry.status, entry.original_amount, entry.net_amount)
   ...  for entry in cleaned.occurrences]
   [('C', Decimal('10.00'), Decimal('10.00')),
    ('RC', Decimal('10.00'), Decimal('-10.00'))]
   >>> cleaned.net_amount('EUR')
   Decimal('0.00')

Each :class:`mt940.CleanedEntry` retains the original ``status``, original
signed ``original_amount`` and canonical ``net_amount``. The transaction object
is available as ``entry.transaction`` and is not modified.

Nets are grouped by the amount's own currency. No conversion is attempted and
amounts without a currency form a separate ``None`` key in
``cleaned.net_amounts``. Totals are computed from decimal digits rather than
the active context, so a low precision setting cannot round them.

Two offsetting entries are only removed from ``cleaned.effective`` when they
share a non-empty business reference under one of
:data:`mt940.cleaning.DEFAULT_ASSOCIATION_KEYS`. The shared reference must
belong to the same currency and absolute amount. Both sides remain in
``cleaned.occurrences``, and every detected pair is in ``cleaned.reversals``.
An offsetting pair without a shared reference remains in ``effective`` and is
listed in ``cleaned.unassociated_reversals``; it is not silently deleted.

.. doctest::

   >>> paired: mt940.Cleaning = mt940.clean(
   ...     mt940.parse(
   ...         ':20:EXAMPLE\n:60F:C260101EUR0,00\n'
   ...         ':61:2601020101C5,00NTRFLINK//BANK\n'
   ...         ':61:2601020101RC5,00NTRFLINK//BANK\n'
   ...     )
   ... )
   >>> len(paired.occurrences), len(paired.effective), paired.net_amount('EUR')
   (2, 0, Decimal('0.00'))
   >>> unlinked: mt940.Cleaning = mt940.clean(
   ...     mt940.parse(
   ...         ':20:EXAMPLE\n:60F:C260101EUR0,00\n'
   ...         ':61:2601020101C5,00NTRF\n'
   ...         ':61:2601020101RC5,00NTRF\n'
   ...     )
   ... )
   >>> len(unlinked.occurrences), len(unlinked.effective)
   (2, 2)
   >>> len(unlinked.unassociated_reversals)
   1

``transaction_reference`` is deliberately not an association key: the default
processor copies the statement reference into every transaction, so it cannot
distinguish a reversal from its original. Pass ``association_keys`` to
:func:`mt940.clean` (or :meth:`mt940.models.Transactions.clean`) for
bank-specific links such as a payment identifier stored in ``purpose``.
An empty iterable disables association so every offsetting pair remains in
``effective``.

Cleaning is separate from parsing. It does not change tag output, the
``Amount`` stored on a transaction, or balance reconciliation.

Dates, time and balances
------------------------

:class:`mt940.models.Date` and :class:`mt940.models.DateTime` subclass Python's
standard date classes. Tag-derived two-digit years become years in the 2000s.
The default statement pre-processor repairs certain out-of-range February
value dates. Invalid model dates can still raise ``ValueError``.

Entry dates have no year in the input. The parser first tries the value date's
year and moves it by one year when the difference is at least 330 days. Both
``entry_date`` and ``guessed_entry_date`` expose that result. The initial date
is validated before the year correction, which limits leap-day recovery.
The ``timezone_offset`` option controls interpretation of ``:13D:`` offsets.

Balances are stored under tag-specific names such as
``final_opening_balance`` and ``intermediate_closing_balance``. Later balances
under the same name replace earlier ones. They are not a historical ledger.
The :doc:`statements` guide explains the implications for paged exports.

The library does not check that opening balance plus transactions equals
closing balance. The :doc:`quickstart` example performs that check, and
:doc:`json` demonstrates explicit validation before constructing a report.

Equality and mutation
---------------------

``Amount`` equality includes amount and currency. ``SumAmount.number`` is
informational and does not participate. ``Balance`` equality includes amount
and status but ignores date. Comparing balances alone therefore does not prove
that they describe the same reporting day.

Amounts and balances are hashable for compatibility, although their attributes
remain writable. Do not mutate an object after using it as a dictionary key or
set member. See :doc:`internals` for shared configuration and pickle behaviour.
