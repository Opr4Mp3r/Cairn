"""Prepare an AuditPlan using one harness session and read-only Kritt commands."""
from __future__ import annotations

from collections.abc import Callable
from graphlib import TopologicalSorter
from importlib import resources
import json
import os
from pathlib import Path
import shlex
import sys
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from cairn.controller.app import call_agent
from cairn.controller.audit import AuditPlan
from cairn.controller.kritt import KrittView, read_kritt_view
from cairn.controller.kritt_query import collect_queries, write_json as _write
from cairn.controller.kritt_source import verify_source
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["ask"]
    questions: list[str] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def concrete(self):
        if any(not item.strip() or len(item) > 1500 for item in self.questions):
            raise ValueError("questions must be concrete and short")
        return self


class Finish(AuditPlan):
    action: Literal["finish"]

    @model_validator(mode="after")
    def usable_plan(self):
        keys = [seed.key for seed in self.seeds]
        if len(keys) != len(set(keys)) or {"origin", "goal"} & set(keys):
            raise ValueError("seed keys must be unique and cannot be origin or goal")
        allowed = {"origin", *keys}
        if any(not set(sources) <= allowed for sources in [
            *(seed.audit.premise_ids for seed in self.seeds), *(intent.from_ for intent in self.intents),
        ]):
            raise ValueError("unknown seed reference or goal used as a source")
        TopologicalSorter({seed.key: [ref for ref in seed.audit.premise_ids if ref != "origin"]
                           for seed in self.seeds}).prepare()
        return self


class BuilderAction(RootModel[Annotated[Ask | Finish, Field(discriminator="action")]]):
    pass


def _references(target, folder: Path) -> Path:
    destination = folder / "references"
    destination.mkdir()
    loaded = []
    for index, value in enumerate(target.references, 1):
        path = Path(value).expanduser().resolve()
        saved = destination / f"{index:03d}-{path.name}"
        saved.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        loaded.append({"source_path": str(path), "path": str(saved)})
    output = folder / "references.json"
    _write(output, loaded)
    return output


def _turn(config, backend, folder: Path, call: int, instruction: str, context: dict, cancellation, session):
    deadline = time.monotonic() + config.controller.timeouts.build
    payload, prompt_text = context, instruction
    for attempt in range(2):
        if cancellation.is_cancelled:
            raise KeyboardInterrupt(cancellation.reason or "builder cancelled")
        remaining = int(deadline - time.monotonic()) - 1
        if remaining <= 0:
            raise TimeoutError("builder call timed out")
        directory = folder / (f"call-{call:03d}" if not attempt else f"call-{call:03d}-format-repair")
        directory.mkdir()
        _write(directory / "input.json", payload)
        called = call_agent(config, backend, role="builder", session=session,
                            prompt=prompt_text + "\n\n" + json.dumps(payload, ensure_ascii=False),
                            result_model=BuilderAction, workspace=folder, timeout_seconds=remaining,
                            phase="builder" if not attempt else "builder_format_repair", cancellation=cancellation)
        _write(directory / "result.json", json.loads(called.model_dump_json()))
        session = called.session
        if called.status == "success":
            return BuilderAction.model_validate(called.data).root, session
        if called.status != "invalid_output" or attempt:
            raise RuntimeError(f"builder: {called.status}: {called.error}")
        if not session:
            raise RuntimeError("builder harness returned no session for format repair")
        prompt_text = "只修复已有输出，使它符合 schema。不要查询，不要改研究结论。只返回一个原始 JSON 对象。"
        payload = {"previous_response": called.response or "", "validation_error": called.error,
                   "output_schema": context["output_schema"]}
    raise AssertionError("unreachable")


def run_builder(config: DispatchConfig, *, target_name: str, user_goal: str, run_dir: Path,
                scan_id: int | None = None, view_path: Path | None = None,
                ask: Callable[[list[str]], str] | None = None,
                cancellation: TaskCancellation | None = None) -> dict:
    """Prepare an audit plan. R-CP1 does not create or activate a Cairn project."""
    if config.controller is None or config.controller.kritt is None or config.runtime.execution != "local":
        raise ValueError("builder requires controller.kritt and local execution")
    if not user_goal.strip():
        raise ValueError("a nonempty goal is required")
    settings = config.controller.kritt
    if target_name not in settings.targets:
        raise ValueError("a configured target is required")
    target = settings.targets[target_name]
    folder = run_dir.expanduser().resolve()
    repo = Path(target.source_root).expanduser().resolve()
    manifest = Path(target.source_manifest).expanduser().resolve()
    if folder.is_relative_to(repo):
        raise ValueError("run directory must be outside the audited source")
    folder.mkdir(parents=True, exist_ok=False)
    cancellation = cancellation or TaskCancellation()
    calls, session = 0, None
    query_context = folder / "kritt-context.json"
    try:
        if view_path is None:
            dsn = os.environ.get(settings.dsn_env)
            if not dsn:
                raise ValueError(f"configured read-only connection environment {settings.dsn_env} is missing")
            view = read_kritt_view(dsn, repo_full=target.repo_full, scan_id=scan_id or target.scan_id,
                                   commit=target.commit, profile=settings.profile, max_rows=settings.max_rows,
                                   max_bytes=settings.max_bytes, statement_timeout_ms=settings.statement_timeout_ms,
                                   cancellation=cancellation)
        else:
            view = KrittView.load(view_path, max_rows=settings.max_rows, max_bytes=settings.max_bytes)
            selected = scan_id or target.scan_id
            if view.scan["repo_full"] != target.repo_full or (
                selected is not None and view.scan["id"] != selected
            ) or (target.commit is not None and view.scan["commit_sha"] != target.commit):
                raise ValueError("offline view does not match the configured target selection")
        baseline = verify_source(repo, view, manifest, cancellation=cancellation)
        _write(folder / "source-baseline.json", baseline)
        view.save(folder / "kritt-view.json")
        _write(query_context, {
            "view_path": str(folder / "kritt-view.json"), "view_digest": view.digest,
            "queries_dir": str(folder / "queries"), "max_rows": settings.max_rows, "max_bytes": settings.max_bytes,
            "response_rows": settings.response_rows, "response_bytes": settings.response_bytes,
        })
        references = _references(target, folder)
        messages = [{"role": "user", "content": user_goal.strip()}]
        _write(folder / "requirements.json", {"user_goal": user_goal.strip(), "messages": messages})
        local = config.local or LocalConfig()
        backend = LocalBackend(local.model_copy(update={
            "excluded_env": list(dict.fromkeys([*local.excluded_env, settings.dsn_env])),
        }))
        instruction = resources.files("cairn.controller").joinpath("prompts/builder.md").read_text(encoding="utf-8")
        context = {
            "user_goal": user_goal.strip(), "requirements_path": str(folder / "requirements.json"),
            "repo_path": str(repo), "repo_full": view.scan["repo_full"],
            "scan_id": view.scan["id"], "commit_sha": view.scan["commit_sha"],
            "references_path": str(references),
            "kritt_query_command": shlex.join([sys.executable, "-m", "cairn.controller.kritt_query",
                                                "--context", str(query_context)]),
            "output_schema": BuilderAction.model_json_schema(),
        }
        while True:
            calls += 1
            action, session = _turn(config, backend, folder, calls, instruction, context, cancellation, session)
            counts = collect_queries(query_context)
            if isinstance(action, Ask):
                if ask is None:
                    raise ValueError("需要补充信息后重新运行：" + "；".join(action.questions))
                if not session:
                    raise RuntimeError("builder harness returned no session for the answer")
                reply = ask(action.questions)
                if not isinstance(reply, str) or not reply.strip():
                    raise ValueError("补充内容为空")
                messages.extend([{"role": "assistant", "content": "\n".join(action.questions)},
                                 {"role": "user", "content": reply.strip()}])
                _write(folder / "requirements.json", {"user_goal": user_goal.strip(), "messages": messages})
                context = {"reply": reply.strip(), "requirements_path": str(folder / "requirements.json"),
                           "output_schema": BuilderAction.model_json_schema()}
                instruction = "结合用户答复继续当前开题，必要时使用已有工具；最终只返回 ask 或 finish JSON。"
                continue
            for seed in action.seeds:
                seed.audit.review_status = "unverified"
            plan = action.model_dump(mode="json", by_alias=True, exclude={"action"})
            _write(folder / "plan.json", plan)
            summary = {
                "status": "plan_ready", "scan_id": view.scan["id"], "commit_sha": view.scan["commit_sha"],
                "repo_full": target.repo_full, "source_root": str(repo), "run_dir": str(folder),
                "target": target_name, "user_goal": user_goal.strip(), "builder_session": session,
                "plan": str(folder / "plan.json"), "materials": str(folder / "selected-materials.json"),
                "requirements": str(folder / "requirements.json"), "references": str(references),
                "source_baseline": str(folder / "source-baseline.json"), "query_context": str(query_context),
                "calls": calls, **counts, "seeds": len(action.seeds), "intents": len(action.intents),
            }
            _write(folder / "run.json", summary)
            return summary
    except BaseException as exc:
        error = {"status": "error", "type": type(exc).__name__, "error": str(exc),
                 "calls": calls, "builder_session": session}
        if query_context.exists():
            try:
                collect_queries(query_context)
            except (OSError, ValueError, KeyError) as collection_error:
                error["collection_error"] = str(collection_error)
        _write(folder / "error.json", error)
        raise
