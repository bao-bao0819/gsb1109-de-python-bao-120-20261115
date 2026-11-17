from os import path
import os
import shutil
import tempfile
import unittest
import warnings
import pyteomics
pyteomics.__path__ = [path.abspath(path.join(path.dirname(__file__), path.pardir, 'pyteomics'))]
from pyteomics import mztab


class MzTabTest(unittest.TestCase):

    path_mztab1 = 'test.mztab'
    path_mztab2 = 'test_mztab2.mztab'

    def test_metadata_mztab1(self):
        reader_mztab1 = mztab.MzTab(self.path_mztab1)
        self.assertEqual(len(reader_mztab1.metadata), 208)
        value_from_mztab1 = reader_mztab1.metadata['fixed_mod[1]']
        self.assertEqual(value_from_mztab1, 'CHEMMOD:57.0214637236')
        reader_mztab1.file.close()

    def test_metadata_mztab2(self):
        reader_mztab2 = mztab.MzTab(self.path_mztab2)
        self.assertEqual(len(reader_mztab2.metadata), 61)
        value_from_mztab2 = reader_mztab2.metadata['sample_processing[1]']
        self.assertEqual(value_from_mztab2, 'high performance liquid chromatography')
        reader_mztab2.file.close()

    def test_metadata_variant_P(self):
        reader_mztab1 = mztab.MzTab(self.path_mztab1)
        self.assertEqual(reader_mztab1.variant, 'P')
        reader_mztab1.file.close()

    def test_metadata_variant_M(self):
        reader_mztab2 = mztab.MzTab(self.path_mztab2)
        self.assertEqual(reader_mztab2.variant, 'M')
        reader_mztab2.file.close()

    def test_iter_mztab1(self):
        reader_mztab1 = mztab.MzTab(self.path_mztab1)
        tables = list(reader_mztab1)
        self.assertEqual(len(tables), 4)
        [self.assertEqual(len(t), 2) for t in tables]
        reader_mztab1.file.close()

    def test_iter_mztab2(self):
        reader_mztab2 = mztab.MzTab(self.path_mztab2)
        tables = list(reader_mztab2)
        self.assertEqual(len(tables), 3)
        [self.assertEqual(len(t), 2) for t in tables]
        reader_mztab2.file.close()

    def test_getitem_mztab1(self):
        reader_mztab1 = mztab.MzTab(self.path_mztab1)
        table = reader_mztab1['psm']
        self.assertIsInstance(table, mztab.pd.DataFrame)
        reader_mztab1.file.close()

    def test_getitem_mztab2(self):
        reader_mztab2 = mztab.MzTab(self.path_mztab2)
        table = reader_mztab2['sme']
        self.assertIsInstance(table, mztab.pd.DataFrame)
        reader_mztab2.file.close()

    def test_keys_values_items(self):
        reader_mztab2 = mztab.MzTab(self.path_mztab2, table_format='dict')
        keys = list(reader_mztab2.keys())
        self.assertEqual(keys, [k for k, v in reader_mztab2])
        values = list(reader_mztab2.values())
        self.assertEqual(values, [v for k, v in reader_mztab2])
        items = list(reader_mztab2.items())
        self.assertEqual(items, list(reader_mztab2))
        reader_mztab2.file.close()

    def test_generated_accessors(self):
        reader = mztab.MzTab(self.path_mztab1)
        self.assertEqual(reader.mode, 'Complete')
        self.assertEqual(reader.version, '1.0.0')
        self.assertEqual(reader.software, {1: ('MaxQuant', '1.6.3.4')})
        ms_runs = reader.ms_runs
        self.assertEqual(len(ms_runs), 63)
        self.assertEqual(
            sorted(ms_runs[1].items()),
            [
                ('format', 'Andromeda:apl file format'),
                ('id_format', 'scan number only nativeID format'),
                ('location', 'file://c:/users/jklein/projects/msv000080527_abelin2017/combined/andromeda/allspectra.hcd.ftms.secpep.sil0_0.apl'),
             ])
        reader.file.close()

    def test_missing_version(self):
        class OverridingMzTab(mztab.MzTab):
            def _parse(self):
                super(OverridingMzTab, self)._parse()
                self.metadata.pop("mzTab-version", None)
        with warnings.catch_warnings(record=True) as w:
            reader = OverridingMzTab(self.path_mztab1)
            assert reader.variant == 'P'
            assert reader.version == '1.0.0'
            reader.file.close()
        assert len(w) > 0

    def test_override(self):
        class OverridingMzTab(mztab.MzTab):
            def mode(self):
                return super(OverridingMzTab, self).mode
        reader = OverridingMzTab(self.path_mztab1)
        self.assertEqual(reader.mode(), 'Complete')
        reader.file.close()


_MINIMAL_MZTAB_HEADER = """MTD\tmzTab-version\t1.0.0
MTD\tmzTab-mode\tComplete
MTD\tmzTab-type\tIdentification
MTD\tms_run[1]-location\tfile:///data/run1.mzML
MTD\tms_run[2]-location\tfile:///data/run2.mzML
PRH\taccession\tdescription
PEH\tsequence\taccession\tunique\tmodifications\tspectra_ref\tcharge
PSH\tsequence\tPSM_ID\taccession\tunique\tmodifications\tspectra_ref\tcharge
"""


def _write_mztab(directory, name, rows, header=_MINIMAL_MZTAB_HEADER):
    file_name = path.join(directory, name)
    with open(file_name, 'w') as handle:
        handle.write(header)
        for row in rows:
            handle.write('\t'.join(str(cell) for cell in row) + '\n')
    return file_name


class MzTabProteinSupportTest(unittest.TestCase):

    rows = [
        ('PRT', 'P1', 'unique protein'),
        ('PRT', 'P2', 'shared protein'),
        ('PRT', 'P3', 'other shared protein'),
        ('PRT', 'P4', 'no evidence'),
        ('PSM', 'PEPTIDEK', 1, 'P1', 1, '0-CHEMMOD:42.0105646863',
         'ms_run[1]:index=1', 2),
        ('PSM', 'SHAREDPEPTIDEK', 2, 'P2', 0, 'null',
         'ms_run[1]:index=2', 2),
        ('PSM', 'SHAREDPEPTIDEK', 2, 'P3', 0, 'null',
         'ms_run[1]:index=2', 2),
        ('PSM', 'PEPTIDEK', 3, 'P1', 1, '0-UNIMOD:1',
         'ms_run[2]:index=9', 2),
    ]

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.file = _write_mztab(self.directory, 'support.mztab', self.rows)

    def test_report_counts_and_edge_table(self):
        report = mztab.protein_support(self.file)
        self.assertIsInstance(report, mztab.ProteinSupportReport)
        p1 = report['P1']
        self.assertEqual(p1.unique_peptides, 1)
        self.assertEqual(p1.shared_peptides, 0)
        self.assertEqual(p1.total_peptides, 1)
        self.assertEqual(p1.unique_psms, 2)
        self.assertTrue(p1.uniquely_identified)

        p2 = report['P2']
        self.assertEqual(p2.unique_peptides, 0)
        self.assertEqual(p2.shared_peptides, 1)
        self.assertEqual(p2.support_type, 'shared')
        self.assertFalse(p2.uniquely_identified)
        self.assertEqual(p2.shared_with, ('P3',))

        p4 = report['P4']
        self.assertEqual(p4.support_type, 'none')
        self.assertFalse(p4.uniquely_identified)

        self.assertEqual(len(report.links), 3)
        self.assertEqual(len(report.protein_table), 4)
        self.assertEqual(
            set(report.protein_table['accession']), {'P1', 'P2', 'P3', 'P4'})

    def test_modification_forms_are_merged_with_sources(self):
        report = mztab.protein_support(self.file)
        peptide = [p for p in report.peptides if p.sequence == 'PEPTIDEK'][0]
        self.assertEqual(peptide.n_proteins, 1)
        self.assertEqual(len(peptide.proteins), 1)
        self.assertIn('N-term|UNIMOD:1', peptide.modifications)
        self.assertEqual(
            set(peptide.raw_modifications),
            {'0-CHEMMOD:42.0105646863', '0-UNIMOD:1'})
        self.assertEqual(len(peptide.modification_forms), 1)
        form = peptide.modification_forms[0]
        self.assertEqual(form['canonical'], 'UNIMOD:1')
        self.assertEqual(
            set(form['raw_forms']),
            {'0-CHEMMOD:42.0105646863', '0-UNIMOD:1'})
        self.assertEqual(len(form['sources']), 1)
        self.assertEqual(peptide.evidence_count, 2)

    def test_custom_modification_rules_collapse_forms(self):
        report = mztab.protein_support(
            self.file,
            modification_rules=lambda position, name: 'ACETYL')
        peptide = [p for p in report.peptides if p.sequence == 'PEPTIDEK'][0]
        self.assertEqual(peptide.modifications, ('N-term|ACETYL',))
        self.assertEqual(
            set(peptide.raw_modifications),
            {'0-CHEMMOD:42.0105646863', '0-UNIMOD:1'})

    def test_duplicate_exports_do_not_add_support(self):
        duplicate = _write_mztab(
            self.directory, 'support-copy.mztab', self.rows)
        single = mztab.protein_support(self.file)
        combined = mztab.protein_support([self.file, duplicate])
        self.assertEqual(
            single['P1'].total_peptides, combined['P1'].total_peptides)
        self.assertEqual(
            single['P1'].unique_psms, combined['P1'].unique_psms)
        self.assertEqual(len(single.evidence), len(combined.evidence))
        self.assertEqual(combined.summary()['sources'], 2)
        self.assertGreater(combined.summary()['duplicate_evidence'], 0)

    def test_distinct_runs_are_independent_evidence(self):
        header = _MINIMAL_MZTAB_HEADER.replace(
            'file:///data/run2.mzML', 'file:///data/other_run.mzML')
        rows = [row for row in self.rows if row[0] == 'PRT']
        rows.extend([
            ('PSM', 'PEPTIDEK', 1, 'P1', 1, '0-CHEMMOD:42.0105646863',
             'ms_run[1]:index=1', 2),
            ('PSM', 'PEPTIDEK', 3, 'P1', 1, '0-UNIMOD:1',
             'ms_run[2]:index=9', 2),
        ])
        other = _write_mztab(
            self.directory, 'other.mztab', rows, header=header)
        single = mztab.protein_support(self.file)
        combined = mztab.protein_support([self.file, other])
        self.assertGreater(
            combined['P1'].unique_psms, single['P1'].unique_psms)
        self.assertEqual(len(combined.evidence), 4)

    def test_shared_peptide_cannot_claim_unique_protein(self):
        rows = [
            ('PRT', 'P2', 'shared protein'),
            ('PSM', 'SHAREDPEPTIDEK', 1, 'P2', 1, 'null',
             'ms_run[1]:index=1', 2),
            ('PSM', 'SHAREDPEPTIDEK', 1, 'P3', 1, 'null',
             'ms_run[1]:index=1', 2),
        ]
        file_name = _write_mztab(self.directory, 'liar.mztab', rows)
        report = mztab.protein_support(file_name)
        for accession in ('P2', 'P3'):
            record = report[accession]
            self.assertFalse(record.uniquely_identified)
            self.assertEqual(record.unique_peptides, 0)
            self.assertTrue(record.unique_flag_conflict)

    def test_shared_flag_with_one_accession_counts_as_shared(self):
        rows = [
            ('PRT', 'P2', 'shared protein'),
            ('PSM', 'SHAREDPEPTIDEK', 1, 'P2', 0, 'null',
             'ms_run[1]:index=1', 2),
        ]
        file_name = _write_mztab(self.directory, 'flagged.mztab', rows)
        report = mztab.protein_support(file_name)
        record = report['P2']
        self.assertEqual(record.unique_peptides, 0)
        self.assertEqual(record.shared_peptides, 1)
        self.assertEqual(record.unique_psms, 0)
        self.assertEqual(record.shared_psms, 1)
        self.assertFalse(record.uniquely_identified)

    def test_reader_method_and_outputs(self):
        reader = mztab.MzTab(self.file)
        self.addCleanup(reader.file.close)
        report = reader.protein_support()
        self.assertEqual(report['P1'].total_peptides, 1)
        self.assertEqual(len(report.to_dataframe('peptides')), 2)
        self.assertEqual(len(report.to_dataframe('evidence')), 3)
        self.assertEqual(report.to_dict('proteins')[0]['accession'], 'P1')
        self.assertIn('unique_support', report['P1'].as_dict())


if __name__ == '__main__':
    unittest.main()
