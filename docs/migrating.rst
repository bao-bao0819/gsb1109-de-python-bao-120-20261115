.. _migrating:

Migrating OFXv1 (SGML) to OFXv2 (XML)
=====================================

``ofxtools.migrate`` converts legacy OFXv1 documents to XML while keeping the
information that a naive re-serialization would lose.

.. code:: python

    from ofxtools.migrate import convert, convert_file

    result = convert_file("legacy.ofx", "legacy.xml", repair=True)
    print(result.report)
    verification = result.verify("legacy.ofx")

The command line front end does the same and re-reads each output by default:

.. code:: console

    $ ofxconvert legacy.ofx --repair --report

What is preserved
-----------------
* **Original version and encoding.**  The migrated ``<?OFX ...?>`` instruction
  carries ``ORIGOFXHEADER``, ``ORIGDATA``, ``ORIGVERSION``,
  ``ORIGENCODING``, ``ORIGCHARSET`` and ``ORIGCOMPRESSION``.  The body itself
  is UTF-8, as OFXv2 requires.  Pass ``version=203`` (or another supported
  version) to choose the output header version; the source version remains in
  ``ORIGVERSION``.
* **Empty versus missing.**  ``<MEMO></MEMO>`` is written back as an explicit
  start and end tag, and reads back as ``ofxtools.Types.EMPTY``.  An element
  that was simply absent reads back as ``None``.  Empty *required* elements
  raise an error instead of being treated as a default value.
* **Business order.**  Accounts, transactions, amounts and list members such
  as ``BUYSTOCK``/``INVBANKTRAN`` keep their relative order.

Illegal omissions
-----------------
OFXv1 permits leaving out the end tag of a data-bearing element, but not that
of an aggregate.  When an aggregate end tag is missing, its children are
parsed under the wrong parent.  ``convert()`` rejects such input by default:

.. code:: python

    convert("broken.ofx")            # raises StructureError
    convert("broken.ofx", repair=True)   # re-attaches and records the repair

Every repair is listed in ``result.report.repairs`` with the tag, the parent it
was found under, and the parent it belongs under.  Nothing is repaired
silently, and a mis-nested aggregate is never accepted as if it were legal.
The same applies to children that are out of schema order.

Re-reading and comparing
------------------------
``verify(source, converted)`` re-reads the converted document and compares:

* the complete business tree, including whether each node is an aggregate, a
  populated leaf, or a present-but-empty leaf;
* ordered accounts, transactions, and monetary amounts, one-to-one;
* the original header fields.

.. code:: python

    from ofxtools.migrate import verify

    verify("legacy.ofx", "legacy.xml")
    result.verify("legacy.ofx")  # same, using the converted tree

The CLI re-reads each output and deletes it if verification fails.
