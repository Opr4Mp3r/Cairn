"""B-CP1: exercise the shared harness and bounded Kritt tools without creating a project."""
from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cairn.controller.app import call_agent
from cairn.controller.kritt import KrittRef, KrittView, check_control, read_kritt_view
from cairn.controller.kritt_source import assert_source_unchanged, check_observation, normalize_source_path, verify_source
from cairn.controller.kritt_tools import GetAction, KrittTools, SearchAction, ToolAction
from cairn.dispatcher.config import DispatchConfig, LocalConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend


class ProbeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    references: list[KrittRef] = Field(min_length=1, max_length=8)
    path: str = Field(min_length=1)
    start_line: int = Field(gt=0)
    end_line: int = Field(gt=0)
    quotation: str = Field(min_length=1, max_length=12000)
    observation: str = Field(min_length=1, max_length=2000)
    conditions: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def short_excerpt(self):
        if self.end_line < self.start_line or self.end_line - self.start_line >= 40:
            raise ValueError("source excerpt must contain 1..40 complete lines")
        return self


class ProbeAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: str = Field(pattern=r"^(query|finish)$")
    query: ToolAction | None = None
    result: ProbeObservation | None = None

    @model_validator(mode="after")
    def one_action(self):
        if self.action == "query" and (self.query is None or self.result is not None):
            raise ValueError("query requires only a query payload")
        if self.action == "finish" and (self.result is None or self.query is not None):
            raise ValueError("finish requires only a result payload")
        return self


def _source_paths(tools: KrittTools, repo: Path, baseline: dict) -> list[str]:
    paths = set()
    for ref in tools.ledger:
        answer = tools.resolve(ref).get("json_answer", {})
        for field in ("entrypoint_file_path", "flow_file_path", "file_path"):
            if answer.get(field):
                try:
                    paths.add(normalize_source_path(repo, baseline, answer[field]))
                except ValueError:
                    pass  # A source gap stays a gap; no path is guessed.
    return sorted(paths)


def validate_observation(tools: KrittTools, repo: Path, baseline: dict, result: ProbeObservation) -> dict:
    tools.validate_references(result.references)
    referenced_paths = set()
    for ref in result.references:
        answer = tools.resolve(ref).get("json_answer", {})
        for field in ("entrypoint_file_path", "flow_file_path", "file_path"):
            if answer.get(field):
                try:
                    referenced_paths.add(normalize_source_path(repo, baseline, answer[field]))
                except ValueError:
                    pass
    evidence = check_observation(repo, baseline, result.path, result.start_line, result.end_line, result.quotation)
    if evidence.path not in referenced_paths:
        raise ValueError("source observation must correspond to a fully read entrypoint/flow/finding")
    return {**result.model_dump(mode="json"), "source_evidence": evidence.model_dump(mode="json"),
            "status": "unverified", "note": "引用和引文已机械核对；未独立确认漏洞、匿名可达性或Goal达成。"}


def run_kritt_probe(config: DispatchConfig, *, target_name: str, user_goal: str, output: Path,
                    scan_id: int | None = None, view_path: Path | None = None,
                    cancellation: TaskCancellation | None = None) -> dict:
    if config.controller is None or config.controller.kritt is None or config.runtime.execution != "local":
        raise ValueError("kritt-probe requires controller.kritt and local execution")
    settings = config.controller.kritt
    if target_name not in settings.targets or not user_goal.strip():
        raise ValueError("a configured target and a nonempty goal are required")
    target = settings.targets[target_name]
    output = output.expanduser().resolve()
    repo = Path(target.source_root).expanduser().resolve()
    manifest = Path(target.source_manifest).expanduser().resolve()
    if output.is_relative_to(repo):
        raise ValueError("probe output must be outside the audited source")
    output.mkdir(parents=True, exist_ok=False)
    cancellation = cancellation or TaskCancellation()
    deadline = time.monotonic() + settings.probe_seconds
    tools = None
    calls = queries = 0
    try:
        if cancellation.is_cancelled:
            raise KeyboardInterrupt(cancellation.reason or "probe cancelled")
        if view_path is None:
            dsn = os.environ.get(settings.dsn_env)
            if not dsn:
                raise ValueError(f"configured read-only connection environment {settings.dsn_env} is missing")
            view = read_kritt_view(dsn, repo_full=target.repo_full, scan_id=scan_id or target.scan_id,
                                   commit=target.commit, profile=settings.profile, max_rows=settings.max_rows,
                                   max_bytes=settings.max_bytes, statement_timeout_ms=settings.statement_timeout_ms,
                                   deadline=deadline, cancellation=cancellation)
        else:
            view = KrittView.load(view_path, max_rows=settings.max_rows, max_bytes=settings.max_bytes)
            if view.scan["repo_full"] != target.repo_full or (
                (scan_id or target.scan_id) is not None and view.scan["id"] != (scan_id or target.scan_id)
            ) or (target.commit is not None and view.scan["commit_sha"] != target.commit):
                raise ValueError("offline view does not match the configured target selection")
        check_control(deadline, cancellation)
        view.save(output / "view.json")
        check_control(deadline, cancellation)
        baseline = verify_source(repo, view, manifest, deadline=deadline, cancellation=cancellation)
        check_control(deadline, cancellation)
        (output / "source-baseline.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        tools = KrittTools(view, max_rows=settings.response_rows, max_bytes=settings.response_bytes)
        description = tools.execute({"action": "describe"}).model_dump(mode="json")
        local = config.local or LocalConfig()
        backend = LocalBackend(local.model_copy(update={
            "excluded_env": list(dict.fromkeys([*local.excluded_env, settings.dsn_env])),
        }))
        instruction = resources.files("cairn.controller").joinpath("prompts/kritt_probe.md").read_text(encoding="utf-8")
        history = []
        phase = "search"
        hits = []
        selected = None
        materials_path = output / "selected-materials.json"
        error = None
        latest = description
        for number in range(settings.max_agent_calls):
            if cancellation.is_cancelled:
                raise KeyboardInterrupt(cancellation.reason or "probe cancelled")
            check_control(deadline, cancellation)
            assert_source_unchanged(repo, baseline, deadline=deadline, cancellation=cancellation)
            remaining = int(deadline - time.monotonic()) - 5
            if remaining <= 0:
                raise ValueError("Kritt probe preparation deadline exhausted")
            schema = ProbeAction.model_json_schema()
            schema["properties"]["action"]["enum"] = ["finish" if phase == "observe" else "query"]
            schema["properties"]["query"] = {"type": "null"} if phase == "observe" else {
                "$ref": f"#/$defs/{'SearchAction' if phase == 'search' else 'GetAction'}"}
            context = {"user_goal": user_goal, "probe_phase": phase, "repo_path": str(repo), "scan_id": view.scan["id"],
                       "commit_sha": view.scan["commit_sha"], "view_digest": view.digest,
                       "description": description, "last_tool_result": latest, "history": history,
                       "fully_read_references": [ref.model_dump(mode="json") for ref in tools.ledger],
                       "search_hits": hits, "selected_reference": selected,
                       "materials_path": str(materials_path) if phase == "observe" else None,
                       "last_error": error,
                       "available_source_paths": _source_paths(tools, repo, baseline),
                       "remaining_calls": settings.max_agent_calls - number,
                       "remaining_queries": settings.max_queries - queries,
                       "output_schema": schema}
            folder = output / f"call-{number + 1:02d}"
            folder.mkdir()
            (folder / "input.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
            remaining = int(check_control(deadline, cancellation)) - 5
            if remaining <= 0:
                raise TimeoutError("Kritt probe preparation deadline exhausted")
            called = call_agent(config, backend, role="builder",
                                prompt=instruction + "\n\n" + json.dumps(context, ensure_ascii=False),
                                result_model=ProbeAction, workspace=output,
                                timeout_seconds=min(config.controller.timeouts.build, remaining),
                                phase="kritt_probe", cancellation=cancellation)
            calls += 1
            (folder / "result.json").write_text(called.model_dump_json(indent=2), encoding="utf-8")
            assert_source_unchanged(repo, baseline, deadline=deadline, cancellation=cancellation)
            if cancellation.is_cancelled:
                raise KeyboardInterrupt(cancellation.reason or "probe cancelled")
            if called.status != "success":
                if called.status == "invalid_output":
                    error = {"error": called.error, "previous_output": (called.response or "")[:12000]}
                    history.append({"call": calls, "status": "invalid_output"})
                    continue
                raise ValueError(f"Agent probe failed: {called.status}")
            action = ProbeAction.model_validate(called.data)
            error = None
            if action.action == "query":
                try:
                    query = action.query.root
                    if phase == "search" and not isinstance(query, SearchAction):
                        raise ValueError("search phase requires a search query")
                    if phase == "get" and (not isinstance(query, GetAction) or query.cursor is not None or
                                          query.ref.model_dump(mode="json") not in hits):
                        raise ValueError("get phase requires an exact reference from the search hits, without a cursor")
                    if phase == "observe":
                        raise ValueError("materials are complete; read their source and return finish")
                    # The probe needs one branch; program-owned pagination avoids repeated broad searches.
                    while True:
                        if cancellation.is_cancelled:
                            raise KeyboardInterrupt(cancellation.reason or "probe cancelled")
                        if queries >= settings.max_queries or time.monotonic() >= deadline:
                            raise RuntimeError("Kritt probe query/time budget exhausted")
                        queries += 1
                        latest = tools.execute(query).model_dump(mode="json")
                        history.append({"call": calls, "query": query.model_dump(mode="json"),
                                        "receipt_id": latest["receipt"]["receipt_id"]})
                        tools.save_receipts(output / "query-receipts.json")
                        (folder / f"tool-result-{queries:02d}.json").write_text(
                            json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
                        cursor = latest["receipt"]["next_cursor"]
                        if not isinstance(query, GetAction) or cursor is None:
                            break
                        query = query.model_copy(update={"cursor": cursor})
                    if phase == "search":
                        hits = [record["ref"] for record in latest["records"]]
                        if hits:
                            phase = "get"
                    else:
                        selected = action.query.root.ref.model_dump(mode="json")
                        materials_path.write_text(json.dumps([
                            {"ref": ref.model_dump(mode="json"), "content": tools.resolve(ref)}
                            for ref in tools.ledger], ensure_ascii=False, indent=2), encoding="utf-8")
                        phase = "observe"
                except ValueError as exc:
                    error = {"tool_error": str(exc)}
                    history.append({"call": calls, "query_status": "error"})
                tools.save_receipts(output / "query-receipts.json")
                continue
            try:
                if phase != "observe":
                    raise ValueError("finish requires a model-selected search and a fully expanded get")
                check_control(deadline, cancellation)
                result = validate_observation(tools, repo, baseline, action.result)
                check_control(deadline, cancellation)
            except ValueError as exc:
                error = {"observation_error": str(exc)}
                history.append({"call": calls, "observation_status": "invalid"})
                continue
            if time.monotonic() >= deadline:
                raise ValueError("Kritt probe preparation deadline exhausted")
            summary = {"status": "success", "scan_id": view.scan["id"], "repo_full": target.repo_full,
                       "commit_sha": view.scan["commit_sha"], "view_digest": view.digest,
                       "source_digest": baseline["digest"], "calls": calls, "queries": queries,
                       "output": str(output), "observation": result}
            (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            return summary
        raise ValueError("Kritt probe Agent-call budget exhausted")
    except BaseException as exc:
        if tools is not None:
            tools.save_receipts(output / "query-receipts.json")
        (output / "failure.json").write_text(json.dumps({"status": "error", "type": type(exc).__name__,
                                                       "error": str(exc), "calls": calls, "queries": queries},
                                                      ensure_ascii=False, indent=2), encoding="utf-8")
        raise
