"""
Peak list matching.

Tools for one-to-one matching of peaks from two m/z lists (e.g. two MS2 peak
lists exported from the same acquisition at different precision) under
absolute and/or relative m/z error rules.
"""
from bisect import bisect_left, bisect_right
import heapq

from .structures import PyteomicsError


class Tolerance:
    """m/z matching tolerance combining an absolute (Da) and a relative
    (ppm) rule.

    A pair of m/z values is accepted only if *both* active rules are
    satisfied. The relative rule is symmetric:
    ``|mz2 - mz1| <= rtol_fraction * (mz1 + mz2) / 2``. This is equivalent to
    the usual ppm error computed against the mean of the two values, so
    matching does not depend on which list is passed first.

    Parameters
    ----------
    atol : float or None
        Absolute tolerance in Da. :py:const:`None` disables the absolute rule.
    rtol : float or None
        Relative tolerance. Interpreted according to `rtol_unit`.
        :py:const:`None` disables the relative rule.
    rtol_unit : str
        ``'ppm'`` (default) or ``'fraction'``.

    Examples
    --------
    >>> tol = Tolerance(atol=0.02, rtol=10.0)
    >>> tol.contains(100.0, 100.0005)
    True
    >>> Tolerance(atol=0.02).contains(100.0, 100.05)
    False
    """

    def __init__(self, atol=None, rtol=None, rtol_unit='ppm'):
        if atol is None and rtol is None:
            raise PyteomicsError(
                'At least one of `atol` and `rtol` must be specified.')
        self.atol = None if atol is None else float(atol)
        if self.atol is not None and self.atol < 0:
            raise PyteomicsError('`atol` must be non-negative.')
        self.rtol_unit = rtol_unit
        if rtol is None:
            self.rtol = None
            self.rtol_fraction = None
        else:
            self.rtol = float(rtol)
            if self.rtol < 0:
                raise PyteomicsError('`rtol` must be non-negative.')
            if rtol_unit == 'ppm':
                self.rtol_fraction = self.rtol * 1e-6
            elif rtol_unit == 'fraction':
                self.rtol_fraction = self.rtol
            else:
                raise PyteomicsError(
                    "`rtol_unit` must be 'ppm' or 'fraction', got: "
                    '{}'.format(rtol_unit))
            if self.rtol_fraction >= 1.0:
                raise PyteomicsError(
                    'Relative tolerance must be below 1 (100%).')

    @classmethod
    def ppm(cls, value):
        """Create a relative-only tolerance of `value` ppm."""
        return cls(atol=None, rtol=value, rtol_unit='ppm')

    @classmethod
    def dalton(cls, value):
        """Create an absolute-only tolerance of `value` Da."""
        return cls(atol=value, rtol=None)

    def bounds(self, mz):
        """Return the inclusive ``(low, high)`` m/z window around `mz`."""
        low, high = float('-inf'), float('inf')
        if self.atol is not None:
            low = max(low, mz - self.atol)
            high = min(high, mz + self.atol)
        if self.rtol_fraction is not None:
            f = self.rtol_fraction
            low = max(low, mz * (1.0 - f / 2.0) / (1.0 + f / 2.0))
            high = min(high, mz * (1.0 + f / 2.0) / (1.0 - f / 2.0))
        return low, high

    def contains(self, mz1, mz2):
        """Return :py:const:`True` if the two m/z values fit the tolerance."""
        low, high = self.bounds(mz1)
        return low <= mz2 <= high

    @staticmethod
    def absolute_error(mz1, mz2):
        """Signed absolute error: ``mz2 - mz1`` (Da)."""
        return mz2 - mz1

    @staticmethod
    def relative_error(mz1, mz2):
        """Signed relative error in ppm against the mean m/z value."""
        reference = (mz1 + mz2) / 2.0
        return (mz2 - mz1) / reference * 1e6


def _as_mz_list(peaks):
    """Extract a list of m/z floats from 1-D values, (m/z, intensity) pairs
    or a pyteomics spectrum dict."""
    if hasattr(peaks, 'keys') and 'm/z array' in peaks:
        peaks = peaks['m/z array']
    mz_values = []
    for peak in peaks:
        if isinstance(peak, (list, tuple)):
            mz = float(peak[0])
        else:
            mz = float(peak)
        if mz <= 0:
            raise PyteomicsError(
                'm/z values must be positive, got: {}'.format(mz))
        mz_values.append(mz)
    return mz_values


# Integer cost encoding so lexicographic optimization uses plain ints.
_ABS_SCALE = 10 ** 9    # Da -> sub-picodalton integer cost
_PPM_SCALE = 10 ** 3    # ppm -> 0.001 ppm integer cost
_COST_BASE = 10 ** 9    # secondary cost is always below this value


def _edge_cost(abs_error, ppm_error, objective):
    abs_cost = min(round(abs(abs_error) * _ABS_SCALE), _COST_BASE - 1)
    ppm_cost = min(round(abs(ppm_error) * _PPM_SCALE), _COST_BASE - 1)
    if objective == 'absolute_error':
        return abs_cost * _COST_BASE + ppm_cost
    elif objective == 'relative_error':
        return ppm_cost * _COST_BASE + abs_cost
    raise PyteomicsError(
        "`objective` must be 'absolute_error' or 'relative_error', "
        'got: {}'.format(objective))


def _min_cost_max_flow(n_a, n_b, candidates, objective):
    """Maximum-cardinality, minimum-cost one-to-one assignment.

    `candidates` is an iterable of ``(i, j, mz_a, mz_b)`` feasible pairs.
    Returns a dict ``{i: j}`` of matched indices.

    The flow network is source -> A peaks -> B peaks -> sink, every edge has
    unit capacity, so each peak is used at most once. Successive shortest
    augmenting paths (Dijkstra with node potentials) maximize the number of
    matches first and, among maximum matchings, minimize the total error cost.
    """
    n_nodes = n_a + n_b + 2
    source = 0
    sink = n_nodes - 1
    a_offset = 1
    b_offset = 1 + n_a
    graph = [[] for _ in range(n_nodes)]

    def add_edge(u, v, cost):
        graph[u].append([v, len(graph[v]), 1, cost])
        graph[v].append([u, len(graph[u]) - 1, 0, -cost])

    for i in range(n_a):
        add_edge(source, a_offset + i, 0)
    for j in range(n_b):
        add_edge(b_offset + j, sink, 0)
    # Deterministic edge order: by A index then B index.
    for i, j, mz_a, mz_b in sorted(candidates):
        cost = _edge_cost(
            Tolerance.absolute_error(mz_a, mz_b),
            Tolerance.relative_error(mz_a, mz_b),
            objective)
        add_edge(a_offset + i, b_offset + j, cost)

    potential = [0] * n_nodes
    while True:
        dist = [None] * n_nodes
        previous = [None] * n_nodes
        dist[source] = 0
        queue = [(0, source)]
        while queue:
            d, u = heapq.heappop(queue)
            if d != dist[u]:
                continue
            for edge_index, edge in enumerate(graph[u]):
                v, rev, capacity, cost = edge
                if capacity == 0:
                    continue
                reduced = cost + potential[u] - potential[v]
                nd = d + reduced
                if dist[v] is None or nd < dist[v]:
                    dist[v] = nd
                    previous[v] = (u, edge_index)
                    heapq.heappush(queue, (nd, v))
        if dist[sink] is None:
            break
        for v, d in enumerate(dist):
            if d is not None:
                potential[v] += d
        v = sink
        while v != source:
            u, edge_index = previous[v]
            edge = graph[u][edge_index]
            edge[2] -= 1
            graph[v][edge[1]][2] += 1
            v = u

    matches = {}
    for i in range(n_a):
        u = a_offset + i
        for edge in graph[u]:
            v, rev, capacity, cost = edge
            if b_offset <= v < b_offset + n_b and capacity == 0:
                matches[i] = v - b_offset
    return matches


def match_peaks(peaks_a, peaks_b, atol=None, rtol=10.0, rtol_unit='ppm',
                objective='absolute_error'):
    """Match peaks from two m/z lists one-to-one under error tolerances.

    The two lists may be in any order; results depend only on peak m/z values,
    not on their positions. Every peak is used at most once, so a single strong
    peak can never explain several peaks from the other list. Peaks without a
    feasible partner are retained in the unmatched lists.

    Matching maximizes the number of pairs first. If several maximum
    matchings exist, the one with the smallest total error is selected,
    measured according to `objective` (absolute m/z error by default;
    relative ppm error if requested).

    Parameters
    ----------
    peaks_a, peaks_b : array_like
        Peak m/z values. Accepted forms are:

        - a 1-D sequence of m/z floats;
        - a sequence of ``(m/z, intensity)`` pairs (only m/z is used);
        - a pyteomics spectrum dict containing an ``'m/z array'`` key.
    atol : float or None
        Absolute tolerance in Da. :py:const:`None` (default) disables the
        absolute rule.
    rtol : float or None
        Relative tolerance in ppm (or fractions, see `rtol_unit`).
        Default is 10 ppm. :py:const:`None` disables the relative rule.
        When both `atol` and `rtol` are given, both rules must hold.
    rtol_unit : str
        ``'ppm'`` (default) or ``'fraction'``.
    objective : str
        Tie-breaking error target among maximum-cardinality matchings:
        ``'absolute_error'`` (default) or ``'relative_error'``.

    Returns
    -------
    out : dict
        - ``'matches'``: list of ``(index_a, index_b, mz_a, mz_b,
          absolute_error, relative_error)`` tuples, where the absolute error
          is in Da (``mz_b - mz_a``) and the relative error is in ppm against
          the mean m/z;
        - ``'unmatched a'`` / ``'unmatched b'``: sorted lists of unmatched
          indices from each input;
        - ``'n matches'``: number of matched pairs.

    Examples
    --------
    >>> result = match_peaks([100.0, 200.0], [100.0001, 200.0002, 300.0],
    ...     atol=0.02, rtol=10.0)
    >>> result['n matches']
    2
    >>> result['unmatched b']
    [2]
    """
    tolerance = Tolerance(atol=atol, rtol=rtol, rtol_unit=rtol_unit)
    if objective not in ('absolute_error', 'relative_error'):
        raise PyteomicsError(
            "`objective` must be 'absolute_error' or 'relative_error', "
            'got: {}'.format(objective))

    mz_a = _as_mz_list(peaks_a)
    mz_b = _as_mz_list(peaks_b)
    n_a, n_b = len(mz_a), len(mz_b)

    order_b = sorted(range(n_b), key=lambda j: mz_b[j])
    sorted_b = [mz_b[j] for j in order_b]

    candidates = []
    for i, mz in enumerate(mz_a):
        low, high = tolerance.bounds(mz)
        left = bisect_left(sorted_b, low)
        right = bisect_right(sorted_b, high)
        for k in range(left, right):
            j = order_b[k]
            if tolerance.contains(mz, mz_b[j]):
                candidates.append((i, j, mz, mz_b[j]))

    assignment = _min_cost_max_flow(n_a, n_b, candidates, objective)

    matched_a = set(assignment)
    matched_b = set(assignment.values())
    matches = []
    for i in sorted(assignment):
        j = assignment[i]
        matches.append((
            i, j, mz_a[i], mz_b[j],
            Tolerance.absolute_error(mz_a[i], mz_b[j]),
            Tolerance.relative_error(mz_a[i], mz_b[j])))
    return {
        'matches': matches,
        'unmatched a': [i for i in range(n_a) if i not in matched_a],
        'unmatched b': [j for j in range(n_b) if j not in matched_b],
        'n matches': len(matches),
    }
