# 第 3 周实验记录

| 记录 | 用途 |
| --- | --- |
| [101 问同题对照](paired-101.csv) | 按题排列五种流程的得分和交付情况，1 表示通过或已交付。 |
| [逐问耗时与 token](timing-and-tokens.csv) | 两批同题对照和其他小测的计时及用量，可复算效率表。 |
| [502 问样本计划](frozen-plan.json) | 对话顺序、题目、SQLite 版本、预先筛查规则。 |
| [502 问 MCP 预测](predictions.jsonl) | 逐问保存答复、最终 SQL、实际交付表、执行判分。 |
| [502 问原基线](../../cosql/reports/2026-11-19_continuous_expanded_v2/turns.jsonl) | 逐问保存 SQL、原库结果和比较记录。 |
| [159 问归因](baseline-159.csv) | 每个基线差异的主要类别和原因。 |
| [疑题核查](review-decisions.jsonl) | 提问歧义、缺少指代或参考查询疑点的核查理由。 |
| [事后复评](answer-review/decisions.jsonl) | 两版每问得分和依据。 |

发布版保留复算所需字段，去掉会话标识和账本字段。文件校验值对应发布版。

题目、参考 SQL 和原库派生数据来自 Tao Yu 等人的 CoSQL 1.0，本仓库按实验样本摘录并附上模型答复及判分。CoSQL 派生部分沿用 CC BY-SA 4.0。 [CoSQL 项目](https://yale-lily.github.io/cosql/) · [原论文](https://aclanthology.org/D19-1204/) · [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
