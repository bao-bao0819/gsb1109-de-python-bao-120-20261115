---
title: Extension Integration
---

# Extension Integration

## Working with the ElementTree {: #working_with_et }

As mentioned, the Markdown parser converts a source document to an [ElementTree][ElementTree] object before
serializing that back to Unicode text. Markdown has provided some helpers to ease that manipulation within the context
of the Markdown module.

First, import the ElementTree module:

```python
import xml.etree.ElementTree as etree
```
Sometimes you may want text inserted into an element to be parsed by [Inline Patterns][]. In such a situation, simply
insert the text as you normally would and the text will be automatically run through the Inline Patterns. However, if
you do *not* want some text to be parsed by Inline Patterns, then insert the text as an `AtomicString`.

```python
from markdown.util import AtomicString
some_element.text = AtomicString(some_text)
```

Here's a basic example which creates an HTML table (note that the contents of the second cell (`td2`) will be run
through Inline Patterns latter):

```python
table = etree.Element("table")
table.set("cellpadding", "2")                      # Set cellpadding to 2
tr = etree.SubElement(table, "tr")                 # Add child tr to table
td1 = etree.SubElement(tr, "td")                   # Add child td1 to tr
td1.text = markdown.util.AtomicString("Cell content") # Add plain text content
td2 = etree.SubElement(tr, "td")                   # Add second td to tr
td2.text = "*text* with **inline** formatting."    # Add markup text
table.tail = "Text after table"                    # Add text after table
```

You can also manipulate an existing tree. Consider the following example which adds a `class` attribute to `<a>`
elements:

```python
def set_link_class(self, element):
    for child in element:
        if child.tag == "a":
              child.set("class", "myclass") #set the class attribute
        set_link_class(child) # run recursively on children
```

For more information about working with ElementTree see the [ElementTree
Documentation][ElementTree].

## Working with Raw HTML {: #working_with_raw_html }

Occasionally an extension may need to call out to a third party library which returns a pre-made string
of raw HTML that needs to be inserted into the document unmodified. Raw strings can be stashed for later
retrieval using an [`htmlStash`][markdown.util.HtmlStash] instance, rather than converting them into
`ElementTree` objects. A raw string (which may or may not be raw HTML) passed to 
[`self.md.htmlStash.store()`][markdown.util.HtmlStash.store] will be saved to the stash and a
placeholder string will be returned which should be inserted into the tree instead. After the tree is
serialized, a postprocessor will replace the placeholder with the raw string. This prevents subsequent
processing steps from modifying the HTML data. For example,

```python
html = "<p>This is some <em>raw</em> HTML data</p>"
el = etree.Element("div")
el.text = self.md.htmlStash.store(html)
```

For the global `htmlStash` instance to be available from a processor, the [`markdown.Markdown`][markdown.Markdown]
instance must be passed to the processor from [extendMarkdown](#extendmarkdown) and will be available as 
`self.md.htmlStash`.

## Integrating Your Code Into Markdown {: #integrating_into_markdown }

Once you have the various pieces of your extension built, you need to tell Markdown about them and ensure that they
are run in the proper sequence. Markdown accepts an `Extension` instance for each extension. Therefore, you will need
to define a class that extends [`markdown.extensions.Extension`][markdown.extensions.Extension] and overrides the
[`extendMarkdown`][markdown.extensions.Extension.extendMarkdown] method. Within this class you will manage
configuration options for your extension and attach the various processors and patterns to the Markdown instance.

It is important to note that the order of the various processors and patterns matters. For example, if we replace
`http://...` links with `<a>` elements, and *then* try to deal with  inline HTML, we will end up with a mess.
Therefore, the various types of processors and patterns are stored within an instance of the
[`markdown.Markdown`][markdown.Markdown] class in a [Registry][]. Your `Extension` class will need to manipulate
those registries appropriately. You may `register` instances of your processors and patterns with an appropriate
priority, `deregister` built-in instances, or replace a built-in instance with your own.

### `extendMarkdown` {: #extendmarkdown }

The [`extendMarkdown`][markdown.extensions.Extension.extendMarkdown] method of a 
[`markdown.extensions.Extension`][markdown.extensions.Extension] class accepts one argument `md`, which is a
pointer to the instance of the [`markdown.Markdown`][markdown.Markdown] class. You should use this to access
the [Registries][Registry] of processors and patterns. They are found under the following attributes:

* `md.preprocessors`
* `md.inlinePatterns`
* `md.parser.blockprocessors`
* `md.treeprocessors`
* `md.postprocessors`

Some other things you may want to access on the [`markdown.Markdown`][markdown.Markdown] instance are:

* `md.htmlStash`
* [`md.output_formats`][markdown.Markdown.output_formats]
* [`md.set_output_format()`][markdown.Markdown.set_output_format]
* `md.output_format`
* `md.serializer`
* [`md.registerExtension()`][markdown.Markdown.registerExtension]
* `md.tab_length`
* [`md.block_level_elements`][markdown.util.BLOCK_LEVEL_ELEMENTS]
* [`md.is_block_level()`][markdown.Markdown.is_block_level]
* [`md.ESCAPED_CHARS`][markdown.Markdown.ESCAPED_CHARS]

!!! Warning
    With access to the above items, theoretically you have the option to change anything through various
    [monkey_patching][] techniques. However, you should be aware that the various undocumented parts of Markdown may
    change without notice and your monkey_patches may break with a new release. Therefore, what you really should be
    doing is inserting processors and patterns into the Markdown pipeline. Consider yourself warned!

[monkey_patching]: https://en.wikipedia.org/wiki/Monkey_patch

A simple example:

```python
from markdown.extensions import Extension

class MyExtension(Extension):
    def extendMarkdown(self, md):
        # Register instance of 'mypattern' with a priority of 175
        md.inlinePatterns.register(MyPattern(md), 'mypattern', 175)
```

### registerExtension {: #registerextension }

Some extensions may need to have their state reset between multiple runs of the [`markdown.Markdown`][markdown.Markdown] class. For
example, consider the following use of the [Footnotes][] extension:

```python
md = markdown.Markdown(extensions=['footnotes'])
html1 = md.convert(text_with_footnote)
md.reset()
html2 = md.convert(text_without_footnote)
```

Without calling [`reset`][markdown.Markdown.reset], the footnote definitions from the first document will be inserted
into the second document as they are still stored within the class instance. Therefore the
[`Extension`][markdown.extensions.Extension] class needs to define a `reset` method that
will reset the state of the extension (i.e.: `self.footnotes = {}`). However, as many extensions do not have a need
for `reset`, `reset` is only called on extensions that are registered.

To register an extension, call [`md.registerExtension`][markdown.Markdown.registerExtension] from within your 
[`extendMarkdown`][markdown.extensions.Extension.extendMarkdown] method:

```python
def extendMarkdown(self, md):
    md.registerExtension(self)
    # insert processors and patterns here
```

Then, each time `reset` is called on the `markdown.Markdown` instance, the `reset` method of each registered extension
will be called as well. You should also note that `reset` will be called on each registered extension after it is
initialized the first time. Keep that in mind when over-riding the extension's `reset` method.

### Configuration Settings {: #configsettings }

If an extension uses any parameters that the user may want to change, those parameters should be stored in
[`self.config`][markdown.extensions.Extension.config] of your [`markdown.extensions.Extension`][markdown.extensions.Extension]
class in the following format:

```python
class MyExtension(markdown.extensions.Extension):
    def __init__(self, **kwargs):
        self.config = {
            'option1' : ['value1', 'description1'],
            'option2' : ['value2', 'description2']
        }
        super(MyExtension, self).__init__(**kwargs)
```

When implemented this way the configuration parameters can be overridden at run time (thus the call to `super`). For
example:

```python
markdown.Markdown(extensions=[MyExtension(option1='other value')])
```

Note that if a keyword is passed in that is not already defined in `self.config`, then a `KeyError` is raised.

The [`markdown.extensions.Extension`][markdown.extensions.Extension] class and its subclasses have the following
methods available to assist in working with configuration settings:

* [`getConfig`][markdown.extensions.Extension.getConfig]
* [`getConfigs`][markdown.extensions.Extension.getConfigs]
* [`getConfigInfo`][markdown.extensions.Extension.getConfigInfo]
* [`setConfig`][markdown.extensions.Extension.setConfig]
* [`setConfigs`][markdown.extensions.Extension.setConfigs]

### Naming an Extension { #naming_an_extension }

As noted in the [library reference] an instance of an extension can be passed directly to [`markdown.Markdown`][markdown.Markdown].
In fact, this is the preferred way to use third-party extensions.

For example:

```python
import markdown
from path.to.module import MyExtension
md = markdown.Markdown(extensions=[MyExtension(option='value')])
```

However, Markdown also accepts "named" third party extensions for those occasions when it is impractical to import an
extension directly (from the command line or from within templates). A "name" can either be a registered [entry
point](#entry_point) or a string using Python's [dot notation](#dot_notation).

#### Entry Point { #entry_point }

[Entry points] are defined in a Python package's `setup.py` script. The script must use [setuptools] to support entry
points. Python-Markdown extensions must be assigned to the `markdown.extensions` group. An entry point definition
might look like this:

```python
from setuptools import setup

setup(
    # ...
    entry_points={
        'markdown.extensions': ['myextension = path.to.module:MyExtension']
    }
)
```

After a user installs your extension using the above script, they could then call the extension using the
`myextension` string name like this:

```python
markdown.markdown(text, extensions=['myextension'])
```

Note that if two or more entry points within the same group are assigned the same name, Python-Markdown will only ever
use the first one found and ignore all others. Therefore, be sure to give your extension a unique name.

For more information on writing `setup.py` scripts, see the Python documentation on [Packaging and Distributing
Projects].

#### Dot Notation { #dot_notation }

If an extension does not have a registered entry point, Python's dot notation may be used instead. The extension must
be installed as a Python module on your PYTHONPATH. Generally, a class should be specified in the name. The class must
be at the end of the name and be separated by a colon from the module.

Therefore, if you were to import the class like this:

```python
from path.to.module import MyExtension
```

Then the extension can be loaded as follows:

```python
markdown.markdown(text, extensions=['path.to.module:MyExtension'])
```

You do not need to do anything special to support this feature. As long as your extension class is able to be
imported, a user can include it with the above syntax.

The above two methods are especially useful if you need to implement a large number of extensions with more than one
residing in a module. However, if you do not want to require that your users include the class name in their string,
you must define only one extension per module and that module must contain a module-level function called
`makeExtension` that accepts `**kwargs` and returns an extension instance.

For example:

```python
class MyExtension(markdown.extensions.Extension)
    # Define extension here...

def makeExtension(**kwargs):
    return MyExtension(**kwargs)
```

When [`markdown.Markdown`][markdown.Markdown] is passed the "name" of your extension as a dot notation string that
does not include a class (for example `path.to.module`), it will import the module and call the `makeExtension`
function to initiate your extension.

[ElementTree]: https://docs.python.org/3/library/xml.etree.elementtree.html
[Footnotes]: https://github.com/Python-Markdown/markdown/blob/master/markdown/extensions/footnotes.py
[Inline Patterns]: api.md#inline-patterns
[library reference]: ../library.md
[Entry points]: https://setuptools.readthedocs.io/en/latest/setuptools.html#dynamic-discovery-of-services-and-plugins
[setuptools]: https://packaging.python.org/key_projects/#setuptools
[Packaging and Distributing Projects]: https://packaging.python.org/tutorials/distributing-packages/
[Registry]: extension_api_registries.md#registry
