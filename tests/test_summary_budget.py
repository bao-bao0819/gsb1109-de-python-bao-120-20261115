import json
import tempfile
import unittest
from pathlib import Path

from tools.summary_budget import (
    Chapter,
    CountPolicy,
    Fragment,
    _allocate,
    count_fragment,
    load_markdown_files,
    main,
    parse_markdown,
    plan,
    render_markdown,
)


class TestCounting(unittest.TestCase):

    def test_prose_words_ignore_markdown_markup(self):
        fragment = Fragment(id='p', chapter_id='c', text='A **bold** [link](https://example.com).')
        self.assertEqual(count_fragment(fragment, CountPolicy()), 3)

    def test_footnote_reference_follows_explicit_policy(self):
        fragment = Fragment(id='p', chapter_id='c', text='Claim.[^1]')
        self.assertEqual(count_fragment(fragment, CountPolicy()), 1)
        policy = CountPolicy(count_footnote_references=True)
        self.assertEqual(count_fragment(fragment, policy), 2)

    def test_code_counts_lines_without_fences_or_info_string(self):
        fragment = Fragment(id='code', chapter_id='c', kind='code', text='a = 1\n\nprint(a)')
        self.assertEqual(count_fragment(fragment, CountPolicy()), 2)

    def test_footnote_counts_words_without_marker(self):
        fragment = Fragment(id='n', chapter_id='c', kind='footnote', text='A note with four words.')
        self.assertEqual(count_fragment(fragment, CountPolicy()), 5)

    def test_cjk_runs_count_per_character(self):
        fragment = Fragment(id='p', chapter_id='c', text='中文测试')
        self.assertEqual(count_fragment(fragment, CountPolicy()), 4)
        self.assertEqual(count_fragment(fragment, CountPolicy(cjk_as_chars=False)), 1)

    def test_explicit_count_overrides_policy(self):
        fragment = Fragment(id='p', chapter_id='c', text='ignored', count=42)
        self.assertEqual(count_fragment(fragment, CountPolicy()), 42)

    def test_invalid_count_policy_field_is_rejected(self):
        with self.assertRaises(ValueError):
            CountPolicy.from_dict({'code': 'lines'})


class TestParseMarkdown(unittest.TestCase):

    def test_front_matter_is_not_a_selectable_block(self):
        text = '---\ntitle: Demo\n---\n\n# Intro\n\nBody text.'
        parsed = parse_markdown(text, 'demo')
        self.assertEqual([fragment.text for fragment in parsed.fragments], ['Body text.'])

    def test_headings_build_nested_chapters(self):
        text = '# One\n\nA.\n\n## Two\n\nB.\n\n# Three\n\nC.\n'
        parsed = parse_markdown(text, 'doc')
        by_title = {chapter.title: chapter for chapter in parsed.chapters}
        self.assertEqual(by_title['Two'].parent_id, by_title['One'].id)
        self.assertEqual(by_title['Three'].parent_id, parsed.chapters[0].id)
        self.assertEqual(by_title['Two'].heading_level, 2)

    def test_paragraphs_code_and_footnotes_are_separate_atomic_blocks(self):
        text = (
            '# Doc\n\n'
            'First paragraph.\n\n'
            '```python\nprint(1)\n```\n\n'
            'Second paragraph.[^1]\n\n'
            '[^1]: Footnote body.\n'
        )
        parsed = parse_markdown(text, 'doc')
        self.assertEqual(
            [fragment.kind for fragment in parsed.fragments],
            ['prose', 'code', 'prose', 'footnote'],
        )
        self.assertEqual(parsed.fragments[1].meta['lang'], 'python')
        self.assertEqual(parsed.fragments[3].meta['marker'], '1')

    def test_setext_heading_is_recognised(self):
        text = 'Title\n=====\n\nBody.'
        parsed = parse_markdown(text, 'doc')
        self.assertEqual(parsed.chapters[1].title, 'Title')
        self.assertEqual(len(parsed.fragments), 1)

    def test_load_multiple_files_uses_sibling_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / 'one.md'
            second = Path(tmp) / 'two.md'
            first.write_text('# A\n\nAlpha.', encoding='utf-8')
            second.write_text('# B\n\nBeta.', encoding='utf-8')
            parsed = load_markdown_files([first, second])
        roots = [chapter for chapter in parsed.chapters if chapter.parent_id is None]
        self.assertEqual([root.title for root in roots], ['one', 'two'])


class TestAllocation(unittest.TestCase):

    def test_proportional_allocation_is_exact_and_stable(self):
        parts = [('a', 1.0), ('b', 1.0), ('c', 1.0)]
        self.assertEqual(_allocate(10, parts), {'a': 4, 'b': 3, 'c': 3})
        self.assertEqual(sum(_allocate(10, parts).values()), 10)

    def test_zero_weights_receive_no_budget(self):
        self.assertEqual(_allocate(6, [('a', 0.0), ('b', 0.0)]), {'a': 0, 'b': 0})
        self.assertEqual(_allocate(6, [('a', 0.0), ('b', 1.0)]), {'a': 0, 'b': 6})

    def test_child_selection_is_charged_once(self):
        chapters = [
            Chapter(id='root', parent_id=None, self_weight=1.0),
            Chapter(id='child', parent_id='root', weight=1.0),
        ]
        fragments = [
            Fragment(id='p', chapter_id='root', text='one two three four'),
            Fragment(id='c', chapter_id='child', text='five six seven eight'),
        ]
        result = plan(chapters, fragments, budget=8)
        self.assertEqual(result.total_words, 8)
        self.assertEqual(result.unused_words, 0)
        by_id = {row.chapter.id: row for row in result.chapters}
        self.assertEqual(by_id['root'].own_words, 4)
        self.assertEqual(by_id['root'].subtree_words, 8)
        self.assertEqual(by_id['child'].own_words, 4)
        self.assertEqual(by_id['child'].subtree_words, 4)

    def test_parent_and_child_do_not_double_charge_budget(self):
        chapters = [
            Chapter(id='root', parent_id=None, self_weight=1.0),
            Chapter(id='child', parent_id='root', weight=1.0),
        ]
        fragments = [
            Fragment(id='p', chapter_id='root', text=' '.join(['p'] * 5)),
            Fragment(id='c', chapter_id='child', text=' '.join(['c'] * 5)),
        ]
        result = plan(chapters, fragments, budget=10)
        self.assertEqual(result.total_words, 10)
        self.assertEqual(result.unused_words, 0)
        charged = sum(decision.charged_words for decision in result.decisions)
        self.assertEqual(charged, 10)

    def test_duplicate_body_text_is_charged_only_to_first_owner(self):
        chapters = [
            Chapter(id='a', parent_id=None),
            Chapter(id='b', parent_id=None),
        ]
        fragments = [
            Fragment(id='a1', chapter_id='a', text='Repeated body.'),
            Fragment(id='b1', chapter_id='b', text='Repeated body.'),
        ]
        result = plan(chapters, fragments, budget=10)
        self.assertEqual(result.total_words, 2)
        duplicate = next(row for row in result.decisions if row.fragment_id == 'b1')
        self.assertFalse(duplicate.selected)
        self.assertEqual(duplicate.reason, 'duplicate')
        self.assertEqual(duplicate.canonical_id, 'a1')
        self.assertEqual(duplicate.charged_words, 0)

    def test_paragraph_cannot_be_split(self):
        chapters = [Chapter(id='a', parent_id=None)]
        fragments = [Fragment(id='p', chapter_id='a', text='one two three four five six seven')]
        result = plan(chapters, fragments, budget=5)
        self.assertEqual(result.total_words, 0)
        self.assertEqual(result.decisions[0].reason, 'indivisible_overflow')

    def test_priority_wins_before_size(self):
        chapters = [Chapter(id='a', parent_id=None)]
        fragments = [
            Fragment(id='low', chapter_id='a', text='one two three', priority=0, order=0),
            Fragment(id='high', chapter_id='a', text='four five six seven', priority=5, order=1),
        ]
        result = plan(chapters, fragments, budget=4)
        self.assertEqual([fragment.id for fragment in result.selected], ['high'])

    def test_equal_priority_equal_size_uses_earlier_source_order(self):
        chapters = [Chapter(id='a', parent_id=None)]
        fragments = [
            Fragment(id='first', chapter_id='a', text='one two three', priority=3, order=0),
            Fragment(id='second', chapter_id='a', text='four five six', priority=3, order=1),
        ]
        result = plan(chapters, fragments, budget=3)
        self.assertEqual([fragment.id for fragment in result.selected], ['first'])
        rejected = next(row for row in result.decisions if row.fragment_id == 'second')
        self.assertEqual(rejected.reason, 'stable_tie_lost')

    def test_equal_priority_different_size_prefers_full_use_of_quota(self):
        chapters = [Chapter(id='a', parent_id=None)]
        fragments = [
            Fragment(id='small', chapter_id='a', text='one two', priority=3, order=0),
            Fragment(id='large', chapter_id='a', text='three four five six', priority=3, order=1),
        ]
        result = plan(chapters, fragments, budget=4)
        self.assertEqual([fragment.id for fragment in result.selected], ['large'])

    def test_unselected_fragment_reports_lower_priority(self):
        chapters = [Chapter(id='a', parent_id=None)]
        fragments = [
            Fragment(id='high', chapter_id='a', text='one two', priority=9, order=0),
            Fragment(id='low', chapter_id='a', text='three four', priority=1, order=1),
        ]
        result = plan(chapters, fragments, budget=2)
        rejected = next(row for row in result.decisions if row.fragment_id == 'low')
        self.assertEqual(rejected.reason, 'lower_priority')

    def test_zero_quota_is_reported(self):
        chapters = [
            Chapter(id='a', parent_id=None),
            Chapter(id='b', parent_id=None, weight=50.0),
        ]
        fragments = [
            Fragment(id='a1', chapter_id='a', text='one two'),
            Fragment(id='b1', chapter_id='b', text='three four five six'),
        ]
        result = plan(chapters, fragments, budget=4)
        rejected = next(row for row in result.decisions if row.fragment_id == 'a1')
        self.assertEqual(rejected.reason, 'chapter_quota_zero')

    def test_unknown_parent_is_rejected(self):
        with self.assertRaises(ValueError):
            plan([Chapter(id='a', parent_id='missing')], [], budget=10)

    def test_cycle_is_rejected(self):
        chapters = [
            Chapter(id='a', parent_id='b'),
            Chapter(id='b', parent_id='a'),
        ]
        with self.assertRaises(ValueError):
            plan(chapters, [], budget=10)


class TestRenderingAndCli(unittest.TestCase):

    def _write_config(self, directory: str) -> Path:
        config = {
            'budget': 8,
            'count_policy': {'unit': 'words', 'code_unit': 'lines', 'footnote_unit': 'words'},
            'chapters': [
                {'id': 'root', 'title': 'Root'},
                {'id': 'child', 'title': 'Child', 'parent_id': 'root'},
            ],
            'fragments': [
                {'id': 'p', 'chapter_id': 'root', 'text': 'one two three four'},
                {'id': 'c', 'chapter_id': 'child', 'text': 'five six seven eight'},
            ],
        }
        path = Path(directory) / 'plan.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        return path

    def test_render_markdown_contains_only_selected_blocks(self):
        chapters = [Chapter(id='a', parent_id=None, title='A')]
        fragments = [
            Fragment(id='keep', chapter_id='a', text='keep these words', priority=9),
            Fragment(id='drop', chapter_id='a', text='drop these words', priority=0),
        ]
        result = plan(chapters, fragments, budget=3)
        rendered = render_markdown(result)
        self.assertIn('keep these words', rendered)
        self.assertNotIn('drop these words', rendered)

    def test_synthetic_root_renders_preamble_without_a_fake_heading(self):
        parsed = parse_markdown('Preamble text here.\n\n# One\n\nBody.\n', 'doc')
        result = plan(parsed.chapters, parsed.fragments, budget=10)
        rendered = render_markdown(result)
        self.assertTrue(rendered.startswith('Preamble text here.'))
        self.assertNotIn('# doc', rendered)
        self.assertIn('# One', rendered)

    def test_json_output_contains_decisions_and_quotas(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(tmp)
            output_path = Path(tmp) / 'out.json'
            self.assertEqual(
                main(['--config', str(config_path), '--output', str(output_path)]),
                0,
            )
            data = json.loads(output_path.read_text(encoding='utf-8'))
        self.assertEqual(data['budget'], 8)
        self.assertEqual(data['total_words'], 8)
        self.assertEqual(data['unused_words'], 0)
        self.assertEqual(len(data['chapters']), 2)
        self.assertTrue(all('reason' in row for row in data['decisions']))

    def test_no_text_omits_fragment_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = self._write_config(tmp)
            output_path = Path(tmp) / 'out.json'
            main(['--config', str(config_path), '--no-text', '--output', str(output_path)])
            data = json.loads(output_path.read_text(encoding='utf-8'))
        self.assertTrue(all('text' not in row for row in data['selected']))

    def test_markdown_cli_parses_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'doc.md'
            source.write_text('# Doc\n\none two three four five six.\n', encoding='utf-8')
            output_path = Path(tmp) / 'out.json'
            main(['--markdown', str(source), '--budget', '6', '--output', str(output_path)])
            data = json.loads(output_path.read_text(encoding='utf-8'))
        self.assertEqual(data['budget'], 6)
        self.assertEqual(data['total_words'], 6)


if __name__ == '__main__':
    unittest.main()
