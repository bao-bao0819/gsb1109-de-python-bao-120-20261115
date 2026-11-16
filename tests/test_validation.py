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

import os
import re
import tempfile
import unittest
from io import StringIO

import hl7apy
from hl7apy.core import Component, Field, Group, Segment, SubComponent
from hl7apy.exceptions import ChildNotFound, ValidationError
from hl7apy.parser import parse_field, parse_message, parse_segment, parse_segments
from hl7apy.validation import VALIDATION_LEVEL


class TestValidation(unittest.TestCase):
    def setUp(self):
        hl7apy.set_default_validation_level(VALIDATION_LEVEL.TOLERANT)
        self.adt_a01 = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20130101101500||ADT^A01^ADT_A01|0123456789|P|2.6||||AL\r'
            'EVN||20130101101500||01|AAA|20130101082500\r'
            'PID|1||123-456-789^^^HOSPITAL^MR||SURNAME^NAME^A|||M|||1111 SOMEWHERE STREET^^SOMEWHERE^^^USA|||||M\r'
            'NK1|1|WOMAN^WIFE|SPO|1111 SOMEWHERE STREET^^SOMEWHERE^^^USA\r'
            'PV1|1|I|PATIENT WARD|U||||^REFERRING^DOCTOR^^MD|^CONSULTING^DOCTOR|CAR||||2|A0|||||||||||||||||||||||||||||2013\r'
            'IN1|1|INSURANCE PLAN ID^PLAN DESC|COMPANY ID|INSURANCE COMPANY, INC.|5555 INSURERS STREET^^SOMEWHERE^^^USA||||||||||||||||||||||||||||||||||||||||||||555-44-3333\r'
        )

        self.zdt_z01 = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20130101101500||ZDT^Z01^ZDT_Z01|0123456789|P|2.5||||AL\r'
            'EVN||20130101101500||AAA|AAA|20130101082500\r'
            'PID|1||123-456-789^^^HOSPITAL^MR||SURNAME^NAME^A|||M|||1111 SOMEWHERE STREET^^SOMEWHERE^^^USA||555-555-2004~444-333-222|||M\r'
            'NK1|1|WOMAN^WIFE|SPO|1111 SOMEWHERE STREET^^SOMEWHERE^^^USA\r'
            'PV1|1|I|PATIENT WARD|U||||^REFERRING^DOCTOR^^MD|^CONSULTING^DOCTOR|CAR||||2|A0|||||||||||||||||||||||||||||2013\r'
            'IN1|1|INSURANCE PLAN ID^PLAN DESC|COMPANY ID|INSURANCE COMPANY, INC.|5555 INSURERS STREET^^SOMEWHERE^^^USA||||||||||||||||||||||||||||||||||||||||||||555-44-3333\r'
        )

        self.oml_o33 = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20110708162817||OML^O33^OML_O33|978226056138290600|D|2.5|||||USA||EN\r'
            'PID|||1010110909194822^^^GATEWAY_IL&1.3.6.1.4.1.21367.2011.2.5.17&ISO^PK||PIPPO^PLUTO^^^^^L||19790515|M|||VIA DI TOPOLINO^CAGLIARI^CAGLIARI^^09100^100^H^^092009^^~^^^^^^L^^^|||||||PPPPPP79E15B354I^^^CF|||||CAGLIARI|||100|||||||||||\r'
            'PV1||O|||||||||||||||||1107080001^^^LIS\r'
            'SPM|1|100187400201^||BLDV|||||||B||||||20110708162817||20110708162817|||||||1|CONTAINER^CONTAINER DESC\r'
            'ORC|NW|83428|83428|18740|SC||||20110708162817||||||||^\r'
            'TQ1|||||||||R\r'
            'OBR||83428|83428|TPO^ANTI THYROPEROXIDASE ANTIBODIES(TPO)^^TPO||||||||||||ND^UNKNOWN^UNKNOWN\r'
        )

        self.oml_o33_2 = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20110708162817||OML^O33^OML_O33|978226056138290600|D|2.5|||||USA||EN\r'
            'PID|||1010110909194822^^^GATEWAY_IL&1.3.6.1.4.1.21367.2011.2.5.17&ISO^PK||PIPPO^PLUTO^^^^^L||19790515|M|||VIA DI TOPOLINO^CAGLIARI^CAGLIARI^^09100^100^H^^092009^^~^^^^^^L^^^|||||||PPPPPP79E15B354I^^^CF|||||CAGLIARI|||100|||||||||||\r'
            'PV1||O|||||||||||||||||1107080001^^^LIS\r'
            'SPM|1|100187400201^||BLDV|||||||PSN^Human Patient||||||20110708162817||20110708162817|||||||1|CONTAINER^CONTAINER DESC\r'
            'ORC|NW|83428|83428|18740|SC||||20110708162817||||||||^\r'
            'TQ1|||||||||R\r'
            'OBR||83428|83428|TPO^ANTI THYROPEROXIDASE ANTIBODIES(TPO)^^TPO||||||||||||ND^UNKNOWN^UNKNOWN\r'
            'SPM|2|100187400101^||BLDV|||||||PSN^Human Patient||||||20110708162817||20110708162817|||||||1|CONTAINER^CONTAINER DESC\r'
            'ORC|NW|83425|83425|18740|SC||||20110708162817||||||||^\r'
            'TQ1|||||||||R\r'
            'OBR||83425|83425|CA^S-CALCIUM^^CA||||||||||||ND^UNKNOWN^UNKNOWN\r'
            'ORC|NW|83426|83426|18740|SC||||20110708162817||||||||^\r'
            'TQ1|||||||||R\r'
            'OBR||83426|83426|HDL^HDL CHOLESTEROL^^HDL||||||||||||ND^UNKNOWN^UNKNOWN\r'
            'ORC|NW|83427|83427|18740|SC||||20110708162817||||||||^\r'
            'TQ1|||||||||R\r'
            'OBR||83427|83427|LDL^LDL CHOLESTEROL^^LDL||||||||||||ND^UNKNOWN^UNKNOWN\r'
        )

        report_file = tempfile.NamedTemporaryFile()
        self.report_file = report_file.name

        self.orm_o01 = (
            'MSH|^~\\&|SND|SND|RCV|RCV|20260101120000||ORM^O01|MSG001|P|2.3\r'
            'PID|1||12345||DOE^JOHN|||19800101|M\r'
            'ORC|NW||||||1^^^20260101120000|20260101120000\r'
            'OBR|1|||SPEC-001|PANEL|||20260101090000|||||||CSF||||||||||||1^^^20260101120000\r'
        )

    def _create_message(self, msg_str):
        return parse_message(msg_str)

    def _test_report_file(self, error_type):

        with open(self.report_file, 'r') as f:
            s = f.read()
        if error_type == 'ERROR':
            regex = r'Error:.*'
        elif error_type == 'WARNING':
            regex = r'Warning:.*'

        self.assertTrue(re.search(regex, s))

        os.remove(self.report_file)

    def test_well_structured_message(self):
        """
        Tests that a valid message is validated
        """
        for msg_str in (self.adt_a01, self.oml_o33):
            msg = self._create_message(msg_str)
            self.assertTrue(msg.validate())

    def test_choice_group_with_single_alternative(self):
        """
        Tests that a choice group with exactly one of its alternatives present is validated.
        Regression test for https://github.com/crs4/hl7apy/issues/151
        The ORM_O01_CHOICE group is a choice group: OBR alone must satisfy it.
        """
        msg = parse_message(self.orm_o01, find_groups=True)
        self.assertTrue(msg.validate())

    def test_choice_group_with_multiple_alternatives(self):
        """
        Tests that a choice group with more than one alternative present is not validated
        """
        msg = parse_message(self.orm_o01, find_groups=True)
        choice = msg.orm_o01_order.orm_o01_order_detail.orm_o01_choice
        rqd = Segment('RQD', version='2.3')
        rqd.rqd_1 = 'X'
        choice.add(rqd)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_choice_group_with_no_alternatives(self):
        """
        Tests that an empty choice group is not validated
        """
        msg = parse_message(self.orm_o01, find_groups=True)
        detail = msg.orm_o01_order.orm_o01_order_detail
        del detail.orm_o01_choice
        empty_choice = Group('ORM_O01_CHOICE', version='2.3')
        detail.add(empty_choice)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_mislabeled_choice_groups_are_sequences(self):
        """
        The 16 groups below were labeled 'choice' in the version tables but the
        HL7 standard defines them as ordered sequences (no vertical bars between
        alternatives in the abstract message syntax, e.g. ``QAK QPD`` rather than
        ``<QAK|QPD>``). They must be labeled 'sequence' so the validator does not
        reject valid messages carrying several of their children.
        Regression test for https://github.com/crs4/hl7apy/pull/152#issuecomment (wshallwshall)
        """
        groups = (
            'EHC_E01_INVOICE_INFORMATION',
            'EHC_E01_INVOICE_INFORMATION_SUBMIT',
            'EHC_E02_INVOICE_INFORMATION',
            'EHC_E02_INVOICE_INFORMATION_CANCEL',
            'EHC_E04_REASSESSMENT_REQUEST_INFO',
            'EHC_E15_PAYMENT_REMITTANCE_HEADER_INFO',
            'EHC_E20_AUTHORIZATION_REQUEST',
            'EHC_E21_AUTHORIZATION_REQUEST',
            'EHC_E24_AUTHORIZATION_RESPONSE_INFO',
            'QBP_E03_QUERY_INFORMATION',
            'QBP_E22_QUERY',
            'RSP_E03_QUERY_ACK',
            'RSP_E03_QUERY_ACK_IPR',
            'RSP_E22_QUERY_ACK',
            'SDR_S31_ANTI_MICROBIAL_DEVICE_DATA',
            'SDR_S32_ANTI_MICROBIAL_DEVICE_CYCLE_DATA',
        )
        checked = 0
        for version in ('2.6', '2.7', '2.8', '2.8.1', '2.8.2'):
            for name in groups:
                try:
                    ref = hl7apy.load_reference(name, 'Group', version)
                except ChildNotFound:
                    continue
                self.assertEqual(ref[0], 'sequence', '{} should be a sequence in v{}'.format(name, version))
                checked += 1
        self.assertGreater(checked, 0)

    def test_rsp_e22_query_ack_validates_as_sequence(self):
        """
        A valid RSP^E22 carrying QAK then QPD must validate: RSP_E22_QUERY_ACK
        is an ordered sequence, not a choice group.
        """
        msg_str = (
            'MSH|^~\\&|SND|SND|RCV|RCV|20260101120000||RSP^E22^RSP_E22|MSG001|P|2.6\r'
            'MSA|AA|123\r'
            'QAK|QRY001|OK\r'
            'QPD|QRY001|QueryName\r'
        )
        msg = parse_message(msg_str, find_groups=True)
        self.assertTrue(msg.validate())

    def test_rsp_e22_query_ack_enforces_sequence_cardinality(self):
        """
        Sequence semantics still apply: QPD without the required QAK must fail.
        """
        msg_str = (
            'MSH|^~\\&|SND|SND|RCV|RCV|20260101120000||RSP^E22^RSP_E22|MSG001|P|2.6\rMSA|AA|123\rQPD|QRY001|QueryName\r'
        )
        msg = parse_message(msg_str, find_groups=True)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_choice_group_with_repeated_alternative(self):
        """
        Tests that a choice group whose chosen alternative exceeds its maximum
        repetitions is not validated
        """
        msg = parse_message(self.orm_o01, find_groups=True)
        choice = msg.orm_o01_order.orm_o01_order_detail.orm_o01_choice
        obr = Segment('OBR', version='2.3')
        obr.obr_1 = '2'
        choice.add(obr)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    # def test_oml_o33_2_message(self):
    #     # This test is failing because the oml_o33_2 parsing creates a
    #     # OML_O33_ORDER_PRIOR which expect a OBR segment, but the parser's
    #     # implementation puts the OBR segment in the group OML_O33_OBSERVATION_REQUEST
    #     # This behaviour HL7 fault
    #     msg = self._create_message(self.oml_o33_2)
    #     self.assertTrue(msg.validate())

    def test_unknown_message(self):
        msg = self._create_message(self.adt_a01)
        msg.name = None
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_group(self):
        """
        Test that if a required group is not present the messase in not validated
        The message misses the oml_o33_specimen group
        """
        msg = self._create_message(self.oml_o33)
        del msg.oml_o33_specimen
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_segment(self):
        """
        Tests that when a required segment is missing the message is not validated
        The message used misses the EVN segment
        """
        msg = self._create_message(self.adt_a01)
        del msg.evn
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_field(self):
        """
        Tests that when a required field is missing the message is not validated
        The message used misses the msh_9 segment
        """
        msg = self._create_message(self.adt_a01)
        del msg.msh.msh_9
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_component(self):
        """
        Tests that when a required component is missing the message is not validated
        The message used misses the msg_1 component in the msh_9 field
        """
        msg = self._create_message(self.adt_a01)
        del msg.msh.msh_9.msg_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_subcomponent(self):
        """
        Tests that when a required subcomponent is missing the message is not validated
        The message used misses the cwe_1 subcomponent in the cx_10 component
        """
        msg = self._create_message(self.oml_o33)
        msg.pid.pid_3.cx_10 = 'xxx&yyy&zzz&&&&&&&'
        del msg.pid.pid_3.cx_10.cwe_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_having_more_groups(self):
        """
        Tests that when a group occurs more than the allowed times the message is not validated
        The message used has 2 occurrence of oml_o33_group
        """
        msg = self._create_message(self.oml_o33)
        oml_o33_patient = Group('OML_O33_PATIENT')
        segments = parse_segments(
            'PID|||1010110909194822^^^GATEWAY_IL&1.3.6.1.4.1.21367.2011.2.5.17&ISO^PK||PIPPO^PLUTO^^^^^L||19790515|M|||VIA DI TOPOLINO^CAGLIARI^CAGLIARI^^09100^100^H^^092009~^^^^^^L|||||||PPPPPP79E15B354I^^^CF|||||CAGLIARI|||100\rPV1||O|||||||||||||||||1107080001^^^LIS'
        )
        oml_o33_patient.children = segments
        msg.add(oml_o33_patient)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_having_more_segments(self):
        """
        Tests that when a segment occurs more than the allowed times the message is not validated
        The message used has 2 occurrence of the segment evn
        """
        msg = self._create_message(self.adt_a01)
        evn = parse_segment('EVN||20080115153000||AAA|AAA|20080114003000', version='2.6')
        msg.add(evn)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_having_more_fields(self):
        """
        Tests that when a field occurs more than the allowed times the message is not validated
        The message used has 2 occurrence of msh_9
        """
        msg = self._create_message(self.adt_a01)
        msh_9 = parse_field('RSP^SLI^RSP_K11', 'MSH_9', version="2.6")
        msg.msh.add(msh_9)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_datatype_field(self):
        """
        Tests that if a field is not of the correct datatype the message is not validate
        The message used has the MSH_9 of type ST
        """
        msg = self._create_message(self.adt_a01)
        msh_9 = Field('MSH_9', datatype='ST', version='2.6')
        msh_9.msh_9_1 = 'ADT_A01'
        msg.msh.msh_9 = msh_9
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_datatype_component(self):
        """
        Tests that if a component is not of the correct datatype the message is not validate
        The message used has the MSG_1 of type ST
        """
        msg = self._create_message(self.adt_a01)
        msg_1 = Component('MSG_1', datatype='ST', version='2.6')
        msg_1.add(SubComponent(datatype='ST', value='ADT_A01', version='2.6'))
        msg.msh.msh_9.msg_1 = msg_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_datatype_subcomponent(self):
        """
        Tests that if a component is not of the correct datatype the message is not validated
        The message used has the CWE_1 of type NM
        """
        msg = self._create_message(self.adt_a01)
        cwe_1 = SubComponent('CWE_1', datatype='NM', value='1', version='2.6')
        msg.pid.pid_3.cx_10.cwe_1 = cwe_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_segment(self):
        """
        Tests that if there is an unexpected segment the message in not validated
        The message used has an unexpected SPM
        """
        msg = self._create_message(self.adt_a01)
        spm = parse_segment(
            'SPM|1|100187400201^||SPECIMEN^Blood|||||||PSN^Human Patient||||||20110708162817||'
            '20110708162817|||||||1|CONTAINER^CONTAINER DESC\r',
            version='2.6',
        )
        msg.add(spm)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_group(self):
        """
        Tests that if there is an unexpected segment the message in not validated
        The message used has an unexpected OML_O33_PATIENT
        """
        msg = self._create_message(self.adt_a01)
        oml_o33_patient = Group('OML_O33_PATIENT', version='2.6')
        segments = parse_segments(
            'PID|||1010110909194822^^^GATEWAY_IL&1.3.6.1.4.1.21367.2011.2.5.17&ISO^PK||'
            'PIPPO^PLUTO^^^^^L||19790515|M|||VIA DI TOPOLINO^CAGLIARI^CAGLIARI^^09100^100^'
            'H^^092009~^^^^^^L|||||||PPPPPP79E15B354I^^^CF|||||CAGLIARI|||100\rPV1||O|||||||||'
            '||||||||1107080001^^^LIS',
            version='2.6',
        )
        oml_o33_patient.children = segments
        msg.add(oml_o33_patient)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_field(self):
        """
        Tests that if there is an unexpected field the message in not validated
        The message used has an unexpected unknown field in the pid
        """
        msg = self._create_message(self.adt_a01)
        unkn_field = Field(version='2.6')
        msg.pid.add(unkn_field)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_component(self):
        """
        Tests that if there is an unexpected component the message in not validated
        The message used has an unexpected unknown component in the msh_9
        """
        msg = self._create_message(self.adt_a01)
        unkn_component = Component(version='2.6')
        msg.msh.msh_9.add(unkn_component)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_subcomponent(self):
        """
        Tests that if there is an unexpected subcomponent the message in not validated
        The message used has an unexpected unknown subcomponent in the cx_10
        """
        msg = self._create_message(self.adt_a01)
        unkn_subcomponent = SubComponent(datatype='ST', version='2.6')
        msg.pid.pid_3.cx_10 = Component('CX_10', version='2.6')
        msg.pid.pid_3.cx_10.add(unkn_subcomponent)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_value_table(self):
        """
        Tests that if a value doesn't belong to the table specified in HL7 structures, it writes a warning in
        the report file
        """
        msg = self._create_message(self.adt_a01)
        msg.msh.msh_15 = 'AB'  # AB is not one of the value in the msh_15 table (HL70155)
        self.assertTrue(msg.validate(report_file=self.report_file))
        self._test_report_file('WARNING')

    # def test_too_long_value(self):
    # TODO: reinsert this test after adding the length control of base datatype
    #     """
    #     Tests that if a value exceeds the maximum length, it writes a warning in the report file
    #     """
    #     msg = self._create_message(self.adt_a01)
    #     msg.pid.pid_1 = '12345'  # PID 1 is an SI field, which has maximum length of 4
    #     self.assertTrue(msg.validate(report_file=self.report_file))
    #     self._test_report_file('WARNING')

    def test_z_message(self):
        """
        Tests that a Z Message is validated, no matter what segments it has
        """
        msg = self._create_message(self.zdt_z01)
        self.assertTrue(msg.validate())

    def test_z_segment(self):
        """
        Tests that, after adding a Z segment to a valid message, the message is still valid
        """
        msg = self._create_message(self.adt_a01)
        msg.add_segment('zin')
        msg.zin = 'ZIN|aa|bb|cc|'
        zin_4 = Field('ZIN_4', datatype='CWE', version='2.6')
        zin_4.value = 'dd'
        msg.zin.zin_4 = zin_4
        self.assertTrue(msg.validate())

    def test_wrong_z_segment(self):
        """
        Tests that, if the Z segment doesn't follow HL7 rules, the message is not validated
        """
        msg = self._create_message(self.adt_a01)
        msg.add_segment('zin')
        msg.zin = 'ZIN|aa|bb|cc|'
        # CX_1 is mandatory
        zin_4 = Field('ZIN_4', datatype='CX', version='2.6')
        zin_4.value = '^12'
        msg.zin.add(zin_4)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)

        del msg.zin.zin_4

        # Z segment with unknown fields are not validated
        msg.zin.add(Field(version="2.6"))
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_z_field(self):
        f = Field('zin_1')
        self.assertTrue(f.validate())

    def test_wrong_z_field(self):
        """
        Tests that Fields of None datatype are not validated
        """
        f = Field('zin_1')
        f.value = 'aa^bb'
        self.assertRaises(ValidationError, f.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wd_type_field(self):
        """
        Tests that, in strict mode, a wd field is not present
        """
        # The EV1 message is of type WD
        s = 'EVN|EV1|20080115153000||AAA|AAA|20080114003000'
        parsed_s = parse_segment(s, version='2.7')
        self.assertRaises(ValidationError, parsed_s.validate)

    def test_validate_with_param_report_file_as_string_io(self):
        """
        Tests that, if you use stringIO to validate, warning and errors are written there
        """
        string_io = StringIO()
        msg = self._create_message(self.oml_o33)
        self.assertTrue(msg.validate(report_file=string_io))
        self.assertGreater(string_io.tell(), 0)

    def test_validate_with_param_return_errors(self):
        """
        Tests message is valid with warnings using return_errors=True
        """
        msg = self._create_message(self.oml_o33)
        errors_and_warnings = msg.validate(return_errors=True)
        self.assertTrue(errors_and_warnings.is_valid)
        self.assertEqual(len(errors_and_warnings.warnings), 2)

    def test_well_structured_message_using_return_error(self):
        """
        Tests that a valid message is validated using return_error
        """
        msg = self._create_message(self.adt_a01)
        errors_and_warnings = msg.validate(return_errors=True)
        self.assertTrue(errors_and_warnings.is_valid)
        self.assertEqual(len(errors_and_warnings.errors), 0)
        self.assertEqual(len(errors_and_warnings.warnings), 0)


class TestMessageProfile(unittest.TestCase):
    def setUp(self):
        self.rsp_k21 = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|RECEIVING APP|RECEIVING FAC|20140410170011||RSP^K22^RSP_K21|11111111|P|2.5\r'
            'MSA|AA|20140410170015\r'
            'QAK|222222222|OK\r'
            'QPD|IHE PDQ Query|222222222|@PID.3.1.1^3333333|||||^^^IHEFACILITY&1.3.6.1.4.1.21367.3000.1.6&ISO^|\r'
            'PID|1||10101109091948^^^GATEWAY&1.3.6.1.4.1.21367.2011.2.5.17&ISO||JOHN^SMITH^^^^^A||19690113|M|||VIA DELLE VIE^^CAGLIARI^^^ITA^H^^092009||||||||||||CAGLIARI|||\r'
        )

        base_path = os.path.abspath(os.path.dirname(__file__))
        path = os.path.join(base_path, 'profiles/iti_21')
        self.rsp_k21_mp = hl7apy.load_message_profile(path)

        report_file = tempfile.NamedTemporaryFile()
        self.report_file = report_file.name

    def _create_message(self, msg_str):
        return parse_message(msg_str, message_profile=self.rsp_k21_mp)

    def _test_report_file(self, error_type, present=True):

        with open(self.report_file, 'r') as f:
            s = f.read()
        if error_type == 'ERROR':
            regex = r'Error:.*'
        elif error_type == 'WARNING':
            regex = r'Warning:.*'
        else:
            return

        if present:
            self.assertTrue(re.search(regex, s))
        else:
            self.assertFalse(re.search(regex, s))

        os.remove(self.report_file)

    def test_well_structured_message(self):
        """
        Tests that a valid message is validated
        """
        m = self._create_message(self.rsp_k21)
        self.assertTrue(m.validate())

    # def test_oml_o33_2_message(self):
    #     # This test is failing because the oml_o33_2 parsing creates a
    #     # OML_O33_ORDER_PRIOR which expect a OBR segment, but the parser's
    #     # implementation puts the OBR segment in the group OML_O33_OBSERVATION_REQUEST
    #     # This behaviour HL7 fault
    #     msg = self._create_message(self.oml_o33_2)
    #     self.assertTrue(msg.validate())

    # def test_missing_required_group(self):
    #     """
    #     Test that if a required group is not present the messase in not validated
    #     The message misses the oml_o33_specimen group
    #     """
    #     msg = self._create_message(self.rsp_k21)
    #     del msg.oml_o33_specimen
    #     self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
    #     self._test_report_file('ERROR')

    def test_missing_required_segment(self):
        """
        Tests that when a required segment is missing the message is not validated
        The message used misses the EVN segment
        """
        msg = self._create_message(self.rsp_k21)
        del msg.qak
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_field(self):
        """
        Tests that when a required field is missing the message is not validated
        The message used misses the msh_9 segment
        """
        msg = self._create_message(self.rsp_k21)
        del msg.qak.qak_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_component(self):
        """
        Tests that when a required component is missing the message is not validated
        The message used misses the msg_1 component in the msh_9 field
        """
        msg = self._create_message(self.rsp_k21)
        del msg.qpd.qpd_8.cx_4
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_missing_required_subcomponent(self):
        """
        Tests that when a required subcomponent is missing the message is not validated
        The message used misses the cwe_1 subcomponent in the cx_10 component
        """
        msg = self._create_message(self.rsp_k21)
        msg.pid.pid_2.cx_4 = '&yyy&zzz'  # it misses the required cwe_1
        # del msg.pid.pid_3.cx_10.cwe_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    # def test_having_more_groups(self):
    #     """
    #     Tests that when a group occurs more than the allowed times the message is not validated
    #     The message used has 2 occurrence of oml_o33_group
    #     """
    #     msg = self._create_message(self.oml_o33)
    #     oml_o33_patient = Group('OML_O33_PATIENT')
    #     segments = parse_segments('PID|||1010110909194822^^^GATEWAY_IL&1.3.6.1.4.1.21367.2011.2.5.17&ISO^PK||PIPPO^PLUTO^^^^^L||19790515|M|||VIA DI TOPOLINO^CAGLIARI^CAGLIARI^^09100^100^H^^092009~^^^^^^L|||||||PPPPPP79E15B354I^^^CF|||||CAGLIARI|||100\rPV1||O|||||||||||||||||1107080001^^^LIS')
    #     oml_o33_patient.children = segments
    #     msg.add(oml_o33_patient)
    #     self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
    #     self._test_report_file('ERROR')

    def test_having_more_segments(self):
        """
        Tests that when a segment occurs more than the allowed times the message is not validated
        The message used has 2 occurrence of the segment evn
        """
        msg = self._create_message(self.rsp_k21)
        msg.add_segment('qak')
        msg.qak[1] = 'QAK|222222222|OK'
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_having_more_fields(self):
        """
        Tests that when a field occurs more than the allowed times the message is not validated
        The message used has 2 occurrence of msh_9
        """
        msg = self._create_message(self.rsp_k21)
        msg.msh.add_field('msh_9')
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_datatype_field(self):
        """
        Tests that if a field is not of the correct datatype the message is not validate
        The message used has the MSH_9 of type ST
        """
        msg = self._create_message(self.rsp_k21)
        msh_9 = Field('MSH_9', datatype='ST')
        msh_9.msh_9_1 = 'RSP_K21'
        msg.msh.msh_9 = msh_9
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_datatype_component(self):
        """
        Tests that if a component is not of the correct datatype the message is not validate
        The message used has the MSG_1 of type ST
        """
        msg = self._create_message(self.rsp_k21)
        msg_1 = Component('MSG_1', datatype='ST')
        msg_1.add(SubComponent(datatype='ST', value='ADT_A01'))
        msg.msh.msh_9.msg_1 = msg_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_datatype_subcomponent(self):
        """
        Tests that if a component is not of the correct datatype the message is not validated
        The message used has the CWE_1 of type NM
        """
        msg = self._create_message(self.rsp_k21)
        cwe_1 = SubComponent('CWE_1', datatype='NM', value='1')
        msg.pid.pid_3.cx_10.cwe_1 = cwe_1
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_segment(self):
        """
        Tests that if there is an unexpected segment the message in not validated
        The message used has an unexpected SPM
        """
        msg = self._create_message(self.rsp_k21)
        spm = parse_segment(
            'SPM|1|100187400201^||SPECIMEN^Blood|||||||PSN^Human Patient||||||20110708162817||20110708162817|||||||1|CONTAINER^CONTAINER DESC\r'
        )
        msg.add(spm)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_group(self):
        """
        Tests that if there is an unexpected segment the message in not validated
        The message used has an unexpected OML_O33_PATIENT
        """
        msg = self._create_message(self.rsp_k21)
        oml_o33_patient = Group('OML_O33_PATIENT')
        segments = parse_segments(
            'PID|||1010110909194822^^^GATEWAY_IL&1.3.6.1.4.1.21367.2011.2.5.17&ISO^PK||PIPPO^PLUTO^^^^^L||19790515|M|||VIA DI TOPOLINO^CAGLIARI^CAGLIARI^^09100^100^H^^092009~^^^^^^L|||||||PPPPPP79E15B354I^^^CF|||||CAGLIARI|||100\rPV1||O|||||||||||||||||1107080001^^^LIS'
        )
        oml_o33_patient.children = segments
        msg.add(oml_o33_patient)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_field(self):
        """
        Tests that if there is an unexpected field the message in not validated
        The message used has an unexpected unknown field in the pid
        """
        msg = self._create_message(self.rsp_k21)
        unkn_field = Field()
        msg.rsp_k21_query_response.pid.add(unkn_field)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_component(self):
        """
        Tests that if there is an unexpected component the message in not validated
        The message used has an unexpected unknown component in the msh_9
        """
        msg = self._create_message(self.rsp_k21)
        unkn_component = Component()
        msg.msh.msh_9.add(unkn_component)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_subcomponent(self):
        """
        Tests that if there is an unexpected subcomponent the message in not validated
        The message used has an unexpected unknown subcomponent in the cx_10
        """
        msg = self._create_message(self.rsp_k21)
        unkn_subcomponent = SubComponent(datatype='ST')
        msg.rsp_k21_query_response.pid.pid_3.cx_10 = Component('CX_10')
        msg.rsp_k21_query_response.pid.pid_3.cx_10.add(unkn_subcomponent)
        self.assertRaises(ValidationError, msg.validate, report_file=self.report_file)
        self._test_report_file('ERROR')

    def test_wrong_value_table(self):
        """
        Tests that if a value doesn't belong to the table specified in HL7 structures, it writes a warning in
        the report file
        """
        msg = self._create_message(self.rsp_k21)
        msg.msh.msh_15 = 'AB'  # AB is not one of the value in the msh_15 table (HL70155)
        self.assertTrue(msg.validate(report_file=self.report_file))
        self._test_report_file('WARNING')

    def test_too_long_value(self):
        """
        Tests that if a value exceeds the maximum length, it writes a warning in the report file
        """
        msg = self._create_message(self.rsp_k21)
        msg.rsp_k21_query_response.pid.pid_1 = '12345'  # PID 1 is an SI field, which has maximum length of 4
        self.assertTrue(msg.validate(report_file=self.report_file))
        self._test_report_file('WARNING')

    def test_z_segment(self):
        """
        Tests that, after adding a Z segment to a valid message, the message is still valid
        """
        msg = self._create_message(self.rsp_k21)
        msg.add_segment('zin')
        msg.zin = 'ZIN|aa|bb|cc|'
        zin_4 = Field('ZIN_4', datatype='CWE')
        zin_4.value = 'dd'
        msg.zin.zin_4 = zin_4
        self.assertTrue(msg.validate())


class TestLocatableValidation(unittest.TestCase):
    """
    Validation issues must be locatable: category, group path, occurrence,
    constraint and the original ER7 fragment. A missing parent group must be
    reported once instead of turning all of its absent children into separate
    source problems.
    """

    def setUp(self):
        hl7apy.set_default_validation_level(VALIDATION_LEVEL.TOLERANT)
        self.oml_o33 = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20110708162817||OML^O33^OML_O33|'
            '978226056138290600|D|2.5|||||USA||EN\r'
            'PID|||1010110909194822^^^GATEWAY^PK||PIPPO^PLUTO||19790515|M\r'
            'SPM|1|100187400201^||BLDV\r'
            'ORC|NW|83428|83428\r'
            'TQ1|||||||||R\r'
            'OBR|1|83428|83428|TPO^ANTI THYROPEROXIDASE ANTIBODIES^^TPO\r'
        )

    def test_structure_error_has_group_path_occurrence_and_fragment(self):
        msg = parse_message(self.oml_o33)
        del msg.oml_o33_specimen[0].oml_o33_order[0].orc
        result = msg.validate(return_errors=True)
        self.assertFalse(result.is_valid)
        self.assertEqual(len(result.errors), 1)
        issue = result.errors[0]

        self.assertEqual(issue.category, 'structure')
        self.assertEqual(issue.code, 'MISSING_REQUIRED_CHILD')
        self.assertEqual(issue.occurrence, 1)
        self.assertEqual(issue.constraint, '1..1')
        self.assertEqual(
            issue.group_path,
            'OML_O33/OML_O33_SPECIMEN[1]/OML_O33_ORDER[1]',
        )
        self.assertTrue(issue.path.endswith('/ORC[1]'))
        self.assertEqual(
            issue.fragment,
            'TQ1|||||||||R\rOBR|1|83428|83428|TPO^ANTI THYROPEROXIDASE ANTIBODIES^^TPO',
        )

    def test_value_error_is_separate_from_structure_errors(self):
        msg = parse_message(self.oml_o33)
        msg.msh.msh_15 = 'AB'
        result = msg.validate(return_errors=True)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.structure_errors, [])
        self.assertTrue(all(issue.category == 'value' for issue in result.value_errors))
        table_issue = [issue for issue in result.value_errors if issue.constraint == 'table HL70155'][0]
        self.assertEqual(table_issue.category, 'value')
        self.assertEqual(table_issue.path, 'OML_O33/MSH[1]/MSH_15[1]')
        self.assertEqual(table_issue.fragment, 'AB')

    def test_missing_parent_group_is_not_reported_as_many_child_errors(self):
        msg = parse_message(self.oml_o33)
        del msg.oml_o33_specimen
        result = msg.validate(return_errors=True)

        self.assertEqual(len(result.errors), 1)
        issue = result.errors[0]
        self.assertEqual(issue.code, 'MISSING_REQUIRED_CHILD')
        self.assertEqual(issue.element, 'OML_O33_SPECIMEN')
        self.assertEqual(issue.path, 'OML_O33/OML_O33_SPECIMEN[1]')
        self.assertEqual(issue.group_path, 'OML_O33')
        self.assertEqual(issue.constraint, '1..-1')
        self.assertIsNone(issue.fragment)

    def test_empty_required_group_reports_only_the_group(self):
        msg = parse_message(self.oml_o33)
        msg.oml_o33_specimen[0].children = []
        result = msg.validate(return_errors=True)

        self.assertEqual(len(result.errors), 1)
        issue = result.errors[0]
        self.assertEqual(issue.code, 'EMPTY_REQUIRED_GROUP')
        self.assertEqual(issue.group_path, 'OML_O33/OML_O33_SPECIMEN[1]')
        self.assertFalse(issue.is_cascade)

    def test_second_occurrence_is_identified(self):
        msg = parse_message(self.oml_o33)
        specimen = Group('OML_O33_SPECIMEN', version='2.5')
        specimen.children = parse_segments('SPM|2|100187400202^||BLDV', version='2.5', find_groups=False)
        empty_order = Group('OML_O33_ORDER', version='2.5')
        specimen.add(empty_order)
        msg.add(specimen)

        result = msg.validate(return_errors=True)
        self.assertEqual(len(result.errors), 1)
        issue = result.errors[0]
        self.assertEqual(issue.code, 'EMPTY_REQUIRED_GROUP')
        self.assertEqual(issue.occurrence, 1)
        self.assertEqual(
            issue.group_path,
            'OML_O33/OML_O33_SPECIMEN[2]/OML_O33_ORDER[1]',
        )


class TestBatchValidation(unittest.TestCase):
    """
    A batch keeps delivering the valid messages while reporting every invalid
    one with locatable structural and value problems.
    """

    def setUp(self):
        hl7apy.set_default_validation_level(VALIDATION_LEVEL.TOLERANT)
        self.good = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20110708162817||OML^O33^OML_O33|'
            'GOOD-1|D|2.5\r'
            'PID|||101^^^GATEWAY^MR||PIPPO^PLUTO||19790515|M\r'
            'SPM|1|100187400201^||BLDV\r'
            'ORC|NW|83428|83428\r'
            'TQ1|||||||||R\r'
            'OBR|1|83428|83428|TPO^ANTI THYROPEROXIDASE ANTIBODIES^^TPO\r'
        )
        self.invalid = (
            'MSH|^~\\&|SENDING APP|SENDING FAC|REC APP|REC FAC|20110708162817||ADT^A01^ADT_A01|'
            'BAD-1|P|2.6\r'
            'EVN||20130101101500||01|AAA|20130101082500\r'
        )

    def test_valid_messages_are_still_delivered(self):
        from hl7apy.validation import validate_batch

        result = validate_batch([('good', self.good), ('bad', self.invalid)])
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0].is_valid)
        self.assertFalse(result[1].is_valid)
        self.assertEqual([item.message_id for item in result.valid_items], ['good'])
        self.assertEqual(result.summary()['valid'], 1)
        self.assertEqual(result.summary()['invalid'], 1)

    def test_structure_and_value_errors_are_listed_separately(self):
        from hl7apy.validation import validate_batch

        invalid_value = self.good.replace('|EN\r', '|EN\r') .replace('||19790515|M', '||19790515|Z')
        result = validate_batch([('values', invalid_value)])
        item = result[0]
        self.assertTrue(all(issue.category == 'value' for issue in item.value_errors))
        self.assertTrue(all(issue.category == 'structure' for issue in item.structure_errors))
        self.assertTrue(item.value_errors)
        self.assertEqual(item.structure_errors, [])

    def test_batch_report_contains_group_path_occurrence_constraint_and_fragment(self):
        from hl7apy.validation import validate_batch

        # Drop the required ORC while keeping its sibling segments: the report
        # must locate the missing child inside the second-level order group.
        broken = self.good.replace('ORC|NW|83428|83428\r', '')
        result = validate_batch([{'id': 'broken', 'message': broken}])
        text = result.report_text()

        self.assertIn('group_path=OML_O33/OML_O33_SPECIMEN[1]/OML_O33_ORDER[1]', text)
        self.assertIn('occurrence=1', text)
        self.assertIn('constraint=1..1', text)
        self.assertIn("fragment='TQ1|||||||||R\\rOBR|1|83428|83428|"
                      "TPO^ANTI THYROPEROXIDASE ANTIBODIES^^TPO'", text)

    def test_batch_report_can_be_written_as_json(self):
        import json

        from hl7apy.validation import validate_batch

        with tempfile.NamedTemporaryFile('r', suffix='.json', delete=False) as report:
            report_path = report.name
        try:
            result = validate_batch([('good', self.good), ('bad', self.invalid)], report_file=report_path)
            with open(report_path) as report:
                payload = json.load(report)
            self.assertEqual(payload['summary']['total'], 2)
            self.assertEqual(payload['items'][0]['message_id'], 'good')
            bad = payload['items'][1]
            self.assertEqual(bad['message_id'], 'bad')
            self.assertTrue(bad['structure_errors'])
        finally:
            os.remove(report_path)

    def test_invalid_message_does_not_stop_the_batch(self):
        from hl7apy.validation import validate_batch

        result = validate_batch([('bad', self.invalid), ('good', self.good)])
        self.assertEqual(len(result), 2)
        self.assertEqual([item.message_id for item in result.valid_items], ['good'])

    def test_valid_messages_are_returned_for_delivery(self):
        from hl7apy.validation import validate_batch

        result = validate_batch([('one', self.good), ('two', self.good)])
        self.assertTrue(result.is_valid)
        self.assertEqual(len(result.valid_messages), 2)
        self.assertEqual([message.name for message in result.valid_messages], ['OML_O33', 'OML_O33'])
        self.assertEqual(result.structure_errors, [])
        self.assertEqual(result.value_errors, [])

    def test_batch_accepts_a_single_message_and_a_file(self):
        from hl7apy.validation import validate_batch

        single = validate_batch(self.good)
        self.assertEqual(len(single), 1)
        self.assertTrue(single[0].is_valid)

        with tempfile.NamedTemporaryFile('w', suffix='.hl7', delete=False, newline='') as source:
            source.write(self.good)
            path = source.name
        try:
            from_file = validate_batch([('file', path)])
            self.assertTrue(from_file[0].is_valid)
            self.assertEqual(from_file[0].message_id, 'file')
        finally:
            os.remove(path)

    def test_parse_error_is_reported_and_rest_of_batch_continues(self):
        from hl7apy.validation import validate_batch

        result = validate_batch([('broken', 'NOTHL7'), ('good', self.good)])
        self.assertFalse(result[0].is_valid)
        self.assertEqual(result[0].parse_error, 'Invalid message')
        self.assertEqual(result[0].errors[0].code, 'PARSE_ERROR')
        self.assertTrue(result[1].is_valid)

    def test_batch_accepts_a_directory_and_a_mapping(self):
        from hl7apy.validation import validate_batch

        with tempfile.TemporaryDirectory() as directory:
            for name, payload in (('a.hl7', self.good), ('b.hl7', self.invalid)):
                with open(os.path.join(directory, name), 'w', newline='') as target:
                    target.write(payload)
            result = validate_batch(directory)
            self.assertEqual(len(result), 2)
            self.assertEqual([item.message_id for item in result], ['a.hl7', 'b.hl7'])
            self.assertFalse(result[1].is_valid)

        by_id = validate_batch({'GOOD': self.good, 'BAD': self.invalid})
        self.assertEqual([item.message_id for item in by_id], ['GOOD', 'BAD'])

    def test_text_report_is_written(self):
        from hl7apy.validation import validate_batch

        with tempfile.NamedTemporaryFile('r', suffix='.txt', delete=False) as report:
            report_path = report.name
        try:
            validate_batch([('bad', self.invalid)], report_file=report_path)
            with open(report_path) as report:
                text = report.read()
            self.assertIn('Message bad (ADT_A01): INVALID', text)
            self.assertIn('Structure errors:', text)
        finally:
            os.remove(report_path)


if __name__ == '__main__':
    unittest.main()
