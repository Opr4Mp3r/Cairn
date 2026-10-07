import hashlib
import json
import os
import sys
from pathlib import Path
import signal
import uuid

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
@click.option("--fd", type=int, hidden=True, help="Inherited listening socket for the local launcher")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool, fd: int | None):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
        fd=fd,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    def terminate(_signum, _frame):
        if getattr(loop, "_stopping", False):
            return
        loop.cancel_running_tasks("dispatcher received termination signal")
        raise KeyboardInterrupt("dispatcher received termination signal")

    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@main.command("audit-probe")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--file", "target_file", type=click.Path(exists=True, dir_okay=False, resolve_path=True, path_type=Path), required=True)
@click.option("--role", type=click.Choice(["builder", "reviewer"]), default="builder", show_default=True)
@click.option("--output", "output_dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--timeout", type=click.IntRange(min=1), help="Agent timeout in seconds")
def audit_probe(config_path: Path, target_file: Path, role: str, output_dir: Path | None, timeout: int | None):
    """通过控制器和现有 harness 读取文件；CP1 不创建审计项目。"""
    from cairn.controller.app import call_agent, file_probe_prompt
    from cairn.controller.models import FileReadResult
    from cairn.dispatcher.config import DispatchConfig, LocalConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend

    try:
        config = DispatchConfig.load(config_path)
        if config.controller is None:
            raise ValueError("controller configuration is required")
        if config.runtime.execution != "local":
            raise ValueError("audit-probe requires local execution to read the supplied host file")
        source_bytes = target_file.read_bytes()
        original = source_bytes.decode("utf-8")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        if output_dir is None:
            root = (Path(config.local.workspace_root) if config.local and config.local.workspace_root else Path("datas/controller-probes")).expanduser()
            output_dir = root if root.is_absolute() else config_path.resolve().parent / root
        backend = LocalBackend(LocalConfig(workspace_root=str(output_dir.expanduser().resolve())))
        workspace = Path(backend.ensure_running("probe-" + uuid.uuid4().hex))
        prompt = file_probe_prompt(target_file)
        workspace.joinpath("input.json").write_text(json.dumps({
            "file": str(target_file), "sha256": source_hash, "role": role, "prompt": prompt,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        def terminate(_signum, _frame):
            raise KeyboardInterrupt("controller received SIGTERM")

        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            result = call_agent(
                config, backend, role=role, prompt=prompt, result_model=FileReadResult,
                workspace=workspace, timeout_seconds=timeout or config.controller.timeouts.build,
                phase="controller_file_probe",
            )
        finally:
            signal.signal(signal.SIGTERM, previous)
            backend.close()
        if result.status == "success":
            try:
                reported_path = Path(result.data["path"])
                unchanged = target_file.read_bytes() == source_bytes
                matches = reported_path.is_absolute() and reported_path.resolve() == target_file and result.data["content"] == original and unchanged
            except (OSError, ValueError):
                matches = False
            if not matches:
                result.status = "invalid_output"
                result.error = "返回路径或内容与目标文件不匹配，或目标文件在执行期间变化"
                result.data = None
        receipt = workspace / "result.json"
        receipt.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        click.echo(json.dumps({"status": result.status, "session": result.session, "receipt": str(receipt), "data": result.data}, ensure_ascii=False))
        if result.status != "success":
            raise click.ClickException(result.error or result.status)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("kritt-probe")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--target", "target_name", required=True, help="Configured Kritt target key")
@click.option("--goal", required=True)
@click.option("--scan", "scan_id", type=click.IntRange(min=1))
@click.option("--view", "view_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Explicit offline fixture; otherwise read PostgreSQL")
@click.option("--output", type=click.Path(file_okay=False, path_type=Path), required=True)
def kritt_probe(config_path: Path, target_name: str, goal: str, scan_id: int | None,
                view_path: Path | None, output: Path):
    """只读取材及源码探针；不创建 Cairn 项目。"""
    from cairn.controller.kritt_probe import run_kritt_probe
    from cairn.controller.local import stop_agent_groups
    from cairn.dispatcher.config import DispatchConfig
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV

    cancellation = TaskCancellation()
    original_registry = os.environ.get(REGISTRY_ENV)
    registry = output.expanduser().resolve() / "agent-processes"
    os.environ[REGISTRY_ENV] = str(registry)

    cleaning = interrupted = False

    def terminate(_signum, _frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        if cleaning:
            return
        # Unwind lock-holding code first; normal exception handling performs cancellation.
        raise KeyboardInterrupt("kritt-probe interrupted")

    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        summary = run_kritt_probe(DispatchConfig.load(config_path), target_name=target_name,
                                  user_goal=goal, output=output, scan_id=scan_id, view_path=view_path,
                                  cancellation=cancellation)
    except (OSError, ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        cleaning = True
        try:
            cancellation.cancel("kritt-probe shutdown")
            stop_agent_groups(registry, {os.getpid()})
        except (OSError, ValueError, RuntimeError) as exc:
            raise click.ClickException(f"kritt-probe cleanup failed: {exc}; registry: {registry}") from exc
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if original_registry is None:
                os.environ.pop(REGISTRY_ENV, None)
            else:
                os.environ[REGISTRY_ENV] = original_registry
    if interrupted:
        raise click.Abort()
    click.echo(json.dumps({key: summary[key] for key in ("status", "scan_id", "commit_sha", "calls", "queries", "output")}, ensure_ascii=False))


@main.command("builder")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--target", "target_name", required=True)
@click.option("--goal", required=True)
@click.option("--scan-id", type=click.IntRange(min=1))
@click.option("--run-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option("--view", "view_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Explicit offline fixture; otherwise read PostgreSQL")
def builder(config_path, target_name, goal, scan_id, run_dir, view_path):
    """从自然语言需求查询 Kritt 并生成研究方案。本阶段不创建项目。"""
    from cairn.controller.builder import run_builder
    from cairn.controller.local import stop_agent_groups
    from cairn.dispatcher.config import DispatchConfig
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV

    folder = run_dir.expanduser().resolve()
    token = TaskCancellation()
    registry = folder / "agent-processes"
    original_registry = os.environ.get(REGISTRY_ENV)
    os.environ[REGISTRY_ENV] = str(registry)
    cleaning = interrupted = False

    def reply(questions: list[str]) -> str:
        if not sys.stdin.isatty():
            raise ValueError("需要补充信息后重新运行：" + "；".join(questions))
        for question in questions:
            click.echo(question)
        return click.prompt("请补充", type=str)

    def terminate(_signum, _frame):
        nonlocal interrupted
        if interrupted:
            return
        interrupted = True
        if not cleaning:
            raise KeyboardInterrupt("Builder interrupted")

    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        result = run_builder(DispatchConfig.load(config_path), target_name=target_name, user_goal=goal,
                             run_dir=folder, scan_id=scan_id, view_path=view_path, ask=reply, cancellation=token)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        cleaning = True
        try:
            token.cancel("Builder shutdown")
            stop_agent_groups(registry, {os.getpid()})
        except (OSError, ValueError, RuntimeError) as exc:
            raise click.ClickException(f"Builder cleanup failed: {exc}; registry: {registry}") from exc
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if original_registry is None:
                os.environ.pop(REGISTRY_ENV, None)
            else:
                os.environ[REGISTRY_ENV] = original_registry
    if interrupted:
        raise click.Abort()
    click.echo(json.dumps({key: result[key] for key in ("status", "scan_id", "commit_sha", "calls", "queries", "plan")}, ensure_ascii=False))


@main.command("audit")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--repo", type=click.Path(exists=True, file_okay=False, resolve_path=True, path_type=Path))
@click.option("--goal")
@click.option("--project", "project_id", help="恢复已有审计项目的复核与预算收尾；不重建项目或重置期限")
@click.option("--once", is_flag=True, help="新建时只激活；连接已有项目时只执行一轮复核")
@click.option("--retry-review", help="显式重试审核执行错误或失效申请，不重置项目期限")
@click.option("--reference", "references", type=click.Path(exists=True, dir_okay=False, resolve_path=True, path_type=Path), multiple=True)
@click.option("--seeds", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def audit(config_path: Path, repo: Path | None, goal: str | None, project_id: str | None,
          once: bool, retry_review: str | None, references: tuple[Path, ...], seeds: Path | None):
    """创建或连接本地审计项目，运行独立 Overseer 和完成验收。"""
    import requests

    from cairn.controller.audit import create_local_audit
    from cairn.controller.monitor import monitor_audit
    from cairn.dispatcher.config import DispatchConfig, LocalConfig
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.runtime.local_backend import LocalBackend

    client = backend = None
    previous = None
    cancellation = TaskCancellation()
    try:
        if project_id:
            if repo is not None or goal is not None or references or seeds is not None:
                raise ValueError("--project cannot be combined with new audit inputs")
        elif repo is None or goal is None or retry_review is not None:
            raise ValueError("new audit requires --repo and --goal; --retry-review requires --project")
        config = DispatchConfig.load(config_path)
        root = Path(config.local.workspace_root if config.local and config.local.workspace_root else "datas/audit-workspaces").expanduser()
        root = root.resolve() if root.is_absolute() else (config_path.resolve().parent / root).resolve()
        backend = LocalBackend(LocalConfig(workspace_root=str(root)))
        client = CairnClient(config.server)

        def terminate(_signum, _frame):
            cancellation.cancel("controller received SIGTERM")
            raise KeyboardInterrupt("controller received SIGTERM")

        previous = signal.signal(signal.SIGTERM, terminate)
        if project_id is None:
            result = create_local_audit(config, client, backend, repo=repo, user_goal=goal,
                                        workspace_root=root, references=references, input_seeds=seeds,
                                        cancellation=cancellation)
            click.echo(json.dumps(result, ensure_ascii=False))
            if once:
                return
            project_id = result["project_id"]
        monitored = monitor_audit(config, client, backend, project_id, once=once, retry_review=retry_review,
                                  cancellation=cancellation, report=lambda event: click.echo(json.dumps(event, ensure_ascii=False)))
        if monitored["status"] == "cancelled":
            raise click.Abort()
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        cancellation.cancel("controller shutdown")
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if backend is not None:
            backend.close()
        if client is not None:
            client.close()
