"""Bounded, single-scan Kritt provenance views. Model conclusions remain claims."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from contextlib import suppress
from datetime import date, datetime
from decimal import Decimal
import asyncio
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time

from pydantic import BaseModel, ConfigDict, Field

from cairn.dispatcher.runtime.cancellation import TaskCancellation


PROFILE = "kritt_v1"
DEFAULT_MAX_ROWS = 10_000
DEFAULT_MAX_BYTES = 16 * 1024 * 1024

# Explicit profile fields exclude prompts, sessions, account data and arbitrary scan extra.
FIELDS = {
    "scan": "id workflow_id repo_full repo_kind commit_sha repo_scope status inserted_at updated_at model harness model_provider".split(),
    "workflow": "id step_ids name description inserted_at updated_at".split(),
    "steps": "id name depth output_format output_table multi_output consume_all_previous is_last_step inserted_at updated_at".split(),
    "step_results": "id step_id scan_id workflow_id inserted_at updated_at partition_key prev_id prev_table repeat_run json_answer stub".split(),
    "vulnerabilities": "id scan_id workflow_id scan_metadata_id prev_id prev_table repeat_run json_answer inserted_at updated_at dedupe_is_canonical dedupe_canonical_id dedupe_cluster_id dedupe_reason bounty_rank bounty_rank_impact_level bounty_rank_reasoning rank_root_bug stub".split(),
    "step_metadata": "id scan_id workflow_id step_id prev_id prev_table repeat_run status phase error kind post_process_metadata_id post_script_id vulnerability_id supplemental_run_id target_vulnerability_ids batch_index stub stub_explanation output_json checked_out_commit run_started_at run_time_ms token_count_total model harness model_provider".split(),
    "post_process_metadata": "id scan_id workflow_id post_script_id post_script_name vulnerability_id supplemental_run_id kind batch_index target_vulnerability_ids status phase error output_json checked_out_commit run_started_at run_time_ms token_count_total model harness model_provider".split(),
    "enrichments": "id scan_id vulnerability_id post_script_id post_script_name result stub stub_explanation inserted_at updated_at supplemental_run_id".split(),
    "post_scripts": "id name output_format description inserted_at updated_at".split(),
}
TABLES = {
    "scan": "public.scans", "workflow": "public.llm_workflows", "steps": "public.steps",
    "step_results": "workflows.step_results", "vulnerabilities": "workflows.vulnerabilities",
    "step_metadata": "workflows.step_metadata", "post_process_metadata": "workflows.post_process_metadata",
    "enrichments": "workflows.vulnerability_enrichments", "post_scripts": "public.post_scripts",
}
FORMATS = {
    "entrypoint": dict.fromkeys("entrypoint_kind entrypoint_label entrypoint_handler entrypoint_file_path entrypoint_actor entrypoint_input entrypoint_reachability".split(), "string") | {"entrypoint_line": "number"},
    "flow": dict.fromkeys("flow_summary flow_terminal_action flow_file_path flow_input_to_sink flow_security_relevance".split(), "string") | {"flow_line": "number", "flow_trace": "array"},
    "finding": dict.fromkeys("explanation file_path malicious_input_example summary vulnerability_type malicious_actor".split(), "string") | {"line": "number", "trigger_flow": "array", "exploitable": "boolean"},
}


class KrittError(ValueError):
    pass


def check_control(deadline: float | None = None, cancellation: TaskCancellation | None = None) -> float:
    if cancellation is not None and cancellation.is_cancelled:
        raise InterruptedError(cancellation.reason or "Kritt preparation cancelled")
    remaining = math.inf if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Kritt preparation deadline exhausted")
    return remaining


class KrittRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    table: str
    id: int = Field(gt=0)
    partition_key: str | None = None
    scan_id: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            return int(value)
        return str(value)
    raise TypeError(f"unsupported database value: {type(value).__name__}")


def _encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False, default=_json_default).encode("utf-8")


def _hash(value) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def _positive(value, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise KrittError(f"{label} must be a positive integer")
    return value


def _limits(max_rows: int, max_bytes: int) -> None:
    _positive(max_rows, "max_rows")
    _positive(max_bytes, "max_bytes")


def _job_key(row: dict, step_id: int | None = None) -> tuple:
    return (row["scan_id"], step_id or row["step_id"], row.get("prev_id") or 0,
            row.get("prev_table"), row.get("repeat_run") or 1)


class KrittView:
    def __init__(self, data: dict, *, profile: str = PROFILE,
                 max_rows: int = DEFAULT_MAX_ROWS, max_bytes: int = DEFAULT_MAX_BYTES):
        _limits(max_rows, max_bytes)
        if profile != PROFILE:
            raise KrittError(f"unsupported Kritt schema profile: {profile}")
        if not isinstance(data, dict):
            raise KrittError("Kritt view must be an object")
        normalized = {"snapshot_at": data.get("snapshot_at")}
        total = 0
        for key, fields in FIELDS.items():
            value = data.get(key)
            if key in ("scan", "workflow"):
                if not isinstance(value, dict):
                    raise KrittError(f"missing {key} object")
                normalized[key] = {field: value[field] for field in fields if field in value}
                total += 1
            else:
                if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
                    raise KrittError(f"missing or invalid {key} collection")
                normalized[key] = [{field: row[field] for field in fields if field in row} for row in value]
                normalized[key].sort(key=lambda row: (str(row.get("partition_key", "")), str(row.get("id", 0))))
                total += len(value)
        if total > max_rows:
            raise KrittError("Kritt view row budget exceeded")
        encoded = _encode(normalized)
        if len(encoded) > max_bytes:
            raise KrittError("Kritt view byte budget exceeded")
        self.profile = profile
        self._data = json.loads(encoded)
        self.byte_count = len(encoded)
        self.row_count = total
        self.digest = _hash({"profile": profile, "data": self._data})
        self._index: dict[tuple, dict] = {}
        self._validate()

    @property
    def data(self) -> dict:
        return deepcopy(self._data)

    @property
    def scan(self) -> dict:
        return deepcopy(self._data["scan"])

    @property
    def records(self) -> dict[str, list[dict]]:
        return {TABLES[key]: deepcopy(rows) for key, rows in self._data.items()
                if key in TABLES and isinstance(rows, list)}

    @property
    def steps_by_layer(self) -> dict[str, dict]:
        return deepcopy(self._steps_by_layer)

    def get(self, table: str, id: int, partition_key: str | None = None) -> dict:
        _positive(id, "record id")
        if table not in TABLES.values():
            raise KrittError("unknown Kritt table")
        if partition_key is not None:
            row = self._index.get((table, partition_key, id))
            if row is None:
                raise KrittError("Kritt record not found in this scan/partition")
        else:
            matches = [row for (kind, _, row_id), row in self._index.items() if kind == table and row_id == id]
            if len(matches) != 1:
                raise KrittError("Kritt record missing or ambiguous; use its partition identity")
            row = matches[0]
        return deepcopy(row)

    def ref(self, table: str, id: int, partition_key: str | None = None) -> KrittRef:
        row = self.get(table, id, partition_key)
        return KrittRef(table=table, id=id, partition_key=row.get("partition_key"),
                        scan_id=self._data["scan"]["id"], sha256=_hash(row))

    def save(self, path: Path | str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(_encode({"profile": self.profile, "digest": self.digest, "data": self._data}))
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path | str, *, max_rows: int = DEFAULT_MAX_ROWS,
             max_bytes: int = DEFAULT_MAX_BYTES) -> KrittView:
        _limits(max_rows, max_bytes)
        with Path(path).open("rb") as stream:
            encoded = stream.read(max_bytes + 1024 + 1)
        if len(encoded) > max_bytes + 1024:
            raise KrittError("Kritt view file byte budget exceeded")
        try:
            saved = json.loads(encoded)
        except (ValueError, UnicodeError) as exc:
            raise KrittError("invalid Kritt JSON view") from exc
        if isinstance(saved, dict) and "data" in saved:
            view = cls(saved["data"], profile=saved.get("profile", ""), max_rows=max_rows, max_bytes=max_bytes)
            if saved.get("digest") != view.digest:
                raise KrittError("Kritt view digest mismatch")
            return view
        return cls(saved, max_rows=max_rows, max_bytes=max_bytes)

    def _validate(self) -> None:
        data, scan = self._data, self._data["scan"]
        scan_id = _positive(scan.get("id"), "scan id")
        workflow_id = _positive(scan.get("workflow_id"), "workflow id")
        if scan.get("status") != "completed" or not isinstance(scan.get("repo_full"), str) or not scan["repo_full"].strip():
            raise KrittError("selected scan must be completed and identify its repository")
        commit = scan.get("commit_sha")
        if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
            raise KrittError("selected scan requires a full source commit")
        workflow = data["workflow"]
        if workflow.get("id") != workflow_id or not isinstance(workflow.get("step_ids"), list):
            raise KrittError("scan/workflow relation mismatch")
        self._steps_by_layer = {}
        for depth, (layer, output_format) in enumerate(FORMATS.items()):
            matches = [row for row in data["steps"] if row.get("depth") == depth]
            if len(matches) != 1:
                raise KrittError("unsupported Kritt step topology")
            step = matches[0]
            declared = step.get("output_format")
            try:
                declared = json.loads(declared) if isinstance(declared, str) else declared
            except ValueError as exc:
                raise KrittError("invalid Kritt step output schema") from exc
            expected_table = TABLES["vulnerabilities" if layer == "finding" else "step_results"]
            if declared != output_format or step.get("output_table") != expected_table:
                raise KrittError(f"unsupported {layer} schema for {PROFILE}")
            if step.get("consume_all_previous") is not False or step.get("multi_output") is not True:
                raise KrittError("unsupported Kritt parent/output semantics")
            self._steps_by_layer[layer] = step
        if len(data["steps"]) != 3 or set(workflow["step_ids"]) != {step["id"] for step in data["steps"]}:
            raise KrittError("workflow step selection mismatch")
        for key, table in TABLES.items():
            rows = data[key] if isinstance(data[key], list) else [data[key]]
            for row in rows:
                identity = (table, row.get("partition_key"), _positive(row.get("id"), f"{key} id"))
                if identity in self._index:
                    raise KrittError(f"duplicate {key} identity")
                if "scan_id" in row and row["scan_id"] != scan_id:
                    raise KrittError(f"cross-scan {key} record")
                if "workflow_id" in row and row["workflow_id"] != workflow_id:
                    raise KrittError(f"cross-workflow {key} record")
                if key in ("step_results", "vulnerabilities", "step_metadata", "post_process_metadata", "enrichments") and row.get("scan_id") != scan_id:
                    raise KrittError(f"missing scan identity for {key}")
                if key in ("step_results", "vulnerabilities", "step_metadata", "post_process_metadata") and row.get("workflow_id") != workflow_id:
                    raise KrittError(f"missing workflow identity for {key}")
                if row.get("checked_out_commit") is not None and row["checked_out_commit"] != commit:
                    raise KrittError("execution/source commit mismatch")
                self._index[identity] = row
        entry_id = self._steps_by_layer["entrypoint"]["id"]
        flow_id = self._steps_by_layer["flow"]["id"]
        finding_id = self._steps_by_layer["finding"]["id"]
        successful = defaultdict(list)
        for job in data["step_metadata"]:
            if job.get("kind") == "step":
                if job.get("step_id") not in (entry_id, flow_id, finding_id):
                    raise KrittError("unknown execution step")
                self._parent(job, {entry_id: None, flow_id: entry_id, finding_id: flow_id}[job["step_id"]])
                if job.get("status") == "completed":
                    if job.get("checked_out_commit") != commit:
                        raise KrittError("successful job requires the selected source commit")
                    successful[_job_key(job)].append(job)
            elif job.get("post_process_metadata_id") is not None:
                mirrored = self.get(TABLES["post_process_metadata"], job["post_process_metadata_id"])
                if any(job.get(key) != mirrored.get(key) for key in ("scan_id", "workflow_id", "kind", "status", "vulnerability_id", "supplemental_run_id", "post_script_id")):
                    raise KrittError("post-process mirror relation mismatch")
        if any(len(jobs) != 1 for jobs in successful.values()):
            raise KrittError("ambiguous successful execution identity")
        for row in data["step_results"]:
            layer = "entrypoint" if row.get("step_id") == entry_id else "flow" if row.get("step_id") == flow_id else None
            if layer is None or row.get("partition_key") != f"{scan_id}-{0 if layer == 'entrypoint' else 1}":
                raise KrittError("unsupported result step/partition identity")
            self._parent(row, None if layer == "entrypoint" else entry_id)
            self._answer(row, FORMATS[layer])
            jobs = successful.get(_job_key(row), [])
            if len(jobs) != 1:
                raise KrittError("result has no unique successful execution")
            self._payload(row, jobs[0])
        for row in data["vulnerabilities"]:
            self._parent(row, flow_id)
            self._answer(row, FORMATS["finding"])
            job = self.get(TABLES["step_metadata"], _positive(row.get("scan_metadata_id"), "finding execution id"))
            if job.get("kind") != "step" or job.get("status") != "completed" or _job_key(job) != _job_key(row, finding_id):
                raise KrittError("finding/execution relation mismatch")
            self._payload(row, job)
            if row.get("dedupe_canonical_id") is not None:
                head = self.get(TABLES["vulnerabilities"], row["dedupe_canonical_id"])
                if head.get("dedupe_is_canonical") is not True or head.get("dedupe_canonical_id") != head["id"]:
                    raise KrittError("invalid dedupe canonical relation")
        for key in ("post_process_metadata", "enrichments"):
            for row in data[key]:
                targets = row.get("target_vulnerability_ids") or []
                if row.get("vulnerability_id") is not None:
                    targets = [*targets, row["vulnerability_id"]]
                for target in targets:
                    self.get(TABLES["vulnerabilities"], target)
                if row.get("post_script_id") is not None:
                    self.get(TABLES["post_scripts"], row["post_script_id"])

    def _parent(self, row: dict, parent_step: int | None) -> None:
        if parent_step is None:
            if row.get("prev_id") not in (None, 0) or row.get("prev_table") is not None:
                raise KrittError("entrypoint has an unexpected parent")
            return
        if row.get("prev_table") != TABLES["step_results"]:
            raise KrittError("unsupported parent table")
        parent = self.get(TABLES["step_results"], _positive(row.get("prev_id"), "parent id"))
        if parent.get("step_id") != parent_step:
            raise KrittError("parent layer mismatch")

    @staticmethod
    def _answer(row: dict, output_format: dict) -> None:
        answer = row.get("json_answer")
        if not isinstance(answer, dict) or set(answer) != set(output_format):
            raise KrittError("record does not match the selected output schema")
        expected = {"string": str, "array": list, "boolean": bool, "number": (int, float)}
        if any(not isinstance(answer[key], expected[kind]) or (kind == "number" and isinstance(answer[key], bool)) for key, kind in output_format.items()):
            raise KrittError("record field type does not match the selected output schema")

    @staticmethod
    def _payload(row: dict, job: dict) -> None:
        output = job.get("output_json")
        if not isinstance(output, dict) or not isinstance(output.get("results"), list) or row["json_answer"] not in output["results"]:
            raise KrittError("record is absent from its successful execution output")


def read_kritt_view(dsn: str, *, repo_full: str, scan_id: int | None = None,
                    commit: str | None = None, profile: str = PROFILE,
                    max_rows: int = DEFAULT_MAX_ROWS, max_bytes: int = DEFAULT_MAX_BYTES,
                    statement_timeout_ms: int = 10_000, deadline: float | None = None,
                    cancellation: TaskCancellation | None = None) -> KrittView:
    """Select and read one completed scan in a short read-only snapshot transaction."""
    import psycopg
    from psycopg.rows import dict_row

    _limits(max_rows, max_bytes)
    _positive(statement_timeout_ms, "statement_timeout_ms")
    if profile != PROFILE:
        raise KrittError(f"unsupported Kritt schema profile: {profile}")
    if not repo_full.strip():
        raise KrittError("repository selection is required")
    if scan_id is not None:
        _positive(scan_id, "scan_id")
    if commit is not None and not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise KrittError("commit selection must be a full commit")
    connection = None

    async def read_view():
        nonlocal connection
        check_control(deadline, cancellation)
        parameters = psycopg.conninfo.conninfo_to_dict(dsn)
        host = parameters.get("host") or os.environ.get("PGHOST", "")
        addresses = parameters.get("hostaddr") or os.environ.get("PGHOSTADDR", "")
        # ponytail: MVP requires pre-resolved addresses; add an isolated resolver when hostname-only DSNs are needed.
        if parameters.get("service") or os.environ.get("PGSERVICE"):
            raise KrittError("Kritt MVP requires an explicit IP/socket/hostaddr DSN, without a connection service")
        try:
            if addresses:
                for address in addresses.split(","):
                    ipaddress.ip_address(address)
            elif host and all(value.startswith("/") for value in host.split(",")):
                pass
            elif host:
                for value in host.split(","):
                    ipaddress.ip_address(value)
                addresses = host
            else:
                raise ValueError("missing explicit host")
        except ValueError:
            raise KrittError("Kritt MVP requires a numeric IP, Unix socket, or pre-resolved hostaddr; DNS is not performed") from None
        connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True,
            connect_timeout=max(1, math.ceil(min(10, check_control(deadline, cancellation)))),
            row_factory=dict_row, host=host, hostaddr=addresses)
        check_control(deadline, cancellation)

        async def execute(query, params=()):
            remaining = check_control(deadline, cancellation)
            limit = max(1, int(min(statement_timeout_ms, remaining * 1000)))
            await connection.execute("SELECT set_config('statement_timeout', %s, true)", (str(limit),))
            check_control(deadline, cancellation)
            result = await connection.execute(query, params)
            check_control(deadline, cancellation)
            return result

        try:
            async with connection.transaction():
                await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                check_control(deadline, cancellation)
                available = await (await execute("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema IN ('public','workflows') AND table_name = ANY(%s)", ([table.split(".")[1] for table in TABLES.values()],))).fetchall()
                columns = defaultdict(set)
                for column in available:
                    columns[f"{column['table_schema']}.{column['table_name']}"].add(column["column_name"])
                for key, fields in FIELDS.items():
                    if not set(fields) <= columns[TABLES[key]]:
                        raise KrittError(f"unsupported database schema for {TABLES[key]}")
                params, clauses = [repo_full], ["repo_full = %s", "status = 'completed'"]
                if scan_id is not None:
                    params.append(scan_id)
                    clauses.append("id = %s")
                if commit is not None:
                    params.append(commit)
                    clauses.append("commit_sha = %s")
                scan = await (await execute(f"SELECT {','.join(FIELDS['scan'])} FROM public.scans WHERE {' AND '.join(clauses)} ORDER BY inserted_at DESC, id DESC LIMIT 1", params)).fetchone()
                if scan is None:
                    raise KrittError("no completed scan matches the selected repository/scan/commit")
                snapshot = {"snapshot_at": (await (await execute("SELECT CURRENT_TIMESTAMP AS at")).fetchone())["at"], "scan": scan}
                used_rows, used_bytes = 1, len(_encode(scan))

                async def read(key: str, where: str, values: tuple) -> list:
                    nonlocal used_rows, used_bytes
                    rows = []
                    check_control(deadline, cancellation)
                    async with connection.cursor(name=f"kritt_{key}") as cursor:
                        limit = max(1, int(min(statement_timeout_ms, check_control(deadline, cancellation) * 1000)))
                        await connection.execute("SELECT set_config('statement_timeout', %s, true)", (str(limit),))
                        check_control(deadline, cancellation)
                        await cursor.execute(f"SELECT {','.join(FIELDS[key])} FROM {TABLES[key]} WHERE {where} ORDER BY id", values)
                        check_control(deadline, cancellation)
                        iterator = cursor.__aiter__()
                        while True:
                            check_control(deadline, cancellation)
                            try:
                                row = await iterator.__anext__()
                            except StopAsyncIteration:
                                break
                            check_control(deadline, cancellation)
                            used_rows += 1
                            used_bytes += len(_encode(row))
                            if used_rows > max_rows or used_bytes > max_bytes:
                                raise KrittError("Kritt view read budget exceeded")
                            rows.append(row)
                    return rows

                workflow = await read("workflow", "id = %s", (scan["workflow_id"],))
                if len(workflow) != 1:
                    raise KrittError("scan workflow is missing")
                snapshot["workflow"] = workflow[0]
                snapshot["steps"] = await read("steps", "id = ANY(%s)", (workflow[0]["step_ids"],))
                for key in ("step_results", "vulnerabilities", "step_metadata", "post_process_metadata", "enrichments"):
                    snapshot[key] = await read(key, "scan_id = %s", (scan["id"],))
                script_ids = sorted({row["post_script_id"] for key in ("post_process_metadata", "enrichments") for row in snapshot[key] if row.get("post_script_id") is not None})
                snapshot["post_scripts"] = await read("post_scripts", "id = ANY(%s)", (script_ids,))
                check_control(deadline, cancellation)
                view = KrittView(snapshot, profile=profile, max_rows=max_rows, max_bytes=max_bytes)
                check_control(deadline, cancellation)
                return view
        finally:
            await connection.close()

    async def bounded_read():
        check_control(deadline, cancellation)
        task = asyncio.create_task(read_view())
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=min(.05, check_control(deadline, cancellation)))
            check_control(deadline, cancellation)
            return task.result()
        finally:
            if connection is not None and not connection.closed:
                await connection.close()
            if not task.done():
                # Close before cancelling: otherwise driver cancellation can wait beyond our deadline.
                task.cancel()
            # Await consumes the cancellation exception and releases a pending connect's socket.
            with suppress(asyncio.CancelledError, Exception):
                await task

    try:
        return asyncio.run(bounded_read())
    except KrittError:
        raise
    except psycopg.Error as exc:
        check_control(deadline, cancellation)
        # Connection messages can contain DSN details; only expose the exception class.
        raise KrittError(f"Kritt database read failed ({type(exc).__name__})") from None
