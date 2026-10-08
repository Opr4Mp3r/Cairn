from __future__ import annotations

import json
from pathlib import PurePosixPath

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


# Catalog key written into the container's private GROK_HOME. The upstream model id
# stays in GROK_MODEL; `-m` always selects this injected entry.
_CONTAINER_MODEL_KEY = "cairn"
_API_BACKENDS = frozenset({"chat_completions", "responses", "messages"})
_HEALTH_SUFFIX = {
    "chat_completions": "/chat/completions",
    "responses": "/responses",
    "messages": "/messages",
}
_CONTAINER_SCRIPT = (
    'agent_dir="$1"\n'
    'config_toml="$2"\n'
    "shift 2\n"
    'mkdir -p "$agent_dir"\n'
    'printf "%s" "$config_toml" > "$agent_dir/config.toml"\n'
    'export GROK_HOME="$agent_dir"\n'
    "export GROK_MEMORY=0\n"
    "export GROK_DISABLE_AUTOUPDATER=1\n"
    'exec grok "$@"\n'
)


def validate_grok_env(worker_name: str, env: dict[str, str]) -> None:
    backend = env.get("GROK_API_BACKEND")
    if backend is not None and backend not in _API_BACKENDS:
        allowed = ", ".join(sorted(_API_BACKENDS))
        raise ValueError(f"worker {worker_name} env GROK_API_BACKEND must be one of: {allowed}")


def grok_api_backend(env: dict[str, str]) -> str:
    return env.get("GROK_API_BACKEND") or "chat_completions"


class GrokDriver(SeedSessionDriver):
    """Grok Build (`grok`) headless worker.

    Local mode reuses the host login and default model. Container mode writes a private
    config under ``/tmp/cairn-grok/<worker>`` so the CLI calls ``GROK_BASE_URL`` with
    ``XAI_API_KEY`` instead of the image user's ``~/.grok``.
    """

    type_name = "grok"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "grok"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        backend = grok_api_backend(env)
        base = env["GROK_BASE_URL"].rstrip("/")
        headers = {
            "Authorization": f"Bearer {env['XAI_API_KEY']}",
            "content-type": "application/json",
        }
        if backend == "messages":
            headers["anthropic-version"] = "2023-06-01"
        model = env["GROK_MODEL"]
        if backend == "responses":
            body: dict = {"model": model, "input": [{"role": "user", "content": "ping"}], "stream": False}
        else:
            body = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]}
        return http_ping(
            f"{base}{_HEALTH_SUFFIX[backend]}",
            headers=headers,
            json_body=body,
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        env = worker.env
        backend = grok_api_backend(env)
        base = env["GROK_BASE_URL"].rstrip("/")
        return f"POST {base}{_HEALTH_SUFFIX[backend]} (model={env['GROK_MODEL']}, api={backend})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        argv = self._argv(worker, prompt, session_id=session, resume=None)
        return DriverResult(argv=argv, session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._argv(worker, prompt, session_id=None, resume=session)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        # `--output-format json` prints one object. Cairn wants the model answer, not the envelope.
        payload = _json_object(stdout)
        if payload is None:
            return stdout
        text = payload.get("text")
        if isinstance(text, str):
            return text
        return stdout

    def _argv(self, worker: WorkerConfig, prompt: str, *, session_id: str | None, resume: str | None) -> list[str]:
        grok_argv = [
            "--always-approve",
            "--output-format",
            "json",
            "--no-auto-update",
            "--verbatim",
            "--no-plan",
            # The worker image (and some checkouts) ship project `.claude` files. Headless
            # startup blocks on an untrusted folder; there is no interactive grant.
            "--trust",
        ]
        if not self.local:
            grok_argv.extend(["--model", _CONTAINER_MODEL_KEY])
        if resume is not None:
            grok_argv.extend(["--resume", resume])
        elif session_id is not None:
            grok_argv.extend(["--session-id", session_id])
        grok_argv.extend(["--single", prompt])
        if self.local:
            return ["grok", *grok_argv]
        return [
            "/bin/sh",
            "-lc",
            _CONTAINER_SCRIPT,
            "--",
            _agent_dir(worker),
            _config_toml(worker),
            *grok_argv,
        ]


def _agent_dir(worker: WorkerConfig) -> str:
    return str(PurePosixPath("/tmp/cairn-grok") / worker.name)


def _config_toml(worker: WorkerConfig) -> str:
    env = worker.env
    lines = [
        "[models]",
        f"default = {_toml_string(_CONTAINER_MODEL_KEY)}",
        "",
        f"[model.{_CONTAINER_MODEL_KEY}]",
        f"model = {_toml_string(env['GROK_MODEL'])}",
        f"base_url = {_toml_string(env['GROK_BASE_URL'].rstrip('/'))}",
        f"name = {_toml_string(_CONTAINER_MODEL_KEY)}",
        'env_key = "XAI_API_KEY"',
        f"api_backend = {_toml_string(grok_api_backend(env))}",
    ]
    context_window = env.get("GROK_MODEL_CONTEXT_WINDOW")
    if context_window:
        lines.append(f"context_window = {int(context_window)}")
    return "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _json_object(stdout: str) -> dict | None:
    raw = stdout.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
