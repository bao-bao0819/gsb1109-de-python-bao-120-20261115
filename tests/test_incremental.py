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

Python-Markdown Regression Tests
================================

Tests for the incremental, dependency-tracking renderer.
"""

import unittest
import markdown
from markdown.incremental import IncrementalMarkdown


class TestIncrementalBasics(unittest.TestCase):
    """ Basic rendering correctness. """

    def testPlainParagraphs(self):
        inc = IncrementalMarkdown()
        self.assertEqual(inc.convert('foo\n\nbar'), '<p>foo</p>\n<p>bar</p>')

    def testBlankInput(self):
        inc = IncrementalMarkdown()
        self.assertEqual(inc.convert(''), '')
        self.assertEqual(inc.convert('\n\n'), '')

    def testMatchesFullRender(self):
        text = (
            '# Title\n\n'
            'A [link][ref] and *emphasis*.\n\n'
            '- one\n- two\n\n'
            '[ref]: /url "Title"\n'
        )
        inc = IncrementalMarkdown()
        self.assertEqual(inc.convert(text), markdown.markdown(text, output_format='html'))

    def testReferenceDefinedAfterUse(self):
        inc = IncrementalMarkdown()
        html = inc.convert('A [link][ref].\n\n[ref]: /url')
        self.assertEqual(html, '<p>A <a href="/url">link</a>.</p>')

    def testReferenceKinds(self):
        inc = IncrementalMarkdown()
        html = inc.convert(
            '[full][a] [collapsed][] [shortcut] ![img][b]\n\n'
            '[a]: /a\n[collapsed]: /c\n[shortcut]: /s\n[b]: /b\n'
        )
        self.assertEqual(
            html,
            '<p><a href="/a">full</a> <a href="/c">collapsed</a> '
            '<a href="/s">shortcut</a> <img alt="img" src="/b"></p>'
        )

    def testFootnotes(self):
        inc = IncrementalMarkdown()
        html = inc.convert('Text[^1]\n\n[^1]: Note')
        self.assertIn('<sup id="fnref:1">', html)
        self.assertIn('<div class="footnote">', html)
        self.assertIn('<li id="fn:1">', html)

    def testFragmentsRecorded(self):
        inc = IncrementalMarkdown()
        inc.convert('one\n\ntwo\n\n[ref]: /url')
        self.assertEqual(len(inc.fragments), 2)
        self.assertEqual(inc.stats, {'fragments': 2, 'rendered': 2, 'reused': 0})
        self.assertEqual(inc.references, {'ref': ('/url', None)})


class TestDependencyTracking(unittest.TestCase):
    """ Each fragment records exactly the definitions it depends on. """

    def testReferenceDependencyRecorded(self):
        inc = IncrementalMarkdown()
        inc.convert('A [link][ref].\n\nNo links here.\n\n[ref]: /url')
        self.assertEqual(inc.fragments[0].dependencies, frozenset({('ref', 'ref')}))
        self.assertEqual(inc.fragments[1].dependencies, frozenset())

    def testFootnoteDependencyRecorded(self):
        inc = IncrementalMarkdown()
        inc.convert('Text[^a] and [^b].\n\n[^a]: A\n[^b]: B')
        self.assertEqual(
            inc.fragments[0].dependencies,
            frozenset({('fn', 'a'), ('fn', 'b')})
        )

    def testUndefinedReferenceIsADependency(self):
        inc = IncrementalMarkdown()
        inc.convert('A [missing][ref].')
        self.assertEqual(inc.fragments[0].dependencies, frozenset({('ref', 'ref')}))

    def testFootnoteReferencingFootnote(self):
        inc = IncrementalMarkdown()
        inc.convert('Text[^a].\n\n[^a]: See [^b]\n\n[^b]: Base')
        # Fragment depends on ^a directly and ^b transitively.
        self.assertEqual(
            inc.fragments[0].dependencies,
            frozenset({('fn', 'a'), ('fn', 'b')})
        )

    def testFootnoteReferencingLinkReference(self):
        inc = IncrementalMarkdown()
        inc.convert('Text[^a].\n\n[^a]: See [this][r]\n\n[r]: /url')
        self.assertEqual(
            inc.fragments[0].dependencies,
            frozenset({('fn', 'a'), ('ref', 'r')})
        )

    def testCyclicFootnotesTerminate(self):
        inc = IncrementalMarkdown()
        html = inc.convert('Text[^a].\n\n[^a]: See [^b]\n\n[^b]: See [^a]')
        self.assertEqual(
            inc.fragments[0].dependencies,
            frozenset({('fn', 'a'), ('fn', 'b')})
        )
        self.assertIn('fn:a', html)
        self.assertIn('fn:b', html)


class TestIncrementalRebuild(unittest.TestCase):
    """ Updates rebuild only the fragments affected by a change. """

    def testTailDefinitionChangeRebuildsOnlyUsers(self):
        inc = IncrementalMarkdown()
        v1 = 'First [link][ref].\n\nMiddle.\n\nLast.\n\n[ref]: /old'
        inc.convert(v1)
        self.assertEqual(inc.stats['rendered'], 3)

        v2 = 'First [link][ref].\n\nMiddle.\n\nLast.\n\n[ref]: /new'
        html = inc.convert(v2)
        # Only the fragment using `ref` is rebuilt; the index is not stale.
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertEqual(inc.stats['reused'], 2)
        self.assertTrue(inc.fragments[0].rebuilt)
        self.assertFalse(inc.fragments[1].rebuilt)
        self.assertFalse(inc.fragments[2].rebuilt)
        self.assertIn('href="/new"', html)
        self.assertNotIn('/old', html)

    def testUnusedDefinitionChangeRebuildsNothing(self):
        inc = IncrementalMarkdown()
        v1 = 'A [link][used].\n\n[used]: /a\n[unused]: /x'
        inc.convert(v1)

        v2 = 'A [link][used].\n\n[used]: /a\n[unused]: /y'
        html = inc.convert(v2)
        self.assertEqual(inc.stats['rendered'], 0)
        self.assertEqual(html, inc.convert(v1))

    def testRemovedUnusedDefinitionRebuildsNothing(self):
        inc = IncrementalMarkdown()
        inc.convert('A [link][used].\n\n[used]: /a\n[unused]: /x')
        html = inc.convert('A [link][used].\n\n[used]: /a')
        self.assertEqual(inc.stats['rendered'], 0)
        self.assertIn('href="/a"', html)

    def testRemovedUsedDefinitionRebuilds(self):
        inc = IncrementalMarkdown()
        inc.convert('A [link][ref].\n\n[ref]: /a')
        html = inc.convert('A [link][ref].')
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertNotIn('<a href', html)

    def testNewDefinitionForMissingRefRebuilds(self):
        inc = IncrementalMarkdown()
        inc.convert('A [link][ref].')
        html = inc.convert('A [link][ref].\n\n[ref]: /new')
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertIn('href="/new"', html)

    def testEditOneFragmentRebuildsOnlyIt(self):
        inc = IncrementalMarkdown()
        inc.convert('one\n\ntwo\n\nthree')
        inc.convert('one\n\nTWO\n\nthree')
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertTrue(inc.fragments[1].rebuilt)
        self.assertFalse(inc.fragments[0].rebuilt)
        self.assertFalse(inc.fragments[2].rebuilt)

    def testFootnoteDefinitionChangeRebuildsUsers(self):
        inc = IncrementalMarkdown()
        v1 = 'Text[^a].\n\nOther.\n\n[^a]: Old note'
        inc.convert(v1)

        v2 = 'Text[^a].\n\nOther.\n\n[^a]: New note'
        html = inc.convert(v2)
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertTrue(inc.fragments[0].rebuilt)
        self.assertFalse(inc.fragments[1].rebuilt)
        self.assertIn('New note', html)
        self.assertNotIn('Old note', html)

    def testTransitiveFootnoteChangeRebuildsUser(self):
        inc = IncrementalMarkdown()
        v1 = 'Text[^a].\n\n[^a]: See [^b]\n\n[^b]: Old base'
        inc.convert(v1)

        v2 = 'Text[^a].\n\n[^a]: See [^b]\n\n[^b]: New base'
        html = inc.convert(v2)
        # ^a is unchanged, but the fragment transitively depends on ^b.
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertIn('New base', html)

    def testIndirectFootnoteChangeLeavesOthersCached(self):
        inc = IncrementalMarkdown()
        v1 = 'First[^a].\n\nSecond[^b].\n\n[^a]: A\n[^b]: B'
        inc.convert(v1)

        v2 = 'First[^a].\n\nSecond[^b].\n\n[^a]: A2\n[^b]: B'
        inc.convert(v2)
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertTrue(inc.fragments[0].rebuilt)
        self.assertFalse(inc.fragments[1].rebuilt)

    def testRepeatedConvertReusesEverything(self):
        inc = IncrementalMarkdown()
        text = 'A [link][ref].\n\nText[^1].\n\n[ref]: /url\n\n[^1]: Note'
        inc.convert(text)
        html = inc.convert(text)
        self.assertEqual(inc.stats['rendered'], 0)
        self.assertEqual(html, inc.convert(text))

    def testLongDocumentTailEdit(self):
        # Simulate a long document: 50 paragraphs, one link definition at the tail.
        paras = ['Paragraph %d with [link][ref].' % i if i == 0
                 else 'Paragraph %d.' % i for i in range(50)]
        v1 = '\n\n'.join(paras) + '\n\n[ref]: /old'
        inc = IncrementalMarkdown()
        inc.convert(v1)
        self.assertEqual(inc.stats['rendered'], 50)

        v2 = '\n\n'.join(paras) + '\n\n[ref]: /new'
        html = inc.convert(v2)
        self.assertEqual(inc.stats['rendered'], 1)
        self.assertEqual(inc.stats['reused'], 49)
        self.assertIn('href="/new"', html)


class TestReset(unittest.TestCase):

    def testResetClearsCache(self):
        inc = IncrementalMarkdown()
        inc.convert('A [link][ref].\n\n[ref]: /url')
        inc.reset()
        self.assertEqual(inc.fragments, [])
        self.assertEqual(inc.references, {})
        inc.convert('A [link][ref].\n\n[ref]: /url')
        self.assertEqual(inc.stats['rendered'], 1)


if __name__ == '__main__':
    unittest.main()
