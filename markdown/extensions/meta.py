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
        yaml_style = False
        consumed: list[str] = []
        if lines and BEGIN_RE.match(lines[0]):
            yaml_style = True
            consumed.append(lines.pop(0))
        terminated = not yaml_style
        while lines:
            line = lines.pop(0)
            m1 = META_RE.match(line)
            if line.strip() == '' or END_RE.match(line):
                terminated = True
                break  # blank line or end of YAML header - done
            if m1:
                key = m1.group('key').lower().strip()
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
                else:
                    lines.insert(0, line)
                    break  # no meta data - done
            consumed.append(line)
        if yaml_style and not terminated:
            # Incomplete YAML header: restore the original lines so the
            # body is passed through with its original bytes unchanged.
            lines[:0] = consumed
            meta = {}
        self.md.Meta = meta
        return lines


def makeExtension(**kwargs):  # pragma: no cover
    return MetaExtension(**kwargs)
