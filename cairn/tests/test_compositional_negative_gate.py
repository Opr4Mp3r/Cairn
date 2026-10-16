from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from cairn.audit_evidence import source_evidence
from cairn.server.models import AuditClosing, AuditFactData, AuditRecordRequest, ClosingResult, EvidenceReference, Fact, Intent
from test_compositional_research import assert_blocked_example, prepare_example


@pytest.fixture
def blocked_example(tmp_path):
    _, prepared = prepare_example(tmp_path, cached=True, server="http://127.0.0.1:65530")
    repo = Path(prepared["source_root"])
    baseline = json.loads(Path(prepared["source_baseline"]).read_text())
    evidence = source_evidence(repo, [EvidenceReference(path="sample.py", start_line=n) for n in (4, 8, 11)], baseline)
    fact = Fact(id="f002", description="固定顺序下读取独立构造快照，返回 before，不能衔接。",
                audit=AuditFactData(evidence=evidence, conditions=["同一对象先构造，后发布，再读取"], review_status="unverified"))
    project = NS(project=NS(status="budget_exhausted", audit=NS(seed_ids={"publish": "f001"}, baseline=baseline)),
                 facts=[Fact(id="f001", description="写入共享配置"), fact],
                 intents=[Intent(id="i002", from_=["f001"], to="f002", description="核对衔接", creator="builder",
                                 worker="explorer", created_at="now", concluded_at="later")])
    closing = AuditClosing(status="success", session="closing", result=ClosingResult(
        kind="no_candidate", description="f002 的独立快照阻断了固定顺序的衔接。"))
    audit = NS(closing=closing, records=[
        AuditRecordRequest(record_key="closing", object_type="project", object_id="proj_001", phase="closing",
                           input_digest="a" * 64, attempt_status="success", session="closing",
                           result=closing.result.model_dump(mode="json", by_alias=True)),
    ])
    return project, audit, repo


@pytest.mark.parametrize("by_alias", [False, True])
def test_unreviewed_blocker_with_decisive_source_and_closing_passes(blocked_example, by_alias):
    _, audit, _ = blocked_example
    audit.records[0].result = audit.closing.result.model_dump(mode="json", by_alias=by_alias)
    assert assert_blocked_example(*blocked_example) == ["f002"]


@pytest.mark.parametrize("failure", ["no_observation", "refuted", "no_evidence", "write_only",
                                      "no_conditions", "not_produced", "closing_failed", "closing_without_record",
                                      "closing_does_not_cite_blocker", "source_returns_after"])
def test_failed_or_evidence_free_research_cannot_pass_as_incompatible(blocked_example, failure):
    project, audit, repo = deepcopy(blocked_example)
    fact = project.facts[1]
    if failure == "no_observation":
        project.facts = project.facts[:1]
        project.intents = []
    elif failure == "refuted":
        fact.audit.review_status = "refuted"
    elif failure == "no_evidence":
        fact.audit.evidence = []
    elif failure == "write_only":
        fact.audit.evidence = fact.audit.evidence[:1]
    elif failure == "no_conditions":
        fact.audit.conditions = []
    elif failure == "not_produced":
        project.intents[0].concluded_at = None
    elif failure == "closing_failed":
        audit.closing.status = "error"
    elif failure == "closing_without_record":
        audit.records = []
    elif failure == "closing_does_not_cite_blocker":
        audit.closing.result.description = "预算结束，没有足够证据。"
        audit.records[0].result = audit.closing.result.model_dump(mode="json", by_alias=True)
    elif failure == "source_returns_after":
        path = repo / "sample.py"
        path.write_text(path.read_text().replace('return self._initial["text"]', 'return STORE["text"]'))
    with pytest.raises(AssertionError):
        assert_blocked_example(project, audit, repo)
