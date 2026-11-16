"""
mzmlwarehouse - warehouse mzML binary arrays with spectrum identity
===================================================================

mzML binary arrays may be compressed and may differ in byte order and
precision. This module ingests them into a *warehouse* of array groups,
each carrying the identity of the spectrum it came from. m/z and
intensity arrays are paired by their semantic declarations (controlled
vocabulary terms such as "m/z array" and "intensity array"), never by
position, and the original encoding evidence is preserved for every
array.

Safeguards
----------

* When semantically paired arrays disagree in length, the conflict is
  reported via :py:class:`ArrayLengthConflictError`; the arrays are
  kept at their full declared lengths and are never truncated to force
  a pair.
* Arrays with unknown compression or precision declarations are stored
  as their original base64 payload; they are never decoded into
  seemingly valid zero-filled arrays.

Data access
-----------

  :py:class:`MzMLWarehouse` - ingest an mzML file and hold the
  resulting :py:class:`SpectrumArrayGroup` objects, iterable and
  accessible by spectrum ID.

Dependencies
------------

This module requires :py:mod:`lxml` and :py:mod:`numpy`. It does not
require :py:mod:`psims`.

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

import base64
import binascii
import os
import re
import warnings
from collections import namedtuple
from io import BytesIO

import numpy as np

from .auxiliary.utils import _default_compression_map
from .mzml import STANDARD_ARRAYS, NON_STANDARD_DATA_ARRAY
from .xml import etree, _local_name

MZ_ARRAY = 'm/z array'
INTENSITY_ARRAY = 'intensity array'

#: Precision declarations (CV term names) mapped to numpy dtypes.
_PRECISION_TYPES = {
    '32-bit float': np.dtype('f4'),
    '64-bit float': np.dtype('f8'),
    '32-bit integer': np.dtype('i4'),
    '64-bit integer': np.dtype('i8'),
    'null-terminated ASCII string': np.dtype('u1'),
}

#: Pattern matching declarations that look like a precision term but are
#: not in :data:`_PRECISION_TYPES` (e.g. "16-bit float").
_UNKNOWN_PRECISION_PATTERN = re.compile(
    r'(\d+-bit\s+(float|integer)|float|integer|string)$', re.IGNORECASE)

#: Byte order declarations mapped to numpy byte order characters.
_BYTE_ORDER_TERMS = {'little endian': '<', 'big endian': '>'}

#: The mzML specification mandates little-endian byte order.
_BYTE_ORDER_NAMES = {'<': 'little', '>': 'big'}

#: Decoding statuses recorded in :py:class:`EncodingEvidence`.
STATUS_DECODED = 'decoded'
STATUS_RAW_PRESERVED = 'raw-preserved'
STATUS_EMPTY = 'empty'


class WarehouseError(Exception):
    """Base class for mzML warehouse errors."""


class MissingArrayError(WarehouseError):
    """Raised when a semantically declared array is absent from a group."""

    def __init__(self, spectrum_id, missing):
        self.spectrum_id = spectrum_id
        self.missing = tuple(missing)
        super(MissingArrayError, self).__init__(
            'Spectrum %r is missing declared arrays: %s' % (
                spectrum_id, ', '.join(self.missing)))


class UndecodedArrayError(WarehouseError):
    """Raised when a pair is requested for arrays kept as raw payload."""

    def __init__(self, spectrum_id, arrays):
        self.spectrum_id = spectrum_id
        self.arrays = tuple(arrays)
        super(UndecodedArrayError, self).__init__(
            'Spectrum %r has undecoded arrays (original payload preserved): %s' % (
                spectrum_id, ', '.join(self.arrays)))


class ArrayLengthConflictError(WarehouseError):
    """Raised when semantically paired arrays disagree in length.

    The conflicting arrays are exposed untruncated as
    :attr:`mz_array` and :attr:`intensity_array`.
    """

    def __init__(self, spectrum_id, mz_array, intensity_array):
        self.spectrum_id = spectrum_id
        self.mz_array = mz_array
        self.intensity_array = intensity_array
        super(ArrayLengthConflictError, self).__init__(
            'Spectrum %r: m/z array length %d conflicts with intensity '
            'array length %d; arrays are kept untruncated' % (
                spectrum_id, len(mz_array), len(intensity_array)))


class EncodingEvidence(namedtuple('EncodingEvidence', (
        'compression', 'byte_order', 'precision', 'dtype',
        'encoded_length', 'declared_length', 'decoded', 'status', 'reason'))):
    """Original encoding evidence for a warehoused binary array.

    Attributes
    ----------
    compression : str or None
        The declared compression term, e.g. ``'zlib compression'``,
        ``'no compression'``, or the verbatim unknown term.
    byte_order : str
        ``'little'`` or ``'big'``. mzML mandates little-endian; a
        differing declaration is recorded as found.
    precision : str or None
        The declared precision term, e.g. ``'64-bit float'``, or the
        verbatim unknown term.
    dtype : numpy.dtype or None
        The dtype used for decoding, with explicit byte order.
        :py:const:`None` when the precision declaration is unknown.
    encoded_length : int
        Length of the original base64 payload, in characters.
    declared_length : int or None
        The array length declared by the spectrum or the array element,
        if any.
    decoded : bool
        Whether :py:attr:`WarehousedArray.data` holds a decoded array.
    status : str
        One of ``'decoded'``, ``'raw-preserved'`` or ``'empty'``.
    reason : str or None
        Why the payload was not decoded, when applicable.
    """
    __slots__ = ()

    def __new__(cls, compression=None, byte_order='little', precision=None,
                dtype=None, encoded_length=0, declared_length=None,
                decoded=False, status=STATUS_RAW_PRESERVED, reason=None):
        return super(EncodingEvidence, cls).__new__(
            cls, compression, byte_order, precision, dtype,
            encoded_length, declared_length, decoded, status, reason)


class WarehousedArray(object):
    """A single binary array stored in the warehouse.

    Attributes
    ----------
    name : str or None
        The semantic declaration of the array (e.g. ``'m/z array'``),
        or :py:const:`None` if no semantic name was declared.
    data : numpy.ndarray or None
        The decoded array, or :py:const:`None` when the payload was
        preserved undecoded. Never a fabricated zero-filled array.
    raw : str or None
        The original base64 payload, always preserved as evidence.
    encoding : EncodingEvidence
        The original encoding evidence.
    params : tuple
        The raw ``(name, accession, value)`` parameter triples declared
        on the originating ``<binaryDataArray>`` element.
    """

    __slots__ = ('name', 'data', 'raw', 'encoding', 'params')

    def __init__(self, name, data, raw, encoding, params=()):
        self.name = name
        self.data = data
        self.raw = raw
        self.encoding = encoding
        self.params = tuple(params)

    @property
    def decoded(self):
        """Whether the payload was decoded into :py:attr:`data`."""
        return self.encoding.decoded

    def __len__(self):
        return 0 if self.data is None else len(self.data)

    def __repr__(self):
        return 'WarehousedArray(%r, %s, %d values)' % (
            self.name, self.encoding.status, len(self))


class SpectrumArrayGroup(object):
    """The arrays of one spectrum, grouped under its identity.

    Attributes
    ----------
    spectrum_id : str or None
        The ``id`` attribute of the ``<spectrum>`` element.
    index : int or None
        The ``index`` attribute of the ``<spectrum>`` element.
    default_array_length : int or None
        The ``defaultArrayLength`` declared by the spectrum.
    arrays : dict
        Maps semantic array names to :py:class:`WarehousedArray`.
    unnamed : list
        Arrays without a semantic name declaration, in file order.
    """

    def __init__(self, spectrum_id=None, index=None, default_array_length=None):
        self.spectrum_id = spectrum_id
        self.index = index
        self.default_array_length = default_array_length
        self.arrays = {}
        self.unnamed = []

    def add(self, array):
        """Add a :py:class:`WarehousedArray` to the group."""
        if array.name is None:
            self.unnamed.append(array)
        else:
            if array.name in self.arrays:
                warnings.warn('Spectrum %r declares %r more than once; '
                              'keeping the first array' % (
                                  self.spectrum_id, array.name))
                return
            self.arrays[array.name] = array
        return array

    def __contains__(self, name):
        return name in self.arrays

    def __getitem__(self, name):
        return self.arrays[name]

    def __iter__(self):
        return iter(self.arrays.values())

    def __len__(self):
        return len(self.arrays) + len(self.unnamed)

    @property
    def mz(self):
        """The semantically declared m/z array, or :py:const:`None`."""
        return self.arrays.get(MZ_ARRAY)

    @property
    def intensity(self):
        """The semantically declared intensity array, or :py:const:`None`."""
        return self.arrays.get(INTENSITY_ARRAY)

    def pair(self):
        """Return the semantically paired ``(m/z, intensity)`` arrays.

        Returns
        -------
        (numpy.ndarray, numpy.ndarray)

        Raises
        ------
        MissingArrayError
            If either semantic array was not declared.
        UndecodedArrayError
            If either array was preserved as raw payload.
        ArrayLengthConflictError
            If the arrays disagree in length. Both arrays are kept
            untruncated on the exception; they are never truncated to
            force a pair.
        """
        mz = self.mz
        intensity = self.intensity
        missing = [name for name, arr in
                   ((MZ_ARRAY, mz), (INTENSITY_ARRAY, intensity)) if arr is None]
        if missing:
            raise MissingArrayError(self.spectrum_id, missing)
        undecoded = [arr.name for arr in (mz, intensity) if not arr.decoded]
        if undecoded:
            raise UndecodedArrayError(self.spectrum_id, undecoded)
        if len(mz.data) != len(intensity.data):
            raise ArrayLengthConflictError(self.spectrum_id, mz.data, intensity.data)
        return mz.data, intensity.data

    def try_pair(self):
        """Like :py:meth:`pair`, but return :py:const:`None` instead of
        raising when the pair cannot be produced."""
        try:
            return self.pair()
        except WarehouseError:
            return None

    def encoding_report(self):
        """Map every array name to its :py:class:`EncodingEvidence`."""
        report = {name: arr.encoding for name, arr in self.arrays.items()}
        for i, arr in enumerate(self.unnamed):
            report['<unnamed:%d>' % i] = arr.encoding
        return report

    def __repr__(self):
        return 'SpectrumArrayGroup(%r, %d arrays)' % (
            self.spectrum_id, len(self))


def _detect_array_name(params):
    """Determine the semantic name of an array from its parameters."""
    candidates = []
    for name, _accession, value in params:
        if name and name.endswith(' array') and not value \
                and name != NON_STANDARD_DATA_ARRAY:
            candidates.append(name)
    for name, _accession, value in params:
        if name == NON_STANDARD_DATA_ARRAY and value:
            return value
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        standard = [c for c in candidates if c in STANDARD_ARRAYS]
        warnings.warn('Multiple options for naming binary array: %r' % (candidates,))
        return max(standard or candidates, key=len)
    return None


def _select_compression(params):
    """Return ``(term, decompressor)`` or ``(term, None)`` for unknown terms."""
    declared = [name for name, _acc, _val in params
                if name and 'compression' in name]
    if not declared:
        return None, _default_compression_map['no compression']
    if len(declared) > 1:
        warnings.warn('Multiple options for binary array compression: %r' % (
            declared,))
    term = declared[0]
    return term, _default_compression_map.get(term)


def _select_precision(params):
    """Return ``(term, dtype)``; ``dtype`` is :py:const:`None` when the
    declaration is missing or unknown."""
    fallback = None
    for name, _acc, _val in params:
        if name in _PRECISION_TYPES:
            return name, _PRECISION_TYPES[name]
        if name and _UNKNOWN_PRECISION_PATTERN.search(name):
            fallback = fallback or name
    return fallback, None


def _select_byte_order(params):
    """Return the numpy byte order character; mzML defaults to little."""
    for name, _acc, _val in params:
        if name in _BYTE_ORDER_TERMS:
            return _BYTE_ORDER_TERMS[name]
    return '<'


def _decode_payload(name, raw, params, declared_length):
    """Build a :py:class:`WarehousedArray` from a raw base64 payload.

    Unknown encodings are preserved as the original payload; seemingly
    valid zero-filled arrays are never fabricated.
    """
    compression, decompressor = _select_compression(params)
    precision, base_dtype = _select_precision(params)
    order = _select_byte_order(params)
    byte_order = _BYTE_ORDER_NAMES[order]
    dtype = base_dtype.newbyteorder(order) if base_dtype is not None else None
    encoded_length = len(raw) if raw else 0

    def preserve(reason):
        evidence = EncodingEvidence(
            compression=compression, byte_order=byte_order, precision=precision,
            dtype=dtype, encoded_length=encoded_length,
            declared_length=declared_length, decoded=False,
            status=STATUS_RAW_PRESERVED, reason=reason)
        return WarehousedArray(name, None, raw, evidence, params)

    if decompressor is None:
        return preserve('unknown compression: %r' % (compression,))
    if dtype is None:
        if precision is None:
            return preserve('no precision declaration')
        return preserve('unknown precision: %r' % (precision,))

    if not raw or not raw.strip():
        if declared_length:
            # Never fabricate a seemingly valid zero-filled array.
            evidence = EncodingEvidence(
                compression=compression, byte_order=byte_order,
                precision=precision, dtype=dtype,
                encoded_length=encoded_length,
                declared_length=declared_length, decoded=False,
                status=STATUS_EMPTY,
                reason='empty payload with declared length %d' % declared_length)
            return WarehousedArray(name, None, raw, evidence, params)
        evidence = EncodingEvidence(
            compression=compression, byte_order=byte_order,
            precision=precision, dtype=dtype,
            encoded_length=encoded_length, declared_length=declared_length,
            decoded=True, status=STATUS_EMPTY)
        return WarehousedArray(name, np.array([], dtype=dtype), raw, evidence, params)

    try:
        binary = base64.b64decode(raw.encode('ascii'))
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return preserve('invalid base64 payload')
    try:
        binary = decompressor(binary)
    except Exception as e:
        return preserve('decompression failed: %s' % e)
    array = np.frombuffer(bytearray(binary), dtype=dtype)

    if declared_length is not None and len(array) != declared_length:
        warnings.warn(
            'Decoded array %r has length %d but length %d was declared' % (
                name, len(array), declared_length))
    evidence = EncodingEvidence(
        compression=compression, byte_order=byte_order, precision=precision,
        dtype=dtype, encoded_length=encoded_length,
        declared_length=declared_length, decoded=True, status=STATUS_DECODED)
    return WarehousedArray(name, array, raw, evidence, params)


def _read_spectrum_element(elem):
    """Build a :py:class:`SpectrumArrayGroup` from a ``<spectrum>`` element."""
    index = elem.get('index')
    default_length = elem.get('defaultArrayLength')
    group = SpectrumArrayGroup(
        spectrum_id=elem.get('id'),
        index=int(index) if index is not None else None,
        default_array_length=int(default_length) if default_length is not None else None)
    for child in elem.iter():
        if _local_name(child) != 'binaryDataArray':
            continue
        params = []
        binary_text = None
        array_length = child.get('arrayLength')
        for node in child:
            tag = _local_name(node)
            if tag in ('cvParam', 'userParam'):
                params.append((node.get('name'), node.get('accession'),
                               node.get('value')))
            elif tag == 'binary':
                binary_text = node.text
        declared = int(array_length) if array_length is not None \
            else group.default_array_length
        name = _detect_array_name(params)
        group.add(_decode_payload(name, binary_text, params, declared))
    return group


def _as_binary_source(source):
    """Pass paths through; re-wrap text streams as byte streams."""
    if isinstance(source, (str, bytes, os.PathLike)):
        return source
    data = source.read()
    if isinstance(data, str):
        data = data.encode('utf-8')
    return BytesIO(data)


def _iter_spectrum_elements(source):
    """Iteratively parse `source` and yield ``<spectrum>`` elements."""
    context = etree.iterparse(_as_binary_source(source), events=('end',),
                              recover=True, remove_comments=True, huge_tree=True)
    for _event, elem in context:
        if _local_name(elem) == 'spectrum':
            yield elem
            while elem.getprevious() is not None:
                del elem.getparent()[0]
            elem.clear()


class MzMLWarehouse(object):
    """A warehouse of mzML binary array groups with spectrum identity.

    Parameters
    ----------
    source : str or file, optional
        A path to an mzML file or a file-like object. If given,
        :py:meth:`ingest` is called immediately.

    Attributes
    ----------
    groups : list of SpectrumArrayGroup
        The warehoused array groups, in file order.
    """

    def __init__(self, source=None):
        self.groups = []
        self._by_id = {}
        if source is not None:
            self.ingest(source)

    def ingest(self, source):
        """Ingest all spectra from `source` into the warehouse.

        Parameters
        ----------
        source : str or file
            A path to an mzML file or a file-like object.

        Returns
        -------
        self
        """
        for elem in _iter_spectrum_elements(source):
            self.add_group(_read_spectrum_element(elem))
        return self

    def add_group(self, group):
        """Add a :py:class:`SpectrumArrayGroup` to the warehouse."""
        self.groups.append(group)
        if group.spectrum_id is not None:
            if group.spectrum_id in self._by_id:
                warnings.warn('Duplicate spectrum id: %r' % (group.spectrum_id,))
            else:
                self._by_id[group.spectrum_id] = group
        return group

    def __iter__(self):
        return iter(self.groups)

    def __len__(self):
        return len(self.groups)

    def __getitem__(self, key):
        """Look a group up by spectrum ID or by position."""
        if isinstance(key, str):
            return self._by_id[key]
        return self.groups[key]

    def encoding_report(self):
        """Map every spectrum ID to its group's encoding report."""
        return {group.spectrum_id: group.encoding_report()
                for group in self.groups}

    def __repr__(self):
        return 'MzMLWarehouse(%d spectra)' % len(self)
