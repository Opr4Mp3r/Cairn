---
name: cairn-builder
description: 在 Claude Code 原生会话中管理 Cairn 的本地源码审查任务：配置角色资源池、只读查询 Kritt、阅读源码、生成和修订 AuditPlan，向已在线服务提交任务，查看进度、暂停和恢复原任务、追溯结果。适用于已授权本地源码的静态审查与风险证据核对。Kritt/Cairn 系统服务始终由用户手动启停。
---

# Cairn 原生审查与任务控制

按任务阅读：[路径选择](#1-先选择任务路径) · [资源池](#2-主动配置新任务的资源池) · [固定输入与取材](#3-准备固定输入与取材) · [方案与提交](#4-保存方案并提交) · [原任务控制](#5-查看暂停和恢复原任务)。

直接使用当前会话的 Read、Bash、Write/Edit；不创建 MCP、嵌套 Builder 或另一套调度器。服务运行后可以提交并开始任务；服务的启动、关闭、重启由用户在终端执行。连接失败只说明人工启动方式，不执行 Docker 启停、`cairn up/serve/dispatch/start/builder/audit`、后台守护或换端口启动服务。

本 Skill 负责已授权本地源码的静态审查、风险证据核对和修复研究；不构造或执行利用链。保留用户条件与未知，研究判断由现有独立审核流程处理。

## 1. 先选择任务路径

- 新任务或方案修订：按第 2～4 节准备、取材，再提交。用户已有 prepared run/plan 时读取原材料与配置，跳过已完成步骤，不重复 prepare 或新建 run。
- 已有任务的进度、暂停、恢复或结果：直接按第 5 节定位原任务，不重新 prepare、查询新 Kritt 扫描、生成 plan 或询问新资源池。
- 只配置资源池或只准备方案：完成所需部分后交付，不自动提交。

从本 Skill 实际路径定位 `scripts/cairn.py`，用以下统一命令前缀（下称 `CLIENT`）：

```sh
python3 /absolute/path/to/cairn/.claude/skills/cairn-builder/scripts/cairn.py
```

脚本仅提供 `prepare/submit/status/pause/resume`，固定使用仓库的 `cairn/.venv` 和仓库目录，可从任意目录执行。缺少环境时按指南完成 `uv sync --project cairn`。

仅配置资源池或准备方案时不要求 Cairn 在线。提交、控制或在线查询前，优先使用用户给出的服务地址；否则读取指定配置的 `server` 或原 run 的 `research.json.server`。缺少地址或定位时只追问缺失项，不猜数据库、scan、余额或另一个服务。运行 `CLIENT status --server URL`。只有 `health.managed=true` 且 `health.ready=true` 才能提交或控制任务；查看实例和组件故障只能读取，返回 `manual_start` 或已有配置/数据库的人工启动说明。

## 2. 主动配置新任务的资源池

读取用户指定配置；未指定时读取仓库 `dispatch.builder.yaml`，不存在则从 `dispatch.builder.example.yaml` 创建。已有配置只改 `role_pools`，保留目标、Kritt、预算、超时等字段。

展示各角色的 harness 和数量。用户未明确本次选择时主动问一次是否沿用或调整；已经说“沿用”“其余默认”或给出完整选择时不重复确认。无资源池时提出七个角色各 `claudecode: 1`；未决定的名额保持待定，不自行增加并发或提交任务。

按答复直接保存 `explore/reason/preflight/seed_review/overseer/closing/final_review` 七个角色，每项使用已支持的 harness 与正整数数量。移除项就删除，不写 0；每个角色至少一种。角色容量等于各数量之和，不另设总上限，不手写内部 workers。Reason、Preflight、Closing、Final Review 每项目仍串行。

用项目 Python 的 `DispatchConfig.load` 检查能否加载，只展示资源池与合计，不输出整份配置或凭据。模型、provider、登录、代理沿用本机 CLI/CCSwitch，不修改全局设置。当前 Builder 会话不占后台池名额。

prepare 使用刚保存的配置；把资源池问答追加到新 run 的 `requirements.json.messages`。修改 YAML 不代表在线服务热加载；submit 的 `--config` 明确把这份执行快照随任务提交。已有运行沿用原池，本轮不热修改恢复任务的配置。

## 3. 准备固定输入与取材

```sh
CLIENT prepare --config /path/to/task.yaml --target TARGET \
  --goal '用户原始审查目标' --run-dir /absolute/path/to/new-run
```

实际执行时把 `CLIENT` 替换为第 1 节完整前缀。可指定 `--scan-id N`；仅当用户选择离线视图时使用 `--view /path/to/view.json`。目标、scan/commit、源码、manifest、只读连接必须匹配；错误时报告具体阻塞，不自行切版本或刷新材料。run 放在目标源码之外。prepare 不调用模型或启动服务。

读取返回的 `run.json`、`plan_schema`、`requirements` 和资料路径。将需求问答追加到 `requirements.json.messages`，使用 role/content，保留 user_goal 原文。仅追问影响范围或阻止开工的缺失信息。

通过 Bash 使用 prepare 返回的完整 `kritt_query_command`，向 stdin 输入 JSON，不另找数据库凭据或写 Kritt：

```json
{"action":"describe"}
{"action":"search","kind":"flow","path":"src/"}
{"action":"get","ref":原始返回的ref对象}
```

按需要查询 entrypoint/flow/finding，保留原始 ref 和分页 cursor；get 自动收齐分支。用 Read 阅读材料、回执、参考资料及必要源码，不拼接完整源码清单。Kritt 的 rank/canonical/valid/scope 是分析标签，不能当作已确认前提。开题不改目标源码、不部署目标或接管后台研究。

按用户目标的具体缺口选材，不只看高排名 finding；正常功能、低排名及无 finding 流程也可能提供关键条件。查询未命中不等于不存在，必要时返回固定源码独立发现。提供紧凑起点与待验证假设即可，不把全库导成 seeds。

研究角色会获得已提交固定视图的 kritt_query 命令，并可持续查询；并发查询各自保存材料和回执，不回写开题选材或刷新数据库。原生 Builder 会话无需留驻或替后台继续研究。

## 4. 保存方案并提交

读取本 run 的 plan_schema，将原始 AuditPlan JSON 写入 plan.json，不使用 ask/finish 封装。

- origin 只写身份、版本和用户要求；代码行为、可达性、部署和影响作为待复核 seeds。
- 所有种子的 audit.review_status 为 unverified；description 首段给出主要结论和关键条件，保留对象归属、作用范围、状态／时序及未知。无源码引用时 evidence 可为空；evidence 只写仓库内路径与行号，Kritt 回执写 description/review_notes。
- seed key 唯一，不用 origin/goal；premise_ids 和 Intent.from 只引用本次 seed 或 origin，不引用 goal、不循环。Intent.from 是选材来源，premise_ids 仅列实际逻辑依赖，不自动互相复制。
- policy 保留 effect/deployment/attacker_capabilities/scope/cvss/evidence_requirements/threat_model。威胁模型 claim 注明 official/inferred/unknown 与 source；源码不自动等于官方模型。静态任务 cvss 可为空。
- 不放宽用户条件、不把待复核判断当成 policy 前提、不加入“证明不可能”出口。
- policy 固定初始能力；后续能力须由前序证据解释。组合 Intent 说明前一步产生什么、后一步缺什么及待验证的连接；条件下机制成立、可接入当前路径、满足 Goal 分别判断，不预判衔接成功。

用户要求提交时运行：

```sh
CLIENT submit --server URL --run-dir /path/to/run --config /path/to/task.yaml
```

默认只等待准备结果，不等待研究终态；不需要留在当前调用时可用 `--no-wait`。执行后按真实返回处理：needs_revision 读取 issues，在当前会话修订原 plan/requirements，再对同一 run submit；pending/preparing 返回提交 ID 与进度路径；running 返回原 project_url、run、配置和池摘要。error/interrupted 不作为证据反证。不要调用底层建图 API 绕过预审和种子复核，也不要把已有项目重新提交成新项目。

## 5. 查看、暂停和恢复原任务

优先用原 run；用户只给 project ID 时使用 `--project ID`。两者二选一：

```sh
CLIENT status --server URL --run-dir /path/to/run
CLIENT pause --server URL --run-dir /path/to/run
CLIENT resume --server URL --run-dir /path/to/run
```

status 只读，不修改本地回执；即使本地 submission.json 尚无 project_id，也通过稳定 submission_id 查询原项目。以在线 submission/runtime 为准，不把旧文件或网页地址当实时状态。

展示 runtime 的 state/audit_phase、当前阶段和总余额、原角色池、available_actions/blocked_reason，再执行用户要求的控制。已运行的恢复、已暂停的暂停直接说明当前状态；pausing/resuming 只说明正在收敛/恢复，不密集重复提交。短时再次查询确认后，仍在过渡就返回当前状态及网页。

只有旧运行缺失余额时追问一次当前阶段的剩余秒数；preparing 尚未消耗预算，先直接恢复。researching/draining/finalizing 展示对应原预算上限，不从已过期的绝对期限猜余额。用户提供后运行：

```sh
CLIENT resume --server URL --run-dir /path/to/run --remaining-seconds N
```

新时钟任务及已迁移任务不再传此参数。completed/budget_exhausted 只查看和导出，不请求恢复或补预算。源码、材料缺失或身份不符时展示具体阻塞，不刷新 Kritt、不新建替代图、不自行回收或重启服务。

任务终态后读取原 run 的 research.json 中 results 指向的五个产物；若还在导出，说明尚未交接完成，稍后读取。用 result.json、completions.json、audit.json 对照真实终态与独立终审，再沿 graph.yaml/timeline.txt、generation 调用记录及 Kritt 查询回执追溯。没有 approved 完成申请不能宣称目标已达成；预算耗尽也不等于证明不可能。

操作细节见仓库 docs/managed-service.md；配置、材料与方案格式见 docs/builder-prepare.md。仅配置或准备方案的请求到交付即结束。
