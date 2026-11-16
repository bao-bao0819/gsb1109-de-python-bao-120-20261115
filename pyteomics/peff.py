"""
peff - PSI Extended FASTA Format
================================

PEFF is a forth-coming standard from PSI-HUPO formalizing and extending the
encoding of protein features and annotations for building search spaces for
proteomics. See `The PEFF specification <http://www.psidev.info/peff>`_ for
more up-to-date information on the standard.

Data manipulation
-----------------

Classes
.......

The PEFF parser inherits several properties from implementation in the :mod:`~.fasta` module,
building on top of the :class:`~.TwoLayerIndexedFASTA` reader.

Available classes:

  :py:class:`IndexedPEFF` - Parse a PEFF format file in binary-mode, supporting
  direct indexing by header string or by tag.

  :py:class:`PEFFIndexer` - Write a new PEFF version in which a single record
  annotation is corrected, rebuilding the byte-offset index for the new file
  without touching the source snapshot.

"""

#   Copyright 2018 Joshua Klein, Lev Levitsky
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

import io
import os
import re
import tempfile
try:
    from collections.abc import Sequence as SequenceABC, Mapping
except ImportError:
    from collections import Sequence as SequenceABC, Mapping
from collections import OrderedDict, defaultdict

from .fasta import TwoLayerIndexedFASTA
from .auxiliary import OffsetIndex, PyteomicsError


class Header(Mapping):
    """Hold parsed properties of a key-value pair like a sequence's
    definition line.

    This object supports the :class:`Mapping` interface, and
    keys may be accessed by attribute access notation.
    """
    def __init__(self, mapping, original=None):
        self._mapping = mapping

    def __getitem__(self, key):
        return self._mapping[key]

    def __iter__(self):
        return iter(self._mapping)

    def items(self):
        return self._mapping.items()

    def keys(self):
        return self._mapping.keys()

    def values(self):
        return self._mapping.values()

    def __len__(self):
        return len(self._mapping)

    def __contains__(self, key):
        return key in self._mapping

    def __getattr__(self, key):
        if key == "_mapping":
            raise AttributeError(key)
        try:
            return self._mapping[key]
        except KeyError:
            raise AttributeError(key)

    def __repr__(self):
        return "{self.__class__.__name__}({mapping})".format(self=self, mapping=dict(self._mapping))

    def __hash__(self):
        return hash(self.defline)

    def __eq__(self, other):
        try:
            return self._mapping == other._mapping
        except AttributeError:
            return str(self) == str(other)

    def __ne__(self, other):
        return not (self == other)

    def __dir__(self):
        base = set(dir(super(Header, self)))
        keys = set(self._mapping.keys())
        return list(base | keys)


class IndexedPEFF(TwoLayerIndexedFASTA):
    """Creates an :py:class:`IndexedPEFF` object.

    Parameters
    ----------
    source : str or file
        The file to read. If a file object, it needs to be in *rb* mode.
    parse : bool, optional
        Defines whether the descriptions should be parsed in the produced tuples.
        Default is :py:const:`True`.
    kwargs : passed to the :py:class:`TwoLayerIndexedFASTA` constructor.
    """

    kv_pattern = re.compile(r"\\(?P<key>\S+)=(?P<value>.+?)(?:\s(?=\\)|$)")
    header_pattern = re.compile(r"^>?(\S+):(\S+)")
    has_feature_index = re.compile(r"^\(?(\d+):")
    header_group = 2

    class _PEFFFeature(SequenceABC):
        def __init__(self, *fields, **kwargs):
            self.fields = tuple(fields)
            self.id = kwargs.get('id')
            self.feature_type = kwargs.get("feature_type")

        def __eq__(self, other):
            return tuple(self) == tuple(other)

        def __ne__(self, other):
            return not (self == other)

        def __getitem__(self, i):
            return self.fields[i]

        def __len__(self):
            return len(self.fields)

        def __repr__(self):
            return repr(tuple(self))

        def __str__(self):
            return "(%s%s)" % (
                '%r:' % self.id if self.id is not None else '',
                '|'.join(map(str, self)), )

    def __init__(self, source, ignore_comments=False, **kwargs):
        super(IndexedPEFF, self).__init__(
            source, ignore_comments=ignore_comments, parser=self.parser,
            header_pattern=self.header_pattern, **kwargs)
        self.header_blocks = []
        self.comments = []
        self.version = None
        self.number_of_entries = 0
        self._parse_header()

    def _parse_header(self):
        self.seek(0)
        line = self.readline().decode("ascii")
        if not line.startswith("# PEFF"):
            raise ValueError("Not a PEFF File")
        self.version = tuple(map(int, line.strip()[7:].split(".")))
        current_block = defaultdict(list)
        in_header = True
        while in_header:
            line = self.readline().decode("ascii")
            if not line.startswith("#"):
                in_header = False
            line = line.strip()[2:]
            if '=' in line:
                key, value = line.split("=", 1)
                if key == "GeneralComment":
                    self.comments.append(value)
                else:
                    current_block[key].append(value)
            if line.startswith("//"):
                if current_block:
                    self.header_blocks.append(
                        Header(OrderedDict((k, v if len(v) > 1 else v[0])
                                           for k, v in current_block.items())))
                current_block = defaultdict(list)
        number_of_entries = 0
        for block in self.header_blocks:
            try:
                number_of_entries += int(block['NumberOfEntries'])
            except KeyError:
                pass
        self.number_of_entries = number_of_entries

    def _extract_parenthesis_list(self, text):
        chunks = []
        chunk = []
        paren_level = 0
        i = 0
        n = len(text)
        while i < n:
            c = text[i]
            i += 1
            if c == "(":
                if paren_level > 0:
                    chunk.append(c)
                paren_level += 1
            elif c == ")":
                if paren_level > 1:
                    chunk.append(c)
                paren_level -= 1
                if paren_level == 0:
                    if chunk:
                        chunks.append(chunk)
                    chunk = []
            else:
                chunk.append(c)
        chunks = list(map(''.join, chunks))
        return chunks

    def _split_pipe_separated_tuple(self, text):
        parts = text.split("|")
        return parts

    def _coerce_types(self, key, value):
        value = value.strip()
        feature_id_match = self.has_feature_index.search(value)
        if feature_id_match:
            feature_id = int(feature_id_match.group(1))
            value = self.has_feature_index.sub('', value)
        else:
            feature_id = None
        if "|" in value:
            value = self._split_pipe_separated_tuple(value)
            result = []
            for i, v in enumerate(value):
                result.append(self._coerce_value(key, v, i))
            return self._PEFFFeature(*result, feature_type=key, id=feature_id)
        else:
            return self._coerce_value(key, value, 0)

    def _coerce_value(self, key, value, index):
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        return str(value)

    def parser(self, line):
        match = self.header_pattern.match(line)
        if not match:
            raise ValueError(
                "Failed to parse {!r} using {!r}".format(
                    line, self))
        storage = OrderedDict()
        prefix = None
        db_uid = None
        if line.startswith(">"):
            line = line[1:]
        prefix, line = line.split(":", 1)
        db_uid, line = line.split(" ", 1)
        storage['Prefix'] = prefix
        storage['Tag'] = db_uid
        kv_pattern = re.compile(r"\\(?P<key>\S+)=(?P<value>.+?)(?:\s(?=\\)|$)")
        for key, value in kv_pattern.findall(line):
            if not (value.startswith("(") or " (" in value):
                storage[key] = self._coerce_types(key, value)
            else:
                # multi-value
                storage[key] = [self._coerce_types(key, v) for v in self._extract_parenthesis_list(value)]
        return Header(storage)


class PEFFIndexer(object):
    """Write a new PEFF version in which exactly one record annotation is
    corrected.

    The source file and its byte-offset index form a read-only snapshot and
    are never modified: the corrected record together with all other,
    byte-identical records is written to a *new* file, accompanied by a
    *new* byte-offset index in which the offsets affected by the length
    change are updated.

    Records are addressed solely by their stable record identity, the PEFF
    tag (``DbUniqueId``). Duplicate display names (e.g. repeated ``PName``
    values) are never used to guess a target, and an ID that resolves to
    more than one record is an error.

    Parameters
    ----------
    source : str or file
        The source PEFF file. If a file object, it must be open in *rb* mode.
    new_path : str, optional
        Path of the corrected PEFF file. Must not point at `source`.
        Defaults to the source path with ``.corrected`` inserted before
        the extension.
    index_path : str, optional
        Path of the byte-offset index JSON written for the new file.
        Must not point at the index of the source file. Defaults to the
        conventional ``<name>-<ext>-byte-offsets.json`` next to `new_path`.
    kwargs : passed to the :class:`IndexedPEFF` constructor.
    """

    _index_suffix = '-byte-offsets.json'

    def __init__(self, source, new_path=None, index_path=None, **kwargs):
        self._source_path = self._resolve_path(source)
        self._reader = IndexedPEFF(source, **kwargs)
        self.new_path = new_path or self._default_new_path(self._source_path)
        self.index_path = index_path or self._default_index_path(self.new_path)
        self._guard_snapshot()

    @staticmethod
    def _resolve_path(source):
        path = getattr(source, 'name', source)
        if not isinstance(path, str):
            raise PyteomicsError(
                'A file path is required to write a corrected PEFF version, '
                'got {!r}.'.format(source))
        return os.path.abspath(path)

    @staticmethod
    def _default_new_path(source_path):
        name, ext = os.path.splitext(source_path)
        return name + '.corrected' + ext

    @classmethod
    def _default_index_path(cls, peff_path):
        name, ext = os.path.splitext(peff_path)
        return '{}-{}{}'.format(name, ext[1:], cls._index_suffix)

    def _guard_snapshot(self):
        if os.path.abspath(self.new_path) == self._source_path:
            raise PyteomicsError(
                'Refusing to overwrite the source PEFF file {!r}; the '
                'corrected version must be written to a new path.'.format(
                    self._source_path))
        if os.path.abspath(self.index_path) == self._default_index_path(
                self._source_path):
            raise PyteomicsError(
                'Refusing to overwrite the byte-offset index of the source '
                'file {!r}; the new index must be written to a new path.'.format(
                    self._source_path))

    def _resolve_record(self, record_id):
        """Return the unique full-header key whose stable tag is `record_id`."""
        pattern = self._reader.header_pattern
        group = self._reader.header_group
        matches = []
        for full_header in self._reader._offset_index:
            match = re.match(pattern, full_header)
            if match and match.group(group) == record_id:
                matches.append(full_header)
        if not matches:
            raise PyteomicsError(
                'No PEFF record with stable ID {!r} found in {!r}.'.format(
                    record_id, self._source_path))
        if len(matches) > 1:
            raise PyteomicsError(
                'Stable ID {!r} matches {} records; a unique target is '
                'required.'.format(record_id, len(matches)))
        return matches[0]

    @staticmethod
    def _header_key(header_text):
        key = header_text.strip()
        if key.startswith('>'):
            key = key[1:].strip()
        return key

    @staticmethod
    def _is_feature_value(value):
        """True for parsed PEFF feature containers (a parenthesised tuple
        or a list of them), as opposed to scalar annotations."""
        if isinstance(value, (str, bytes)):
            return False
        return isinstance(value, (list, SequenceABC))

    def _replace_annotation(self, header_text, key, value):
        if not isinstance(value, str) or not value:
            raise PyteomicsError(
                'The corrected annotation value must be a non-empty string, '
                'got {!r}.'.format(value))
        parsed = self._reader.parser(header_text)
        if key not in parsed:
            raise PyteomicsError(
                'Annotation {!r} is not present on record {!r}; only an '
                'existing annotation can be corrected.'.format(
                    key, parsed.get('Tag')))
        if self._is_feature_value(parsed[key]):
            raise PyteomicsError(
                'Annotation {!r} is a multi-valued feature; only scalar '
                'annotations can be corrected.'.format(key))
        token = re.compile(r'(\\' + re.escape(key) + r'=)(.*?)(?= \\|$)')
        new_header_text, count = token.subn(
            lambda match: match.group(1) + value, header_text, count=1)
        if count != 1:
            raise PyteomicsError(
                'Could not locate the {!r} annotation token for '
                'replacement.'.format(key))
        reparsed = self._reader.parser(new_header_text)
        new_value = reparsed[key]
        if self._is_feature_value(new_value) or str(new_value) != value:
            raise PyteomicsError(
                'The corrected value {!r} is not parsed back as the scalar '
                'value of {!r}; choose a value without parenthesised '
                'sub-fields.'.format(value, key))
        for other_key, other_value in parsed.items():
            if other_key != key and reparsed[other_key] != other_value:
                raise PyteomicsError(
                    'Correcting {!r} unexpectedly altered {!r}.'.format(
                        key, other_key))
        return new_header_text

    def _build_new_index(self, old_header, new_header, start, delta):
        new_index = OffsetIndex()
        new_key = self._header_key(new_header)
        for full_header, offsets in self._reader._offset_index.items():
            off_start, off_end = offsets
            if full_header == old_header:
                new_index[new_key] = (off_start, off_end + delta)
            elif off_start > start:
                new_index[full_header] = (off_start + delta, off_end + delta)
            else:
                new_index[full_header] = offsets
        if not new_index._integrity_check():
            raise PyteomicsError('Failed to build a consistent offset index.')
        return new_index

    @staticmethod
    def _atomic_write(path, data, binary):
        directory = os.path.dirname(os.path.abspath(path)) or '.'
        mode = 'wb' if binary else 'w'
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix='.' + os.path.basename(path) + '.',
            suffix='.tmp')
        try:
            with os.fdopen(fd, mode) as handle:
                handle.write(data)
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def correct_annotation(self, record_id, key, value):
        """Correct a single annotation of a single record and write the
        new PEFF version with an updated index.

        Parameters
        ----------
        record_id : str
            Stable record identity (PEFF tag / ``DbUniqueId``) of the
            target record.
        key : str
            Name of the annotation to correct, e.g. ``'PName'``.
        value : str
            New value of the annotation; everything else on the header
            line stays byte-identical.

        Returns
        -------
        IndexedPEFF
            A fresh reader open on the corrected file. The paths of the new
            file and its index are available as :attr:`new_path` and
            :attr:`index_path`.
        """
        full_header = self._resolve_record(record_id)
        start, end = self._reader._offset_index[full_header]
        with open(self._source_path, 'rb') as handle:
            handle.seek(start)
            record = handle.read(end - start)
        marker = record.find(b'>')
        if marker == -1:
            raise PyteomicsError(
                'Record at offset {} of {!r} does not start with a PEFF '
                'header line.'.format(start, self._source_path))
        newline = record.find(b'\n', marker)
        if newline == -1:
            newline = len(record)
        record_prefix = record[:marker]
        header_bytes = record[marker:newline]
        record_tail = record[newline:]
        header_text = header_bytes.decode(self._reader.encoding)
        new_header_text = self._replace_annotation(header_text, key, value)
        new_header_bytes = new_header_text.encode(self._reader.encoding)
        delta = len(new_header_bytes) - len(header_bytes)

        with open(self._source_path, 'rb') as handle:
            before = handle.read(start)
            handle.seek(end)
            after = handle.read()
        new_data = (before + record_prefix + new_header_bytes
                    + record_tail + after)
        new_index = self._build_new_index(
            full_header, new_header_text, start, delta)

        self._atomic_write(self.new_path, new_data, binary=True)
        try:
            self._atomic_write(
                self.index_path, self._dump_index(new_index), binary=False)
            with open(self.index_path) as handle:
                loaded = OffsetIndex.load(handle)
            normalized = {key: tuple(value) for key, value in loaded.items()}
            if normalized != dict(new_index):
                raise PyteomicsError(
                    'The written byte-offset index does not match the '
                    'corrected file {!r}.'.format(self.new_path))
            rebuilt = IndexedPEFF(self.new_path)
            if rebuilt._offset_index != new_index:
                raise PyteomicsError(
                    'The corrected file {!r} does not rebuild the expected '
                    'byte-offset index.'.format(self.new_path))
        except BaseException:
            for path in (self.index_path, self.new_path):
                if os.path.exists(path):
                    os.remove(path)
            raise
        return rebuilt

    @staticmethod
    def _dump_index(index):
        buffer = io.StringIO()
        index.save(buffer)
        return buffer.getvalue()

    def close(self):
        """Close the reader held on the source file."""
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
