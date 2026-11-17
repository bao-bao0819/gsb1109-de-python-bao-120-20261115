from os import path
import unittest
import pyteomics
pyteomics.__path__ = [path.abspath(path.join(path.dirname(__file__), path.pardir, 'pyteomics'))]
from pyteomics import peff


class PEFFTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.peff_file = 'test.peff'

    def test_parse(self, reader=None):
        if reader is None:
            reader = peff.IndexedPEFF(self.peff_file)
        self.assertEqual(reader.number_of_entries, 5)
        self.assertEqual(len(reader.header_blocks), 1)

        protein = next(reader)
        self.assertEqual(protein.description.Tag, "NX_P07585-1")
        self.assertEqual(protein, reader.get_entry("NX_P07585-1"))

        protein2 = reader.get_entry("NX_P07585-3")
        self.assertEqual(protein, protein)
        self.assertNotEqual(protein, protein2)

        self.assertEqual(protein.description.TaxName, "Homo Sapiens")
        self.assertEqual(protein.description["NcbiTaxId"], 9606)
        self.assertEqual(len(protein.description.ModResPsi), 2)


class PEFFCatalogTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.catalog = peff.PEFFCatalog('test.peff')

    def test_tables_are_separate(self):
        catalog = self.catalog
        self.assertEqual(len(catalog.entities), 5)
        self.assertEqual(len(catalog.sequence_versions), 5)
        self.assertEqual(list(catalog.entities)[0], 'NX_P07585-1')
        self.assertEqual(list(catalog.sequence_versions),
                         [('NX_P07585-1', '1'), ('NX_P07585-2', '1'),
                          ('NX_P07585-3', '1'), ('NX_P07585-4', '1'),
                          ('NX_P07585-5', '1')])
        entity = catalog.get_entity('NX_P07585-1')
        self.assertEqual(entity.name, 'Decorin isoform A')
        self.assertEqual(entity.ncbi_tax_id, 9606)
        version = catalog.get_sequence('NX_P07585-1')
        self.assertEqual(version.length, 359)
        self.assertEqual(version.sequence[:10], 'MKATIILLLL')
        self.assertEqual(version.sequence_type, 'AA')
        self.assertEqual(catalog.proteins['NX_P07585-1'], entity)
        self.assertEqual(catalog.versions[('NX_P07585-1', '1')], version)
        self.assertIsInstance(catalog.occurrences_table[0], dict)
        self.assertEqual(catalog.occurrences_table[0]['start'], 34)

    def test_declared_annotations_are_preserved(self):
        catalog = self.catalog
        self.assertEqual(len(catalog.annotations), 623)
        entity = catalog.get_entity('NX_P07585-1')
        self.assertEqual(entity.prefix, 'nxp')
        self.assertEqual(entity.gene, 'DCN')
        version = catalog.get_sequence('NX_P07585-1')
        self.assertEqual(version.entry_version, '199')
        self.assertEqual(catalog.current_version('NX_P07585-1'), version)

    def test_occurrences_keep_declared_positions(self):
        catalog = self.catalog
        mods = catalog.occurrences_for('NX_P07585-1', annotation_type='ModResPsi')
        self.assertEqual(len(mods), 2)
        first = mods[0]
        self.assertEqual(first.start, 34)
        self.assertEqual(first.end, 34)
        self.assertEqual(first.position, 34)
        self.assertEqual(first.accession, 'MOD:00046')
        self.assertEqual(first.name, 'O-phospho-L-serine')
        self.assertEqual(first.affected_residues, 'S')
        self.assertEqual(first.status, peff.STATUS_CURRENT)
        self.assertIsNone(first.replacement)
        self.assertIsNone(first.replacement_length)

    def test_variant_complex_uses_declared_range_and_replacement_length(self):
        catalog = self.catalog
        occurrence = catalog.occurrences_for(
            'NX_P07585-1', annotation_type='VariantComplex')[0]
        self.assertEqual(occurrence.range, (9, 9))
        self.assertTrue(occurrence.end_declared)
        self.assertTrue(occurrence.is_range)
        self.assertEqual(occurrence.declared_length, 1)
        self.assertEqual(occurrence.replacement, '')
        self.assertEqual(occurrence.replacement_length, 0)
        self.assertEqual(occurrence.affected_residues, 'L')

    def test_deletion_annotation_is_pending_after_remap(self):
        catalog = self.catalog
        sequence = catalog.get_sequence('NX_P07585-1').sequence
        deleted = sequence[:8] + sequence[9:]
        catalog.add_version('NX_P07585-1', '2', deleted,
                            previous_sequence=sequence,
                            previous_sequence_version='1')
        remapped, pending = catalog.remap_annotations(
            'NX_P07585-1', '1', '2', correction=(9, sequence[8], ''))
        variant = [a for a in pending if a.annotation_type == 'VariantComplex']
        self.assertEqual(len(variant), 1)
        self.assertEqual(variant[0].status, peff.STATUS_PENDING_REMAP)
        self.assertIsNone(variant[0].current_start)
        self.assertIn('deletion', variant[0].remap_note)

    def test_processed_uses_inclusive_range(self):
        catalog = self.catalog
        processed = catalog.occurrences_for('NX_P07585-1', annotation_type='Processed')
        self.assertEqual([(o.start, o.end, o.name) for o in processed],
                         [(1, 16, 'signal peptide'),
                          (17, 30, 'maturation peptide'),
                          (31, 359, 'mature protein')])
        self.assertEqual(processed[0].declared_length, 16)

    def test_old_annotations_require_explicit_remap(self):
        catalog = self.catalog
        current = catalog.get_sequence('NX_P07585-1')
        sequence = current.sequence
        edited = sequence[:99] + sequence[100:]

        catalog.add_version('NX_P07585-1', '2', edited,
                            previous_sequence=sequence,
                            previous_sequence_version='1')
        pending_before = catalog.pending('NX_P07585-1')
        self.assertTrue(pending_before)
        self.assertTrue(all(a.sequence_version == '1' for a in pending_before))
        self.assertTrue(all(a.status == peff.STATUS_PENDING_REMAP
                            for a in pending_before))
        old = [a for a in pending_before if a.start == 331][0]
        self.assertEqual(old.position, 331)
        self.assertEqual(old.current_start, None)
        self.assertEqual(old.target_sequence_version, '2')

        remapped, pending = catalog.remap_annotations(
            'NX_P07585-1', '1', '2', correction=(100, sequence[99], ''))
        self.assertGreater(len(remapped), 0)
        shifted = [a for a in remapped if a.start == 331][0]
        self.assertEqual(shifted.current_start, 330)
        self.assertEqual(shifted.status, peff.STATUS_MAPPED)
        removed = [a for a in pending if a.start == 100]
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0].status, peff.STATUS_PENDING_REMAP)
        self.assertEqual(removed[0].affected_residues, sequence[99])
        self.assertEqual(removed[0].current_start, None)

    def test_equal_length_substitution_keeps_only_unchanged_residues(self):
        catalog = self.catalog
        sequence = catalog.get_sequence('NX_P07585-1').sequence
        edited = sequence[:99] + 'A' + sequence[100:]
        catalog.add_version('NX_P07585-1', '2', edited,
                            previous_sequence=sequence,
                            previous_sequence_version='1')
        remapped, pending = catalog.remap_annotations(
            'NX_P07585-1', '1', '2', correction=(100, sequence[99], 'A'))
        changed = [a for a in pending if a.start == 100]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].status, peff.STATUS_PENDING_REMAP)
        untouched = [a for a in remapped if a.start == 331][0]
        self.assertEqual(untouched.current_start, 331)

    def test_insertion_shifts_subsequent_annotations(self):
        catalog = self.catalog
        sequence = catalog.get_sequence('NX_P07585-1').sequence
        edited = sequence[:100] + 'GGG' + sequence[100:]
        catalog.add_version('NX_P07585-1', '2', edited,
                            previous_sequence=sequence,
                            previous_sequence_version='1')
        remapped, pending = catalog.remap_annotations(
            'NX_P07585-1', '1', '2', correction=(101, '', 'GGG'))
        shifted = [a for a in remapped if a.start == 331][0]
        self.assertEqual(shifted.current_start, 334)

    def test_spanning_range_shifts_with_correction(self):
        catalog = self.catalog
        sequence = catalog.get_sequence('NX_P07585-1').sequence
        edited = sequence[:100] + 'GGG' + sequence[100:]
        catalog.add_version('NX_P07585-1', '2', edited,
                            previous_sequence=sequence,
                            previous_sequence_version='1')
        remapped, pending = catalog.remap_annotations(
            'NX_P07585-1', '1', '2', correction=(101, '', 'GGG'))
        processed = [a for a in remapped if a.annotation_type == 'Processed']
        mature = [a for a in processed if a.name == 'mature protein'][0]
        self.assertEqual((mature.start, mature.end), (31, 359))
        self.assertEqual((mature.current_start, mature.current_end), (31, 362))
        self.assertEqual(pending, [])

    def test_infer_correction_requires_single_block(self):
        catalog = self.catalog
        sequence = catalog.get_sequence('NX_P07585-1').sequence
        edited = sequence[:99] + sequence[100:]
        catalog.add_version('NX_P07585-1', '2', edited,
                            previous_sequence=sequence,
                            previous_sequence_version='1')
        self.assertEqual(catalog.infer_correction('NX_P07585-1', '1', '2'),
                         (100, sequence[99], ''))
        catalog.add_version('NX_P07585-1', '3',
                            edited[:49] + 'Q' + edited[50:],
                            previous_sequence=edited,
                            previous_sequence_version='2')
        with self.assertRaises(peff.PyteomicsError):
            catalog.infer_correction('NX_P07585-1', '1', '3')

    def test_remap_rejects_inconsistent_correction(self):
        catalog = self.catalog
        catalog.add_version('NX_P07585-1', '2',
                            catalog.get_sequence('NX_P07585-1').sequence + 'A',
                            previous_sequence=catalog.get_sequence('NX_P07585-1').sequence,
                            previous_sequence_version='1')
        with self.assertRaises(peff.PyteomicsError):
            catalog.remap_annotations('NX_P07585-1', '1', '2',
                                      correction=(1, 'MK', 'X'))


if __name__ == '__main__':
    unittest.main()

