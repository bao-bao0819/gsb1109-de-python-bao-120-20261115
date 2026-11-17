"""Immutable switches and amount-literal conventions for parser behaviour.

The ten boolean :class:`Options` switches default to ``False`` and preserve
legacy output for affected fields unless explicitly enabled. The additional
:attr:`Options.amount_format` names a bank's amount-literal convention; when it
is omitted, the historical permissive comma/dot handling remains available.

Example:
    >>> import mt940
    >>> options = mt940.Options(reversal_sign=True, applicant_iban=True)
    >>> options.reversal_sign, options.strip_bom
    (True, False)
    >>> mt940.Options.all().strip_bom
    True
    >>> amount_format = mt940.AmountFormat(',', '.')
    >>> amount_format.parse('1.234,56')
    Decimal('1234.56')
"""

from __future__ import annotations

import dataclasses
import decimal
import re
import typing


class AmountFormatError(ValueError, decimal.InvalidOperation):
    """An amount literal does not follow its configured bank convention.

    The error is both a :class:`ValueError` and a
    :class:`decimal.InvalidOperation`, so callers that already handle invalid
    decimal text keep working. It names the offending literal and the expected
    separators.
    """


#: Sentinel distinguishing an omitted constructor argument from ``None``.
_UNSET = object()


@dataclasses.dataclass(frozen=True, slots=True, init=False)
class AmountFormat:
    """Describe one bank's exact amount-literal convention.

    A format names the separator for the fractional part and, optionally, the
    separator used between groups of three integer digits. Parsing is strict:
    a separator that is not part of the configured convention is rejected
    instead of being guessed from the number's magnitude or the count of
    following digits.

    The default is the MT940 comma convention without grouping. Use
    :meth:`legacy` only when the source has no bank-specific contract and the
    historical permissive comma-or-point handling is required.

    Args:
        decimal_separator: Character separating integer and fractional digits.
            Empty or ``None`` means the literal has no fractional part.
        thousands_separator: Optional character between integer groups of
            exactly three digits. It must differ from ``decimal_separator``.
        decimal_places: Optional exact number of fractional digits. ``None``
            accepts any number, including an omitted fractional part.
        aliases: Constructor aliases accepted for convenience: ``decimal``,
            ``decimal_mark``, ``decimal_sep``, ``thousands``,
            ``thousands_mark``, ``thousands_sep`` and ``grouping_separator``.

    Raises:
        AmountFormatError: Separators are repeated, empty where a character is
            required, or identical.
        TypeError: A separator is not text.

    Examples:
        >>> AmountFormat(',', '.').parse('1.234,56')
        Decimal('1234.56')
        >>> AmountFormat('.', ',').parse('1,234.56')
        Decimal('1234.56')
        >>> AmountFormat('.').parse('1.234')
        Decimal('1.234')
        >>> AmountFormat(',', '.').parse('1,234.56')
        Traceback (most recent call last):
        ...
        mt940.options.AmountFormatError: amount '1,234.56' does not match ...
    """

    decimal_separator: str | None
    thousands_separator: str | None
    decimal_places: int | None
    #: Whether :meth:`legacy` allows either comma or point, without grouping.
    _legacy: bool = dataclasses.field(default=False, repr=False)

    def __init__(
        self,
        decimal_separator: str | None | object = _UNSET,
        thousands_separator: str | None | object = _UNSET,
        *,
        decimal_places: int | None = None,
        decimal: str | None = None,
        decimal_mark: str | None = None,
        decimal_sep: str | None = None,
        thousands: str | None = None,
        thousands_mark: str | None = None,
        thousands_sep: str | None = None,
        grouping_separator: str | None = None,
        legacy: bool = False,
        **kwargs: object,
    ) -> None:
        """Create and validate an amount format.

        Keyword aliases make the convention readable in different bank
        configurations. Supplying more than one *explicit* name for the same
        separator is an error rather than a silent precedence rule. Omitted
        positional separators fall back to the MT940 comma convention and no
        grouping.

        Raises:
            AmountFormatError: The separator names conflict or are invalid.
            TypeError: A supplied alias is not text.
        """
        if kwargs:
            unexpected = ', '.join(sorted(kwargs))
            msg = f'unknown amount-format option(s): {unexpected}'
            raise TypeError(msg)

        decimal_default = decimal_separator is _UNSET
        thousands_default = thousands_separator is _UNSET
        decimal_value = _one_alias(
            'decimal separator',
            typing.cast(
                'str | None',
                None if decimal_default else decimal_separator,
            ),
            decimal,
            decimal_mark,
            decimal_sep,
        )
        thousands_value = _one_alias(
            'thousands separator',
            typing.cast(
                'str | None',
                None if thousands_default else thousands_separator,
            ),
            thousands,
            thousands_mark,
            thousands_sep,
            grouping_separator,
        )
        if decimal_default and decimal_value is None:
            decimal_value = ','
        if thousands_default and thousands_value is None:
            thousands_value = None
        decimal_value = _check_separator(decimal_value, 'decimal')
        thousands_value = _check_separator(thousands_value, 'thousands')
        if (
            decimal_value is not None
            and decimal_value == thousands_value
        ):
            msg = 'decimal and thousands separators must differ'
            raise AmountFormatError(msg)
        if decimal_places is not None and decimal_places < 0:
            msg = 'decimal_places must not be negative'
            raise AmountFormatError(msg)
        object.__setattr__(self, 'decimal_separator', decimal_value)
        object.__setattr__(self, 'thousands_separator', thousands_value)
        object.__setattr__(self, 'decimal_places', decimal_places)
        object.__setattr__(self, '_legacy', bool(legacy))

    @classmethod
    def legacy(cls) -> AmountFormat:
        """Return the historical permissive comma-or-point format.

        This format accepts either ``','`` or ``'.'`` as the decimal separator
        and no grouping. It exists for compatibility with sources that do not
        state a bank convention; explicit bank imports should use a strict
        format instead.
        """
        return cls(legacy=True)

    def parse(self, literal: str) -> decimal.Decimal:
        """Parse one literal using exactly this convention.

        Args:
            literal: Source amount text. Surrounding whitespace is ignored for
                parsing but remains available on the resulting
                :class:`mt940.models.Amount` as its original text.

        Returns:
            The exact decimal represented by ``literal``. No currency scale,
            rounding or magnitude-based inference is applied.

        Raises:
            AmountFormatError: The literal contains a separator, grouping or
                fractional digit count that the format does not permit.
            TypeError: ``literal`` is not text.
        """
        if not isinstance(typing.cast('object', literal), str):
            msg = f'amount literal must be str, not {type(literal).__name__}'
            raise TypeError(msg)
        if self._legacy:
            return _parse_legacy(literal)
        return self._parse_strict(literal)

    def _parse_strict(self, literal: str) -> decimal.Decimal:
        """Parse with the configured separators and grouping rules.

        Raises:
            AmountFormatError: The literal does not match this format.
        """
        text = literal.strip()
        if not text:
            _invalid_amount(literal, self, 'it is empty')

        sign = ''
        if text[0] in '+-':
            sign, text = text[0], text[1:]
        if not text:
            _invalid_amount(literal, self, 'it has no digits')

        decimal_separator = self.decimal_separator
        if decimal_separator:
            if text.count(decimal_separator) > 1:
                _invalid_amount(
                    literal,
                    self,
                    f'it repeats {decimal_separator!r}',
                )
            if decimal_separator in text:
                integer_part, fraction = text.split(decimal_separator, 1)
            else:
                integer_part, fraction = text, ''
        else:
            integer_part, fraction = text, ''
            if self.thousands_separator is None and (
                '.' in text or ',' in text
            ):
                _invalid_amount(
                    literal,
                    self,
                    'it contains a separator but the format has no decimal '
                    'separator',
                )

        if decimal_separator and not _DIGITS_RE.fullmatch(fraction):
            _invalid_amount(
                literal,
                self,
                f'fraction {fraction!r} is not a digit sequence',
            )
        if (
            self.decimal_places is not None
            and len(fraction) != self.decimal_places
        ):
            _invalid_amount(
                literal,
                self,
                f'it has {len(fraction)} fractional digits, expected '
                f'{self.decimal_places}',
            )

        integer_digits = self._integer_digits(literal, integer_part)
        if not integer_digits and not fraction:
            _invalid_amount(literal, self, 'it has no digits')

        normalized = f'{sign}{integer_digits or "0"}.{fraction or "0"}'
        return decimal.Decimal(normalized)

    def _integer_digits(self, literal: str, integer_part: str) -> str:
        """Validate integer grouping and return its digits.

        Grouping is optional: an ungrouped integer of any length is accepted
        when its only characters are decimal digits. Once a configured
        thousands separator appears, every group after the first must contain
        exactly three digits.

        Raises:
            AmountFormatError: A group or character is not permitted.
        """
        if not integer_part:
            return ''
        if self.thousands_separator is None:
            if not _DIGITS_RE.fullmatch(integer_part):
                _invalid_amount(
                    literal,
                    self,
                    f'integer part {integer_part!r} contains a disallowed '
                    'separator',
                )
            return integer_part

        if self.thousands_separator not in integer_part:
            if not _DIGITS_RE.fullmatch(integer_part):
                _invalid_amount(
                    literal,
                    self,
                    f'integer part {integer_part!r} contains a disallowed '
                    'separator',
                )
            return integer_part

        groups = integer_part.split(self.thousands_separator)
        if not groups[0] or any(not group for group in groups):
            _invalid_amount(
                literal,
                self,
                f'grouping in {integer_part!r} has an empty group',
            )
        if not 1 <= len(groups[0]) <= 3:
            _invalid_amount(
                literal,
                self,
                f'first group {groups[0]!r} is not one to three digits',
            )
        for group in groups[1:]:
            if len(group) != 3:
                _invalid_amount(
                    literal,
                    self,
                    f'group {group!r} is not exactly three digits',
                )
        if any(not _DIGITS_RE.fullmatch(group) for group in groups):
            _invalid_amount(
                literal,
                self,
                f'integer part {integer_part!r} is not a digit sequence',
            )
        return ''.join(groups)

    def __call__(self, literal: str) -> decimal.Decimal:
        """Parse ``literal``; an alias for :meth:`parse`."""
        return self.parse(literal)


#: A complete ASCII digit sequence. ``str.isdigit`` also accepts non-ASCII
#: digits that ``decimal.Decimal`` does not treat as money digits.
_DIGITS_RE = re.compile(r'[0-9]*')


def _one_alias(name: str, *values: str | None) -> str | None:
    """Return one value from constructor aliases, rejecting conflicts.

    Raises:
        AmountFormatError: More than one non-``None`` alias is supplied.
        TypeError: An alias is not text.
    """
    present = [value for value in values if value is not None]
    if len(present) > 1:
        msg = f'{name} was supplied more than once'
        raise AmountFormatError(msg)
    value = present[0] if present else None
    return value


def _check_separator(value: object, name: str) -> str | None:
    """Validate a separator character.

    Raises:
        AmountFormatError: A non-empty separator is not exactly one character,
            or is a digit, sign or newline.
        TypeError: The separator is not text.
    """
    if value is None or value == '':
        return None if value is None else ''
    if not isinstance(value, str):
        msg = f'{name} separator must be text, not {type(value).__name__}'
        raise TypeError(msg)
    if len(value) != 1:
        msg = f'{name} separator must be one character, got {value!r}'
        raise AmountFormatError(msg)
    if value.isdigit() or value in '+-' or value in '\r\n':
        msg = f'{name} separator {value!r} is not a separator character'
        raise AmountFormatError(msg)
    return value


def _parse_legacy(literal: str) -> decimal.Decimal:
    """Apply the historical comma-or-point conversion.

    Raises:
        AmountFormatError: The text is not a valid decimal after the legacy
            comma replacement.
    """
    try:
        return decimal.Decimal(literal.replace(',', '.'))
    except decimal.InvalidOperation as exc:
        raise AmountFormatError(
            f'amount {literal!r} is not valid legacy decimal text'
        ) from exc


def _invalid_amount(
    literal: str, amount_format: AmountFormat, reason: str
) -> None:
    """Raise an :class:`AmountFormatError` describing a failed match."""
    raise AmountFormatError(
        _amount_error_message(literal, amount_format, reason)
    )


def _amount_error_message(
    literal: str, amount_format: AmountFormat, reason: str
) -> str:
    """Build a message that identifies the literal and expected convention."""
    return (
        f'amount {literal!r} does not match the configured amount format '
        f'(decimal={amount_format.decimal_separator!r}, '
        f'thousands={amount_format.thousands_separator!r}): {reason}'
    )


@dataclasses.dataclass(frozen=True, slots=True)
class Options:
    """Frozen, slotted configuration for parser behaviours.

    Each boolean constructor keyword is described below. Omitted boolean fields
    remain ``False``. ``amount_format`` is an optional explicit bank
    convention; leaving it as ``None`` keeps the historical permissive amount
    handling. Instances can be shared between collections because fields cannot
    be reassigned. Use :func:`dataclasses.replace` to make an instance with
    selected fields changed.

    No field changes tag registration or processor ordering. Options are read
    from the collection while its tags and processors run. ``strip_bom`` is
    applied by the high-level source readers, not by ``Transactions.parse``.
    """

    #: File the ``?31`` sub-field of a structured ``:86:`` under
    #: ``applicant_iban`` instead of prepending it to ``applicant_name``
    #: (issue #132, the 4.x behaviour).
    applicant_iban: bool = False

    #: A structured ``:86:`` emits ``None`` for every sub-field it does not
    #: carry. With this on, such a ``None`` never replaces a value another
    #: tag already provided, so the ``:61:`` customer reference survives an
    #: ``:86:`` without a ``KREF``.
    merge_keeps_values: bool = False

    #: Give the ``:61:`` reversal-of-credit mark ``RC`` a negative amount,
    #: like the debit it is (issue #130).
    reversal_sign: bool = False

    #: Treat lowercase debit/credit marks (``d``, ``c``, ``rc``, ``rd``) like
    #: their uppercase forms when signing an amount. The tag patterns already
    #: accept them.
    case_insensitive_marks: bool = False

    #: Read the ``:13D:`` offset as hours and minutes, so ``+0100`` is one
    #: hour. 5.0.0 read the digits as a minute count.
    timezone_offset: bool = False

    #: Keep ``:86:`` details of any length. 5.0.0 silently truncated them
    #: after nine chunks of 65 characters.
    unbounded_details: bool = False

    #: Keep the content of ``:NS:`` lines that do not start with a two-digit
    #: sub-tag. 5.0.0 replaced such a line with an empty one when it followed
    #: a sub-tag line.
    non_swift_free_text: bool = False

    #: Treat a blank ``:34F:`` debit/credit mark as "both", giving
    #: ``d_floor_limit`` and ``c_floor_limit`` and a statement currency.
    #: 5.0.0 produced a key with a leading space.
    floor_limit_blank_mark: bool = False

    #: Drop a leading byte-order mark, so the first ``:20:`` tag is
    #: recognised. 5.0.0 kept it and lost that tag.
    strip_bom: bool = False

    #: Do not interpret a ``+`` in the first four purpose characters as a
    #: GVC keyword terminator. This preserves free text that the legacy
    #: empty-key match can drop. Text before a genuine keyword is still
    #: dropped.
    gvc_leading_text: bool = False

    #: Exact amount-literal convention for this bank import. ``None`` retains
    #: the historical permissive comma-or-point handling. A configured format
    #: rejects every other grouping or decimal separator instead of inferring
    #: one from the magnitude.
    amount_format: AmountFormat | None = None

    @classmethod
    def all(cls) -> Options:
        """Construct an instance with every boolean switch set to ``True``.

        Boolean fields returned by :meth:`names` are passed to the constructor
        as keywords with value ``True``. Non-boolean configuration such as
        ``amount_format`` keeps its default and must be supplied explicitly.
        Subclass boolean fields are supported when the subclass constructor
        accepts those keywords.

        Returns:
            An instance of the called class. For ``Options`` this enables all
            ten legacy compatibility switches and leaves the amount format
            unspecified.

        Raises:
            TypeError: The constructor rejects a switch keyword, for example
                when a subclass adds a dataclass field with ``init=False``.
        """
        return cls(
            **dict.fromkeys(cls.names(), True)  # type: ignore[arg-type]
        )

    @classmethod
    def names(cls) -> tuple[str, ...]:
        """Return the boolean compatibility-switch names in order.

        Returns:
            Names whose dataclass default is a boolean. ``amount_format`` and
            other non-boolean configuration fields are excluded, so the
            historical ten-switch contract and :meth:`all` remain stable.
        """
        return tuple(
            field.name
            for field in dataclasses.fields(cls)
            if isinstance(field.default, bool)
        )

    @classmethod
    def fields(cls) -> tuple[str, ...]:
        """Return all dataclass field names in declaration order.

        Returns:
            A tuple of names, including inherited fields when called on a
            dataclass subclass. Unlike :meth:`names`, this includes non-boolean
            configuration such as ``amount_format``.
        """
        return tuple(field.name for field in dataclasses.fields(cls))

    @classmethod
    def switches(cls) -> tuple[str, ...]:
        """Return the boolean compatibility-switch names in order.

        Returns:
            An alias for :meth:`names`, kept for callers that find the explicit
            name clearer.
        """
        return cls.names()

    def __post_init__(self) -> None:
        """Validate the optional amount format.

        Raises:
            TypeError: ``amount_format`` is not an :class:`AmountFormat`.
        """
        value: object = self.amount_format
        if value is None or isinstance(value, AmountFormat):
            return
        msg = (
            'amount_format must be an AmountFormat or None, not '
            f'{type(value).__name__}'
        )
        raise TypeError(msg)

    def __getstate__(self) -> tuple[object, ...]:
        """Return field values in declaration order for serialisation."""
        return tuple(
            getattr(self, field.name)
            for field in dataclasses.fields(self)
        )

    def __setstate__(self, state: tuple[object, ...]) -> None:
        """Restore options, backfilling fields absent from older state.

        Args:
            state: Field values in declaration order or, from an older
                serialized result, the boolean switches only.
        """
        fields = dataclasses.fields(self)
        values = tuple(state)
        for index, field in enumerate(fields):
            value: object
            if index < len(values):
                value = values[index]
            elif isinstance(field.default, bool):
                value = field.default
            else:
                value = None
            object.__setattr__(self, field.name, value)
        self.__post_init__()
