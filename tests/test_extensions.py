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
from concurrent.futures import ThreadPoolExecutor

from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message, Segment
from hl7apy.exceptions import OperationNotAllowed
from hl7apy.extensions import (
    ExtensionContext,
    clear_z_segment_registry,
    get_registered_z_segments,
    register_z_segment,
    register_z_segments,
    unregister_z_segment,
)
from hl7apy.parser import parse_message, parse_segment

HL7_VERSION = '2.5'
SEGMENT_TEXT = 'ZZZ|hello|123^text^sys'
MESSAGE_TEXT = (
    r'MSH|^~\&|APP|FAC|||20260101000000||ADT^A01^ADT_A01|1|P|2.5'
    '\rPID|1||123^^^HOSP^MR||DOE^JOHN\rZZZ|hello|123^text^sys\r'
)


class ExtensionTestCase(unittest.TestCase):
    def setUp(self):
        clear_z_segment_registry()
        register_z_segments(
            'HOSP_A',
            '1.0',
            {'ZZZ': [('ST', 'A_NOTE'), ('CWE', 'A_CODE', (0, -1))]},
        )
        register_z_segments(
            'HOSP_B',
            '1.0',
            {'ZZZ': [('NM', 'B_MEASURE'), ('ST', 'B_LABEL')]},
        )
        register_z_segments(
            'HOSP_A',
            '2.0',
            {
                'ZZZ': [
                    {'datatype': 'ST', 'long_name': 'A2_NOTE'},
                    {'datatype': 'ST', 'long_name': 'A2_EXTRA'},
                    {'datatype': 'CWE', 'long_name': 'A2_CODE', 'table': 'HL79999'},
                ]
            },
        )

    def tearDown(self):
        clear_z_segment_registry()

    def test_same_segment_name_resolved_per_institution(self):
        segment_a = parse_segment(
            SEGMENT_TEXT, version=HL7_VERSION, extension_context=ExtensionContext('HOSP_A', '1.0')
        )
        segment_b = parse_segment(
            SEGMENT_TEXT, version=HL7_VERSION, extension_context=ExtensionContext('HOSP_B', '1.0')
        )

        self.assertEqual(
            [(f.name, f.datatype, f.long_name) for f in segment_a.children],
            [('ZZZ_1', 'ST', 'A_NOTE'), ('ZZZ_2', 'CWE', 'A_CODE')],
        )
        # hospital B declares an ST field; the multi-component input triggers the
        # standard tolerant downgrade (datatype None), but its own definition is kept
        self.assertEqual(
            [(f.name, f.long_name) for f in segment_b.children],
            [('ZZZ_1', 'B_MEASURE'), ('ZZZ_2', 'B_LABEL')],
        )
        self.assertEqual(segment_b.structure_by_name['ZZZ_2']['ref'][2], 'ST')
        # the CWE definition of hospital A expands the components
        self.assertEqual([c.name for c in segment_a.zzz_2.children], ['CWE_1', 'CWE_2', 'CWE_3'])
        self.assertEqual([c.datatype for c in segment_a.zzz_2.children], ['ST', 'ST', 'ID'])

    def test_definition_selected_by_config_version(self):
        segment_v1 = parse_segment(
            SEGMENT_TEXT, version=HL7_VERSION, extension_context=ExtensionContext('HOSP_A', '1.0')
        )
        three_field_text = 'ZZZ|hello|middle|123^text^sys'
        segment_v2 = parse_segment(
            three_field_text, version=HL7_VERSION, extension_context=ExtensionContext('HOSP_A', '2.0')
        )

        self.assertEqual([f.long_name for f in segment_v1.children], ['A_NOTE', 'A_CODE'])
        self.assertEqual([f.long_name for f in segment_v2.children], ['A2_NOTE', 'A2_EXTRA', 'A2_CODE'])
        self.assertEqual(segment_v2.structure_by_name['ZZZ_3']['ref'][4], 'HL79999')
        self.assertEqual(segment_v2.to_er7(), three_field_text)

    def test_unknown_extension_keeps_generic_structure(self):
        # unknown institution, same segment name present in the message
        segment = parse_segment(SEGMENT_TEXT, version=HL7_VERSION, extension_context=ExtensionContext('HOSP_C', '1.0'))
        self.assertEqual([(f.name, f.datatype) for f in segment.children], [('ZZZ_1', 'ST'), ('ZZZ_2', None)])
        self.assertEqual(segment.to_er7(), SEGMENT_TEXT)

        # no context at all: legacy behavior is unchanged
        legacy_segment = parse_segment(SEGMENT_TEXT, version=HL7_VERSION)
        self.assertEqual(
            [(f.name, f.datatype) for f in legacy_segment.children],
            [(f.name, f.datatype) for f in segment.children],
        )
        self.assertEqual(legacy_segment.to_er7(), SEGMENT_TEXT)

    def test_standard_segments_are_not_overridden(self):
        context = ExtensionContext('HOSP_A', '1.0')
        pid = parse_segment('PID|1||123^^^HOSP^MR', version=HL7_VERSION, extension_context=context)
        self.assertEqual(pid.pid_3.long_name, 'PATIENT_IDENTIFIER_LIST')

        message = parse_message(MESSAGE_TEXT, find_groups=False, extension_context=context)
        self.assertEqual(message.pid.pid_3.long_name, 'PATIENT_IDENTIFIER_LIST')
        z_segment = [child for child in message.children if child.name == 'ZZZ'][0]
        self.assertEqual([f.datatype for f in z_segment.children], ['ST', 'CWE'])

    def test_roundtrip_is_preserved(self):
        for institution, config_version in (('HOSP_A', '1.0'), ('HOSP_B', '1.0'), ('HOSP_A', '2.0')):
            segment = parse_segment(
                SEGMENT_TEXT,
                version=HL7_VERSION,
                extension_context=ExtensionContext(institution, config_version),
            )
            self.assertEqual(segment.to_er7(), SEGMENT_TEXT)

    def test_contexts_have_independent_caches(self):
        context_a = ExtensionContext('HOSP_A', '1.0')
        context_b = ExtensionContext('HOSP_B', '1.0')
        reference_a = context_a.get_segment_reference('ZZZ', HL7_VERSION)
        reference_b = context_b.get_segment_reference('ZZZ', HL7_VERSION)

        self.assertIsNotNone(reference_a)
        self.assertIsNotNone(reference_b)
        self.assertIsNot(reference_a, reference_b)
        self.assertIsNot(context_a._reference_cache, context_b._reference_cache)
        self.assertIsNone(context_a.get_segment_reference('Z99', HL7_VERSION))

    def test_interleaved_parsing_never_mixes_rules(self):
        text = SEGMENT_TEXT
        expectations = {
            'HOSP_A': ('ST', 'CWE'),
            'HOSP_B': ('NM', 'ST'),
        }

        for _ in range(50):
            for institution, expected in expectations.items():
                segment = parse_segment(
                    text,
                    version=HL7_VERSION,
                    extension_context=ExtensionContext(institution, '1.0'),
                )
                declared = tuple(segment.structure_by_name[name]['ref'][2] for name in segment.ordered_children)
                self.assertEqual(declared, expected)

    def test_parallel_parsing_uses_own_rules(self):
        def job(institution):
            expected = ('ST', 'CWE') if institution == 'HOSP_A' else ('NM', 'ST')
            context = ExtensionContext(institution, '1.0')
            for _ in range(100):
                segment = parse_segment(SEGMENT_TEXT, version=HL7_VERSION, extension_context=context)
                declared = tuple(segment.structure_by_name[name]['ref'][2] for name in segment.ordered_children)
                self.assertEqual(declared, expected)
                self.assertEqual(segment.to_er7(), SEGMENT_TEXT)
            return institution

        with ThreadPoolExecutor(max_workers=4) as executor:
            institutions = ['HOSP_A', 'HOSP_B'] * 4
            result = list(executor.map(job, institutions))
        self.assertEqual(sorted(result), sorted(institutions))

    def test_fields_beyond_definition_keep_generic_handling(self):
        segment = parse_segment(
            'ZZZ|note|1^2^3|extra1|extra2',
            version=HL7_VERSION,
            extension_context=ExtensionContext('HOSP_A', '1.0'),
        )
        self.assertEqual(
            [(f.name, f.datatype) for f in segment.children],
            [('ZZZ_1', 'ST'), ('ZZZ_2', 'CWE'), ('ZZZ_3', 'ST'), ('ZZZ_4', 'ST')],
        )
        self.assertEqual(segment.to_er7(), 'ZZZ|note|1^2^3|extra1|extra2')

    def test_build_message_with_core_api(self):
        context = ExtensionContext('HOSP_A', '1.0')
        message = Message('ADT_A01', extension_context=context)
        message.zzz = 'ZZZ|note|1^2^3'
        z_segment = [child for child in message.children if child.name == 'ZZZ'][0]
        self.assertEqual(
            [(f.name, f.datatype, f.long_name) for f in z_segment.children],
            [('ZZZ_1', 'ST', 'A_NOTE'), ('ZZZ_2', 'CWE', 'A_CODE')],
        )

    def test_strict_parsing_with_known_definition(self):
        segment = parse_segment(
            SEGMENT_TEXT,
            version=HL7_VERSION,
            validation_level=VALIDATION_LEVEL.STRICT,
            extension_context=ExtensionContext('HOSP_A', '1.0'),
        )
        self.assertEqual([(f.name, f.datatype) for f in segment.children], [('ZZZ_1', 'ST'), ('ZZZ_2', 'CWE')])

    def test_segment_created_directly_uses_context(self):
        context = ExtensionContext('HOSP_B', '1.0')
        segment = Segment('ZZZ', version=HL7_VERSION, extension_context=context)
        self.assertEqual(segment.ordered_children, ['ZZZ_1', 'ZZZ_2'])
        self.assertEqual(segment.structure_by_name['ZZZ_1']['ref'][2], 'NM')

    def test_registration_helpers(self):
        self.assertEqual(
            get_registered_z_segments('HOSP_A', '1.0'),
            ('ZZZ',),
        )
        unregister_z_segment('HOSP_A', '1.0', 'zzz')
        self.assertEqual(get_registered_z_segments('HOSP_A', '1.0'), ())

        with self.assertRaises(ValueError):
            register_z_segment('HOSP_B', '1.0', 'ZZZ', ['ST'])
        register_z_segment('HOSP_B', '1.0', 'ZZZ', ['ST'], replace=True)
        segment = parse_segment(
            'ZZZ|only_one', version=HL7_VERSION, extension_context=ExtensionContext('HOSP_B', '1.0')
        )
        self.assertEqual([f.datatype for f in segment.children], ['ST'])

    def test_invalid_registration(self):
        with self.assertRaises(ValueError):
            ExtensionContext('', '1.0')
        with self.assertRaises(ValueError):
            ExtensionContext('HOSP_A', '')
        with self.assertRaises(ValueError):
            register_z_segment('HOSP_A', '1.0', 'PID', ['ST'])
        with self.assertRaises(ValueError):
            register_z_segment('HOSP_A', '1.0', 'ZZZ', [])
        with self.assertRaises(ValueError):
            register_z_segment('HOSP_A', '1.0', 'ZZZ', [('ST', 'NOTE', (2, 1))])

    def test_explicit_reference_takes_precedence(self):
        explicit_reference = (
            'sequence',
            (('ZZZ_1', ('leaf', None, 'NM', 'EXPLICIT_NUM', None, -1), (0, 1), 'FIE'),),
        )
        segment = parse_segment(
            'ZZZ|5',
            version=HL7_VERSION,
            reference=explicit_reference,
            extension_context=ExtensionContext('HOSP_A', '1.0'),
        )
        self.assertEqual(
            [(f.name, f.datatype, f.long_name) for f in segment.children],
            [('ZZZ_1', 'NM', 'EXPLICIT_NUM')],
        )

    def test_invalid_extension_context_is_rejected(self):
        with self.assertRaises(OperationNotAllowed):
            Segment('ZZZ', version=HL7_VERSION, extension_context='not-a-context')


if __name__ == '__main__':
    unittest.main()
