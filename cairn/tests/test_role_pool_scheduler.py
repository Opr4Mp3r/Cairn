from __future__ import annotations

from collections import Counter
from concurrent.futures import Future

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult

from conftest import make_config, make_intent, make_project
from test_scheduler_logic import _loop, _summary


def pool_config(*, projects: int = 1, audit: bool = False) -> DispatchConfig:
    data = make_config().model_dump(mode="json")
    data["runtime"].update(execution="local", max_workers=1, max_project_workers=1, max_running_projects=projects)
    data["common_env"] = {"ANTHROPIC_MODEL": "must-not-override-host"}
    data["role_pools"] = {"explore": {"claudecode": 3, "grok": 2}, "reason": {"claudecode": 2, "grok": 1}}
    if audit:
        data["controller"] = {}
        data["role_pools"].update({role: {"grok": 1} for role in (
            "preflight", "seed_review", "overseer", "closing", "final_review",
        )})
    return DispatchConfig.model_validate(data)


def test_role_pools_derive_capacity_and_survive_saved_config_reload() -> None:
    config = pool_config(projects=2, audit=True)
    assert config.pool_capacity("explore") == 5
    assert config.runtime.max_project_workers == 6
    assert config.runtime.max_workers == 12
    assert all(worker.env == {} for worker in config.workers)
    assert config.controller.roles.builder == "pool-closing-grok"
    assert config.controller.roles.reviewer == "pool-preflight-grok"
    assert [worker.type for worker in config.pool_workers("explore")] == ["claudecode", "grok"]
    assert DispatchConfig.model_validate(config.model_dump()).model_dump() == config.model_dump()


def test_role_pools_need_no_legacy_worker_or_capacity_configuration() -> None:
    data = pool_config().model_dump()
    del data["workers"]
    del data["runtime"]["max_workers"]
    del data["runtime"]["max_project_workers"]
    config = DispatchConfig.model_validate(data)
    assert config.runtime.max_workers == 6
    assert config.pool_capacity("explore") == 5


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_role_pool_slots_are_positive_integers(count) -> None:
    data = pool_config().model_dump()
    data["role_pools"]["explore"]["claudecode"] = count
    with pytest.raises(ValidationError):
        DispatchConfig.model_validate(data)


def test_role_pools_report_missing_roles_and_unsupported_execution() -> None:
    data = pool_config(audit=True).model_dump()
    del data["role_pools"]["seed_review"]
    with pytest.raises(ValidationError, match="missing required roles: seed_review"):
        DispatchConfig.model_validate(data)
    data = pool_config().model_dump()
    data["runtime"]["execution"] = "container"
    with pytest.raises(ValidationError, match="requires runtime.execution=local"):
        DispatchConfig.model_validate(data)


def make_pool_loop(project_ids):
    loop = _loop()
    loop.config = pool_config(projects=len(project_ids))
    loop.futures = {}
    loop.runtime_project_ids = set(project_ids)
    projects = {}
    for project_id in project_ids:
        project = make_project(intents=[make_intent(f"i{index}") for index in range(8)])
        project.project.id = project_id
        for intent in project.intents:
            intent.worker = None
        projects[project_id] = project
    loop.container_manager = type("Backend", (), {"container_name": lambda self, project_id: project_id})()
    loop.executor = type("Executor", (), {"submit": lambda self, *args: Future()})()
    loop.client = type("Client", (), {
        "get_project": lambda self, project_id: projects[project_id],
        "export_project": lambda self, project_id: "graph",
        "claim_reason": lambda self, *args: ApiResult(200, {}),
        "heartbeat": lambda self, *args, **kwargs: ApiResult(200, {}),
    })()
    return loop


def test_dispatcher_fills_each_project_pool_and_refills_finished_slots() -> None:
    loop = make_pool_loop(["first", "second"])
    loop.runtime_project_ids.clear()
    summaries = [_summary(project_id, "active") for project_id in ("first", "second")]
    loop._dispatch_available(summaries)
    assert len(loop.futures) == 12
    for project_id in ("first", "second"):
        tasks = [task for task in loop.futures.values() if task.project_id == project_id]
        assert Counter(task.task_type for task in tasks) == {"explore": 5, "reason": 1}
        assert Counter(task.worker_name for task in tasks if task.task_type == "explore") == {
            "pool-explore-claudecode": 3, "pool-explore-grok": 2,
        }
        assert [task.worker_name for task in tasks if task.task_type == "explore"] == [
            "pool-explore-claudecode", "pool-explore-grok", "pool-explore-claudecode",
            "pool-explore-grok", "pool-explore-claudecode",
        ]
    future = next(future for future, task in loop.futures.items()
                  if task.project_id == "first" and task.worker_name == "pool-explore-grok")
    future.set_result("success")
    loop._reap_futures()
    loop._dispatch_available(summaries)
    assert len(loop.futures) == 12
    assert loop._worker_counts("first")["pool-explore-grok"] == 2


def test_unavailable_reason_does_not_block_explore_pool() -> None:
    loop = make_pool_loop(["proj_001"])
    for worker in loop.config.pool_workers("reason"):
        loop.worker_unhealthy_until[worker.name] = float("inf")
    loop._dispatch_available([_summary("proj_001", "active")])
    assert len(loop.futures) == 5
    assert {task.task_type for task in loop.futures.values()} == {"explore"}
