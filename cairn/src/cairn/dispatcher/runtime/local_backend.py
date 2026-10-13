from __future__ import annotations

import logging
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.local_process import LocalProcess

LOG = logging.getLogger(__name__)


class LocalBackend:
    """Runs workers directly on the dispatcher host instead of in per-project containers.

    Each project gets an isolated working directory under ``workspace_root`` (defaulting
    to the directory the dispatcher was started in). Worker processes inherit the host
    environment so the pre-configured ``claude`` / ``codex`` / ``pi`` / ``grok`` CLIs and their
    credentials are used as-is; no API keys are injected. There are no containers to
    build or tear down, so the container-lifecycle methods are inert.
    """

    def __init__(self, config: LocalConfig):
        self._config = config
        root = config.workspace_root
        self._root = Path(root).expanduser() if root else Path.cwd()
        self._calls = {}
        self._calls_lock = threading.Lock()

    @contextmanager
    def observe_call(self, workspace, worker, phase):
        role = phase.removesuffix("_format_repair").removesuffix("_execute").removesuffix("_conclude")
        if role == "builder":
            role = "build"
        key = (str(Path(workspace).resolve()), role, worker.type)
        with self._calls_lock:
            self._calls[key] = self._calls.get(key, 0) + 1
        try:
            yield
        finally:
            with self._calls_lock:
                self._calls[key] -= 1
                if not self._calls[key]:
                    del self._calls[key]

    def active_calls(self, workspace=None):
        path = str(Path(workspace).resolve()) if workspace else None
        with self._calls_lock:
            return [{"role": role, "harness": harness, "count": count}
                    for (folder, role, harness), count in self._calls.items()
                    if path is None or folder == path]

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return str(self._project_dir(project_id))

    def ensure_running(self, project_id: str) -> str:
        project_dir = self._project_dir(project_id)
        project_dir.mkdir(parents=True, exist_ok=True)
        LOG.debug("local project workdir ready project=%s dir=%s", project_id, project_dir)
        return str(project_dir)

    def build_exec_process(
        self,
        container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        merged_env = {key: value for key, value in {**os.environ, **(env or {})}.items()
                      if key not in self._config.excluded_env}
        return LocalProcess(
            command,
            cwd=container_name,
            env=merged_env,
            timeout_seconds=timeout_seconds,
            term_grace_seconds=kill_after_seconds,
        )

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        target = Path(path)
        if not target.is_absolute():
            raise ValueError(f"local file path must be absolute: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def needs_completed_cleanup(self, project_id: str) -> bool:
        return self._config.completed_action == "remove" and self._project_dir(project_id).exists()

    def needs_stopped_cleanup(self, project_id: str) -> bool:
        return False

    def cleanup_completed(self, project_id: str) -> bool:
        if self._config.completed_action == "remove":
            project_dir = self._project_dir(project_id)
            LOG.info("removing completed project workdir project=%s dir=%s", project_id, project_dir)
            shutil.rmtree(project_dir, ignore_errors=True)
        return True

    def cleanup_stopped(self, project_id: str) -> bool:
        return True

    def managed_container_names(self) -> list[str]:
        return []

    def _project_dir(self, project_id: str) -> Path:
        return self._root / project_id.replace("/", "-")
