import os
import shutil
import subprocess
import sys
import tempfile
import pyteomics
from io import BytesIO
from lxml import etree
pyteomics.__path__ = [os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, 'pyteomics'))]
from itertools import product
import unittest
from pyteomics.mzml import (
    MzML, PreIndexedMzML, read, chain, spectrum_manifest,
    merge_spectrum_manifests, dump_spectrum_manifest, load_spectrum_manifest)
from pyteomics import auxiliary as aux, xml
from data import mzml_spectra
import numpy as np
import pickle
import operator as op
import pynumpress
import base64
import gzip
import json
import zlib
from copy import copy

from psims.controlled_vocabulary.controlled_vocabulary import obo_cache
obo_cache.cache_path = '.'
obo_cache.enabled = True


class MzmlTest(unittest.TestCase):
    maxDiff = None
    path = 'test.mzML'

    def test_read(self):
        for rs, it, ui in product([True, False], repeat=3):
            if rs:
                continue  # temporarily disable retrieval of schema
            for func in [MzML, read, chain, lambda x, **kw: chain.from_iterable([x], **kw), PreIndexedMzML]:
                with func(self.path, read_schema=rs, iterative=it, use_index=ui) as r:
                    # http://stackoverflow.com/q/14246983/1258041
                    self.assertEqual(mzml_spectra, list(r))

    def test_map_read(self):
        key = op.itemgetter('index')
        with MzML(self.path) as f:
            for method in ['p', 't']:
                with self.subTest(method=method):
                    self.assertEqual(sorted(mzml_spectra, key=key), sorted(list(f.map(method=method)), key=key))

    def test_map_chain(self):
        key = op.itemgetter('index')
        with chain(self.path, self.path) as f:
            for method in ['p', 't']:
                with self.subTest(method=method):
                    self.assertEqual(sorted(mzml_spectra * 2, key=key), sorted(list(f.map(method=method)), key=key))

    def test_mp_requires_index(self):
        with MzML(self.path, use_index=False) as r:
            self.assertRaises(aux.PyteomicsError, r.map)

    def test_map_qsize(self):
        key = op.itemgetter('index')
        with MzML(self.path, queue_size=1000) as f:
            self.assertEqual(f._queue_size, 1000)
            self.assertEqual(sorted(mzml_spectra, key=key), sorted(list(f.map()), key=key))

    def test_decoding(self):
        with MzML(self.path, decode_binary=True) as reader:
            spectrum = next(reader)
            self.assertIsNotNone(spectrum['m/z array'])
        validation = spectrum['m/z array']
        with MzML(self.path) as reader:
            spectrum = next(reader)
            self.assertIsNotNone(spectrum['m/z array'])
            self.assertTrue(np.allclose(spectrum['m/z array'], validation))
        with MzML(self.path, decode_binary=False) as reader:
            spectrum = next(reader)
            record = spectrum['m/z array']
            self.assertEqual(record.compression, "no compression")
            self.assertEqual(record.dtype, np.float64)
            array = record.decode()
            self.assertTrue(np.allclose(validation, array))
            record = spectrum['intensity array']
            self.assertEqual(record.dtype, np.float32)
            self.assertEqual(record.compression, "no compression")

            spectrum = next(reader)
            record = spectrum['intensity array']
            self.assertEqual(record.compression, "zlib compression")
            self.assertEqual(mzml_spectra[1]['intensity array'], record.decode())

    def test_read_dtype(self):
        dtypes = {'m/z array': np.float32, 'intensity array': np.int32}
        with read(self.path, dtype=dtypes) as f:
            for spec in f:
                for k, v in dtypes.items():
                    self.assertEqual(spec[k].dtype, v)

    def test_has_built_index(self):
        with read(self.path, use_index=True) as f:
            self.assertGreater(len(f._offset_index), 0)
        with read(self.path, use_index=False) as f:
            self.assertEqual(f._offset_index, None)

    def test_prebuild_index(self):
        test_dir = tempfile.mkdtemp()
        work_path = os.path.join(test_dir, self.path)
        with open(work_path, 'w') as dest, open(self.path) as source:
            dest.write(source.read())
        assert dest.closed
        with MzML(work_path, use_index=False) as inst:
            self.assertRaises(IOError, inst._read_byte_offsets)
            with open(inst._byte_offset_filename, 'wt') as fh:
                fh.write("{}")
            self.assertRaises(TypeError, inst._read_byte_offsets)
            os.remove(inst._byte_offset_filename)
        with MzML(work_path, use_index=True) as inst:
            offsets_exist = os.path.exists(inst._byte_offset_filename)
            self.assertEqual(offsets_exist, inst._check_has_byte_offset_file())
            self.assertTrue(isinstance(inst._offset_index, xml.HierarchicalOffsetIndex))
        self.assertTrue(inst._source.closed)
        MzML.prebuild_byte_offset_file(work_path)
        with open(inst._byte_offset_filename, 'rt') as fh:
            index = MzML._index_class.load(fh)
            assert inst._offset_index['spectrum'] == index['spectrum']
        with MzML(work_path, use_index=True) as inst:
            offsets_exist = os.path.exists(inst._byte_offset_filename)
            self.assertTrue(offsets_exist)
            self.assertEqual(offsets_exist, inst._check_has_byte_offset_file())
            self.assertTrue(isinstance(inst._offset_index, xml.HierarchicalOffsetIndex))
        self.assertTrue(inst._source.closed)
        os.remove(inst._byte_offset_filename)
        with MzML(work_path, use_index=True) as inst:
            offsets_exist = os.path.exists(inst._byte_offset_filename)
            self.assertEqual(offsets_exist, inst._check_has_byte_offset_file())
            self.assertTrue(isinstance(inst._offset_index, xml.HierarchicalOffsetIndex))
        self.assertTrue(inst._source.closed)
        shutil.rmtree(test_dir, True)

    def test_unit_extract(self):
        with MzML(self.path) as handle:
            for scan in handle:
                scan_inst = scan['scanList']['scan'][0]
                scan_time = scan_inst['scan start time']
                scan_window_lower_limit = scan_inst['scanWindowList']['scanWindow'][0]['scan window lower limit']
                self.assertEqual(scan_time.unit_info, 'minute')
                self.assertEqual(scan_window_lower_limit.unit_info, 'm/z')

    def test_cv_query(self):
        with MzML(self.path) as handle:
            scan = next(handle)
            index = aux.cvquery(scan)
            self.assertEqual(index['MS:1000511'], 1)
            self.assertEqual(aux.cvquery(scan, "MS:1000511"), 1)

            # test deep traversal
            self.assertEqual(index['MS:1000016'], 0.004935)
            self.assertEqual(aux.cvquery(scan, 'MS:1000016'), 0.004935)

    def test_retrieve_refs(self):
        with MzML(self.path) as reader:
            derefed = list(reader.iterfind("instrumentConfiguration", retrieve_refs=True))
            reader.reset()
            raw = list(reader.iterfind("instrumentConfiguration", retrieve_refs=False))
            self.assertEqual(raw[0].get("softwareRef"),
                             {'ref': 'Xcalibur'})
            self.assertNotIn("ref", derefed[0]['softwareRef'])
            self.assertEqual(derefed[0].get('softwareRef'), {
                             'version': '1.1 Beta 7', 'Xcalibur': ''})

    def test_in_memory_buffer(self):
        with open(self.path, 'rb') as fh:
            data_buffer = BytesIO(fh.read())
        with MzML(data_buffer) as reader:
            spectrum = next(reader)
            self.assertEqual(spectrum['id'], 'controllerType=0 controllerNumber=1 scan=1')
        data_buffer.seek(0)
        with MzML(data_buffer, use_index=True) as reader:
            spectrum = next(reader)
            self.assertEqual(spectrum['id'], 'controllerType=0 controllerNumber=1 scan=1')

    def test_picklable(self):
        with MzML(self.path) as reader:
            expected_data = next(reader)
            spec = pickle.dumps(reader)
        with pickle.loads(spec) as reader:
            self.assertEqual(next(reader)['id'], expected_data['id'])

    def test_indexing(self):
        with MzML(self.path) as reader:
            self.assertEqual(mzml_spectra[0], reader[0])
            self.assertEqual(mzml_spectra[0], reader['controllerType=0 controllerNumber=1 scan=1'])
            self.assertEqual(mzml_spectra, reader[0:2])
            self.assertEqual(mzml_spectra,
                [reader['controllerType=0 controllerNumber=1 scan=1'],
                 reader['controllerType=0 controllerNumber=1 scan=2']])
            self.assertEqual(mzml_spectra, reader[[0, 1]])
            self.assertEqual(mzml_spectra, reader[
                ['controllerType=0 controllerNumber=1 scan=1', 'controllerType=0 controllerNumber=1 scan=2']])
            self.assertEqual(mzml_spectra, reader[
                'controllerType=0 controllerNumber=1 scan=2':'controllerType=0 controllerNumber=1 scan=1'])

    def test_time_locator(self):
        with MzML(self.path) as reader:
            self.assertEqual(mzml_spectra[0], reader.time[0])
            self.assertEqual(mzml_spectra[1], reader.time[0.1])
            self.assertEqual(mzml_spectra, reader.time[0:0.1])

    def test_numpress_slof(self):
        data = mzml_spectra[0]['intensity array']
        encoded = base64.b64encode(pynumpress.encode_slof(data, pynumpress.optimal_slof_fixed_point(data)).tobytes()).decode('ascii')
        record = aux.BinaryDataArrayTransformer()._make_record(encoded, 'MS-Numpress short logged float compression', data.dtype)
        self.assertTrue(np.allclose(data, record.decode(), rtol=0.001))

    def test_numpress_slof_zlib(self):
        data = mzml_spectra[0]['intensity array']
        encoded = base64.b64encode(zlib.compress(pynumpress.encode_slof(data, pynumpress.optimal_slof_fixed_point(data)).tobytes())).decode('ascii')
        record = aux.BinaryDataArrayTransformer()._make_record(encoded, 'MS-Numpress short logged float compression followed by zlib compression', data.dtype)
        self.assertTrue(np.allclose(data, record.decode(), rtol=0.001))

    def test_numpress_linear(self):
        data = mzml_spectra[0]['intensity array']
        encoded = base64.b64encode(pynumpress.encode_linear(data, pynumpress.optimal_linear_fixed_point(data)).tobytes()).decode('ascii')
        record = aux.BinaryDataArrayTransformer()._make_record(encoded, 'MS-Numpress linear prediction compression', data.dtype)
        self.assertTrue(np.allclose(data, record.decode(), rtol=0.001))

    def test_numpress_linear_zlib(self):
        data = mzml_spectra[0]['intensity array']
        encoded = base64.b64encode(zlib.compress(pynumpress.encode_linear(data, pynumpress.optimal_linear_fixed_point(data)).tobytes())).decode('ascii')
        record = aux.BinaryDataArrayTransformer()._make_record(encoded, 'MS-Numpress linear prediction compression followed by zlib compression', data.dtype)
        self.assertTrue(np.allclose(data, record.decode(), rtol=0.001))

    def test_numpress_pic(self):
        data = mzml_spectra[0]['intensity array']
        encoded = base64.b64encode(pynumpress.encode_pic(data).tobytes()).decode('ascii')
        record = aux.BinaryDataArrayTransformer()._make_record(encoded, 'MS-Numpress positive integer compression', data.dtype)
        self.assertTrue(np.allclose(data, record.decode(), atol=0.6))

    def test_numpress_pic_zlib(self):
        data = mzml_spectra[0]['intensity array']
        encoded = base64.b64encode(zlib.compress(pynumpress.encode_pic(data).tobytes())).decode('ascii')
        record = aux.BinaryDataArrayTransformer()._make_record(encoded, 'MS-Numpress positive integer compression followed by zlib compression', data.dtype)
        self.assertTrue(np.allclose(data, record.decode(), atol=0.6))

    def test_numpress_without_pynumpress(self):
        # Without pynumpress, numpress arrays must fail loudly instead of being read as raw floats.
        # This 72-byte payload (a multiple of 8) used to decode silently into garbage.
        code = """if True:
            import sys
            sys.modules['pynumpress'] = None
            sys.path.insert(0, {root!r})
            import numpy as np
            from pyteomics import auxiliary as aux
            encoded = 'QWqxfWAAAABnivVTxv//fyDBrmqUaLEYc/MhqZkLU8a9MryaZaLxPxIsFLYkioK6pZe6p/QbukCP5ylHLKIjtUEncGsqjMIt'
            for name in ['MS-Numpress linear prediction compression',
                         'MS-Numpress positive integer compression',
                         'MS-Numpress short logged float compression',
                         'MS-Numpress linear prediction compression followed by zlib compression',
                         'MS-Numpress positive integer compression followed by zlib compression',
                         'MS-Numpress short logged float compression followed by zlib compression']:
                record = aux.BinaryDataArrayTransformer()._make_record(encoded, name, np.float64)
                try:
                    record.decode()
                except aux.PyteomicsError as e:
                    assert 'pynumpress' in str(e)
                else:
                    sys.exit('decoded without pynumpress: ' + name)
            """.format(root=os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir)))
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_userparam_units(self):
        xml_str = '<userParam name="some quantity" value="42" unitName="cats"/>'
        parser = etree.XMLParser()
        parser.feed(xml_str)
        element = parser.close()

        with MzML(self.path) as reader:
            param = reader._handle_param(element)
            self.assertEqual(param.value.unit_info, 'cats')

    def test_cvparam_unitname_lookup(self):
        # uniName omitted
        xml_str = '<cvParam cvRef="MS" accession="MS:1000504" name="base peak m/z" value="810.415283203125" unitCvRef="MS" unitAccession="MS:1000040"/>'
        parser = etree.XMLParser()
        parser.feed(xml_str)
        element = parser.close()

        with MzML(self.path) as reader:
            param = reader._handle_param(element)
            self.assertEqual(param.value.unit_info, 'm/z')

    def test_copy_behavior(self):
        with MzML(self.path) as reader:
            creader = copy(reader)
            self.assertTrue(isinstance(creader, MzML))
            self.assertIs(creader.index, reader.index)
            self.assertIsNot(creader._source, reader._source)
            self.assertNotEqual(creader._source.fileno(), reader._source.fileno())
            creader.close()


class ManifestTest(unittest.TestCase):
    maxDiff = None
    path = 'test.mzML'

    def _write(self, content, mode='wb'):
        handle = tempfile.NamedTemporaryFile(
            mode=mode, suffix='.mzML', delete=False)
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        handle.write(content)
        handle.close()
        return handle.name

    def _spectrum(self, spectrum_id, index, arrays, count=2, ms_level=1):
        array_xml = []
        for name, (dtype, compression, payload, declared) in arrays.items():
            array_xml.append(
                '<binaryDataArray arrayLength="{declared}" encodedLength="{encoded}">'
                '<cvParam cvRef="MS" accession="{dtype_acc}" name="{dtype}"/>'
                '<cvParam cvRef="MS" accession="{comp_acc}" name="{compression}"/>'
                '<cvParam cvRef="MS" accession="{name_acc}" name="{name}"/>'
                '<binary>{payload}</binary>'
                '</binaryDataArray>'.format(
                    declared=declared, encoded=len(payload),
                    dtype_acc={'32-bit float': 'MS:1000521',
                               '64-bit float': 'MS:1000523'}[dtype],
                    comp_acc={'no compression': 'MS:1000576',
                              'zlib compression': 'MS:1000574'}[compression],
                    name_acc={'m/z array': 'MS:1000514',
                              'intensity array': 'MS:1000515',
                              'charge array': 'MS:1000516'}[name],
                    dtype=dtype, compression=compression, name=name,
                    payload=payload))
        return (
            '<spectrum index="{index}" id="{spectrum_id}" defaultArrayLength="{length}">'
            '<cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="{ms_level}"/>'
            '<binaryDataArrayList count="{count}">{arrays}</binaryDataArrayList>'
            '</spectrum>'.format(
                index=index, spectrum_id=spectrum_id, length=count,
                ms_level=ms_level, count=len(arrays),
                arrays=''.join(array_xml)))

    def _document(self, spectra, declared=None):
        if declared is None:
            declared = len(spectra)
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<indexedmzML xmlns="http://psi.hupo.org/ms/mzml">'
            '<mzML><run><spectrumList count="{declared}">{spectra}'
            '</spectrumList></run></mzML></indexedmzML>'.format(
                declared=declared, spectra=''.join(spectra))
        ).encode('utf-8')

    @staticmethod
    def _payload(values, dtype=np.float64):
        return base64.b64encode(np.asarray(values, dtype=dtype).tobytes()).decode('ascii')

    def test_manifest_complete_file(self):
        manifest = spectrum_manifest(self.path)
        self.assertEqual(manifest['format'], 'mzML-spectrum-manifest')
        self.assertEqual(manifest['declared_spectrum_count'], 2)
        self.assertEqual(manifest['received_scan_count'], 2)
        self.assertEqual(manifest['complete_spectrum_count'], 2)
        self.assertTrue(manifest['complete'])
        self.assertEqual(manifest['missing_scans'], [])
        self.assertEqual(manifest['missing_arrays'], [])
        self.assertEqual(manifest['duplicate_ids'], [])
        for spectrum in manifest['spectra']:
            self.assertTrue(spectrum['complete'])
            self.assertEqual(spectrum['defaultArrayLength'], 19914)
            self.assertEqual(
                spectrum['arrays']['m/z array']['length'], 19914)
            self.assertEqual(
                spectrum['arrays']['intensity array']['length'], 19914)
            self.assertEqual(
                spectrum['arrays']['intensity array']['dtype'], 'float32')
            self.assertEqual(len(spectrum['arrays']['m/z array']['sha1']), 40)
        # The manifest must not leak non-JSON-serializable values.
        json.dumps(manifest)

    def test_manifest_truncated_download(self):
        with open(self.path, 'rb') as handle:
            data = handle.read()
        truncated = self._write(data[:len(data) // 3])
        manifest = spectrum_manifest(truncated)
        self.assertFalse(manifest['complete'])
        self.assertTrue(manifest['truncated'])
        self.assertEqual(manifest['declared_spectrum_count'], 2)
        self.assertEqual(manifest['received_scan_count'], 1)
        self.assertEqual(manifest['missing_scans'], [1])
        self.assertEqual(manifest['missing_scan_indices'], [1])
        self.assertEqual(manifest['missing_scan_numbers'], [2])
        partial = manifest['spectra'][0]
        self.assertFalse(partial['complete'])
        self.assertFalse(partial['arrays_complete'])
        self.assertEqual(
            set(partial['missing_arrays']), {'m/z array', 'intensity array'})
        self.assertEqual(
            sum(item['id'] == partial['id']
                for item in manifest['missing_arrays']), 1)

    def test_manifest_missing_array_is_not_complete(self):
        mz = self._payload([100.0, 200.0])
        intensity = self._payload([10.0, 20.0])
        spectra = [
            self._spectrum('scan=1', 0, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
                'intensity array': ('64-bit float', 'no compression', intensity, 2),
            }),
            # Missing intensity array despite a truthful defaultArrayLength.
            self._spectrum('scan=2', 1, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
            }),
            # Registered array, but the payload decodes to the wrong length.
            self._spectrum('scan=3', 2, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
                'intensity array': ('64-bit float', 'no compression', mz, 2),
            }, count=3),
        ]
        manifest = spectrum_manifest(self._write(self._document(spectra)))
        self.assertFalse(manifest['complete'])
        self.assertEqual(manifest['received_scan_count'], 3)
        self.assertEqual(manifest['complete_spectrum_count'], 1)
        self.assertEqual(manifest['missing_scans'], [])
        missing = {item['id']: item for item in manifest['missing_arrays']}
        self.assertEqual(missing['scan=2']['missing_arrays'], ['intensity array'])
        self.assertIn('length mismatch', missing['scan=3']['issues']['intensity array'])

    def test_manifest_missing_scans_separate_from_arrays(self):
        mz = self._payload([100.0, 200.0])
        spectra = [self._spectrum('scan=1', 0, {
            'm/z array': ('64-bit float', 'no compression', mz, 2),
            'intensity array': ('64-bit float', 'no compression', mz, 2),
        })]
        manifest = spectrum_manifest(
            self._write(self._document(spectra, declared=3)))
        self.assertEqual(manifest['received_scan_count'], 1)
        self.assertEqual(manifest['complete_spectrum_count'], 1)
        self.assertEqual(manifest['missing_scans'], [1, 2])
        self.assertEqual(manifest['missing_scan_numbers'], [2, 3])
        self.assertEqual(manifest['missing_arrays'], [])
        self.assertEqual(manifest['missing_spectrum_count'], 2)
        self.assertFalse(manifest['complete'])

    def test_manifest_duplicate_ids(self):
        mz = self._payload([100.0, 200.0])
        spectra = [
            self._spectrum('scan=1', 0, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
                'intensity array': ('64-bit float', 'no compression', mz, 2),
            }),
            self._spectrum('scan=1', 0, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
                'intensity array': ('64-bit float', 'no compression', mz, 2),
            }),
            self._spectrum('scan=2', 1, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
                'intensity array': ('64-bit float', 'no compression', mz, 2),
            }),
        ]
        manifest = spectrum_manifest(
            self._write(self._document(spectra, declared=2)))
        self.assertEqual(manifest['received_scan_count'], 2)
        self.assertEqual(manifest['missing_scans'], [])
        self.assertEqual(len(manifest['duplicate_ids']), 1)
        duplicate = manifest['duplicate_ids'][0]
        self.assertEqual(duplicate['id'], 'scan=1')
        self.assertEqual(duplicate['duplicate_count'], 2)
        self.assertFalse(duplicate['conflict'])

    def test_manifest_gzip(self):
        with open(self.path, 'rb') as handle:
            data = handle.read()
        path = self._write(gzip.compress(data))
        manifest = spectrum_manifest(path)
        self.assertTrue(manifest['source']['compressed'])
        self.assertEqual(manifest['complete_spectrum_count'], 2)
        self.assertTrue(manifest['complete'])

    def test_manifest_segment_starting_mid_document(self):
        with open(self.path, 'rb') as handle:
            data = handle.read()
        start = data.find(b'<spectrum index="1"')
        self.assertGreater(start, 0)
        manifest = spectrum_manifest(
            BytesIO(data[start:]), declared_count=2)
        self.assertEqual(manifest['declared_spectrum_count'], 2)
        self.assertEqual(manifest['received_scan_count'], 1)
        self.assertEqual(manifest['complete_spectrum_count'], 1)
        self.assertEqual(manifest['missing_scans'], [0])
        self.assertEqual(manifest['missing_scan_numbers'], [1])
        self.assertTrue(manifest['truncated'])
        self.assertEqual(
            [spectrum['id'] for spectrum in manifest['spectra']],
            ['controllerType=0 controllerNumber=1 scan=2'])

    def test_manifest_roundtrip(self):
        manifest = spectrum_manifest(self.path)
        text = dump_spectrum_manifest(manifest)
        restored = load_spectrum_manifest(BytesIO(text.encode('utf-8')))
        self.assertEqual(restored['summary'], manifest['summary'])

    def test_manifest_rejects_mid_spectrum_fragment(self):
        mz = self._payload([100.0, 200.0])
        complete = self._spectrum('scan=1', 0, {
            'm/z array': ('64-bit float', 'no compression', mz, 2),
            'intensity array': ('64-bit float', 'no compression', mz, 2),
        })
        fragment = (
            '<spectrum><binaryDataArrayList count="1">'
            '<binaryDataArray><cvParam cvRef="MS" accession="MS:1000514" '
            'name="m/z array"/><binary>{}</binary></binaryDataArray>'
            '</binaryDataArrayList></spectrum>'.format(mz))
        manifest = spectrum_manifest(self._write(self._document(
            [complete, fragment], declared=1)))
        self.assertEqual(manifest['received_scan_count'], 1)
        self.assertEqual(manifest['complete_spectrum_count'], 1)
        self.assertEqual(len(manifest['rejected_fragments']), 1)
        self.assertEqual(
            manifest['rejected_fragments'][0]['arrays'], ['m/z array'])
        self.assertEqual(manifest['missing_scans'], [])
        self.assertEqual(manifest['duplicate_ids'], [])

    def test_merge_adds_only_missing_spectra(self):
        mz = self._payload([100.0, 200.0])
        one = self._write(self._document([self._spectrum('scan=1', 0, {
            'm/z array': ('64-bit float', 'no compression', mz, 2),
            'intensity array': ('64-bit float', 'no compression', mz, 2),
        })], declared=2))
        two = self._write(self._document([self._spectrum('scan=2', 1, {
            'm/z array': ('64-bit float', 'no compression', mz, 2),
            'intensity array': ('64-bit float', 'no compression', mz, 2),
        })], declared=2))
        base = spectrum_manifest(one)
        updated = spectrum_manifest(two)
        merged = merge_spectrum_manifests(base, updated)
        self.assertEqual(merged['received_scan_count'], 2)
        self.assertEqual(merged['complete_spectrum_count'], 2)
        self.assertEqual([item['id'] for item in merged['merge']['added']], ['scan=2'])
        self.assertEqual(merged['merge']['added_count'], 1)
        self.assertTrue(merged['complete'])
        # Re-merging the same manifests must not grow the scan count.
        again = merge_spectrum_manifests(merged, updated)
        self.assertEqual(again['received_scan_count'], 2)
        self.assertEqual(again['merge']['added_count'], 0)
        self.assertEqual(again['merge']['unchanged_count'], 2)

    def test_merge_completes_partial_without_promoting(self):
        mz = self._payload([100.0, 200.0])
        intensity = self._payload([10.0, 20.0])
        partial = spectrum_manifest(self._write(self._document([
            self._spectrum('scan=1', 0, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
            })], declared=1)))
        complete = spectrum_manifest(self._write(self._document([
            self._spectrum('scan=1', 0, {
                'intensity array': ('64-bit float', 'no compression', intensity, 2),
            })], declared=1)))
        merged = merge_spectrum_manifests(partial, complete)
        self.assertEqual(merged['received_scan_count'], 1)
        self.assertEqual(merged['complete_spectrum_count'], 1)
        self.assertEqual(merged['merge']['added_count'], 0)
        self.assertEqual(merged['merge']['upgraded_count'], 1)
        self.assertEqual(
            merged['merge']['upgraded'][0]['missing_arrays_before'],
            ['intensity array'])
        spectrum = merged['spectra'][0]
        self.assertTrue(spectrum['complete'])
        self.assertEqual(
            set(spectrum['arrays']), {'m/z array', 'intensity array'})

    def test_merge_does_not_trust_stale_complete_flag(self):
        mz = self._payload([100.0, 200.0])
        manifest = spectrum_manifest(self._write(self._document([
            self._spectrum('scan=1', 0, {
                'm/z array': ('64-bit float', 'no compression', mz, 2),
            })], declared=1)))
        manifest['spectra'][0]['complete'] = True
        manifest['complete_spectrum_count'] = 1
        merged = merge_spectrum_manifests(manifest)
        self.assertEqual(merged['complete_spectrum_count'], 0)
        self.assertFalse(merged['spectra'][0]['complete'])
        self.assertEqual(
            merged['spectra'][0]['missing_arrays'], ['intensity array'])
        self.assertFalse(merged['complete'])


if __name__ == '__main__':
    unittest.main()
