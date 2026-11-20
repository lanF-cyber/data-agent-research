# 第 3 周复算指南

先用保存的 502 条答复复算结果。复算在本地 SQLite 上执行；重跑模型的入口在末节。

## 1. 准备同一版数据和工具

从 CoSQL 官方页面下载数据，解压到仓库的 `cosql/data/cosql_dataset/`。展开后应能看到 `sql_state_tracking/cosql_dev.json`、`tables.json` 和 `database/`。复算脚本核对开发集与 20 个 SQLite 文件的 SHA-256。 [官方数据下载](https://yale-lily.github.io/cosql/#getting-started) · [样本及版本](../../evidence/week-03/frozen-plan.json)

将三个依赖仓库放到 `cosql/upstream/` 的同名目录，检出表中的提交。

| 依赖目录 | 提交 |
| --- | --- |
| [research](https://github.com/LDlornd/research) | `e52531bb2fdb0d3b9ff4ffac32514034110f5d54` |
| [agent-session-analyzer](https://github.com/LDlornd/agent-session-analyzer) | `502da5b899667bd525241b4e3d0878f9e83540e8` |
| [test-suite-sql-eval](https://github.com/taoyds/test-suite-sql-eval) | `e97acc546ecbee8fa27fa8dbf025ef61493a876c` |

本周在 Windows、Python 3.13.7 上复算。从仓库根目录运行以下 PowerShell 命令。

```powershell
Set-Location cosql
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-eval.txt -r requirements-answer-mcp.txt python-dotenv==1.2.4
```

## 2. 复算成绩

```powershell
& .\.venv\Scripts\python.exe -B -X utf8 verify_research_cosql_502.py --predictions ..\evidence\week-03\predictions.jsonl
& .\.venv\Scripts\python.exe -B -X utf8 score_research_cosql_answers.py --evidence ..\evidence\week-03
```

第一条命令重算基线 **343/502**、MCP 版 **325/502** 的原规则成绩，并核验 **500 张**实际交付的表。第二条命令读两版已保存的答复，在库上核查并应用逐题复评依据，得到基线 **458/502**、MCP 版 **492/502**。 [预期结果](../../evidence/week-03/answer-review/summary.json) · [公开复算记录说明](../../evidence/week-03/README.md)

原规则在 CoSQL 原始 SQLite 上对照执行结果，适用于本周的同题对照。CoSQL 官方排行榜还使用 Test Suite Accuracy，对其他测试库执行结果进行比较。 [官方评测说明](https://yale-lily.github.io/cosql/)

## 3. 从头重跑一批

| 流程 | 入口 |
| --- | --- |
| 导读与早期手动评分 | [读三轮题](../../cosql/pilot_read.py) · [车型试题](../../cosql/score_one.py) · [政体三轮](../../cosql/pilot_dev106_turn123.py) |
| 逐题独立基线和 27 问对照 | [逐题基线](../../cosql/run_harness_baseline.py) · [27 问对照](../../cosql/run_harness_dialogue_comparison.py) |
| 连续提问与扩样 | [101 至 300 问](../../cosql/run_harness_continuous.py) · [追加至 502 问](../../cosql/run_expansion_v2.py) |
| 建表语句、值样例与脚本代查 | [25 问试验](../../cosql/run_grounded_pilot_v2.py) |
| 终端自主查库与提示改版 | [自主查库入口](../../cosql/run_autonomous_db_pilot.py) |
| MCP 定向试跑与 Research 批跑 | [13 问试跑](../../cosql/run_answer_submission_pilot.py) · [101 问](../../cosql/run_research_cosql.py) · [502 问](../../cosql/run_research_cosql_502.py) |

Research 批跑沿用 Windows PowerShell、SQLite 3.50.6、Node 24.14.1 和 DeepSeek Harness SDK 0.2.1-rc.2。模型路由从本机 DSH 凭据读取，Research 按目录管理各段对话的独立数据库副本。 [运行配置](../../cosql/research_config_502/agent.toml) · [DSH 环境定位](../../cosql/research_cosql.py)

```powershell
& .\.venv\Scripts\python.exe -B -X utf8 run_research_cosql_502.py prepare
& .\.venv\Scripts\python.exe -B -X utf8 run_research_cosql_502.py run --user-id '<Research 用户标识>' --workers 4
```
