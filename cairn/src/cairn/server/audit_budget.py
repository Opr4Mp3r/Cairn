from __future__ import annotations

import json
import uuid

from fastapi import HTTPException

from cairn.audit_evidence import json_digest
from cairn.server import services, runtime
from cairn.server.models import AuditLease, ClosingResult


def live_leases(conn, project_id: str) -> set[str]:
    services.expire_workers(conn, project_id)
    services.expire_reason_leases(conn, project_id)
    metadata = services.audit_metadata(conn, project_id)
    scopes = {f"intent:{row['id']}" for row in conn.execute(
        "SELECT id FROM intents WHERE project_id=? AND worker IS NOT NULL AND to_fact_id IS NULL", (project_id,)
    )}
    if services.get_project_or_404(conn, project_id)["reason_worker"] is not None:
        scopes.add("reason")
    return {metadata.leases[scope].id if scope in metadata.leases else f"legacy:{scope}" for scope in scopes}


def closing_input(metadata) -> str:
    return json_digest({"revision": metadata.audit_revision, "policy": metadata.policy_digest, "baseline": metadata.baseline})


def closing_current(metadata) -> bool:
    return metadata.closing.status == "success" and metadata.closing.input_digest == closing_input(metadata)


def facts_reviewed(conn, metadata) -> bool:
    from cairn.server.audit_review import node_input

    records = [json.loads(row["record_json"]) for row in conn.execute(
        "SELECT record_json FROM audit_records WHERE project_id=? AND phase='overseer' AND object_type='fact'",
        (metadata.project_id,),
    )]
    for row in conn.execute("SELECT fact_id FROM audit_facts WHERE project_id=?", (metadata.project_id,)):
        digest = json_digest(node_input(conn, metadata, "fact", row["fact_id"]))
        if not any(record["object_id"] == row["fact_id"] and record["attempt_status"] == "success"
                   and digest in (record["input_digest"], record.get("output_digest")) for record in records):
            return False
    return True


def advance_budget(conn, project_id: str, *, finalize: bool = False):
    metadata = services.audit_metadata(conn, project_id)
    row = services.get_project_or_404(conn, project_id)
    if metadata is None:
        raise HTTPException(404, "Project is not an audit")
    if row["status"] not in ("active", "draining") or not metadata.started_at:
        return metadata
    clock = runtime.BudgetClock(conn, metadata)
    now = clock.now
    closing = metadata.closing
    if closing.status == "success" and not closing_current(metadata):
        closing.status = "stale"
        closing.error = "有效知识已变化，旧 closing 不适用于当前快照"
    if closing.token and clock.deadline(closing, "attempt_deadline") is not None and clock.due(closing, "attempt_deadline") and closing.status != "success":
        closing.status = "error"
        closing.error = "结案调用或回执超过本次期限，未产生有效结果"
        closing.token = None
    if row["status"] == "active" and now >= clock.research_end:
        metadata.draining_lease_ids = sorted(live_leases(conn, project_id))
        metadata.phase = "draining"
        conn.execute("UPDATE projects SET status='draining' WHERE id=?", (project_id,))
    if metadata.phase == "draining" and (now >= clock.drain_end or not live_leases(conn, project_id)):
        conn.execute("UPDATE intents SET worker=NULL WHERE project_id=? AND to_fact_id IS NULL", (project_id,))
        services.clear_project_reason(conn, project_id)
        metadata.phase = "finalizing"
    if now >= clock.final_end:
        metadata.phase = "closed"
        metadata.termination_reason = "final_deadline"
        conn.execute("UPDATE projects SET status='budget_exhausted' WHERE id=?", (project_id,))
    elif finalize and metadata.phase == "finalizing":
        from cairn.server.audit_review import list_completions

        requests = [request.model_dump(mode="json") for request in list_completions(conn, metadata)]
        closing = metadata.closing
        submitted = closing.result is not None and (
            closing.result.kind == "no_candidate" or any(request["id"] == closing.request_id for request in requests)
        )
        settled = all(request["status"] in ("approved", "rejected", "needs_evidence") for request in requests)
        if closing_current(metadata) and submitted and settled and facts_reviewed(conn, metadata):
            metadata.phase = "closed"
            metadata.termination_reason = "research_budget_exhausted"
            conn.execute("UPDATE projects SET status='budget_exhausted' WHERE id=?", (project_id,))
    services.save_audit_metadata(conn, metadata)
    if metadata.phase == "closed":
        runtime.freeze(conn, project_id, "finished", metadata.termination_reason)
    return metadata


def new_research(conn, project_id: str) -> None:
    row = services.get_project_or_404(conn, project_id)
    if row["mode"] != "audit":
        services.check_project_active(conn, project_id)
        return
    metadata = services.audit_metadata(conn, project_id)
    runtime.require_running(conn, project_id)
    clock = runtime.BudgetClock(conn, metadata)
    if (row["status"] != "active" or metadata.phase != "researching"
            or clock.research_end is None or clock.now >= clock.research_end):
        raise HTTPException(403, "Audit no longer accepts new research")


def register_lease(conn, project_id: str, scope: str, worker: str) -> str | None:
    metadata = services.audit_metadata(conn, project_id)
    if metadata is None:
        return None
    lease = AuditLease(id=uuid.uuid4().hex, worker=worker)
    metadata.leases[scope] = lease
    services.save_audit_metadata(conn, metadata)
    return lease.id


def existing_research(conn, project_id: str, scope: str, worker: str, lease_id: str | None) -> None:
    row = services.get_project_or_404(conn, project_id)
    if row["mode"] != "audit":
        services.check_project_active(conn, project_id)
        return
    metadata = services.audit_metadata(conn, project_id)
    runtime.require_running(conn, project_id)
    clock = runtime.BudgetClock(conn, metadata)
    now = clock.now
    lease = metadata.leases.get(scope)
    if row["status"] not in ("active", "draining") or metadata.phase == "finalizing" or now >= clock.drain_end:
        raise HTTPException(403, "Audit research writeback window is closed")
    if scope == "reason":
        services.expire_reason_leases(conn, project_id)
        holder = services.get_project_or_404(conn, project_id)["reason_worker"]
    else:
        services.expire_workers(conn, project_id)
        holder = services.get_intent_or_404(conn, project_id, scope.split(":", 1)[1])["worker"]
    if holder != worker or (lease_id is not None and (lease is None or lease.id != lease_id)):
        raise HTTPException(409, "Research lease is no longer held by this attempt")
    # ponytail: retain pre-CP5 callers during research; draining always requires the frozen credential.
    if now >= clock.research_end or row["status"] == "draining":
        metadata = advance_budget(conn, project_id)
        if lease is None or lease_id != lease.id or lease.id not in metadata.draining_lease_ids or metadata.phase != "draining":
            raise HTTPException(409, "Only admitted in-flight leases may finish during draining")


def control_window(conn, metadata) -> None:
    row = services.get_project_or_404(conn, metadata.project_id)
    runtime.require_running(conn, metadata.project_id)
    clock = runtime.BudgetClock(conn, metadata)
    if row["status"] not in ("active", "draining") or clock.final_end is None or clock.now >= clock.final_end:
        raise HTTPException(409, "Audit control window is closed")


def claim_closing(conn, metadata, body):
    metadata = advance_budget(conn, metadata.project_id)
    control_window(conn, metadata)
    if metadata.phase != "finalizing" or not facts_reviewed(conn, metadata):
        raise HTTPException(409, "Final source observations must be reviewed before closing")
    closing = metadata.closing
    clock = runtime.BudgetClock(conn, metadata)
    now = clock.now
    if closing.status == "success":
        return closing
    if closing.token and clock.deadline(closing, "attempt_deadline") is not None and now < clock.deadline(closing, "attempt_deadline"):
        raise HTTPException(409, "Closing attempt is still running")
    if clock.deadline(closing, "round_deadline") is None:
        closing.max_attempts = body.max_attempts
        try:
            clock.set_deadline(closing, "round_deadline", min(now + body.timeout_seconds * body.max_attempts, clock.final_end))
        except OverflowError as exc:
            raise HTTPException(422, "Closing time budget is out of range") from exc
    if closing.attempts >= closing.max_attempts or clock.due(closing, "round_deadline"):
        raise HTTPException(409, "Closing attempts exhausted")
    closing.status = "running"
    closing.attempts += 1
    closing.token = uuid.uuid4().hex
    closing.input_digest = closing_input(metadata)
    closing.result = None
    closing.session = closing.request_id = closing.error = None
    clock.set_deadline(closing, "attempt_deadline", min(now + body.timeout_seconds, clock.deadline(closing, "round_deadline")))
    services.save_audit_metadata(conn, metadata)
    return closing


def finish_closing(conn, metadata, body):
    from cairn.server.audit_review import store_record, premise_closure, facts_snapshot

    existing = conn.execute("SELECT record_json FROM audit_records WHERE project_id=? AND record_key=?",
                            (metadata.project_id, body.record.record_key)).fetchone()
    if existing:
        store_record(conn, metadata.project_id, body.record)
        return metadata.closing
    control_window(conn, metadata)
    closing = metadata.closing
    if (metadata.phase != "finalizing" or closing.token != body.token or body.record.object_id != metadata.project_id
            or body.input_digest != closing.input_digest or body.record.input_digest != closing.input_digest):
        raise HTTPException(409, "Closing receipt does not own this snapshot")
    success = body.record.attempt_status == "success"
    if success and (runtime.BudgetClock(conn, metadata).due(closing, "attempt_deadline") or closing_input(metadata) != closing.input_digest):
        raise HTTPException(409, "Closing snapshot or attempt deadline changed")
    if success and not body.record.session:
        raise HTTPException(400, "Closing requires its own session")
    if success:
        try:
            matched = ClosingResult.model_validate(body.record.result) == body.result
        except ValueError:
            matched = False
        if not matched:
            raise HTTPException(400, "Closing result must match its execution receipt")
    if success and body.result.kind == "candidate":
        premise_closure(facts_snapshot(conn, metadata.project_id), body.result.from_)
    store_record(conn, metadata.project_id, body.record)
    closing.status = "success" if success else "error"
    closing.result = body.result
    closing.session = body.record.session
    closing.error = body.record.error
    closing.token = None
    services.save_audit_metadata(conn, metadata)
    return closing


def submit_closing(conn, metadata):
    from cairn.server.audit_review import create_completion, completion_by_id
    from cairn.server.models import CompleteRequest

    closing = metadata.closing
    if not closing_current(metadata):
        raise HTTPException(409, "Closing candidate does not apply to the current snapshot")
    if closing.request_id:
        return completion_by_id(conn, metadata.project_id, closing.request_id)
    control_window(conn, metadata)
    if metadata.phase != "finalizing" or closing.status != "success" or closing.result is None or closing.result.kind != "candidate":
        raise HTTPException(409, "No successful closing candidate to submit")
    request = create_completion(conn, metadata, CompleteRequest(
        from_=closing.result.from_, description=closing.result.description,
        worker="audit-controller.closing", source_session=closing.session,
    ), closing=True)
    closing.request_id = request.id
    services.save_audit_metadata(conn, metadata)
    return request
