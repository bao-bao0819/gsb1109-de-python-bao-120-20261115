#
# Copyright (c) 2012-2018, CRS4
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
Anonymization of HL7 messages driven by policies declared on full schema paths.

A policy maps a *full path pattern* to a pseudonym or to a callable that builds
one. A path describes an element from the message down to the field, component
or subcomponent to anonymize, using the names exposed by the library:

  * short names, e.g. ``ADT_A01.PID.PID_5.PID_5_2``;
  * long names, e.g. ``ADT_A01.PID.PATIENT_NAME.GIVEN_NAME``;
  * positional paths, e.g. ``ADT_A01.PID.5.2`` or ``ADT_A01-PID-5-2``;
  * segment/field/component notation, e.g. ``PID-5-2``.

Paths are matched against the actual structure of the message, so repeating
groups, repeated segments/fields and dynamic (``varies``) datatypes work without
special syntax:

  * a repeated element matches every occurrence unless an occurrence index is
    given explicitly (``PID_3[2]`` selects only the second occurrence);
  * ``varies`` fields and components are matched by position
    (``OBX.OBX_5.OBX_5_1``), by their dynamic name (``OBX.OBX_5.VARIES_1``), by
    the datatype suggested by the message (``OBX.OBX_5.CWE_1``) or as a whole
    (``OBX.OBX_5``).

Only elements addressed by the policy are changed. Replacing a field rewrites
the whole field, while replacing a component or a subcomponent rewrites only
that part and leaves its siblings untouched (e.g. ``PID_5_2`` changes the given
name and keeps family name and suffix). A value that also occurs at a
different, non-target path is left untouched: rules are applied by position in
the tree, never by textual search.

Generated identifiers are stable: the same value always receives the same token
and different values receive different tokens. When the target element carries
constraints the token respects them, because a pseudonym that violates the
schema would make the output unusable:

  * the maximum length of the element is never exceeded; when the requested
    replacement is longer it is replaced by a deterministic shorter token;
  * values subject to an HL7 table are replaced with a deterministic member of
    the same table;
  * numeric elements receive a numeric value with the original number of digits
    and date/time elements a valid value with the original precision;
  * for ``varies`` elements the datatype declared by the message (e.g. OBX-2
    for OBX-5) drives the same checks.

The functions operate on a deep copy of the message by default and return a new
:class:`Message <hl7apy.core.Message>`; the input message is never modified.
"""

import copy
import datetime
import hashlib
import hmac
import re
from collections.abc import Mapping

from hl7apy.core import Component, Field, Group, Message, Segment, SubComponent, is_base_datatype
from hl7apy.exceptions import ChildNotFound, HL7apyException

__all__ = [
    'AnonymizationError',
    'AnonymizationPolicy',
    'Anonymizer',
    'PathPattern',
    'anonymize',
    'anonymize_er7',
    'anonymize_message',
    'deidentify',
]

_DEFAULT_PREFIX = 'ANON'
_TOKEN_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
_DIGITS = '0123456789'
_TOKEN_MIN_LENGTH = 6
_DATE_FORMATS = {4: '%Y', 6: '%Y%m', 8: '%Y%m%d'}
_TIME_FORMATS = {2: '%H', 4: '%H%M', 6: '%H%M%S'}
_DATATYPE_MAX_LENGTH = {'ST': 199, 'GTS': 199, 'IS': 20, 'FT': 65536, 'TX': 65536, 'SI': 4, 'NM': 16}


class AnonymizationError(HL7apyException):
    """
    Raised when a policy is malformed or cannot be applied to a message
    """


def _text(value):
    if value is None:
        return ''
    return '{0}'.format(value)


def _attr(element, name, default=None):
    return element.__dict__.get(name, default)


def _element_text(element):
    try:
        if isinstance(element, Component) and len(element.children) == 1:
            child = element.children[0]
            if isinstance(child, SubComponent):
                return _text(child.to_er7())
        return _text(element.to_er7())
    except Exception:
        return ''


def _children_of(element):
    try:
        return list(element.children)
    except TypeError:
        return []


def _parent(element):
    if element is None:
        return None
    return element.__dict__.get('_parent')


def _is_structural(element):
    return isinstance(element, (Message, Group, Segment))


def _normalize_path(path):
    path = path.strip()
    path = path.replace('->', '.')
    path = path.replace('/', '.')
    path = re.sub(r'-(?=\d)', '.', path)
    return path


class PathPattern:
    """
    A full schema path pattern

    A pattern is a sequence of element names starting from the message name.
    Every step can carry an occurrence selector: ``[n]`` selects the n-th
    occurrence (1-based), while ``[*]`` or no selector matches every occurrence.
    Names are compared case insensitively and the wildcards ``*`` and ``?`` are
    allowed inside a step.

    >>> pattern = PathPattern('ADT_A01.PID.PID_5.PID_5_2')
    >>> pattern.matches(PathPattern('ADT_A01.PID.PID_5.PID_5_2'))
    True
    >>> PathPattern('PID.PID_5').matches(PathPattern('ADT_A01.PID.PID_5'))
    True
    """

    def __init__(self, path):
        if isinstance(path, PathPattern):
            self.steps = [dict(step) for step in path.steps]
            self.source = path.source
            return
        if isinstance(path, (tuple, list)):
            path = '.'.join(_text(step) for step in path)
        if not isinstance(path, str) or not path.strip():
            raise AnonymizationError('The path pattern must be a non empty string')
        self.source = _normalize_path(path)
        self.steps = [self._parse_step(step) for step in self.source.split('.')]

    @staticmethod
    def _parse_step(step):
        step = step.strip()
        if not step:
            raise AnonymizationError('The path pattern contains an empty step')
        match = re.match(r'^(?P<name>.*?)(?:\[(?P<index>\d+|\*)\])?$', step)
        if match is None:
            raise AnonymizationError('Invalid path step: {0}'.format(step))
        name = match.group('name').strip()
        if not name:
            raise AnonymizationError('Invalid path step: {0}'.format(step))
        index = match.group('index')
        if index == '*':
            index = None
        elif index is not None:
            index = int(index)
            if index < 0:
                raise AnonymizationError('Occurrence indexes cannot be negative: {0}'.format(step))
            if index == 0:  # tolerate a zero-based first occurrence
                index = 1
        return {'name': name.upper(), 'index': index}

    @property
    def name(self):
        """
        The name of the last step, i.e. the target element
        """
        return self.steps[-1]['name']

    def specificity(self):
        """
        Sort key ordering patterns from the most specific to the least specific
        """
        exact = sum(1 for step in self.steps if not any(char in step['name'] for char in '*?'))
        indexed = sum(1 for step in self.steps if step['index'] is not None)
        return (len(self.steps), exact, indexed)

    def __len__(self):
        return len(self.steps)

    def __iter__(self):
        return iter(self.steps)

    def __eq__(self, other):
        if not isinstance(other, PathPattern):
            return NotImplemented
        return self.steps == other.steps

    def __hash__(self):
        return hash(tuple((step['name'], step['index']) for step in self.steps))

    def __str__(self):
        return '.'.join(
            '{0}[{1}]'.format(step['name'], step['index']) if step['index'] is not None else step['name']
            for step in self.steps
        )

    def __repr__(self):
        return '<PathPattern {0}>'.format(self)

    def matches(self, path):
        """
        Check whether a concrete path matches this pattern

        A pattern shorter than the given path is anchored to the end of the
        path, so ``PID.PID_5`` matches ``ADT_A01.PID.PID_5``. Steps without an
        explicit occurrence selector match any occurrence, so
        ``ADT_A01.PID.PID_3`` matches ``ADT_A01.PID.PID_3[2]``.

        :type path: ``str`` or :class:`PathPattern`
        :param path: the concrete path to match
        """
        candidate = path if isinstance(path, PathPattern) else PathPattern(path)
        if len(self.steps) > len(candidate.steps):
            return False
        pattern = self.steps
        steps = candidate.steps[len(candidate.steps) - len(pattern):] if len(pattern) < len(candidate.steps) else candidate.steps
        for expected, actual in zip(pattern, steps):
            if not _name_matches(expected['name'], actual['name']):
                return False
            if expected['index'] is not None and expected['index'] != actual['index']:
                return False
        return True


def _name_matches(pattern, name):
    if pattern == name:
        return True
    if '*' not in pattern and '?' not in pattern:
        return False
    regex = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return re.match('^{0}$'.format(regex), name) is not None


class AnonymizationPolicy:
    """
    A collection of anonymization rules keyed by full schema path patterns

    :type rules: ``dict``, iterable of pairs or ``None``
    :param rules: a mapping from path patterns to pseudonyms. A pseudonym can be
        a string (used as the token), a callable accepting the original text and
        the target element and returning the replacement, or ``None`` to use the
        default stable token.

    :type prefix: ``str``
    :param prefix: prefix used when no explicit pseudonym is supplied. It
        defaults to ``ANON``; shorter tokens are derived when the target
        element cannot hold the prefix.

    :type salt: ``str`` or ``bytes``
    :param salt: optional salt used to derive tokens. It makes token values
        depend on a secret while keeping them deterministic for the same value.

    >>> policy = AnonymizationPolicy({'PID.PID_5': 'PATIENT'})
    >>> policy.paths()
    ['PID.PID_5']
    """

    def __init__(self, rules=None, prefix=_DEFAULT_PREFIX, salt=None):
        self.prefix = prefix or _DEFAULT_PREFIX
        if isinstance(salt, str):
            salt = salt.encode('utf-8')
        self.salt = salt
        self._patterns = {}
        if rules:
            self._add_rules(rules)

    def _add_rules(self, rules):
        if isinstance(rules, Mapping):
            items = list(rules.items())
        else:
            items = list(rules)
        for item in items:
            if isinstance(item, Mapping) or hasattr(item, 'keys'):
                path = item.get('path') or item.get('pattern')
                replacement = item.get('value', item.get('replacement', item.get('token')))
            elif isinstance(item, str):
                path, replacement = item, None
            else:
                try:
                    path, replacement = item
                except (TypeError, ValueError):
                    path, replacement = item, None
            self.add(path, replacement)

    def add(self, pattern, replacement=None):
        """
        Add a rule to the policy

        :type pattern: ``str`` or :class:`PathPattern`
        :param pattern: the full path pattern

        :param replacement: the pseudonym (a string, a callable or ``None``)

        :return: the policy itself, to allow chaining
        """
        path = pattern if isinstance(pattern, PathPattern) else PathPattern(pattern)
        if isinstance(replacement, Mapping):
            options = dict(replacement)
            replacement = options.get(
                'value', options.get('replacement', options.get('token', options.get('pseudonym')))
            )
        self._patterns[path] = replacement
        return self

    def paths(self):
        """
        Return the declared patterns as strings, preserving declaration order
        """
        return [str(pattern) for pattern in self._patterns]

    def __len__(self):
        return len(self._patterns)

    def __contains__(self, pattern):
        return PathPattern(pattern) in self._patterns

    def __iter__(self):
        return iter(self._patterns.items())

    def __repr__(self):
        return '<AnonymizationPolicy with {0} rule(s)>'.format(len(self._patterns))


def _child_position(parent, child):
    """
    Return the position used by numeric path steps

    For repeated siblings (e.g. two PID_13 fields) the occurrence index is
    used, while for fields and components the HL7 position from the name is
    kept (e.g. PID_5 -> 5), even when some positions are empty.
    """
    name = child.name or ''
    match = re.match(r'^.*_(\d+)$', name)
    if match is not None:
        return int(match.group(1))
    return 1


def _element_datatype(element):
    if isinstance(element, (Field, Component, SubComponent)):
        return element.datatype
    return _attr(element, 'datatype')


def _dynamic_datatype(parent):
    datatype = _element_datatype(parent)
    if datatype == 'varies':
        return _inferred_field_datatype(_ancestor_field(parent) or parent)
    return None


def _child_aliases(parent, child, position):
    names = set()
    if child.name:
        names.add(child.name.upper())
    long_name = _attr(child, 'long_name')
    if long_name:
        names.add(_text(long_name).upper())
    parent_name = _attr(parent, 'name')
    dynamic = _dynamic_datatype(parent)
    datatype = _element_datatype(child)
    if isinstance(child, (Field, Component, SubComponent)):
        if parent_name:
            names.add('{0}_{1}'.format(parent_name.upper(), position))
        names.add(str(position))
        if dynamic:
            names.add(dynamic.upper())
            names.add('{0}_{1}'.format(dynamic.upper(), position))
            effective = _effective_datatype(child)
            if effective and effective != 'varies':
                names.add(_text(effective).upper())
        elif (child.name or '').upper().startswith('VARIES_') and datatype not in (None, 'varies'):
            names.add(_text(datatype).upper())
    if isinstance(child, Group):
        root = _root_message_name(child)
        name = (child.name or '').upper()
        if root and name.startswith(root + '_'):
            names.add(name[len(root) + 1:])
    return names


def _root_message_name(element):
    current = element
    while current is not None and not isinstance(current, Message):
        current = _parent(current)
    if current is None:
        return None
    return (current.name or '').upper()


def _matches_step(step, parent, child, position):
    name = step['name']
    if name.isdigit():
        return name == str(position)
    return any(_name_matches(name, alias) for alias in _child_aliases(parent, child, position))


def _collect_matches(parent, step):
    matched = []
    ordinal = 0
    for child in _children_of(parent):
        ordinal += 1
        position = _child_position(parent, child) if child.name is not None else ordinal
        if _matches_step(step, parent, child, position):
            matched.append(child)
        elif isinstance(child, Group):
            matched.extend(_collect_matches(child, step))
    return matched


def _root_matches(step, message):
    aliases = {(message.name or '').upper()}
    aliases.add('MESSAGE')
    return any(_name_matches(step['name'], alias) for alias in aliases)


def _resolve_pattern(message, pattern):
    """
    Return the elements of the message addressed by the given pattern

    Steps are resolved against the real tree. Groups are transparent when a
    step does not match a direct child, so a policy can either spell out all
    the groups (``ORU_R01_PATIENT_RESULT[1].ORU_R01_PATIENT.PID``) or simply
    use the segment name (``PID``). An occurrence index counts the matches at
    its own level, across the groups traversed transparently.
    """
    start = 0
    if _root_matches(pattern.steps[0], message):
        start = 1
    frontier = [message]
    for step in pattern.steps[start:]:
        candidates = []
        for element in frontier:
            candidates.extend(_collect_matches(element, step))
        if step['index'] is not None:
            position = step['index'] - 1
            candidates = candidates[position:position + 1]
        frontier = candidates
        if not frontier:
            break
    return frontier


class _TokenRegistry:
    """
    Build stable tokens and make sure different values do not collide
    """

    def __init__(self, prefix, salt=None):
        self.prefix = prefix
        self.salt = salt
        self._forward = {}
        self._reverse = {}

    def _digest(self, value, length, alphabet, attempt=0):
        key = '{0}\x00{1}'.format(value, attempt).encode('utf-8')
        if self.salt is not None:
            digest = hmac.new(self.salt, key, hashlib.sha256).digest()
        else:
            digest = hashlib.sha256(key).digest()
        number = int.from_bytes(digest, 'big')
        chars = []
        base = len(alphabet)
        for _ in range(length):
            number, remainder = divmod(number, base)
            chars.append(alphabet[remainder])
        return ''.join(chars)

    def token(self, value, maximum=None, alphabet=_TOKEN_ALPHABET, prefix=None):
        key = (value, maximum, alphabet, prefix)
        if key in self._forward:
            return self._forward[key]
        prefix = self.prefix if prefix is None else prefix
        if maximum is not None and maximum > 0:
            room = maximum - len(prefix)
            if room < _TOKEN_MIN_LENGTH:
                length = maximum
                candidate_prefix = ''
            else:
                length = min(_TOKEN_MIN_LENGTH, room)
                candidate_prefix = prefix
        else:
            length = 8
            candidate_prefix = prefix
        attempt = 0
        while True:
            token = candidate_prefix + self._digest(value, length, alphabet, attempt)
            if token not in self._reverse or self._reverse[token] == value:
                self._forward[key] = token
                self._reverse[token] = value
                return token
            attempt += 1
            if attempt > 1000:
                raise AnonymizationError('Cannot build a unique token for the given value')


class _ValueFactory:
    """
    Build replacement values that respect the constraints of the target element
    """

    def __init__(self, prefix, salt=None):
        self.registry = _TokenRegistry(prefix, salt)

    def make(self, value, element, rule=None):
        if rule is not None and callable(rule):
            try:
                replacement = rule(value, element)
            except TypeError:
                replacement = rule(value)
        elif isinstance(rule, str):
            replacement = rule
        else:
            replacement = None
        if replacement is None:
            return self._default_token(value, element)
        return self._fit(_text(replacement), value, element)

    def _default_token(self, value, element):
        constraint = _constraint(element)
        datatype = constraint['datatype']
        maximum = constraint['max_length']
        table = constraint['table']
        if table:
            table_value = self._table_value(None, value, element, table)
            if table_value is not None:
                return table_value
        if datatype in ('NM', 'SI'):
            return self._numeric_value(None, value, datatype)
        if datatype in ('DT', 'DTM', 'TM'):
            return self._datetime_value(value, datatype)
        return self.registry.token(value, maximum)

    def _fit(self, replacement, original, element):
        constraint = _constraint(element)
        datatype = constraint['datatype']
        table = constraint['table']
        maximum = constraint['max_length']
        if table:
            table_value = self._table_value(replacement, original, element, table)
            if table_value is not None:
                return table_value
        if datatype in ('NM', 'SI'):
            return self._numeric_value(replacement, original, datatype)
        if datatype in ('DT', 'DTM', 'TM'):
            if _is_valid_datetime(replacement, datatype):
                return replacement
            return self._datetime_value(original, datatype)
        if maximum is not None and maximum > 0 and len(replacement) > maximum:
            return self.registry.token(replacement, maximum)
        return replacement

    def _table_value(self, replacement, original, element, table):
        try:
            from hl7apy import load_reference

            values = tuple(load_reference(table, 'Table', element.version)[1])
        except (ChildNotFound, KeyError, IndexError, TypeError):
            values = ()
        except Exception as error:
            if _attr(element, 'version') not in _supported_versions():
                return None
            raise AnonymizationError(
                'Cannot load table {0} for {1}: {2}'.format(table, element.name, error)
            )
        if not values:
            return None
        if replacement is not None:
            if replacement in values:
                return replacement
            digest = hashlib.sha256((replacement or '').encode('utf-8') + (original or '').encode('utf-8')).digest()
        else:
            digest = hashlib.sha256((original or '').encode('utf-8')).digest()
        position = int.from_bytes(digest[:4], 'big') % len(values)
        if replacement is None and values[position] == original and len(values) > 1:
            position = (position + 1) % len(values)
        return values[position]

    def _numeric_value(self, replacement, original, datatype):
        maximum = 4 if datatype == 'SI' else 16
        if replacement is not None and re.match(r'^[+-]?\d+(\.\d+)?$', replacement):
            if len(replacement.lstrip('+-')) <= maximum:
                return replacement
        negative = original.lstrip().startswith('-')
        fractional = len(original.split('.')[-1]) if '.' in original else 0
        if replacement is None:
            width = max(1, len(original.replace('.', '').replace('-', '').strip()) or 1)
            width = min(width, maximum)
            digits = self.registry.token(original, width, _DIGITS)
            digits = digits.lstrip('0') or '1'
        else:
            digits = ''.join(char for char in replacement if char.isdigit())
            if not digits:
                width = max(1, len(original.replace('.', '').replace('-', '').strip()) or 1)
                digits = self.registry.token(original, min(width, maximum), _DIGITS).lstrip('0') or '1'
        if datatype == 'SI':
            return (digits or '1')[-maximum:] or '1'
        sign = '-' if negative else ''
        digits = digits[: max(1, maximum - (fractional + 1 if fractional else 0))]
        if fractional:
            if len(digits) <= fractional:
                digits = digits.rjust(fractional + 1, '0')
            return '{0}{1}.{2}'.format(sign, digits[:-fractional], digits[-fractional:])
        return sign + (digits or '0')

    def _datetime_value(self, original, datatype):
        value = original
        offset = ''
        match = re.search(r'([+-]\d{4})$', value)
        if match is not None:
            offset = match.group(1)
            value = value[: -len(offset)]
        if datatype == 'TM':
            value = value.lstrip('T')
        precision = len(value)
        fmt = None
        if datatype == 'TM':
            fmt = _TIME_FORMATS.get(precision)
        elif datatype == 'DT':
            fmt = _DATE_FORMATS.get(precision)
        elif datatype == 'DTM':
            if precision in _DATE_FORMATS:
                fmt = _DATE_FORMATS[precision]
            elif precision in (10, 12, 14):
                fmt = {10: '%Y%m%d%H', 12: '%Y%m%d%H%M', 14: '%Y%m%d%H%M%S'}[precision]
            elif 16 <= precision <= 19 and '.' in value:
                fmt = '%Y%m%d%H%M%S.%f'
        if fmt is None:
            fmt = _DATE_FORMATS.get(precision, '%Y%m%d')
        seed = (original or '') + (fmt or '')
        if self.registry.salt is not None:
            digest = hmac.new(self.registry.salt, seed.encode('utf-8'), hashlib.sha256).digest()
        else:
            digest = hashlib.sha256(seed.encode('utf-8')).digest()
        seconds = int.from_bytes(digest[:6], 'big') % (50 * 365 * 24 * 3600)
        moment = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=seconds)
        token = moment.strftime(fmt)
        if '%f' in fmt:
            digits = len(value.split('.')[-1])
            token = token[: token.index('.') + 1] + self.registry.token(original, digits, _DIGITS)
        return token + offset


def _supported_versions():
    from hl7apy import SUPPORTED_LIBRARIES

    return SUPPORTED_LIBRARIES


def _is_valid_datetime(value, datatype):
    try:
        from hl7apy.utils import check_date, check_datetime, check_timestamp

        if datatype == 'DT':
            return check_date(value)
        if datatype == 'TM':
            return check_timestamp(value)
        return check_datetime(value)
    except Exception:
        return False


def _ancestor_field(element):
    current = element
    while current is not None and not isinstance(current, Field):
        current = _parent(current)
        if isinstance(current, (Message, Segment)):
            return None
    return current


def _field_value(field):
    if field is None:
        return None
    try:
        return _text(field.to_er7())
    except Exception:
        return None


def _effective_datatype(element):
    datatype = _element_datatype(element)
    if datatype and datatype != 'varies':
        return datatype
    if isinstance(element, Field):
        return _inferred_field_datatype(element) or datatype
    if isinstance(element, (Component, SubComponent)):
        field = _ancestor_field(element)
        return _inferred_field_datatype(field) or datatype
    return datatype


def _is_known_datatype(datatype, version):
    if not datatype or datatype == 'varies':
        return False
    if is_base_datatype(datatype, version):
        return True
    try:
        from hl7apy import load_reference

        load_reference(datatype, 'Datatypes_Structs', version)
    except (ChildNotFound, KeyError):
        return False
    return True


def _inferred_field_datatype(field):
    if field is None or field.datatype != 'varies':
        return None
    segment = _parent(field)
    version = _attr(field, 'version')
    if isinstance(segment, Segment) and segment.name == 'OBX':
        value_type = None
        for child in _children_of(segment):
            if child.name == 'OBX_2':
                value_type = _field_value(child)
                break
        if value_type:
            value_type = value_type.strip().upper()
            if _is_known_datatype(value_type, version):
                return value_type
    for child in _children_of(field):
        candidate = _leaf_datatype(child)
        if _is_known_datatype(candidate, version):
            return candidate
    return None


def _leaf_datatype(element):
    datatype = _element_datatype(element)
    if is_base_datatype(datatype, _attr(element, 'version')):
        return datatype
    for child in _children_of(element):
        candidate = _leaf_datatype(child)
        if candidate:
            return candidate
    return datatype


def _effective_max_length(element):
    maximum = _attr(element, 'max_length')
    if maximum is None:
        try:
            maximum = element.reference[5]
        except (AttributeError, IndexError, TypeError):
            maximum = None
    if maximum is None or maximum < 0:
        maximum = _DATATYPE_MAX_LENGTH.get(_effective_datatype(element))
    return maximum


def _first_component_constraint(element):
    """
    Return the constraint of the first component of a composite element

    When the whole element is replaced, the replacement lands in its first
    component, so the constraints that matter are the ones of that component
    (e.g. the DTM of a TS field, the ST of an FN component).
    """
    children = _children_of(element)
    if children:
        child = children[0]
        if _is_structural(child):
            return None
        return _constraint(child)
    version = _attr(element, 'version')
    structure = _attr(element, 'structure_by_name') or {}
    ordered = _attr(element, 'ordered_children') or []
    if not structure or not ordered:
        return None
    entry = structure.get(ordered[0])
    if not entry:
        return None
    reference = entry.get('ref')
    if reference is None or len(reference) <= 2:
        return None
    datatype = reference[2]
    if datatype in (None, 'varies'):
        return None
    if is_base_datatype(datatype, version):
        return {'datatype': datatype, 'table': reference[4] if len(reference) > 4 else None,
                'max_length': reference[5] if len(reference) > 5 else None}
    try:
        from hl7apy import load_reference

        children_ref = load_reference(datatype, 'Datatypes_Structs', version)
    except (ChildNotFound, KeyError, IndexError, TypeError):
        return None
    if not children_ref:
        return None
    first = children_ref[0]
    if first is None or len(first) < 3:
        return None
    first_ref = first[2]
    if first_ref is None or len(first_ref) <= 2:
        return None
    first_datatype = first_ref[2]
    return {'datatype': first_datatype,
            'table': first_ref[4] if len(first_ref) > 4 else None,
            'max_length': first_ref[5] if len(first_ref) > 5 else None}


def _constraint(element):
    datatype = _effective_datatype(element)
    table = _attr(element, 'table')
    maximum = _effective_max_length(element)
    if isinstance(element, (Field, Component)) and datatype not in (None, 'varies'):
        if not is_base_datatype(datatype, _attr(element, 'version')):
            nested = _first_component_constraint(element)
            if nested is not None:
                if nested['datatype'] in (None, 'varies'):
                    return {'datatype': datatype, 'table': table, 'max_length': maximum}
                if not nested['table']:
                    nested['table'] = table
                if nested['max_length'] in (None, -1):
                    nested['max_length'] = maximum
                return nested
    return {'datatype': datatype, 'table': table, 'max_length': maximum}


def _apply_rule(element, replacement, factory):
    if _is_structural(element):
        raise AnonymizationError('Structural elements cannot be anonymized: {0}'.format(element.name))
    original = _element_text(element)
    if not original:
        return
    name = (element.name or '').upper()
    if name in ('MSH_1', 'MSH_2'):
        raise AnonymizationError('The encoding characters cannot be anonymized')
    if isinstance(element, SubComponent):
        element.value = factory.make(original, element, replacement)
        return
    if isinstance(element, (Field, Component)):
        element.value = factory.make(original, element, replacement)
        return
    raise AnonymizationError('Unsupported element: {0}'.format(element))


def _as_policy(policy):
    if isinstance(policy, AnonymizationPolicy):
        return policy
    if isinstance(policy, Mapping) or isinstance(policy, (list, tuple, set)):
        return AnonymizationPolicy(policy)
    raise AnonymizationError('The policy must be an AnonymizationPolicy or a mapping')


def anonymize_message(message, policy, deep_copy=True, in_place=None):
    """
    Anonymize an HL7 message according to the given policy

    :type message: :class:`Message <hl7apy.core.Message>` or ``str``
    :param message: the message to anonymize. A string is parsed with
        :func:`parse_message <hl7apy.parser.parse_message>`.

    :type policy: :class:`AnonymizationPolicy` or ``dict``
    :param policy: the anonymization policy

    :type deep_copy: ``bool``
    :param deep_copy: when ``True`` (the default) the input message is left
        untouched and an anonymized copy is returned.

    :type in_place: ``bool`` or ``None``
    :param in_place: when ``True`` the given message is modified in place and
        returned. ``None`` means ``not deep_copy``.

    :rtype: :class:`Message <hl7apy.core.Message>`

    >>> from hl7apy.parser import parse_message
    >>> message = parse_message('MSH|^~\\\\&|S|F|R|F|20200101000000||ADT^A01^ADT_A01|1|P|2.5\\r'
    ...                         'EVN||20200101000000\\r'
    ...                         'PID|1||123^^^H^MR||DOE^JOHN^A||19800101|M\\r')
    >>> policy = AnonymizationPolicy({'PID.PID_5.PID_5_2': 'ANON'})
    >>> result = anonymize_message(message, policy)
    >>> print(result.pid.pid_5.to_er7())
    DOE^ANON^A
    >>> print(message.pid.pid_5.to_er7())
    DOE^JOHN^A
    """
    if isinstance(message, (str, bytes)):
        from hl7apy.parser import parse_message

        message = parse_message(message)
    if not isinstance(message, Message):
        raise AnonymizationError('The message must be an instance of hl7apy.core.Message')
    policy = _as_policy(policy)
    if in_place is not None:
        deep_copy = not in_place
    target = copy.deepcopy(message) if deep_copy else message
    factory = _ValueFactory(policy.prefix, policy.salt)

    # Specific rules (e.g. PID_5_2) run before broader ones (e.g. PID_5), so
    # that a broader rule cannot overwrite a more precise replacement.
    rules = sorted(policy, key=lambda item: item[0].specificity())
    for pattern, replacement in rules:
        for element in _resolve_pattern(target, pattern):
            if _is_structural(element):
                raise AnonymizationError(
                    'Structural elements cannot be anonymized: {0}'.format(element.name)
                )
            _apply_rule(element, replacement, factory)
    return target


def anonymize_er7(message, policy):
    """
    Anonymize an ER7-encoded message and return the anonymized ER7 string

    :type message: ``str``
    :param message: the ER7-encoded message

    :type policy: :class:`AnonymizationPolicy` or ``dict``
    :param policy: the anonymization policy

    :rtype: ``str``

    >>> msg = ('MSH|^~\\\\&|S|F|R|F|20200101000000||ADT^A01^ADT_A01|1|P|2.5\\r'
    ...        'EVN||20200101000000\\r'
    ...        'PID|1||123^^^H^MR||DOE^JOHN^A||19800101|M\\r')
    >>> print(anonymize_er7(msg, {'PID.PID_5.PID_5_2': 'X'}).split('\\r')[2])
    PID|1||123^^^H^MR||DOE^X^A||19800101|M
    """
    return anonymize_message(message, policy).to_er7()


class Anonymizer:
    """
    Reusable anonymizer bound to a policy

    >>> anonymizer = Anonymizer({'PID.PID_5': 'PATIENT'})
    >>> anonymizer.paths()
    ['PID.PID_5']
    """

    def __init__(self, policy=None, prefix=_DEFAULT_PREFIX, salt=None):
        if isinstance(policy, AnonymizationPolicy):
            self.policy = policy
        else:
            self.policy = AnonymizationPolicy(policy, prefix=prefix, salt=salt)

    def anonymize(self, message, deep_copy=True):
        """
        Anonymize a message with the bound policy
        """
        return anonymize_message(message, self.policy, deep_copy=deep_copy)

    def anonymize_message(self, message, deep_copy=True):
        """
        Anonymize a message with the bound policy
        """
        return self.anonymize(message, deep_copy=deep_copy)

    def anonymize_er7(self, message):
        """
        Anonymize an ER7-encoded message with the bound policy
        """
        return anonymize_er7(message, self.policy)

    def paths(self):
        """
        Return the patterns of the bound policy
        """
        return self.policy.paths()

    def add(self, pattern, replacement=None):
        """
        Add a rule to the bound policy
        """
        self.policy.add(pattern, replacement)
        return self


anonymize = anonymize_message
deidentify = anonymize_message
