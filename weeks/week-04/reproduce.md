# 启动命令与依赖版本

第四周 4.2 的版本对照、人工选源和原库复跑从[个人后端](https://github.com/lanF-cyber/rsi-data-spring-boot)与[个人前端](https://github.com/lanF-cyber/rsi-data-platform)启动。下文记录第四周 4.1 当时的环境。

适用 Windows x64 和 PowerShell 7.3 及以上。Next.js 页面使用本机 3000 端口，Spring Boot 使用 30845 端口，Python 调度器使用 3216 端口。首次复现可选择“记录动作复跑”，直接核验工具动作和原库结果。

## 依赖

| 组件 | 验证版本 | 用途 |
| --- | --- | --- |
| nvm-windows、Node.js | nvm 1.2.2，Node 24.14.1 和 18.20.8 x64 | Node 24 运行平台与 pnpm，Node 18 构建可视化库 |
| 前端 | pnpm 11.25.0、Next.js 16.2.6、React 19.2.4 | 平台开发服务 |
| 画布 | `@horsecoder/visual-core` 0.3.12、esbuild 0.17.19 | 构建本地依赖并核对安装副本的 SHA-256 |
| Java | Amazon Corretto 8.514.01.1（`1.8.0_514`）、Maven 3.9.16 | 编译并启动后端 |
| 后端依赖 | Spring Boot 2.1.9.RELEASE、flow-spring-boot-starter 0.4.14、scheduler-core 0.1.2、H2 1.4.199 | 流程引擎及单机事务 |
| CoSQL 调度 | Python 3.13.7，标准库 `sqlite3` | 巡检、内部试查、交付和独立核对 |
| 开发工具 | VS Code 1.147.0、IntelliJ IDEA 2025.3.3、Git for Windows 2.56.1、Chrome 157 | 编辑、源码检出、浏览器验收 |

| 上游 | 固定提交 | 本次改动 |
| --- | --- | --- |
| [rsi-data-spring-boot](https://github.com/ymxiong/rsi-data-spring-boot/commit/58051d9b7e679e9730e0a431fc5229cc01f972dd) | `58051d9` | 流程节点注册输出，并派发 Harness 任务 |
| [rsi-data-platform](https://github.com/ymxiong/rsi-data-platform/commit/29ecb6c0b3abe32a5df2a649b22085377c86fce7) | `29ecb6c` | 新增 `/harness` 路由，沿用开发代理 |
| [rsi-data-visual](https://github.com/ymxiong/rsi-data-visual/commit/b80f121c3752f5cb2b566983f0ba664c57c9d13c) | `b80f121` | 从固定源码构建 |

## 首次准备

在 Windows 安装 Git、nvm-windows、Python 3.10 或更高版本、VS Code、IntelliJ IDEA，并重新打开 PowerShell。下面的准备脚本通过 nvm 安装和选择指定 Node 版本，校验后解压 JDK、Maven 与 H2。[nvm-windows](https://github.com/nvm-windows/nvm/releases/tag/1.2.2) · [Git](https://git-scm.com/downloads/win) · [Python](https://www.python.org/downloads/windows/) · [VS Code](https://code.visualstudio.com/download) · [IntelliJ IDEA](https://www.jetbrains.com/idea/download/other.html)

```powershell
$lab = Join-Path $env:USERPROFILE 'rsi-lab'
$materials = New-Item -ItemType Directory -Force -Path "$lab\materials"
git -c core.autocrlf=false clone https://github.com/lanF-cyber/data-agent-research.git "$lab\report"
$task = Join-Path $lab 'report\weeks\week-04'
```

将课程提供的 `settings.xml` 和 `0.1.2.zip` 放入 `$materials`。前者用于读取课程 Maven 仓库，后者包含调度依赖的 JAR 与 POM。另将官方 `cosql_dataset.zip` 或其中的 `database/flight_2/flight_2.sqlite` 放入同一目录。脚本只取本题数据库并校验 SHA-256。[CoSQL 官方数据下载](https://yale-lily.github.io/cosql)

```powershell
& "$task\prepare.ps1" -Lab $lab
& "$task\start.ps1" -Lab $lab
```

准备脚本固定三个上游提交、在 Node 18 下构建画布、在 Node 24 下按锁文件安装依赖，再应用 Java 补丁并构建 `core-starter` 及其依赖模块。Maven 的测试跳过，验证使用下列真实请求。启动后输出三个本机进程号；运行日志在 `$lab\logs`。VS Code 打开 `$lab\rsi-data-platform`，IDEA 打开后端的 `pom.xml`，项目 SDK 和 Maven Runner JRE 均选择 `$lab\tools\jdk8`。[准备脚本](prepare.ps1) · [启动脚本](start.ps1)

## 浏览器验收

1. 打开 [本机 Harness 画布](http://127.0.0.1:3000/harness)，按 F12 打开 Console 和 Network。保持“记录动作复跑”和“启用专用查值工具”，点击“运行编排”。
2. 核对 `lookup_value → check_context → submit_answer`、2 次内部试查、62 行及“机场集合一致”。关闭专用查值再运行，预期 1 次内部试查、62 行。切换后，两次运行编号不同。
3. 在 Network 选中最后一个 `flow` 请求，检查 HTTP 200、外层 `result=true` 和 `status=200`。解析 `data.value.result` 中的 JSON 字符串，核对运行编号与页面一致。把同一请求地址改为 `http://127.0.0.1:30845/api/engine/flow` 重发，最终回执应相同。
4. 后端启动日志含 `Tomcat started on port(s): 30845` 与 `Started AppStarter`。可通过 [本机接口文档](http://127.0.0.1:30845/v2/api-docs) 核对服务标识 `dataTesting-core`。

“模型现场生成”读取启动调度器时进程环境中的 `RSI_HARNESS_TOKEN`。现场生成的查询步骤会随运行变化。记录动作模式可无凭据复现。

## 复现范围

数据库文件 `flight_2.sqlite` 的 SHA-256 为 `db777ba6488ae2b515cb138d71ff23c4533e5fd0355854648a1102faff3fb7af`。依赖下载速度与课程 Maven 仓访问条件会影响首次耗时。再次启动已准备好的环境只需执行 `start.ps1`。

2026-11-24 在新目录检出三个仓库，从空 Maven 本地仓和空项目 pnpm 存储开始复跑。Git、nvm 管理的两个 Node 版本、Python、编辑器、课程材料和题库已就绪。准备与构建用时 4 分 41 秒，服务启动至浏览器完成开关两组复验用时 17 秒。从准备开始至两组结束的墙钟时间为 **6 分 27 秒**，包含切换本机服务的间隔。两组均得到 62 个机场，页面异常与失败请求均为 0。[复现记录](../../evidence/week-04/verification.json)
