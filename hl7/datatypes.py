import datetime
import re

DTM_TZ_RE = re.compile(r"(\d+(?:\.\d+)?)(?:([+-]\d{2})(\d{2}))?")

_DTM_RE = re.compile(
    r"(?P<base>\d{4}(?:\d{2}){0,5})"
    r"(?:\.(?P<fraction>\d+))?"
    r"(?P<offset>[+-]\d{2}:?\d{2}|[Zz])?"
)

_PRECISION_NAMES = {
    4: "year",
    6: "month",
    8: "day",
    10: "hour",
    12: "minute",
    14: "second",
}

_FRACTION_PRECISION_NAMES = {
    1: "decisecond",
    2: "centisecond",
    3: "millisecond",
    4: "100-microsecond",
    5: "10-microsecond",
    6: "microsecond",
}

_UTC_NAMES = {"UTC", "GMT", "Z", "z"}


class _UTCOffset(datetime.tzinfo):
    """Fixed offset timezone from UTC."""

    def __init__(self, minutes):
        """``minutes`` is an offset from UTC, negative for west of UTC."""
        # ``minutes`` may be passed in as a float when constructed via
        # :func:`parse_datetime`.  ``datetime.timedelta`` and formatting of the
        # timezone name expect an ``int``.  Store the offset as an ``int`` to
        # avoid producing values like ``-5.00.0`` from ``tzname`` when floats are
        # used.
        self.minutes = int(minutes)

    def __eq__(self, other):
        if isinstance(other, _UTCOffset):
            return self.minutes == other.minutes
        return NotImplemented

    def __hash__(self):
        return hash(self.minutes)

    def utcoffset(self, dt):
        return datetime.timedelta(minutes=self.minutes)

    def tzname(self, dt):
        minutes = abs(self.minutes)
        return "{0}{1:02}{2:02}".format(
            "-" if self.minutes < 0 else "+", minutes // 60, minutes % 60
        )

    def dst(self, dt):
        return datetime.timedelta(0)


def _parse_offset(value):
    """Return a fixed offset timezone for an HL7/ISO offset string."""
    if value in ("Z", "z"):
        return _UTCOffset(0)

    sign = -1 if value.startswith("-") else 1
    digits = value[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:])
    if hours > 23 or minutes > 59:
        raise ValueError("Malformed timezone offset {0}".format(value))
    return _UTCOffset(sign * (hours * 60 + minutes))


def _coerce_timezone(value):
    """Coerce a timezone name or offset into a ``tzinfo`` instance."""
    if value is None:
        return None
    if isinstance(value, datetime.tzinfo):
        return value
    if not isinstance(value, str):
        raise TypeError("source_tz must be a tzinfo or timezone name")

    value = value.strip()
    if value in _UTC_NAMES:
        return _UTCOffset(0)
    if re.fullmatch(r"[+-]\d{2}:?\d{2}", value):
        return _parse_offset(value)

    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python 3.9+ always has zoneinfo
        raise ValueError("IANA timezone names require zoneinfo")

    try:
        return ZoneInfo(value)
    except Exception as exc:
        raise ValueError("Unknown source timezone {0}".format(value)) from exc


def _next_month(year, month):
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _is_aware(value):
    return value.tzinfo is not None and value.utcoffset() is not None


def _check_comparable(left, right):
    if _is_aware(left) != _is_aware(right):
        raise ValueError(
            "Cannot compare offset-aware and offset-naive datetimes; "
            "provide source_tz for values without an offset"
        )


def _compare_key(value):
    """Return a key that compares aware values on the absolute timeline."""
    if _is_aware(value):
        return value.astimezone(datetime.timezone.utc)
    return value


def _add_duration(start, duration):
    """Add a duration on the absolute timeline for aware values."""
    if not _is_aware(start):
        return start + duration
    return (start.astimezone(datetime.timezone.utc) + duration).astimezone(start.tzinfo)


class DateTimeRange:
    """A parsed HL7 DTM declaration and its comparable half-open range.

    ``start`` is inclusive and ``end`` is exclusive.  A month-only value such as
    ``202403`` therefore covers the whole month; it is not treated as the
    instant ``2024-03-01T00:00:00``.  The range is expressed in the declared
    offset when one is present, otherwise in ``source_tz``.  If neither is
    available the boundaries are naive.
    """

    def __init__(
        self,
        value,
        precision,
        precision_digits,
        start,
        end,
        timezone_source,
        fraction_digits=0,
    ):
        self.value = value
        self.precision = precision
        self.precision_digits = precision_digits
        self.start = start
        self.end = end
        self.timezone_source = timezone_source
        self.fraction_digits = fraction_digits

    @property
    def raw(self):
        """The original declaration exactly as supplied."""
        return self.value

    @property
    def original(self):
        """Alias for :attr:`raw`."""
        return self.value

    @property
    def declaration(self):
        """Alias for :attr:`raw`."""
        return self.value

    @property
    def declared(self):
        """Alias for :attr:`raw`."""
        return self.value

    @property
    def comparable_range(self):
        """Return ``(start, end)`` for comparisons; ``end`` is exclusive."""
        return (self.start, self.end)

    @property
    def interval(self):
        """Alias for :attr:`comparable_range`."""
        return self.comparable_range

    @property
    def range(self):
        """Alias for :attr:`comparable_range`."""
        return self.comparable_range

    @property
    def range_start(self):
        """Inclusive start of :attr:`comparable_range`."""
        return self.start

    @property
    def range_end(self):
        """Exclusive end of :attr:`comparable_range`."""
        return self.end

    @property
    def sort_key(self):
        """A stable key that does not collapse a partial date to one instant."""
        return (_compare_key(self.start), _compare_key(self.end), self.raw)

    @property
    def has_offset(self):
        """Whether the declaration itself carried a UTC offset."""
        return self.timezone_source == "declared"

    @property
    def tzinfo(self):
        """The timezone attached to the comparable range, if any."""
        return self.start.tzinfo

    @property
    def utc_offset(self):
        """The UTC offset as a ``timedelta``, or ``None`` when naive."""
        return self.start.utcoffset()

    @property
    def offset(self):
        """The UTC offset as ``+HH:MM``/``-HH:MM``, or ``None`` when naive."""
        offset = self.utc_offset
        if offset is None:
            return None
        seconds = int(offset.total_seconds())
        sign = "-" if seconds < 0 else "+"
        seconds = abs(seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return "{0}{1:02}:{2:02}".format(sign, hours, minutes)

    @property
    def granularity(self):
        """The width of the declared precision as a ``timedelta``."""
        return _compare_key(self.end) - _compare_key(self.start)

    @property
    def resolution(self):
        """Alias for :attr:`granularity`."""
        return self.granularity

    @property
    def precision_code(self):
        """The precision as the number of significant DTM digits."""
        return self.precision

    @property
    def precision_level(self):
        """Alias for :attr:`precision_digits`."""
        return self.precision

    @property
    def precision_length(self):
        """Alias for :attr:`precision_digits`."""
        return self.precision

    @property
    def precision_name(self):
        """A human-readable name, including any fractional-second precision."""
        if self.fraction_digits > 6:
            return "sub-microsecond"
        if self.fraction_digits:
            return _FRACTION_PRECISION_NAMES[self.fraction_digits]
        return _PRECISION_NAMES[self.precision_digits]

    @property
    def base_precision(self):
        """The precision before applying fractional seconds."""
        return _PRECISION_NAMES[self.precision_digits - self.fraction_digits]

    @property
    def fractional_precision(self):
        """The number of fractional-second digits, or ``0`` when absent."""
        return self.fraction_digits

    def overlaps(self, other):
        """Return whether this range intersects another range.

        ``other`` may be a :class:`DateTimeRange`, an ``(start, end)`` pair, or
        a single ``datetime``.
        """
        if isinstance(other, datetime.datetime):
            other_start = other_end = other
        elif isinstance(other, DateTimeRange):
            other_start, other_end = other.start, other.end
        else:
            other_start, other_end = other

        _check_comparable(self.start, other_start)
        _check_comparable(self.end, other_end)
        return _compare_key(self.start) < _compare_key(other_end) and _compare_key(
            other_start
        ) < _compare_key(self.end)

    def contains(self, value):
        """Return whether ``value`` falls inside this half-open range."""
        _check_comparable(self.start, value)
        return _compare_key(self.start) <= _compare_key(value) < _compare_key(self.end)

    def to_dict(self):
        """Return a JSON-ready description of the declaration and its range."""
        return {
            "raw": self.raw,
            "original": self.original,
            "declaration": self.declaration,
            "precision": self.precision,
            "precision_name": self.precision_name,
            "precision_digits": self.precision_digits,
            "base_precision": self.base_precision,
            "offset": self.offset,
            "timezone": self.tzinfo.tzname(self.start) if self.tzinfo else None,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "range": [self.start.isoformat(), self.end.isoformat()],
            "comparable_range": [self.start.isoformat(), self.end.isoformat()],
            "timezone_source": self.timezone_source,
        }

    as_dict = to_dict

    @property
    def declaration_info(self):
        """The three core audit fields: declaration, precision and range."""
        return {
            "declaration": self.declaration,
            "precision": self.precision,
            "comparable_range": [self.start.isoformat(), self.end.isoformat()],
        }

    def get(self, key, default=None):
        """Return a field from :meth:`to_dict`, or ``default`` when absent."""
        return self.to_dict().get(key, default)

    def describe(self):
        """Return a compact, human-readable declaration summary."""
        return "{0} ({1}): [{2}, {3})".format(
            self.raw, self.precision_name, self.start.isoformat(), self.end.isoformat()
        )

    def __eq__(self, other):
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return self.comparable_range == tuple(other)
        if not isinstance(other, DateTimeRange):
            return NotImplemented
        return (
            self.raw,
            self.precision,
            self.precision_digits,
            self.start,
            self.end,
            self.timezone_source,
            self.fraction_digits,
        ) == (
            other.raw,
            other.precision,
            other.precision_digits,
            other.start,
            other.end,
            other.timezone_source,
            other.fraction_digits,
        )

    def __repr__(self):
        return (
            "DateTimeRange(value={0!r}, precision={1!r}, start={2!r}, end={3!r})"
        ).format(self.value, self.precision, self.start, self.end)

    def __iter__(self):
        return iter((self.start, self.end))

    def __len__(self):
        return 2

    def __getitem__(self, key):
        if isinstance(key, str):
            values = self.to_dict()
            if key in values:
                return values[key]
            raise KeyError(key)
        return (self.start, self.end)[key]


def parse_datetime_range(
    value, source_tz=None, source_timezone=None, default_timezone=None
):
    """Parse an HL7 DTM declaration into a :class:`DateTimeRange`.

    ``value`` is of the format
    ``YYYY[MM[DD[HH[MM[SS[.S[S[S[S[S[S]]]]]]]]]]][+/-HHMM]``.  The original
    declaration, its precision and the half-open comparable range are retained.
    A one- to six-digit fraction is accepted; its width determines the end of
    the range.

    Values without an offset use ``source_tz``.  ``source_timezone`` and
    ``default_timezone`` are aliases accepted for callers that spell the
    parameter out.  It may be a
    ``tzinfo`` instance, an IANA name such as ``"Asia/Shanghai"``, or a fixed
    offset such as ``"+08:00"``.  If no timezone is supplied, offset-less values
    produce naive boundaries rather than silently assuming UTC.

    Empty input returns ``None``, matching :func:`parse_datetime`.
    """
    if not value:
        return None
    if not isinstance(value, str):
        raise TypeError("HL7 datetime must be a string")

    if source_tz is None:
        source_tz = source_timezone if source_timezone is not None else default_timezone

    raw = value
    text = value.strip()
    match = _DTM_RE.fullmatch(text)
    if not match:
        raise ValueError("Malformed HL7 datetime {0}".format(raw))

    base = match.group("base")
    fraction = match.group("fraction")
    offset = match.group("offset")

    if fraction is not None and len(base) != 14:
        raise ValueError("Fractional seconds require a complete second {0}".format(raw))

    year = int(base[0:4])
    month = int(base[4:6]) if len(base) >= 6 else 1
    day = int(base[6:8]) if len(base) >= 8 else 1
    hour = int(base[8:10]) if len(base) >= 10 else 0
    minute = int(base[10:12]) if len(base) >= 12 else 0
    second = int(base[12:14]) if len(base) >= 14 else 0

    fraction_digits = len(fraction) if fraction else 0
    if fraction_digits:
        microsecond = int((fraction + "000000")[:6])
    else:
        microsecond = 0

    if offset is not None:
        tzinfo = _parse_offset(offset)
        timezone_source = "declared"
    elif source_tz is not None:
        tzinfo = _coerce_timezone(source_tz)
        timezone_source = "source"
    else:
        tzinfo = None
        timezone_source = "naive"

    try:
        start = datetime.datetime(
            year, month, day, hour, minute, second, microsecond, tzinfo=tzinfo
        )
    except ValueError as exc:
        raise ValueError("Malformed HL7 datetime {0}".format(raw)) from exc

    length = len(base)
    try:
        if length == 4:
            end = datetime.datetime(year + 1, 1, 1, tzinfo=tzinfo)
        elif length == 6:
            end_year, end_month = _next_month(year, month)
            end = datetime.datetime(end_year, end_month, 1, tzinfo=tzinfo)
        elif length == 8:
            next_day = datetime.date(year, month, day) + datetime.timedelta(days=1)
            end = datetime.datetime(
                next_day.year, next_day.month, next_day.day, tzinfo=tzinfo
            )
        elif length == 10:
            end = _add_duration(start, datetime.timedelta(hours=1))
        elif length == 12:
            end = _add_duration(start, datetime.timedelta(minutes=1))
        elif fraction_digits:
            quantum_us = 10 ** (6 - fraction_digits)
            if quantum_us < 1:
                quantum_us = 1
            end = _add_duration(start, datetime.timedelta(microseconds=quantum_us))
        else:
            end = _add_duration(start, datetime.timedelta(seconds=1))
    except (OverflowError, ValueError) as exc:
        raise ValueError("HL7 datetime range exceeds supported bounds") from exc

    if _compare_key(end) <= _compare_key(start):
        raise ValueError("HL7 datetime range is empty for {0}".format(raw))

    precision_digits = 14 + fraction_digits if fraction_digits else length
    return DateTimeRange(
        raw,
        precision_digits,
        precision_digits,
        start,
        end,
        timezone_source,
        fraction_digits,
    )


class DateTimeFilterResult:
    """Result of filtering values by a half-open time range.

    ``contained`` holds ranges fully inside the filter.  ``overlapping`` holds
    ranges that intersect it but extend before or after it, which keeps a
    month/date-only declaration separate from values wholly inside a day.
    ``outside`` holds the rest.  ``matched`` is ``contained + overlapping``.
    Input order is preserved in every group.
    """

    def __init__(self, contained, overlapping, outside, start, end):
        self.contained = tuple(contained)
        self.overlapping = tuple(overlapping)
        self.outside = tuple(outside)
        self.start = start
        self.end = end

    @property
    def matched(self):
        """Ranges intersecting the filter, contained or overlapping."""
        return self.contained + self.overlapping

    @property
    def intersecting(self):
        """Alias for :attr:`matched`."""
        return self.matched

    @property
    def included(self):
        """Alias for :attr:`contained`."""
        return self.contained

    @property
    def excluded(self):
        """Alias for :attr:`outside`."""
        return self.outside

    def __iter__(self):
        return iter(self.matched)

    def __len__(self):
        return len(self.matched)

    def __bool__(self):
        return bool(self.matched)

    def to_dict(self):
        """Return a JSON-ready breakdown of the filter result."""
        return {
            "contained": [item.to_dict() for item in self.contained],
            "overlapping": [item.to_dict() for item in self.overlapping],
            "outside": [item.to_dict() for item in self.outside],
        }

    def __getitem__(self, key):
        if isinstance(key, str):
            groups = {
                "contained": self.contained,
                "overlapping": self.overlapping,
                "outside": self.outside,
                "matched": self.matched,
                "intersecting": self.matched,
                "included": self.contained,
                "excluded": self.outside,
            }
            if key in groups:
                return groups[key]
            raise KeyError(key)
        return (self.contained, self.overlapping, self.outside)[key]


def _coerce_filter_bound(value, source_tz):
    if isinstance(value, DateTimeRange):
        return value.start
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None and source_tz is not None:
            return value.replace(tzinfo=_coerce_timezone(source_tz))
        return value
    if isinstance(value, str):
        parsed = parse_datetime_range(value, source_tz=source_tz)
        if parsed is None:
            raise ValueError("Filter boundary cannot be empty")
        return parsed.start
    raise TypeError("Filter boundaries must be datetimes, DTM strings or ranges")


def filter_datetime_ranges(
    values,
    start,
    end=None,
    source_tz=None,
    source_timezone=None,
    default_tz=None,
    default_timezone=None,
):
    """Partition DTM declarations by a half-open filter range ``[start, end)``.

    ``values`` may contain raw DTM strings or :class:`DateTimeRange` instances.
    String boundaries are parsed as DTM declarations and their starts are used,
    so ``start="20240315", end="20240316"`` selects one day.  Ranges that merely
    intersect the filter are returned in ``overlapping`` rather than being
    forced into ``contained`` or padded to a midnight instant.
    """
    if source_tz is None:
        source_tz = source_timezone
    if source_tz is None:
        source_tz = default_tz
    if source_tz is None:
        source_tz = default_timezone

    if isinstance(values, (str, DateTimeRange)):
        values = [values]

    filter_start = _coerce_filter_bound(start, source_tz)
    if end is None:
        if isinstance(start, str):
            start_range = parse_datetime_range(start, source_tz=source_tz)
            if start_range is None:
                raise ValueError("Filter boundary cannot be empty")
            filter_end = start_range.end
        elif isinstance(start, DateTimeRange):
            filter_end = start.end
        else:
            raise ValueError("An end boundary is required for datetime filters")
    else:
        filter_end = _coerce_filter_bound(end, source_tz)
    _check_comparable(filter_start, filter_end)
    filter_start_key = _compare_key(filter_start)
    filter_end_key = _compare_key(filter_end)
    if filter_end_key <= filter_start_key:
        raise ValueError("Filter end must be after its start")

    contained = []
    overlapping = []
    outside = []
    for value in values:
        if value is None:
            continue
        parsed = (
            value
            if isinstance(value, DateTimeRange)
            else parse_datetime_range(value, source_tz=source_tz)
        )
        if parsed is None:
            continue

        _check_comparable(filter_start, parsed.start)
        _check_comparable(filter_end, parsed.end)
        range_start = _compare_key(parsed.start)
        range_end = _compare_key(parsed.end)
        if range_end <= filter_start_key or range_start >= filter_end_key:
            outside.append(parsed)
        elif range_start >= filter_start_key and range_end <= filter_end_key:
            contained.append(parsed)
        else:
            overlapping.append(parsed)

    return DateTimeFilterResult(
        contained, overlapping, outside, filter_start, filter_end
    )


def sort_datetime_ranges(values, source_tz=None, source_timezone=None):
    """Return parsed ranges sorted by ``(start, end, raw)``.

    Incomplete declarations keep their whole interval as the sort key, so a
    month does not become a single midnight instant.  When two values share a
    start, the narrower declaration sorts first.
    """
    if source_tz is None:
        source_tz = source_timezone
    if isinstance(values, (str, DateTimeRange)):
        values = [values]

    parsed = []
    for value in values:
        if value is None:
            continue
        item = (
            value
            if isinstance(value, DateTimeRange)
            else parse_datetime_range(value, source_tz=source_tz)
        )
        if item is not None:
            parsed.append(item)
    return sorted(parsed, key=lambda item: item.sort_key)


def filter_by_day(values, day, source_tz=None, source_timezone=None):
    """Filter by one DTM day declaration, returning the same grouped result.

    ``day`` may be ``"20240315"`` or a :class:`DateTimeRange`; its own end is
    used as the exclusive filter end, so a day-only value is never collapsed to
    midnight before comparison.
    """
    if source_tz is None:
        source_tz = source_timezone
    return filter_datetime_ranges(values, day, end=None, source_tz=source_tz)


def describe_datetime_ranges(values, source_tz=None, source_timezone=None):
    """Return declaration/precision/range dictionaries for ``values``.

    A single string or :class:`DateTimeRange` returns one dictionary; any
    iterable returns a list.  ``None`` entries are skipped.
    """
    if source_tz is None:
        source_tz = source_timezone

    single = isinstance(values, (str, DateTimeRange))
    if single:
        values = [values]

    described = []
    for value in values:
        if value is None:
            continue
        item = (
            value
            if isinstance(value, DateTimeRange)
            else parse_datetime_range(value, source_tz=source_tz)
        )
        if item is not None:
            described.append(item.to_dict())
    if single:
        return described[0] if described else None
    return described


def datetime_range_report(
    values, start, end=None, source_tz=None, source_timezone=None
):
    """Return original declarations and the contained/overlapping breakdown.

    This is the single-call form intended for audit output.  ``declarations``
    preserves input order and every original string, while ``result`` is the
    :class:`DateTimeFilterResult` with the same values grouped by overlap.
    """
    if source_tz is None:
        source_tz = source_timezone
    if isinstance(values, (str, DateTimeRange)):
        values = [values]

    parsed = [
        value
        if isinstance(value, DateTimeRange)
        else parse_datetime_range(value, source_tz=source_tz)
        for value in values
        if value is not None
    ]
    parsed = [value for value in parsed if value is not None]
    result = filter_datetime_ranges(parsed, start, end=end, source_tz=source_tz)
    return {
        "declarations": [value.to_dict() for value in parsed],
        "contained": [value.raw for value in result.contained],
        "overlapping": [value.raw for value in result.overlapping],
        "outside": [value.raw for value in result.outside],
    }


def parse_datetime(value, source_tz=None):
    """Parse hl7 DTM string ``value`` :py:class:`datetime.datetime`.

    ``value`` is of the format YYYY[MM[DD[HH[MM[SS[.S[S[S[S]]]]]]]]][+/-HHMM]
    or a ValueError will be raised.

    :rtype: :py:class:`datetime.datetime`
    """
    parsed = parse_datetime_range(value, source_tz=source_tz)
    return None if parsed is None else parsed.start


#: Descriptive aliases used when the field is specifically a collection time.
parse_collection_time = parse_datetime_range
parse_dtm = parse_datetime_range
parse_dtm_range = parse_datetime_range
parse_datetime_interval = parse_datetime_range
filter_collection_times = filter_datetime_ranges
sort_collection_times = sort_datetime_ranges
describe_collection_times = describe_datetime_ranges
collection_time_report = datetime_range_report
