"""Budgeted summarization of a Markdown knowledge base.

Parses a Markdown document into a section tree, attributes every body
fragment (paragraph, code block, footnote definition) to exactly one
section, distributes a limited word budget across a requested set of
sections by weight, and greedily selects whole paragraphs per section.

Guarantees:
- Each fragment counts at most once toward the global total.
- Paragraphs are atomic: a paragraph is selected whole or not at all.
- Code blocks and footnotes are counted per configurable rules.
- A parent section never double-counts descendants that have their own
  budget; rolled-up fragments are consumed globally at most once.
- Ties in priority and word count resolve deterministically (document
  order), and every skipped fragment records a reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CODE_MODES = ('exclude', 'none', 'words', 'lines')
FOOTNOTE_MODES = ('exclude', 'none', 'words')

REASON_OVER_BUDGET = 'over_budget'
REASON_ALREADY_COUNTED = 'already_counted'
REASON_EXCLUDED_KIND = 'excluded_kind'


@dataclass(frozen=True)
class CountingRules:
    """How code blocks and footnote definitions count toward the budget.

    code / footnotes modes:
      'exclude' - not a candidate for summaries at all
      'none'    - candidate, counts as 0 words
      'words'   - candidate, counted by words
      'lines'   - (code only) candidate, counted by non-blank lines
    """
    code: str = 'none'
    footnotes: str = 'none'

    def __post_init__(self):
        if self.code not in CODE_MODES:
            raise ValueError(f'invalid code mode: {self.code!r}')
        if self.footnotes not in FOOTNOTE_MODES:
            raise ValueError(f'invalid footnotes mode: {self.footnotes!r}')


_CJK_RE = re.compile(
    '[一-鿿㐀-䶿豈-﫿぀-ヿ가-힯]'
)
_FOOTNOTE_REF_RE = re.compile(r'\[\^[^\]]+\]')
_WORD_CHAR_RE = re.compile(r'\w', re.UNICODE)


def count_words(text: str) -> int:
    """Count words: each CJK character is one word; other words are
    whitespace-separated tokens containing at least one word character.
    Footnote references (``[^id]``) are ignored."""
    text = _FOOTNOTE_REF_RE.sub(' ', text)
    cjk = _CJK_RE.findall(text)
    rest = _CJK_RE.sub(' ', text)
    tokens = [t for t in rest.split() if _WORD_CHAR_RE.search(t)]
    return len(cjk) + len(tokens)


@dataclass
class Block:
    """An atomic body fragment. Paragraphs are never split."""
    id: int
    kind: str  # 'paragraph' | 'code' | 'footnote'
    text: str
    line: int
    words: int
    priority: int = 0
    candidate: bool = True


@dataclass
class Section:
    id: str
    title: str
    level: int
    blocks: list[Block] = field(default_factory=list)
    children: list['Section'] = field(default_factory=list)
    parent: 'Section | None' = None

    def own_words(self) -> int:
        """Words attributed to this section only (never descendants)."""
        return sum(b.words for b in self.blocks)

    def flatten(self) -> list['Section']:
        out = [self]
        for child in self.children:
            out.extend(child.flatten())
        return out


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_FENCE_RE = re.compile(r'^\s*(`{3,}|~{3,})')
_FOOTNOTE_DEF_RE = re.compile(r'^\[\^[^\]]+\]:')
_INDENT_RE = re.compile(r'^(?:    |\t)')


def _slugify(title: str, used: dict[str, int]) -> str:
    slug = re.sub(r'[^\w一-鿿-]+', '-', title.strip().lower()).strip('-') or 'section'
    n = used.get(slug, 0)
    used[slug] = n + 1
    return slug if n == 0 else f'{slug}-{n}'


def _block_words(kind: str, text: str, rules: CountingRules) -> tuple[int, bool]:
    if kind == 'paragraph':
        return count_words(text), True
    if kind == 'code':
        mode = rules.code
        if mode == 'exclude':
            return 0, False
        if mode == 'none':
            return 0, True
        if mode == 'lines':
            return sum(1 for ln in text.splitlines() if ln.strip()), True
        return count_words(text), True
    # footnote
    mode = rules.footnotes
    if mode == 'exclude':
        return 0, False
    if mode == 'none':
        return 0, True
    return count_words(text), True


def parse_document(text: str, rules: CountingRules = CountingRules()) -> Section:
    """Parse Markdown source into a section tree with explicit attribution.

    Every fragment belongs to the nearest preceding heading (or to the
    synthetic ``root`` section for content before the first heading).
    """
    root = Section(id='root', title='', level=0)
    used_slugs: dict[str, int] = {'root': 1}
    stack = [root]
    next_id = [0]

    def add_block(kind: str, block_text: str, line: int) -> None:
        words, candidate = _block_words(kind, block_text, rules)
        # Explicit attribution: the fragment belongs to the section that
        # is current at parse time, and to no other section.
        stack[-1].blocks.append(Block(id=next_id[0], kind=kind,
                                      text=block_text, line=line,
                                      words=words, candidate=candidate))
        next_id[0] += 1

    paragraph: list[str] = []
    paragraph_line = 0

    def flush_paragraph() -> None:
        nonlocal paragraph, paragraph_line
        if paragraph:
            add_block('paragraph', '\n'.join(paragraph), paragraph_line)
            paragraph = []

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)
            start = i
            i += 1
            body = []
            while i < len(lines):
                closing = _FENCE_RE.match(lines[i])
                if closing and closing.group(1)[0] == marker[0] \
                        and len(closing.group(1)) >= len(marker):
                    break
                body.append(lines[i])
                i += 1
            i += 1  # skip closing fence (or EOF)
            add_block('code', '\n'.join(body), start + 1)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            section = Section(id=_slugify(title, used_slugs), title=title,
                              level=level)
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            section.parent = stack[-1]
            stack[-1].children.append(section)
            stack.append(section)
            i += 1
            continue
        if _FOOTNOTE_DEF_RE.match(line):
            flush_paragraph()
            start = i
            body = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and _INDENT_RE.match(lines[i]):
                body.append(lines[i])
                i += 1
            add_block('footnote', '\n'.join(body), start + 1)
            continue
        if not line.strip():
            flush_paragraph()
            i += 1
            continue
        if _INDENT_RE.match(line) and not paragraph:
            flush_paragraph()
            start = i
            body = []
            while i < len(lines) and (not lines[i].strip()
                                      or _INDENT_RE.match(lines[i])):
                if lines[i].strip():
                    body.append(lines[i].strip())
                i += 1
            add_block('code', '\n'.join(body), start + 1)
            continue
        if not paragraph:
            paragraph_line = i + 1
        paragraph.append(line)
        i += 1
    flush_paragraph()
    return root


def allocate_budget(sections: list[Section], total_budget: float,
                    weights: dict[str, float] | None = None) -> dict[str, float]:
    """Distribute ``total_budget`` across sections by weight.

    Weight resolution per section: explicit ``weights`` entry (by id or
    title) if present, otherwise the section's own attributed word count
    (descendants never count toward a parent's weight, so a parent and
    its children never double-occupy quota). If every weight is zero the
    budget is split equally.
    """
    weights = weights or {}
    raw: dict[str, float] = {}
    for section in sections:
        if section.id in weights:
            weight = weights[section.id]
        elif section.title and section.title in weights:
            weight = weights[section.title]
        else:
            weight = float(section.own_words())
        raw[section.id] = max(float(weight), 0.0)
    total_weight = sum(raw.values())
    if total_weight <= 0:
        share = total_budget / len(sections) if sections else 0.0
        return {s.id: share for s in sections}
    return {s.id: total_budget * raw[s.id] / total_weight for s in sections}


def _collect_candidates(section: Section, requested_ids: set[str],
                        rollup: bool) -> list[Block]:
    candidates = list(section.blocks)
    if rollup:
        for child in section.children:
            if child.id in requested_ids:
                continue  # child has its own budget; never double-count
            candidates.extend(_collect_candidates(child, requested_ids, rollup))
    return candidates


@dataclass
class SkippedBlock:
    block: Block
    reason: str


@dataclass
class SectionSummary:
    section: Section
    budget: float
    selected: list[Block] = field(default_factory=list)
    skipped: list[SkippedBlock] = field(default_factory=list)

    @property
    def used(self) -> int:
        return sum(b.words for b in self.selected)

    def to_dict(self) -> dict:
        return {
            'id': self.section.id,
            'title': self.section.title,
            'budget': round(self.budget, 4),
            'used': self.used,
            'selected': [
                {'id': b.id, 'kind': b.kind, 'line': b.line,
                 'words': b.words, 'text': b.text}
                for b in sorted(self.selected, key=lambda b: b.id)
            ],
            'skipped': [
                {'id': s.block.id, 'kind': s.block.kind, 'line': s.block.line,
                 'words': s.block.words, 'reason': s.reason,
                 'text': s.block.text}
                for s in sorted(self.skipped, key=lambda s: s.block.id)
            ],
        }


@dataclass
class SummaryResult:
    total_budget: float
    sections: list[SectionSummary]

    @property
    def total_used(self) -> int:
        return sum(s.used for s in self.sections)

    def to_dict(self) -> dict:
        return {
            'total_budget': self.total_budget,
            'total_used': self.total_used,
            'sections': [s.to_dict() for s in self.sections],
        }


def _select(candidates: list[Block], budget: float,
            consumed: set[int]) -> tuple[list[Block], list[SkippedBlock]]:
    """Greedy whole-paragraph selection.

    Order: higher priority first, then fewer words (fits more fragments),
    then document order — so equal priority and equal word count always
    yields the same stable choice.
    """
    selected: list[Block] = []
    skipped: list[SkippedBlock] = []
    remaining = budget
    for block in sorted(candidates, key=lambda b: (-b.priority, b.words, b.id)):
        if not block.candidate:
            skipped.append(SkippedBlock(block, REASON_EXCLUDED_KIND))
        elif block.id in consumed:
            skipped.append(SkippedBlock(block, REASON_ALREADY_COUNTED))
        elif block.words <= remaining:
            selected.append(block)
            consumed.add(block.id)
            remaining -= block.words
        else:
            skipped.append(SkippedBlock(block, REASON_OVER_BUDGET))
    return selected, skipped


def summarize(text: str, total_budget: float,
              sections: list[str] | None = None,
              weights: dict[str, float] | None = None,
              priorities: dict[str, int] | None = None,
              rules: CountingRules = CountingRules(),
              rollup: bool = True) -> SummaryResult:
    """Produce per-section summaries of ``text`` within ``total_budget``.

    ``sections`` (ids or titles) picks which sections get a budget;
    defaults to every section that owns or contains content. ``weights``
    and ``priorities`` are keyed by section id or title.
    """
    root = parse_document(text, rules)
    all_sections = [s for s in root.flatten()
                    if s is not root or s.blocks]
    if sections is not None:
        wanted = set(sections)
        requested = [s for s in all_sections
                     if s.id in wanted or s.title in wanted]
        missing = wanted - {s.id for s in requested} - {s.title for s in requested}
        if missing:
            raise ValueError(f'unknown sections: {sorted(missing)}')
    else:
        requested = [s for s in all_sections
                     if s.blocks or s.children]
    priorities = priorities or {}
    for section in all_sections:
        priority = priorities.get(section.id,
                                  priorities.get(section.title, 0))
        for block in section.blocks:
            block.priority = priority
    budgets = allocate_budget(requested, total_budget, weights)
    requested_ids = {s.id for s in requested}
    consumed: set[int] = set()
    summaries: list[SectionSummary] = []
    for section in requested:
        candidates = _collect_candidates(section, requested_ids, rollup)
        selected, skipped = _select(candidates, budgets[section.id], consumed)
        summaries.append(SectionSummary(section=section,
                                        budget=budgets[section.id],
                                        selected=selected,
                                        skipped=skipped))
    return SummaryResult(total_budget=total_budget, sections=summaries)
