import hashlib
import json
import os
from pathlib import Path
import sys

from click.testing import CliRunner
import pytest

from cairn.cli import main
from cairn.controller import kritt_probe
from cairn.controller.kritt import KrittView
from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from test_controller import config_payload
from test_kritt_tools import snapshot


@pytest.fixture
def probe(tmp_path, monkeypatch):
    view = KrittView(snapshot())
    repo = tmp_path / "source"
    files = {}
    for rows in view.records.values():
        for row in rows:
            for key, value in row.get("json_answer", {}).items():
                if key.endswith("file_path"):
                    path = repo / value
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("<?php\nrequireLogin();\necho $input;\n")
                    files[value] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({"format_version": 1, "repo_full": view.scan["repo_full"],
        "commit_sha": view.scan["commit_sha"], "git_tree": "b" * 40, "archive_sha256": "c" * 64,
        "tracked_count": len(files), "files": files, "path_aliases": ["/workspace"]}))
    view_path = tmp_path / "view.json"
    view.save(view_path)
    payload = config_payload()
    payload["controller"]["kritt"] = {"targets": {"test": {
        "repo_full": view.scan["repo_full"], "source_root": str(repo), "source_manifest": str(manifest)}},
        "response_rows": 1, "response_bytes": 4096, "max_queries": 30}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload))
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
context = json.loads(sys.argv[-1].rsplit("\\n\\n", 1)[1])
assert "CAIRN_KRITT_READONLY_DSN" not in os.environ
phase = context["probe_phase"]
mode = os.environ.get("PROBE_TEST_MODE")
if mode == "invalid":
    print("not-json")
    sys.exit()
if phase == "search":
    result = {"action": "query", "query": {"action": "search", "kind": "finding",
        "route": "candidates", "effect": "rce", "limit": 1}}
elif phase == "get":
    result = {"action": "query", "query": {"action": "get", "ref": context["search_hits"][0]}}
else:
    materials = json.loads(pathlib.Path(context["materials_path"]).read_text())
    selected = next(row for row in materials if row["ref"] == context["selected_reference"])
    path = selected["content"]["json_answer"]["file_path"]
    source = pathlib.Path(context["repo_path"]) / path
    quote = "\\n".join(source.read_text().splitlines()[:2])
    if mode == "change_source":
        source.write_text("changed")
    if mode == "false_quote":
        quote = "invented evidence"
    result = {"action": "finish", "result": {"references": [selected["ref"]], "path": path,
        "start_line": 1, "end_line": 2, "quotation": quote, "observation": "The handler requires login.",
        "conditions": ["Reachability still needs independent review."]}}
print(json.dumps(result))
''')
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CAIRN_KRITT_READONLY_DSN", "password=must-not-reach-agent")
    return DispatchConfig.load(config_path), config_path, view_path, tmp_path / "output"


def run_probe(probe, **kwargs):
    config, _, view, output = probe
    return kritt_probe.run_kritt_probe(config, target_name="test", user_goal="Review RCE mechanisms",
                                     output=output, view_path=view, **kwargs)


@pytest.mark.parametrize("override_dsn", [False, True])
def test_probe_search_get_all_pages_and_native_source_observation(probe, override_dsn):
    if override_dsn:
        probe[0].workers[0].env["CAIRN_KRITT_READONLY_DSN"] = "worker-must-not-inject-secret"
    result = run_probe(probe)
    assert result["status"] == "success" and result["calls"] == 3
    assert result["queries"] > result["calls"]  # Pagination consumes the same query budget.
    assert result["observation"]["status"] == "unverified"
    assert result["observation"]["quotation"] == "<?php\nrequireLogin();"
    output = probe[-1]
    receipts = json.loads((output / "query-receipts.json").read_text())
    assert len(receipts["ledger"]) > 5
    assert (output / "selected-materials.json").exists()
    observe = json.loads((output / "call-03/input.json").read_text())
    assert observe["probe_phase"] == "observe" and observe["materials_path"]
    assert observe["output_schema"]["properties"]["action"]["enum"] == ["finish"]
    assert "must-not-reach-agent" not in json.dumps(observe)


@pytest.mark.parametrize("mode,error", [("invalid", "Agent-call budget"),
                                      ("false_quote", "Agent-call budget"),
                                      ("change_source", "source files changed")])
def test_probe_invalid_output_and_false_or_changed_source_do_not_succeed(probe, monkeypatch, mode, error):
    monkeypatch.setenv("PROBE_TEST_MODE", mode)
    probe[0].controller.kritt.max_agent_calls = 4
    with pytest.raises(ValueError, match=error):
        run_probe(probe)
    failure = json.loads((probe[-1] / "failure.json").read_text())
    assert failure["status"] == "error" and not (probe[-1] / "summary.json").exists()
    if mode == "false_quote":
        observe = json.loads((probe[-1] / "call-04/input.json").read_text())
        assert observe["materials_path"] and observe["selected_reference"]
        assert "quotation" in observe["last_error"]["observation_error"]


def test_probe_get_pagination_is_bounded_and_does_not_finish_partial_branch(probe):
    probe[0].controller.kritt.max_queries = 2
    with pytest.raises(RuntimeError, match="query/time budget"):
        run_probe(probe)
    failure = json.loads((probe[-1] / "failure.json").read_text())
    assert failure["queries"] == 2 and failure["calls"] == 2
    assert not (probe[-1] / "selected-materials.json").exists()


def test_probe_source_mismatch_fails_before_agent(probe, monkeypatch):
    manifest = Path(probe[0].controller.kritt.targets["test"].source_manifest)
    data = json.loads(manifest.read_text())
    data["commit_sha"] = "d" * 40
    manifest.write_text(json.dumps(data))
    monkeypatch.setattr(kritt_probe, "call_agent", lambda *a, **kw: pytest.fail("must not spend Agent calls"))
    with pytest.raises(ValueError, match="does not match"):
        run_probe(probe)


def test_probe_cancelled_before_query_preserves_failure(probe, monkeypatch):
    token = TaskCancellation()
    token.cancel("user cancelled")
    monkeypatch.setattr(kritt_probe, "call_agent", lambda *a, **kw: pytest.fail("must not start Agent"))
    with pytest.raises(KeyboardInterrupt, match="user cancelled"):
        run_probe(probe, cancellation=token)
    assert json.loads((probe[-1] / "failure.json").read_text())["calls"] == 0


def test_probe_cli_is_a_readonly_probe_not_project_start(probe):
    _, config, view, output = probe
    result = CliRunner().invoke(main, ["kritt-probe", "--config", str(config), "--target", "test",
        "--goal", "Review RCE mechanisms", "--view", str(view), "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["calls"] == 3
    assert not list(output.glob("**/project.json"))


def test_probe_api_can_use_online_reader_without_passing_dsn_to_model(probe, monkeypatch):
    config, _, view_path, output = probe
    reads = []

    def read(dsn, **selection):
        reads.append(selection)
        assert dsn == "password=must-not-reach-agent"
        return KrittView.load(view_path)

    monkeypatch.setattr(kritt_probe, "read_kritt_view", read)
    result = kritt_probe.run_kritt_probe(config, target_name="test", user_goal="Review RCE mechanisms", output=output)
    assert result["status"] == "success" and len(reads) == 1
    assert reads[0]["repo_full"] == "easysoft/zentaopms"


def test_kritt_source_paths_are_relative_to_config_not_launch_directory(probe, tmp_path, monkeypatch):
    _, config_path, _, _ = probe
    data = json.loads(config_path.read_text())
    target = data["controller"]["kritt"]["targets"]["test"]
    target["source_root"], target["source_manifest"] = "source", "source-manifest.json"
    config_path.write_text(json.dumps(data))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    target = DispatchConfig.load(config_path).controller.kritt.targets["test"]
    assert Path(target.source_root) == config_path.parent / "source"
    assert Path(target.source_manifest) == config_path.parent / "source-manifest.json"


def test_probe_example_loads_with_registered_worker_and_secret_exclusion():
    config = DispatchConfig.load(Path(__file__).resolve().parents[2] / "dispatch.kritt-probe.example.yaml")
    assert config.workers[0].type == "claudecode"
    assert config.controller.kritt.dsn_env in config.local.excluded_env
