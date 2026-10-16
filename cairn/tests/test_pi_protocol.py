from __future__ import annotations

import json

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters.pi import PiDriver


def _worker(**env: str) -> WorkerConfig:
    return WorkerConfig(name="pi-protocol", type="pi", task_types=["explore"],
                        max_running=1, priority=0, env=env)


def _assistant(text: str = '{"accepted":true,"data":{}}', *, reason: str = "stop") -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": reason}


def _stream(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _end(message: dict) -> dict:
    return {"type": "agent_end", "messages": [message]}


def test_pi_local_commands_preserve_native_configuration_and_explicit_session() -> None:
    driver = PiDriver(local=True)
    worker = _worker()
    first = driver.build_execute(worker, "- leading option-like prompt", None)
    assert first.argv[0] == "pi"
    assert first.argv[-3:] == ["-p", "--", "- leading option-like prompt"]
    for absent in ("/bin/sh", "--provider", "--model", "--api-key", "--session", "--continue"):
        assert absent not in first.argv
    session_dir = first.argv[first.argv.index("--session-dir") + 1]
    assert "--tools" in first.argv

    for command in (driver.build_execute(worker, "again", "specific-id").argv,
                    driver.build_conclude(worker, "again", "specific-id")):
        assert command[command.index("--session") + 1] == "specific-id"
        assert command[command.index("--session-dir") + 1] == session_dir
        assert command[-3:] == ["-p", "--", "again"]
        assert "--continue" not in command
        assert "--resume" not in command


def test_pi_container_commands_keep_provider_injection_and_prompt_separator() -> None:
    driver = PiDriver()
    worker = _worker(PI_MODEL="fixture-model", PI_BASE_URL="http://localhost:1/v1",
                     PI_API_KEY="fixture-key", PI_PROVIDER_API="openai-completions")
    first = driver.build_execute(worker, "- prompt", None).argv
    resumed = driver.build_conclude(worker, "- prompt", "exact-id")
    for command in (first, resumed):
        assert command[:2] == ["/bin/sh", "-lc"]
        assert command[command.index("--provider") + 1] == "cairn"
        assert command[command.index("--model") + 1] == "fixture-model"
        assert command[-3:] == ["-p", "--", "- prompt"]
    assert resumed[resumed.index("--session") + 1] == "exact-id"


def test_pi_extracts_session_even_if_timeout_truncates_last_event() -> None:
    stdout = _stream({"type": "session", "id": "exact-id"}, {"type": "agent_start"}) + '\n{"type":'
    assert PiDriver().extract_session(None, stdout, "") == "exact-id"
    assert PiDriver().extract_session("already-selected", stdout, "") == "already-selected"
    with pytest.raises(ValueError, match="incomplete or invalid"):
        PiDriver().extract_response_text(stdout, "")


def test_pi_extracts_only_terminal_assistant_after_tool_round_trip() -> None:
    intermediate = _assistant('{"accepted":false,"data":{}}', reason="toolUse")
    intermediate["content"].append({"type": "toolCall", "id": "read-1", "name": "read", "arguments": {"path": "file"}})
    tool = {"role": "toolResult", "toolCallId": "read-1", "content": [{"type": "text", "text": '{"accepted":false}'}]}
    final = _assistant("  final answer  ")
    final["content"].insert(0, {"type": "thinking", "thinking": "private analysis"})
    stdout = _stream(
        {"type": "session", "id": "test-id"}, {"type": "agent_start"},
        {"type": "turn_start"}, {"type": "message_end", "message": intermediate},
        {"type": "tool_execution_end", "toolName": "read", "result": tool},
        {"type": "turn_end", "message": intermediate, "toolResults": [tool]},
        {"type": "turn_start"}, {"type": "message_end", "message": final},
        {"type": "turn_end", "message": final, "toolResults": []},
        {"type": "agent_end", "messages": [intermediate, tool, final]},
        {"type": "queue_update", "steering": [], "followUp": []},
    )
    assert PiDriver().extract_response_text(stdout, "") == "final answer"


@pytest.mark.parametrize("reason", ["error", "aborted", "length", "toolUse", "pending", "deferred", None])
def test_pi_model_failure_or_incomplete_response_is_not_json_success(reason: str | None) -> None:
    # JSON mode can return exit 0 even when its terminal assistant contains an error.
    final = _assistant(reason=reason)
    final["errorMessage"] = "synthetic provider failure"
    with pytest.raises(ValueError, match="did not complete"):
        PiDriver().extract_response_text(_stream(_end(final)), "")


def test_pi_last_retry_result_overrides_previous_attempt() -> None:
    failed = _assistant(reason="error")
    final = _assistant("recovered answer")
    events = [_end(failed), {"type": "auto_retry_start"}, {"type": "agent_start"}, _end(final)]
    assert PiDriver().extract_response_text(_stream(*events), "") == "recovered answer"
    with pytest.raises(ValueError, match="did not complete"):
        PiDriver().extract_response_text(_stream(_end(final), {"type": "agent_start"}, _end(failed)), "")


@pytest.mark.parametrize("tail", [
    {"type": "agent_start"}, {"type": "turn_start"}, {"type": "auto_retry_start"},
    {"type": "message_end", "message": _assistant("unfinished")},
    {"type": "turn_end", "message": _assistant("unfinished")},
    {"type": "tool_execution_start", "toolName": "read"},
])
def test_pi_unfinished_new_work_cannot_reuse_previous_completed_answer(tail: dict) -> None:
    with pytest.raises(ValueError, match="no completed"):
        PiDriver().extract_response_text(_stream(_end(_assistant("old answer")), tail), "")


@pytest.mark.parametrize("stdout", [
    "",
    _stream({"type": "turn_end", "message": _assistant()}),
    _stream({"type": "tool_execution_end", "result": {"accepted": True, "data": {}}}),
    _stream(_end({"role": "toolResult", "content": [{"type": "text", "text": '{"accepted":true}'}]})),
    _stream({"type": "agent_end", "messages": []}),
    _stream(_end(_assistant(""))),
    _stream(_end({"role": "assistant", "stopReason": "stop", "content": None})),
    _stream(_end(_assistant())) + '\n{"type":',
    _stream(_end(_assistant())) + "\n[]",
    '{"accepted":true,"data":{}}',
])
def test_pi_never_falls_back_to_unfinished_stream_or_tool_content(stdout: str) -> None:
    with pytest.raises(ValueError):
        PiDriver().extract_response_text(stdout, "")
