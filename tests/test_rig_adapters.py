"""Contract tests for the Claude and Codex CLI adapters."""















from __future__ import annotations

import json
import os
import tempfile

import pytest

from rig.adapters.base import Capabilities, HarnessAdapter, InvokeResult, SchemaNotApplied
from rig.adapters.claude import ClaudeAdapter
from rig.adapters.codex import CodexAdapter
from rig.core.result import Result
from rig.transport.base import ExecResult, Handle, Runner
from rig.transport.local import LocalRunner



class _FakeHandle(Handle):
    log_path = "/fake"

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class FakeRunner(Runner):
    def __init__(self, exec_result=None):
        self._res = exec_result or ExecResult(0, "", "", 0.0)
        self.exec_calls = []
        self.spawn_calls = []
        self.files = {}

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.exec_calls.append({"cmd": cmd, "cwd": cwd, "env": env, "risk": risk, "timeout": timeout})
        return self._res

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        self.spawn_calls.append({"cmd": cmd, "cwd": cwd, "env": env, "worker": worker})
        return _FakeHandle()

    def read_file(self, path):
        return self.files[path]

    def write_file(self, path, content):
        self.files[path] = content

    def exists(self, path):
        return path in self.files


class EnvCapture(Runner):


    def __init__(self, stdout=""):
        self._stdout = stdout
        self.spawn_env = self.invoke_env = "UNSET"

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.invoke_env = env
        return ExecResult(0, self._stdout, "", 0.0)

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        self.spawn_env = env
        return _FakeHandle()

    def read_file(self, p):
        return ""

    def write_file(self, p, c):
        pass

    def exists(self, p):
        return False


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



def test_claude_capabilities():

    c = ClaudeAdapter(FakeRunner()).capabilities()
    assert c.effort and c.schema_output and c.native_resume and not c.agents_md_native


def test_claude_build_cmd_shape():


    cmd = ClaudeAdapter(FakeRunner()).build_cmd(
        "do X", model="claude-sonnet-4-6", effort="high", system_context_path="/m.md")
    assert cmd[0] == "claude" and "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert cmd[cmd.index("--append-system-prompt-file") + 1] == "/m.md"
    assert "--permission-mode" in cmd and "bypassPermissions" not in cmd
    assert cmd[-1] == "do X"


def test_claude_build_cmd_minimal():

    cmd = ClaudeAdapter(FakeRunner()).build_cmd("x", model="m")
    assert "--effort" not in cmd and "--append-system-prompt-file" not in cmd
    assert "--resume" not in cmd and "--json-schema" not in cmd and "--allowedTools" not in cmd


def test_claude_build_cmd_resume_flag():

    cmd = ClaudeAdapter(FakeRunner()).build_cmd("go", model="m", resume_session_id="sess-9")
    assert cmd[cmd.index("--resume") + 1] == "sess-9"



def test_claude_perms_default_is_safe_not_bypass():

    cmd = ClaudeAdapter(FakeRunner()).build_cmd("x", model="m")
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    assert "bypassPermissions" not in cmd and "acceptEdits" not in cmd


def test_claude_perms_plan():
    cmd = ClaudeAdapter(FakeRunner()).build_cmd("x", model="m", perms="plan")
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"


def test_claude_perms_auto_accept_edits_not_bypass():

    cmd = ClaudeAdapter(FakeRunner()).build_cmd("x", model="m", perms="auto")
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "bypassPermissions" not in cmd


def test_claude_perms_read_physical_readonly():

    cmd = ClaudeAdapter(FakeRunner()).build_cmd("x", model="m", perms="read")
    assert "--allowedTools" in cmd
    tools = cmd[cmd.index("--allowedTools") + 1]
    assert "Read" in tools and "Grep" in tools
    assert "Write" not in tools and "Edit" not in tools and "Bash" not in tools
    assert "bypassPermissions" not in cmd


def test_claude_perms_never_bypass_for_any_mode():

    a = ClaudeAdapter(FakeRunner())
    for p in ("read", "plan", "auto", "default"):
        assert "bypassPermissions" not in a.build_cmd("x", model="m", perms=p)


def test_claude_perms_unknown_raises():

    with pytest.raises(ValueError):
        ClaudeAdapter(FakeRunner()).build_cmd("x", model="m", perms="god-mode")



def test_claude_build_cmd_schema_inline():

    fr = FakeRunner()
    fr.files["/p/schema.json"] = '{"type":"object","properties":{"ok":{"type":"boolean"}}}'
    cmd = ClaudeAdapter(fr).build_cmd("x", model="m", schema_path="/p/schema.json")
    assert cmd[cmd.index("--json-schema") + 1] == '{"type":"object","properties":{"ok":{"type":"boolean"}}}'


def test_claude_schema_unreadable_raises_not_applied():

    with pytest.raises(SchemaNotApplied):
        ClaudeAdapter(_RaisingRunner()).build_cmd("x", model="m", schema_path="/nope.json")


def test_claude_schema_empty_raises_not_applied():

    fr = FakeRunner()
    fr.files["/p/empty.json"] = ""
    with pytest.raises(SchemaNotApplied):
        ClaudeAdapter(fr).build_cmd("x", model="m", schema_path="/p/empty.json")


def test_claude_invoke_schema_not_applied_fail_closed():

    r = _RaisingRunner()
    res = ClaudeAdapter(r).invoke("x", model="m", cwd="/p", schema_path="/nope.json")
    assert not res.ok and res.schema_applied is False and "schema not applied" in (res.error or "")
    assert r.exec_called is False


def test_claude_invoke_schema_applied_true_when_attached():

    fr = FakeRunner(ExecResult(0, _OK_JSON, "", 0.0))
    fr.files["/p/s.json"] = '{"type":"object"}'
    res = ClaudeAdapter(fr).invoke("x", model="m", cwd="/p", schema_path="/p/s.json")
    assert res.ok and res.schema_applied is True


def test_claude_invoke_no_schema_applied_none():

    res = ClaudeAdapter(FakeRunner(ExecResult(0, _OK_JSON, "", 0.0))).invoke("x", model="m", cwd="/p")
    assert res.schema_applied is None



def test_claude_invoke_parses_ok_json():

    fr = FakeRunner(ExecResult(0, _OK_JSON, "", 1.0))
    res = ClaudeAdapter(fr).invoke("do X", model="m", cwd="/proj")
    assert isinstance(res, Result)
    assert res.ok and res.text == "done: added function X"
    assert res.cost_usd == 0.0042 and res.session_id == "sess-123" and res.error is None
    assert fr.exec_calls[0]["cwd"] == "/proj" and fr.exec_calls[0]["risk"] == "medium"


def test_claude_parse_is_error_true_at_exit0_not_ok():

    res = ClaudeAdapter(FakeRunner()).parse_output(
        json.dumps({"is_error": True, "result": "x", "subtype": "refusal"}), 0)
    assert not res.ok and res.error == "refusal"


def test_claude_parse_result_null_no_literal_none():

    res = ClaudeAdapter(FakeRunner()).parse_output(
        json.dumps({"is_error": True, "subtype": "error_max_budget_usd", "result": None}), 1)
    assert res.text == "" and not res.ok and res.error == "error_max_budget_usd"


def test_claude_parse_non_json_graceful():

    res = ClaudeAdapter(FakeRunner()).parse_output("not json garbage", 0)
    assert isinstance(res, Result) and not res.ok and "non-json" in (res.error or "")


def test_claude_invoke_nonzero_exit_not_ok():

    res = ClaudeAdapter(FakeRunner(ExecResult(1, _OK_JSON, "boom", 1.0))).invoke("x", model="m", cwd="/p")
    assert not res.ok and res.exit_code == 1


def test_claude_invoke_wall_timeout_forces_not_ok():

    fr = FakeRunner(ExecResult(124, "", "[runner: timeout]", 0.0, timed_out=True))
    res = ClaudeAdapter(fr).invoke("x", model="m", cwd="/p", timeout=0.01)
    assert not res.ok and res.timed_out and res.exit_code == 124
    assert fr.exec_calls[0]["timeout"] == 0.01



def test_claude_inject_context_no_clobber():

    fr = FakeRunner()
    p = ClaudeAdapter(fr).inject_context("METHOD RULES", cwd="/proj")
    assert "/proj/.rig/system-prompt-" in p and p.endswith(".md")
    assert "/proj/CLAUDE.md" not in fr.files
    assert fr.files[p] == "METHOD RULES"


def test_claude_inject_context_scopes_by_content_no_clobber():



    fr = FakeRunner()
    ad = ClaudeAdapter(fr)
    p_lead = ad.inject_context("METHOD LEAD", cwd="/proj")
    p_worker = ad.inject_context("brief WORKER", cwd="/proj")
    assert p_lead != p_worker
    assert ad.inject_context("METHOD LEAD", cwd="/proj") == p_lead
    assert fr.files[p_lead] == "METHOD LEAD" and fr.files[p_worker] == "brief WORKER"



def test_claude_auth_env_reinjects_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-abc")
    assert ClaudeAdapter(FakeRunner()).auth_env().get("ANTHROPIC_API_KEY") == "sk-abc"


def test_claude_auth_env_empty_when_no_creds(monkeypatch):

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert ClaudeAdapter(FakeRunner()).auth_env() == {}


def test_claude_spawn_uses_runner_worker():

    fr = FakeRunner()
    ClaudeAdapter(fr).spawn("x", model="m", cwd="/p", log_path="/p/w.log")
    assert fr.spawn_calls[0]["worker"] is True and fr.spawn_calls[0]["cmd"][0] == "claude"


def test_claude_spawn_and_invoke_wire_auth_env(monkeypatch):


    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-wire")
    r = EnvCapture(stdout='{"result": "ok", "is_error": false}')
    a = ClaudeAdapter(r)
    a.spawn("prompt", model="m", cwd="/p", log_path="/p/w.log")
    assert r.spawn_env and r.spawn_env.get("ANTHROPIC_API_KEY") == "sk-wire"
    a.invoke("prompt", model="m", cwd="/p")
    assert r.invoke_env and r.invoke_env.get("ANTHROPIC_API_KEY") == "sk-wire"


def test_claude_security_chain_real_runner(monkeypatch):


    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-keep")
    monkeypatch.setenv("MY_DB_SECRET", "leak-me")
    a = ClaudeAdapter(LocalRunner())
    log = os.path.join(tempfile.mkdtemp(), "w.log")

    h = a.runner.spawn(["sh", "-c", "env"], cwd="/tmp", log_path=log, env=a.auth_env(), worker=True)
    h.wait(timeout=5)
    out = open(log).read()
    assert "sk-keep" in out
    assert "leak-me" not in out
    assert "MY_DB_SECRET" not in out



def test_codex_capabilities_agents_md_native():

    c = CodexAdapter(FakeRunner()).capabilities()
    assert c.effort and c.schema_output and c.native_resume
    assert c.agents_md_native is True


def test_codex_build_cmd_differs_from_claude():

    cmd = CodexAdapter(FakeRunner()).build_cmd("do X", model="gpt-5.5", effort="high")
    assert cmd[:2] == ["codex", "exec"]
    assert "--json" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert "-p" not in cmd and "--effort" not in cmd
    joined = " ".join(cmd)
    assert "model_reasoning_effort=high" in joined
    assert "approval_policy=" in joined
    assert cmd[-1] == "do X"


def test_codex_build_cmd_minimal_no_effort():
    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="m")
    assert "model_reasoning_effort" not in " ".join(cmd)
    assert "--output-schema" not in cmd and "resume" not in cmd


def test_codex_build_cmd_empty_model_omits_flag():


    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="")
    assert "--model" not in cmd
    assert cmd[-1] == "x" and "--json" in cmd
    cmd2 = CodexAdapter(FakeRunner()).build_cmd("x", model="gpt-5.5")
    assert cmd2[cmd2.index("--model") + 1] == "gpt-5.5"


def test_codex_build_cmd_schema_is_file_path():

    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="m", schema_path="/p/schema.json")
    assert cmd[cmd.index("--output-schema") + 1] == "/p/schema.json"


def test_codex_resume_is_subcommand():

    cmd = CodexAdapter(FakeRunner()).build_cmd("go", model="m", resume_session_id="t-7")
    assert cmd[:4] == ["codex", "exec", "resume", "t-7"]



def test_codex_perms_default_workspace_on_request():

    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="m")
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "approval_policy=on-request" in " ".join(cmd)


def test_codex_perms_auto_workspace_never():

    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="m", perms="auto")
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "approval_policy=never" in " ".join(cmd)


def test_codex_perms_read_sandbox_readonly():

    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="m", perms="read")
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_codex_perms_plan_sandbox_readonly():
    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="m", perms="plan")
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_codex_perms_never_danger_for_any_mode():

    a = CodexAdapter(FakeRunner())
    for p in ("read", "plan", "auto", "default"):
        joined = " ".join(a.build_cmd("x", model="m", perms=p))
        assert "danger-full-access" not in joined


def test_codex_perms_unknown_raises():
    with pytest.raises(ValueError):
        CodexAdapter(FakeRunner()).build_cmd("x", model="m", perms="yolo")


def test_codex_approval_is_config_not_flag():

    cmd = CodexAdapter(FakeRunner()).build_cmd("x", model="m")
    assert "approval_policy=" in " ".join(cmd) and "--approval-policy" not in cmd



def test_codex_parse_jsonl():

    res = CodexAdapter(FakeRunner()).parse_output(_CODEX_JSONL, 0)
    assert res.ok and res.text == "done: codex did X"
    assert res.cost_usd is None
    assert res.raw["usage"]["input_tokens"] == 50
    assert res.session_id == "019ef5fe-abc"


def test_codex_parse_agent_message_content_list():

    jsonl = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t-1"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                    "content": [{"type": "text", "text": "part1 "},
                                {"type": "text", "text": "part2"}]}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ])
    res = CodexAdapter(FakeRunner()).parse_output(jsonl, 0)
    assert res.ok and res.text == "part1 part2" and "[{" not in res.text


def test_codex_parse_error_event_not_ok():

    bad = _CODEX_JSONL + "\n" + json.dumps({"type": "error", "message": "mcp failed"})
    res = CodexAdapter(FakeRunner()).parse_output(bad, 0)
    assert not res.ok and res.error == "mcp failed"


def test_codex_parse_non_jsonl_graceful():

    res = CodexAdapter(FakeRunner()).parse_output("totally not jsonl", 0)
    assert not res.ok and "non-jsonl" in (res.error or "")


def test_codex_invoke_through_fakerunner():

    fr = FakeRunner(ExecResult(0, _CODEX_JSONL, "", 1.0))
    res = CodexAdapter(fr).invoke("do X", model="gpt-5.5", cwd="/proj")
    assert res.ok and res.text == "done: codex did X"
    assert fr.exec_calls[0]["cmd"][:2] == ["codex", "exec"]



def test_codex_inject_context_no_clobber():

    fr = FakeRunner()
    p = CodexAdapter(fr).inject_context("METHOD RULES", cwd="/proj")
    assert "/proj/.rig/system-prompt-" in p and p.endswith(".md")
    assert "/proj/AGENTS.md" not in fr.files
    assert fr.files[p] == "METHOD RULES"


def test_codex_auth_env(monkeypatch):
    monkeypatch.setenv("CODEX_API_KEY", "ck-123")
    assert CodexAdapter(FakeRunner()).auth_env() == {"CODEX_API_KEY": "ck-123"}


def test_codex_auth_env_empty_when_no_creds(monkeypatch):
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    assert CodexAdapter(FakeRunner()).auth_env() == {}


def test_codex_spawn_worker():
    fr = FakeRunner()
    CodexAdapter(fr).spawn("x", model="m", cwd="/p", log_path="/p/w.log")
    assert fr.spawn_calls[0]["worker"] is True and fr.spawn_calls[0]["cmd"][:2] == ["codex", "exec"]


def test_codex_spawn_and_invoke_wire_auth_env(monkeypatch):

    monkeypatch.setenv("CODEX_API_KEY", "ck-wire")
    r = EnvCapture(stdout=_CODEX_JSONL)
    a = CodexAdapter(r)
    a.spawn("prompt", model="m", cwd="/p", log_path="/p/w.log")
    assert r.spawn_env and r.spawn_env.get("CODEX_API_KEY") == "ck-wire"
    a.invoke("prompt", model="m", cwd="/p")
    assert r.invoke_env and r.invoke_env.get("CODEX_API_KEY") == "ck-wire"



def test_invokeresult_alias_is_core_result():

    assert InvokeResult is Result


def test_both_adapters_are_harness_adapters():

    assert issubclass(ClaudeAdapter, HarnessAdapter) and issubclass(CodexAdapter, HarnessAdapter)
    for cls in (ClaudeAdapter, CodexAdapter):
        a = cls(FakeRunner())
        assert isinstance(a.capabilities(), Capabilities)
        for op in ("build_cmd", "parse_output", "inject_context", "auth_env"):
            assert callable(getattr(a, op))



class _RaisingRunner(Runner):


    def __init__(self):
        self.exec_called = False

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.exec_called = True
        return ExecResult(0, "", "", 0.0)

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        return _FakeHandle()

    def read_file(self, path):
        raise OSError(f"no such file: {path}")

    def write_file(self, path, content):
        pass

    def exists(self, path):
        return False
