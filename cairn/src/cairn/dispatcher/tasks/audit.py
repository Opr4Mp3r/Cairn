from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import subprocess
import uuid

from pydantic import BaseModel, ConfigDict, Field

from cairn.dispatcher.config import DispatchConfig
from cairn.audit_evidence import source_evidence, assert_repository_unchanged
from cairn.audit_inputs import baseline_identity, fixed_contract, READING_GUIDANCE
from cairn.dispatcher.contracts import parse_json_output, validate_explore_payload, validate_reason_payload
from cairn.dispatcher.output_parser import FENCED_BLOCK_RE
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.server.models import AuditFactData, AuditInitialIntent, CompleteRequest, ProjectDetail


def research_payload(project: ProjectDetail, text: str) -> dict:
    if project.project.mode != "audit":
        return parse_json_output(text)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate answer field: {key}")
            result[key] = value
        return result

    segment = text.strip()
    fenced = FENCED_BLOCK_RE.fullmatch(segment)
    if fenced is not None:
        segment = fenced.group(1).strip()
    for suffix in ("", "}"):
        try:
            payload = json.loads(segment + suffix, object_pairs_hook=unique_object)
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("accepted"), bool):
            return payload
    raise ValueError("audit output requires a complete top-level accepted wrapper")


def research_receipt(backend, workspace: str, project: ProjectDetail, worker, *, phase: str, session, prompt: str, process,
                     intent_id: str | None = None) -> str | None:
    if project.project.mode != "audit":
        return
    folder = Path(workspace) / "research"
    if project.project.runtime_generation is not None:
        folder /= f"generation-{project.project.runtime_generation:03d}"
    path = str(folder / f"{phase}-{uuid.uuid4().hex}.json")
    backend.write_text_file(workspace, path, json.dumps({
        "project_id": project.project.id, "worker": worker.name, "harness": worker.type, "phase": phase,
        "role": phase.split("_", 1)[0], "intent_id": intent_id,
        "generation": project.project.runtime_generation,
        "session": session, "prompt": prompt, "process": asdict(process),
        "note": "Raw execution receipt; exit code alone does not validate an audit claim.",
    }, ensure_ascii=False, indent=2))
    return path


def research_prompt(config: DispatchConfig, project: ProjectDetail, name: str, replacements: dict[str, str], *,
                    reason_memory: dict | None = None) -> str:
    if project.project.mode != "audit":
        return render_prompt(load_prompt(config.runtime.prompt_group, name), replacements)
    if config.controller is None or not config.controller.research_prompt_group or project.project.audit is None:
        raise ValueError("audit research requires configured prompt group and metadata")
    metadata = project.project.audit
    workspace_input = json.loads((Path(metadata.workspace_path) / "input.json").read_text()) if metadata.workspace_path else {}
    context = {
        "repo_path": metadata.repo_path,
        "fixed_contract": fixed_contract(
            next((fact.description for fact in project.facts if fact.id == "goal"), ""),
            metadata.policy, metadata.user_goal,
        ),
        "baseline": baseline_identity(metadata.baseline),
        "materials": workspace_input.get("materials", {}),
        "kritt_query": workspace_input.get("kritt_query", {
            "available": False, "reason": "此工作区尚未交付固定 Kritt 查询入口；仍可按任务读取源码。",
        }),
    }
    if name == "reason.md":
        context["candidate_fact_ids"] = candidate_fact_ids(project)
    if reason_memory is not None:
        context["reason_memory"] = reason_memory
    return render_prompt(READING_GUIDANCE + "\n\n" + load_prompt(config.controller.research_prompt_group, name),
                         {"intent_sources": "[]", **replacements,
                          "goal": "见固定任务契约 fixed_contract.goal。",
                          "audit_context": json.dumps(context, ensure_ascii=False, indent=2)})


def candidate_fact_ids(project: ProjectDetail) -> list[str]:
    """Return structurally usable candidates, without judging their evidence."""
    supported = {"origin"}
    remaining = [fact for fact in project.facts if fact.id not in ("origin", "goal") and fact.audit and fact.audit.review_status != "refuted"]
    for _ in remaining:
        added = {fact.id for fact in remaining if set(fact.audit.premise_ids) <= supported}
        if added <= supported:
            break
        supported.update(added)
    return [fact.id for fact in project.facts if fact.id in supported and fact.id != "origin"]


def supported_fact_ids(project: ProjectDetail) -> list[str]:
    """Compatibility name; membership is not an independent evidence verdict."""
    return candidate_fact_ids(project)


def research_workspace(config: DispatchConfig, client, backend, project: ProjectDetail) -> str:
    if project.project.mode != "audit":
        return backend.ensure_running(project.project.id)
    try:
        metadata = project.project.audit
        if config.runtime.execution != "local" or metadata is None or metadata.workspace_path is None:
            raise ValueError("audit project requires its activated local workspace")
        workspace, repo = Path(metadata.workspace_path), Path(metadata.repo_path)
        context = json.loads((workspace / "input.json").read_text(encoding="utf-8"))
        if not isinstance(context, dict) or not workspace.is_dir() or (workspace / "repo").resolve(strict=True) != repo or any(
            context.get(key) != value for key, value in {"project_id": project.project.id, "repo_path": str(repo), "policy_digest": metadata.policy_digest}.items()
        ):
            raise ValueError("audit workspace does not match the activated project")
        files = metadata.baseline.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("baseline source fingerprints are missing")
        try:
            assert_repository_unchanged(repo, metadata.baseline, Path(metadata.workspace_root))
        except ValueError as exc:
            raise ValueError(f"baseline_changed before research: {exc}") from exc
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        try:
            response = client.set_project_status(project.project.id, "stopped")
            if not response.ok:
                raise ValueError(f"{response.status_code}: {response.text}")
        except Exception as stop_error:
            raise RuntimeError(f"project {project.project.id}: baseline check failed ({exc}); stop failed: {stop_error}; state uncertain") from stop_error
        raise ValueError(f"project {project.project.id} stopped: baseline check failed: {exc}") from exc
    return str(workspace)


class ExploreFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    audit: AuditFactData


def explore_result(project: ProjectDetail, payload: dict) -> tuple[str, str | None, AuditFactData | None]:
    if project.project.mode != "audit":
        kind, description = validate_explore_payload(payload)
        return kind, description, None
    if payload.get("accepted") is False:
        return "rejected", None, None
    if payload.get("accepted") is not True:
        raise ValueError("audit output requires accepted wrapper")
    fact = ExploreFact.model_validate(payload.get("data"), strict=True)
    if not fact.description.strip() or fact.audit.review_status != "unverified":
        raise ValueError("research observations must be non-empty and unverified")
    known = {item.id for item in project.facts if item.id != "goal"}
    if not set(fact.audit.premise_ids) <= known:
        raise ValueError("unknown or goal premise in research observation")
    metadata = project.project.audit
    assert metadata is not None
    repo = Path(metadata.repo_path)
    fact.audit.evidence = source_evidence(repo, fact.audit.evidence, metadata.baseline)
    for reference in fact.audit.evidence:
        if "files" in metadata.baseline and metadata.baseline["files"].get(reference.path) != reference.sha256:
            raise ValueError(f"baseline_changed or source not snapshotted: {reference.path}")
    return "fact", fact.description.strip(), fact.audit


def reason_result(project: ProjectDetail, payload: dict, *, open_intents_empty: bool, max_intents: int):
    if project.project.mode == "audit" and payload.get("accepted") is True and isinstance(payload.get("data"), dict):
        data = dict(payload["data"])
        if "planning_notes" in data:
            if not isinstance(data.pop("planning_notes"), str):
                raise ValueError("planning_notes must be a string")
            payload = {**payload, "data": data}
    if project.project.mode == "audit" and payload.get("accepted") is not False:
        if payload.get("accepted") is not True or not isinstance(payload.get("data"), dict) or set(payload["data"]) - {"intents", "intent", "complete"}:
            raise ValueError("audit reason requires a strict action wrapper")
    kind, data = validate_reason_payload(payload, open_intents_empty, max_intents)
    if project.project.mode != "audit" or kind in ("noop", "rejected"):
        return kind, data
    if payload.get("accepted") is not True or set(payload["data"]) - {"intents", "intent", "complete"}:
        raise ValueError("audit reason requires a strict action wrapper")
    known = {fact.id for fact in project.facts if fact.id != "goal"}
    if kind == "intents":
        intents = [AuditInitialIntent.model_validate(item, strict=True) for item in data]
        if any(not set(intent.from_) <= known for intent in intents):
            raise ValueError("unknown or goal source in audit intent")
        return kind, [intent.model_dump(by_alias=True) for intent in intents]
    complete = CompleteRequest.model_validate({**data, "worker": "reason"}, strict=True)
    if not set(complete.from_) <= set(candidate_fact_ids(project)):
        raise ValueError("audit completion requires non-refuted facts with a valid premise closure")
    return kind, {"from": complete.from_, "description": complete.description}


def bootstrap_metadata(project: ProjectDetail, payload: dict) -> AuditFactData | None:
    if project.project.mode != "audit" or payload.get("accepted") is False:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) - {"fact", "complete"}:
        raise ValueError("audit bootstrap requires fact metadata")
    _kind, _description, audit = explore_result(project, {"accepted": True, "data": data.get("fact")})
    return audit
