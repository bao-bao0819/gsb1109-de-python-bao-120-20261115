"""Budgeted summarization of Markdown knowledge bases."""

from .core import (
    CODE_MODES,
    FOOTNOTE_MODES,
    REASON_ALREADY_COUNTED,
    REASON_EXCLUDED_KIND,
    REASON_OVER_BUDGET,
    Block,
    CountingRules,
    Section,
    SectionSummary,
    SkippedBlock,
    SummaryResult,
    allocate_budget,
    count_words,
    parse_document,
    summarize,
)

__all__ = [
    'CODE_MODES',
    'FOOTNOTE_MODES',
    'REASON_ALREADY_COUNTED',
    'REASON_EXCLUDED_KIND',
    'REASON_OVER_BUDGET',
    'Block',
    'CountingRules',
    'Section',
    'SectionSummary',
    'SkippedBlock',
    'SummaryResult',
    'allocate_budget',
    'count_words',
    'parse_document',
    'summarize',
]
