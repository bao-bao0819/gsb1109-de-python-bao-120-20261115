Export a reproducible bank configuration
========================================

A parse result depends on more than the statement text. Parser options, tag
overrides, processor callbacks and transaction boundaries all change the
fields that appear in the output. :mod:`mt940.manifest` records that
configuration as JSON so another machine can reproduce the same parsing
without copying Python objects.

Build a manifest
----------------

:func:`mt940.manifest.export_manifest` takes a bank namespace and the same
``tags``, ``processors``, ``options`` and ``transaction_boundary`` values that
:func:`mt940.parse` accepts:

.. doctest::

   >>> import mt940
   >>> manifest = mt940.manifest.export_manifest(
   ...     'acme-bank',
   ...     tags={61: mt940.tags.StatementGLS()},
   ...     options=mt940.Options(reversal_sign=True),
   ... )
   >>> manifest['schema']
   'mt940.bank-manifest/1'
   >>> manifest['bank']
   'acme-bank'
   >>> manifest['content_version'].startswith('sha256:')
   True

Only rules that differ from the built-in defaults are serialized. Built-in
tags and processors are recorded as digests in ``environment``, so a manifest
also detects a changed ``mt940`` version with different parsing behaviour.
Use :func:`mt940.manifest.capture_manifest` when the configuration already
lives on a parsed collection. It also records tags and processor slots that
were removed.

The ``content_version`` field hashes every other field. Editing the JSON
without recalculating the digest makes verification fail before a statement
is parsed:

.. doctest::

   >>> tampered = mt940.manifest.manifest_loads(
   ...     mt940.manifest.manifest_dumps(manifest)
   ... )
   >>> tampered['options']['values']['reversal_sign'] = False
   >>> mt940.manifest.verify_manifest(tampered)[0].kind
   'content_version'

Restore and parse
-----------------

:func:`mt940.manifest.restore_manifest` returns a
:class:`mt940.manifest.RestoredConfig`. Its ``parse`` and ``parse_statements``
methods use exactly the recorded configuration:

.. doctest::

   >>> config = mt940.manifest.restore_manifest(manifest)
   >>> config.bank
   'acme-bank'
   >>> source = (
   ...     ':20:REF\n'
   ...     ':60F:C260101EUR100,00\n'
   ...     ':61:2601020102RC12,50NTRFCOFFEE//BANK\n'
   ...     ':62F:C260102EUR87,50\n'
   ... )
   >>> config.parse(source)[0].data['amount']
   <-12.50 EUR>

Restoring is strict. A custom tag or processor that cannot be imported,
rebuilt or bound raises :class:`mt940.manifest.ManifestError`. The error lists
every unavailable rule with its bank-qualified identifier, the reason and the
output fields it would have supplied. Nothing is silently replaced with a
built-in default.

Provide a binding when a rule cannot be imported, for example a callback
defined inside a test or a notebook:

.. doctest::

   >>> from typing import Any
   >>> def add_category(
   ...     transactions: Any,
   ...     tag: Any,
   ...     tag_dict: dict[str, Any],
   ...     result: dict[str, Any],
   ... ) -> dict[str, Any]:
   ...     """Add a category and declare its output field."""
   ...     del transactions, tag, tag_dict
   ...     return {**result, 'category': 'supplies'}
   >>> add_category.affected_fields = ('category',)
   >>> configured = mt940.manifest.export_manifest(
   ...     'acme-bank',
   ...     processors={
   ...         'post_transaction_details': [
   ...             mt940.processors.transaction_details_post_processor,
   ...             add_category,
   ...         ]
   ...     },
   ... )
   >>> restored = mt940.manifest.restore_manifest(
   ...     configured,
   ...     available_processors={'acme-bank:processor:'
   ...         'post_transaction_details:1': add_category},
   ... )
   >>> restored.processors['post_transaction_details'][1].__name__
   'add_category'

Declaring ``affected_fields`` on a custom callback keeps the impact report
specific. Without it, a missing processor reports ``*`` because the manifest
cannot know which keys the callback writes.

Keep banks apart
----------------

Every rule identifier starts with the bank namespace. Two banks may therefore
have rules with the same tag ID, class name or processor slot without sharing
state. :class:`mt940.manifest.ManifestRegistry` stores manifests per bank and
rejects a second manifest for one bank with a different content version unless
``replace=True`` is passed:

.. doctest::

   >>> registry = mt940.manifest.ManifestRegistry()
   >>> registry.add(mt940.manifest.export_manifest(
   ...     'bank-a', tags={61: mt940.tags.StatementGLS()}
   ... ))
   >>> registry.add(mt940.manifest.export_manifest(
   ...     'bank-b', tags={61: mt940.tags.StatementASNB()}
   ... ))
   >>> registry.banks
   ('bank-a', 'bank-b')
   >>> type(registry.restore('bank-a').tags[61]) is mt940.tags.StatementGLS
   True
   >>> type(registry.restore('bank-b').tags[61]) is mt940.tags.StatementASNB
   True

Write the file next to the statement or in version control:

.. code-block:: python

   mt940.manifest.write_manifest(manifest, 'acme-bank.manifest.json')
   same = mt940.manifest.read_manifest('acme-bank.manifest.json')

``manifest_dumps`` sorts keys and preserves non-ASCII text, so the same
configuration produces the same bytes. Keep the statement's own checksum or
revision alongside the manifest when a downstream result must be traceable to
both the input and the parser configuration.

What is not recorded
--------------------

The manifest describes the parser configuration, not the parser code or the
statement. It does not embed ``mt940`` itself, Python packages that a custom
callback imports, or the statement text. Record the package version from
``manifest['mt940_version']`` and the content version from
``manifest['content_version']`` in job metadata. A changed ``environment``
digest or a missing module is reported by
:func:`mt940.manifest.verify_manifest`.
