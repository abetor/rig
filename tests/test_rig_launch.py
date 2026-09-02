"""Tests for synchronous, parallel, and background agent invocation."""














from __future__ import annotations

import json
import os
import time
from subprocess import TimeoutExpired

import pytest

from rig.adapters.base import Capabilities, HarnessAdapter
from rig.adapters.claude import ClaudeAdapter
from rig.adapters.codex import CodexAdapter
from rig.core.launch import BackgroundRun, agent
from rig.core.result import Result
from rig.db import dao
from rig.transport.base import ExecResult, Handle, Runner



class _DoneHandle(Handle):


    def __init__(self, log_path):
        self.log_path = log_path

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class EchoRunner(Runner):



    def __init__(self, *, delay: float = 0.0):
        self.delay = delay
        self.exec_calls = []
        self.spawn_calls = []

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.exec_calls.append({"cmd": cmd, "cwd": cwd, "env": env, "risk": risk,
                                "timeout": timeout, "worker": worker})
        if self.delay:
            time.sleep(self.delay)
        return ExecResult(0, " ".join(cmd[1:]), "", self.delay)

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        self.spawn_calls.append({"cmd": cmd, "cwd": cwd, "env": env, "worker": worker})
        self.write_file(log_path, " ".join(cmd[1:]))
        return _DoneHandle(log_path)

    def read_file(self, path):
        with open(path) as fh:
            return fh.read()

    def write_file(self, path, content):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def exists(self, path):
        return os.path.exists(path)


class EchoAdapter(HarnessAdapter):



    name = "echo"

    def __init__(self, runner, *, schema_output: bool = True, binary: str = "echo"):
        super().__init__(runner)
        self._schema_output = schema_output
        self.binary = binary

    def capabilities(self):
        return Capabilities(effort=True, schema_output=self._schema_output,
                            native_resume=True, agents_md_native=True)

    def build_cmd(self, prompt, *, model, perms="default", effort=None, system_context_path=None,
                  resume_session_id=None, schema_path=None, allowed_tools=None, confine=False):
        return [self.binary, prompt]

    def parse_output(self, stdout, exit_code):
        return Result(ok=(exit_code == 0), text=stdout, exit_code=exit_code)

    def inject_context(self, method_text, cwd):
        p = os.path.join(cwd, ".rig", "system-prompt.md")
        self.runner.write_file(p, method_text)
        return p

    def auth_env(self):
        return {}


class RecRunner(Runner):



    def __init__(self, exec_result=None):
        self._res = exec_result or ExecResult(0, "", "", 0.0)
        self.exec_calls = []
        self.spawn_calls = []
        self.files = {}

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.exec_calls.append({"cmd": cmd, "cwd": cwd, "env": env, "risk": risk,
                                "timeout": timeout, "worker": worker})
        return self._res

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        self.spawn_calls.append({"cmd": cmd, "cwd": cwd, "env": env, "worker": worker})
        return _DoneHandle(log_path)

    def read_file(self, path):
        return self.files[path]

    def write_file(self, path, content):
        self.files[path] = content

    def exists(self, path):
        return path in self.files


class _RaisingRunner(Runner):


    def __init__(self):
        self.exec_called = False

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.exec_called = True
        return ExecResult(0, "", "", 0.0)

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        return _DoneHandle(log_path)

    def read_file(self, path):
        raise OSError(f"no such file: {path}")

    def write_file(self, path, content):
        pass

    def exists(self, path):
        return False


class _HangingHandle(Handle):



    def __init__(self, log_path):
        self.log_path = log_path
        self.killed = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        raise TimeoutExpired(cmd="bg", timeout=timeout or 0)

    def kill(self):
        self.killed = True


class _HangRunner(EchoRunner):


    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        self.spawn_calls.append({"cmd": cmd})
        self.handle = _HangingHandle(log_path)
        return self.handle


_OK_JSON = json.dumps({
    "result": "done: added function X", "session_id": "sess-123",
    "total_cost_usd": 0.0042, "is_error": False,
})
_CODEX_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "t-1"}),
    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex did X"}}),
    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5}}),
])



def test_agent_sync_returns_result(tmp_path):

    res = agent(EchoAdapter(EchoRunner()), "greeting(s) peace", cwd=str(tmp_path), model="m")
    assert isinstance(res, Result)
    assert res.ok and res.text == "greeting(s) peace" and res.exit_code == 0


def test_agent_sync_passes_through_runner(tmp_path):

    er = EchoRunner()
    agent(EchoAdapter(er), "x", cwd=str(tmp_path), model="m", timeout=12.5, risk="low")
    assert er.exec_calls[0]["cwd"] == str(tmp_path)
    assert er.exec_calls[0]["timeout"] == 12.5
    assert er.exec_calls[0]["risk"] == "low"


def test_confine_scrubs_worker_class_roles_on_sync(tmp_path):



    for role, expect in [("worker", True), ("judge", True), ("scout", True),
                         ("lead", False), ("", False)]:
        er = EchoRunner()
        agent(EchoAdapter(er), "x", cwd=str(tmp_path), model="m", role=role)
        assert er.exec_calls[0]["worker"] is expect, role


def test_confine_fail_closed_on_unknown_role(tmp_path):




    for role in ("worrker", "reviewer", "new-role"):
        er = EchoRunner()
        agent(EchoAdapter(er), "x", cwd=str(tmp_path), model="m", role=role)
        assert er.exec_calls[0]["worker"] is True, role



def test_agent_perms_default_reaches_build_cmd():

    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    agent(ClaudeAdapter(r), "x", cwd="/p", model="m")
    cmd = r.exec_calls[0]["cmd"]
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    assert "bypassPermissions" not in cmd and "acceptEdits" not in cmd


def test_agent_perms_read_physical_readonly():

    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    agent(ClaudeAdapter(r), "x", cwd="/p", model="m", perms="read")
    cmd = r.exec_calls[0]["cmd"]
    assert "--allowedTools" in cmd
    tools = cmd[cmd.index("--allowedTools") + 1]
    assert "Read" in tools and "Write" not in tools and "Bash" not in tools


def test_agent_perms_auto_accept_edits():

    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    agent(ClaudeAdapter(r), "x", cwd="/p", model="m", perms="auto")
    cmd = r.exec_calls[0]["cmd"]
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "bypassPermissions" not in cmd


def test_agent_perms_reach_codex_too():

    r = RecRunner(ExecResult(0, _CODEX_JSONL, "", 0.0))
    agent(CodexAdapter(r), "x", cwd="/p", model="m", perms="read")
    cmd = r.exec_calls[0]["cmd"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_agent_invalid_perms_raises():

    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), "x", perms="god-mode", cwd="/p", model="m")



def test_agent_resume_passthrough():

    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    agent(ClaudeAdapter(r), "go", cwd="/p", model="m", resume_id="sess-9")
    assert r.exec_calls[0]["cmd"][r.exec_calls[0]["cmd"].index("--resume") + 1] == "sess-9"


def test_agent_isolated_drops_resume():

    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    agent(ClaudeAdapter(r), "go", cwd="/p", model="m", resume_id="sess-9", isolated=True)
    assert "--resume" not in r.exec_calls[0]["cmd"]



def test_agent_wall_timeout_forces_not_ok():

    r = RecRunner(ExecResult(124, "", "[timeout]", 0.0, timed_out=True))
    res = agent(ClaudeAdapter(r), "x", cwd="/p", model="m", timeout=0.01)
    assert not res.ok and res.timed_out and res.exit_code == 124
    assert r.exec_calls[0]["timeout"] == 0.01


def test_agent_honest_parse_is_error_not_ok():

    r = RecRunner(ExecResult(0, json.dumps({"is_error": True, "result": "x", "subtype": "refusal"}), "", 0.0))
    res = agent(ClaudeAdapter(r), "x", cwd="/p", model="m")
    assert not res.ok and res.error == "refusal"



def test_agent_invalid_wait_raises():
    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), "x", wait="nope", cwd="/p", model="m")


def test_agent_requires_model():
    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), "x", cwd="/p")


def test_agent_requires_cwd():
    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), "x", model="m")


def test_agent_sync_rejects_list():
    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), ["a", "b"], wait="sync", cwd="/p", model="m")



def test_agent_schema_fail_closed_no_capability(tmp_path):

    er = EchoRunner()
    res = agent(EchoAdapter(er, schema_output=False), "x", cwd=str(tmp_path), model="m",
                schema="/s.json")
    assert not res.ok and res.schema_applied is False
    assert er.exec_calls == []
    assert "schema_output" in (res.error or "")


def test_agent_schema_fail_closed_not_applied():

    r = _RaisingRunner()
    res = agent(ClaudeAdapter(r), "x", cwd="/p", model="m", schema="/nope.json")
    assert not res.ok and res.schema_applied is False
    assert r.exec_called is False


def test_agent_schema_applied_ok():

    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    r.files["/p/s.json"] = '{"type":"object"}'
    res = agent(ClaudeAdapter(r), "x", cwd="/p", model="m", schema="/p/s.json")
    assert res.ok and res.schema_applied is True


def test_agent_no_schema_applied_none(tmp_path):

    res = agent(EchoAdapter(EchoRunner()), "x", cwd=str(tmp_path), model="m")
    assert res.schema_applied is None



def test_agent_parallel_returns_list_by_index(tmp_path):

    prompts = ["p0", "p1", "p2", "p3"]
    results = agent(EchoAdapter(EchoRunner()), prompts, wait="parallel",
                    cwd=str(tmp_path), model="m")
    assert isinstance(results, list) and len(results) == 4
    assert [r.text for r in results] == prompts


def test_agent_parallel_runs_concurrently(tmp_path):

    er = EchoRunner(delay=0.2)
    t0 = time.monotonic()
    results = agent(EchoAdapter(er), ["a", "b", "c", "d"], wait="parallel",
                    cwd=str(tmp_path), model="m")
    elapsed = time.monotonic() - t0
    assert len(results) == 4 and all(r.ok for r in results)
    assert elapsed < 0.5


def test_agent_parallel_deterministic_order_under_jitter(tmp_path):


    class JitterRunner(EchoRunner):
        def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
            self.exec_calls.append({"cmd": cmd})
            time.sleep(0.04 * (3 - int(cmd[1][1])))
            return ExecResult(0, " ".join(cmd[1:]), "", 0.0)

    results = agent(EchoAdapter(JitterRunner()), ["p0", "p1", "p2", "p3"], wait="parallel",
                    cwd=str(tmp_path), model="m")
    assert [r.text for r in results] == ["p0", "p1", "p2", "p3"]


def test_agent_parallel_isolates_one_failure(tmp_path):


    class BoomAdapter(EchoAdapter):
        def build_cmd(self, prompt, *, model, perms="default", effort=None,
                      system_context_path=None, resume_session_id=None, schema_path=None, allowed_tools=None, confine=False):
            if prompt == "boom":
                raise RuntimeError("kaboom")
            return [self.binary, prompt]

    results = agent(BoomAdapter(EchoRunner()), ["ok0", "boom", "ok2"], wait="parallel",
                    cwd=str(tmp_path), model="m")
    assert len(results) == 3
    assert results[0].ok and results[0].text == "ok0"
    assert not results[1].ok and "kaboom" in (results[1].error or "")
    assert results[2].ok and results[2].text == "ok2"


def test_agent_parallel_empty_list_is_empty(tmp_path):

    assert agent(EchoAdapter(EchoRunner()), [], wait="parallel", cwd=str(tmp_path), model="m") == []


def test_agent_parallel_rejects_str(tmp_path):
    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), "single", wait="parallel", cwd=str(tmp_path), model="m")



def test_agent_logs_call_to_runs(tmp_path):

    conn = dao.connect(str(tmp_path / "m.db"))
    sid = dao.create_session(conn, name="s")
    prompt = "COMPLETE PROMPT\nline 2\n  indentation\n" + "x" * 3000
    res = agent(EchoAdapter(EchoRunner()), prompt, cwd=str(tmp_path), model="claude-x",
                effort="high", conn=conn, session_id=sid, role="worker", phase="impl", perms="auto")
    runs = dao.list_runs(conn)
    assert len(runs) == 1
    row = runs[0]
    assert row["prompt"] == prompt
    assert row["result"] == res.text == prompt
    assert row["session_id"] == sid and row["kind"] == "invoke"
    assert row["adapter"] == "echo" and row["agent"] == "worker" and row["phase"] == "impl"
    assert row["ok"] == 1
    assert row["meta"]["wait"] == "sync" and row["meta"]["perms"] == "auto"
    assert row["meta"]["model"] == "claude-x" and row["meta"]["schema"] is False

    assert row["meta"]["effort"] == "high"
    conn.close()


def test_agent_logs_transcript_to_runs(tmp_path):

    stream = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "thought"}]}}),
        json.dumps({"type": "result", "subtype": "success", "result": "ok",
                    "total_cost_usd": 0.01, "session_id": "s1", "is_error": False}),
    ])
    conn = dao.connect(str(tmp_path / "t.db"))
    agent(ClaudeAdapter(RecRunner(ExecResult(0, stream, "", 0.0))), "x", cwd="/p",
          model="m", conn=conn)
    row = dao.list_runs(conn)[0]
    assert row["transcript"] == stream
    assert row["result"] == "ok" and row["ok"] == 1
    conn.close()


def test_agent_log_cost_origin_exact_for_claude(tmp_path):

    conn = dao.connect(str(tmp_path / "c.db"))
    agent(ClaudeAdapter(RecRunner(ExecResult(0, _OK_JSON, "", 0.0))), "x", cwd="/p", model="m", conn=conn)
    row = dao.list_runs(conn)[0]
    assert row["cost"] == 0.0042 and row["cost_origin"] == "exact"
    assert row["resume_id"] == "sess-123"
    conn.close()


def test_agent_log_cost_origin_no_pricing_for_codex(tmp_path):

    conn = dao.connect(str(tmp_path / "x.db"))
    agent(CodexAdapter(RecRunner(ExecResult(0, _CODEX_JSONL, "", 0.0))), "x", cwd="/p", model="m", conn=conn)
    row = dao.list_runs(conn)[0]
    assert row["cost"] is None and row["cost_origin"] == "no_pricing"
    conn.close()


def test_agent_parallel_logs_each_call(tmp_path):

    conn = dao.connect(str(tmp_path / "p.db"))
    sid = dao.create_session(conn, name="s")
    agent(EchoAdapter(EchoRunner()), ["p0", "p1", "p2"], wait="parallel",
          cwd=str(tmp_path), model="m", conn=conn, session_id=sid)
    runs = dao.list_runs(conn)
    assert len(runs) == 3
    assert sorted(r["prompt"] for r in runs) == ["p0", "p1", "p2"]
    assert all(r["meta"]["wait"] == "parallel" and r["session_id"] == sid for r in runs)
    conn.close()


def test_agent_abstain_is_logged(tmp_path):

    conn = dao.connect(str(tmp_path / "a.db"))
    agent(EchoAdapter(EchoRunner(), schema_output=False), "x", cwd=str(tmp_path), model="m",
          schema="/s.json", conn=conn)
    runs = dao.list_runs(conn)
    assert len(runs) == 1 and runs[0]["ok"] == 0
    assert runs[0]["meta"]["schema"] is True and runs[0]["meta"]["schema_applied"] is False
    conn.close()


def test_agent_no_conn_runs_without_log(tmp_path):

    res = agent(EchoAdapter(EchoRunner()), "x", cwd=str(tmp_path), model="m")
    assert res.ok and res.text == "x"



def test_agent_background_returns_handle_then_collects(tmp_path):

    bg = agent(EchoAdapter(EchoRunner()), "hi-bg", wait="background",
               cwd=str(tmp_path), model="m")
    assert isinstance(bg, BackgroundRun)
    assert bg.poll() == 0
    res = bg.result(timeout=5)
    assert res.ok and res.text == "hi-bg"


def test_agent_background_logs_on_collect_not_spawn(tmp_path):

    conn = dao.connect(str(tmp_path / "bg.db"))
    sid = dao.create_session(conn, name="s")
    bg = agent(EchoAdapter(EchoRunner()), "deferred", wait="background", cwd=str(tmp_path),
               model="m", conn=conn, session_id=sid, role="worker")
    assert dao.list_runs(conn) == []
    res = bg.result(timeout=5)
    runs = dao.list_runs(conn)
    assert len(runs) == 1
    assert runs[0]["result"] == res.text == "deferred"
    assert runs[0]["meta"]["wait"] == "background" and runs[0]["agent"] == "worker"
    bg.result(timeout=5)
    assert len(dao.list_runs(conn)) == 1
    conn.close()


def test_agent_background_rejects_schema(tmp_path):

    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), "x", wait="background", schema="/s.json",
              cwd=str(tmp_path), model="m")


def test_agent_background_rejects_list(tmp_path):
    with pytest.raises(ValueError):
        agent(EchoAdapter(EchoRunner()), ["a"], wait="background", cwd=str(tmp_path), model="m")


def test_agent_background_wall_timeout_kills_and_not_ok(tmp_path):




    runner = _HangRunner()
    bg = agent(EchoAdapter(runner), "hang", wait="background", cwd=str(tmp_path), model="m")
    res = bg.result(timeout=0.01)
    assert not res.ok and res.timed_out and res.exit_code == 124
    assert runner.handle.killed is True


def test_agent_background_isolated_drops_resume(tmp_path):



    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    agent(ClaudeAdapter(r), "go", wait="background", cwd="/p", model="m",
          resume_id="sess-9", isolated=True)
    assert "--resume" not in r.spawn_calls[0]["cmd"]


def test_agent_background_keeps_resume_when_not_isolated(tmp_path):

    r = RecRunner(ExecResult(0, _OK_JSON, "", 0.0))
    agent(ClaudeAdapter(r), "go", wait="background", cwd="/p", model="m", resume_id="sess-9")
    cmd = r.spawn_calls[0]["cmd"]
    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "sess-9"
