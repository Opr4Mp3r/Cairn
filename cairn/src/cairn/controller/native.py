"""Native conversation → prepared plan → independently owned local research."""
from __future__ import annotations

from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from cairn.controller.builder import Finish
from cairn.controller.audit import AuditPlan
from cairn.controller.kritt_query import collect_queries, write_json
from cairn.controller.local import stop_agent_groups
from cairn.controller.research import run_research
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV


def start_prepared(config_path: Path, run_dir: Path) -> dict:
    """Wait for preflight or handoff; the child owns research after this returns."""
    folder = run_dir.expanduser().resolve(strict=True)
    config_path = config_path.expanduser().resolve(strict=True)
    with (folder / "launch.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"此 run 正在准备或研究中；查看 {folder / 'launch.json'}") from exc
        has_project = (folder / "project.json").exists()
        database = folder / "cairn.db"
        if not has_project and database.exists():
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone():
                    has_project = connection.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None
        if has_project:
            raise ValueError("此 run 已创建项目；请查看 research.json，开启新研究请使用新 run-dir")
        prepared = json.loads((folder / "run.json").read_text())
        if Path(prepared["run_dir"]).resolve() != folder:
            raise ValueError("run-dir 与准备材料记录不一致")
        plan = AuditPlan.model_validate_json(Path(prepared["plan"]).read_text())
        Finish.model_validate({**plan.model_dump(mode="json", by_alias=True), "action": "finish"})
        config = DispatchConfig.load(config_path)
        if config.runtime.execution != "local" or config.controller is None or not config.controller.research_prompt_group:
            raise ValueError("Native Builder requires local execution and controller.research_prompt_group")
        counts = collect_queries(Path(prepared["query_context"]))
        prepared.update(counts)
        write_json(folder / "run.json", prepared)
        status_path, log_path = folder / "launch.json", folder / "launch.log"
        write_json(status_path, {"status": "preparing", "run_dir": str(folder)})
        child = None

        def interrupted(_sig, _frame):
            raise KeyboardInterrupt("启动已中断")

        previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with log_path.open("w") as output, log_path.open() as progress:
                child = subprocess.Popen(
                    [sys.executable, "-m", "cairn.controller.native", str(config_path), str(folder), str(lock.fileno())],
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    start_new_session=True, pass_fds=(lock.fileno(),),
                )
                while True:
                    text = progress.read()
                    if text:
                        print(text, end="", file=sys.stderr, flush=True)
                    ended = child.poll() is not None
                    state = json.loads(status_path.read_text())
                    if ended:
                        if state["status"] == "preparing":
                            raise RuntimeError(f"启动进程异常退出 ({child.returncode})；查看 {log_path}")
                        return state
                    if state["status"] == "researching":
                        return state
                    time.sleep(.2)
        except BaseException:
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
            if child is not None and child.poll() is None:
                child.terminate()
                # The child owns service and Agent cleanup; retain the lock until it exits.
                child.wait()
            raise
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        # Closing (not unlocking) preserves the inherited flock in the child.


def supervise(config_path: Path, folder: Path, lock_fd: int) -> None:
    configure_logging("INFO")
    token = TaskCancellation()
    registry = folder / "agent-processes"
    os.environ[REGISTRY_ENV] = str(registry)
    state = {"status": "preparing", "run_dir": str(folder), "supervisor_pid": os.getpid(),
             "progress": str(folder / "research.json"), "log": str(folder / "launch.log")}

    def publish(event):
        state.update(event)
        write_json(folder / "launch.json", state)

    def interrupted(_sig, _frame):
        raise KeyboardInterrupt("研究进程收到终止信号")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        publish({})
        config = DispatchConfig.load(config_path)
        prepared = json.loads((folder / "run.json").read_text())
        result = run_research(config, prepared, cancellation=token, report=publish, external_builder=True)
        state.update(result)
    except BaseException as exc:
        state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                     error=f"{type(exc).__name__}: {exc}")
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        try:
            token.cancel("supervisor shutdown")
            stop_agent_groups(registry, {os.getpid()})
        except BaseException as exc:
            state.update(status="failed", cleanup_error=f"{type(exc).__name__}: {exc}")
        finally:
            publish(state)
            os.close(lock_fd)


if __name__ == "__main__":
    supervise(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
