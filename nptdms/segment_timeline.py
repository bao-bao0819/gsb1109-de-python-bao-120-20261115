""" Time information for channels that is valid per TDMS segment.

A TDMS channel can be written over several segments with different waveform
properties.  A single increment for the whole channel, or a single time track
covering all values, cannot describe that data when the increment changes or
when acquisition was stopped between segments.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple, Optional

import numpy as np

from nptdms.timestamp import TdmsTimestamp


_ACCURACY_UNITS = {
    's': 1e0,
    'ms': 1e3,
    'us': 1e6,
    'ns': 1e9,
}


class AmbiguousTimeError(ValueError):
    """ Raised when a time maps to more than one sample.

    This happens when segments overlap in time.  Use
    :meth:`ChannelSegmentTimeline.samples_at_time` to retain all sources
    rather than selecting one.
    """


class SamplePosition(NamedTuple):
    """ A sample at a particular time

    :ivar sample_index: Index in the complete channel.
    :ivar segment_index: Index of the physical TDMS segment containing the sample.
    :ivar segment_sample_index: Index within that segment's data for the channel.
    :ivar time: The sample's time as a datetime64 or float relative to the timeline start.
    """

    sample_index: int
    segment_index: int
    segment_sample_index: int
    time: object


@dataclass(frozen=True)
class SegmentTimeSpan:
    """ The data and time range contributed by one TDMS segment.

    ``start_index`` is the first sample's index in the complete channel and
    ``stop_index`` is exclusive.  A span always describes real samples, so
    a stopped acquisition appears as a time gap between spans rather than as
    samples in the gap.
    """

    segment_index: int
    start_index: int
    sample_count: int
    increment: float
    start_offset: float
    wf_start_time: Optional[object]
    relative_start: float
    accuracy: str = 'ns'
    gap_before: bool = False
    gap_segments: tuple = ()
    clock_continuous: bool = True
    start_time_known: bool = True
    absolute_start_time: Optional[object] = None
    has_absolute_start: bool = False
    relative_anchor: float = 0.0

    @property
    def start_sample_index(self):
        """ First sample index in the complete channel """
        return self.start_index

    @property
    def stop_sample_index(self):
        """ Index just after the last sample in the complete channel """
        return self.stop_index

    @property
    def last_sample_index(self):
        """ Last sample index in the complete channel, or None when empty """
        return self.last_index

    @property
    def sample_range(self):
        """ ``(start_index, stop_index)`` for this span """
        return (self.start_index, self.stop_index)

    @property
    def wf_increment(self):
        """ Alias for :attr:`increment` """
        return self.increment

    @property
    def stop_time(self):
        """ Alias for :attr:`end_time` """
        return self.end_time

    @property
    def stop_index(self):
        """ Index just after the last sample in this span """
        return self.start_index + self.sample_count

    @property
    def start_sample(self):
        """ Alias for :attr:`start_index` """
        return self.start_index

    @property
    def stop_sample(self):
        """ Alias for :attr:`stop_index` """
        return self.stop_index

    @property
    def last_index(self):
        """ Index of the last sample in this span, or None for an empty span """
        return self.stop_index - 1 if self.sample_count else None

    @property
    def offset(self):
        """ Alias for :attr:`start_offset` """
        return self.start_offset

    @property
    def start_time(self):
        """ Absolute time of the first sample, or None if this span has no absolute anchor """
        if not self.has_absolute_start:
            return None
        return self._absolute_time_at(0)

    @property
    def end_time(self):
        """ Absolute time of the last sample, or None if this span has no absolute anchor """
        if not self.has_absolute_start or self.sample_count == 0:
            return None
        return self._absolute_time_at(self.sample_count - 1)

    def relative_times(self):
        """ Time of each sample relative to the timeline start, as seconds """
        return self._linspace(self.relative_start)

    def times(self, absolute_time=False, accuracy=None):
        """ Return this span's sample times

        :param absolute_time: Return absolute UTC times rather than seconds
            relative to the start of the timeline.
        :param accuracy: NumPy datetime64 unit for absolute times.
            Defaults to the accuracy used when the timeline was created.
        """
        if not absolute_time:
            return self.relative_times()
        accuracy = self.accuracy if accuracy is None else accuracy
        if not self.has_absolute_start:
            raise KeyError("Object does not have start time property available.")
        return self._absolute_times(accuracy)

    def time_at(self, segment_sample_index, absolute_time=False, accuracy=None):
        """ Time for a sample index local to this segment """
        if segment_sample_index < 0 or segment_sample_index >= self.sample_count:
            raise IndexError(
                "Segment sample index {0} is outside of the span bounds [0, {1}]".format(
                    segment_sample_index, self.sample_count - 1))
        if absolute_time:
            accuracy = self.accuracy if accuracy is None else accuracy
            if not self.has_absolute_start:
                raise KeyError("Object does not have start time property available.")
            return self._absolute_time_at(segment_sample_index, accuracy)
        return self._relative_time_at(segment_sample_index)

    def local_indices_at_time(self, time, absolute_time=None):
        """ Local sample indices in this span matching ``time`` """
        if absolute_time is None:
            absolute_time = _is_absolute_time(time)
        if self.sample_count == 0:
            return []
        if absolute_time:
            if not self.has_absolute_start:
                return []
            return self._absolute_local_indices_at_time(time)
        return self._relative_local_indices_at_time(time)

    def to_dict(self, absolute_time=False, accuracy=None):
        """ Return a JSON-friendly description of this span """
        start_time = self.start_time if absolute_time else self.relative_start
        end_time = self.end_time if absolute_time else (
            self._relative_time_at(self.sample_count - 1) if self.sample_count else None)
        return {
            'segment_index': self.segment_index,
            'start_index': self.start_index,
            'stop_index': self.stop_index,
            'sample_count': self.sample_count,
            'increment': self.increment,
            'start_offset': self.start_offset,
            'start_time': _json_time(start_time),
            'end_time': _json_time(end_time),
            'gap_before': self.gap_before,
            'gap_segments': list(self.gap_segments),
            'clock_continuous': self.clock_continuous,
            'start_is_absolute': self.has_absolute_start,
            'start_is_unknown': self.has_unknown_start,
        }

    @property
    def has_unknown_start(self):
        """ Whether this span has no absolute anchor and no clock continuation """
        return not self.start_time_known

    @property
    def start_is_unknown(self):
        """ Alias for :attr:`has_unknown_start` """
        return self.has_unknown_start

    def _linspace(self, start):
        count = self.sample_count
        if count == 0:
            return np.empty(0, dtype=np.float64)
        # Use the same arithmetic as the scalar lookup so vectorized times and
        # time_at_sample agree bit-for-bit at every index.
        return start + np.arange(count, dtype=np.float64) * self.increment

    def _step(self):
        return self.increment

    def _relative_time_at(self, segment_sample_index):
        return self.relative_start + segment_sample_index * self._step()

    def _relative_local_indices_at_time(self, time):
        index = self._nearest_local_index(
            float(time) - self.relative_start, self.increment)
        if index is None:
            return []
        candidate = self._relative_time_at(index)
        if _times_equal(candidate, time, self.increment):
            return [index]
        return []

    def _absolute_times(self, accuracy):
        relative = self._linspace(self.relative_start) - self.relative_anchor
        return self.absolute_start_time.astype('datetime64[{0}]'.format(accuracy)) + _relative_deltas(
            relative, accuracy)

    def _absolute_time_at(self, segment_sample_index, accuracy=None):
        accuracy = self.accuracy if accuracy is None else accuracy
        relative = np.array([
            self._relative_time_at(segment_sample_index) - self.relative_anchor
        ])
        return self.absolute_start_time.astype(
            'datetime64[{0}]'.format(accuracy)) + _relative_deltas(relative, accuracy)[0]

    def _absolute_local_indices_at_time(self, time):
        index = self._nearest_local_index_from_absolute(time)
        if index is None:
            return []
        candidate = self._absolute_time_at(index, _time_accuracy(time))
        if _times_equal(candidate, time, None):
            return [index]
        return []

    def _nearest_local_index_from_absolute(self, time):
        if self.sample_count == 1:
            return 0
        accuracy = _time_accuracy(time)
        first = self._absolute_time_at(0, accuracy)
        step_ns = _datetime64_delta(self._absolute_time_at(1, accuracy) - first)
        if step_ns == 0:
            return 0 if _times_equal(first, time, None) else None
        delta_ns = _datetime64_delta(
            np.datetime64(time, 'ns') - np.datetime64(first, 'ns'))
        index = int(round(delta_ns / step_ns))
        if index < 0 or index >= self.sample_count:
            return None
        return index

    def _nearest_local_index(self, relative_seconds, increment):
        if self.sample_count == 1:
            return 0
        step = self._step()
        if step == 0:
            return 0 if _times_equal(relative_seconds, 0, increment) else None
        index = int(round(relative_seconds / step))
        if index < 0 or index >= self.sample_count:
            return None
        return index


class ChannelSegmentTimeline:
    """ The per-segment time information for one channel.

    Iterating over the timeline yields :class:`SegmentTimeSpan` objects in
    physical file order.  Times are never sorted or de-duplicated, so
    overlapping segments keep both of their data sources.
    """

    def __init__(self, spans, accuracy='ns'):
        self._spans = tuple(spans)
        self.accuracy = accuracy
        self._start_indices = tuple(span.start_index for span in self._spans)
        self._sample_count = self._spans[-1].stop_index if self._spans else 0

    @classmethod
    def empty(cls, accuracy='ns'):
        """ Create a timeline with no segments """
        return cls((), accuracy=accuracy)

    @property
    def spans(self):
        """ Tuple of :class:`SegmentTimeSpan` objects """
        return self._spans

    @property
    def segments(self):
        """ Alias for :attr:`spans` """
        return self._spans

    def __iter__(self):
        return iter(self._spans)

    def __len__(self):
        return len(self._spans)

    def __getitem__(self, index):
        return self._spans[index]

    @property
    def has_overlap(self):
        """ Whether any two spans contain samples at exactly the same time """
        for i, first in enumerate(self._spans):
            if not first.sample_count:
                continue
            first_end = first._relative_time_at(first.sample_count - 1)
            for second in self._spans[i + 1:]:
                if not second.sample_count:
                    continue
                if second.relative_start <= first_end:
                    return True
        return False

    def __repr__(self):
        return "<ChannelSegmentTimeline with {0} segments>".format(len(self._spans))

    def describe(self):
        """ A short human-readable summary of the segment spans """
        lines = []
        for span in self._spans:
            lines.append(
                "segment {0}: samples [{1}, {2}) start={3!r} increment={4!r} count={5}".format(
                    span.segment_index, span.start_index, span.stop_index,
                    span.relative_start, span.increment, span.sample_count))
        return "\n".join(lines)

    def to_dict(self, absolute_time=False, accuracy=None):
        """ Return the timeline as JSON-friendly dictionaries

        Sample indexes and segment indexes remain integers.  Times are
        converted to ISO strings when ``absolute_time`` is true.
        """
        accuracy = self.accuracy if accuracy is None else accuracy
        return {
            'accuracy': accuracy,
            'sample_count': self.sample_count,
            'overlap': self.has_overlap,
            'segments': [
                span.to_dict(absolute_time=absolute_time, accuracy=accuracy)
                for span in self._spans],
        }

    def as_list(self, absolute_time=False, accuracy=None):
        """ Return the per-segment dictionaries as a list """
        return self.to_dict(absolute_time, accuracy)['segments']

    @property
    def sample_count(self):
        """ Total number of samples described by the timeline """
        return self._sample_count

    def time_track(self, absolute_time=False, accuracy=None):
        """ Sample times for the complete channel, in channel index order """
        accuracy = self.accuracy if accuracy is None else accuracy
        arrays = [span.times(absolute_time, accuracy) for span in self._spans]
        if not arrays:
            dtype = 'datetime64[{0}]'.format(accuracy) if absolute_time else np.float64
            return np.empty(0, dtype=dtype)
        return np.concatenate(arrays)

    def span_for_sample(self, sample_index):
        """ Return the span containing a channel sample index """
        sample_index = self._normalize_sample_index(sample_index)
        span_index = bisect_right(self._start_indices, sample_index) - 1
        if span_index < 0:
            raise IndexError("Sample index {0} is not in the timeline".format(sample_index))
        return self._spans[span_index]

    def time_at_sample(self, sample_index, absolute_time=False, accuracy=None):
        """ Time of a sample in the complete channel """
        span = self.span_for_sample(sample_index)
        normalized = sample_index
        if normalized < 0:
            normalized += self.sample_count
        return span.time_at(
            normalized - span.start_index, absolute_time=absolute_time, accuracy=accuracy)

    # Common aliases for reverse/forward lookup
    def time_at_index(self, sample_index, absolute_time=False, accuracy=None):
        """ Alias for :meth:`time_at_sample` """
        return self.time_at_sample(sample_index, absolute_time, accuracy)

    def samples_at_time(self, time, absolute_time=None, accuracy=None):
        """ Return every sample at ``time`` as :class:`SamplePosition` objects

        More than one result means that segments overlap at this time.
        """
        if absolute_time is None:
            absolute_time = _is_absolute_time(time)
        accuracy = self.accuracy if accuracy is None else accuracy
        results = []
        candidate_spans = self._spans_for_time(time, absolute_time)
        for span in candidate_spans:
            for local_index in span.local_indices_at_time(time, absolute_time):
                sample_time = span.time_at(
                    local_index, absolute_time=absolute_time, accuracy=accuracy)
                results.append(SamplePosition(
                    span.start_index + local_index,
                    span.segment_index,
                    local_index,
                    sample_time))
        results.sort(key=lambda position: position.sample_index)
        return results

    def sample_indices_at_time(self, time, absolute_time=None):
        """ Return sample indices for every source at ``time`` """
        return [position.sample_index for position in self.samples_at_time(time, absolute_time)]

    def sample_at_time(self, time, absolute_time=None, accuracy=None):
        """ Return the only sample at ``time``

        :raises AmbiguousTimeError: If overlapping segments contain this time.
        :raises KeyError: If no sample has this time.
        """
        positions = self.samples_at_time(time, absolute_time, accuracy)
        if not positions:
            raise KeyError("No sample found at time {0!r}".format(time))
        if len(positions) > 1:
            raise AmbiguousTimeError(
                "Time {0!r} occurs in {1} samples from segments {2}".format(
                    time, len(positions), [p.segment_index for p in positions]))
        return positions[0]

    def sample_index_at_time(self, time, absolute_time=None):
        """ Return the sample index at ``time`` when the result is unique

        :raises AmbiguousTimeError: If overlapping segments contain this time.
        :raises KeyError: If no sample has this time.
        """
        return self.sample_at_time(time, absolute_time).sample_index

    # Alias matching the common "index_at_time" wording
    def index_at_time(self, time, absolute_time=None):
        """ Alias for :meth:`sample_index_at_time` """
        return self.sample_index_at_time(time, absolute_time)

    def _spans_for_time(self, time, absolute_time):
        """ Narrow candidate spans while preserving every possible source """
        if not self._spans:
            return ()
        if absolute_time:
            values = []
            for span in self._spans:
                start = span.start_time
                end = span.end_time
                if start is None or end is None:
                    values = None
                    break
                values.append((np.datetime64(start, 'ns'), np.datetime64(end, 'ns')))
            if values is not None:
                target = np.datetime64(time, 'ns')
                selected = [
                    span for span, (start, end) in zip(self._spans, values)
                    if span.sample_count and start <= target <= end]
                if selected:
                    return selected
        return self._spans

    def _normalize_sample_index(self, sample_index):
        if not isinstance(sample_index, (int, np.integer)):
            raise TypeError("Sample index must be an integer")
        if sample_index < 0:
            sample_index += self.sample_count
        if sample_index < 0 or sample_index >= self.sample_count:
            raise IndexError(
                "Sample index {0} is outside of the timeline bounds [0, {1}]".format(
                    sample_index - (self.sample_count if sample_index < 0 else 0),
                    self.sample_count - 1))
        return sample_index


def build_channel_timeline(channel, accuracy='ns'):
    """ Build a timeline for a channel from the metadata read by its file """
    reader = channel._reader
    runs = reader.get_channel_data_segments(channel.path)
    converted_start_times = []
    for (_, _, _, properties) in runs:
        start_time = properties.get('wf_start_time')
        converted_start_times.append(_convert_start_time(start_time, accuracy))
    base_start_time = next((t for t in converted_start_times if t is not None), None)

    spans = []
    previous_span = None
    for (segment_index, start_index, sample_count, properties), wf_start_time in zip(
            runs, converted_start_times):
        if 'wf_increment' not in properties:
            raise KeyError("Object does not have time properties available.")
        increment = float(properties['wf_increment'])
        start_offset = float(properties.get('wf_start_offset', 0.0))
        gap_segments = ()
        if previous_span is not None and segment_index != previous_span.segment_index + 1:
            gap_segments = tuple(range(previous_span.segment_index + 1, segment_index))
        clock_continuous = False
        start_time_known = True
        has_absolute_start = False
        absolute_start_time = None
        relative_anchor = 0.0
        if wf_start_time is not None:
            repeated_metadata = (
                previous_span is not None and
                not gap_segments and
                previous_span.has_absolute_start and
                wf_start_time == previous_span.absolute_start_time and
                start_offset == previous_span.start_offset)
            if repeated_metadata:
                # Standard multi-segment streams often repeat the same
                # absolute start and offset while the sample clock continues.
                # The segment's own increment still applies.
                clock_continuous = True
                relative_start = previous_span.relative_start + (
                    previous_span.sample_count * previous_span.increment)
                has_absolute_start = True
                absolute_start_time = previous_span.absolute_start_time
                relative_anchor = previous_span.relative_anchor
            else:
                if base_start_time is not None:
                    # A new absolute start is exact metadata.  Place it
                    # independently so a stop or overlap is never shifted.
                    relative_start = start_offset + _seconds_between(
                        wf_start_time, base_start_time)
                else:
                    relative_start = start_offset
                has_absolute_start = True
                absolute_start_time = wf_start_time
                # wf_start_time is the time at relative zero; wf_start_offset
                # moves the first sample away from that zero.
                relative_anchor = relative_start - start_offset
        elif previous_span is None:
            relative_start = start_offset
        elif not gap_segments and start_offset == previous_span.start_offset:
            # Adjacent segments with an unchanged offset are the normal
            # multi-segment stream: the sample clock continues from the
            # previous segment's last sample.
            clock_continuous = True
            relative_start = previous_span.relative_start + (
                previous_span.sample_count * previous_span.increment)
            if previous_span.has_absolute_start:
                # The clock is known, so this run has exact absolute times
                # even though it did not restate wf_start_time.
                has_absolute_start = True
                absolute_start_time = previous_span.absolute_start_time
                relative_anchor = previous_span.relative_anchor
        else:
            # A physical break or a changed/new offset is an explicit new
            # start.  Keep the stated offset exactly; a break is never
            # bridged by assuming samples in between.
            relative_start = start_offset
            start_time_known = gap_segments == ()
            if previous_span.has_absolute_start and not gap_segments:
                # The same clock can still anchor this offset.
                has_absolute_start = True
                absolute_start_time = previous_span.absolute_start_time
                relative_anchor = previous_span.relative_anchor

        previous_span = SegmentTimeSpan(
            segment_index=segment_index,
            start_index=start_index,
            sample_count=sample_count,
            increment=increment,
            start_offset=start_offset,
            wf_start_time=wf_start_time,
            relative_start=relative_start,
            accuracy=accuracy,
            gap_before=bool(gap_segments),
            gap_segments=gap_segments,
            clock_continuous=clock_continuous,
            start_time_known=start_time_known,
            absolute_start_time=absolute_start_time,
            has_absolute_start=has_absolute_start,
            relative_anchor=relative_anchor)
        spans.append(previous_span)
    return ChannelSegmentTimeline(spans, accuracy=accuracy)


def _relative_deltas(relative_seconds, accuracy):
    unit_correction = _ACCURACY_UNITS[accuracy]
    return (relative_seconds * unit_correction).astype(
        'timedelta64[{0}]'.format(accuracy))


def _json_time(value):
    if value is None:
        return None
    if isinstance(value, np.datetime64):
        return str(value)
    return float(value)


def _convert_start_time(value, accuracy):
    if value is None:
        return None
    if isinstance(value, TdmsTimestamp):
        return value.as_datetime64(accuracy)
    return np.datetime64(value, accuracy)


def _seconds_between(later, earlier):
    difference = np.datetime64(later, 'ns') - np.datetime64(earlier, 'ns')
    return _datetime64_delta(difference) * 1.0e-9


def _datetime64_delta(value):
    return int(np.asarray(value).astype('timedelta64[ns]').astype(np.int64))


def _is_absolute_time(value):
    return isinstance(value, (np.datetime64, datetime))


def _time_accuracy(value):
    if isinstance(value, np.datetime64):
        unit = np.datetime_data(value.dtype)[0]
        if unit in _ACCURACY_UNITS:
            return unit
    return 'ns'


def _times_equal(first, second, increment):
    if _is_absolute_time(first) or _is_absolute_time(second):
        return np.datetime64(first, 'ns') == np.datetime64(second, 'ns')
    tolerance = 1.0e-12
    if increment:
        tolerance = max(tolerance, abs(float(increment)) * 1.0e-9)
    return abs(float(first) - float(second)) <= tolerance
