from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING

from cairn.audit_evidence import json_digest, source_evidence
from cairn.controller.kritt import check_control
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.server.models import EvidenceReference

if TYPE_CHECKING:
    from cairn.controller.kritt import KrittView


def _relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("source path must be a string")
    path = PurePosixPath(value)
    if not value or "\x00" in value or "\\" in value or path.is_absolute() or any(
        part in {".", ".."} for part in value.split("/")
    ) or str(path) != value:
        raise ValueError(f"source path must be canonical and relative: {value}")
    return value


def _source_files(repo: Path, deadline: float | None = None,
                  cancellation: TaskCancellation | None = None) -> dict[str, str]:
    files = {}
    for directory, dirs, names in os.walk(repo, followlinks=False):
        check_control(deadline, cancellation)
        for name in dirs + names:
            check_control(deadline, cancellation)
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"source symlinks are not supported: {path.relative_to(repo)}")
        for name in sorted(names):
            check_control(deadline, cancellation)
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError(f"source contains a non-regular file: {path.relative_to(repo)}")
            relative = _relative_path(path.relative_to(repo).as_posix())
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while True:
                    check_control(deadline, cancellation)
                    chunk = stream.read(1024 * 1024)
                    check_control(deadline, cancellation)
                    if not chunk:
                        break
                    digest.update(chunk)
            files[relative] = digest.hexdigest()
    check_control(deadline, cancellation)
    return files


def verify_source(repo: Path, view: KrittView, manifest: Path | None, *, deadline: float | None = None,
                  cancellation: TaskCancellation | None = None) -> dict:
    """Validate an operator-provided archive manifest; it is not a Git attestation."""
    check_control(deadline, cancellation)
    repo = repo.resolve(strict=True)
    if not repo.is_dir() or manifest is None:
        raise ValueError("a complete source directory and external manifest are required")
    manifest = manifest.resolve(strict=True)
    if manifest.is_relative_to(repo):
        raise ValueError("source manifest must be outside the source directory")
    raw = manifest.read_bytes()
    check_control(deadline, cancellation)
    data = json.loads(raw)
    check_control(deadline, cancellation)
    required = {"format_version", "repo_full", "commit_sha", "git_tree", "archive_sha256", "tracked_count", "files"}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - {"path_aliases", "path_mappings"}:
        raise ValueError("unsupported source manifest fields")
    if type(data["format_version"]) is not int or data["format_version"] != 1:
        raise ValueError("unsupported source manifest version")
    if data["repo_full"] != view.scan["repo_full"] or data["commit_sha"] != view.scan["commit_sha"]:
        raise ValueError("source repository or commit does not match the selected Kritt scan")
    if any(not isinstance(data[key], str) or not re.fullmatch(r"[0-9a-f]{40}", data[key]) for key in ("commit_sha", "git_tree")):
        raise ValueError("source manifest requires commit and Git tree hashes")
    if not isinstance(data["archive_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["archive_sha256"]):
        raise ValueError("source manifest requires an archive SHA-256")
    expected = data["files"]
    if not isinstance(expected, dict) or not expected or type(data["tracked_count"]) is not int or data["tracked_count"] != len(expected):
        raise ValueError("source manifest file count is incomplete")
    for name, digest in expected.items():
        check_control(deadline, cancellation)
        _relative_path(name)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"source manifest has an invalid file hash: {name}")
    aliases = data.get("path_aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) or not alias.startswith("/") or alias == "/" or alias.endswith("/")
        or "\x00" in alias or "\\" in alias
        or str(PurePosixPath(alias)) != alias or ".." in PurePosixPath(alias).parts
        for alias in aliases
    ) or len(set(aliases)) != len(aliases):
        raise ValueError("source path aliases must be unique canonical absolute prefixes")
    mappings = data.get("path_mappings", {})
    if not isinstance(mappings, dict):
        raise ValueError("source path mappings must be an object")
    for original, target in mappings.items():
        _relative_path(original)
        _relative_path(target)
        if target not in expected or original in expected:
            raise ValueError(f"source path mapping is ambiguous or missing: {original}")
    files = _source_files(repo, deadline, cancellation)
    check_control(deadline, cancellation)
    if files != expected:
        raise ValueError("source files do not match the complete archive manifest")
    # An archive can sit inside another Git checkout; that ancestor is not its source commit.
    return dict(git_commit=None, git_status=None, files=files, digest=json_digest(files), source_manifest={
        "path": str(manifest), "sha256": hashlib.sha256(raw).hexdigest(),
        "repo_full": data["repo_full"], "commit_sha": data["commit_sha"], "git_tree": data["git_tree"],
        "archive_sha256": data["archive_sha256"], "tracked_count": data["tracked_count"],
        "path_aliases": aliases, "path_mappings": mappings,
    })


def assert_source_unchanged(repo: Path, baseline: dict, *, deadline: float | None = None,
                             cancellation: TaskCancellation | None = None) -> None:
    check_control(deadline, cancellation)
    repo = repo.resolve(strict=True)
    info = baseline["source_manifest"]
    if hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest() != info["sha256"]:
        raise ValueError("source manifest changed after preparation")
    check_control(deadline, cancellation)
    if _source_files(repo, deadline, cancellation) != baseline["files"]:
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
    relative = _relative_path(relative)
    relative = baseline["source_manifest"]["path_mappings"].get(relative, relative)
    if relative not in baseline["files"]:
        raise ValueError(f"source evidence file is absent from the manifest: {raw_path}")
    return relative


def check_observation(repo: Path, baseline: dict, path: str, start_line: int, end_line: int, quotation: str) -> EvidenceReference:
    repo = repo.resolve(strict=True)
    relative = normalize_source_path(repo, baseline, path)
    reference = EvidenceReference(path=relative, start_line=start_line, end_line=end_line, sha256=baseline["files"][relative])
    checked = source_evidence(repo, [reference])[0]
    lines = (repo / checked.path).read_text(encoding="utf-8").splitlines()
    if quotation != "\n".join(lines[start_line - 1:end_line]):
        raise ValueError(f"source quotation does not match the declared complete lines: {path}")
    return checked
