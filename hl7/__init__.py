"""python-hl7 is a simple library for parsing messages of Health Level 7
(HL7) version 2.x into Python objects.

* Documentation: http://python-hl7.readthedocs.org
* Source Code: http://github.com/johnpaulett/python-hl7
"""

from .accessor import Accessor
from .containers import (
    Batch,
    Component,
    Container,
    Factory,
    Field,
    File,
    Message,
    Repetition,
    Segment,
    Sequence,
)
from .datatypes import (
    DateTimeFilterResult,
    DateTimeRange,
    collection_time_report,
    describe_collection_times,
    describe_datetime_ranges,
    datetime_range_report,
    filter_collection_times,
    filter_by_day,
    filter_datetime_ranges,
    parse_collection_time,
    parse_datetime,
    parse_datetime_range,
    parse_datetime_interval,
    parse_dtm,
    parse_dtm_range,
    sort_collection_times,
    sort_datetime_ranges,
)
from .exceptions import (
    HL7Exception,
    MalformedBatchException,
    MalformedFileException,
    MalformedSegmentException,
    ParseException,
)
from .parser import parse, parse_batch, parse_file, parse_hl7
from .util import generate_message_control_id, isbatch, isfile, ishl7, split_file

__version__ = "0.4.6.dev0"
__author__ = "John Paulett"
__email__ = "john -at- paulett.org"
__license__ = "BSD"
__copyright__ = "Copyright 2011, John Paulett <john -at- paulett.org>"

#: This is the HL7 Null value. It means that a field is present and blank.
NULL = '""'


__all__ = [
    "parse",
    "parse_hl7",
    "parse_batch",
    "parse_file",
    "Sequence",
    "Container",
    "File",
    "Batch",
    "Message",
    "Segment",
    "Field",
    "Repetition",
    "Component",
    "Factory",
    "Accessor",
    "ishl7",
    "isbatch",
    "isfile",
    "split_file",
    "generate_message_control_id",
    "DateTimeRange",
    "DateTimeFilterResult",
    "parse_datetime",
    "parse_datetime_range",
    "parse_dtm",
    "parse_dtm_range",
    "parse_datetime_interval",
    "parse_collection_time",
    "filter_datetime_ranges",
    "filter_collection_times",
    "filter_by_day",
    "sort_datetime_ranges",
    "sort_collection_times",
    "describe_datetime_ranges",
    "describe_collection_times",
    "datetime_range_report",
    "collection_time_report",
    "HL7Exception",
    "MalformedBatchException",
    "MalformedFileException",
    "MalformedSegmentException",
    "ParseException",
]
