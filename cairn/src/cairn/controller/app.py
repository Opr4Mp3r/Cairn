from __future__ import annotations

import json
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cairn.controller.models import AgentCallResult, FileReadResult
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.output_parser import extract_json_object
from cairn.dispatcher.runtime import execution
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.registry import get_driver


def call_agent(
    config: DispatchConfig,
    backend: ExecutionBackend,
    *,
    role: str,
    prompt: str,
    result_model: type[BaseModel],
    workspace: Path,
    timeout_seconds: int,
    phase: str,
    cancellation: TaskCancellation | None = None,
    session: str | None = None,
) -> AgentCallResult:
    if config.controller is None:
        raise ValueError("controller configuration is required")
    name = config.controller.roles.model_dump().get(role)
    if name is None:
        raise ValueError(f"unknown controller role: {role}")
    worker = next(worker for worker in config.workers if worker.name == name)
    driver = get_driver(worker.type, config.runtime.execution)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if not workspace.is_dir():
        raise ValueError(f"controller workspace does not exist: {workspace}")
    if config.runtime.execution == "local":
        binary = driver.local_binary()
        if binary is not None and shutil.which(binary) is None:
            raise ValueError(f"worker CLI is not on PATH: {binary}")

    if session is None:
        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        session = command.session or session
        argv = command.argv
    else:
        argv = driver.build_conclude(worker, prompt, session)
    started = time.monotonic()
    cancellation = cancellation or TaskCancellation()
    try:
        process = execution.run_worker_process(
            backend, str(workspace.resolve()), worker, argv,
            phase=phase, timeout_seconds=timeout_seconds, cancellation=cancellation,
        )
    except OSError as exc:
        process = ProcessResult(1, "", str(exc))

    session = driver.extract_session(session, process.stdout, process.stderr)
    result = AgentCallResult(
        role=role, worker=worker.name, phase=phase, workspace=str(workspace.resolve()),
        session=session, prompt=prompt, status="failed", process=process,
        elapsed_seconds=time.monotonic() - started,
    )
    if process.cancelled or cancellation.is_cancelled:
        result.status = "cancelled"
        result.error = process.cancel_reason or cancellation.reason or "cancelled"
    elif process.timed_out:
        result.status = "timed_out"
        result.error = "Agent 调用超时"
    elif process.returncode != 0:
        result.error = f"Agent 退出码为 {process.returncode}"
    else:
        try:
            result.response = driver.extract_response_text(process.stdout, process.stderr)
            payload = extract_json_object(result.response)
            result.data = result_model.model_validate(payload, strict=True).model_dump(mode="json")
        except (ValueError, TypeError) as exc:
            result.status = "invalid_output"
            result.error = str(exc)
        else:
            result.status = "success"
    return result


def file_probe_prompt(path: Path) -> str:
    instruction = resources.files("cairn.controller").joinpath("prompts/probe.md").read_text(encoding="utf-8")
    context: dict[str, Any] = {
        "target_file": str(path.resolve()),
        "output_schema": FileReadResult.model_json_schema(),
    }
    return instruction + "\n\n" + json.dumps(context, ensure_ascii=False, indent=2)
