from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from cairn.controller.kritt import FIELDS, FORMATS, TABLES, KrittError, KrittRef, KrittView, read_kritt_view


@pytest.fixture
def snapshot():
    commit = "a" * 40
    data = {"snapshot_at": "2026-10-06T00:00:00Z",
            "scan": {"id": 19, "workflow_id": 4, "repo_full": "example/project", "commit_sha": commit,
                     "status": "completed", "inserted_at": "2026-10-06", "extra": {"token": "omit-me"}},
            "workflow": {"id": 4, "step_ids": [7, 12, 20], "extra": {"account": "omit-me"}},
            "steps": [], "step_results": [], "vulnerabilities": [], "step_metadata": [],
            "post_process_metadata": [], "enrichments": [], "post_scripts": []}
    for depth, (layer, fields) in enumerate(FORMATS.items()):
        step_id = data["workflow"]["step_ids"][depth]
        data["steps"].append({"id": step_id, "depth": depth, "output_format": json.dumps(fields),
                              "output_table": TABLES["vulnerabilities" if depth == 2 else "step_results"],
                              "consume_all_previous": False, "multi_output": True, "content": "omit-me"})
        answer = {key: {"string": "installer input", "number": 8, "array": ["input → sink"], "boolean": True}[kind]
                  for key, kind in fields.items()}
        parent = {} if depth == 0 else {"prev_id": 100 + depth - 1, "prev_table": "workflows.step_results"}
        shared = {"scan_id": 19, "workflow_id": 4, "prev_id": 0, "prev_table": None, "repeat_run": 1, **parent}
        job = {**shared, "id": 300 + depth, "step_id": step_id, "kind": "step", "status": "completed",
               "checked_out_commit": commit, "output_json": {"results": [answer]}, "prompt_filled": "omit-me"}
        data["step_metadata"].append(job)
        if depth < 2:
            data["step_results"].append({**shared, "id": 100 + depth, "step_id": step_id,
                                         "partition_key": f"19-{depth}", "json_answer": answer})
        else:
            data["vulnerabilities"].append({**shared, "id": 102, "scan_metadata_id": job["id"],
                                            "json_answer": answer, "dedupe_is_canonical": True,
                                            "dedupe_canonical_id": 102})
    data["post_scripts"] = [{"id": 6, "name": "Scope", "output_format": '{"is_valid":"boolean"}', "content": "omit-me"}]
    data["post_process_metadata"] = [{"id": 500, "scan_id": 19, "workflow_id": 4, "post_script_id": 6,
                                      "vulnerability_id": 102, "kind": "post_script", "status": "completed",
                                      "checked_out_commit": commit, "output_json": {"results": [{"is_valid": True}]}}]
    data["enrichments"] = [{"id": 600, "scan_id": 19, "vulnerability_id": 102, "post_script_id": 6,
                           "result": {"is_valid": True}}]
    return data


def test_view_profile_lineage_and_immutable_identity(snapshot, tmp_path):
    view = KrittView(snapshot)
    assert view.steps_by_layer["flow"]["id"] == 12  # The profile does not assume step IDs 1/2/3.
    assert "omit-me" not in json.dumps(view.data)
    assert view.get("workflows.step_results", 101, "19-1")["prev_id"] == 100
    ref = view.ref("workflows.step_results", 101)
    assert isinstance(ref, KrittRef) and ref.scan_id == 19 and ref.partition_key == "19-1"
    copy = view.data
    copy["scan"]["commit_sha"] = "b" * 40
    assert view.scan["commit_sha"] == "a" * 40
    path = tmp_path / "view.json"
    view.save(path)
    assert KrittView.load(path).digest == view.digest
    saved = json.loads(path.read_text())
    saved["data"]["scan"]["repo_full"] = "other/project"
    path.write_text(json.dumps(saved))
    with pytest.raises(KrittError, match="digest mismatch"):
        KrittView.load(path)


def test_postgres_integral_numeric_identity_normalizes_without_weakening_validation(snapshot):
    expected = KrittView(snapshot).digest
    for row in snapshot["step_metadata"]:
        row["workflow_id"] = Decimal(row["workflow_id"])
    assert KrittView(snapshot).digest == expected
    snapshot["step_metadata"][0]["workflow_id"] = Decimal("4.5")
    with pytest.raises(KrittError, match="cross-workflow"):
        KrittView(snapshot)


@pytest.mark.parametrize("mutation, message", [
    (lambda s: s["step_results"][1].update(prev_id=999), "missing or ambiguous"),
    (lambda s: s["step_results"][1].update(prev_id=101), "parent layer"),
    (lambda s: s["step_results"][1].update(scan_id=20), "cross-scan"),
    (lambda s: s["step_results"][1].pop("workflow_id"), "workflow identity"),
    (lambda s: s["step_results"][1].update(partition_key="20-1"), "step/partition"),
    (lambda s: s["step_metadata"][1].update(checked_out_commit="b" * 40), "commit mismatch"),
    (lambda s: s["step_metadata"][1].update(output_json={"results": []}), "execution output"),
    (lambda s: s["vulnerabilities"][0].update(scan_metadata_id=301), "execution relation"),
    (lambda s: s["vulnerabilities"][0].update(dedupe_canonical_id=999), "missing or ambiguous"),
    (lambda s: s["steps"][1].update(output_format='{"guess":"string"}'), "unsupported flow schema"),
    (lambda s: s["post_process_metadata"][0].update(vulnerability_id=999), "missing or ambiguous"),
    (lambda s: s["enrichments"][0].update(scan_id=20), "cross-scan"),
    (lambda s: s["step_results"].append(deepcopy(s["step_results"][0])), "duplicate"),
    (lambda s: s["step_metadata"].append({**s["step_metadata"][1], "id": 390}), "ambiguous successful"),
])
def test_invalid_provenance_fails_before_use(snapshot, mutation, message):
    mutation(snapshot)
    with pytest.raises(KrittError, match=message):
        KrittView(snapshot)


def test_no_finding_flow_and_failed_review_are_not_verdicts(snapshot):
    snapshot["vulnerabilities"] = []
    snapshot["post_process_metadata"] = []
    snapshot["enrichments"] = []
    snapshot["step_metadata"][2].update(stub=True, output_json={"stub": True, "results": []})
    view = KrittView(snapshot)
    assert len(view.records["workflows.step_results"]) == 2
    assert view.records["workflows.vulnerabilities"] == []
    snapshot["step_metadata"][2].update(status="failed", checked_out_commit=None, output_json=None,
                                        error="checkout not available")
    assert KrittView(snapshot).get("workflows.step_metadata", 302)["status"] == "failed"


def test_view_limits_profiles_and_ref_validation(snapshot, tmp_path):
    with pytest.raises(KrittError, match="row budget"):
        KrittView(snapshot, max_rows=3)
    with pytest.raises(KrittError, match="byte budget"):
        KrittView(snapshot, max_bytes=100)
    with pytest.raises(KrittError, match="unsupported Kritt schema"):
        KrittView(snapshot, profile="unknown")
    path = tmp_path / "large.json"
    path.write_text("x" * 2048)
    with pytest.raises(KrittError, match="file byte budget"):
        KrittView.load(path, max_bytes=100)
    with pytest.raises(ValueError):
        KrittRef(table="workflows.vulnerabilities", id="102", scan_id=19, sha256="a" * 64)
    with pytest.raises(KrittError, match="unknown Kritt table"):
        KrittView(snapshot).get("accounts", 1)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params):
        self.connection.calls.append((query, params))
        for key, table in TABLES.items():
            if f"FROM {table} " in query:
                value = self.connection.snapshot[key]
                self.rows = value if isinstance(value, list) else [value]
                break
        return self

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    def __init__(self, snapshot):
        self.snapshot, self.calls = snapshot, []
        self.transactions = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def transaction(self):
        self.transactions += 1
        return self

    def cursor(self, **kwargs):
        return FakeCursor(self)

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if "information_schema" in query:
            rows = [{"table_schema": table.split(".")[0], "table_name": table.split(".")[1], "column_name": field}
                    for key, table in TABLES.items() for field in FIELDS[key]]
        elif "FROM public.scans" in query:
            rows = [self.snapshot["scan"]]
        elif "CURRENT_TIMESTAMP" in query:
            rows = [{"at": self.snapshot["snapshot_at"]}]
        else:
            rows = []
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)


class AsyncResult:
    def __init__(self, response):
        self.response = response

    async def fetchall(self):
        return self.response.fetchall()

    async def fetchone(self):
        return self.response.fetchone()


class FakeAsyncCursor(FakeCursor):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, query, params):
        return super().execute(query, params)

    def __aiter__(self):
        self.iterator = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


class FakeAsyncConnection(FakeConnection):
    closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, query, params=()):
        return AsyncResult(super().execute(query, params))

    def cursor(self, **kwargs):
        return FakeAsyncCursor(self)

    async def close(self):
        self.closed = True


def patch_connection(monkeypatch, connection):
    psycopg = pytest.importorskip("psycopg")

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)


def test_pg_uses_one_read_only_transaction_parameterized_selection_and_scan_scope(snapshot, monkeypatch):
    connection = FakeAsyncConnection(snapshot)
    patch_connection(monkeypatch, connection)
    view = read_kritt_view("host=127.0.0.1", repo_full="example/project", scan_id=19, commit="a" * 40)
    assert view.scan["id"] == 19 and connection.transactions == 1
    assert connection.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    selection = next(call for call in connection.calls if "FROM public.scans" in call[0])
    assert selection[1] == ["example/project", 19, "a" * 40]
    assert "ORDER BY inserted_at DESC, id DESC LIMIT 1" in selection[0]
    for key in ("step_results", "vulnerabilities", "step_metadata", "post_process_metadata", "enrichments"):
        query, params = next(call for call in connection.calls if f"FROM {TABLES[key]} " in call[0])
        assert "scan_id = %s" in query and params == (19,)
    queries = " ".join(call[0] for call in connection.calls)
    assert "prompt_filled" not in queries and "extra" not in queries and "content" not in queries


def test_pg_unknown_profile_does_not_open_connection(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", lambda *args, **kwargs: pytest.fail("connection must not be opened"))
    with pytest.raises(KrittError, match="unsupported Kritt schema"):
        read_kritt_view("host=127.0.0.1", repo_full="example/project", profile="unknown")


def test_pg_credential_errors_are_not_exposed(monkeypatch):
    psycopg = pytest.importorskip("psycopg")

    async def fail_connect(*args, **kwargs):
        raise psycopg.OperationalError("password=secret DB connection failed")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fail_connect)
    with pytest.raises(KrittError, match="OperationalError") as error:
        read_kritt_view("host=127.0.0.1 password=secret", repo_full="example/project")
    assert "secret" not in str(error.value)


def test_pg_missing_profile_column_fails_before_reading_material(snapshot, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    connection = FakeAsyncConnection(snapshot)
    execute = connection.execute

    async def missing_column(query, params=()):
        response = await execute(query, params)
        if "information_schema" in query:
            columns = [row for row in await response.fetchall() if row["column_name"] != "scan_metadata_id"]
            return AsyncResult(SimpleNamespace(fetchall=lambda: columns))
        return response

    monkeypatch.setattr(connection, "execute", missing_column)
    patch_connection(monkeypatch, connection)
    with pytest.raises(KrittError, match="unsupported database schema"):
        read_kritt_view("host=127.0.0.1", repo_full="example/project")
    assert not any("FROM public.scans" in query for query, _ in connection.calls)
