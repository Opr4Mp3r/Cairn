from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.server.models import (AuditCompletion, AuditDetail, AuditRecordRequest, ClosingReviewRequest, CompletionClaimRequest, CreateProjectRequest,
                                CompletionReviewRequest, CreateAuditRequest, Intent, ProjectDetail, ProjectSummary, Settings)

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(self, base_url: str, timeout: float = 10.0, *, trust_env: bool = True,
                 headers: dict[str, str] | None = None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._trust_env = trust_env
        self._headers = headers or {}
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def service_health(self) -> dict:
        response = self._session().get(self._url("/runtime/health"), timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def get_runtime(self, project_id: str) -> dict:
        response = self._session().get(self._url(f"/projects/{project_id}/runtime"), timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def control_runtime(self, project_id: str, action: str, body: dict) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/runtime/{action}", body)

    def for_generation(self, generation: int):
        return CairnClient(self._base_url, self._timeout, trust_env=self._trust_env,
                           headers={**self._headers, "X-Cairn-Generation": str(generation)})

    def set_generation(self, generation: int):
        self._headers["X-Cairn-Generation"] = str(generation)
        with self._sessions_lock:
            for session in self._sessions.values():
                session.headers["X-Cairn-Generation"] = str(generation)

    def submit_audit(self, body: dict) -> ApiResult:
        return self._request_json("POST", "/audit-submissions", body)

    def revise_submission(self, submission_id: str, body: dict) -> ApiResult:
        return self._request_json("PUT", f"/audit-submissions/{submission_id}", body)

    def get_submission(self, submission_id: str) -> dict:
        response = self._session().get(self._url(f"/audit-submissions/{submission_id}"), timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def get_execution_config(self, project_id: str) -> dict | None:
        response = self._session().get(self._url(f"/projects/{project_id}/execution-config"), timeout=self._timeout)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def create_audit(self, request: CreateAuditRequest) -> ApiResult:
        return self._request_json("POST", "/audits", request.model_dump(mode="json", by_alias=True))

    def create_project(self, request: CreateProjectRequest) -> ApiResult:
        return self._request_json("POST", "/projects", request.model_dump(mode="json"))

    def start_audit(self, project_id: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/start", {})

    def audit_budget(self, project_id: str, *, finalize=False) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/budget", {"finalize": finalize})

    def claim_closing(self, project_id: str, request: CompletionClaimRequest) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/closing/claim", request.model_dump(mode="json"))

    def finish_closing(self, project_id: str, request: ClosingReviewRequest) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/closing", request.model_dump(mode="json"))

    def submit_closing(self, project_id: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/closing/submit", {})

    def get_audit(self, project_id: str) -> AuditDetail:
        response = self._session().get(self._url(f"/projects/{project_id}/audit"), timeout=self._timeout)
        response.raise_for_status()
        return AuditDetail.model_validate(response.json())

    def record_audit_review(self, project_id: str, request: AuditRecordRequest) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/reviews", request.model_dump(mode="json"))

    def get_review_input(self, project_id: str, object_type: str, object_id: str) -> dict:
        response = self._session().get(self._url(f"/projects/{project_id}/audit/nodes/{object_type}/{object_id}"), timeout=self._timeout)
        response.raise_for_status()
        return response.json()

    def list_completions(self, project_id: str) -> list[AuditCompletion]:
        response = self._session().get(self._url(f"/projects/{project_id}/audit/completions"), timeout=self._timeout)
        response.raise_for_status()
        return [AuditCompletion.model_validate(item) for item in response.json()]

    def claim_completion(self, project_id: str, request_id: str, request: CompletionClaimRequest) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/completions/{request_id}/claim", request.model_dump(mode="json"))

    def review_completion(self, project_id: str, request_id: str, request: CompletionReviewRequest) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/audit/completions/{request_id}/review", request.model_dump(mode="json"))

    def export_project(self, project_id: str, *, format: str = "yaml") -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": format},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str, lease_id: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker, **({"lease_id": lease_id} if lease_id else {})},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str, lease_id: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker, **({"lease_id": lease_id} if lease_id else {})},
        )

    def release_reason(self, project_id: str, worker: str, lease_id: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker, **({"lease_id": lease_id} if lease_id else {})},
        )

    def release(self, project_id: str, intent_id: str, worker: str, lease_id: str | None = None) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker, **({"lease_id": lease_id} if lease_id else {})},
        )

    def set_project_status(self, project_id: str, status: str) -> ApiResult:
        return self._request_json("PUT", f"/projects/{project_id}/status", {"status": status})

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str, audit=None, lease_id=None) -> ApiResult:
        payload = {"worker": worker, "description": description}
        if audit is not None:
            payload["audit"] = audit.model_dump(mode="json")
        if lease_id is not None:
            payload["lease_id"] = lease_id
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json=payload,
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str, source_session: str | None = None, lease_id=None) -> ApiResult:
        payload = {"from": from_ids, "description": description, "worker": worker}
        if source_session is not None:
            payload["source_session"] = source_session
        if lease_id is not None:
            payload["lease_id"] = lease_id
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json=payload,
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        session.trust_env = self._trust_env
        session.headers.update(self._headers)
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
