from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone

from fastapi import HTTPException

from cairn.server.models import AuditFactData, AuditMetadata, Fact, Intent, ProjectMeta, ProjectReason


def json_text(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "Metadata must contain finite JSON values") from exc


def audit_metadata(conn: sqlite3.Connection, project_id: str) -> AuditMetadata | None:
    row = conn.execute("SELECT metadata_json FROM audits WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    metadata = AuditMetadata.model_validate_json(row["metadata_json"])
    metadata.completion_pending = has_pending_completion(conn, project_id)
    return metadata


def has_pending_completion(conn: sqlite3.Connection, project_id: str) -> bool:
    rows = conn.execute("SELECT data_json FROM audit_completions WHERE project_id=?", (project_id,)).fetchall()
    from cairn.server import runtime
    clock = runtime.load(conn, project_id)
    now = runtime.elapsed(clock) if clock else runtime.seconds(utcnow())
    for row in rows:
        request = json.loads(row["data_json"])
        if request["status"] not in ("pending", "stale", "review_error"):
            continue
        deadline = request.get("round_deadline_seconds") if clock else runtime.seconds(request["round_deadline"])
        if deadline is None:
            return True
        if now >= deadline:
            continue
        attempt = request.get("attempt_deadline_seconds") if clock else runtime.seconds(request["attempt_deadline"])
        if request["review_token"] and attempt is not None and now < attempt:
            return True  # 最后一次审核仍在执行时继续暂停，不因 attempts 到上限抢先派发。
        if request["attempts"] < request["max_attempts"]:
            return True
    return False


def check_new_research_allowed(conn: sqlite3.Connection, project_id: str) -> None:
    from cairn.server.audit_budget import new_research

    new_research(conn, project_id)
    if get_project_or_404(conn, project_id)["mode"] == "audit" and has_pending_completion(conn, project_id):
        raise HTTPException(409, "Audit completion is awaiting independent review; new research is paused")


def save_audit_metadata(conn: sqlite3.Connection, metadata: AuditMetadata) -> None:
    conn.execute(
        "UPDATE audits SET metadata_json = ? WHERE project_id = ?",
        (json_text(metadata.model_dump(mode="json")), metadata.project_id),
    )


def bump_audit_revision(conn: sqlite3.Connection, project_id: str, *, reason: bool) -> None:
    metadata = audit_metadata(conn, project_id)
    if metadata is not None:
        metadata.audit_revision += 1
        if reason:
            metadata.reason_revision += 1
        save_audit_metadata(conn, metadata)


def fact_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str, *, is_audit: bool = True) -> Fact:
    audit_row = conn.execute(
        "SELECT metadata_json FROM audit_facts WHERE project_id = ? AND fact_id = ?",
        (project_id, row["id"]),
    ).fetchone() if is_audit else None
    return Fact(
        id=row["id"], description=row["description"],
        audit=AuditFactData.model_validate_json(audit_row["metadata_json"]) if audit_row is not None else None,
    )

def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"


def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"


def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)


def next_intent_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "intent", "i", project_id)


def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)


def next_review_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "review", "r", project_id)


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed", "draining", "budget_exhausted"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_not_in_sources(fact_ids: list[str]) -> None:
    if "goal" in fact_ids:
        raise HTTPException(400, "goal cannot be used in from")


def validate_intent_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM intents WHERE id = ? AND project_id = ?",
        (intent_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    return row


def get_claimable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_releasable_open_intent_or_404(
    conn: sqlite3.Connection, project_id: str, intent_id: str, worker: str
) -> sqlite3.Row:
    expire_workers(conn, project_id)
    row = get_intent_or_404(conn, project_id, intent_id)
    if row["to_fact_id"] is not None:
        raise HTTPException(409, "Intent already concluded")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Intent is currently claimed by {row['worker']}")
    return row


def get_completion_intent_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? AND to_fact_id = 'goal'",
        (project_id,),
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion intent")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion intents")
    return rows[0]


def intent_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Intent:
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    metadata = audit_metadata(conn, project_id)
    lease = metadata.leases.get(f"intent:{row['id']}") if metadata else None
    return Intent(
        id=row["id"],
        **{"from": [s["fact_id"] for s in sources]},
        to=row["to_fact_id"],
        description=row["description"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        lease_id=lease.id if lease and lease.worker == row["worker"] and row["to_fact_id"] is None else None,
    )


def build_intents(conn: sqlite3.Connection, project_id: str) -> list[Intent]:
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    return [intent_to_model(conn, r, project_id) for r in rows]


def get_intent_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT intent_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["intent_timeout"]


def get_reason_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT reason_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["reason_timeout"]


def project_reason_from_row(row: sqlite3.Row) -> ProjectReason | None:
    if row["reason_worker"] is None:
        return None
    return ProjectReason(
        worker=row["reason_worker"],
        trigger=row["reason_trigger"],
        started_at=row["reason_started_at"],
        last_heartbeat_at=row["reason_last_heartbeat_at"],
    )


def project_meta_from_row(row: sqlite3.Row, conn: sqlite3.Connection) -> ProjectMeta:
    from cairn.server import runtime
    current_runtime = runtime.load(conn, row["id"])
    metadata = audit_metadata(conn, row["id"]) if row["mode"] == "audit" else None
    reason = project_reason_from_row(row)
    if reason and metadata and "reason" in metadata.leases:
        reason.lease_id = metadata.leases["reason"].id
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        bootstrap_enabled=bool(row["bootstrap_enabled"]),
        created_at=row["created_at"],
        reason=reason,
        mode=row["mode"],
        audit=metadata,
        runtime_generation=current_runtime["generation"] if current_runtime else None,
    )


def clear_project_reason(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )


def expire_workers(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_intent_timeout(conn)
    now = utcnow()
    query = """
        UPDATE intents
        SET worker = NULL, worker_harness = NULL
        WHERE to_fact_id IS NULL
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)


def expire_reason_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_reason_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE reason_worker IS NOT NULL
          AND reason_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(reason_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)
