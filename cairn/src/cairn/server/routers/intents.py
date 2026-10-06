from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.audit_budget import existing_research, register_lease
from cairn.server.models import (
    AuditFactData,
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    HeartbeatRequest,
    Intent,
)
from cairn.server.services import (
    check_project_active,
    check_new_research_allowed,
    get_project_or_404,
    bump_audit_revision,
    fact_to_model,
    get_claimable_open_intent_or_404,
    get_releasable_open_intent_or_404,
    intent_to_model,
    next_fact_id,
    next_intent_id,
    json_text,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        if get_project_or_404(conn, project_id)["mode"] == "audit":
            conn.execute("BEGIN IMMEDIATE")
        check_project_active(conn, project_id)
        check_new_research_allowed(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )
        bump_audit_revision(conn, project_id, reason=False)
        lease_id = register_lease(conn, project_id, f"intent:{iid}", body.worker) if claimed else None

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
            lease_id=lease_id,
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        if get_project_or_404(conn, project_id)["mode"] == "audit":
            conn.execute("BEGIN IMMEDIATE")
        else:
            check_project_active(conn, project_id)
        row = get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)
        if row["worker"] is None:
            if body.lease_id is not None:
                raise HTTPException(409, "Expired lease cannot be reclaimed by its old attempt")
            check_new_research_allowed(conn, project_id)
            register_lease(conn, project_id, f"intent:{intent_id}", body.worker)
        else:
            existing_research(conn, project_id, f"intent:{intent_id}", body.worker, body.lease_id)

        now = utcnow()
        conn.execute(
            "UPDATE intents SET worker = ?, last_heartbeat_at = ? WHERE id = ? AND project_id = ?",
            (body.worker, now, intent_id, project_id),
        )

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        return intent_to_model(conn, updated, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        if get_project_or_404(conn, project_id)["mode"] == "audit":
            conn.execute("BEGIN IMMEDIATE")
        row = get_releasable_open_intent_or_404(conn, project_id, intent_id, body.worker)
        if row["worker"] is not None:
            existing_research(conn, project_id, f"intent:{intent_id}", body.worker, body.lease_id)
        elif get_project_or_404(conn, project_id)["status"] not in ("active", "draining"):
            check_project_active(conn, project_id)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            )
            row = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        if get_project_or_404(conn, project_id)["mode"] == "audit":
            conn.execute("BEGIN IMMEDIATE")
        else:
            check_project_active(conn, project_id)
        project = get_project_or_404(conn, project_id)
        claimed = get_claimable_open_intent_or_404(conn, project_id, intent_id, body.worker)
        if claimed["worker"] is None:
            if body.lease_id is not None:
                raise HTTPException(409, "Expired lease cannot write a result")
            check_new_research_allowed(conn, project_id)
        else:
            existing_research(conn, project_id, f"intent:{intent_id}", body.worker, body.lease_id)

        now = utcnow()
        fid = next_fact_id(conn, project_id)

        audit = None
        if project["mode"] == "audit":
            audit = body.audit or AuditFactData()
            if audit.review_status != "unverified":
                raise HTTPException(400, "Research facts must start unverified")
            validate_facts_exist(conn, project_id, audit.premise_ids)
            validate_goal_not_in_sources(audit.premise_ids)

        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fid, project_id, body.description),
        )
        if audit is not None:
            conn.execute("INSERT INTO audit_facts VALUES (?, ?, ?)", (project_id, fid, json_text(audit.model_dump(mode="json"))))
        conn.execute(
            "UPDATE intents SET to_fact_id = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ? WHERE id = ? AND project_id = ?",
            (fid, body.worker, now, now, intent_id, project_id),
        )
        bump_audit_revision(conn, project_id, reason=True)

        updated = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()

        fact = conn.execute("SELECT * FROM facts WHERE project_id = ? AND id = ?", (project_id, fid)).fetchone()
        return ConcludeResponse(
            fact=fact_to_model(conn, fact, project_id, is_audit=project["mode"] == "audit"),
            intent=intent_to_model(conn, updated, project_id),
        )
