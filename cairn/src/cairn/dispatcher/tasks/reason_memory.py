"""Small planning receipts, separate from facts and scheduling state."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import uuid

LOG = logging.getLogger(__name__)


def load_memory(project, workspace: str) -> dict:
    if project.project.mode != "audit":
        return {}
    records = []
    root = Path(workspace) / "research"
    # ponytail: scan small receipts; add an index only if history size warrants it.
    paths = [*root.glob("reason-memory-*.json"), *root.glob("generation-*/reason-memory-*.json")]
    for path in paths:
        try:
            record = json.loads(path.read_text())
            if (record["project_id"] == project.project.id
                    and record["policy_digest"] == project.project.audit.policy_digest
                    and record["generation"] <= (project.project.runtime_generation or 0)):
                records.append(((record["generation"], record["reason_started_at"], record.get("attempt_started_ns", 0)), record, str(path)))
        except (OSError, ValueError, KeyError, TypeError):
            LOG.warning("cannot read Reason memory path=%s", path)
    records.sort(key=lambda item: item[0], reverse=True)
    context = {"note": "可修订规划记忆，不是事实；以当前图、Hint 和固定 policy 为准。"}
    for _, record, path in records:
        if record.get("outcome") == "success" and record.get("planning_notes") and "previous_plan" not in context:
            context["previous_plan"] = {**record, "memory_path": path}
        elif record.get("outcome") not in (None, "success") and "last_failure" not in context:
            context["last_failure"] = {**record, "memory_path": path}
        if "previous_plan" in context and "last_failure" in context:
            break
    return context


def save_memory(backend, workspace, client, project, worker, cancellation, lease, *,
                outcome, attempt_started_ns, error=None, planning_notes=None, receipt_path=None):
    if project.project.mode != "audit" or cancellation.is_cancelled or lease.failure is not None:
        return
    if outcome == "success" and not (planning_notes or "").strip():
        return
    try:
        current = client.get_project(project.project.id)
        before, after = project.project.reason, current.project.reason
        if (cancellation.is_cancelled or lease.failure is not None
                or current.project.runtime_generation != project.project.runtime_generation
                or current.project.status != "active" or before is None or after is None
                or after.worker != worker.name or after.started_at != before.started_at
                or after.lease_id != before.lease_id):
            return
        folder = Path(workspace) / "research"
        if project.project.runtime_generation is not None:
            folder /= f"generation-{project.project.runtime_generation:03d}"
        record = {"project_id": project.project.id, "policy_digest": project.project.audit.policy_digest,
                  "generation": project.project.runtime_generation or 0,
                  "reason_started_at": before.started_at,
                  "attempt_started_ns": attempt_started_ns,
                  "audit_revision": project.project.audit.audit_revision,
                  "reason_revision": project.project.audit.reason_revision,
                  "receipt_path": receipt_path, "outcome": outcome}
        if outcome == "success":
            record["planning_notes"] = planning_notes
        else:
            record["error"] = " ".join(str(error or outcome).split())[:1200]
        path = str(folder / f"reason-memory-{uuid.uuid4().hex}.json")
        content = json.dumps(record, ensure_ascii=False, indent=2)
        if cancellation.is_cancelled or lease.failure is not None:
            return
        # Order by attempt start, not save time: older delayed results stay older.
        backend.write_text_file(workspace, path, content)
    except Exception:
        LOG.warning("cannot save Reason memory project=%s", project.project.id, exc_info=True)
