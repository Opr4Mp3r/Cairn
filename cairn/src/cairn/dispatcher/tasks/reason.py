from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_reason_payload
from cairn.dispatcher.prompting import (
    format_fact_ids,
    format_open_intents,
    load_prompt,
    render_prompt,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.execution import run_worker_process
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    did_timeout,
    preview,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.dispatcher.tasks.audit import research_payload, research_prompt, research_receipt, research_workspace, reason_result
from cairn.dispatcher.tasks.reason_memory import load_memory, save_memory
from cairn.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_reason_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type, config.runtime.execution)
    attempt_started_ns = time.monotonic_ns()
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease_id = project.project.reason.lease_id if project.project.reason else None
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval, lease_id)
    container_name = receipt_path = planning_notes = None

    def finish(outcome, error=None, *, memory_outcome=None):
        if container_name is not None:
            save_memory(container_manager, container_name, client, project, worker, cancellation, lease,
                        outcome=memory_outcome or outcome, error=error, planning_notes=planning_notes,
                        receipt_path=receipt_path, attempt_started_ns=attempt_started_ns)
        return outcome

    lease.start()
    try:
        container_name = research_workspace(config, client, container_manager, project)

        if task_healthcheck_enabled(config):
            LOG.info(
                "checking worker health project=%s worker=%s timeout=%ss",
                project.project.id,
                worker.name,
                healthcheck_timeout,
            )
            health = driver.check_health(worker, timeout=healthcheck_timeout)
            if cancellation.is_cancelled:
                LOG.info(
                    "reason cancelled during healthcheck project=%s worker=%s reason=%s",
                    project.project.id,
                    worker.name,
                    cancellation.reason,
                )
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during reason healthcheck project=%s worker=%s status=%s",
                    project.project.id,
                    worker.name,
                    lease.failure.status_code,
                )
                return "failed"
            if not health.ok:
                LOG.warning(
                    "worker unhealthy project=%s worker=%s status=%s detail=%s",
                    project.project.id,
                    worker.name,
                    health.status,
                    health.detail,
                )
                return finish("unhealthy", f"healthcheck: {health.status}: {health.detail}")
        open_intents = [
            {
                "id": intent.id,
                "from": intent.from_,
                "description": intent.description,
                "worker": intent.worker,
            }
            for intent in project.intents
            if intent.to is None
        ]
        allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
        memory = load_memory(project, container_name)
        LOG.debug(
            "reason context prepared project=%s worker=%s facts=%s allowed_fact_ids=%s hints=%s open_intents=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_intents),
        )
        prompt = research_prompt(
            config, project, "reason.md",
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="reason_execute",
                    audit=project.project.mode == "audit",
                ),
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_intents": ("见 Graph navigation 的 Open Intents：问题预览、来源、占用状态和全文位置。"
                                 if project.project.mode == "audit" else format_open_intents(open_intents)),
                "max_intents": str(config.tasks.reason.max_intents),
            },
            reason_memory=memory,
        )

        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="reason_execute",
            timeout_seconds=config.tasks.reason.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        receipt_path = research_receipt(container_manager, container_name, project, worker, phase="reason_execute", session=session, prompt=prompt, process=result)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return finish("failed", "execution_timeout: Reason execution timed out")
        if result.returncode != 0:
            LOG.warning(
                "reason command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return finish("failed", f"command_failed: exit {result.returncode}: {preview(result.stderr)}")
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = research_payload(project, model_output)
            kind, data = reason_result(
                project, payload, open_intents_empty=not open_intents, max_intents=config.tasks.reason.max_intents,
            )
            if project.project.mode == "audit" and isinstance(payload.get("data"), dict):
                planning_notes = payload["data"].get("planning_notes")
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return finish("failed", f"invalid_output: {exc}")
        if kind == "rejected":
            LOG.warning(
                "reason rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return finish("rejected", payload.get("reason", "Reason rejected this task"))
        if kind == "complete":
            kwargs = {"source_session": session} if project.project.mode == "audit" else {}
            if lease_id:
                kwargs["lease_id"] = lease_id
            response = client.complete(project.project.id, data["from"], data["description"], worker.name, harness=worker.type, **kwargs)
            if response.status_code == 403:
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if not response.ok:
                LOG.warning(
                    "reason complete write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                return finish("failed", f"complete_write_failed: {response.status_code}: {response.text}")
            LOG.info(
                "completion request processed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                data["from"],
                execute_ms,
                total_ms,
            )
            return finish("success")
        if kind == "intents":
            created = 0
            write_errors = []
            for intent_data in data:
                response = client.create_intent(project.project.id, intent_data["from"], intent_data["description"], worker.name, harness=worker.type)
                if response.status_code == 403:
                    LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                    write_errors.append(f"HTTP 409: {response.text}; Intent: {intent_data['description']!r}")
                    continue
                if not response.ok:
                    LOG.warning(
                        "reason intent write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
                    write_errors.append(f"HTTP {response.status_code}: {response.text}; Intent: {intent_data['description']!r}")
                    continue
                created += 1
                LOG.info(
                    "reason created intent project=%s worker=%s from=%s description=%s",
                    project.project.id,
                    worker.name,
                    intent_data["from"],
                    intent_data["description"],
                )
            LOG.info(
                "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                len(data),
                execute_ms,
                total_ms,
            )
            if created == 0:
                LOG.warning(
                    "reason created no intents project=%s worker=%s attempted=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    len(data),
                    execute_ms,
                    total_ms,
                )
                return finish("failed", "intent_write_failed: no requested Intent was created; " + "; ".join(write_errors))
            if write_errors:
                return finish("success", f"intent_write_failed: created {created}/{len(data)}; " + "; ".join(write_errors),
                              memory_outcome="failed")
            return finish("success")
        LOG.info(
            "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return finish("success")
    except Exception as exc:
        finish("failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name, lease_id)
