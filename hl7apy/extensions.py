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
Institution-specific extension (Z-segment) definitions.

Two institutions may use the same Z-segment name (e.g. ``ZZZ``) with completely
different field meanings.  Definitions are therefore keyed by both the
institution identifier and the configuration version, and they only describe
Z-elements: the standard HL7 structures are never touched.

Every parsing job gets its own :class:`ExtensionContext`, which resolves the
definitions registered for its ``(institution, config_version)`` pair and keeps
its own reference cache.  A definition loaded while handling institution A is
therefore never applied to a message coming from institution B, even when the
two messages contain segments with the same name.  If no definition is found,
``None`` is returned and the caller falls back to the generic Z-segment
handling, preserving the original structure.
"""

import re

from hl7apy import load_library

# (institution, config_version) -> {segment_name: {'fields': (spec, ...)}}
_REGISTRY = {}

_Z_SEGMENT_RE = re.compile(r'^Z[A-Z0-9]{2}$')
_FIELD_CLASS = 'FIE'


def _valid_z_segment_name(name):
    return isinstance(name, str) and _Z_SEGMENT_RE.match(name.upper()) is not None


def _validate_institution(institution):
    if not isinstance(institution, str) or not institution.strip():
        raise ValueError('institution must be a non-empty string')
    return institution.strip()


def _validate_config_version(config_version):
    if not isinstance(config_version, str) or not config_version.strip():
        raise ValueError('config_version must be a non-empty string')
    return config_version.strip()


def _validate_cardinality(cardinality):
    if cardinality is None:
        return (0, 1)
    if (
        not isinstance(cardinality, (tuple, list))
        or len(cardinality) != 2
        or not all(isinstance(v, int) for v in cardinality)
    ):
        raise ValueError('cardinality must be a (min, max) pair of integers')
    minimum, maximum = cardinality
    if minimum < 0 or (maximum != -1 and maximum < minimum):
        raise ValueError('invalid cardinality {0}'.format(cardinality))
    return minimum, maximum


def _normalize_field_spec(segment_name, index, spec):
    """
    Return a normalized field spec:
    ``(field_name, datatype, long_name, table, cardinality)``
    """
    field_name = '{0}_{1}'.format(segment_name, index + 1)
    long_name = None
    table = None
    cardinality = (0, 1)

    if isinstance(spec, str):
        datatype = spec
    elif isinstance(spec, (tuple, list)):
        if not spec or not isinstance(spec[0], str):
            raise ValueError('field {0}: a datatype is required'.format(field_name))
        datatype = spec[0]
        if len(spec) > 1 and spec[1] is not None:
            long_name = spec[1]
        if len(spec) > 2 and spec[2] is not None:
            cardinality = _validate_cardinality(spec[2])
    elif isinstance(spec, dict):
        datatype = spec.get('datatype')
        if not isinstance(datatype, str):
            raise ValueError('field {0}: a datatype is required'.format(field_name))
        long_name = spec.get('long_name')
        table = spec.get('table')
        cardinality = _validate_cardinality(spec.get('cardinality'))
        custom_name = spec.get('name')
        if custom_name is not None:
            if not isinstance(custom_name, str) or not custom_name.upper().startswith(segment_name):
                raise ValueError('field {0}: invalid custom name {1}'.format(field_name, custom_name))
            field_name = custom_name.upper()
    else:
        raise ValueError('field {0}: invalid specification {1!r}'.format(field_name, spec))

    return field_name, datatype, long_name, table, cardinality


def register_z_segment(institution, config_version, segment_name, fields, replace=False):
    """
    Register the definition of a Z-segment for the given institution and
    configuration version.

    :type institution: ``str``
    :param institution: identifier of the institution owning the definition

    :type config_version: ``str``
    :param config_version: version of the institution's configuration

    :type segment_name: ``str``
    :param segment_name: the Z-segment name (e.g. ``"ZZZ"``)

    :type fields: ``list``
    :param fields: the field definitions, in positional order. Each item can be:

        * a datatype name (``"ST"``, ``"CWE"``, ...);
        * a ``(datatype, long_name)`` or
          ``(datatype, long_name, cardinality)`` tuple;
        * a ``dict`` with keys ``datatype`` (required), ``long_name``,
          ``table``, ``cardinality`` and optional ``name``.

    :type replace: ``bool``
    :param replace: if ``True`` overwrite an already registered definition,
        otherwise a :exc:`ValueError` is raised
    """
    institution = _validate_institution(institution)
    config_version = _validate_config_version(config_version)
    if not _valid_z_segment_name(segment_name):
        raise ValueError('{0!r} is not a valid Z-segment name'.format(segment_name))
    if not fields:
        raise ValueError('at least one field definition is required')

    segment_name = segment_name.upper()
    normalized = tuple(_normalize_field_spec(segment_name, index, spec) for index, spec in enumerate(fields))

    key = (institution, config_version)
    definitions = _REGISTRY.setdefault(key, {})
    if not replace and segment_name in definitions:
        raise ValueError(
            'segment {0} is already registered for {1} {2}'.format(segment_name, institution, config_version)
        )
    definitions[segment_name] = {'fields': normalized}


def register_z_segments(institution, config_version, segment_definitions, replace=False):
    """
    Register several Z-segment definitions at once.

    :type segment_definitions: ``dict``
    :param segment_definitions: a mapping ``segment_name -> fields`` as accepted
        by :func:`register_z_segment`
    """
    for segment_name, fields in segment_definitions.items():
        register_z_segment(institution, config_version, segment_name, fields, replace=replace)


def unregister_z_segment(institution, config_version, segment_name):
    """
    Remove a previously registered Z-segment definition.
    """
    key = (
        _validate_institution(institution),
        _validate_config_version(config_version),
    )
    try:
        del _REGISTRY[key][segment_name.upper()]
    except KeyError:
        pass
    else:
        if not _REGISTRY[key]:
            del _REGISTRY[key]


def clear_z_segment_registry():
    """
    Remove every registered Z-segment definition. Mainly useful for testing.
    """
    _REGISTRY.clear()


def get_registered_z_segments(institution, config_version):
    """
    Return the names of the Z-segments registered for the given institution
    and configuration version.
    """
    key = (
        _validate_institution(institution),
        _validate_config_version(config_version),
    )
    return tuple(sorted(_REGISTRY.get(key, {})))


def _build_field_reference(spec, library):
    _field_name, datatype, long_name, table, _cardinality = spec
    if datatype == 'varies' or library.is_base_datatype(datatype):
        return ('leaf', None, datatype, long_name, table, -1)
    # raises ChildNotFound if the datatype is unavailable in the target HL7 version
    datatype_structure = library.get(datatype, 'Datatypes_Structs')
    return ('sequence', datatype_structure, datatype, long_name, table, -1)


class ExtensionContext:
    """
    Holds the ``(institution, config_version)`` pair used while parsing or
    building a message.

    Each context owns its reference cache, so contexts created for different
    institutions never share resolved definitions.
    """

    def __init__(self, institution, config_version):
        self.institution = _validate_institution(institution)
        self.config_version = _validate_config_version(config_version)
        self._reference_cache = {}

    @property
    def _definitions(self):
        return _REGISTRY.get((self.institution, self.config_version), {})

    def has_segment(self, segment_name):
        return segment_name.upper() in self._definitions

    def get_segment_reference(self, segment_name, hl7_version):
        """
        Return the reference structure for the given Z-segment, resolved
        against the specified HL7 version, or ``None`` if no definition has
        been registered for this context's institution and configuration
        version.
        """
        if not _valid_z_segment_name(segment_name):
            return None

        segment_name = segment_name.upper()
        definition = self._definitions.get(segment_name)
        if definition is None:
            return None

        cache_key = (segment_name, hl7_version)
        try:
            return self._reference_cache[cache_key]
        except KeyError:
            pass

        library = load_library(hl7_version)
        children = tuple(
            (
                spec[0],
                _build_field_reference(spec, library),
                spec[4],
                _FIELD_CLASS,
            )
            for spec in definition['fields']
        )
        reference = ('sequence', children)
        self._reference_cache[cache_key] = reference
        return reference

    def __repr__(self):
        return 'ExtensionContext(institution={0!r}, config_version={1!r})'.format(self.institution, self.config_version)


def get_extension_reference(element):
    """
    Resolve the extension reference for an element, using its
    :class:`ExtensionContext`. Only Z-segments are considered; ``None`` is
    returned in every other case so the standard lookup is used.
    """
    context = getattr(element, 'extension_context', None)
    if context is None or not isinstance(context, ExtensionContext):
        return None
    if not _valid_z_segment_name(getattr(element, 'name', None)):
        return None
    return context.get_segment_reference(element.name, element.version)
