from __future__ import annotations

import json
from collections.abc import Iterator

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "codex"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['CODEX_BASE_URL']}/responses",
            headers={
                "Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["CODEX_MODEL"],
                "input": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['CODEX_BASE_URL']}/responses (model={worker.env['CODEX_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._argv(worker, prompt, session), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._argv(worker, prompt, session)

    def _argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        argv = ["codex", "exec"]
        if session is not None:
            argv.extend(["resume", session])
        argv.extend([
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
        ])
        if not self.local:
            env = worker.env
            argv.extend([
                "--model", env["CODEX_MODEL"],
                "-c", 'model_provider="cairn"',
                "-c", 'model_providers.cairn.name="cairn"',
                "-c", 'model_providers.cairn.wire_api="responses"',
                "-c", 'model_reasoning_effort="high"',
                "-c", f'model_providers.cairn.base_url="{env["CODEX_BASE_URL"]}"',
                "-c", 'model_providers.cairn.env_key="OPENAI_API_KEY"',
            ])
        return [*argv, "--", prompt]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in _events(stdout):
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id.strip():
                    return thread_id
        return super().extract_session(session, stdout, stderr)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        final_text = None
        response = None
        completed = False
        failed = False
        for event in _events(stdout, strict=True):
            kind = event.get("type")
            if kind in ("thread.started", "turn.started", "turn.failed", "error"):
                final_text = response = None
                completed = False
                if kind in ("thread.started", "turn.started"):
                    failed = False
                elif kind == "turn.failed":
                    failed = True
            elif kind in ("item.started", "item.updated", "item.completed"):
                # Any new work invalidates the preceding answer, including a
                # tool call made after an apparently final assistant message.
                final_text = response = None
                completed = False
                item = event.get("item")
                if (
                    kind == "item.completed"
                    and isinstance(item, dict)
                    and item.get("type") == "agent_message"
                    and item.get("phase") in (None, "final_answer")
                ):
                    final_text = item.get("text")
            elif kind == "turn.completed":
                # A reconnect error may precede a successful retry in the same turn.
                completed = True
                response = final_text
        if failed or not completed:
            raise ValueError("Codex did not complete a successful turn")
        if not isinstance(response, str) or not response.strip():
            raise ValueError("Codex completed without final answer text")
        return response


def _events(stdout: str, *, strict: bool = False) -> Iterator[dict]:
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            if strict:
                raise ValueError("Codex output must contain complete JSON events") from None
            continue
        if isinstance(event, dict):
            yield event
        elif strict:
            raise ValueError("Codex output events must be JSON objects")
