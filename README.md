# 数据 Agent 研究

## 第 4 周 4.2 · 数据 Agent 的多轮指代消解

本研究使用 CoSQL 多轮数据库问答。用户连续用英语提问，系统根据对话生成 SQL，并在原始数据库中执行。研究关注查询范围、库内存值和输出字段怎样影响结果。例如，上一轮问的是北美有哪些国家，错误答案只列出三国。下一轮查询“这些国家的官方语言”时，引用三国名单得到 4 行，引用北美全体得到 35 行。[专项研究报告](weeks/week-04/reference-resolution/README.md) · [14 道修复的前后对照](evidence/week-04/reference-cases.csv)

**我的后端项目**　[rsi-data-spring-boot](https://github.com/lanF-cyber/rsi-data-spring-boot)

**我的前端项目**　[rsi-data-platform](https://github.com/lanF-cyber/rsi-data-platform)

**评审入口**　本次专项实际测试了 184 个 CoSQL 问次、2 个人工对照输入和 10 个报表续问场景，共 196 个案例。其中 14 个问次有明确的修复前后对照，前 10 项沿用课程验收编号。界面另列本次分析引用的 86 个历史题号。启动后可按编号或题号运行，查看输入、流程、SQL 和结果。[编号案例](https://github.com/lanF-cyber/rsi-data-spring-boot/blob/homework/harness/cases.md) · [全部专项实验](https://github.com/lanF-cyber/rsi-data-spring-boot/blob/homework/harness/studies.md) · [本次公开版本复验](evidence/week-04/study-review.json)

## 第 4 周 4.1 · RSI 数据平台与 CoSQL Harness 联调

五节点画布调度了一道 CoSQL 原题。现场生成的 SQL 返回 62 个始发机场，与独立查询的完整机场代码集合一致。[运行截图与实验结果](weeks/week-04/README.md) · [启动命令与依赖版本](weeks/week-04/reproduce.md)

## 第 3 周 · CoSQL 多轮数据库问答

在 CoSQL 上比较 GPT-6 Sol 的提问、查库和交付方式，探索如何让正确率逐步逼近 100%。用同一批问题对照实验，逐题查清模型、参考答案和评分规则各会造成哪些失分。

[本周报告](weeks/week-03/README.md) · [具体错例](weeks/week-03/cases.md) · [复算与代码](weeks/week-03/reproduce.md)

### 同一批 101 问，五种做法

| 做法 | 怎样问、怎样查 | 按原规则通过 | 可执行 SQL 或核验表 |
| --- | --- | ---: | ---: |
| 1. 逐问新开会话 | 每次给表结构和前面问过的问题 | 75/101（74.26%） | 101 份 SQL |
| 2. 同一会话追问 | 脚本执行上轮 SQL，将查出的结果带入下一问 | **77/101（76.24%）** | 101 份 SQL |
| 3. 让模型自主查库 | 给 SQLite 文件，由模型用终端查库并交 SQL | 53/101（52.48%） | 96 份 SQL |
| 4. 改进自主查库提示 | 开场说明怎样接续追问、怎样交付，后续逐轮发原题 | 75/101（74.26%） | 100 份 SQL |
| 5. 由 MCP 交付结果 | 模型自主查库，MCP 执行最终 SQL，直接交结果表 | 66/101（65.35%） | 98 张表 |

原规则用 SQLite 执行模型 SQL 和参考 SQL，比较完整结果。第 5 种做法另有 3 问提出澄清。 [101 问逐题对照](evidence/week-03/paired-101.csv)

### 从五种试跑到两种扩测

从上表选了 **第 2 种和第 5 种**，在同一批 **143 段对话、502 问**上做扩测。

- **第 2 种 · 同一会话追问。** 101 问试跑中按原规则通过数最高。保留它作基线，按原流程先跑到 300 问，再追加 202 问。
- **第 5 种 · 自主查库，由 MCP 交付结果。** 模拟用户请 Agent 自己查库并拿到结果表的场景。补充精度和歧义处理要求后，完整跑了同一批 502 问。 [各轮试跑和扩样记录](weeks/week-03/README.md#实验怎样推进)

| 同一批 502 问，按什么给分 | 连续提问基线 | 自主查库 + MCP |
| --- | ---: | ---: |
| 按参考 SQL 的完整执行结果 | 343/502（68.33%） | 325/502（64.74%） |
| 按题意与原库事后复评 | 458/502（91.24%） | **492/502（98.01%）** |

### 评分与工程实际

我的判断是，固定参考表便于同题对照，实际用起来还要看答案能否直接帮用户解决问题。问战役日期时，附上战役名能说明对应关系，原规则会因多一列扣分。查航班时，模型和参考都返回 0，核对原库发现实际有 3 班。工程场景下，Agent 还需要指出题意缺哪项信息，或发现题目前提与库中记录对不上。按题意事后复评把这些情况纳入判分。 [具体题目和查证过程](weeks/week-03/cases.md)

## 周度索引

| 周次 | 研究主题 | 交付 |
| --- | --- | --- |
| 第 4 周 | RSI 环境与 CoSQL Harness 调度 | [报告与两张截图](weeks/week-04/README.md) · [复现](weeks/week-04/reproduce.md) · [运行回执](evidence/week-04/verification.json) |
| 第 3 周 | CoSQL 多轮问答、MCP 交付与失分归因 | [报告](weeks/week-03/README.md) · [错例](weeks/week-03/cases.md) · [复算](weeks/week-03/reproduce.md) |

周报告放在 `weeks/week-NN/`，用于复算的记录放在 `evidence/week-NN/`。CoSQL 实验代码集中在 [cosql/](cosql/)。
