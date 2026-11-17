"""
mzml - reader for mass spectrometry data in mzML format
=======================================================

Summary
-------

mzML is a standard rich XML-format for raw mass spectrometry data storage.
Please refer to `psidev.info <http://www.psidev.info/index.php?q=node/257>`_
for the detailed specification of the format and structure of mzML files.

This module provides a minimalistic way to extract information from mzML
files. You can use the old functional interface (:py:func:`read`) or the new
object-oriented interface (:py:class:`MzML` or :py:class:`PreIndexedMzML`)
to iterate over entries in ``<spectrum>`` elements.
:py:class:`MzML` and :py:class:`PreIndexedMzML` also support direct indexing
with spectrum IDs.

Data access
-----------

  :py:class:`MzML` - a class representing a single mzML file.
  Other data access functions use this class internally.

  :py:class:`PreIndexedMzML` - a class representing a single mzML file.
  Uses byte offsets listed at the end of the file for quick access to spectrum elements.

  :py:func:`read` - iterate through spectra in mzML file. Data from a
  single spectrum are converted to a human-readable dict. Spectra themselves are
  stored under 'm/z array' and 'intensity array' keys.

  :py:func:`chain` - read multiple mzML files at once.

  :py:func:`chain.from_iterable` - read multiple files at once, using an
  iterable of files.

Spectrum manifests
------------------

  :py:func:`spectrum_manifest` - inventory the spectra in a file or an
  uploaded segment. Each record reports the actual decoded length and a
  checksum for every binary array, so missing arrays and truncated payloads
  are distinguishable from complete scans.

  :py:func:`merge_spectrum_manifests` - merge manifests from segmented uploads,
  adding only spectra that were not already present and completing partial
  ones.

  :py:func:`dump_spectrum_manifest`, :py:func:`load_spectrum_manifest` and
  :py:func:`write_spectrum_manifest` - JSON serialization helpers.

Controlled Vocabularies and Caching
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
mzML relies on controlled vocabularies to describe its contents extensibly.
Every :py:class:`MzML` needs a copy of PSI-MS CV, which it handles using the :py:mod:`psims` library.
If you want to save time when creating instances of :py:class:`MzML`, consider enabling the :py:mod:`psims` cache.
See `psims documentation <https://mobiusklein.github.io/psims/docs/build/html/controlled_vocabulary/controlled_vocabulary.html#caching>`_
on how to enable and configure the cache (alternatively, you can handle CV creation yourself and pass a pre-created instance
using the `cv` parameter to :py:class:`MzML`).
See also
`Controlled Vocabulary Terms <../data.html#controlled-vocabulary-terms-in-structured-data>`_
for more details on how they are used.

Handling Time Units and Other Qualified Quantities
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
mzML contains information which may be described as using a variety of different time units.
See `Unit Handling <../data.html#unit-handling>`_ for more information.

Deprecated functions
--------------------

  :py:func:`version_info` - get version information about the mzML file.
  You can just read the corresponding attribute of the :py:class:`MzML` object.

  :py:func:`iterfind` - iterate over elements in an mzML file.
  You can just call the corresponding method of the :py:class:`MzML` object.

Dependencies
------------

This module requires :py:mod:`lxml`, :py:mod:`numpy` and :py:mod:`psims`.

-------------------------------------------------------------------------------
"""

#   Copyright 2012 Anton Goloborodko, Lev Levitsky
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
import copy
import hashlib
import io
import json
import os
import re
import zlib
import warnings
import numpy as np
from . import xml, auxiliary as aux, _schema_defaults
from .xml import etree

NON_STANDARD_DATA_ARRAY = 'non-standard data array'

_ARRAY_ACCESSION_NAMES = {
    'MS:1000514': 'm/z array',
    'MS:1000515': 'intensity array',
    'MS:1000516': 'charge array',
    'MS:1000517': 'signal to noise array',
    'MS:1000595': 'time array',
    'MS:1000617': 'wavelength array',
    'MS:1000820': 'flow rate array',
    'MS:1000821': 'pressure array',
    'MS:1000822': 'temperature array',
    'MS:1001143': 'mean charge array',
    'MS:1002073': 'resolution array',
    'MS:1002074': 'baseline array',
    'MS:1002075': 'noise array',
}

_ARRAY_DTYPE_NAMES = {
    '32-bit float': 'float32',
    '64-bit float': 'float64',
    '32-bit integer': 'int32',
    '64-bit integer': 'int64',
    'null-terminated ASCII string': 'uint8',
}

_ARRAY_DTYPE_ACCESSIONS = {
    'MS:1000521': 'float32',
    'MS:1000523': 'float64',
    'MS:1000519': 'int32',
    'MS:1000522': 'int64',
    'MS:1001479': 'uint8',
}

_MANIFEST_FORMAT = 'mzML-spectrum-manifest'
_MANIFEST_VERSION = 1
_DEFAULT_REQUIRED_ARRAYS = ('m/z array', 'intensity array')

STANDARD_ARRAYS = set([
    'm/z array',
    'intensity array',
    'charge array',
    'signal to noise array',
    'time array',
    'wavelength array',
    'flow rate array',
    'pressure array',
    'temperature array',
    'mean charge array',
    'resolution array',
    'baseline array',
    'noise array',
    'sampled noise m/z array',
    'sampled noise intensity array',
    'sampled noise baseline array',
    'ion mobility array',
    'deconvoluted ion mobility drift time array',
    'deconvoluted inverse reduced ion mobility array',
    'deconvoluted ion mobility array',
    'raw ion mobility drift time array',
    'raw inverse reduced ion mobility array',
    'raw ion mobility array',
    'mean inverse reduced ion mobility array',
    'mean ion mobility array',
    'mean ion mobility drift time array',
    'mass array',
    'scanning quadrupole position lower bound m/z array',
    'scanning quadrupole position upper bound m/z array',
])


class MzML(aux.BinaryArrayConversionMixin, xml.CVParamParser, aux.TimeOrderedIndexedReaderMixin, xml.MultiProcessingXML, xml.IndexSavingXML):
    """Parser class for mzML files."""
    file_format = 'mzML'
    _root_element = 'mzML'
    _default_schema = _schema_defaults._mzml_schema_defaults
    _default_version = '1.1.0'
    _default_iter_tag = 'spectrum'
    _structures_to_flatten = {'binaryDataArrayList', 'referenceableParamGroupRef'}
    _indexed_tags = {'spectrum', 'chromatogram'}

    def __init__(self, *args, **kwargs):
        self.decode_binary = kwargs.pop('decode_binary', True)
        self._referenceable_param_groups = {}
        super(MzML, self).__init__(*args, **kwargs)

    def __getstate__(self):
        state = super(MzML, self).__getstate__()
        state['decode_binary'] = self.decode_binary
        return state

    def __setstate__(self, state):
        super(MzML, self).__setstate__(state)
        self.decode_binary = state['decode_binary']

    def _handle_referenceable_param_group(self, param_group_ref, **kwargs):
        ref_name = param_group_ref.attrib['ref']
        if ref_name not in self._referenceable_param_groups:
            params = self._referenceable_param_groups[ref_name] = self._retrieve_param_group(ref_name)
            return params
        return self._referenceable_param_groups[ref_name]

    @xml._keepstate
    def _retrieve_param_group(self, ref_name):
        group = self.get_by_id(ref_name)
        group.pop("id", None)
        return [xml._XMLParam(k, v, None) for k, v in group.items()]

    def _detect_array_name(self, info):
        """Determine what the appropriate name for this
        array is by inspecting the available param-based
        keys.

        Parameters
        ----------
        info : dict
            The collapsed binary tag plus
            associated *Param  data

        Returns
        -------
        out : str
            The name for this array entry
        """
        # If this is a non-standard array, we hope the userParams
        # will conform to the same array suffix pattern.
        is_non_standard = False

        # Accumulate possible name candidates
        candidates = []
        for k in info:
            if k.endswith(' array') and not info[k]:
                if NON_STANDARD_DATA_ARRAY == k:
                    is_non_standard = True
                else:
                    candidates.append(k)
        # A non-standard data array term key might have the name for the data array
        # as the value.
        nonstandard_name = info.get(NON_STANDARD_DATA_ARRAY)
        if nonstandard_name:
            return nonstandard_name
        if isinstance(info.get('name'), list):
            for val in info['name']:
                if val.endswith(' array'):
                    if NON_STANDARD_DATA_ARRAY == val:
                        is_non_standard = True
                    else:
                        candidates.append(val)
        # Name candidate resolution
        n_candidates = len(candidates)
        # Easy case, exactly one name given
        if n_candidates == 1:
            return candidates[0]
        # We are missing information, but at least
        # if we know the array is non-standard we
        # can report it as such. Otherwise fall back
        # to "binary". This fallback signals special
        # behavior elsewhere.
        if n_candidates == 0:
            invalid = {"encodedLength", "dataProcessingRef", "arrayLength",
                       "binary"}
            for k in info:
                if k in invalid:
                    continue
                candidates.append(k)
            if len(candidates) == 0:
                if is_non_standard:
                    return NON_STANDARD_DATA_ARRAY
                warnings.warn("No options for non-standard data array")
                return "binary"
            else:
                warnings.warn(
                    "Multiple options for naming binary array after no valid name found: %r" % candidates)
                return max(candidates, key=len)
        # Multiple choices means we need to make a decision which could
        # mask data from the user. This should never happen but stay safe.
        # There are multiple options to choose from. There is no way to
        # make a good choice here. We first prefer the standardized
        # arrays before falling back to just guessing.
        else:
            candidates = set(candidates)
            # Maybe we just have a repeated term?
            if len(candidates) == 1:
                return next(iter(candidates))
            warnings.warn(
                "Multiple options for naming binary array: %r" % candidates)
            standard_options = candidates & STANDARD_ARRAYS
            if standard_options:
                return max(standard_options, key=len)
            return max(candidates, key=len)

    def _determine_array_dtype(self, info):
        dtype = None
        types = {'32-bit float': np.float32, '64-bit float': np.float64,
                 '32-bit integer': np.int32, '64-bit integer': np.int64,
                 'null-terminated ASCII string': np.uint8}
        for t, code in types.items():
            if t in info:
                dtype = code
                del info[t]
                break
        # sometimes it's under 'name'
        else:
            if 'name' in info:
                for t, code in types.items():
                    if t in info['name']:
                        dtype = code
                        info['name'].remove(t)
                        break
        return dtype

    def _determine_compression(self, info):
        known_compression_types = set(self.compression_type_map)
        found_compression_types = known_compression_types & set(info)
        if found_compression_types:
            found_compression_types = tuple(found_compression_types)
            if len(found_compression_types) == 1:
                del info[found_compression_types[0]]
                return found_compression_types[0]
            warnings.warn("Multiple options for binary array compression: %r" % (
                found_compression_types,))
            return found_compression_types[0]
        elif "name" in info:
            found_compression_types = known_compression_types & set(info['name'])
            if found_compression_types:
                found_compression_types = tuple(found_compression_types)
                if len(found_compression_types) == 1:
                    del info['name'][found_compression_types[0]]
                    return found_compression_types[0]
                else:
                    warnings.warn("Multiple options for binary array compression: %r" % (
                        found_compression_types,))
                    return found_compression_types[0]
        else:
            return 'no compression'

    def _handle_binary(self, info, **kwargs):
        """Special handling when processing and flattening
        a <binary> tag and its sibling *Param tags.

        Parameters
        ----------
        info : dict
            Unprocessed binary array data and metadata

        Returns
        -------
        out : dict
            The processed and flattened data array and metadata
        """
        dtype = self._determine_array_dtype(info)
        compressed = self._determine_compression(info)
        name = self._detect_array_name(info)
        binary = info.pop('binary')
        if not self.decode_binary:
            info[name] = self._make_record(binary, compressed, dtype, name)
            return info

        if binary:
            array = self.decode_data_array(binary, compressed, dtype)
        else:
            array = np.array([], dtype=dtype)

        if name == 'binary':
            info[name] = self._convert_array(None, array)
        else:
            info = {name: self._convert_array(name, array)}
        return info

    def _get_info_smart(self, element, **kw):
        name = xml._local_name(element)
        kwargs = dict(kw)
        default_rec = (name not in {'indexedmzML', 'mzML'})
        rec = kwargs.pop('recursive', None)
        if rec is None:
            rec = default_rec
        info = self._get_info(element, recursive=rec, **kwargs)

        if 'binary' in info and isinstance(info, dict):
            info = self._handle_binary(info, **kwargs)

        if 'binaryDataArray' in info and isinstance(info, dict):
            for array in info.pop('binaryDataArray'):
                info.update(array)
        intkeys = {'ms level'}
        for k in intkeys:
            if k in info:
                try:
                    info[k] = int(info[k])
                except (ValueError, TypeError):
                    pass
        return info

    def _retrieve_refs(self, info, **kwargs):
        """Retrieves and embeds the data for each attribute in `info` that
        ends in _ref. Removes the id attribute from `info`"""
        for k, v in dict(info).items():
            if k == 'ref':
                by_id = self.get_by_id(v, retrieve_refs=True)
                if by_id is None:
                    warnings.warn('Ignoring unresolved reference: ' + v)
                else:
                    info.update(by_id)
                    del info[k]
                    info.pop('id', None)

    @staticmethod
    def _get_time(scan):
        return scan['scanList']['scan'][0]['scan start time']


def read(source, read_schema=False, iterative=True, use_index=False, dtype=None, huge_tree=False, decode_binary=True, cv=None):
    """Parse `source` and iterate through spectra.

    Parameters
    ----------
    source : str or file
        A path to a target mzML file or the file object itself.

    read_schema : bool, optional
        If :py:const:`True`, attempt to extract information from the XML schema
        mentioned in the mzML header. Otherwise, use default parameters.
        Not recommended without Internet connection or
        if you don't like to get the related warnings.

    iterative : bool, optional
        Defines whether iterative parsing should be used. It helps reduce
        memory usage at almost the same parsing speed. Default is
        :py:const:`True`.

    use_index : bool, optional
        Defines whether an index of byte offsets needs to be created for
        spectrum elements. Default is :py:const:`False`.

    dtype : type or dict, optional
        dtype to convert arrays to, one for both m/z and intensity arrays or one for each key.
        If :py:class:`dict`, keys should be 'm/z array' and 'intensity array'.

    decode_binary : bool, optional
        Defines whether binary data should be decoded and included in the output
        (under "m/z array", "intensity array", etc.).
        Default is :py:const:`True`.

    huge_tree : bool, optional
        This option is passed to the `lxml` parser and defines whether
        security checks for XML tree depth and node size should be disabled.
        Default is :py:const:`False`.
        Enable this option for trusted files to avoid XMLSyntaxError exceptions
        (e.g. `XMLSyntaxError: xmlSAX2Characters: huge text node`).

    cv : psims.controlled_vocabulary.controlled_vocabulary.ControlledVocabulary, optional
        An instance of PSI-MS CV. If provided, the parser will use it for type checking.
        Otherwise, a CV will be loaded from the Internet or from cache, if it is configured.

        .. seealso ::
            See `psims documentation <https://mobiusklein.github.io/psims/docs/build/html/controlled_vocabulary/controlled_vocabulary.html#caching>`_
            about cache configuration.


    Returns
    -------
    out : iterator
       An iterator over the dicts with spectrum properties.
    """

    return MzML(source, read_schema=read_schema, iterative=iterative,
                use_index=use_index, dtype=dtype, huge_tree=huge_tree,
                decode_binary=decode_binary)


def iterfind(source, path, **kwargs):
    """Parse `source` and yield info on elements with specified local
    name or by specified "XPath".

    .. note:: This function is provided for backward compatibility only.
        If you do multiple :py:func:`iterfind` calls on one file, you should
        create an :py:class:`MzML` object and use its
        :py:meth:`!iterfind` method.

    Parameters
    ----------
    source : str or file
        File name or file-like object.

    path : str
        Element name or XPath-like expression. Only local names separated
        with slashes are accepted. An asterisk (`*`) means any element.
        You can specify a single condition in the end, such as:
        ``"/path/to/element[some_value>1.5]"``
        Note: you can do much more powerful filtering using plain Python.
        The path can be absolute or "free". Please don't specify
        namespaces.

    recursive : bool, optional
        If :py:const:`False`, subelements will not be processed when
        extracting info from elements. Default is :py:const:`True`.

    iterative : bool, optional
        Specifies whether iterative XML parsing should be used. Iterative
        parsing significantly reduces memory usage and may be just a little
        slower. When `retrieve_refs` is :py:const:`True`, however, it is
        highly recommended to disable iterative parsing if possible.
        Default value is :py:const:`True`.

    read_schema : bool, optional
        If :py:const:`True`, attempt to extract information from the XML schema
        mentioned in the mzIdentML header. Otherwise, use default
        parameters. Not recommended without Internet connection or
        if you don't like to get the related warnings.

    decode_binary : bool, optional
        Defines whether binary data should be decoded and included in the output
        (under "m/z array", "intensity array", etc.).
        Default is :py:const:`True`.

    Returns
    -------
    out : iterator
    """
    return MzML(source, **kwargs).iterfind(path, **kwargs)


version_info = xml._make_version_info(MzML)

# chain = aux._make_chain(read, 'read')

chain = aux.ChainBase._make_chain(MzML)


def _as_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_SCAN_ID_PATTERNS = (
    re.compile(r'(?:^|[;,_\s])scan=(\d+)', re.IGNORECASE),
    re.compile(r'(?:^|[;,_\s])spectrum=(\d+)', re.IGNORECASE),
    re.compile(r'(?:^|[;,_\s])index=(\d+)', re.IGNORECASE),
    re.compile(r'(?:^|[;,_\s])cycle=(\d+)', re.IGNORECASE),
    re.compile(r'(?:^|[;,_\s])scanId=(\d+)', re.IGNORECASE),
    re.compile(r'(?:^|[;,_\s])id=(\d+)', re.IGNORECASE),
    re.compile(r'(\d+)$'),
)


def _scan_number_from_id(spectrum_id):
    if not spectrum_id:
        return None
    for pattern in _SCAN_ID_PATTERNS:
        match = pattern.search(spectrum_id)
        if match:
            return int(match.group(1))
    return None


def _scan_number(record):
    """Return the scan number used for missing-scan accounting.

    An mzML ``index`` attribute is the position in ``spectrumList``. When the
    ID carries a scan number, that is the more meaningful value for users.
    """
    if record.get('scan_number') is not None:
        return record['scan_number']
    if record.get('index') is not None:
        return record['index']
    return None


def _is_fragment(record):
    """A chunk may start in the middle of a spectrum.

    Such a fragment has no ``id``, ``index`` or ``defaultArrayLength`` and is
    not evidence that a scan was received.
    """
    return (
        record.get('id') is None and
        record.get('index') is None and
        record.get('defaultArrayLength') is None
    )


def _scan_number_offset(records):
    """Infer ``scan_number - index`` when it is consistent across records."""
    offsets = set()
    for record in records:
        number = _scan_number(record)
        index = record.get('index')
        if number is not None and index is not None:
            offsets.add(number - index)
    if len(offsets) == 1:
        return offsets.pop()
    return None


def _scan_domain(records, declared_count):
    """Infer missing and unexpected positions from the mzML ``index`` values.

    mzML ``index`` attributes are zero-based positions in ``spectrumList``;
    ``spectrumList/@count`` declares how many positions must exist. This makes
    the index the reliable identity for missing-scan accounting even when IDs
    embed scan numbers from a different numbering scheme. Without a declared
    count there is no basis for calling a scan missing, so nothing is reported.

    Returns ``(missing, unexpected, offset)``. ``offset`` is the consistent
    difference between the scan number in the ID and the index, or ``None``.
    """
    if declared_count is None:
        return [], [], None
    observed = {
        record['index'] for record in records if record.get('index') is not None
    }
    expected = set(range(declared_count))
    return (sorted(expected - observed), sorted(observed - expected),
            _scan_number_offset(records))


def _child_by_local_name(element, name):
    for child in element:
        if xml._local_name(child) == name:
            return child
    return None


def _collect_params(element, referenceable_param_groups):
    """Collect ``cvParam``/``userParam`` information from an element.

    Arrays may describe their type and name either inline or through a
    ``referenceableParamGroupRef``. This function expands both forms so that
    array metadata is available without constructing a full :class:`MzML`
    parser (and therefore without requiring an MS controlled vocabulary).
    """
    params = []
    for child in element:
        name = xml._local_name(child)
        if name == 'cvParam':
            params.append({
                'name': child.get('name'),
                'accession': child.get('accession'),
                'value': child.get('value', ''),
            })
        elif name == 'userParam':
            params.append({
                'name': child.get('name'),
                'accession': None,
                'value': child.get('value', ''),
            })
        elif name == 'referenceableParamGroupRef':
            ref = child.get('ref')
            params.extend(referenceable_param_groups.get(ref, ()))
    return params


def _detect_array_name(params):
    """Return an array name and the candidate names that produced it."""
    candidates = []
    non_standard = None
    for param in params:
        accession = param.get('accession')
        name = param.get('name')
        value = param.get('value')
        if accession in _ARRAY_ACCESSION_NAMES:
            candidates.append(_ARRAY_ACCESSION_NAMES[accession])
        elif name == NON_STANDARD_DATA_ARRAY:
            if value:
                non_standard = value
        elif name and name.endswith(' array'):
            candidates.append(name)
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    if not unique:
        return non_standard, []
    if len(unique) == 1:
        return unique[0], unique
    standard = [name for name in unique if name in STANDARD_ARRAYS]
    if len(standard) == 1:
        return standard[0], unique
    return max(unique, key=len), unique


def _detect_array_dtype(params):
    for param in params:
        accession = param.get('accession')
        name = param.get('name')
        if accession in _ARRAY_DTYPE_ACCESSIONS:
            return _ARRAY_DTYPE_ACCESSIONS[accession]
        if name in _ARRAY_DTYPE_NAMES:
            return _ARRAY_DTYPE_NAMES[name]
    return None


def _detect_array_compression(params):
    for param in params:
        accession = param.get('accession')
        name = param.get('name')
        if accession == 'MS:1000576' or name == 'no compression':
            return 'no compression'
        if accession == 'MS:1000574' or name == 'zlib compression':
            return 'zlib compression'
        if accession == 'MS:1002312' or name == 'MS-Numpress linear prediction compression':
            return 'MS-Numpress linear prediction compression'
        if accession == 'MS:1002313' or name == 'MS-Numpress positive integer compression':
            return 'MS-Numpress positive integer compression'
        if accession == 'MS:1002314' or name == 'MS-Numpress short logged float compression':
            return 'MS-Numpress short logged float compression'
        if accession == 'MS:1002746' or name == (
                'MS-Numpress linear prediction compression followed by zlib compression'):
            return 'MS-Numpress linear prediction compression followed by zlib compression'
        if accession == 'MS:1002747' or name == (
                'MS-Numpress positive integer compression followed by zlib compression'):
            return 'MS-Numpress positive integer compression followed by zlib compression'
        if accession == 'MS:1002748' or name == (
                'MS-Numpress short logged float compression followed by zlib compression'):
            return 'MS-Numpress short logged float compression followed by zlib compression'
    return 'no compression'


def _measure_binary_array(text, compression, dtype):
    """Decode an array just far enough to determine its length and checksum.

    Returns
    -------
    tuple
        ``(length, sha1_hex, error)``. ``length`` is ``None`` when the payload
        cannot be decoded. Decoding the whole payload (rather than trusting
        ``encodedLength``) is what makes the length check meaningful for
        truncated uploads.
    """
    if not text:
        return 0, None, None
    try:
        raw = base64.b64decode(text.encode('ascii'))
    except Exception as exc:
        return None, None, 'invalid base64: {}'.format(exc)
    if 'zlib' in compression:
        try:
            raw = zlib.decompress(raw)
        except Exception as exc:
            return None, None, 'invalid zlib payload: {}'.format(exc)
    if compression.startswith('MS-Numpress'):
        try:
            import pynumpress
        except ImportError:
            return None, None, 'pynumpress is required to decode MS-Numpress arrays'
        try:
            source = np.frombuffer(raw, dtype=np.uint8)
            if 'linear' in compression:
                decoded = pynumpress.decode_linear(source)
            elif 'positive' in compression:
                decoded = pynumpress.decode_pic(source)
            else:
                decoded = pynumpress.decode_slof(source)
            decoded = np.asarray(decoded)
            return int(decoded.size), hashlib.sha1(decoded.tobytes()).hexdigest(), None
        except Exception as exc:
            return None, None, 'invalid MS-Numpress payload: {}'.format(exc)
    if dtype is None:
        if not raw:
            return 0, hashlib.sha1(b'').hexdigest(), None
        return None, None, 'array data type is not declared'
    itemsize = np.dtype(dtype).itemsize
    if len(raw) % itemsize:
        return None, None, 'payload size {} is not a multiple of {}'.format(len(raw), itemsize)
    return len(raw) // itemsize, hashlib.sha1(raw).hexdigest(), None


def _parse_binary_array(element, referenceable_param_groups):
    params = _collect_params(element, referenceable_param_groups)
    name, candidates = _detect_array_name(params)
    dtype = _detect_array_dtype(params)
    compression = _detect_array_compression(params)
    binary = _child_by_local_name(element, 'binary')
    text = ''
    if binary is not None and binary.text:
        text = binary.text.strip()
    array_length = _as_int(element.get('arrayLength'))
    encoded_length = _as_int(element.get('encodedLength'))
    length, digest, error = _measure_binary_array(text, compression, dtype)
    payload_length = len(''.join(text.split()))
    if error is None and encoded_length is not None:
        # mzML defines encodedLength as the base64 string length, but some
        # writers report the raw byte count instead. Accept either so that a
        # valid array is not rejected over a metadata convention.
        decoded_length = None
        if payload_length:
            try:
                decoded_length = len(base64.b64decode(text.encode('ascii')))
            except Exception:
                decoded_length = None
        if encoded_length not in (payload_length, decoded_length):
            error = 'encodedLength mismatch: declared {}, payload has {}'.format(
                encoded_length, payload_length)
    verified = bool(
        error is None and length is not None and
        (array_length is None or length == array_length))
    return {
        'name': name,
        'length': length,
        'dtype': dtype,
        'compression': compression,
        'arrayLength': array_length,
        'encodedLength': encoded_length,
        'sha1': digest,
        'verified': verified,
        'error': error,
        'name_candidates': candidates,
    }


def _parse_spectrum_element(element, referenceable_param_groups, position):
    spectrum_id = element.get('id')
    index = _as_int(element.get('index'))
    default_length = _as_int(element.get('defaultArrayLength'))
    structural_errors = []

    if not spectrum_id:
        structural_errors.append('missing id')
    if default_length is None:
        structural_errors.append('missing defaultArrayLength')

    ms_level = None
    params = _collect_params(element, referenceable_param_groups)
    for param in params:
        if param.get('accession') == 'MS:1000511' or param.get('name') == 'ms level':
            ms_level = _as_int(param.get('value'))
            break

    arrays = {}
    array_elements = []
    binary_data_array_list = _child_by_local_name(element, 'binaryDataArrayList')
    if binary_data_array_list is None:
        structural_errors.append('missing binaryDataArrayList')
    else:
        for child in binary_data_array_list:
            if xml._local_name(child) == 'binaryDataArray':
                array_elements.append(child)
        declared_array_count = _as_int(binary_data_array_list.get('count'))
        if declared_array_count is not None and declared_array_count != len(array_elements):
            structural_errors.append(
                'binaryDataArrayList count {} does not match {} arrays'.format(
                    declared_array_count, len(array_elements)))
    for array_element in array_elements:
        array = _parse_binary_array(array_element, referenceable_param_groups)
        name = array['name']
        if not name:
            name = 'unidentified array {}'.format(len(arrays))
            structural_errors.append('array {} has no recognizable name'.format(len(arrays)))
        if name in arrays:
            structural_errors.append('duplicate array name: {}'.format(name))
        arrays[name] = array

    record = {
        'id': spectrum_id,
        'index': index,
        'position': position,
        'scan_number': _scan_number_from_id(spectrum_id),
        'ms level': ms_level,
        'defaultArrayLength': default_length,
        'arrays': arrays,
        'structural_errors': structural_errors,
        'fragment': _is_fragment({
            'id': spectrum_id,
            'index': index,
            'defaultArrayLength': default_length,
        }),
    }
    return record


def _recompute_spectrum(record, required_arrays):
    """Recompute completeness from array evidence.

    This deliberately ignores a pre-existing ``complete`` flag. A manifest
    assembled from an earlier upload may contain partial spectra; merging such
    records must not promote them to complete without new arrays.
    """
    default_length = record.get('defaultArrayLength')
    arrays = record.get('arrays')
    missing = []
    issues = {}
    if not isinstance(arrays, dict):
        arrays = {}
        record['array_evidence'] = False
    else:
        record['array_evidence'] = True
    for name in required_arrays:
        array = arrays.get(name)
        if not isinstance(array, dict):
            missing.append(name)
            issues[name] = 'absent'
            continue
        length = array.get('length')
        if length is None:
            missing.append(name)
            issues[name] = 'unverifiable' + (
                ': ' + array['error'] if array.get('error') else '')
            continue
        if default_length is not None and length != default_length:
            missing.append(name)
            issues[name] = 'length mismatch: {} != {}'.format(length, default_length)
            continue
        if default_length is None and length == 0:
            missing.append(name)
            issues[name] = 'empty array without a declared length'
            continue
        if array.get('verified') is False:
            missing.append(name)
            issues[name] = 'array length could not be verified' + (
                ': ' + array['error'] if array.get('error') else '')
    structural_errors = list(record.get('structural_errors') or ())
    record['missing_arrays'] = missing
    record['array_issues'] = issues
    record['arrays_complete'] = not missing
    record['structurally_complete'] = not structural_errors
    record['complete'] = not missing and not structural_errors
    fingerprint = hashlib.sha1()
    for name in sorted(arrays):
        array = arrays[name]
        fingerprint.update(name.encode('utf-8'))
        fingerprint.update(str(array.get('length')).encode('ascii'))
        fingerprint.update(str(array.get('dtype')).encode('ascii'))
        fingerprint.update(str(array.get('sha1')).encode('ascii'))
    record['fingerprint'] = fingerprint.hexdigest()
    return record


def _merge_group_records(records, required_arrays):
    """Combine repeated observations of the same spectrum ID.

    Duplicate IDs are common when segments overlap. Their arrays may be
    complementary (for example, one segment contains the m/z array and the
    next contains the intensity array), so arrays are unioned by name rather
    than choosing one record wholesale. Array lengths must agree with the
    declared length, and conflicting complete records are flagged by the
    duplicate report.
    """
    merged = copy.deepcopy(_select_best_record(records, required_arrays))
    merged.pop('_manifest_source', None)
    merged_arrays = dict(merged.get('arrays') or {})
    lengths = {
        record.get('defaultArrayLength') for record in records
        if record.get('defaultArrayLength') is not None
    }
    if len(lengths) > 1:
        merged['structural_errors'] = list(merged.get('structural_errors') or ())
        merged['structural_errors'].append(
            'duplicate spectrum records disagree on defaultArrayLength: {}'.format(
                sorted(lengths)))
    for record in records:
        for name, array in (record.get('arrays') or {}).items():
            if not isinstance(array, dict):
                continue
            candidate = merged_arrays.get(name)
            if candidate is None:
                merged_arrays[name] = copy.deepcopy(array)
                continue
            candidate_verified = bool(candidate.get('verified'))
            array_verified = bool(array.get('verified'))
            if array_verified and not candidate_verified:
                merged_arrays[name] = copy.deepcopy(array)
    merged['arrays'] = merged_arrays
    if merged.get('id') is None or merged.get('index') is None:
        for record in records:
            if merged.get('id') is None and record.get('id') is not None:
                merged['id'] = record['id']
                merged['scan_number'] = record.get('scan_number')
            if merged.get('index') is None and record.get('index') is not None:
                merged['index'] = record['index']
    return _recompute_spectrum(merged, required_arrays)


def _spectrum_key(record):
    if record.get('id'):
        return ('id', record['id'])
    if record.get('index') is not None:
        return ('index', record['index'])
    return ('position', record.get('position'))


def _select_best_record(records, required_arrays):
    def score(record):
        verified = 0
        total_length = 0
        for name in required_arrays:
            array = (record.get('arrays') or {}).get(name)
            if isinstance(array, dict):
                if array.get('verified'):
                    verified += 1
                if array.get('length') is not None:
                    total_length += array['length']
        return (
            1 if record.get('complete') else 0,
            1 if record.get('arrays_complete') else 0,
            verified,
            len(record.get('arrays') or {}),
            total_length,
            -(record.get('position') if record.get('position') is not None else 10 ** 12),
        )
    return max(records, key=score)


def _record_conflict(records):
    """Return True when complete duplicate records disagree on payload."""
    fingerprints = {
        record.get('fingerprint') for record in records
        if record.get('complete')
    }
    return len(fingerprints) > 1


def _assemble_manifest(records, declared_count, source, required_arrays,
                       parse_errors=None, observed_records=None):
    fragments = [record for record in records if _is_fragment(record)]
    records = [record for record in records if not _is_fragment(record)]
    if observed_records is None:
        observed_records = records
    else:
        observed_records = [
            record for record in observed_records
            if not _is_fragment(record)]
    groups = {}
    for record in records:
        groups.setdefault(_spectrum_key(record), []).append(record)

    chosen = []
    duplicate_ids = []
    duplicate_indexes = {}
    for key, group in groups.items():
        for record in group:
            _recompute_spectrum(record, required_arrays)
        best = _merge_group_records(group, required_arrays)
        if best.get('index') is None:
            # A fragment that carried no index may be merged with an indexed
            # record; keep the scan position when one is available.
            indexes = [
                record.get('index') for record in group
                if record.get('index') is not None
            ]
            if indexes:
                best['index'] = indexes[0]
                best['scan_number'] = (
                    best.get('scan_number') or _scan_number(best))
        chosen.append(best)
        if key[0] == 'id' and len(group) > 1:
            duplicate_ids.append({
                'id': key[1],
                'occurrences': [
                    {
                        'index': record.get('index'),
                        'position': record.get('position'),
                        'complete': record.get('complete'),
                        'missing_arrays': list(record.get('missing_arrays') or ()),
                        'fingerprint': record.get('fingerprint'),
                    }
                    for record in group
                ],
                'kept_index': best.get('index'),
                'duplicate_count': len(group),
                'conflict': _record_conflict(group),
            })
        if best.get('index') is not None:
            duplicate_indexes.setdefault(best['index'], []).append(best)

    chosen.sort(key=lambda record: (
        record.get('index') is None,
        record.get('index') if record.get('index') is not None else 0,
        record.get('position') if record.get('position') is not None else 0,
    ))

    missing_indices, unexpected_indices, scan_offset = _scan_domain(
        observed_records, declared_count)
    if declared_count is None:
        missing_scan_count = 0
    elif missing_indices:
        missing_scan_count = len(missing_indices)
    else:
        missing_scan_count = max(declared_count - len(chosen), 0)
    missing_scan_numbers = (
        [index + scan_offset for index in missing_indices]
        if scan_offset is not None else [])

    missing_arrays = []
    for record in chosen:
        if record.get('missing_arrays'):
            missing_arrays.append({
                'id': record.get('id'),
                'index': record.get('index'),
                'missing_arrays': list(record['missing_arrays']),
                'issues': dict(record.get('array_issues') or {}),
            })

    structural_errors = []
    for record in chosen:
        if record.get('structural_errors'):
            structural_errors.append({
                'id': record.get('id'),
                'index': record.get('index'),
                'errors': list(record['structural_errors']),
            })
    for index, records_at_index in duplicate_indexes.items():
        ids = {record.get('id') for record in records_at_index}
        if len(ids) > 1:
            structural_errors.append({
                'id': None,
                'index': index,
                'errors': ['index {} is used by multiple spectrum IDs'.format(index)],
            })

    complete_count = sum(1 for record in chosen if record.get('complete'))
    duplicate_conflicts = any(
        item.get('conflict') is True for item in duplicate_ids)
    complete = (
        declared_count is not None and
        complete_count >= declared_count and
        missing_scan_count == 0 and
        not missing_arrays and
        not structural_errors and
        not duplicate_conflicts
    )
    manifest = {
        'format': _MANIFEST_FORMAT,
        'version': _MANIFEST_VERSION,
        'source': source,
        'required_arrays': list(required_arrays),
        'declared_spectrum_count': declared_count,
        'observed_spectrum_count': len(records),
        'received_scan_count': len(chosen),
        'complete_spectrum_count': complete_count,
        'incomplete_spectrum_count': len(chosen) - complete_count,
        'missing_spectrum_count': (
            max(declared_count - complete_count, 0)
            if declared_count is not None else 0),
        'missing_scan_count': missing_scan_count,
        'missing_scans': missing_indices,
        'missing_scan_indices': missing_indices,
        'missing_scan_numbers': missing_scan_numbers,
        'scan_number_offset': scan_offset,
        'unexpected_scans': unexpected_indices,
        'unexpected_scan_indices': unexpected_indices,
        'unexpected_scan_numbers': (
            [index + scan_offset for index in unexpected_indices]
            if scan_offset is not None else []),
        'missing_arrays': missing_arrays,
        'duplicate_ids': duplicate_ids,
        'structural_errors': structural_errors,
        'parse_errors': parse_errors or [],
        'rejected_fragments': [
            {
                'position': record.get('position'),
                'arrays': list((record.get('arrays') or {}).keys()),
            }
            for record in fragments
        ],
        'complete': complete,
        'clean': complete and not duplicate_ids and not parse_errors,
        'summary': {
            'declared': declared_count,
            'received': len(chosen),
            'complete': complete_count,
            'missing_spectra': (
                max(declared_count - complete_count, 0)
                if declared_count is not None else 0),
            'missing_scans': missing_scan_count,
            'missing_arrays': len(missing_arrays),
            'duplicate_ids': len(duplicate_ids),
        },
        'spectra': chosen,
    }
    return manifest


def _manifest_source(source):
    """Return source metadata and an iterator over spectrum elements."""
    import gzip
    from contextlib import contextmanager

    @contextmanager
    def open_source():
        close_raw = False
        raw = source
        name = None
        already_compressed = isinstance(source, gzip.GzipFile)
        is_path = isinstance(source, (str, os.PathLike)) or (
            isinstance(source, bytes) and os.path.exists(source))
        if is_path:
            raw = open(source, 'rb')
            close_raw = True
            name = os.fspath(source)
        elif isinstance(source, (bytes, bytearray, memoryview)):
            raw = io.BytesIO(bytes(source))
            name = None
        else:
            name = getattr(source, 'name', None)
            if hasattr(raw, 'buffer') and isinstance(getattr(raw, 'mode', ''), str):
                if 'b' not in raw.mode:
                    raw = raw.buffer
        position = None
        try:
            position = raw.tell()
        except (AttributeError, OSError):
            pass
        size = None
        if position is not None and not already_compressed:
            try:
                raw.seek(0, 2)
                size = raw.tell()
                raw.seek(position)
            except (AttributeError, OSError):
                size = None

        # Detect gzip from the byte signature. Checking the name as well would
        # double-decompress an already opened GzipFile, whose .name is the
        # underlying file name.
        compressed = already_compressed
        if not compressed:
            try:
                signature_position = raw.tell()
                signature = raw.read(2)
                raw.seek(signature_position)
                compressed = signature == b'\x1f\x8b'
            except (AttributeError, OSError):
                compressed = False
            if not compressed and is_path and name and str(name).lower().endswith(
                    ('.gz', '.gzip')):
                compressed = True

        stream = None
        try:
            if compressed and not already_compressed:
                stream = gzip.GzipFile(fileobj=raw, mode='rb')
            else:
                stream = raw
            yield stream, {
                'name': os.path.basename(os.fspath(name)) if name else None,
                'size': size,
                'compressed': compressed,
            }
        finally:
            if stream is not None and stream is not raw:
                stream.close()
            if close_raw:
                raw.close()
            elif position is not None:
                try:
                    raw.seek(position)
                except (AttributeError, OSError):
                    pass

    return open_source()


def _iter_manifest_events(stream, state, huge_tree=True):
    """Parse a segment, recovering from an offset that starts mid-spectrum.

    A chunk cut from the middle of a document may begin with the tail of a
    ``<spectrum>`` or inside a text node. lxml cannot recover such input
    because it is not a document. Retry from the first intact ``<spectrum>``
    start after skipping that tail; nothing before that point can be a
    complete scan anyway.

    ``state`` receives the active parser under ``'parser'`` so its error log
    can be inspected after iteration.
    """
    def make_parser(source):
        return etree.iterparse(
            source, events=('start', 'end'), recover=True,
            huge_tree=huge_tree, remove_comments=True)

    parser = make_parser(stream)
    state['parser'] = parser
    state['rewound'] = False
    saw_spectrum = False
    try:
        for event, element in parser:
            if event == 'start' and xml._local_name(element) == 'spectrum':
                saw_spectrum = True
            yield event, element
    except etree.XMLSyntaxError:
        pass
    if saw_spectrum:
        return

    if not hasattr(stream, 'seek'):
        return
    stream.seek(0)
    data = stream.read()
    match = re.search(br'<(?:[A-Za-z_][\w.-]*:)?spectrum(?=[\s/>])', data)
    if match is None:
        return
    tail = data[match.start():]
    wrapper_open = b'<manifestSegment xmlns="http://psi.hupo.org/ms/mzml">'
    wrapper_close = b'</manifestSegment>'
    parser = make_parser(io.BytesIO(wrapper_open + tail + wrapper_close))
    state['parser'] = parser
    state['rewound'] = True
    for item in parser:
        yield item


def spectrum_manifest(source, required_arrays=None, huge_tree=True,
                      declared_count=None):
    """Build a JSON-friendly inventory of spectra in an mzML document.

    The manifest can be built from a complete file or from a single uploaded
    segment. Segments may begin and end in the middle of a spectrum; a partial
    leading tail is skipped, and a partial trailing spectrum still contributes
    its array evidence but is never counted as complete.

    Every spectrum record contains the array metadata needed to check whether
    its peak list is usable:

    - ``arrays`` maps array names to ``length``, ``dtype``, compression,
      a SHA-1 of the decoded payload and a ``verified`` flag.
    - ``missing_arrays`` lists required arrays that are absent, empty or whose
      decoded length disagrees with ``defaultArrayLength``.
    - ``missing_scans`` lists indices below the declared ``spectrumList`` count
      that were not received; ``missing_scan_numbers`` translates them using
      the observed ID numbering when it is consistent.
    - ``duplicate_ids`` reports repeated spectrum IDs separately from missing
      scans and from missing arrays.

    A spectrum is only counted in ``complete_spectrum_count`` when the arrays
    were actually decoded and their lengths match the declaration. Merely
    seeing a ``<spectrum>`` element is not enough. In particular, ``complete``
    is only true when the declared spectrum count is known and every declared
    spectrum is complete.

    Parameters
    ----------
    source : str, bytes, os.PathLike or file
        Path to an mzML file or a binary file-like object. Gzip-compressed
        sources are detected and decompressed transparently.
    required_arrays : iterable of str, optional
        Array names that must be present and verifiable. Defaults to
        ``('m/z array', 'intensity array')``.
    huge_tree : bool, optional
        Allow very large XML text nodes. Default is :py:const:`True` for
        manifest generation.
    declared_count : int, optional
        The ``spectrumList/@count`` from the file header. Supply this when
        parsing a later segment whose own header is not included. A count
        found in the segment takes precedence.

    Returns
    -------
    dict
        The manifest. All values are JSON-serializable.
    """
    required = tuple(required_arrays or _DEFAULT_REQUIRED_ARRAYS)
    records = []
    segment_declared_count = None
    parse_errors = []
    referenceable_param_groups = {}
    position = 0
    with _manifest_source(source) as (stream, source_meta):
        state = {'parser': None, 'rewound': False}
        current_spectrum = None
        try:
            for event, element in _iter_manifest_events(stream, state, huge_tree):
                local_name = xml._local_name(element)
                if event == 'start':
                    if local_name == 'spectrumList' and segment_declared_count is None:
                        segment_declared_count = _as_int(element.get('count'))
                    elif local_name == 'spectrum':
                        current_spectrum = element
                    continue
                if local_name == 'referenceableParamGroup':
                    group_id = element.get('id')
                    if group_id:
                        referenceable_param_groups[group_id] = _collect_params(
                            element, referenceable_param_groups)
                    element.clear()
                elif local_name == 'spectrum':
                    records.append(_parse_spectrum_element(
                        element, referenceable_param_groups, position))
                    position += 1
                    element.clear()
                    parent = element.getparent()
                    if parent is not None:
                        while element.getprevious() is not None:
                            del parent[0]
                    current_spectrum = None
                elif local_name == 'binaryDataArray' and current_spectrum is None:
                    element.clear()
                elif local_name in ('referenceableParamGroupList', 'run', 'mzML',
                                    'indexedmzML', 'indexList', 'index'):
                    element.clear()
        except etree.XMLSyntaxError as exc:
            parse_errors = [{
                'type': 'ERR_NO_RECOVERABLE_SPECTRUM',
                'message': str(exc),
                'line': getattr(exc, 'lineno', None),
            }]
        parser = state['parser']
    truncated = False
    if parser is not None:
        try:
            error_log = parser.error_log
        except AttributeError:
            error_log = []
        parse_errors.extend([
            {
                'type': error.type_name,
                'message': error.message,
                'line': error.line,
            }
            for error in error_log
            if error.type_name not in (
                'ERR_DOCUMENT_END', 'ERR_TAG_NOT_FINISHED')
        ])
        truncated = any(
            error.type_name in ('ERR_DOCUMENT_END', 'ERR_TAG_NOT_FINISHED')
            for error in error_log)
        if state.get('rewound'):
            truncated = True
    if segment_declared_count is not None:
        declared_count = segment_declared_count
    manifest = _assemble_manifest(
        records, declared_count, source_meta, required, parse_errors)
    manifest['truncated'] = truncated
    return manifest


build_spectrum_manifest = spectrum_manifest


def dump_spectrum_manifest(manifest, output=None, **kwargs):
    """Serialize a manifest to JSON.

    Parameters
    ----------
    manifest : dict
        A manifest returned by :py:func:`spectrum_manifest`.
    output : str, os.PathLike or file, optional
        Destination. If omitted, the JSON text is returned.
    **kwargs
        Passed to :py:func:`json.dumps` (for example ``indent``).

    Returns
    -------
    str or None
        JSON text when ``output`` is omitted, otherwise :py:const:`None`.
    """
    kwargs.setdefault('indent', 2)
    kwargs.setdefault('ensure_ascii', False)
    text = json.dumps(manifest, **kwargs)
    if output is None:
        return text
    if isinstance(output, (str, bytes, os.PathLike)):
        with open(output, 'w', encoding='utf-8') as handle:
            handle.write(text)
    else:
        output.write(text)
    return None


def write_spectrum_manifest(source, output=None, required_arrays=None,
                            huge_tree=True, **kwargs):
    """Build a manifest from ``source`` and write it as JSON.

    See :py:func:`spectrum_manifest` and :py:func:`dump_spectrum_manifest`.
    """
    manifest = spectrum_manifest(
        source, required_arrays=required_arrays, huge_tree=huge_tree)
    return dump_spectrum_manifest(manifest, output, **kwargs)


def load_spectrum_manifest(source):
    """Read a manifest from a JSON file or file-like object."""
    if isinstance(source, (str, bytes, os.PathLike)):
        with open(source, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    return json.load(source)


def merge_spectrum_manifests(base, *updates, required_arrays=None):
    """Merge manifests from segmented uploads without double-counting scans.

    The first argument is the manifest already held. Each additional manifest
    contributes only spectra whose IDs are not present in the base, plus
    arrays that complete an existing partial spectrum. Completeness is
    recomputed from the array evidence in every record; a record that was
    counted as complete in an older manifest but has no verifiable arrays is
    downgraded rather than trusted.

    Parameters
    ----------
    base : dict
        The existing manifest.
    *updates : dict
        Manifests built from newly received segments.
    required_arrays : iterable of str, optional
        Overrides the required array names stored in the manifests.

    Returns
    -------
    dict
        A merged manifest with ``added``, ``upgraded`` and ``unchanged``
        lists under the ``merge`` key. ``added`` contains only spectrum IDs
        that were absent from the base manifest. ``upgraded`` contains records
        whose arrays became more complete; their ``missing_arrays_before`` and
        ``missing_arrays_after`` fields record exactly what changed.
    """
    manifests = [base] + list(updates)
    required = tuple(
        required_arrays or
        (base.get('required_arrays') if base.get('required_arrays') else
         _DEFAULT_REQUIRED_ARRAYS))
    for manifest in manifests:
        if manifest.get('format') not in (None, _MANIFEST_FORMAT):
            raise aux.PyteomicsError(
                'Cannot merge a non-manifest document: {!r}'.format(
                    manifest.get('format')))
    declared_counts = [
        manifest.get('declared_spectrum_count') for manifest in manifests
        if manifest.get('declared_spectrum_count') is not None
    ]
    declared_count = max(declared_counts) if declared_counts else None

    base_groups = {}
    for record in base.get('spectra') or ():
        base_groups.setdefault(_spectrum_key(record), []).append(record)

    all_records = []
    for source_index, manifest in enumerate(manifests):
        for record in manifest.get('spectra') or ():
            record = copy.deepcopy(record)
            record.setdefault(
                '_manifest_source', manifest.get('source'))
            all_records.append(record)

    groups = {}
    for record in all_records:
        if _is_fragment(record):
            continue
        groups.setdefault(_spectrum_key(record), []).append(record)

    merged_groups = {}
    added = []
    upgraded = []
    unchanged = []
    duplicate_ids = []
    for key, group in groups.items():
        for record in group:
            _recompute_spectrum(record, required)
        merged_record = _merge_group_records(group, required)
        merged_groups[key] = merged_record
        merged_record['manifest_sources'] = [
            record.get('_manifest_source') for record in group
        ]
        if key[0] == 'id' and len(group) > 1:
            duplicate_ids.append({
                'id': key[1],
                'occurrences': [
                    {
                        'index': record.get('index'),
                        'position': record.get('position'),
                        'complete': record.get('complete'),
                        'missing_arrays': list(record.get('missing_arrays') or ()),
                        'fingerprint': record.get('fingerprint'),
                        'manifest_source': record.get('_manifest_source'),
                    }
                    for record in group
                ],
                'kept_index': merged_record.get('index'),
                'duplicate_count': len(group),
                'conflict': _record_conflict(group),
            })
        base_group = base_groups.get(key)
        if not base_group:
            added.append({
                'id': merged_record.get('id'),
                'index': merged_record.get('index'),
                'complete': merged_record.get('complete'),
            })
            continue
        for record in base_group:
            _recompute_spectrum(record, required)
        base_best = _select_best_record(base_group, required)
        if (not base_best.get('complete')) and merged_record.get('complete'):
            upgraded.append({
                'id': merged_record.get('id'),
                'index': merged_record.get('index'),
                'missing_arrays_before': list(base_best.get('missing_arrays') or ()),
            })
        elif (not base_best.get('complete')) and (
                base_best.get('missing_arrays') or ()) != (
                merged_record.get('missing_arrays') or ()):
            upgraded.append({
                'id': merged_record.get('id'),
                'index': merged_record.get('index'),
                'missing_arrays_before': list(base_best.get('missing_arrays') or ()),
                'missing_arrays_after': list(merged_record.get('missing_arrays') or ()),
            })
        else:
            unchanged.append({
                'id': merged_record.get('id'),
                'index': merged_record.get('index'),
            })

    parse_errors = []
    for manifest in manifests:
        parse_errors.extend(manifest.get('parse_errors') or ())
    merged = _assemble_manifest(
        [copy.deepcopy(record) for record in merged_groups.values()],
        declared_count, None, required, parse_errors,
        observed_records=[copy.deepcopy(record) for record in all_records
                          if not _is_fragment(record)])
    merged['duplicate_ids'] = duplicate_ids
    merged['duplicate_id_count'] = len(duplicate_ids)
    merged['duplicate_conflict_count'] = sum(
        1 for item in duplicate_ids if item.get('conflict') is True)
    merged['summary']['duplicate_ids'] = len(duplicate_ids)
    duplicate_conflicts = merged['duplicate_conflict_count'] > 0
    if duplicate_conflicts:
        merged['complete'] = False
        merged['clean'] = False
    merged['truncated'] = any(
        manifest.get('truncated') for manifest in manifests)
    merged['sources'] = [
        manifest.get('source') for manifest in manifests
        if manifest.get('source') is not None
    ]
    merged['observed_spectrum_count'] = sum(
        len(manifest.get('spectra') or ()) for manifest in manifests)
    merged['rejected_fragments'] = [
        fragment for manifest in manifests
        for fragment in (manifest.get('rejected_fragments') or ())
    ]
    merged['merge'] = {
        'added': added,
        'upgraded': upgraded,
        'unchanged': unchanged,
        'added_count': len(added),
        'upgraded_count': len(upgraded),
        'unchanged_count': len(unchanged),
    }
    merged['summary'].update({
        'added': len(added),
        'upgraded': len(upgraded),
    })
    return merged


class PreIndexedMzML(MzML):
    """Parser class for mzML files, subclass of :py:class:`MzML`.
    Uses byte offsets listed at the end of the file for quick access to spectrum elements.
    """
    def build_byte_index(self):
        """
        Build up a :class:`HierarchicalOffsetIndex` of offsets for elements. Calls :meth:`_find_index_list` or
        falls back on regular :class:`MzML` indexing.

        Returns
        -------

        out : HierarchicalOffsetIndex
        """
        index = self._find_index_list()
        if index:
            return index
        else:
            warnings.warn('Could not extract the embedded offset index. Falling back to default indexing procedure.')
            return super(PreIndexedMzML, self).build_byte_index()

    @xml._keepstate
    def _iterparse_index_list(self, offset):
        index_map = xml.HierarchicalOffsetIndex()
        index = index_map._inner_type()
        self._source.seek(offset)
        try:
            for event, elem in etree.iterparse(self._source, events=('start', 'end'), remove_comments=True):
                if event == 'start':
                    if elem.tag == 'index':
                        index = index_map._inner_type()
                        index_map[elem.attrib['name']] = index
                else:
                    if elem.tag == 'offset':
                        index[elem.attrib['idRef']] = int(elem.text)
                    elem.clear()
        except etree.XMLSyntaxError:
            # The iteration has reached the end of the indexList tag and the parser
            # encounters the later elements in the document.
            pass
        return index_map

    @xml._keepstate
    def _find_index_list_offset(self):
        """
        Search relative to the bottom of the file upwards to find the offsets
        of the index lists.

        Returns
        -------
        list of int
            A list of byte offsets for `<indexList>` elements
        """
        self._source.seek(-1024, 2)
        text = self._source.read(1024)
        index_offsets = list(map(int, re.findall(br'<indexListOffset>(\d+)</indexListOffset>', text)))
        return index_offsets

    @xml._keepstate
    def _find_index_list(self):
        """
        Extract lists of index offsets from the end of the file.

        Returns
        -------
        dict of str -> dict of str -> int
        """
        offsets = self._find_index_list_offset()
        index_list = xml.HierarchicalOffsetIndex()
        for offset in offsets:
            # Sometimes the offset is at the very beginning of the file,
            # due to a bug in an older version of ProteoWizard. If this crude
            # check fails, don't bother searching the entire file, and fall back
            # on the base class's mechanisms.
            #
            # Alternative behavior here would be to start searching for the start
            # of the index from the bottom of the file, but this version of Proteowizard
            # also emits invalid offsets which do not improve retrieval time.
            if offset < 1024:
                continue
            index_list = self._iterparse_index_list(offset)
        return index_list
