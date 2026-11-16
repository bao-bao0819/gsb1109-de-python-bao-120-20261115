auxiliary - common functions and objects
========================================

Math
----

  :py:func:`linear_regression_vertical` - a wrapper for NumPy linear regression,
  minimizes the sum of squares of *y* errors.

  :py:func:`linear_regression` - alias for :py:func:`linear_regression_vertical`.

  :py:func:`linear_regression_perpendicular` - a wrapper for NumPy linear regression,
  minimizes the sum of squares of (perpendicular) distances between the points and the line.


Target-Decoy Approach
---------------------

  :py:func:`qvalues` - estimate q-values for a set of PSMs.

  :py:func:`!filter` - filter PSMs to specified FDR level using TDA or given PEPs.

  :py:func:`filter.chain` - a chained version of :py:func:`!filter`.

  :py:func:`fdr` - estimate FDR in a set of PSMs using TDA or given PEPs.

Project infrastructure
----------------------

  :py:class:`PyteomicsError` - a pyteomics-specific exception.

Helpers
-------

  :py:class:`Charge` - a subclass of :py:class:`int` for charge states.

  :py:class:`ChargeList` - a subclass of :py:class:`list` for lists of charges.

  :py:func:`print_tree` - display the structure of a complex nested
  :py:class:`dict`.

  :py:func:`memoize` - makes a
  `memoization <http://stackoverflow.com/a/1988826/1258041>`_
  `function decorator <http://stackoverflow.com/a/1594484/1258041>`_.

  :py:func:`cvquery` - traverse an arbitrarily nested dictionary looking
  for keys which are :py:class:`cvstr` instances, or objects
  with an attribute called ``accession``.

  :py:func:`set_start_method` - set the method of starting multiprocessing workers.

  :py:func:`convert_time_unit` - convert an annotated time quantity
  (:class:`unitfloat`) between exact units using its declared unit only.

  :py:func:`to_minutes` - convert an annotated time quantity to minutes.

  :py:func:`retention_time_report` - produce a per-scan retention time table
  with one exact unit, retaining the original declaration and scan order.

  :py:class:`TimeUnitError` - raised when a time unit is missing or unknown
  instead of guessing it from the value.

-------------------------------------------------------------------------------


.. automodule :: pyteomics.auxiliary.math

.. automodule :: pyteomics.auxiliary.target_decoy

.. automodule :: pyteomics.auxiliary.utils

.. automodule :: pyteomics.auxiliary.structures
  :exclude-members: unitint, unitfloat, unitstr, cvstr, CVQueryEngine

.. automodule :: pyteomics.auxiliary.file_helpers