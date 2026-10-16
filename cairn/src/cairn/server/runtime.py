"""Persisted active-time budgets for manually managed local projects."""
from __future__ import annotations

from datetime import datetime, timezone
from contextvars import ContextVar
import json
import logging
import time
import uuid

from fastapi import HTTPException

INSTANCE = uuid.uuid4().hex
HEARTBEAT_SECONDS = 2
HEARTBEAT_WARNING_SECONDS = 10
LEASE_SECONDS = 60
REQUEST_FENCE = ContextVar("cairn_request_fence", default=None)
LOG = logging.getLogger(__name__)


def check_request(conn):
    fence = REQUEST_FENCE.get()
    if fence is None:
        return
    project_id, generation, token, suffix = fence
    data = load(conn, project_id)
    if data is None:
        return
    if token is not None:
        from cairn.server.submissions import creation_owner
        if creation_owner(conn, token)["project_id"] != project_id:
            raise HTTPException(403, "Submission does not own this preparation")
        return
    if generation != str(data["generation"]):
        raise HTTPException(409, "Runtime generation changed; old attempt cannot write")
    # The transaction completing the task also freezes its clock. Its receipt
    # remains idempotent; other stopped-task writes still fail.
    if data["state"] == "finished" and suffix.endswith("/review"):
        return
    if not live(data):
        raise HTTPException(409, "Task execution is paused, interrupted or awaiting resume")


def monotonic():
    return time.monotonic()


def load(conn, project_id):
    row = conn.execute("SELECT data_json FROM project_runtime WHERE project_id=?", (project_id,)).fetchone()
    return json.loads(row[0]) if row else None


def save(conn, project_id, data):
    conn.execute("INSERT INTO project_runtime VALUES (?,?) ON CONFLICT(project_id) DO UPDATE SET data_json=excluded.data_json",
                 (project_id, json.dumps(data, ensure_ascii=False, allow_nan=False)))


def event(data, kind, reason=None):
    data.setdefault("history", []).append({"event": kind, "reason": reason,
        "generation": data["generation"], "elapsed_seconds": data["elapsed"],
        "at": datetime.now(timezone.utc).isoformat()})


def initialize(conn, project_id):
    data = {"version": 1, "generation": 1, "elapsed": 0.0, "state": "preparing",
            "instance": INSTANCE, "anchor": None, "valid_until": None, "reason": None}
    event(data, "created")
    save(conn, project_id, data)
    return data


def elapsed(data):
    value = data["elapsed"]
    if (data["state"] in ("running", "pausing") and data["instance"] == INSTANCE
            and data["anchor"] is not None and data["valid_until"] is not None):
        value += max(0, min(monotonic(), data["valid_until"]) - data["anchor"])
    return value


def live(data):
    return (data["instance"] == INSTANCE and data["state"] == "running"
            and data["valid_until"] is not None and monotonic() < data["valid_until"])


def settle(data):
    data["elapsed"] = elapsed(data)
    data["anchor"] = monotonic() if data["instance"] == INSTANCE else None


def freeze(conn, project_id, state, reason=None):
    data = load(conn, project_id)
    if data is None:
        return
    settle(data)
    changed = data["state"] != state
    data.update(state=state, anchor=None, valid_until=None, reason=reason)
    if changed:
        event(data, state, reason)
    save(conn, project_id, data)


def restart(conn):
    for row in conn.execute("SELECT project_id FROM project_runtime").fetchall():
        data = load(conn, row[0])
        status = conn.execute("SELECT status FROM projects WHERE id=?", (row[0],)).fetchone()[0]
        if status in ("completed", "budget_exhausted"):
            freeze(conn, row[0], "finished")
        elif data["state"] not in ("paused", "blocked"):
            # A new Server never interprets a previous instance's monotonic anchor.
            data.update(instance=INSTANCE, anchor=None, valid_until=None, state="interrupted",
                        reason="Cairn service restarted; explicit resume required")
            event(data, "interrupted", data["reason"])
            save(conn, row[0], data)


def activate(conn, project_id):
    data = load(conn, project_id)
    if data is None:
        return
    if data["state"] not in ("preparing", "resuming"):
        raise HTTPException(409, "Task activation was superseded by pause or interruption")
    now = monotonic()
    data.update(state="running", instance=INSTANCE, anchor=now, valid_until=now + LEASE_SECONDS,
                last_heartbeat_at=datetime.now(timezone.utc).isoformat(), reason=None)
    event(data, "running")
    save(conn, project_id, data)


def heartbeat(conn):
    for row in conn.execute("SELECT project_id FROM project_runtime").fetchall():
        data = load(conn, row[0])
        if data["state"] not in ("running", "pausing"):
            continue
        if data["state"] == "pausing" and data["valid_until"] is None:
            continue  # Preparing/resuming has not acquired a running-time lease.
        now = monotonic()
        if data["instance"] != INSTANCE or now >= data["valid_until"]:
            freeze(conn, row[0], "interrupted", "Runtime heartbeat expired")
            conn.execute("UPDATE projects SET status='stopped' WHERE id=? AND status IN ('active','draining')", (row[0],))
        else:
            gap = now - (data["valid_until"] - LEASE_SECONDS)
            if gap >= HEARTBEAT_WARNING_SECONDS:
                LOG.warning("runtime heartbeat delayed project=%s generation=%s gap=%.3fs lease_remaining=%.3fs",
                            row[0], data["generation"], gap, data["valid_until"] - now)
            settle(data)
            data["valid_until"] = monotonic() + LEASE_SECONDS
            data["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            save(conn, row[0], data)


def require_running(conn, project_id):
    check_request(conn)
    data = load(conn, project_id)
    if data is not None and not live(data):
        raise HTTPException(409, "Task execution is paused, interrupted or awaiting resume")


def pause(conn, project_id):
    data = load(conn, project_id)
    if data is None:
        raise HTTPException(409, "Legacy task must be migrated before pause")
    status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()[0]
    if status in ("completed", "budget_exhausted"):
        return data
    if data["state"] in ("paused", "pausing", "interrupted", "blocked"):
        return data
    data.update(state="pausing", reason="User paused task")
    event(data, "pausing", data["reason"])
    save(conn, project_id, data)
    conn.execute("UPDATE projects SET status='stopped' WHERE id=?", (project_id,))
    return data


def invalidate_calls(conn, metadata):
    metadata.leases = {}
    metadata.draining_lease_ids = []
    closing = metadata.closing
    if closing.token:
        closing.token = None
        closing.status, closing.error = "error", "Execution interrupted"
        closing.attempts = 0
        closing.round_deadline = closing.attempt_deadline = None
        closing.round_deadline_seconds = closing.attempt_deadline_seconds = None
    for row in conn.execute("SELECT id,data_json FROM audit_completions WHERE project_id=?", (metadata.project_id,)).fetchall():
        request = json.loads(row["data_json"])
        if request["review_token"]:
            request.update(review_token=None, review_owner=None, status="review_error", error="Execution interrupted",
                           attempts=0, round_deadline=None, attempt_deadline=None,
                           round_deadline_seconds=None, attempt_deadline_seconds=None)
            conn.execute("UPDATE audit_completions SET data_json=? WHERE project_id=? AND id=?",
                         (json.dumps(request), metadata.project_id, row["id"]))
    from cairn.server.services import save_audit_metadata, clear_project_reason
    save_audit_metadata(conn, metadata)
    conn.execute("UPDATE intents SET worker=NULL WHERE project_id=? AND to_fact_id IS NULL", (metadata.project_id,))
    clear_project_reason(conn, metadata.project_id)


def resume(conn, metadata, remaining_seconds=None):
    project_id = metadata.project_id
    status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()[0]
    if status in ("completed", "budget_exhausted"):
        raise HTTPException(409, "Finished projects cannot resume")
    data = load(conn, project_id)
    if data is None:
        if metadata.started_at is not None and remaining_seconds is None:
            raise HTTPException(409, {"status": "needs_budget", "phase": metadata.phase,
                                      "reason": "Legacy task has no persisted active-time balance"})
        data = initialize(conn, project_id)
        if metadata.started_at is not None:
            r, d, f = limits(metadata)
            caps = {"researching": (r, r), "draining": (d, d-r), "finalizing": (f, f-r)}
            if metadata.phase not in caps:
                raise HTTPException(409, "This legacy stage cannot resume")
            end, cap = caps[metadata.phase]
            if remaining_seconds < 0 or remaining_seconds > cap:
                raise HTTPException(422, "Remaining budget exceeds the original stage window")
            data["elapsed"] = end - remaining_seconds
            event(data, "legacy_budget_migrated")
    elif remaining_seconds is not None:
        raise HTTPException(409, "Budget migration is one-time; this task already has an active clock")
    if live(data) or data["state"] == "resuming":
        return data
    if data["state"] == "pausing":
        raise HTTPException(409, "Wait for in-flight calls to stop before resuming")
    invalidate_calls(conn, metadata)
    settle(data)
    previous_state = data["state"]
    data.update(generation=data["generation"]+1, state="resuming", instance=INSTANCE,
                anchor=None, valid_until=None, reason=None)
    event(data, "resuming")
    r, d, f = limits(metadata)
    end = {"preparing": 0, "researching": r, "draining": d, "finalizing": f}[metadata.phase]
    data["history"][-1].update(from_state=previous_state, audit_phase=metadata.phase,
        remaining_seconds=max(0, end-data["elapsed"]), unfinished_intents=conn.execute(
            "SELECT COUNT(*) FROM intents WHERE project_id=? AND to_fact_id IS NULL", (project_id,)).fetchone()[0])
    save(conn, project_id, data)
    return data


def limits(metadata):
    r = metadata.budget.research_seconds
    d = r + metadata.budget.drain_seconds
    return r, d, d + metadata.budget.final_review_seconds


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


class BudgetClock:
    def __init__(self, conn, metadata):
        self.metadata = metadata
        self.data = load(conn, metadata.project_id)
        self.active = self.data is not None
        if self.active:
            self.now = elapsed(self.data)
            self.research_end, self.drain_end, self.final_end = limits(metadata)
        else:
            from cairn.server.services import utcnow
            self.now = seconds(utcnow())
            self.research_end = seconds(metadata.research_deadline)
            self.drain_end = seconds(metadata.drain_deadline)
            self.final_end = seconds(metadata.final_deadline)

    def deadline(self, obj, name):
        return getattr(obj, name+"_seconds") if self.active else seconds(getattr(obj, name))

    def set_deadline(self, obj, name, value):
        if self.active:
            setattr(obj, name+"_seconds", value)
            setattr(obj, name, None)
        else:
            setattr(obj, name, timestamp(value) if value is not None else None)

    def due(self, obj, name):
        value = self.deadline(obj, name)
        return value is not None and self.now >= value


def view(conn, metadata):
    data = load(conn, metadata.project_id)
    if data is None:
        return {"clock_mode": "legacy", "state": "needs_budget", "audit_phase": metadata.phase}
    clock = BudgetClock(conn, metadata)
    phase_end = {"preparing": 0, "researching": clock.research_end,
                 "draining": clock.drain_end, "finalizing": clock.final_end, "closed": clock.final_end}[metadata.phase]
    return {**data, "clock_mode": "active", "audit_phase": metadata.phase,
            "elapsed_seconds": clock.now, "stage_remaining_seconds": max(0, phase_end-clock.now),
            "research_remaining_seconds": max(0, clock.research_end-clock.now),
            "total_remaining_seconds": max(0, clock.final_end-clock.now)}
