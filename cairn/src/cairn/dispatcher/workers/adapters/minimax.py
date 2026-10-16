from __future__ import annotations

import json
import re
from collections.abc import Iterator

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver
from cairn.dispatcher.workers.health import HealthResult


class MiniMaxDriver(WorkerDriver):
    """Local MiniMax Code, using the versioned mcode exec JSONL contract."""

    type_name = "minimax"

    def local_binary(self) -> str | None:
        return "mcode"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        return HealthResult(ok=False, status=None, detail=self.describe_health(worker))

    def describe_health(self, worker: WorkerConfig) -> str:
        return "MiniMax Code uses host CLI configuration; automatic API health checks are unavailable"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._argv(prompt, session), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._argv(prompt, session)

    @staticmethod
    def _argv(prompt: str, session: str | None) -> list[str]:
        argv = ["mcode", "exec", "--output-format", "stream-json", "--permission", "full"]
        if session is not None:
            argv.extend(["--session", session])
        return [*argv, "--", prompt]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        # The first complete start event survives a timeout with a truncated tail.
        for event in _events(stdout, strict=False):
            if event.get("type") in ("exec.started", "session.started", "session.resumed"):
                identity = _identity(event)
                if identity is not None:
                    return identity[1]
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        identity = None
        result = None
        for event in _events(stdout):
            if result is not None:
                raise ValueError("MiniMax Code emitted events after exec.completed")
            event_identity = _identity(event)
            if event_identity is None or (identity is not None and event_identity != identity):
                raise ValueError("MiniMax Code output has invalid or inconsistent execution identity")
            identity = event_identity
            if event.get("type") == "turn.failed":
                raise ValueError(f"MiniMax Code turn failed: {event.get('status')}")
            if event.get("type") != "exec.completed":
                continue
            result = event.get("result")
            if not isinstance(result, dict) or result.get("type") != "exec.result" or _identity(result) != identity:
                raise ValueError("MiniMax Code output has an invalid terminal result")
            if result.get("status") != "succeeded":
                raise ValueError(f"MiniMax Code did not succeed: {result.get('status')}")
        if result is None:
            raise ValueError("MiniMax Code output has no completed result")
        output = result.get("output")
        if not isinstance(output, str):
            raise ValueError("MiniMax Code completed without final answer text")
        # Machine output can retain the reasoning prefix hidden by mcode's text renderer.
        # Only consume leading blocks; tags inside the final JSON are ordinary data.
        while match := re.match(r"\A\s*<(think|mavis-thinking)(?=\s|>|$)", output, re.IGNORECASE):
            opening_end = output.find(">", match.end())
            closing = re.search(rf"</{match[1]}\s*>", output, re.IGNORECASE)
            if opening_end < 0 or closing is None or closing.start() < opening_end:
                raise ValueError("MiniMax Code output contains an unclosed reasoning prefix")
            output = output[closing.end():].lstrip()
        if not output.strip():
            raise ValueError("MiniMax Code completed without final answer text")
        return output


def _identity(event: dict) -> tuple[str, str, str] | None:
    if type(event.get("schemaVersion")) is not int or event["schemaVersion"] != 1:
        return None
    fields = tuple(event.get(key) for key in ("runId", "sessionId", "turnId"))
    if not all(isinstance(value, str) and value.strip() for value in fields):
        return None
    return fields


def _events(stdout: str, *, strict: bool = True) -> Iterator[dict]:
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if strict:
                raise ValueError("MiniMax Code output must contain complete JSON events") from None
            continue
        if isinstance(event, dict):
            yield event
        elif strict:
            raise ValueError("MiniMax Code output events must be JSON objects")
