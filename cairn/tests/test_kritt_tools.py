import json

import pytest
from pydantic import ValidationError

from cairn.controller.kritt import FORMATS, KrittView
from cairn.controller.kritt_tools import KrittTools, ToolAction


COMMIT = "a" * 40


def snapshot():
    def answer(layer, **updates):
        value = {key: [] if kind == "array" else True if kind == "boolean" else 10 if kind == "number"
                 else "command file write" for key, kind in FORMATS[layer].items()}
        value.update(updates)
        return value

    entry_answer = answer("entrypoint", entrypoint_file_path="module/install/control.php")
    entry = dict(id=10, scan_id=19, workflow_id=1, step_id=1, partition_key="19-0", prev_id=0,
                 prev_table=None, repeat_run=1, json_answer=entry_answer)
    flows = [dict(id=id, scan_id=19, workflow_id=1, step_id=2, partition_key="19-1", prev_id=10,
                  prev_table="workflows.step_results", repeat_run=1,
                  json_answer=answer("flow", flow_file_path=f"module/install/flow{id}.php"))
             for id in range(20, 24)]
    findings = [dict(id=id, scan_id=19, workflow_id=1, scan_metadata_id=id - 71, prev_id=id - 81,
                     prev_table="workflows.step_results", repeat_run=1,
                     json_answer=answer("finding", summary=f"RCE {id}", file_path=f"module/install/f{id}.php"),
                     dedupe_canonical_id=101, dedupe_is_canonical=id == 101, dedupe_cluster_id="19:101")
                for id in (101, 102)]

    def job(id, step_id, prev_id, answers, **updates):
        value = dict(id=id, scan_id=19, workflow_id=1, kind="step", step_id=step_id,
                     prev_id=prev_id, prev_table="workflows.step_results" if prev_id else None,
                     repeat_run=1, status="completed", checked_out_commit=COMMIT,
                     output_json={"results": answers}, stub=False)
        value.update(updates)
        return value

    jobs = [job(1, 1, 0, [entry_answer]), job(2, 2, 10, [f["json_answer"] for f in flows]),
            job(30, 3, 20, [findings[0]["json_answer"]]), job(31, 3, 21, [findings[1]["json_answer"]]),
            job(32, 3, 22, [], stub=True), job(33, 3, 23, [], status="failed", checked_out_commit=None),
            job(34, 3, 20, [], repeat_run=2, status="failed", error="CLI fault", checked_out_commit=None)]
    reviews = [dict(id=1001, scan_id=19, workflow_id=1, kind="post_script", post_script_id=6,
                    vulnerability_id=101, target_vulnerability_ids=[101], status="completed",
                    output_json={"results": [{"is_valid": True, "_chip_is_in_scope": True}]}),
               dict(id=1002, scan_id=19, workflow_id=1, kind="supplemental_post_script", post_script_id=6,
                    vulnerability_id=101, target_vulnerability_ids=[101], status="failed",
                    output_json=None, error="checkout unavailable")]
    return dict(scan=dict(id=19, workflow_id=1, repo_full="easysoft/zentaopms", commit_sha=COMMIT,
                          status="completed"), workflow=dict(id=1, step_ids=[1, 2, 3]),
                steps=[dict(id=n, depth=n - 1, name=layer, output_format=FORMATS[layer],
                            output_table="workflows.vulnerabilities" if layer == "finding" else "workflows.step_results",
                            multi_output=True, consume_all_previous=False)
                       for n, layer in enumerate(FORMATS, start=1)],
                step_results=[entry, *flows], vulnerabilities=findings, step_metadata=jobs,
                post_process_metadata=reviews,
                enrichments=[dict(id=2001, scan_id=19, vulnerability_id=101, post_script_id=6,
                                  result={"is_valid": True, "_chip_is_in_scope": True})],
                post_scripts=[dict(id=6, name="scope", output_format={"is_valid": "boolean"})])


@pytest.fixture
def view():
    return KrittView(snapshot())


def get_action(view, id=101, **updates):
    return {"action": "get", "ref": view.ref("workflows.vulnerabilities", id).model_dump(mode="json"), **updates}


def collect(tools, action):
    records = []
    for _ in range(200):
        result = tools.execute(action)
        records.extend(result.records)
        assert result.receipt.returned_bytes == len(result.model_dump_json().encode())
        assert result.receipt.returned_bytes <= tools.max_bytes
        assert result.receipt.returned_rows <= tools.max_rows
        if not result.receipt.next_cursor:
            return records
        action = {**action, "cursor": result.receipt.next_cursor}
    pytest.fail("pagination did not terminate")


def test_describe_and_search_keep_duplicates_as_navigation_only(view):
    tools = KrittTools(view)
    description = tools.execute({"action": "describe"})
    assert description.summary["counts"] == {"entrypoint": 1, "flow": 4, "finding": 2, "flows_without_findings": 2}
    hits = tools.execute({"action": "search", "kind": "finding", "route": "candidates", "effect": "rce"})
    assert [r.ref.id for r in hits.records] == [101, 102]
    assert tools.ledger == []
    with pytest.raises(ValueError, match="fully returned"):
        tools.validate_references([hits.records[0].ref])


def test_get_expands_cluster_alternative_flow_and_all_review_history(view, tmp_path):
    tools = KrittTools(view, max_rows=2)
    state = tools.execute(get_action(view)).summary["review_selection"]["scripts"][0]
    assert state["latest_execution_id"] == 1002 and state["latest_execution_status"] == "failed"
    assert state["latest_successful_id"] == 1001
    records = collect(tools, get_action(view))
    pairs = {(r.ref.table, r.ref.id) for r in records}
    assert len(pairs) == len(records)
    assert ("workflows.vulnerabilities", 102) in pairs
    assert {10, 20, 21} <= {id for table, id in pairs if table == "workflows.step_results"}
    assert {30, 31, 34} <= {id for table, id in pairs if table == "workflows.step_metadata"}
    reviews = {r.ref.id: r.content for r in records if r.ref.table == "workflows.post_process_metadata"}
    assert reviews[1001]["output_json"]["results"][0]["is_valid"] is True
    assert reviews[1002]["status"] == "failed"
    assert reviews[1002]["output_json"] is None
    assert tools.validate_references([view.ref("workflows.vulnerabilities", 102)])
    path = tmp_path / "receipts.json"
    tools.save_receipts(path)
    saved = json.loads(path.read_text())
    assert saved["view_digest"] == view.digest and len(saved["ledger"]) == len(pairs)


def test_completed_supplemental_review_supersedes_main_result_but_preserves_conflict():
    data = snapshot()
    data["post_process_metadata"][-1].update(status="completed", error=None,
        output_json={"results": [{"is_valid": True, "_chip_is_in_scope": False}]})
    view = KrittView(data)
    tools = KrittTools(view)
    state = tools.execute(get_action(view)).summary["review_selection"]["scripts"][0]
    assert state["latest_execution_id"] == state["latest_successful_id"] == 1002
    reviews = {r.ref.id: r.content for r in collect(tools, get_action(view))
               if r.ref.table == "workflows.post_process_metadata"}
    assert reviews[1001]["output_json"]["results"][0]["_chip_is_in_scope"] is True
    assert reviews[1002]["output_json"]["results"][0]["_chip_is_in_scope"] is False


def test_coverage_reads_no_findings_stub_and_execution_failure(view):
    tools = KrittTools(view)
    search = {"action": "search", "kind": "flow", "route": "coverage", "coverage": "no_findings"}
    assert [r.ref.id for r in tools.execute(search).records] == [22, 23]
    assert [r.ref.id for r in tools.execute({**search, "coverage": "stub"}).records] == [22]
    assert [r.ref.id for r in tools.execute({**search, "coverage": "failed"}).records] == [20, 23]
    records = collect(tools, {"action": "get", "ref": view.ref("workflows.step_results", 22, "19-1").model_dump(mode="json")})
    assert any(r.ref.id == 32 and r.content["stub"] is True for r in records)
    assert not any(r.ref.table == "workflows.vulnerabilities" for r in records)


def test_pagination_is_stable_query_bound_and_view_bound(view):
    action = {"action": "search", "kind": "flow", "route": "coverage", "limit": 1}
    tools = KrittTools(view)
    first = tools.execute(action)
    assert first == KrittTools(view).execute(action)
    records = collect(tools, action)
    assert [r.ref.id for r in records] == [20, 21, 22, 23]
    cursor = first.receipt.next_cursor
    with pytest.raises(ValueError, match="fixed query"):
        tools.execute({**action, "route": "candidates", "cursor": cursor})
    changed = snapshot()
    changed["snapshot_at"] = "2026-10-07T00:00:00Z"
    with pytest.raises(ValueError, match="fixed query"):
        KrittTools(KrittView(changed)).execute({**action, "cursor": cursor})
    with pytest.raises(ValueError, match="invalid Kritt cursor"):
        tools.execute({**action, "cursor": "this is not a cursor"})


def test_oversized_material_is_explicitly_fragmented_and_citation_waits_for_all_bytes():
    data = snapshot()
    data["vulnerabilities"][0]["json_answer"]["explanation"] = "待验证条件" * 3000
    data["step_metadata"][2]["output_json"]["results"][0] = data["vulnerabilities"][0]["json_answer"]
    view = KrittView(data)
    tools = KrittTools(view, max_rows=1, max_bytes=4096)
    first = tools.execute(get_action(view))
    assert first.records[0].complete is False and first.receipt.truncated
    with pytest.raises(ValueError, match="fully returned"):
        tools.validate_references([view.ref("workflows.vulnerabilities", 101)])
    records = collect(tools, get_action(view))
    parts = [r for r in records if r.ref.table == "workflows.vulnerabilities" and r.ref.id == 101]
    recovered = json.loads("".join(r.content["json_fragment"] for r in parts))
    assert recovered == view.get("workflows.vulnerabilities", 101)
    assert tools.validate_references([view.ref("workflows.vulnerabilities", 101)])


@pytest.mark.parametrize("action", [
    {"action": "search", "kind": "finding", "route": "candidates", "sql": "SELECT 1"},
    {"action": "search", "kind": "finding", "route": "candidates", "fields": ["flow_summary"]},
    {"action": "search", "kind": "flow", "route": "coverage", "fields": ["password"]},
    {"action": "search", "kind": "flow", "route": "coverage", "path": "../config.php"},
    {"action": "search", "kind": "flow", "route": "coverage", "limit": "2"},
    {"action": "search", "kind": "finding", "route": "coverage", "coverage": "failed"},
    {"action": "delete"},
])
def test_invalid_actions_fail_before_query(action):
    with pytest.raises(ValidationError):
        ToolAction.model_validate(action)


def test_cross_scan_hash_forgery_and_unissued_receipt_are_rejected(view):
    tools = KrittTools(view)
    action = get_action(view)
    with pytest.raises(ValueError, match="fixed scan"):
        tools.execute({**action, "ref": {**action["ref"], "scan_id": 20}})
    with pytest.raises(ValueError, match="hash"):
        tools.execute({**action, "ref": {**action["ref"], "sha256": "b" * 64}})
    result = tools.execute(action)
    assert tools.validate_receipt(result.receipt) == result.receipt
    forged = result.receipt.model_dump(mode="json")
    forged["returned_rows"] += 1
    with pytest.raises(ValueError, match="not emitted"):
        tools.validate_receipt(forged)


def test_unmatched_query_is_complete_empty_result(view):
    result = KrittTools(view).execute({"action": "search", "kind": "flow", "route": "coverage",
                                      "terms": ["does-not-appear-in-this-scan"]})
    assert result.records == [] and result.receipt.total_rows == 0
    assert result.receipt.truncated is False and result.receipt.next_cursor is None
