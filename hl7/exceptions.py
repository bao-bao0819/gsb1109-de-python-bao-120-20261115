class HL7Exception(Exception):
    pass


class MalformedSegmentException(HL7Exception):
    pass


class MalformedBatchException(HL7Exception):
    pass


class MalformedFileException(HL7Exception):
    pass


class ParseException(HL7Exception):
    pass


class MissingOccurrenceError(HL7Exception):
    """Raised when a segment occurrence referenced by an issue no longer exists."""
