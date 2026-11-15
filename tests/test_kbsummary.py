"""Tests for the kbsummary budgeted summarization package."""

import json

from kbsummary import (
    REASON_ALREADY_COUNTED,
    REASON_EXCLUDED_KIND,
    REASON_OVER_BUDGET,
    CountingRules,
    allocate_budget,
    count_words,
    parse_document,
    summarize,
)
from kbsummary.__main__ import run as cli_run
from kbsummary.core import _select, Block

DOC = """\
Preface paragraph here.

# Alpha

Alpha intro words.

## Alpha Sub

Sub section body text.

# Beta

Beta body words.

```python
x = 1
y = 2
```

[^1]: A footnote definition.
"""


def test_count_words_cjk_and_latin():
    assert count_words('你好 world') == 3
    assert count_words('one two three') == 3
    assert count_words('see the note[^1] here') == 4


def test_explicit_attribution():
    root = parse_document(DOC)
    sections = {s.id: s for s in root.flatten()}
    assert [b.text for b in sections['root'].blocks] == ['Preface paragraph here.']
    assert [b.text for b in sections['alpha'].blocks] == ['Alpha intro words.']
    assert [b.text for b in sections['alpha-sub'].blocks] == ['Sub section body text.']
    kinds = [b.kind for b in sections['beta'].blocks]
    assert kinds == ['paragraph', 'code', 'footnote']
    # Each fragment is attributed to exactly one section.
    all_blocks = [b for s in root.flatten() for b in s.blocks]
    assert len({b.id for b in all_blocks}) == len(all_blocks)


def test_weighted_allocation():
    root = parse_document(DOC)
    sections = [s for s in root.flatten() if s.id in ('alpha', 'beta')]
    budgets = allocate_budget(sections, 100, weights={'alpha': 3, 'beta': 1})
    assert budgets['alpha'] == 75
    assert budgets['beta'] == 25


def test_default_weight_excludes_descendants():
    root = parse_document(DOC)
    sections = {s.id: s for s in root.flatten()}
    alpha, sub = sections['alpha'], sections['alpha-sub']
    budgets = allocate_budget([alpha, sub], 90)
    # alpha owns 3 words, sub owns 4 words; descendants never count twice.
    assert budgets['alpha'] == 90 * 3 / 7
    assert budgets['alpha-sub'] == 90 * 4 / 7


def test_equal_weights_split_evenly():
    root = parse_document('# A\n\n## B\n\n## C\n')
    empty = [s for s in root.flatten() if s.id in ('b', 'c')]
    budgets = allocate_budget(empty, 50)
    assert budgets == {'b': 25, 'c': 25}


def test_paragraph_atomicity():
    doc = '# A\n\n' + 'word ' * 9 + 'word\n\nshort\n'
    result = summarize(doc, total_budget=5, sections=['a'])
    summary = result.sections[0]
    texts = [b.text for b in summary.selected]
    assert 'short' in texts
    assert not any('word word' in t for t in texts)
    reasons = {s.block.id: s.reason for s in summary.skipped}
    long_block = next(b for b in summary.section.blocks if b.words == 10)
    assert reasons[long_block.id] == REASON_OVER_BUDGET


def test_code_counting_modes():
    root = parse_document(DOC)
    code = next(b for s in root.flatten() for b in s.blocks if b.kind == 'code')
    assert code.words == 0  # default 'none'
    root = parse_document(DOC, CountingRules(code='lines'))
    code = next(b for s in root.flatten() for b in s.blocks if b.kind == 'code')
    assert code.words == 2
    root = parse_document(DOC, CountingRules(code='exclude'))
    code = next(b for s in root.flatten() for b in s.blocks if b.kind == 'code')
    assert not code.candidate
    result = summarize(DOC, total_budget=1000, sections=['beta'],
                       rules=CountingRules(code='exclude'))
    reasons = {s.block.kind: s.reason for s in result.sections[0].skipped}
    assert reasons['code'] == REASON_EXCLUDED_KIND


def test_footnote_counting_modes():
    root = parse_document(DOC, CountingRules(footnotes='words'))
    note = next(b for s in root.flatten() for b in s.blocks if b.kind == 'footnote')
    assert note.words == count_words('[^1]: A footnote definition.')
    root = parse_document(DOC, CountingRules(footnotes='exclude'))
    note = next(b for s in root.flatten() for b in s.blocks if b.kind == 'footnote')
    assert not note.candidate


def test_parent_child_no_double_count():
    doc = '# Parent\n\nparent body words\n\n## Child\n\nchild body words\n'
    result = summarize(doc, total_budget=100, sections=['parent', 'child'])
    selected_ids = [b.id for s in result.sections for b in s.selected]
    assert len(selected_ids) == len(set(selected_ids))
    by_id = {s.section.id: s for s in result.sections}
    assert [b.text for b in by_id['parent'].selected] == ['parent body words']
    assert [b.text for b in by_id['child'].selected] == ['child body words']
    assert result.total_used <= result.total_budget


def test_rollup_unbudgeted_descendants():
    doc = '# A\n\nalpha body\n\n## B\n\nbeta body\n'
    result = summarize(doc, total_budget=100, sections=['a'])
    texts = sorted(b.text for b in result.sections[0].selected)
    assert texts == ['alpha body', 'beta body']


def test_rollup_consumed_once():
    doc = '# A\n\nalpha body\n\n## B\n\nbeta body\n\n## C\n\ngamma body\n'
    result = summarize(doc, total_budget=100, sections=['a', 'c'])
    selected_ids = [b.id for s in result.sections for b in s.selected]
    assert len(selected_ids) == len(set(selected_ids))


def test_already_counted_reason():
    block = Block(id=1, kind='paragraph', text='x', line=1, words=1)
    selected, skipped = _select([block], budget=10, consumed={1})
    assert selected == []
    assert skipped[0].reason == REASON_ALREADY_COUNTED


def test_stable_tie_break():
    doc = '# A\n\none two\n\nthree four\n'
    first = summarize(doc, total_budget=2, sections=['a'])
    second = summarize(doc, total_budget=2, sections=['a'])
    chosen = [b.text for b in first.sections[0].selected]
    assert chosen == ['one two']  # document order breaks the tie
    assert chosen == [b.text for b in second.sections[0].selected]


def test_priority_overrides_tie():
    doc = '# A\n\none two\n\nthree four\n'
    result = summarize(doc, total_budget=2, sections=['a'],
                       priorities={'a': 0})
    assert [b.text for b in result.sections[0].selected] == ['one two']


def test_unknown_section_raises():
    try:
        summarize(DOC, total_budget=10, sections=['nope'])
    except ValueError as e:
        assert 'nope' in str(e)
    else:
        raise AssertionError('expected ValueError')


def test_result_to_dict_json():
    result = summarize(DOC, total_budget=10)
    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert payload['total_budget'] == 10
    assert payload['total_used'] <= 10
    assert all('reason' in s for sec in payload['sections']
               for s in sec['skipped'])


def test_cli_json(tmp_path, capsys):
    doc = tmp_path / 'doc.md'
    doc.write_text(DOC, encoding='utf-8')
    assert cli_run([str(doc), '--budget', '20', '--json']) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['total_used'] <= 20
