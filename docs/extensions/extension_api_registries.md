---
title: Extension Registries
---

# Extension Registries

## Registries { #registry }

The [`markdown.util.Registry`][markdown.util.Registry] class is a priority sorted registry which Markdown uses
internally to determine the processing order of its various processors and patterns.

A `Registry` instance provides two public methods to alter the data of the registry: 
[`register`][markdown.util.Registry.register] and [`deregister`][markdown.util.Registry.deregister]. Use
`register` to add items and `deregister` to remove items. See each method for specifics.

When registering an item, a "name" and a "priority" must be provided. All items are automatically sorted by the value
of the "priority" parameter such that the item with the highest value will be processed first. The "name" is used to
remove (`deregister`) and get items.

A `Registry` instance is like a list (which maintains order) when reading data. You may iterate over the items, get an
item and get a count (length) of all items. You may also check that the registry contains an item.

When getting an item you may use either the index of the item or the string-based "name". For example:

```python
registry = Registry()
registry.register(SomeItem(), 'itemname', 20)
# Get the item by index
item = registry[0]
# Get the item by name
item = registry['itemname']
```

When checking that the registry contains an item, you may use either the string-based "name", or a reference to the
actual item. For example:

```python
someitem = SomeItem()
registry.register(someitem, 'itemname', 20)
# Contains the name
assert 'itemname' in registry
# Contains the item instance
assert someitem in registry
```

The method [`get_index_for_name`][markdown.util.Registry.get_index_for_name] is also available to obtain the index of
an item using that item’s assigned "name."

```python
index = registry.get_index_for_name('itemname')
```

The priorities of all built-in processors are documented in the API Reference for each type of processor as outlined below.

* [`preprocessors`][markdown.preprocessors.build_preprocessors]
* [`blockprocessors`][markdown.blockprocessors.build_block_parser]
* [`treeprocessors`][markdown.treeprocessors.build_treeprocessors]
* [`inlinepatterns`][markdown.inlinepatterns.build_inlinepatterns]
* [`postprocessors`][markdown.postprocessors.build_postprocessors]

The built-in extensions modify those collections of processors as documented in the API Reference for each extension.

* [Abbreviations][markdown.extensions.abbr.AbbrExtension.extendMarkdown]
* [Admonition][markdown.extensions.admonition.AdmonitionExtension.extendMarkdown]
* [Attribute Lists][markdown.extensions.attr_list.AttrListExtension.extendMarkdown]
* [CodeHilite][markdown.extensions.codehilite.CodeHiliteExtension.extendMarkdown]
* [Definition Lists][markdown.extensions.def_list.DefListExtension.extendMarkdown]
* [Fenced Code Blocks][markdown.extensions.fenced_code.FencedCodeExtension.extendMarkdown]
* [Footnotes][markdown.extensions.footnotes.FootnoteExtension.extendMarkdown]
* [Legacy Attributes][markdown.extensions.legacy_attrs.LegacyAttrExtension.extendMarkdown]
* [Legacy Emphasis][markdown.extensions.legacy_em.LegacyEmExtension.extendMarkdown]
* [Markdown in HTML][markdown.extensions.md_in_html.MarkdownInHtmlExtension.extendMarkdown]
* [Meta-Data][markdown.extensions.meta.MetaExtension.extendMarkdown]
* [New Line to Break][markdown.extensions.nl2br.Nl2BrExtension.extendMarkdown]
* [Sane Lists][markdown.extensions.sane_lists.SaneListExtension.extendMarkdown]
* [SmartyPants][markdown.extensions.smarty.SmartyExtension.extendMarkdown]
* [Tables][markdown.extensions.tables.TableExtension.extendMarkdown]
* [Table of Contents][markdown.extensions.toc.TocExtension.extendMarkdown]
* [WikiLinks][markdown.extensions.wikilinks.WikiLinkExtension.extendMarkdown]

A few things to note about the above linked collections.

* A higher number has a higher priority. Processors assigned a higher priority are run before processors with a lower
  priority. In other words, processors assigned a higher number are run before those assigned a lower number.
* Large gaps have been left between the default processors. This leaves lots of room for multiple different extensions
  to insert many additional processors. While integers have been used, floats may be used to get even more precision.
  While negative numbers could be used, that would only be useful to ensure that a processor was the absolutely last
  processor run.
* While it is preferred that the assigned priorities of the built-in processors remain stable, a bug fix has occasionally
  required an adjustment to an assigned priority. Any such changes will always be documented as backward incompatible
  changes.
* You will likely need to check the source code to determine assigned priorities of third-party extensions when resolving
  conflicts.
