from copy import deepcopy
import json
import re
import textwrap

import pytest
import yaml

from cairn.audit_inputs import (
    agent_graph_yaml, agent_view, fixed_contract, graph_index, graph_view,
    model_output_schema, project_contract,
)
from cairn.server.models import AuditFactData


@pytest.fixture
def graph():
    return {
        "project": {
            "id": "proj_001", "title": "条件性知识", "mode": "audit",
            "runtime_generation": 9,
            "audit": {
                "policy": {"effect": "effect alone is incomplete", "scope": ["fixed boundary"],
                           "threat_model": [{"baseline": {"digest": "a scientific observation"}}]},
                "user_goal": "original request with additional wording",
                "baseline": {"digest": "machine fingerprint", "files": {"app.py": "a" * 64}},
                "policy_digest": "policy fingerprint", "leases": {"i1": "secret-lease"},
            },
        },
        "facts": [
            {"id": "origin", "description": "scan and commit; historical claims remain leads"},
            {"id": "goal", "description": "full target, including original deployment limits"},
            {"id": "f001", "description": "条件性结论\n\n原始细节：sha256 保留在历史正文。",
             "audit": {"conditions": ["critical condition only in this field"],
                       "premise_ids": ["origin"], "review_status": "unverified",
                       "validation_level": "source_analysis",
                       "review_notes": "末尾边界不能丢\n此处是第二行",
                       "evidence": [{"path": "app.py", "start_line": 2, "end_line": 9, "sha256": "a" * 64}]}},
            {"id": "f002", "description": "另一分支的反证", "audit": {
                "conditions": ["only in profile B"], "premise_ids": [], "review_status": "refuted"}},
        ],
        "intents": [
            {"id": "i001", "from": ["f001"], "description": "问题\n\n完整计划", "to": None,
             "worker": "worker", "lease_id": "secret-lease", "last_heartbeat_at": "runtime-only"},
            {"id": "i002", "from": ["f002"], "description": "已结束研究不重复进入开放任务",
             "to": "f001", "worker": "worker"},
        ],
        "hints": [{"id": "h001", "creator": "overseer", "content": "核对另一分支反证\n完整条件见来源",
                   "created_at": "2026-10-15T08:19:02Z"}],
    }


def test_contract_preserves_goal_policy_and_original_request_without_mutating_graph(graph):
    before = deepcopy(graph)
    contract = project_contract(graph)
    view = graph_view(graph)
    assert contract == fixed_contract(graph["facts"][1]["description"], graph["project"]["audit"]["policy"],
                                      graph["project"]["audit"]["user_goal"])
    assert contract["goal"] != contract["policy"]["effect"]
    assert view["facts"][1]["description"] != contract["goal"]
    assert "fixed_contract" in view["facts"][1]["description"]
    assert graph == before
    contract["policy"]["scope"].append("changed in a caller")
    view["facts"][2]["audit"]["conditions"].clear()
    assert graph == before


def test_typed_projection_preserves_research_content_and_kritt_ref(graph):
    view = graph_view(graph)
    assert set(view["project"]) == {"id", "title", "mode"}
    assert "secret-lease" not in json.dumps(view)
    assert "runtime-only" not in json.dumps(view)
    assert view["facts"][2]["description"] == graph["facts"][2]["description"]
    before = graph["facts"][2]["audit"]
    after = view["facts"][2]["audit"]
    for key in ("conditions", "premise_ids", "review_notes", "review_status", "validation_level"):
        assert after[key] == before[key]
    assert after["evidence"] == [{"path": "app.py", "start_line": 2, "end_line": 9}]
    kritt_ref = {"table": "workflows.vulnerabilities", "id": 7, "scan_id": 1, "sha256": "b" * 64}
    assert agent_view({"references": [kritt_ref]})["references"] == [kritt_ref]
    assert agent_view({"policy": graph["project"]["audit"]["policy"]})["policy"] == graph["project"]["audit"]["policy"]


@pytest.mark.parametrize("flow_style", [False, True])
def test_navigation_ranges_open_complete_nodes_with_all_conditions_and_tail_notes(graph, flow_style):
    projected = graph_view(graph)
    text = yaml.safe_dump(projected, allow_unicode=True, sort_keys=False, default_flow_style=flow_style)
    index = graph_index(text)
    locations = {identifier: (int(start), int(end)) for identifier, start, end in
                 re.findall(r"^- (\w+).*?graph\.yaml:(\d+)-(\d+)$", index, re.M)}
    for kind in ("facts", "intents", "hints"):
        for node in projected[kind]:
            start, end = locations[node["id"]]
            fragment = "\n".join(text.splitlines()[start - 1:end])
            if not flow_style:
                assert yaml.safe_load(textwrap.dedent(fragment)) == [node]
            else:
                assert node["id"] in fragment
    assert "critical condition only in this field" not in index
    assert "末尾边界不能丢" not in index
    assert "节选" in index
    open_section = index.split("## Open Intents\n", 1)[1].split("## 已完成 Intent 定位", 1)[0]
    assert "i001" in open_section and "i002" not in open_section
    assert "i002 → f001" in index
    assert "h001" in index and "overseer" in index and "f002" in index


def test_long_legacy_nodes_remain_complete_while_index_is_only_a_locator(graph):
    graph["facts"][2]["description"] = "长段落" * 1000 + "\n\n末尾真正限制"
    graph["facts"][2]["audit"]["conditions"] *= 100
    text = agent_graph_yaml(yaml.safe_dump(graph, allow_unicode=True, sort_keys=False))
    index = graph_index(text)
    assert "末尾真正限制" in text and "末尾真正限制" not in index
    assert yaml.safe_load(text)["facts"][2]["description"] == graph["facts"][2]["description"]
    assert "条件 100 项" in index and len(index) < len(text)


def test_model_schema_only_hides_optional_source_evidence_hash():
    original = AuditFactData.model_json_schema()
    original["$defs"]["KrittRef"] = {"properties": {"sha256": {"type": "string"}}, "required": ["sha256"]}
    before = deepcopy(original)
    view = model_output_schema(original)
    assert "sha256" not in view["$defs"]["EvidenceReference"]["properties"]
    assert view["$defs"]["KrittRef"] == original["$defs"]["KrittRef"]
    assert original == before
    assert AuditFactData.model_validate({"evidence": [{"path": "app.py", "start_line": 2}]}).evidence[0].sha256 is None


def test_generic_graph_bytes_are_untouched():
    text = "project:\n  title: Generic\nfacts: [{id: f1, description: 'same bytes'}]\n"
    assert agent_graph_yaml(text) == text
