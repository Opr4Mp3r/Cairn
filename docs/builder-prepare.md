# Builder：Kritt 取材、audit 研究与独立结案

本指南对应 [v0.5 实现方案](specs/kritt-audit-reconnect-mvp.md)。R-CP1、R-CP2 已验收并提交；R-CP3 已补齐结案归档并通过独立代码验收。历史 generic 入口及结果说明见 [v0.3 记录](specs/kritt-builder-mvp.md)。

一条 `cairn builder` 命令会完成：需求 → 同会话查询/阅读 → AuditPlan → 独立目标预审 → 种子复核 → audit Explore/Reason 与旁路监督 → 独立批准或预算结束 → 保存产物。研究图沿用已有网页，不需要先运行 Server 或 Dispatcher。

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

使用数字 IP、Unix socket，或主机名加数字 `hostaddr`；当前实现不自行解析 DNS。账号提供必要表的 SELECT。Agent 的 Kritt 查询只访问本次固定视图，不取得 DSN，也不写 Kritt 数据库。

`controller.research_prompt_group` 必须为已配置的审计提示组，示例使用 `audit`；`runtime.prompt_group: default` 继续用于普通项目。参考文档、源码和查询材料供 Agent 按需读取，完整文件指纹由程序核验。

## 启动任务

```sh
uv run --project cairn cairn builder \
  --config dispatch.builder.yaml --target zentao --scan-id 19 \
  --goal '静态梳理 module/install/control.php 中构造函数的进入和终止条件，给出表达式与行号；部署和匿名可达性未知，保留为未知。' \
  --run-dir /absolute/path/to/new-run
```

`--run-dir` 使用源码目录之外、尚不存在的新目录；重跑换一个目录。`--scan-id` 可省略，使用目标配置的选择。缺少会改变可接受范围的关键信息时，Builder 在当前终端提问，收到答复后续接原 session。

离线材料可显式加 `--view /path/to/view.json`。该文件是 Cairn 的 Kritt 适配视图，普通 findings JSON 或 ZIP 不能直接替代。离线运行不会读在线 PG；保存的视图只表示本次输入，不等于重新验证了数据库最新结果。

目标应写清效果、部署、攻击者能力和范围。适合源码理解、调用链/鉴权梳理、复核既有候选及进一步漏洞研究。当前没有统一的应用部署、动态 PoC 或正式漏洞报告生成器。静态任务可以使用空 CVSS 约束；真实漏洞任务仍需披露所有必要条件。

## 查看进度与停止

终端显示本次 `server`、`project_url`、`project_id` 和 `run_dir`。打开 `project_url` 查看现有任务图；服务由本次命令创建，结束后会关闭。

变化时显示：

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

可在图页面停止项目，或在启动终端按 Ctrl+C。draining 不是终态：系统停止新派发，允许已开始任务有限收尾，再由 Closing 检查最终图中的候选并走独立终审。原 CLI 与研究进程只回收本次拥有的服务和 Agent。

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

单次调用时限包含其内部读文件和工具循环；不因每次查询重置。audit 预算从项目激活时开始，三段绝对期限保存在 Server 中。复核、Closing、终审还受剩余总期限约束，配置的单次时限不是每个阶段都能获得的保留时长。当前没有 Token 总预算或准备阶段累计预算。

## 保存结果与复盘

终端最后的 `results` 列出五个结果文件路径。每次运行目录保存：

| 文件/目录 | 内容 |
|---|---|
| `run.json`、`requirements.json` | 原始需求/答复、repo/scan/commit、Builder session 和准备产物位置 |
| `plan.json` | 最终 AuditPlan、policy、种子及初始 Intents |
| `kritt-view.json`、`queries/`、`query-receipts.json` | 固定输入视图、每次查询与分页返回 |
| `selected-materials.json` | 完整 get 返回的材料；不代表模型全部读过或结论已验证 |
| `source-baseline.json`、`references.json`、`references/` | 核验基线及一次保存的参考资料 |
| `project.json` | 项目 ID、seed key 到 Fact ID 映射、工作目录 |
| `research.json` | 服务进程、运行/项目状态、阶段、终止原因、结果位置及错误 |
| `result.json` | 结束时的项目、Facts、Intents 和 Hints |
| `graph.yaml`、`timeline.txt` | 最终图及图变化时间线 |
| `audit.json` | policy、基线、预算期限、审核记录和 Closing 状态 |
| `completions.json` | 完成申请、证明来源、审核 session、裁决与执行错误；没有申请时为 [] |
| `cairn.db`、三个服务日志 | 本次数据库、server/dispatcher/controller 日志 |
| `call-*/`、`controller/prepare/` | Builder、修订和准备审核的原始输入/回答 |
| `work/<project>/research/`、`controller-reviews/` | Explore/Reason 与旁路复核原始 Prompt、session、stdout/stderr |

复盘从 `result.json` 的真实终态开始，结合 `completions.json` 判断是谁提出申请、终审是否批准，再看 `audit.json` 的证据/条件及错误。用 `project.json` 区分 Kritt 导入种子和新增 Fact，沿图与时间线找到产生它的 Intent，回查原始调用与查询资料。

执行失败与证据驳回有不同记录。错误或中断也保留数据库与已经取得的材料；服务不可用或导出失败时文件可能不齐，`research.json` 记录实际错误，不把缺失文件伪装成正常结案。CLI 的退出码不能替代项目和申请状态判断。

源码仍是链接，不是运行目录中的完整副本；保留对应源码及 manifest。原生 harness 的细粒度工具轨迹依赖其会话日志，当前归档不承诺完整可重放环境。

## 重新查看已保存项目

```sh
uv run --project cairn cairn serve \
  --db-path /absolute/path/to/run/cairn.db --port 8000
```

打开 `http://127.0.0.1:8000/#/projects/proj_001`，ID 使用本次实际值。这只展示已有项目，无需重跑 Builder 或启动 Dispatcher。新 Builder 不提供对话恢复/自动重启入口；完整恢复语义沿用旧 audit 指南，不重置预算。

## 使用 Grok Build

本机 `grok` 应已登录并可调用。把示例 Worker 改为 `name: grok-main`、`type: grok`，同时把 `controller.roles.builder` 和 `reviewer` 指向 `grok-main`。其他 audit、Kritt 和预算配置保持对应值。本地模式复用 Grok 原生登录和默认模型配置。

Builder 答复与修订续接同 session；独立审核默认新 session，角色没有额外 harness 白名单。当前已验证的完整真实交付使用哪个 harness，以 R-CP3 交接记录为准；未真实调用另一个供应商的回归不冒充完整真实演示。
