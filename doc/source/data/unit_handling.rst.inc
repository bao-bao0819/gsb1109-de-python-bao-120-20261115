Unit Handling
=============

When parsing parsing a data file with unit information associated with a scalar value,
:mod:`pyteomics` uses annotated data types to carry units around, with the specific type
determined by parsing the value.

For instance, given the XML string:

.. code:: xml

    <cvParam cvRef="MS" accession="MS:1000927" name="ion injection time" value="68.227485656738"
             unitCvRef="UO" unitAccession="UO:0000028" unitName="millisecond"/>

This will be parsed into a :class:`~.unitfloat`:

.. code:: python

    >>> value = unitfloat(68.227485656738, "millisecond")
    # Get the unit name, perhaps to do a conversion to another unit
    >>> value.unit_info
    "millisecond"
    # Can be coerced into a plain float without issue
    >>> float(value)
    68.227485656738
    # Can be used identically to a normal float
    >>> value > 50.0 and value < 90.0
    True

To normalize the time unit, use :func:`~pyteomics.auxiliary.convert_time_unit`
(or the :func:`~pyteomics.auxiliary.to_minutes` convenience wrapper):

.. code:: python

    from pyteomics.auxiliary import unitfloat, to_minutes, convert_time_unit

    >>> seconds = unitfloat(93.5, "second")
    >>> minutes = to_minutes(seconds)
    >>> minutes
    1.5583333333333333
    >>> minutes.unit_info
    'minute'
    >>> convert_time_unit(minutes, "second").unit_info
    'second'

The converter only ever trusts the *declared* unit. A value without a unit
raises :class:`~pyteomics.auxiliary.TimeUnitError` instead of being
interpreted from its numeric size, so a bare ``0.004935`` is never silently
assumed to be minutes while ``353.43`` is assumed to be seconds.

To build a single table from one or more time-ordered readers (mzML, mzXML or
indexed MGF), use :func:`~pyteomics.auxiliary.retention_time_report`. It keeps
the originally parsed value, normalizes the converted column to one exact unit,
preserves scan order, and treats every reader as its own segment: times are
never offset or stitched together, so a restart cannot create a fake continuous
time axis. Scans sharing a time value remain separate rows.

.. code:: python

    from pyteomics import mzml, auxiliary

    >>> with mzml.MzML("data.mzML") as reader:
    ...     report = auxiliary.retention_time_report(reader, unit="minute")
    ...     row = report[0]
    >>> row["order"]          # position within this reader's iteration
    0
    >>> row["declared_time"]  # original quantity, declared unit intact
    42.05
    >>> row["declared_time"].unit_info
    'second'
    >>> row["time"]           # converted to the single requested unit
    0.7008333333333334
    >>> row["time"].unit_info
    'minute'

The equivalent conversion written out manually looks like this:

.. code:: python

    from pyteomics.auxiliary import unitfloat

    def in_minutes(x):
        '''Convert a time quantity to minutes

        Parameters
        ----------
        x: unitfloat
            A float representing a quantity of time annotated with a time unit

        Returns
        -------
        unitfloat:
            The time after conversion to minutes
        '''
        try:
            unit = x.unit_info
        except AttributeError:
            return x
        if unit == 'minute':
            return x
        elif unit == 'second':
            y = unitfloat(x / 60., 'minute')
            return y
        elif unit == 'hour':
            y = unitfloat(x * 60, 'minute')
            return y
        else:
            warnings.warn("Time unit %r not recognized" % unit)
        return x

.. autoclass:: pyteomics.auxiliary.structures.unitfloat
    :no-inherited-members:

.. autoclass:: pyteomics.auxiliary.structures.unitint
    :no-inherited-members:

.. autoclass:: pyteomics.auxiliary.structures.unitstr
    :no-inherited-members:
