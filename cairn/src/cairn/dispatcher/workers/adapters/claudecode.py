from __future__ import annotations

import json

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


ANTHROPIC_VERSION = "2023-06-01"
FINAL_JSON_INSTRUCTION = (
    "For this non-interactive invocation, your final answer must be exactly one JSON object matching the requested schema. "
    "Do not put prose, progress reports, or Markdown before or after the final JSON object. "
    "Tools and progress updates while working are allowed; the final answer must contain only the JSON object."
)


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def local_binary(self) -> str | None:
        return "claude"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={
                "Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": env["ANTHROPIC_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['ANTHROPIC_BASE_URL']}/v1/messages (model={worker.env['ANTHROPIC_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "--output-format", "json",
                "--append-system-prompt", FINAL_JSON_INSTRUCTION,
                "-p",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--append-system-prompt", FINAL_JSON_INSTRUCTION,
            "-p",
            "--",
            prompt,
        ]

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        try:
            payload = json.loads(stdout)
        except ValueError:
            return stdout
        if not isinstance(payload, dict) or payload.get("type") != "result":
            return stdout  # Older adapters and local CLI fixtures return the answer directly.
        if payload.get("is_error") or payload.get("subtype") != "success":
            raise ValueError(f"Claude Code result failed: {payload.get('subtype')}")
        if not isinstance(payload.get("result"), str):
            raise ValueError("Claude Code result must contain final answer text")
        return payload["result"]
