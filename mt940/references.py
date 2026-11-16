r"""Source-named reference tables for caller-controlled deduplication.

A single MT940 transaction can carry several identifiers from different
domains: the ``:61:`` customer and bank references, the ``:20:`` statement
reference copied onto each transaction, the ``:21:`` related reference, the
SEPA end-to-end reference (``EREF``) decoded from a structured ``:86:`` and
the transaction code. :func:`build_reference_table` collects every identifier
under its :class:`ReferenceSource` instead of flattening them into one value.

The table never decides that two rows are the same transaction. It indexes
entries by ``(source, value)`` and returns all matching rows, including every
row of a repeated key, so callers state their own deduplication basis through
:meth:`ReferenceTable.groups` or :meth:`ReferenceTable.duplicates`.

Placeholder text such as ``NOREF`` and ``NONREF`` is not a reliable identifier
and repeats across many unrelated transactions. Such values are skipped by
default and their transactions remain individually listed in
:attr:`ReferenceTable.unreferenced`; a whole batch of ``NOREF`` rows can
therefore never collapse into one transaction. Pass
``include_placeholders=True`` to keep them as ordinary, per-transaction rows.

Example:
    >>> import mt940
    >>> source = (
    ...     ':20:STMT-1\n'
    ...     ':25:NL00BANK0123456789\n'
    ...     ':28C:1/1\n'
    ...     ':60F:C260101EUR100,00\n'
    ...     ':61:260102C5,00NTRFINV-1//BANK-1\n'
    ...     ':61:260102D5,00NTRFNONREF\n'
    ...     ':62F:C260102EUR100,00\n'
    ... )
    >>> transactions = mt940.parse(source)
    >>> # The caller states which domains count; the :20: statement reference
    >>> # copied onto every row is deliberately excluded.
    >>> table = mt940.references.build_reference_table(
    ...     transactions,
    ...     sources=[
    ...         mt940.references.ReferenceSource.CUSTOMER,
    ...         mt940.references.ReferenceSource.BANK,
    ...     ],
    ... )
    >>> table.unreferenced
    frozenset({1})
    >>> entry = table.lookup(mt940.references.ReferenceSource.BANK, 'BANK-1')
    >>> [(row.transaction_index, row.key.value) for row in entry]
    [(0, 'BANK-1')]
"""

from __future__ import annotations

import enum
from collections.abc import Collection, Iterable, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Transaction

#: Placeholder values sent when a bank has no real reference. They repeat
#: across unrelated transactions and are skipped unless explicitly requested.
NON_REFERENCE_VALUES: frozenset[str] = frozenset({'NOREF', 'NONREF'})


@enum.unique
class ReferenceSource(enum.Enum):
    """Named domain a reference value was read from.

    Each member keeps the parsed ``Transaction.data`` key it is extracted
    from. The same key can be fed by more than one SWIFT field, for example a
    structured ``KREF`` lands on ``customer_reference`` alongside the
    ``:61:`` customer reference; the table reports the parsed domain, not the
    individual sub-tag.
    """

    #: ``customer_reference`` from the ``:61:`` statement line.
    CUSTOMER = 'customer_reference'
    #: ``bank_reference`` following the ``//`` delimiter on ``:61:``.
    BANK = 'bank_reference'
    #: ``transaction_reference`` from the ``:20:`` statement reference.
    STATEMENT = 'transaction_reference'
    #: ``related_reference`` from the ``:21:`` related reference.
    RELATED = 'related_reference'
    #: ``end_to_end_reference`` decoded from an ``EREF`` GVC keyword.
    END_TO_END = 'end_to_end_reference'
    #: ``transaction_code`` from structured details or an mBank processor.
    TRANSACTION_CODE = 'transaction_code'


class ReferenceKey:
    """A reference value together with the source domain it belongs to.

    Keys are immutable and hashable. Equality never crosses sources, so an
    identical string from the customer and bank domains stays distinct.

    Attributes:
        source: Domain the value was extracted from.
        value: Stripped reference text. Integer transaction codes are rendered
            as their decimal string.
    """

    def __init__(self, source: ReferenceSource, value: str) -> None:
        """Bind a stripped reference value to its source domain.

        Args:
            source: Domain the value was extracted from.
            value: Reference text. The builder strips surrounding whitespace,
                including field padding, before constructing the key.
        """
        self.source: ReferenceSource = source
        self.value: str = value

    def __eq__(self, other: object) -> bool:
        """Return whether both source and value match."""
        return (
            isinstance(other, ReferenceKey)
            and self.source is other.source
            and self.value == other.value
        )

    def __hash__(self) -> int:
        """Hash the source and value pair."""
        return hash((self.source, self.value))

    def __repr__(self) -> str:
        """Return ``ReferenceSource.NAME='value'`` in angle brackets."""
        return f'<{self.source.name}={self.value!r}>'


class ReferenceEntry:
    """One reference value observed on one transaction.

    Entries are immutable. Two transactions sharing a reference produce two
    entries with equal :attr:`key` values but different indices, which is what
    keeps a repeated key from silently merging transactions.

    Attributes:
        transaction_index: Position of the transaction in the parsed sequence.
        key: Source-named reference value.
    """

    def __init__(self, transaction_index: int, key: ReferenceKey) -> None:
        """Store the transaction position and its source-named key.

        Args:
            transaction_index: Position of the transaction in the sequence the
                table was built from.
            key: Source-named reference value observed on that transaction.
        """
        self.transaction_index: int = transaction_index
        self.key: ReferenceKey = key

    def __eq__(self, other: object) -> bool:
        """Return whether index and key both match."""
        return (
            isinstance(other, ReferenceEntry)
            and self.transaction_index == other.transaction_index
            and self.key == other.key
        )

    def __hash__(self) -> int:
        """Hash the index and key pair."""
        return hash((self.transaction_index, self.key))

    def __repr__(self) -> str:
        """Return the index and key in angle brackets."""
        return f'<{self.transaction_index}: {self.key!r}>'


class ReferenceTable:
    """Source-named index over the references of a transaction sequence.

    Construct instances with :func:`build_reference_table`. The table stores
    one :class:`ReferenceEntry` per ``(transaction, source)`` pair and groups
    equal keys without merging the underlying transactions. Iteration yields
    entries in transaction order and ``len`` counts entries rather than
    transactions.

    Attributes:
        unreferenced: Positions of transactions that contributed no reliable
            entry, kept as distinct rows even when their placeholders repeat.
        _entries: Every entry in transaction order.
        _index: Entries grouped by their :class:`ReferenceKey`.
    """

    def __init__(
        self,
        entries: Iterable[ReferenceEntry],
        unreferenced: Iterable[int],
    ) -> None:
        """Index prebuilt entries and record transactions without references.

        Args:
            entries: Entries in transaction order. The builder is responsible
                for ordering and placeholder filtering.
            unreferenced: Transaction positions without a reliable entry.
        """
        self._entries: tuple[ReferenceEntry, ...] = tuple(entries)
        self.unreferenced: frozenset[int] = frozenset(unreferenced)
        index: dict[ReferenceKey, list[ReferenceEntry]] = {}
        for entry in self._entries:
            index.setdefault(entry.key, []).append(entry)
        self._index: dict[ReferenceKey, tuple[ReferenceEntry, ...]] = {
            key: tuple(rows) for key, rows in index.items()
        }

    def __len__(self) -> int:
        """Return the number of entries, one per transaction and source."""
        return len(self._entries)

    def __iter__(self) -> Iterator[ReferenceEntry]:
        """Yield every entry in transaction order."""
        return iter(self._entries)

    def __contains__(self, key: object) -> bool:
        """Return whether ``key`` is a stored :class:`ReferenceKey`."""
        return key in self._index

    def lookup(
        self, source: ReferenceSource, value: str
    ) -> tuple[ReferenceEntry, ...]:
        """Return every entry for a source and value, without raising.

        Args:
            source: Domain the caller wants to match within.
            value: Stripped reference text, identical to the parsed value.

        Returns:
            All matching entries in transaction order, or an empty tuple when
            the key is unknown or only appeared as a skipped placeholder.
        """
        return self._index.get(ReferenceKey(source, value), ())

    def groups(
        self, *sources: ReferenceSource
    ) -> dict[ReferenceKey, tuple[ReferenceEntry, ...]]:
        """Group entries by key, restricted to the caller's chosen domains.

        Args:
            *sources: Domains to include. With no arguments every domain
                participates, so an explicit argument list is the way to
                state the business deduplication basis.

        Returns:
            A new mapping from key to all entries carrying it, ordered by the
            first occurrence of each key. Repeated keys keep one entry per
            transaction and are never collapsed.
        """
        selected: frozenset[ReferenceSource] = frozenset(sources)
        return {
            key: rows
            for key, rows in self._index.items()
            if not selected or key.source in selected
        }

    def duplicates(
        self, *sources: ReferenceSource
    ) -> dict[ReferenceKey, tuple[ReferenceEntry, ...]]:
        """Return only keys shared by two or more transactions.

        Args:
            *sources: Domains eligible for comparison, with the same meaning
                as :meth:`groups`. The caller chooses which references are
                trustworthy enough to suggest a duplicate.

        Returns:
            The subset of :meth:`groups` whose key occurs on at least two
            entries. Repeated placeholder values appear here only when the
            table was built with ``include_placeholders=True``.
        """
        return {
            key: rows
            for key, rows in self.groups(*sources).items()
            if len(rows) > 1
        }


def _entry_value(value: object) -> str | None:
    """Normalise one parsed field to stripped text or skip it.

    Args:
        value: Raw value from ``Transaction.data``.

    Returns:
        Stripped text for strings, decimal text for integers, or ``None`` for
        missing, blank or unsupported values such as model objects.
    """
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, int) and not isinstance(value, bool):
        text = str(value)
    else:
        return None
    return text or None


def build_reference_table(
    transactions: Iterable[Transaction],
    *,
    sources: Iterable[ReferenceSource] = tuple(ReferenceSource),
    include_placeholders: bool = False,
    placeholder_values: Collection[str] = NON_REFERENCE_VALUES,
) -> ReferenceTable:
    """Collect every transaction reference under its source domain.

    Args:
        transactions: Parsed transactions, enumerated in their given order.
            Only each transaction's ``data`` mapping is read.
        sources: Domains to extract. Defaults to every
            :class:`ReferenceSource`. Restricting this states upfront which
            references the caller considers usable.
        include_placeholders: Keep values in ``placeholder_values`` as normal
            per-transaction entries when true. By default they are skipped and
            their transactions stay in
            :attr:`ReferenceTable.unreferenced`.
        placeholder_values: Exact, case-sensitive stripped texts treated as
            non-reference placeholders. Defaults to
            :data:`NON_REFERENCE_VALUES`.

    Returns:
        A table holding one entry per transaction and selected source. Blank,
        missing and unsupported fields contribute nothing. Transactions with
        no reliable entry remain individually addressable through
        :attr:`ReferenceTable.unreferenced`.

    Raises:
        ValueError: ``sources`` contains an object that is not a
            :class:`ReferenceSource` member.
    """
    raw_sources: tuple[object, ...] = tuple(sources)
    selected_sources: list[ReferenceSource] = []
    for source in raw_sources:
        if not isinstance(source, ReferenceSource):
            msg = f'Not a ReferenceSource: {source!r}'
            raise ValueError(msg)
        selected_sources.append(source)

    entries: list[ReferenceEntry] = []
    unreferenced: list[int] = []
    for index, transaction in enumerate(transactions):
        reliable = False
        data = transaction.data
        for source in selected_sources:
            text = _entry_value(data.get(source.value))
            if text is None:
                continue
            key = ReferenceKey(source, text)
            if text in placeholder_values:
                if include_placeholders:
                    entries.append(ReferenceEntry(index, key))
                continue
            reliable = True
            entries.append(ReferenceEntry(index, key))
        if not reliable:
            unreferenced.append(index)

    return ReferenceTable(entries, unreferenced)
