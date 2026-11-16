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
Migration of HL7 v2.x messages and composite fields across versions.

The migration is driven by a *version mapping* that describes, for a
(source version, target version) pair, how fields and datatype components
correspond. The mapping is derived from the official hl7apy references of
the two versions (see :func:`build_version_mapping`) and can be overridden
by the caller.

The migration guarantees the following properties:

* every migrated value keeps a reference to the original path and datatype
  of the component it comes from (see :class:`ComponentMigration`);
* source values that have no counterpart in the target version are not
  silently dropped: they are preserved, with their original path and
  datatype, in the ``unmapped`` lists (see :class:`UnmappedValue`);
* when a required component of the target version is missing, the field
  migration is marked as ``incomplete`` (see :attr:`FieldMigration.status`);
* values coming from the source message are never mixed up with defaults
  introduced by the target version: the latter are recorded with
  ``origin == ORIGIN_DEFAULT`` so they cannot masquerade as source
  submissions;
* every lossy or ambiguous step (dropped components, composites collapsed
  to scalars, values placed positionally into split components) is
  explicitly listed as irreversible (see :class:`IrreversibleChange`), so
  the parts of the migration that cannot be round-tripped are known.
"""

from hl7apy import check_version, load_library
from hl7apy.consts import VALIDATION_LEVEL
from hl7apy.core import Message, Segment
from hl7apy.parser import parse_field

#: the value comes from the source message
ORIGIN_SOURCE = 'source'
#: the value is a default introduced by the target version mapping
ORIGIN_DEFAULT = 'default'

#: all the required target components have been populated
STATUS_COMPLETE = 'complete'
#: at least one required target component is missing
STATUS_INCOMPLETE = 'incomplete'

#: reasons for irreversible changes
REASON_UNMAPPED_SOURCE = 'unmapped_source'
REASON_WITHDRAWN = 'withdrawn_in_target'
REASON_COLLAPSED_COMPOSITE = 'collapsed_composite'
REASON_SPLIT_COMPONENT = 'split_component'
REASON_UNKNOWN_FIELD = 'unknown_field'
REASON_UNKNOWN_SEGMENT = 'unknown_segment'


def build_version_mapping(source_version, target_version):
    """
    Build the version mapping used to migrate from ``source_version`` to
    ``target_version``.

    The mapping is derived from the official hl7apy references of the two
    versions. It contains the datatype correspondence of every field and
    the component correspondence of every (source datatype, target
    datatype) pair, matched by component long name with positional
    fallback. Callers may pass their own mapping, built directly or
    customized starting from this one, to :func:`migrate_field`,
    :func:`migrate_segment` and :func:`migrate_message`.

    :type source_version: ``str``
    :param source_version: the source HL7 version (e.g. ``2.5``)
    :type target_version: ``str``
    :param target_version: the target HL7 version (e.g. ``2.8.1``)
    :rtype: ``dict``
    :returns: the version mapping
    """
    check_version(source_version)
    check_version(target_version)
    source_lib = load_library(source_version)
    target_lib = load_library(target_version)

    fields = {}
    for name, ref in source_lib.FIELDS.items():
        target_ref = target_lib.FIELDS.get(name)
        fields[name] = {
            'source_datatype': ref[2],
            'source_kind': ref[0],
            'target_datatype': target_ref[2] if target_ref else None,
            'target_kind': target_ref[0] if target_ref else None,
        }

    return {
        'source_version': source_version,
        'target_version': target_version,
        'fields': fields,
        'datatypes': {},  # component correspondences, computed lazily
    }


def _struct(lib, datatype):
    return lib.DATATYPES_STRUCTS.get(datatype)


def _struct_by_name(struct):
    return {entry[0]: entry for entry in struct} if struct else {}


def _component_mapping(mapping, source_lib, target_lib, source_dt, target_dt):
    """
    Return the component correspondence between two composite datatypes,
    computing and caching it into the version mapping when needed.
    """
    key = (source_dt, target_dt)
    cached = mapping['datatypes'].get(key)
    if cached is not None:
        return cached

    source_struct = _struct(source_lib, source_dt)
    target_struct = _struct(target_lib, target_dt)
    components = {}
    if source_struct and target_struct:
        unmatched = list(range(len(target_struct)))
        # first pass: match by long name
        for source_index, source_entry in enumerate(source_struct):
            source_long_name = source_entry[1][3]
            for target_index in unmatched:
                if target_struct[target_index][1][3] == source_long_name:
                    components[source_entry[0]] = target_struct[target_index][0]
                    unmatched.remove(target_index)
                    break
        # second pass: positional fallback for components still unmatched
        for source_index, source_entry in enumerate(source_struct):
            if source_entry[0] in components:
                continue
            if source_index in unmatched:
                components[source_entry[0]] = target_struct[source_index][0]
                unmatched.remove(source_index)

    required = []
    if target_struct:
        required = [entry[0] for entry in target_struct if entry[2][0] >= 1]

    result = {
        'components': components,
        'required': required,
        'source_struct': source_struct,
        'target_struct': target_struct,
    }
    mapping['datatypes'][key] = result
    return result


class ComponentMigration(object):
    """
    Record of a single migrated value.

    ``origin`` is :data:`ORIGIN_SOURCE` when the value comes from the
    source message or :data:`ORIGIN_DEFAULT` when it is a default
    introduced by the target version mapping, so that defaults can never
    masquerade as source submissions.
    """

    def __init__(self, source_path, source_datatype, target_path, target_datatype, value, origin, notes=()):
        self.source_path = source_path
        self.source_datatype = source_datatype
        self.target_path = target_path
        self.target_datatype = target_datatype
        self.value = value
        self.origin = origin
        self.notes = list(notes)

    def __repr__(self):
        return '<ComponentMigration {0} ({1}) -> {2} ({3}) [{4}]>'.format(
            self.source_path, self.source_datatype, self.target_path, self.target_datatype, self.origin
        )


class UnmappedValue(object):
    """
    A source value that has no counterpart in the target version.

    The original path, datatype and value are preserved so that no source
    data is silently lost.
    """

    def __init__(self, source_path, source_datatype, value, reason):
        self.source_path = source_path
        self.source_datatype = source_datatype
        self.value = value
        self.reason = reason

    def __repr__(self):
        return '<UnmappedValue {0} ({1}) = {2!r} [{3}]>'.format(
            self.source_path, self.source_datatype, self.value, self.reason
        )


class IrreversibleChange(object):
    """
    A migration step that cannot be round-tripped back to the source
    version without loss or ambiguity.
    """

    def __init__(self, path, reason, detail):
        self.path = path
        self.reason = reason
        self.detail = detail

    def __repr__(self):
        return '<IrreversibleChange {0} [{1}] {2}>'.format(self.path, self.reason, self.detail)


class FieldMigration(object):
    """
    Result of the migration of a single field.
    """

    def __init__(self, source_name, target_name, source_datatype, target_datatype, field=None):
        self.source_name = source_name
        self.target_name = target_name
        self.source_datatype = source_datatype
        self.target_datatype = target_datatype
        #: the migrated :class:`hl7apy.core.Field` in the target version, or ``None``
        self.field = field
        #: list of :class:`ComponentMigration`
        self.entries = []
        #: list of :class:`UnmappedValue`
        self.unmapped = []
        #: list of :class:`IrreversibleChange`
        self.irreversible = []
        #: target paths of required components that are missing
        self.missing_required = []

    @property
    def status(self):
        """
        ``incomplete`` when at least one required target component is
        missing, ``complete`` otherwise.
        """
        if self.missing_required:
            return STATUS_INCOMPLETE
        return STATUS_COMPLETE

    @property
    def defaults_applied(self):
        """
        The entries whose value is a target version default
        (``origin == ORIGIN_DEFAULT``).
        """
        return [e for e in self.entries if e.origin == ORIGIN_DEFAULT]

    def __repr__(self):
        return '<FieldMigration {0} -> {1} [{2}]>'.format(self.source_name, self.target_name, self.status)


class SegmentMigration(object):
    """
    Result of the migration of a single segment.
    """

    def __init__(self, source_name, segment=None):
        self.source_name = source_name
        #: the migrated :class:`hl7apy.core.Segment` in the target version, or ``None``
        self.segment = segment
        #: list of :class:`FieldMigration`
        self.fields = []
        #: list of :class:`UnmappedValue`
        self.unmapped = []
        #: list of :class:`IrreversibleChange`
        self.irreversible = []

    @property
    def status(self):
        if any(f.status == STATUS_INCOMPLETE for f in self.fields):
            return STATUS_INCOMPLETE
        return STATUS_COMPLETE

    def __repr__(self):
        return '<SegmentMigration {0} [{1}]>'.format(self.source_name, self.status)


class MigrationReport(object):
    """
    Result of the migration of a whole message.
    """

    def __init__(self, source_version, target_version, message=None):
        self.source_version = source_version
        self.target_version = target_version
        #: the migrated :class:`hl7apy.core.Message` in the target version
        self.message = message
        #: list of :class:`SegmentMigration`
        self.segments = []

    @property
    def fields(self):
        return [f for s in self.segments for f in s.fields]

    @property
    def entries(self):
        return [e for f in self.fields for e in f.entries]

    @property
    def unmapped(self):
        values = [u for f in self.fields for u in f.unmapped]
        values.extend(u for s in self.segments for u in s.unmapped)
        return values

    @property
    def irreversible(self):
        changes = [i for f in self.fields for i in f.irreversible]
        changes.extend(i for s in self.segments for i in s.irreversible)
        return changes

    @property
    def missing_required(self):
        return [m for f in self.fields for m in f.missing_required]

    @property
    def defaults_applied(self):
        return [e for e in self.entries if e.origin == ORIGIN_DEFAULT]

    @property
    def status(self):
        if any(s.status == STATUS_INCOMPLETE for s in self.segments):
            return STATUS_INCOMPLETE
        return STATUS_COMPLETE

    def summary(self):
        """
        Return a human readable summary of the migration, including the
        list of the irreversible changes.
        """
        lines = [
            'migration from {0} to {1}: {2}'.format(self.source_version, self.target_version, self.status),
            '  migrated values: {0} (of which defaults: {1})'.format(
                len(self.entries), len(self.defaults_applied)
            ),
            '  unmapped source values: {0}'.format(len(self.unmapped)),
            '  missing required components: {0}'.format(len(self.missing_required)),
            '  irreversible changes: {0}'.format(len(self.irreversible)),
        ]
        for change in self.irreversible:
            lines.append('    {0}: {1} ({2})'.format(change.path, change.reason, change.detail))
        return '\n'.join(lines)

    def __repr__(self):
        return '<MigrationReport {0} -> {1} [{2}]>'.format(
            self.source_version, self.target_version, self.status
        )


def _leaf_values(element):
    """
    Return the list of (name, datatype, er7 value) of the valued leaves
    below a field or a component.

    When called on a field, leaf components are reported with the
    component name (e.g. ``TS_1``) and composite components are expanded
    to their subcomponents (e.g. ``HD_1``). When called on a component,
    its valued subcomponents are reported.
    """
    parts = []
    for child in element.children:
        subs = list(child.children)
        if len(subs) == 1 and subs[0].name == subs[0].datatype:
            # a leaf component: its only child is the unnamed base
            # subcomponent holding the value
            value = child.to_er7()
            if value != '':
                parts.append((child.name, child.datatype, value))
        elif subs:
            for sub in subs:
                value = sub.to_er7()
                if value != '':
                    parts.append((sub.name, sub.datatype, value))
        else:
            value = child.to_er7()
            if value != '':
                parts.append((child.name, child.datatype, value))
    return parts


class _Migrator(object):
    def __init__(self, mapping, defaults=None):
        self.mapping = mapping
        self.source_lib = load_library(mapping['source_version'])
        self.target_lib = load_library(mapping['target_version'])
        self.target_version = mapping['target_version']
        self.defaults = defaults or {}

    def field_defaults(self, field_name):
        defaults = {}
        defaults.update(self.defaults.get('*', {}))
        defaults.update(self.defaults.get(field_name, {}))
        return defaults

    def migrate_field(self, field, segment_path=None):
        name = field.name
        field_map = self.mapping['fields'].get(name, {})
        target_dt = field_map.get('target_datatype')
        target_kind = field_map.get('target_kind')
        source_dt = field.datatype or field_map.get('source_datatype')
        base_path = '{0}/{1}'.format(segment_path, name) if segment_path else name

        fm = FieldMigration(name, name if target_dt else None, source_dt, target_dt)

        if target_dt is None:
            # the field does not exist in the target version
            value = field.to_er7()
            if value:
                fm.unmapped.append(UnmappedValue(base_path, source_dt, value, REASON_UNKNOWN_FIELD))
                fm.irreversible.append(
                    IrreversibleChange(base_path, REASON_UNKNOWN_FIELD,
                                       'field {0} does not exist in version {1}'.format(name, self.target_version))
                )
            return fm

        if target_dt == 'WD':
            # the field has been withdrawn in the target version
            self._unmap_all(field, base_path, source_dt, fm, REASON_WITHDRAWN,
                            'field {0} is withdrawn in version {1}'.format(name, self.target_version))
            return fm

        if target_kind == 'leaf':
            self._migrate_to_scalar(field, name, base_path, source_dt, target_dt, fm)
        else:
            self._migrate_to_composite(field, name, base_path, source_dt, target_dt, fm)

        self._apply_defaults(name, base_path, fm)
        self._check_required(name, base_path, target_dt, fm)
        fm.field = self._build_field(name, fm)
        return fm

    def _unmap_all(self, field, base_path, source_dt, fm, reason, detail):
        parts = []
        for component in field.children:
            comp_name = component.name
            for part_name, part_dt, value in _leaf_values(component):
                if part_name in (None, part_dt):
                    # unnamed leaf placeholder: the path ends at the component
                    path = '{0}/{1}'.format(base_path, comp_name) if comp_name else base_path
                elif comp_name:
                    path = '{0}/{1}/{2}'.format(base_path, comp_name, part_name)
                else:
                    path = '{0}/{1}'.format(base_path, part_name)
                parts.append((path, part_dt, value))
        if not parts:
            value = field.to_er7()
            if value:
                parts = [(base_path, source_dt, value)]
        for path, part_dt, value in parts:
            fm.unmapped.append(UnmappedValue(path, part_dt or source_dt, value, reason))
        if parts:
            fm.irreversible.append(IrreversibleChange(base_path, reason, detail))

    def _migrate_to_scalar(self, field, name, base_path, source_dt, target_dt, fm):
        parts = _leaf_values(field)
        if not parts:
            return
        part_name, part_dt, value = parts[0]
        source_path = base_path if len(parts) == 1 and part_name in (None, part_dt) else \
            '{0}/{1}'.format(base_path, part_name)
        notes = []
        if source_dt != target_dt:
            notes.append('datatype changed from {0} to {1}'.format(source_dt, target_dt))
        fm.entries.append(
            ComponentMigration(source_path, part_dt or source_dt, base_path, target_dt, value, ORIGIN_SOURCE, notes)
        )
        if len(parts) > 1:
            fm.irreversible.append(
                IrreversibleChange(base_path, REASON_COLLAPSED_COMPOSITE,
                                   'composite {0} collapsed to scalar {1}'.format(source_dt, target_dt))
            )
            for part_name, part_dt, value in parts[1:]:
                fm.unmapped.append(
                    UnmappedValue('{0}/{1}'.format(base_path, part_name), part_dt, value, REASON_COLLAPSED_COMPOSITE)
                )

    def _migrate_to_composite(self, field, name, base_path, source_dt, target_dt, fm):
        dt_map = _component_mapping(self.mapping, self.source_lib, self.target_lib, source_dt, target_dt)
        target_struct = dt_map['target_struct']
        target_by_name = _struct_by_name(target_struct)
        source_struct = dt_map['source_struct']
        source_by_name = _struct_by_name(source_struct)
        source_by_index = {i: e for i, e in enumerate(source_struct)} if source_struct else {}

        fm._target_values = {}

        if source_struct is None:
            # a scalar source field is migrated into a composite target
            # field (e.g. IS -> CWE)
            parts = _leaf_values(field)
            if not parts:
                return
            source_ref = self.source_lib.FIELDS.get(name)
            long_name = source_ref[3] if source_ref else None
            target_entry = None
            for entry in target_struct:
                if entry[1][3] == long_name:
                    target_entry = entry
                    break
            if target_entry is None:
                target_entry = target_struct[0]
                fm.irreversible.append(
                    IrreversibleChange(
                        base_path, REASON_SPLIT_COMPONENT,
                        'scalar {0} field placed positionally into {1}/{2}'.format(
                            source_dt, base_path, target_entry[0])
                    )
                )
            target_index = target_struct.index(target_entry)
            target_ref = target_entry[1]
            target_path = '{0}/{1}'.format(base_path, target_entry[0])
            if target_ref[0] == 'leaf':
                fm.entries.append(
                    ComponentMigration(base_path, source_dt, target_path, target_ref[2],
                                       parts[0][2], ORIGIN_SOURCE)
                )
                fm._target_values[target_index] = [parts[0][2]]
            else:
                sub_struct = _struct(self.target_lib, target_ref[2])
                sub_entry = None
                for entry in sub_struct:
                    if entry[1][3] == long_name:
                        sub_entry = entry
                        break
                if sub_entry is None:
                    sub_entry = sub_struct[0]
                fm.entries.append(
                    ComponentMigration(base_path, source_dt,
                                       '{0}/{1}'.format(target_path, sub_entry[0]), sub_entry[1][2],
                                       parts[0][2], ORIGIN_SOURCE)
                )
                fm._target_values[target_index] = {sub_entry[0]: parts[0][2]}
            for part_name, part_dt, value in parts[1:]:
                fm.unmapped.append(
                    UnmappedValue('{0}/{1}'.format(base_path, part_name), part_dt, value,
                                  REASON_UNMAPPED_SOURCE)
                )
            return

        components = list(field.children)
        for index, component in enumerate(components):
            # the position in the children list does not necessarily match
            # the position in the structure, since empty components are
            # not instantiated: look the entry up by name first
            source_entry = source_by_name.get(component.name) or source_by_index.get(index)
            source_comp_name = component.name or (source_entry[0] if source_entry else None)
            source_comp_ref = source_entry[1] if source_entry else None
            parts = _leaf_values(component)
            if not parts:
                continue
            source_comp_path = '{0}/{1}'.format(base_path, source_comp_name or index + 1)

            target_comp_name = dt_map['components'].get(source_comp_name)
            if target_comp_name is None and source_entry is None:
                # component beyond the source structure (tolerant parsing)
                fm.irreversible.append(
                    IrreversibleChange(source_comp_path, REASON_UNMAPPED_SOURCE,
                                       'component not described by the {0} structure'.format(source_dt))
                )
                for part_name, part_dt, value in parts:
                    fm.unmapped.append(
                        UnmappedValue('{0}/{1}'.format(source_comp_path, part_name), part_dt, value,
                                      REASON_UNMAPPED_SOURCE)
                    )
                continue
            if target_comp_name is None:
                for part_name, part_dt, value in parts:
                    fm.unmapped.append(
                        UnmappedValue('{0}/{1}'.format(source_comp_path, part_name), part_dt, value,
                                      REASON_UNMAPPED_SOURCE)
                    )
                fm.irreversible.append(
                    IrreversibleChange(source_comp_path, REASON_UNMAPPED_SOURCE,
                                       'no corresponding component in target {0}'.format(target_dt))
                )
                continue

            target_entry = target_by_name[target_comp_name]
            target_comp_ref = target_entry[1]
            target_comp_path = '{0}/{1}'.format(base_path, target_comp_name)
            target_index = target_struct.index(target_entry)

            if target_comp_ref[0] == 'leaf':
                self._place_in_leaf(parts, source_comp_ref, source_comp_path,
                                    target_comp_ref, target_comp_path, target_index, fm)
            else:
                self._place_in_composite(parts, source_comp_ref, source_comp_path,
                                         target_comp_ref, target_comp_path, target_index, fm)

    def _place_in_leaf(self, parts, source_ref, source_path, target_ref, target_path, target_index, fm):
        target_dt = target_ref[2]
        part_name, part_dt, value = parts[0]
        if len(parts) > 1:
            # a composite source component collapses into a leaf target component
            match = [p for p in parts if p[1] == target_dt]
            if match:
                part_name, part_dt, value = match[0]
            fm.irreversible.append(
                IrreversibleChange(source_path, REASON_COLLAPSED_COMPOSITE,
                                   'composite component collapsed to {0}'.format(target_path))
            )
            for other_name, other_dt, other_value in parts:
                if (other_name, other_dt, other_value) != (part_name, part_dt, value):
                    fm.unmapped.append(
                        UnmappedValue('{0}/{1}'.format(source_path, other_name), other_dt, other_value,
                                      REASON_COLLAPSED_COMPOSITE)
                    )
        notes = []
        source_dt = source_ref[2] if source_ref else part_dt
        if source_dt != target_dt:
            notes.append('datatype changed from {0} to {1}'.format(source_dt, target_dt))
        fm.entries.append(
            ComponentMigration(source_path, source_dt, target_path, target_dt, value, ORIGIN_SOURCE, notes)
        )
        fm._target_values.setdefault(target_index, []).append(value)

    def _place_in_composite(self, parts, source_ref, source_path, target_ref, target_path, target_index, fm):
        target_sub_struct = _struct(self.target_lib, target_ref[2])
        target_sub_by_name = _struct_by_name(target_sub_struct)
        source_composite = source_ref is not None and source_ref[0] == 'sequence'
        source_sub_struct = _struct(self.source_lib, source_ref[2]) if source_composite else None
        source_sub_by_name = _struct_by_name(source_sub_struct)
        values = fm._target_values.setdefault(target_index, {})

        if not source_composite or len(parts) == 1 and parts[0][0] in (None, parts[0][1]):
            # a leaf source component is placed into a component that the
            # target version has split into subcomponents
            part_name, part_dt, value = parts[0]
            target_sub_name = None
            source_long_name = source_ref[3] if source_ref else None
            for entry in target_sub_struct:
                if entry[1][3] == source_long_name:
                    target_sub_name = entry[0]
                    break
            if target_sub_name is None:
                target_sub_name = target_sub_struct[0][0]
                fm.irreversible.append(
                    IrreversibleChange(
                        source_path, REASON_SPLIT_COMPONENT,
                        'component split in target version; value placed positionally into {0}/{1}'.format(
                            target_path, target_sub_name)
                    )
                )
            target_sub_ref = target_sub_by_name[target_sub_name][1]
            fm.entries.append(
                ComponentMigration(source_path, source_ref[2] if source_ref else part_dt,
                                   '{0}/{1}'.format(target_path, target_sub_name), target_sub_ref[2],
                                   value, ORIGIN_SOURCE)
            )
            values[target_sub_name] = value
            for other_name, other_dt, other_value in parts[1:]:
                fm.unmapped.append(
                    UnmappedValue('{0}/{1}'.format(source_path, other_name), other_dt, other_value,
                                  REASON_UNMAPPED_SOURCE)
                )
            return

        # both source and target components are composite: match the
        # subcomponents by long name with positional fallback
        target_sub_list = list(target_sub_struct)
        used = set()
        for position, (part_name, part_dt, value) in enumerate(parts):
            source_sub_entry = source_sub_by_name.get(part_name)
            source_long_name = source_sub_entry[1][3] if source_sub_entry else None
            target_sub_name = None
            for entry in target_sub_list:
                if entry[0] in used:
                    continue
                if source_long_name is not None and entry[1][3] == source_long_name:
                    target_sub_name = entry[0]
                    break
            if target_sub_name is None and position < len(target_sub_list):
                candidate = target_sub_list[position][0]
                if candidate not in used:
                    target_sub_name = candidate
            source_sub_path = '{0}/{1}'.format(source_path, part_name)
            if target_sub_name is None:
                fm.unmapped.append(UnmappedValue(source_sub_path, part_dt, value, REASON_UNMAPPED_SOURCE))
                fm.irreversible.append(
                    IrreversibleChange(source_sub_path, REASON_UNMAPPED_SOURCE,
                                       'no corresponding subcomponent in target {0}'.format(target_ref[2]))
                )
                continue
            used.add(target_sub_name)
            target_sub_ref = target_sub_by_name[target_sub_name][1]
            notes = []
            if part_dt != target_sub_ref[2]:
                notes.append('datatype changed from {0} to {1}'.format(part_dt, target_sub_ref[2]))
            fm.entries.append(
                ComponentMigration(source_sub_path, part_dt, '{0}/{1}'.format(target_path, target_sub_name),
                                   target_sub_ref[2], value, ORIGIN_SOURCE, notes)
            )
            values[target_sub_name] = value

    def _apply_defaults(self, name, base_path, fm):
        defaults = self.field_defaults(name)
        if not defaults or not hasattr(fm, '_target_values'):
            return
        target_struct = _struct(self.target_lib, fm.target_datatype)
        if not target_struct:
            return
        target_by_name = _struct_by_name(target_struct)
        target_index = {e[0]: i for i, e in enumerate(target_struct)}
        for component_name, value in defaults.items():
            entry = target_by_name.get(component_name)
            if entry is None or entry[1][0] != 'leaf':
                continue
            index = target_index[component_name]
            if fm._target_values.get(index):
                continue
            target_path = '{0}/{1}'.format(base_path, component_name)
            fm.entries.append(
                ComponentMigration(None, None, target_path, entry[1][2], value, ORIGIN_DEFAULT,
                                   ['default value introduced by the target version mapping'])
            )
            fm._target_values[index] = [value]

    def _check_required(self, name, base_path, target_dt, fm):
        target_struct = _struct(self.target_lib, target_dt)
        if not target_struct:
            return
        values = getattr(fm, '_target_values', {})
        target_index = {e[0]: i for i, e in enumerate(target_struct)}
        for entry in target_struct:
            comp_name, comp_ref, (min_occ, _), _ = entry
            if min_occ < 1:
                continue
            if not values.get(target_index[comp_name]):
                fm.missing_required.append('{0}/{1}'.format(base_path, comp_name))

    def _build_field(self, name, fm):
        values = getattr(fm, '_target_values', None)
        if values is None:
            # scalar target: the value is in the first entry
            if not fm.entries:
                return None
            text = fm.entries[0].value
        else:
            if not values:
                return None
            target_struct = _struct(self.target_lib, fm.target_datatype) or ()
            components = []
            for index in range(max(values) + 1):
                value = values.get(index)
                if value is None:
                    components.append('')
                elif isinstance(value, dict):
                    sub_struct = _struct(self.target_lib, target_struct[index][1][2]) if target_struct else None
                    if sub_struct:
                        ordered = [value[e[0]] for e in sub_struct if e[0] in value]
                    else:
                        ordered = list(value.values())
                    components.append('&'.join(ordered))
                else:
                    components.append('&'.join(value))
            text = '^'.join(components)
        return parse_field(text, name=name, version=self.target_version,
                           validation_level=VALIDATION_LEVEL.TOLERANT)

    def migrate_segment(self, segment):
        name = segment.name
        sm = SegmentMigration(name)
        try:
            target_segment = Segment(name, version=self.target_version,
                                     validation_level=VALIDATION_LEVEL.TOLERANT)
        except Exception:
            value = segment.to_er7()
            if value:
                sm.unmapped.append(UnmappedValue(name, None, value, REASON_UNKNOWN_SEGMENT))
                sm.irreversible.append(
                    IrreversibleChange(name, REASON_UNKNOWN_SEGMENT,
                                       'segment {0} does not exist in version {1}'.format(name, self.target_version))
                )
            return sm

        for field in list(segment.children):
            field_name = field.name
            if field_name is None:
                continue
            if field_name in ('MSH_1', 'MSH_2'):
                # the field separator and the encoding characters are
                # carried over with their raw value: they must not go
                # through escape translation
                fm = FieldMigration(field_name, field_name, 'ST', 'ST')
                raw = field.to_er7()
                path = '{0}/{1}'.format(name, field_name)
                fm.entries.append(ComponentMigration(path, 'ST', path, 'ST', raw, ORIGIN_SOURCE))
                fm.field = parse_field(raw, name=field_name, version=self.target_version,
                                       validation_level=VALIDATION_LEVEL.TOLERANT)
            else:
                fm = self.migrate_field(field, segment_path=name)
                if field_name == 'MSH_12':
                    # the version id must describe the target version: it is a
                    # default of the migration, not a source submission
                    fm = self._migrate_version_id(field, name, fm)
            sm.fields.append(fm)
            if fm.field is not None:
                try:
                    target_segment.add(fm.field)
                except Exception:
                    sm.unmapped.append(
                        UnmappedValue('{0}/{1}'.format(name, field_name), fm.source_datatype,
                                      field.to_er7(), REASON_UNMAPPED_SOURCE)
                    )
        if len(target_segment.children):
            sm.segment = target_segment
        return sm

    def _migrate_version_id(self, field, segment_name, fm):
        base_path = '{0}/{1}'.format(segment_name, field.name)
        source_value = field.to_er7()
        fm.entries = [e for e in fm.entries
                      if e.target_path != base_path and not e.target_path.startswith(base_path + '/')]
        fm.entries.append(
            ComponentMigration(base_path, field.datatype, base_path, fm.target_datatype,
                               self.target_version, ORIGIN_DEFAULT,
                               ['version id set to the target version; source value was {0!r}'.format(source_value)])
        )
        fm.field = parse_field(self.target_version, name='MSH_12', version=self.target_version,
                               validation_level=VALIDATION_LEVEL.TOLERANT)
        return fm


def _get_mapping(source_version, target_version, mapping):
    if mapping is not None:
        return mapping
    return build_version_mapping(source_version, target_version)


def migrate_field(field, target_version, mapping=None, defaults=None):
    """
    Migrate a single :class:`hl7apy.core.Field` to ``target_version``
    according to the version mapping.

    :type field: :class:`hl7apy.core.Field`
    :param field: the field to migrate
    :type target_version: ``str``
    :param target_version: the target HL7 version (e.g. ``2.8.1``)
    :type mapping: ``dict``
    :param mapping: a version mapping (see :func:`build_version_mapping`);
        when ``None`` it is built from the field version and
        ``target_version``
    :type defaults: ``dict``
    :param defaults: default values for target components, keyed by field
        name (or ``'*'`` for every field) and then by target component
        name, e.g. ``{'PID_5': {'XPN_7': 'L'}}``. Defaults are recorded
        with ``origin == ORIGIN_DEFAULT``.
    :rtype: :class:`FieldMigration`
    """
    mapping = _get_mapping(field.version, target_version, mapping)
    return _Migrator(mapping, defaults).migrate_field(field)


def migrate_segment(segment, target_version, mapping=None, defaults=None):
    """
    Migrate a single :class:`hl7apy.core.Segment` to ``target_version``
    according to the version mapping.

    :rtype: :class:`SegmentMigration`
    """
    mapping = _get_mapping(segment.version, target_version, mapping)
    return _Migrator(mapping, defaults).migrate_segment(segment)


def migrate_message(message, target_version, mapping=None, defaults=None):
    """
    Migrate a whole :class:`hl7apy.core.Message` to ``target_version``
    according to the version mapping.

    The returned :class:`MigrationReport` holds the migrated message
    together with the per-component provenance (original path and
    datatype), the unmapped source values, the missing required components
    and the list of the irreversible changes.

    :type message: :class:`hl7apy.core.Message`
    :param message: the message to migrate
    :type target_version: ``str``
    :param target_version: the target HL7 version (e.g. ``2.8.1``)
    :type mapping: ``dict``
    :param mapping: a version mapping (see :func:`build_version_mapping`)
    :type defaults: ``dict``
    :param defaults: see :func:`migrate_field`
    :rtype: :class:`MigrationReport`
    """
    mapping = _get_mapping(message.version, target_version, mapping)
    migrator = _Migrator(mapping, defaults)
    report = MigrationReport(mapping['source_version'], mapping['target_version'])

    try:
        target_message = Message(message.name, version=mapping['target_version'],
                                 validation_level=VALIDATION_LEVEL.TOLERANT)
    except Exception:
        target_message = Message(version=mapping['target_version'],
                                 validation_level=VALIDATION_LEVEL.TOLERANT)

    for segment in _iter_segments(message):
        sm = migrator.migrate_segment(segment)
        report.segments.append(sm)
        if sm.segment is not None:
            if sm.segment.name == 'MSH':
                # replace the MSH segment automatically created together
                # with the target message
                try:
                    target_message.children.remove_by_name('MSH')
                except Exception:
                    pass
            target_message.add(sm.segment)

    report.message = target_message
    return report


def _iter_segments(element):
    for child in element.children:
        if child.classname == 'Segment':
            yield child
        elif child.classname == 'Group':
            for segment in _iter_segments(child):
                yield segment
