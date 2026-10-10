from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import time

from cairn.server.models import EvidenceReference
from cairn.dispatcher.runtime.cancellation import TaskCancellation


def json_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def repository_baseline(repo: Path, workspace_root: Path) -> dict:
    # ponytail: 小仓库全量指纹；只有实际规模成为瓶颈时再做增量缓存。
    files = {}
    for directory, dirs, names in os.walk(repo):
        dirs[:] = sorted(name for name in dirs if name not in {".git", ".venv", "node_modules", "__pycache__"}
                         and (Path(directory) / name).resolve() != workspace_root)
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_file():
                files[str(path.relative_to(repo))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:
        raise ValueError("repository has no readable source files")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--", "."], capture_output=True, text=True, timeout=5)
    return {"git_commit": result.stdout.strip() if result.returncode == 0 else None,
            "git_status": status.stdout if status.returncode == 0 else None,
            "files": files, "digest": json_digest(files)}


def source_evidence(repo: Path, references: list[EvidenceReference], baseline: dict | None = None) -> list[EvidenceReference]:
    checked = []
    for reference in references:
        if baseline and baseline.get("source_manifest"):
            reference = reference.model_copy(update={"path": normalize_source_path(repo, baseline, reference.path)})
        path = Path(reference.path)
        target = (path if path.is_absolute() else repo / path).resolve()
        if not target.is_relative_to(repo) or not target.is_file():
            raise ValueError(f"evidence is not a repository file: {reference.path}")
        content = target.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if reference.sha256 is not None and reference.sha256 != digest:
            raise ValueError(f"evidence content changed: {reference.path}")
        if reference.start_line is not None:
            line_count = len(content.decode("utf-8").splitlines())
            if (reference.end_line or reference.start_line) > line_count:
                raise ValueError(f"evidence line does not exist: {reference.path}")
        checked.append(reference.model_copy(update={"path": str(target.relative_to(repo)), "sha256": digest}))
    return checked


def _check_source_control(deadline=None, cancellation=None):
    if cancellation is not None and cancellation.is_cancelled:
        raise InterruptedError(cancellation.reason or "source preparation cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("source preparation deadline exhausted")


def relative_source_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("source path must be a string")
    path = PurePosixPath(value)
    if not value or "\x00" in value or "\\" in value or path.is_absolute() or any(
        part in {".", ".."} for part in value.split("/")
    ) or str(path) != value:
        raise ValueError(f"source path must be canonical and relative: {value}")
    return value


def source_files(repo: Path, deadline: float | None = None,
                  cancellation: TaskCancellation | None = None) -> dict[str, str]:
    files = {}
    for directory, dirs, names in os.walk(repo, followlinks=False):
        _check_source_control(deadline, cancellation)
        for name in dirs + names:
            _check_source_control(deadline, cancellation)
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"source symlinks are not supported: {path.relative_to(repo)}")
        for name in sorted(names):
            _check_source_control(deadline, cancellation)
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError(f"source contains a non-regular file: {path.relative_to(repo)}")
            relative = relative_source_path(path.relative_to(repo).as_posix())
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while True:
                    _check_source_control(deadline, cancellation)
                    chunk = stream.read(1024 * 1024)
                    _check_source_control(deadline, cancellation)
                    if not chunk:
                        break
                    digest.update(chunk)
            files[relative] = digest.hexdigest()
    _check_source_control(deadline, cancellation)
    return files


def assert_source_unchanged(repo: Path, baseline: dict, *, deadline: float | None = None,
                             cancellation: TaskCancellation | None = None) -> None:
    _check_source_control(deadline, cancellation)
    repo = repo.resolve(strict=True)
    info = baseline["source_manifest"]
    if hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest() != info["sha256"]:
        raise ValueError("source manifest changed after preparation")
    _check_source_control(deadline, cancellation)
    if source_files(repo, deadline, cancellation) != baseline["files"]:
        raise ValueError("source files changed after preparation")


def normalize_source_path(repo: Path, baseline: dict, raw_path: str) -> str:
    """Only declared aliases and packaging mappings can rewrite a source path."""
    repo = repo.resolve(strict=True)
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise ValueError("invalid source evidence path")
    path = PurePosixPath(raw_path)
    if path.is_absolute():
        if str(path) != raw_path or ".." in path.parts:
            raise ValueError("source evidence path must be canonical")
        local = Path(raw_path)
        if local.is_relative_to(repo):
            relative = local.relative_to(repo).as_posix()
        else:
            aliases = baseline["source_manifest"]["path_aliases"]
            alias = next((value for value in sorted(aliases, key=len, reverse=True) if raw_path.startswith(value + "/")), None)
            if alias is None:
                raise ValueError(f"source evidence path is outside the source directory: {raw_path}")
            relative = raw_path[len(alias) + 1:]
    else:
        relative = raw_path
    relative = relative_source_path(relative)
    relative = baseline["source_manifest"]["path_mappings"].get(relative, relative)
    if relative not in baseline["files"]:
        raise ValueError(f"source evidence file is absent from the manifest: {raw_path}")
    return relative


def assert_repository_unchanged(repo: Path, baseline: dict, workspace_root: Path, *, cancellation=None) -> None:
    """Use the same source rules as the preparation that created this baseline."""
    if baseline.get("source_manifest"):
        assert_source_unchanged(repo, baseline, cancellation=cancellation)
    elif repository_baseline(repo, workspace_root)["files"] != baseline.get("files"):
        raise ValueError("source baseline changed")
