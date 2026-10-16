from __future__ import annotations

from datetime import datetime, timedelta
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, Request, Header

from cairn.server.db import get_conn
from cairn.server.models import (AuditCompletion, AuditDetail, AuditMetadata, AuditRecord, AuditRecordRequest,
                                AuditBudgetRequest, AuditClosing, ClosingReviewRequest, ProjectMeta,
                                CompletionClaimRequest, CompletionReviewRequest, CreateAuditRequest, ProjectDetail)
from cairn.server.audit_budget import advance_budget, claim_closing, finish_closing, submit_closing, control_window
from cairn.server.audit_review import (
    apply_node_review, claim_completion, completion_by_id, finish_completion,
    list_completions, node_input, store_record,
)
from cairn.audit_evidence import json_digest
from cairn.server.services import (
    audit_metadata, build_intents, fact_to_model, get_project_or_404, json_text,
    next_fact_id, next_hint_id, next_intent_id, next_project_id, next_review_id, project_meta_from_row,
    save_audit_metadata, utcnow,
)

router = APIRouter(tags=["audits"])


def _absolute_path(value: str) -> Path:
    try:
        if "\x00" in value:
            raise ValueError("NUL in path")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("path is not absolute")
        return path.resolve()
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(422, "Audit paths must be valid absolute paths") from exc


def _audit_or_404(conn, project_id: str) -> AuditMetadata:
    get_project_or_404(conn, project_id)
    metadata = audit_metadata(conn, project_id)
    if metadata is None:
        raise HTTPException(404, "Project is not an audit")
    return metadata


def _seed_order(body: CreateAuditRequest) -> list[str]:
    seeds = {seed.key: seed for seed in body.seeds}
    if len(seeds) != len(body.seeds) or {"origin", "goal"} & seeds.keys():
        raise HTTPException(400, "Seed keys must be unique and cannot be origin or goal")
    allowed = {"origin", *seeds}
    for sources in [*(seed.audit.premise_ids for seed in body.seeds), *(intent.from_ for intent in body.intents)]:
        if not set(sources) <= allowed:
            raise HTTPException(400, "Unknown seed reference or goal used as a source")
    try:
        return list(TopologicalSorter({key: [ref for ref in seed.audit.premise_ids if ref != "origin"] for key, seed in seeds.items()}).static_order())
    except CycleError as exc:
        raise HTTPException(400, "Seed premises must not contain cycles") from exc


def _insert_intent(conn, project_id: str, sources: list[str], description: str, creator: str, now: str, target: str | None = None):
    intent_id = next_intent_id(conn, project_id)
    conn.execute(
        "INSERT INTO intents (id, project_id, description, creator, worker, to_fact_id, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (intent_id, project_id, description, creator, creator if target else None, target, now, now if target else None),
    )
    conn.executemany(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
        [(intent_id, project_id, source) for source in sources],
    )


@router.post("/audits", response_model=ProjectDetail, status_code=201)
def create_audit(body: CreateAuditRequest, request: Request,
                 x_cairn_submission_token: str | None = Header(default=None)):
    order = _seed_order(body)
    repo = _absolute_path(body.repo_path)
    workspace_root = _absolute_path(body.workspace_root)
    if not repo.is_dir() or not os.access(repo, os.R_OK | os.X_OK):
        raise HTTPException(422, "Audit repository must be a readable directory")
    policy_digest = hashlib.sha256(json_text(body.policy).encode()).hexdigest()
    json_text(body.baseline)
    seeds = {seed.key: seed for seed in body.seeds}
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = None
        if getattr(request.app.state, "managed_service", None) is not None:
            from cairn.server.submissions import creation_owner
            owner = creation_owner(conn, x_cairn_submission_token)
            if owner["project_id"]:
                project_id = owner["project_id"]
                project = get_project_or_404(conn, project_id)
                facts = conn.execute("SELECT * FROM facts WHERE project_id=? ORDER BY rowid", (project_id,)).fetchall()
                hints = conn.execute("SELECT * FROM hints WHERE project_id=? ORDER BY rowid", (project_id,)).fetchall()
                return ProjectDetail(project=project_meta_from_row(project, conn),
                    facts=[fact_to_model(conn, row, project_id) for row in facts],
                    intents=build_intents(conn, project_id), hints=[dict(row) for row in hints])
        project_id = next_project_id(conn)
        now = utcnow()
        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at, mode) VALUES (?, ?, 'stopped', 0, ?, 'audit')",
            (project_id, body.title, now),
        )
        conn.executemany("INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)", [
            ("origin", project_id, body.origin), ("goal", project_id, body.goal),
        ])
        seed_ids = {key: next_fact_id(conn, project_id) for key in order}
        mapping = {"origin": "origin", **seed_ids}
        for key in order:
            seed = seeds[key]
            fact_id = seed_ids[key]
            data = seed.audit.model_copy(update={"premise_ids": [mapping[ref] for ref in seed.audit.premise_ids]})
            conn.execute("INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)", (fact_id, project_id, seed.description))
            conn.execute("INSERT INTO audit_facts VALUES (?, ?, ?)", (project_id, fact_id, json_text(data.model_dump(mode="json"))))
            _insert_intent(conn, project_id, ["origin"], "seed_import", body.creator, now, fact_id)
        for intent in body.intents:
            _insert_intent(conn, project_id, [mapping[ref] for ref in intent.from_], intent.description, body.creator, now)
        for hint in body.hints:
            hint_id = next_hint_id(conn, project_id)
            conn.execute("INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)", (hint_id, project_id, hint.content, hint.creator, now))
        metadata = AuditMetadata(
            project_id=project_id, repo_path=str(repo), workspace_root=str(workspace_root),
            baseline=body.baseline, user_goal=body.user_goal, policy=body.policy,
            policy_digest=policy_digest, budget=body.budget, seed_ids=seed_ids,
        )
        conn.execute("INSERT INTO audits VALUES (?, ?)", (project_id, json_text(metadata.model_dump(mode="json"))))
        if owner is not None:
            conn.execute("UPDATE audit_submissions SET project_id=?,updated_at=? WHERE id=?", (project_id, now, owner["id"]))
            if getattr(request.app.state.managed_service, "runtime_enabled", False):
                from cairn.server.runtime import initialize
                initialize(conn, project_id)
        project = get_project_or_404(conn, project_id)
        facts = conn.execute("SELECT * FROM facts WHERE project_id = ? ORDER BY rowid", (project_id,)).fetchall()
        hints = conn.execute("SELECT * FROM hints WHERE project_id = ? ORDER BY rowid", (project_id,)).fetchall()
        return ProjectDetail(project=project_meta_from_row(project, conn),
                             facts=[fact_to_model(conn, row, project_id) for row in facts],
                             intents=build_intents(conn, project_id), hints=[dict(row) for row in hints])


@router.post("/projects/{project_id}/audit/start", response_model=AuditMetadata)
def start_audit(project_id: str, request: Request,
                x_cairn_submission_token: str | None = Header(default=None)):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        metadata = _audit_or_404(conn, project_id)
        if getattr(request.app.state, "managed_service", None) is not None:
            from cairn.server.submissions import creation_owner
            if creation_owner(conn, x_cairn_submission_token)["project_id"] != project_id:
                raise HTTPException(403, "Submission does not own this project activation")
        project = get_project_or_404(conn, project_id)
        if metadata.started_at is not None:
            if project["status"] != "active":
                raise HTTPException(409, "Audit has already started; use project status to resume")
            return metadata
        if project["status"] != "stopped" or metadata.phase != "preparing":
            raise HTTPException(409, "Audit is not preparing")
        workspace = Path(metadata.workspace_root) / project_id
        try:
            ready = workspace.is_dir() and os.access(workspace, os.R_OK | os.W_OK | os.X_OK)
            repo = (workspace / "repo").resolve(strict=True)
            context = json.loads((workspace / "input.json").read_text(encoding="utf-8"))
            ready = ready and repo.is_dir() and repo == Path(metadata.repo_path)
            ready = ready and isinstance(context, dict) and all(context.get(key) == value for key, value in {
                "project_id": project_id, "repo_path": metadata.repo_path, "policy_digest": metadata.policy_digest,
            }.items())
        except (OSError, ValueError, RuntimeError):
            ready = False
        if not ready:
            raise HTTPException(409, "Workspace requires a matching repo reference and input.json")
        now = utcnow()
        started = datetime.fromisoformat(now.replace("Z", "+00:00"))
        try:
            research = started + timedelta(seconds=metadata.budget.research_seconds)
            drain = research + timedelta(seconds=metadata.budget.drain_seconds)
            final = drain + timedelta(seconds=metadata.budget.final_review_seconds)
        except OverflowError as exc:
            raise HTTPException(422, "Audit budget exceeds the supported datetime range") from exc
        metadata.phase = "researching"
        metadata.workspace_path = str(workspace)
        metadata.started_at = now
        metadata.research_deadline = research.strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata.drain_deadline = drain.strftime("%Y-%m-%dT%H:%M:%SZ")
        metadata.final_deadline = final.strftime("%Y-%m-%dT%H:%M:%SZ")
        save_audit_metadata(conn, metadata)
        conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (project_id,))
        from cairn.server.runtime import activate
        activate(conn, project_id)
        return metadata


@router.get("/projects/{project_id}/audit", response_model=AuditDetail)
def get_audit(project_id: str):
    with get_conn() as conn:
        metadata = _audit_or_404(conn, project_id)
        records = conn.execute("SELECT record_json FROM audit_records WHERE project_id = ? ORDER BY rowid", (project_id,)).fetchall()
        return AuditDetail(**metadata.model_dump(), records=[AuditRecord.model_validate_json(row["record_json"]) for row in records])


@router.post("/projects/{project_id}/audit/reviews", response_model=AuditRecord, status_code=201)
def record_review(project_id: str, body: AuditRecordRequest, response: Response):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _audit_or_404(conn, project_id)
        if body.object_type == "project":
            exists = body.object_id == project_id
        else:
            table = "facts" if body.object_type == "fact" else "intents"
            exists = conn.execute(f"SELECT 1 FROM {table} WHERE project_id = ? AND id = ?", (project_id, body.object_id)).fetchone() is not None
        if not exists:
            raise HTTPException(404, "Review target not found in this audit")
        record, created = store_record(conn, project_id, body)
        if not created:
            response.status_code = 200
            return record
        if body.phase in ("final_review", "closing"):
            raise HTTPException(400, "Final review and closing must use their dedicated endpoints")
        metadata = _audit_or_404(conn, project_id)
        if body.phase == "overseer":
            control_window(conn, metadata)
        apply_node_review(conn, metadata, body)
        if body.phase == "overseer" and body.attempt_status == "success":
            record.output_digest = json_digest(node_input(conn, metadata, body.object_type, body.object_id))
            conn.execute("UPDATE audit_records SET record_json=? WHERE project_id=? AND id=?",
                         (json_text(record.model_dump(mode="json")), project_id, record.id))
        return record


@router.get("/projects/{project_id}/audit/nodes/{object_type}/{object_id}")
def review_input(project_id: str, object_type: str, object_id: str):
    with get_conn() as conn:
        metadata = _audit_or_404(conn, project_id)
        context = node_input(conn, metadata, object_type, object_id)
        return {"input_digest": json_digest(context), "context": context}


@router.get("/projects/{project_id}/audit/completions", response_model=list[AuditCompletion])
def completions(project_id: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return list_completions(conn, _audit_or_404(conn, project_id))


@router.post("/projects/{project_id}/audit/completions/{request_id}/claim", response_model=AuditCompletion)
def claim_review(project_id: str, request_id: str, body: CompletionClaimRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return claim_completion(conn, _audit_or_404(conn, project_id), completion_by_id(conn, project_id, request_id), body)


@router.post("/projects/{project_id}/audit/completions/{request_id}/review", response_model=AuditCompletion)
def completion_review(project_id: str, request_id: str, body: CompletionReviewRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return finish_completion(conn, _audit_or_404(conn, project_id), completion_by_id(conn, project_id, request_id), body)


@router.post("/projects/{project_id}/audit/budget", response_model=ProjectMeta)
def audit_budget(project_id: str, body: AuditBudgetRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        advance_budget(conn, project_id, finalize=body.finalize)
        return project_meta_from_row(get_project_or_404(conn, project_id), conn)


@router.post("/projects/{project_id}/audit/closing/claim", response_model=AuditClosing)
def closing_claim(project_id: str, body: CompletionClaimRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return claim_closing(conn, _audit_or_404(conn, project_id), body)


@router.post("/projects/{project_id}/audit/closing", response_model=AuditClosing)
def closing_review(project_id: str, body: ClosingReviewRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return finish_closing(conn, _audit_or_404(conn, project_id), body)


@router.post("/projects/{project_id}/audit/closing/submit", response_model=AuditCompletion)
def closing_submit(project_id: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return submit_closing(conn, _audit_or_404(conn, project_id))
