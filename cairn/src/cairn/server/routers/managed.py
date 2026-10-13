from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cairn.server.db import get_conn
from cairn.server import db
from cairn.server.submissions import SubmissionRequest, RevisionRequest, accept, get_row, summary

router = APIRouter(tags=["managed service"])


class RuntimeAction(BaseModel):
    database_id: str
    service_id: str
    remaining_seconds: float | None = Field(default=None, ge=0)


def control_service(request, body):
    service = managed(request)
    if body.database_id != service.database_id or body.service_id != service.service_id:
        raise HTTPException(409, "Cairn database or instance changed")
    if not service.health()["ready"]:
        raise HTTPException(503, "Restart Cairn manually before task control")
    return service


def managed(request: Request):
    service = getattr(request.app.state, "managed_service", None)
    if service is None:
        raise HTTPException(503, "This is a data viewer; start the execution service manually with cairn up")
    return service


@router.get("/runtime/health")
def health(request: Request):
    service = getattr(request.app.state, "managed_service", None)
    return service.health() if service else {"managed": False, "ready": False}


@router.post("/audit-submissions", status_code=202)
def submit(body: SubmissionRequest, request: Request):
    return accept(managed(request), body)


@router.get("/audit-submissions/{submission_id}")
def get_submission(submission_id: str, request: Request):
    service = managed(request)
    with get_conn() as conn:
        return summary(get_row(conn, submission_id), service)


@router.put("/audit-submissions/{submission_id}", status_code=202)
def revise(submission_id: str, body: RevisionRequest, request: Request):
    return accept(managed(request), body, submission_id=submission_id)


@router.get("/projects/{project_id}/execution-config")
def execution_config(project_id: str, request: Request):
    managed(request)
    with get_conn() as conn:
        row = conn.execute("SELECT execution_json FROM audit_submissions WHERE project_id=? AND status='running'", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Project is not admitted for managed execution")
        # Generated worker environments are empty in the role-pool mode.
        from cairn.dispatcher.config import DispatchConfig
        config = DispatchConfig.model_validate_json(row["execution_json"])
        config.common_env = {}
        return config.model_dump(mode="json")


@router.get("/projects/{project_id}/runtime")
def project_runtime(project_id: str, request: Request):
    from cairn.server import runtime
    from cairn.server.services import audit_metadata, get_project_or_404
    service = getattr(request.app.state, "managed_service", None)
    with get_conn() as conn:
        metadata = audit_metadata(conn, project_id)
        if metadata is None:
            get_project_or_404(conn, project_id)
            raise HTTPException(404, "Project is not an audit")
        data = runtime.view(conn, metadata)
        clock = runtime.BudgetClock(conn, metadata)
        closing_end = clock.deadline(metadata.closing, "attempt_deadline")
        data["closing_remaining_seconds"] = max(0, closing_end-clock.now) if closing_end is not None else None
        from cairn.server.audit_review import list_completions
        data["review_remaining_seconds"] = {}
        for completion in list_completions(conn, metadata):
            end = clock.deadline(completion, "attempt_deadline")
            data["review_remaining_seconds"][completion.id] = max(0, end-clock.now) if end is not None else None
        data.update(project_id=project_id, managed=service is not None,
                    service_id=service.service_id if service else None,
                    database_id=service.database_id if service else None)
        row = conn.execute("SELECT run_dir,id FROM audit_submissions WHERE project_id=?", (project_id,)).fetchone()
        data["run_dir"] = row["run_dir"] if row else str(db._db_path.parent)
        data["submission_id"] = row["id"] if row else None
        return data


@router.post("/projects/{project_id}/runtime/pause", status_code=202)
def pause_project(project_id: str, body: RuntimeAction, request: Request):
    service = control_service(request, body)
    service.pause(project_id)
    return project_runtime(project_id, request)


@router.post("/projects/{project_id}/runtime/resume", status_code=202)
def resume_project(project_id: str, body: RuntimeAction, request: Request):
    service = control_service(request, body)
    service.resume(project_id, body.remaining_seconds)
    return project_runtime(project_id, request)
