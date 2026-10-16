"""Human-facing authorship, separate from the research graph and its prompts."""
from __future__ import annotations

import json

from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.services import audit_metadata, get_project_or_404

router = APIRouter(tags=["projects"])


def _saved_harnesses(conn, project_id: str) -> dict[str, str]:
    row = conn.execute("SELECT execution_json FROM audit_submissions WHERE project_id=?", (project_id,)).fetchone()
    if row is None:
        return {}
    try:
        saved = json.loads(row["execution_json"])
        workers = saved.get("workers")
        if not workers:
            workers = [{"name": f"pool-{role}-{harness}", "type": harness}
                       for role, pool in saved.get("role_pools", {}).items() for harness in pool]
        candidates = {}
        for worker in workers:
            name, harness = worker.get("name"), worker.get("type")
            if isinstance(name, str) and name and isinstance(harness, str) and harness:
                candidates.setdefault(name, set()).add(harness)
        return {name: next(iter(types)) for name, types in candidates.items() if len(types) == 1}
    except (ValueError, TypeError, AttributeError):
        return {}


@router.get("/projects/{project_id}/provenance")
def project_provenance(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        saved = _saved_harnesses(conn, project_id)
        metadata = audit_metadata(conn, project_id)
        seeds = set(metadata.seed_ids.values()) if metadata else set()
        intents = conn.execute("SELECT * FROM intents WHERE project_id=? ORDER BY rowid", (project_id,)).fetchall()
        facts = conn.execute("SELECT id FROM facts WHERE project_id=? ORDER BY rowid", (project_id,)).fetchall()
        hints = conn.execute("SELECT * FROM hints WHERE project_id=? ORDER BY rowid", (project_id,)).fetchall()

        def actor(worker, harness=None, *, infer=True):
            if not worker:
                return None
            return {"worker": worker, "harness": harness or (saved.get(worker) if infer else None)}

        incoming = {}
        intent_origins = {}
        for intent in intents:
            target = intent["to_fact_id"]
            kind = ("import" if target in seeds else "completion" if target == "goal" else
                    "feedback" if target and intent["description"] == "external_feedback"
                    and intent["creator"] == intent["worker"] and not intent["creator_harness"]
                    and not intent["worker_harness"] else "research")
            infer = kind not in ("import", "feedback")
            intent_origins[intent["id"]] = {
                "kind": kind,
                "creator": actor(intent["creator"], intent["creator_harness"],
                                 infer=infer and intent["creator"] != "audit-controller"),
                "executor": actor(intent["worker"], intent["worker_harness"],
                                  infer=infer and not (metadata and kind == "completion" and intent["worker"] == "final-reviewer")),
            }
            if target and intent["concluded_at"]:
                incoming.setdefault(target, []).append(intent)
        fact_origins = {}
        for fact in facts:
            fact_id = fact["id"]
            edges = incoming.get(fact_id, [])
            edge = edges[0] if len(edges) == 1 and fact_id not in ("origin", "goal") else None
            source = intent_origins[edge["id"]] if edge else None
            fact_origins[fact_id] = {
                "kind": "input" if fact_id in ("origin", "goal") else "import" if fact_id in seeds else source["kind"] if source else "research",
                "producer": source["executor"] if source else None,
                "intent_id": edge["id"] if edge else None,
            }
        return {
            "facts": fact_origins,
            "intents": intent_origins,
            "hints": {hint["id"]: {
                "kind": "hint",
                "producer": actor(hint["worker"] or hint["creator"], hint["harness"],
                                  infer=bool(hint["worker"]) or hint["creator"] not in ("overseer", "final-reviewer")),
            } for hint in hints},
        }
