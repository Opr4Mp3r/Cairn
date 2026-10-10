"""Run prepared audit plans, preserving the previously delivered generic importer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cairn.controller.kritt_query import write_json as _write
from cairn.controller.audit import AuditPlan, prepare_audit
from cairn.controller.builder import continue_builder
from cairn.controller.models import AgentCallResult
from cairn.controller.local import CLI, start_server, stop_processes
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.owned_process import REGISTRY_ENV
from cairn.server.models import AuditInitialIntent, AuditSeed, CreateHintInline, CreateProjectRequest


class GenericPlan(BaseModel):
    """Keep the already-delivered generic importer independent of audit plans."""
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    seeds: list[AuditSeed] = Field(min_length=1)
    intents: list[AuditInitialIntent] = Field(min_length=1)
    hints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def sources(self):
        keys = [seed.key for seed in self.seeds]
        if len(keys) != len(set(keys)) or any(not set(intent.from_) <= set(keys) for intent in self.intents):
            raise ValueError("generic intents require unique imported seed keys")
        return self


def _data(response, action: str):
    if not response.ok:
        raise RuntimeError(f"{action} failed ({response.status_code}): {response.text}")
    return response.data


def import_plan(client: CairnClient, backend: LocalBackend, prepared: dict) -> dict:
    """Use ordinary project/intent/conclude calls before the Dispatcher starts."""
    folder = Path(prepared["run_dir"]).resolve()
    repo = Path(prepared["source_root"]).resolve(strict=True)
    plan = GenericPlan.model_validate(json.loads(Path(prepared["plan"]).read_text()))
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


def import_audit_plan(config, client, backend, prepared, *, ask=None, cancellation=None) -> dict:
    folder = Path(prepared["run_dir"])
    repo = Path(prepared["source_root"]).resolve(strict=True)
    plan = AuditPlan.model_validate(json.loads(Path(prepared["plan"]).read_text()))
    baseline = json.loads(Path(prepared["source_baseline"]).read_text())
    controller_run = folder / "controller" / "prepare"
    receipts, known_calls = [], set()

    def add_builder_receipts():
        for directory in sorted(folder.glob("call-*")):
            artifact = directory / "result.json"
            if artifact in known_calls or not artifact.exists():
                continue
            called = AgentCallResult.model_validate_json(artifact.read_text())
            payload = json.loads((directory / "input.json").read_text())
            receipts.append((called.phase, payload, called, artifact))
            known_calls.add(artifact)

    research_context = {key: prepared[key] for key in ("scan_id", "commit_sha", "repo_full", "source_root", "user_goal")}
    research_path = folder / "research-context.json"
    _write(research_path, {**research_context, "goal": plan.goal})
    context = {
        "repo_path": str(repo), "workspace_root": config.local.workspace_root,
        "user_goal": prepared["user_goal"], "baseline": baseline,
        "requirements_path": prepared["requirements"], "references_path": prepared["references"],
        "reference_paths": [entry["path"] for entry in json.loads(Path(prepared["references"]).read_text())],
        "plan_path": prepared["plan"], "project_path": str(folder / "project.json"),
        "materials": {"kritt-materials.json": prepared["materials"], "references.json": prepared["references"],
                      "requirements.json": prepared["requirements"], "query-receipts.json": str(folder / "query-receipts.json"),
                      "research-context.json": str(research_path)},
    }
    add_builder_receipts()

    def revise(current, issues):
        continue_builder(config, prepared, current_plan=current, preflight_issues=issues,
                         ask=ask, cancellation=cancellation)
        add_builder_receipts()
        revised = AuditPlan.model_validate(json.loads(Path(prepared["plan"]).read_text()))
        _write(research_path, {**research_context, "goal": revised.goal})
        return revised

    result = prepare_audit(config, client, backend, plan=plan, context=context, run_dir=controller_run,
                           revise=revise, receipts=receipts, cancellation=cancellation)
    result["source_root"] = str(repo)
    _write(folder / "project.json", result)
    return result


def save_results(client: CairnClient, project_id: str, folder: Path) -> dict[str, str]:
    project = client.get_project(project_id)
    paths = {"project": folder / "result.json", "graph": folder / "graph.yaml",
             "timeline": folder / "timeline.txt"}
    _write(paths["project"], project.model_dump(mode="json", by_alias=True))
    paths["graph"].write_text(client.export_project(project_id), encoding="utf-8")
    paths["timeline"].write_text(client.export_project(project_id, format="timeline"), encoding="utf-8")
    if project.project.audit is not None:
        paths.update(audit=folder / "audit.json", completions=folder / "completions.json")
        _write(paths["audit"], client.get_audit(project_id).model_dump(mode="json", by_alias=True))
        _write(paths["completions"], [request.model_dump(mode="json", by_alias=True)
                                     for request in client.list_completions(project_id)])
    return {key: str(path) for key, path in paths.items()}


def run_research(config: DispatchConfig, prepared: dict, *, cancellation: TaskCancellation | None = None,
                 report=lambda event: None, ask=None) -> dict:
    """Own a local Server, import the graph, then start the existing Dispatcher."""
    if config.runtime.execution != "local":
        raise ValueError("Builder research requires local execution")
    config = config.model_copy(deep=True)
    local = config.local or LocalConfig()
    config.local = local
    folder = Path(prepared["run_dir"]).resolve()
    audit_run = "policy" in json.loads(Path(prepared["plan"]).read_text())
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
        if audit_run:
            state.update(import_audit_plan(config, client, backend, prepared, ask=ask, cancellation=token))
        else:
            state.update(import_plan(client, backend, prepared))
        state["project_url"] = f"{config.server}/#/projects/{state['project_id']}"
        state["status"] = "researching"
        if audit_run:
            spawn("controller", ["audit", "--project", state["project_id"], "--config", str(effective)])
        dispatcher = spawn("dispatcher", ["dispatch", "--config", str(effective)])
        _write(folder / "research.json", state)
        last_progress = None
        while True:
            check_cancelled()
            if processes["server"].poll() is not None:
                raise RuntimeError("Server exited; see server.log")
            project = client.get_project(state["project_id"])
            progress = {
                "project_status": project.project.status,
                "fact_count": sum(fact.id not in {"origin", "goal"} for fact in project.facts),
                "open_intent_count": sum(intent.to is None for intent in project.intents),
            }
            if project.project.audit is not None:
                progress["audit_phase"] = project.project.audit.phase
                progress["termination_reason"] = project.project.audit.termination_reason
            state.update(progress)
            if project.project.status not in ("active", "draining"):
                state["status"] = project.project.status
                return state
            if progress != last_progress:
                _write(folder / "research.json", state)
                report(dict(state))
                last_progress = progress
            ended = [name for name in ("controller", "dispatcher")
                     if name in processes and processes[name].poll() is not None]
            if ended:
                current = client.get_project(state["project_id"])
                if current.project.status not in ("active", "draining"):
                    state["status"] = current.project.status
                    return state
                if "controller" in ended:
                    raise RuntimeError("Audit monitor exited before project termination; see controller.log")
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
                try:
                    response = client.set_project_status(state["project_id"], "stopped")
                    if not response.ok:
                        state["stop_error"] = f"{response.status_code}: {response.text}"
                    else:
                        state["project_status"] = "stopped"
                except Exception as stop_error:
                    state["stop_error"] = f"{type(stop_error).__name__}: {stop_error}"
        raise
    finally:
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            if client is not None and "project_id" in state:
                try:
                    state["results"] = save_results(client, state["project_id"], folder)
                    saved = json.loads(Path(state["results"]["project"]).read_text())
                    state["project_status"] = saved["project"]["status"]
                    metadata = saved["project"].get("audit")
                    if metadata is not None:
                        state.update(audit_phase=metadata["phase"], termination_reason=metadata["termination_reason"])
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
