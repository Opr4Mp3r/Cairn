import json

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver


def test_claude_execute_and_resume_request_machine_results():
    worker = WorkerConfig(name="cc", type="claudecode", task_types=["reason"], max_running=1, priority=0, env={})
    driver = ClaudeCodeDriver()
    execute = driver.build_execute(worker, "request", "session")
    resume = driver.build_conclude(worker, "request", "session")
    for argv in (execute.argv, resume):
        assert argv[argv.index("--output-format") + 1] == "json"
        assert "--append-system-prompt" in argv
        assert argv[-3:] == ["-p", "--", "request"]
    assert execute.session == "session" and resume[:3] == ["claude", "-r", "session"]


def test_claude_extracts_only_final_answer_and_preserves_raw_cli_receipt():
    answer = '{"accepted":true,"data":{"planning_notes":"wait for evidence"}}'
    raw = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": answer, "session_id": "session", "usage": {"input_tokens": 20}})
    assert ClaudeCodeDriver().extract_response_text(raw, "") == answer
    assert json.loads(raw)["usage"]["input_tokens"] == 20
    assert ClaudeCodeDriver().extract_response_text(answer, "") == answer
    prose = 'working...\n' + answer
    assert ClaudeCodeDriver().extract_response_text(prose, "") == prose


@pytest.mark.parametrize("update", [
    {"is_error": True}, {"subtype": "error_max_turns"}, {"result": {"accepted": True}},
])
def test_claude_failed_or_malformed_result_cannot_be_a_successful_answer(update):
    payload = {"type": "result", "subtype": "success", "is_error": False, "result": '{"accepted":true}'}
    payload.update(update)
    with pytest.raises(ValueError):
        ClaudeCodeDriver().extract_response_text(json.dumps(payload), "")
