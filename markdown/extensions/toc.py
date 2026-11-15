# Table of Contents Extension for Python-Markdown
# ===============================================

# See https://Python-Markdown.github.io/extensions/toc
# for documentation.

# Original code Copyright 2008 [Jack Miller](https://codezen.org/)

# All changes Copyright 2008-2024 The Python Markdown Project

# License: [BSD](https://opensource.org/licenses/bsd-license.php)

"""
Add table of contents support to Python-Markdown.

See the [documentation](https://Python-Markdown.github.io/extensions/toc)
for details.
"""

from __future__ import annotations

from . import Extension
from ..treeprocessors import Treeprocessor
from ..util import parseBoolValue, AMP_SUBSTITUTE, deprecated, HTML_PLACEHOLDER_RE, AtomicString
from ..treeprocessors import UnescapeTreeprocessor
from ..serializers import RE_AMP
from ..inlinepatterns import HTML_RE
import re
import html
import unicodedata
from copy import deepcopy
import xml.etree.ElementTree as etree
from typing import TYPE_CHECKING, Any, Iterator, MutableSet

if TYPE_CHECKING:  # pragma: no cover
    from markdown import Markdown


def slugify(value: str, separator: str, unicode: bool = False) -> str:
    """ Slugify a string, to make it URL friendly. """
    if not unicode:
        # Replace Extended Latin characters with ASCII, i.e. `žlutý` => `zluty`
        value = unicodedata.normalize('NFKD', value)
        value = value.encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value).strip().lower()
    return re.sub(r'[{}\s]+'.format(separator), separator, value)


def slugify_unicode(value: str, separator: str) -> str:
    """ Slugify a string, to make it URL friendly while preserving Unicode characters. """
    return slugify(value, separator, unicode=True)


IDCOUNT_RE = re.compile(r'^(.*)_([0-9]+)$')


class AnchorRegistry:
    """ Track heading ids and the legacy anchors which alias them.

    A knowledge base may insert a new section carrying the same title ahead of
    the original one. The original section must keep the anchor derived from
    its title so that existing links keep resolving, while the inserted
    section needs a different anchor.

    Two kinds of evidence are accepted when binding a legacy anchor:

    * Explicit evidence: an explicit id (or `data-toc-anchor` attribute) on a
      heading. Explicit evidence always wins.
    * Inferred evidence: the configured old-to-new `slug_mapping`, applied to
      the id a heading's title would naturally produce.

    A same-name heading on its own is no evidence. When an anchor cannot be
    bound safely, the disagreement is recorded in `conflicts` rather than
    guessed, and links using it are left untouched.

    Resolution happens in two phases. `plan` records every heading and reserves
    explicitly declared ids up front, so a heading cannot steal an anchor an
    earlier or later heading explicitly owns. `finalize` then assigns the
    remaining ids and builds the alias table.
    """

    def __init__(
        self,
        used_ids: MutableSet[str],
        slugify,
        separator: str,
        id_counters: dict[str, int] | None = None,
        slug_mapping: dict[str, str] | None = None,
    ):
        self.used_ids = used_ids
        self.slugify = slugify
        self.separator = separator
        self.id_counters = id_counters if id_counters is not None else {}
        self.slug_mapping = slug_mapping or {}
        # Planned headings in document order, each holding its evidence and id.
        self._headings: list[dict[str, Any]] = []
        # Legacy anchors finalized to a safe rewrite target.
        self.aliases: dict[str, str] = {}
        # Structured records of anchors which could not be bound safely.
        self.conflicts: list[dict[str, str]] = []

    def slug(self, name: str) -> str:
        return self.slugify(html.unescape(name), self.separator)

    def _conflict(self, anchor: str, reason: str, **details: str) -> None:
        record = {'anchor': anchor, 'reason': reason}
        record.update(details)
        if record not in self.conflicts:
            self.conflicts.append(record)

    def _set_explicit_claim(
        self, claims: dict[str, str], anchor: str, target_id: str
    ) -> None:
        """ Record an explicit anchor binding, flagging disagreement. """
        if not anchor:
            return
        existing = claims.get(anchor)
        if existing is not None and existing != target_id:
            self._conflict(
                anchor, 'duplicate explicit anchor',
                target=existing, other=target_id,
            )
        else:
            claims[anchor] = target_id

    def plan(
        self,
        name: str,
        explicit_id: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """ Record a heading and reserve its explicit id immediately. """
        natural = self.slug(name)
        entry: dict[str, Any] = {
            'natural': natural,
            'mapped': self.slug_mapping.get(natural),
            'explicit_id': explicit_id,
            'aliases': tuple(aliases),
            'id': None,
        }
        if explicit_id:
            if explicit_id in self.used_ids:
                # Two headings (or a heading and another element) claim one id;
                # nothing says which is the original section, so keep a unique
                # fallback and flag the disagreement.
                entry['id'] = unique(explicit_id, self.used_ids, self.id_counters)
                self._conflict(
                    explicit_id, 'duplicate explicit id', fallback=entry['id'],
                )
            else:
                entry['id'] = explicit_id
                self.used_ids.add(explicit_id)
        self._headings.append(entry)
        return entry

    def finalize(self) -> None:
        """ Assign every id and resolve evidence into safe aliases. """
        # Ownership of a heading's natural anchor, proven by an explicit id.
        explicit_claims: dict[str, str] = {}
        # Direct `data-toc-anchor` instructions, which must be honored or flagged.
        alias_claims: dict[str, str] = {}
        # Anchors inferred from `slug_mapping` plus document order.
        inferred_claims: dict[str, str] = {}
        self_assigned_mapped: set[str] = set()
        explicit_owned = {
            entry['explicit_id']
            for entry in self._headings if entry['explicit_id']
        }

        # Assign ids to headings without an explicit id, in document order.
        for entry in self._headings:
            natural = entry['natural']
            mapped = entry['mapped']
            if entry['id'] is not None:
                heading_id = entry['id']
            elif mapped is not None and mapped not in self.used_ids:
                # The first same-name (inserted) section takes the new anchor,
                # leaving the natural anchor free for the original section.
                heading_id = mapped
                self.used_ids.add(mapped)
                self_assigned_mapped.add(mapped)
                inferred_claims.setdefault(natural, mapped)
            else:
                # No usable evidence for a new anchor: retain historic
                # deduplication rather than stealing another section's anchor.
                heading_id = unique(natural, self.used_ids, self.id_counters)
                self.used_ids.add(heading_id)
                if mapped is not None:
                    if (
                        mapped in self.used_ids
                        and mapped not in self_assigned_mapped
                        and mapped not in explicit_owned
                    ):
                        # The mapped anchor belongs to an unrelated element.
                        self._conflict(
                            natural, 'mapped anchor already in use',
                            target=mapped, fallback=heading_id,
                        )
                    inferred_claims.setdefault(natural, mapped)
            entry['id'] = heading_id

            # Explicit evidence from the heading itself. A mapping connects the
            # natural anchor to this heading; an id equal to the natural slug
            # declares outright that the heading owns that anchor.
            if entry['explicit_id'] and (
                mapped is not None or entry['explicit_id'] == natural
            ):
                self._set_explicit_claim(explicit_claims, natural, heading_id)
            for alias in entry['aliases']:
                self._set_explicit_claim(alias_claims, alias, heading_id)

        # Same-name headings without any evidence binding their natural anchor
        # cannot be told apart, so leave a conflict instead of guessing.
        counts: dict[str, int] = {}
        for entry in self._headings:
            counts[entry['natural']] = counts.get(entry['natural'], 0) + 1
        evidenced = set(explicit_claims) | set(inferred_claims)
        for natural, count in counts.items():
            if count > 1 and natural not in evidenced:
                fallback = max(
                    entry['id'] for entry in self._headings
                    if entry['natural'] == natural
                )
                self._conflict(
                    natural, 'unresolved duplicate heading', fallback=fallback,
                )

        # Build the alias table, dropping anchors which are unsafe to rewrite.
        conflicted = {record['anchor'] for record in self.conflicts}
        claims = dict(inferred_claims)
        claims.update(explicit_claims)
        claims.update(alias_claims)
        for anchor, target_id in claims.items():
            if anchor in conflicted:
                continue
            if anchor in self.used_ids:
                # The legacy anchor is still a real id, so links resolve
                # directly. A direct alias pointing elsewhere cannot be
                # honored; derived natural/mapping claims are simply moot.
                if alias_claims.get(anchor) not in (None, anchor):
                    self._conflict(
                        anchor, 'anchor is also an existing id', target=target_id,
                    )
                continue
            if target_id not in self.used_ids:
                self._conflict(anchor, 'target section not found', target=target_id)
                continue
            self.aliases[anchor] = target_id

    def resolve_fragment(self, fragment: str) -> str | None:
        """ Return the rewrite target for a fragment, or `None` to keep it. """
        if not fragment or fragment in self.used_ids or fragment not in self.aliases:
            return None
        return self.aliases[fragment]


def unique(id: str, ids: MutableSet[str], counters: dict[str, int] | None = None) -> str:
    """ Ensure id is unique in set of ids. Append '_1', '_2'... if not

    `counters` is an optional cache which maps a base id to the highest `n` for which
    `base_1` through `base_n` are all known to be in `ids`. When the same cache is passed
    along with the same `ids` on every call, a repeated id skips past the suffixes which
    earlier calls already found to be taken rather than checking each of them again.
    """
    if id in ids or not id:
        m = IDCOUNT_RE.match(id)
        if m:
            base, count = m.group(1), int(m.group(2))
        else:
            base, count = id, 0
        skip = False
        if counters is not None:
            known = counters.get(base, 0)
            # Only skip ahead when this id falls within the run of suffixes known to be
            # taken, or is the next one after it, as any later suffix may still be free.
            if count <= known or id == f'{base}_{known + 1}':
                skip = True
                count = max(count, known)
        count += 1
        while f'{base}_{count}' in ids:
            count += 1
        id = f'{base}_{count}'
        if skip:
            # Every suffix from `base_1` through the new id is now known to be taken.
            counters[base] = count
    ids.add(id)
    return id


@deprecated('Use `render_inner_html` and `striptags` instead.')
def get_name(el: etree.Element) -> str:
    """Get title name."""

    text = []
    for c in el.itertext():
        if isinstance(c, AtomicString):
            text.append(html.unescape(c))
        else:
            text.append(c)
    return ''.join(text).strip()


@deprecated('Use `run_postprocessors`, `render_inner_html` and/or `striptags` instead.')
def stashedHTML2text(text: str, md: Markdown, strip_entities: bool = True) -> str:
    """ Extract raw HTML from stash, reduce to plain text and swap with placeholder. """
    def _html_sub(m: re.Match[str]) -> str:
        """ Substitute raw html with plain text. """
        try:
            raw = md.htmlStash.rawHtmlBlocks[int(m.group(1))]
        except (IndexError, TypeError):  # pragma: no cover
            return m.group(0)
        # Strip out tags and/or entities - leaving text
        res = re.sub(r'(<[^>]+>)', '', raw)
        if strip_entities:
            res = re.sub(r'(&[\#a-zA-Z0-9]+;)', '', res)
        return res

    return HTML_PLACEHOLDER_RE.sub(_html_sub, text)


def unescape(text: str) -> str:
    """ Unescape Markdown backslash escaped text. """
    c = UnescapeTreeprocessor()
    return c.unescape(text)


def strip_tags(text: str) -> str:
    """ Strip HTML tags and return plain text. Note: HTML entities are unaffected. """
    # A comment could contain a tag, so strip comments first
    while (start := text.find('<!--')) != -1 and (end := text.find('-->', start)) != -1:
        text = f'{text[:start]}{text[end + 3:]}'

    # Use the inline HTML pattern to respect quoted attribute values.
    text = re.sub(HTML_RE, '', text, flags=re.DOTALL)

    # Collapse whitespace
    text = ' '.join(text.split())
    return text


def escape_cdata(text: str) -> str:
    """ Escape character data. """
    if "&" in text:
        # Only replace & when not part of an entity
        text = RE_AMP.sub('&amp;', text)
    if "<" in text:
        text = text.replace("<", "&lt;")
    if ">" in text:
        text = text.replace(">", "&gt;")
    return text


def run_postprocessors(text: str, md: Markdown) -> str:
    """ Run postprocessors from Markdown instance on text. """
    for pp in md.postprocessors:
        text = pp.run(text)
    return text.strip()


def render_inner_html(el: etree.Element, md: Markdown) -> str:
    """ Fully render inner html of an `etree` element as a string. """
    # The `UnescapeTreeprocessor` runs after `toc` extension so run here.
    text = unescape(md.serializer(el))

    # strip parent tag
    start = text.index('>') + 1
    end = text.rindex('<')
    text = text[start:end].strip()

    return run_postprocessors(text, md)


def remove_fnrefs(root: etree.Element) -> etree.Element:
    """ Remove footnote references from a copy of the element, if any are present. """
    # Remove footnote references, which look like this: `<sup id="fnref:1">...</sup>`.
    # If there are no `sup` elements, then nothing to do.
    if next(root.iter('sup'), None) is None:
        return root
    root = deepcopy(root)
    # Find parent elements that contain `sup` elements.
    for parent in root.findall('.//sup/..'):
        carry_text = ""
        for child in reversed(parent):  # Reversed for the ability to mutate during iteration.
            # Remove matching footnote references but carry any `tail` text to preceding elements.
            if child.tag == 'sup' and child.get('id', '').startswith('fnref'):
                carry_text = f'{child.tail or ""}{carry_text}'
                parent.remove(child)
            elif carry_text:
                child.tail = f'{child.tail or ""}{carry_text}'
                carry_text = ""
        if carry_text:
            parent.text = f'{parent.text or ""}{carry_text}'
    return root


def nest_toc_tokens(toc_list):
    """Given an unsorted list with errors and skips, return a nested one.

        [{'level': 1}, {'level': 2}]
        =>
        [{'level': 1, 'children': [{'level': 2, 'children': []}]}]

    A wrong list is also converted:

        [{'level': 2}, {'level': 1}]
        =>
        [{'level': 2, 'children': []}, {'level': 1, 'children': []}]
    """

    ordered_list = []
    if len(toc_list):
        # Initialize everything by processing the first entry
        last = toc_list.pop(0)
        last['children'] = []
        levels = [last['level']]
        ordered_list.append(last)
        parents = []

        # Walk the rest nesting the entries properly
        while toc_list:
            t = toc_list.pop(0)
            current_level = t['level']
            t['children'] = []

            # Reduce depth if current level < last item's level
            if current_level < levels[-1]:
                # Pop last level since we know we are less than it
                levels.pop()

                # Pop parents and levels we are less than or equal to
                to_pop = 0
                for p in reversed(parents):
                    if current_level <= p['level']:
                        to_pop += 1
                    else:  # pragma: no cover
                        break
                if to_pop:
                    levels = levels[:-to_pop]
                    parents = parents[:-to_pop]

                # Note current level as last
                levels.append(current_level)

            # Level is the same, so append to
            # the current parent (if available)
            if current_level == levels[-1]:
                (parents[-1]['children'] if parents
                 else ordered_list).append(t)

            # Current level is > last item's level,
            # So make last item a parent and append current as child
            else:
                last['children'].append(t)
                parents.append(last)
                levels.append(current_level)
            last = t

    return ordered_list


class TocTreeprocessor(Treeprocessor):
    """ Step through document and build TOC. """

    def __init__(self, md: Markdown, config: dict[str, Any]):
        super().__init__(md)

        self.marker: str = config["marker"]
        self.title: str = config["title"]
        self.base_level = int(config["baselevel"]) - 1
        self.slugify = config["slugify"]
        self.sep = config["separator"]
        self.toc_class = config["toc_class"]
        self.title_class: str = config["title_class"]
        self.use_anchors: bool = parseBoolValue(config["anchorlink"])
        self.anchorlink_class: str = config["anchorlink_class"]
        self.use_permalinks = parseBoolValue(config["permalink"], False)
        if self.use_permalinks is None:
            self.use_permalinks = config["permalink"]
        self.permalink_class: str = config["permalink_class"]
        self.permalink_title: str = config["permalink_title"]
        self.permalink_leading: bool | None = parseBoolValue(config["permalink_leading"], False)
        self.slug_mapping: dict[str, str] = config["slug_mapping"]
        self.anchor_attribute: str = config["anchor_attribute"]
        self.header_rgx = re.compile("[Hh][123456]")
        if isinstance(config["toc_depth"], str) and '-' in config["toc_depth"]:
            self.toc_top, self.toc_bottom = [int(x) for x in config["toc_depth"].split('-')]
        else:
            self.toc_top = 1
            self.toc_bottom = int(config["toc_depth"])

    def iterparent(self, node: etree.Element) -> Iterator[tuple[etree.Element, etree.Element]]:
        """ Iterator wrapper to get allowed parent and child all at once. """

        # We do not allow the marker inside a header as that
        # would causes an endless loop of placing a new TOC
        # inside previously generated TOC.
        for child in node:
            if not self.header_rgx.match(child.tag) and child.tag not in ['pre', 'code']:
                yield node, child
                yield from self.iterparent(child)

    def replace_marker(self, root: etree.Element, elem: etree.Element) -> None:
        """ Replace marker with elem. """
        for (p, c) in self.iterparent(root):
            text = ''.join(c.itertext()).strip()
            if not text:
                continue

            # To keep the output from screwing up the
            # validation by putting a `<div>` inside of a `<p>`
            # we actually replace the `<p>` in its entirety.

            # The `<p>` element may contain more than a single text content
            # (`nl2br` can introduce a `<br>`). In this situation, `c.text` returns
            # the very first content, ignore children contents or tail content.
            # `len(c) == 0` is here to ensure there is only text in the `<p>`.
            if c.text and c.text.strip() == self.marker and len(c) == 0:
                for i in range(len(p)):
                    if p[i] == c:
                        p[i] = elem
                        break

    def set_level(self, elem: etree.Element) -> None:
        """ Adjust header level according to base level. """
        level = int(elem.tag[-1]) + self.base_level
        if level > 6:
            level = 6
        elem.tag = 'h%d' % level

    def add_anchor(self, c: etree.Element, elem_id: str) -> None:
        anchor = etree.Element("a")
        anchor.text = c.text
        anchor.attrib["href"] = "#" + elem_id
        anchor.attrib["class"] = self.anchorlink_class
        c.text = ""
        for elem in c:
            anchor.append(elem)
        while len(c):
            c.remove(c[0])
        c.append(anchor)

    def add_permalink(self, c: etree.Element, elem_id: str) -> None:
        permalink = etree.Element("a")
        permalink.text = ("%spara;" % AMP_SUBSTITUTE
                          if self.use_permalinks is True
                          else self.use_permalinks)
        permalink.attrib["href"] = "#" + elem_id
        permalink.attrib["class"] = self.permalink_class
        if self.permalink_title:
            permalink.attrib["title"] = self.permalink_title
        if self.permalink_leading:
            permalink.tail = c.text
            c.text = ""
            c.insert(0, permalink)
        else:
            c.append(permalink)

    def build_toc_div(self, toc_list: list) -> etree.Element:
        """ Return a string div given a toc list. """
        div = etree.Element("div")
        div.attrib["class"] = self.toc_class

        # Add title to the div
        if self.title:
            header = etree.SubElement(div, "span")
            if self.title_class:
                header.attrib["class"] = self.title_class
            header.text = self.title

        def build_etree_ul(toc_list: list, parent: etree.Element) -> etree.Element:
            ul = etree.SubElement(parent, "ul")
            for item in toc_list:
                # List item link, to be inserted into the toc div
                li = etree.SubElement(ul, "li")
                link = etree.SubElement(li, "a")
                link.text = item.get('name', '')
                link.attrib["href"] = '#' + item.get('id', '')
                if item['children']:
                    build_etree_ul(item['children'], li)
            return ul

        build_etree_ul(toc_list, div)

        if 'prettify' in self.md.treeprocessors:
            self.md.treeprocessors['prettify'].run(div)

        return div

    def _split_anchor_attribute(self, el: etree.Element) -> tuple[str, ...]:
        """ Read explicit legacy anchors declared on a heading.

        The attribute is whitespace separated and removed from the output,
        mirroring how `data-toc-label` is handled.
        """
        raw = el.attrib.get(self.anchor_attribute)
        if raw is None:
            return ()
        del el.attrib[self.anchor_attribute]
        return tuple(anchor for anchor in unescape(raw).split() if anchor)

    def _rewrite_fragment_links(self, doc: etree.Element, registry: AnchorRegistry) -> None:
        """ Point same-document fragment links at their resolved targets. """
        for el in doc.iter():
            if el.tag != 'a':
                continue
            href = el.attrib.get('href')
            if not href or not href.startswith('#'):
                continue
            fragment = unescape(href[1:])
            target = registry.resolve_fragment(fragment)
            if target is not None:
                el.attrib['href'] = '#' + target

    def run(self, doc: etree.Element) -> None:
        # Collect ids of non-heading elements. Heading ids are managed by the
        # registry itself so that a pre-existing id is not mistaken for a
        # collision with the heading carrying it.
        used_ids = set()
        headings = []
        for el in doc.iter():
            if isinstance(el.tag, str) and self.header_rgx.match(el.tag):
                headings.append(el)
            elif "id" in el.attrib:
                used_ids.add(el.attrib["id"])
        id_counters: dict[str, int] = {}
        registry = AnchorRegistry(
            used_ids, self.slugify, self.sep, id_counters, self.slug_mapping
        )

        # First pass: record headings and their evidence, reserving explicit
        # ids before any generated id can claim them.
        planned = []
        for el in headings:
            self.set_level(el)
            innerhtml = render_inner_html(remove_fnrefs(el), self.md)
            name = strip_tags(innerhtml)
            explicit_aliases = self._split_anchor_attribute(el)
            # The `attr_list` extension writes bare attributes with a
            # value of None. A valueless id is no id at all, so a slug
            # is generated for it like for any other id-less heading.
            explicit_id = el.attrib.get("id") or None
            entry = registry.plan(
                name, explicit_id=explicit_id, aliases=explicit_aliases
            )
            planned.append((el, entry, innerhtml, name))

        registry.finalize()
        self._rewrite_fragment_links(doc, registry)

        toc_tokens = []
        for el, entry, innerhtml, name in planned:
            el.attrib["id"] = entry['id']

            data_toc_label = ''
            if 'data-toc-label' in el.attrib:
                data_toc_label = run_postprocessors(unescape(el.attrib['data-toc-label']), self.md)
                # Overwrite name with sanitized value of `data-toc-label`.
                name = escape_cdata(strip_tags(data_toc_label))
                # Remove the data-toc-label attribute as it is no longer needed
                del el.attrib['data-toc-label']

            if int(el.tag[-1]) >= self.toc_top and int(el.tag[-1]) <= self.toc_bottom:
                toc_tokens.append({
                    'level': int(el.tag[-1]),
                    'id': unescape(el.attrib["id"]),
                    'name': name,
                    'html': innerhtml,
                    'data-toc-label': data_toc_label
                })

            if self.use_anchors:
                self.add_anchor(el, el.attrib["id"])
            if self.use_permalinks not in [False, None]:
                self.add_permalink(el, el.attrib["id"])

        toc_tokens = nest_toc_tokens(toc_tokens)
        div = self.build_toc_div(toc_tokens)
        if self.marker:
            self.replace_marker(doc, div)

        # serialize and attach to markdown instance.
        toc = self.md.serializer(div)
        for pp in self.md.postprocessors:
            toc = pp.run(toc)
        self.md.toc_tokens = toc_tokens
        self.md.toc = toc
        self.md.toc_anchor_aliases = dict(registry.aliases)
        self.md.toc_anchor_conflicts = list(registry.conflicts)


class TocExtension(Extension):
    """ Table of Contents Extension to Python-Markdown. """

    TreeProcessorClass = TocTreeprocessor

    def __init__(self, **kwargs):
        self.config = {
            'marker': [
                '[TOC]',
                'Text to find and replace with Table of Contents. Set to an empty string to disable. '
                'Default: `[TOC]`.'
            ],
            'title': [
                '', 'Title to insert into TOC `<div>`. Default: an empty string.'
            ],
            'title_class': [
                'toctitle', 'CSS class used for the title. Default: `toctitle`.'
            ],
            'toc_class': [
                'toc', 'CSS class(es) used for the link. Default: `toclink`.'
            ],
            'anchorlink': [
                False, 'True if header should be a self link. Default: `False`.'
            ],
            'anchorlink_class': [
                'toclink', 'CSS class(es) used for the link. Defaults: `toclink`.'
            ],
            'permalink': [
                0, 'True or link text if a Sphinx-style permalink should be added. Default: `False`.'
            ],
            'permalink_class': [
                'headerlink', 'CSS class(es) used for the link. Default: `headerlink`.'
            ],
            'permalink_title': [
                'Permanent link', 'Title attribute of the permalink. Default: `Permanent link`.'
            ],
            'permalink_leading': [
                False,
                'True if permalinks should be placed at start of the header, rather than end. Default: False.'
            ],
            'baselevel': ['1', 'Base level for headers. Default: `1`.'],
            'slugify': [
                slugify, 'Function to generate anchors based on header text. Default: `slugify`.'
            ],
            'separator': ['-', 'Word separator. Default: `-`.'],
            'toc_depth': [
                6,
                'Define the range of section levels to include in the Table of Contents. A single integer '
                '(b) defines the bottom section level (<h1>..<hb>) only. A string consisting of two digits '
                'separated by a hyphen in between (`2-5`) defines the top (t) and the bottom (b) (<ht>..<hb>). '
                'Default: `6` (bottom).'
            ],
            'slug_mapping': [
                {},
                'Mapping of legacy anchors (the slug a heading would naturally get) to the anchors which '
                'identify the new same-name sections inserted ahead of the originals. Legacy fragment links '
                'are rewritten to the resolved targets. Default: an empty mapping.'
            ],
            'anchor_attribute': [
                'data-toc-anchor',
                'Name of the header attribute (usable via `attr_list`) which explicitly declares one or '
                'more whitespace-separated legacy anchors for a heading. The attribute is removed from the '
                'rendered output. Set to an empty string to disable. Default: `data-toc-anchor`.'
            ],
        }
        """ Default configuration options. """

        super().__init__(**kwargs)

    def extendMarkdown(self, md):
        """ Register the processor.

        | Class Instance                                                 | Registry                                                         | Name   | Priority |
        | -------------------------------------------------------------- | ---------------------------------------------------------------- | ------ | :------: |
        | [`TocTreeprocessor`][markdown.extensions.toc.TocTreeprocessor] | [`treeprocessors`][markdown.treeprocessors.build_treeprocessors] | `toc` | `5` |

        """
        # flake8: noqa: E501 478-480
        md.registerExtension(self)
        self.md = md
        self.reset()
        tocext = self.TreeProcessorClass(md, self.getConfigs())
        md.treeprocessors.register(tocext, 'toc', 5)

    def reset(self) -> None:
        self.md.toc = ''
        self.md.toc_tokens = []
        self.md.toc_anchor_aliases = {}
        self.md.toc_anchor_conflicts = []


def makeExtension(**kwargs):  # pragma: no cover
    return TocExtension(**kwargs)
