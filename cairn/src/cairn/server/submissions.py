"""Persist the existing audit preparation flow, without owning service startup."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import shutil
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from cairn.controller.submissions import (
    ExecutionSnapshot, project_config, read_prepared, save_snapshot, snapshot_fingerprint,
)
from cairn.server.db import get_conn
from cairn.server.services import utcnow


class SubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_dir: str = Field(min_length=1)
    database_id: str = Field(min_length=1)
    service_id: str = Field(min_length=1)
    execution: ExecutionSnapshot


class RevisionRequest(SubmissionRequest):
    revision: int = Field(gt=0)


def get_row(conn, submission_id):
    row = conn.execute("SELECT * FROM audit_submissions WHERE id=?", (submission_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Submission not found")
    return row


def summary(row, service):
    data = {key: row[key] for key in ("run_dir", "revision", "status", "project_id", "error")}
    data.update(submission_id=row["id"], issues=json.loads(row["issues_json"]),
                service_id=service.service_id, database_id=service.database_id)
    if row["project_id"]:
        data["project_url"] = f"{service.config.server}/#/projects/{row['project_id']}"
    return data


def accept(service, body: SubmissionRequest, *, submission_id=None):
    if body.database_id != service.database_id or body.service_id != service.service_id:
        raise HTTPException(409, "Cairn database or service instance changed; no submission was accepted")
    if not service.health()["ready"]:
        raise HTTPException(503, "Execution components are not ready; start Cairn manually")
    try:
        folder = Path(body.run_dir).expanduser().resolve(strict=True)
        prepared, plan, artifacts = read_prepared(folder)
        config = project_config(service.config, body.execution)
        fingerprint = snapshot_fingerprint(plan, body.execution, artifacts)
        if service.directory.is_relative_to(Path(prepared["source_root"]).resolve()):
            raise ValueError("service data must be outside the audited source")
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not service.health()["ready"]:
            raise HTTPException(503, "Execution components failed; restart Cairn manually")
        existing = conn.execute("SELECT * FROM audit_submissions WHERE run_dir=?", (str(folder),)).fetchone()
        if submission_id is not None:
            row = get_row(conn, submission_id)
            if row["run_dir"] != str(folder):
                raise HTTPException(409, "Revision must use the original run")
            if row["revision"] != body.revision:
                raise HTTPException(409, "Submission revision changed; reread it")
            if row["fingerprint"] == fingerprint:
                return summary(row, service)
            if row["project_id"] is not None or row["status"] not in ("needs_revision", "error"):
                raise HTTPException(409, "Only an uncreated, returned submission can be revised")
            revision = row["revision"] + 1
        elif existing:
            if existing["fingerprint"] == fingerprint:
                return summary(existing, service)
            raise HTTPException(409, summary(existing, service))
        else:
            submission_id, revision = uuid.uuid4().hex, 1
        destination = service.directory / "submissions" / submission_id / f"revision-{revision:03d}-{uuid.uuid4().hex[:8]}"
        try:
            saved = save_snapshot(destination, prepared, artifacts, body.execution)
        except (OSError, ValueError) as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise HTTPException(422, f"Cannot save submission inputs: {exc}") from exc
        now = utcnow()
        if existing:
            conn.execute("UPDATE audit_submissions SET revision=?,fingerprint=?,status='pending',prepared_json=?,execution_json=?,worker_token=NULL,issues_json='[]',error=NULL,updated_at=? WHERE id=?",
                (revision, fingerprint, json.dumps(saved), config.model_dump_json(), now, submission_id))
        else:
            conn.execute("INSERT INTO audit_submissions (id,run_dir,revision,fingerprint,status,prepared_json,execution_json,created_at,updated_at) VALUES (?,?,?,?,'pending',?,?,?,?)",
                (submission_id, str(folder), revision, fingerprint, json.dumps(saved), config.model_dump_json(), now, now))
        return summary(get_row(conn, submission_id), service)


def claim_next():
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM audit_submissions WHERE status IN ('pending','resuming') ORDER BY created_at,rowid LIMIT 1").fetchone()
        if row is None:
            return None
        token = secrets.token_hex(32)
        conn.execute("UPDATE audit_submissions SET status='preparing',worker_token=?,updated_at=? WHERE id=?",
                     (token, utcnow(), row["id"]))
        return dict(get_row(conn, row["id"]))


def finish(submission_id, status, *, issues=(), error=None):
    with get_conn() as conn:
        conn.execute("UPDATE audit_submissions SET status=?,issues_json=?,error=?,updated_at=? WHERE id=? AND status NOT IN ('deleted','error','interrupted','paused','pausing')",
                     (status, json.dumps(list(issues)), error, utcnow(), submission_id))


def creation_owner(conn, token):
    row = conn.execute("SELECT * FROM audit_submissions WHERE worker_token=? AND status IN ('preparing','running')", (token,)).fetchone()
    if row is None:
        raise HTTPException(403, "Managed audits must pass the submission preparation flow")
    return row


def adopt_legacy(conn, service, metadata):
    """Attach an old native run in this database directory without rebuilding its graph."""
    from cairn.server import db
    from cairn.dispatcher.config import DispatchConfig, ControllerBudgetConfig
    from cairn.controller.submissions import execution_snapshot

    folder = db._db_path.parent
    try:
        locator = json.loads((folder / "project.json").read_text())
        if locator.get("project_id") != metadata.project_id:
            raise ValueError("Legacy project locator does not match this database")
        prepared, plan, artifacts = read_prepared(folder)
        baseline = json.loads(artifacts["source_baseline"])
        if Path(prepared["source_root"]).resolve() != Path(metadata.repo_path) or baseline != metadata.baseline:
            raise ValueError("Legacy inputs do not match the original source baseline")
        config = DispatchConfig.load(folder / "config.yaml")
        if Path(config.local.workspace_root).resolve() != Path(metadata.workspace_root):
            raise ValueError("Legacy workspace configuration does not match this project")
        config.server = service.config.server
        config.controller.budget = ControllerBudgetConfig.model_validate(metadata.budget.model_dump())
        # Revalidate the original configuration, retaining its harness pools.
        config = DispatchConfig.model_validate(config.model_dump())
        snapshot = execution_snapshot(config)
        key = uuid.uuid4().hex
        saved = save_snapshot(service.directory / "submissions" / key / "legacy", prepared, artifacts, snapshot)
        now = utcnow()
        conn.execute("INSERT INTO audit_submissions (id,run_dir,revision,fingerprint,status,prepared_json,execution_json,project_id,created_at,updated_at) VALUES (?,?,1,?,'interrupted',?,?,?,?,?)",
                     (key, str(folder), snapshot_fingerprint(plan, snapshot, artifacts), json.dumps(saved),
                      config.model_dump_json(), metadata.project_id, now, now))
        return get_row(conn, key)
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise HTTPException(409, f"Legacy run cannot be recovered from its original inputs: {exc}") from exc
