# 人工启动 Cairn、在线提交与恢复（REC-CP2）

当前提供人工 `up` 和纯 HTTP `submit`、`pause`、`resume`。服务持有 Server、Dispatcher 与 Controller；没有任务、预审要求修订或任务结束时，服务继续运行。Builder 只使用在线服务，Kritt 服务也由用户手动启动。

REC-CP2 新增暂停时钟、原项目恢复和世代隔离，等待检查点验收。新版运行面板属于 REC-CP3，Skill 提交/恢复路由属于 REC-CP4，尚未开发。现有 Skill 仍负责准备方案；本页的提交命令可以由用户执行，也可供调用已在线服务的客户端使用。旧 `start` / `builder` 仍是拥有自己服务生命周期的兼容入口，不用于连接这里的服务。

## 1. 用户启动一次服务

在仓库根目录准备配置，已有文件时不要覆盖：

```sh
uv sync --project cairn --python 3.13
cp dispatch.service.example.yaml dispatch.service.yaml
uv run --project cairn cairn up \
  --config dispatch.service.yaml \
  --db-path /absolute/path/to/cairn-service/cairn.db
```

配置必须使用 `runtime.execution: local`、完整角色资源池、审计提示组，以及显式固定端口，例如 `http://127.0.0.1:8000`。CLI 登录、模型和 provider 沿用启动终端的本机配置。运行目录与服务数据放在目标源码之外；数据库应保留，同一数据库只允许一个 `up` 实例。

`up` 在前台运行，输出服务地址、`database_id`、本次 `service_id` 和 PID。日志写到该终端；需要保存时可由用户重定向。数据库旁的 `cairn-service/` 保存有效服务配置、提交快照和进程登记，默认工作目录位于其中的 `work/`。系统服务可通过终端 Ctrl+C 关闭，这会取消当前任务及其模型进程。

检查就绪状态：

```sh
curl -s http://127.0.0.1:8000/runtime/health
```

`managed=true` 且 `ready=true` 才能接纳任务。仅运行 `cairn serve` 是数据查看实例，不能消费提交。端口冲突或执行组件失效时，应由用户处理并重启；客户端不会启动备用服务或换端口。

服务先成功绑定并持有监听 socket，再更新旧运行状态。Dispatcher 或 Controller 故障时先撤销就绪、标记未结束的提交为 error，并取消在途调用，随后等待清理；等待提交的客户端会读取该错误或就绪失败，明确退出。

## 2. 准备和提交一个任务

源码、manifest、Kritt 只读连接、`prepare`、取材和 AuditPlan 格式沿用 [Builder 指南](builder-prepare.md)。`prepare` 在角色池模式下额外保存 `execution.json`，包含本次角色池、任务超时、审计提示组、审核及预算配置。

保存好本 run 的 `plan.json` 后，连接上一步服务：

```sh
uv run --project cairn cairn submit \
  --server http://127.0.0.1:8000 \
  --run-dir /absolute/path/to/prepared-run
```

需要明确使用另一份本次任务配置时，加 `--config /path/to/task.yaml`。该参数只提取任务配置快照，不启停服务、不改变服务地址或数据库。省略时使用 prepare 保存的 `execution.json`。`--no-wait` 在接纳后立即返回；默认等待准备结束或返回修订意见，不等待整个研究结束。

提交返回稳定的 `submission_id`、revision、状态、服务和数据库身份。有项目时返回 `project_id` 与 `project_url`。同一 run、相同快照重试返回同一个提交；不能通过重复提交创建第二个项目。

run 已有 `submission.json` 时，客户端先核对原数据库身份与提交关联，换库后重提会在 POST 前拒绝并保留原定位。每次接纳/修订请求须携带就绪接口返回的 `database_id` 和 `service_id`，服务端在保存输入前再次校验，拒绝检查与提交之间的实例切换。同一数据库人工重启后可以查询原提交，但查询与重提不等于恢复研究。即使 `submit --no-wait` 留下的本地回执尚无 project_id，pause/resume 也会在核对数据库身份后通过原 submission_id 查询权威项目，再执行控制。

| 状态 | 下一步 |
|---|---|
| `pending` / `preparing` | 服务排队或执行独立 preflight、种子复核 |
| `needs_revision` | 阅读 issues，在原会话修改原 run 的方案，再执行相同 submit 命令；沿用提交 ID、递增 revision |
| `running` | 打开 project_url；Dispatcher 与旁路 Controller 按本次快照工作 |
| `completed` / `budget_exhausted` | 查看图及结果；不能重新分配预算或恢复研究 |
| `pausing` / `paused` | 当前任务正在回收调用或已经冻结预算；系统服务继续在线 |
| `resuming` | 原项目正在恢复，等待管理循环接纳 |
| `error` / `interrupted` | 查看 error 和服务日志；执行失败不代表证据被反驳 |
| `deleted` | 原项目已删除，保留提交身份和快照；同 run 不重新建图，新任务使用新的 run |

preflight 与 seed review 复用已有独立会话，审核通过后才创建和激活图；研究继续复用 Overseer、Closing 与独立终审。管理服务内的审计创建/激活必须经过服务内部提交关联，客户端不能直接绕过这条准备链路。

## 3. 查看进度和复盘

打开 `project_url` 查看现有图页面，或查询提交及项目接口：

```sh
curl -s http://127.0.0.1:8000/audit-submissions/SUBMISSION_ID
curl -s http://127.0.0.1:8000/projects/PROJECT_ID
```

受管理 audit 的旧网页 Stop/Resume 也转到后端暂停/恢复流程，任务控制不关闭服务。当前页面尚无新的预算概览及离线提示，准确状态以 runtime API 为准。generic 与原独立运行模式保留既有行为。

| 保存位置 | 内容 |
|---|---|
| 用户 run：`submission.json` | 最近一次客户端读取的提交状态，实时状态以 API 为准 |
| 用户 run：`project.json`、`controller/prepare-*/` | 项目定位、种子映射、独立预审与复核调用材料 |
| 用户 run：`research.json`、结果文件 | 终态、身份、导出路径；运行中的实时进度查询 API |
| 服务目录：`submissions/<id>/revision-*/` | 接纳时的方案、材料、资料、查询回执及执行快照；submitted-plan 保留原始方案，工作 plan 保存审核后版本 |
| 服务工作目录：`work/<project>/` | 源码链接、固定材料；`research/generation-NNN/` 与 `controller-reviews/generation-NNN/` 保留各次执行记录 |
| 服务数据库 | 图、审核记录、完成申请、提交关联及完整任务配置 |

结果文件沿用 `result.json`、`graph.yaml`、`timeline.txt`、`audit.json`、`completions.json`。源码仍是链接，需要保留匹配版本。终态导出失败会记录为执行错误，数据库中已有成果保留。

## 4. 暂停与恢复原任务

服务在线时，使用原项目或原 run 二选一：

```sh
uv run --project cairn cairn pause \
  --server http://127.0.0.1:8000 --run-dir /absolute/path/to/prepared-run

uv run --project cairn cairn resume \
  --server http://127.0.0.1:8000 --run-dir /absolute/path/to/prepared-run

curl -s http://127.0.0.1:8000/projects/PROJECT_ID/runtime
```

也可将 `--run-dir` 改为 `--project PROJECT_ID`。命令只调用已在线服务：离线时报告人工启动说明，既不启动系统服务，也不读取新的 Kritt 扫描或新建替代项目。请求快速返回 `pausing` / `resuming`；查询 runtime，直到 `paused` / `running`。若原 job 已显示 paused 但该项目的进程清理尚未结束，resume 返回 409，清理结束后再请求；不会先接纳恢复再被旧清理覆盖。

预算只累计有效运行时间，暂停和停机不消耗余额。研究、在途收尾、结案和独立审核共用该时钟；单次 CLI timeout 与终止宽限仍按真实经过时间计算。正常暂停在模型进程退出后冻结预算。强杀服务时从最近持久化心跳恢复，可能少计最多约 2 秒；不会把停机时间算入。当前实例中无人续租时最多累计至 10 秒许可边界，然后停止写入。

每个登记 CLI 配有独立进程看守，读取 `agent-processes/owners/<服务PID>.json` 的许可；服务每2秒原子更新，窗口仍为10秒。服务强杀或停止心跳后，看守发 TERM，并在原有终止宽限后对仍存活的已登记进程组发 KILL；看守自己也退出，不等待下次人工重启。旧独立入口没有服务许可文件时沿用父进程身份检测。执行、进程组身份和模型资源池计数保持原边界，不新增用户参数。

服务崩溃后，由用户用相同 `up --db-path ...` 手动启动。程序核验并清理登记的遗留 Agent 进程，将未结束任务保留为 `interrupted`。随后显式执行 resume，沿用原图、Goal/policy、源码基线、材料和资源池。已经完成的 Intent 不再执行；未完成 Intent 使用新会话继续。恢复后补一次 Reason，draining/finalizing 保持原阶段，不重新开放 Explore。

`runtime` 返回 `generation`、运行状态、阶段、累计运行秒数、各窗口剩余秒数、最近心跳、停止原因与历史。旧世代的领取、写回和审核回执被拒绝；历史审核结论与成果保留。暂停、中断或恢复不会清除实质性的证据驳回。

研究 Worker 和 Controller 的协议客户端自动携带 `X-Cairn-Generation`。自行调用图写接口时必须使用本次执行世代；`runtime/pause` / `runtime/resume` 请求须携带健康接口返回的 `database_id` 与 `service_id`。这些控制接口不等待长模型调用。

## 5. 旧任务的首次余额迁移

没有运行时钟的旧任务不能依据过期的绝对期限猜余额。首次 resume 返回 `needs_budget` 后，由用户提供当前阶段剩余秒数：

```sh
uv run --project cairn cairn resume \
  --server http://127.0.0.1:8000 --project PROJECT_ID \
  --remaining-seconds 2400
```

余额仅允许一次：researching 上限为原研究预算；draining 上限为原收尾预算；finalizing 上限为原收尾加结案预算。后续恢复不再传该参数，不能借迁移增加原窗口或重开终态。

旧原生 `start` 的 run 可用其原 `cairn.db` 手动启动服务，再从原 run 恢复。需保留原 `run.json`、`project.json`、`config.yaml`、固定源码与准备材料；程序核对原项目和基线后登记已有图，不重新运行 Builder。旧启动器或子服务仍在线且无法核验归属时，要求用户先手动停止原运行，不凭历史 PID 强杀服务。材料或配置缺失时会明确返回阻塞，不能用新任务替代原图。

源码版本改变、工作目录与项目不一致时返回阻塞并冻结余额。恢复匹配版本/材料后再次请求 resume。前端运行信息和 Builder 对话中的恢复操作将在后续检查点接入。
