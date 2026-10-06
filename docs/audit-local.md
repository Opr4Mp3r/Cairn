# 本地代码审计运行指南

本指南对应审计控制器 MVP 的 CP6 交付。控制器是 Cairn 包内模块；本地入口运行独立的 Server、Dispatcher 和 Controller 三个进程，共用既有 harness。默认示例仅使用 claudecode。

## 运行准备

本地入口适用于 macOS 和 Linux。在 Cairn 仓库根执行。Python 至少 3.12，已验证的开发环境为 Python 3.13；先使用现有项目依赖建立环境：

```sh
uv sync --project cairn --group dev --python 3.13
```

本机 `claude` 必须已登录并能够从终端直接执行任务。Cairn 不配置或替换你的 CLI 登录信息。示例为 local 模式，不需要 Docker 或 open-kritt。

`dispatch.audit.example.yaml` 定义一个 Claude Worker，builder 和 reviewer 都引用它；每次控制器阶段使用独立 session。研究和控制器通过共享 Driver、进程执行函数调用 CLI，审核状态、policy 和预算保存在 Cairn 数据库。

## 从夹具启动完整任务

```sh
./scripts/run-audit-local.sh \
  --config dispatch.audit.example.yaml \
  --repo cairn/tests/fixtures/audit-target \
  --reference cairn/tests/fixtures/audit-target/SECURITY.md \
  --goal "核对默认匿名调用 /public 与 /admin 的实际源码行为，记录证据和条件；这是合成夹具，不夸大为高危或 RCE。" \
  --port 0
```

`--port 0` 自动选择本机端口；不传时使用配置中的端口。被占用的端口会报错，入口不会连接或停止该端口的已有服务。启动器将自己已绑定的 socket 交给 Uvicorn，确认 Server 就绪后启动其他两个进程。

终端先打印 UI 地址和本次目录。研究期间可打开该地址查看图，详情面板展示审计阶段、项目状态、完成申请、结案判断和终止原因。选中普通 Fact 可以查看 verified、unverified 或 refuted，以及源码引用、必要条件和逻辑前提。审计 Complete 提交的是申请；终审通过后项目才是 completed。

运行最终打印状态并回收本次创建的进程；如果启动或控制器执行失败则返回非零退出码。按 Ctrl+C 或收到 SIGTERM 时，也会取消在途 Agent 并有限等待进程退出。不会清理其他已有服务。

## 配置和期限

可以复制示例为自己的配置，继续通过 `workers` 与 `controller.roles` 选择 Agent。控制器没有独立的 harness 类型白名单；首版示例和真实验收覆盖 claudecode，其他 harness 仍需对应接入验证。

- `runtime.prompt_group` 用于 generic 项目，示例保持 default；`controller.research_prompt_group` 为审计研究使用 audit。
- `controller.timeouts` 限制构建、预审、种子复核、Overseer、closing 和终审的单次时间。示例不是调用耗时保证，模型和本机负载会影响实际结果。
- `controller.budget.research_seconds` 从项目激活时开始；耗尽后不再派新任务。
- `drain_seconds` 允许已有 lease 限时收尾，之后禁止迟到研究事实写回。
- `final_review_seconds` 给最后证据复核、只读 closing 和独立终审提供固定窗口。重试与恢复不重置期限。

正常结果可以是 completed，也可以是 budget_exhausted。预算耗尽不意味着证明无漏洞；构建失败、权限条件未知、审核执行错误都不能伪装成确认结果。

## 保存的结果

默认在配置所在目录的 `datas/audit-runs/<唯一编号>/` 保存本次材料，也可用 `--run-dir /absolute/path/to/new-run` 指定尚不存在的目录。启动器为本次运行生成配置副本，并把工作目录限定到该目录，不修改原配置。

运行目录必须在待审仓库之外，避免日志和数据库变动污染固定源码基线。审计 Cairn 自身或包含默认运行目录的仓库时，显式指定仓库外的 `--run-dir`；不满足时在启动前报错。

| 文件 | 内容 |
| --- | --- |
| summary.json | 运行结果、项目 ID、服务地址、本次进程 PID 与退出码；失败时含错误 |
| server.log、dispatcher.log、controller.log | 三个进程的原始输出 |
| cairn.db | 本次专用数据库 |
| config.yaml | 可用于检查或恢复的实际配置 |
| project.json、audit.json、graph.yaml | 正常结案时的项目、审计记录和图快照 |
| work/controller、work/proj_编号 | 控制器与研究的输入、原始回答、session 和证据材料 |

失败或中断也保留数据库、已有日志和工作材料；可能还没有正常结案快照。保留这些文件用于核对实际阶段，不用一次 CLI 成功退出代替状态判断。

## 查看保存的项目和恢复

启动器结案后 Server 已停止。要查看保存的图，单独运行 Server，明确传入本次数据库路径：

```sh
uv run --project cairn cairn serve \
  --db-path /absolute/path/to/run/cairn.db --port 8000
```

打开 `http://127.0.0.1:8000`。审计 completed 和 budget_exhausted 不提供普通 Resume/Reopen；需要新基线研究时新建项目。

如果需要恢复未结案项目，先让 Server 地址与本次 config.yaml 中的 `server` 一致，再使用现有命令：

```sh
uv run --project cairn cairn dispatch --config /absolute/path/to/run/config.yaml
uv run --project cairn cairn audit --project proj_001 \
  --config /absolute/path/to/run/config.yaml
```

两条命令各在一个终端运行。恢复使用 Server 已保存的基线、lease、closing 记录和绝对期限，不重新构建项目。单独恢复控制器也可在研究停止后处理预算收尾。执行错误需要显式重试时可加 `--retry-review 申请编号`，仍受已有期限约束。

## 验证范围

`cairn/tests/test_audit_delivery.py` 用真实三个进程、HTTP 和 SQLite 加固定判断的假 Claude，检查成功、预算耗尽、启动失败、端口占用、中断及进程组回收。它验证编排，不能称作真实模型或真实漏洞验证。

真实 Claude 的完整运行证据另存于 CP6 交接目录，明确记录源码夹具、配置、各阶段 session、实际终态和未确认项。代码到 CP6 即停，等待用户安排独立检查；开发自检不代表完整 MVP 已验收。
