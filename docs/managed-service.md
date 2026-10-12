# 人工启动 Cairn 与在线提交（REC-CP1）

本检查点提供 `up` 和纯 HTTP `submit`。服务持有 Server、Dispatcher 与 Controller；没有任务、预审要求修订或任务结束时，服务继续运行。Builder 只使用在线服务，Kritt 服务也由用户手动启动。

暂停时钟、`resume`、恢复按钮及新版 Skill 路由属于后续检查点，当前尚未实现。现有 Skill 仍负责准备方案；本页的提交命令可以由用户执行，也可供调用已在线服务的客户端使用。旧 `start` / `builder` 仍是拥有自己服务生命周期的兼容入口，不用于连接这里的服务。

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

| 状态 | 下一步 |
|---|---|
| `pending` / `preparing` | 服务排队或执行独立 preflight、种子复核 |
| `needs_revision` | 阅读 issues，在原会话修改原 run 的方案，再执行相同 submit 命令；沿用提交 ID、递增 revision |
| `running` | 打开 project_url；Dispatcher 与旁路 Controller 按本次快照工作 |
| `completed` / `budget_exhausted` / `stopped` | 查看图及结果；系统服务继续在线 |
| `error` / `interrupted` | 查看 error 和服务日志；执行失败不代表证据被反驳 |

preflight 与 seed review 复用已有独立会话，审核通过后才创建和激活图；研究继续复用 Overseer、Closing 与独立终审。管理服务内的审计创建/激活必须经过服务内部提交关联，客户端不能直接绕过这条准备链路。

## 3. 查看进度和复盘

打开 `project_url` 查看现有图页面，或查询提交及项目接口：

```sh
curl -s http://127.0.0.1:8000/audit-submissions/SUBMISSION_ID
curl -s http://127.0.0.1:8000/projects/PROJECT_ID
```

网页停止操作只停止项目，不关闭服务。当前 `stopped` 还不是具备冻结预算和恢复语义的暂停，后续 REC-CP2 将提供该能力。不要用直接更改 status 来冒充恢复。

| 保存位置 | 内容 |
|---|---|
| 用户 run：`submission.json` | 最近一次客户端读取的提交状态，实时状态以 API 为准 |
| 用户 run：`project.json`、`controller/prepare-*/` | 项目定位、种子映射、独立预审与复核调用材料 |
| 用户 run：`research.json`、结果文件 | 终态、身份、导出路径；运行中的实时进度查询 API |
| 服务目录：`submissions/<id>/revision-*/` | 接纳时的方案、材料、资料、查询回执及执行快照；submitted-plan 保留原始方案，工作 plan 保存审核后版本 |
| 服务工作目录：`work/<project>/` | 源码链接、固定材料、研究和复核调用记录 |
| 服务数据库 | 图、审核记录、完成申请、提交关联及完整任务配置 |

结果文件沿用 `result.json`、`graph.yaml`、`timeline.txt`、`audit.json`、`completions.json`。源码仍是链接，需要保留匹配版本。终态导出失败会记录为执行错误，数据库中已有成果保留。

当前预算仍按已有绝对期限计时，停机暂停预算尚未交付。人工重启同一数据库后，未结束的旧提交标为 `interrupted`，不自动继续；显式恢复、旧进程隔离和预算迁移将在 REC-CP2 实现。
