from __future__ import annotations

from datetime import datetime, timezone
from importlib import resources
import json
from pathlib import Path
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cairn.audit_evidence import source_evidence
from cairn.audit_inputs import agent_view, material_paths, READING_GUIDANCE
from cairn.controller.app import call_agent
from cairn.controller.audit import _require_ok
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.output_parser import FENCED_BLOCK_RE
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.tasks.audit import research_workspace
from cairn.server.models import (
    AuditCompletion, AuditFactData, AuditRecordRequest, CompletionClaimRequest,
    CompletionReviewRequest, FinalAssessment, ClosingResult, ClosingReviewRequest, AuditClosing,
)


class NodeAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern=r"^(approved|rejected|needs_evidence)$")
    reason: str = Field(min_length=1)
    fact_audit: AuditFactData | None = None
    hint: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_verdict(self):
        if not self.reason.strip() or (self.hint is not None and not self.hint.strip()):
            raise ValueError("assessment reason and Hint must not be blank")
        expected = {"approved": "verified", "rejected": "refuted", "needs_evidence": "unverified"}[self.decision]
        if self.fact_audit is not None and self.fact_audit.review_status != expected:
            raise ValueError("fact state must match the assessment")
        return self


def _strict_result(text: str, model: type[BaseModel]) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate result field: {key}")
            result[key] = value
        return result

    segment = text.strip()
    fence = FENCED_BLOCK_RE.fullmatch(segment)
    if fence:
        segment = fence.group(1).strip()
    return model.model_validate(json.loads(segment, object_pairs_hook=unique), strict=True).model_dump(mode="json")


def _remaining(deadline: str) -> int:
    end = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    return int((end - datetime.now(timezone.utc)).total_seconds()) - 5


def _invoke(config, client, backend, project, *, phase, context, model, timeout, cancellation):
    if cancellation.is_cancelled:
        raise KeyboardInterrupt(cancellation.reason or "controller cancelled before Agent call")
    workspace = Path(research_workspace(config, client, backend, project))
    folder = workspace / "controller-reviews" / f"{phase}-{uuid.uuid4().hex}"
    folder.mkdir(parents=True)
    payload = {**agent_view(context), "repo_path": project.project.audit.repo_path,
               "materials": material_paths(workspace),
               "output_schema": model.model_json_schema(by_alias=True)}
    if phase == "overseer" and "audit" in context["target"]:
        payload["unsupported_premise_ids"] = [
            fact_id for fact_id, fact in context.get("premises", {}).items()
            if fact["audit"]["review_status"] != "verified"
        ]
        try:
            data = AuditFactData.model_validate(context["target"]["audit"])
            checked = source_evidence(Path(project.project.audit.repo_path), data.evidence, project.project.audit.baseline)
            fixed = project.project.audit.baseline.get("files", {})
            if not checked or any(fixed.get(ref.path) != ref.sha256 for ref in checked):
                raise ValueError("引用不足或未纳入固定基线，不能据此确认")
            payload["reference_check"] = {"status": "matched", "evidence": [ref.model_dump() for ref in checked]}
        except (OSError, ValueError, RuntimeError) as exc:
            payload["reference_check"] = {"status": "needs_evidence", "error": str(exc)}
    instruction = resources.files("cairn.controller").joinpath(f"prompts/{phase}.md").read_text(encoding="utf-8")
    instruction += "\n" + READING_GUIDANCE
    prompt = instruction + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    (folder / "input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if cancellation.is_cancelled:
            raise KeyboardInterrupt(cancellation.reason or "controller cancelled before Agent call")
        result = call_agent(config, backend, role="builder" if phase == "closing" else "reviewer", prompt=prompt, result_model=model,
                            workspace=workspace, timeout_seconds=timeout, phase=phase, cancellation=cancellation)
        if result.status == "cancelled":
            cancellation.cancel(result.error or result.process.cancel_reason or "Agent call interrupted")
        if result.status == "success":
            try:
                result.data = _strict_result(result.response, model)
                if phase == "overseer":
                    is_fact = "audit" in context["target"]
                    if is_fact != (result.data["fact_audit"] is not None):
                        raise ValueError("fact reviews require metadata; Intent reviews must not mutate facts")
            except (ValueError, TypeError) as exc:
                result.status, result.error, result.data = "invalid_output", str(exc), None
        artifact = folder / "result.json"
        artifact.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        if not cancellation.is_cancelled:
            research_workspace(config, client, backend, client.get_project(project.project.id))
        return result, artifact
    except BaseException as exc:
        (folder / "failure.json").write_text(json.dumps({"error": str(exc)}, ensure_ascii=False), encoding="utf-8")
        raise


def monitor_once(config: DispatchConfig, client, backend, project_id: str, *, cancellation=None, retry_review: str | None = None) -> dict:
    if config.controller is None or config.runtime.execution != "local":
        raise ValueError("audit monitor requires controller configuration and local execution")
    cancellation = cancellation or TaskCancellation()
    if cancellation.is_cancelled:
        return {"project_id": project_id, "status": "cancelled", "reviewed": 0}
    project = client.get_project(project_id)
    if project.project.audit is None:
        raise ValueError("project is not an audit")
    if project.project.status in ("active", "draining"):
        _require_ok(client.audit_budget(project_id), "advance audit budget")
        project = client.get_project(project_id)
    if project.project.status not in ("active", "draining"):
        return {"project_id": project_id, "status": project.project.status, "reviewed": 0}
    research_workspace(config, client, backend, project)
    reviewed = 0
    targets = [("fact", fact.id) for fact in project.facts if fact.id not in ("origin", "goal")]
    if project.project.audit.phase != "finalizing":
        targets += [("intent", intent.id) for intent in project.intents if intent.to is None]
    for kind, object_id in targets:
        if cancellation.is_cancelled:
            break
        timeout = min(config.controller.timeouts.overseer, _remaining(project.project.audit.final_deadline))
        if timeout <= 0:
            break
        inputs = client.get_review_input(project_id, kind, object_id)
        records = [record for record in client.get_audit(project_id).records
                   if record.phase == "overseer" and record.object_type == kind and record.object_id == object_id]
        if any(record.attempt_status == "success" and inputs["input_digest"] in (record.input_digest, record.output_digest) for record in records):
            continue
        attempts = sum(record.input_digest == inputs["input_digest"] for record in records)
        if attempts >= config.controller.review.max_attempts:
            continue
        result, artifact = _invoke(config, client, backend, project, phase="overseer", context=inputs["context"],
                                   model=NodeAssessment, timeout=timeout, cancellation=cancellation)
        success = result.status == "success"
        data = NodeAssessment.model_validate(result.data) if success else None
        body = AuditRecordRequest(
            record_key=f"overseer:{kind}:{object_id}:{inputs['input_digest']}:{attempts + 1}",
            object_type=kind, object_id=object_id, input_digest=inputs["input_digest"], phase="overseer",
            session=result.session, attempt_status="success" if success else "error",
            decision=data.decision if data else None, error=None if success else result.error or result.status,
            result=result.data, artifacts=[str(artifact)], fact_audit=data.fact_audit if data else None, hint=data.hint if data else None,
        )
        response = client.record_audit_review(project_id, body)
        if response.status_code in (400, 409, 422):
            current = client.get_review_input(project_id, kind, object_id)
            if current["input_digest"] != inputs["input_digest"]:
                continue  # 图在执行期变化，下一轮读取新输入，不复用旧判断。
            body = body.model_copy(update={"attempt_status": "error", "decision": None, "fact_audit": None,
                                          "hint": None, "error": f"复核结果未满足服务端证据约束：{response.text}"})
            response = client.record_audit_review(project_id, body)
        if response.status_code == 409 and _remaining(project.project.audit.final_deadline) <= 0:
            break
        _require_ok(response, "apply Overseer review")
        reviewed += 1
        project = client.get_project(project_id)

    if cancellation.is_cancelled:
        return {"project_id": project_id, "status": "cancelled", "reviewed": reviewed}
    _require_ok(client.audit_budget(project_id), "advance audit budget")
    project = client.get_project(project_id)
    if project.project.status not in ("active", "draining"):
        return {"project_id": project_id, "status": project.project.status, "reviewed": reviewed}
    if project.project.reason is not None or any(intent.worker is not None and intent.to is None for intent in project.intents):
        return {"project_id": project_id, "status": project.project.status, "reviewed": reviewed, "waiting": "existing research leases"}
    if project.project.audit.phase == "finalizing":
        _closing_once(config, client, backend, project, cancellation)
        if cancellation.is_cancelled:
            return {"project_id": project_id, "status": "cancelled", "reviewed": reviewed}
    requests = client.list_completions(project_id)
    retry_consumed = None
    for request in requests:
        if cancellation.is_cancelled:
            break
        explicit_retry = request.id == retry_review
        if request.status in ("approved", "rejected", "needs_evidence"):
            continue
        response = client.claim_completion(project_id, request.id, CompletionClaimRequest(
            owner=uuid.uuid4().hex, max_attempts=config.controller.review.max_attempts,
            timeout_seconds=config.controller.timeouts.final_review, retry=explicit_retry,
        ))
        if response.status_code == 409:
            continue
        request = AuditCompletion.model_validate(_require_ok(response, "claim final review"))
        if explicit_retry:
            retry_consumed = request.id
        timeout = min(config.controller.timeouts.final_review, _remaining(request.attempt_deadline))
        if timeout <= 0:
            continue
        project = client.get_project(project_id)
        result, artifact = _invoke(config, client, backend, project, phase="final_review",
                                   context={"request": request.model_dump(mode="json", by_alias=True),
                                            "graph": project.model_dump(mode="json", by_alias=True),
                                            "policy": project.project.audit.policy}, model=FinalAssessment,
                                   timeout=timeout, cancellation=cancellation)
        success = result.status == "success"
        assessment = FinalAssessment.model_validate(result.data) if success else None
        record = AuditRecordRequest(
            record_key=f"final_review:{request.id}:{request.review_token}", object_type="project", object_id=project_id,
            input_digest=request.proof_digest, phase="final_review", session=result.session,
            attempt_status="success" if success else "error", decision=assessment.decision if assessment else None,
            error=None if success else result.error or result.status, result=result.data, artifacts=[str(artifact)],
        )
        response = client.review_completion(project_id, request.id, CompletionReviewRequest(
            token=request.review_token, expected_audit_revision=request.audit_revision,
            policy_digest=request.policy_digest, proof_digest=request.proof_digest, record=record, assessment=assessment,
        ))
        if response.status_code in (400, 409, 422):
            error_record = record.model_copy(update={"attempt_status": "error", "decision": None,
                                                    "error": f"终审结果未获服务端接受：{response.text}"})
            response = client.review_completion(project_id, request.id, CompletionReviewRequest(
                token=request.review_token, expected_audit_revision=request.audit_revision,
                policy_digest=request.policy_digest, proof_digest=request.proof_digest, record=error_record,
            ))
            if response.status_code == 409:
                continue
        _require_ok(response, "submit final review")
        reviewed += 1
        if client.get_project(project_id).project.status not in ("active", "draining"):
            break
    if not cancellation.is_cancelled:
        _require_ok(client.audit_budget(project_id, finalize=True), "settle audit budget")
    return {"project_id": project_id, "status": "cancelled" if cancellation.is_cancelled else client.get_project(project_id).project.status,
            "reviewed": reviewed, "retry_review_consumed": retry_consumed,
            "note": "Research budget, persisted closing and independent review keep the original deadlines."}


def monitor_audit(config, client, backend, project_id: str, *, once=False, cancellation=None, retry_review=None, report=None) -> dict:
    cancellation = cancellation or TaskCancellation()
    while not cancellation.is_cancelled:
        result = monitor_once(config, client, backend, project_id, cancellation=cancellation, retry_review=retry_review)
        if result.get("retry_review_consumed") == retry_review:
            retry_review = None
        if report:
            report(result)
        if cancellation.is_cancelled or once or result["status"] not in ("active", "draining"):
            return result
        time.sleep(config.controller.poll_interval)
    return {"project_id": project_id, "status": "cancelled"}


def _closing_once(config, client, backend, project, cancellation):
    project_id = project.project.id
    response = client.claim_closing(project_id, CompletionClaimRequest(
        owner=uuid.uuid4().hex, timeout_seconds=config.controller.timeouts.closing,
        max_attempts=config.controller.review.max_attempts,
    ))
    if response.status_code == 409:
        return
    closing = AuditClosing.model_validate(_require_ok(response, "claim read-only closing"))
    if closing.status != "success":
        timeout = min(config.controller.timeouts.closing, _remaining(closing.attempt_deadline))
        if timeout <= 0:
            return
        project = client.get_project(project_id)
        result, artifact = _invoke(config, client, backend, project, phase="closing",
                                   context={"graph": project.model_dump(mode="json", by_alias=True),
                                            "policy": project.project.audit.policy},
                                   model=ClosingResult, timeout=timeout, cancellation=cancellation)
        success = result.status == "success"
        record = AuditRecordRequest(
            record_key=f"closing:{closing.token}", object_type="project", object_id=project_id,
            input_digest=closing.input_digest, phase="closing", session=result.session,
            attempt_status="success" if success else "error", error=None if success else result.error or result.status,
            result=result.data, artifacts=[str(artifact)],
        )
        body = ClosingReviewRequest(token=closing.token, input_digest=closing.input_digest, record=record,
                                    result=ClosingResult.model_validate(result.data) if success else None)
        response = client.finish_closing(project_id, body)
        if response.status_code in (400, 409, 422):
            body = body.model_copy(update={"result": None, "record": record.model_copy(update={
                "attempt_status": "error", "error": f"结案结果未获服务端接受：{response.text}",
            })})
            response = client.finish_closing(project_id, body)
            if response.status_code == 409:
                return
        closing = AuditClosing.model_validate(_require_ok(response, "persist closing result"))
    if not cancellation.is_cancelled and closing.status == "success" and closing.result.kind == "candidate" and not closing.request_id:
        _require_ok(client.submit_closing(project_id), "submit persisted closing candidate")
