from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
import json
import logging
from pathlib import Path
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from cairn.audit_inputs import (
    baseline_identity, fact_view, intent_view, material_paths, model_output_schema, project_contract, READING_GUIDANCE,
)
from cairn.controller.app import call_agent
from cairn.controller.audit import _require_ok
from cairn.controller.pool import run_pooled
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.output_parser import FENCED_BLOCK_RE
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.tasks.audit import research_workspace
from cairn.dispatcher.tasks.common import write_graph_snapshot_reference
from cairn.server.audit_review import premise_closure
from cairn.server.models import (
    AuditCompletion, AuditRecordRequest, CompletionClaimRequest,
    CompletionReviewRequest, FinalAssessment, ClosingResult, ClosingReviewRequest, AuditClosing,
)

LOG = logging.getLogger(__name__)


class NodeAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern=r"^(approved|rejected|needs_evidence)$")
    reason: str = Field(min_length=1)
    fact_audit: None = None
    hint: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_verdict(self):
        if not self.reason.strip() or (self.hint is not None and not self.hint.strip()):
            raise ValueError("assessment reason and Hint must not be blank")
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


def _budget_remaining(client, project, *, request_id=None, closing=False):
    if project.project.runtime_generation is None:
        return None
    data = client.get_runtime(project.project.id)
    if closing:
        value = data["closing_remaining_seconds"]
    elif request_id:
        value = data["review_remaining_seconds"][request_id]
    else:
        value = data["total_remaining_seconds"]
    return max(0, int(value) - 5) if value is not None else 0


def _review_payload(backend, workspace, project, phase, context):
    graph = project.model_dump(mode="json", by_alias=True)
    payload = {
        "fixed_contract": project_contract(graph),
        "graph_reference": write_graph_snapshot_reference(
            backend, str(workspace), yaml.safe_dump(graph, allow_unicode=True, sort_keys=False), phase=phase, audit=True,
        ),
    }
    if phase == "overseer":
        target = context["target"]
        payload["target"] = intent_view({
            **target,
            "to": target.get("to_fact_id"),
            "from": next((intent.from_ for intent in project.intents if intent.id == target["id"]), []),
        })
    elif phase == "final_review":
        request = context.get("request", {})
        payload["request"] = {key: request[key] for key in ("id", "from", "description") if key in request}
        facts = {fact.id: {"id": fact.id, "description": fact.description, "audit": fact.audit} for fact in project.facts}
        closure = premise_closure(facts, request.get("from", []))
        payload["proof_facts"] = {key: fact_view({**fact, "audit": fact["audit"].model_dump(mode="json")})
                                  for key, fact in closure.items()}
    return payload


def _invoke(config, client, backend, project, *, phase, context, model, timeout, cancellation, worker=None):
    if cancellation.is_cancelled:
        raise KeyboardInterrupt(cancellation.reason or "controller cancelled before Agent call")
    if phase == "overseer":
        metadata = project.project.audit
        if not metadata.workspace_path or not Path(metadata.workspace_path).is_dir():
            raise ValueError("Intent supervision requires the activated project workspace")
        workspace = Path(metadata.workspace_path)
    else:
        workspace = Path(research_workspace(config, client, backend, project))
    folder = workspace / "controller-reviews"
    if project.project.runtime_generation is not None:
        folder /= f"generation-{project.project.runtime_generation:03d}"
    folder /= f"{phase}-{uuid.uuid4().hex}"
    folder.mkdir(parents=True)
    (folder / "raw-context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    materials = material_paths(workspace)
    payload = {**_review_payload(backend, workspace, project, phase, context), "repo_path": project.project.audit.repo_path,
               "baseline": baseline_identity(project.project.audit.baseline),
               "materials": materials,
               "query_receipts": {
                   "opening_ledger": materials.get("query-receipts.json"),
                   "research_directory": str(workspace / "research" / "kritt-queries"),
                   "view_context": str(workspace / "kritt-context.json") if (workspace / "kritt-context.json").is_file() else None,
               },
               "output_schema": model_output_schema(model.model_json_schema(by_alias=True))}
    instruction = resources.files("cairn.controller").joinpath(f"prompts/{phase}.md").read_text(encoding="utf-8")
    instruction += "\n" + READING_GUIDANCE
    instruction += ("\nquery_receipts.opening_ledger 只含开题查询，不是完整研究账本。研究期已发生的查询在 "
                    "research_directory/q-*/，每次保存 request.json、result.json、receipts.json，get 另有 materials.json；"
                    "引用研究回执时到该目录按 receipt_id、参数和 view_digest 定位，不能仅因开题账本没有就判定不存在。"
                    "view_context 标识本次固定视图。复核只读取已有文件，不运行新查询或展开新探索。")
    prompt = instruction + "\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    (folder / "input.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if cancellation.is_cancelled:
            raise KeyboardInterrupt(cancellation.reason or "controller cancelled before Agent call")
        result = call_agent(config, backend, role="builder" if phase == "closing" else "reviewer", prompt=prompt, result_model=model,
                            workspace=workspace, timeout_seconds=timeout, phase=phase, cancellation=cancellation,
                            **({"worker": worker} if worker is not None else {}))
        if phase != "overseer" and result.status == "cancelled":
            cancellation.cancel(result.error or result.process.cancel_reason or "Agent call interrupted")
        if result.status == "success":
            try:
                result.data = _strict_result(result.response, model)
            except (ValueError, TypeError) as exc:
                result.status, result.error, result.data = "invalid_output", str(exc), None
        artifact = folder / "result.json"
        artifact.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        if phase != "overseer" and not cancellation.is_cancelled:
            research_workspace(config, client, backend, client.get_project(project.project.id))
        return result, artifact
    except BaseException as exc:
        (folder / "failure.json").write_text(json.dumps({"error": str(exc)}, ensure_ascii=False), encoding="utf-8")
        raise


def _can_supervise(project):
    return (project.project.status == "active" and project.project.audit.phase == "researching"
            and not project.project.audit.completion_pending)


def _review_items(config, client, project_id, targets, cancellation):
    for object_id in targets:
        if cancellation.is_cancelled:
            return
        project = client.get_project(project_id)
        if not _can_supervise(project):
            return
        if not any(intent.id == object_id and intent.to is None for intent in project.intents):
            continue
        timeout = min(config.controller.timeouts.overseer,
                      _budget_remaining(client, project) if project.project.runtime_generation is not None else _remaining(project.project.audit.final_deadline))
        if timeout <= 0:
            return
        inputs = client.get_review_input(project_id, "intent", object_id)
        if inputs["context"]["target"].get("to_fact_id") is not None:
            continue
        records = [record for record in client.get_audit(project_id).records
                   if record.phase == "overseer" and record.object_type == "intent" and record.object_id == object_id]
        if any(record.attempt_status == "success" and inputs["input_digest"] in (record.input_digest, record.output_digest) for record in records):
            continue
        attempts = sum(record.input_digest == inputs["input_digest"] and (project.project.runtime_generation is None or
                       record.record_key.startswith(f"overseer:g{project.project.runtime_generation}:")) for record in records)
        if attempts < config.controller.review.max_attempts:
            yield object_id, inputs, attempts, project, timeout


def _node_reviews(config, client, backend, project, targets, cancellation):
    project_id = project.project.id

    def invoke(item, worker, child):
        object_id, inputs, _, _, timeout = item
        try:
            current = client.get_project(project_id)
            if (child.is_cancelled or not _can_supervise(current)
                    or not any(intent.id == object_id and intent.to is None for intent in current.intents)):
                return None, None
            return _invoke(config, client, backend, current, phase="overseer", context=inputs["context"],
                           model=NodeAssessment, timeout=timeout, cancellation=child,
                           **({"worker": worker} if worker is not None else {}))
        except Exception as exc:
            LOG.warning("Intent supervision failed project=%s intent=%s: %s", project_id, object_id, exc)
            return exc, None

    items = _review_items(config, client, project_id, targets, cancellation)
    if not config.role_pools:
        for item in items:
            yield item, invoke(item, None, cancellation)
        return
    with closing(run_pooled(config, "overseer", items, invoke, cancellation)) as results:
        yield from results


def _supervise_once(config, client, backend, project_id, cancellation):
    project = client.get_project(project_id)
    if not _can_supervise(project):
        return 0
    targets = [intent.id for intent in project.intents if intent.to is None]
    reviewed = 0
    with closing(_node_reviews(config, client, backend, project, targets, cancellation)) as results:
        for item, (result, artifact) in results:
            if cancellation.is_cancelled:
                break
            if result is None:
                continue
            object_id, inputs, attempts, _, _ = item
            project = client.get_project(project_id)
            if not _can_supervise(project):
                break
            if not any(intent.id == object_id and intent.to is None for intent in project.intents):
                continue
            failed = isinstance(result, Exception)
            success = not failed and result.status == "success"
            data = NodeAssessment.model_validate(result.data) if success else None
            body = AuditRecordRequest(
                record_key=(f"overseer:g{project.project.runtime_generation}:intent:{object_id}:{inputs['input_digest']}:{attempts + 1}"
                            if project.project.runtime_generation is not None else f"overseer:intent:{object_id}:{inputs['input_digest']}:{attempts + 1}"),
                object_type="intent", object_id=object_id, input_digest=inputs["input_digest"], phase="overseer",
                session=None if failed else result.session, worker=None if failed else result.worker,
                harness=None if failed else result.harness, attempt_status="success" if success else "error",
                decision=data.decision if data else None,
                error=str(result) if failed else None if success else result.error or result.status,
                result=None if failed else result.data, artifacts=[str(artifact)] if artifact else [], hint=data.hint if data else None,
            )
            response = client.record_audit_review(project_id, body)
            if response.status_code in (400, 409, 422):
                current = client.get_project(project_id)
                if not _can_supervise(current):
                    break
                current_input = client.get_review_input(project_id, "intent", object_id)
                if (current_input["input_digest"] != inputs["input_digest"]
                        or current_input["context"]["target"].get("to_fact_id") is not None):
                    continue
                body = body.model_copy(update={"attempt_status": "error", "decision": None, "hint": None,
                                              "error": f"纠偏结果未获服务端接受：{response.text}"})
                response = client.record_audit_review(project_id, body)
            _require_ok(response, "record Intent supervision")
            reviewed += 1
    return reviewed


def monitor_once(config: DispatchConfig, client, backend, project_id: str, *, cancellation=None, retry_review=None) -> dict:
    """Advance budget, Closing and Final Review without waiting for Overseer."""
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
    reviewed = 0
    if project.project.reason is not None or any(intent.worker is not None and intent.to is None for intent in project.intents):
        return {"project_id": project_id, "status": project.project.status, "reviewed": reviewed, "waiting": "existing research leases"}
    reviewed, retry_consumed = _final_reviews(config, client, backend, project, cancellation, retry_review)
    project = client.get_project(project_id)
    if not cancellation.is_cancelled and project.project.status in ("active", "draining") and project.project.audit.phase == "finalizing":
        _closing_once(config, client, backend, project, cancellation)
        if not cancellation.is_cancelled:
            count, consumed = _final_reviews(config, client, backend, client.get_project(project_id), cancellation,
                                             None if retry_consumed else retry_review)
            reviewed += count
            retry_consumed = consumed or retry_consumed
    if not cancellation.is_cancelled:
        _require_ok(client.audit_budget(project_id, finalize=True), "settle audit budget")
    return {"project_id": project_id, "status": "cancelled" if cancellation.is_cancelled else client.get_project(project_id).project.status,
            "reviewed": reviewed, "retry_review_consumed": retry_consumed,
            "note": "Research budget, persisted closing and independent review keep the original deadlines."}


def _final_reviews(config, client, backend, project, cancellation, retry_review):
    project_id = project.project.id
    reviewed = 0
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
        timeout = min(config.controller.timeouts.final_review,
                      _budget_remaining(client, project, request_id=request.id) if project.project.runtime_generation is not None else _remaining(request.attempt_deadline))
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
            input_digest=request.proof_digest, phase="final_review", session=result.session, worker=result.worker, harness=result.harness,
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
    return reviewed, retry_consumed


def monitor_audit(config, client, backend, project_id: str, *, once=False, cancellation=None, retry_review=None, report=None) -> dict:
    if config.controller is None or config.runtime.execution != "local":
        raise ValueError("audit monitor requires controller configuration and local execution")
    cancellation = cancellation or TaskCancellation()
    # The controller token owns Closing/Final calls; the side channel has its own
    # token because TaskCancellation can attach only one process at a time.
    supervision_cancel = TaskCancellation()
    future = None
    started = False
    reviewed = 0
    retry_consumed = None
    result = {"project_id": project_id, "status": "cancelled", "reviewed": 0}
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cairn-supervision")
    try:
        while not cancellation.is_cancelled:
            result = monitor_once(config, client, backend, project_id,
                                   cancellation=cancellation, retry_review=retry_review)
            reviewed += result["reviewed"]
            if result.get("retry_review_consumed") is not None:
                retry_consumed = result["retry_review_consumed"]
                retry_review = None
            if future is not None and future.done():
                try:
                    reviewed += future.result()
                except BaseException as exc:
                    LOG.warning("Intent supervision batch failed project=%s: %s", project_id, exc)
                    result["supervision_error"] = str(exc)
                future = None
            result.update(reviewed=reviewed, retry_review_consumed=retry_consumed)
            done = (cancellation.is_cancelled or result["status"] not in ("active", "draining")
                    or (once and started and future is None))
            if report and (not once or done):
                report(result)
            if done:
                return result
            if future is None:
                supervision_cancel = TaskCancellation()
                future = executor.submit(_supervise_once, config, client, backend, project_id, supervision_cancel)
                started = True
            end = time.monotonic() + config.controller.poll_interval
            while not cancellation.is_cancelled and time.monotonic() < end:
                if once and future is not None and future.done():
                    break
                time.sleep(min(.1, max(0, end - time.monotonic())))
        return {**result, "status": "cancelled", "reviewed": reviewed}
    finally:
        try:
            supervision_cancel.cancel(cancellation.reason or "audit monitor stopped")
        except Exception:
            LOG.exception("Intent supervision cancellation failed project=%s", project_id)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)


def _closing_once(config, client, backend, project, cancellation):
    project_id = project.project.id
    response = client.claim_closing(project_id, CompletionClaimRequest(
        owner=uuid.uuid4().hex, timeout_seconds=config.controller.timeouts.closing,
        max_attempts=config.controller.review.max_attempts,
    ))
    if response.status_code == 409:
        return
    closing = AuditClosing.model_validate(_require_ok(response, "claim read-only closing"))
    reused_result = closing.status == "success"
    if closing.status != "success":
        timeout = min(config.controller.timeouts.closing,
                      _budget_remaining(client, project, closing=True) if project.project.runtime_generation is not None else _remaining(closing.attempt_deadline))
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
            input_digest=closing.input_digest, phase="closing", session=result.session, worker=result.worker, harness=result.harness,
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
        if reused_result:
            research_workspace(config, client, backend, client.get_project(project_id))
        _require_ok(client.submit_closing(project_id), "submit persisted closing candidate")
