import sys
import codecs
import re
import hashlib
from functools import wraps
from contextlib import contextmanager
from collections import OrderedDict, defaultdict
import json
import multiprocessing as mp
import threading
import queue
import tempfile
from copy import copy
import warnings
import os
from abc import ABCMeta
from queue import Empty
from collections.abc import Sequence

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import dill
except ImportError:
    dill = None
    import pickle
    serializer = pickle
else:
    serializer = dill


from .structures import PyteomicsError
from .utils import add_metaclass


def _get_default_start_method():
    supported_methods = mp.get_all_start_methods()
    if supported_methods[0] == 'fork':
        for alternative in ['forkserver', 'spawn']:
            if alternative in supported_methods:
                return alternative
        else:
            raise RuntimeError('Cannot determine a suitable process start method.')
    return supported_methods[0]


ctx = mp.get_context(_get_default_start_method())


def set_start_method(method):
    global ctx
    ctx = mp.get_context(method)
    FileReadingProcess.__bases__ = (ctx.Process,)
    FileReadingProcess._event_class = ctx.Event


def _keepstate(func):
    """Decorator to help keep the position in open files passed as
    positional arguments to functions"""
    @wraps(func)
    def wrapped(*args, **kwargs):
        positions = [getattr(arg, 'seek', None) and getattr(arg, 'tell', type(None))() for arg in args]
        for arg, pos in zip(args, positions):
            if pos is not None:
                arg.seek(0)
        res = func(*args, **kwargs)
        for arg, pos in zip(args, positions):
            if pos is not None:
                try:
                    arg.seek(pos)
                except ValueError:
                    pass
        return res
    return wrapped


def _keepstate_method(func):
    """Decorator for :py:class:`FileReader` methods to help keep the position
    in the underlying file.
    """
    @wraps(func)
    def wrapped(self, *args, **kwargs):
        position = self.tell()
        self.seek(0)
        try:
            return func(self, *args, **kwargs)
        finally:
            self.seek(position)
    return wrapped


class _file_obj(object):
    """Check if `f` is a file name and open the file in `mode`.
    A context manager."""

    def __init__(self, f, mode, encoding=None):
        self._file_spec = None
        self.mode = mode
        if f is None:
            self.file = {'r': sys.stdin, 'a': sys.stdout, 'w': sys.stdout
                         }[mode[0]]
            self._file_spec = None
        elif isinstance(f, (str, bytes)):
            self.file = open(f, mode, encoding=encoding)
            self._file_spec = f
        else:
            self._file_spec = f
            self.file = f
        self.encoding = getattr(self.file, 'encoding', encoding)
        self.close_file = (self.file is not f)

    def __enter__(self):
        return self

    def __reduce_ex__(self, protocol):
        return self.__class__, (self._file_spec, self.mode, self.encoding)

    def __exit__(self, *args, **kwargs):
        if (not self.close_file) or self._file_spec is None:
            return  # do nothing
        # clean up
        exit = getattr(self.file, '__exit__', None)
        if exit is not None:
            return exit(*args, **kwargs)
        else:
            exit = getattr(self.file, 'close', None)
            if exit is not None:
                exit()

    def __getattr__(self, attr):
        return getattr(self.file, attr)

    def __iter__(self):
        return iter(self.file)


class NoOpBaseReader(object):
    def __init__(self, *args, **kwargs):
        pass

    @property
    def _source_path(self):
        try:
            path = self._source.name
        except AttributeError:
            return None
        if isinstance(path, (str, bytes, os.PathLike)):
            return path
        return None


class IteratorContextManager(NoOpBaseReader):
    def __init__(self, *args, **kwargs):
        self._func = kwargs.pop('parser_func')
        self._args = args
        self._kwargs = kwargs
        if type(self) is IteratorContextManager:
            self.reset()
        super(IteratorContextManager, self).__init__(*args, **kwargs)

    def __getstate__(self):
        state = {}
        state['_iterator_args'] = self._args
        state['_iterator_kwargs'] = self._kwargs
        return state

    def __setstate__(self, state):
        self._args = state['_iterator_args']
        self._kwargs = state['_iterator_kwargs']

    def reset(self):
        """Resets the iterator to its initial state."""
        try:
            self._reader = self._func(*self._args, **self._kwargs)
        except Exception:
            self.__exit__(*sys.exc_info())
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        pass

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._reader)


@add_metaclass(ABCMeta)
class FileReader(IteratorContextManager):
    """Abstract class implementing context manager protocol
    for file readers.
    """

    def __init__(self, source, **kwargs):
        func = kwargs['parser_func']
        super(FileReader, self).__init__(*kwargs['args'], parser_func=func, **kwargs['kwargs'])
        self._pass_file = kwargs['pass_file']
        self._source_init = source
        self._mode = kwargs['mode']
        self._encoding = kwargs.get('encoding')
        self.reset()

    def reset(self):
        if hasattr(self, '_source'):
            self._source.__exit__(None, None, None)
        self._source = _file_obj(self._source_init, self._mode, self._encoding)
        try:
            if self._pass_file:
                self._reader = self._func(
                    self._source, *self._args, **self._kwargs)
            else:
                self._reader = self._func(*self._args, **self._kwargs)
        except Exception:  # clean up on any error
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *args, **kwargs):
        self._source.__exit__(*args, **kwargs)

    # delegate everything else to file object
    def __getattr__(self, attr):
        if attr == '_source':
            raise AttributeError
        return getattr(self._source, attr)


def remove_bom(bstr):
    return bstr.replace(codecs.BOM_LE, b'').lstrip(b"\x00")


class IndexedReaderMixin(NoOpBaseReader):
    """Common interface for :py:class:`IndexedTextReader` and :py:class:`IndexedXML`."""
    #: Identifies how byte offsets are interpreted by this reader. Cached
    #: indexes built with a different convention must not be reused.
    _offset_convention = None

    @property
    def index(self):
        return self._offset_index

    @property
    def default_index(self):
        return self._offset_index

    def _index_parameters(self):
        """Return the parser parameters that affect the index contents.

        A cached index is only applicable if it was built with the same
        parameters, e.g. the same indexed tags or the same record label.
        """
        return {}

    def _source_fingerprint(self):
        """Return a fingerprint of the source file used to detect stale indexes.

        The fingerprint contains the file size, modification time and a
        SHA-256 digest of the contents. It changes whenever the file is
        rewritten, recompressed or has metadata inserted, so an index built
        for another revision of the source is never applied.
        """
        path = self._source_path
        if path is None:
            return None
        try:
            stat = os.stat(path)
            with open(path, 'rb') as f:
                digest = hashlib.sha256()
                while True:
                    block = f.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
        except (OSError, IOError):
            return None
        return {
            'size': stat.st_size,
            'mtime_ns': getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9)),
            'sha256': digest.hexdigest(),
        }

    def _index_is_stale(self, index):
        """Check if a cached index still describes the current source file.

        Unchanged size and modification time are accepted without reading the
        file. If either differs, the contents are hashed so that changes which
        preserve the size (e.g. recompression) are still detected, while
        metadata-only timestamps do not force an unnecessary rebuild.
        """
        recorded = index.source
        if not isinstance(recorded, dict):
            return True
        path = self._source_path
        if path is None:
            return True
        try:
            stat = os.stat(path)
        except OSError:
            return True
        if recorded.get('size') != stat.st_size:
            return True
        mtime_ns = getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1e9))
        if recorded.get('mtime_ns') == mtime_ns:
            return False
        recorded_digest = recorded.get('sha256')
        if recorded_digest is None:
            return True
        current = self._source_fingerprint()
        if current is None:
            return True
        return recorded_digest != current.get('sha256')

    def _stamp_index(self, index):
        """Attach applicability metadata to an index before it is saved.

        Only called on the write path: the source fingerprint requires reading
        the whole file, which should not slow down plain index construction.
        """
        if index is None:
            return index
        index.offset_convention = self._offset_convention
        index.parameters = self._index_parameters()
        index.source = self._source_fingerprint()
        return index

    def __len__(self):
        return len(self._offset_index)

    def __contains__(self, key):
        return key in self._offset_index

    def _item_from_offsets(self, offsets):
        raise NotImplementedError

    def get_by_id(self, elem_id):
        index = self.default_index
        if index is None:
            raise PyteomicsError('Access by ID requires building an offset index.')
        offsets = index[elem_id]
        return self._item_from_offsets(offsets)

    def get_by_ids(self, ids):
        return [self.get_by_id(key) for key in ids]

    def get_all_by_id(self, elem_id):
        """Retrieve every record sharing the given ID.

        Repeated IDs are recorded in the index's ``duplicates`` attribute.
        This method returns the record at each of those positions instead of
        only the first one. For unique IDs the result is a one-element list.
        """
        index = self.default_index
        if index is None:
            raise PyteomicsError('Access by ID requires building an offset index.')
        duplicates = getattr(index, 'duplicates', None) or {}
        offsets = duplicates.get(elem_id) if isinstance(duplicates, dict) else None
        if offsets:
            return [self._item_from_offsets(offset) for offset in offsets]
        return [self.get_by_id(elem_id)]

    def get_all_by_ids(self, ids):
        return [item for key in ids for item in self.get_all_by_id(key)]

    def get_by_index(self, i):
        try:
            key = self.default_index.from_index(i, False)
        except AttributeError:
            raise PyteomicsError('Positional access requires building an offset index.')
        return self.get_by_id(key)

    def get_by_indexes(self, indexes):
        return [self.get_by_index(i) for i in indexes]

    def get_by_index_slice(self, s):
        try:
            keys = self.default_index.from_slice(s, False)
        except AttributeError:
            raise PyteomicsError('Positional access requires building an offset index.')
        return self.get_by_ids(keys)

    def get_by_key_slice(self, s):
        keys = self.default_index.between(s.start, s.stop)
        if s.step:
            keys = keys[::s.step]
        return self.get_by_ids(keys)

    def __getitem__(self, key):
        if isinstance(key, (str, bytes)):
            return self.get_by_id(key)
        if isinstance(key, int):
            return self.get_by_index(key)
        if isinstance(key, Sequence):
            if not key:
                return []
            if isinstance(key[0], int):
                return self.get_by_indexes(key)
            if isinstance(key[0], (str, bytes)):
                return self.get_by_ids(key)
        if isinstance(key, slice):
            for item in (key.start, key.stop, key.step):
                if item is not None:
                    break
            if isinstance(item, int):
                return self.get_by_index_slice(key)
            if isinstance(item, (str, bytes)):
                return self.get_by_key_slice(key)
            if item is None:
                return list(self)
        raise PyteomicsError('Unsupported query key: {}'.format(key))


class RTLocator():
    def __init__(self, reader):
        self._reader = reader

    def _get_scan_by_time(self, time):
        """Retrieve the scan object for the specified scan time.

        Parameters
        ----------
        time : float
            The time to get the nearest scan from
        Returns
        -------
        tuple: (scan_id, scan, scan_time)
        """
        if not self._reader.default_index:
            raise PyteomicsError("This method requires the index. Please pass `use_index=True` during initialization")

        scan_ids = tuple(self._reader.default_index)
        lo = 0
        hi = len(scan_ids)

        best_match = None
        best_error = float('inf')
        best_time = None
        best_id = None

        if time == float('inf'):
            scan = self._reader.get_by_id(scan_ids[-1])
            return scan_ids[-1], scan, self._reader._get_time(scan)

        while hi != lo:
            mid = (hi + lo) // 2
            sid = scan_ids[mid]
            scan = self._reader.get_by_id(sid)
            scan_time = self._reader._get_time(scan)
            err = abs(scan_time - time)
            if err < best_error:
                best_error = err
                best_match = scan
                best_time = scan_time
                best_id = sid
            if scan_time == time:
                return sid, scan, scan_time
            elif (hi - lo) == 1:
                return best_id, best_match, best_time
            elif scan_time > time:
                hi = mid
            else:
                lo = mid

    def __getitem__(self, key):
        if isinstance(key, (int, float)):
            return self._get_scan_by_time(key)[1]
        if isinstance(key, Sequence):
            return [self._get_scan_by_time(t)[1] for t in key]
        if isinstance(key, slice):
            if key.start is None:
                start_index = self._reader.default_index.from_index(0)
            else:
                start_index = self._get_scan_by_time(key.start)[0]
            if key.stop is None:
                stop_index = self._reader.default_index.from_index(-1)
            else:
                stop_index = self._get_scan_by_time(key.stop)[0]
            return self._reader[start_index:stop_index:key.step]


class TimeOrderedIndexedReaderMixin(IndexedReaderMixin):
    @property
    def time(self):
        return self._time

    def __init__(self, *args, **kwargs):
        super(TimeOrderedIndexedReaderMixin, self).__init__(*args, **kwargs)
        self._time = RTLocator(self)

    @staticmethod
    def _get_time(scan):
        raise NotImplementedError


class IndexedTextReader(IndexedReaderMixin, FileReader):
    """Abstract class for text file readers that keep an index of records for random access.
    This requires reading the file in binary mode."""

    delimiter = None
    label = None
    block_size = 1000000
    label_group = 1
    _kw_keys = ['delimiter', 'label', 'block_size', 'label_group']
    _warn_if_empty = True
    #: Text offsets point at the first byte of a record, i.e. the label line.
    _offset_convention = 'record-start'

    def __init__(self, source, **kwargs):
        # the underlying _file_obj gets None as encoding
        # to avoid transparent decoding of StreamReader on read() calls
        encoding = kwargs.pop('encoding', 'utf-8')
        if 'warn_if_empty' in kwargs:
            self._warn_if_empty = kwargs.pop('warn_if_empty')
        super(IndexedTextReader, self).__init__(source, mode='rb', encoding=None, **kwargs)
        self.encoding = encoding
        for attr in self._kw_keys:
            if attr in kwargs:
                setattr(self, attr, kwargs.pop(attr))
        self._offset_index = None
        if not kwargs.pop('_skip_index', False):
            self._offset_index = self.build_byte_index()

    def __getstate__(self):
        state = super(IndexedTextReader, self).__getstate__()
        state['offset_index'] = self._offset_index
        for key in self._kw_keys:
            state[key] = getattr(self, key)
        return state

    def __setstate__(self, state):
        super(IndexedTextReader, self).__setstate__(state)
        self._offset_index = state['offset_index']
        for key in self._kw_keys:
            if key in state:
                setattr(self, key, state[key])

    def _chunk_iterator(self):
        fh = self._source.file
        delim = remove_bom(self.delimiter.encode(self.encoding))
        buff = fh.read(self.block_size)
        parts = buff.split(delim)
        started_with_delim = buff.startswith(delim)
        tail = parts[-1]
        front = parts[:-1]
        i = 0
        for part in front:
            i += 1
            if part == b"":
                continue
            if i == 1:
                if started_with_delim:
                    yield delim + part
                else:
                    yield part
            else:
                yield delim + part
        running = True
        while running:
            buff = fh.read(self.block_size)
            if len(buff) == 0:
                running = False
                buff = tail
            else:
                buff = tail + buff
            parts = buff.split(delim)
            tail = parts[-1]
            front = parts[:-1]
            for part in front:
                yield delim + part
        yield delim + tail

    def _generate_offsets(self):
        i = 0
        pattern = re.compile(remove_bom(self.label.encode(self.encoding)))
        for chunk in self._chunk_iterator():
            match = pattern.search(chunk)
            if match:
                label = match.group(self.label_group)
                yield i, label.decode(self.encoding), match
            i += len(chunk)
        yield i, None, None

    def build_byte_index(self):
        index = OffsetIndex()
        g = self._generate_offsets()
        last_offset = 0
        last_label = None
        conflicts = {}
        for offset, label, keyline in g:
            if last_label is not None:
                key = last_label.strip()
                span = (last_offset, offset)
                if key in index:
                    conflicts.setdefault(key, [index[key]]).append(span)
                else:
                    index[key] = span
            last_label = label
            last_offset = offset
        assert last_label is None
        index.duplicates = conflicts
        if conflicts:
            _warn_duplicates(conflicts, stacklevel=3)
        if self._warn_if_empty and not index:
            self._warn_empty()
        return index

    def _index_parameters(self):
        return {
            'delimiter': self.delimiter,
            'label': self.label,
            'label_group': self.label_group,
            'encoding': str(getattr(self, 'encoding', '') or ''),
        }

    def _read_lines_from_offsets(self, start, end):
        self._source.seek(start)
        lines = self._source.read(end - start).decode(self.encoding).split('\n')
        return lines

    def _warn_empty(self):
        warnings.warn("{} object has an empty index for file {}. If this is unexpected, consider adjusting `label`.".format(
            self.__class__.__name__, getattr(self._source, 'name', self._source_init)))


class IndexSavingMixin(NoOpBaseReader):
    """Common interface for :py:class:`IndexSavingXML` and :py:class:`IndexSavingTextReader`."""
    _index_class = NotImplemented

    @property
    def _byte_offset_filename(self):
        path = self._source_path
        if path is None:
            return None
        name, ext = os.path.splitext(path)
        byte_offset_filename = '{}-{}-byte-offsets.json'.format(name, ext[1:])
        return byte_offset_filename

    def _check_has_byte_offset_file(self):
        """Check if the file at :attr:`_byte_offset_filename` exists

        Returns
        -------
        bool
            Whether the file exists
        """
        path = self._byte_offset_filename
        if path is None:
            return False
        return os.path.exists(path)

    def _validate_index(self, index):
        """Check that a cached index can be applied to the current source."""
        expected_version = tuple(self._index_class.schema_version)
        if tuple(index.schema_version or ()) != expected_version:
            raise TypeError(
                'Inapplicable offset index schema version: {!r} (expected {!r})'.format(
                    index.schema_version, expected_version))
        if index.offset_convention != self._offset_convention:
            raise TypeError(
                'Inapplicable offset index convention: {!r} (expected {!r})'.format(
                    index.offset_convention, self._offset_convention))
        if index.parameters != self._index_parameters():
            raise TypeError('Offset index was built with different parser parameters')
        if self._index_is_stale(index):
            raise TypeError('Offset index does not match the current source file')
        if index.duplicates:
            _warn_duplicates(index.duplicates)
        return index

    @classmethod
    def prebuild_byte_offset_file(cls, path):
        """Construct a new XML reader, build its byte offset index and
        write it to file

        Parameters
        ----------
        path : str
            The path to the file to parse
        """
        with cls(path) as inst:
            inst.write_byte_offsets()

    def write_byte_offsets(self):
        """Write the byte offsets in :attr:`_offset_index` to the file
        at :attr:`_byte_offset_filename`.

        The index is written to a temporary file in the target directory and
        atomically moved into place. If index construction or writing is
        interrupted, the previous index (if any) is left untouched and no
        partially written index is published.
        """
        index = self._offset_index
        if index is None:
            raise PyteomicsError('Cannot save an empty offset index')
        self._write_index_file(index)

    def _write_index_file(self, index):
        """Atomically write `index` to :attr:`_byte_offset_filename`."""
        path = self._byte_offset_filename
        if path is None:
            raise PyteomicsError('Cannot save an offset index for a source without a file name')
        self._stamp_index(index)
        index.schema_version = tuple(self._index_class.schema_version)
        index.duplicates = getattr(index, 'duplicates', None) or {}

        directory = os.path.dirname(os.path.abspath(path))
        prefix = '.{}-'.format(os.path.basename(path))
        fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=directory)
        try:
            with os.fdopen(fd, 'w') as f:
                index.save(f)
                f.flush()
                os.fsync(f.fileno())
            try:
                mode = os.stat(path).st_mode & 0o777
            except OSError:
                mode = 0o644
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, path)
            try:
                dir_fd = os.open(directory, os.O_RDONLY)
            except OSError:
                pass
            else:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @_keepstate_method
    def build_byte_index(self):
        """Build the byte offset index by either reading these offsets
        from the file at :attr:`_byte_offset_filename`, or falling back
        to the method used by :class:`IndexedXML` or :class:`IndexedTextReader` if this operation fails
        due to an IOError
        """
        if not getattr(self, '_use_index', True): return  # indexed text readers do not have `_use_index`
        try:
            return self._read_byte_offsets()
        except (IOError, OSError):
            # The index file is missing or unreadable: build a fresh index.
            return super(IndexSavingMixin, self).build_byte_index()
        except (AttributeError, TypeError, ValueError):
            # The index file exists but is not applicable to this source or
            # to this parser (incompatible version, offset convention or
            # parser parameters, or the source has changed since it was
            # built). Rebuild it and refresh the cached copy if one exists.
            had_index_file = self._check_has_byte_offset_file()
            index = super(IndexSavingMixin, self).build_byte_index()
            if had_index_file and index is not None:
                try:
                    self._write_index_file(index)
                except (OSError, IOError):
                    # Refreshing the cache is best effort; read-only media and
                    # concurrent writers must not prevent parsing.
                    pass
            return index

    def _read_byte_offsets(self):
        """Read the byte offset index JSON file at :attr:`_byte_offset_filename`
        and populate :attr:`_offset_index`
        """
        with open(self._byte_offset_filename, 'r') as f:
            index = self._index_class.load(f)
        return self._validate_index(index)


def _file_reader(_mode='r'):
    # a lot of the code below is borrowed from
    # http://stackoverflow.com/a/14095585/1258041
    def decorator(_func):
        """A decorator implementing the context manager protocol for functions
        that read files.

        Note: 'close' must be in kwargs! Otherwise it won't be respected.
        """
        @wraps(_func)
        def helper(*args, **kwargs):
            if args:
                return FileReader(args[0], mode=_mode, parser_func=_func, pass_file=True, args=args[1:], kwargs=kwargs,
                                  encoding=kwargs.pop('encoding', None))
            source = kwargs.pop('source', None)
            return FileReader(source, mode=_mode, parser_func=_func, pass_file=True, args=(), kwargs=kwargs, encoding=kwargs.pop('encoding', None))
        return helper
    return decorator


def _file_writer(_mode='w'):
    def decorator(_func):
        """A decorator that opens output files for writer functions.
        """
        @wraps(_func)
        def helper(*args, **kwargs):
            m = kwargs.pop('file_mode', _mode)
            enc = kwargs.pop('encoding', None)
            if len(args) > 1:
                out_arg = args[1]
            else:
                out_arg = kwargs.pop('output', None)

            with _file_obj(out_arg, m, encoding=enc) as out:
                if len(args) > 1:
                    call_args = (args[0], out) + args[2:]
                    call_kwargs = kwargs
                else:
                    call_args = args
                    call_kwargs = dict(output=out, **kwargs)
                return _func(*call_args, **call_kwargs)
        return helper
    return decorator


class WritableIndex(object):
    schema_version = (2, 0, 0)
    _schema_version_tag_key = "@pyteomics_schema_version"
    _offset_convention_tag_key = "@pyteomics_offset_convention"
    _parameters_tag_key = "@pyteomics_index_parameters"
    _source_tag_key = "@pyteomics_source"
    _duplicates_tag_key = "@pyteomics_duplicate_ids"

    def _init_metadata(self):
        self.schema_version = type(self).schema_version
        self.offset_convention = None
        self.parameters = {}
        self.source = None
        self.duplicates = {}

    def _serializable_container(self):
        container = {'index': list(self.items())}
        return container

    def save(self, fp):
        container = self._serializable_container()
        container[self._schema_version_tag_key] = self.schema_version
        container[self._offset_convention_tag_key] = self.offset_convention
        container[self._parameters_tag_key] = self.parameters
        container[self._source_tag_key] = self.source
        container[self._duplicates_tag_key] = self.duplicates
        json.dump(container, fp)

    @classmethod
    def load(cls, fp):
        container = json.load(fp, object_hook=OrderedDict)
        version_tag = container.get(cls._schema_version_tag_key)
        if version_tag is None:
            # The legacy case, no special processing yet
            inst = cls()
            inst.schema_version = None
            return inst
        try:
            version_tag = tuple(version_tag)
        except TypeError:
            inst = cls()
            inst.schema_version = version_tag
            return inst
        index = container.get("index")
        inst = cls(index) if index is not None else cls()
        inst.schema_version = version_tag
        inst.offset_convention = container.get(cls._offset_convention_tag_key)
        inst.parameters = container.get(cls._parameters_tag_key) or {}
        inst.source = container.get(cls._source_tag_key)
        inst.duplicates = container.get(cls._duplicates_tag_key) or {}
        return inst

def _describe_duplicates(conflicts):
    """Return a human-readable listing of conflicting record IDs."""
    lines = []
    for element_type in sorted(conflicts, key=str):
        records = conflicts[element_type]
        if isinstance(records, dict):
            for key in sorted(records, key=str):
                offsets = records[key]
                lines.append('{!r} ({}) at {}'.format(
                    key, element_type, ', '.join(str(offset) for offset in offsets)))
        else:
            offsets = records
            lines.append('{!r} at {}'.format(
                element_type, ', '.join(str(offset) for offset in offsets)))
    return '; '.join(lines)


class DuplicateIDWarning(UserWarning):
    """Warn about records sharing the same identifier.

    The message lists every conflicting identifier together with all of its
    positions. The complete mapping is stored on the index as its
    ``duplicates`` attribute, so no occurrence is silently discarded.
    """


def _warn_duplicates(conflicts, stacklevel=3):
    warnings.warn(
        'Duplicate record IDs found while building the offset index: {}. '
        'All positions are recorded in the index `duplicates` attribute; '
        'lookup returns the first occurrence.'.format(
            _describe_duplicates(conflicts)),
        DuplicateIDWarning, stacklevel=stacklevel)
    return conflicts


class OffsetIndex(OrderedDict, WritableIndex):
    '''An augmented OrderedDict that formally wraps getting items by index
    '''

    def __init__(self, *args, **kwargs):
        super(OffsetIndex, self).__init__(*args, **kwargs)
        self._index_sequence = None
        self._init_metadata()

    def _invalidate(self):
        self._index_sequence = None

    @property
    def index_sequence(self):
        """Keeps a cached copy of the :meth:`items` sequence
        stored as a :class:`tuple` to avoid repeatedly copying
        the sequence over many method calls.

        Returns
        -------
        :class:`tuple`
        """
        if self._index_sequence is None:
            self._index_sequence = tuple(self.items())
        return self._index_sequence

    def __setitem__(self, key, value):
        self._invalidate()
        return super(OffsetIndex, self).__setitem__(key, value)

    def pop(self, *args, **kwargs):
        self._invalidate()
        return super(OffsetIndex, self).pop(*args, **kwargs)

    def find(self, key, *args, **kwargs):
        return self[key]

    def from_index(self, index, include_value=False):
        '''Get an entry by its integer index in the ordered sequence
        of this mapping.

        Parameters
        ----------
        index: int
            The index to retrieve.
        include_value: bool
            Whether to return both the key and the value or just the key.
            Defaults to :const:`False`.

        Returns
        -------
        object:
            If ``include_value`` is :const:`True`, a tuple of (key, value) at ``index``
            else just the key at ``index``.
        '''
        items = self.index_sequence
        if include_value:
            return items[index]
        else:
            return items[index][0]

    def from_slice(self, spec, include_value=False):
        '''Get a slice along index in the ordered sequence
        of this mapping.

        Parameters
        ----------
        spec: slice
            The slice over the range of indices to retrieve
        include_value: bool
            Whether to return both the key and the value or just the key.
            Defaults to :const:`False`

        Returns
        -------
        list:
            If ``include_value`` is :const:`True`, a tuple of (key, value) at ``index``
            else just the key at ``index`` for each ``index`` in ``spec``
        '''
        items = self.index_sequence
        return [(k, v) if include_value else k for k, v in items[spec]]

    def between(self, start, stop, include_value=False):
        keys = list(self)
        if start is not None:
            try:
                start_index = keys.index(start)
            except ValueError:
                raise KeyError(start)
        else:
            start_index = 0
        if stop is not None:
            try:
                stop_index = keys.index(stop)
            except ValueError:
                raise KeyError(stop)
        else:
            stop_index = len(keys) - 1
        if start is None or stop is None:
            pass  # won't switch indices
        else:
            start_index, stop_index = min(start_index, stop_index), max(start_index, stop_index)

        if include_value:
            return [(k, self[k]) for k in keys[start_index:stop_index + 1]]
        return keys[start_index:stop_index + 1]

    def __repr__(self):
        template = "{self.__class__.__name__}({items})"
        return template.format(self=self, items=list(self.items()))

    def _integrity_check(self):
        indices = list(self.values())
        sorted_indices = sorted(self.values())
        return indices == sorted_indices

    def sort(self):
        sorted_pairs = sorted(self.items(), key=lambda x: x[1])
        self.clear()
        self._invalidate()
        for key, value in sorted_pairs:
            self[key] = value
        return self


class IndexSavingTextReader(IndexSavingMixin, IndexedTextReader):
    _index_class = OffsetIndex


class HierarchicalOffsetIndex(WritableIndex):
    _inner_type = OffsetIndex

    def __init__(self, base=None):
        self.mapping = defaultdict(self._inner_type)
        for key, value in (base or {}).items():
            self.mapping[key] = self._inner_type(value)
        self._init_metadata()

    def _integrity_check(self):
        for key, value in self.items():
            if not value._integrity_check():
                return False
        return True

    def sort(self):
        for key, value in self.items():
            value.sort()
        return self

    def __getitem__(self, key):
        return self.mapping[key]

    def __setitem__(self, key, value):
        self.mapping[key] = value

    def __iter__(self):
        return iter(self.mapping)

    def __len__(self):
        return sum(len(group) for key, group in self.items())

    def __contains__(self, key):
        return key in self.mapping

    def find(self, key, element_type=None):
        if element_type is None:
            for element_type in self.keys():
                try:
                    return self.find(key, element_type)
                except KeyError:
                    continue
            raise KeyError(key)
        else:
            return self[element_type][key]

    def find_no_type(self, key):
        """Try to find `key` in each of the lower-level indexes, returning both
        value and the element type that match the key."""
        for element_type in self.keys():
            try:
                return self.find(key, element_type), element_type
            except KeyError:
                continue
        raise KeyError(key)

    def update(self, *args, **kwargs):
        self.mapping.update(*args, **kwargs)

    def pop(self, key, default=None):
        return self.mapping.pop(key, default)

    def keys(self):
        return self.mapping.keys()

    def values(self):
        return self.mapping.values()

    def items(self):
        return self.mapping.items()

    def _serializable_container(self):
        encoded_index = {}
        container = {
            'keys': list(self.keys())
        }
        for key, offset in self.items():
            encoded_index[key] = list(offset.items())
        container['index'] = encoded_index
        return container


def _make_chain(reader, readername, full_output=False):

    def concat_results(*args, **kwargs):
        results = [reader(arg, **kwargs) for arg in args]
        if pd is not None and all(isinstance(a, pd.DataFrame) for a in args):
            return pd.concat(results)
        return np.concatenate(results)

    def _iter(files, kwargs):
        for f in files:
            with reader(f, **kwargs) as r:
                for item in r:
                    yield item

    def chain(*files, **kwargs):
        return _iter(files, kwargs)

    def from_iterable(files, **kwargs):
        return _iter(files, kwargs)

    @contextmanager
    def _chain(*files, **kwargs):
        yield chain(*files, **kwargs)

    @contextmanager
    def _from_iterable(files, **kwargs):
        yield from_iterable(files, **kwargs)

    def dispatch(*args, **kwargs):
        return dispatch_from_iterable(args, **kwargs)

    def dispatch_from_iterable(args, **kwargs):
        if kwargs.get('full_output', full_output):
            return concat_results(*args, **kwargs)
        return _chain(*args, **kwargs)

    dispatch.__doc__ = """Chain :py:func:`{0}` for several files.
        Positional arguments should be file names or file objects.
        Keyword arguments are passed to the :py:func:`{0}` function.
        """.format(readername)
    dispatch_from_iterable.__doc__ = """Chain :py:func:`{0}` for several files.
        Keyword arguments are passed to the :py:func:`{0}` function.

        Parameters
        ----------
        files : iterable
            Iterable of file names or file objects.
        """.format(readername)
    dispatch.from_iterable = dispatch_from_iterable

    return dispatch


def _check_use_index(source, use_index, default):
    try:
        if use_index is not None:
            use_index = bool(use_index)

        # if a file name is given, do not override anything; short-circuit
        if isinstance(source, (str, bytes)):
            return use_index if use_index is not None else default

        # collect information on source
        if hasattr(source, 'seekable'):
            seekable = source.seekable()
        else:
            seekable = None

        if hasattr(source, 'mode'):
            binary = 'b' in source.mode
        else:
            binary = None

        # now check for conflicts
        if seekable is False:
            if binary:
                raise PyteomicsError('Cannot work with non-seekable file in binary mode: {}.'.format(source))
            if use_index:
                warnings.warn('Cannot use indexing as {} is not seekable. Setting `use_index` to False.'.format(source))
                use_index = False
        elif binary is not None:
            if use_index is not None and binary != use_index:
                warnings.warn('use_index is {}, but the file mode is {}. '
                              'Setting `use_index` to {}'.format(use_index, source.mode, binary))
            use_index = binary
        elif use_index is None:
            warnings.warn('Could not check mode on {}. Specify `use_index` explicitly to avoid errors.'.format(source))

        if use_index is not None:
            return use_index

        return default

    except PyteomicsError:
        raise
    except Exception as e:
        if use_index is None:
            warnings.warn('Could not check mode on {}. Reason: {!r}. '
                          'Specify `use_index` explicitly to avoid errors.'.format(source, e))
            return default
        return use_index


class FileReadingProcess(ctx.Process):
    """Process that does a share of distributed work on entries read from file.
    Reconstructs a reader object, parses an entries from given indexes,
    optionally does additional processing, sends results back.

    The reader class must support the :py:meth:`__getitem__` dict-like lookup.
    """

    _event_class = ctx.Event

    def __init__(self, reader_spec, target_spec, qin, qout, args_spec, kwargs_spec):
        super(FileReadingProcess, self).__init__(name='pyteomics-map-worker')
        self.reader_spec = reader_spec
        self.target_spec = target_spec
        self.args_spec = args_spec
        self.kwargs_spec = kwargs_spec
        self._qin = qin
        self._qout = qout
        # self._in_flag = in_flag
        self._done_flag = self._event_class()
        self.daemon = True

    def run(self):
        reader = serializer.loads(self.reader_spec)
        target = serializer.loads(self.target_spec)
        args = serializer.loads(self.args_spec)
        kwargs = serializer.loads(self.kwargs_spec)
        for key in iter(self._qin.get, None):
            item = reader[key]
            if target is not None:
                result = target(item, *args, **kwargs)
            else:
                result = item
            self._qout.put(result)
        self._done_flag.set()

    def is_done(self):
        return self._done_flag.is_set()


try:
    _NPROC = mp.cpu_count()
except NotImplementedError:
    _NPROC = 4
_QUEUE_TIMEOUT = 4
_QUEUE_SIZE = int(1e7)


class BaseTaskMappingMixin(NoOpBaseReader):
    def _task_map_iterator(self):
        """Returns the :class:`Iterable` to use when dealing work items onto the input
        queue used by :meth:`map`

        Returns
        -------
        :class:`Iterable`
        """

        return iter(self._offset_index.keys())


class MultiProcessingTaskMappingMixin(BaseTaskMappingMixin):
    def __init__(self, *args, **kwargs):
        """
        Instantiate a :py:class:`MultiProcessingTaskMappingMixin` object, set default parameters for IPC.

        Parameters
        ----------

        queue_timeout : float, keyword only, optional
            The number of seconds to block, waiting for a result before checking to see if
            all workers are done.
        queue_size : int, keyword only, optional
            The length of IPC queue used.
        workers : int, keyword only, optional
            Number of worker processes or threads to spawn when :py:meth:`map` is called. This can also be
            specified in the :py:meth:`map` call.
        """
        self._queue_size = kwargs.pop('queue_size', _QUEUE_SIZE)
        self._queue_timeout = kwargs.pop('timeout', _QUEUE_TIMEOUT)
        self._nproc = kwargs.pop('workers', _NPROC)
        super().__init__(*args, **kwargs)

    def _get_reader_for_worker_spec(self):
        return self

    def _build_worker_spec(self, target, args, kwargs):
        serialized = []
        for obj, objname in [(self._get_reader_for_worker_spec(), 'reader'), (target, 'target'), (args, 'args'),
                             (kwargs, 'kwargs')]:
            try:
                serialized.append(serializer.dumps(obj))
            except serializer.PicklingError:
                msg = 'Could not serialize {0} {1} with {2.__name__}.'.format(objname, obj, serializer)
                if serializer is not dill:
                    msg += ' Try installing `dill`.'
                raise PyteomicsError(msg)
        return serialized

    def _spawn_workers(self, specifications, in_queue, out_queue, processes):
        reader_spec, target_spec, args_spec, kwargs_spec = specifications
        workers = []
        for _ in range(processes):
            worker = FileReadingProcess(
                reader_spec, target_spec, in_queue, out_queue, args_spec, kwargs_spec)
            workers.append(worker)
        return workers

    def _spawn_feeder_thread(self, in_queue, iterator, processes):
        def feeder():
            for key in iterator:
                in_queue.put(key)
            for _ in range(processes):
                in_queue.put(None)

        feeder_thread = threading.Thread(target=feeder)
        feeder_thread.daemon = True
        feeder_thread.start()
        return feeder_thread

    def map(self, target=None, workers=None, args=None, kwargs=None, **_kwargs):
        """Execute the ``target`` function over entries of this object across up to ``workers``
        processes.

        Results will be returned out of order.

        Parameters
        ----------
        target : :class:`Callable`, optional
            The function to execute over each entry. It will be given a single object yielded by
            the wrapped iterator as well as all of the values in ``args`` and ``kwargs``.
        workers : int or None, optional
            The number of worker processes to use. If not a positive integer,
            defaults to the number of available CPUs.
            This parameter can also be set at reader creation.
        args : :class:`Sequence`, optional
            Additional positional arguments to be passed to the target function.
        kwargs : :class:`Mapping`, optional
            Additional keyword arguments to be passed to the target function.
        **_kwargs
            Additional keyword arguments to be passed to the target function.

        Yields
        ------
        object
            The work item returned by the target function.
        """

        if workers is None or workers < 1:
            workers = self._nproc
        iterator = self._task_map_iterator()

        if args is None:
            args = tuple()
        else:
            args = tuple(args)
        if kwargs is None:
            kwargs = dict()
        else:
            kwargs = dict(kwargs)
        kwargs.update(_kwargs)

        serialized = self._build_worker_spec(target, args, kwargs)

        in_queue = ctx.Queue(self._queue_size)
        out_queue = ctx.Queue(self._queue_size)

        worker_procs = self._spawn_workers(serialized, in_queue, out_queue, workers)
        feeder_thread = self._spawn_feeder_thread(in_queue, iterator, workers)
        for worker in worker_procs:
            worker.start()

        def iterate():
            while True:
                try:
                    result = out_queue.get(True, self._queue_timeout)
                    yield result
                except Empty:
                    if all(w.is_done() for w in worker_procs):
                        break
                    else:
                        continue

            feeder_thread.join()
            for worker in worker_procs:
                worker.join()
        return iterate()


class ThreadingTaskMappingMixin(BaseTaskMappingMixin):
    """
    Mixin implementing thread-based parallelism for :py:meth:`map` calls.
    The use of threaded parallelism is warranted in free-threaded environments,
    such as the `experimental free-threaded CPython interpreter <https://docs.python.org/3/howto/free-threading-python.html>`_.
    """
    chunk_size: int = 1

    def map(self, target=None, workers=None, args=None, kwargs=None, chunk_size=None, **_kwargs):
        """
        Execute the ``target`` function over entries of this object across up to ``workers`` threads.

        Results will be returned out of order.

        Parameters
        ----------
        target : :class:`Callable`, optional
            The function to execute over each entry. It will be given a single object yielded by
            the wrapped iterator as well as all of the values in ``args`` and ``kwargs``.

            .. warning::
                `target` must be thread-safe. The target function cannot interact with the underlying file object directly.

        workers : int or None, optional
            The number of worker threads to use. If not a positive integer,
            defaults to the number of available CPUs.
        args : :class:`Sequence`, optional
            Additional positional arguments to be passed to the target function.
        kwargs : :class:`Mapping`, optional
            Additional keyword arguments to be passed to the target function.
        chunk_size : int, optional
            The number of work items to hand out to each worker thread at a time. If not specified,
            defaults to :attr:`chunk_size` attribute of this object.
        **_kwargs
            Additional keyword arguments to be passed to the target function.

        Yields
        ------
        object
            The work item returned by the target function.
        """
        if args is None:
            args = tuple()
        else:
            args = tuple(args)
        if kwargs is None:
            kwargs = dict()
        else:
            kwargs = dict(kwargs)
        if chunk_size is None:
            chunk_size = self.chunk_size
        kwargs.update(_kwargs)

        # Determine number of workers
        if workers is None or workers < 1:
            workers = _NPROC

        # Queues for distributing work and collecting results
        output_queue = queue.Queue()
        key_queue = queue.Queue()
        worker_threads = []
        _SENTINEL = object()

        def producer():
            """Iterate over task keys in a single thread and enqueue them for workers."""
            try:
                batch = []
                for key in self._task_map_iterator():
                    batch.append(key)
                    if len(batch) >= chunk_size:
                        key_queue.put(batch)
                        batch = []
                if batch:
                    key_queue.put(batch)
            finally:
                # Signal completion to all worker threads
                for _ in range(workers):
                    key_queue.put(_SENTINEL)

        def thread_worker():
            # Initialize thread-local reader copy on first access
            local_reader = copy(self)
            try:
                while True:
                    batch = key_queue.get()
                    if batch is _SENTINEL:
                        # Do not put the sentinel back; each worker consumes one
                        break
                    for key in batch:
                        try:
                            item = local_reader[key]
                            if target is not None:
                                item = target(item, *args, **kwargs)
                            output_queue.put((True, item))
                        except Exception as e:
                            output_queue.put((False, e))
            finally:
                # Explicitly close the thread-local reader copy
                if hasattr(local_reader, 'close'):
                    local_reader.close()

        # Start producer thread to feed keys to workers
        producer_thread = threading.Thread(
            target=producer,
            name=f'pyteomics-{type(self).__name__.lower()}-producer'
        )
        producer_thread.daemon = True
        producer_thread.start()
        # Spawn worker threads
        for _ in range(workers):
            t = threading.Thread(
                target=thread_worker,
                name=f'pyteomics-{type(self).__name__.lower()}-worker'
            )
            t.daemon = True
            t.start()
            worker_threads.append(t)

        def iterate():
            results_received = 0
            total_items = len(self._offset_index)  # must exist for map() to work

            while results_received < total_items:
                try:
                    success, result = output_queue.get(timeout=_QUEUE_TIMEOUT)
                    results_received += 1
                    if success:
                        yield result
                    else:
                        raise result
                except queue.Empty:
                    # Check if all threads are still alive
                    if not any(t.is_alive() for t in worker_threads):
                        break
                    continue

            # Wait for all worker threads to finish
            for t in worker_threads:
                t.join()

        return iterate()


class TaskMappingMixin(MultiProcessingTaskMappingMixin, ThreadingTaskMappingMixin):
    pmap = MultiProcessingTaskMappingMixin.map
    tmap = ThreadingTaskMappingMixin.map

    def _has_index(self) -> bool:
        return self._offset_index is not None

    def map(self, target=None, workers=None, args=None, kwargs=None, method='mp', **_kwargs):
        """
        Execute the ``target`` function over entries of this object in parallel.
        The type of parallelism is determined by the ``method`` parameter.

        Results will be returned out of order.

        Parameters
        ----------
        target : :class:`Callable`, optional
            The function to execute over each entry. It will be given a single object yielded by
            the wrapped iterator as well as all of the values in ``args`` and ``kwargs``.
        workers : int, optional
            The number of worker threads or processes to use. The default depends on the ``method`` parameter.
        args : :class:`Sequence`, optional
            Additional positional arguments to be passed to the target function.
        kwargs : :class:`Mapping`, optional
            Additional keyword arguments to be passed to the target function.
        method : str, optional
            The type of parallelism to use. Can be one of the following:

            - either one of 'p', 'mp', 'processes', or 'multiprocessing': use multiprocessing
              This is the default. This is also equivalent to calling :meth:`pmap`, see there for details.

            - either one of 't', 'threading', or 'threads': use threading
              This is also equivalent to calling :meth:`tmap`, see there for details.
        **_kwargs
            Additional keyword arguments to be passed to the target function.

        Yields
        ------
        object
            The work item returned by the target function.
        """
        if not self._has_index():
            raise PyteomicsError('The reader needs an index for map() calls. Create the reader with `use_index=True`.')

        if method in {'p', 'mp', 'multiprocessing', 'processes'}:
            # Always pop 'processes' from _kwargs if present
            if 'processes' in _kwargs:
                if workers is not None and workers > 0:
                    raise PyteomicsError(
                        "Cannot specify both 'workers' and legacy 'processes' arguments. "
                        "Please use only 'workers'."
                    )
                # accept `processes` for backward compatibility
                workers = _kwargs.pop('processes', None)

            return self.pmap(target=target, workers=workers, args=args, kwargs=kwargs, **_kwargs)
        if method in {'t', 'threading', 'threads'}:
            return self.tmap(target=target, workers=workers, args=args, kwargs=kwargs, **_kwargs)
        raise PyteomicsError(f'Invalid value of `method`: {method}.')


class ChainBase(TaskMappingMixin):
    """Chain :meth:`sequence_maker` for several sources into a
    single iterable. Positional arguments should be sources like
    file names or file objects. Keyword arguments are passed to
    the :meth:`sequence_maker` function.

    Parameters
    ----------
    sources : :class:`Iterable`
        Sources for creating new sequences from, such as paths or
        file-like objects
    kwargs : :class:`Mapping`
        Additional arguments used to instantiate each sequence
    """

    def __init__(self, *sources, **kwargs):
        self.sources = sources
        self.kwargs = kwargs
        self._iterator = None

    @classmethod
    def from_iterable(cls, sources, **kwargs):
        return cls(*sources, **kwargs)

    @classmethod
    def _make_chain(cls, sequence_maker):
        if isinstance(sequence_maker, type):
            tp = type('%sChain' % sequence_maker.__class__.__name__, (cls,), {
                'sequence_maker': sequence_maker,
                '__doc__': cls.__doc__.replace(':meth:`sequence_maker`', ':class:`{}`'.format(sequence_maker.__name__))
            })
        else:
            tp = type('FunctionChain', (cls,), {
                'sequence_maker': staticmethod(sequence_maker),
                '__doc__': cls.__doc__.replace(':meth:`sequence_maker`', ':func:`{}`'.format(sequence_maker.__name__))
            })
        return tp

    def sequence_maker(self, file):
        raise NotImplementedError()

    def _create_sequence(self, file):
        return self.sequence_maker(file, **self.kwargs)

    def _iterate_over_series(self):
        for f in self.sources:
            with self._create_sequence(f) as r:
                for item in r:
                    yield item

    def __enter__(self):
        self._iterator = iter(self._iterate_over_series())
        return self

    def __exit__(self, *args, **kwargs):
        self._iterator = None

    def __iter__(self):
        return self

    def __next__(self):
        if self._iterator is None:
            self._iterator = self._iterate_over_series()
        return next(self._iterator)

    def next(self):
        return self.__next__()

    def _has_index(self):
        return True  # do not create all indexed parsers in advance, check them later

    def _map(self, method_name, *args, **kwargs):
        for f in self.sources:
            with self._create_sequence(f) as r:
                if not r._has_index():
                    raise PyteomicsError(f'The reader for {f} needs an index for map() calls. Create the reader with `use_index=True`: {r!r}')
                method = getattr(r, method_name)
                for result in method(*args, **kwargs):
                    yield result

    def tmap(self, *args, **kwargs):
        return self._map('tmap', *args, **kwargs)

    def pmap(self, *args, **kwargs):
        return self._map('pmap', *args, **kwargs)


class TableJoiner(ChainBase):
    def concatenate(self, results):
        if pd is not None and all(isinstance(a, pd.DataFrame) for a in results):
            return pd.concat(results)
        if isinstance(results[0], np.ndarray):
            return np.concatenate(results)
        else:
            return np.array([b for a in results for b in a])

    def _iterate_over_series(self):
        results = [self._create_sequence(f) for f in self.sources]
        return self.concatenate(results)
