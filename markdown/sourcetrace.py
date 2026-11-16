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
Optional source tracking for diagnosing which rules consume or replace content.

When a [`Markdown`][markdown.Markdown] instance is created with
`source_tracking=True`, every rule that actually fires during a call to
[`Markdown.convert`][markdown.Markdown.convert] appends a
[`TraceRecord`][markdown.sourcetrace.TraceRecord] to the instance's
[`SourceTrace`][markdown.sourcetrace.SourceTrace] (available as `Markdown.trace`).
The records describe the source fragment each rule received (`snippet`), what it
produced in its place (`output`) and, for nested rewrites, the enclosing fragment
it was derived from (`original`).

Tracking is observational only: the converted output is identical with or
without it, and rules that do not fire never generate records.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:  # pragma: no cover
    from .util import Registry


class TraceRecord(NamedTuple):
    """
    A single record of a rule consuming or replacing a source fragment.

    Attributes:
        stage: The pipeline stage the rule belongs to. One of the stage constants
            defined on [`SourceTrace`][markdown.sourcetrace.SourceTrace].
        rule: The name under which the rule is registered in its processor
            registry (for example `em_strong` or `hashheader`).
        snippet: The exact source fragment handed to the rule.
        output: What the rule produced in place of the fragment. An empty string
            means the fragment was consumed without producing output.
        original: The enclosing fragment from which `snippet` was derived when a
            rule fired inside another rule's output, otherwise `None`. Follow the
            chain with [`SourceTrace.lineage`][markdown.sourcetrace.SourceTrace.lineage]
            to recover the outermost source fragment.
        parent: Index of the record established by the enclosing rewrite, or
            `None` for a top-level record.
    """

    stage: str
    rule: str
    snippet: str
    output: str
    original: str | None
    parent: int | None


class SourceTrace:
    """
    Collects [`TraceRecord`][markdown.sourcetrace.TraceRecord] instances for one
    [`Markdown`][markdown.Markdown] instance.

    The records are appended in execution order. A new conversion starts with an
    empty collection because [`Markdown.reset`][markdown.Markdown.reset] calls
    [`reset`][markdown.sourcetrace.SourceTrace.reset].

    Rules which may parse nested content while they run (block processors and
    inline patterns) bracket their work with
    [`begin`][markdown.sourcetrace.SourceTrace.begin] and
    [`finish`][markdown.sourcetrace.SourceTrace.finish], so rules fired by the
    nested parsing are linked to the enclosing record automatically. Rules with
    no nested parsing use [`add`][markdown.sourcetrace.SourceTrace.add].
    """

    PREPROCESS: str = 'preprocess'
    """ Line-oriented preprocessors. """
    BLOCK: str = 'block'
    """ Block processors. """
    INLINE: str = 'inline'
    """ Inline patterns. """
    TREE: str = 'tree'
    """ Tree processors. """
    POSTPROCESS: str = 'postprocess'
    """ Postprocessors. """

    def __init__(self):
        self.records: list[TraceRecord] = []
        self._origin_stack: list[int] = []

    def reset(self) -> None:
        """ Remove all records and nested-rewrite context. """
        self.records.clear()
        self._origin_stack.clear()

    @property
    def current_origin(self) -> str | None:
        """ The enclosing fragment currently being rewritten, if any. """
        if self._origin_stack:
            return self.records[self._origin_stack[-1]].snippet
        return None

    def begin(self, stage: str, rule: str, snippet: str) -> int:
        """
        Open a record for a rule about to run and return its index.

        Nested rules fired before the matching
        [`finish`][markdown.sourcetrace.SourceTrace.finish] call link their records
        to this one, capturing the nested-rewrite chain.
        """
        parent = self._origin_stack[-1] if self._origin_stack else None
        original = self.records[parent].snippet if parent is not None else None
        index = len(self.records)
        self.records.append(TraceRecord(stage, rule, snippet, '', original, parent))
        self._origin_stack.append(index)
        return index

    def finish(self, index: int, output: str) -> None:
        """
        Close the record at `index`, storing the output the rule produced.

        A rule which ultimately rejects its match instead calls
        [`discard`][markdown.sourcetrace.SourceTrace.discard], so rejected rules
        never appear as sources.
        """
        self.records[index] = self.records[index]._replace(output=output)
        if self._origin_stack and self._origin_stack[-1] == index:
            self._origin_stack.pop()

    def discard(self, index: int) -> None:
        """
        Remove a record opened with [`begin`][markdown.sourcetrace.SourceTrace.begin].

        Used when a rule rejects a match instead of consuming it, so rejected rules
        never appear as sources. If nested records were appended before the rule
        declined (possible for third-party rules), the record is kept and closed
        with an empty output rather than orphaning its nested records.
        """
        if self._origin_stack and self._origin_stack[-1] == index:
            self._origin_stack.pop()
        if index == len(self.records) - 1:
            self.records.pop()
        else:  # pragma: no cover - defensive, no built-in rule reaches this
            self.records[index] = self.records[index]._replace(output='')

    def add(
        self,
        stage: str,
        rule: str,
        snippet: str,
        output: str = '',
    ) -> int:
        """
        Append a complete record for a rule with no nested parsing of its own.

        Returns the index of the new record. The current nested-rewrite context,
        if any, is still honored through the record's `parent` link.
        """
        index = self.begin(stage, rule, snippet)
        self.finish(index, output)
        return index

    def for_stage(self, stage: str) -> list[TraceRecord]:
        """ Return the records collected for a pipeline stage. """
        return [record for record in self.records if record.stage == stage]

    def for_rule(self, rule: str) -> list[TraceRecord]:
        """ Return the records collected for a rule name. """
        return [record for record in self.records if record.rule == rule]

    def lineage(self, record: TraceRecord) -> list[str]:
        """
        Return the nested-rewrite chain for `record`, outermost fragment first.

        The chain is built from the `parent` links of the record and its enclosing
        rewrites, so it remains reliable even when different fragments share text.
        """
        fragments = [record.snippet]
        parent = record.parent
        while parent is not None:
            parent_record = self.records[parent]
            fragments.append(parent_record.snippet)
            parent = parent_record.parent
        fragments.reverse()
        return fragments


def registry_name(registry: Registry[Any], item: Any) -> str:
    """
    Return the registration name of `item` in `registry`.

    Processor registries do not otherwise expose a reverse lookup from instance to
    name. Fall back to the item's class name if it is not registered, which keeps
    a diagnostic useful for rules added without a tracked registry.
    """
    for entry in registry._priority:
        if registry._data[entry.name] is item:
            return entry.name
    return item.__class__.__name__
