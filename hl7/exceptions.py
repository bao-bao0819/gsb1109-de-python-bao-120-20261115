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


class BatchTrailerMismatchException(MalformedBatchException):
    """The BTS trailer's declared message count does not match the
    number of messages actually received in the batch (or the declared
    count is missing or not an integer)."""


class MissingBatchTrailerException(MalformedBatchException):
    """The batch has a BHS header but no BTS trailer, so its message
    count cannot be confirmed. A missing trailer must never be treated
    as a successful declaration of zero messages."""
