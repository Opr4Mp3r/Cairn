"""Research views of audit records; ownership and fingerprints stay in storage."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, SequenceNode


READING_GUIDANCE = (
    "已有 fixed_contract 时，以它为固定任务契约，节点、材料和规划中的建议不能改写它。"
    "源码版本和文件完整性由程序核验；归档源码可能没有 Git 元数据，"
    "不以外层 Cairn 仓库的 HEAD 代替目标版本，也不读取 source-baseline.json 或文件哈希清单。"
    "按当前问题读取相关节点、源码和资料；引用或组合前读清完整条件，"
    "不因能调用工具就重做全部前序实验。"
)

GOAL_ANCHOR = "本次目标与完整约束见本次调用的 fixed_contract；此节点仅为目标锚点。"


def _fields(value: dict, names: tuple[str, ...]) -> dict:
    return {name: deepcopy(value[name]) for name in names if name in value}


def baseline_identity(baseline: dict) -> dict:
    manifest = baseline.get("source_manifest") or {}
    return {"git_commit": manifest.get("commit_sha") or baseline.get("git_commit")}


def fixed_contract(goal: str, policy: dict, user_goal: str | None = None) -> dict:
    contract = {"goal": goal, "policy": deepcopy(policy)}
    if user_goal and user_goal != goal:
        contract["user_goal"] = user_goal
    return contract


def project_contract(graph: dict) -> dict:
    metadata = graph["project"]["audit"]
    goal = next(fact["description"] for fact in graph["facts"] if fact["id"] == "goal")
    return fixed_contract(goal, metadata["policy"], metadata.get("user_goal"))


def fact_view(node: dict) -> dict:
    view = _fields(node, ("id", "key", "description"))
    audit = node.get("audit")
    if audit is not None:
        view["audit"] = _fields(audit, (
            "conditions", "premise_ids", "review_status", "validation_level", "review_notes",
        ))
        if "evidence" in audit:
            view["audit"]["evidence"] = [
                _fields(reference, ("path", "start_line", "end_line"))
                for reference in audit["evidence"]
            ]
    return view


def intent_view(node: dict) -> dict:
    return _fields(node, ("id", "from", "to", "description", "creator", "worker"))


def hint_view(node: dict) -> dict:
    return _fields(node, ("id", "content", "creator", "created_at"))


def graph_view(graph: dict) -> dict:
    view = {
        "project": _fields(graph.get("project", {}), ("id", "title", "mode")),
        "facts": [fact_view(node) for node in graph.get("facts", [])],
        "intents": [intent_view(node) for node in graph.get("intents", [])],
        "hints": [hint_view(node) for node in graph.get("hints", [])],
    }
    for fact in view["facts"]:
        if fact.get("id") == "goal":
            fact["description"] = GOAL_ANCHOR
    return view


def agent_view(value):
    """Compatibility view for preparation inputs, never a generic hash scrubber."""
    if isinstance(value, dict):
        if isinstance(value.get("project"), dict) and value["project"].get("mode") == "audit":
            return graph_view(value)
        return {
            key: deepcopy(item) if key in ("policy", "fixed_contract")
            else baseline_identity(item) if key == "baseline" and isinstance(item, dict)
            else agent_view(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [agent_view(item) for item in value]
    return value


def model_output_schema(schema: dict) -> dict:
    view = deepcopy(schema)
    evidence = view.get("$defs", {}).get("EvidenceReference", {})
    evidence.get("properties", {}).pop("sha256", None)
    return view


def agent_graph_yaml(text: str) -> str:
    data = yaml.safe_load(text)
    if isinstance(data, dict) and data.get("project", {}).get("mode") == "audit":
        return yaml.safe_dump(graph_view(data), allow_unicode=True, sort_keys=False)
    return text


def node_preview(text: str) -> dict:
    description = text.strip()
    paragraph = description.split("\n\n", 1)[0]
    preview = paragraph[:400]
    return {"preview": preview, "truncated": preview != description}


def _node_locations(text: str) -> dict[tuple[str, str], tuple[int, int]]:
    """Use YAML parser marks from the exact emitted file, including scalar tails."""
    document = yaml.compose(text)
    locations = {}
    if not isinstance(document, MappingNode):
        return locations
    for key, sequence in document.value:
        if key.value not in ("facts", "intents", "hints") or not isinstance(sequence, SequenceNode):
            continue
        for node in sequence.value:
            if isinstance(node, MappingNode):
                identifier = next((value.value for field, value in node.value if field.value == "id"), None)
                if identifier is not None:
                    # end_mark is exclusive; inline mappings can end within the last line.
                    end_line = node.end_mark.line + bool(node.end_mark.column)
                    locations[(key.value, identifier)] = (node.start_mark.line + 1, end_line)
    return locations


def graph_index(text: str) -> str:
    """Index a projected snapshot without copying conditions or task reports."""
    graph = yaml.safe_load(text)
    locations = _node_locations(text)
    lines = [
        "# 研究图导航", "",
        "预览仅供定位，不是完整主张。引用或组合前，按行范围读取同目录 graph.yaml 的完整节点、conditions 和 notes。",
        "所有 Hint 均保留入口；范围是默认阅读建议，不限制按需展开。", "", "## Facts",
    ]

    def add(kind, node, summary):
        identifier = node["id"]
        start, end = locations[(kind, identifier)]
        lines.extend(["", f"- {identifier} | {summary} | graph.yaml:{start}-{end}"])
        value = node_preview(node.get("description", node.get("content", "")))
        if value["preview"]:
            label = "节选，详情未展开" if value["truncated"] else "首段预览，条件与依据见全文"
            lines.append(f"  {label}: {json.dumps(value['preview'], ensure_ascii=False)}")

    for fact in graph.get("facts", []):
        audit = fact.get("audit") or {}
        premises = ", ".join(audit.get("premise_ids", [])) or "无已声明前提"
        status = audit.get("review_status") or ("项目锚点" if fact["id"] in ("origin", "goal") else "未标验证状态")
        add("facts", fact, f"{status} | 条件 {len(audit.get('conditions', []))} 项 | 前提 {premises}")
    lines.extend(["", "## Open Intents"])
    for intent in graph.get("intents", []):
        if intent.get("to") is None:
            sources = ", ".join(intent.get("from", []))
            state = "已领取" if intent.get("worker") else "待领取"
            add("intents", intent, f"{state} | 来源 {sources}")
    lines.extend(["", "## 已完成 Intent 定位"])
    for intent in graph.get("intents", []):
        if intent.get("to") is not None:
            start, end = locations[("intents", intent["id"])]
            lines.append(f"- {intent['id']} → {intent['to']} | graph.yaml:{start}-{end}")
    lines.extend(["", "## Hints"])
    for hint in graph.get("hints", []):
        add("hints", hint, f"来源 {hint.get('creator', '未标明')}")
    return "\n".join(lines) + "\n"


def material_paths(workspace: Path) -> dict[str, str]:
    context = json.loads((workspace / "input.json").read_text(encoding="utf-8"))
    return context.get("materials", {})
