from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.workers.registry import get_driver

from conftest import make_config
from test_role_pool_scheduler import make_pool_loop
from test_scheduler_logic import _summary


def minimax_config() -> dict:
    data = make_config().model_dump()
    data["runtime"]["execution"] = "local"
    data["workers"][0]["type"] = "minimax"
    return data


def test_minimax_local_worker_reuses_host_configuration() -> None:
    config = DispatchConfig.model_validate(minimax_config())
    assert config.workers[0].env == {}
    assert get_driver("minimax", "local").local_binary() == "mcode"
    with pytest.raises(ValueError, match="not registered for container execution"):
        get_driver("minimax", "container")


def test_minimax_all_role_pools_survive_saved_config_reload() -> None:
    data = minimax_config()
    data["controller"] = {}
    data["role_pools"] = {role: {"minimax": 1} for role in (
        "explore", "reason", "preflight", "seed_review", "overseer", "closing", "final_review",
    )}
    config = DispatchConfig.model_validate(data)
    assert config.controller.roles.builder == "pool-closing-minimax"
    assert config.controller.roles.reviewer == "pool-preflight-minimax"
    assert all(worker.type == "minimax" and worker.env == {} for worker in config.workers)
    assert DispatchConfig.model_validate(config.model_dump()).model_dump() == config.model_dump()


@pytest.mark.parametrize("controller", [False, True])
def test_minimax_container_configuration_reports_unsupported_execution(controller) -> None:
    data = minimax_config()
    data["runtime"]["execution"] = "container"
    if controller:
        data["controller"] = {"roles": {"builder": "test-worker", "reviewer": "test-worker"}}
    with pytest.raises(ValidationError, match="minimax.*not registered for container execution"):
        DispatchConfig.model_validate(data)


def test_minimax_mixed_pool_capacity_is_dispatched_without_a_separate_limit() -> None:
    loop = make_pool_loop(["mixed"])
    data = loop.config.model_dump()
    data["role_pools"] = {"explore": {"claudecode": 1, "minimax": 2}, "reason": {"minimax": 1}}
    loop.config = DispatchConfig.model_validate(data)
    loop.runtime_project_ids.clear()
    loop._dispatch_available([_summary("mixed", "active")])
    assert loop.config.pool_capacity("explore") == 3
    assert Counter(task.worker_name for task in loop.futures.values()) == {
        "pool-explore-claudecode": 1, "pool-explore-minimax": 2, "pool-reason-minimax": 1,
    }
