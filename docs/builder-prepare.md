# Builder：原生 Skill 开题与后台审计研究

交互开题使用项目内的 [cairn-builder Skill](../.claude/skills/cairn-builder/SKILL.md)。Claude Code 当前会话负责问答、Kritt 只读取材、源码阅读和 AuditPlan 修订；独立 Controller 负责 preflight、seed review 与研究监督。

**服务职责：Kritt 和 Cairn 服务由用户在终端手动启动，Builder 只使用服务，可以提交并开始任务。** REC-CP4 已验收，Skill 已连接 `submit/status/pause/resume`。服务的人工启动与运行面板见[在线服务指南](managed-service.md)。

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

Skill 只调整所选文件的 `role_pools`，保留已有目标、Kritt、预算和超时，展示最终分配与合计，并说明 prepare 及在线提交所用的配置路径。新运行会把有关问答记入 `requirements.json`。修改本地 YAML 不会使已在线服务自动生效；纯客户端随任务提交资源池快照。恢复已有任务跳过资源池问答，沿用它保存的配置和余额；本轮不热更新。

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

项目的 `.claude/skills/cairn-builder/` 是原生安装目录；`.claude/artifacts/cairn-builder.skill` 是分发包，不需要额外 MCP、插件框架或嵌套 Agent。薄脚本 `scripts/cairn.py` 从自己的位置定位仓库，使用 `cairn/.venv`，可从任意工作目录执行；只开放 prepare、submit、status、pause、resume，系统服务命令由用户直接在终端执行。

Skill 在原会话中处理必要追问，并将问题和答复追加到本 run 的 `requirements.json`。目标应写清效果、部署、攻击者能力和范围；已给出的条件会被保留，未知项仍标为未知。开题取材建立初始方向，研究期间仍可查询同一固定视图，适合源码理解、调用链/鉴权梳理、复核既有候选及进一步研究。当前没有统一的应用部署、动态 PoC 或正式漏洞报告生成器。

## 准备与在线提交

先由用户启动 Cairn 执行服务，并通过 `/runtime/health` 确认 `managed=true, ready=true`。只配置资源池或准备方案时无需 Cairn 在线。

在原生会话中准备固定输入，或在仓库根目录使用：

```sh
uv run --project cairn cairn prepare \
  --config dispatch.builder.yaml --target zentao --scan-id 19 \
  --goal '静态梳理构造函数的进入和终止条件，引用源码；部署和匿名可达性保留为未知。' \
  --run-dir /absolute/path/to/new-run
```

run 必须在目标源码之外且尚不存在。`--scan-id` 可省略；仅在用户选择离线材料时加 `--view /path/to/view.json`。普通 findings JSON 或 ZIP 不是 Cairn 适配视图。prepare 不调用模型或创建研究项目；目标、版本、源码或连接不匹配时报告阻塞，不自动更换。

读取 `run.json` 中的 `plan_schema`、需求/资料及 `kritt_query_command`。经 Bash 使用该完整查询命令，stdin 输入 describe/search/get JSON，按需用 Read 阅读返回的材料及源码。保留原始 ref 和回执；所有种子初始为 unverified，代码判断不能藏入 origin 或 policy 的已知前提。

将原始 AuditPlan JSON 保存为 `plan.json` 后提交：

```sh
uv run --project cairn cairn submit \
  --server http://127.0.0.1:8000 \
  --run-dir /absolute/path/to/run --config dispatch.builder.yaml
```

原生 Skill 使用随包的薄脚本执行同样命令。本次 `--config` 提交资源池及执行配置快照，不修改服务配置或热更新其他项目。省略时使用 prepare 保存的 execution.json。

默认只等独立准备完成，不等待整个任务。`--no-wait` 在接纳后返回提交 ID。preflight 与 seed review 使用独立会话，开题会话不自审。

| 返回状态 | 当前会话处理 |
|---|---|
| pending / preparing | 交付 submission_id 与 run，用 status 查询准备进度 |
| needs_revision | 读取 issues，在同一会话修改原 plan/requirements，再对同一 run submit；保持提交 ID |
| running | 交付原 project_url、run、配置与池摘要，后台任务继续 |
| error / interrupted | 报告错误与阶段；执行失败不等于证据反证；有原图时走恢复路径 |
| completed / budget_exhausted | 读取原结果与独立终审；系统服务继续在线 |

已有 project 的 run 不重新建图；同 run 重提相同快照保持提交身份。源码、材料和配置的固定快照用于恢复，不在恢复时刷新 Kritt。

## 研究期按需查询 Kritt

审计项目首次激活或恢复时，程序向研究工作区交付固定 Kritt 查询入口。Reason／Explore 的审计上下文中，`kritt_query.command` 是当前环境可执行的完整命令，`examples` 提供 stdin JSON 示例；可用 describe、search、get 按需查询开题时未选中的入口、流程和 finding。get 使用 search 返回的完整 ref，材料及回执按返回路径读取。

查询读取已提交的同一份 view，不连接最新 Kritt 数据库。工作区的 `kritt-context.json` 指向该固定视图，查询结果分别保存在 `research/kritt-queries/q-*/`，不会覆盖开题的 selected-materials 或查询汇总。新增材料仍是研究线索，不自动成为已验证 Fact。

已有 input.json 的工作区在恢复时同样补齐入口；缺少固定查询材料时显示不可用原因，仍可继续读取源码。该能力只作用于使用新版代码激活或恢复的任务，不要求为现有研究重建项目。

运行期 Overseer、结案与终审的 `query_receipts` 输入区分开题账本 `opening_ledger` 和已发生研究查询的 `research_directory`。开题账本不包含后来查询的全部回执；研究引用按该目录的 `q-*/receipts.json`、request/result 和 get 材料定位，固定视图身份见 `view_context`。复核只读已有记录，不发起新查询；不能仅因开题账本没有某个回执就判定它不存在。

## 组合研究与规划记忆

Reason 先读取 `graph-index.md` 导航，再按当前缺口打开完整 `graph.yaml`、Kritt 材料和源码。导航保留条件、验证状态和逻辑前提；截断预览仅供定位，旧 Fact 不被改写。Explore 的结论先写带关键条件的主要观察，再保留对象、作用范围、生效时序和未知项。

桥接研究需要解释“前一步执行后的状态如何满足后一步”，分别验证的两个机制不自动组成有效路径。`verified` 支持的是声明的条件命题，是否符合当前部署及 Goal 还需判断。正常功能、低排名材料和无 finding 流程都可用于寻找互补条件；未确认观察也可用于安排验证，普通探索不等待逐条复核，完成申请仍经过独立终审。

审计 Reason 可返回 `planning_notes` 保存已考虑的连接、暂搁原因、重开条件和下一步重点。程序把有效成功的备注保存在工作区 `research/generation-*/reason-memory-*.json`，下一轮提供最近规划及最近失败原因的简要记录和回执路径。它不进入 Fact、不改变审核状态，也不触发新一轮 Reason；无需增加配置。旧输出不含备注时继续兼容。暂停或恢复后以当前图、Hint 和 policy 为准，不把旧备注当作证据。

后台 claudecode 调用使用 CLI 原生 JSON 结果信封和每次调用的最终格式指引，adapter 只解包 `result`，上层继续严格校验业务 JSON。原始信封仍留在调用回执中；CLI 执行失败不会被当成有效回答。源码版本与文件指纹由程序核验，归档源码没有 Git 元数据时，不以外层 Cairn 仓库 HEAD 替代目标版本。

## 隔离的组合研究样例

开发样例位于 `cairn/tests/test_compositional_research.py`，使用自有合成源码与离线 Kritt 格式材料，无需运行 Kritt。它比较同一调用顺序下的两种读取行为：创建 Display，发布新值，再用原 Display 读取。实时读取共享状态可以衔接；读取初始化快照时，发布动作不能更新原对象的快照。两例均保持相同 Goal，不允许通过重建对象或改源码凑结果。

当前样例的开题 get20 展开元数据已经包含读取端流程；后续 get21 验证固定查询入口与材料定位，不作为“发现此前未提供的信息”的证明。规划备注是否进入后续 prompt、该调用是否成功返回分别核对，不能由注入推断模型成功运用。

样例显式启用后会调用本机真实 Claude Code，沿用 CLI／CCSwitch 的模型与登录。运行于独立临时源码、端口、数据库和各角色仅一个 Claude Code 的资源池；自动启动和清理的是测试服务。该测试启动器仅用于开发验证，不改变日常服务由用户手动启动的方式。普通测试默认跳过模型调用。

样例研究预算为 420 秒、收尾 60 秒、终审 480 秒；目标达成可提前结束。预审和种子复核另占调用时间，实际耗时随本机模型而变。Reason 单次 1800 秒上限仍受这些项目预算约束。

在仓库根目录执行，并为 `--basetemp` 选择全新目录（pytest 会重建该目录）：

```sh
CAIRN_COMPOSITION_REAL_CLAUDE=1 uv run --project cairn pytest -s \
  cairn/tests/test_compositional_research.py \
  --basetemp=.claude/composition-example-new-run
```

每例的 `service.log`、独立 `cairn.db`、`run/` 和 `work/` 留在该测试目录中。查看 `run/result.json`、`audit.json`、`completions.json`、研究查询回执和规划记录即可复盘；测试按执行回执中的路径把导航及全文快照归档到 `graph-snapshots/`，成功时额外生成 `observation.json`。正例要求独立批准后 completed。反例还必须有 Explore 实际产出、经独立复核批准的新增事实，其证据覆盖写入、构造快照和读取端关键行，并被成功 no_candidate 结案明确引用；测试在内存中独立执行这份自有样例，核对共享状态更新而既有对象仍返回 before。反例最后为 budget_exhausted 且无完成批准；仅查询成功、调用全部失败或没有支持证据时不能通过。这些是该合成样例的验证条件，不作为通用产品 Fact 数量门槛。

这是正常功能的静态状态衔接示例，验证材料交付和研究流程，不能据此宣称发现真实漏洞或保证高难度目标的研究效果。

## 原生进度、暂停与恢复

在 Claude Code 中可以直接请求：

```text
/cairn-builder 查看 http://127.0.0.1:8000 上 /absolute/path/to/run 的进度。
/cairn-builder 暂停这个 run。
/cairn-builder 恢复这个 run，沿用原资源池和余额。
```

这条路径直接定位原任务，不重新询问新任务的目标、scan 或资源池。系统服务离线时只返回人工启动说明，用户手动恢复服务后再请求恢复任务。

对应 CLI：

```sh
uv run --project cairn cairn status --server http://127.0.0.1:8000
uv run --project cairn cairn status --server http://127.0.0.1:8000 --run-dir /absolute/path/to/run
uv run --project cairn cairn pause --server http://127.0.0.1:8000 --run-dir /absolute/path/to/run
uv run --project cairn cairn resume --server http://127.0.0.1:8000 --run-dir /absolute/path/to/run
```

run 可替换为 `--project PROJECT_ID`。status 不带任务定位时只查服务；带定位时返回 health、在线 submission、runtime 和 project_url。该命令只读，不改本地回执；本地 submission.json 尚无 project_id 时用稳定 submission_id 查询原图。

runtime 展示实际 state、audit_phase、Server 剩余预算、角色池和占用、可用操作及中断原因。pausing/resuming 是过渡状态，短时查询后仍未结束时返回当前状态和网页，不密集重复提交。单项目暂停/终态不关闭系统服务。

旧运行缺少累计时钟时只询问当前阶段的剩余秒数，首次恢复加 `--remaining-seconds N`；新运行和已迁移运行不再传该值。Preparing 尚未消耗运行预算。原材料或身份不匹配时保持阻塞，不猜余额或新建替代任务。上限及旧原生 run 迁移见[在线指南](managed-service.md#5-旧任务的首次余额迁移)。

| 状态 | 含义 |
|---|---|
| completed | 独立终审批准满足固定 policy；静态任务完成不等于发现漏洞 |
| budget_exhausted | 预算结束并保存成果，不表示证明不存在风险 |
| paused / interrupted / blocked | 原任务暂停、中断或恢复受阻，图与余额保留 |

Reason 的 Complete 只是申请，Overseer 复核事实和方向，独立终审决定完成。draining 停止新 Explore，再用最终图走有界结案，不重新分配研究预算。

## 时间配置

| 配置 | 示例秒数 |
|---|---:|
| controller.timeouts.build | 300 |
| controller.timeouts.preflight、seed_review | 240 |
| controller.timeouts.overseer、closing | 180 |
| controller.timeouts.final_review | 240 |
| tasks.reason.timeout、tasks.explore.timeout | 1800、300 |
| controller.budget.research_seconds | 1800 |
| controller.budget.drain_seconds | 120 |
| controller.budget.final_review_seconds | 600 |

build 时限只用于旧批处理 Builder，不限制原生会话问答。业务预算使用 Server 累计有效运行时间，暂停和停机冻结余额；审核轮次/单次窗口也使用同一业务时钟。CLI 调用超时与终止宽限仍按真实经过时间计时。当前没有 Token 总预算或准备阶段累计预算。

审计示例的 Reason 单次上限为 1800 秒，使用现有 `tasks.reason.timeout`；其他角色、心跳租约和项目预算不随之改变。已有本地配置需要显式设置该值。新任务 prepare 会保存执行快照，submit 默认使用该快照；若 prepare 后才改 YAML，需在 submit 时用 `--config /path/to/task.yaml` 明确提交新配置。修改示例或重启服务不会热更新已有任务的执行快照，恢复原任务继续沿用其时限和余额。

## 保存结果与复盘

运行中的在线状态以 status/API 为准，run 文件是本地交接记录。终态后读取 research.json 的 results，确认导出已完成，再检查五个结果产物。

| 文件或位置 | 内容 |
|---|---|
| run.json、requirements.json | 固定输入、版本、路径、原会话需求/答复 |
| audit-plan.schema.json、plan.json | 本次 schema 和方案 |
| kritt-view.json、queries/、query-receipts.json、selected-materials.json | 固定视图、查询材料及回执 |
| source-baseline.json、references.json、references/ | 源码基线和参考资料 |
| execution.json、submission.json | 准备时的执行配置与客户端最近读取的提交记录 |
| project.json、controller/prepare-*/ | 原 project、种子映射、独立预审/种子复核 |
| research.json | 当前交接状态、世代、服务/数据库身份、终态 results |
| result.json | 最终项目、Facts、Intents、Hints |
| graph.yaml、timeline.txt | 图和变化时间线 |
| audit.json、completions.json | policy、审核、结案与完成申请、证明、裁决和执行错误 |
| 服务目录 submissions/、work/ | 接纳快照及按 generation 保存的研究/复核原始调用 |
| 用户手动 up 的数据库与终端日志 | 持久化图、运行历史、系统执行记录 |

从 result.json 的终态与 completions.json 的独立批准开始，沿图和时间线找到 Fact 的生产 Intent，核对 audit.json 的证据/条件，再看对应世代的调用和 Kritt 查询回执。没有 approved 完成申请不能声称目标达成。日志面板显示保存回执，不是 CLI 内部步骤直播；历史产物与数据库应一并保留。

## 人工旧兼容入口

旧 start / builder 仍管理自己的服务生命周期，仅供用户在终端手动执行，原生 Skill 不调用它们。已有在线执行服务时优先使用 submit，避免重复启动。

```sh
uv run --project cairn cairn start --config dispatch.builder.yaml --run-dir /absolute/path/to/run
```

旧 run 的 launch.json/launch.log、cairn.db 和三个服务日志属于该旧启动器，不是新在线提交必须生成的文件。迁移时由用户用原数据库手动 up，保留原 run/config/源码和材料，再请求在线恢复；已经创建项目的 run 不再次 start。
