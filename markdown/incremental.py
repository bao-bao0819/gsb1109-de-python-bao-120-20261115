# Python Markdown

# A Python implementation of John Gruber's Markdown.

# Documentation: https://python-markdown.github.io/
# GitHub: https://github.com/Python-Markdown/markdown/
# PyPI: https://pypi.org/project/Markdown/

# Started by Manfred Stienstra (http://www.dwerg.net/).
# Maintained for a few years by Yuri Takhteyev (http://www.freewisdom.org).
# Currently maintained by Waylan Limberg (https://github.com/waylan),
# Dmitry Shachnev (https://github.com/mitya57) and Isaac Muse (https://github.com/facelessuser).

# Copyright 2007-2023 The Python Markdown Project (v. 1.7 and later)
# Copyright 2004, 2005, 2006 Yuri Takhteyev (v. 0.2-1.6b)
# Copyright 2004 Manfred Stienstra (the original version)

# License: BSD (see LICENSE.md for details).

"""
Incremental Markdown rendering with per-fragment dependency tracking.

This module provides [`IncrementalMarkdown`][markdown.incremental.IncrementalMarkdown],
a wrapper around [`Markdown`][markdown.Markdown] which splits a document into
fragments, records which link reference and footnote definitions each fragment
actually depends on, and -- when the document is updated -- re-renders only the
fragments whose dependencies changed, reusing the cached output of all others.

Definitions (link references such as `[id]: /url` and footnotes such as
`[^id]: text`) are extracted from the document before fragments are built, so
definitions may appear anywhere in the document, including after the fragments
which use them. A fragment's dependency set is the transitive closure of the
definitions it uses: footnote definitions may themselves reference other
footnotes or link references, and those are dependencies too. Cycles in
footnote references are interpreted finitely -- each definition is expanded at
most once when computing the closure, so mutually recursive footnotes
terminate.

Changing a definition which no fragment (transitively) uses does not cause any
fragment to be re-rendered. Fragments are content-addressed: editing one
paragraph does not invalidate the cached output of any other paragraph.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .core import Markdown
from .blockprocessors import ReferenceProcessor
from .extensions.footnotes import FootnoteExtension

__all__ = ['IncrementalMarkdown', 'FragmentInfo']

# Matches footnote definitions, e.g. `[^id]: text` (mirrors the footnotes extension).
FOOTNOTE_DEF_RE = re.compile(r'^[ ]{0,3}\[\^([^\]]*)\]:[ ]*(.*)$', re.MULTILINE)
# Matches footnote references, e.g. `[^id]` (mirrors the footnotes extension).
FOOTNOTE_REF_RE = re.compile(r'(?<!!)\[\^([^\]]*)\](?!\s*:)')
# Reference-style links/images with an explicit (possibly empty) id: `[text][id]`, `![text][id]`, `[text][]`.
FULL_REF_RE = re.compile(r'!?\[([^\]]*)\]\[([^\]]*)\]')
# Shortcut reference links/images: `[id]`, `![id]` (not an inline link `[text](url)` and not `[text][id]`).
SHORT_REF_RE = re.compile(r'!?\[([^\]]+)\](?![(\[])')
# Reference definitions, e.g. `[id]: /url "title"` (shared with the block processor).
REFERENCE_DEF_RE = ReferenceProcessor.RE
# Collapses whitespace in reference ids the same way the inline processor does.
WHITESPACE_RE = re.compile(r'\s+')


@dataclass
class FragmentInfo:
    """ Information about a single rendered fragment of the document.

    Attributes:
        index (int): Position of the fragment in the document.
        source (str): The Markdown source of the fragment.
        dependencies (frozenset[tuple[str, str]]): The definitions this fragment depends on.
            Each dependency is a `(kind, id)` tuple where `kind` is `'ref'` for link
            references and `'fn'` for footnotes. The set is the transitive closure:
            definitions used by footnote definitions this fragment uses are included.
        html (str): The rendered HTML of the fragment.
        rebuilt (bool): Whether the fragment was re-rendered during the most recent
            [`convert`][markdown.incremental.IncrementalMarkdown.convert] call (`False`
            when the cached output was reused).

    """
    index: int
    source: str
    dependencies: frozenset[tuple[str, str]]
    html: str
    rebuilt: bool


class IncrementalMarkdown:
    """ Render Markdown documents incrementally, rebuilding only affected fragments.

    The document is split into block-level fragments. Each fragment is rendered
    independently and cached, keyed by its source and the content of every
    definition it (transitively) depends on. On subsequent calls to
    [`convert`][markdown.incremental.IncrementalMarkdown.convert], fragments whose
    source and dependencies are unchanged are served from the cache; only
    fragments whose own text or whose used definitions changed are re-rendered.

    Keyword Arguments:
        extensions (list): Additional extensions to use. The `footnotes`
            extension is always enabled and managed internally; do not pass it here.
        extension_configs (dict[str, dict[str, Any]]): Configuration settings for extensions.
        output_format (str): Format of output. Supported formats are `xhtml` and `html`.
            Default: `html`.

    Attributes:
        fragments (list[FragmentInfo]): Information about each fragment of the most
            recently converted document, including its recorded dependencies.
        references (dict[str, tuple[str, str]]): Link reference definitions of the most
            recently converted document.
        footnotes (dict[str, str]): Footnote definitions of the most recently converted
            document, in definition order.
        stats (dict[str, int]): Counts from the most recent `convert` call:
            `fragments` (total fragments), `rendered` (fragments actually re-rendered)
            and `reused` (fragments served from the cache).

    """

    def __init__(self, **kwargs: Any):
        self.output_format: str = kwargs.get('output_format', 'html')
        self._extensions: list = list(kwargs.get('extensions', []))
        configs: dict[str, dict[str, Any]] = dict(kwargs.get('extension_configs', {}))
        self._footnote_configs: dict[str, Any] = configs.pop('footnotes', {})
        self._extension_configs = configs
        self._cache: dict[tuple[str, tuple], str] = {}
        self.fragments: list[FragmentInfo] = []
        self.references: dict[str, tuple[str, str]] = {}
        self.footnotes: OrderedDict[str, str] = OrderedDict()
        self.stats: dict[str, int] = {'fragments': 0, 'rendered': 0, 'reused': 0}

    def reset(self) -> IncrementalMarkdown:
        """ Clear the fragment cache and all recorded state. """
        self._cache.clear()
        self.fragments = []
        self.references = {}
        self.footnotes = OrderedDict()
        self.stats = {'fragments': 0, 'rendered': 0, 'reused': 0}
        return self

    def convert(self, source: str) -> str:
        """ Convert a Markdown document to HTML, re-rendering only affected fragments.

        Arguments:
            source: Markdown formatted text as a Unicode string.

        Returns:
            A string in the specified output format.

        The first call renders every fragment. Later calls reuse the cached
        output of any fragment whose source and (transitive) dependencies are
        unchanged, so editing a definition only rebuilds the fragments which
        actually use it.

        """
        fragment_sources = self._parse(source)
        output = []
        self.fragments = []
        rendered = 0
        for index, fragment_source in enumerate(fragment_sources):
            dependencies = self._closure(self._scan_dependencies(fragment_source))
            key = (fragment_source, self._signature(dependencies))
            if key in self._cache:
                html = self._cache[key]
                rebuilt = False
            else:
                html = self._render_fragment(fragment_source, dependencies)
                self._cache[key] = html
                rebuilt = True
                rendered += 1
            self.fragments.append(FragmentInfo(
                index=index,
                source=fragment_source,
                dependencies=dependencies,
                html=html,
                rebuilt=rebuilt,
            ))
            if html:
                output.append(html)
        self.stats = {
            'fragments': len(self.fragments),
            'rendered': rendered,
            'reused': len(self.fragments) - rendered,
        }
        return '\n'.join(output)

    # -- Parsing ---------------------------------------------------------

    def _parse(self, source: str) -> list[str]:
        """ Split the document into content fragments, collecting definitions.

        Fills `self.references` and `self.footnotes` and returns the list of
        remaining content fragments in document order.
        """
        self.references = {}
        self.footnotes = OrderedDict()
        source = source.expandtabs(4).replace('\r\n', '\n').replace('\r', '\n')
        # Split on blank lines. Whitespace-only lines count as blank, and blank
        # blocks are kept so footnote continuation across blank lines can be
        # detected the same way the block parser detects it.
        blocks = re.split(r'\n[ \t]*\n', source)
        fragments = []
        i = 0
        while i < len(blocks):
            block = blocks[i]
            i += 1
            if not block.strip():
                continue
            m = FOOTNOTE_DEF_RE.search(block)
            if m:
                preceding = block[:m.start()]
                if preceding.strip():
                    fragments.extend(self._extract_references(preceding))
                i = self._extract_footnotes(blocks, i, block[m.start():])
            else:
                fragments.extend(self._extract_references(block))
        return [f for f in fragments if f.strip()]

    def _extract_footnotes(self, blocks: list[str], i: int, rest: str) -> int:
        """ Extract footnote definitions starting at `rest` (block index `i`).

        Mirrors `FootnoteBlockProcessor`: consecutive definitions in the same
        block are split, the remainder of the block is the last definition's
        content, and following blocks indented by four spaces are continuations.
        Returns the index of the next unprocessed block.
        """
        while rest:
            m = FOOTNOTE_DEF_RE.match(rest)
            id, first = m.group(1), m.group(2)
            m2 = FOOTNOTE_DEF_RE.search(rest, m.end())
            if m2 is not None and not rest[m.end():m2.start()].strip('\n'):
                # Another definition directly follows; this one is a single line.
                self.footnotes[id] = first.rstrip()
                rest = rest[m2.start():]
                continue
            if m2 is not None:
                # Content up to the next definition belongs to this one.
                before = rest[m.end():m2.start()].rstrip('\n')
                content = '\n'.join([first, self._detab(before)]).lstrip('\n')
                self.footnotes[id] = content.rstrip()
                rest = rest[m2.start():]
                continue
            # Last definition in the block: the rest of the block, plus any
            # following indented blocks, is its content.
            therest = rest[m.end():].lstrip('\n')
            content = '\n'.join([first, self._detab(therest)]).strip('\n')
            fn_blocks = [content]
            while i < len(blocks) and blocks[i].startswith(' ' * 4):
                fn_blocks.append(self._detab(blocks[i]))
                i += 1
            self.footnotes[id] = '\n\n'.join(fn_blocks).rstrip()
            rest = ''
        return i

    @staticmethod
    def _detab(block: str) -> str:
        """ Remove one level of four-space indentation from a block. """
        return '\n'.join(
            line[4:] if line.startswith(' ' * 4) else line
            for line in block.split('\n')
        )

    def _extract_references(self, block: str) -> list[str]:
        """ Extract link reference definitions from a block.

        Mirrors `ReferenceProcessor`: definitions on consecutive lines are all
        collected and any content before or after them is returned as fragments.
        """
        fragments = []
        while True:
            m = REFERENCE_DEF_RE.search(block)
            if not m:
                if block.strip():
                    fragments.append(block)
                return fragments
            start = end = m.start()
            for m in REFERENCE_DEF_RE.finditer(block, start):
                if m.start() > start and (block[end:m.start()].strip('\n') or '\n' in m.group()):
                    break
                id = m.group(1).strip().lower()
                link = m.group(2).lstrip('<').rstrip('>')
                title = m.group(5) or m.group(6)
                self.references[id] = (link, title)
                end = m.end()
            if block[:start].strip():
                fragments.append(block[:start].rstrip('\n'))
            block = block[end:].lstrip('\n')
            if not block.strip():
                return fragments

    # -- Dependency tracking ----------------------------------------------

    @staticmethod
    def _normalize_ref(id: str) -> str:
        """ Normalize a reference id the way the inline processor does. """
        return WHITESPACE_RE.sub(' ', id).lower()

    def _scan_dependencies(self, text: str) -> set[tuple[str, str]]:
        """ Find the definitions directly used by a fragment of source text. """
        dependencies = set()
        for m in FOOTNOTE_REF_RE.finditer(text):
            dependencies.add(('fn', m.group(1)))
        for m in FULL_REF_RE.finditer(text):
            id = self._normalize_ref(m.group(2) or m.group(1))
            if id:
                dependencies.add(('ref', id))
        for m in SHORT_REF_RE.finditer(text):
            id = m.group(1)
            if id.startswith('^'):
                continue  # Already recorded as a footnote reference.
            id = self._normalize_ref(id)
            if id:
                dependencies.add(('ref', id))
        return dependencies

    def _closure(self, dependencies: set[tuple[str, str]]) -> frozenset[tuple[str, str]]:
        """ Expand dependencies transitively through footnote definitions.

        Footnote definitions may reference other footnotes or link references;
        those become dependencies of the fragment as well. Each definition is
        expanded at most once, so reference cycles between footnotes terminate.
        """
        seen: set[tuple[str, str]] = set()
        stack = list(dependencies)
        while stack:
            dependency = stack.pop()
            if dependency in seen:
                continue
            seen.add(dependency)
            kind, id = dependency
            if kind == 'fn':
                text = self.footnotes.get(id)
                if text is not None:
                    stack.extend(self._scan_dependencies(text))
        return frozenset(seen)

    def _signature(self, dependencies: frozenset[tuple[str, str]]) -> tuple:
        """ Build a cache signature from the current content of each dependency.

        A dependency on an undefined id is recorded as `None`, so defining a
        previously missing id changes the signature and triggers a rebuild.
        """
        parts = []
        for kind, id in sorted(dependencies):
            if kind == 'ref':
                parts.append((kind, id, self.references.get(id)))
            else:
                parts.append((kind, id, self.footnotes.get(id)))
        return tuple(parts)

    # -- Rendering ----------------------------------------------------------

    def _render_fragment(self, source: str, dependencies: frozenset[tuple[str, str]]) -> str:
        """ Render one fragment with only the definitions it depends on. """
        footnote_extension = FootnoteExtension(**self._footnote_configs)
        md = Markdown(
            extensions=[footnote_extension, *self._extensions],
            extension_configs=self._extension_configs,
            output_format=self.output_format,
        )
        for kind, id in dependencies:
            if kind == 'ref' and id in self.references:
                md.references[id] = self.references[id]
        # Seed footnotes in document definition order so numbering is stable.
        for id, text in self.footnotes.items():
            if ('fn', id) in dependencies:
                footnote_extension.footnotes[id] = text
        return md.convert(source)
