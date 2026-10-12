from fastapi import APIRouter, HTTPException, Request

from cairn.server.db import get_conn
from cairn.server.submissions import SubmissionRequest, RevisionRequest, accept, get_row, summary

router = APIRouter(tags=["managed service"])


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
