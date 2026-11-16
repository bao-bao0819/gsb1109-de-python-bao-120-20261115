import os
import random
import unittest

import pyteomics
pyteomics.__path__ = [os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, 'pyteomics'))]

from pyteomics.auxiliary import PyteomicsError
from pyteomics.auxiliary.peak_matching import Tolerance, match_peaks


def _canonical(result, mz_a, mz_b):
    """Order-independent view of a matching result for comparison."""
    pairs = tuple(sorted((round(ma, 12), round(mb, 12))
                         for _, _, ma, mb, _, _ in result['matches']))
    unmatched_a = tuple(sorted(round(mz_a[i], 12)
                               for i in result['unmatched a']))
    unmatched_b = tuple(sorted(round(mz_b[j], 12)
                               for j in result['unmatched b']))
    return pairs, unmatched_a, unmatched_b, result['n matches']


class ToleranceTest(unittest.TestCase):
    def test_absolute_only(self):
        tol = Tolerance.dalton(0.1)
        self.assertTrue(tol.contains(100.0, 100.05))
        self.assertTrue(tol.contains(100.0, 100.1))
        self.assertFalse(tol.contains(100.0, 100.1001))

    def test_relative_only(self):
        tol = Tolerance.ppm(10.0)
        # 5 ppm at m/z 100 -> 0.0005 Da
        self.assertTrue(tol.contains(100.0, 100.0005))
        # 20 ppm -> rejected
        self.assertFalse(tol.contains(100.0, 100.002))
        # same absolute window grows with m/z
        self.assertTrue(tol.contains(1000.0, 1000.005))

    def test_combined_rules_intersect(self):
        tol = Tolerance(atol=0.001, rtol=10.0)
        # within both: 5 ppm and 0.0005 Da
        self.assertTrue(tol.contains(100.0, 100.0005))
        # 0.0005 Da at m/z 10000 is 0.05 ppm: passes ppm, fails Da rule
        self.assertFalse(tol.contains(10000.0, 10000.0005 + 0.001))
        # 0.4 Da at m/z 1000 is 400 ppm: passes Da rule, fails ppm rule
        self.assertTrue(Tolerance(atol=0.5).contains(1000.0, 1000.4))
        self.assertFalse(Tolerance(rtol=10.0).contains(1000.0, 1000.4))
        self.assertFalse(tol.contains(1000.0, 1000.4))

    def test_symmetric_and_bounded(self):
        tol = Tolerance(atol=0.02, rtol=20.0)
        for mz1, mz2 in [(100.0, 100.001), (531.27, 531.2704), (10.0, 9.999)]:
            self.assertEqual(tol.contains(mz1, mz2), tol.contains(mz2, mz1))
        low, high = tol.bounds(100.0)
        self.assertTrue(tol.contains(100.0, low))
        self.assertTrue(tol.contains(100.0, high))
        self.assertFalse(tol.contains(100.0, low - 1e-12))
        self.assertFalse(tol.contains(100.0, high + 1e-12))

    def test_errors(self):
        self.assertAlmostEqual(Tolerance.absolute_error(100.0, 100.002), 0.002)
        self.assertAlmostEqual(Tolerance.relative_error(100.0, 100.001),
                               0.001 / 100.0005 * 1e6)

    def test_bad_arguments(self):
        with self.assertRaises(PyteomicsError):
            Tolerance()
        with self.assertRaises(PyteomicsError):
            Tolerance(atol=-0.1)
        with self.assertRaises(PyteomicsError):
            Tolerance(rtol=-1.0)
        with self.assertRaises(PyteomicsError):
            Tolerance(rtol=1.0, rtol_unit='nonsense')
        with self.assertRaises(PyteomicsError):
            Tolerance(rtol=200.0, rtol_unit='fraction')
        tol = Tolerance(rtol=1e-4, rtol_unit='fraction')
        self.assertTrue(tol.contains(100.0, 100.005))


class MatchPeaksTest(unittest.TestCase):
    def test_basic_match_and_unmatched(self):
        result = match_peaks([100.0, 200.0], [100.0001, 200.0002, 300.0],
                             atol=0.02, rtol=10.0)
        self.assertEqual(result['n matches'], 2)
        self.assertEqual(result['unmatched a'], [])
        self.assertEqual(result['unmatched b'], [2])
        matched_pairs = {(i, j) for i, j, *_ in result['matches']}
        self.assertEqual(matched_pairs, {(0, 0), (1, 1)})
        for i, j, ma, mb, abs_err, ppm_err in result['matches']:
            self.assertAlmostEqual(abs_err, mb - ma)
            self.assertAlmostEqual(ppm_err, (mb - ma) / ((ma + mb) / 2) * 1e6)

    def test_one_to_one_injection(self):
        # One B peak is within tolerance of two A peaks: match only one.
        result = match_peaks([100.0, 100.0001], [100.00005],
                             atol=0.001, rtol=None)
        self.assertEqual(result['n matches'], 1)
        self.assertEqual(len(result['unmatched a']), 1)
        self.assertEqual(result['unmatched b'], [])
        used_b = [j for _, j, *_ in result['matches']]
        self.assertEqual(len(used_b), len(set(used_b)))

        # And the other direction: one A peak cannot explain two B peaks.
        result = match_peaks([100.0], [100.00005, 100.00008],
                             atol=0.001, rtol=None)
        self.assertEqual(result['n matches'], 1)
        self.assertEqual(len(result['unmatched b']), 1)

    def test_maximum_cardinality_beats_greedy(self):
        # List-order greedy would pair a0 with b0 and strand a1; the optimal
        # one-to-one matching recovers both pairs.
        a = [100.0, 100.35]
        b = [100.2, 100.1]
        result = match_peaks(a, b, atol=0.2, rtol=None)
        self.assertEqual(result['n matches'], 2)
        assignment = {i: j for i, j, *_ in result['matches']}
        self.assertEqual(assignment, {0: 1, 1: 0})

    def test_order_independence(self):
        a = [100.0, 200.0, 300.0, 400.0]
        b = [400.0001, 99.9999, 250.0, 300.0002, 200.0003]
        reference = _canonical(
            match_peaks(a, b, atol=0.02, rtol=10.0), a, b)
        rng = random.Random(42)
        for _ in range(20):
            shuffled_b = b[:]
            rng.shuffle(shuffled_b)
            result = match_peaks(a, shuffled_b, atol=0.02, rtol=10.0)
            self.assertEqual(_canonical(result, a, shuffled_b), reference)
            shuffled_a = a[:]
            rng.shuffle(shuffled_a)
            result = match_peaks(shuffled_a, b, atol=0.02, rtol=10.0)
            self.assertEqual(_canonical(result, shuffled_a, b), reference)

    def test_deterministic_ties(self):
        a = [100.0, 100.001]
        b = [100.001, 100.002]
        first = match_peaks(a, b, atol=0.002, rtol=None)
        for _ in range(10):
            again = match_peaks(a, b, atol=0.002, rtol=None)
            self.assertEqual(first['matches'], again['matches'])

    def test_objective_tie_breaking(self):
        # Duplicate-m/z case admits two full matchings with different costs;
        # both error targets must select a globally optimal maximum matching.
        a = [100.0, 100.0]
        b = [100.0010, 100.0002]
        for objective in ('absolute_error', 'relative_error'):
            result = match_peaks(a, b, atol=0.002, rtol=None,
                                 objective=objective)
            self.assertEqual(result['n matches'], 2)
            assignment = {i: j for i, j, *_ in result['matches']}
            # Cheapest pairing pairs each A with the nearer distinct B peak.
            self.assertEqual(assignment[0], 1)
            self.assertEqual(assignment[1], 0)

    def test_reexport_at_different_precision(self):
        # Same acquisition re-exported with coarser m/z rounding.
        full = [127.0314, 185.1277, 244.9871, 500.2513, 771.4398]
        coarse = [round(mz, 2) for mz in full]
        result = match_peaks(full, coarse, atol=0.01, rtol=None)
        self.assertEqual(result['n matches'], 5)
        result_ppm = match_peaks(full, coarse, atol=None, rtol=50.0)
        self.assertEqual(result_ppm['n matches'], 5)

    def test_input_formats(self):
        a = [(100.0, 1e5), (200.0, 5e4)]
        b = {'m/z array': [100.0001, 200.0002, 300.0]}
        result = match_peaks(a, b, atol=0.02, rtol=10.0)
        self.assertEqual(result['n matches'], 2)
        self.assertEqual(result['unmatched b'], [2])

    def test_empty_lists(self):
        result = match_peaks([], [100.0], atol=0.1, rtol=None)
        self.assertEqual(result['n matches'], 0)
        self.assertEqual(result['unmatched a'], [])
        self.assertEqual(result['unmatched b'], [0])

    def test_bad_arguments(self):
        with self.assertRaises(PyteomicsError):
            match_peaks([100.0], [100.0], atol=None, rtol=None)
        with self.assertRaises(PyteomicsError):
            match_peaks([100.0], [100.0], atol=0.1, objective='nonsense')
        with self.assertRaises(PyteomicsError):
            match_peaks([0.0], [100.0], atol=0.1, rtol=None)

    def test_brute_force_optimum(self):
        rng = random.Random(7)
        for objective in ('absolute_error', 'relative_error'):
            for _ in range(200):
                n_a = rng.randint(1, 4)
                n_b = rng.randint(1, 4)
                mz_a = [100.0 + rng.random() * 2.0 for _ in range(n_a)]
                mz_b = [100.0 + rng.random() * 2.0 for _ in range(n_b)]
                tol = Tolerance(atol=rng.choice([0.05, 0.2, 0.6]),
                                rtol=None)
                feasible = {(i, j) for i in range(n_a) for j in range(n_b)
                            if tol.contains(mz_a[i], mz_b[j])}
                result = match_peaks(mz_a, mz_b, atol=tol.atol, rtol=None,
                                     objective=objective)

                matchings = []

                def enumerate_matchings(i, used_b, pairs):
                    if i == n_a:
                        matchings.append(list(pairs))
                        return
                    enumerate_matchings(i + 1, used_b, pairs)
                    for j in range(n_b):
                        if j not in used_b and (i, j) in feasible:
                            pairs.append((i, j))
                            used_b.add(j)
                            enumerate_matchings(i + 1, used_b, pairs)
                            used_b.remove(j)
                            pairs.pop()

                enumerate_matchings(0, set(), [])
                best_cardinality, best_cost = -1, None
                for pairs in matchings:
                    if objective == 'absolute_error':
                        cost = sum(abs(mz_b[j] - mz_a[i]) for i, j in pairs)
                    else:
                        cost = sum(abs(mz_b[j] - mz_a[i])
                                   / ((mz_a[i] + mz_b[j]) / 2)
                                   for i, j in pairs)
                    if (len(pairs) > best_cardinality or
                            (len(pairs) == best_cardinality and
                             (best_cost is None
                              or cost < best_cost - 1e-15))):
                        best_cardinality = len(pairs)
                        best_cost = cost

                self.assertEqual(result['n matches'], best_cardinality)
                if objective == 'absolute_error':
                    got_cost = sum(abs(ae) for *_, ae, _ in result['matches'])
                else:
                    got_cost = sum(abs(pe) / 1e6
                                   for *_, pe in result['matches'])
                self.assertAlmostEqual(got_cost, best_cost, places=12)


if __name__ == '__main__':
    unittest.main()
