# Table Recognition for Python-Markdown
# =====================================

# Recognize Markdown tables with explicit column roles and unit declarations.

# Copyright 2026 The Python Markdown Project

# License: [BSD](https://opensource.org/licenses/bsd-license.php)

"""
Table recognition entry with explicit column roles and unit declarations.

Supplier documents frequently repeat the same header (for example two `数量`
columns meaning ordered quantity and received quantity) and state the units in
prose before the table. [`recognize_table`][markdown.tablerecognition.recognize_table]
accepts explicit column roles and unit declarations so each column gets a
stable identity, its original coordinates, and the conversion basis (unit) it
was declared with. Reordering columns afterwards maps by role, never by old
column index.

Declarations are scoped to the single table passed to `recognize_table`; they
never leak into other tables of the same document. A column with no declared
unit (`unit is None`, status `missing`) is kept strictly apart from a column
declared dimensionless (`unit is DIMENSIONLESS`, status `dimensionless`).
Columns sharing the same header text are kept as distinct columns with their
original values; they are disambiguated by role, never merged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Sequence

from .extensions.tables import (
    PIPE_LEFT,
    PIPE_RIGHT,
    TableProcessor,
    split_row_cells,
)

__all__ = [
    'DIMENSIONLESS',
    'Cell',
    'RecognizedColumn',
    'RecognizedTable',
    'TableRecognitionError',
    'recognize_table',
]


class TableRecognitionError(ValueError):
    """ Raised when a table cannot be recognized or declarations are invalid. """


class _DimensionlessType:
    """ Type of the [`DIMENSIONLESS`][markdown.tablerecognition.DIMENSIONLESS] sentinel. """

    __slots__ = ()

    def __repr__(self) -> str:
        return 'DIMENSIONLESS'

    def __bool__(self) -> bool:
        return False


DIMENSIONLESS = _DimensionlessType()
""" Sentinel declaring a column explicitly dimensionless (as opposed to a missing unit). """

_SEPARATOR_CHARS = frozenset('|:- ')


@dataclass(frozen=True)
class Cell:
    """ A single body cell with its original coordinates. """

    value: str
    """ Cell text, stripped of surrounding whitespace. """
    row: int
    """ 0-based index of the body row within the source table. """
    column: int
    """ 0-based index of the column within the source table. """
    line: int
    """ 0-based line number of the row within the source document. """


@dataclass(frozen=True)
class RecognizedColumn:
    """ A recognized column: stable role identity, original position and unit. """

    role: str | None
    """ Declared role identity, or `None` when no role was declared for this position. """
    header: str
    """ Header text as written in the source; may repeat across columns. """
    index: int
    """ 0-based original column index in the source table. """
    unit: Any
    """ Declared unit string, `DIMENSIONLESS`, or `None` when no unit was declared. """
    cells: tuple[Cell, ...]
    """ Body cells of this column, in source row order, with original coordinates. """

    @property
    def unit_status(self) -> str:
        """ One of `declared`, `dimensionless` or `missing`. """
        if self.unit is DIMENSIONLESS:
            return 'dimensionless'
        if self.unit is None:
            return 'missing'
        return 'declared'


@dataclass(frozen=True)
class RecognizedTable:
    """ Result of recognizing one Markdown table. """

    index: int
    """ 0-based index of the table within the source document. """
    header_line: int
    """ 0-based line number of the header row within the source document. """
    columns: tuple[RecognizedColumn, ...]
    """ Columns in current order; each keeps its original `index` and cell coordinates. """

    @property
    def headers(self) -> list[str]:
        """ Header texts in current column order. """
        return [column.header for column in self.columns]

    @property
    def roles(self) -> list[str | None]:
        """ Column roles in current column order. """
        return [column.role for column in self.columns]

    @property
    def row_count(self) -> int:
        """ Number of body rows. """
        if not self.columns:
            return 0
        return len(self.columns[0].cells)

    @property
    def conversion_basis(self) -> dict[str, Any]:
        """
        Conversion basis per role: the unit each roled column was declared with.

        Values are unit strings, `DIMENSIONLESS` for explicitly dimensionless
        columns, or `None` for columns with a missing unit declaration.
        """
        return {column.role: column.unit for column in self.columns if column.role is not None}

    def rows(self) -> list[tuple[str, ...]]:
        """ Body cell values row by row, in current column order. """
        return [
            tuple(column.cells[row].value for column in self.columns)
            for row in range(self.row_count)
        ]

    def reorder(self, roles: Sequence[str]) -> RecognizedTable:
        """
        Return a copy with columns reordered to match `roles`.

        Columns are looked up by role identity, never by old column index, so
        every column keeps its original `index` and cell coordinates. Roles not
        listed keep their relative order and are appended after the listed
        ones. Raises `KeyError` for an unknown role and `ValueError` for a
        duplicate or `None` role.
        """
        by_role = {column.role: column for column in self.columns if column.role is not None}
        ordered: list[RecognizedColumn] = []
        seen: set[str] = set()
        for role in roles:
            if role is None:
                raise ValueError('cannot reorder by a None role')
            if role in seen:
                raise ValueError(f'duplicate role in reorder: {role!r}')
            try:
                ordered.append(by_role[role])
            except KeyError:
                raise KeyError(f'unknown column role: {role!r}') from None
            seen.add(role)
        ordered.extend(column for column in self.columns if column.role not in seen)
        return replace(self, columns=tuple(ordered))


@dataclass(frozen=True)
class _RawTable:
    header_line: int
    header: str
    border: int
    rows: tuple[tuple[int, str], ...]


def _border_of(line: str) -> int:
    border = 0
    if line.startswith('|'):
        border |= PIPE_LEFT
    if TableProcessor.RE_END_BORDER.search(line) is not None:
        border |= PIPE_RIGHT
    return border


def _split_line(line: str, border: int) -> list[str]:
    if border:
        if line.startswith('|'):
            line = line[1:]
        line = TableProcessor.RE_END_BORDER.sub('', line)
    return split_row_cells(line)


def _find_tables(source: str) -> list[_RawTable]:
    """ Locate pipe tables with at least two columns, mirroring the `tables` extension. """
    lines = source.split('\n')
    tables: list[_RawTable] = []
    i = 0
    while i < len(lines) - 1:
        header = lines[i].strip(' ')
        separator = lines[i + 1].strip(' ')
        border = _border_of(header)
        header_cells = _split_line(header, border)
        if len(header_cells) > 1:
            separator_cells = _split_line(separator, _border_of(separator))
            if (
                len(separator_cells) == len(header_cells)
                and set(''.join(separator_cells)) <= _SEPARATOR_CHARS
            ):
                rows = []
                j = i + 2
                while j < len(lines) and lines[j].strip(' ') != '':
                    rows.append((j, lines[j]))
                    j += 1
                tables.append(_RawTable(
                    header_line=i,
                    header=lines[i],
                    border=border,
                    rows=tuple(rows),
                ))
                i = j
                continue
        i += 1
    return tables


def recognize_table(
    source: str,
    table: int = 0,
    column_roles: Sequence[str | None] | None = None,
    units: dict[str, Any] | None = None,
) -> RecognizedTable:
    """
    Recognize the `table`-th (0-based) Markdown table in `source`.

    Arguments:
        source: Markdown text containing at least one pipe table.
        table: 0-based index of the table to recognize.
        column_roles: Optional roles assigned left to right, one per column.
            Fewer roles than columns leaves the trailing columns role-less;
            `None` entries stay role-less. Roles must be unique.
        units: Optional mapping of role to unit string, or to `DIMENSIONLESS`
            for an explicitly dimensionless column. Roles absent from the
            mapping have a missing unit (`unit is None`). The mapping is
            scoped to this table only; keys naming a role this table does not
            have raise `TableRecognitionError`.

    Returns:
        A `RecognizedTable` whose columns carry stable role identities,
        original coordinates and the conversion basis.

    Raises:
        TableRecognitionError: If no such table exists or the declarations
            are inconsistent.
    """
    tables = _find_tables(source)
    if not tables:
        raise TableRecognitionError('no Markdown table found in source')
    if not 0 <= table < len(tables):
        raise TableRecognitionError(
            f'table index {table} out of range; found {len(tables)} table(s)'
        )
    raw = tables[table]

    headers = [cell.strip(' ') for cell in _split_line(raw.header.strip(' '), raw.border)]
    column_count = len(headers)

    roles: list[str | None] = list(column_roles) if column_roles is not None else []
    if len(roles) > column_count:
        raise TableRecognitionError(
            f'{len(roles)} column roles declared for {column_count} column(s)'
        )
    roles.extend([None] * (column_count - len(roles)))
    named = [role for role in roles if role is not None]
    if len(set(named)) != len(named):
        raise TableRecognitionError('column roles must be unique')

    declared_units = dict(units) if units else {}
    unknown = sorted(set(declared_units) - set(named))
    if unknown:
        raise TableRecognitionError(
            f'unit declared for role(s) not present in table {table}: {unknown}'
        )

    body: list[tuple[int, list[str]]] = []
    for line_number, row_line in raw.rows:
        cells = _split_line(row_line.strip(' '), raw.border)
        values = []
        for position in range(column_count):
            try:
                values.append(cells[position].strip(' '))
            except IndexError:
                values.append('')
        body.append((line_number, values))

    columns = []
    for position in range(column_count):
        role = roles[position]
        if role is None or role not in declared_units:
            unit = None
        else:
            unit = declared_units[role]
            if unit is DIMENSIONLESS:
                unit = DIMENSIONLESS
            elif unit is None:
                unit = None
            elif not isinstance(unit, str) or not unit:
                raise TableRecognitionError(
                    f'invalid unit for role {role!r}: {unit!r}; '
                    'use a non-empty string or DIMENSIONLESS'
                )
        columns.append(RecognizedColumn(
            role=role,
            header=headers[position],
            index=position,
            unit=unit,
            cells=tuple(
                Cell(value=values[position], row=row, column=position, line=line_number)
                for row, (line_number, values) in enumerate(body)
            ),
        ))

    return RecognizedTable(index=table, header_line=raw.header_line, columns=tuple(columns))
