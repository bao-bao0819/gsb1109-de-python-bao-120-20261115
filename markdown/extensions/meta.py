# Meta Data Extension for Python-Markdown
# =======================================

# This extension adds Meta Data handling to markdown.

# See https://Python-Markdown.github.io/extensions/meta_data
# for documentation.

# Original code Copyright 2007-2008 [Waylan Limberg](https://github.com/waylan).

# All changes Copyright 2008-2014 The Python Markdown Project

# License: [BSD](https://opensource.org/licenses/bsd-license.php)

"""
This extension adds Meta Data handling to markdown.

See the [documentation](https://Python-Markdown.github.io/extensions/meta_data)
for details.
"""

from __future__ import annotations

from . import Extension
from ..preprocessors import Preprocessor
import re
import logging
from typing import Any

log = logging.getLogger('MARKDOWN')

# Global Vars
META_RE = re.compile(r'^[ ]{0,3}(?P<key>[A-Za-z0-9_-]+):\s*(?P<value>.*)')
META_MORE_RE = re.compile(r'^[ ]{4,}(?P<value>.*)')
BEGIN_RE = re.compile(r'^-{3}(\s.*)?')
END_RE = re.compile(r'^(-{3}|\.{3})(\s.*)?')


class MetaExtension (Extension):
    """ Meta-Data extension for Python-Markdown. """

    def extendMarkdown(self, md):
        """ Register the processor.

        | Class Instance                                                  | Registry                                                      | Name   | Priority |
        | --------------------------------------------------------------- | ------------------------------------------------------------- | ------ | :------: |
        | [`MetaPreprocessor`][markdown.extensions.meta.MetaPreprocessor] | [`preprocessors`][markdown.preprocessors.build_preprocessors] | `meta` | `27` |

        """
        # flake8: noqa: E501 45-47
        md.registerExtension(self)
        self.md = md
        md.preprocessors.register(MetaPreprocessor(md), 'meta', 27)

    def reset(self) -> None:
        self.md.Meta = {}


class MetaPreprocessor(Preprocessor):
    """ Get Meta-Data. """

    def run(self, lines: list[str]) -> list[str]:
        """ Parse Meta-Data and store in Markdown.Meta. """
        meta: dict[str, Any] = {}
        key = None

        in_yaml = bool(lines and BEGIN_RE.match(lines[0]))
        # Index of the first header line. The YAML opening delimiter is only
        # consumed once the header proves to be complete.
        start = 1 if in_yaml else 0
        # Exclusive end of the header; `None` means it runs to end of input.
        end = None

        index = start
        while index < len(lines):
            line = lines[index]
            if line.strip() == '':
                if in_yaml:
                    # A blank line before a closing delimiter means the YAML
                    # header is incomplete, so no line is consumed.
                    self.md.Meta = {}
                    return lines
                # The first blank line ends the header and stays in the body.
                end = index
                break
            if END_RE.match(line):
                # An end delimiter closes the header whether or not an
                # opening delimiter was used, and is itself consumed.
                end = index + 1
                break
            m1 = META_RE.match(line)
            if m1:
                key = m1.group('key')
            elif not (META_MORE_RE.match(line) and key):
                if in_yaml:
                    # Inside YAML delimiters only fields and continuations are
                    # legal, so an unrecognized line means an incomplete header.
                    self.md.Meta = {}
                    return lines
                # Without delimiters the header simply stops here; this line
                # and everything after it remain part of the body.
                end = index
                break
            index += 1
        else:
            if in_yaml:
                # Reached end of input without a closing delimiter.
                self.md.Meta = {}
                return lines

        header = lines[start:end if end is not None else len(lines)]
        body = lines[end if end is not None else len(lines):]

        for line in header:
            m1 = META_RE.match(line)
            if m1:
                key = m1.group('key').lower().strip()
                # A field with nothing after the colon is an explicit empty
                # value; it is stored as its own entry and never folded into
                # a continuation line.
                value = m1.group('value').strip()
                try:
                    meta[key].append(value)
                except KeyError:
                    meta[key] = [value]
            else:
                m2 = META_MORE_RE.match(line)
                if m2 and key:
                    # Add another line to existing key
                    meta[key].append(m2.group('value').strip())

        self.md.Meta = meta
        return body


def makeExtension(**kwargs):  # pragma: no cover
    return MetaExtension(**kwargs)
