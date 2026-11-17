"""Results of randomly reading a range of string samples from a TDMS channel"""


class StringReadResult(object):
    """String samples read from part of a channel, with their locations in the file

    ``samples`` and ``source_ranges`` have the same length and are in sample order.
    Each source range is an ``(offset, length)`` byte range in the TDMS data file
    containing the bytes for that sample, before text decoding.  Empty strings have
    a zero length range at the position where their (empty) payload is stored.

    If a chunk in the requested range has invalid string offsets, reading stops at
    that chunk.  Samples from earlier chunks and valid samples before the bad offset
    in the affected chunk are still returned in ``samples`` and the problem is
    described by ``error``, an instance of
    :class:`nptdms.types.StringOffsetError`.  Samples after the bad offset and from
    later chunks are not returned, so bytes from a later chunk are never joined to
    a string from the affected chunk.

    :ivar samples: List of string values.
    :ivar source_ranges: List of ``(offset, length)`` byte ranges, one per sample,
        or None if the channel data was already read into memory and source ranges
        are no longer available.
    :ivar error: The error that stopped reading, or None if the whole range was read.
    """

    def __init__(self, samples=None, source_ranges=None, error=None):
        self.samples = [] if samples is None else samples
        self.source_ranges = source_ranges
        self.error = error

    @property
    def ranges_available(self):
        """Whether source byte ranges are available for these samples"""
        return self.source_ranges is not None

    @property
    def ranges(self):
        """Alias for :attr:`source_ranges`"""
        return self.source_ranges

    @property
    def values(self):
        """Alias for :attr:`samples`"""
        return self.samples

    @property
    def complete(self):
        """Whether the requested range was read without an error"""
        return self.error is None

    def __len__(self):
        return len(self.samples)

    def __iter__(self):
        return iter(self.samples)

    def __getitem__(self, index):
        return self.samples[index]

    def __repr__(self):
        return "<StringReadResult with %d samples%s>" % (
            len(self.samples), "" if self.error is None else ", stopped by %s" % self.error)
