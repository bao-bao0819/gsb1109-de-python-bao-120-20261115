"""Command line interface for kbsummary.

Usage:
    python -m kbsummary document.md --budget 500 [--json]
"""

from __future__ import annotations

import argparse
import json
import sys

from .core import CODE_MODES, FOOTNOTE_MODES, CountingRules, summarize


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='kbsummary',
        description='Summarize Markdown sections within a global word budget.')
    parser.add_argument('document', help='Markdown file to summarize')
    parser.add_argument('--budget', type=float, required=True,
                        help='total word budget across all sections')
    parser.add_argument('--section', action='append', default=None,
                        help='section id or title to budget (repeatable); '
                             'defaults to all sections')
    parser.add_argument('--weight', action='append', default=[],
                        metavar='SECTION=W',
                        help='explicit weight for a section (repeatable)')
    parser.add_argument('--priority', action='append', default=[],
                        metavar='SECTION=P',
                        help='priority for a section\'s fragments (repeatable)')
    parser.add_argument('--code', choices=CODE_MODES, default='none',
                        help='how code blocks count toward the budget')
    parser.add_argument('--footnotes', choices=FOOTNOTE_MODES, default='none',
                        help='how footnote definitions count toward the budget')
    parser.add_argument('--no-rollup', action='store_true',
                        help='do not roll unbudgeted descendants into a '
                             'section\'s candidates')
    parser.add_argument('--json', action='store_true',
                        help='emit the full result as JSON')
    return parser


def _parse_mapping(pairs: list[str], cast):
    mapping = {}
    for pair in pairs:
        key, sep, value = pair.partition('=')
        if not sep:
            raise SystemExit(f'expected KEY=VALUE, got {pair!r}')
        mapping[key] = cast(value)
    return mapping


def run(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    with open(args.document, encoding='utf-8') as f:
        text = f.read()
    result = summarize(
        text,
        total_budget=args.budget,
        sections=args.section,
        weights=_parse_mapping(args.weight, float),
        priorities=_parse_mapping(args.priority, int),
        rules=CountingRules(code=args.code, footnotes=args.footnotes),
        rollup=not args.no_rollup,
    )
    if args.json:
        json.dump(result.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write('\n')
    else:
        print(f'budget: {result.total_budget}  used: {result.total_used}')
        for section in result.sections:
            title = section.section.title or '(preface)'
            print(f'\n## {section.section.id} — {title} '
                  f'({section.used}/{section.budget:.0f} words)')
            for block in sorted(section.selected, key=lambda b: b.id):
                print(f'  + [{block.kind}:{block.words}w] '
                      f'{block.text.splitlines()[0][:70]}')
            for skipped in sorted(section.skipped,
                                  key=lambda s: s.block.id):
                print(f'  - [{skipped.block.kind}:{skipped.block.words}w] '
                      f'{skipped.reason}: '
                      f'{skipped.block.text.splitlines()[0][:60]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
