"""本地三进程入口，复用 Cairn 命令，不接管已有服务。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse
import uuid

import click
import requests
import yaml

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV, process_identities

CLI = [sys.executable, "-c", "from cairn.cli import main; main()"]
SHUTDOWN_SECONDS = 20
AGENT_SHUTDOWN_SECONDS = 5
FORCE_REAP_SECONDS = 2


def _owned_agent_groups(registry: Path, owners: set[int]) -> set[int]:
    records = {}
    for path in registry.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        pid = record.get("pid")
        if isinstance(pid, int) and pid > 0 and record.get("parent_pid") in owners:
            records[pid] = record
    identities = process_identities(list(records))
    groups = set()
    for pid, record in records.items():
        # PID 重用时不发送信号；leader 已退出时，存活的旧 PGID 仍属于本次组。
        if pid in identities and identities[pid] != record["identity"]:
            continue
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            continue
        groups.add(pid)
    return groups


def stop_agent_groups(registry: Path, owners: set[int]) -> None:
    signalled = set()
    deadline = time.monotonic() + AGENT_SHUTDOWN_SECONDS
    while True:
        groups = _owned_agent_groups(registry, owners)
        for pid in groups - signalled:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        signalled.update(groups)
        if not groups:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    for pid in _owned_agent_groups(registry, owners):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + FORCE_REAP_SECONDS
    while _owned_agent_groups(registry, owners):
        if time.monotonic() >= deadline:
            raise RuntimeError("owned Agent groups did not exit within the cleanup deadline")
        time.sleep(0.05)


def stop_processes(processes: dict[str, subprocess.Popen], registry: Path | None = None) -> None:
    errors = []
    # Server 最后停止，让研究与控制器仍可释放 lease 和保存结果。
    for names in (("controller", "dispatcher"), ("server",)):
        targets = [processes[name] for name in names if name in processes]
        for process in targets:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + SHUTDOWN_SECONDS
        for process in targets:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=FORCE_REAP_SECONDS)
                except subprocess.TimeoutExpired:
                    errors.append(f"owned parent {process.pid} did not exit")
    if registry is not None:
        owners = {processes[name].pid for name in ("controller", "dispatcher") if name in processes}
        stop_agent_groups(registry, owners)
    if errors:
        raise RuntimeError("; ".join(errors))


def start_server(config: DispatchConfig, run_dir: Path, spawn, *, port: int | None = None,
                 check_interrupted=lambda: None) -> Path:
    """Start an owned loopback Server before any Dispatcher is launched."""
    address = urlparse(config.server)
    if address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost") or address.path not in ("", "/"):
        raise ValueError("local launcher requires a loopback HTTP server URL")
    # 将已绑定 socket 交给 Uvicorn；端口冲突即失败，不把已有服务误当成本次 Server。
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port if port is not None else (address.port if address.port is not None else 8000)))
        listener.listen()
        actual_port = listener.getsockname()[1]
        config.server = f"http://127.0.0.1:{actual_port}"
        config.local.workspace_root = str(run_dir / "work")
        effective_config = run_dir / "config.yaml"
        effective_config.write_text(yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True), encoding="utf-8")
        server = spawn("server", ["serve", "--host", "127.0.0.1", "--port", str(actual_port),
                        "--fd", str(listener.fileno()), "--db-path", str(run_dir / "cairn.db"), "--no-access-log"],
                       pass_fds=(listener.fileno(),))
        deadline = time.monotonic() + 20
        with requests.Session() as http:
            http.trust_env = False
            while True:
                check_interrupted()
                if server.poll() is not None:
                    raise RuntimeError("Server exited; see server.log")
                try:
                    if http.get(config.server + "/settings", timeout=0.5).ok:
                        break
                except requests.RequestException:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Server readiness timed out; see server.log")
                time.sleep(0.1)
    return effective_config


def run_local(config_path: Path, repo: Path, goal: str, run_dir: Path,
              references: tuple[Path, ...] = (), port: int | None = None) -> dict:
    config = DispatchConfig.load(config_path)
    if config.runtime.execution != "local" or config.controller is None:
        raise ValueError("local launcher requires local execution and controller configuration")
    address = urlparse(config.server)
    if address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost") or address.path not in ("", "/"):
        raise ValueError("local launcher requires a loopback HTTP server URL")
    repo = repo.expanduser().resolve()
    if not repo.is_dir() or not goal.strip():
        raise ValueError("repository and non-empty goal are required")
    for reference in references:
        reference.read_text(encoding="utf-8")
    run_dir = run_dir.expanduser().resolve()
    if run_dir.is_relative_to(repo):
        raise ValueError("run directory must be outside the audited repository; choose --run-dir")
    run_dir.mkdir(parents=True, exist_ok=False)
    processes: dict[str, subprocess.Popen] = {}
    logs = []
    summary = {"run_dir": str(run_dir), "status": "failed", "processes": {}}
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    registry = run_dir / "agent-processes"
    registry.mkdir()
    env[REGISTRY_ENV] = str(registry)
    interrupted = False
    # 本机协议流量不经过用户的外部代理，其他代理配置原样留给 Agent。
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, [env.get(key), "127.0.0.1", "localhost"]))

    def spawn(name, args, **kwargs):
        if interrupted:
            raise KeyboardInterrupt("local audit interrupted")
        log = (run_dir / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(CLI + args, stdout=log, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True, **kwargs)
        processes[name] = process
        summary["processes"][name] = {"pid": process.pid}
        if interrupted:
            raise KeyboardInterrupt("local audit interrupted")
        return process

    def terminate(_signum, _frame):
        # 先登记子进程，再在安全点退出，避免 Popen 与所有权记录之间丢失 PID。
        nonlocal interrupted
        interrupted = True

    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        def check_interrupted():
            if interrupted:
                raise KeyboardInterrupt("local audit interrupted")

        effective_config = start_server(config, run_dir, spawn, port=port, check_interrupted=check_interrupted)
        server = processes["server"]
        summary.update(server=config.server, config=str(effective_config))
        dispatcher = spawn("dispatcher", ["dispatch", "--config", str(effective_config)])
        args = ["audit", "--config", str(effective_config), "--repo", str(repo), "--goal", goal]
        for reference in references:
            args += ["--reference", str(reference.resolve())]
        controller = spawn("controller", args)
        click.echo(json.dumps({"server": config.server, "run_dir": str(run_dir), "logs": "controller.log, dispatcher.log, server.log"}, ensure_ascii=False))
        while controller.poll() is None:
            if interrupted:
                raise KeyboardInterrupt("local audit interrupted")
            if server.poll() is not None or dispatcher.poll() is not None:
                raise RuntimeError("Server or Dispatcher exited; see run logs")
            time.sleep(0.2)
        if interrupted:
            raise KeyboardInterrupt("local audit interrupted")
        if controller.returncode:
            raise RuntimeError(f"Controller exited with code {controller.returncode}; see controller.log")
        if server.poll() is not None or dispatcher.poll() is not None:
            raise RuntimeError("Server or Dispatcher exited before audit completion")
        with requests.Session() as http:
            http.trust_env = False
            projects = http.get(config.server + "/projects", timeout=5)
            projects.raise_for_status()
            items = projects.json()
            if len(items) != 1 or items[0]["status"] not in ("completed", "budget_exhausted"):
                raise RuntimeError("Controller exited without a completed or budget_exhausted project")
            project_id = items[0]["id"]
            detail = http.get(f"{config.server}/projects/{project_id}", timeout=5)
            detail.raise_for_status()
            (run_dir / "project.json").write_text(json.dumps(detail.json(), ensure_ascii=False, indent=2), encoding="utf-8")
            snapshot = http.get(f"{config.server}/projects/{project_id}/export?format=yaml", timeout=5)
            snapshot.raise_for_status()
            (run_dir / "graph.yaml").write_text(snapshot.text, encoding="utf-8")
            audit = http.get(f"{config.server}/projects/{project_id}/audit", timeout=5)
            audit.raise_for_status()
            (run_dir / "audit.json").write_text(json.dumps(audit.json(), ensure_ascii=False, indent=2), encoding="utf-8")
            summary.update(status=items[0]["status"], project_id=project_id)
    except BaseException as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, KeyboardInterrupt):
            summary["status"] = "interrupted"
        raise
    finally:
        # 清理期间重复的退出信号不打断进程回收。
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            try:
                stop_processes(processes, registry)
            except BaseException as exc:
                summary.update(status="failed", cleanup_error=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                for name, process in processes.items():
                    summary["processes"][name]["returncode"] = process.returncode
                (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            for log in logs:
                log.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return summary


@click.command()
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
@click.option("--repo", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--goal", required=True)
@click.option("--reference", "references", type=click.Path(exists=True, dir_okay=False, path_type=Path), multiple=True)
@click.option("--run-dir", type=click.Path(file_okay=False, path_type=Path))
@click.option("--port", type=click.IntRange(min=0, max=65535), help="本机端口；0 自动分配，默认使用配置端口")
def main(config_path, repo, goal, references, run_dir, port):
    """启动本次专用的 Server、Dispatcher 和 Controller，结案或退出后回收。"""
    if run_dir is None:
        run_dir = config_path.resolve().parent / "datas/audit-runs" / uuid.uuid4().hex
    try:
        result = run_local(config_path, repo, goal, run_dir, references, port)
        click.echo(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
