"""
precursor - map MSn spectra to their precursor scans
=====================================================

Summary
-------

Data-dependent and multi-stage (MSn) acquisitions produce spectra that are
linked to each other: every MS2, MS3, ... scan is generated from one or more
precursor ions measured in an earlier scan. In mzML files these relations are
declared in the ``<precursorList>`` element of each spectrum, where every
``<precursor>`` entry may carry a ``spectrumRef`` attribute pointing to the
spectrum of the precursor scan, an ``<isolationWindow>`` element and a
``<selectedIonList>``.

This module builds an explicit map of these relations from parsed spectra
(e.g. an :py:class:`pyteomics.mzml.MzML` reader):

  :py:class:`PrecursorMap` - a container linking product scans to their
  precursor scans. Built with :py:func:`build_precursor_map` or
  :py:meth:`pyteomics.mzml.MzML.precursor_map`.

  :py:class:`PrecursorChain` - a list of spectrum IDs describing the declared
  lineage of a scan, with honesty flags for incomplete lineages.

Design notes
------------

* A product scan may declare **multiple** ``<precursor>`` entries (and each
  entry may list multiple selected ions). Every entry becomes a separate
  link, so one scan can be associated with several selected ions.
* Product scans whose precursor entries **lack** a ``spectrumRef`` are
  collected separately in :py:attr:`PrecursorMap.missing_references`.
  The map never guesses: missing references are **not** backfilled with the
  nearest preceding lower-level scan.
* Declared references that form **cycles** or violate the expected MS-level
  hierarchy (a precursor whose MS level is not lower than the product's)
  are reported in :py:attr:`PrecursorMap.anomalies` together with the
  original declared reference. Edges are never silently pruned, and
  :py:meth:`PrecursorMap.chain` never reports a truncated lineage as a
  complete chain.

-------------------------------------------------------------------------------
"""

#   Copyright 2026 Lev Levitsky
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

ISOLATION_WINDOW_TARGET = 'isolation window target m/z'
ISOLATION_WINDOW_LOWER = 'isolation window lower offset'
ISOLATION_WINDOW_UPPER = 'isolation window upper offset'

ANOMALY_CYCLE = 'cycle'
ANOMALY_REVERSED_HIERARCHY = 'reversed_hierarchy'
ANOMALY_UNRESOLVED_REFERENCE = 'unresolved_reference'


def _get_ms_level(spectrum):
    level = spectrum.get('ms level')
    try:
        return int(level)
    except (TypeError, ValueError):
        return None


def _iter_precursor_entries(spectrum):
    precursor_list = spectrum.get('precursorList') or {}
    for precursor in precursor_list.get('precursor') or []:
        yield precursor


def _get_selected_ions(precursor_entry):
    ion_list = precursor_entry.get('selectedIonList') or {}
    return list(ion_list.get('selectedIon') or [])


def isolation_window_bounds(isolation_window):
    """Compute the m/z interval covered by an isolation window.

    Parameters
    ----------
    isolation_window : dict or None
        Parsed ``<isolationWindow>`` element, as found in a precursor entry.

    Returns
    -------
    out : tuple of (float, float) or None
        ``(lower, upper)`` m/z bounds, or :py:const:`None` if no target m/z
        is given. Missing offsets default to 0.
    """
    if not isolation_window:
        return None
    target = isolation_window.get(ISOLATION_WINDOW_TARGET)
    if target is None:
        return None
    target = float(target)
    lower = float(isolation_window.get(ISOLATION_WINDOW_LOWER, 0) or 0)
    upper = float(isolation_window.get(ISOLATION_WINDOW_UPPER, 0) or 0)
    return (target - lower, target + upper)


class PrecursorChain(list):
    """A list of spectrum IDs describing the declared lineage of a product scan.

    The first element is the product scan itself; each following element is
    the declared precursor of the previous one.

    Attributes
    ----------
    complete : bool
        :py:const:`True` only if the lineage was followed up to a scan that
        declares no further precursor reference, without encountering any
        anomaly. If a cycle, a hierarchy violation or an unresolved reference
        is met, the traversal stops (or continues, for hierarchy violations)
        and `complete` is :py:const:`False` -- a pruned lineage is never
        reported as a complete chain.
    anomaly : dict or None
        The first anomaly encountered while building the chain, with the
        original declared reference, or :py:const:`None` if the chain is
        anomaly-free.
    """
    def __init__(self, ids=(), complete=True, anomaly=None):
        super(PrecursorChain, self).__init__(ids)
        self.complete = complete
        self.anomaly = anomaly

    def __repr__(self):
        return '%s(%s, complete=%r, anomaly=%r)' % (
            type(self).__name__, list.__repr__(self), self.complete, self.anomaly)


class PrecursorMap(object):
    """A map linking product (MS2, MS3, ...) scans to their precursor scans.

    Built from parsed spectra with :py:func:`build_precursor_map` or
    :py:meth:`pyteomics.mzml.MzML.precursor_map`.

    Attributes
    ----------
    links : tuple of dict
        One entry per declared ``<precursor>`` element. Each link is a dict
        with keys:

        - ``'product_id'``: ID of the product (MSn) spectrum;
        - ``'product_ms_level'``: MS level of the product spectrum;
        - ``'precursor_id'``: the declared ``spectrumRef``, or
          :py:const:`None` if not declared;
        - ``'precursor_ms_level'``: MS level of the referenced spectrum,
          or :py:const:`None` if unknown;
        - ``'isolation_window'``: parsed ``<isolationWindow>`` dict or
          :py:const:`None`;
        - ``'selected_ions'``: list of parsed ``<selectedIon>`` dicts.

        Product scans without any ``<precursor>`` entry are represented by a
        single link with no precursor information.
    missing_references : tuple of dict
        Links without a declared ``spectrumRef``. These are listed separately
        and are **not** resolved by guessing the nearest preceding scan.
    anomalies : tuple of dict
        Declared references that are cyclic, violate the MS-level hierarchy,
        or point to spectra not present in the file. Each anomaly is a dict
        with keys ``'type'`` (one of ``'cycle'``, ``'reversed_hierarchy'``,
        ``'unresolved_reference'``), ``'product_id'`` and
        ``'declared_reference'`` (the original declared ``spectrumRef``).
        Cycle anomalies also carry a ``'chain'`` of spectrum IDs closing the
        loop; hierarchy anomalies carry ``'product_ms_level'`` and
        ``'precursor_ms_level'``. Anomalous edges are kept in the map, not
        silently pruned.
    """
    def __init__(self, links, missing_references, anomalies):
        self.links = tuple(links)
        self.missing_references = tuple(missing_references)
        self.anomalies = tuple(anomalies)
        self._by_product = {}
        self._by_precursor = {}
        for link in self.links:
            self._by_product.setdefault(link['product_id'], []).append(link)
            if link['precursor_id'] is not None:
                self._by_precursor.setdefault(link['precursor_id'], []).append(link)

    def __len__(self):
        return len(self.links)

    def __iter__(self):
        return iter(self.links)

    @property
    def product_ids(self):
        """tuple of str : IDs of all product (MSn) scans, in file order."""
        return tuple(self._by_product)

    @property
    def precursor_ids(self):
        """tuple of str : IDs of all declared precursor scans, in first-use order."""
        return tuple(self._by_precursor)

    def precursors_of(self, product_id):
        """Get all precursor links declared by a product scan.

        Parameters
        ----------
        product_id : str
            ID of the product spectrum.

        Returns
        -------
        out : list of dict
            Link dicts (see :py:class:`PrecursorMap`). A scan with multiple
            precursor entries / selected ions yields multiple links.
        """
        return list(self._by_product.get(product_id, ()))

    def products_of(self, precursor_id):
        """Get all links that declare `precursor_id` as their precursor scan.

        Parameters
        ----------
        precursor_id : str
            ID of the precursor spectrum.

        Returns
        -------
        out : list of dict
        """
        return list(self._by_precursor.get(precursor_id, ()))

    def selected_ions_of(self, product_id):
        """Get a flat list of all selected ions declared by a product scan.

        Parameters
        ----------
        product_id : str
            ID of the product spectrum.

        Returns
        -------
        out : list of dict
            Parsed ``<selectedIon>`` dicts from all precursor entries of the
            scan.
        """
        ions = []
        for link in self._by_product.get(product_id, ()):
            ions.extend(link['selected_ions'])
        return ions

    def covering(self, mz):
        """Find links whose isolation window covers a given m/z.

        Parameters
        ----------
        mz : float
            The m/z value to look up.

        Returns
        -------
        out : list of dict
            Links whose isolation window contains `mz`. Links without an
            isolation window target m/z are ignored.
        """
        found = []
        for link in self.links:
            bounds = isolation_window_bounds(link['isolation_window'])
            if bounds is not None and bounds[0] <= mz <= bounds[1]:
                found.append(link)
        return found

    def chain(self, product_id):
        """Follow declared precursor references from a product scan upwards.

        Parameters
        ----------
        product_id : str
            ID of the product spectrum to start from.

        Returns
        -------
        out : PrecursorChain
            Spectrum IDs starting with `product_id` and continuing with the
            declared precursor of each scan (the first declared reference is
            followed when a scan declares several). If a cycle or an
            unresolved reference is met, the traversal stops; if a hierarchy
            violation is met, it is recorded but the traversal continues.
            In all these cases :py:attr:`PrecursorChain.complete` is
            :py:const:`False` and :py:attr:`PrecursorChain.anomaly` holds the
            original declaration -- a truncated lineage is never reported as
            a complete chain.
        """
        ids = [product_id]
        anomaly = None
        current = product_id
        while True:
            links = [link for link in self._by_product.get(current, ())
                     if link['precursor_id'] is not None]
            if not links:
                break
            link = links[0]
            ref = link['precursor_id']
            if ref in ids:
                ids.append(ref)
                anomaly = {
                    'type': ANOMALY_CYCLE,
                    'product_id': current,
                    'declared_reference': ref,
                    'chain': list(ids[ids.index(ref):]),
                }
                return PrecursorChain(ids, complete=False, anomaly=anomaly)
            if anomaly is None:
                anomaly = self._edge_anomaly(link)
            ids.append(ref)
            current = ref
        return PrecursorChain(ids, complete=anomaly is None, anomaly=anomaly)

    @staticmethod
    def _edge_anomaly(link):
        if link['precursor_id'] is None:
            return None
        if link['precursor_ms_level'] is None:
            return {
                'type': ANOMALY_UNRESOLVED_REFERENCE,
                'product_id': link['product_id'],
                'declared_reference': link['precursor_id'],
            }
        if link['precursor_ms_level'] >= link['product_ms_level']:
            return {
                'type': ANOMALY_REVERSED_HIERARCHY,
                'product_id': link['product_id'],
                'declared_reference': link['precursor_id'],
                'product_ms_level': link['product_ms_level'],
                'precursor_ms_level': link['precursor_ms_level'],
            }
        return None


def _detect_cycles(adjacency):
    """Find cycles in a graph of declared references.

    Parameters
    ----------
    adjacency : dict
        Maps a spectrum ID to a list of ``(declared_reference, link)`` tuples.

    Returns
    -------
    out : list of dict
        Cycle anomalies, each with the declared chain of spectrum IDs that
        closes the loop (first and last elements are equal).
    """
    anomalies = []
    reported = set()
    state = {}  # spectrum id -> 'visiting' (on the current path) | 'done'

    for start in adjacency:
        if state.get(start) is not None:
            continue
        stack = [start]
        state[start] = 'visiting'
        edge_iters = [iter(adjacency.get(start, []))]
        while stack:
            descended = False
            for ref, _link in edge_iters[-1]:
                if state.get(ref) == 'visiting':
                    cycle = stack[stack.index(ref):] + [ref]
                    key = frozenset(cycle)
                    if key not in reported:
                        reported.add(key)
                        anomalies.append({
                            'type': ANOMALY_CYCLE,
                            'product_id': stack[-1],
                            'declared_reference': ref,
                            'chain': cycle,
                        })
                elif state.get(ref) is None:
                    state[ref] = 'visiting'
                    stack.append(ref)
                    edge_iters.append(iter(adjacency.get(ref, [])))
                    descended = True
                    break
            if not descended:
                state[stack.pop()] = 'done'
                edge_iters.pop()
    return anomalies


def build_precursor_map(spectra):
    """Build a :py:class:`PrecursorMap` from parsed spectra.

    Parameters
    ----------
    spectra : iterable of dict
        Parsed spectra, e.g. an :py:class:`pyteomics.mzml.MzML` reader or a
        list of spectrum dicts. Spectra with MS level 2 and above are treated
        as product scans; their ``precursorList`` entries define the links.

    Returns
    -------
    out : PrecursorMap

    Notes
    -----
    * Every ``<precursor>`` entry of a product scan becomes a separate link,
      so a scan can be associated with multiple selected ions.
    * Precursor entries without a ``spectrumRef`` are collected in
      :py:attr:`PrecursorMap.missing_references`; they are never backfilled
      with the nearest preceding lower-level scan.
    * Cyclic and hierarchy-violating references are reported in
      :py:attr:`PrecursorMap.anomalies` with their original declarations;
      the corresponding edges are kept in the map.
    """
    spectra = list(spectra)
    ms_levels = {}
    for spectrum in spectra:
        spectrum_id = spectrum.get('id')
        if spectrum_id is not None:
            ms_levels[spectrum_id] = _get_ms_level(spectrum)

    links = []
    missing = []
    anomalies = []
    adjacency = {}

    for spectrum in spectra:
        product_id = spectrum.get('id')
        if product_id is None:
            continue
        product_level = _get_ms_level(spectrum)
        if product_level is None or product_level < 2:
            continue
        entries = list(_iter_precursor_entries(spectrum))
        if not entries:
            link = {
                'product_id': product_id,
                'product_ms_level': product_level,
                'precursor_id': None,
                'precursor_ms_level': None,
                'isolation_window': None,
                'selected_ions': [],
            }
            links.append(link)
            missing.append(link)
            continue
        for entry in entries:
            ref = entry.get('spectrumRef')
            precursor_level = ms_levels.get(ref) if ref is not None else None
            link = {
                'product_id': product_id,
                'product_ms_level': product_level,
                'precursor_id': ref,
                'precursor_ms_level': precursor_level,
                'isolation_window': entry.get('isolationWindow'),
                'selected_ions': _get_selected_ions(entry),
            }
            links.append(link)
            if ref is None:
                missing.append(link)
                continue
            adjacency.setdefault(product_id, []).append((ref, link))
            if ref not in ms_levels:
                anomalies.append({
                    'type': ANOMALY_UNRESOLVED_REFERENCE,
                    'product_id': product_id,
                    'declared_reference': ref,
                })
            elif precursor_level >= product_level:
                anomalies.append({
                    'type': ANOMALY_REVERSED_HIERARCHY,
                    'product_id': product_id,
                    'declared_reference': ref,
                    'product_ms_level': product_level,
                    'precursor_ms_level': precursor_level,
                })

    anomalies.extend(_detect_cycles(adjacency))
    return PrecursorMap(links, missing, anomalies)
