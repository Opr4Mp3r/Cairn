from __future__ import annotations

import logging
from contextlib import suppress

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG = logging.getLogger(__name__)


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def run_worker_process(
    container_manager: ExecutionBackend,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> ProcessResult:
    if cancellation is not None and cancellation.is_cancelled:
        return ProcessResult(130, "", "", cancelled=True, cancel_reason=cancellation.reason)
    LOG.info(
        "starting worker process handle=%s worker=%s phase=%s timeout=%ss",
        container_name, worker.name, phase, timeout_seconds,
    )
    process = container_manager.build_exec_process(
        container_name, dict(worker.env), argv, timeout_seconds=timeout_seconds,
    )
    started = False
    try:
        process.start()
        started = True
        if lease is not None:
            lease.attach_process(process)
        if cancellation is not None:
            cancellation.attach_process(process)
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    except KeyboardInterrupt as exc:
        reason = str(exc) or "worker execution interrupted"
        process.cancel(reason)
        if not started:
            return ProcessResult(130, "", "", cancelled=True, cancel_reason=reason)
        # 取消后收集同一进程，保留已经产生的 stdout 和 stderr。
        result = process.communicate(timeout=PROCESS_COMMUNICATE_GRACE_SECONDS)
        result.cancelled = True
        result.cancel_reason = result.cancel_reason or reason
        return result
    except BaseException:
        with suppress(Exception):
            process.cancel("worker execution interrupted")
        raise
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
