"""Tests for local and tmux transports, process control, environment isolation, and file access."""









import json
import os
import shutil
import subprocess
import tempfile
import time

import pytest

from rig.transport.base import ExecResult, Handle, Runner
from rig.transport.local import LocalRunner
from rig.transport.tmux import TmuxRunner

def _tmux_usable() -> bool:

    if shutil.which("tmux") is None:
        return False
    session = f"rig-pytest-probe-{os.getpid()}"
    probe = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "sleep", "5"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return False
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
        text=True,
        check=False,
    )
    return True


HAS_TMUX = _tmux_usable()
needs_tmux = pytest.mark.skipif(not HAS_TMUX, reason="tmux server is unavailable")


def _r(**kw):
    return LocalRunner(**kw)



def test_exec_ok():
    res = _r().exec(["echo", "hello"], cwd="/tmp")
    assert res.ok and res.exit_code == 0 and res.stdout.strip() == "hello"
    assert isinstance(res, ExecResult)


def test_exec_nonzero():
    res = _r().exec(["sh", "-c", "exit 3"], cwd="/tmp")
    assert not res.ok and res.exit_code == 3


def test_local_is_a_runner():
    assert isinstance(_r(), Runner)



@pytest.mark.parametrize(
    "bad",
    [
        "rm -rf /",
        "rm -rf /etc",
        "rm --recursive --force /",
        "rm -rf /*",
        'rm -rf "$HOME"',
        "find / -delete",
        "chmod -R 000 /",
        ": () { : ; } ; :",
    ],
)
def test_guard_blocks_adversarial(bad):
    with pytest.raises(PermissionError):
        _r().exec(["sh", "-c", bad], cwd="/tmp")


def test_guard_allows_scoped_rm():
    d = tempfile.mkdtemp()
    f = os.path.join(d, "x")
    open(f, "w").close()
    res = _r().exec(["rm", "-rf", f], cwd=d)
    assert res.ok and not os.path.exists(f)


def test_guard_is_best_effort_not_boundary():


    d = tempfile.mkdtemp()
    victim = os.path.join(d, "victim")
    open(victim, "w").close()
    res = _r().exec(["python3", "-c", f"import os; os.remove({victim!r})"], cwd=d)
    assert res.ok and not os.path.exists(victim)


def test_allow_catastrophic_bypasses_tripwire():

    d = tempfile.mkdtemp()
    res = _r(allow_catastrophic=True).exec(
        ["sh", "-c", "echo find / -delete >/dev/null; true"], cwd=d
    )
    assert res.ok



@pytest.mark.parametrize("risk", ["high", "core"])
def test_risk_blocked_without_allow(risk):
    with pytest.raises(PermissionError):
        _r().exec(["true"], cwd="/tmp", risk=risk)


@pytest.mark.parametrize("risk", ["low", "medium"])
def test_risk_low_medium_allowed(risk):
    assert _r().exec(["true"], cwd="/tmp", risk=risk).ok


def test_risk_high_allowed_when_configured():
    assert _r(allow_high_risk=True).exec(["true"], cwd="/tmp", risk="high").ok



def test_worker_env_drops_inherited_secrets(monkeypatch):

    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:realpass@h/db")
    monkeypatch.setenv("MY_API_KEY", "sk-secret-xyz")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tok-abc")
    log = os.path.join(tempfile.mkdtemp(), "w.log")
    h = _r().spawn(["sh", "-c", "env"], cwd="/tmp", log_path=log, worker=True)
    h.wait(timeout=5)
    out = open(log).read()
    assert "SSH_AUTH_SOCK" not in out
    assert "realpass" not in out
    assert "sk-secret-xyz" not in out
    assert "tok-abc" not in out
    assert "PATH=" in out


def test_worker_env_keeps_explicit_auth():
    log = os.path.join(tempfile.mkdtemp(), "w.log")
    h = _r().spawn(
        ["sh", "-c", "echo ${ANTHROPIC_API_KEY:-none}"],
        cwd="/tmp", log_path=log, env={"ANTHROPIC_API_KEY": "injected"}, worker=True,
    )
    h.wait(timeout=5)
    assert "injected" in open(log).read()


def test_nonworker_exec_inherits_env(monkeypatch):
    monkeypatch.setenv("ORCH_INFRA_VAR", "visible")
    res = _r().exec(["sh", "-c", "echo $ORCH_INFRA_VAR"], cwd="/tmp")
    assert "visible" in res.stdout


def test_nonworker_spawn_inherits_secrets(monkeypatch):

    monkeypatch.setenv("ORCH_ONLY_VAR", "kept")
    log = os.path.join(tempfile.mkdtemp(), "n.log")
    h = _r().spawn(["sh", "-c", "echo $ORCH_ONLY_VAR"], cwd="/tmp", log_path=log, worker=False)
    h.wait(timeout=5)
    assert "kept" in open(log).read()



def test_exec_timeout_returns_result():
    res = _r().exec(["sleep", "5"], cwd="/tmp", timeout=0.3)
    assert res.timed_out and res.exit_code == 124 and not res.ok


def test_exec_missing_binary_returns_result():
    res = _r().exec(["no_such_binary_xyz_123"], cwd="/tmp")
    assert res.exit_code == 127 and not res.ok and "not found" in res.stderr


def test_exec_bad_cwd_returns_result():
    res = _r().exec(["true"], cwd="/no/such/dir/xyz")
    assert not res.ok and res.exit_code in (126, 127)


def test_exec_env_none_and_int_value_ok():
    res = _r().exec(["sh", "-c", "echo ok"], cwd="/tmp", env={"REMOVED": None, "PORT": 8080})
    assert res.ok and res.stdout.strip() == "ok"


def test_exec_nul_in_argv_returns_result():



    res = _r().exec(["echo", "hello\x00world"], cwd="/tmp")
    assert res.exit_code == 126 and not res.ok and "ValueError" in res.stderr



def test_spawn_returns_handle():
    log = os.path.join(tempfile.mkdtemp(), "h.log")
    h = _r().spawn(["true"], cwd="/tmp", log_path=log, worker=False)
    assert isinstance(h, Handle) and h.log_path == log
    h.wait(timeout=5)


def test_spawn_poll_kill_group():
    log = os.path.join(tempfile.mkdtemp(), "s.log")
    h = _r().spawn(["sh", "-c", "sleep 30 & echo up; wait"], cwd="/tmp", log_path=log, worker=False)
    time.sleep(0.3)
    assert h.poll() is None
    h.kill()
    time.sleep(0.4)
    assert h.poll() is not None


def test_kill_reaps_orphaned_child():


    d = tempfile.mkdtemp()
    marker = os.path.join(d, "child_survived")
    h = _r().spawn(
        ["sh", "-c", f'( trap "" TERM; sleep 3; echo x > {marker!r} ) & exit 0'],
        cwd="/tmp", log_path=os.path.join(d, "k.log"), worker=False,
    )
    time.sleep(0.3)
    h.kill()
    time.sleep(0.6)
    assert not os.path.exists(marker)


def test_spawn_no_fd_leak_smoke():
    r = _r()
    d = tempfile.mkdtemp()
    for i in range(60):
        r.spawn(["true"], cwd="/tmp", log_path=os.path.join(d, f"{i}.log"), worker=False).wait(timeout=5)


def test_ambient_log_append_durable():
    p = os.path.join(tempfile.mkdtemp(), "ambient.jsonl")
    r = _r(log_path=p)
    r.exec(["true"], cwd="/tmp")
    evs = [json.loads(l)["ev"] for l in open(p)]
    assert "exec" in evs and "exec_done" in evs


def test_ambient_log_records_guard_block():
    p = os.path.join(tempfile.mkdtemp(), "ambient.jsonl")
    r = _r(log_path=p)
    with pytest.raises(PermissionError):
        r.exec(["sh", "-c", "rm -rf /"], cwd="/tmp")
    evs = [json.loads(l)["ev"] for l in open(p)]
    assert "guard_block" in evs



def test_file_io_roundtrip():
    r = _r()
    p = os.path.join(tempfile.mkdtemp(), "sub", "f.txt")
    assert not r.exists(p)
    r.write_file(p, "data-123")
    assert r.exists(p) and r.read_file(p) == "data-123"


def test_read_file_binary_raises_clear_error_not_raw_unicode():


    r = _r()
    p = os.path.join(tempfile.mkdtemp(), "bin.dat")
    with open(p, "wb") as fh:
        fh.write(b"\xff\xfe\x00\x80binary\x00")
    with pytest.raises(ValueError) as ei:
        r.read_file(p)
    assert "not UTF-8" in str(ei.value)



def _tr(**kw):

    return TmuxRunner(session_prefix=f"rigtest-{os.getpid()}-{int(time.time()*1000) % 100000}", **kw)


@needs_tmux
def test_tmux_is_a_runner():
    assert isinstance(_tr(), Runner)


@needs_tmux
def test_tmux_spawn_result_from_file():
    log = os.path.join(tempfile.mkdtemp(), "t.log")
    h = _tr().spawn(["sh", "-c", "echo hi-tmux"], cwd="/tmp", log_path=log, worker=False)
    assert h.wait(timeout=10) == 0
    assert "hi-tmux" in open(log).read()


@needs_tmux
def test_tmux_nonzero_exit_from_status():
    log = os.path.join(tempfile.mkdtemp(), "t.log")
    h = _tr().spawn(["sh", "-c", "echo boom >&2; exit 7"], cwd="/tmp", log_path=log, worker=False)
    assert h.wait(timeout=10) == 7
    assert "boom" in open(log).read()


@needs_tmux
def test_tmux_survives_parent_detached():



    d = tempfile.mkdtemp()
    marker = os.path.join(d, "done")
    log = os.path.join(d, "t.log")
    r = _tr()
    h = r.spawn(["sh", "-c", f"sleep 0.6; echo ok > {marker!r}"], cwd="/tmp", log_path=log, worker=False)
    assert h.poll() is None
    assert r._has_session(h._session)
    assert h.wait(timeout=10) == 0
    assert os.path.exists(marker)


@needs_tmux
def test_tmux_poll_kill():
    log = os.path.join(tempfile.mkdtemp(), "t.log")
    h = _tr().spawn(["sh", "-c", "sleep 30"], cwd="/tmp", log_path=log, worker=False)
    time.sleep(0.3)
    assert h.poll() is None
    h.kill()
    time.sleep(0.4)
    assert h.poll() is not None


@needs_tmux
def test_tmux_worker_cut_legs_over_server_env(monkeypatch):


    monkeypatch.setenv("MY_API_KEY", "sk-tmux-leak-xyz")
    log = os.path.join(tempfile.mkdtemp(), "t.log")
    h = _tr().spawn(["sh", "-c", "env"], cwd="/tmp", log_path=log, worker=True)
    h.wait(timeout=10)
    out = open(log).read()
    assert "sk-tmux-leak-xyz" not in out
    assert "PATH=" in out


@needs_tmux
def test_tmux_exec_delegates_contract():

    r = _tr()
    assert r.exec(["echo", "x"], cwd="/tmp").stdout.strip() == "x"
    assert r.exec(["no_such_bin_xyz"], cwd="/tmp").exit_code == 127
    assert r.exec(["sleep", "5"], cwd="/tmp", timeout=0.3).exit_code == 124
