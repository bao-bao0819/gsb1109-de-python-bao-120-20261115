class ReadError(Exception):
    pass


class WriteError(Exception):
    pass


class OrderingError(Exception):
    """Raised when a lossless node-order conversion cannot be established."""


class CorruptionError(Exception):
    pass
