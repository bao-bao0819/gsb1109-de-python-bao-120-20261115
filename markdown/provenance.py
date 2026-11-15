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
Optional fragment provenance tracking.

When enabled, the parser records which rules (preprocessors and postprocessors)
consumed or replaced fragments of the document during conversion. This is a
diagnostic aid for debugging conflicts between multiple extensions which process
the same markers. Tracking is purely observational: it never alters the output
of a conversion, so the output of a tracked ("diagnostic") run is identical to
the output of an untracked run.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class ProvenanceRecord:
    """
    A single fragment edit made by a rule during conversion.

    Attributes:
        rule (str): The registered name of the processor which made the edit.
        stage (str): The stage at which the edit was made (`preprocessor` or `postprocessor`).
        consumed (str): The fragment the rule removed from the document. Empty if
            the rule only inserted content.
        produced (str): The fragment the rule inserted in place of the consumed
            content. Empty if the rule only removed content.
        parents (list[ProvenanceRecord]): Earlier records (most recent first) whose
            produced fragments were consumed by this edit. Following the chain of
            parents traces a nested rewrite back to the original source fragment.

    """

    rule: str
    stage: str
    consumed: str
    produced: str
    parents: list[ProvenanceRecord] = field(default_factory=list)

    def __repr__(self):
        return (
            f'<ProvenanceRecord {self.stage}:{self.rule} '
            f'consumed={self.consumed!r} produced={self.produced!r}>'
        )


class FragmentProvenance:
    """
    Collects [`ProvenanceRecord`][markdown.provenance.ProvenanceRecord]s during a conversion.

    A record is only created when a rule actually alters the document. A rule
    which runs but leaves the content unchanged is not recorded, and a rule
    which never runs can never appear as a source.
    """

    def __init__(self):
        self.records: list[ProvenanceRecord] = []
        """ All recorded fragment edits, in the order they occurred. """

    def __iter__(self) -> Iterator[ProvenanceRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def reset(self) -> None:
        """ Clear all collected records. """
        self.records.clear()

    def record(self, stage: str, rule: str, before: str, after: str) -> None:
        """
        Compare the document before and after a rule ran and record each changed span.

        Arguments:
            stage: The stage at which the rule ran (`preprocessor` or `postprocessor`).
            rule: The registered name of the rule.
            before: The document text before the rule ran.
            after: The document text after the rule ran.

        If `before` and `after` are identical, nothing is recorded; a rule which
        changed nothing is not a source of any fragment.

        The comparison is done line by line, so each record describes a coherent
        block of consumed and produced text rather than single characters.
        """
        if before == after:
            return
        before_lines = before.split('\n')
        after_lines = after.split('\n')
        matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == 'equal':
                continue
            consumed = '\n'.join(before_lines[i1:i2])
            produced = '\n'.join(after_lines[j1:j2])
            parents = [
                r for r in reversed(self.records)
                if r.produced and self._shares_fragment(r.produced, consumed)
            ]
            self.records.append(ProvenanceRecord(rule, stage, consumed, produced, parents))

    @staticmethod
    def _shares_fragment(produced: str, consumed: str) -> bool:
        """
        Return `True` if any non-whitespace line of a previously produced
        fragment is found within the newly consumed text.

        Whitespace-only fragments are ignored: they are never a meaningful
        source of content.
        """
        return any(
            line and line in consumed
            for line in (line.strip() for line in produced.split('\n'))
        )

    def trace(self, fragment: str) -> list[ProvenanceRecord]:
        """
        Trace a fragment back through the chain of nested rewrites.

        Arguments:
            fragment: A fragment of the final output.

        Returns:
            A list of [`ProvenanceRecord`][markdown.provenance.ProvenanceRecord]s,
            starting with the rule which most recently produced the fragment and
            ending with the rule which consumed the original source text. Returns
            an empty list if no recorded rule produced the fragment.
        """
        chain = []
        record = self._find_producer(fragment)
        while record is not None:
            chain.append(record)
            record = record.parents[0] if record.parents else None
        return chain

    def origin(self, fragment: str) -> str | None:
        """
        Return the original source fragment a final output fragment derives from.

        Arguments:
            fragment: A fragment of the final output.

        Returns:
            The fragment consumed by the earliest rule in the chain of nested
            rewrites, or `None` if no recorded rule produced the fragment.
        """
        chain = self.trace(fragment)
        return chain[-1].consumed if chain else None

    def _find_producer(self, fragment: str) -> ProvenanceRecord | None:
        """ Return the most recent record which produced the given fragment. """
        for record in reversed(self.records):
            if record.produced and (fragment in record.produced or record.produced in fragment):
                return record
        return None

    def report(self) -> str:
        """ Return a human-readable summary of all recorded fragment edits. """
        lines = []
        for record in self.records:
            lines.append(f'{record.stage}:{record.rule}')
            if record.consumed:
                lines.append(f'  consumed: {record.consumed!r}')
            if record.produced:
                lines.append(f'  produced: {record.produced!r}')
            for parent in record.parents:
                lines.append(f'  rewrites fragment from: {parent.stage}:{parent.rule}')
        return '\n'.join(lines)
