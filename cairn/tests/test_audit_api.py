from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest
import yaml

from cairn.dispatcher.protocol.client import CairnClient
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import AuditRecordRequest, CreateAuditRequest
from cairn.server.routers import audits


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as http:
        yield http


def payload(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir(exist_ok=True)
    (repo / "app.py").write_text("def public():\n    return 'ok'\n")
    return {
        "title": "local audit", "origin": "local source", "goal": "find a valid finding",
        "user_goal": "audit the default deployment", "repo_path": str(repo),
        "workspace_root": str(tmp_path / "work"), "baseline": {"snapshot": "fixture-v1"},
        "policy": {"effect": "code execution", "cvss": {"AV": "N", "PR": "N"}},
        "budget": {"research_seconds": 60, "drain_seconds": 10, "final_review_seconds": 20},
        "seeds": [
            {"key": "dependent", "description": "check the path", "audit": {"premise_ids": ["entry"]}},
            {"key": "entry", "description": "entry declared", "audit": {
                "evidence": [{"path": "app.py", "start_line": 1}],
                "conditions": ["fixture-v1"], "review_status": "verified", "validation_level": "source_analysis",
            }},
        ],
        "intents": [{"from": ["origin", "dependent"], "description": "verify authentication"}],
        "hints": [{"content": "do not assume reachability", "creator": "human"}],
    }


def create(client, tmp_path):
    response = client.post("/audits", json=payload(tmp_path))
    assert response.status_code == 201, response.text
    return response.json()


def prepare(project):
    audit = project["project"]["audit"]
    workspace = Path(audit["workspace_root"]) / audit["project_id"]
    workspace.mkdir(parents=True)
    (workspace / "repo").symlink_to(audit["repo_path"], target_is_directory=True)
    (workspace / "input.json").write_text(json.dumps({key: audit[key] for key in ["project_id", "repo_path", "policy_digest"]}))
    return workspace


def test_initialization_maps_forward_references_and_preserves_metadata(client, tmp_path):
    project = create(client, tmp_path)
    meta = project["project"]
    audit = meta["audit"]
    assert meta["mode"] == "audit" and meta["status"] == "stopped"
    assert meta["bootstrap_enabled"] is False
    assert audit["phase"] == "preparing" and audit["research_deadline"] is None
    assert audit["audit_revision"] == audit["reason_revision"] == 1
    ids = audit["seed_ids"]
    assert ids == {"entry": "f001", "dependent": "f002"}
    facts = {f["id"]: f for f in project["facts"]}
    assert facts["f002"]["audit"]["premise_ids"] == ["f001"]
    assert facts["f001"]["audit"]["review_status"] == "verified"
    assert "audit" not in facts["origin"] and "audit" not in facts["goal"]
    assert [i["description"] for i in project["intents"]] == ["seed_import", "seed_import", "verify authentication"]
    assert [i["to"] for i in project["intents"]] == ["f001", "f002", None]
    assert project["intents"][2]["from"] == ["origin", "f002"]
    pid = meta["id"]
    assert client.get(f"/projects/{pid}").json() == project
    assert client.get("/projects").json()[0]["audit"] == audit
    exported = yaml.safe_load(client.get(f"/projects/{pid}/export?format=yaml").text)
    assert exported["project"]["audit"] == audit
    assert exported["facts"] == project["facts"]
    assert [i["id"] for i in exported["intents"]] == [i["id"] for i in project["intents"]]
    assert client.get(f"/projects/{pid}/export?format=timeline").status_code == 200


@pytest.mark.parametrize("problem", ["unknown", "duplicate_key", "cycle", "self", "goal", "duplicate_source", "reserved", "duplicate_premise"])
def test_invalid_graph_leaves_no_partial_project(client, tmp_path, problem):
    body = payload(tmp_path)
    if problem == "unknown": body["intents"][0]["from"] = ["not-found"]
    elif problem == "duplicate_key": body["seeds"][0]["key"] = "entry"
    elif problem == "cycle": body["seeds"][1]["audit"]["premise_ids"] = ["dependent"]
    elif problem == "self": body["seeds"][0]["audit"]["premise_ids"] = ["dependent"]
    elif problem == "goal": body["intents"][0]["from"] = ["goal"]
    elif problem == "duplicate_source": body["intents"][0]["from"] = ["entry", "entry"]
    elif problem == "reserved": body["seeds"][0]["key"] = "origin"
    else: body["seeds"][0]["audit"]["premise_ids"] = ["entry", "entry"]
    assert client.post("/audits", json=body).status_code in (400, 422)
    assert client.get("/projects").json() == []
    with db.get_conn() as conn:
        for table in ["projects", "facts", "intents", "intent_sources", "audits", "audit_facts"]:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM counters WHERE name='project'").fetchone()[0] == 0


def test_mid_transaction_failure_rolls_back_rows_and_identifiers(client, tmp_path, monkeypatch):
    original = audits._insert_intent
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected after first seed and edge")
    monkeypatch.setattr(audits, "_insert_intent", fail)
    with TestClient(app, raise_server_exceptions=False) as http:
        assert http.post("/audits", json=payload(tmp_path)).status_code == 500
    assert client.get("/projects").json() == []
    with db.get_conn() as conn:
        for table in ["audits", "audit_facts", "facts", "intents", "intent_sources", "hints", "scoped_counters"]:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(audits, "_insert_intent", original)
    assert create(client, tmp_path)["project"]["id"] == "proj_001"


def test_reader_cannot_observe_partial_seed_transaction(client, tmp_path, monkeypatch):
    original = audits._insert_intent
    inserted = threading.Event()
    release = threading.Event()
    def pause(*args, **kwargs):
        original(*args, **kwargs)
        if not inserted.is_set():
            inserted.set()
            assert release.wait(5)
    monkeypatch.setattr(audits, "_insert_intent", pause)
    body = payload(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, "/audits", json=body)
        try:
            assert inserted.wait(5)
            with sqlite3.connect(db._db_path) as reader:
                assert reader.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
                assert reader.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 201
    assert client.get("/projects").json()[0]["status"] == "stopped"


def test_activation_checks_workspace_and_freezes_deadlines(client, tmp_path, monkeypatch):
    project = create(client, tmp_path)
    pid = project["project"]["id"]
    url = f"/projects/{pid}/audit/start"
    assert client.put(f"/projects/{pid}/status", json={"status": "active"}).status_code == 409
    assert client.post(url).status_code == 409
    workspace = prepare(project)
    context = json.loads((workspace / "input.json").read_text())
    (workspace / "input.json").write_text(json.dumps({**context, "policy_digest": "wrong"}))
    assert client.post(url).status_code == 409
    assert client.get(f"/projects/{pid}").json()["project"]["status"] == "stopped"
    (workspace / "input.json").write_text(json.dumps(context))
    monkeypatch.setattr(audits, "utcnow", lambda: "2026-10-05T00:00:00Z")
    first = client.post(url)
    assert first.status_code == 200
    data = first.json()
    assert data["phase"] == "researching" and data["workspace_path"] == str(workspace)
    start = datetime.fromisoformat(data["started_at"])
    assert (datetime.fromisoformat(data["research_deadline"]) - start).total_seconds() == 60
    assert (datetime.fromisoformat(data["final_deadline"]) - start).total_seconds() == 90
    monkeypatch.setattr(audits, "utcnow", lambda: "2026-10-05T01:00:00Z")
    assert client.post(url).json() == data
    assert client.get(f"/projects/{pid}").json()["project"]["status"] == "active"
    client.put(f"/projects/{pid}/status", json={"status": "stopped"})
    assert client.post(url).status_code == 409
    assert client.get(f"/projects/{pid}/audit").json()["started_at"] == data["started_at"]


@pytest.mark.parametrize("problem", ["relative_repo", "nul", "missing_repo", "empty_policy", "bad_lines", "zero_budget"])
def test_invalid_input_is_rejected_without_database_changes(client, tmp_path, problem):
    body = payload(tmp_path)
    if problem == "relative_repo": body["repo_path"] = "relative/source"
    elif problem == "nul": body["workspace_root"] = "/tmp/invalid\x00path"
    elif problem == "missing_repo": body["repo_path"] = str(tmp_path / "missing")
    elif problem == "empty_policy": body["policy"] = {}
    elif problem == "bad_lines": body["seeds"][1]["audit"]["evidence"][0]["end_line"] = 0
    else: body["budget"]["research_seconds"] = 0
    assert client.post("/audits", json=body).status_code == 422
    assert client.get("/projects").json() == []


def test_activation_rejects_wrong_repo_and_unrepresentable_budget(client, tmp_path):
    body = payload(tmp_path)
    body["budget"]["research_seconds"] = 10**30
    project = client.post("/audits", json=body).json()
    pid = project["project"]["id"]
    workspace = prepare(project)
    wrong = tmp_path / "wrong-source"
    wrong.mkdir()
    (workspace / "repo").unlink()
    (workspace / "repo").symlink_to(wrong, target_is_directory=True)
    assert client.post(f"/projects/{pid}/audit/start").status_code == 409
    (workspace / "repo").unlink()
    (workspace / "repo").symlink_to(body["repo_path"], target_is_directory=True)
    assert client.post(f"/projects/{pid}/audit/start").status_code == 422
    assert client.get(f"/projects/{pid}/audit").json()["started_at"] is None
    assert client.get(f"/projects/{pid}").json()["project"]["status"] == "stopped"


def test_revisions_and_research_fact_metadata_without_controller_or_model(client, tmp_path):
    project = create(client, tmp_path)
    pid = project["project"]["id"]
    prepare(project)
    client.post(f"/projects/{pid}/audit/start")
    intent = client.post(f"/projects/{pid}/intents", json={"from": ["f001"], "description": "check", "creator": "worker"}).json()
    meta = client.get(f"/projects/{pid}/audit").json()
    assert (meta["audit_revision"], meta["reason_revision"]) == (2, 1)
    client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={"worker": "worker"})
    assert client.get(f"/projects/{pid}/audit").json() == meta
    forged = client.post(f"/projects/{pid}/intents/{intent['id']}/conclude", json={"worker": "worker", "description": "claim", "audit": {"review_status": "verified"}})
    assert forged.status_code == 400
    response = client.post(f"/projects/{pid}/intents/{intent['id']}/conclude", json={
        "worker": "worker", "description": "observed route", "audit": {
            "premise_ids": ["f001"], "evidence": [{"path": "app.py", "start_line": 1}],
        },
    })
    assert response.status_code == 200
    assert response.json()["fact"]["id"] == "f003"
    assert response.json()["fact"]["audit"]["review_status"] == "unverified"
    assert client.get(f"/projects/{pid}/audit").json()["reason_revision"] == 2
    client.post(f"/projects/{pid}/hints", json={"content": "new clue", "creator": "human"})
    assert client.get(f"/projects/{pid}/audit").json()["reason_revision"] == 3
    assert client.post(f"/projects/{pid}/complete", json={"from": ["f001"], "description": "done", "worker": "worker"}).status_code == 409
    assert client.get(f"/projects/{pid}").json()["project"]["status"] == "active"


def test_records_are_idempotent_and_errors_do_not_become_verdicts(client, tmp_path):
    project = create(client, tmp_path)
    pid = project["project"]["id"]
    url = f"/projects/{pid}/audit/reviews"
    body = {"record_key": "attempt-1", "object_type": "fact", "object_id": "f001", "phase": "seed_review",
            "input_digest": "a" * 64, "attempt_status": "error", "error": "CLI timeout"}
    first = client.post(url, json=body)
    assert first.status_code == 201
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json={**body, "error": "different"}).status_code == 409
    assert client.post(url, json={**body, "decision": "approved"}).status_code == 422
    success = {**body, "record_key": "attempt-2", "attempt_status": "success", "error": None, "decision": "approved"}
    assert client.post(url, json=success).status_code == 201
    meta = client.get(f"/projects/{pid}/audit").json()
    assert [r["attempt_status"] for r in meta["records"]] == ["error", "success"]
    assert meta["records"][0]["decision"] is None
    assert (meta["audit_revision"], meta["reason_revision"]) == (1, 1)
    assert client.get(f"/projects/{pid}").json()["project"]["status"] == "stopped"
    assert client.get(f"/projects/{pid}").json()["facts"][2]["audit"]["review_status"] == "verified"
    other = create(client, tmp_path)["project"]["id"]
    assert client.post(f"/projects/{other}/audit/reviews", json={**body, "object_type": "project", "object_id": pid}).status_code == 404
    client.delete(f"/projects/{pid}")
    with db.get_conn() as conn:
        for table in ["audits", "audit_facts", "audit_records"]:
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE project_id=?", (pid,)).fetchone()[0] == 0


def test_additive_migration_twice_preserves_generic_project_and_api(tmp_path, monkeypatch):
    database = tmp_path / "legacy.db"
    legacy_schema = db.SCHEMA.split("CREATE TABLE IF NOT EXISTS audits")[0]
    with sqlite3.connect(database) as conn:
        conn.executescript(legacy_schema)
        conn.execute("INSERT INTO projects (id,title,created_at) VALUES ('proj_001','old','2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO facts VALUES ('origin','proj_001','old start')")
        conn.execute("INSERT INTO facts VALUES ('goal','proj_001','old goal')")
        conn.execute("UPDATE counters SET value=1 WHERE name='project'")
    for _ in range(2):
        monkeypatch.setattr(db, "_db_path", None)
        db.configure(database)
    with TestClient(app) as client:
        old = client.get("/projects/proj_001").json()
        assert old["project"]["title"] == "old" and "mode" not in old["project"]
        assert old["facts"] == [{"id": "origin", "description": "old start"}, {"id": "goal", "description": "old goal"}]
        assert client.get("/projects/proj_001/audit").status_code == 404
        assert "audit:" not in client.get("/projects/proj_001/export?format=yaml").text
        assert client.post("/projects/proj_001/complete", json={"from": ["origin"], "description": "old complete", "worker": "worker"}).status_code == 200
        assert client.post("/projects/proj_001/reopen", json={"description": "feedback", "creator": "human"}).status_code == 200
        assert create(client, tmp_path)["project"]["id"] == "proj_002"
    with db.get_conn() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT mode FROM projects WHERE id='proj_001'").fetchone()[0] == "generic"


def test_shared_client_uses_real_protocol_and_typed_metadata(client, tmp_path, monkeypatch):
    api = CairnClient("http://testserver")
    monkeypatch.setattr(api, "_session", lambda: client)
    request = CreateAuditRequest.model_validate(payload(tmp_path))
    result = api.create_audit(request)
    assert result.status_code == 201
    project = result.data
    pid = project["project"]["id"]
    assert api.get_project(pid).project.mode == "audit"
    assert api.get_audit(pid).policy == request.policy
    prepare(project)
    assert api.start_audit(pid).ok
    record = AuditRecordRequest(record_key="client-attempt", object_type="project", object_id=pid,
                                input_digest=hashlib.sha256(b"input").hexdigest(), phase="build", attempt_status="success")
    assert api.record_audit_review(pid, record).ok
    assert api.get_audit(pid).records[0].record_key == record.record_key
