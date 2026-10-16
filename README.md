<div align="center">

<img src="./README/banner.png" alt="Cairn Banner"/>

# Cairn
### More Than Just AI Penetration Testing — Towards General State-Space Search

<p>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tencent.png" alt="Tencent" height="55" />
  </a>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tch.png" alt="TCH" height="55" />
  </a>
  <a href="https://wiki.chainreactors.red" target="_blank" rel="noopener noreferrer">
    <img src="./README/c.png" alt="ChainReactors" height="45" />
  </a>
</p>

Cairn is a general-purpose problem-solving engine. <br/>It defines no roles, no workflows. Given an origin and a goal, it searches for a path through an unknown state space. <br/>AI Penetration Testing is one such problem — and a proven one.

<p>
  <a href="https://discord.gg/nDSy4NZVP" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/Discord-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord" />
  </a>
  <a href="https://x.com/le1xia0" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/X-000000?style=flat-square&logo=x&logoColor=white" alt="X" />
  </a>
</p>

</div>

<p align="center">
  <a href="https://www.bilibili.com/video/BV1a8R5BhEVi/" target="_blank" rel="noopener noreferrer">
    <img src="./README/cairn.png" alt="Cairn runtime screenshot" width="900" />
  </a>
</p>

## What is Cairn?

Penetration testing is fundamentally a **directed search through a near-infinite state space**:

- **Origin**: known (target IP, target system)
- **Goal**: defined (get a shell, capture the flag)
- **Path**: unknown

This structure is not unique to penetration testing. Vulnerability research, mathematical proof, CTF challenges — any problem with a clear starting point, a clear success condition, and an unknown path in between shares the same shape.

Cairn is built for this class of problems. Penetration testing is the first domain it has been validated on.

The engine is built on a **Blackboard Architecture** with an explicit fact-intent graph. Three primitives are all it needs:

| Concept | Meaning |
|---------|---------|
| **Fact** | A confirmed, objective finding written to the board |
| **Intent** | A declared direction of exploration, not yet executed |
| **Hint** | Human judgment injected at any time; absorbed by agents on the next read |

The graph grows from `origin` toward `goal`. Every new Fact is a stepping stone; every Intent is a step into the unknown.

Agent Workers run an OODA loop — Observe the full graph, Orient to the current state, Decide on next intents, Act to explore — and write their findings back as new Facts. Workers have no fixed roles. Tasks are generated at runtime from the graph's current state, not from predefined job descriptions.

Agents coordinate exclusively through the shared board (Stigmergy). No direct communication. No information silos.

## Cairn in Action

https://github.com/user-attachments/assets/e557b1ac-dda4-41cb-87dd-9d56dbf05133


## How It Works

Three task types, all executed by the same Worker:

| Task | What it does | Output |
|------|-------------|--------|
| **Bootstrap** | At project start, attempts to solve the problem directly | Fact + possible Complete |
| **Reason** | Reads the full graph: is the goal met? What should be explored next? | Complete / new Intents / no-op |
| **Explore** | Claims one Intent, executes the exploration, reports findings | One Fact |

System architecture:

```
          ┌──────────────────────────────────┐
          │           Cairn Server           │
          │    Facts + Intents + Hints       │
          └─────────────────┬────────────────┘
                            │
                     Read / Write API
                            │
          ┌─────────────────┴────────────────┐
          │             Dispatcher           │
          │   Schedules tasks, manages       │
          │   containers, writes protocol    │
          └──────────┬───────────────┬───────┘
                     │               │
     ┌───────────────┴──┐     ┌──────┴──────────────┐
     │  Worker Container│     │  Worker Container   │
     │   (Project A)    │     │   (Project B)       │
     │  ┌────┐  ┌────┐  │     │  ┌────┐  ┌────┐     │
     │  │ W. │  │ W. │  │     │  │ W. │  │ W. │     │
     │  └────┘  └────┘  │     │  └────┘  └────┘     │
     └──────────────────┘     └─────────────────────┘
```

**Cairn Server** maintains graph consistency only.

**Cairn Dispatcher** reads the graph, schedules tasks, spins up and tears down worker containers, and is the sole writer to the protocol. Each project gets its own Worker Container; multiple Agent Workers run concurrently inside it. Agent Workers only receive a prompt and return structured output.

Workers can also run directly on the dispatcher host instead of in per-project containers — **local mode**, no Docker required. See [Local mode](#local-mode-no-docker) below.

Supported worker backends: **Claude Code**, **Codex**, and **Pi**.

## Results

**Tencent Cloud Hackathon · AI Penetration Testing Challenge · 2nd Edition**

610 teams · 1,345 participants · top universities and security firms across China

| Metric | Value |
|--------|-------|
| Problems solved | **54 / 54 — only team to AK** |
| Final ranking | 3rd |

> The system had never been tested before the competition. The full pipeline came online for the first time at 4 AM on race day. No training, no tuning, no domain-specific tooling. Zero MCP tools, zero RAG, zero predefined agent roles.

## Further Reading

- <a href="https://mp.weixin.qq.com/s/DlpEH7bVr0xi0VawPJs3XA" target="_blank" rel="noopener noreferrer">The Strongest AI Penetration Testing Agent: Postmortem of the Only Team to Achieve AK at the TCH Tencent Cloud Hackathon Intelligent Penetration Testing Challenge (2nd Edition)</a>
- <a href="https://mp.weixin.qq.com/s/2rEqFLvkxvYWM3gW170C2w" target="_blank" rel="noopener noreferrer">The Pathless Path: Cairn AI from Penetration Testing to General Problem Solving</a>

## Getting Started

**Prerequisites**
 
- macOS or Linux
- Python ≥ 3.12
- Docker (container execution only — not needed for local mode)


### Pull required images
 
Both setup methods require the worker container image:
 
```bash
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
```

Create your local dispatcher configuration and fill in your LLM endpoints and API keys:

```bash
cp dispatch.example.yaml dispatch.yaml
```
 
### Docker Compose (recommended)
 
Pull the base image used to build Cairn:
 
```bash
docker pull ghcr.io/astral-sh/uv:python3.13-trixie
```
 
```bash
docker compose up --build
```
 
This starts `cairn-server` on port `8000` and `cairn-dispatcher` once the server passes its health check. The dispatcher mounts `dispatch.yaml` from the project root and connects to Docker via the host socket. Data is persisted to `./datas/cairn/`.
 
### Manual
 
```bash
# Start the server
uv run --project cairn cairn serve
 
# Run the dispatcher
uv run --project cairn cairn dispatch --config dispatch.yaml
 
# Run startup health checks only
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only
```

### Local mode (no Docker)

Instead of one container per project, workers can run directly on the dispatcher host, reusing the machine's already-configured `claude` / `codex` / `pi` / `grok` / `mcode` CLIs — no Docker, and no API keys in the config. `grok` is Grok Build's headless CLI. `minimax` selects MiniMax Code's `mcode` CLI and currently supports local execution only.

```bash
cp dispatch.local.example.yaml dispatch.yaml

# Start the server
uv run --project cairn cairn serve

# Run the dispatcher on the same host, where the CLIs are installed and logged in
uv run --project cairn cairn dispatch --config dispatch.yaml
```

Local mode is selected by `runtime.execution: local` (see `dispatch.local.example.yaml`). On startup the dispatcher checks each configured worker CLI is installed and runnable, and reminds you they must already be logged in. Each project gets an isolated working directory under `local.workspace_root` (default: the dispatcher's current directory). Run the dispatcher directly on the host — not inside Docker — since the agents run with your user's permissions and no sandbox.

### Kritt 与本地审计入口

以项目内的 [cairn-builder Skill](.claude/skills/cairn-builder/SKILL.md) 作为 Claude Code 的主交互入口。在本仓库打开 Claude Code，调用 `/cairn-builder` 并说明目标：当前会话补充需求、配置资源池、只读查询 Kritt、阅读源码并保存 AuditPlan。

```text
/cairn-builder 使用 zentao 目标，静态梳理 module/install/control.php 构造函数的进入和终止条件，列出表达式与行号；部署和匿名可达性未知。
```

先按 [Builder 使用指南](docs/builder-prepare.md) 配置源码、manifest 和只读连接。Kritt/Cairn 服务由用户手动启动，Builder 只使用已有服务。[在线服务入口](docs/managed-service.md)提供人工 `cairn up` 与纯 HTTP `cairn submit/status/pause/resume`，任务结束后服务继续在线。REC-CP3 已验收，提供网页运行概览、在线暂停/恢复、资源池占用与离线快照。REC-CP4 已验收，Skill 已接入纯客户端 submit/status/pause/resume；旧 `start` 仍由用户手动执行。任务恢复及运行面板进度见[开发方案](docs/specs/task-recovery-mvp.md)。

后台角色使用 [角色资源池](docs/builder-prepare.md#角色资源池)：例如 `explore: {claudecode: 3, grok: 2}` 允许同一项目最多五个 Explore 并行。各角色容量直接由 harness 数量相加，模型与 provider 沿用本机 CLI 配置；原生 Builder 留在当前会话。

| 入口或文档 | 当前用途 |
|---|---|
| [原生 cairn-builder Skill](.claude/skills/cairn-builder/SKILL.md) | 资源配置、取材、在线提交与原任务查询/恢复；服务启停由用户手动执行 |
| [Builder 使用指南](docs/builder-prepare.md) | 准备配置、prepare/submit/status/pause/resume、旧兼容入口和结果复盘 |
| [在线服务指南](docs/managed-service.md) | 人工 up、纯客户端任务控制、运行面板和旧余额迁移 |
| [Kritt 取材探针](docs/kritt-probe.md) | 只读查询与源码观察，不创建研究项目 |
| [本地 audit 模式](docs/audit-local.md) | 已有独立审计路径，包含复核、终审及预算 |
| [任务恢复与运行面板方案](docs/specs/task-recovery-mvp.md) | REC-CP1/CP2 已验收；REC-CP3 运行面板已验收；REC-CP4 Skill 路由已验收 |

原 `uv run --project cairn cairn builder ...` 保留为已有自动化的批处理兼容入口。其内部 Builder 会话及前台研究生命周期与原生 Skill 入口不同；新交互任务使用 Skill。

Kritt 接回实现背景见 [v0.5 接回方案](docs/specs/kritt-audit-reconnect-mvp.md)，历史 generic 流程见 [正向 MVP 开发约定](docs/specs/kritt-builder-mvp.md)。

### Local code audit MVP（已完成的 audit 路径）

以下命令运行已有源码型审计模式，包含独立批准与 `budget_exhausted`。Kritt 的 R-CP2 入口复用这里的准备、研究和监督语义；R-CP3 已验收通过，包含独立结案、预算收尾与五份结果归档。

The audit controller shares Cairn's worker drivers and runs alongside the Server and Dispatcher. The example uses only an already-configured local Claude Code CLI:

```bash
./scripts/run-audit-local.sh --config dispatch.audit.example.yaml \
  --repo cairn/tests/fixtures/audit-target \
  --reference cairn/tests/fixtures/audit-target/SECURITY.md \
  --goal "核对默认匿名调用 /public 与 /admin 的实际源码行为，记录证据和条件；这是合成夹具，不夸大为高危或 RCE。" \
  --port 0
```

The launcher prints the local UI URL and run directory, uses a fresh local database, and stops its own processes when the audit finishes or is interrupted. `completed` requires independent approval; `budget_exhausted` preserves partial results without declaring the goal achieved. See [the local audit guide](docs/audit-local.md) for configuration, saved evidence, and recovery.

### Tests

Run the fast regression suite without Docker or live model endpoints:

```bash
uv run --project cairn --group dev pytest
```

## Disclaimer

Cairn is a general-purpose problem-solving engine. Although it supports penetration testing, CTF solving, security assessment, and vulnerability research workflows, it is intended to be used only in environments where you have explicit authorization to operate.

You are solely responsible for how you use this project. Do not use Cairn against systems, networks, applications, or data without clear prior permission from the owner or operator. Unauthorized security testing, exploitation, or data access may be illegal and may cause harm.

The developers and contributors of this project do not endorse or accept responsibility for any misuse, abuse, damage, loss, or legal consequences arising from its use. By using this project, you agree to ensure that your activities comply with all applicable laws, regulations, contractual obligations, and professional or organizational policies in your jurisdiction.

## Star History

<a href="https://www.star-history.com/#oritera/Cairn&Date" target="_blank" rel="noopener noreferrer">
  <img src="https://api.star-history.com/svg?repos=oritera/Cairn&type=Date" alt="Star History Chart" />
</a>

## ⚖️ License
This project is licensed under **GNU AGPLv3** for personal and educational use.

**Commercial Use**: If you wish to use this project in a commercial or proprietary environment without the AGPL-3.0 open-source obligations, **please contact me to obtain a commercial license.**

**Contributions**: By submitting a Pull Request, you agree that your contributions may be used under both the AGPL-3.0 and the project's commercial license.
