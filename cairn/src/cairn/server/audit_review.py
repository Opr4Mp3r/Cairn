from __future__ import annotations

from datetime import datetime, timedelta, timezone
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
import uuid

from fastapi import HTTPException

from cairn.audit_evidence import json_digest, repository_baseline, source_evidence
from cairn.server.models import (
    AuditCompletion, AuditFactData, AuditMetadata, AuditRecord, AuditRecordRequest,
    CompleteRequest, CompletionClaimRequest, CompletionReviewRequest,
)
from cairn.server.services import (
    audit_metadata, bump_audit_revision, check_project_active, clear_project_reason,
    expire_reason_leases, expire_workers, get_project_or_404, json_text,
    next_hint_id, next_intent_id, next_review_id, utcnow,
)
from cairn.server.audit_budget import control_window, existing_research


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def facts_snapshot(conn, project_id: str) -> dict:
    rows = conn.execute(
        "SELECT f.id, f.description, a.metadata_json FROM facts f LEFT JOIN audit_facts a "
        "ON f.project_id=a.project_id AND f.id=a.fact_id WHERE f.project_id=?", (project_id,),
    ).fetchall()
    return {row["id"]: {"id": row["id"], "description": row["description"],
                        "audit": AuditFactData.model_validate_json(row["metadata_json"]) if row["metadata_json"] else None}
            for row in rows}


def premise_closure(facts: dict, sources: list[str]) -> dict:
    result, visiting = {}, set()

    def visit(fact_id):
        if fact_id == "origin":
            return
        if fact_id == "goal" or fact_id not in facts:
            raise HTTPException(400, "Unknown or goal proof premise")
        if fact_id in visiting:
            raise HTTPException(400, "Proof premises contain a cycle")
        if fact_id in result:
            return
        visiting.add(fact_id)
        fact = facts[fact_id]
        if fact["audit"] is None:
            raise HTTPException(400, "Proof requires audit fact metadata")
        for premise in fact["audit"].premise_ids:
            visit(premise)
        visiting.remove(fact_id)
        result[fact_id] = fact

    for source in sources:
        visit(source)
    return result


def proof_digest(conn, metadata: AuditMetadata, sources: list[str]) -> str:
    closure = premise_closure(facts_snapshot(conn, metadata.project_id), sources)
    return json_digest({
        "policy": metadata.policy_digest, "baseline": metadata.baseline,
        "sources": sorted(sources),
        "facts": {key: {"description": fact["description"],
                        "audit": fact["audit"].model_dump(mode="json", exclude={"review_notes"})}
                  for key, fact in sorted(closure.items())},
    })


def node_input(conn, metadata: AuditMetadata, object_type: str, object_id: str) -> dict:
    facts = facts_snapshot(conn, metadata.project_id)
    if object_type == "fact":
        if object_id not in facts or object_id in ("origin", "goal"):
            raise HTTPException(400, "Only ordinary audit facts can be assessed")
        fact = facts[object_id]
        data = fact["audit"] or AuditFactData()
        target = {"id": object_id, "description": fact["description"],
                  "audit": data.model_dump(mode="json", exclude={"review_status", "review_notes", "validation_level"})}
        sources = data.premise_ids
    elif object_type == "intent":
        row = conn.execute("SELECT id, description, to_fact_id FROM intents WHERE project_id=? AND id=?",
                           (metadata.project_id, object_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Intent not found")
        sources = [row["fact_id"] for row in conn.execute(
            "SELECT fact_id FROM intent_sources WHERE project_id=? AND intent_id=? ORDER BY fact_id",
            (metadata.project_id, object_id),
        )]
        target = dict(row)
    else:
        raise HTTPException(400, "Overseer requires a fact or intent")
    closure = premise_closure(facts, sources)
    return {"target": target, "policy": metadata.policy, "policy_digest": metadata.policy_digest,
            "baseline": metadata.baseline,
            "premises": {key: {"description": fact["description"], "audit": fact["audit"].model_dump(mode="json")}
                         for key, fact in sorted(closure.items())}}


def validate_fact_evidence(metadata: AuditMetadata, data: AuditFactData) -> AuditFactData:
    try:
        checked = source_evidence(Path(metadata.repo_path), data.evidence)
        files = metadata.baseline.get("files", {})
        if not isinstance(files, dict) or not checked or any(files.get(ref.path) != ref.sha256 for ref in checked):
            raise ValueError("evidence is missing or outside the fixed source baseline")
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(409, f"Audit evidence cannot be confirmed: {exc}") from exc
    return data.model_copy(update={"evidence": checked})


def store_record(conn, project_id: str, body: AuditRecordRequest) -> tuple[AuditRecord, bool]:
    existing = conn.execute("SELECT record_json FROM audit_records WHERE project_id=? AND record_key=?",
                            (project_id, body.record_key)).fetchone()
    if existing:
        record = AuditRecord.model_validate_json(existing["record_json"])
        if json_text(record.model_dump(mode="json", exclude={"id", "created_at", "output_digest"})) != json_text(body.model_dump(mode="json")):
            raise HTTPException(409, "Review record_key was used for different content")
        return record, False
    record = AuditRecord(**body.model_dump(), id=next_review_id(conn, project_id), created_at=utcnow())
    conn.execute("INSERT INTO audit_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
        record.id, project_id, record.record_key, record.object_type, record.object_id, record.phase,
        record.input_digest, json_text(record.model_dump(mode="json")),
    ))
    return record, True


def apply_node_review(conn, metadata: AuditMetadata, body: AuditRecordRequest) -> None:
    if body.phase != "overseer":
        if body.fact_audit is not None or body.hint is not None:
            raise HTTPException(400, "Only Overseer records may apply fact or Hint changes")
        return
    if body.input_digest != json_digest(node_input(conn, metadata, body.object_type, body.object_id)):
        raise HTTPException(409, "Review input changed; reread the node")
    if body.attempt_status == "error":
        return
    if body.decision is None or not body.session:
        raise HTTPException(400, "Overseer result requires an independent session and decision")
    changed, reason_changed = False, False
    if body.object_type == "fact":
        if body.fact_audit is None:
            raise HTTPException(400, "Fact assessment requires audit metadata")
        data = body.fact_audit.model_copy(deep=True)
        expected = {"approved": "verified", "rejected": "refuted", "needs_evidence": "unverified"}[body.decision]
        if data.review_status != expected:
            raise HTTPException(400, "Fact state must match the assessment")
        facts = facts_snapshot(conn, metadata.project_id)
        old = facts[body.object_id]["audit"] or AuditFactData()
        if not set(old.conditions) <= set(data.conditions) or not set(old.premise_ids) <= set(data.premise_ids):
            raise HTTPException(409, "Existing conditions and logical premises cannot disappear during review; record a new claim instead")
        if data.review_status != "unverified":
            data = validate_fact_evidence(metadata, data)
        if any(ref == "goal" or ref not in facts for ref in data.premise_ids):
            raise HTTPException(400, "Unknown or goal logical premise")
        graph = {key: fact["audit"].premise_ids for key, fact in facts.items() if fact["audit"]}
        graph[body.object_id] = data.premise_ids
        try:
            list(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise HTTPException(400, "Fact assessment introduces a premise cycle") from exc
        closure = premise_closure(facts, data.premise_ids)
        if data.review_status == "verified" and any(fact["audit"].review_status != "verified" for fact in closure.values()):
            raise HTTPException(409, "Verified claim cannot inherit an unsupported premise")
        changed = old != data
        reason_changed = old.model_dump(exclude={"review_notes"}) != data.model_dump(exclude={"review_notes"})
        if changed:
            conn.execute("UPDATE audit_facts SET metadata_json=? WHERE project_id=? AND fact_id=?",
                         (json_text(data.model_dump(mode="json")), metadata.project_id, body.object_id))
        if reason_changed:
            affected = {body.object_id}
            for _ in facts:
                descendants = {key for key, fact in facts.items() if fact["audit"] and set(fact["audit"].premise_ids) & affected}
                if descendants <= affected:
                    break
                affected.update(descendants)
            for key in affected - {body.object_id}:
                dependent = facts[key]["audit"]
                if dependent and dependent.review_status == "verified":
                    dependent = dependent.model_copy(update={"review_status": "unverified", "review_notes": "必要前提已变化，须重新复核"})
                    conn.execute("UPDATE audit_facts SET metadata_json=? WHERE project_id=? AND fact_id=?",
                                 (json_text(dependent.model_dump(mode="json")), metadata.project_id, key))
    elif body.fact_audit is not None:
        raise HTTPException(400, "Intent review cannot change a fact")
    if body.hint:
        if not body.hint.strip():
            raise HTTPException(400, "Hint must not be blank")
        conn.execute("INSERT INTO hints VALUES (?, ?, ?, ?, ?)",
                     (next_hint_id(conn, metadata.project_id), metadata.project_id, body.hint, "overseer", utcnow()))
        changed = reason_changed = True
    if changed:
        bump_audit_revision(conn, metadata.project_id, reason=reason_changed)


def _save_completion(conn, request: AuditCompletion) -> None:
    conn.execute("UPDATE audit_completions SET data_json=? WHERE project_id=? AND id=?",
                 (json_text(request.model_dump(mode="json")), request.project_id, request.id))


def completion_by_id(conn, project_id: str, request_id: str) -> AuditCompletion:
    row = conn.execute("SELECT data_json FROM audit_completions WHERE project_id=? AND id=?",
                       (project_id, request_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Completion request not found")
    return AuditCompletion.model_validate_json(row["data_json"])


def _refresh_completion(conn, metadata: AuditMetadata, request: AuditCompletion) -> AuditCompletion:
    if request.status == "approved":
        return request
    current = proof_digest(conn, metadata, request.from_)
    if current != request.proof_digest:
        request.proof_digest = current
        request.status = "stale"
        request.decision = request.error = None
        request.review_token = request.review_owner = None
        request.round_deadline = request.attempt_deadline = None
        request.attempts = 0
    elif request.status == "pending" and request.audit_revision != metadata.audit_revision:
        request.status = "stale"
    if request.review_token and request.attempt_deadline and utcnow() >= request.attempt_deadline:
        request.status = "review_error"
        request.error = "审核调用或回执超过本次期限，未产生证据判断"
        request.review_token = request.review_owner = None
    request.updated_at = utcnow()
    _save_completion(conn, request)
    return request


def list_completions(conn, metadata: AuditMetadata) -> list[AuditCompletion]:
    rows = conn.execute("SELECT data_json FROM audit_completions WHERE project_id=? ORDER BY rowid",
                        (metadata.project_id,)).fetchall()
    return [_refresh_completion(conn, metadata, AuditCompletion.model_validate_json(row["data_json"])) for row in rows]


def create_completion(conn, metadata: AuditMetadata, body: CompleteRequest, *, closing=False) -> AuditCompletion:
    control_window(conn, metadata)
    row = get_project_or_404(conn, metadata.project_id)
    if not closing and (row["status"] == "draining" or utcnow() >= metadata.research_deadline):
        if metadata.phase == "finalizing":
            raise HTTPException(403, "Only persisted closing candidates may submit after research is closed")
        scope = next((scope for scope, lease in metadata.leases.items()
                      if lease.id == body.lease_id and lease.worker == body.worker), "reason")
        if scope.startswith("intent:"):
            intent = conn.execute("SELECT to_fact_id FROM intents WHERE project_id=? AND id=?",
                                  (metadata.project_id, scope.split(":", 1)[1])).fetchone()
            if (intent is None or intent["to_fact_id"] not in body.from_ or body.lease_id not in metadata.draining_lease_ids
                    or utcnow() >= metadata.drain_deadline):
                raise HTTPException(409, "Intent completion must use its admitted result")
        else:
            existing_research(conn, metadata.project_id, scope, body.worker, body.lease_id)
    if len(body.from_) != len(set(body.from_)) or set(body.from_) & {"origin", "goal"}:
        raise HTTPException(400, "Completion requires unique ordinary proof facts")
    digest = proof_digest(conn, metadata, body.from_)
    source_key = json_digest({"policy": metadata.policy_digest, "baseline": metadata.baseline,
                              "facts": sorted(body.from_)})
    existing = conn.execute("SELECT data_json FROM audit_completions WHERE project_id=? AND source_key=?",
                            (metadata.project_id, source_key)).fetchone()
    if existing:
        return _refresh_completion(conn, metadata, AuditCompletion.model_validate_json(existing["data_json"]))
    now = utcnow()
    request = AuditCompletion(
        id="c" + uuid.uuid4().hex, project_id=metadata.project_id, from_=body.from_,
        description=body.description, worker=body.worker, source_session=body.source_session,
        proof_digest=digest, policy_digest=metadata.policy_digest, baseline_digest=json_digest(metadata.baseline),
        audit_revision=metadata.audit_revision, created_at=now, updated_at=now,
    )
    conn.execute("INSERT INTO audit_completions VALUES (?, ?, ?, ?)",
                 (request.id, metadata.project_id, source_key, json_text(request.model_dump(mode="json"))))
    return request


def claim_completion(conn, metadata: AuditMetadata, request: AuditCompletion, body: CompletionClaimRequest) -> AuditCompletion:
    control_window(conn, metadata)
    request = _refresh_completion(conn, metadata, request)
    now = _datetime(utcnow())
    if metadata.final_deadline is None or now >= _datetime(metadata.final_deadline):
        raise HTTPException(409, "Final review deadline exhausted")
    if request.status in ("approved", "rejected", "needs_evidence"):
        raise HTTPException(409, "No new proof evidence to review")
    if request.review_token is not None:
        raise HTTPException(409, "Review attempt is already running")
    if body.retry and request.status not in ("review_error", "stale"):
        raise HTTPException(409, "Explicit retry only applies to execution errors or stale reviews")
    if request.round_deadline is None or body.retry:
        request.max_attempts = body.max_attempts
        request.attempts = 0
        try:
            deadline = min(now + timedelta(seconds=body.timeout_seconds * body.max_attempts), _datetime(metadata.final_deadline))
        except OverflowError as exc:
            raise HTTPException(422, "Review time budget is out of range") from exc
        request.round_deadline = _timestamp(deadline)
    if request.attempts >= request.max_attempts or now >= _datetime(request.round_deadline):
        raise HTTPException(409, "Review round exhausted; an explicit retry is required")
    request.attempts += 1
    request.status = "pending"
    request.review_token = uuid.uuid4().hex
    request.review_owner = body.owner
    request.attempt_deadline = _timestamp(min(now + timedelta(seconds=body.timeout_seconds), _datetime(request.round_deadline)))
    request.audit_revision = metadata.audit_revision
    request.updated_at = utcnow()
    _save_completion(conn, request)
    return request


def _cvss_matches(vector: dict, constraints: dict) -> bool:
    if not isinstance(constraints, dict):
        raise HTTPException(400, "CVSS constraints must be a metric object")
    for key, value in constraints.items():
        if key in ("any_of", "all_of"):
            if not isinstance(value, list) or not value or any(not isinstance(item, dict) or not item for item in value):
                raise HTTPException(400, "CVSS logic requires a non-empty list of metric constraints")
            results = [_cvss_matches(vector, item) for item in value]
            if not (any(results) if key == "any_of" else all(results)):
                return False
        elif key not in vector or not isinstance(value, str):
            raise HTTPException(400, f"Unsupported CVSS constraint: {key}")
        elif vector[key] != value:
            return False
    return True


def finish_completion(conn, metadata: AuditMetadata, request: AuditCompletion, body: CompletionReviewRequest) -> AuditCompletion:
    existing = conn.execute("SELECT record_json FROM audit_records WHERE project_id=? AND record_key=?",
                            (metadata.project_id, body.record.record_key)).fetchone()
    if existing:
        if body.record.input_digest != request.proof_digest or body.record.object_id != metadata.project_id:
            raise HTTPException(409, "Receipt belongs to a different proof")
        store_record(conn, metadata.project_id, body.record)
        return completion_by_id(conn, metadata.project_id, request.id)
    control_window(conn, metadata)
    if request.review_token != body.token or body.record.object_id != metadata.project_id:
        raise HTTPException(409, "Review attempt no longer owns this request")
    if not metadata.final_deadline or utcnow() >= metadata.final_deadline:
        raise HTTPException(409, "Final review deadline exhausted")
    if body.record.attempt_status == "success" and request.attempt_deadline and utcnow() >= request.attempt_deadline:
        raise HTTPException(409, "Review attempt deadline exhausted")
    if body.record.input_digest != request.proof_digest:
        raise HTTPException(400, "Review record is not bound to this proof")
    if body.record.attempt_status == "success" and (not body.record.session or body.record.session == request.source_session):
        raise HTTPException(400, "Final review requires a distinct independent session")
    if (body.record.attempt_status == "success" and metadata.closing.request_id == request.id
            and body.record.session == metadata.closing.session):
        raise HTTPException(400, "Final review cannot reuse the candidate closing session")
    stale = (body.expected_audit_revision != metadata.audit_revision or
             body.policy_digest != metadata.policy_digest or body.proof_digest != request.proof_digest or
             proof_digest(conn, metadata, request.from_) != request.proof_digest)
    if body.record.attempt_status == "success" and body.assessment.decision == "approved" and not stale:
        if json_digest(metadata.policy) != metadata.policy_digest:
            raise HTTPException(409, "Fixed policy content no longer matches its digest")
        if set(body.assessment.proof_ids) != set(request.from_):
            raise HTTPException(400, "Approval must assess the requested proof facts")
        constraints = metadata.policy.get("cvss", {})
        if constraints and body.assessment.cvss_vector is None:
            raise HTTPException(409, "A CVSS-constrained goal requires a complete CVSS 3.1 vector")
        if not _cvss_matches(body.assessment.cvss_vector.model_dump() if body.assessment.cvss_vector else {}, constraints):
            raise HTTPException(409, "CVSS vector does not meet the fixed policy")
        try:
            if repository_baseline(Path(metadata.repo_path), Path(metadata.workspace_root))["files"] != metadata.baseline.get("files"):
                raise ValueError("source baseline changed")
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, f"Baseline cannot support approval: {exc}") from exc
        closure = premise_closure(facts_snapshot(conn, metadata.project_id), request.from_)
        if any(fact["audit"].review_status != "verified" for fact in closure.values()):
            raise HTTPException(409, "Proof depends on an unsupported premise")
        for fact in closure.values():
            validate_fact_evidence(metadata, fact["audit"])
        control_window(conn, metadata)
        edge = next_intent_id(conn, metadata.project_id)
        now = utcnow()
        conn.execute("INSERT INTO intents (id,project_id,to_fact_id,description,creator,worker,last_heartbeat_at,created_at,concluded_at) "
                     "VALUES (?,?,'goal',?,?,?,?,?,?)",
                     (edge, metadata.project_id, "独立终审通过：" + body.assessment.reason, "audit-controller", "final-reviewer", now, now, now))
        conn.executemany("INSERT INTO intent_sources VALUES (?, ?, ?)",
                         [(edge, metadata.project_id, fact_id) for fact_id in request.from_])
        conn.execute("UPDATE projects SET status='completed' WHERE id=?", (metadata.project_id,))
        metadata.phase = "closed"
        metadata.termination_reason = "goal_approved"
        from cairn.server.services import save_audit_metadata
        save_audit_metadata(conn, metadata)
        clear_project_reason(conn, metadata.project_id)
    store_record(conn, metadata.project_id, body.record)
    request.review_token = request.review_owner = None
    request.review_session = body.record.session
    request.updated_at = utcnow()
    if stale:
        request.status = "stale"
        request.error = "审核期间知识或证明变化，结果未批准"
    elif body.record.attempt_status == "error":
        request.status = "review_error"
        request.error = body.record.error
    else:
        request.status = body.assessment.decision
        request.decision = body.assessment.decision
        request.error = None
        if request.status in ("rejected", "needs_evidence"):
            conn.execute("INSERT INTO hints VALUES (?, ?, ?, ?, ?)",
                         (next_hint_id(conn, metadata.project_id), metadata.project_id,
                          f"独立终审 {request.status}：{body.assessment.reason}", "final-reviewer", utcnow()))
            bump_audit_revision(conn, metadata.project_id, reason=True)
    _save_completion(conn, request)
    return request
