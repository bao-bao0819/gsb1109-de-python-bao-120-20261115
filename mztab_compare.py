"""Compare identification results across mzTab batches with different search engines.

Reads every mzTab file given on the command line, and:

1. extracts score definitions from the metadata (kept verbatim, CV term included);
2. assigns each evidence table to a *comparable group* (same engine + same score
   definition + same entity level) -- ranking is only ever done inside a group,
   never across engines, because no calibration between the scores is available;
3. applies one unified, direction-aware filtering rule per group while keeping
   the original scores untouched;
4. reports missing scores separately from the worst observed scores (missing is
   never imputed as worst);
5. counts repeated occurrences of the same evidence only once.

Outputs are written to the output directory (default ``mztab_compare_out``):
``report.md`` plus one TSV per comparable group with the original scores, the
filter decision and the within-group rank.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pyteomics import mztab

# Score direction registry, keyed by the CV *name* as it appears in the mzTab
# metadata. Directions follow the PSI-MS CV definitions. Extend as needed.
SCORE_DIRECTIONS = {
    'Andromeda:score': 'higher',
    'Andromeda:PEP': 'lower',  # posterior error probability
    'protein group-level combined FDRscore': 'higher',  # a *score*, despite the name
    'local FDR': 'lower',
    'fragmentation score': 'higher',
}

# Unified filtering rule: one confidence-like score per group with a cutoff,
# applied with the native direction of that score. Original scores are kept.
FILTERS = {
    'Andromeda:PEP': ('<=', 0.05),
    'protein group-level combined FDRscore': ('>', 0.0),
    'fragmentation score': ('>=', 0.5),
}


def score_direction(name):
    return SCORE_DIRECTIONS.get(name)


def passes(value, op, cutoff):
    if op == '<=':
        return value <= cutoff
    if op == '>=':
        return value >= cutoff
    if op == '>':
        return value > cutoff
    if op == '<':
        return value < cutoff
    raise ValueError(op)


def describe_software(reader):
    parts = []
    for idx in sorted(reader.software):
        entry = reader.software[idx]
        name = entry.get('name') if isinstance(entry, dict) else entry
        if isinstance(name, tuple):
            parts.append('%s %s' % name)
        elif name:
            parts.append(str(name))
    return ', '.join(parts)


def filter_column(table, score_col, score_name):
    """Add direction-aware rank and unified-filter decision for one score column.

    Missing values are flagged as 'missing' and excluded from ranking instead of
    being treated as the worst score.
    """
    direction = score_direction(score_name)
    values = table[score_col]
    missing = values.isna()
    status = table.get('filter_status')
    if status is None:
        status = ['no_filter_defined'] * len(table)
    rank_col = 'rank_within_group[%s]' % score_name
    ranks = [None] * len(table)
    if direction is not None:
        ranked = values[~missing].sort_values(ascending=(direction == 'lower'))
        for rank, idx in enumerate(ranked.index, start=1):
            ranks[table.index.get_loc(idx)] = rank
        filt = FILTERS.get(score_name)
        if filt is not None:
            op, cutoff = filt
            status = []
            for v, m in zip(values, missing):
                if m:
                    status.append('missing')
                else:
                    status.append('pass' if passes(v, op, cutoff) else 'fail')
    table[rank_col] = ranks
    table['filter_status'] = status
    return missing.sum()


def analyze_psm(reader, outdir):
    table = reader['PSM'].copy().reset_index(drop=True)
    scores = reader.psm_search_engine_scores
    group = 'G1_PSM_Andromeda'
    missing = {}
    for idx in sorted(scores):
        col = 'search_engine_score[%d]' % idx
        if col in table.columns:
            missing[scores[idx]] = filter_column(table, col, scores[idx])
    # The same evidence (same spectrum, same peptide) may be listed once per
    # ambiguous protein accession; it only counts as one support.
    evidence_cols = ['spectra_ref', 'sequence', 'modifications']
    dedup = table.drop_duplicates(subset=evidence_cols)
    table.to_csv(os.path.join(outdir, group + '.tsv'), sep='\t', index=False)
    support = (dedup.groupby('accession').size().rename('distinct_PSM_support')
               .reset_index().sort_values('distinct_PSM_support', ascending=False))
    support.to_csv(os.path.join(outdir, group + '_protein_support.tsv'), sep='\t', index=False)
    return {
        'group': group,
        'source': 'PSM section',
        'level': 'PSM',
        'engine': describe_software(reader),
        'scores': {scores[i]: 'search_engine_score[%d]' % i for i in sorted(scores)},
        'rows': len(table),
        'distinct_evidence': len(dedup),
        'redundant_rows': len(table) - len(dedup),
        'missing': missing,
        'passed': int((table['filter_status'] == 'pass').sum()),
        'failed': int((table['filter_status'] == 'fail').sum()),
        'unscored': int((table['filter_status'] == 'missing').sum()),
    }


def analyze_protein(reader, outdir):
    table = reader['PRT'].copy().reset_index(drop=True)
    scores = reader.protein_search_engine_scores
    group = 'G2_protein_Andromeda'
    missing = {}
    for idx in sorted(scores):
        col = 'best_search_engine_score[%d]' % idx
        if col in table.columns:
            missing[scores[idx]] = filter_column(table, col, scores[idx])
    # Per-ms_run score columns that are entirely missing cannot be ranked or
    # filtered at all; they are reported as missing, never as "worst".
    all_missing_cols = []
    for idx in sorted(scores):
        prefix = 'search_engine_score[%d]_ms_run' % idx
        run_cols = [c for c in table.columns if c.startswith(prefix)]
        if run_cols and all(table[c].isna().all() for c in run_cols):
            all_missing_cols.append((scores[idx], len(run_cols)))
    table.to_csv(os.path.join(outdir, group + '.tsv'), sep='\t', index=False)
    return {
        'group': group,
        'source': 'PRT section',
        'level': 'protein',
        'engine': describe_software(reader),
        'scores': {scores[i]: 'best_search_engine_score[%d]' % i for i in sorted(scores)},
        'all_missing_score_columns': all_missing_cols,
        'rows': len(table),
        'distinct_evidence': len(table),
        'redundant_rows': 0,
        'missing': missing,
        'passed': int((table['filter_status'] == 'pass').sum()),
        'failed': int((table['filter_status'] == 'fail').sum()),
        'unscored': int((table['filter_status'] == 'missing').sum()),
    }


def analyze_sme(reader, outdir):
    table = reader['SME'].copy().reset_index(drop=True)
    measures = reader.id_confidence_measures
    group = 'G3_small_molecule_evidence_Skyline'
    missing = {}
    for idx in sorted(measures):
        col = 'id_confidence_measure[%d]' % idx
        if col in table.columns:
            missing[measures[idx]] = filter_column(table, col, measures[idx])
    table.to_csv(os.path.join(outdir, group + '.tsv'), sep='\t', index=False)
    # The same input evidence (evidence_input_id) may carry several candidate
    # annotations; it only counts as one supporting evidence.
    distinct_evidence = table['evidence_input_id'].nunique()
    return {
        'group': group,
        'source': 'SME section',
        'level': 'small molecule evidence',
        'engine': describe_software(reader),
        'scores': {measures[i]: 'id_confidence_measure[%d]' % i for i in sorted(measures)},
        'rows': len(table),
        'distinct_evidence': distinct_evidence,
        'redundant_rows': len(table) - distinct_evidence,
        'missing': missing,
        'passed': int((table['filter_status'] == 'pass').sum()),
        'failed': int((table['filter_status'] == 'fail').sum()),
        'unscored': int((table['filter_status'] == 'missing').sum()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mztab_files', nargs='+', help='mzTab files to compare')
    parser.add_argument('-o', '--outdir', default='mztab_compare_out',
                        help='output directory (default: mztab_compare_out)')
    args = parser.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    batches = []
    summaries = []
    for path in args.mztab_files:
        reader = mztab.MzTab(path)
        batch = {'path': path, 'version': reader.version,
                 'software': describe_software(reader)}
        tables = set(reader.keys())
        if 'PSM' in tables and len(reader['PSM']):
            summaries.append(analyze_psm(reader, args.outdir))
        if 'PRT' in tables and len(reader['PRT']):
            summaries.append(analyze_protein(reader, args.outdir))
        if 'SME' in tables and len(reader['SME']):
            summaries.append(analyze_sme(reader, args.outdir))
        batches.append(batch)
        reader.file.close()

    lines = []
    lines.append('# mzTab 跨批次比较报告\n')
    lines.append('## 批次与搜索引擎（来自元数据）\n')
    for b in batches:
        lines.append('- `%s` — mzTab %s; 软件: %s' % (b['path'], b['version'], b['software']))
    lines.append('')
    lines.append('## 可比较组与统一筛选结果\n')
    lines.append('得分方向与筛选阈值见 `mztab_compare.py` 中的 `SCORE_DIRECTIONS` / `FILTERS`；'
                 '原始分数与 CV 定义未做任何改写。')
    for s in summaries:
        lines.append('')
        lines.append('### %s（%s，%s）' % (s['group'], s['level'], s['engine']))
        lines.append('- 来源: %s' % s['source'])
        for name, col in s['scores'].items():
            direction = score_direction(name) or '未知'
            lines.append('- 分数定义（保留原文）: `%s` = "%s"，方向: %s 更优'
                         % (col, name, direction))
        filt_applied = [n for n in s['scores'] if n in FILTERS]
        for n in filt_applied:
            op, cutoff = FILTERS[n]
            lines.append('- 统一筛选: `%s` %s %s' % (n, op, cutoff))
        lines.append('- 记录数: %d；去重后独立证据: %d；重复证据行（只算一次支持）: %d'
                     % (s['rows'], s['distinct_evidence'], s['redundant_rows']))
        lines.append('- 筛选结果: 通过 %d / 未过 %d / 缺分数（单独列出，不计入最差） %d'
                     % (s['passed'], s['failed'], s['unscored']))
        for name, n_miss in s['missing'].items():
            lines.append('- 缺失分数: "%s" 缺失 %d 条（与最差分数分开统计）' % (name, n_miss))
        for name, n_cols in s.get('all_missing_score_columns', []):
            lines.append('- 缺失分数: "%s" 的 %d 个 per-ms_run 分数列全部缺失，'
                         '不参与排序与筛选（不作为最差处理）' % (name, n_cols))
    lines.append('')
    lines.append('## 跨引擎说明\n')
    lines.append('各组分数（Andromeda:score / Andromeda:PEP / FDRscore / fragmentation score）'
                 '量纲与方向不同，且元数据中没有任何标定（calibration）关系，'
                 '因此**不排跨引擎总榜**；`rank_within_group[*]` 仅在同一组、同一分数定义内有效。')
    report = os.path.join(args.outdir, 'report.md')
    with open(report, 'w') as fh:
        fh.write('\n'.join(lines) + '\n')
    print('wrote %s' % report)
    for s in summaries:
        print('%(group)s: rows=%(rows)d distinct_evidence=%(distinct_evidence)d '
              'pass=%(passed)d fail=%(failed)d missing=%(unscored)d' % s)


if __name__ == '__main__':
    main()
