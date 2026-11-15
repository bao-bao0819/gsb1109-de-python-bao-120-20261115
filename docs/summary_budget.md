# Summary budget allocator

`tools/summary_budget.py` turns a chapter tree into a budgeted, extractive
summary. It is designed for the case where several chapters share one finite
word budget and the same body block must not be charged twice.

## Rules

- **Whole blocks only.** A paragraph, fenced code block, or footnote definition
  is selected or rejected as a unit. Paragraphs are never truncated.
- **Explicit ownership.** Every block names exactly one owning chapter. The
  block is rendered under that chapter and charged only to that chapter's
  quota.
- **No double charging.** A parent envelope is split between the parent's own
  blocks (`self_weight`) and its child chapters (each child's `weight`). The
  parent's `subtree_words` is the sum of already-charged descendants.
- **Quota is not silently reassigned.** If a chapter cannot use its own quota
  because the remaining blocks do not fit whole, the shortfall is reported as
  `unused_own` and stays unused. It is not moved to siblings, so the published
  weights remain the effective allocation.
- **Global deduplication.** Identical blocks (whitespace-normalised) are
  charged once to the first owner in source order. Later copies report
  `reason: "duplicate"` and `charged_words: 0`.
- **Stable selection.** Within a chapter, selection maximises priority, then
  uses as much of the quota as whole blocks allow, then prefers the earlier
  source-order selection. Equal words and priority therefore produce the same
  result on every run.
- **Auditable rejections.** Every rejected block has a reason such as
  `indivisible_overflow`, `chapter_quota_zero`, `lower_priority`,
  `stable_tie_lost`, `budget_exhausted`, or `duplicate`.

## Counting

`CountPolicy` makes the counting convention explicit:

| Kind | Default | Notes |
| --- | --- | --- |
| prose | words | Markdown markup, links, HTML tags, and footnote references are ignored |
| code | non-blank lines | fence markers and the info string are not counted |
| footnote | words | the `[^id]:` marker is not counted |

CJK runs count as one unit per character by default; set
`"cjk_as_chars": false` to count each run as one word. An explicit `count` on
a block overrides the policy for that block.

## Input

For documents which already have headings, let the parser derive the tree:

```console
python -m tools.summary_budget \
  --markdown docs/extensions/toc.md \
  --budget 120 --format markdown --audit
```

For editorial control over chapter weights, ownership, priorities, and
counting, pass a JSON configuration:

```json
{
  "budget": 1200,
  "count_policy": {
    "unit": "words",
    "code_unit": "lines",
    "footnote_unit": "words",
    "count_footnote_references": false,
    "cjk_as_chars": true
  },
  "chapters": [
    {"id": "guide", "title": "Guide", "weight": 2.0, "self_weight": 1.0},
    {"id": "guide/intro", "title": "Introduction", "parent_id": "guide", "weight": 3.0}
  ],
  "fragments": [
    {"id": "p1", "chapter_id": "guide/intro", "text": "...", "priority": 10},
    {"id": "code1", "chapter_id": "guide", "kind": "code", "text": "..."},
    {"id": "n1", "chapter_id": "guide", "kind": "footnote", "text": "..."}
  ]
}
```

`owner` is an alias for `chapter_id`; `parent` is an alias for `parent_id`.
`order` defaults to the input position and can be set explicitly for stable
tie-breaking. `dedupe_key` overrides whitespace-normalised text comparison.

## Output

`--format json` (default) returns the selected blocks, per-chapter quotas, and
one decision row per candidate block, including `words`, `priority`, `reason`,
and `detail`. `--format markdown` renders the selected blocks under their
chapter headings; `--audit` adds budget comments.

## Tests

```console
python -m unittest tests.test_summary_budget
```
