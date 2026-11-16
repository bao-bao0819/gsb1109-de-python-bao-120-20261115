import os
import base64
import zlib
import unittest
from io import BytesIO, StringIO

import numpy as np

import pyteomics
pyteomics.__path__ = [os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, 'pyteomics'))]

from pyteomics.mzmlwarehouse import (
    MzMLWarehouse, SpectrumArrayGroup, WarehousedArray,
    MissingArrayError, UndecodedArrayError, ArrayLengthConflictError,
    MZ_ARRAY, INTENSITY_ARRAY,
    STATUS_DECODED, STATUS_RAW_PRESERVED, STATUS_EMPTY)


def _b64(data):
    return base64.b64encode(data).decode('ascii')


def _array_xml(params, b64_text, extra_attrs=''):
    cvparams = ''.join(
        '<cvParam cvRef="MS" accession="MS:0000001" name="%s" value="%s"/>' % (name, value)
        for name, value in params)
    binary = '<binary>%s</binary>' % (b64_text or '')
    return '<binaryDataArray %s>%s%s</binaryDataArray>' % (extra_attrs, cvparams, binary)


def _mzml(arrays_xml, spectrum_id='scan=1', index=0, default_length=None):
    dal = ' defaultArrayLength="%d"' % default_length if default_length is not None else ''
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<mzML xmlns="http://psi.hupo.org/ms/mzml">'
        '<run id="r"><spectrumList count="1">'
        '<spectrum index="%d" id="%s"%s>'
        '<binaryDataArrayList count="%d">%s</binaryDataArrayList>'
        '</spectrum></spectrumList></run></mzML>'
    ) % (index, spectrum_id, dal, len(arrays_xml), ''.join(arrays_xml))


def _write_tmp(text):
    tmp = BytesIO(text.encode('utf-8'))
    return tmp


class MzMLWarehouseFileTest(unittest.TestCase):
    path = os.path.join(os.path.dirname(__file__), 'test.mzML')

    def setUp(self):
        self.wh = MzMLWarehouse(self.path)

    def test_groups_carry_spectrum_identity(self):
        self.assertEqual(len(self.wh), 2)
        first, second = self.wh.groups
        self.assertEqual(first.spectrum_id, 'controllerType=0 controllerNumber=1 scan=1')
        self.assertEqual(first.index, 0)
        self.assertEqual(second.index, 1)
        self.assertEqual(first.default_array_length, 19914)
        self.assertIs(self.wh['controllerType=0 controllerNumber=1 scan=2'], second)

    def test_arrays_decoded_against_reference(self):
        ref_mz = np.load(os.path.join(os.path.dirname(__file__), 'test_mzml_mz.npy'))
        ref_i = np.load(os.path.join(os.path.dirname(__file__), 'test_mzml_intensity.npy'))
        group = self.wh.groups[0]
        mz, intensity = group.pair()
        self.assertTrue(np.allclose(mz, ref_mz))
        self.assertTrue(np.allclose(intensity, ref_i))
        self.assertEqual(len(mz), 19914)

    def test_encoding_evidence_preserved(self):
        first, second = self.wh.groups
        ev = first[MZ_ARRAY].encoding
        self.assertEqual(ev.compression, 'no compression')
        self.assertEqual(ev.precision, '64-bit float')
        self.assertEqual(ev.byte_order, 'little')
        self.assertEqual(ev.dtype, np.dtype('<f8'))
        self.assertEqual(ev.encoded_length, len(first[MZ_ARRAY].raw))
        self.assertEqual(ev.declared_length, 19914)
        self.assertTrue(ev.decoded)
        self.assertEqual(ev.status, STATUS_DECODED)
        self.assertEqual(first[INTENSITY_ARRAY].encoding.precision, '32-bit float')
        self.assertEqual(second[INTENSITY_ARRAY].encoding.compression, 'zlib compression')
        # original payload is kept as evidence
        self.assertTrue(first[MZ_ARRAY].raw)

    def test_pairing_by_semantic_declaration(self):
        for group in self.wh:
            mz, intensity = group.pair()
            self.assertEqual(len(mz), len(intensity))
            self.assertIs(mz, group[MZ_ARRAY].data)
            self.assertIs(intensity, group[INTENSITY_ARRAY].data)


class SyntheticWarehouseTest(unittest.TestCase):
    def warehouse(self, arrays_xml, **kw):
        return MzMLWarehouse(_write_tmp(_mzml(arrays_xml, **kw)))

    def test_pairing_is_semantic_not_positional(self):
        mz = np.array([100.0, 200.0, 300.0], dtype='<f8')
        inten = np.array([10.0, 20.0, 30.0], dtype='<f4')
        # intensity array declared first on purpose
        arrays = [
            _array_xml([(INTENSITY_ARRAY, ''), ('32-bit float', ''), ('no compression', '')], _b64(inten.tobytes())),
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('no compression', '')], _b64(mz.tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        got_mz, got_inten = group.pair()
        self.assertTrue(np.allclose(got_mz, mz))
        self.assertTrue(np.allclose(got_inten, inten))

    def test_zlib_compressed_roundtrip(self):
        mz = np.arange(5, dtype='<f8')
        inten = np.arange(5, dtype='<f8') * 2
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('zlib compression', '')],
                       _b64(zlib.compress(mz.tobytes()))),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('zlib compression', '')],
                       _b64(zlib.compress(inten.tobytes()))),
        ]
        group = self.warehouse(arrays).groups[0]
        got_mz, got_inten = group.pair()
        self.assertTrue(np.array_equal(got_mz, mz))
        self.assertTrue(np.array_equal(got_inten, inten))
        self.assertEqual(group[MZ_ARRAY].encoding.compression, 'zlib compression')

    def test_big_endian_declaration(self):
        mz = np.array([1.5, 2.5], dtype='>f8')
        inten = np.array([3.5, 4.5], dtype='>f8')
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('no compression', ''), ('big endian', '')],
                       _b64(mz.tobytes())),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', ''), ('big endian', '')],
                       _b64(inten.tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        got_mz, got_inten = group.pair()
        self.assertTrue(np.array_equal(got_mz, [1.5, 2.5]))
        self.assertTrue(np.array_equal(got_inten, [3.5, 4.5]))
        self.assertEqual(group[MZ_ARRAY].encoding.byte_order, 'big')
        self.assertEqual(group[MZ_ARRAY].encoding.dtype, np.dtype('>f8'))

    def test_length_conflict_not_truncated(self):
        mz = np.array([100.0, 200.0, 300.0], dtype='<f8')
        inten = np.array([10.0, 20.0], dtype='<f8')
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('no compression', '')], _b64(mz.tobytes())),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', '')], _b64(inten.tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        with self.assertRaises(ArrayLengthConflictError) as ctx:
            group.pair()
        err = ctx.exception
        # both arrays exposed untruncated
        self.assertEqual(len(err.mz_array), 3)
        self.assertEqual(len(err.intensity_array), 2)
        self.assertIsNone(group.try_pair())
        # stored arrays are intact, not truncated to force a pair
        self.assertEqual(len(group[MZ_ARRAY].data), 3)
        self.assertEqual(len(group[INTENSITY_ARRAY].data), 2)

    def test_unknown_compression_preserves_raw(self):
        payload = _b64(np.arange(4, dtype='<f8').tobytes())
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('lzma compression', '')], payload),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', '')],
                       _b64(np.arange(4, dtype='<f8').tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        arr = group[MZ_ARRAY]
        self.assertIsNone(arr.data)
        self.assertEqual(arr.raw, payload)
        self.assertFalse(arr.decoded)
        self.assertEqual(arr.encoding.status, STATUS_RAW_PRESERVED)
        self.assertIn('lzma compression', arr.encoding.reason)
        self.assertEqual(arr.encoding.compression, 'lzma compression')
        with self.assertRaises(UndecodedArrayError):
            group.pair()

    def test_unknown_precision_preserves_raw(self):
        payload = _b64(np.arange(4, dtype='<f2').tobytes())
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('16-bit float', ''), ('no compression', '')], payload),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', '')],
                       _b64(np.arange(4, dtype='<f8').tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        arr = group[MZ_ARRAY]
        self.assertIsNone(arr.data)
        self.assertEqual(arr.raw, payload)
        self.assertEqual(arr.encoding.precision, '16-bit float')
        self.assertEqual(arr.encoding.status, STATUS_RAW_PRESERVED)

    def test_missing_precision_preserves_raw(self):
        payload = _b64(np.arange(4, dtype='<f8').tobytes())
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('no compression', '')], payload),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', '')],
                       _b64(np.arange(4, dtype='<f8').tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        self.assertIsNone(group[MZ_ARRAY].data)
        self.assertEqual(group[MZ_ARRAY].encoding.status, STATUS_RAW_PRESERVED)

    def test_empty_payload_not_zero_filled(self):
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('no compression', '')], ''),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', '')], ''),
        ]
        group = self.warehouse(arrays, default_length=5).groups[0]
        arr = group[MZ_ARRAY]
        # no seemingly valid zero array of the declared length
        self.assertIsNone(arr.data)
        self.assertEqual(arr.encoding.status, STATUS_EMPTY)
        self.assertEqual(arr.encoding.declared_length, 5)

    def test_empty_payload_without_declared_length(self):
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('no compression', '')], ''),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', '')], ''),
        ]
        group = self.warehouse(arrays).groups[0]
        mz, inten = group.pair()
        self.assertEqual(len(mz), 0)
        self.assertEqual(mz.dtype, np.dtype('<f8'))

    def test_missing_semantic_array(self):
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('no compression', '')],
                       _b64(np.arange(3, dtype='<f8').tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        with self.assertRaises(MissingArrayError):
            group.pair()

    def test_unnamed_array_kept_aside(self):
        arrays = [
            _array_xml([('64-bit float', ''), ('no compression', '')],
                       _b64(np.arange(3, dtype='<f8').tobytes())),
        ]
        group = self.warehouse(arrays).groups[0]
        self.assertEqual(len(group.unnamed), 1)
        self.assertEqual(group.unnamed[0].encoding.status, STATUS_DECODED)
        self.assertIn('<unnamed:0>', group.encoding_report())

    def test_accepts_file_object_and_path(self):
        arrays = [
            _array_xml([(MZ_ARRAY, ''), ('64-bit float', ''), ('no compression', '')],
                       _b64(np.arange(3, dtype='<f8').tobytes())),
            _array_xml([(INTENSITY_ARRAY, ''), ('64-bit float', ''), ('no compression', '')],
                       _b64(np.arange(3, dtype='<f8').tobytes())),
        ]
        text = _mzml(arrays)
        for source in [_write_tmp(text), StringIO(text)]:
            wh = MzMLWarehouse(source)
            self.assertEqual(len(wh), 1)
            mz, inten = wh.groups[0].pair()
            self.assertEqual(len(mz), 3)


if __name__ == '__main__':
    unittest.main()
