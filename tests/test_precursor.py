import os
import unittest
import pyteomics
pyteomics.__path__ = [os.path.abspath(os.path.join(os.path.dirname(__file__), os.path.pardir, 'pyteomics'))]
from pyteomics import precursor
from pyteomics.precursor import PrecursorMap, PrecursorChain, build_precursor_map, isolation_window_bounds
from pyteomics.auxiliary import PyteomicsError

try:
    from psims.controlled_vocabulary.controlled_vocabulary import obo_cache
    obo_cache.cache_path = '.'
    obo_cache.enabled = True
    from pyteomics.mzml import MzML
    reason = None
except ImportError as err:
    MzML = None
    reason = err

SCAN = 'controllerType=0 controllerNumber=1 scan=%d'


def make_spectrum(scan, ms_level, precursors=None):
    spectrum = {'id': SCAN % scan, 'ms level': ms_level}
    if precursors is not None:
        spectrum['precursorList'] = {'count': len(precursors), 'precursor': precursors}
    return spectrum


def make_precursor(ref=None, target=None, lower=None, upper=None, ions=()):
    entry = {}
    if ref is not None:
        entry['spectrumRef'] = SCAN % ref if isinstance(ref, int) else ref
    if target is not None:
        window = {'isolation window target m/z': target}
        if lower is not None:
            window['isolation window lower offset'] = lower
        if upper is not None:
            window['isolation window upper offset'] = upper
        entry['isolationWindow'] = window
    if ions:
        entry['selectedIonList'] = {'count': len(ions), 'selectedIon': list(ions)}
    return entry


class IsolationWindowTest(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(
            isolation_window_bounds({'isolation window target m/z': 500.0,
                                     'isolation window lower offset': 1.5,
                                     'isolation window upper offset': 2.5}),
            (498.5, 502.5))

    def test_default_offsets(self):
        self.assertEqual(
            isolation_window_bounds({'isolation window target m/z': 500.0}), (500.0, 500.0))

    def test_empty(self):
        self.assertIsNone(isolation_window_bounds(None))
        self.assertIsNone(isolation_window_bounds({}))
        self.assertIsNone(isolation_window_bounds({'isolation window lower offset': 1.0}))


class PrecursorMapLogicTest(unittest.TestCase):
    def setUp(self):
        self.spectra = [
            make_spectrum(1, 1),
            make_spectrum(2, 2, [make_precursor(1, 445.34, 0.5, 0.5,
                                                [{'selected ion m/z': 445.34, 'charge state': 2}])]),
            make_spectrum(3, 2, [make_precursor(1, 521.2, 1.0, 1.0, [{'selected ion m/z': 521.2}]),
                                 make_precursor(1, 700.4, 0.8, 0.8, [{'selected ion m/z': 700.4}])]),
            make_spectrum(4, 3, [make_precursor(2, 445.0, 0.5, 0.5, [{'selected ion m/z': 445.0}])]),
            make_spectrum(5, 2, [make_precursor(None, 612.5, 0.4, 0.4, [{'selected ion m/z': 612.5}])]),
            make_spectrum(6, 2, [make_precursor(7, 500.0)]),
            make_spectrum(7, 2, [make_precursor(6, 501.0)]),
            make_spectrum(8, 2, [make_precursor(SCAN % 999, 750.0)]),
            make_spectrum(9, 2),
            make_spectrum(10, 2, [make_precursor(4, 445.0)]),
        ]
        self.pm = build_precursor_map(self.spectra)

    def test_links_cover_all_product_scans(self):
        self.assertEqual(self.pm.product_ids, tuple(SCAN % i for i in (2, 3, 4, 5, 6, 7, 8, 9, 10)))
        # scan=3 declares two precursor entries, scan=9 has none (placeholder link)
        self.assertEqual(len(self.pm.links), 10)

    def test_multiple_selected_ions_per_scan(self):
        links = self.pm.precursors_of(SCAN % 3)
        self.assertEqual(len(links), 2)
        targets = [link['isolation_window']['isolation window target m/z'] for link in links]
        self.assertEqual(targets, [521.2, 700.4])
        ions = self.pm.selected_ions_of(SCAN % 3)
        self.assertEqual([ion['selected ion m/z'] for ion in ions], [521.2, 700.4])

    def test_products_of(self):
        self.assertEqual([link['product_id'] for link in self.pm.products_of(SCAN % 1)],
                         [SCAN % 2, SCAN % 3, SCAN % 3])
        self.assertEqual([link['product_id'] for link in self.pm.products_of(SCAN % 2)], [SCAN % 4])

    def test_missing_references_not_backfilled(self):
        missing = {link['product_id'] for link in self.pm.missing_references}
        self.assertEqual(missing, {SCAN % 5, SCAN % 9})
        for link in self.pm.missing_references:
            self.assertIsNone(link['precursor_id'])
        # no nearest-scan backfilling: scan=5 and scan=9 must not appear under scan=1
        self.assertNotIn(SCAN % 5, [link['product_id'] for link in self.pm.products_of(SCAN % 1)])
        self.assertNotIn(SCAN % 9, [link['product_id'] for link in self.pm.products_of(SCAN % 1)])

    def test_reversed_hierarchy_flagged_with_declaration(self):
        reversed_anomalies = [a for a in self.pm.anomalies if a['type'] == 'reversed_hierarchy']
        by_product = {a['product_id']: a for a in reversed_anomalies}
        self.assertEqual(set(by_product), {SCAN % 6, SCAN % 7, SCAN % 10})
        self.assertEqual(by_product[SCAN % 10]['declared_reference'], SCAN % 4)
        self.assertEqual(by_product[SCAN % 10]['product_ms_level'], 2)
        self.assertEqual(by_product[SCAN % 10]['precursor_ms_level'], 3)
        # anomalous edges are kept, not pruned
        self.assertEqual(self.pm.precursors_of(SCAN % 10)[0]['precursor_id'], SCAN % 4)

    def test_cycle_flagged_with_declaration(self):
        cycles = [a for a in self.pm.anomalies if a['type'] == 'cycle']
        self.assertEqual(len(cycles), 1)
        cycle = cycles[0]
        self.assertEqual(cycle['chain'][0], cycle['chain'][-1])
        self.assertEqual(set(cycle['chain']), {SCAN % 6, SCAN % 7})
        self.assertIn(cycle['declared_reference'], (SCAN % 6, SCAN % 7))
        # edges of the cycle are still present in the map
        self.assertEqual(self.pm.precursors_of(SCAN % 6)[0]['precursor_id'], SCAN % 7)
        self.assertEqual(self.pm.precursors_of(SCAN % 7)[0]['precursor_id'], SCAN % 6)

    def test_unresolved_reference(self):
        unresolved = [a for a in self.pm.anomalies if a['type'] == 'unresolved_reference']
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['product_id'], SCAN % 8)
        self.assertEqual(unresolved[0]['declared_reference'], SCAN % 999)

    def test_covering(self):
        self.assertEqual([link['product_id'] for link in self.pm.covering(445.3)],
                         [SCAN % 2, SCAN % 4])
        self.assertEqual([link['product_id'] for link in self.pm.covering(700.0)], [SCAN % 3])
        self.assertEqual(self.pm.covering(100.0), [])

    def test_chain_complete(self):
        chain = self.pm.chain(SCAN % 4)
        self.assertIsInstance(chain, PrecursorChain)
        self.assertEqual(list(chain), [SCAN % 4, SCAN % 2, SCAN % 1])
        self.assertTrue(chain.complete)
        self.assertIsNone(chain.anomaly)

    def test_chain_cycle_not_reported_complete(self):
        chain = self.pm.chain(SCAN % 6)
        self.assertFalse(chain.complete)
        self.assertIsNotNone(chain.anomaly)
        self.assertEqual(chain.anomaly['type'], 'cycle')
        self.assertEqual(chain.anomaly['declared_reference'], SCAN % 6)
        self.assertEqual(chain[0], chain[-1])

    def test_chain_reversed_hierarchy_not_reported_complete(self):
        chain = self.pm.chain(SCAN % 10)
        self.assertEqual(list(chain), [SCAN % 10, SCAN % 4, SCAN % 2, SCAN % 1])
        self.assertFalse(chain.complete)
        self.assertEqual(chain.anomaly['type'], 'reversed_hierarchy')
        self.assertEqual(chain.anomaly['declared_reference'], SCAN % 4)

    def test_chain_unresolved_not_reported_complete(self):
        chain = self.pm.chain(SCAN % 8)
        self.assertEqual(list(chain), [SCAN % 8, SCAN % 999])
        self.assertFalse(chain.complete)
        self.assertEqual(chain.anomaly['type'], 'unresolved_reference')
        self.assertEqual(chain.anomaly['declared_reference'], SCAN % 999)

    def test_chain_missing_reference(self):
        chain = self.pm.chain(SCAN % 5)
        self.assertEqual(list(chain), [SCAN % 5])
        self.assertTrue(chain.complete)

    def test_container_protocol(self):
        self.assertEqual(len(self.pm), len(self.pm.links))
        self.assertEqual(list(iter(self.pm)), list(self.pm.links))
        self.assertIsInstance(self.pm, PrecursorMap)


class MzMLPrecursorMapTest(unittest.TestCase):
    maxDiff = None
    path = 'test.precursor.mzML'

    def setUp(self):
        if MzML is None:
            self.skipTest('psims is not available: %s' % (reason,))
        try:
            self.reader = MzML(self.path)
        except PyteomicsError as err:
            self.skipTest('mzML parsing is not available: %s' % (err,))
        self.pm = self.reader.precursor_map()

    def tearDown(self):
        if getattr(self, 'reader', None) is not None:
            self.reader.close()

    def test_links(self):
        self.assertEqual(self.pm.product_ids, tuple(SCAN % i for i in (2, 3, 4, 5, 6, 7, 8, 9, 10)))
        link = self.pm.precursors_of(SCAN % 2)[0]
        self.assertEqual(link['precursor_id'], SCAN % 1)
        self.assertEqual(link['product_ms_level'], 2)
        self.assertEqual(link['precursor_ms_level'], 1)
        self.assertEqual(link['isolation_window']['isolation window target m/z'], 445.34)
        self.assertEqual(link['selected_ions'][0]['selected ion m/z'], 445.34)

    def test_multiple_selected_ions(self):
        self.assertEqual(len(self.pm.precursors_of(SCAN % 3)), 2)
        ions = self.pm.selected_ions_of(SCAN % 3)
        self.assertEqual([ion['selected ion m/z'] for ion in ions], [521.2, 700.4])

    def test_missing_references(self):
        missing = {link['product_id'] for link in self.pm.missing_references}
        self.assertEqual(missing, {SCAN % 5, SCAN % 9})

    def test_anomalies(self):
        by_type = {}
        for anomaly in self.pm.anomalies:
            by_type.setdefault(anomaly['type'], []).append(anomaly)
        self.assertEqual({a['product_id'] for a in by_type['reversed_hierarchy']},
                         {SCAN % 6, SCAN % 7, SCAN % 10})
        self.assertEqual({a['product_id'] for a in by_type['unresolved_reference']}, {SCAN % 8})
        self.assertEqual(len(by_type['cycle']), 1)
        self.assertEqual(set(by_type['cycle'][0]['chain']), {SCAN % 6, SCAN % 7})

    def test_chain(self):
        chain = self.pm.chain(SCAN % 4)
        self.assertEqual(list(chain), [SCAN % 4, SCAN % 2, SCAN % 1])
        self.assertTrue(chain.complete)
        self.assertFalse(self.pm.chain(SCAN % 6).complete)


if __name__ == '__main__':
    unittest.main()
