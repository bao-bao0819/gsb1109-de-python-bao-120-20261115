import unittest
import platform
import os
import pyteomics
import multiprocessing as mp
pyteomics.__path__ = [os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, 'pyteomics'))]
from pyteomics import auxiliary as aux
from pyteomics.auxiliary import unitfloat, TimeUnitError
from pyteomics import mzxml


class UtilTest(unittest.TestCase):
    def test_ensure_prefix(self):
        pairs = [
            ('file:///home/test/unimod.xml', 'file:///home/test/unimod.xml'),
            ('https://example.org/test/unimod.xml', 'https://example.org/test/unimod.xml'),
            ('ftp://example.org/test/unimod.xml', 'ftp://example.org/test/unimod.xml'),
            ('http://example.org/test/unimod.xml', 'http://example.org/test/unimod.xml'),
        ]
        pairs_windows = [
            ('C:/Data folder/unimod.xml', 'file:///C:/Data%20folder/unimod.xml'),
            ('file:///C:/Data folder/unimod.xml', 'file:///C:/Data folder/unimod.xml'),
        ]
        pairs_other = [('/home/test/unimod.xml', 'file:///home/test/unimod.xml'),]
        system = platform.system()
        print('Testing on', system)
        if system == 'Windows':
            pairs.extend(pairs_windows)
        else:
            pairs.extend(pairs_other)
        for inp, out in pairs:
            try:
                self.assertEqual(aux.ensure_url_prefix(inp), out)
            except Exception:
                print('Failed with:', inp, out)
                raise

    def test_start_method(self):
        self.assertNotEqual(aux.file_helpers._get_default_start_method(), 'fork')
        if mp.get_start_method(allow_none=False) != 'fork':
            self.assertEqual(mp.get_start_method(allow_none=False), aux.file_helpers._get_default_start_method())


class TimeUnitConversionTest(unittest.TestCase):
    def test_declared_unit_drives_conversion(self):
        seconds = unitfloat(42.05, 'second')
        minutes = unitfloat(5.8905, 'minute')
        self.assertAlmostEqual(float(aux.to_minutes(seconds)), 42.05 / 60.0)
        self.assertEqual(aux.to_minutes(seconds).unit_info, 'minute')
        self.assertAlmostEqual(
            float(aux.convert_time_unit(minutes, 'second')), 5.8905 * 60.0)
        self.assertEqual(aux.convert_time_unit(minutes, 'second').unit_info, 'second')
        self.assertEqual(
            float(aux.convert_time_unit(unitfloat(1.0, 'hour'), 'second')), 3600.0)
        self.assertEqual(
            float(aux.convert_time_unit(unitfloat(1000.0, 'millisecond'), 'second')), 1.0)

    def test_magnitude_is_not_used_to_guess_units(self):
        # A small number without a unit must not be assumed to be minutes,
        # nor a large one assumed to be seconds.
        for value in (unitfloat(0.004935, None), unitfloat(353.43, None), 353.43):
            with self.subTest(value=value):
                with self.assertRaises(TimeUnitError):
                    aux.to_minutes(value)

    def test_unknown_declared_and_target_units(self):
        with self.assertRaises(TimeUnitError):
            aux.convert_time_unit(unitfloat(3.0, 'furlong'), 'minute')
        with self.assertRaises(TimeUnitError):
            aux.convert_time_unit(unitfloat(3.0, 'second'), 'fortnight')

    def test_same_unit_is_not_rescaled(self):
        value = unitfloat(5.8905, 'minute')
        result = aux.convert_time_unit(value, 'minute')
        self.assertEqual(float(result), 5.8905)
        self.assertEqual(result.unit_info, 'minute')

    def test_report_preserves_declaration_and_order(self):
        with mzxml.MzXML('test.mzXML') as reader:
            report = aux.retention_time_report(reader, unit='second')
        self.assertEqual([row['order'] for row in report], [0, 1])
        self.assertEqual([row['scan_id'] for row in report], ['19', '20'])
        # original declaration kept verbatim: ISO durations are parsed to minutes
        self.assertEqual([row['declared_time'].unit_info for row in report],
                         ['minute', 'minute'])
        # unified exact unit in the converted column
        self.assertTrue(all(row['unit'] == 'second' for row in report))
        self.assertAlmostEqual(float(report[0]['time']), 353.43)
        self.assertAlmostEqual(float(report[1]['time']), 356.68)
        self.assertTrue(all(row['warning'] is None for row in report))

    def test_report_keeps_repeated_times_as_distinct_scans(self):
        chained = mzxml.chain('test.mzXML', 'test.mzXML')
        report = aux.retention_time_report(chained, unit='minute')
        self.assertEqual(len(report), 4)
        # both segments restart from their own zero-based ordering
        self.assertEqual([row['order'] for row in report], [0, 1, 0, 1])
        # the restart is not stitched into a cumulative, fake-continuous axis
        self.assertEqual(float(report[0]['time']), float(report[2]['time']))
        self.assertEqual(report[0]['scan_id'], report[2]['scan_id'])


if __name__ == '__main__':
    unittest.main()
