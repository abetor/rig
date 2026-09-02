"""Tests for startup state classification and stale-state reconciliation."""










from __future__ import annotations

from rig.db import dao
from rig.memory import (COLD, IN_FLIGHT, STALE, WARM, boot, classify,
                        read_handoff, read_state, reconcile_stale)
from rig.memory.reconcile_state import ALIVE, CORE_VERSION, GONE, MERGED
from rig.transport.base import ExecResult, Runner
from rig.util import git

PREFIX = "rig/"


class GitFakeRunner(Runner):




    def __init__(self, *, head="HEAD0", branches=None, merged=(), missing=(),
                 unreachable=False, branch_fails=False):
        self._head = head
        self._branches = list(branches or [])
        self._merged = set(merged)
        self._missing = set(missing)
        self._unreachable = unreachable
        self._branch_fails = branch_fails

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        a = cmd[3:]
        if a[:2] == ["rev-parse", "HEAD"]:
            return ExecResult(0, self._head, "", 0.0)
        if a[:2] == ["branch", "--list"]:
            if self._branch_fails:
                return ExecResult(128, "", "fatal", 0.0)
            return ExecResult(0, "\n".join(self._branches), "", 0.0)
        if a[:3] == ["rev-parse", "--verify", "--quiet"]:
            br = a[3]
            return ExecResult(1 if br in self._missing else 0, "", "", 0.0)
        if a[:2] == ["merge-base", "--is-ancestor"]:
            anc = a[2]
            return ExecResult(0 if anc in self._merged else 1, "", "", 0.0)
        if a[:2] == ["rev-list", "--count"]:
            if self._unreachable:
                return ExecResult(128, "", "fatal: bad revision", 0.0)
            return ExecResult(0, "3", "", 0.0)
        return ExecResult(0, "", "", 0.0)

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True):
        raise NotImplementedError

    def read_file(self, path):
        raise NotImplementedError

    def write_file(self, path, content):
        raise NotImplementedError

    def exists(self, path):
        return False


def _db(tmp_path):
    return dao.connect(str(tmp_path / "m.db"))


def _session(conn, *, head="", in_flight=None, core=CORE_VERSION, name="proj"):
    return dao.create_session(conn, name=name, head=head, in_flight=in_flight or [],
                              core_version=core)



def test_read_state_none_when_no_sessions(tmp_path):
    assert read_state(_db(tmp_path)) is None


def test_read_state_is_last_session(tmp_path):
    conn = _db(tmp_path)
    _session(conn, head="H_old", in_flight=["rig/u0"])
    _session(conn, head="H_new", in_flight=["rig/u1"])
    s = read_state(conn)
    assert s.head == "H_new" and s.in_flight == ["rig/u1"]



def test_classify_cold_when_no_memory(tmp_path):
    conn = _db(tmp_path)
    assert classify(GitFakeRunner(branches=[]), conn, "/repo")[0] == COLD


def test_classify_in_flight_orphan_without_marker(tmp_path):

    conn = _db(tmp_path)
    r = GitFakeRunner(branches=["rig/orphan"])
    assert classify(r, conn, "/repo")[0] == IN_FLIGHT


def test_classify_warm_head_match_no_branches(tmp_path):
    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=[])
    assert classify(GitFakeRunner(head="H1", branches=[]), conn, "/repo")[0] == WARM


def test_classify_stale_head_diverged(tmp_path):
    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=[])
    assert classify(GitFakeRunner(head="H2", branches=[]), conn, "/repo")[0] == STALE


def test_classify_stale_on_core_upgrade(tmp_path):

    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=[], core="v-OLD")
    assert classify(GitFakeRunner(head="H1", branches=[]), conn, "/repo")[0] == STALE


def test_classify_warm_when_branches_match_recorded(tmp_path):

    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=["rig/u1"])
    mode, current, _ = classify(GitFakeRunner(head="H1", branches=["rig/u1"]), conn, "/repo")
    assert mode == WARM and current == ["rig/u1"]


def test_classify_in_flight_on_unexpected_branch(tmp_path):

    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=["rig/u1"])
    mode, current, _ = classify(GitFakeRunner(head="H1", branches=["rig/u1", "rig/orphan"]), conn, "/repo")
    assert mode == IN_FLIGHT and "rig/orphan" in current



def test_read_handoff_from_latest_note_run(tmp_path):
    conn = _db(tmp_path)
    sid = _session(conn, head="H1")
    dao.log_run(conn, session_id=sid, kind="note", result="THAT HE WOULD HAVE GONE WITH THEM, BUT HE FELL ILL: add(); NEXT: sub()")
    dao.log_run(conn, session_id=sid, kind="invoke", result="not handoff")
    assert "NEXT: sub()" in read_handoff(conn)


def test_read_handoff_empty_when_no_notes(tmp_path):
    conn = _db(tmp_path)
    _session(conn, head="H1")
    assert read_handoff(conn) == ""



def test_boot_warm_remembers_handoff(tmp_path):
    conn = _db(tmp_path)
    sid = _session(conn, head="H1", in_flight=[])
    dao.log_run(conn, session_id=sid, kind="note", result="THAT HE WOULD HAVE GONE WITH THEM, BUT HE FELL ILL: sold add(); NEXT: sub()")
    res = boot(GitFakeRunner(head="H1", branches=[]), conn, "/repo")
    assert res.mode == WARM
    assert "THAT HE WOULD HAVE GONE WITH THEM, BUT HE FELL ILL" in res.context and "NEXT" in res.context
    assert "MODE ENTRY: WARM" in res.context


def test_boot_cold_has_no_handoff(tmp_path):
    conn = _db(tmp_path)
    res = boot(GitFakeRunner(branches=[]), conn, "/repo")
    assert res.mode == COLD and res.handoff == "" and "COLD" in res.context


def test_boot_cold_with_orphan_handoff_flags_partial_loss(tmp_path):

    conn = _db(tmp_path)
    dao.log_run(conn, kind="note", result="orphan-handoff")
    res = boot(GitFakeRunner(branches=[]), conn, "/repo")
    assert res.mode == COLD and "partly to lose" in res.context


def test_boot_known_branches_say_merge_one_not_all(tmp_path):

    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=["rig/u1", "rig/u2"])
    res = boot(GitFakeRunner(head="H1", branches=["rig/u1", "rig/u2"]), conn, "/repo")
    assert res.mode == WARM
    assert "wait yours merge" not in res.context
    assert "STACK" in res.context and "ONE" in res.context



def test_boot_stale_wires_reconcile_report(tmp_path):

    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=[])
    res = boot(GitFakeRunner(head="H2", branches=[]), conn, "/repo")
    assert res.mode == STALE
    assert res.report is not None and res.report.diverged is True
    assert "RECONCILIATION" in res.context and "git advanced" in res.context


def test_boot_non_stale_has_no_report(tmp_path):
    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=[])
    res = boot(GitFakeRunner(head="H1", branches=[]), conn, "/repo")
    assert res.mode == WARM and res.report is None



def test_reconcile_no_state_is_empty(tmp_path):
    rep = reconcile_stale(GitFakeRunner(head="H1"), _db(tmp_path), "/repo")
    assert rep.has_state is False and rep.diverged is False


def test_reconcile_head_moved_counts_ahead(tmp_path):
    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=[])
    rep = reconcile_stale(GitFakeRunner(head="H2", branches=[]), conn, "/repo")
    assert rep.head_moved and rep.ahead == 3 and rep.head_unreachable is False


def test_reconcile_head_unreachable_when_base_gone(tmp_path):

    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=[])
    rep = reconcile_stale(GitFakeRunner(head="H2", unreachable=True), conn, "/repo")
    assert rep.head_unreachable is True
    assert any("was not found" in r for r in rep.recommendations)


def test_reconcile_head_unconfirmed_still_diagnoses(tmp_path):


    conn = _db(tmp_path)
    _session(conn, head="", in_flight=[])
    res = boot(GitFakeRunner(head="H1", branches=[]), conn, "/repo")
    assert res.mode == STALE
    assert res.report.diverged is True
    assert "RECONCILIATION" in res.context and "HEAD not to check (sth against sth)" in res.context


def test_reconcile_branch_alive_merged_gone(tmp_path):

    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=["rig/alive", "rig/merged", "rig/gone"], core="v-OLD")
    r = GitFakeRunner(head="H1", branches=["rig/alive", "rig/merged"],
                      merged=["rig/merged"], missing=["rig/gone"])
    rep = reconcile_stale(r, conn, "/repo")
    byname = {f.branch: f.status for f in rep.branches}
    assert byname == {"rig/alive": ALIVE, "rig/merged": MERGED, "rig/gone": GONE}
    assert rep.core_upgraded is True and rep.diverged is True


def test_boot_in_flight_wires_reconcile_for_orphans(tmp_path):


    conn = _db(tmp_path)
    _session(conn, head="H1", in_flight=["rig/u1"])
    r = GitFakeRunner(head="H1", branches=["rig/u1", "rig/orphan"])
    res = boot(r, conn, "/repo")
    assert res.mode == IN_FLIGHT
    assert res.report is not None and res.report.diverged is True
    byname = {f.branch: f.status for f in res.report.branches}
    assert byname["rig/orphan"] == ALIVE
    assert "RECONCILIATION" in res.context and "rig/orphan" in res.context


def test_reconcile_orphans_without_state(tmp_path):


    conn = _db(tmp_path)
    r = GitFakeRunner(head="H1", branches=["rig/orphan"])
    rep = reconcile_stale(r, conn, "/repo")
    assert rep.has_state is False and rep.diverged is True
    assert {f.branch: f.status for f in rep.branches} == {"rig/orphan": ALIVE}



def test_git_head_empty_on_failure():
    class _Fail(GitFakeRunner):
        def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
            return ExecResult(128, "", "fatal", 0.0)
    assert git.head(_Fail(), "/repo") == ""


def test_git_branches_empty_on_git_failure():

    assert git.branches(GitFakeRunner(branch_fails=True), "/repo", PREFIX) == []


def test_git_ahead_count_none_when_unreachable():
    assert git.ahead_count(GitFakeRunner(unreachable=True), "/repo", "BASE", "HEAD") is None


def test_git_uses_low_risk_read_only():

    calls = []

    class _Rec(GitFakeRunner):
        def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
            calls.append(risk)
            return super().exec(cmd, cwd=cwd, env=env, timeout=timeout, risk=risk)

    r = _Rec(head="H1", branches=["rig/u1"], merged=["rig/u1"])
    git.head(r, "/repo"); git.branches(r, "/repo", PREFIX)
    git.branch_exists(r, "/repo", "rig/u1"); git.is_ancestor(r, "/repo", "rig/u1", "H1")
    git.ahead_count(r, "/repo", "B", "H")
    assert calls and all(c == "low" for c in calls)



def test_resume_demo_cold_to_warm_to_stale(tmp_path):

    conn = _db(tmp_path)

    assert boot(GitFakeRunner(branches=[]), conn, "/repo").mode == COLD

    sid = _session(conn, head="H1", in_flight=[])
    dao.log_run(conn, session_id=sid, kind="note", result="NEXT: to finish writing sub()")
    warm = boot(GitFakeRunner(head="H1", branches=[]), conn, "/repo")
    assert warm.mode == WARM and "NEXT: to finish writing sub()" in warm.context

    stale = boot(GitFakeRunner(head="H2", branches=[]), conn, "/repo")
    assert stale.mode == STALE and stale.report.diverged and stale.report.ahead == 3
