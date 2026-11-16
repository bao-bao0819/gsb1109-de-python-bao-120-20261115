
Validation module
=================

.. automodule:: hl7apy.validation

.. autoclass:: Validator
   :members:

Batch validation
----------------

``hl7apy.validation.validate_batch`` validates many messages at once. Invalid
messages do not stop the delivery of the others.

.. autofunction:: hl7apy.validation.validate_batch

.. autoclass:: hl7apy.validation.BatchValidationResult
   :members: is_valid, valid_items, invalid_items, errors, warnings, structure_errors, value_errors, summary, to_dict, report_text

.. autoclass:: hl7apy.validation.BatchValidationItem
   :members: is_valid, errors, warnings, structure_errors, value_errors, to_dict

Locatable issues
----------------

Every :class:`ValidationError <hl7apy.exceptions.ValidationError>` and
:class:`ValidationWarning <hl7apy.exceptions.ValidationWarning>` carries
``category`` (``structure`` or ``value``), ``path``, ``group_path``,
``occurrence``, ``constraint`` and ``fragment``. ``fragment`` is the original
ER7 text of the segment (or of the enclosing group when a required segment is
missing), not a re-encoded copy.

A missing required group is reported once, as ``MISSING_REQUIRED_CHILD`` on the
group path. Its absent children are not reported as independent problems. A
required group that is present but empty is reported once as
``EMPTY_REQUIRED_GROUP``.
