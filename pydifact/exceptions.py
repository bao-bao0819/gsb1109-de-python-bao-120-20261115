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


class ValidationError(Exception):
    pass


class EDIEncodingError(EDISyntaxError, UnicodeError):
    """Undecodable bytes in an EDI byte stream.

    Raised instead of silently inserting replacement characters, so a
    corrupted field (e.g. an order number) can never go unnoticed. Carries
    the byte range in the original byte stream and - if the affected field
    could be located - the segment tag and element/component position.
    """

    def __init__(
        self,
        message: str,
        *,
        byte_range: tuple[int, int] | None = None,
        segment_tag: str | None = None,
        element_index: int | None = None,
        component_index: int | None = None,
        reason: str | None = None,
    ):
        super().__init__(message)
        self.byte_range = byte_range
        self.segment_tag = segment_tag
        self.element_index = element_index
        self.component_index = component_index
        self.reason = reason


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
