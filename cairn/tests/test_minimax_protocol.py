from __future__ import annotations

import json

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters.minimax import MiniMaxDriver


def _worker() -> WorkerConfig:
    return WorkerConfig.model_validate({
        "name": "minimax",
        "type": "minimax",
        "task_types": ["reason", "explore"],
        "max_running": 1,
        "priority": 0,
        "env": {"MINIMAX_DATA_DIR": "/host/profile", "UNRELATED_SECRET": "keep-out-of-argv"},
    })


def _event(kind: str, **extra: object) -> dict:
    return {
        "schemaVersion": 1,
        "runId": "exec_turn-1",
        "sessionId": "session-1",
        "turnId": "turn-1",
        "type": kind,
        **extra,
    }


def _terminal(**extra: object) -> dict:
    return _event("exec.completed", result=_event(
        "exec.result", status="succeeded", output='{"facts": []}', durationMs=10, **extra,
    ))


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize("session", [None, "session-1"])
def test_minimax_uses_machine_mode_exact_session_and_host_configuration(session: str | None) -> None:
    driver = MiniMaxDriver()
    worker = _worker()
    prompt = "--model stays literal\nread the file; $(not a shell)"
    result = driver.build_execute(worker, prompt, session)
    assert result.argv == [
        "mcode", "exec", "--output-format", "stream-json", "--permission", "full",
        *(["--session", session] if session else []), "--", prompt,
    ]
    assert result.session == session
    assert worker.env == {"MINIMAX_DATA_DIR": "/host/profile", "UNRELATED_SECRET": "keep-out-of-argv"}
    assert driver.local_binary() == "mcode"
    assert driver.prepare_session() is None
    assert driver.supports_conclude()
    if session:
        assert driver.build_conclude(worker, prompt, session) == result.argv
    assert not driver.check_health(worker, timeout=1).ok
    assert "host CLI configuration" in driver.describe_health(worker)


@pytest.mark.parametrize("kind", ["exec.started", "session.started", "session.resumed"])
def test_minimax_recovers_session_from_complete_prefix_after_interruption(kind: str) -> None:
    stdout = _stream(_event(kind)) + '\n{"type":"item.upd'
    driver = MiniMaxDriver()
    assert driver.extract_session(None, stdout, "") == "session-1"
    assert driver.extract_session("known", stdout, "") == "known"


@pytest.mark.parametrize("event", [
    {"type": "exec.started", "sessionId": "session-1"},
    _event("exec.started", sessionId=1),
    _event("session.started", sessionId=" "),
    _event("session.started", schemaVersion=2),
    _event("item.completed", item={"type": "session.started", "sessionId": "tool-session"}),
])
def test_minimax_does_not_invent_session_from_tool_or_malformed_output(event: dict) -> None:
    assert MiniMaxDriver().extract_session(None, _stream(event), "session id: fake-session") is None


def test_minimax_uses_only_terminal_result_not_tools_or_intermediate_messages() -> None:
    stdout = _stream(
        _event("exec.started"),
        _event("session.started"),
        _event("turn.started"),
        _event("item.completed", item={"type": "agent_message", "content": '{"stale": true}'}),
        _event("item.completed", item={"type": "tool_call", "content": json.dumps(_terminal())}),
        _event("turn.completed", usage={"inputTokens": 10, "outputTokens": 5}),
        _terminal(),
    )
    assert MiniMaxDriver().extract_response_text(stdout, "irrelevant diagnostic") == '{"facts": []}'


@pytest.mark.parametrize("status", ["failed", "timeout", "cancelled", "limit_exceeded", None, "unknown"])
def test_minimax_rejects_unsuccessful_status_even_if_answer_is_present(status: str | None) -> None:
    terminal = _terminal()
    terminal["result"]["status"] = status
    with pytest.raises(ValueError, match="did not succeed"):
        MiniMaxDriver().extract_response_text(_stream(_event("exec.started"), terminal), "")


@pytest.mark.parametrize("output", [None, "", " \n", {}, ["text"], 1])
def test_minimax_requires_nonempty_final_text(output: object) -> None:
    terminal = _terminal()
    terminal["result"]["output"] = output
    with pytest.raises(ValueError, match="final answer text"):
        MiniMaxDriver().extract_response_text(_stream(terminal), "")


@pytest.mark.parametrize("stdout", [
    "",
    '{"facts": []}',
    _stream(_event("exec.started")),
    _stream(_event("turn.completed")),
    _stream(_event("item.completed", item=_terminal())),
    _stream(_event("exec.started"), _event("exec.completed", result=None)),
    _stream(_event("exec.started"), _event("turn.failed", status="failed"), _terminal()),
])
def test_minimax_rejects_missing_or_invalid_terminal_result(stdout: str) -> None:
    with pytest.raises(ValueError):
        MiniMaxDriver().extract_response_text(stdout, "")


@pytest.mark.parametrize("suffix", [
    '{"type":"exec.comp',
    "not-json",
    "[]",
    "null",
    _stream(_terminal()),
    _stream(_event("turn.started")),
    _stream(_event("item.completed", item={"type": "tool_call"})),
])
def test_minimax_rejects_truncated_or_additional_work_after_success(suffix: str) -> None:
    with pytest.raises(ValueError):
        MiniMaxDriver().extract_response_text(_stream(_terminal()) + "\n" + suffix, "")


@pytest.mark.parametrize("field,value", [
    ("runId", "another-run"),
    ("sessionId", "another-session"),
    ("turnId", "another-turn"),
    ("schemaVersion", 2),
    ("schemaVersion", True),
])
@pytest.mark.parametrize("inside_result", [False, True])
def test_minimax_rejects_wrong_execution_identity(field: str, value: object, inside_result: bool) -> None:
    terminal = _terminal()
    (terminal["result"] if inside_result else terminal)[field] = value
    with pytest.raises(ValueError, match="identity|terminal result"):
        MiniMaxDriver().extract_response_text(_stream(_event("exec.started"), terminal), "")


def test_minimax_rejects_unversioned_result_and_diagnostic_stdout() -> None:
    terminal = _terminal()
    terminal["result"]["type"] = "result"
    with pytest.raises(ValueError, match="terminal result"):
        MiniMaxDriver().extract_response_text(_stream(terminal), "")
    with pytest.raises(ValueError, match="complete JSON"):
        MiniMaxDriver().extract_response_text("diagnostic\n" + _stream(_terminal()), "")


@pytest.mark.parametrize("prefix", [
    '<think>{"draft": true}</think>\n',
    ' \n<mavis-thinking>{"draft": true}</mavis-thinking>\n',
    '<think>{"draft": true}</think><mavis-thinking>more reasoning</mavis-thinking>\n',
])
def test_minimax_strips_only_complete_leading_reasoning_blocks(prefix: str) -> None:
    terminal = _terminal()
    terminal["result"]["output"] = prefix + '{"facts": []}'
    assert MiniMaxDriver().extract_response_text(_stream(terminal), "") == '{"facts": []}'


def test_minimax_preserves_reasoning_tags_inside_final_json() -> None:
    output = ' {"evidence": "<think>code content</think>", "note": "<mavis-thinking>"}'
    terminal = _terminal()
    terminal["result"]["output"] = output
    assert MiniMaxDriver().extract_response_text(_stream(terminal), "") == output


@pytest.mark.parametrize("output", [
    '<think>{"draft": true}',
    '<mavis-thinking>{"draft": true}',
    '<think',
    '<think>done</think><mavis-thinking>unfinished',
])
def test_minimax_rejects_unclosed_reasoning_prefix(output: str) -> None:
    terminal = _terminal()
    terminal["result"]["output"] = output
    with pytest.raises(ValueError, match="unclosed reasoning prefix"):
        MiniMaxDriver().extract_response_text(_stream(terminal), "")


def test_minimax_rejects_reasoning_without_final_answer() -> None:
    terminal = _terminal()
    terminal["result"]["output"] = '<think>{"draft": true}</think> \n'
    with pytest.raises(ValueError, match="final answer text"):
        MiniMaxDriver().extract_response_text(_stream(terminal), "")
