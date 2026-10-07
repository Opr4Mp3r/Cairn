from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cairn.controller.kritt_source import assert_source_unchanged, check_observation, normalize_source_path, verify_source


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    (repo / "module").mkdir(parents=True)
    (repo / "module" / "entry.php").write_text("<?php\nrequireLogin();\necho $input;\n")
    # Tracked dependencies must be included even where ordinary audit baselines skip them.
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "guard.js").write_text("checkUser();\n")
    (repo / "install.php.tmp").write_text("<?php\ninstall();\n")
    files = {path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in repo.rglob("*") if path.is_file()}
    payload = {
        "format_version": 1, "repo_full": "example/target", "commit_sha": "a" * 40,
        "git_tree": "b" * 40, "archive_sha256": "c" * 64, "tracked_count": len(files),
        "files": files, "path_aliases": ["/workspace"], "path_mappings": {},
    }
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps(payload))
    view = SimpleNamespace(scan={"repo_full": "example/target", "commit_sha": "a" * 40})
    return repo, view, manifest, payload


def test_complete_archive_and_precise_source_observation(source):
    repo, view, manifest, payload = source
    baseline = verify_source(repo, view, manifest)
    assert baseline["files"] == payload["files"]
    assert baseline["source_manifest"]["commit_sha"] == view.scan["commit_sha"]
    assert_source_unchanged(repo, baseline)
    evidence = check_observation(repo, baseline, "/workspace/module/entry.php", 1, 2, "<?php\nrequireLogin();")
    assert evidence.path == "module/entry.php"
    assert evidence.sha256 == payload["files"][evidence.path]
    assert normalize_source_path(repo, baseline, str(repo / "module" / "entry.php")) == evidence.path


@pytest.mark.parametrize("kind", ["missing", "modified", "extra", "symlink", "directory_symlink"])
def test_incomplete_or_changed_source_cannot_prepare(source, kind):
    repo, view, manifest, _payload = source
    target = repo / "module" / "entry.php"
    if kind == "missing":
        target.unlink()
    elif kind == "modified":
        target.write_text("replacement")
    elif kind == "extra":
        (repo / "extra.php").write_text("untracked")
    elif kind == "symlink":
        target.unlink()
        target.symlink_to(manifest)
    else:
        (repo / "linked").symlink_to(manifest.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        verify_source(repo, view, manifest)


@pytest.mark.parametrize("key,value", [
    ("repo_full", "other/target"), ("commit_sha", "d" * 40), ("tracked_count", 2),
    ("format_version", 2), ("git_tree", "not-a-hash"), ("archive_sha256", "not-a-hash"),
])
def test_scan_identity_and_manifest_contract(source, key, value):
    repo, view, manifest, payload = source
    payload[key] = value
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        verify_source(repo, view, manifest)


def test_manifest_is_external_and_required(source):
    repo, view, manifest, _payload = source
    with pytest.raises(ValueError, match="external manifest"):
        verify_source(repo, view, None)
    internal = repo / "manifest.json"
    internal.write_bytes(manifest.read_bytes())
    with pytest.raises(ValueError, match="outside"):
        verify_source(repo, view, internal)


@pytest.mark.parametrize("path", ["../outside.php", "module/../module/entry.php", "./module/entry.php",
                                      "/unknown/module/entry.php", "/workspace/../module/entry.php",
                                      "/workspace-other/module/entry.php", "module//entry.php", "install.php"])
def test_paths_do_not_guess_aliases_packaging_or_escape_source(source, path):
    repo, view, manifest, _payload = source
    baseline = verify_source(repo, view, manifest)
    with pytest.raises(ValueError):
        normalize_source_path(repo, baseline, path)


def test_declared_packaging_mapping_is_explicit(source):
    repo, view, manifest, payload = source
    payload["path_mappings"] = {"install.php": "install.php.tmp"}
    manifest.write_text(json.dumps(payload))
    baseline = verify_source(repo, view, manifest)
    evidence = check_observation(repo, baseline, "/workspace/install.php", 2, 2, "install();")
    assert evidence.path == "install.php.tmp"


@pytest.mark.parametrize("mapping", [{"install.php": "missing.php"}, {"module/entry.php": "install.php.tmp"},
                                         {"../install.php": "install.php.tmp"}])
def test_packaging_mapping_requires_unambiguous_existing_target(source, mapping):
    repo, view, manifest, payload = source
    payload["path_mappings"] = mapping
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        verify_source(repo, view, manifest)


@pytest.mark.parametrize("start,end,quote", [(1, 2, "requireLogin();"), (2, 2, "<?php"),
                                                 (3, 4, "echo $input;"), (0, 1, "<?php"),
                                                 (2, 1, ""), (1, 1, "<?php\n")])
def test_quote_is_the_exact_complete_declared_lines(source, start, end, quote):
    repo, view, manifest, _payload = source
    baseline = verify_source(repo, view, manifest)
    with pytest.raises(ValueError):
        check_observation(repo, baseline, "module/entry.php", start, end, quote)


def test_rechecks_detect_manifest_and_source_changes(source):
    repo, view, manifest, _payload = source
    baseline = verify_source(repo, view, manifest)
    original = manifest.read_bytes()
    manifest.write_bytes(original + b"\n")
    with pytest.raises(ValueError, match="manifest changed"):
        assert_source_unchanged(repo, baseline)
    manifest.write_bytes(original)
    (repo / "node_modules" / "guard.js").write_text("unguarded();\n")
    with pytest.raises(ValueError, match="source files changed"):
        assert_source_unchanged(repo, baseline)
    (repo / "module" / "entry.php").write_text("changed\n")
    with pytest.raises(ValueError, match="content changed"):
        check_observation(repo, baseline, "module/entry.php", 1, 1, "changed")
