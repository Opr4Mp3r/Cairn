# Builder：原生 Skill 开题与后台审计研究

交互开题使用项目内的 [cairn-builder Skill](../.claude/skills/cairn-builder/SKILL.md)。Claude Code 当前会话负责问答、Kritt 只读取材、源码阅读和 AuditPlan 修订；独立 Controller 负责 preflight、seed review 与研究监督。启动成功后 Skill 交付网页并结束开题，后台进程继续 Explore/Reason、旁路监督、独立结案和归档。

原 `cairn builder` 保留为已有自动化的批处理兼容入口。研究与结案语义沿用 [v0.5 实现方案](specs/kritt-audit-reconnect-mvp.md)；历史 generic 入口见 [v0.3 记录](specs/kritt-builder-mvp.md)。

## 首次准备

在仓库根目录执行：

```sh
uv sync --project cairn --python 3.13
cp dispatch.builder.example.yaml dispatch.builder.yaml
```

已有本地配置时直接修改，避免覆盖。默认使用本机已登录、可调用的 `claude`，复用其模型配置。Cairn 本地运行无需 Docker；Kritt 数据库可以继续运行在原有 Docker 环境。

编辑 `controller.kritt.targets` 中的目标：

| 配置 | 内容 |
|---|---|
| 目标键，如 `zentao` | 命令的 `--target` 所选择的名称 |
| `repo_full` | Kritt 中的仓库名，如 `easysoft/zentaopms` |
| `scan_id`、`commit` | 选定的已完成扫描及版本；CLI scan 覆盖配置 scan，commit 仍需匹配 |
| `source_root` | 对应该 scan/commit 的完整本地源码 |
| `source_manifest` | 与源码匹配的外置 manifest，放在源码目录之外 |
| `references`（可选） | UTF-8 安全、部署、威胁模型等本地资料路径 |

路径相对于配置文件所在目录解析。沿用已准备好的源码及 manifest 即可；格式见 [Kritt 源码准备说明](kritt-probe.md)。使用禅道大小写敏感源码卷时先挂载。切换 scan 不会自动下载或切换源码；删除 scan/commit 固定项以选择最新已完成扫描时，也必须同步准备对应源码。

在运行终端配置只读连接，地址和账号使用实际值，密码通过本机连接配置提供：

```sh
export CAIRN_KRITT_READONLY_DSN='host=127.0.0.1 port=5433 dbname=open_kritt user=YOUR_READONLY_USER'
```

使用数字 IP、Unix socket，或主机名加数字 `hostaddr`；当前实现不自行解析 DNS。账号提供必要表的 SELECT。Kritt 查询工具只访问本次固定视图，不使用 DSN，也不写 Kritt 数据库。Controller 从后台 Worker 的环境中排除配置的 DSN 变量；原生 Claude Code 会话继承本机环境，不提供同样的权限隔离。

`controller.research_prompt_group` 必须为已配置的审计提示组，示例使用 `audit`；`runtime.prompt_group: default` 继续用于普通项目。参考文档、源码和查询材料供 Agent 按需读取，完整文件指纹由程序核验。

## 角色资源池

使用 `/cairn-builder` 时，Claude Code 会先展示当前资源池，主动询问本次沿用还是调整，并按你的自然语言答复直接修改配置。例如：“Explore 用 3 个 Claude Code、2 个 Grok，其余保持当前配置。”没有资源池时，会提出七个角色各一个 Claude Code 的初始方案供选择。已明确的选择不会再次确认，配置后继续开题；也可以只要求配置资源池而不启动研究。

Skill 只调整所选文件的 `role_pools`，保留已有目标、Kritt、预算和超时，展示最终分配与合计，并让 `prepare`、`start` 使用同一文件。新运行会把有关问答记入 `requirements.json`。已有任务不热更新，修改用于下一次启动。

资源池用于 `runtime.execution: local`。在顶层 `role_pools` 中为每个角色填写可用 harness 及数量。示例默认所有角色各用一个 `claudecode`；若本机 `claude`、`grok` 均已登录并可调用，可这样配置：

```yaml
role_pools:
  explore: {claudecode: 3, grok: 2}
  reason: {claudecode: 1}
  preflight: {claudecode: 1}
  seed_review: {claudecode: 1}
  overseer: {claudecode: 1}
  closing: {claudecode: 1}
  final_review: {claudecode: 1}
```

Explore 有五个名额：同一项目的待研究 Intent 足够时，最多同时启动三个 Claude Code 和两个 Grok CLI。数量按角色、按项目分别计算；相同 harness 在不同角色池中的名额独立。名额表示可同时执行的任务数，任务结束即释放，不预先启动常驻 Agent。

| 角色 | 同一项目的执行方式 |
|---|---|
| Explore | 不同 Intent 并行，结果写回同一任务图 |
| Seed Review | 将种子拆成小批并行复核，全部汇总后继续 |
| Overseer | 不同事实或研究方向并行复核 |
| Reason、Preflight、Closing、Final Review | 各角色保持串行，池用于选择执行者 |

原生 Builder 使用当前 Claude Code 会话，不在后台资源池中。新任务使用独立 session；同一任务需要续接时保持原 harness 与 session。各 harness 的模型、provider 和登录由用户通过 CCSwitch 或本机 CLI 管理，`role_pools` 不配置 `model`。

容量直接等于该角色各 harness 数量之和，没有额外的角色总并发字段。资源池模式自动生成内部 Worker 和调度容量，无需再写 `workers`、`controller.roles`、`runtime.max_workers` 或 `runtime.max_project_workers`；`runtime.max_running_projects` 仍控制同时运行的项目数，示例保留为 `1`。

资源池模式不使用旧 `workers`、`controller.roles` 或 `common_env` 作为调度或模型配置来源；进程仍继承本机环境，登录和代理配置沿用本机 CLI。需要代理时配置启动 Cairn 的终端环境。未启用 `role_pools` 的旧配置继续保留 Worker 与 `common_env` 合并行为。

## 在 Claude Code 中开题

在本仓库目录打开 Claude Code，调用项目 Skill：

```text
/cairn-builder 使用 zentao 目标，静态梳理 module/install/control.php 中构造函数的进入和终止条件，给出表达式与行号；部署和匿名可达性未知，保留为未知。
```

项目的 `.claude/skills/cairn-builder/` 是原生安装目录；`.claude/artifacts/cairn-builder.skill` 是分发包，不需要额外 MCP、插件框架或嵌套 Agent。薄脚本 `scripts/cairn.py` 从自己的位置定位仓库，使用 `cairn/.venv`，可从任意工作目录执行。

Skill 在原会话中处理必要追问，并将问题和答复追加到本 run 的 `requirements.json`。目标应写清效果、部署、攻击者能力和范围；已给出的条件会被保留，未知项仍标为未知。取材只用于开题，适合源码理解、调用链/鉴权梳理、复核既有候选及进一步研究。当前没有统一的应用部署、动态 PoC 或正式漏洞报告生成器。

## prepare / start 接口

Skill 使用以下两个接口，也可在仓库根目录手动执行。

先固定输入、核验源码并保存 schema；此步骤不调用模型、不创建研究项目：

```sh
uv run --project cairn cairn prepare \
  --config dispatch.builder.yaml --target zentao --scan-id 19 \
  --goal '静态梳理 module/install/control.php 中构造函数的进入和终止条件，给出表达式与行号；部署和匿名可达性未知，保留为未知。' \
  --run-dir /absolute/path/to/new-run
```

`--run-dir` 必须是源码目录之外、尚不存在的新目录。`--scan-id` 可省略，使用配置中的选择。本 run 的 `run.json` 和命令返回的 JSON 保存 `plan_schema`、需求/参考资料路径及 `kritt_query_command`；读取 schema 后，将原始 AuditPlan JSON 写入本 run 的 `plan.json`，不使用旧 `ask`/`finish` 封装。

查询时直接经 Bash 调用返回的完整 `kritt_query_command`，向 stdin 传入 `describe`、`search` 或 `get` JSON，再用 Read 阅读返回的材料路径。查询复用已有 Kritt 只读 CLI，不需用 ToolSearch 寻找 Read/Bash。所有种子初始为 `unverified`，代码判断不能藏入 origin 或 policy 的已知前提。

离线材料可显式加 `--view /path/to/view.json`，必须是匹配目标的 Cairn Kritt 适配视图，普通 findings JSON 或 ZIP 不能替代。离线模式不会读取在线 PG；保存的视图只表示本次输入，不等于数据库最新结果。目标、scan、commit、源码或连接不匹配时先解决阻塞，不自动换目标或猜测连接。

保存方案后提交同一个 run：

```sh
uv run --project cairn cairn start \
  --config dispatch.builder.yaml --run-dir /absolute/path/to/run
```

`start` 启动后台监督进程，等到独立预审结果或项目启动后返回 JSON：

| 返回情形 | 当前会话的下一步 |
|---|---|
| `status=needs_revision` | 读取 `issues`，保留既有取材，在原会话修订 `plan.json`；必要时补充并保存需求，然后对同一 run 重提 `start` |
| `status=researching` | 交付 `project_url`、run 目录、`progress` 和 `log` 路径，结束开题 |
| `status=completed` / `budget_exhausted` / `stopped` | 研究已快速结束、服务已关闭；报告实际终态和 `results` 中的结果文件，不将旧 `project_url` 当作仍可访问的服务 |
| 执行错误 | 查看错误与日志；执行失败不代表证据被反驳或目标不成立 |

preflight 与 seed review 使用独立审核会话，开题会话不得自审。已有项目的 run 禁止再次 `start`，直接查看已有网页或进度；需要新任务时使用新的 run 目录。

## 查看进度与停止

打开返回的 `project_url` 查看任务图。后台进程保有本次 Server、Dispatcher 与 Controller 的生命周期；`start` 命令返回和 Skill 会话结束不会停止研究。本机仍需保持运行，研究终止后会回收本次服务。

返回的 `progress` 指向 `research.json`，`log` 指向 `launch.log`，`supervisor_pid` 标识本次监督进程。`research.json` 保存研究状态、服务与项目路径；主要状态字段包括：

- `project_status`：真实项目状态；`status` 还可能表示启动器的 researching、failed 或 interrupted。
- `audit_phase`：preparing、researching、draining、finalizing 或 closed。
- `fact_count`：导入种子与新研究事实的总数，不含 origin/goal。
- `open_intent_count`：尚无结论的研究方向。
- `termination_reason`：审计终止原因，运行中通常为空。

Reason 的 Complete 只提出申请；独立终审批准后才是 `completed`。旁路 Overseer 复核事实及研究方向，通过状态和 Hint 纠偏。未验证种子可以作为研究对象，不能成为已确认的完成证明。

| 终态 | 含义 |
|---|---|
| `completed` | 独立终审批准满足本任务固定 policy；静态任务完成不等于漏洞被确认 |
| `budget_exhausted` | 有限研究/结案预算结束，保留已有成果；不表示证明无漏洞 |
| `stopped` | 用户停止或执行错误后的项目停止；错误详情另存 |

可在图页面停止项目；需要终止本次监督进程时，对返回的 `supervisor_pid` 发送 SIGTERM（`kill -TERM PID`），由该进程清理本次拥有的服务和 Agent。draining 不是终态：系统停止新派发，允许已开始任务有限收尾，再由 Closing 检查最终图中的候选并走独立终审。

## 时间配置

示例使用已有字段，按实际模型耗时调整：

| 配置 | 示例秒数 |
|---|---:|
| `controller.timeouts.build` | 300 |
| `controller.timeouts.preflight`、`seed_review` | 240 |
| `controller.timeouts.overseer`、`closing` | 180 |
| `controller.timeouts.final_review` | 240 |
| `tasks.reason.timeout`、`tasks.explore.timeout` | 120、300 |
| `controller.budget.research_seconds` | 1800 |
| `controller.budget.drain_seconds` | 120 |
| `controller.budget.final_review_seconds` | 600 |

`controller.timeouts.build` 仅约束旧批处理的内部 Builder 调用，不限制原生 Skill 的用户问答。单次审核/研究调用时限包含其内部读文件和工具循环；不因每次查询重置。audit 预算从项目激活时开始，三段绝对期限保存在 Server 中。复核、Closing、终审还受剩余总期限约束，配置的单次时限不是每个阶段都能获得的保留时长。当前没有 Token 总预算或准备阶段累计预算。

## 保存结果与复盘

后台研究结束后在状态文件的 `results` 中记录五个结果文件路径。每次运行目录保存：

| 文件/目录 | 内容 |
|---|---|
| `run.json`、`requirements.json` | 固定输入、repo/scan/commit、准备产物位置，以及原会话保存的需求/答复 |
| `audit-plan.schema.json` | 本次 AuditPlan 的 JSON schema |
| `plan.json` | 最终 AuditPlan、policy、种子及初始 Intents |
| `kritt-view.json`、`queries/`、`query-receipts.json` | 固定输入视图、每次查询与分页返回 |
| `selected-materials.json` | 完整 get 返回的材料；不代表模型全部读过或结论已验证 |
| `source-baseline.json`、`references.json`、`references/` | 核验基线及一次保存的参考资料 |
| `project.json` | 项目 ID、seed key 到 Fact ID 映射、工作目录 |
| `research.json` | 服务进程、运行/项目状态、阶段、终止原因、结果位置及错误 |
| `launch.json`、`launch.log` | 后台监督进程的启动/修订/研究状态与日志 |
| `error.json` | Builder 已创建运行目录后的准备错误；此时可能尚无研究数据库或 `research.json` |
| `result.json` | 结束时的项目、Facts、Intents 和 Hints |
| `graph.yaml`、`timeline.txt` | 最终图及图变化时间线 |
| `audit.json` | policy、基线、预算期限、审核记录和 Closing 状态 |
| `completions.json` | 完成申请、证明来源、审核 session、裁决与执行错误；没有申请时为 [] |
| `cairn.db`、三个服务日志 | 本次数据库、server/dispatcher/controller 日志 |
| `controller/prepare-<submission>/` | 原生入口每次提交的独立预审与种子审核原始输入/回答；旧批处理使用 `controller/prepare/`，另有 `call-*/` |
| `work/<project>/research/`、`work/<project>/controller-reviews/` | Explore/Reason 与旁路复核原始 Prompt、session、stdout/stderr |

复盘从 `result.json` 的真实终态开始，结合 `completions.json` 判断是谁提出申请、终审是否批准，再看 `audit.json` 的证据/条件及错误。用 `project.json` 区分 Kritt 导入种子和新增 Fact，沿图与时间线找到产生它的 Intent，回查原始调用与查询资料。

执行失败与证据驳回有不同记录。准备阶段失败先查看终端输出和本 run 已生成的错误文件；尚未创建运行目录的错误以终端输出为准，此时可能没有研究数据库或 `research.json`。研究启动后的错误或中断会保留已有数据库和材料；服务不可用或导出失败时文件可能不齐，`research.json` 记录实际错误，不把缺失文件伪装成正常结案。CLI 的退出码不能替代项目和申请状态判断。

源码仍是链接，不是运行目录中的完整副本；保留对应源码及 manifest。原生 harness 的细粒度工具轨迹依赖其会话日志，当前归档不承诺完整可重放环境。

## 重新查看已保存项目

```sh
uv run --project cairn cairn serve \
  --db-path /absolute/path/to/run/cairn.db --port 8000
```

打开 `http://127.0.0.1:8000/#/projects/proj_001`，ID 使用本次实际值。这只展示已有项目，无需重新 `start` 或启动 Dispatcher。已创建项目不能借 `start` 重复启动；恢复研究需遵循旧 audit 指南，不重置预算。

## 兼容入口

原 `cairn builder` 保留为已有批处理兼容入口：在目标和需求已明确时，一条命令完成内部 Builder 取材、审核、前台研究与结案。它有自己的模型调用和旧问答协议；需要持续原生问答的新任务使用 Skill。

```sh
uv run --project cairn cairn builder \
  --config dispatch.builder.yaml --target zentao --scan-id 19 \
  --goal '完整且明确的任务目标' --run-dir /absolute/path/to/new-run
```

旧命令在前台持有研究生命周期，可在其终端按 Ctrl+C 停止。原生 Skill 的 `start` 在后台持有生命周期，停止研究使用项目网页。

新配置通过 `role_pools` 选择后台研究与审核的 harness，不会改变原生 Skill 当前会话所使用的模型。旧 `workers` / `controller.roles` 配置仍可使用；纯资源池配置下，旧批处理 Builder 复用 Closing 池的执行者。

各 harness 的实际验证范围以验收记录为准，单元回归或命令帮助检查不等于真实模型的完整研究演示。
