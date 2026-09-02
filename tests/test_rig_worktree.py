"""Tests for provisioning ignored dependencies and configuration into isolated worktrees."""










from __future__ import annotations

import json
import os
import subprocess

import pytest

from rig.tools.spawn import Spawner
from rig.util import worktree as wt



class RecRunner:
    def __init__(self, ok=True):
        self.calls = []
        self._ok = ok

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False):
        self.calls.append({"cmd": list(cmd), "cwd": cwd, "risk": risk, "timeout": timeout})
        from rig.transport.base import ExecResult
        return ExecResult(0 if self._ok else 1, "", "", 0.0)


def _write_cfg(repo, obj):
    d = os.path.join(repo, ".rig")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "worktree.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f)



def test_read_config_missing_is_empty(tmp_path):

    assert wt.read_config(str(tmp_path)).is_empty()


def test_read_config_parses_lists(tmp_path):
    _write_cfg(str(tmp_path), {"link": [".venv", "frontend/node_modules"],
                               "copy": [".env"], "setup": ["bash", "-lc", "uv sync"]})
    cfg = wt.read_config(str(tmp_path))
    assert cfg.link == [".venv", "frontend/node_modules"]
    assert cfg.copy == [".env"]
    assert cfg.setup == ["bash", "-lc", "uv sync"]


def test_read_config_malformed_is_empty(tmp_path):
    d = os.path.join(str(tmp_path), ".rig"); os.makedirs(d)
    with open(os.path.join(d, "worktree.json"), "w") as f:
        f.write("{not json")
    assert wt.read_config(str(tmp_path)).is_empty()


def test_read_config_non_dict_is_empty(tmp_path):
    _write_cfg(str(tmp_path), ["link"])
    assert wt.read_config(str(tmp_path)).is_empty()


def test_read_config_filters_non_strings(tmp_path):
    _write_cfg(str(tmp_path), {"link": [".venv", 5, None, {"x": 1}], "copy": "not-a-list"})
    cfg = wt.read_config(str(tmp_path))
    assert cfg.link == [".venv"] and cfg.copy == []



@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape", "~/x", "..", ".", "a/../../b", "", "   "])
def test_safe_rel_rejects_escapes(bad):
    assert wt._safe_rel(bad) is None


@pytest.mark.parametrize("good,exp", [(".venv", ".venv"),
                                      ("frontend/node_modules", "frontend/node_modules"),
                                      ("a/b/c", "a/b/c"), ("a/../b", "b")])
def test_safe_rel_allows_relative(good, exp):
    assert wt._safe_rel(good) == exp



def test_provision_link_symlinks_from_main(tmp_path):
    repo = tmp_path / "repo"; (repo / "vendor").mkdir(parents=True)
    (repo / "vendor" / "dep.txt").write_text("DEP", encoding="utf-8")
    wtd = tmp_path / "wt"; wtd.mkdir()
    rep = wt.provision(None, str(repo), str(wtd), wt.WorktreeConfig(link=["vendor"]))
    link = wtd / "vendor"
    assert rep.linked == ["vendor"]
    assert os.path.islink(str(link))
    assert (link / "dep.txt").read_text(encoding="utf-8") == "DEP"
    assert os.path.realpath(str(link)) == os.path.realpath(str(repo / "vendor"))


def test_provision_link_missing_source_skipped(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    wtd = tmp_path / "wt"; wtd.mkdir()
    rep = wt.provision(None, str(repo), str(wtd), wt.WorktreeConfig(link=["nope"]))
    assert rep.linked == [] and any("no spring" in s for s in rep.skipped)
    assert not (wtd / "nope").exists()


def test_provision_link_no_clobber(tmp_path):
    repo = tmp_path / "repo"; (repo / "vendor").mkdir(parents=True)
    wtd = tmp_path / "wt"; (wtd / "vendor").mkdir(parents=True)
    rep = wt.provision(None, str(repo), str(wtd), wt.WorktreeConfig(link=["vendor"]))
    assert rep.linked == [] and any("already exists" in s for s in rep.skipped)
    assert not os.path.islink(str(wtd / "vendor"))


def test_provision_copy_file_not_symlink(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / ".env").write_text("SECRET=1", encoding="utf-8")
    wtd = tmp_path / "wt"; wtd.mkdir()
    rep = wt.provision(None, str(repo), str(wtd), wt.WorktreeConfig(copy=[".env"]))
    assert rep.copied == [".env"]
    assert not os.path.islink(str(wtd / ".env"))
    assert (wtd / ".env").read_text(encoding="utf-8") == "SECRET=1"


def test_provision_traversal_skipped_no_fs_escape(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    wtd = tmp_path / "wt"; wtd.mkdir()
    rep = wt.provision(None, str(repo), str(wtd),
                       wt.WorktreeConfig(link=["../../etc"], copy=["/etc/x"]))
    assert rep.linked == [] and rep.copied == []
    assert len(rep.skipped) == 2 and all("unsafe" in s for s in rep.skipped)


def test_provision_setup_runs_in_worktree_with_args(tmp_path):

    rr = RecRunner(ok=True)
    wtd = tmp_path / "wt"; wtd.mkdir()
    rep = wt.provision(rr, str(tmp_path), str(wtd),
                       wt.WorktreeConfig(setup=["bash", "-lc", "uv sync"]))
    assert rep.setup_ok is True
    assert {"cmd": ["bash", "-lc", "uv sync"], "cwd": str(wtd), "risk": "medium", "timeout": 600} in rr.calls


def test_provision_setup_failure_nonfatal(tmp_path):
    wtd = tmp_path / "wt"; wtd.mkdir()
    rep = wt.provision(RecRunner(ok=False), str(tmp_path), str(wtd),
                       wt.WorktreeConfig(setup=["false"]))
    assert rep.setup_ok is False


def test_provision_empty_config_noop(tmp_path):

    rr = RecRunner()
    rep = wt.provision(rr, str(tmp_path), str(tmp_path), wt.WorktreeConfig())
    assert rep.linked == [] and rep.copied == [] and rep.setup_ok is None
    assert rr.calls == []



def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


class _Ad:

    def __init__(self, runner):
        self.runner = runner


def _seed_repo(repo):
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("vendor/\nlocal.env\n", encoding="utf-8")
    (repo / "vendor").mkdir(); (repo / "vendor" / "dep.txt").write_text("DEP", encoding="utf-8")
    (repo / "local.env").write_text("K=V", encoding="utf-8")
    _git(repo, "add", "app.py", ".gitignore")
    _git(repo, "commit", "-q", "-m", "seed")


def test_make_worktree_provisions_on_real_git(tmp_path):




    from rig.transport.local import LocalRunner
    from rig.util import git as gitutil
    repo = tmp_path / "proj"
    _seed_repo(repo)
    os.makedirs(repo / ".rig")
    (repo / ".rig" / "worktree.json").write_text(
        '{"link": ["vendor"], "copy": ["local.env"]}', encoding="utf-8")

    runner = LocalRunner()
    sp = Spawner(adapter=_Ad(runner), repo=str(repo), model="m")
    wtpath = sp._make_worktree("u1", "HEAD")


    assert os.path.islink(os.path.join(wtpath, "vendor"))
    assert open(os.path.join(wtpath, "vendor", "dep.txt")).read() == "DEP"
    assert open(os.path.join(wtpath, "local.env")).read() == "K=V"
    assert not os.path.islink(os.path.join(wtpath, "local.env"))
    assert open(os.path.join(wtpath, "app.py")).read() == "x = 1\n"


    porcelain = _git(wtpath, "status", "--porcelain").stdout
    assert "vendor" not in porcelain and "local.env" not in porcelain
    assert _git(wtpath, "check-ignore", "vendor").returncode == 0


    gitutil.worktree_remove(runner, str(repo), wtpath)
    assert not os.path.exists(wtpath)
    assert (repo / "vendor" / "dep.txt").read_text(encoding="utf-8") == "DEP"


def test_make_worktree_no_config_is_clean(tmp_path):

    from rig.transport.local import LocalRunner
    repo = tmp_path / "proj"
    _seed_repo(repo)
    sp = Spawner(adapter=_Ad(LocalRunner()), repo=str(repo), model="m")
    wtpath = sp._make_worktree("u1", "HEAD")
    assert open(os.path.join(wtpath, "app.py")).read() == "x = 1\n"
    assert not os.path.exists(os.path.join(wtpath, "vendor"))


def test_provision_report_is_logged_not_silent(tmp_path):


    from rig.db import dao
    conn = dao.connect(":memory:")
    sid = dao.create_session(conn, name="s")
    sp = Spawner(adapter=None, repo="/r", model="m", conn=conn, session_id=sid)
    sp._log_provision("U1", wt.ProvisionReport(linked=[".venv"], copied=[".env"],
                                               skipped=["node_modules: no spring"], setup_ok=False))
    prov = [r for r in dao.list_runs(conn, session_id=sid) if r["kind"] == "provision"]
    assert len(prov) == 1
    res = prov[0]["result"]
    assert ".venv" in res and ".env" in res and "node_modules" in res and "setup=FAIL" in res
    sp._log_provision("U2", wt.ProvisionReport())
    assert len([r for r in dao.list_runs(conn, session_id=sid) if r["kind"] == "provision"]) == 1
