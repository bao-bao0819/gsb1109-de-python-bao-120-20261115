"""
Python Markdown

A Python implementation of John Gruber's Markdown.

Documentation: https://python-markdown.github.io/
GitHub: https://github.com/Python-Markdown/markdown/
PyPI: https://pypi.org/project/Markdown/

Started by Manfred Stienstra (http://www.dwerg.net/).
Maintained for a few years by Yuri Takhteyev (http://www.freewisdom.org).
Currently maintained by Waylan Limberg (https://github.com/waylan),
Dmitry Shachnev (https://github.com/mitya57) and Isaac Muse (https://github.com/facelessuser).

Copyright 2007-2023 The Python Markdown Project (v. 1.7 and later)
Copyright 2004, 2005, 2006 Yuri Takhteyev (v. 0.2-1.6b)
Copyright 2004 Manfred Stienstra (the original version)

License: BSD (see LICENSE.md for details).

Tests for optional source tracking (`source_tracking=True`).
"""

import unittest

import markdown
from markdown.sourcetrace import SourceTrace, TraceRecord, registry_name


class SourceTrackingTests(unittest.TestCase):
    """ Tests of the optional rule-source diagnostics. """

    SOURCE = (
        '# Heading **bold**\n'
        '\n'
        'A [link](http://example.com) and `code` and *em*.\n'
        '\n'
        '    indented\n'
    )

    def test_disabled_by_default(self):
        """ Without the option no records are collected. """
        md = markdown.Markdown()
        md.convert(self.SOURCE)
        self.assertEqual(md.trace.records, [])
        self.assertIsInstance(md.trace, SourceTrace)

    def test_output_is_identical(self):
        """ Tracking is observational and must not change the conversion. """
        without = markdown.markdown(self.SOURCE, extensions=['toc', 'attr_list'])
        md = markdown.Markdown(extensions=['toc', 'attr_list'], source_tracking=True)
        with_tracking = md.convert(self.SOURCE)
        self.assertEqual(with_tracking, without)

    def test_records_are_named_tuples(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('*em*')
        self.assertTrue(all(isinstance(r, TraceRecord) for r in md.trace.records))

    def test_only_rules_that_fire_are_recorded(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert(self.SOURCE)
        rules = {r.rule for r in md.trace.records}
        # Rules which actually consumed or replaced content.
        self.assertIn('hashheader', rules)
        self.assertIn('paragraph', rules)
        self.assertIn('em_strong', rules)
        self.assertIn('backtick', rules)
        self.assertIn('link', rules)
        # Rules which never fired must not masquerade as a source.
        self.assertNotIn('linebreak', rules)
        self.assertNotIn('html', rules)
        self.assertNotIn('autolink', rules)

    def test_inline_snippet_and_output(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('a **bold** b')
        record, = md.trace.for_rule('em_strong')
        self.assertEqual(record.stage, SourceTrace.INLINE)
        self.assertEqual(record.snippet, '**bold**')
        self.assertEqual(record.output, '<strong>bold</strong>')
        self.assertIsNone(record.original)
        self.assertIsNone(record.parent)

    def test_block_snippet_and_output(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('# Title')
        record, = md.trace.for_rule('hashheader')
        self.assertEqual(record.stage, SourceTrace.BLOCK)
        self.assertEqual(record.snippet, '# Title')
        self.assertEqual(record.output, '<h1>Title</h1>')

    def test_rejected_match_is_not_recorded(self):
        """ A rule which matches but rejects the span is not a source. """
        md = markdown.Markdown(source_tracking=True)
        md.convert('an `unclosed span')
        self.assertNotIn('backtick', {r.rule for r in md.trace.records})

        md = markdown.Markdown(source_tracking=True)
        md.convert('[missing][ref]')
        self.assertNotIn('reference', {r.rule for r in md.trace.records})

    def test_nested_inline_rewrite_keeps_original(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('A [**bold** link](http://example.com) here.')
        strong, = md.trace.for_rule('em_strong')
        link, = md.trace.for_rule('link')
        self.assertEqual(strong.snippet, '**bold**')
        self.assertEqual(strong.original, '[**bold** link](http://example.com)')
        self.assertEqual(strong.parent, md.trace.records.index(link))
        self.assertEqual(
            md.trace.lineage(strong),
            ['[**bold** link](http://example.com)', '**bold**']
        )
        # The enclosing rule has no parent of its own.
        self.assertIsNone(link.parent)

    def test_nested_block_rewrite_chain(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('> * [**x**](u)')
        # The deepest block rule is the paragraph inside the list item.
        paragraphs = [
            r for r in md.trace.for_stage('block') if r.rule == 'paragraph'
        ]
        inner = max(paragraphs, key=lambda r: len(md.trace.lineage(r)))
        self.assertEqual(
            md.trace.lineage(inner),
            ['> * [**x**](u)', '* [**x**](u)', '[**x**](u)']
        )

    def test_inline_chain_is_scoped_to_inline_phase(self):
        """ Inline rules nest among themselves, in a later pipeline phase. """
        md = markdown.Markdown(source_tracking=True)
        md.convert('> * [**x**](u)')
        link, = md.trace.for_rule('link')
        self.assertEqual(md.trace.lineage(link), ['[**x**](u)'])

    def test_lineage_with_duplicate_fragments(self):
        """ Identical fragment text must not confuse the parent chain. """
        md = markdown.Markdown(source_tracking=True)
        md.convert('**a** and **a** inside [**a**](u)')
        strong_records = md.trace.for_rule('em_strong')
        self.assertEqual(len(strong_records), 3)
        nested = [r for r in strong_records if r.parent is not None]
        self.assertEqual(len(nested), 1)
        self.assertEqual(md.trace.lineage(nested[0]), ['[**a**](u)', '**a**'])

    def test_pre_and_post_processors_recorded_on_change(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('before\n\n<div>raw</div>\n\nafter')
        self.assertIn('html_block', {r.rule for r in md.trace.for_stage('preprocess')})
        self.assertIn('raw_html', {r.rule for r in md.trace.for_stage('postprocess')})
        # An unchanged postprocessor does not claim the content.
        amp_records = md.trace.for_rule('amp_substitute')
        self.assertEqual(amp_records, [])

    def test_extensions_are_attributed(self):
        md = markdown.Markdown(extensions=['toc', 'attr_list'], source_tracking=True)
        md.convert('[TOC]\n\n# Title {#custom}')
        rules = {r.rule for r in md.trace.records}
        self.assertIn('toc', rules)
        self.assertIn('attr_list', rules)
        toc_record, = md.trace.for_rule('toc')
        self.assertEqual(toc_record.stage, SourceTrace.TREE)

    def test_reset_clears_records(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('a **b**')
        self.assertTrue(md.trace.records)
        md.reset()
        self.assertEqual(md.trace.records, [])
        # A fresh conversion starts clean and still tracks correctly.
        md.convert('plain')
        self.assertNotIn('em_strong', {r.rule for r in md.trace.records})

    def test_records_accumulate_between_resets(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('first')
        first_count = len(md.trace.records)
        md.convert('second')
        self.assertGreater(len(md.trace.records), first_count)

    def test_stage_and_rule_lookups(self):
        md = markdown.Markdown(source_tracking=True)
        md.convert('# H\n\n**x**')
        stages = {r.stage for r in md.trace.records}
        self.assertIn(SourceTrace.BLOCK, stages)
        self.assertIn(SourceTrace.INLINE, stages)
        self.assertTrue(all(r.stage == SourceTrace.BLOCK for r in md.trace.for_stage('block')))
        self.assertTrue(all(r.rule == 'em_strong' for r in md.trace.for_rule('em_strong')))


class RegistryNameTests(unittest.TestCase):
    """ Tests of the registry reverse lookup helper. """

    def test_returns_registration_name(self):
        md = markdown.Markdown()
        pattern = md.inlinePatterns['em_strong']
        self.assertEqual(registry_name(md.inlinePatterns, pattern), 'em_strong')

    def test_falls_back_to_class_name(self):
        class Unregistered:
            pass

        item = Unregistered()
        self.assertEqual(registry_name(markdown.Markdown().inlinePatterns, item), 'Unregistered')


if __name__ == '__main__':
    unittest.main()
