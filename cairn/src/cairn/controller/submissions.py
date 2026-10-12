"""Prepared audit submissions and an HTTP-only client for an existing Cairn."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import shutil
import time
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from cairn.controller.audit import AuditPlan
from cairn.controller.builder import Finish
from cairn.controller.kritt_query import collect_queries, write_json
from cairn.dispatcher.config import (
    ControllerBudgetConfig, ControllerReviewConfig, ControllerTimeouts,
    DispatchConfig, RolePools, TasksConfig,
)
from cairn.dispatcher.protocol.client import CairnClient


class ExecutionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_pools: RolePools
    tasks: TasksConfig
    research_prompt_group: str = Field(min_length=1)
    timeouts: ControllerTimeouts
    review: ControllerReviewConfig
    budget: ControllerBudgetConfig


def execution_snapshot(config: DispatchConfig) -> ExecutionSnapshot:
    if not config.role_pools or config.controller is None or not config.controller.research_prompt_group:
        raise ValueError("submission requires role_pools and controller.research_prompt_group")
    return ExecutionSnapshot(role_pools=config.role_pools, tasks=config.tasks,
        research_prompt_group=config.controller.research_prompt_group, timeouts=config.controller.timeouts,
        review=config.controller.review, budget=config.controller.budget)


def project_config(service: DispatchConfig, snapshot: ExecutionSnapshot) -> DispatchConfig:
    data = service.model_dump(mode="json")
    data.update(role_pools=snapshot.role_pools, tasks=snapshot.tasks.model_dump(), common_env={})
    data["controller"].update(research_prompt_group=snapshot.research_prompt_group,
        timeouts=snapshot.timeouts.model_dump(), review=snapshot.review.model_dump(), budget=snapshot.budget.model_dump())
    return DispatchConfig.model_validate(data)


def read_prepared(folder: Path) -> tuple[dict, dict, dict[str, bytes]]:
    folder = folder.expanduser().resolve(strict=True)
    prepared = json.loads((folder / "run.json").read_text())
    if Path(prepared["run_dir"]).resolve() != folder:
        raise ValueError("prepared run directory does not match")
    if folder.is_relative_to(Path(prepared["source_root"]).resolve()):
        raise ValueError("prepared run must be outside the audited source")
    artifacts = {}
    for key in ("plan", "materials", "requirements", "references", "source_baseline", "query_context"):
        path = Path(prepared[key]).resolve(strict=True)
        if not path.is_relative_to(folder):
            raise ValueError(f"prepared {key} must be inside this run")
        artifacts[key] = path.read_bytes()
    plan = AuditPlan.model_validate_json(artifacts["plan"])
    Finish.model_validate({**plan.model_dump(mode="json", by_alias=True), "action": "finish"})
    for entry in json.loads(artifacts["references"]):
        path = Path(entry["path"]).resolve(strict=True)
        if not path.is_relative_to(folder):
            raise ValueError("saved references must be inside this run")
        artifacts["reference:" + str(path)] = path.read_bytes()
    receipts = folder / "query-receipts.json"
    artifacts["query_receipts"] = receipts.read_bytes()
    view = Path(json.loads(artifacts["query_context"])["view_path"]).resolve(strict=True)
    if not view.is_relative_to(folder):
        raise ValueError("fixed Kritt view must be inside this run")
    artifacts["view"] = view.read_bytes()
    return prepared, plan.model_dump(mode="json", by_alias=True), artifacts


def snapshot_fingerprint(plan: dict, execution: ExecutionSnapshot, artifacts: dict[str, bytes]) -> str:
    content = {"plan": plan, "execution": execution.model_dump(mode="json"),
               "artifacts": {key: hashlib.sha256(value).hexdigest() for key, value in artifacts.items()}}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save_snapshot(folder: Path, prepared: dict, artifacts: dict[str, bytes], execution: ExecutionSnapshot) -> dict:
    folder.mkdir(parents=True)
    saved = dict(prepared)
    for key, filename in {"plan": "plan.json", "materials": "selected-materials.json",
        "requirements": "requirements.json", "source_baseline": "source-baseline.json",
        "query_context": "kritt-context.json", "query_receipts": "query-receipts.json"}.items():
        path = folder / filename
        path.write_bytes(artifacts[key])
        saved[key] = str(path)
    # The reviewed working plan may change; keep the submitted input separately.
    shutil.copyfile(saved["plan"], folder / "submitted-plan.json")
    refs = []
    for index, entry in enumerate(json.loads(artifacts["references"]), 1):
        source = str(Path(entry["path"]).resolve())
        target = folder / "references" / f"{index:03d}-{Path(source).name}"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(artifacts["reference:" + source])
        refs.append({**entry, "path": str(target)})
    saved["references"] = str(folder / "references.json")
    write_json(Path(saved["references"]), refs)
    (folder / "kritt-view.json").write_bytes(artifacts["view"])
    context = json.loads(artifacts["query_context"])
    context.update(view_path=str(folder / "kritt-view.json"), queries_dir=str(folder / "queries"))
    write_json(Path(saved["query_context"]), context)
    write_json(folder / "execution.json", execution.model_dump(mode="json"))
    return saved


def validate_server_url(server: str) -> None:
    address = urlparse(server)
    if (address.scheme != "http" or address.hostname not in ("127.0.0.1", "localhost")
            or address.path not in ("", "/") or not address.port
            or address.username is not None or address.query or address.fragment):
        raise ValueError("use the fixed loopback address of an already running Cairn service")


def submit_prepared(server: str, run_dir: Path, *, config_path: Path | None = None,
                    wait: bool = True, report=lambda event: None) -> dict:
    validate_server_url(server)
    folder = run_dir.expanduser().resolve(strict=True)
    with closing(CairnClient(server, trust_env=False)) as client:
        health = client.service_health()
        if not health.get("managed") or not health.get("ready"):
            raise ValueError("Cairn execution service is not ready; start it manually with cairn up")
        if config_path is not None:
            snapshot = execution_snapshot(DispatchConfig.load(config_path))
        else:
            snapshot = ExecutionSnapshot.model_validate_json((folder / "execution.json").read_text())
        prepared = json.loads((folder / "run.json").read_text())
        collect_queries(Path(prepared["query_context"]))
        read_prepared(folder)
        body = {"run_dir": str(folder), "execution": snapshot.model_dump(mode="json")}
        response = client.submit_audit(body)
        if response.status_code == 409:
            current = response.data.get("detail", {}) if response.data else {}
            if isinstance(current, dict) and current.get("submission_id") and not current.get("project_id"):
                response = client.revise_submission(current["submission_id"], {**body, "revision": current["revision"]})
        if not response.ok:
            raise ValueError(f"submission failed ({response.status_code}): {response.text}")
        result = response.data
        if result["database_id"] != health["database_id"] or result["service_id"] != health["service_id"]:
            raise ValueError("service changed during submission; query the submitted run before retrying")
        write_json(folder / "submission.json", result)
        report(result)
        previous = result["status"]
        while wait and result["status"] in ("pending", "preparing"):
            time.sleep(.2)
            result = client.get_submission(result["submission_id"])
            if result["database_id"] != health["database_id"]:
                raise ValueError("submission database changed")
            if result["status"] != previous:
                report(result)
                previous = result["status"]
            write_json(folder / "submission.json", result)
        return result
