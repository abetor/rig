"""Tests for normalized agent results, parser behavior, status signals, and timeouts."""











from __future__ import annotations

import json

from rig.core.result import (
    InvokeResult,
    Result,
    apply_timeout,
    parse_claude_json,
    parse_claude_stream,
    parse_codex_jsonl,
    parse_status,
)


_OK_JSON = json.dumps({
    "result": "done: added function X", "session_id": "sess-123",
    "total_cost_usd": 0.0042, "is_error": False,
})

_CODEX_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "019ef5fe-abc"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning"}}),
    json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message",
                                                   "text": "done: codex did X"}}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 0,
                                                    "output_tokens": 10, "reasoning_output_tokens": 2}}),
])



def test_parse_claude_single_json_ok():

    res = parse_claude_json(_OK_JSON, 0)
    assert isinstance(res, Result)
    assert res.ok and res.text == "done: added function X"
    assert res.cost_usd == 0.0042 and res.session_id == "sess-123" and res.error is None
    assert res.exit_code == 0 and res.timed_out is False


def test_parse_claude_is_error_at_exit0_not_ok():

    res = parse_claude_json(json.dumps({"is_error": True, "result": "x", "subtype": "refusal"}), 0)
    assert not res.ok and res.error == "refusal" and res.exit_code == 0


def test_parse_claude_result_null_is_empty_not_literal_none():

    res = parse_claude_json(
        json.dumps({"is_error": True, "subtype": "error_max_budget_usd", "result": None}), 1)
    assert res.text == "" and not res.ok
    assert res.error == "error_max_budget_usd"


def test_parse_claude_api_error_status_in_error():

    res = parse_claude_json(json.dumps({"is_error": True, "api_error_status": 404, "result": "bad model"}), 1)
    assert not res.ok and res.error == "404"


def test_parse_claude_error_fallback_label():

    res = parse_claude_json(json.dumps({"is_error": True, "result": "x"}), 0)
    assert not res.ok and res.error == "claude error"


def test_parse_claude_non_json_graceful():

    res = parse_claude_json("not json garbage", 0)
    assert isinstance(res, Result) and not res.ok and "non-json" in (res.error or "")
    assert res.text == "not json garbage"


def test_parse_claude_non_dict_json_graceful():

    res = parse_claude_json("[1, 2, 3]", 0)
    assert not res.ok and "unexpected json shape" in (res.error or "")


def test_parse_claude_nonzero_exit_not_ok():

    res = parse_claude_json(_OK_JSON, 1)
    assert not res.ok and res.exit_code == 1 and res.error == "claude error"


def test_parse_claude_raw_kept():

    res = parse_claude_json(_OK_JSON, 0)
    assert res.raw["session_id"] == "sess-123" and res.raw["is_error"] is False



def test_parse_codex_jsonl_ok():

    res = parse_codex_jsonl(_CODEX_JSONL, 0)
    assert res.ok and res.text == "done: codex did X"
    assert res.cost_usd is None
    assert res.raw["usage"]["input_tokens"] == 50
    assert res.session_id == "019ef5fe-abc"


def test_parse_codex_agent_message_content_list_spliced():

    jsonl = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t-1"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                    "content": [{"type": "text", "text": "part1 "},
                                {"type": "text", "text": "part2"}]}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ])
    res = parse_codex_jsonl(jsonl, 0)
    assert res.ok and res.text == "part1 part2" and "[{" not in res.text


def test_parse_codex_multiple_agent_messages_joined():

    jsonl = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t-2"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "line1"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "line2"}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    res = parse_codex_jsonl(jsonl, 0)
    assert res.text == "line1\nline2"


def test_parse_codex_error_event_not_ok():

    bad = _CODEX_JSONL + "\n" + json.dumps({"type": "error", "message": "mcp failed"})
    res = parse_codex_jsonl(bad, 0)
    assert not res.ok and res.error == "mcp failed"


def test_parse_codex_is_error_flag_event_not_ok():

    bad = _CODEX_JSONL + "\n" + json.dumps({"is_error": True, "error": "boom"})
    res = parse_codex_jsonl(bad, 0)
    assert not res.ok and res.error == "boom"


def test_parse_codex_non_jsonl_graceful():

    res = parse_codex_jsonl("totally not jsonl", 0)
    assert not res.ok and "non-jsonl" in (res.error or "")


def test_parse_codex_skips_blank_and_garbage_lines():

    jsonl = "\n".join([
        "", "   ", "not json {",
        json.dumps({"type": "thread.started", "thread_id": "t-3"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7}}),
    ])
    res = parse_codex_jsonl(jsonl, 0)
    assert res.ok and res.text == "ok" and res.session_id == "t-3"


def test_parse_codex_nonzero_exit_not_ok():

    res = parse_codex_jsonl(_CODEX_JSONL, 1)
    assert not res.ok and res.exit_code == 1



def test_terminal_state_is_exit_and_parse_not_a_done_event():

    res = parse_claude_json(json.dumps({"result": "plain answer", "is_error": False}), 0)
    assert res.ok and res.status is None


def test_self_reported_done_does_not_make_failed_result_ok():

    payload = json.dumps({"result": 'sum {"status":"done"}', "is_error": True, "subtype": "refusal"})
    res = parse_claude_json(payload, 0)
    assert not res.ok
    assert res.status == {"status": "done"}
    assert res.error == "refusal"



def test_parse_status_last_wins():

    text = 'decoy {"status":"approved"} ... present {"status":"rejected","note":"x"}'
    assert parse_status(text) == {"status": "rejected", "note": "x"}


def test_parse_status_none_when_absent():

    assert parse_status("simply text without object") is None
    assert parse_status("") is None
    assert parse_status(None) is None


def test_parse_status_ignores_objects_without_key():

    assert parse_status('{"foo":1} then {"status":"go"}') == {"status": "go"}


def test_result_status_populated_from_codex_text():

    jsonl = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                       "text": 'go {"status":"blocked"}'}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    assert parse_codex_jsonl(jsonl, 0).status == {"status": "blocked"}


def test_status_is_input_not_acceptance():

    with_status = parse_claude_json(json.dumps({"result": '{"status":"done"}', "is_error": False}), 0)
    no_status = parse_claude_json(json.dumps({"result": "no status here", "is_error": False}), 0)
    assert with_status.ok and no_status.ok and no_status.status is None



def test_apply_timeout_forces_not_ok():

    res = parse_claude_json(_OK_JSON, 0)
    assert res.ok
    res = apply_timeout(res, timed_out=True)
    assert not res.ok and res.timed_out and "timeout" in (res.error or "").lower()


def test_apply_timeout_noop_when_not_timed_out():

    res = parse_claude_json(_OK_JSON, 0)
    res = apply_timeout(res, timed_out=False)
    assert res.ok and not res.timed_out and res.error is None


def test_apply_timeout_silent_hang_from_empty_output():

    res = parse_codex_jsonl("", 124)
    res = apply_timeout(res, timed_out=True, exit_code=124)
    assert not res.ok and res.timed_out and res.exit_code == 124


def test_apply_timeout_keeps_existing_error():

    res = parse_codex_jsonl(_CODEX_JSONL + "\n" + json.dumps({"type": "error", "message": "mcp failed"}), 0)
    res = apply_timeout(res, timed_out=True)
    assert res.error == "mcp failed" and res.timed_out and not res.ok


def test_apply_timeout_overrides_self_reported_done():

    res = parse_claude_json(json.dumps({"result": '{"status":"done"}', "is_error": False}), 0)
    assert res.ok and res.status == {"status": "done"}
    res = apply_timeout(res, timed_out=True)
    assert not res.ok and res.timed_out



def test_invokeresult_alias_is_result():

    assert InvokeResult is Result


def test_result_field_defaults():

    r = Result(ok=True, text="x", exit_code=0)
    assert r.cost_usd is None and r.session_id is None and r.raw is None
    assert r.error is None and r.schema_applied is None and r.timed_out is False and r.status is None


def test_schema_applied_passthrough_default_none():

    assert parse_claude_json(_OK_JSON, 0).schema_applied is None
    assert Result(ok=False, text="", exit_code=-1, schema_applied=False).schema_applied is False




_CLAUDE_STREAM = "\n".join([
    json.dumps({"type": "system", "subtype": "init", "model": "haiku"}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "to scrutinize files"}]}}),
    json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}),
    json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "f1"}]}}),
    json.dumps({"type": "result", "subtype": "success", "result": "complete",
                "total_cost_usd": 0.02, "session_id": "sess-7", "is_error": False}),
])


def test_claude_stream_parses_final_result_event():

    res = parse_claude_stream(_CLAUDE_STREAM, 0)
    assert res.ok and res.text == "complete" and res.session_id == "sess-7"
    assert res.cost_usd == 0.02
    assert res.transcript == _CLAUDE_STREAM


def test_claude_stream_is_error_in_result_event_not_ok():

    stream = _CLAUDE_STREAM.replace('"is_error": false', '"is_error": true') \
                           .replace('"subtype": "success"', '"subtype": "error_max_turns"')
    res = parse_claude_stream(stream, 0)
    assert not res.ok and res.error == "error_max_turns"
    assert res.transcript == stream


def test_claude_stream_truncated_no_result_event():


    truncated = "\n".join(_CLAUDE_STREAM.splitlines()[:3])
    res = parse_claude_stream(truncated, 137)
    assert not res.ok and "result" in (res.error or "")
    assert res.transcript == truncated


def test_claude_stream_single_json_compat():

    res = parse_claude_stream(_OK_JSON, 0)
    assert res.ok and res.text == "done: added function X" and res.session_id == "sess-123"


def test_claude_stream_pretty_printed_single_json_compat():


    pretty = json.dumps(json.loads(_OK_JSON), indent=2, ensure_ascii=False)
    res = parse_claude_stream(pretty, 0)
    assert res.ok and res.text == "done: added function X"


def test_claude_stream_single_event_is_truncation_not_ok():


    one_event = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "to begin to think"}]}})
    res0 = parse_claude_stream(one_event, 0)
    assert not res0.ok and "result" in (res0.error or "")
    assert res0.transcript == one_event
    res137 = parse_claude_stream(one_event, 137)
    assert not res137.ok and "result" in (res137.error or "")
    assert res137.transcript == one_event


def test_claude_stream_non_json_graceful():
    res = parse_claude_stream("boom: command not found", 127)
    assert not res.ok and res.error == "non-json output from claude"


def test_codex_jsonl_transcript_is_full_stdout():

    res = parse_codex_jsonl(_CODEX_JSONL, 0)
    assert res.ok and res.transcript == _CODEX_JSONL
