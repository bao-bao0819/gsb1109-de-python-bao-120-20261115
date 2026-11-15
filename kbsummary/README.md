# kbsummary — 章节摘要预算分配

`kbsummary` 为 Markdown 知识库的一组章节在**有限总字数预算**内各生成一份摘要。
核心约束与口径如下。

## 明确章节归属

每个正文片段（段落、代码块、脚注定义）只属于**最近的前置标题**所在章节；
首个标题之前的内容归属合成的 `root` 章节。同一片段绝不会同时属于两个章节，
因此全局长度统计中**每一片段只计一次**。

## 计数口径

- 单词计数：每个 CJK 字符计 1 词；其余按空白切分、至少含一个单词字符的
  token 计 1 词；行内脚注引用 `[^id]` 不计。
- 代码块与脚注定义按 `CountingRules` 给定口径计数：
  - `exclude`：不作为候选（输出中记 `excluded_kind`）；
  - `none`（默认）：作为候选但计 0 词；
  - `words`：按词计数；
  - `lines`（仅代码）：按非空行计数。

## 预算分配

`allocate_budget` 按权重把总预算分配给受预算章节：

- 显式权重（按章节 id 或标题传入）优先；
- 缺省权重为章节**自身**归属词数——父章节含子章节时，子章节的字数
  不重复计入父章节权重，即父子不重复占额度；
- 所有权重为 0 时均分。

## 片段选择与稳定性

- 段落不可拆：放不进剩余预算的段落整体跳过，记 `over_budget`。
- 选择顺序：优先级降序 → 词数升序 → 文档顺序。词数与优先级都相同时
  恒选文档中靠前者，结果稳定可复现。
- 父章节可把**未受预算**的后代章节片段并入候选（rollup）；一旦某后代
  自己持有预算，其父不再占用其片段。全局消费集合保证任一片段最多入选
  一次，冲突时记 `already_counted`。
- 每个未入选片段都带原因：`over_budget` / `already_counted` /
  `excluded_kind`。

## 用法

```python
from kbsummary import summarize, CountingRules

result = summarize(
    text,
    total_budget=500,
    sections=['intro', 'guide'],        # 缺省为全部章节
    weights={'intro': 2, 'guide': 1},   # 缺省按自身词数
    priorities={'guide': 1},
    rules=CountingRules(code='lines', footnotes='exclude'),
)
print(result.total_used)
for section in result.sections:
    print(section.section.id, section.used, section.budget)
```

命令行：

```sh
python -m kbsummary doc.md --budget 500 \
    --section intro --weight intro=2 \
    --code lines --footnotes exclude --json
```

测试：`python3 -m pytest tests/test_kbsummary.py`
