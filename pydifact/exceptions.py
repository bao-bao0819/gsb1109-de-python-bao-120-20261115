class EDISyntaxError(Exception):
    def __init__(
        self,
        message: str,
        line_number: int | None = None,
        column_number: int | None = None,
    ):
        if line_number is not None and column_number is not None:
            super().__init__(
                f"EDIFACT Syntax Error: {message} (line {line_number}, column "
                f"{column_number})"
            )
        else:
            super().__init__(f"EDIFACT Syntax Error: {message}")


class MessageStructureError(EDISyntaxError):
    """A message does not follow the segment/group order of its message
    structure definition.

    Raised when a segment cannot be placed into the message structure of its
    message type (e.g. because of an illegal segment order) or when a
    required segment or group is missing. Segments are never reordered to
    make a message fit its structure; the error always names the offending
    segment as it appears in the original message.
    """


class ValidationError(Exception):
    pass


# ---------------- Warnings ----------------
class MissingImplementationWarning(Warning):
    pass


class NonConformantEDIWarning(Warning):
    """Emitted when pydifact accepts input that deviates from the EDIFACT
    specification (e.g. content in front of the UNA segment, an empty
    segment, or an empty mandatory element).

    Suppress it with a standard `warnings` filter; the parser's own warnings
    can also be switched off with `Parser(warn_non_conformant=False)`.
    """
