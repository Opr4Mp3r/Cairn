"""Import a Builder plan and run the existing generic Cairn research loop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from cairn.controller.builder import Finish, _write
from cairn.controller.local import CLI, start_server, stop_processes
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV
from cairn.server.models import CreateHintInline, CreateProjectRequest


def _data(response, action: str):
    if not response.ok:
        raise RuntimeError(f"{action} failed ({response.status_code}): {response.text}")
    return response.data


def import_plan(client: CairnClient, backend: LocalBackend, prepared: dict) -> dict:
    """Use ordinary project/intent/conclude calls before the Dispatcher starts."""
    folder = Path(prepared["run_dir"]).resolve()
    repo = Path(prepared["source_root"]).resolve(strict=True)
    plan = Finish.model_validate({"action": "finish", **json.loads(Path(prepared["plan"]).read_text())})
    context = {key: prepared[key] for key in ("scan_id", "commit_sha", "repo_full", "source_root")}
    context.update(goal=plan.goal, user_goal=prepared.get("user_goal", plan.goal))
    instructions = (
        f"目标源码位于当前工作目录的 repo/（原路径 {repo}）；Kritt材料在 kritt-materials.json，"
        f"任务上下文在 research-context.json。来源为 scan {prepared['scan_id']} / {prepared['commit_sha']}。"
        "导入Facts是先前分析材料；继续读取实际源码，保留部署、身份、前置条件和未知项。"
    )
    project = _data(client.create_project(CreateProjectRequest(
        title=plan.title, origin=plan.origin, goal=plan.goal, bootstrap_enabled=False,
        hints=[CreateHintInline(content=text, creator="builder") for text in [instructions, *plan.hints]],
    )), "create project")
    project_id = project["project"]["id"]
    result = {"project_id": project_id, "seed_ids": {}, "intent_ids": [], "source_root": str(repo)}
    _write(folder / "project.json", result)
    for seed in plan.seeds:
        imported = _data(client.create_intent(project_id, ["origin"], f"导入Kritt材料：{seed.key}", "builder-import"),
                         "create import intent")
        concluded = _data(client.conclude(project_id, imported["id"], "builder-import",
                                         "Kritt导入的研究材料：" + seed.description), "import seed")
        result["seed_ids"][seed.key.strip()] = concluded["fact"]["id"]
    for intent in plan.intents:
        created = _data(client.create_intent(project_id, [result["seed_ids"][key] for key in intent.from_],
                                           intent.description, "builder"), "create research intent")
        result["intent_ids"].append(created["id"])
    workspace = Path(backend.ensure_running(project_id)).resolve()
    (workspace / "repo").symlink_to(repo, target_is_directory=True)
    shutil.copyfile(prepared["materials"], workspace / "kritt-materials.json")
    _write(workspace / "research-context.json", context)
    result["workspace"] = str(workspace)
    _write(folder / "project.json", result)
    return result


def save_results(client: CairnClient, project_id: str, folder: Path) -> dict[str, str]:
    project = client.get_project(project_id)
    paths = {"project": folder / "result.json", "graph": folder / "graph.yaml",
             "timeline": folder / "timeline.txt"}
    _write(paths["project"], project.model_dump(mode="json", by_alias=True))
    paths["graph"].write_text(client.export_project(project_id), encoding="utf-8")
    paths["timeline"].write_text(client.export_project(project_id, format="timeline"), encoding="utf-8")
    return {key: str(path) for key, path in paths.items()}


def run_research(config: DispatchConfig, prepared: dict, *, cancellation: TaskCancellation | None = None,
                 report=lambda event: None) -> dict:
    """Own a local Server, import the graph, then start the existing Dispatcher."""
    if config.runtime.execution != "local":
        raise ValueError("Builder research requires local execution")
    config = config.model_copy(deep=True)
    local = config.local or LocalConfig()
    config.local = local
    folder = Path(prepared["run_dir"]).resolve()
    registry = folder / "agent-processes"
    token = cancellation or TaskCancellation()
    processes, logs = {}, []
    client = backend = None
    state = {"status": "starting", "run_dir": str(folder), "processes": {}}
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", REGISTRY_ENV: str(registry)}
    if config.controller and config.controller.kritt:
        secret = config.controller.kritt.dsn_env
        env.pop(secret, None)
        local.excluded_env = list(dict.fromkeys([*local.excluded_env, secret]))
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, [env.get(key), "127.0.0.1", "localhost"]))

    def check_cancelled():
        if token.is_cancelled:
            raise KeyboardInterrupt(token.reason or "research interrupted")

    def spawn(name, args, **kwargs):
        check_cancelled()
        log = (folder / f"{name}.log").open("w", encoding="utf-8")
        logs.append(log)
        process = subprocess.Popen(CLI + args, stdout=log, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True, **kwargs)
        processes[name] = process
        state["processes"][name] = {"pid": process.pid}
        check_cancelled()
        return process

    try:
        effective = start_server(config, folder, spawn, check_interrupted=check_cancelled)
        state.update(server=config.server, database=str(folder / "cairn.db"))
        client = CairnClient(config.server, trust_env=False)
        backend = LocalBackend(config.local)
        state.update(import_plan(client, backend, prepared))
        state["project_url"] = f"{config.server}/#/projects/{state['project_id']}"
        state["status"] = "researching"
        dispatcher = spawn("dispatcher", ["dispatch", "--config", str(effective)])
        _write(folder / "research.json", state)
        last_progress = None
        while True:
            check_cancelled()
            if processes["server"].poll() is not None:
                raise RuntimeError("Server exited; see server.log")
            project = client.get_project(state["project_id"])
            progress = {
                "fact_count": sum(fact.id not in {"origin", "goal"} for fact in project.facts),
                "open_intent_count": sum(intent.to is None for intent in project.intents),
            }
            state.update(progress)
            if project.project.status != "active":
                state["status"] = project.project.status
                return state
            if progress != last_progress:
                _write(folder / "research.json", state)
                report(dict(state))
                last_progress = progress
            if dispatcher.poll() is not None:
                raise RuntimeError("Dispatcher exited; see dispatcher.log")
            time.sleep(.2)
    except BaseException as exc:
        state.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                     error=f"{type(exc).__name__}: {exc}")
        if client is not None:
            locator = folder / "project.json"
            if "project_id" not in state and locator.exists():
                state["project_id"] = json.loads(locator.read_text())["project_id"]
            if "project_id" in state:
                client.set_project_status(state["project_id"], "stopped")
        raise
    finally:
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            if client is not None and "project_id" in state:
                try:
                    state["results"] = save_results(client, state["project_id"], folder)
                except Exception as exc:
                    state["export_error"] = f"{type(exc).__name__}: {exc}"
            token.cancel("research shutdown")
            stop_processes(processes, registry)
        except BaseException as exc:
            state.update(status="failed", cleanup_error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            for name, process in processes.items():
                state["processes"][name]["returncode"] = process.returncode
            _write(folder / "research.json", state)
            if client is not None:
                client.close()
            if backend is not None:
                backend.close()
            for log in logs:
                log.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if state["status"] in {"interrupted", "failed"}:
                report(dict(state))
