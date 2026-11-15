# Python Markdown

# A Python implementation of John Gruber's Markdown.

# Documentation: https://python-markdown.github.io/
# GitHub: https://github.com/Python-Markdown/markdown/
# PyPI: https://pypi.org/project/Markdown/

# Started by Manfred Stienstra (http://www.dwerg.net/).
# Maintained for a few years by Yuri Takhteyev (http://www.freewisdom.org).
# Currently maintained by Waylan Limberg (https://github.com/waylan),
# Dmitry Shachnev (https://github.com/mitya57) and Isaac Muse (https://github.com/facelessuser).

# Copyright 2007-2023 The Python Markdown Project (v. 1.7 and later)
# Copyright 2004, 2005, 2006 Yuri Takhteyev (v. 0.2-1.6b)
# Copyright 2004 Manfred Stienstra (the original version)

# License: BSD (see LICENSE.md for details).

""" Tests for `markdown.tablerecognition`. """

from __future__ import annotations

import unittest

from markdown.tablerecognition import (
    DIMENSIONLESS,
    TableRecognitionError,
    recognize_table,
)

SUPPLIER_DOC = (
    '单位：订货数量按箱，实收数量按件，备注无量纲。\n'
    '\n'
    '| 物料 | 数量 | 数量 | 备注 |\n'
    '| --- | --- | --- | --- |\n'
    '| 螺丝 | 10 | 48 | 含税 |\n'
    '| 螺母 | 20 | 96 | 未税 |\n'
)

ROLES = ['item', 'order_qty', 'received_qty', 'note']
UNITS = {'order_qty': '箱', 'received_qty': '件', 'note': DIMENSIONLESS}


class TestRecognizeTable(unittest.TestCase):

    def test_roles_give_same_name_columns_stable_identities(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=ROLES, units=UNITS)
        self.assertEqual(table.headers, ['物料', '数量', '数量', '备注'])
        self.assertEqual(table.roles, ROLES)
        order, received = table.columns[1], table.columns[2]
        self.assertEqual(order.header, received.header)
        self.assertEqual(order.role, 'order_qty')
        self.assertEqual(received.role, 'received_qty')

    def test_same_name_columns_keep_original_values(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=ROLES, units=UNITS)
        order, received = table.columns[1], table.columns[2]
        self.assertEqual([cell.value for cell in order.cells], ['10', '20'])
        self.assertEqual([cell.value for cell in received.cells], ['48', '96'])

    def test_original_coordinates(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=ROLES, units=UNITS)
        self.assertEqual(table.header_line, 2)
        cell = table.columns[2].cells[1]
        self.assertEqual((cell.value, cell.row, cell.column, cell.line), ('96', 1, 2, 5))

    def test_conversion_basis(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=ROLES, units=UNITS)
        self.assertEqual(table.conversion_basis, {
            'item': None,
            'order_qty': '箱',
            'received_qty': '件',
            'note': DIMENSIONLESS,
        })

    def test_missing_unit_and_dimensionless_are_distinct(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=ROLES, units=UNITS)
        item, note = table.columns[0], table.columns[3]
        self.assertIsNone(item.unit)
        self.assertEqual(item.unit_status, 'missing')
        self.assertIs(note.unit, DIMENSIONLESS)
        self.assertEqual(note.unit_status, 'dimensionless')
        self.assertEqual(table.columns[1].unit_status, 'declared')

    def test_reorder_maps_by_role_not_old_index(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=ROLES, units=UNITS)
        reordered = table.reorder(['received_qty', 'order_qty'])
        self.assertEqual(
            reordered.roles, ['received_qty', 'order_qty', 'item', 'note']
        )
        # Original indices and coordinates survive the reorder.
        self.assertEqual([column.index for column in reordered.columns], [2, 1, 0, 3])
        self.assertEqual(reordered.columns[0].cells[0].value, '48')
        self.assertEqual(reordered.columns[0].cells[0].column, 2)
        self.assertEqual(reordered.rows()[0], ('48', '10', '螺丝', '含税'))

    def test_reorder_unknown_role_raises(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=ROLES)
        with self.assertRaises(KeyError):
            table.reorder(['not_a_role'])

    def test_declaration_scope_is_limited_to_the_given_table(self):
        source = (
            '| a | b |\n| - | - |\n| 1 | 2 |\n'
            '\n'
            '| c | d |\n| - | - |\n| 3 | 4 |\n'
        )
        first = recognize_table(source, table=0, column_roles=['x', 'y'])
        second = recognize_table(
            source, table=1, column_roles=['x', 'y'], units={'x': 'kg'}
        )
        self.assertEqual(first.conversion_basis, {'x': None, 'y': None})
        self.assertEqual(second.conversion_basis, {'x': 'kg', 'y': None})
        self.assertEqual(second.index, 1)
        self.assertEqual(second.columns[0].cells[0].value, '3')

    def test_unit_for_unknown_role_raises(self):
        with self.assertRaises(TableRecognitionError):
            recognize_table(
                SUPPLIER_DOC, column_roles=ROLES, units={'other_table_role': '箱'}
            )

    def test_duplicate_roles_raise(self):
        with self.assertRaises(TableRecognitionError):
            recognize_table(SUPPLIER_DOC, column_roles=['q', 'q'])

    def test_too_many_roles_raise(self):
        with self.assertRaises(TableRecognitionError):
            recognize_table(SUPPLIER_DOC, column_roles=ROLES + ['extra'])

    def test_fewer_roles_leave_trailing_columns_roleless(self):
        table = recognize_table(SUPPLIER_DOC, column_roles=['item'])
        self.assertEqual(table.roles, ['item', None, None, None])
        self.assertEqual(table.headers, ['物料', '数量', '数量', '备注'])

    def test_no_table_raises(self):
        with self.assertRaises(TableRecognitionError):
            recognize_table('no table here\n')

    def test_table_index_out_of_range_raises(self):
        with self.assertRaises(TableRecognitionError):
            recognize_table(SUPPLIER_DOC, table=1)

    def test_invalid_unit_raises(self):
        with self.assertRaises(TableRecognitionError):
            recognize_table(SUPPLIER_DOC, column_roles=ROLES, units={'item': ''})

    def test_escaped_and_code_pipes_stay_in_cell(self):
        source = (
            '| a | b |\n| - | - |\n'
            '| x \\| y | `p | q` |\n'
        )
        table = recognize_table(source, column_roles=['first', 'second'])
        self.assertEqual(table.columns[0].cells[0].value, 'x \\| y')
        self.assertEqual(table.columns[1].cells[0].value, '`p | q`')

    def test_short_rows_are_padded(self):
        source = '| a | b |\n| - | - |\n| 1 |\n'
        table = recognize_table(source)
        self.assertEqual(table.columns[1].cells[0].value, '')

    def test_table_without_border(self):
        source = 'a | b\n-- | --\n1 | 2\n'
        table = recognize_table(source, column_roles=['x', 'y'], units={'y': 'm'})
        self.assertEqual(table.headers, ['a', 'b'])
        self.assertEqual(table.rows(), [('1', '2')])
        self.assertEqual(table.conversion_basis, {'x': None, 'y': 'm'})


if __name__ == '__main__':
    unittest.main()
