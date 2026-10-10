"""Model-facing audit inputs; full evidence fingerprints stay in the program."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import json

import yaml

READING_GUIDANCE = "源码版本和文件完整性由程序核验；按任务读取相关源码和资料，不读取 source-baseline.json 或文件哈希清单。"


def baseline_identity(baseline: dict) -> dict:
    manifest = baseline.get("source_manifest") or {}
    return {"git_commit": manifest.get("commit_sha") or baseline.get("git_commit"),
            "digest": baseline.get("digest")}


def agent_view(value):
    if isinstance(value, dict):
        return {key: deepcopy(item) if key == "policy"
                else baseline_identity(item) if key == "baseline" and isinstance(item, dict)
                else agent_view(item) for key, item in value.items()}
    if isinstance(value, list):
        return [agent_view(item) for item in value]
    return value


def agent_graph_yaml(text: str) -> str:
    data = yaml.safe_load(text)
    if isinstance(data, dict) and data.get("project", {}).get("mode") == "audit":
        return yaml.safe_dump(agent_view(data), allow_unicode=True, sort_keys=False)
    return text


def material_paths(workspace: Path) -> dict[str, str]:
    context = json.loads((workspace / "input.json").read_text(encoding="utf-8"))
    return context.get("materials", {})
