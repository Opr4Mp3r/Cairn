from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from cairn.server.models import EvidenceReference


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


def source_evidence(repo: Path, references: list[EvidenceReference]) -> list[EvidenceReference]:
    checked = []
    for reference in references:
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
