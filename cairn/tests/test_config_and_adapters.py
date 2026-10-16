from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.grok import GrokDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver

from conftest import make_config


def test_dispatch_config_merges_common_env_with_worker_override() -> None:
    payload = make_config().model_dump()
    payload["common_env"] = {"SHARED": "common", "OVERRIDE": "common"}
    payload["workers"][0]["env"] = {"OVERRIDE": "worker"}

    config = DispatchConfig.model_validate(payload)

    assert config.workers[0].env["SHARED"] == "common"
    assert config.workers[0].env["OVERRIDE"] == "worker"


def test_dispatch_config_defaults_worker_healthcheck_and_rejects_unknown_mode() -> None:
    payload = make_config().model_dump()
    payload["runtime"].pop("worker_healthcheck")

    assert DispatchConfig.model_validate(payload).runtime.worker_healthcheck == "startup_only"

    payload["runtime"]["worker_healthcheck"] = "sometimes"
    with pytest.raises(ValidationError):
        DispatchConfig.model_validate(payload)


def test_dispatch_config_rejects_duplicate_workers_and_excess_project_parallelism() -> None:
    payload = make_config().model_dump()
    payload["workers"].append(dict(payload["workers"][0]))
    with pytest.raises(ValidationError, match="worker names must be unique"):
        DispatchConfig.model_validate(payload)

    payload = make_config().model_dump()
    payload["runtime"]["max_project_workers"] = 3
    with pytest.raises(ValidationError, match="max_project_workers cannot exceed max_workers"):
        DispatchConfig.model_validate(payload)


def test_pi_worker_rejects_invalid_context_window() -> None:
    with pytest.raises(ValidationError, match="PI_MODEL_CONTEXT_WINDOW must be greater than 0"):
        WorkerConfig.model_validate(
            {
                "name": "pi",
                "type": "pi",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {
                    "PI_MODEL": "model",
                    "PI_BASE_URL": "http://api",
                    "PI_API_KEY": "secret",
                    "PI_PROVIDER_API": "openai-completions",
                    "PI_MODEL_CONTEXT_WINDOW": "0",
                },
            }
        )


def test_mock_worker_rejects_unknown_phase_configuration() -> None:
    with pytest.raises(ValidationError, match="unsupported mock env keys"):
        WorkerConfig.model_validate(
            {
                "name": "mock",
                "type": "mock",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"MOCK_UNKNOWN": "{}"},
            }
        )


def test_bundled_prompt_groups_have_required_placeholders() -> None:
    validate_prompt_resources("default")
    validate_prompt_resources("mock")


def test_pi_driver_models_json_and_execute_argv_include_context_window_and_tools() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "pi-worker",
            "type": "pi",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "model",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "secret",
                "PI_PROVIDER_API": "openai-completions",
                "PI_MODEL_CONTEXT_WINDOW": "131072",
            },
        }
    )

    result = PiDriver().build_execute(worker, "prompt", None)
    models = json.loads(result.argv[5])

    assert models["providers"]["cairn"]["models"][0]["contextWindow"] == 131072
    assert "--tools" in result.argv
    assert result.argv[-3:] == ["-p", "--", "prompt"]


def _grok_worker(**extra: str) -> WorkerConfig:
    env = {
        "GROK_MODEL": 'grok-4"pro',
        "GROK_BASE_URL": "https://api.x.ai/v1/",
        "XAI_API_KEY": "xai-secret",
    }
    env.update(extra)
    return WorkerConfig.model_validate(
        {
            "name": "grok-worker",
            "type": "grok",
            "task_types": ["explore"],
            "max_running": 1,
            "priority": 0,
            "env": env,
        }
    )


def test_grok_worker_rejects_unknown_backend_and_bad_context_window() -> None:
    with pytest.raises(ValidationError, match="GROK_API_BACKEND"):
        _grok_worker(GROK_API_BACKEND="websocket")
    with pytest.raises(ValidationError, match="GROK_MODEL_CONTEXT_WINDOW must be greater than 0"):
        _grok_worker(GROK_MODEL_CONTEXT_WINDOW="0")


def test_grok_container_driver_injects_private_model_config() -> None:
    worker = _grok_worker(GROK_API_BACKEND="responses", GROK_MODEL_CONTEXT_WINDOW="131072")

    result = GrokDriver().build_execute(worker, "prompt", "11111111-1111-1111-1111-111111111111")

    assert result.argv[0] == "/bin/sh"
    assert result.argv[4] == "/tmp/cairn-grok/grok-worker"
    config = result.argv[5]
    assert 'model = "grok-4\\"pro"' in config
    assert 'base_url = "https://api.x.ai/v1"' in config
    assert 'api_backend = "responses"' in config
    assert "context_window = 131072" in config
    assert "xai-secret" not in config
    assert result.argv[result.argv.index("--model") + 1] == "cairn"
    assert result.argv[result.argv.index("--session-id") + 1] == "11111111-1111-1111-1111-111111111111"
    assert result.argv[-2:] == ["--single", "prompt"]
    assert "GROK_MEMORY=0" in result.argv[2]

    conclude = GrokDriver().build_conclude(worker, "done", "11111111-1111-1111-1111-111111111111")
    assert conclude[conclude.index("--resume") + 1] == "11111111-1111-1111-1111-111111111111"
    assert "--session-id" not in conclude


def test_codex_driver_execute_argv_passes_model_endpoint_and_prompt() -> None:
    worker = WorkerConfig.model_validate(
        {
            "name": "codex",
            "type": "codex",
            "task_types": ["reason"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "CODEX_MODEL": "gpt-test",
                "CODEX_BASE_URL": "http://api/v1",
                "OPENAI_API_KEY": "secret",
            },
        }
    )

    argv = CodexDriver().build_execute(worker, "prompt", None).argv

    assert "--model" in argv
    assert "gpt-test" in argv
    assert 'model_providers.cairn.base_url="http://api/v1"' in argv
    assert argv[-2:] == ["--", "prompt"]
