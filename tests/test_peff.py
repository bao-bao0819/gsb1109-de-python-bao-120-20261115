import json
import os
import shutil
import tempfile
import unittest

import pyteomics
pyteomics.__path__ = [os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, 'pyteomics'))]
from pyteomics import peff
from pyteomics.auxiliary import OffsetIndex, PyteomicsError


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


class PEFFIndexerTest(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.source = os.path.join(self.workdir, 'test.peff')
        shutil.copy('test.peff', self.source)
        self.original_bytes = open(self.source, 'rb').read()
        name, ext = os.path.splitext(self.source)
        self.old_index_path = '{}-{}-byte-offsets.json'.format(name, ext[1:])
        with peff.IndexedPEFF(self.source) as reader:
            self.old_index = OffsetIndex(reader._offset_index)
        with open(self.old_index_path, 'w') as handle:
            self.old_index.save(handle)
        self.new_path = os.path.join(self.workdir, 'corrected.peff')
        self.index_path = os.path.join(
            self.workdir, 'corrected-peff-byte-offsets.json')

    def tearDown(self):
        shutil.rmtree(self.workdir, True)

    def _indexer(self, **kwargs):
        kwargs.setdefault('new_path', self.new_path)
        kwargs.setdefault('index_path', self.index_path)
        return peff.PEFFIndexer(self.source, **kwargs)

    def test_single_annotation_corrected(self):
        reader = self._indexer().correct_annotation(
            'NX_P07585-3', 'PName', 'Decorin isoform C corrected name')
        with peff.IndexedPEFF(self.source) as old:
            self.assertEqual(len(reader), len(old))
            for tag in old._id2header:
                old_entry = old.get_entry(tag)
                new_entry = reader.get_entry(tag)
                if tag == 'NX_P07585-3':
                    self.assertEqual(
                        new_entry.description.PName,
                        'Decorin isoform C corrected name')
                    for key in old_entry.description:
                        if key != 'PName':
                            self.assertEqual(
                                old_entry.description[key],
                                new_entry.description[key])
                    self.assertEqual(old_entry.sequence, new_entry.sequence)
                else:
                    self.assertEqual(old_entry, new_entry)

    def test_affected_offsets_updated(self):
        indexer = self._indexer()
        indexer.correct_annotation(
            'NX_P07585-3', 'PName', 'Decorin isoform C corrected name')
        delta = (len('Decorin isoform C corrected name')
                 - len('Decorin isoform C'))
        with open(self.index_path) as handle:
            saved = {key: tuple(value)
                     for key, value in OffsetIndex.load(handle).items()}
        with peff.IndexedPEFF(self.new_path) as reader:
            self.assertEqual(saved, dict(reader._offset_index))
            old_by_tag = {
                peff.IndexedPEFF(self.source).parser(header).Tag: offsets
                for header, offsets in self.old_index.items()}
            new_by_tag = {
                reader.parser(header).Tag: offsets
                for header, offsets in reader._offset_index.items()}
            target_start = old_by_tag['NX_P07585-3'][0]
            for tag, new_offsets in new_by_tag.items():
                old_offsets = old_by_tag[tag]
                if tag == 'NX_P07585-3':
                    expected = (old_offsets[0], old_offsets[1] + delta)
                elif old_offsets[0] > target_start:
                    expected = (old_offsets[0] + delta, old_offsets[1] + delta)
                else:
                    expected = old_offsets
                self.assertEqual(new_offsets, expected)
            for tag in reader._id2header:
                self.assertEqual(reader.get_entry(tag).description.Tag, tag)

    def test_index_json_format(self):
        self._indexer().correct_annotation('NX_P07585-1', 'GName', 'DCNX')
        container = json.load(open(self.index_path))
        self.assertEqual(
            set(container),
            {'index', OffsetIndex._schema_version_tag_key})

    def test_numeric_annotation_corrected(self):
        reader = self._indexer().correct_annotation(
            'NX_P07585-5', 'EV', '200')
        self.assertEqual(reader.get_entry('NX_P07585-5').description.EV, 200)
        self.assertEqual(reader.get_entry('NX_P07585-4').description.EV, 199)

    def test_duplicate_display_name_not_used_for_lookup(self):
        duplicated = os.path.join(self.workdir, 'duplicated.peff')
        with open(self.source, 'rb') as handle:
            data = handle.read().replace(
                b'Decorin isoform B', b'Decorin isoform A')
        with open(duplicated, 'wb') as handle:
            handle.write(data)
        indexer = peff.PEFFIndexer(
            duplicated,
            new_path=os.path.join(self.workdir, 'duplicated.corrected.peff'))
        with self.assertRaises(PyteomicsError):
            indexer.correct_annotation('Decorin isoform A', 'GName', 'X')
        reader = indexer.correct_annotation(
            'NX_P07585-2', 'GName', 'DCN2')
        self.assertEqual(
            reader.get_entry('NX_P07585-2').description.GName, 'DCN2')
        self.assertEqual(
            reader.get_entry('NX_P07585-1').description.GName, 'DCN')

    def test_unknown_id_rejected(self):
        with self.assertRaises(PyteomicsError):
            self._indexer().correct_annotation(
                'NX_DOES_NOT_EXIST', 'PName', 'x')

    def test_feature_annotation_cannot_be_corrected(self):
        with self.assertRaises(PyteomicsError):
            self._indexer().correct_annotation(
                'NX_P07585-1', 'ModResPsi', 'x')
        with self.assertRaises(PyteomicsError):
            self._indexer().correct_annotation(
                'NX_P07585-1', 'NoSuchField', 'x')

    def test_snapshot_never_modified(self):
        self._indexer().correct_annotation(
            'NX_P07585-2', 'PName', 'Decorin isoform B renamed')
        self.assertEqual(open(self.source, 'rb').read(), self.original_bytes)
        with open(self.old_index_path) as handle:
            old_index = {key: tuple(value)
                         for key, value in OffsetIndex.load(handle).items()}
        self.assertEqual(old_index, dict(self.old_index))

    def test_overwrite_guards(self):
        with self.assertRaises(PyteomicsError):
            peff.PEFFIndexer(self.source, new_path=self.source)
        with self.assertRaises(PyteomicsError):
            peff.PEFFIndexer(
                self.source, index_path=self.old_index_path)

    def test_failure_leaves_usable_old_snapshot(self):
        missing_dir = os.path.join(self.workdir, 'missing_index_dir')
        partial_path = os.path.join(self.workdir, 'partial.peff')
        with self.assertRaises(OSError):
            peff.PEFFIndexer(
                self.source,
                new_path=partial_path,
                index_path=os.path.join(missing_dir, 'index.json')
                ).correct_annotation('NX_P07585-1', 'PName', 'renamed')
        self.assertFalse(os.path.exists(
            os.path.join(missing_dir, 'index.json')))
        self.assertFalse(os.path.exists(partial_path))
        with peff.IndexedPEFF(self.source) as reader:
            self.assertEqual(reader.number_of_entries, 5)
        with open(self.old_index_path) as handle:
            old_index = {key: tuple(value)
                         for key, value in OffsetIndex.load(handle).items()}
        self.assertEqual(old_index, dict(self.old_index))

    def test_default_output_paths(self):
        indexer = peff.PEFFIndexer(self.source)
        self.assertTrue(indexer.new_path.endswith('test.corrected.peff'))
        self.assertTrue(
            indexer.index_path.endswith(
                'test.corrected-peff-byte-offsets.json'))


if __name__ == '__main__':
    unittest.main()
