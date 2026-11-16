"""
Python Markdown

A Python implementation of John Gruber's Markdown.

Documentation: https://python-markdown.github.io/
GitHub: https://github.com/Python-Markdown/markdown/
PyPI: https://pypi.org/project/Markdown/

License: BSD (see LICENSE.md for details).

Tests for the source-mapped visible-text index.
"""

import markdown
from markdown import fulltext
from markdown.test_tools import TestCase


class TestVisibleText(TestCase):

    def test_plain_paragraph(self):
        visible = fulltext.extract('Hello world')
        self.assertEqual(visible.text, 'Hello world')
        self.assertEqual(visible.range(0, 5), fulltext.SourceRange(0, 5))

    def test_blocks_are_separated(self):
        self.assertEqual(fulltext.plain_text('one\n\ntwo'), 'one\ntwo')

    def test_soft_break_is_preserved(self):
        self.assertEqual(fulltext.plain_text('one\ntwo'), 'one\ntwo')

    def test_hard_break(self):
        self.assertEqual(fulltext.plain_text('one  \ntwo'), 'one\ntwo')

    def test_list_items(self):
        self.assertEqual(fulltext.plain_text('- one\n- two'), 'one\ntwo')

    def test_table_cells(self):
        source = '| a | b |\n| - | - |\n| c | d |'
        self.assertEqual(
            fulltext.plain_text(source, markdown.Markdown(extensions=['tables'])),
            'a b\nc d'
        )


class TestInlineRules(TestCase):

    def test_emphasis_markers_are_not_visible(self):
        visible = fulltext.extract('pre *bold* post')
        self.assertEqual(visible.text, 'pre bold post')
        self.assertEqual(visible.range(4, 8), fulltext.SourceRange(5, 9))

    def test_link_uses_label_not_destination(self):
        visible = fulltext.extract('[label](https://example.com "title")')
        self.assertEqual(visible.text, 'label')
        self.assertEqual(visible.range(0, 5), fulltext.SourceRange(1, 6))

    def test_image_uses_alt_text(self):
        visible = fulltext.extract('![*alt* **bold**](image.png "title")')
        self.assertEqual(visible.text, '*alt* **bold**')
        self.assertEqual(visible.range(0, 5), fulltext.SourceRange(2, 7))

    def test_image_alt_entities_are_expanded(self):
        visible = fulltext.extract('![a&amp;b](image.png)')
        self.assertEqual(visible.text, 'a&b')
        self.assertEqual(visible.range(1, 2), fulltext.SourceRange(3, 8))

    def test_image_alt_escaped_marker(self):
        self.assertEqual(fulltext.plain_text(r'![a\*b](image.png)'), 'a*b')

    def test_autolink_text(self):
        visible = fulltext.extract('<https://example.com>')
        self.assertEqual(visible.text, 'https://example.com')
        self.assertEqual(visible.range(0, 5), fulltext.SourceRange(1, 6))

    def test_automail_text(self):
        self.assertEqual(fulltext.plain_text('<me@example.com>'), 'me@example.com')

    def test_code_span_is_literal(self):
        visible = fulltext.extract('`a *b* <c>`')
        self.assertEqual(visible.text, 'a *b* <c>')

    def test_code_span_with_entities(self):
        self.assertEqual(fulltext.plain_text('`a &lt; b`'), 'a &lt; b')

    def test_character_references(self):
        visible = fulltext.extract('a &amp; b &#65;')
        self.assertEqual(visible.text, 'a & b A')
        self.assertEqual(visible.range(2, 3), fulltext.SourceRange(2, 7))
        self.assertEqual(visible.range(6, 7), fulltext.SourceRange(10, 15))

    def test_backslash_escape(self):
        visible = fulltext.extract(r'\*literal\*')
        self.assertEqual(visible.text, '*literal*')
        self.assertEqual(visible.range(0, 1), fulltext.SourceRange(0, 2))


class TestRawHtml(TestCase):

    def test_inline_element_text_is_visible(self):
        self.assertEqual(fulltext.plain_text('<span>hello</span> world'), 'hello world')

    def test_block_element_text_is_visible(self):
        self.assertEqual(fulltext.plain_text('<div>raw <b>html</b></div>'), 'raw html')

    def test_comments_are_hidden(self):
        self.assertEqual(fulltext.plain_text('<!-- hidden --> visible'), 'visible')
        self.assertEqual(fulltext.plain_text('a <!-- hidden --> b'), 'a b')

    def test_script_and_style_are_hidden(self):
        source = '<script>bad()</script> text <style>p { color: red }</style>'
        self.assertEqual(fulltext.plain_text(source), 'text')

    def test_textarea_is_hidden(self):
        self.assertEqual(fulltext.plain_text('<textarea>x</textarea>visible'), 'visible')

    def test_processing_instruction_is_hidden(self):
        self.assertEqual(fulltext.plain_text('<?php echo 1; ?>\n\ntext'), 'text')

    def test_declaration_is_hidden(self):
        self.assertEqual(fulltext.plain_text('<!DOCTYPE html>\n\ntext'), 'text')

    def test_image_alt_attribute_is_visible(self):
        source = 'before <img src="x.png" alt="A *picture*"> after'
        visible = fulltext.extract(source)
        self.assertEqual(visible.text, 'before A *picture* after')
        self.assertEqual(visible.range(7, 18), fulltext.SourceRange(29, 40))

    def test_quoted_angle_bracket_in_attribute(self):
        source = '<span data-tip="<kbd>Ctrl</kbd>">Anything</span>'
        self.assertEqual(fulltext.plain_text(source), 'Anything')

    def test_pre_content_preserves_whitespace(self):
        self.assertEqual(fulltext.plain_text('<pre>a  b\nc</pre>'), 'a  b\nc')

    def test_entity_in_raw_text(self):
        visible = fulltext.extract('<pre>a &amp; b</pre>')
        self.assertEqual(visible.text, 'a & b')
        self.assertEqual(visible.range(2, 3), fulltext.SourceRange(7, 12))

    def test_raw_image_alt_entity(self):
        visible = fulltext.extract('<img alt="a&amp;b" src="x.png">')
        self.assertEqual(visible.text, 'a&b')


class TestCodeBlocks(TestCase):

    def test_indented_code(self):
        visible = fulltext.extract('    code *x*\n    second')
        self.assertEqual(visible.text, 'code *x*\nsecond')
        self.assertEqual(visible.range(0, 4), fulltext.SourceRange(4, 8))

    def test_fenced_code(self):
        source = '```py\na < b\n```'
        visible = fulltext.extract(source, markdown.Markdown(extensions=['fenced_code']))
        self.assertEqual(visible.text, 'a < b')
        self.assertEqual(visible.range(0, 1), fulltext.SourceRange(6, 7))


class TestSourceRanges(TestCase):

    def test_match_across_emphasis(self):
        matches = fulltext.search('pre *bold* post', 'bold')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].source_range, fulltext.SourceRange(5, 9))

    def test_match_includes_marker_between_words(self):
        matches = fulltext.search('foo *bar* baz', 'foo bar')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].text, 'foo bar')
        self.assertEqual(matches[0].source_range, fulltext.SourceRange(0, 8))
        self.assertEqual(matches[0].synthetic, False)

    def test_match_crossing_synthetic_separator(self):
        matches = fulltext.search('one\n\ntwo', 'one\ntwo')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].source_range, fulltext.SourceRange(0, 8))
        self.assertEqual(matches[0].synthetic, True)

    def test_match_does_not_include_destination(self):
        matches = fulltext.search('[label](https://example.com)', 'label')
        self.assertEqual(matches[0].source_range, fulltext.SourceRange(1, 6))

    def test_synthetic_separator_has_no_source(self):
        visible = fulltext.extract('one\n\ntwo')
        self.assertEqual(visible.text, 'one\ntwo')
        self.assertEqual(visible.span(3), fulltext.Span())

    def test_no_match(self):
        self.assertEqual(fulltext.search('text', 'missing'), [])

    def test_regex_search(self):
        matches = fulltext.search('one two three', r't\w+', regex=True)
        self.assertEqual([m.text for m in matches], ['two', 'three'])

    def test_index_finditer(self):
        visible = fulltext.extract('a a a')
        self.assertEqual([m.start for m in visible.finditer('a')], [0, 2, 4])

    def test_slice_retains_mapping(self):
        visible = fulltext.extract('one two')
        partial = visible[4:7]
        self.assertEqual(partial.text, 'two')
        self.assertEqual(partial.range(0, 3), fulltext.SourceRange(4, 7))

    def test_empty_source(self):
        visible = fulltext.extract('')
        self.assertEqual(visible.text, '')
        self.assertEqual(visible.spans, [])


class TestMarkdownIntegration(TestCase):

    def test_method(self):
        md = markdown.Markdown(extensions=['tables'])
        visible = md.extract_text('| a |\n| - |\n| b |')
        self.assertEqual(visible.text, 'a\nb')

    def test_extensions_are_used(self):
        md = markdown.Markdown(extensions=['extra'])
        visible = md.extract_text('Term\n: Definition')
        self.assertEqual(visible.text, 'Term\nDefinition')
