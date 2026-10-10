from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import TYPE_CHECKING

from cairn.audit_evidence import (
    json_digest, source_evidence, source_files as _source_files, relative_source_path as _relative_path,
    assert_source_unchanged, normalize_source_path,
)
from cairn.controller.kritt import check_control
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.server.models import EvidenceReference

if TYPE_CHECKING:
    from cairn.controller.kritt import KrittView


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


def check_observation(repo: Path, baseline: dict, path: str, start_line: int, end_line: int, quotation: str) -> EvidenceReference:
    repo = repo.resolve(strict=True)
    relative = normalize_source_path(repo, baseline, path)
    reference = EvidenceReference(path=relative, start_line=start_line, end_line=end_line, sha256=baseline["files"][relative])
    checked = source_evidence(repo, [reference])[0]
    lines = (repo / checked.path).read_text(encoding="utf-8").splitlines()
    if quotation != "\n".join(lines[start_line - 1:end_line]):
        raise ValueError(f"source quotation does not match the declared complete lines: {path}")
    return checked
