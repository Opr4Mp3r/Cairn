---
name: cairn-builder
description: 在 Claude Code 原生会话中完成 Cairn 审计开题：主动配置角色资源池、补充需求、只读查询 Kritt、阅读源码并生成 AuditPlan。Builder 只使用已启动的系统，Kritt/Cairn 服务必须由用户手动启动。用于配置资源池、根据扫描开题、修订方案和查看任务；纯客户端提交功能未交付前只准备并交付方案。
---

# Cairn 原生开题

在当前会话完成资源池配置、问答、取材和方案修订。Kritt/Cairn 服务的启动、关闭、重启由用户在终端手动执行；Builder 不执行 Docker 启停、Cairn 服务启动器或后台守护命令，连接失败也不自动拉起另一套服务。
服务在线后允许 Builder 提交并开始研究任务，这与启动系统服务不同；调用前确认当前版本确实提供纯客户端入口。当前 `cairn start` 会启动后台服务，禁止 Builder 执行它，按第 5 步交付方案。
直接使用已有 Read、Bash、Write/Edit 工具，不用 ToolSearch 寻找这些内置工具，不创建 MCP 或嵌套 Builder Agent。

## 1. 主动询问并配置资源池

先读取用户指定的配置文件，未指定时使用仓库的 `dispatch.builder.yaml`；不存在时从 `dispatch.builder.example.yaml` 创建。已有文件只调整 `role_pools`，保留目标源码、Kritt、预算、超时和其他设置。
展示当前各角色的 harness 与数量；用户尚未明确本次选择时，即使已有配置，也要主动问一次：“本次沿用这些资源池，还是调整？Explore 要用哪些 harness、各几个？其余角色可沿用当前设置。”用户已经给出配置、说“沿用”或“其他保持默认”时，直接按答复执行，不重复确认。
没有资源池时，提出七个角色各 `claudecode: 1` 的初始方案供用户选择；用户只调整 Explore 时，明确其余角色拟沿用的配置或默认值，补问尚未决定的部分。未答复的选择保持待定，不自行增加并发或启动研究。
按用户的自然语言答复直接写入完整 `role_pools`，不要只给 YAML 让用户自己编辑。使用 `explore`、`reason`、`preflight`、`seed_review`、`overseer`、`closing`、`final_review` 七个角色；每个角色填写已支持的 harness 名和正整数数量，例如 `explore: {claudecode: 3, grok: 2}`。移除某 harness 时删除该项，不写数量 0；每个角色至少保留一种。
角色容量直接等于数量之和，3 个 Claude Code 加 2 个 Grok 就是 5；不增加 `max_concurrency` 或另一套总量字段。内部 Worker 与容量自动生成，无需另写 `workers`、`controller.roles`、`runtime.max_workers` 或 `runtime.max_project_workers`。
模型/provider、登录与代理沿用用户的本机 CLI 和环境；不改 CCSwitch 或全局 harness 配置，也不从旧 `common_env` 注入模型。原生 Builder 保持当前会话，不占后台名额。
保存后用项目的 Python 调用 `DispatchConfig.load` 检查配置能否加载；仅展示资源池及各角色合计，不输出整份配置或凭据。说明 Reason、Preflight、Closing、Final Review 每项目仍串行，名额不代表强制重复执行同一决策。
`prepare` 使用刚保存的配置路径，交付时说明该路径与本次资源池。修改本地 YAML 不代表在线服务已经热加载；纯提交功能交付后，须把本次配置快照随任务提交。待运行目录创建后，将资源池问答和最终选择追加到 `requirements.json.messages`；同次方案修订不重复询问，除非用户要求调整。
用户仅要求配置资源池时，交付配置路径和最终分配后结束，不创建或启动研究。已有运行不热更新；配置调整用于下一次启动。

## 2. 准备固定输入

从本 Skill 的实际路径定位 `scripts/cairn.py`，后续通过 Bash 调用：

```sh
python3 /absolute/path/to/cairn/.claude/skills/cairn-builder/scripts/cairn.py prepare \
  --config dispatch.builder.yaml --target zentao \
  --goal '用户原始目标' --run-dir /absolute/path/to/new-run
```

脚本固定使用仓库的 `cairn/.venv` 和仓库根目录，支持从任何目录调用；先按项目指南完成 `uv sync --project cairn`。
确认目标配置、用户目标和源码之外的新 run 目录。保留用户已给出的部署、攻击者能力、效果与范围。
除资源池选择外，仅在缺少会改变范围或阻止开工的信息时提问；未知条件如实记录，不自行降级或替换目标。
可指定 `--scan-id N`；离线时指定 `--view /absolute/path/to/view.json`，必须是匹配该目标的 Cairn Kritt 视图。
不要猜 scan、commit、数据库或 DSN；目标不存在、扫描未完成、源码不匹配、连接失败时报告具体阻塞。
`prepare` 只保存输入并核验源码，不调用模型或启动研究。读取本 run 的 `run.json` 以及返回的 `plan_schema`、`requirements` 路径。

## 3. 原生对话与取材

由当前会话继续问答。将每轮相关问题与答复追加到 `requirements.json` 的 `messages`，使用 `role`、`content`；保留 `user_goal` 原文和已有消息。
使用 `prepare` 返回的完整 `kritt_query_command`，经 Bash 向其 stdin 传入 JSON；不要另找数据库工具或凭据：

```json
{"action":"describe"}
{"action":"search","kind":"flow","path":"module/install/"}
{"action":"get","ref":原始返回的ref对象}
```

先了解视图，再按需要查询 `entrypoint`、`flow` 或 `finding`；保留原始 ref，按返回 cursor 翻页。`get` 自动收齐该分支分页。
使用 Read 阅读查询返回的材料、回执、`references` 中列出的资料和 `source_root` 下的必要源码。
取材只服务于开题；不修改目标源码、不部署目标、不执行利用实验，不在开题会话中接管研究。
Kritt 的 rank、canonical、valid、scope 和自报确认只是分析标签，不能直接当作已确认事实。

## 4. 保存 AuditPlan

读取返回的 `plan_schema`，将符合该 schema 的原始 AuditPlan JSON 写入本 run 的 `plan.json`。
不要使用 `ask`/`finish` action 封装；问答发生在当前会话中。
`origin` 仅写任务身份、版本、用户要求；代码行为、可达性、部署状态、危害判断均作为待复核 seeds。
所有种子的 `audit.review_status` 设为 `unverified`，保留条件和未知；无源码引用时 evidence 可为空。
`audit.evidence` 只写仓库内源码路径和行号；Kritt 材料与回执路径写入 description 或 review_notes。
seed key 唯一，不用 origin/goal；`premise_ids`、Intent 的 `from` 只引用本次 seed key 或 origin，不引用 goal、不形成循环。
policy 保留 effect、deployment、attacker_capabilities、scope、cvss、evidence_requirements、threat_model。
threat_model 每项注明 claim、kind（official/inferred/unknown）、source；官方仓库的代码不自动等于官方威胁模型。
不将未复核判断写成 policy 的已知前提，不加入“证明不可能”出口，不放宽用户条件；静态任务的 cvss 可为空对象。

## 5. 交付方案，使用已在线的服务

当前版本尚未交付“连接已有 Cairn 并提交 prepared plan”的纯客户端接口；`cairn start`、旧 `cairn builder` 会管理服务进程，不能由 Builder 调用。不要臆造 submit/resume 命令或用底层建图 API 绕过独立 preflight/seed review。
完成后交付 plan.json、run 目录、配置路径和资源池摘要；如需立即运行，给出指南中的旧兼容命令，由用户在终端手动执行。若用户已经运行另一套 Cairn，说明当前缺少接入它的提交链路，不建议再启动第二套。
用户手动运行后，可读取本 run 的状态、修订意见、日志或查询已在线的项目。已有项目禁止重复创建；needs_revision 时在原会话修改方案，当前仍由用户手动重提兼容入口。
后续纯客户端交付并实际可用后，Builder 可提交并开始任务、请求暂停/恢复、查询进度；服务离线只提示人工启动并等待，不能把系统服务启动当成任务恢复。
运行与审核保持原有独立会话和结案契约；执行错误不是证据反证。报告真实项目状态、可用网页或结果文件，不伪造已提交/已开始，也不把旧网页地址当作仍在线。

当前用法见 `docs/builder-prepare.md`；人工启动系统与纯客户端使用的目标方案见 `docs/specs/task-recovery-mvp.md`。仅要求配置或准备方案时，交付后结束。
