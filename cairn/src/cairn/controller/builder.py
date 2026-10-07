"""B-CP2: turn a user requirement into a Cairn research plan. No project is created."""
from __future__ import annotations

from collections.abc import Callable
from importlib import resources
import json
import os
from pathlib import Path
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from cairn.controller.app import call_agent
from cairn.controller.kritt import KrittView, read_kritt_view
from cairn.controller.kritt_source import verify_source
from cairn.controller.kritt_tools import FIELDS, MECHANISMS, GetAction, KrittTools, ToolAction
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Seed(_Strict):
    key: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=4000)


class Intent(_Strict):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    from_: list[str] = Field(alias="from", min_length=1, max_length=6)
    description: str = Field(min_length=1, max_length=4000)


class Ask(_Strict):
    action: Literal["ask"]
    questions: list[str] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def concrete(self):
        if any(not item.strip() or len(item) > 1500 for item in self.questions):
            raise ValueError("questions must be concrete and short")
        return self


class Query(_Strict):
    action: Literal["query"]
    query: ToolAction


class Finish(_Strict):
    action: Literal["finish"]
    title: str = Field(min_length=1, max_length=200)
    origin: str = Field(min_length=1, max_length=4000)
    goal: str = Field(min_length=1, max_length=4000)
    seeds: list[Seed] = Field(min_length=1, max_length=8)
    intents: list[Intent] = Field(min_length=1, max_length=8)
    hints: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def usable_plan(self):
        keys = [seed.key.strip() for seed in self.seeds]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("seed keys must be unique")
        allowed = set(keys)
        for intent in self.intents:
            if any(not item.strip() for item in intent.from_) or not set(intent.from_) <= allowed:
                raise ValueError("each intent must start from a seed in this plan")
        if any(not hint.strip() for hint in self.hints):
            raise ValueError("hints must be nonempty")
        return self


class BuilderAction(RootModel[Annotated[Ask | Query | Finish, Field(discriminator="action")]]):
    pass


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _materials(tools: KrittTools) -> list[dict]:
    return [{"ref": ref.model_dump(mode="json"), "content": tools.resolve(ref)} for ref in tools.ledger]


def _references(target) -> list[dict]:
    loaded = []
    for value in target.references:
        path = Path(value).expanduser().resolve()
        loaded.append({"path": str(path), "text": path.read_text(encoding="utf-8")})
    return loaded


def _turn(config, backend, folder: Path, call: int, instruction: str, context: dict, cancellation):
    deadline = time.monotonic() + config.controller.timeouts.build
    payload = context
    prompt_text = instruction
    for attempt in range(2):
        if cancellation.is_cancelled:
            raise KeyboardInterrupt(cancellation.reason or "builder cancelled")
        remaining = int(deadline - time.monotonic()) - 1
        if remaining <= 0:
            raise TimeoutError("builder call timed out")
        directory = folder / f"call-{call:03d}"
        if attempt:
            directory = folder / f"call-{call:03d}-format-repair"
        directory.mkdir()
        _write(directory / "input.json", payload)
        called = call_agent(config, backend, role="builder",
                            prompt=prompt_text + "\n\n" + json.dumps(payload, ensure_ascii=False),
                            result_model=BuilderAction, workspace=folder, timeout_seconds=remaining,
                            phase="builder" if not attempt else "builder_format_repair", cancellation=cancellation)
        _write(directory / "result.json", json.loads(called.model_dump_json()))
        if called.status == "success":
            return BuilderAction.model_validate(called.data).root
        if called.status != "invalid_output" or attempt:
            raise RuntimeError(f"builder: {called.status}: {called.error}")
        prompt_text = "只修复已有输出，使它符合 schema。不要查询，不要改研究结论。只返回一个原始 JSON 对象。"
        payload = {"previous_response": called.response or "", "validation_error": called.error,
                   "output_schema": context["output_schema"]}
    raise AssertionError("unreachable")


def run_builder(config: DispatchConfig, *, target_name: str, user_goal: str, run_dir: Path,
                scan_id: int | None = None, view_path: Path | None = None,
                ask: Callable[[list[str]], str] | None = None,
                cancellation: TaskCancellation | None = None) -> dict:
    """Query Kritt and write plan.json. Questions are answered in this process."""
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
    tools = None
    calls = queries = 0
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
        tools = KrittTools(view, max_rows=settings.response_rows, max_bytes=settings.response_bytes)
        described = tools.execute({"action": "describe"}).model_dump(mode="json")
        local = config.local or LocalConfig()
        backend = LocalBackend(local.model_copy(update={
            "excluded_env": list(dict.fromkeys([*local.excluded_env, settings.dsn_env])),
        }))
        instruction = resources.files("cairn.controller").joinpath("prompts/builder.md").read_text(encoding="utf-8")
        messages = [{"role": "user", "content": user_goal.strip()}]
        last = described
        references = _references(target)
        while True:
            calls += 1
            context = {"user_goal": user_goal.strip(), "messages": messages, "repo_path": str(repo),
                       "scan_id": view.scan["id"], "commit_sha": view.scan["commit_sha"],
                       "view_digest": view.digest, "references": references, "description": described,
                       "last_tool_result": last, "fields": {key: list(value) for key, value in FIELDS.items()},
                       "mechanisms": {key: list(value) for key, value in MECHANISMS.items()},
                       "output_schema": BuilderAction.model_json_schema()}
            action = _turn(config, backend, folder, calls, instruction, context, cancellation)
            if isinstance(action, Ask):
                if ask is None:
                    raise ValueError("需要补充信息后重新运行：" + "；".join(action.questions))
                reply = ask(action.questions)
                if not isinstance(reply, str) or not reply.strip():
                    raise ValueError("补充内容为空")
                messages.append({"role": "assistant", "content": "\n".join(action.questions)})
                messages.append({"role": "user", "content": reply.strip()})
                continue
            if isinstance(action, Query):
                query = action.query.root
                while True:
                    result = tools.execute(query)
                    queries += 1
                    last = result.model_dump(mode="json")
                    cursor = result.receipt.next_cursor
                    if not isinstance(query, GetAction) or cursor is None:
                        break
                    query = query.model_copy(update={"cursor": cursor})
                continue
            plan = action.model_dump(mode="json", by_alias=True)
            plan.pop("action", None)
            materials = _materials(tools)
            _write(folder / "plan.json", plan)
            _write(folder / "selected-materials.json", materials)
            tools.save_receipts(folder / "query-receipts.json")
            summary = {"status": "plan_ready", "scan_id": view.scan["id"], "commit_sha": view.scan["commit_sha"],
                       "repo_full": target.repo_full, "source_root": str(repo), "run_dir": str(folder),
                       "plan": str(folder / "plan.json"), "materials": str(folder / "selected-materials.json"),
                       "calls": calls, "queries": queries, "seeds": len(action.seeds), "intents": len(action.intents)}
            _write(folder / "run.json", summary)
            return summary
    except BaseException as exc:
        if not isinstance(exc, KeyboardInterrupt):
            if tools is not None:
                _write(folder / "selected-materials.json", _materials(tools))
                tools.save_receipts(folder / "query-receipts.json")
            _write(folder / "error.json", {"status": "error", "type": type(exc).__name__, "error": str(exc),
                                           "calls": calls, "queries": queries})
        raise
