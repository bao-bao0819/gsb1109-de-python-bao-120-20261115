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

import json
import os
import re
from collections import namedtuple
from collections.abc import Mapping

from hl7apy import load_reference
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.exceptions import ChildNotFound, ValidationError, ValidationWarning

ErrorsAndWarnings = namedtuple("ErrorsAndWarnings", ("is_valid", "errors", "warnings"))


class _ValidatedIssues(ErrorsAndWarnings):
    """
    Historical ``ErrorsAndWarnings`` result enriched with the structure/value
    split. It is still a 3-field tuple, so existing unpacking keeps working.
    """

    __slots__ = ()

    @property
    def issues(self):
        return list(self.errors) + list(self.warnings)

    @property
    def structure_errors(self):
        return [issue for issue in self.errors if issue.category == 'structure']

    @property
    def value_errors(self):
        return [issue for issue in self.issues if issue.category == 'value']

    def to_dict(self):
        return {
            'is_valid': self.is_valid,
            'errors': [issue.to_dict() for issue in self.errors],
            'warnings': [issue.to_dict() for issue in self.warnings],
            'structure_errors': [issue.to_dict() for issue in self.structure_errors],
            'value_errors': [issue.to_dict() for issue in self.value_errors],
        }


def _is_container(element):
    return getattr(element, 'classname', None) in ('Message', 'Group')


def _element_occurrence(element):
    """
    Return the 1-based occurrence of ``element`` among its same-named siblings.
    """
    parent = getattr(element, 'parent', None)
    if parent is None:
        return 1
    try:
        siblings = parent.children.indexes.get(element.name, [])
    except AttributeError:
        return 1
    for position, sibling in enumerate(siblings, 1):
        if sibling is element:
            return position
    return 1


def _element_path(element):
    """
    Build a locatable ``/``-separated path from the root message down to
    ``element``. Message and group occurrences are always made explicit so that
    two repetitions of the same group cannot be confused.
    """
    parts = []
    current = element
    while current is not None:
        name = current.name or current.classname
        if getattr(current, 'classname', None) == 'Message':
            parts.append(name)
        else:
            parts.append('{0}[{1}]'.format(name, _element_occurrence(current)))
        current = getattr(current, 'parent', None)
    return '/'.join(reversed(parts))


def _group_path(element):
    """
    Return the path of the nearest enclosing message/group (the element itself
    when it is a group).
    """
    current = element
    while current is not None and not _is_container(current):
        current = getattr(current, 'parent', None)
    if current is None:
        return None
    return _element_path(current)


def _target_kind(element):
    classname = getattr(element, 'classname', None)
    return {
        'Message': 'message',
        'Group': 'group',
        'Segment': 'segment',
        'Field': 'field',
        'Component': 'component',
        'SubComponent': 'subcomponent',
    }.get(classname, 'element')


def _child_kind(kind):
    return {'SEG': 'segment', 'GRP': 'group', 'FIE': 'field'}.get(kind, 'element')


def _constraint_from_cardinality(cardinality):
    if cardinality is None:
        return None
    return '{0}..{1}'.format(cardinality[0], cardinality[1])

_NAME_INDEX_RE = re.compile(r'_([0-9]+)$')


def _safe_er7(element):
    try:
        return element.to_er7()
    except Exception:
        return None


def _position_from_name(name, parent, element):
    match = _NAME_INDEX_RE.search(name or '')
    if match:
        return int(match.group(1))
    try:
        return parent.children.list.index(element) + 1
    except (AttributeError, ValueError):
        return 1


class _SourceLocator:
    """
    Map elements back to the ER7 text they were parsed from.

    The parser discards empty trailing fields and separators when encoding to
    ER7 again, so ``to_er7`` cannot be used as the original fragment. The raw
    source is attached to the parsed message by
    :func:`hl7apy.parser.parse_message` and sliced here.
    """

    def __init__(self, source_text, root):
        self.source_text = source_text
        self.root = root
        self.segment_fragments = {}
        self.field_sep = '|'
        self.component_sep = '^'
        self.subcomponent_sep = '&'
        self.repetition_sep = '~'
        self.segment_sep = '\r'
        try:
            chars = root.encoding_chars
            self.field_sep = chars.get('FIELD', self.field_sep)
            self.component_sep = chars.get('COMPONENT', self.component_sep)
            self.subcomponent_sep = chars.get('SUBCOMPONENT', self.subcomponent_sep)
            self.repetition_sep = chars.get('REPETITION', self.repetition_sep)
            self.segment_sep = chars.get('SEGMENT', self.segment_sep)
        except Exception:
            pass

        source_segments = self._split_source()
        tree_segments = list(self._iter_segments(root))
        if len(tree_segments) == len(source_segments):
            for segment, raw in zip(tree_segments, source_segments):
                self.segment_fragments[id(segment)] = raw
        else:
            # The tree has been modified after parsing (e.g. a segment was
            # deleted). Align by segment name without choosing the same source
            # segment twice, so removing OBR does not shift every later
            # fragment.
            by_name = {}
            for raw in source_segments:
                if len(raw) >= 3:
                    by_name.setdefault(raw[:3].upper(), []).append(raw)
            for name, candidates in by_name.items():
                matching = [s for s in tree_segments if (s.name or '').upper() == name]
                for segment, raw in zip(matching, candidates):
                    self.segment_fragments[id(segment)] = raw

    def _split_source(self):
        segments = []
        for raw in self.source_text.split(self.segment_sep):
            raw = raw.strip('\r\n')
            if raw:
                segments.append(raw)
        return segments

    def _iter_segments(self, element):
        classname = getattr(element, 'classname', None)
        if classname == 'Segment':
            yield element
            return
        if classname in ('Message', 'Group'):
            try:
                children = list(element.children.list)
            except AttributeError:
                children = []
            for child in children:
                yield from self._iter_segments(child)

    def _segment_of(self, element):
        current = element
        while current is not None and getattr(current, 'classname', None) != 'Segment':
            current = getattr(current, 'parent', None)
        return current

    def content(self, element):
        """
        Return the original fragments of every segment in ``element`` joined
        by the original segment separator. Unlike ``to_er7`` this keeps empty
        trailing fields and repeated values exactly as they were.
        """
        fragments = []
        for segment in self._iter_segments(element):
            raw = self.segment_fragments.get(id(segment))
            if raw is not None:
                fragments.append(raw)
        if not fragments:
            return None
        return self.segment_sep.join(fragments)

    def _field_raw(self, segment_raw, position, repetition):
        parts = segment_raw.split(self.field_sep)
        if segment_raw[:3].upper() == 'MSH':
            if position == 1:
                raw = self.field_sep
            else:
                index = position - 1
                raw = parts[index] if index < len(parts) else ''
        else:
            raw = parts[position] if position < len(parts) else ''
        repetitions = raw.split(self.repetition_sep)
        index = max(repetition, 1) - 1
        if index >= len(repetitions):
            return ''
        return repetitions[index]

    def fragment(self, element):
        if element is None:
            return None
        classname = getattr(element, 'classname', None)
        if classname in ('Message', 'Group'):
            return self.content(element)
        segment = self._segment_of(element)
        if segment is None:
            return None
        raw = self.segment_fragments.get(id(segment))
        if raw is None:
            return None
        if classname == 'Segment':
            return raw

        field = element
        while field is not None and getattr(field, 'classname', None) != 'Field':
            field = getattr(field, 'parent', None)
        if field is None:
            return raw
        field_pos = _position_from_name(field.name, field.parent, field)
        field_raw = self._field_raw(raw, field_pos, _element_occurrence(field))
        if classname == 'Field':
            return field_raw or _safe_er7(field) or raw

        component = element
        while component is not None and getattr(component, 'classname', None) != 'Component':
            component = getattr(component, 'parent', None)
        if component is None:
            return field_raw or raw
        component_pos = _position_from_name(component.name, component.parent, component)
        components = field_raw.split(self.component_sep)
        component_raw = components[component_pos - 1] if component_pos - 1 < len(components) else ''
        if classname == 'Component':
            return component_raw or _safe_er7(component) or field_raw or raw

        sub_pos = _position_from_name(element.name, element.parent, element)
        subcomponents = component_raw.split(self.subcomponent_sep)
        sub_raw = subcomponents[sub_pos - 1] if sub_pos - 1 < len(subcomponents) else ''
        return sub_raw or _safe_er7(element) or component_raw or field_raw or raw


class ValidationResult:
    """
    Result of the validation of a single message.

    It keeps the historical ``is_valid``/``errors``/``warnings`` interface and
    adds the structure/value split used by batch reports.
    """

    def __init__(self, source, errors, warnings):
        self.source = source
        self.errors = list(errors)
        self.warnings = list(warnings)
        self.is_valid = not self.errors

    @property
    def issues(self):
        return self.errors + self.warnings

    @property
    def structure_errors(self):
        return [issue for issue in self.errors if issue.category == 'structure']

    @property
    def value_errors(self):
        # In HL7apy value constraints (tables, lengths) are warnings. They are
        # reported separately from structural problems without making an
        # otherwise deliverable message invalid.
        return [issue for issue in self.issues if issue.category == 'value']

    def to_dict(self):
        return {
            'is_valid': self.is_valid,
            'errors': [issue.to_dict() for issue in self.errors],
            'warnings': [issue.to_dict() for issue in self.warnings],
            'structure_errors': [issue.to_dict() for issue in self.structure_errors],
            'value_errors': [issue.to_dict() for issue in self.value_errors],
        }


class BatchValidationItem:
    """
    Validation result of one message inside a batch.
    """

    def __init__(self, index, message, message_id=None, source=None, parse_error=None):
        self.index = index
        self.message = message
        self.message_id = message_id
        self.input = source
        self.parse_error = parse_error
        self._result = None

    @property
    def result(self):
        return self._result

    @property
    def errors(self):
        return [] if self._result is None else self._result.errors

    @property
    def warnings(self):
        return [] if self._result is None else self._result.warnings

    @property
    def issues(self):
        return self.errors + self.warnings

    @property
    def structure_errors(self):
        return [issue for issue in self.errors if issue.category == 'structure']

    @property
    def value_errors(self):
        return [issue for issue in self.issues if issue.category == 'value']

    @property
    def is_valid(self):
        return False if self.parse_error is not None else bool(self._result and self._result.is_valid)

    @property
    def name(self):
        name = getattr(self.message, 'name', None)
        if name:
            return name
        return None

    @property
    def message_name(self):
        return self.name

    @property
    def structure_problems(self):
        return self.structure_errors

    @property
    def value_problems(self):
        return self.value_errors

    @property
    def as_dict(self):
        return self.to_dict()

    def to_dict(self):
        return {
            'index': self.index,
            'message_id': self.message_id,
            'message_name': self.name,
            'is_valid': self.is_valid,
            'parse_error': self.parse_error,
            'errors': [issue.to_dict() for issue in self.errors],
            'warnings': [issue.to_dict() for issue in self.warnings],
            'structure_errors': [issue.to_dict() for issue in self.structure_errors],
            'value_errors': [issue.to_dict() for issue in self.value_errors],
        }


class BatchValidationResult:
    """
    Result of ``Validator.validate_batch``.

    Valid messages are kept in the result as well: a batch with invalid
    messages still delivers every other message normally.
    """

    def __init__(self, items):
        self.items = list(items)

    @property
    def results(self):
        return self.items

    @property
    def is_valid(self):
        return all(item.is_valid for item in self.items)

    @property
    def valid_items(self):
        return [item for item in self.items if item.is_valid]

    @property
    def invalid_items(self):
        return [item for item in self.items if not item.is_valid]

    @property
    def valid_messages(self):
        return [item.message for item in self.valid_items]

    # Convenience aliases used by callers that prefer a plural/singular naming
    # consistent with the rest of the library.
    @property
    def structure_problems(self):
        return self.structure_errors

    @property
    def value_problems(self):
        return self.value_errors

    def as_dict(self):
        return self.to_dict()

    def write_report(self, report_file):
        _write_batch_report(self, report_file)

    @property
    def errors(self):
        return [issue for item in self.items for issue in item.errors]

    @property
    def warnings(self):
        return [issue for item in self.items for issue in item.warnings]

    @property
    def structure_errors(self):
        return [issue for item in self.items for issue in item.structure_errors]

    @property
    def value_errors(self):
        return [issue for item in self.items for issue in item.value_errors]

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def summary(self):
        return {
            'total': len(self.items),
            'valid': len(self.valid_items),
            'invalid': len(self.invalid_items),
            'structure_errors': len(self.structure_errors),
            'value_errors': len(self.value_errors),
        }

    def to_dict(self):
        return {
            'is_valid': self.is_valid,
            'summary': self.summary(),
            'items': [item.to_dict() for item in self.items],
            'structure_errors': [issue.to_dict() for issue in self.structure_errors],
            'value_errors': [issue.to_dict() for issue in self.value_errors],
        }

    def report_text(self):
        lines = []
        summary = self.summary()
        lines.append(
            'Batch validation: {total} message(s), {valid} valid, {invalid} invalid, '
            '{structure_errors} structure error(s), {value_errors} value error(s)'.format(**summary)
        )
        for item in self.items:
            label = item.message_id if item.message_id is not None else item.index
            lines.append('')
            lines.append(
                'Message {0} ({1}): {2}'.format(label, item.name or 'unknown', 'VALID' if item.is_valid else 'INVALID')
            )
            if item.parse_error:
                lines.append('  Parse error: {0}'.format(item.parse_error))
            other_warnings = [
                issue for issue in item.warnings if issue.category != 'value'
            ]
            if item.structure_errors:
                lines.append('  Structure errors:')
                for issue in item.structure_errors:
                    lines.append('    - {0}'.format(_format_issue(issue)))
            if item.value_errors:
                lines.append('  Value errors:')
                for issue in item.value_errors:
                    lines.append('    - {0}'.format(_format_issue(issue)))
            if other_warnings:
                lines.append('  Other warnings:')
                for issue in other_warnings:
                    lines.append('    - {0}'.format(_format_issue(issue)))
            if not item.structure_errors and not item.value_errors and not other_warnings:
                lines.append('  No issues')
        return '\n'.join(lines)


def _format_issue(issue):
    return (
        '[{code}] path={path} group_path={group} occurrence={occurrence} '
        'constraint={constraint} fragment={fragment!r}: {message}'.format(
            code=issue.code or 'ISSUE',
            path=issue.path,
            group=issue.group_path,
            occurrence=issue.occurrence,
            constraint=issue.constraint,
            fragment=issue.fragment,
            message=issue.message,
        )
    )


class Validator:
    """
    Class that handles validation. It defines validation levels and validate
    an element using :attr:`VALIDATION.STRICT <hl7apy.consts.VALIDATION_LEVEL.STRICT>` validation level
    """

    def __init__(self, level):
        super().__init__()
        self.level = level

    @staticmethod
    def validate(element, reference=None, report_file=None, return_errors=False):
        """
        Checks if the :class:`Element <hl7apy.core.Element>` is a valid HL7 message according to the reference
        specified. If the reference is not specified, it will be used the official HL7 structures for the
        elements.
        In particular it checks:

        * the maximum and minimum number of occurrences for every child
        * that children are all allowed
        * the datatype of fields, components and subcomponents
        * the values, in particular the length and the adherence with the HL7 table, if one is specified

        All errors that occur will be written to a file, if any of the two following conditions is met:
        1. either the :attr:`report_file` is an instance (file-like object) with a .write() method, then report_file.write() is called;
        2. or the :attr:`report_file` is a serializable (string-like object)
        value that will be used to as the path to create a new_file, then new_file.write() is called; if the file already exists, it will be overwritten.

        If :attr:`return_errors` is False, the functions raises the first exception that it finds.
        If :attr:`return_errors` is True, errors and warnings are returned using a namedtuple with "errors" and "warnings" list fields.
        Else the funtion returns True.

        Every returned :class:`ValidationError <hl7apy.exceptions.ValidationError>` and
        :class:`ValidationWarning <hl7apy.exceptions.ValidationWarning>` also carries
        machine readable location information (:attr:`category`, :attr:`path`,
        :attr:`group_path`, :attr:`occurrence`, :attr:`constraint`, :attr:`fragment`).

        :param element: :class:`Element <hl7apy.core.Element>`: The element to validate
        :param reference: the reference to use. Usually is None or a message profile object
        :param report_file: the name of the report file to create, or an instance having a write() method

        :return: The True if everything is ok
        :raises: :exc:`ValidationError <hl7apy.exceptions.ValidationError>`: when errors occur
        :raises: :exc:`ValidationWarning <hl7apy.exceptions.ValidationWarning>`: errors concerning the values
        """

        from hl7apy.core import is_base_datatype

        source_text = getattr(element, '_hl7apy_source_text', None)
        locator = _SourceLocator(source_text, element) if source_text else None

        def _fragment(el):
            if locator is not None:
                fragment = locator.fragment(el)
                if fragment is not None:
                    return fragment
            try:
                if getattr(el, 'classname', None) == 'Segment':
                    return el.to_er7()
                parent = getattr(el, 'parent', None)
                while parent is not None and getattr(parent, 'classname', None) != 'Segment':
                    parent = getattr(parent, 'parent', None)
                if parent is not None:
                    return parent.to_er7()
            except Exception:
                pass
            return None

        def _new_issue(
            cls,
            message,
            code,
            category='structure',
            element=None,
            path=None,
            group_path=None,
            occurrence=None,
            constraint=None,
            fragment=None,
            element_name=None,
            target_kind=None,
            is_cascade=False,
            caused_by=None,
        ):
            if element is not None:
                if path is None:
                    path = _element_path(element)
                if group_path is None:
                    group_path = _group_path(element)
                if occurrence is None:
                    occurrence = _element_occurrence(element)
                if fragment is None:
                    fragment = _fragment(element)
                if element_name is None:
                    element_name = element.name
                if target_kind is None:
                    target_kind = _target_kind(element)
            return cls(
                message,
                code=code,
                category=category,
                path=path,
                group_path=group_path,
                occurrence=occurrence,
                constraint=constraint,
                fragment=fragment,
                element=element,
                element_name=element_name,
                target_kind=target_kind,
                is_cascade=is_cascade,
                caused_by=caused_by,
            )

        def _parent_group_path(parent, parent_is_container):
            if parent_is_container:
                return _element_path(parent)
            return _group_path(parent)

        def _missing_child_issue(el, child_name, cardinality, kind, errs, parent_is_container):
            occurrence = _element_occurrence(el)
            path = '{0}/{1}[{2}]'.format(_element_path(el), child_name, 1)
            group_path = _parent_group_path(el, parent_is_container)
            constraint = _constraint_from_cardinality(cardinality)
            # A missing group has no source text of its own; quoting the parent
            # message would be misleading. A missing segment is located by the
            # original content of the group that should contain it.
            if kind == 'GRP':
                fragment = None
            elif locator is not None and _is_container(el):
                fragment = locator.content(el)
                ancestor = getattr(el, 'parent', None)
                while fragment is None and ancestor is not None:
                    fragment = locator.content(ancestor)
                    ancestor = getattr(ancestor, 'parent', None)
            else:
                fragment = _fragment(el)
            errs.append(
                _new_issue(
                    ValidationError,
                    "Missing required child {}.{}".format(el.name, child_name),
                    'MISSING_REQUIRED_CHILD',
                    element=None,
                    path=path,
                    group_path=group_path,
                    occurrence=occurrence,
                    constraint=constraint,
                    fragment=fragment,
                    element_name=child_name,
                    target_kind=_child_kind(kind),
                )
            )

        def _empty_group_issue(el, cardinality, errs, code, message):
            errs.append(
                _new_issue(
                    ValidationError,
                    message,
                    code,
                    element=el,
                    group_path=_element_path(el),
                    occurrence=_element_occurrence(el),
                    constraint=_constraint_from_cardinality(cardinality),
                    target_kind='group',
                    is_cascade=False,
                )
            )

        def _check_z_element(el, errs, warns):
            if el.classname == 'Field':
                if is_base_datatype(el.datatype, el.version) or el.datatype == 'varies':
                    return True
                elif el.datatype is not None:
                    # if the datatype the is a complex datatype, the z element must follow the correct
                    # structure of that datatype
                    # Component just to search in the datatypes....
                    dt_struct = load_reference(el.datatype, 'Datatypes_Structs', el.version)
                    ref = ('sequence', dt_struct, el.datatype, None, None, -1)
                    _check_known_element(el, ref, errs, warns)
            for c in el.children:
                _is_valid(c, None, errs, warns, required=False)
            return True

        def _check_repetitions(el, children, cardinality, child_name, errs, kind=None, parent_is_container=False):
            children_num = len(children)
            min_repetitions, max_repetitions = cardinality
            if max_repetitions != -1:
                if children_num < min_repetitions:
                    _missing_child_issue(el, child_name, cardinality, kind, errs, parent_is_container)
                elif children_num > max_repetitions:
                    excess = children[max_repetitions]
                    errs.append(
                        _new_issue(
                            ValidationError,
                            "Child limit exceeded {}.{}".format(el.name, child_name),
                            'CHILD_LIMIT_EXCEEDED',
                            element=excess,
                            constraint=_constraint_from_cardinality(cardinality),
                            element_name=child_name,
                            target_kind=_target_kind(excess),
                        )
                    )
            else:
                if children_num < min_repetitions:
                    _missing_child_issue(el, child_name, cardinality, kind, errs, parent_is_container)

        def _check_table_compliance(el, ref, warns):
            table = ref[4]
            if table is not None:
                try:
                    table_ref = load_reference(table, 'Table', el.version)
                except ChildNotFound:
                    pass
                else:
                    table_children = table_ref[1]
                    if el.to_er7() not in table_children:
                        warns.append(
                            _new_issue(
                                ValidationWarning,
                                "Value {} not in table {} in element {}.{}".format(
                                    el.to_er7(), table, el.parent.name, el.name
                                ),
                                'VALUE_NOT_IN_TABLE',
                                category='value',
                                element=el,
                                constraint='table {}'.format(table),
                            )
                        )

        def _check_length(el, ref, warns):
            max_length = ref[5]
            if -1 < max_length < len(el.to_er7()):
                warns.append(
                    _new_issue(
                        ValidationWarning,
                        "Exceeded max length ({}) of {}.{}".format(max_length, el.parent.name, el.name),
                        'MAX_LENGTH_EXCEEDED',
                        category='value',
                        element=el,
                        constraint='max_length {}'.format(max_length),
                    )
                )

        def _check_datatype(el, ref, errs):
            ref_datatype = ref[2]
            if el.datatype != ref_datatype:
                errs.append(
                    _new_issue(
                        ValidationError,
                        "Datatype {} is not correct for {}.{} (it must be {})".format(
                            el.datatype, el.parent.name, el.name, ref_datatype
                        ),
                        'INVALID_DATATYPE',
                        element=el,
                        constraint='datatype {}'.format(ref_datatype),
                    )
                )

        def _get_valid_children_info(ref):
            valid_children = {c[0] for c in ref[1]}
            children_refs = ref[1]
            return valid_children, children_refs

        def _get_child_reference_info(ref):
            child_name, cardinality = ref[0], ref[2]
            return child_name, cardinality

        def _check_choice_group(el, valid_children_refs, errs, warns):
            # A choice group is valid only if exactly one of its alternatives
            # is present. The occurrences of the chosen alternative are checked
            # against its own cardinality.
            chosen = []
            for child_ref in valid_children_refs:
                child_name, cardinality = _get_child_reference_info(child_ref)
                try:
                    # it gets all the occurrences of the children of a type
                    children = el.children.get(child_name)
                except Exception:
                    # TODO: it is due to the lack of element in the official reference files...  should
                    # we raise an exception here?
                    pass
                else:
                    if len(children) > 0:
                        chosen.append((child_name, cardinality, children, child_ref))

            choices = [c[0] for c in valid_children_refs]
            constraint = 'exactly one of {0}'.format(choices)
            if len(chosen) == 0:
                errs.append(
                    _new_issue(
                        ValidationError,
                        "Missing required child for choice group {} (exactly one of {} is required)".format(
                            el.name, choices
                        ),
                        'MISSING_CHOICE_ALTERNATIVE',
                        element=el,
                        group_path=_element_path(el),
                        constraint=constraint,
                        target_kind='group',
                    )
                )
            elif len(chosen) > 1:
                errs.append(
                    _new_issue(
                        ValidationError,
                        "Only one child allowed for choice group {}: found {}".format(
                            el.name, [c[0] for c in chosen]
                        ),
                        'MULTIPLE_CHOICE_ALTERNATIVES',
                        element=el,
                        group_path=_element_path(el),
                        constraint=constraint,
                        target_kind='group',
                    )
                )
            else:
                child_name, cardinality, children, child_ref = chosen[0]
                _check_repetitions(el, children, cardinality, child_name, errs, child_ref[3], True)
                # calls validation for every occurrence of the chosen child
                for c in children:
                    _is_valid(c, child_ref[1], errs, warns, required=cardinality[0] >= 1)

        def _check_known_element(el, ref, errs, warns, required=False, cardinality=None):
            if ref is None:
                try:
                    ref = load_reference(el.name, el.classname, el.version)
                except ChildNotFound:
                    errs.append(
                        _new_issue(
                            ValidationError,
                            "Invalid element found: {}".format(el),
                            'INVALID_ELEMENT',
                            element=el,
                            constraint='known element',
                        )
                    )
                    return

            if ref[0] in ('sequence', 'choice'):
                element_children = {c.name for c in el.children if not c.is_z_element()}
                valid_children, valid_children_refs = _get_valid_children_info(ref)

                # check that the children are all allowed children
                if not element_children <= valid_children:
                    for invalid_child in el.children:
                        if invalid_child.name in (element_children - valid_children):
                            errs.append(
                                _new_issue(
                                    ValidationError,
                                    "Invalid children detected for {}: {}".format(
                                        el, [invalid_child.name]
                                    ),
                                    'INVALID_CHILD',
                                    element=invalid_child,
                                    constraint='allowed children',
                                )
                            )

                if ref[0] == 'choice':
                    # a choice group requires exactly one of its alternatives,
                    # not all of them
                    _check_choice_group(el, valid_children_refs, errs, warns)
                else:
                    # iterates the valid children
                    for child_ref in valid_children_refs:
                        # it gets the structure of the children to check
                        child_name, child_cardinality = _get_child_reference_info(child_ref)
                        try:
                            # it gets all the occurrences of the children of a type
                            children = el.children.get(child_name)
                        except Exception:
                            # TODO: it is due to the lack of element in the official reference files...  should
                            # we raise an exception here?
                            pass
                        else:
                            _check_repetitions(
                                el,
                                children,
                                child_cardinality,
                                child_name,
                                errs,
                                child_ref[3],
                                _is_container(el),
                            )
                            # calls validation for every children. Occurrences
                            # beyond max are already reported once by the
                            # cardinality check; validating them again would
                            # duplicate the same source problem.
                            max_allowed = child_cardinality[1]
                            for position, c in enumerate(children, 1):
                                if max_allowed != -1 and position > max_allowed:
                                    break
                                _is_valid(
                                    c,
                                    child_ref[1],
                                    errs,
                                    warns,
                                    required=child_cardinality[0] >= 1,
                                    cardinality=child_cardinality,
                                )

                # finally calls validation for z_elements
                z_children = [c for c in el.children if c.is_z_element()]
                for c in z_children:
                    _is_valid(c, None, errs, warns, required=False)
            else:
                _check_table_compliance(el, ref, warns)

                _check_length(el, ref, warns)

                if el.datatype == 'varies':  # TODO: it should check the real rule
                    return True
                _check_datatype(el, ref, errs)

                # For complex datatypes element, the reference is the one of the datatype
                if not is_base_datatype(el.datatype, el.version) and el.datatype is not None:
                    # Component just to search in the datatypes....
                    ref = load_reference(el.datatype, 'Datatypes_Structs', el.version)
                    _is_valid(el, ref, errs, warns, required=required, cardinality=cardinality)

        def _is_valid(el, ref, errs, warns, required=False, cardinality=None):
            if el.is_unknown():
                errs.append(
                    _new_issue(
                        ValidationError,
                        "Unknown element found: {}.{}".format(el.parent, el),
                        'UNKNOWN_ELEMENT',
                        element=el,
                        constraint='known element',
                    )
                )
                return

            if el.is_z_element():
                return _check_z_element(el, errs, warns)

            # An empty group carries no information at all, including for
            # optional groups: its required children must not be reported as
            # independent problems since the group itself is the sole issue.
            if getattr(el, 'classname', None) == 'Group' and len(el.children) == 0:
                if required:
                    _empty_group_issue(
                        el,
                        cardinality,
                        errs,
                        'EMPTY_REQUIRED_GROUP',
                        "Empty required group {}".format(el.name),
                    )
                else:
                    _empty_group_issue(
                        el,
                        cardinality,
                        errs,
                        'EMPTY_OPTIONAL_GROUP',
                        "Empty optional group {}".format(el.name),
                    )
                return

            return _check_known_element(
                el, ref, errs, warns, required=required, cardinality=cardinality
            )

        errors = []
        warnings = []

        _is_valid(element, reference, errors, warnings, required=True)

        if report_file is not None:
            try:
                write = report_file.write
            except AttributeError:
                with open(report_file, "w") as f:
                    for e in errors:
                        f.write("Error: {}\n".format(e))
                    for w in warnings:
                        f.write("Warning: {}\n".format(w))
            else:
                for e in errors:
                    write("Error: {}\n".format(e))
                for w in warnings:
                    write("Warning: {}\n".format(w))

        if return_errors:
            return _ValidatedIssues(is_valid=not errors, errors=errors, warnings=warnings)

        if errors:
            raise errors[0]

        return True

    @staticmethod
    def validate_batch(
        messages,
        reference=None,
        report_file=None,
        validation_level=None,
        stop_on_first_error=False,
    ):
        """
        Validate a batch of messages without letting one invalid message stop
        the delivery of the others.

        ``messages`` may contain:

        * ER7 strings
        * :class:`Message <hl7apy.core.Message>` instances
        * ``(message_id, message)`` pairs
        * dictionaries with a ``message``/``payload``/``er7`` key and an
          optional ``id``/``message_id`` key
        * paths to files containing an ER7 message

        The returned :class:`BatchValidationResult` keeps every item and
        exposes structural and value problems separately. Missing parent
        groups are reported once; their absent children are not duplicated as
        independent source problems.

        :param messages: the batch to validate
        :param reference: an optional message profile (or mapping by structure)
        :param report_file: text report path, file-like object or ``.json`` path
        :param validation_level: validation level used when parsing ER7 strings
        :param stop_on_first_error: stop after the first invalid message
        """
        from hl7apy.parser import parse_message

        if isinstance(messages, Mapping):
            if any(key in messages for key in ('message', 'payload', 'er7', 'content', 'text')):
                messages = [messages]
            else:
                messages = list(messages.items())
        elif not isinstance(messages, (list, tuple)):
            from hl7apy.core import Element

            if isinstance(messages, (str, bytes, os.PathLike, Element)) or hasattr(messages, 'read'):
                messages = _expand_batch_source(messages)
                if not isinstance(messages, (list, tuple)):
                    messages = [messages]
        else:
            messages = _expand_batch_source(messages)

        items = []
        for index, entry in enumerate(messages, 1):
            message_id, payload = _unpack_batch_entry(entry, index)
            item = BatchValidationItem(index, None, message_id=message_id, source=payload)

            try:
                message, source_text = _coerce_batch_message(payload, validation_level)
            except Exception as exc:
                issue = ValidationError(
                    str(exc),
                    code='PARSE_ERROR',
                    category='structure',
                    path=None,
                    group_path=None,
                    occurrence=1,
                    constraint='parseable ER7 message',
                    fragment=payload if isinstance(payload, str) else None,
                    element_name=None,
                    target_kind='message',
                )
                issue.message_index = index
                issue.message_id = message_id
                item.parse_error = str(exc)
                item._result = ValidationResult(payload, [issue], [])
                items.append(item)
                if stop_on_first_error:
                    break
                continue

            item.message = message
            if message_id is None:
                message_id = _message_control_id(message)
                item.message_id = message_id
            if source_text is not None and not hasattr(message, '_hl7apy_source_text'):
                try:
                    object.__setattr__(message, '_hl7apy_source_text', source_text)
                except Exception:
                    pass

            item_reference = _reference_for_message(reference, message)
            try:
                outcome = Validator.validate(
                    message, reference=item_reference, return_errors=True
                )
            except Exception as exc:  # defensive: validation must not abort a batch
                issue = ValidationError(
                    str(exc),
                    code='VALIDATION_ERROR',
                    category='structure',
                    path=getattr(message, 'name', None),
                    group_path=getattr(message, 'name', None),
                    occurrence=1,
                    constraint=None,
                    fragment=source_text,
                    target_kind='message',
                )
                issue.message_index = index
                issue.message_id = message_id
                item._result = ValidationResult(message, [issue], [])
                items.append(item)
                if stop_on_first_error:
                    break
                continue

            for issue in outcome.errors + outcome.warnings:
                issue.message_index = index
                issue.message_id = message_id
            item._result = ValidationResult(message, outcome.errors, outcome.warnings)
            items.append(item)
            if stop_on_first_error and not item.is_valid:
                break

        result = BatchValidationResult(items)
        if report_file is not None:
            _write_batch_report(result, report_file)
        return result

    @staticmethod
    def is_strict(level):
        """
        Check if the given validation level is strict

        :type level: ``int``
        :param level: validation level (see :class:`VALIDATION_LEVEL <hl7apy.consts.VALIDATION_LEVEL>`)
        :rtype: ``bool``
        :return: ``True`` if validation level is strict
        """
        return level == VALIDATION_LEVEL.STRICT

    @staticmethod
    def is_tolerant(level):
        """
        Check if the given validation level is tolerant

        :type level: ``int``
        :param level: validation level (see :class:`VALIDATION_LEVEL <hl7apy.consts.VALIDATION_LEVEL>`)
        :rtype: ``bool``
        :return: ``True`` if validation level is tolerant
        """
        return level == VALIDATION_LEVEL.TOLERANT

    @staticmethod
    def is_quiet(level):
        """
        Equal to :func:`is_tolerant <Validator.is_tolerant>`. Kept for backward compatibility
        :param level:
        :rtype: ``bool``
        :return: ``True`` if validation level is tolerant
        """
        return Validator.is_tolerant(level)


def _expand_batch_source(messages):
    """
    Expand a directory of ER7 files into a list of ``(name, path)`` entries.
    """
    if isinstance(messages, os.PathLike):
        messages = os.fspath(messages)
    if isinstance(messages, str) and os.path.isdir(messages):
        entries = []
        for name in sorted(os.listdir(messages)):
            path = os.path.join(messages, name)
            if os.path.isfile(path):
                entries.append((name, path))
        return entries
    return messages


def _unpack_batch_entry(entry, index):
    message_id = None
    payload = entry
    if isinstance(entry, dict):
        message_id = entry.get('message_id', entry.get('id', entry.get('control_id')))
        for key in ('message', 'payload', 'er7', 'content', 'text'):
            if key in entry:
                payload = entry[key]
                break
    elif isinstance(entry, (tuple, list)) and len(entry) == 2:
        first, second = entry
        if isinstance(first, str) and not _looks_like_er7(first):
            message_id, payload = first, second
        else:
            message_id, payload = second, first
    return message_id, payload


__all__ = [
    'Validator',
    'BatchValidationItem',
    'BatchValidationResult',
    'ValidationResult',
    'validate_batch',
    'validate_batch_messages',
]


def _looks_like_er7(value):
    return '\r' in value or '\n' in value or value[:3].upper() == 'MSH'


def _coerce_batch_message(payload, validation_level):
    from hl7apy.core import Element
    from hl7apy.parser import parse_message

    if isinstance(payload, Element):
        return payload, getattr(payload, '_hl7apy_source_text', None)
    if isinstance(payload, bytes):
        payload = payload.decode('utf-8')
    if hasattr(payload, 'read'):
        payload = payload.read()
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8')
    if isinstance(payload, os.PathLike):
        payload = os.fspath(payload)
    if isinstance(payload, str) and not _looks_like_er7(payload) and os.path.isfile(payload):
        # newline='' keeps the HL7 segment separators (\r) intact.
        with open(payload, 'r', newline='') as source:
            payload = source.read()
    if not isinstance(payload, str):
        raise TypeError('Unsupported batch message type: {0}'.format(type(payload).__name__))
    message = parse_message(payload, validation_level=validation_level)
    return message, payload


def _reference_for_message(reference, message):
    if reference is None:
        return None
    if isinstance(reference, dict):
        name = getattr(message, 'name', None)
        if name in reference:
            return reference[name]
        return None
    return reference


def _message_control_id(message):
    """
    Return MSH-10 of a parsed message, used as a human friendly batch label.
    """
    try:
        return message.msh.msh_10.to_er7() or None
    except Exception:
        return None


def _write_batch_report(result, report_file):
    target = report_file
    if isinstance(target, (str, os.PathLike)) and os.fspath(target).lower().endswith('.json'):
        with open(target, 'w') as output:
            json.dump(result.to_dict(), output, indent=2, ensure_ascii=False)
        return
    text = result.report_text()
    try:
        write = report_file.write
    except AttributeError:
        with open(report_file, 'w') as output:
            output.write(text)
            output.write('\n')
    else:
        write(text)
        write('\n')


def validate_batch(messages, reference=None, report_file=None, validation_level=None, stop_on_first_error=False):
    """
    Module level shortcut for :meth:`Validator.validate_batch <hl7apy.validation.Validator.validate_batch>`.
    """
    return Validator.validate_batch(
        messages,
        reference=reference,
        report_file=report_file,
        validation_level=validation_level,
        stop_on_first_error=stop_on_first_error,
    )


def validate_batch_messages(
    messages, reference=None, report_file=None, validation_level=None, stop_on_first_error=False
):
    """
    Alias of :func:`validate_batch <hl7apy.validation.validate_batch>`.
    """
    return validate_batch(
        messages,
        reference=reference,
        report_file=report_file,
        validation_level=validation_level,
        stop_on_first_error=stop_on_first_error,
    )
