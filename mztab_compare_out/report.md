# mzTab 跨批次比较报告

## 批次与搜索引擎（来自元数据）

- `tests/test.mztab` — mzTab 1.0.0; 软件: MaxQuant 1.6.3.4
- `tests/test_mztab2.mztab` — mzTab 2.0.0-M; 软件: Xcalibur 2.8-280502/2.8.1.2806, Skyline 3.5.0.9319

## 可比较组与统一筛选结果

得分方向与筛选阈值见 `mztab_compare.py` 中的 `SCORE_DIRECTIONS` / `FILTERS`；原始分数与 CV 定义未做任何改写。

### G1_PSM_Andromeda（PSM，MaxQuant 1.6.3.4）
- 来源: PSM section
- 分数定义（保留原文）: `search_engine_score[1]` = "Andromeda:score"，方向: higher 更优
- 分数定义（保留原文）: `search_engine_score[2]` = "Andromeda:PEP"，方向: lower 更优
- 统一筛选: `Andromeda:PEP` <= 0.05
- 记录数: 1037；去重后独立证据: 390；重复证据行（只算一次支持）: 647
- 筛选结果: 通过 1037 / 未过 0 / 缺分数（单独列出，不计入最差） 0
- 缺失分数: "Andromeda:score" 缺失 0 条（与最差分数分开统计）
- 缺失分数: "Andromeda:PEP" 缺失 0 条（与最差分数分开统计）

### G2_protein_Andromeda（protein，MaxQuant 1.6.3.4）
- 来源: PRT section
- 分数定义（保留原文）: `best_search_engine_score[1]` = "protein group-level combined FDRscore"，方向: higher 更优
- 统一筛选: `protein group-level combined FDRscore` > 0.0
- 记录数: 283；去重后独立证据: 283；重复证据行（只算一次支持）: 0
- 筛选结果: 通过 282 / 未过 1 / 缺分数（单独列出，不计入最差） 0
- 缺失分数: "protein group-level combined FDRscore" 缺失 0 条（与最差分数分开统计）
- 缺失分数: "protein group-level combined FDRscore" 的 63 个 per-ms_run 分数列全部缺失，不参与排序与筛选（不作为最差处理）

### G3_small_molecule_evidence_Skyline（small molecule evidence，Xcalibur 2.8-280502/2.8.1.2806, Skyline 3.5.0.9319）
- 来源: SME section
- 分数定义（保留原文）: `id_confidence_measure[1]` = "fragmentation score"，方向: higher 更优
- 统一筛选: `fragmentation score` >= 0.5
- 记录数: 4；去重后独立证据: 2；重复证据行（只算一次支持）: 2
- 筛选结果: 通过 4 / 未过 0 / 缺分数（单独列出，不计入最差） 0
- 缺失分数: "fragmentation score" 缺失 0 条（与最差分数分开统计）

## 跨引擎说明

各组分数（Andromeda:score / Andromeda:PEP / FDRscore / fragmentation score）量纲与方向不同，且元数据中没有任何标定（calibration）关系，因此**不排跨引擎总榜**；`rank_within_group[*]` 仅在同一组、同一分数定义内有效。
