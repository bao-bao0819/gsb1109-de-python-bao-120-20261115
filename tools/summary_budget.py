"""Hierarchy-aware, budgeted extractive summaries.

The unit of selection is a whole Markdown block (a paragraph, a fenced code
block, or a footnote definition).  A block is never split.  Each selected
block is charged to exactly one chapter, so duplicated body text, or text
which appears in both a parent and a child chapter, cannot consume the budget
twice.

The public entry points are:

``parse_markdown``
    Split a Markdown document into chapters and selectable blocks.
``plan``
    Allocate a finite word budget by chapter weight and select blocks.
``render_markdown`` / ``plan.to_dict``
    Render the selected blocks or a machine-readable audit report.

A JSON configuration is accepted by the command line interface.  See
``python -m tools.summary_budget --help``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Sequence


CountUnit = Literal["words", "chars"]
BlockUnit = Literal["words", "chars", "lines", "none"]


@dataclass(frozen=True)
class CountPolicy:
    """How each kind of block contributes to the budget.

    ``unit`` applies to prose.  ``code_unit`` and ``footnote_unit`` make the
    treatment of code and footnotes explicit rather than implicit.  Fence
    markers, code info strings, and footnote markers are never counted.
    Footnote references in prose are not counted as separate words unless
    ``count_footnote_references`` is enabled.
    """

    unit: CountUnit = "words"
    code_unit: BlockUnit = "lines"
    footnote_unit: BlockUnit = "words"
    count_footnote_references: bool = False
    cjk_as_chars: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CountPolicy":
        if not data:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"Unknown count policy field(s): {', '.join(sorted(unknown))}")
        return cls(**data)


@dataclass
class Chapter:
    id: str
    title: str = ""
    parent_id: str | None = None
    weight: float = 1.0
    self_weight: float = 1.0
    order: int | None = None
    heading_level: int | None = None
    synthetic: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chapter":
        try:
            chapter_id = str(data["id"])
        except KeyError as exc:
            raise ValueError("Every chapter needs an 'id'") from exc
        return cls(
            id=chapter_id,
            title=str(data.get("title", chapter_id)),
            parent_id=(data.get("parent_id", data.get("parent")) or None),
            weight=float(data.get("weight", 1.0)),
            self_weight=float(data.get("self_weight", 1.0)),
            order=None if data.get("order") is None else int(data["order"]),
            heading_level=None if data.get("heading_level") is None else int(data["heading_level"]),
            synthetic=bool(data.get("synthetic", False)),
        )


@dataclass
class Fragment:
    id: str
    chapter_id: str
    text: str
    kind: str = "prose"
    priority: int = 0
    order: int | None = None
    count: int | None = None
    dedupe_key: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fragment":
        try:
            fragment_id = str(data["id"])
            chapter_id = str(data.get("chapter_id", data.get("chapter")))
        except KeyError as exc:
            raise ValueError("Every fragment needs 'id' and 'chapter'/'chapter_id'") from exc
        count = data.get("count")
        return cls(
            id=fragment_id,
            chapter_id=chapter_id,
            text=str(data.get("text", "")),
            kind=str(data.get("kind", "prose")),
            priority=int(data.get("priority", 0)),
            order=None if data.get("order") is None else int(data["order"]),
            count=None if count is None else int(count),
            dedupe_key=data.get("dedupe_key"),
            meta=dict(data.get("meta", {})),
        )


@dataclass
class Decision:
    fragment_id: str
    chapter_id: str
    kind: str
    words: int
    charged_words: int
    priority: int
    selected: bool
    reason: str
    detail: str
    canonical_id: str | None = None


@dataclass
class ChapterPlan:
    chapter: Chapter
    depth: int
    quota: int
    own_quota: int
    own_words: int
    subtree_words: int
    selected_fragment_ids: list[str]
    unused_own: int
    heading_level: int | None = None


@dataclass
class BudgetPlan:
    budget: int
    total_words: int
    unused_words: int
    policy: CountPolicy
    chapters: list[ChapterPlan]
    selected: list[Fragment]
    decisions: list[Decision]

    def to_dict(self, include_text: bool = False) -> dict[str, Any]:
        chapter_rows = []
        for row in self.chapters:
            chapter_rows.append(
                {
                    "id": row.chapter.id,
                    "title": row.chapter.title,
                    "parent_id": row.chapter.parent_id,
                    "weight": row.chapter.weight,
                    "depth": row.depth,
                    "quota": row.quota,
                    "own_quota": row.own_quota,
                    "own_words": row.own_words,
                    "subtree_words": row.subtree_words,
                    "unused_own": row.unused_own,
                    "heading_level": row.heading_level,
                    "synthetic": row.chapter.synthetic,
                    "selected_fragment_ids": list(row.selected_fragment_ids),
                }
            )

        selected_rows = []
        for fragment in self.selected:
            row: dict[str, Any] = {
                "id": fragment.id,
                "chapter_id": fragment.chapter_id,
                "kind": fragment.kind,
                "words": count_fragment(fragment, self.policy),
                "priority": fragment.priority,
            }
            if include_text:
                row["text"] = fragment.text
            selected_rows.append(row)

        decision_rows = [asdict(decision) for decision in self.decisions]
        return {
            "budget": self.budget,
            "total_words": self.total_words,
            "unused_words": self.unused_words,
            "count_policy": asdict(self.policy),
            "chapters": chapter_rows,
            "selected": selected_rows,
            "decisions": decision_rows,
        }


@dataclass
class ParsedDocument:
    chapters: list[Chapter]
    fragments: list[Fragment]


# ---------------------------------------------------------------------------
# Counting


_CJK_RUN_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]+")
_WORD_RUN_RE = re.compile(r"[^\W_]+(?:['\u2019-][^\W_]+)*", re.UNICODE)
_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FOOTNOTE_RE = re.compile(r"^ {0,3}\[\^([^\]]+)\]:[ \t]*(.*)$")
_ATTR_LIST_RE = re.compile(r"[ \t]*\{[^{}]*\}[ \t]*$")


def _word_count(text: str, cjk_as_chars: bool = True) -> int:
    count = 0
    for match in _WORD_RUN_RE.finditer(text):
        run = match.group(0)
        if not cjk_as_chars:
            count += 1
            continue
        cursor = 0
        for cjk in _CJK_RUN_RE.finditer(run):
            if cjk.start() > cursor:
                count += 1
            count += len(cjk.group(0))
            cursor = cjk.end()
        if cursor < len(run):
            count += 1
    return count


def _char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _line_count(text: str, non_empty_only: bool = False) -> int:
    lines = text.splitlines()
    if non_empty_only:
        return sum(1 for line in lines if line.strip())
    return len(lines)


def _visible_text(text: str, policy: CountPolicy) -> str:
    """Approximate the text a reader sees, for prose word/character counts."""

    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    if not policy.count_footnote_references:
        text = re.sub(r"\[\^[^\]]+\]", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "")
    text = re.sub(r"(\*\*|__|\*|_|~~)", "", text)
    text = re.sub(r"\\([\\`*{}\[\]()#+\-.!_>~])", r"\1", text)
    return text


def _count_text(text: str, unit: str, policy: CountPolicy) -> int:
    if unit == "none":
        return 0
    if unit == "chars":
        return _char_count(text)
    if unit == "words":
        return _word_count(text, policy.cjk_as_chars)
    if unit == "lines":
        return _line_count(text, non_empty_only=True)
    raise ValueError(f"Unknown counting unit: {unit!r}")


def count_fragment(fragment: Fragment, policy: CountPolicy) -> int:
    """Return the budget charge for ``fragment`` under ``policy``.

    A fragment may set ``count`` explicitly.  That is the escape hatch for a
    caller whose counting convention differs from the defaults.
    """

    if fragment.count is not None:
        if fragment.count < 0:
            raise ValueError(f"Fragment {fragment.id!r} has a negative count")
        return fragment.count

    if fragment.kind == "code":
        return _count_text(fragment.text, policy.code_unit, policy)
    if fragment.kind == "footnote":
        return _count_text(_visible_text(fragment.text, policy), policy.footnote_unit, policy)
    if fragment.kind == "prose":
        return _count_text(_visible_text(fragment.text, policy), policy.unit, policy)
    raise ValueError(f"Fragment {fragment.id!r} has unknown kind {fragment.kind!r}")


# ---------------------------------------------------------------------------
# Markdown parsing


def _slug(text: str) -> str:
    text = _ATTR_LIST_RE.sub("", text)
    text = re.sub(r"[`*_~]", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[^\w㐀-䶿一-鿿]+", "-", text, flags=re.UNICODE)
    text = text.strip("-").lower()
    return text or "section"


def _clean_heading(text: str) -> str:
    text = _ATTR_LIST_RE.sub("", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_~]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_indented(line: str) -> bool:
    return line.startswith("    ") or line.startswith("\t")


def _strip_indent(line: str, width: int = 4) -> str:
    if line.startswith("\t"):
        return line[1:]
    return line[min(width, len(line) - len(line.lstrip(" "))):]


def _unique_id(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _code_lines(lines: Sequence[str]) -> str:
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def parse_markdown(
    text: str,
    document_id: str,
    title: str | None = None,
    weight: float = 1.0,
) -> ParsedDocument:
    """Parse a Markdown document into an explicit chapter tree.

    A synthetic root chapter owns any text before the first heading.  ATX and
    setext headings create child chapters.  Blank-line-delimited blocks are
    the atomic selectable units; fenced code blocks and footnote definitions
    are their own units.  Heading text is metadata and is never charged to the
    budget.  A leading YAML front matter block is metadata too.
    """

    document_id = str(document_id)
    used_ids: set[str] = set()
    root_id = _unique_id(document_id, used_ids)
    chapters: list[Chapter] = [
        Chapter(
            id=root_id,
            title=title or document_id,
            parent_id=None,
            weight=float(weight),
            self_weight=1.0,
            order=0,
            synthetic=True,
        )
    ]
    fragments: list[Fragment] = []
    current_chapter = root_id
    stack: list[tuple[int, str]] = [(0, root_id)]
    paragraph: list[str] = []

    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if lines and lines[0].strip() == "---":
        for closing in range(1, len(lines)):
            if lines[closing].strip() in ("---", "..."):
                lines = lines[closing + 1:]
                if lines and not lines[0].strip():
                    lines = lines[1:]
                break

    def add_fragment(kind: str, body: str, meta: dict[str, Any] | None = None) -> None:
        body = body.strip("\n")
        if not body and kind != "code":
            return
        fragment_id = f"{document_id}:f{len(fragments):04d}"
        fragments.append(
            Fragment(
                id=fragment_id,
                chapter_id=current_chapter,
                text=body,
                kind=kind,
                order=len(fragments),
                meta=meta or {},
            )
        )

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            add_fragment("prose", "\n".join(paragraph).strip())
            paragraph = []

    def add_heading(level: int, heading_text: str) -> None:
        nonlocal current_chapter
        flush_paragraph()
        slug = _slug(heading_text)
        chapter_id = _unique_id(f"{document_id}/{slug}", used_ids)
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else root_id
        chapters.append(
            Chapter(
                id=chapter_id,
                title=_clean_heading(heading_text) or slug,
                parent_id=parent_id,
                weight=1.0,
                self_weight=1.0,
                order=len(chapters),
                heading_level=level,
            )
        )
        stack.append((level, chapter_id))
        current_chapter = chapter_id

    index = 0
    while index < len(lines):
        line = lines[index]

        fence = _FENCE_RE.match(line)
        if fence:
            flush_paragraph()
            marker = fence.group(1)
            fence_char = marker[0]
            fence_len = len(marker)
            info = fence.group(2).strip()
            body_lines: list[str] = []
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if re.match(
                    rf"^ {{0,3}}{re.escape(fence_char)}{{{fence_len},}}[ \t]*$",
                    candidate,
                ):
                    index += 1
                    break
                body_lines.append(candidate)
                index += 1
            add_fragment("code", _code_lines(body_lines), {"lang": info})
            continue

        if not paragraph:
            footnote = _FOOTNOTE_RE.match(line)
            if footnote:
                marker = footnote.group(1)
                body_lines = [footnote.group(2).strip()]
                index += 1
                while index < len(lines):
                    candidate = lines[index]
                    if not candidate.strip():
                        next_index = index
                        while next_index < len(lines) and not lines[next_index].strip():
                            next_index += 1
                        if next_index < len(lines) and _is_indented(lines[next_index]):
                            body_lines.extend([""] * (next_index - index))
                            index = next_index
                            continue
                        break
                    if _is_indented(candidate):
                        body_lines.append(_strip_indent(candidate))
                        index += 1
                        continue
                    break
                add_fragment("footnote", "\n".join(body_lines).strip(), {"marker": marker})
                continue

            if line.strip() and index + 1 < len(lines):
                setext = _SETEXT_RE.match(lines[index + 1])
                if setext:
                    level = 1 if setext.group(1).startswith("=") else 2
                    add_heading(level, line.strip())
                    index += 2
                    continue

        if paragraph:
            setext = _SETEXT_RE.match(line)
            if setext:
                level = 1 if setext.group(1).startswith("=") else 2
                heading_text = "\n".join(paragraph).strip()
                paragraph = []
                add_heading(level, heading_text)
                index += 1
                continue

        heading = _ATX_HEADING_RE.match(line)
        if heading:
            add_heading(len(heading.group(1)), heading.group(2).strip())
            index += 1
            continue

        if not paragraph and _is_indented(line):
            body_lines = [_strip_indent(line)]
            index += 1
            while index < len(lines) and (_is_indented(lines[index]) or not lines[index].strip()):
                body_lines.append(_strip_indent(lines[index]) if _is_indented(lines[index]) else "")
                index += 1
            while index < len(lines) and not lines[index].strip():
                index += 1
            add_fragment("code", _code_lines(body_lines), {"lang": "", "indented": True})
            continue

        if not line.strip():
            flush_paragraph()
            index += 1
            continue

        paragraph.append(line)
        index += 1

    flush_paragraph()
    return ParsedDocument(chapters=chapters, fragments=fragments)


def load_markdown_files(paths: Iterable[str | Path], weight: float = 1.0) -> ParsedDocument:
    """Parse several Markdown files as sibling root chapters."""

    all_chapters: list[Chapter] = []
    all_fragments: list[Fragment] = []
    used_document_ids: set[str] = set()
    order_offset = 0
    for raw_path in paths:
        path = Path(raw_path)
        document_id = _slug(path.stem) or "document"
        document_id = _unique_id(document_id, used_document_ids)
        parsed = parse_markdown(
            path.read_text(encoding="utf-8"),
            document_id=document_id,
            title=path.stem,
            weight=weight,
        )
        all_chapters.extend(parsed.chapters)
        for fragment in parsed.fragments:
            fragment.order = (fragment.order or 0) + order_offset
            all_fragments.append(fragment)
        order_offset += len(parsed.fragments)
    return ParsedDocument(chapters=all_chapters, fragments=all_fragments)


# ---------------------------------------------------------------------------
# Budgeting and selection


def _ordered(items: Sequence[Any]) -> list[Any]:
    indexed = list(enumerate(items))
    return [
        item
        for _, item in sorted(
            indexed,
            key=lambda pair: (
                pair[1].order if pair[1].order is not None else pair[0],
                pair[0],
            ),
        )
    ]


def _allocate(total: int, parts: Sequence[tuple[str, float]]) -> dict[str, int]:
    """Split ``total`` integer units proportionally, deterministically.

    Floors are assigned first.  Remaining units go to the largest fractional
    remainders; ties go to the earlier part.  A part with weight zero receives
    zero.  If every weight is zero, every part receives zero and the budget is
    reported as unused rather than silently reallocated.
    """

    result = {part_id: 0 for part_id, _ in parts}
    if total <= 0 or not parts:
        return result

    positive = [(part_id, weight) for part_id, weight in parts if weight > 0]
    if not positive:
        return result

    weighted = positive
    weight_sum = sum(weight for _, weight in positive)

    floors = {part_id: 0 for part_id, _ in parts}
    for part_id, weight in weighted:
        floors[part_id] = math.floor(total * weight / weight_sum)

    result.update(floors)
    assigned = sum(floors.values())
    remainders: list[tuple[float, int, str]] = []
    for position, (part_id, weight) in enumerate(parts):
        exact = total * weight / weight_sum if weight > 0 else 0.0
        remainders.append((exact - math.floor(exact), position, part_id))

    for _, _, part_id in sorted(remainders, key=lambda row: (-row[0], row[1]))[: total - assigned]:
        result[part_id] += 1
    return result


def _normalise_fragment_text(fragment: Fragment) -> str:
    text = fragment.text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if fragment.kind == "code":
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return re.sub(r"\s+", " ", text).strip()


def _dedupe_key(fragment: Fragment) -> str:
    if fragment.dedupe_key:
        return fragment.dedupe_key
    return f"{fragment.kind}:{_normalise_fragment_text(fragment)}"


def _select(fragments: Sequence[tuple[Fragment, int]], quota: int) -> tuple[set[str], int, int]:
    """Choose whole fragments for one chapter's own quota.

    The objective is deterministic:

    1. maximise the sum of fragment priorities;
    2. among equal priorities, use as much of the quota as whole blocks allow;
    3. among equal priority and word totals, prefer the selection whose
       fragments appear earliest in source order.

    The third rule is the stable tie-break.  It is encoded as a bit mask and
    therefore does not depend on dictionary iteration order.
    """

    if quota < 0:
        raise ValueError("quota cannot be negative")
    if not fragments:
        return set(), 0, 0

    # states[words] = (priority, mask), where bit n-1-i represents fragment i.
    # The word dimension is bounded by the chapter quota; the priority
    # dimension is bounded by the input priorities.  This is a deterministic
    # integer objective, not a heuristic.
    states: dict[int, tuple[int, int]] = {0: (0, 0)}
    count = len(fragments)
    for index, (fragment, words) in enumerate(fragments):
        bit = 1 << (count - 1 - index)
        additions: dict[int, tuple[int, int]] = {}
        for used, (priority, mask) in states.items():
            new_used = used + words
            if new_used > quota:
                continue
            candidate = (priority + fragment.priority, mask | bit)
            previous = states.get(new_used)
            if previous is None or candidate[0] > previous[0] or (
                candidate[0] == previous[0] and candidate[1] > previous[1]
            ):
                additions[new_used] = candidate
        for used, candidate in additions.items():
            previous = states.get(used)
            if previous is None or candidate[0] > previous[0] or (
                candidate[0] == previous[0] and candidate[1] > previous[1]
            ):
                states[used] = candidate

    best_words, (best_priority, best_mask) = max(
        states.items(),
        key=lambda row: (row[1][0], row[0], row[1][1]),
    )
    selected = {
        fragment.id
        for index, (fragment, _) in enumerate(fragments)
        if best_mask & (1 << (count - 1 - index))
    }
    return selected, best_words, best_priority


def _decision_detail(
    *,
    words: int,
    priority: int,
    own_quota: int,
    selected_words: int,
    remaining: int,
) -> str:
    return (
        f"words={words}; priority={priority}; own_quota={own_quota}; "
        f"selected_words={selected_words}; remaining={remaining}"
    )


def plan(
    chapters: Sequence[Chapter],
    fragments: Sequence[Fragment],
    budget: int,
    policy: CountPolicy | None = None,
) -> BudgetPlan:
    """Allocate ``budget`` across chapters and select whole fragments.

    Budget flows recursively.  A chapter envelope is split between the
    chapter's own blocks and its child chapters according to ``self_weight``
    and each child's ``weight``.  A child selection is therefore charged only
    to the child's envelope; a parent that contains that child does not charge
    the same words again.  The parent's reported ``subtree_words`` is the sum
    of already-charged descendants, not a second charge.
    """

    if budget < 0:
        raise ValueError("budget cannot be negative")
    policy = policy or CountPolicy()

    ordered_chapters = _ordered(list(chapters))
    chapter_by_id: dict[str, Chapter] = {}
    chapter_depth: dict[str, int] = {}
    for chapter in ordered_chapters:
        if chapter.id in chapter_by_id:
            raise ValueError(f"Duplicate chapter id: {chapter.id!r}")
        if not math.isfinite(chapter.weight) or chapter.weight < 0:
            raise ValueError(f"Chapter {chapter.id!r} has an invalid weight")
        if not math.isfinite(chapter.self_weight) or chapter.self_weight < 0:
            raise ValueError(f"Chapter {chapter.id!r} has an invalid self_weight")
        chapter_by_id[chapter.id] = chapter

    children: dict[str, list[str]] = {chapter.id: [] for chapter in ordered_chapters}
    roots: list[str] = []
    for chapter in ordered_chapters:
        if chapter.parent_id is None:
            roots.append(chapter.id)
            chapter_depth[chapter.id] = 0
            continue
        if chapter.parent_id not in chapter_by_id:
            raise ValueError(f"Unknown parent {chapter.parent_id!r} for chapter {chapter.id!r}")
        children[chapter.parent_id].append(chapter.id)

    # Detect cycles and assign depths before doing any budget arithmetic.
    visited: set[str] = set()
    visiting: set[str] = set()

    def walk(chapter_id: str, depth: int) -> None:
        if chapter_id in visiting:
            raise ValueError(f"Chapter hierarchy contains a cycle at {chapter_id!r}")
        if chapter_id in visited:
            return
        visiting.add(chapter_id)
        chapter_depth[chapter_id] = depth
        for child_id in children[chapter_id]:
            walk(child_id, depth + 1)
        visiting.remove(chapter_id)
        visited.add(chapter_id)

    for chapter_id in roots:
        walk(chapter_id, 0)
    if len(visited) != len(chapter_by_id):
        raise ValueError("Chapter hierarchy contains a cycle or an unreachable chapter")

    ordered_fragments = _ordered(list(fragments))
    direct: dict[str, list[tuple[Fragment, int]]] = {chapter.id: [] for chapter in ordered_chapters}
    seen_keys: dict[str, Fragment] = {}
    duplicate_of: dict[str, Fragment] = {}
    counts: dict[str, int] = {}
    for fragment in ordered_fragments:
        if fragment.id in counts:
            raise ValueError(f"Duplicate fragment id: {fragment.id!r}")
        if fragment.chapter_id not in chapter_by_id:
            raise ValueError(f"Fragment {fragment.id!r} belongs to unknown chapter {fragment.chapter_id!r}")
        if fragment.kind not in ("prose", "code", "footnote"):
            raise ValueError(f"Fragment {fragment.id!r} has unknown kind {fragment.kind!r}")
        words = count_fragment(fragment, policy)
        counts[fragment.id] = words
        key = _dedupe_key(fragment)
        if key in seen_keys:
            duplicate_of[fragment.id] = seen_keys[key]
            continue
        seen_keys[key] = fragment
        direct[fragment.chapter_id].append((fragment, words))

    content_cache: dict[str, bool] = {}

    def has_content(chapter_id: str) -> bool:
        if chapter_id in content_cache:
            return content_cache[chapter_id]
        result = bool(direct[chapter_id]) or any(has_content(child) for child in children[chapter_id])
        content_cache[chapter_id] = result
        return result
    quotas: dict[str, int] = {chapter.id: 0 for chapter in ordered_chapters}
    own_quotas: dict[str, int] = {chapter.id: 0 for chapter in ordered_chapters}

    def allocate_envelope(chapter_id: str, envelope: int) -> None:
        quotas[chapter_id] = envelope
        chapter = chapter_by_id[chapter_id]
        parts: list[tuple[str, float]] = []
        if direct[chapter_id]:
            parts.append((chapter_id, chapter.self_weight))
        for child_id in children[chapter_id]:
            if has_content(child_id):
                parts.append((child_id, chapter_by_id[child_id].weight))

        if not parts:
            own_quotas[chapter_id] = envelope
            return

        allocation = _allocate(envelope, parts)
        own_quotas[chapter_id] = allocation.get(chapter_id, 0)
        for child_id in children[chapter_id]:
            allocate_envelope(child_id, allocation.get(child_id, 0))

    root_parts = [(root_id, chapter_by_id[root_id].weight) for root_id in roots if has_content(root_id)]
    root_allocation = _allocate(budget, root_parts)
    for root_id in roots:
        allocate_envelope(root_id, root_allocation.get(root_id, 0))

    selected_by_chapter: dict[str, list[Fragment]] = {chapter.id: [] for chapter in ordered_chapters}
    own_words: dict[str, int] = {chapter.id: 0 for chapter in ordered_chapters}
    decisions: list[Decision] = []

    for chapter in ordered_chapters:
        chapter_id = chapter.id
        chapter_fragments = direct[chapter_id]
        chosen, selected_words, _ = _select(chapter_fragments, own_quotas[chapter_id])
        remaining = max(0, own_quotas[chapter_id] - selected_words)
        selected_by_chapter[chapter_id] = [
            fragment for fragment, _ in chapter_fragments if fragment.id in chosen
        ]
        own_words[chapter_id] = selected_words

        for fragment, words in chapter_fragments:
            if fragment.id in chosen:
                decisions.append(
                    Decision(
                        fragment_id=fragment.id,
                        chapter_id=chapter_id,
                        kind=fragment.kind,
                        words=words,
                        charged_words=words,
                        priority=fragment.priority,
                        selected=True,
                        reason="selected",
                        detail=_decision_detail(
                            words=words,
                            priority=fragment.priority,
                            own_quota=own_quotas[chapter_id],
                            selected_words=selected_words,
                            remaining=remaining,
                        ),
                    )
                )
                continue

            if own_quotas[chapter_id] == 0:
                reason = "chapter_quota_zero"
                detail = f"own_quota=0; words={words}; priority={fragment.priority}"
            elif words > own_quotas[chapter_id]:
                reason = "indivisible_overflow"
                detail = (
                    f"block of {words} words cannot fit own_quota={own_quotas[chapter_id]}; "
                    f"paragraphs cannot be split"
                )
            elif fragment.priority < 0:
                reason = "negative_priority"
                detail = _decision_detail(
                    words=words,
                    priority=fragment.priority,
                    own_quota=own_quotas[chapter_id],
                    selected_words=selected_words,
                    remaining=remaining,
                )
            else:
                higher = any(
                    other.priority > fragment.priority
                    for other, _ in chapter_fragments
                    if other.id in chosen
                )
                same_size_winner = any(
                    other.priority == fragment.priority
                    and other_words == words
                    and other.id in chosen
                    for other, other_words in chapter_fragments
                )
                if higher:
                    reason = "lower_priority"
                    detail = _decision_detail(
                        words=words,
                        priority=fragment.priority,
                        own_quota=own_quotas[chapter_id],
                        selected_words=selected_words,
                        remaining=remaining,
                    )
                elif same_size_winner:
                    reason = "stable_tie_lost"
                    detail = (
                        _decision_detail(
                            words=words,
                            priority=fragment.priority,
                            own_quota=own_quotas[chapter_id],
                            selected_words=selected_words,
                            remaining=remaining,
                        )
                        + "; equal words and priority; earlier source order won"
                    )
                elif words > remaining:
                    reason = "budget_exhausted"
                    detail = _decision_detail(
                        words=words,
                        priority=fragment.priority,
                        own_quota=own_quotas[chapter_id],
                        selected_words=selected_words,
                        remaining=remaining,
                    )
                else:
                    reason = "not_selected"
                    detail = _decision_detail(
                        words=words,
                        priority=fragment.priority,
                        own_quota=own_quotas[chapter_id],
                        selected_words=selected_words,
                        remaining=remaining,
                    )
            decisions.append(
                Decision(
                    fragment_id=fragment.id,
                    chapter_id=chapter_id,
                    kind=fragment.kind,
                    words=words,
                    charged_words=0,
                    priority=fragment.priority,
                    selected=False,
                    reason=reason,
                    detail=detail,
                )
            )

    # Duplicate fragments never enter a chapter's quota.  Report them after
    # the selection pass so each one points at the single charged owner.
    for fragment in ordered_fragments:
        canonical = duplicate_of.get(fragment.id)
        if canonical is None:
            continue
        words = counts[fragment.id]
        decisions.append(
            Decision(
                fragment_id=fragment.id,
                chapter_id=fragment.chapter_id,
                kind=fragment.kind,
                words=words,
                charged_words=0,
                priority=fragment.priority,
                selected=False,
                reason="duplicate",
                detail=(
                    f"same {fragment.kind} fragment as {canonical.id}; "
                    "charged once to the canonical owner"
                ),
                canonical_id=canonical.id,
            )
        )

    chapter_position = {chapter.id: position for position, chapter in enumerate(ordered_chapters)}
    fragment_position = {fragment.id: position for position, fragment in enumerate(ordered_fragments)}
    decisions.sort(
        key=lambda row: (
            chapter_position[row.chapter_id],
            fragment_position[row.fragment_id],
        )
    )

    subtree_words: dict[str, int] = {}

    def sum_subtree(chapter_id: str) -> int:
        total = own_words[chapter_id] + sum(sum_subtree(child_id) for child_id in children[chapter_id])
        subtree_words[chapter_id] = total
        return total

    for root_id in roots:
        sum_subtree(root_id)

    chapter_plans = [
        ChapterPlan(
            chapter=chapter,
            depth=chapter_depth[chapter.id],
            quota=quotas[chapter.id],
            own_quota=own_quotas[chapter.id],
            own_words=own_words[chapter.id],
            subtree_words=subtree_words.get(chapter.id, 0),
            selected_fragment_ids=[fragment.id for fragment in selected_by_chapter[chapter.id]],
            unused_own=max(0, own_quotas[chapter.id] - own_words[chapter.id]),
            heading_level=chapter.heading_level,
        )
        for chapter in ordered_chapters
    ]

    selected_fragments = [
        fragment
        for chapter in ordered_chapters
        for fragment in selected_by_chapter[chapter.id]
    ]
    total_words = sum(counts[fragment.id] for fragment in selected_fragments)
    return BudgetPlan(
        budget=budget,
        total_words=total_words,
        unused_words=max(0, budget - total_words),
        policy=policy,
        chapters=chapter_plans,
        selected=selected_fragments,
        decisions=decisions,
    )


# ---------------------------------------------------------------------------
# Rendering and CLI


def _render_fragment(fragment: Fragment) -> str:
    if fragment.kind == "code":
        language = str(fragment.meta.get("lang", "")).strip()
        return f"```{language}\n{fragment.text}\n```"
    if fragment.kind == "footnote":
        marker = str(fragment.meta.get("marker", "")).strip()
        if marker:
            return f"[^{marker}]: {fragment.text}"
    return fragment.text


def render_markdown(plan_result: BudgetPlan, include_audit: bool = False) -> str:
    """Render the selected blocks as nested Markdown chapters."""

    fragments_by_id = {fragment.id: fragment for fragment in plan_result.selected}
    lines: list[str] = []
    if include_audit:
        lines.append(
            f"<!-- budget={plan_result.budget} selected={plan_result.total_words} "
            f"unused={plan_result.unused_words} -->"
        )
        lines.append("")
    by_parent: dict[str | None, list[ChapterPlan]] = {}
    for row in plan_result.chapters:
        by_parent.setdefault(row.chapter.parent_id, []).append(row)

    def emit(row: ChapterPlan) -> None:
        if not row.chapter.synthetic:
            level = min(6, row.heading_level or row.depth + 1)
            lines.append(f"{'#' * level} {row.chapter.title or row.chapter.id}")
            lines.append("")
        for fragment_id in row.selected_fragment_ids:
            fragment = fragments_by_id.get(fragment_id)
            if fragment is not None:
                lines.append(_render_fragment(fragment))
                lines.append("")
        if include_audit:
            synthetic = " synthetic=true" if row.chapter.synthetic else ""
            lines.append(
                f"<!-- chapter={row.chapter.id}{synthetic} own_quota={row.own_quota} "
                f"own_words={row.own_words} subtree_words={row.subtree_words} -->"
            )
            lines.append("")
        for child in by_parent.get(row.chapter.id, []):
            emit(child)

    roots = by_parent.get(None, [])
    for root_row in roots:
        emit(root_row)
    return "\n".join(lines).rstrip() + "\n"


def _load_config(path: str | Path) -> tuple[int, CountPolicy, list[Chapter], list[Fragment]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "budget" not in data:
        raise ValueError("Configuration needs a 'budget'")
    chapters = [Chapter.from_dict(row) for row in data.get("chapters", [])]
    fragments = [Fragment.from_dict(row) for row in data.get("fragments", [])]
    policy = CountPolicy.from_dict(data.get("count_policy"))
    return int(data["budget"]), policy, chapters, fragments


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Allocate a finite word budget across chapter weights and select "
            "whole Markdown blocks.  Use a JSON configuration for explicit "
            "chapter ownership, weights, priorities, and counting policy, or "
            "--markdown to derive the chapter tree from one or more files."
        )
    )
    parser.add_argument("--config", help="JSON plan with budget, chapters, fragments, count_policy")
    parser.add_argument(
        "--markdown",
        action="append",
        default=[],
        metavar="FILE",
        help="Markdown file to parse (repeatable); all files become sibling root chapters",
    )
    parser.add_argument("--budget", type=int, help="Total word budget; overrides config budget")
    parser.add_argument("--weight", type=float, default=1.0, help="Default root weight for --markdown")
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument("--output", help="Write output to this file instead of stdout")
    parser.add_argument("--no-text", action="store_true", help="Omit fragment text from JSON selected rows")
    parser.add_argument("--audit", action="store_true", help="Include budget annotations in Markdown output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.config and not args.markdown:
        parser.error("provide --config or at least one --markdown file")

    if args.config:
        budget, policy, chapters, fragments = _load_config(args.config)
    elif args.budget is None:
        parser.error("--markdown requires --budget")
    else:
        budget, policy, chapters, fragments = args.budget, CountPolicy(), [], []

    if args.markdown:
        parsed = load_markdown_files(args.markdown, weight=args.weight)
        chapters.extend(parsed.chapters)
        fragments.extend(parsed.fragments)

    if args.budget is not None:
        budget = args.budget
    if budget is None:
        parser.error("a budget is required (use --budget or a config file)")

    result = plan(chapters, fragments, budget, policy)
    if args.format == "json":
        rendered = json.dumps(
            result.to_dict(include_text=not args.no_text),
            ensure_ascii=False,
            indent=2,
        )
    else:
        rendered = render_markdown(result, include_audit=args.audit)

    if args.output:
        Path(args.output).write_text(rendered + ("" if rendered.endswith("\n") else "\n"), encoding="utf-8")
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
