# 人工画布与实验实现

下列文件记录 11 月对照实验使用的实现。当前运行入口位于[个人后端](https://github.com/lanF-cyber/rsi-data-spring-boot/tree/homework/harness)和[个人前端](https://github.com/lanF-cyber/rsi-data-platform/tree/homework/app/harness)。

| 研究问题 | 对应实现 |
| --- | --- |
| 用户怎样在 RSI 平台上指定历史来源、要继承的内容和输出字段？ | [画布界面](scope-editor/canvas.tsx)选择轮次和原话片段，[平台调用](scope-editor/flow.ts)将该结构交给已注册的 RSI 流程。[服务端](scope_service.py)取得当时的问答，[历史编译程序](../scope_harness.py)只把选中的信息交给生成环节。 |
| 为什么机场码或姓名都写对了，会漏查？ | [存值核验](value_catalog.py)检查空格、字段类型和真实写法，按库和字段记录结果。[版本对照](version_pair.py)分别运行旧流程和第三版，保留同一批用户输入的前后交付。 |
| 计算平均排名时，怎样处理没有排名的球员？ | [缺项处理](missing_policy.py)分别核对对象总数、有明细的对象数、有可用值的对象数。后续 Python 实验还检验了给原名单加列和另开新问题两种情况。 |

复算用到的 CoSQL 原库、巡检输入、完整对话与参考 SQL 已随个人后端交付。[从空会话运行](https://github.com/lanF-cyber/rsi-data-spring-boot#从空会话生成新答案) · [两组平台回执](../../../../evidence/week-04/platform-runs.json)
