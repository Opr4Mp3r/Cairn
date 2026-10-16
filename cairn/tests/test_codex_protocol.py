from __future__ import annotations

import json

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters.codex import CodexDriver


def _worker(*, container: bool = False) -> WorkerConfig:
    return WorkerConfig.model_validate({
        "name": "codex",
        "type": "codex",
        "task_types": ["reason", "explore"],
        "max_running": 1,
        "priority": 0,
        "env": {
            "CODEX_MODEL": "test-model",
            "CODEX_BASE_URL": "http://example.invalid/v1",
            "OPENAI_API_KEY": "test-secret",
        } if container else {},
    })


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _answer(text: str, **extra: object) -> dict:
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text, **extra}}


@pytest.mark.parametrize("session", [None, "thread-123"])
@pytest.mark.parametrize("local", [True, False])
def test_codex_machine_mode_new_and_resume(session: str | None, local: bool) -> None:
    worker = _worker(container=not local)
    driver = CodexDriver(local=local)
    result = driver.build_execute(worker, "--prompt stays literal", session)
    expected_prefix = ["codex", "exec"] + (["resume", session] if session else [])
    assert result.argv[:len(expected_prefix)] == expected_prefix
    assert result.argv[-2:] == ["--", "--prompt stays literal"]
    assert result.session == session
    assert "--json" in result.argv
    assert "--skip-git-repo-check" in result.argv
    assert "--dangerously-bypass-approvals-and-sandbox" in result.argv
    assert "test-secret" not in result.argv
    if local:
        assert "--model" not in result.argv
        assert "-c" not in result.argv
    else:
        assert result.argv[result.argv.index("--model") + 1] == "test-model"
        assert 'model_providers.cairn.base_url="http://example.invalid/v1"' in result.argv
        assert 'model_providers.cairn.env_key="OPENAI_API_KEY"' in result.argv
    if session:
        assert driver.build_conclude(worker, "--prompt stays literal", session) == result.argv


def test_codex_extracts_json_thread_and_keeps_known_session() -> None:
    driver = CodexDriver(local=True)
    stdout = _stream({"type": "thread.started", "thread_id": "thread-123"})
    assert driver.extract_session(None, stdout, "") == "thread-123"
    assert driver.extract_session(None, "diagnostic\n[]\nnull\n" + stdout + '\n{"type":', "") == "thread-123"
    assert driver.extract_session("known", stdout, "") == "known"
    assert driver.extract_session(None, "", "session id: abc-123\n") == "abc-123"
    assert driver.extract_session(None, _stream({"type": "thread.started", "thread_id": 123}), "") is None


@pytest.mark.parametrize("phase", [None, "final_answer"])
def test_codex_extracts_only_successful_final_agent_message(phase: str | None) -> None:
    extra = {"phase": phase} if phase is not None else {}
    stdout = _stream(
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "turn.started"},
        _answer('{"early": true}'),
        {"type": "item.completed", "item": {"type": "reasoning", "text": '{"reasoning": true}'}},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": '{"tool": true}'}},
        _answer('{"progress": true}', phase="commentary"),
        _answer('{"facts": []}', **extra),
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    )
    assert CodexDriver().extract_response_text(stdout, "") == '{"facts": []}'


@pytest.mark.parametrize("events", [
    [],
    [_answer('{"facts": []}')],
    [{"type": "turn.completed"}],
    [_answer(""), {"type": "turn.completed"}],
    [_answer("  "), {"type": "turn.completed"}],
    [_answer('{"progress": true}', phase="commentary"), {"type": "turn.completed"}],
    [_answer('{"facts": []}'), {"type": "turn.failed", "error": {"message": "failed"}}],
    [_answer('{"facts": []}'), {"type": "error", "message": "failed"}],
    [_answer('{"facts": []}'), {"type": "turn.completed"}, {"type": "error", "message": "failed"}],
    [_answer('{"facts": []}'), {"type": "turn.completed"}, {"type": "turn.started"}],
    [_answer('{"facts": []}'), {"type": "turn.completed"}, {"type": "turn.started"}, {"type": "turn.completed"}],
    [_answer('{"stale": true}'), {"type": "error"}, {"type": "turn.completed"}],
    [{"type": "turn.failed"}, _answer('{"facts": []}'), {"type": "turn.completed"}],
    [{"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": '{"facts": []}'}}, {"type": "turn.completed"}],
])
def test_codex_rejects_missing_failed_or_incomplete_result(events: list[dict]) -> None:
    with pytest.raises(ValueError):
        CodexDriver().extract_response_text(_stream(*events), "")


def test_codex_reconnect_error_can_be_followed_by_success() -> None:
    stdout = _stream(
        {"type": "turn.started"},
        _answer('{"stale": true}'),
        {"type": "error", "message": "stream disconnected, retrying"},
        _answer('{"fresh": true}', phase="final_answer"),
        {"type": "turn.completed"},
    )
    assert CodexDriver().extract_response_text(stdout, "") == '{"fresh": true}'


def test_codex_latest_completed_turn() -> None:
    stdout = _stream(
        {"type": []},
        {"type": "turn.failed"},
        {"type": "turn.started"},
        _answer('{"first": true}'),
        {"type": "turn.completed"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "phase": []}},
        _answer('{"last": true}'),
        {"type": "turn.completed"},
    )
    assert CodexDriver().extract_response_text(stdout, "") == '{"last": true}'


@pytest.mark.parametrize("tail", [
    '{"type":"turn.',
    _stream({"type": "item.started", "item": {"type": "agent_message", "text": "new response in progress"}}),
    _stream(_answer('{"fresh": true}', phase="final_answer")),
    _stream({"type": "thread.started", "thread_id": "new-thread"}),
])
def test_codex_rejects_new_or_truncated_work_after_success(tail: str) -> None:
    stdout = _stream(_answer('{"stale": true}'), {"type": "turn.completed"}) + "\n" + tail
    with pytest.raises(ValueError):
        CodexDriver().extract_response_text(stdout, "")


@pytest.mark.parametrize("non_event", ["diagnostic", "[]", "null"])
def test_codex_rejects_non_event_lines_even_with_success(non_event: str) -> None:
    stdout = non_event + "\n" + _stream(_answer('{"facts": []}'), {"type": "turn.completed"})
    with pytest.raises(ValueError):
        CodexDriver().extract_response_text(stdout, "")


@pytest.mark.parametrize("after_success", [False, True])
def test_codex_tool_work_requires_a_new_final_answer(after_success: bool) -> None:
    events = [_answer('{"stale": true}', phase="final_answer")]
    if after_success:
        events.append({"type": "turn.completed"})
    events.extend([
        {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": '{"tool": true}'}},
    ])
    with pytest.raises(ValueError):
        CodexDriver().extract_response_text(_stream(*events, {"type": "turn.completed"}), "")
    assert CodexDriver().extract_response_text(
        _stream(*events, _answer('{"fresh": true}'), {"type": "turn.completed"}), ""
    ) == '{"fresh": true}'


def test_codex_raw_json_is_not_a_successful_envelope() -> None:
    with pytest.raises(ValueError):
        CodexDriver().extract_response_text('{"facts": []}', "")
