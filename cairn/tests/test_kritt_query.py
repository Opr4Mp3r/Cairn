import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from cairn.controller.kritt_query import collect_queries, run_query, write_json
from cairn.controller.kritt_tools import KrittTools
from test_kritt_probe import probe


@pytest.fixture
def query_context(probe):
    config, _, view, output = probe
    settings = config.controller.kritt
    folder = output.parent / "query-run"
    folder.mkdir()
    from cairn.controller.kritt import KrittView
    context = folder / "kritt-context.json"
    write_json(context, {
        "view_path": str(view), "view_digest": KrittView.load(view).digest,
        "queries_dir": str(folder / "queries"), "max_rows": settings.max_rows, "max_bytes": settings.max_bytes,
        "response_rows": settings.response_rows, "response_bytes": settings.response_bytes,
    })
    return context


def cli(context, request):
    result = subprocess.run([sys.executable, "-m", "cairn.controller.kritt_query", "--context", str(context)],
                            input=json.dumps(request), capture_output=True, text=True, cwd=context.parent,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_native_cli_search_cursor_and_get_do_not_overwrite_materials(query_context):
    query = {"action": "search", "kind": "finding", "fields": ["summary", "file_path"]}
    first = cli(query_context, query)
    second = cli(query_context, {**query, "cursor": first["next_cursor"]})
    assert first["query_id"] != second["query_id"]
    assert first["records"][0]["ref"] != second["records"][0]["ref"]
    get = cli(query_context, {"action": "get", "ref": first["records"][0]["ref"]})
    assert get["material_count"] > 5 and "records" not in get
    materials = json.loads(Path(get["materials_path"]).read_text())
    assert any(row["ref"]["table"] == "workflows.vulnerability_enrichments" for row in materials)
    counts = collect_queries(query_context)
    assert counts["queries"] == 3 and counts["query_pages"] > 3
    aggregate = json.loads((query_context.parent / "query-receipts.json").read_text())
    assert len(aggregate["queries"]) == 3 and len(aggregate["ledger"]) == len(materials)


def test_failed_get_does_not_deliver_a_partial_branch(query_context, monkeypatch):
    search = run_query(query_context, {"action": "search", "kind": "finding", "fields": ["summary"]})
    original = KrittTools.execute
    pages = 0
    def interrupt(tools, action):
        nonlocal pages
        pages += 1
        if pages == 2:
            raise ValueError("page failed")
        return original(tools, action)
    monkeypatch.setattr(KrittTools, "execute", interrupt)
    with pytest.raises(ValueError, match="page failed"):
        run_query(query_context, {"action": "get", "ref": search["records"][0]["ref"]})
    counts = collect_queries(query_context)
    assert counts["queries"] == 2
    assert json.loads((query_context.parent / "selected-materials.json").read_text()) == []
    saved = json.loads((query_context.parent / "query-receipts.json").read_text())
    assert saved["queries"][-1]["status"] == "error" and saved["ledger"] == []


def test_get_resolves_full_branch_even_if_given_a_previous_cursor(query_context):
    search = cli(query_context, {"action": "search", "kind": "finding", "fields": ["summary"]})
    query = {"action": "get", "ref": search["records"][0]["ref"]}
    first = cli(query_context, query)
    saved = json.loads(Path(first["receipts_path"]).read_text())
    repeat = cli(query_context, {**query, "cursor": saved["receipts"][0]["receipt"]["next_cursor"]})
    assert repeat["material_count"] == first["material_count"]
    collect_queries(query_context)
    assert len(json.loads((query_context.parent / "selected-materials.json").read_text())) == first["material_count"]
