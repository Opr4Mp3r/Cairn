import hashlib
import json
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
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
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
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


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
