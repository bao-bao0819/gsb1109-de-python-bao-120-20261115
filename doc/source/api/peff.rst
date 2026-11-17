.. automodule:: pyteomics.peff
   :exclude-members: Protein

Catalog example
---------------

:py:class:`~.PEFFCatalog` keeps protein entities, sequence versions and
annotation occurrences in separate tables:

.. code-block:: python

   >>> from pyteomics import peff
   >>> catalog = peff.PEFFCatalog('test.peff')
   >>> len(catalog.entities)
   5
   >>> len(catalog.sequence_versions)
   5
   >>> for occurrence in catalog.occurrences(annotation_type='ModResPsi'):
   ...     print(occurrence.start, occurrence.accession, occurrence.affected_residues)
   34 MOD:00046 S
   331 MOD:00046 S
   ...

Declared coordinates are retained verbatim. For range annotations,
``start``/``end`` are 1-based inclusive positions and ``declared_length`` is
``end - start + 1``. ``replacement_length`` comes from the declared
replacement field; an empty replacement (as in ``VariantComplex`` deletions)
has length 0.

After a sequence correction, annotations declared for the old version are not
silently applied to the new sequence with their old indices. They are marked
``pending_remap``; call :py:meth:`~.PEFFCatalog.remap_annotations` with a
declared correction to map what can be mapped unambiguously. Anything without
an explicit correspondence remains ``pending_remap``.
