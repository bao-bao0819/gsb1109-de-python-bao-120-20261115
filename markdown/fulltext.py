# Python Markdown

# A Python implementation of John Gruber's Markdown.

# Documentation: https://python-markdown.github.io/
# GitHub: https://github.com/Python-Markdown/markdown/
# PyPI: https://pypi.org/project/Markdown/

# License: BSD (see LICENSE.md for details).

"""
A searchable plain-text view of rendered Markdown.

The text produced here approximates what a browser displays.  Every visible
character carries the source range which produced it.  Separators introduced
between blocks and table cells are synthetic and carry no source range.

The visibility rules are explicit:

* Link labels and image alternative text are visible.  Link destinations,
  titles, and reference definitions are not.
* Code spans and code blocks are visible with their content taken literally.
* Raw HTML contributes the text of rendered elements.  Comments, processing
  instructions, declarations, and the content of ``script``, ``style``,
  ``textarea``, ``template``, ``head``, ``title``, and similar non-rendered
  elements are not visible.
* Character references are replaced by the character they denote.
* Markdown markers (emphasis delimiters, brackets, fences, list bullets,
  table pipes, etc.) are not visible.

Text which lies on both sides of an emphasis marker remains adjacent in the
visible text, so a query can match across the marker while the reported source
range still encloses it.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as etree
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Iterator, Sequence

from . import util
from . import inlinepatterns

if TYPE_CHECKING:  # pragma: no cover
    from markdown import Markdown


__all__ = [
    'SourceRange',
    'Span',
    'TextMatch',
    'IndexedText',
    'extract',
    'extract_text',
    'index',
    'build_index',
    'plain_text',
    'visible_text',
    'search',
]


HIDDEN_ELEMENTS: frozenset[str] = frozenset({
    'script', 'style', 'textarea', 'template', 'head', 'title', 'meta',
    'link', 'base', 'noscript', 'iframe', 'object', 'embed', 'param',
    'source', 'track', 'canvas', 'audio', 'video', 'map', 'area', 'col',
    'colgroup',
})
""" Elements whose content is not displayed. """

VOID_ELEMENTS: frozenset[str] = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
    'meta', 'param', 'source', 'track', 'wbr',
})
""" HTML void elements. """

BLOCK_ELEMENTS: frozenset[str] = frozenset({
    'address', 'article', 'aside', 'blockquote', 'details', 'div', 'dl',
    'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3',
    'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'main', 'menu', 'nav', 'ol',
    'p', 'pre', 'section', 'table', 'ul', 'li', 'dt', 'dd', 'tbody', 'thead',
    'tfoot', 'tr', 'td', 'th', 'caption', 'summary',
})
""" Elements which separate adjacent text in the visible view. """


_ENTITY_RE = re.compile(r'&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);')
_PLACEHOLDER_RE = re.compile(
    '(?P<html>{})|(?P<inline>{})|(?P<entity>{})'.format(
        util.HTML_PLACEHOLDER_RE.pattern,
        util.INLINE_PLACEHOLDER_RE.pattern,
        _ENTITY_RE.pattern,
    )
)
_HTML_TOKEN_RE = re.compile(inlinepatterns.HTML_RE, re.DOTALL)
_TAG_NAME_RE = re.compile(r'</?\s*([a-zA-Z][a-zA-Z0-9:-]*)')
_ATTR_RE = re.compile(
    r'''([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?''',
    re.DOTALL,
)
_WHITESPACE_RE = re.compile(r'[^\S\n]+')
_PUNCTUATION = set('\\`*_{}[]()#+-.!|>~<&')
_AUTOLINK_RE = re.compile(r'<([a-zA-Z][a-zA-Z0-9+.-]*://[^<>]*)>|<([^<> !]+@[^@<> ]+)>')
# Internal sentinels used by bundled extensions to defer substitutions.
_NBSP_SENTINEL_RE = re.compile('{0}qq[0-9]+zz{1}'.format(re.escape(util.STX), re.escape(util.ETX)))
_BACKLINK_SENTINEL_RE = re.compile('{0}zz[0-9]+qq{1}'.format(re.escape(util.STX), re.escape(util.ETX)))
_SENTINEL_RE = re.compile('{0}(?:qq[0-9]+zz|zz[0-9]+qq){1}'.format(
    re.escape(util.STX), re.escape(util.ETX)))


@dataclass(frozen=True, order=True)
class SourceRange:
    """ A half-open range of characters in the original Markdown source. """

    start: int
    """ Index of the first character. """

    end: int
    """ Index one past the last character. """

    def __len__(self) -> int:
        return self.end - self.start

    def __bool__(self) -> bool:
        return self.end > self.start


class Span(Sequence[SourceRange]):
    """
    The source ranges which produced one visible character.

    Usually a single range.  A character produced by expanding a character
    reference points at the whole reference; a character synthesized by the
    parser has no ranges at all.
    """

    __slots__ = ('ranges',)

    def __init__(self, ranges: Iterable[SourceRange] = ()) -> None:
        self.ranges: tuple[SourceRange, ...] = tuple(ranges)

    def __len__(self) -> int:
        return len(self.ranges)

    def __getitem__(self, item: int | slice):
        return self.ranges[item]

    def __iter__(self) -> Iterator[SourceRange]:
        return iter(self.ranges)

    def __bool__(self) -> bool:
        return bool(self.ranges)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Span):
            return self.ranges == other.ranges
        if isinstance(other, (tuple, list)):
            return self.ranges == tuple(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.ranges)

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.ranges!r})'

    @property
    def start(self) -> int | None:
        """ The smallest source offset, or ``None`` for a synthetic character. """
        return min((r.start for r in self.ranges), default=None)

    @property
    def end(self) -> int | None:
        """ The largest source offset, or ``None`` for a synthetic character. """
        return max((r.end for r in self.ranges), default=None)


@dataclass(frozen=True)
class TextMatch:
    """ A match in indexed visible text. """

    text: str
    """ The matched visible text. """

    start: int
    """ Index of the first matched visible character. """

    end: int
    """ Index one past the last matched visible character. """

    spans: tuple[Span, ...]
    """ One span for each matched visible character. """

    index: IndexedText
    """ The index which produced the match. """

    @property
    def source_range(self) -> SourceRange | None:
        """
        The source envelope of the match.

        Synthetic characters are ignored.  The envelope includes any markup
        between owned characters, so text spanning an emphasis marker reports
        the range from the first to the last source character.
        """
        starts = [r.start for span in self.spans for r in span]
        ends = [r.end for span in self.spans for r in span]
        if not starts:
            return None
        return SourceRange(min(starts), max(ends))

    @property
    def source_start(self) -> int | None:
        """ The first source offset of the match, if it has one. """
        return self.source_range.start if self.source_range else None

    @property
    def source_end(self) -> int | None:
        """ The end source offset of the match, if it has one. """
        return self.source_range.end if self.source_range else None

    @property
    def source_ranges(self) -> tuple[SourceRange, ...]:
        """ The source ranges of the match, in visible order. """
        return tuple(r for span in self.spans for r in span)

    @property
    def synthetic(self) -> bool:
        """ Whether the match includes any synthetic character. """
        return any(not span for span in self.spans)


@dataclass
class IndexedText:
    """ Visible text with a per-character map back to the Markdown source. """

    text: str
    """ The flattened visible text. """

    source: str
    """ The original Markdown source. """

    spans: list[Span] = field(default_factory=list)
    """ One span for each character of ``text``. """

    def __post_init__(self) -> None:
        if len(self.text) != len(self.spans):
            raise ValueError('spans must contain one entry per visible character')

    def __len__(self) -> int:
        return len(self.text)

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return f'{type(self).__name__}({self.text!r})'

    def __getitem__(self, item: int | slice):
        if isinstance(item, slice):
            return IndexedText(self.text[item], self.source, self.spans[item])
        return self.text[item]

    @property
    def source_range(self) -> SourceRange | None:
        """ The envelope of all owned visible characters. """
        starts = [r.start for span in self.spans for r in span]
        ends = [r.end for span in self.spans for r in span]
        if not starts:
            return None
        return SourceRange(min(starts), max(ends))

    def span(self, position: int) -> Span:
        """ The source span of one visible character. """
        return self.spans[position]

    def range(self, start: int, end: int) -> SourceRange | None:
        """ The source envelope of ``text[start:end]``. """
        starts = [r.start for span in self.spans[start:end] for r in span]
        ends = [r.end for span in self.spans[start:end] for r in span]
        if not starts:
            return None
        return SourceRange(min(starts), max(ends))

    def find(self, substring: str, start: int = 0, end: int | None = None) -> TextMatch | None:
        """ Find the first occurrence of ``substring``. """
        if end is None:
            end = len(self.text)
        found = self.text.find(substring, start, end)
        return None if found < 0 else self._match(found, found + len(substring))

    def rfind(self, substring: str, start: int = 0, end: int | None = None) -> TextMatch | None:
        """ Find the last occurrence of ``substring``. """
        if end is None:
            end = len(self.text)
        found = self.text.rfind(substring, start, end)
        return None if found < 0 else self._match(found, found + len(substring))

    def finditer(self, substring: str) -> Iterator[TextMatch]:
        """ Yield non-overlapping occurrences of ``substring``. """
        start = 0
        while True:
            found = self.find(substring, start)
            if found is None:
                return
            yield found
            start = found.start + 1

    def search(self, pattern: str | re.Pattern[str], flags: int = 0) -> Iterator[TextMatch]:
        """ Yield matches of a regular expression in the visible text. """
        compiled = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
        for found in compiled.finditer(self.text):
            yield self._match(found.start(), found.end())

    def _match(self, start: int, end: int) -> TextMatch:
        return TextMatch(self.text[start:end], start, end, tuple(self.spans[start:end]), self)


class _Builder:
    """ Accumulate visible characters and their source spans. """

    def __init__(self, source: str) -> None:
        self.source = source
        self.parts: list[str] = []
        self.spans: list[Span] = []
        self.pending: str | None = None

    def separator(self, kind: str = ' ') -> None:
        """ Request a separator before the next visible text. """
        if not self.parts:
            return
        last = self.parts[-1]
        if kind == ' ' and last.endswith('\n'):
            return
        if self.pending == '\n':
            return
        if kind == '\n' or not self.pending:
            self.pending = kind

    def linebreak(self) -> None:
        """ Insert a hard line break. """
        if not self.parts:
            return
        self.pending = None
        while self.parts and not self.parts[-1].strip():
            self.parts.pop()
            self.spans.pop()
        if not self.parts or self.parts[-1].endswith('\n'):
            return
        last = self.parts[-1].rstrip(' \t')
        if len(last) != len(self.parts[-1]):
            self.spans = self.spans[:len(self.spans) - (len(self.parts[-1]) - len(last))]
            self.parts[-1] = last
        self.parts.append('\n')
        self.spans.append(Span())

    def add(self, text: str, span: Span | Iterable[Span], preserve: bool = False) -> None:
        """ Append visible text and its spans. """
        if not text:
            return
        if isinstance(span, Span):
            spans = [span] * len(text)
        else:
            spans = list(span)
            if len(spans) != len(text):
                raise ValueError('one span per character is required')
        self._flush()
        if not preserve and self.parts and self.parts[-1][-1:].isspace():
            stripped = text.lstrip()
            removed = len(text) - len(stripped)
            if removed:
                text = stripped
                spans = spans[removed:]
        if not text:
            return
        self.parts.append(text)
        self.spans.extend(spans)

    def _flush(self) -> None:
        if not self.pending:
            return
        last = self.parts[-1] if self.parts else ''
        if last and not last[-1].isspace():
            self.parts.append(self.pending)
            self.spans.append(Span())
        self.pending = None

    def build(self) -> IndexedText:
        self.pending = None
        text = ''.join(self.parts)
        spans = list(self.spans)
        if not text:
            return IndexedText('', self.source, [])
        start = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        return IndexedText(text[start:end], self.source, spans[start:end])


def _tag_name(raw: str) -> str | None:
    m = _TAG_NAME_RE.match(raw)
    return m.group(1).lower() if m else None


def _attributes(raw: str) -> dict[str, str]:
    """ Return the attributes of a single raw HTML start tag. """
    if raw.startswith('</'):
        return {}
    body = raw[1:]
    if body.endswith('/>'):
        body = body[:-2]
    else:
        body = body[:-1]
    body = body.lstrip()
    m = re.match(r'[a-zA-Z][a-zA-Z0-9:-]*', body)
    if m:
        body = body[m.end():]
    attrs: dict[str, str] = {}
    for match in _ATTR_RE.finditer(body):
        name = match.group(1).lower()
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        if value is not None:
            attrs[name] = html.unescape(value)
    return attrs


class _Locator:
    """
    Find rendered text in the original source.

    A monotonic cursor is used because the rendered tree is traversed in source
    order.  Tag internals are skipped so that visible text is not attributed to
    an attribute value, with the exception of autolinks, whose displayed text
    is literally inside the angle brackets.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.position = 0
        self.tags = [(m.start(), m.end(), bool(_AUTOLINK_RE.fullmatch(m.group(0))))
                     for m in _HTML_TOKEN_RE.finditer(source)]
        self.definitions = [m.span() for m in re.finditer(
            r'(?m)^[ ]{0,3}\[[^\[\]\n]+\]:[^\n]*(?:\n[ ]+[^\n]*)*', source
        )]
        # Link destinations and titles are not visible; protect them so a
        # visible label or neighbouring word cannot be mis-attributed there.
        self.destinations = []
        for found in re.finditer(r'\]\((?:<[^<>]*>|[^()\n]*(?:\([^()\n]*\)[^()\n]*)*)', source):
            begin = source.find('(', found.start())
            if begin >= 0:
                self.destinations.append((begin, found.end()))

    def _skip(self, start: int, end: int) -> bool:
        for left, right in self.definitions:
            if left >= end:
                break
            if start < right and end > left:
                return True
        for left, right in self.destinations:
            if left >= end:
                break
            if start < right and end > left:
                return True
        for left, right, autolink in self.tags:
            if left >= end:
                break
            if start < right and end > left:
                return not autolink
        return False

    def raw(self, text: str) -> SourceRange | None:
        """ Locate an exact source token after the current cursor. """
        found = self.source.find(text, self.position)
        if found < 0:
            return None
        self.position = found + len(text)
        return SourceRange(found, self.position)

    def entity(self, raw: str, code: bool = False) -> SourceRange | None:
        """ Locate a character reference in the source. """
        found = self.source.find(raw, self.position)
        if found >= 0 and not self._skip(found, found + len(raw)):
            self.position = found + len(raw)
            return SourceRange(found, self.position)
        value = html.unescape(raw)
        if value and value.strip():
            spans = self.text(value, allow_tags=code)
            if spans:
                return spans[0][0]
        return None

    def alt(self, value: str, markdown: bool = False) -> list[Span] | None:
        """
        Locate the value of an ``alt`` attribute after the cursor.

        With ``markdown``, the value may be Markdown image alternative text,
        which is kept verbatim (including entities and markers) in the tree.
        """
        if not value:
            return []
        expression = (re.escape(value) if markdown else
                      r'''(?i)\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))''')
        if markdown:
            found = self.source.find(value, self.position)
            if found < 0:
                return None
            self.position = found + len(value)
            return [Span((SourceRange(found + i, found + i + 1),)) for i in range(len(value))]
        for match in re.finditer(expression, self.source[self.position:]):
            raw = match.group(1)
            group = 1
            if raw is None:
                raw = match.group(2)
                group = 2
            if raw is None:
                raw = match.group(3)
                group = 3
            if raw is None or html.unescape(raw) != value:
                continue
            start = self.position + match.start(group)
            self.position += match.end()
            return [Span((SourceRange(start + i, start + i + 1),)) for i in range(len(value))]
        return None

    def markdown_alt(self, value: str) -> int | None:
        """ Return the source offset of a Markdown image's alternative text. """
        found = self.source.find('![' + value + ']', self.position)
        if found < 0:
            return None
        start = found + 2
        self.position = start + len(value)
        return start

    def exact(self, text: str, allow_tags: bool = False) -> list[Span] | None:
        """ Locate ``text`` literally and map each character. """
        if not text:
            return []
        found = self.source.find(text, self.position)
        while found >= 0:
            if allow_tags or not self._skip(found, found + len(text)):
                self.position = found + len(text)
                return [Span((SourceRange(found + i, found + i + 1),)) for i in range(len(text))]
            found = self.source.find(text, found + 1)
        return None

    def code(self, text: str) -> list[Span] | None:
        """
        Locate code block content line by line.

        Fenced and indented code is not a literal substring of the source:
        fences, indentation, and a final newline are parser artifacts.  Match
        each line by its stripped content within a limited window so a block
        cannot be attributed to an unrelated similar block later in the file.
        """
        if not text:
            return []
        lines = text.split('\n')
        first = next((i for i, line in enumerate(lines) if line.strip()), None)
        if first is None:
            return None
        wanted = lines[first].strip()
        window_end = min(len(self.source), self.position + max(4000, len(text) * 4))
        cursor = self.position
        while True:
            found = self.source.find(wanted, cursor, window_end)
            if found < 0:
                return None
            newline = self.source.rfind('\n', self.position, found)
            line_start = newline + 1 if newline >= 0 else max(self.position, found)
            if self.source[line_start:found].strip():
                cursor = found + 1
                continue
            spans = self._code_lines(lines, line_start)
            if spans is not None:
                return spans
            cursor = found + 1

    def _code_lines(self, lines: list[str], start: int) -> list[Span] | None:
        spans: list[Span] = []
        position = start
        for line in lines:
            stripped = line.strip()
            if not stripped:
                newline = self.source.find('\n', position)
                if newline < 0:
                    return None
                if self.source[position:newline].strip():
                    return None
                spans.append(Span((SourceRange(newline, newline + 1),)))
                position = newline + 1
                continue
            found = self.source.find(stripped, position)
            if found < 0:
                return None
            prefix = self.source[position:found]
            if prefix.strip() or (prefix and '\n' in prefix):
                return None
            spans.extend(Span((SourceRange(found + i, found + i + 1),)) for i in range(len(stripped)))
            newline = self.source.find('\n', found + len(stripped))
            position = len(self.source) if newline < 0 else newline + 1
        while len(spans) < len('\n'.join(lines)):
            spans.append(Span())
        self.position = position
        return spans

    def text(self, text: str, allow_tags: bool = False) -> list[Span] | None:
        """
        Locate ``text`` and return one span per visible character.

        Whitespace runs and backslash escapes may differ from the source.
        With ``allow_tags``, HTML tag tokens may be matched; code content uses
        this because a code span can contain markup literally.  ``None`` means
        the fragment could not be placed.
        """
        if not text:
            return []
        pattern = []
        for char in text:
            if char.isspace():
                pattern.append(r'(\s+)')
            elif char in _PUNCTUATION:
                pattern.append(r'(\\?%s)' % re.escape(char))
            else:
                pattern.append('(%s)' % re.escape(char))
        expression = re.compile(''.join(pattern), re.DOTALL)

        for match in expression.finditer(self.source, self.position):
            if not allow_tags and self._skip(match.start(), match.end()):
                continue
            spans = []
            for index in range(1, len(text) + 1):
                group = match.group(index)
                if group is None:
                    spans = []
                    break
                start, end = match.span(index)
                spans.append(Span((SourceRange(start, end),)))
            if len(spans) == len(text):
                self.position = match.end()
                return spans
        return None


class _Extractor:
    """ Walk a rendered tree, applying the visibility rules. """

    def __init__(self, md: Markdown, source: str) -> None:
        self.md = md
        self.source = source
        self.locator = _Locator(source)
        self.builder = _Builder(source)
        self.hidden = 0
        self.raw_pre = 0

    def run(self, root: etree.Element) -> IndexedText:
        for child in root:
            self.element(child)
            if child.tail:
                self.text(child.tail)
        return self.builder.build()

    def element(self, el: etree.Element, preserve: bool = False, code: bool = False) -> None:
        tag = el.tag.lower() if isinstance(el.tag, str) else ''

        if tag in HIDDEN_ELEMENTS:
            return
        if tag in VOID_ELEMENTS:
            if tag == 'img':
                self.image(el)
            elif tag == 'br':
                self.builder.linebreak()
            elif tag == 'hr':
                self.builder.separator('\n')
            return

        if tag in ('td', 'th'):
            block = False
        elif tag in ('tr', 'thead', 'tbody', 'tfoot', 'table'):
            self.builder.separator('\n')
            block = True
        else:
            block = tag in BLOCK_ELEMENTS
            if block:
                self.builder.separator('\n')

        preserve = preserve or tag == 'pre'
        code = code or tag == 'code'
        if el.text:
            self.text(el.text, preserve=preserve, code=code)

        for child in el:
            self.element(child, preserve=preserve, code=code)
            if child.tail:
                self.text(child.tail, preserve=preserve, code=code)

        if block:
            self.builder.separator('\n')

    def image(self, el: etree.Element) -> None:
        alt = el.get('alt')
        if alt:
            self.alt_text(alt, markdown=True)

    @staticmethod
    def entity_value(raw: str) -> str:
        value = html.unescape(raw)
        return value if value else raw

    def alt_text(self, alt: str, markdown: bool = False) -> None:
        """
        Emit image alternative text.

        A raw HTML ``alt`` attribute is compared after entity decoding.  A
        Markdown image keeps its alternative text verbatim in the rendered
        tree, including character references and markup, so its value is
        located literally and entities are expanded as a browser would.
        """
        if not markdown:
            spans = self.locator.alt(alt)
            self.builder.add(alt, spans if spans is not None else Span())
            return

        pieces = [piece for piece in re.split('(%s)' % _ENTITY_RE.pattern, alt) if piece]
        if not pieces:
            return
        base = self.locator.markdown_alt(alt)
        mapped: list[Span] = []
        visible: list[str] = []
        position = base
        for piece in pieces:
            if _ENTITY_RE.fullmatch(piece):
                value = self.entity_value(piece)
                if base is not None and position is not None:
                    span = Span((SourceRange(position, position + len(piece)),))
                    position += len(piece)
                else:
                    found = self.locator.entity(piece)
                    span = Span((found,)) if found else Span()
                visible.append(value)
                mapped.extend([span] * len(value))
            else:
                if base is not None and position is not None:
                    spans = [Span((SourceRange(position + i, position + i + 1),))
                             for i in range(len(piece))]
                    position += len(piece)
                else:
                    spans = self.locator.text(piece, allow_tags=True)
                visible.append(piece)
                if spans is None:
                    mapped.extend([Span()] * len(piece))
                else:
                    mapped.extend(spans)
        self.builder.add(''.join(visible), mapped)

    def text(self, text: str, preserve: bool = False, code: bool = False,
             atomic: bool = False) -> None:
        """ Emit a text node, resolving placeholders and character references. """
        if not text:
            return
        atomic = atomic or isinstance(text, util.AtomicString)
        if self.raw_pre:
            preserve = True
            code = True
        if text.isspace() and not preserve:
            self.builder.separator(' ')
            return
        text = str(text)
        if util.STX in text and _SENTINEL_RE.search(text):
            text = self.sentinels(text)
        if atomic:
            self.literal(text, preserve=preserve, code=code, atomic=True)
            return
        if util.AMP_SUBSTITUTE in text:
            text = text.replace(util.AMP_SUBSTITUTE, '&')
        position = 0
        for found in _PLACEHOLDER_RE.finditer(text):
            if found.start() > position:
                self.literal(text[position:found.start()], preserve=preserve, code=code,
                             atomic=atomic)
            token = found.group(0)
            html_placeholder = token.startswith(util.HTML_PLACEHOLDER.split('%s')[0])
            inline_placeholder = (not html_placeholder and
                                  token.startswith(util.INLINE_PLACEHOLDER.split('%s')[0]))
            if html_placeholder:
                key = int(token[len(util.HTML_PLACEHOLDER.split('%s')[0]):-len(util.ETX)])
            elif inline_placeholder:
                key = int(token[len(util.INLINE_PLACEHOLDER.split('%s')[0]):-len(util.ETX)])
            if html_placeholder:
                self.placeholder(key)
            elif inline_placeholder:
                self.inline_placeholder(key)
            else:
                self.entity(token, code=code)
            position = found.end()
        if position < len(text):
            self.literal(text[position:], preserve=preserve, code=code, atomic=atomic)

    def sentinels(self, text: str) -> str:
        """ Resolve or drop deferred substitution markers from bundled extensions. """
        def substitute(found: re.Match[str]) -> str:
            value = found.group(0)
            if _NBSP_SENTINEL_RE.fullmatch(value):
                return ' '
            # A footnote back-reference link is navigation chrome rather than
            # document text, so its marker contributes nothing.
            return ''
        return _SENTINEL_RE.sub(substitute, text)

    def placeholder(self, key: int) -> None:
        """ Expand a stashed HTML fragment or character reference. """
        if key >= self.md.htmlStash.html_counter or key < 0:
            return
        raw = self.md.htmlStash.rawHtmlBlocks[key]
        if not isinstance(raw, str):
            raw = str(raw)
        if _ENTITY_RE.fullmatch(raw):
            self.entity(raw)
        elif raw.startswith('<'):
            self.html(raw)
        else:
            self.literal(raw)

    def inline_placeholder(self, key: int) -> None:
        """ Expand a stash entry which was replaced with an inline placeholder. """
        try:
            node = self.md.treeprocessors['inline'].stashed_nodes.get(str(key))
        except (AttributeError, KeyError):  # pragma: no cover - defensive
            return
        if node is None:
            return
        if isinstance(node, etree.Element):
            self.element(node)
        else:
            self.literal(str(node))

    def entity(self, raw: str, code: bool = False) -> None:
        value = html.unescape(raw)
        if not value:
            return
        if value.isspace():
            self.builder.separator(' ')
            return
        found = self.locator.entity(raw, code=code)
        span = found if isinstance(found, Span) else Span((found,)) if found else Span()
        self.builder.add(value, span)

    def html(self, raw: str) -> None:
        """ Process raw HTML, whether a single tag or a complete fragment. """
        if raw.startswith('<!--') or raw.startswith('<?') or raw.startswith('<!'):
            self.locator.raw(raw)
            return
        if _AUTOLINK_RE.fullmatch(raw):
            self.literal(raw[1:-1])
            return
        tokens = list(_HTML_TOKEN_RE.finditer(raw))
        if len(tokens) == 1 and tokens[0].group(0) == raw:
            self.tag(raw)
            return
        last = 0
        for token in tokens:
            if token.start() > last:
                self.raw_text(raw[last:token.start()])
            self.tag(token.group(0))
            last = token.end()
        if last < len(raw):
            self.raw_text(raw[last:])

    def tag(self, raw: str) -> None:
        """ Process one HTML tag. """
        name = _tag_name(raw)
        if name is None:
            self.locator.raw(raw)
            return
        closing = raw.startswith('</')
        self_closing = raw.rstrip().endswith('/>') or name in VOID_ELEMENTS

        if closing:
            if name in HIDDEN_ELEMENTS and self.hidden:
                self.hidden -= 1
            if name in ('pre', 'code', 'kbd', 'samp') and self.raw_pre:
                self.raw_pre -= 1
            self.locator.raw(raw)
            return

        if name in HIDDEN_ELEMENTS and not self_closing:
            self.hidden += 1
            self.locator.raw(raw)
            return
        if self.hidden:
            self.locator.raw(raw)
            return

        if name == 'img':
            alt = _attributes(raw).get('alt')
            if alt:
                self.alt_text(alt)
            return
        if name == 'br':
            self.builder.linebreak()
            self.locator.raw(raw)
            return
        if name in ('pre', 'code', 'kbd', 'samp'):
            self.raw_pre += 1
        elif name in BLOCK_ELEMENTS:
            self.builder.separator('\n')
        self.locator.raw(raw)

    def raw_text(self, text: str) -> None:
        """ Emit text scanned from a raw HTML fragment. """
        if not text or self.hidden:
            return
        position = 0
        for found in _ENTITY_RE.finditer(text):
            if found.start() > position:
                self.literal(text[position:found.start()], preserve=bool(self.raw_pre),
                             code=bool(self.raw_pre))
            self.entity(found.group(0), code=bool(self.raw_pre))
            position = found.end()
        if position < len(text):
            self.literal(text[position:], preserve=bool(self.raw_pre), code=bool(self.raw_pre))

    def literal(self, text: str, preserve: bool = False, code: bool = False,
                atomic: bool = False) -> None:
        """ Emit ordinary source text. """
        if not text or self.hidden:
            return
        if atomic and '&' in text:
            text = html.unescape(text)
        if preserve:
            text = text.replace('\r\n', '\n').replace('\r', '\n')
        elif '\n' in text:
            text = _WHITESPACE_RE.sub(' ', text)
        else:
            text = re.sub(r'[^\S\n]+', ' ', text)
        if not text:
            return
        if text.isspace() and not preserve:
            text = ' '
        spans = None
        if preserve and not self.raw_pre:
            spans = self.locator.code(text)
        if spans is None:
            spans = self.locator.exact(text, allow_tags=code) if preserve else None
        if spans is None and not preserve:
            spans = self.locator.text(text, allow_tags=code)
        if spans is None and preserve:
            spans = self.locator.text(text, allow_tags=code)
        if spans is None and preserve and not self.raw_pre:
            # A code block's final newline is synthesized by the parser.
            spans = self.locator.code(text.rstrip('\n'))
            if spans is None:
                spans = self.locator.exact(text.rstrip('\n'), allow_tags=code)
            if spans is None:
                spans = self.locator.text(text.rstrip('\n'), allow_tags=code)
            if spans is not None:
                text = text.rstrip('\n')
        if spans is None:
            self.builder.add(text, Span(), preserve=preserve)
        else:
            self.builder.add(text, spans, preserve=preserve)


def _render(md: Markdown, source: str) -> etree.Element:
    """ Run the parser through the tree processors and return the root. """
    md.reset()
    md.lines = source.split('\n')
    for prep in md.preprocessors:
        md.lines = prep.run(md.lines)
    root = md.parser.parseDocument(md.lines).getroot()
    for treeprocessor in md.treeprocessors:
        new_root = treeprocessor.run(root)
        if new_root is not None:
            root = new_root
    return root


def extract(source: str, md: Markdown | None = None) -> IndexedText:
    """
    Return a source-mapped visible-text view of ``source``.

    Arguments:
        source: Markdown source text.
        md: An optional [`Markdown`][markdown.Markdown] instance.  Its parser,
            extensions, and configuration are used.  A new instance with
            default configuration is created when omitted.
    """
    if md is None:
        from .core import Markdown
        md = Markdown()
    return _Extractor(md, source).run(_render(md, source))


extract_text = extract
""" Alias for [`extract`][markdown.fulltext.extract]. """

index = extract
""" Alias for [`extract`][markdown.fulltext.extract]. """

build_index = extract
""" Alias for [`extract`][markdown.fulltext.extract]. """


def plain_text(source: str, md: Markdown | None = None) -> str:
    """ Return only the visible text of ``source``. """
    return extract(source, md).text


visible_text = plain_text
""" Alias for [`plain_text`][markdown.fulltext.plain_text]. """


def search(
    source: str,
    query: str,
    md: Markdown | None = None,
    regex: bool = False,
) -> list[TextMatch]:
    """
    Search the visible text of ``source`` and return matches with source ranges.

    A plain query matches literally.  With ``regex=True``, ``query`` is a
    regular expression evaluated against the visible text.
    """
    visible = extract(source, md)
    if regex:
        return list(visible.search(query))
    return list(visible.finditer(query))
