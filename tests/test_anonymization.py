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

from hl7apy.anonymization import (
    AnonymizationError,
    AnonymizationPolicy,
    Anonymizer,
    PathPattern,
    anonymize_er7,
    anonymize_message,
)
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.parser import parse_message, parse_segment


def _adt_a01():
    return (
        'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20200101000000||ADT^A01^ADT_A01|1|P|2.5\r'
        'EVN||20200101000000\r'
        'PID|1||123-456-789^^^HOSPITAL^MR||DOE^JOHN^A||19800101|M|||1 MAIN STREET^^CITY^^12345^USA||'
        '555-555-2004~444-333-2222|||M\r'
        'NK1|1|DOE^JANE|SPO|1 MAIN STREET^^CITY\r'
        'PV1||O|||||||||||||||||1107080001^^^LIS\r'
    )


def _oru_r01():
    return (
        'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20200101000000||ORU^R01^ORU_R01|1|P|2.5\r'
        'PID|1||123-456-789^^^HOSPITAL^MR||DOE^JOHN^A||19800101|M\r'
        'OBR|1|||GLU^Glucose\r'
        'OBX|1|NM|GLU^Glucose||123.45|mg/dL|||||F\r'
        'OBX|2|ST|NOTE^Note||SECRET TEXT||||||F\r'
        'OBX|3|DT|DATE^Collection date||20200101||||||F\r'
    )


def _find_all(element, name):
    found = []
    if element.name == name:
        found.append(element)
    for child in element.children:
        found.extend(_find_all(child, name))
    return found


class TestPathPattern(unittest.TestCase):
    def test_full_path_matches_itself(self):
        self.assertTrue(PathPattern('ADT_A01.PID.PID_5.PID_5_2').matches('ADT_A01.PID.PID_5.PID_5_2'))

    def test_short_path_is_anchored_to_the_end(self):
        self.assertTrue(PathPattern('PID.PID_5').matches('ADT_A01.PID.PID_5'))
        self.assertFalse(PathPattern('PID.PID_5').matches('ADT_A01.PID.PID_7'))

    def test_wildcards(self):
        self.assertTrue(PathPattern('ADT_A01.*.PID_5').matches('ADT_A01.PID.PID_5'))
        self.assertTrue(PathPattern('PID_?.PID_5_2').matches('ADT_A01.PID.PID_5.PID_5_2'))

    def test_occurrence_index_is_kept_in_the_string_form(self):
        pattern = PathPattern('PID.PID_3[2]')
        self.assertEqual(str(pattern), 'PID.PID_3[2]')
        self.assertEqual(pattern.name, 'PID_3')

    def test_dash_and_positional_notation(self):
        pattern = PathPattern('PID-5-2')
        self.assertEqual(pattern.steps[1]['name'], '5')
        self.assertEqual(pattern.steps[2]['name'], '2')

    def test_invalid_pattern(self):
        self.assertRaises(AnonymizationError, PathPattern, '')
        self.assertRaises(AnonymizationError, PathPattern, 'PID..PID_5')


class TestAnonymizationPolicy(unittest.TestCase):
    def test_paths_and_contains(self):
        policy = AnonymizationPolicy({'PID.PID_5': 'PATIENT'})
        self.assertEqual(policy.paths(), ['PID.PID_5'])
        self.assertIn('PID.PID_5', policy)
        self.assertEqual(len(policy), 1)

    def test_mapping_and_pair_forms_are_equivalent(self):
        from_mapping = AnonymizationPolicy({'PID.PID_5': 'X'})
        from_pairs = AnonymizationPolicy([('PID.PID_5', 'X')])
        self.assertEqual(from_mapping.paths(), from_pairs.paths())

    def test_invalid_policy(self):
        self.assertRaises(AnonymizationError, anonymize_message, _adt_a01(), 1)


class TestAnonymizeMessage(unittest.TestCase):
    def test_target_component_changes_only_that_component(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_5.PID_5_2': 'ANON'})
        self.assertEqual(result.pid.pid_5.to_er7(), 'DOE^ANON^A')
        self.assertEqual(message.pid.pid_5.to_er7(), 'DOE^JOHN^A')

    def test_whole_field_replacement_keeps_the_remaining_structure(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_5': 'PATIENT'})
        self.assertEqual(result.pid.pid_5.to_er7(), 'PATIENT')

    def test_same_value_at_non_target_path_is_not_changed(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'NK1.NK1_2.NK1_2_1': 'FAMILY'})
        self.assertEqual(result.nk1.nk1_2.to_er7(), 'FAMILY^JANE')
        self.assertEqual(result.pid.pid_5.pid_5_1.to_er7(), 'DOE')

    def test_equal_values_receive_the_same_token(self):
        message = parse_message(_adt_a01())
        policy = {'PID.PID_5.PID_5_1': None, 'NK1.NK1_2.NK1_2_1': None}
        result = anonymize_message(message, policy)
        self.assertEqual(result.pid.pid_5.pid_5_1.to_er7(), result.nk1.nk1_2.nk1_2_1.to_er7())

    def test_different_values_receive_different_tokens(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_5.PID_5_2': None, 'PID.PID_5.PID_5_3': None})
        self.assertNotEqual(result.pid.pid_5.pid_5_2.to_er7(), result.pid.pid_5.pid_5_3.to_er7())

    def test_in_place_modifies_the_given_message(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_5.PID_5_2': 'ANON'}, deep_copy=False)
        self.assertIs(result, message)
        self.assertEqual(message.pid.pid_5.to_er7(), 'DOE^ANON^A')

    def test_string_input_is_parsed(self):
        result = anonymize_message(_adt_a01(), {'PID.PID_5.PID_5_2': 'ANON'})
        self.assertEqual(result.pid.pid_5.pid_5_2.to_er7(), 'ANON')

    def test_er7_helper(self):
        output = anonymize_er7(_adt_a01(), {'PID.PID_5.PID_5_2': 'ANON'})
        self.assertIn('DOE^ANON^A', output)
        self.assertIn('DOE^JANE', output)

    def test_callable_replacement(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_5.PID_5_1': lambda value, element: value[::-1]})
        self.assertEqual(result.pid.pid_5.pid_5_1.to_er7(), 'EOD')

    def test_structural_path_is_rejected(self):
        message = parse_message(_adt_a01())
        self.assertRaises(AnonymizationError, anonymize_message, message, {'PID': 'X'})

    def test_encoding_characters_cannot_be_anonymized(self):
        message = parse_message(_adt_a01())
        self.assertRaises(AnonymizationError, anonymize_message, message, {'MSH.MSH_2': 'X'})

    def test_anonymizer_class(self):
        anonymizer = Anonymizer({'PID.PID_5.PID_5_2': 'ANON'})
        result = anonymizer.anonymize(_adt_a01())
        self.assertEqual(result.pid.pid_5.pid_5_2.to_er7(), 'ANON')
        self.assertEqual(anonymizer.paths(), ['PID.PID_5.PID_5_2'])


class TestRepeatingElements(unittest.TestCase):
    def test_repeated_field_all_occurrences(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_13': 'PHONE'})
        phones = [child.to_er7() for child in result.pid.children if child.name == 'PID_13']
        self.assertEqual(phones, ['PHONE', 'PHONE'])

    def test_repeated_field_single_occurrence(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_13[2]': 'PHONE'})
        phones = [child.to_er7() for child in result.pid.children if child.name == 'PID_13']
        self.assertEqual(phones, ['555-555-2004', 'PHONE'])

    def test_repeated_segment_occurrence(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX[2].OBX_5': 'CHANGED'})
        values = [segment.obx_5.to_er7() for segment in _find_all(result, 'OBX')]
        self.assertEqual(values, ['123.45', 'CHANGED', '20200101'])

    def test_repeated_groups(self):
        message = parse_message(_oru_r01())
        path = (
            'ORU_R01.ORU_R01_PATIENT_RESULT.ORU_R01_ORDER_OBSERVATION.'
            'ORU_R01_OBSERVATION[2].OBX.OBX_5'
        )
        result = anonymize_message(message, {path: 'CHANGED'})
        values = [segment.obx_5.to_er7() for segment in _find_all(result, 'OBX')]
        self.assertEqual(values, ['123.45', 'CHANGED', '20200101'])

    def test_short_group_names(self):
        message = parse_message(_oru_r01())
        path = 'ORU_R01.PATIENT_RESULT.ORDER_OBSERVATION.OBSERVATION[2].OBX.OBX_5'
        result = anonymize_message(message, {path: 'CHANGED'})
        values = [segment.obx_5.to_er7() for segment in _find_all(result, 'OBX')]
        self.assertEqual(values, ['123.45', 'CHANGED', '20200101'])


class TestDynamicDatatypes(unittest.TestCase):
    def test_varies_field_as_a_whole(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX.OBX_5': 'VALUE'})
        values = [segment.obx_5.to_er7() for segment in _find_all(result, 'OBX')]
        # the numeric and the date values cannot hold an arbitrary string
        self.assertTrue(values[0].replace('.', '').isdigit())
        self.assertEqual(values[1], 'VALUE')
        self.assertEqual(len(values[2]), 8)
        self.assertTrue(values[2].isdigit())

    def test_varies_component_by_position(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX.OBX_5.OBX_5_1': 'VALUE'})
        values = [segment.obx_5.to_er7() for segment in _find_all(result, 'OBX')]
        self.assertTrue(values[0].replace('.', '').isdigit())
        self.assertEqual(values[1], 'VALUE')
        self.assertEqual(len(values[2]), 8)

    def test_varies_component_by_runtime_name(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX.OBX_5.VARIES_1': 'VALUE'})
        values = [segment.obx_5.to_er7() for segment in _find_all(result, 'OBX')]
        self.assertTrue(values[0].replace('.', '').isdigit())
        self.assertEqual(values[1], 'VALUE')
        self.assertEqual(len(values[2]), 8)

    def test_varies_datatype_from_obx_2(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX.OBX_5.NM_1': '7.5'})
        values = [segment.obx_5.to_er7() for segment in _find_all(result, 'OBX')]
        self.assertEqual(values, ['7.5', 'SECRET TEXT', '20200101'])

    def test_varies_numeric_default_is_numeric(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX.OBX_5.NM_1': None})
        value = _find_all(result, 'OBX')[0].obx_5.to_er7()
        self.assertEqual(value.count('.'), 1)
        self.assertTrue(value.replace('.', '').isdigit())

    def test_varies_date_default_keeps_precision(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX.OBX_5.OBX_5_1': None})
        value = _find_all(result, 'OBX')[2].obx_5.to_er7()
        self.assertEqual(len(value), 8)
        self.assertTrue(value.isdigit())


class TestConstraints(unittest.TestCase):
    def test_length_constraint_is_respected(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_1': 'A-VERY-LONG-SEQUENCE-ID'})
        value = result.pid.pid_1.to_er7()
        self.assertLessEqual(len(value), 4)
        self.assertTrue(value.isdigit())

    def test_short_element_gets_short_token(self):
        message = parse_segment('ZDT|ab|cd')
        result = anonymize_message(parse_message(_adt_a01()), {'PID.PID_1': None})
        self.assertLessEqual(len(result.pid.pid_1.to_er7()), 4)

    def test_table_constraint_is_respected(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_8': 'NOT-A-SEX-CODE'})
        self.assertIn(result.pid.pid_8.to_er7(), ('A', 'F', 'M', 'N', 'O', 'U'))

    def test_table_default_does_not_keep_the_original(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_8': None})
        self.assertNotEqual(result.pid.pid_8.to_er7(), 'M')
        self.assertIn(result.pid.pid_8.to_er7(), ('A', 'F', 'N', 'O', 'U'))

    def test_date_is_valid_and_keeps_precision(self):
        message = parse_message(_adt_a01())
        result = anonymize_message(message, {'PID.PID_7': None})
        value = result.pid.pid_7.to_er7()
        self.assertEqual(len(value), 8)
        self.assertTrue(value.isdigit())

    def test_output_still_validates(self):
        message = parse_message(_adt_a01())
        policy = {
            'PID.PID_5.PID_5_1': None,
            'PID.PID_5.PID_5_2': None,
            'PID.PID_7': None,
            'PID.PID_8': None,
            'NK1.NK1_2.NK1_2_1': None,
        }
        result = anonymize_message(message, policy)
        self.assertTrue(result.validate(return_errors=True).is_valid)

    def test_output_can_be_parsed_again(self):
        message = parse_message(_oru_r01())
        result = anonymize_message(message, {'OBX.OBX_5': None})
        self.assertEqual(parse_message(result.to_er7()).to_er7(), result.to_er7())

    def test_strict_message_is_handled(self):
        message = parse_message(_adt_a01(), validation_level=VALIDATION_LEVEL.STRICT)
        result = anonymize_message(message, {'PID.PID_5.PID_5_2': 'ANON'})
        self.assertEqual(result.pid.pid_5.to_er7(), 'DOE^ANON^A')

    def test_salt_changes_tokens(self):
        message = parse_message(_adt_a01())
        plain = anonymize_message(message, {'PID.PID_5.PID_5_3': None})
        salted = anonymize_message(
            message, AnonymizationPolicy({'PID.PID_5.PID_5_3': None}, salt='secret')
        )
        self.assertNotEqual(plain.pid.pid_5.pid_5_3.to_er7(), salted.pid.pid_5.pid_5_3.to_er7())


class TestDeepCopy(unittest.TestCase):
    def test_deep_copy_produces_an_equal_independent_message(self):
        import copy

        message = parse_message(_adt_a01())
        duplicate = copy.deepcopy(message)
        self.assertEqual(duplicate.to_er7(), message.to_er7())
        duplicate.pid.pid_5.pid_5_2 = 'CHANGED'
        self.assertEqual(message.pid.pid_5.pid_5_2.to_er7(), 'JOHN')
        self.assertIs(duplicate.pid.parent, duplicate)

    def test_deep_copy_keeps_unresolved_traversal_children(self):
        import copy

        from hl7apy.core import Message

        message = Message('ADT_A01')
        message.pid.pid_5.pid_5_1  # builds the traversal chain
        duplicate = copy.deepcopy(message)
        self.assertIn('PID', duplicate.children.traversal_indexes)
        duplicate.pid.pid_5.pid_5_1 = 'DOE'
        self.assertEqual(message.to_er7(), 'MSH|^~\\&|||||{0}|||||2.5'.format(message.msh.msh_7.to_er7()))
