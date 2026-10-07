"""Bounded, source-tracked queries over one immutable Kritt view."""
from __future__ import annotations

import base64
import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from cairn.audit_evidence import json_digest
from cairn.controller.kritt import KrittRef, KrittView


PROFILE_VERSION = "audit-retrieval-v1"
KINDS = {"entrypoint": "workflows.step_results", "flow": "workflows.step_results",
         "finding": "workflows.vulnerabilities"}
FIELDS = {
    "entrypoint": ("entrypoint_kind", "entrypoint_label", "entrypoint_handler", "entrypoint_actor",
                   "entrypoint_input", "entrypoint_reachability", "entrypoint_file_path"),
    "flow": ("flow_summary", "flow_trace", "flow_terminal_action", "flow_input_to_sink",
             "flow_security_relevance", "flow_file_path"),
    "finding": ("summary", "explanation", "trigger_flow", "vulnerability_type", "malicious_actor",
                "malicious_input_example", "file_path"),
}
FieldName = Literal[
    "entrypoint_kind", "entrypoint_label", "entrypoint_handler", "entrypoint_actor", "entrypoint_input",
    "entrypoint_reachability", "entrypoint_file_path", "flow_summary", "flow_trace", "flow_terminal_action",
    "flow_input_to_sink", "flow_security_relevance", "flow_file_path", "summary", "explanation",
    "trigger_flow", "vulnerability_type", "malicious_actor", "malicious_input_example", "file_path",
]
MECHANISMS = {
    "rce": ("rce", "code execution", "command", "exec(", "system(", "shell", "eval(", "include",
            "file write", "file_put_contents", "config", "sql injection", "代码执行", "命令", "文件写",
            "配置写", "安装", "升级"),
    "info_disclosure": ("disclosure", "exfiltration", "file read", "download", "credential", "secret",
                        "enumeration", "泄露", "泄漏", "读取", "下载", "凭据", "枚举"),
}
ROUTES = {
    "candidates": "相关候选/流程机制；get展开父链、同簇输入和条件差异。",
    "coverage": "相关入口/流程及调查job；无finding、stub、失败只代表覆盖状态。",
    "counterevidence": "展开复核/执行历史与同簇冲突，再独立读取同commit源码及上游守卫。",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DescribeAction(_StrictModel):
    action: Literal["describe"]


class SearchAction(_StrictModel):
    action: Literal["search"]
    kind: Literal["entrypoint", "flow", "finding"]
    route: Literal["candidates", "coverage", "counterevidence"]
    effect: Literal["rce", "info_disclosure"] | None = None
    terms: list[str] = Field(default_factory=list, max_length=8)
    fields: list[FieldName] = Field(default_factory=list, max_length=16)
    path: str | None = Field(default=None, max_length=240)
    coverage: Literal["all", "no_findings", "stub", "failed"] = "all"
    limit: int = Field(default=10, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def allowed_search(self):
        if any(not term.strip() or len(term) > 80 for term in self.terms):
            raise ValueError("terms must be nonempty strings of at most 80 characters")
        if self.fields and not set(self.fields) <= set(FIELDS[self.kind]):
            raise ValueError("fields must belong to the selected kind")
        if self.coverage != "all" and self.kind != "flow":
            raise ValueError("coverage filters require flow records")
        if self.path is not None and (not self.path.strip() or ".." in PurePosixPath(self.path).parts
                                      or self.path.startswith("/")):
            raise ValueError("path must be a relative source path without parent traversal")
        return self


class GetAction(_StrictModel):
    action: Literal["get"]
    ref: KrittRef
    limit: int = Field(default=10, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1200)

    @model_validator(mode="after")
    def allowed_table(self):
        if self.ref.table not in {"workflows.step_results", "workflows.vulnerabilities",
                                  "workflows.step_metadata", "workflows.post_process_metadata",
                                  "workflows.vulnerability_enrichments"}:
            raise ValueError("get only accepts fixed scan material tables")
        return self


class ToolAction(RootModel[Annotated[DescribeAction | SearchAction | GetAction, Field(discriminator="action")]]):
    pass


class ToolRecord(_StrictModel):
    ref: KrittRef
    relation: str
    content: dict[str, Any]
    complete: bool
    fragment_start: int = 0
    fragment_end: int = 0
    total_characters: int = 0


class ToolReceipt(_StrictModel):
    receipt_id: str
    view_digest: str
    profile_version: str
    parameters: dict[str, Any]
    returned_refs: list[KrittRef]
    returned_rows: int
    returned_bytes: int
    total_rows: int
    truncated: bool
    next_cursor: str | None


class ToolResult(_StrictModel):
    action: Literal["describe", "search", "get"]
    view_digest: str
    scan_id: int
    commit_sha: str
    records: list[ToolRecord]
    summary: dict[str, Any]
    receipt: ToolReceipt


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class KrittTools:
    """Queries emit receipts; only fully delivered get records enter the evidence ledger."""

    def __init__(self, view: KrittView, *, max_rows: int = 20, max_bytes: int = 65_536):
        if not 1 <= max_rows <= 100 or not 4096 <= max_bytes <= 1_048_576:
            raise ValueError("tool limits require 1..100 rows and 4096..1048576 bytes")
        self.view = view
        self.max_rows, self.max_bytes = max_rows, max_bytes
        self._records = view.records
        self._ledger: dict[str, KrittRef] = {}
        self._fragments: dict[str, list[tuple[int, int]]] = {}
        self.receipts: list[ToolResult] = []

    def resolve(self, ref: KrittRef | dict) -> dict:
        ref = KrittRef.model_validate(ref)
        if ref.scan_id != self.view.scan["id"]:
            raise ValueError("reference is outside the fixed scan")
        record = self.view.get(ref.table, ref.id, ref.partition_key)
        if self.view.ref(ref.table, ref.id, ref.partition_key) != ref:
            raise ValueError("reference identity or content hash does not match the fixed view")
        return record

    def validate_references(self, refs: list[KrittRef | dict]) -> list[KrittRef]:
        validated = []
        for value in refs:
            ref = KrittRef.model_validate(value)
            self.resolve(ref)
            if json_digest(ref.model_dump(mode="json")) not in self._ledger:
                raise ValueError("reference was not fully returned by a get query")
            validated.append(ref)
        return validated

    def validate_receipt(self, receipt: ToolReceipt | dict) -> ToolReceipt:
        receipt = ToolReceipt.model_validate(receipt)
        for result in self.receipts:
            if result.receipt == receipt:
                return receipt
        raise ValueError("receipt was not emitted by this tool instance")

    @property
    def ledger(self) -> list[KrittRef]:
        return list(self._ledger.values())

    def save_receipts(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"view_digest": self.view.digest, "profile_version": PROFILE_VERSION,
                                   "ledger": [ref.model_dump(mode="json") for ref in self.ledger],
                                   "receipts": [r.model_dump(mode="json") for r in self.receipts]},
                                  ensure_ascii=False, indent=2), encoding="utf-8")

    def execute(self, action: DescribeAction | SearchAction | GetAction | dict | ToolAction) -> ToolResult:
        if isinstance(action, ToolAction):
            action = action.root
        elif isinstance(action, dict):
            action = ToolAction.model_validate(action).root
        if isinstance(action, DescribeAction):
            result = self._page(action, [], self._description())
        elif isinstance(action, SearchAction):
            result = self._page(action, self._search(action), {"kind": action.kind, "route": action.route,
                                 "labels_are_evidence": False, "search_records_are_summaries": True})
        elif isinstance(action, GetAction):
            root = self.resolve(action.ref)
            summary = {"labels_are_evidence": False, "parent_links_are_generation_lineage": True,
                       "execution_failure_is_not_rejection": True}
            if action.ref.table == "workflows.vulnerabilities":
                summary["review_selection"] = self._review_selection(root["id"])
            result = self._page(action, self._expand(action.ref, root),
                                summary)
        else:
            raise ValueError("unsupported Kritt tool action")
        self.receipts.append(result)
        if action.action == "get":
            for material in result.records:
                key = json_digest(material.ref.model_dump(mode="json"))
                if material.complete:
                    self._ledger[key] = material.ref
                else:
                    spans = self._fragments.setdefault(key, [])
                    spans.append((material.fragment_start, material.fragment_end))
                    end = 0
                    for start, stop in sorted(spans):
                        if start > end:
                            break
                        end = max(end, stop)
                    if end == material.total_characters:
                        self._ledger[key] = material.ref
        return result

    def _description(self) -> dict:
        counts = {kind: len(self._kind_rows(kind)) for kind in KINDS}
        flows = self._kind_rows("flow")
        counts["flows_without_findings"] = sum(not self._findings_for(row) for row in flows)
        return {"repo_full": self.view.scan["repo_full"], "counts": counts, "fields": FIELDS,
                "mechanisms": MECHANISMS, "routes": ROUTES, "profile_version": PROFILE_VERSION,
                "limits": {"rows": self.max_rows, "bytes": self.max_bytes},
                "cautions": ["rank/canonical/valid/scope are analysis labels, not verified premises",
                             "failed/stub/no finding is not proof of safety",
                             "get pages must be consumed before an incomplete record can be cited"]}

    def _kind_rows(self, kind: str) -> list[dict]:
        rows = self._records[KINDS[kind]]
        if kind == "finding":
            return rows
        step_id = self.view.steps_by_layer[kind]["id"]
        return [row for row in rows if row["step_id"] == step_id]

    def _ref(self, table: str, row: dict) -> KrittRef:
        return self.view.ref(table, row["id"], row.get("partition_key"))

    def _findings_for(self, flow: dict) -> list[dict]:
        return [v for v in self._records["workflows.vulnerabilities"]
                if v.get("prev_table") == "workflows.step_results" and v.get("prev_id") == flow["id"]]

    def _flow_jobs(self, flow: dict) -> list[dict]:
        step_id = self.view.steps_by_layer["finding"]["id"]
        return [job for job in self._records["workflows.step_metadata"] if job.get("kind") == "step"
                and job.get("step_id") == step_id and job.get("prev_table") == "workflows.step_results"
                and job.get("prev_id") == flow["id"]]

    def _review_selection(self, finding_id: int) -> dict:
        groups: dict[int, list[dict]] = {}
        for row in self._records["workflows.post_process_metadata"]:
            if (row.get("kind") in {"post_script", "supplemental_post_script"} and row.get("post_script_id") is not None
                    and (row.get("vulnerability_id") == finding_id
                         or finding_id in (row.get("target_vulnerability_ids") or []))):
                groups.setdefault(row["post_script_id"], []).append(row)
        selection = []
        for script_id, history in sorted(groups.items()):
            ordered = sorted(history, key=lambda row: (row.get("run_started_at") or "", row["id"]))
            success = [row for row in ordered if row.get("status") == "completed"]
            latest = ordered[-1]
            selection.append({"post_script_id": script_id, "latest_execution_id": latest["id"],
                              "latest_execution_status": latest.get("status"),
                              "latest_successful_id": success[-1]["id"] if success else None})
        return {"scripts": selection[:10], "total_scripts": len(selection), "truncated": len(selection) > 10,
                "rule": "latest completed execution remains the successful result; later execution errors do not refute it"}

    def _search(self, action: SearchAction) -> list[tuple[KrittRef, str, dict]]:
        terms = [term.casefold() for term in action.terms]
        if action.effect:
            terms.extend(MECHANISMS[action.effect])
        fields = action.fields or FIELDS[action.kind]
        matches = []
        for row in sorted(self._kind_rows(action.kind), key=lambda r: (r.get("partition_key", ""), r["id"])):
            answer = row["json_answer"]
            haystack = " ".join(_json(answer.get(field, "")) for field in fields).casefold()
            if terms and not any(term in haystack for term in terms):
                continue
            path = answer.get({"entrypoint": "entrypoint_file_path", "flow": "flow_file_path",
                               "finding": "file_path"}[action.kind], "")
            if action.path and action.path.casefold() not in str(path).casefold():
                continue
            coverage = None
            if action.kind == "flow":
                findings, jobs = self._findings_for(row), self._flow_jobs(row)
                coverage = {"finding_count": len(findings), "job_statuses": [j.get("status") for j in jobs],
                            "has_stub": any(j.get("stub") is True for j in jobs),
                            "has_execution_failure": any(j.get("status") in {"failed", "interrupted"} for j in jobs)}
                if action.coverage == "no_findings" and findings:
                    continue
                if action.coverage == "stub" and not coverage["has_stub"]:
                    continue
                if action.coverage == "failed" and not coverage["has_execution_failure"]:
                    continue
            excerpt = {field: answer[field] for field in fields if field in answer}
            # Search only navigates; full records are delivered by get.
            excerpt = {field: value if len(_json(value)) <= 600 else _json(value)[:600] + "…"
                       for field, value in excerpt.items()}
            summary = {"fields": excerpt, "coverage": coverage,
                       "canonical": row.get("dedupe_is_canonical"), "rank": row.get("bounty_rank"),
                       "summary_only": True}
            matches.append((self._ref(KINDS[action.kind], row), "search_hit", summary))
        return matches

    def _expand(self, root_ref: KrittRef, root: dict) -> list[tuple[KrittRef, str, dict]]:
        materials: dict[tuple[str, str | None, int], tuple[KrittRef, str, dict]] = {}

        def add(table: str, row: dict, relation: str):
            key = (table, row.get("partition_key"), row["id"])
            if key not in materials:
                materials[key] = (self._ref(table, row), relation, row)

        def chain(table: str, row: dict, relation: str):
            add(table, row, relation)
            if table not in {"workflows.step_results", "workflows.vulnerabilities"}:
                return
            if row.get("prev_table"):
                parents = [p for p in self._records[row["prev_table"]] if p["id"] == row["prev_id"]]
                if len(parents) != 1:
                    raise ValueError("ambiguous or missing source parent")
                chain(row["prev_table"], parents[0], "source_parent")
            if table == "workflows.vulnerabilities":
                job = self.view.get("workflows.step_metadata", row["scan_metadata_id"])
                add("workflows.step_metadata", job, "generation_job")
            else:
                for job in self._records["workflows.step_metadata"]:
                    if (job.get("kind") == "step" and job.get("step_id") == row["step_id"]
                            and job.get("prev_id") == row.get("prev_id")
                            and job.get("prev_table") == row.get("prev_table")):
                        add("workflows.step_metadata", job, "generation_job_history")
                if row["step_id"] == self.view.steps_by_layer["flow"]["id"]:
                    for job in self._flow_jobs(row):
                        add("workflows.step_metadata", job, "investigation_job_history")

        chain(root_ref.table, root, "requested")
        findings = []
        if root_ref.table == "workflows.vulnerabilities":
            findings = [root]
        elif root_ref.table == "workflows.step_results" and root["step_id"] == self.view.steps_by_layer["flow"]["id"]:
            findings = self._findings_for(root)
        expanded = {}
        all_findings = self._records["workflows.vulnerabilities"]
        for finding in findings:
            canonical = finding.get("dedupe_canonical_id") or finding["id"]
            cluster = finding.get("dedupe_cluster_id")
            for candidate in all_findings:
                if (candidate["id"] == canonical or candidate.get("dedupe_canonical_id") == canonical
                        or (cluster is not None and candidate.get("dedupe_cluster_id") == cluster)):
                    expanded[candidate["id"]] = candidate
        for finding in sorted(expanded.values(), key=lambda r: r["id"]):
            chain("workflows.vulnerabilities", finding, "cluster_branch")
        finding_ids = set(expanded)
        for row in self._records["workflows.post_process_metadata"]:
            if row.get("vulnerability_id") in finding_ids or finding_ids.intersection(row.get("target_vulnerability_ids") or []):
                add("workflows.post_process_metadata", row, "review_or_postprocess_history")
        for row in self._records["workflows.vulnerability_enrichments"]:
            if row.get("vulnerability_id") in finding_ids:
                add("workflows.vulnerability_enrichments", row, "enrichment")
        return list(materials.values())

    def _cursor(self, query_digest: str, index: int, fragment: int = 0) -> str:
        return base64.urlsafe_b64encode(_json({"view": self.view.digest, "query": query_digest,
                                              "index": index, "fragment": fragment}).encode()).decode()

    def _position(self, cursor: str | None, query_digest: str, total: int) -> tuple[int, int]:
        if cursor is None:
            return 0, 0
        try:
            value = json.loads(base64.b64decode(cursor.encode(), altchars=b"-_", validate=True))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid Kritt cursor") from exc
        if (not isinstance(value, dict) or set(value) != {"view", "query", "index", "fragment"} or value["view"] != self.view.digest
                or value["query"] != query_digest or type(value["index"]) is not int
                or type(value["fragment"]) is not int or not 0 <= value["index"] < total or value["fragment"] < 0):
            raise ValueError("cursor does not belong to this fixed query")
        return value["index"], value["fragment"]

    def _page(self, action: _StrictModel, materials: list[tuple[KrittRef, str, dict]], summary: dict) -> ToolResult:
        parameters = action.model_dump(mode="json")
        query_digest = json_digest({k: v for k, v in parameters.items() if k != "cursor"})
        total = len(materials)
        index, fragment = self._position(getattr(action, "cursor", None), query_digest, total)
        records: list[ToolRecord] = []

        def result(next_index: int, next_fragment: int = 0) -> ToolResult:
            cursor = self._cursor(query_digest, next_index, next_fragment) if next_index < total else None
            receipt = ToolReceipt(receipt_id=json_digest({"view": self.view.digest, "parameters": parameters,
                                  "records": [r.model_dump(mode="json") for r in records]}),
                                  view_digest=self.view.digest, profile_version=PROFILE_VERSION,
                                  parameters=parameters, returned_refs=[r.ref for r in records],
                                  returned_rows=len(records), returned_bytes=0, total_rows=total,
                                  truncated=cursor is not None, next_cursor=cursor)
            value = ToolResult(action=action.action, view_digest=self.view.digest, scan_id=self.view.scan["id"],
                               commit_sha=self.view.scan["commit_sha"], records=list(records), summary=summary, receipt=receipt)
            for _ in range(4):
                size = len(value.model_dump_json().encode())
                if size == value.receipt.returned_bytes:
                    break
                value.receipt.returned_bytes = size
            return value

        limit = min(getattr(action, "limit", self.max_rows), self.max_rows)
        while index < total and len(records) < limit:
            ref, relation, content = materials[index]
            text = _json(content)
            if fragment >= len(text) and fragment:
                raise ValueError("cursor fragment is outside the record")
            material = ToolRecord(ref=ref, relation=relation, content=content, complete=True,
                                  fragment_end=len(text), total_characters=len(text))
            if fragment == 0:
                records.append(material)
                if result(index + 1).receipt.returned_bytes <= self.max_bytes:
                    index += 1
                    continue
                records.pop()
                if records:
                    break
            # Large original records are transported in explicit contiguous JSON fragments.
            low, high, best = 1, len(text) - fragment, 0
            while low <= high:
                width = (low + high) // 2
                part = material.model_copy(update={"content": {"json_fragment": text[fragment:fragment + width]},
                                                  "complete": False, "fragment_start": fragment,
                                                  "fragment_end": fragment + width})
                records.append(part)
                next_index = index + 1 if fragment + width == len(text) else index
                candidate = result(next_index, 0 if next_index != index else fragment + width)
                records.pop()
                if candidate.receipt.returned_bytes <= self.max_bytes:
                    best, low = width, width + 1
                else:
                    high = width - 1
            if not best:
                raise ValueError("tool byte limit cannot fit its record/receipt metadata")
            records.append(material.model_copy(update={"content": {"json_fragment": text[fragment:fragment + best]},
                                         "complete": False, "fragment_start": fragment, "fragment_end": fragment + best}))
            fragment += best
            if fragment == len(text):
                index, fragment = index + 1, 0
            break
        output = result(index, fragment)
        if output.receipt.returned_bytes > self.max_bytes:
            raise ValueError("tool byte limit cannot fit its summary/receipt metadata")
        return output
