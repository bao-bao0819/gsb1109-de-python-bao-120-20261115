#
# Copyright (c) 2012-2018, CRS4
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import unittest

from hl7apy.migration import (
    ORIGIN_DEFAULT,
    ORIGIN_SOURCE,
    REASON_COLLAPSED_COMPOSITE,
    REASON_SPLIT_COMPONENT,
    REASON_WITHDRAWN,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    build_version_mapping,
    migrate_field,
    migrate_message,
)
from hl7apy.parser import parse_field, parse_message


class TestBuildVersionMapping(unittest.TestCase):
    def test_field_datatype_correspondence(self):
        mapping = build_version_mapping('2.5', '2.8.1')
        self.assertEqual(mapping['source_version'], '2.5')
        self.assertEqual(mapping['target_version'], '2.8.1')
        self.assertEqual(mapping['fields']['PID_7']['source_datatype'], 'TS')
        self.assertEqual(mapping['fields']['PID_7']['target_datatype'], 'DTM')
        self.assertEqual(mapping['fields']['OBR_4']['source_datatype'], 'CE')
        self.assertEqual(mapping['fields']['OBR_4']['target_datatype'], 'CWE')

    def test_component_correspondence_by_long_name(self):
        mapping = build_version_mapping('2.5', '2.8.1')
        # trigger the lazy computation of the CE -> CWE correspondence
        field = parse_field('123^TEXT^LN', name='OBR_4', version='2.5')
        migrate_field(field, '2.8.1', mapping=mapping)
        components = mapping['datatypes'][('CE', 'CWE')]['components']
        self.assertEqual(components['CE_1'], 'CWE_1')
        self.assertEqual(components['CE_3'], 'CWE_3')


class TestMigrateField(unittest.TestCase):
    def test_composite_field_keeps_provenance(self):
        field = parse_field('123^TEXT^LN', name='OBR_4', version='2.5')
        fm = migrate_field(field, '2.8.1')
        self.assertEqual(fm.status, STATUS_COMPLETE)
        self.assertEqual(fm.source_datatype, 'CE')
        self.assertEqual(fm.target_datatype, 'CWE')
        self.assertEqual(fm.field.to_er7(), '123^TEXT^LN')
        by_path = {e.source_path: e for e in fm.entries}
        entry = by_path['OBR_4/CE_1']
        self.assertEqual(entry.source_datatype, 'ST')
        self.assertEqual(entry.target_path, 'OBR_4/CWE_1')
        self.assertEqual(entry.target_datatype, 'ST')
        self.assertEqual(entry.value, '123')
        self.assertEqual(entry.origin, ORIGIN_SOURCE)

    def test_collapsed_composite_is_irreversible(self):
        # TS in 2.5 becomes the scalar DTM in 2.8.1: TS_2 (degree of
        # precision) cannot be represented anymore
        field = parse_field('19800101120000^Y', name='PID_7', version='2.5')
        fm = migrate_field(field, '2.8.1')
        self.assertEqual(fm.field.to_er7(), '19800101120000')
        self.assertEqual(fm.entries[0].source_path, 'PID_7/TS_1')
        self.assertEqual(fm.entries[0].origin, ORIGIN_SOURCE)
        self.assertEqual(len(fm.unmapped), 1)
        unmapped = fm.unmapped[0]
        self.assertEqual(unmapped.source_path, 'PID_7/TS_2')
        self.assertEqual(unmapped.source_datatype, 'ID')
        self.assertEqual(unmapped.value, 'Y')
        self.assertEqual(unmapped.reason, REASON_COLLAPSED_COMPOSITE)
        self.assertIn(REASON_COLLAPSED_COMPOSITE, [i.reason for i in fm.irreversible])

    def test_split_component_is_irreversible(self):
        # IS in 2.5 becomes CWE in 2.8.1: the scalar value is placed into
        # the first component of the split datatype
        field = parse_field('M', name='PID_8', version='2.5')
        fm = migrate_field(field, '2.8.1')
        self.assertEqual(fm.field.to_er7(), 'M')
        self.assertEqual(fm.entries[0].target_path, 'PID_8/CWE_1')
        self.assertEqual(fm.entries[0].value, 'M')
        self.assertIn(REASON_SPLIT_COMPONENT, [i.reason for i in fm.irreversible])

    def test_missing_required_marks_incomplete(self):
        # CX_1 and CX_5 are required in 2.8.1
        field = parse_field('^^^HOSP', name='PID_3', version='2.5')
        fm = migrate_field(field, '2.8.1')
        self.assertEqual(fm.status, STATUS_INCOMPLETE)
        self.assertIn('PID_3/CX_1', fm.missing_required)
        self.assertIn('PID_3/CX_5', fm.missing_required)

    def test_complete_when_required_present(self):
        field = parse_field('123^^^HOSP^MR', name='PID_3', version='2.5')
        fm = migrate_field(field, '2.8.1')
        self.assertEqual(fm.status, STATUS_COMPLETE)
        self.assertEqual(fm.missing_required, [])

    def test_defaults_do_not_masquerade_as_source(self):
        field = parse_field('DOE^JOHN', name='PID_5', version='2.5')
        fm = migrate_field(field, '2.8.1', defaults={'PID_5': {'XPN_7': 'L'}})
        self.assertEqual(fm.field.to_er7(), 'DOE^JOHN^^^^^L')
        default_entries = fm.defaults_applied
        self.assertEqual(len(default_entries), 1)
        self.assertEqual(default_entries[0].origin, ORIGIN_DEFAULT)
        self.assertEqual(default_entries[0].target_path, 'PID_5/XPN_7')
        self.assertEqual(default_entries[0].value, 'L')
        self.assertIsNone(default_entries[0].source_path)
        source_entries = [e for e in fm.entries if e.origin == ORIGIN_SOURCE]
        self.assertTrue(all(e.value != 'L' for e in source_entries))

    def test_defaults_do_not_override_source_values(self):
        field = parse_field('DOE^JOHN^^^^^DR', name='PID_5', version='2.5')
        fm = migrate_field(field, '2.8.1', defaults={'PID_5': {'XPN_7': 'L'}})
        self.assertEqual(fm.field.to_er7(), 'DOE^JOHN^^^^^DR')
        self.assertEqual(fm.defaults_applied, [])

    def test_withdrawn_field_values_are_preserved_as_unmapped(self):
        # TQ has been withdrawn (WD) in 2.8.1
        field = parse_field('^BID^^20100101', name='ORC_7', version='2.5')
        fm = migrate_field(field, '2.8.1')
        self.assertIsNone(fm.field)
        self.assertTrue(fm.unmapped)
        self.assertTrue(all(u.reason == REASON_WITHDRAWN for u in fm.unmapped))
        values = {u.source_path: u.value for u in fm.unmapped}
        self.assertEqual(values['ORC_7/TQ_2/RI_1'], 'BID')
        self.assertEqual(values['ORC_7/TQ_4/TS_1'], '20100101')
        self.assertIn(REASON_WITHDRAWN, [i.reason for i in fm.irreversible])

    def test_unknown_field_is_preserved_as_unmapped(self):
        field = parse_field('SOME^Z^DATA', version='2.5')
        field.name = 'ZDS_1'
        fm = migrate_field(field, '2.8.1')
        self.assertIsNone(fm.field)
        self.assertEqual(len(fm.unmapped), 1)
        self.assertEqual(fm.unmapped[0].value, 'SOME^Z^DATA')


class TestMigrateMessage(unittest.TestCase):
    def setUp(self):
        self.message = (
            'MSH|^~\\&|SNDAPP|SNDFAC|RCVAPP|RCVFAC|20201101120000||ADT^A01^ADT_A01|MSGID1|P|2.5\r'
            'PID|1||123456^^^HOSP^MR||DOE^JOHN^J||19800101000000|M\r'
        )

    def test_message_migration(self):
        message = parse_message(self.message)
        report = migrate_message(message, '2.8.1')
        er7 = report.message.to_er7()
        segments = er7.split('\r')
        self.assertEqual(len([s for s in segments if s.startswith('MSH')]), 1)
        self.assertTrue(segments[0].startswith('MSH|^~\\&|SNDAPP|'))
        self.assertEqual(segments[0].split('|')[11], '2.8.1')
        self.assertIn('123456^^^HOSP^MR', segments[1])
        self.assertIn('DOE^JOHN^J', segments[1])

    def test_msh12_is_a_default_not_a_source_submission(self):
        message = parse_message(self.message)
        report = migrate_message(message, '2.8.1')
        version_entries = [e for e in report.entries if e.target_path == 'MSH/MSH_12']
        self.assertEqual(len(version_entries), 1)
        self.assertEqual(version_entries[0].origin, ORIGIN_DEFAULT)
        self.assertEqual(version_entries[0].value, '2.8.1')
        self.assertIn(version_entries[0], report.defaults_applied)

    def test_report_aggregates(self):
        message = parse_message(self.message)
        report = migrate_message(message, '2.8.1')
        self.assertEqual(report.status, STATUS_COMPLETE)
        self.assertEqual(report.missing_required, [])
        self.assertTrue(report.entries)
        self.assertIsInstance(report.summary(), str)
        # the split of PID_8 (IS -> CWE) cannot be round-tripped
        self.assertIn(REASON_SPLIT_COMPONENT, [i.reason for i in report.irreversible])

    def test_incomplete_when_required_missing(self):
        message = parse_message(
            'MSH|^~\\&|SNDAPP|SNDFAC|RCVAPP|RCVFAC|20201101120000||ADT^A01|MSGID1|P|2.5\r'
            'PID|1||^^^HOSP^MR\r'
        )
        report = migrate_message(message, '2.8.1')
        self.assertEqual(report.status, STATUS_INCOMPLETE)
        self.assertIn('PID/PID_3/CX_1', report.missing_required)

    def test_reused_mapping(self):
        mapping = build_version_mapping('2.5', '2.8.1')
        message = parse_message(self.message)
        report = migrate_message(message, '2.8.1', mapping=mapping)
        self.assertIsNotNone(report.message)
        self.assertTrue(mapping['datatypes'])


if __name__ == '__main__':
    unittest.main()
