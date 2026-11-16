#
# Exceptions
#


class UnknownCommandError(Exception):
    """Raised when unknown command ID is received"""


class ConnectionError(Exception):
    """Connection error"""


class PDUError(RuntimeError):
    """Error processing PDU"""


class MessageTooLong(ValueError):
    """Text too long to fit 255 SMS"""


class UnencodableCharacters(UnicodeError):
    """Text contains characters that cannot be represented by the encoding.

    ``positions`` contains zero-based indexes into the original text.  The
    text is never modified or replaced, so callers can report exactly which
    characters prevented the message from being sent.
    """

    def __init__(self, encoding, positions, text, requested_encoding=None):
        self.encoding = encoding
        self.requested_encoding = (
            encoding if requested_encoding is None else requested_encoding)
        self.positions = list(positions)
        self.character_positions = self.positions
        self.position_tuple = tuple(self.positions)
        self.characters = tuple(text[position] for position in self.positions)
        self.text = text
        message = (
            'text cannot be encoded as {encoding}; '
            'unencodable character positions: {positions}'
        ).format(encoding=encoding, positions=self.positions)
        UnicodeError.__init__(self, message)


# Descriptive aliases for callers that prefer an "...Error" name.
UnencodableCharacterError = UnencodableCharacters
UnencodableTextError = UnencodableCharacters
