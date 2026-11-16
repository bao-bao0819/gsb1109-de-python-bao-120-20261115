"""Normalization and correlation of SMPP message identifiers.

SMSCs do not all use the same textual representation for a message
identifier.  A submit response may return a decimal identifier, while the
matching delivery receipt may declare the same identifier as hexadecimal,
or use an opaque textual identifier.  Converting values by guessing is
dangerous: a textual identifier such as ``deadbeef`` looks exactly like a
hexadecimal number but may be an arbitrary identifier.

This module therefore works with *declared* formats:

* a rule (optionally scoped to a gateway and a protocol version) declares
  which format a field uses;
* a literal may also declare its own format explicitly, for example
  ``0x1d305b4c``, ``hex:1d305b4c`` or ``decimal:0489708364``;
* values are only converted when a rule or an explicit declaration says
  so.  Anything else is kept as text and is reported as unmatched instead
  of being coerced.

The original value is always retained on :class:`NormalizedMessageId.raw`
so callers can persist it separately from the normalized value.
"""

import re

import six


class MessageIdFormat(object):
    """Names of the supported identifier representations."""

    DECIMAL = 'decimal'
    HEX = 'hex'
    TEXT = 'text'
    AUTO = 'auto'

    ALIASES = {
        '10': DECIMAL,
        'dec': DECIMAL,
        'decimal': DECIMAL,
        'num': DECIMAL,
        'numeric': DECIMAL,
        'int': DECIMAL,
        'integer': DECIMAL,
        '16': HEX,
        'hex': HEX,
        'hexadecimal': HEX,
        'base16': HEX,
        'str': TEXT,
        'string': TEXT,
        'text': TEXT,
        'plain': TEXT,
        'opaque': TEXT,
        'auto': AUTO,
        'infer': AUTO,
        'declared': AUTO,
        'text_or_decimal': AUTO,
        'plain_or_decimal': AUTO,
    }

    @classmethod
    def normalize(cls, value):
        """Return the canonical format name for ``value``.

        ``None`` is returned for an unknown format so callers can report an
        uninterpretable value instead of silently choosing a format.
        """
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, six.string_types):
            return None
        return cls.ALIASES.get(value.strip().lower())


class MessageIdRule(object):
    """A declared format for a message identifier field.

    ``version`` and ``gateway`` make rules versioned/scoped.  A rule with no
    version is the fallback for every version of a gateway.
    """

    def __init__(
        self,
        field,
        format,
        version=None,
        gateway=None,
        allow_declared_format=True,
        case_sensitive=True,
        text_numeric=False,
        strip=True,
        encoding='latin-1',
    ):
        canonical = MessageIdFormat.normalize(format)
        if canonical is None:
            raise ValueError('Unknown message ID format: %r' % (format,))

        self.field = field
        self.format = canonical
        self.version = version
        self.gateway = gateway
        self.allow_declared_format = allow_declared_format
        self.case_sensitive = case_sensitive
        self.text_numeric = text_numeric
        self.strip = strip
        self.encoding = encoding

    def __repr__(self):
        return (
            'MessageIdRule(field={0!r}, format={1!r}, version={2!r}, '
            'gateway={3!r})'
        ).format(self.field, self.format, self.version, self.gateway)


class MessageIdRules(object):
    """Registry of versioned and gateway-scoped message ID rules.

    A rules instance can be populated in several equivalent ways::

        rules = MessageIdRules()
        rules.add('message_id', 'decimal')
        rules.add('receipted_message_id', 'hex', version='3.4')

        rules = MessageIdRules({
            'message_id': 'decimal',
            'receipted_message_id': 'hex',
        })

        rules = MessageIdRules({
            'versions': {
                '1.0': {'receipted_message_id': 'hex'},
                '2.0': {'receipted_message_id': 'decimal'},
            },
        })

    Version lookup chooses the newest rule not newer than the requested
    version.  Without a version only an unscoped rule applies; a versioned
    rule is never applied by guessing.
    """

    def __init__(self, rules=None):
        self._rules = []
        if rules is not None:
            self.load(rules)

    def add(self, field, format, version=None, gateway=None, **kwargs):
        rule = MessageIdRule(field, format, version=version, gateway=gateway, **kwargs)
        self._rules.append(rule)
        return rule

    register = add

    def register_version(self, version, fields, gateway=None, **kwargs):
        """Register a mapping of ``field -> format`` for one version."""
        if not isinstance(fields, dict):
            raise TypeError('fields must be a mapping of field to format')
        for field, format in six.iteritems(fields):
            if isinstance(format, MessageIdRule):
                self.add(
                    field,
                    format.format,
                    version=version,
                    gateway=gateway or format.gateway,
                    allow_declared_format=format.allow_declared_format,
                    case_sensitive=format.case_sensitive,
                    text_numeric=format.text_numeric,
                    strip=format.strip,
                    encoding=format.encoding,
                )
            elif isinstance(format, dict):
                options = dict(format)
                fmt = options.pop('format', options.pop('declared_format', None))
                self.add(field, fmt, version=version, gateway=gateway, **options)
            else:
                self.add(field, format, version=version, gateway=gateway, **kwargs)

    def load(self, rules, version=None, gateway=None):
        """Load rules from a mapping or an iterable of MessageIdRule."""
        if isinstance(rules, MessageIdRules):
            for rule in rules._rules:
                self._rules.append(rule)
            return self

        if isinstance(rules, MessageIdRule):
            self._rules.append(rules)
            return self

        if isinstance(rules, (list, tuple)):
            for rule in rules:
                if isinstance(rule, MessageIdRule):
                    self._rules.append(rule)
                elif isinstance(rule, dict):
                    options = dict(rule)
                    field = options.pop('field', None)
                    fmt = options.pop(
                        'format', options.pop('declared_format', None))
                    self.add(
                        field, fmt,
                        version=options.pop('version', version),
                        gateway=options.pop('gateway', gateway),
                        **options
                    )
                else:
                    raise TypeError('Expected MessageIdRule, got %r' % (rule,))
            return self

        if not isinstance(rules, dict):
            raise TypeError('rules must be a mapping or a sequence of rules')

        for key, value in six.iteritems(rules):
            if key == 'versions':
                for rule_version, fields in six.iteritems(value):
                    self.load(fields, version=rule_version, gateway=gateway)
            elif key == 'gateways':
                for rule_gateway, fields in six.iteritems(value):
                    self.load(fields, version=version, gateway=rule_gateway)
            elif key == 'rules':
                self.load(value, version=version, gateway=gateway)
            elif key in ('version', 'gateway', 'gateway_id', 'system_id',
                         'defaults', 'default'):
                continue
            elif isinstance(value, MessageIdRule):
                self._rules.append(value)
            elif isinstance(value, dict):
                if _looks_like_field_options(value):
                    # A field with per-format options, e.g.
                    # {'receipted_message_id': {'format': 'hex'}}.
                    options = dict(value)
                    fmt = options.pop('format', options.pop('declared_format', None))
                    rule_version = options.pop('version', version)
                    rule_gateway = options.pop('gateway', gateway)
                    self.add(key, fmt, version=rule_version,
                             gateway=rule_gateway, **options)
                elif _looks_like_version(key):
                    self.load(value, version=key, gateway=gateway)
                elif _is_pdu_namespace(key):
                    self._load_pdu_namespace(key, value, version, gateway)
                else:
                    self.load(value, version=version, gateway=key)
            else:
                self.add(key, value, version=version, gateway=gateway)
        return self

    def _load_pdu_namespace(self, namespace, fields, version, gateway):
        """Load ``{'submit_sm_resp': {'message_id': 'decimal'}}`` style rules."""
        for field, value in six.iteritems(fields):
            options = dict(value) if isinstance(value, dict) else {}
            fmt = options.pop('format', options.pop('declared_format', value))
            rule_version = options.pop('version', version)
            rule_gateway = options.pop('gateway', gateway)
            self.add(
                namespace + '.' + field,
                fmt,
                version=rule_version,
                gateway=rule_gateway,
                **options
            )

    def resolve(self, field, version=None, gateway=None):
        """Return the rule applicable to ``field`` or ``None``."""
        candidates = _field_candidates(field)

        for candidate in candidates:
            rule = self._resolve_for_field(candidate, version, gateway)
            if rule is not None:
                return rule
        return None

    def _resolve_for_field(self, field, version, gateway):
        candidates = [
            rule for rule in self._rules if _field_matches(rule.field, field)
        ]
        if not candidates:
            return None

        if gateway is None:
            return _select_rule(
                [rule for rule in candidates if rule.gateway is None], version)

        specific = [
            rule for rule in candidates
            if rule.gateway is not None and _gateway_matches(rule.gateway, gateway)
        ]
        rule = _select_rule(specific, version)
        if rule is not None:
            return rule

        generic = [rule for rule in candidates if rule.gateway is None]
        return _select_rule(generic, version)

    def __len__(self):
        return len(self._rules)


class NormalizedMessageId(object):
    """The result of normalizing one identifier value.

    ``raw`` is never modified.  ``value`` is the canonical value (decimal
    digits for decimal/hex identifiers, exact text for textual ones) and is
    ``None`` when the value could not be interpreted under its rule.
    """

    def __init__(
        self,
        raw,
        value=None,
        key=None,
        format=None,
        field=None,
        version=None,
        gateway=None,
        rule=None,
        reason=None,
        record=None,
    ):
        self.raw = raw
        self.original = raw
        self.value = value
        self.key = key
        self.format = format
        self.field = field
        self.version = version
        self.gateway = gateway
        self.rule = rule
        self.reason = reason
        self.match_reason = None
        self.record = record

    @property
    def ok(self):
        return self.value is not None and self.reason is None

    @property
    def normalized(self):
        return self.value

    @property
    def canonical(self):
        return self.value

    @property
    def error(self):
        return self.reason

    @property
    def status(self):
        """Normalization status: ``normalized`` or ``unmatched``."""
        if not self.ok:
            return 'unmatched'
        if self.match_reason is not None:
            return 'unmatched'
        return 'normalized'

    @property
    def matched(self):
        return self.ok and self.match_reason is None

    @property
    def number(self):
        if self.format in (MessageIdFormat.DECIMAL, MessageIdFormat.HEX) and self.ok:
            try:
                return int(self.value)
            except (TypeError, ValueError):
                return None
        return None

    def __bool__(self):
        return self.ok

    __nonzero__ = __bool__

    def __str__(self):
        if self.value is None:
            return ''
        if isinstance(self.value, six.binary_type):
            return self.value
        if six.PY2:
            return self.value.encode('utf-8')
        return self.value

    def __unicode__(self):
        return '' if self.value is None else six.text_type(self.value)

    def __repr__(self):
        return (
            'NormalizedMessageId(raw={0!r}, value={1!r}, format={2!r}, '
            'reason={3!r})'
        ).format(self.raw, self.value, self.format, self.reason)

    def __eq__(self, other):
        if isinstance(other, NormalizedMessageId):
            return self.ok and other.ok and self.key == other.key
        if other is None:
            return not self.ok
        if isinstance(other, six.integer_types):
            return self.number == other
        if isinstance(other, six.string_types):
            return self.ok and self.value == other
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash(self.key) if self.ok else hash((self.raw, self.reason))

    def to_dict(self):
        """Return a JSON-friendly representation, including the raw value."""
        result = {
            'raw': _json_value(self.raw),
            'original': _json_value(self.raw),
            'value': self.value,
            'normalized': self.value,
            'format': self.format,
            'field': self.field,
            'version': self.version,
            'gateway': self.gateway,
            'ok': self.ok,
            'status': self.status,
        }
        if isinstance(self.raw, six.binary_type):
            result['raw_hex'] = _hexlify(self.raw)
            result['raw_encoding'] = 'latin-1'
        if self.reason is not None:
            result['reason'] = self.reason
        if self.match_reason is not None:
            result['match_reason'] = self.match_reason
        return result


# Friendly aliases used by callers that prefer shorter names.
MessageId = NormalizedMessageId
NormalizedId = NormalizedMessageId


class MessageIdLink(object):
    """A submit response identifier correlated with a delivery receipt."""

    def __init__(self, submit, receipt):
        self.submit = submit
        self.receipt = receipt
        self.key = submit.key
        self.message_id = submit.value
        self.value = submit.value
        self.raw_submit = submit.raw
        self.raw_receipt = receipt.raw
        self.receipt_values = list(getattr(receipt, 'related', []) or [receipt])

    @property
    def response(self):
        return self.submit

    @property
    def normalized(self):
        return self.value

    @property
    def raw_message_id(self):
        """The submit response's untouched original message ID."""
        return self.submit.raw

    @property
    def raw_receipt_id(self):
        """The receipt's untouched original identifier."""
        return self.receipt.raw

    @property
    def raw_receipt_values(self):
        return [value.raw for value in self.receipt_values]

    @property
    def status(self):
        return 'linked'

    def to_dict(self):
        return {
            'message_id': self.value,
            'normalized_message_id': self.value,
            'raw_message_id': _json_value(self.raw_submit),
            'raw_receipt_id': _json_value(self.raw_receipt),
            'submit': self.submit.to_dict(),
            'receipt': self.receipt.to_dict(),
            'receipt_values': [value.to_dict() for value in self.receipt_values],
        }

    def __repr__(self):
        return 'MessageIdLink(message_id={0!r})'.format(self.value)


class CorrelationResult(object):
    """The outcome of correlating submit responses and delivery receipts."""

    def __init__(self, links=None, unmatched_submit=None, unmatched_receipt=None):
        self.links = list(links or [])
        self.unmatched_submit = list(unmatched_submit or [])
        self.unmatched_receipt = list(unmatched_receipt or [])

    @property
    def linked(self):
        return self.links

    @property
    def pairs(self):
        return self.links

    @property
    def matches(self):
        return self.links

    @property
    def correlated(self):
        return self.links

    @property
    def unmatched(self):
        return self.unmatched_submit + self.unmatched_receipt

    @property
    def unmatched_ids(self):
        return [value.raw for value in self.unmatched]

    @property
    def raw_values(self):
        """Every original value seen, linked or unmatched."""
        values = []
        for link in self.links:
            values.extend([link.raw_submit, link.raw_receipt])
        values.extend(value.raw for value in self.unmatched)
        return values

    @property
    def linked_keys(self):
        return [link.key for link in self.links]

    @property
    def unlinked(self):
        return self.unmatched

    @property
    def unassociated(self):
        return self.unmatched

    def __iter__(self):
        return iter(self.links)

    def __len__(self):
        return len(self.links)

    def to_dict(self):
        return {
            'linked': [link.to_dict() for link in self.links],
            'unmatched': [value.to_dict() for value in self.unmatched],
            'unmatched_submit': [value.to_dict() for value in self.unmatched_submit],
            'unmatched_receipt': [value.to_dict() for value in self.unmatched_receipt],
            'unmatched_raw': [_json_value(value.raw) for value in self.unmatched],
            'raw_values': [_json_value(value) for value in self.raw_values],
            'counts': {
                'linked': len(self.links),
                'unmatched': len(self.unmatched),
                'unmatched_submit': len(self.unmatched_submit),
                'unmatched_receipt': len(self.unmatched_receipt),
            },
        }

    def __repr__(self):
        return 'CorrelationResult(linked={0}, unmatched={1})'.format(
            len(self.links), len(self.unmatched))


class InvalidMessageId(ValueError):
    """Raised for an identifier that cannot be interpreted under its rule."""


_DECLARED_FORMAT_RE = re.compile(
    r'^(?:(0[xX])|([A-Za-z][A-Za-z0-9_]*)\s*[:=]\s*)(.*)$', re.DOTALL)
_DECIMAL_RE = re.compile(r'^[0-9]+$')
_HEX_RE = re.compile(r'^(?:0[xX])?[0-9A-Fa-f]+$')
_RECEIPT_ID_RE = re.compile(r'(?<![A-Za-z0-9])id\s*:\s*([^\s;]+)', re.IGNORECASE)
_RECEIPT_IID_RE = re.compile(r'(?<![A-Za-z0-9])iid\s*:\s*([^\s;]+)', re.IGNORECASE)

_DECIMAL_FIELDS = ('submit_message_id',)
_HEX_FIELDS = ('receipted_message_id',)
_TEXT_FIELDS = ('receipt_text_id',)
_DEFAULT_RULES = MessageIdRules()
for _field in _DECIMAL_FIELDS:
    _DEFAULT_RULES.add(_field, MessageIdFormat.AUTO)
for _field in _HEX_FIELDS:
    _DEFAULT_RULES.add(_field, MessageIdFormat.HEX)
for _field in _TEXT_FIELDS:
    _DEFAULT_RULES.add(_field, MessageIdFormat.AUTO)


def default_rules():
    """Return the built-in rule set.

    Submit response identifiers are decimal when all digits (an SMSC may
    return a textual identifier, which stays text); the ``receipted_message_id``
    TLV is declared hexadecimal.  Hex-looking text is never guessed.
    """
    return MessageIdRules(_DEFAULT_RULES)


# Public names for the rule sets and field groups.
DEFAULT_RULES = _DEFAULT_RULES
RULES = _DEFAULT_RULES

FIELD_MESSAGE_ID = 'submit_message_id'
FIELD_RECEIPTED_MESSAGE_ID = 'receipted_message_id'
FIELD_RECEIPT_TEXT_ID = 'receipt_text_id'

FORMAT_DECIMAL = MessageIdFormat.DECIMAL
FORMAT_HEX = MessageIdFormat.HEX
FORMAT_TEXT = MessageIdFormat.TEXT
FORMAT_AUTO = MessageIdFormat.AUTO


_FIELD_ALIASES = {
    'submit_message_id': (
        'message_id', 'submit', 'submit_id', 'submit_response',
        'submit_response_id', 'submit_sm', 'submit_sm_resp',
        'submit_sm_resp.message_id', 'submit_message_id', 'response_id',
        'query_sm_resp.message_id', 'data_sm_resp.message_id',
    ),
    'receipted_message_id': (
        'receipt', 'receipt_id', 'receipt_tlv', 'receipted',
        'receipted_id', 'receipted_message_id', 'deliver_sm',
        'deliver_sm.receipted_message_id',
    ),
    'receipt_text_id': (
        'id', 'receipt_text', 'receipt_text.id', 'receipt_text_id',
        'deliver_sm.receipt_text.id', 'text_id', 'short_message',
        'message_payload', 'receipt_message_text',
    ),
}


def _canonical_field(field):
    """Return the canonical field group for an alias, or the lowercased name."""
    if field is None:
        return None
    value = str(field).strip().lower()
    for canonical, aliases in six.iteritems(_FIELD_ALIASES):
        if value == canonical or value in aliases:
            return canonical
        for alias in aliases:
            if value.endswith('.' + alias) or alias.endswith('.' + value):
                return canonical
    return value


def _field_candidates(field):
    if field is None:
        return []
    if isinstance(field, (list, tuple)):
        candidates = []
        for item in field:
            candidates.extend(_field_candidates(item))
        return candidates
    value = str(field).strip().lower()
    candidates = [value]
    canonical = _canonical_field(value)
    if canonical not in candidates:
        candidates.append(canonical)
    return candidates


def _field_matches(rule_field, candidate):
    return _canonical_field(rule_field) == _canonical_field(candidate)


def _select_rule(rules, version):
    """Select the newest applicable rule for the requested version.

    A versioned rule is only applied when a version is supplied.  Without a
    version only an unscoped rule applies; silently picking the newest
    versioned rule would be a guess.
    """
    if not rules:
        return None

    exact = [rule for rule in rules if _versions_equal(rule.version, version)]
    if exact:
        return exact[-1]

    unscoped = [rule for rule in rules if rule.version is None]
    if version is None:
        return unscoped[-1] if unscoped else None
    if _looks_like_version(version):
        older = [
            rule for rule in rules
            if rule.version is not None
            and _version_key(rule.version) <= _version_key(version)
        ]
        if older:
            return max(older, key=lambda rule: _version_key(rule.version))
    if unscoped:
        return unscoped[-1]
    return None


def _gateway_matches(rule_gateway, gateway):
    if rule_gateway is None:
        return True
    if gateway is None:
        return False
    return _text(rule_gateway).lower() == _text(gateway).lower()


def _text(value):
    if isinstance(value, six.binary_type):
        return value.decode('latin-1')
    return six.text_type(value)


def _version_text(version):
    """Return a version string, accepting SMPP interface-version codes.

    ``0x34`` and ``52`` are SMPP 3.4's interface_version; textual versions
    such as ``'3.4'`` are returned unchanged.  Other integers are treated as
    plain version numbers (``2`` stays ``'2'``).
    """
    if version is None:
        return None
    if isinstance(version, bool):
        return six.text_type(int(version))
    if isinstance(version, six.integer_types):
        if 0x30 <= version <= 0x3F:
            return '3.{0}'.format(version & 0x0F)
        return six.text_type(version)
    return _text(version).strip()


def _looks_like_version(value):
    text = (_version_text(value) or '').lower()
    if text.startswith('v'):
        text = text[1:]
    return bool(re.match(r'^[0-9]+(?:\.[0-9]+)*$', text))


def _looks_like_field_options(value):
    return any(
        key in value for key in
        ('format', 'declared_format', 'allow_declared_format', 'case_sensitive',
         'text_numeric', 'strip', 'encoding')
    )


def _is_pdu_namespace(value):
    text = _text(value).strip().lower()
    return text in (
        'submit_sm', 'submit_sm_resp', 'deliver_sm', 'deliver_sm_resp',
        'data_sm', 'data_sm_resp', 'query_sm', 'query_sm_resp',
    )


def _version_key(version):
    if version is None:
        return ()
    text = (_version_text(version) or '').lower()
    if text.startswith('v'):
        text = text[1:]
    parts = re.findall(r'[0-9]+|[a-z]+', text)
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def _versions_equal(left, right):
    if left is None or right is None:
        return left is None and right is None
    return _version_key(left) == _version_key(right)


def _to_text(value, encoding='latin-1'):
    if isinstance(value, six.binary_type):
        return value.decode(encoding)
    if isinstance(value, six.text_type):
        return value
    return six.text_type(value)


def _hexlify(value):
    if hasattr(value, 'hex'):
        return value.hex()
    import binascii
    return binascii.b2a_hex(value).decode('ascii')


def _json_value(value):
    if isinstance(value, six.binary_type):
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            return value.decode('latin-1')
    if isinstance(value, NormalizedMessageId):
        return value.to_dict()
    return value


def _parse_declared_format(text):
    """Return ``(declared_format, remainder)`` for an explicit declaration."""
    match = _DECLARED_FORMAT_RE.match(text)
    if not match:
        return None, text

    marker, name, remainder = match.groups()
    if marker:
        return MessageIdFormat.HEX, remainder

    declared = MessageIdFormat.normalize(name)
    if declared is None:
        return None, text
    return declared, remainder


def normalize_message_id(
    value,
    format=None,
    field=None,
    version=None,
    gateway=None,
    rules=None,
    rule=None,
    declared_format=None,
    strict=False,
    record=None,
    **options
):
    """Normalize one message identifier using a declared format.

    The value is only converted when ``format``, ``rule``/``rules`` or an
    explicit prefix in the value declares a format.  Without any of those,
    all-digit values are treated as decimal and everything else is treated
    as text; no hexadecimal conversion is guessed.

    Returns a :class:`NormalizedMessageId`.  With ``strict=True`` an
    :class:`InvalidMessageId` is raised for uninterpretable values.
    """
    if isinstance(value, NormalizedMessageId):
        return value

    if rule is None and rules is None and field is not None:
        rules = default_rules()

    if rule is None and rules is not None and field is not None:
        rule = rules.resolve(field, version=version, gateway=gateway)

    if rule is None and format is not None:
        rule = MessageIdRule(
            field or '',
            format,
            version=version,
            gateway=gateway,
            **options
        )
    elif rule is None and options:
        # Direct calls may pass normalization options without a field rule.
        rule = MessageIdRule(
            field or '',
            MessageIdFormat.AUTO,
            version=version,
            gateway=gateway,
            **options
        )
    elif rule is not None and format is not None:
        # An explicit format argument overrides the resolved rule format.
        rule = MessageIdRule(
            rule.field,
            format,
            version=rule.version,
            gateway=rule.gateway,
            allow_declared_format=rule.allow_declared_format,
            case_sensitive=rule.case_sensitive,
            text_numeric=rule.text_numeric,
            strip=rule.strip,
            encoding=rule.encoding,
        )

    if value is None:
        return _invalid(value, field or (rule.field if rule else None),
                        version, gateway, rule, 'missing message ID', strict, record)

    encoding = options.get('encoding', rule.encoding if rule else 'latin-1')
    strip = options.get('strip', rule.strip if rule else True)
    allow_declared = options.get(
        'allow_declared_format', rule.allow_declared_format if rule else True)
    case_sensitive = options.get(
        'case_sensitive', rule.case_sensitive if rule else True)
    text_numeric = options.get(
        'text_numeric', rule.text_numeric if rule else False)

    if isinstance(value, six.integer_types) and not isinstance(value, bool):
        if value < 0:
            return _invalid(value, field or (rule.field if rule else None),
                            version, gateway, rule,
                            'negative message ID', strict, record)
        canonical = six.text_type(value)
        return NormalizedMessageId(
            value, canonical, _numeric_key(canonical),
            MessageIdFormat.DECIMAL,
            field or (rule.field if rule else None),
            version, gateway, rule, None, record)

    try:
        text = _to_text(value, encoding)
    except (UnicodeDecodeError, UnicodeError):
        return _invalid(value, field or (rule.field if rule else None),
                        version, gateway, rule,
                        'value is not decodable text', strict, record)

    if strip:
        text = text.strip().strip('\x00').strip()

    if not text:
        return _invalid(value, field or (rule.field if rule else None),
                        version, gateway, rule, 'empty message ID', strict, record)

    prefix_format = None
    prefix_remainder = text
    if allow_declared:
        prefix_format, prefix_remainder = _parse_declared_format(text)
    if declared_format is not None:
        declared = MessageIdFormat.normalize(declared_format)
        if declared is None:
            return _invalid(value, field or (rule.field if rule else None),
                            version, gateway, rule,
                            'unknown declared format', strict, record)
        if prefix_format is None:
            prefix_remainder = text
        prefix_format = declared

    if prefix_format is not None:
        chosen_format = prefix_format
        text = prefix_remainder.strip()
    elif rule is not None:
        chosen_format = rule.format
    elif format is not None:
        chosen_format = MessageIdFormat.normalize(format)
    else:
        chosen_format = MessageIdFormat.AUTO

    if chosen_format is None:
        return _invalid(value, field or (rule.field if rule else None),
                        version, gateway, rule,
                        'unknown declared format', strict, record)

    return _normalize_text(
        value, text, chosen_format, case_sensitive, text_numeric,
        field or (rule.field if rule else None), version, gateway, rule,
        strict, record)


def _normalize_text(value, text, chosen_format, case_sensitive, text_numeric,
                    field, version, gateway, rule, strict, record):
    if chosen_format in (MessageIdFormat.DECIMAL, MessageIdFormat.AUTO):
        if _DECIMAL_RE.match(text):
            canonical = six.text_type(int(text, 10))
            return NormalizedMessageId(
                value, canonical, _numeric_key(canonical),
                MessageIdFormat.DECIMAL, field, version, gateway, rule,
                None, record)
        if chosen_format == MessageIdFormat.DECIMAL:
            return _invalid(value, field, version, gateway, rule,
                            'not a decimal message ID', strict, record)
        canonical = text if case_sensitive else text.lower()
        return NormalizedMessageId(
            value, canonical, 'text:' + canonical, MessageIdFormat.TEXT,
            field, version, gateway, rule, None, record)

    if chosen_format == MessageIdFormat.HEX:
        if not _HEX_RE.match(text):
            return _invalid(value, field, version, gateway, rule,
                            'not a hexadecimal message ID', strict, record)
        canonical = six.text_type(int(text, 16))
        return NormalizedMessageId(
            value, canonical, _numeric_key(canonical), chosen_format,
            field, version, gateway, rule, None, record)

    if chosen_format == MessageIdFormat.TEXT:
        if text_numeric and _DECIMAL_RE.match(text):
            canonical = six.text_type(int(text, 10))
            return NormalizedMessageId(
                value, canonical, _numeric_key(canonical),
                MessageIdFormat.DECIMAL, field, version, gateway, rule,
                None, record)
        canonical = text if case_sensitive else text.lower()
        return NormalizedMessageId(
            value, canonical, 'text:' + canonical, MessageIdFormat.TEXT,
            field, version, gateway, rule, None, record)

    return _invalid(value, field, version, gateway, rule,
                    'unknown declared format', strict, record)


def _numeric_key(canonical):
    return 'num:' + canonical


def _invalid(raw, field, version, gateway, rule, reason, strict, record):
    if strict:
        raise InvalidMessageId(reason)
    return NormalizedMessageId(
        raw, None, None, rule.format if rule else None, field,
        version, gateway, rule, reason, record)


def parse_receipt_text(text, encoding='latin-1'):
    """Parse the standard fields of an SMPP delivery receipt text.

    The returned mapping always contains ``id`` (or ``None``) and ``raw``.
    ``iid:`` is deliberately not treated as ``id:``.
    """
    raw = text
    try:
        value = _to_text(text, encoding)
    except (UnicodeDecodeError, UnicodeError):
        value = ''

    result = {
        'raw': raw,
        'id': None,
        'iid': None,
        'sub': None,
        'dlvrd': None,
        'submit_date': None,
        'done_date': None,
        'stat': None,
        'err': None,
        'text': None,
    }

    id_match = _RECEIPT_ID_RE.search(value)
    if id_match:
        result['id'] = id_match.group(1)

    iid_match = _RECEIPT_IID_RE.search(value)
    if iid_match:
        result['iid'] = iid_match.group(1)

    for key, pattern in (
        ('sub', r'(?:^|\s)sub\s*:\s*([^\s]+)'),
        ('dlvrd', r'(?:^|\s)dlvrd\s*:\s*([^\s]+)'),
        ('stat', r'(?:^|\s)stat\s*:\s*([^\s]+)'),
        ('err', r'(?:^|\s)err\s*:\s*([^\s]+)'),
    ):
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            result[key] = match.group(1)

    match = re.search(
        r'(?:^|\s)submit\s+date\s*:\s*([^\s]+)', value, re.IGNORECASE)
    if match:
        result['submit_date'] = match.group(1)
    match = re.search(
        r'(?:^|\s)done\s+date\s*:\s*([^\s]+)', value, re.IGNORECASE)
    if match:
        result['done_date'] = match.group(1)
    match = re.search(r'(?:^|\s)text\s*:\s*(.*)$', value, re.IGNORECASE | re.DOTALL)
    if match:
        result['text'] = match.group(1)

    return result


def extract_receipt_id(text, encoding='latin-1'):
    """Return the ``id:`` value from a delivery receipt text, if present."""
    return parse_receipt_text(text, encoding=encoding)['id']


receipt_text_id = extract_receipt_id
parse_receipt_id = extract_receipt_id


def normalize_receipt_text(
    text,
    version=None,
    gateway=None,
    rules=None,
    format=None,
    strict=False,
    record=None,
):
    """Normalize the ``id:`` field of a delivery receipt text."""
    identifier = extract_receipt_id(text)
    if identifier is None:
        return _invalid(text, 'receipt_text.id', version, gateway, None,
                        'receipt text has no id field', strict, record)
    return normalize_message_id(
        identifier,
        format=format,
        field='receipt_text.id',
        version=version,
        gateway=gateway,
        rules=rules if rules is not None else default_rules(),
        strict=strict,
        record=record,
    )


def _is_string(value):
    return isinstance(value, six.string_types) or isinstance(value, six.binary_type)


def _is_record(value):
    """True for mappings and PDU-like objects, not plain values."""
    if isinstance(value, dict):
        return True
    if _is_string(value):
        return False
    return hasattr(value, '__dict__')


def _record_get(record, *names):
    if not _is_record(record):
        return None
    for name in names:
        if isinstance(record, dict):
            if name in record:
                return record[name]
            continue
        value = getattr(record, name, None)
        if value is not None:
            return value
    return None


def _record_get_nonempty(record, *names):
    for name in names:
        value = _record_get(record, name)
        if value is not None and value != b'' and value != '':
            return value
    return None


def _record_version(record, version):
    return (
        _record_get(record, 'message_id_version', 'interface_version',
                    'sc_interface_version', 'version', 'rule_version')
        or version
    )


def _record_gateway(record, gateway):
    return _record_get(record, 'gateway', 'gateway_id', 'system_id') or gateway


def _record_declared_format(record):
    return _record_get(record, 'declared_format', 'format', 'fmt')


def _candidate(record, value, field, version, gateway, rules, record_obj,
               declared_format=None, **options):
    return normalize_message_id(
        value,
        field=field,
        version=version,
        gateway=gateway,
        rules=rules,
        declared_format=declared_format,
        record=record_obj,
        **options
    )


def is_delivery_receipt(record):
    """Return True if a record looks like an SMPP delivery receipt."""
    if isinstance(record, dict):
        command = _record_get(record, 'command', 'command_name')
        esm_class = _record_get(record, 'esm_class')
        registered = _record_get(record, 'registered_delivery')
        has_receipt_text = extract_receipt_id(
            _record_get_nonempty(record, 'message_payload', 'short_message')
            or b'') is not None
        return (
            command == 'deliver_sm'
            and (
                has_receipt_text
                or bool(esm_class is not None and esm_class & 0x04)
                or bool(registered is not None and registered & 0x04)
                or _record_get(record, 'receipted_message_id') is not None
            )
        )
    command = getattr(record, 'command', None)
    if command != 'deliver_sm':
        return False
    esm_class = getattr(record, 'esm_class', None) or 0
    return bool(
        esm_class & 0x04
        or getattr(record, 'receipted_message_id', None)
        or extract_receipt_id(
            getattr(record, 'message_payload', None)
            or getattr(record, 'short_message', None) or b'') is not None
    )


def submit_message_ids(
    record,
    version=None,
    gateway=None,
    rules=None,
    allow_plain_values=True,
):
    """Extract normalized submit-response identifiers from a record."""
    if is_delivery_receipt(record):
        return []

    rules = rules if rules is not None else default_rules()
    version = _record_version(record, version)
    gateway = _record_gateway(record, gateway)
    declared = _record_declared_format(record)

    result = []
    value = _record_get(record, 'message_id', 'submit_message_id', 'response_id')
    if value is not None:
        result.append(_candidate(
            record, value, 'submit_message_id', version, gateway, rules,
            record, declared))
        return result

    if allow_plain_values and not isinstance(record, dict) and not hasattr(record, 'command'):
        result.append(_candidate(
            record, record, 'submit_message_id', version, gateway, rules,
            record, declared))
    return result


def receipt_message_ids(
    record,
    version=None,
    gateway=None,
    rules=None,
    allow_plain_values=True,
):
    """Extract normalized receipt identifiers from a delivery receipt record.

    Both the ``receipted_message_id`` TLV and the ``id:`` field of the
    receipt text are considered.  The returned list includes invalid
    candidates too, so callers can list values the rules could not explain.
    """
    rules = rules if rules is not None else default_rules()
    version = _record_version(record, version)
    gateway = _record_gateway(record, gateway)
    declared = _record_declared_format(record)

    result = []
    tlv = _record_get(
        record, 'receipted_message_id', 'receipted_message_id_raw',
        'receipt_id')
    if tlv is not None and tlv != b'' and tlv != '':
        result.append(_candidate(
            record, tlv, 'receipted_message_id', version, gateway, rules,
            record, declared))

    text = _record_get_nonempty(
        record, 'message_payload', 'short_message', 'receipt_text', 'text')
    if text is not None:
        identifier = extract_receipt_id(text)
        if identifier is not None:
            result.append(_candidate(
                record, identifier, 'receipt_text_id', version, gateway,
                rules, record, None))

    if not result and allow_plain_values:
        if isinstance(record, dict):
            value = _record_get(record, 'id', 'message_id')
            if value is not None:
                # An ambiguous receipt key has no declared field format; use
                # auto (decimal digits or text), never a hexadecimal guess.
                result.append(_candidate(
                    record, value, None, version, gateway,
                    rules, record, declared))
        elif isinstance(record, six.integer_types) and not isinstance(record, bool):
            result.append(_candidate(
                record, record, None, version, gateway, rules, record,
                declared))
        elif not hasattr(record, 'command') and _is_string(record):
            # A bare value passed as a receipt.  It is not yet known whether
            # it is a TLV (hex) or receipt text (decimal), so only explicit
            # declarations/all-digit decimal values are interpreted.
            result.append(_candidate(
                record, record, None, version, gateway, rules, record,
                declared))

    return result


def correlate_message_ids(
    submit_responses,
    receipts,
    version=None,
    gateway=None,
    rules=None,
    allow_plain_values=True,
):
    """Correlate submit responses with delivery receipts.

    Each input may be a PDU object, a mapping, a string/bytes identifier, or
    an iterable of those.  Values are normalized under the applicable
    versioned rule; a receipt with conflicting candidate identifiers is
    reported as unmatched rather than arbitrarily choosing one.
    """
    rules = rules if rules is not None else default_rules()

    submit_items = _as_items(submit_responses)
    receipt_items = _as_items(receipts)

    normalized_submits = []
    unmatched_submit = []
    for index, record in enumerate(submit_items):
        values = submit_message_ids(
            record,
            version=_record_version(record, version),
            gateway=_record_gateway(record, gateway),
            rules=rules,
            allow_plain_values=allow_plain_values,
        )
        if not values:
            continue
        for value in values:
            value.record_index = index
            if value.ok:
                normalized_submits.append(value)
            else:
                unmatched_submit.append(value)

    by_key = {}
    for value in normalized_submits:
        by_key.setdefault(value.key, []).append(value)

    links = []
    unmatched_receipt = []
    matched_submit_keys = set()

    for index, record in enumerate(receipt_items):
        values = receipt_message_ids(
            record,
            version=_record_version(record, version),
            gateway=_record_gateway(record, gateway),
            rules=rules,
            allow_plain_values=allow_plain_values,
        )
        if not values:
            continue

        valid = [value for value in values if value.ok]
        invalid = [value for value in values if not value.ok]

        for value in invalid:
            value.record_index = index
            unmatched_receipt.append(value)

        if not valid:
            continue

        keys = []
        for value in valid:
            value.record_index = index
            if value.key not in keys:
                keys.append(value.key)

        if len(keys) > 1:
            for value in valid:
                value.match_reason = 'conflicting message ID representations'
                unmatched_receipt.append(value)
            continue

        key = keys[0]
        matches = by_key.get(key, [])
        if not matches:
            for value in valid:
                value.match_reason = 'no matching submit response'
                unmatched_receipt.append(value)
            continue

        primary = valid[0]
        primary.related = valid + invalid
        for submit in matches:
            matched_submit_keys.add(submit.key)
            links.append(MessageIdLink(submit, primary))

    for value in normalized_submits:
        if value.key not in matched_submit_keys:
            value.match_reason = 'no matching delivery receipt'
            unmatched_submit.append(value)

    return CorrelationResult(links, unmatched_submit, unmatched_receipt)


def correlate(*args, **kwargs):
    """Alias for :func:`correlate_message_ids`."""
    return correlate_message_ids(*args, **kwargs)


def correlate_receipts(*args, **kwargs):
    """Alias for :func:`correlate_message_ids`."""
    return correlate_message_ids(*args, **kwargs)


def link_message_ids(*args, **kwargs):
    """Alias for :func:`correlate_message_ids`."""
    return correlate_message_ids(*args, **kwargs)


class MessageIdCorrelator(object):
    """Accumulate submit responses and receipts, then correlate them.

    Raw records are retained separately from their normalized identifiers::

        correlator = MessageIdCorrelator(version='3.4', gateway='gateway-a')
        correlator.add_submit(submit_sm_resp)
        correlator.add_receipt(deliver_sm)
        report = correlator.report()
        report['linked'], report['unmatched'], correlator.raw_submits
    """

    def __init__(self, rules=None, version=None, gateway=None):
        self.rules = rules if rules is not None else default_rules()
        self.version = version
        self.gateway = gateway
        self.submit_responses = []
        self.receipts = []
        self.raw_submits = []
        self.raw_receipts = []

    @property
    def normalizer(self):
        return MessageIdNormalizer(
            self.rules, version=self.version, gateway=self.gateway)

    def add_submit(self, record):
        """Add one submit response (PDU, mapping or bare identifier)."""
        self.submit_responses.append(record)
        self.raw_submits.extend(
            value.raw for value in self.normalizer.submit(record))
        return record

    def add_receipt(self, record):
        """Add one delivery receipt (PDU, mapping or bare identifier)."""
        self.receipts.append(record)
        self.raw_receipts.extend(
            value.raw for value in self.normalizer.receipts(record))
        return record

    def add_pdu(self, pdu):
        """Add a PDU, dispatching on its command."""
        command = getattr(pdu, 'command', None)
        if command in ('submit_sm_resp', 'data_sm_resp'):
            return self.add_submit(pdu)
        if command == 'deliver_sm':
            return self.add_receipt(pdu)
        raise ValueError('Unsupported PDU command for message IDs: %r' % (command,))

    def correlate(self):
        """Correlate everything accumulated so far."""
        return correlate_message_ids(
            self.submit_responses,
            self.receipts,
            version=self.version,
            gateway=self.gateway,
            rules=self.rules,
        )

    def report(self):
        """Return the correlation report as a JSON-friendly mapping."""
        return self.correlate().to_dict()

    def reset(self):
        """Drop accumulated records, keeping the configured rules."""
        self.submit_responses = []
        self.receipts = []
        self.raw_submits = []
        self.raw_receipts = []


class MessageIdNormalizer(object):
    """Convenience wrapper binding a rule set, gateway and version."""

    def __init__(self, rules=None, version=None, gateway=None):
        self.rules = rules if rules is not None else default_rules()
        self.version = version
        self.gateway = gateway

    def normalize(self, value, field=None, **kwargs):
        kwargs.setdefault('version', self.version)
        kwargs.setdefault('gateway', self.gateway)
        kwargs.setdefault('rules', self.rules)
        return normalize_message_id(value, field=field, **kwargs)

    def submit(self, record, **kwargs):
        kwargs.setdefault('version', self.version)
        kwargs.setdefault('gateway', self.gateway)
        kwargs.setdefault('rules', self.rules)
        return submit_message_ids(record, **kwargs)

    def receipts(self, record, **kwargs):
        kwargs.setdefault('version', self.version)
        kwargs.setdefault('gateway', self.gateway)
        kwargs.setdefault('rules', self.rules)
        return receipt_message_ids(record, **kwargs)

    def correlate(self, submit_responses, receipts, **kwargs):
        kwargs.setdefault('version', self.version)
        kwargs.setdefault('gateway', self.gateway)
        kwargs.setdefault('rules', self.rules)
        return correlate_message_ids(submit_responses, receipts, **kwargs)


def _as_items(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, dict):
        for key in ('submit_responses', 'receipts', 'items', 'records'):
            if key in value:
                return _as_items(value[key])
        return [value]
    if isinstance(value, six.string_types + six.binary_type):
        return [value]
    if hasattr(value, '__iter__') and not hasattr(value, 'command'):
        return list(value)
    return [value]


def main(argv=None):
    """Command-line entry point for JSON/NDJSON correlation reports."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description='Normalize and correlate SMPP submit/receipt message IDs')
    parser.add_argument(
        'input', nargs='?', default='-',
        help='JSON input file (defaults to stdin)')
    parser.add_argument(
        '--version', default=None, help='protocol/rule version to apply')
    parser.add_argument(
        '--gateway', default=None, help='gateway whose rules to apply')
    args = parser.parse_args(argv)

    if args.input in ('-', None):
        document = json.load(sys.stdin)
    else:
        with open(args.input, 'r') as handle:
            document = json.load(handle)

    if isinstance(document, list):
        submit_responses = document
        receipts = []
    else:
        submit_responses = document.get('submit_responses', document.get('submits', []))
        receipts = document.get('receipts', [])

    rules = default_rules()
    if isinstance(document, dict) and document.get('rules'):
        rules = MessageIdRules(document['rules'])

    result = correlate_message_ids(
        submit_responses,
        receipts,
        version=args.version or (document.get('version') if isinstance(document, dict) else None),
        gateway=args.gateway or (document.get('gateway') if isinstance(document, dict) else None),
        rules=rules,
    )
    json.dump(result.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
