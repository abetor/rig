"""Tests for memory recall, ranking, manifests, and model-assisted selection."""












from __future__ import annotations

from rig.db import dao
from rig.memory import recall, store


def _db(tmp_path, name="r.db"):
    return dao.connect(str(tmp_path / name))


def _sid(conn, name="proj"):
    return dao.create_session(conn, name=name)


def _lesson(conn, sid, text, *, kind=store.FREE, feature="", gt=False, status="active"):

    return dao.add_lesson(conn, text=text, kind=kind, session_id=sid, feature=feature,
                          ground_truth=gt, status=status)



def test_recall_finds_by_keyword(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    _lesson(conn, sid, "gate = tests opredelyayut truth", kind=store.FREE)
    target = _lesson(conn, sid, "worktree for isolation worker", kind=store.FREE)
    hits = recall.recall(conn, "worktree isolation")
    assert hits and hits[0]["lesson_id"] == target and "worktree" in hits[0]["text"]
    conn.close()


def test_recall_ranks_by_term_overlap(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    multi = _lesson(conn, sid, "worktree branch isolation merge", kind=store.FEATURE)
    single = _lesson(conn, sid, "worktree only", kind=store.FEATURE)
    hits = recall.recall(conn, "worktree isolation merge")
    assert hits[0]["lesson_id"] == multi and hits[0]["score"] == 3
    assert hits[-1]["lesson_id"] == single and hits[-1]["score"] == 1
    assert hits[0]["score"] >= hits[-1]["score"]
    conn.close()


def test_recall_excludes_nonmatching_score_boundary(tmp_path):





    conn = _db(tmp_path)
    sid = _sid(conn)
    m1 = _lesson(conn, sid, "worktree isolation working tree", kind=store.FREE)
    m2 = _lesson(conn, sid, "worktree live before attempt", kind=store.FREE)
    n1 = _lesson(conn, sid, "cache to go bad by taymautu joining", kind=store.FREE)
    n2 = _lesson(conn, sid, "retry only on actionable truncate", kind=store.FREE)
    hits = recall.recall(conn, "worktree")
    ids = {h["lesson_id"] for h in hits}
    assert ids == {m1, m2}
    assert n1 not in ids and n2 not in ids
    assert len(hits) == 2
    assert all(h["score"] > 0 for h in hits)
    conn.close()


def test_recall_empty_query_returns_empty(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    _lesson(conn, sid, "worktree to throw out", kind=store.FREE)
    assert recall.recall(conn, "   ") == []
    assert recall.recall(conn, "") == []
    conn.close()


def test_recall_nonpositive_k_empty(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    _lesson(conn, sid, "worktree to throw out", kind=store.FREE)
    assert recall.recall(conn, "worktree", k=0) == []
    assert recall.recall(conn, "worktree", k=-1) == []
    conn.close()


def test_recall_short_term_fallback(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    _lesson(conn, sid, "DATABASE through postgres to throw out", kind=store.FREE)
    hits = recall.recall(conn, "DATABASE")
    assert hits and "DATABASE" in hits[0]["text"]
    conn.close()


def test_recall_dedup_same_text(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    a = _lesson(conn, sid, "worktree isolation", kind=store.FREE)
    b = _lesson(conn, sid, "worktree isolation", kind=store.FEATURE)
    assert a != b
    hits = recall.recall(conn, "worktree")
    assert len(hits) == 1
    conn.close()


def test_recall_empty_memory_no_crash(tmp_path):
    conn = _db(tmp_path)
    _sid(conn)
    assert recall.recall(conn, "that what would you like, what can i do for you") == []
    conn.close()


def test_recall_top_k_caps_results(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    for i in range(7):
        _lesson(conn, sid, f"worktree case {i}", kind=store.FEATURE)
    hits = recall.recall(conn, "worktree", k=3)
    assert len(hits) == 3
    conn.close()


def test_recall_excludes_pending_by_default(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    dao.add_lesson(conn, text="worktree pending", kind=store.FEATURE, session_id=sid, status="pending")
    assert recall.recall(conn, "worktree") == []
    assert len(recall.recall(conn, "worktree", status="pending")) == 1
    conn.close()



def test_recall_by_feature_tag(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    _lesson(conn, sid, "treacle authorization to go bad", kind=store.FEATURE, feature="auth")
    _lesson(conn, sid, "treacle index base data", kind=store.FEATURE, feature="db")
    hits = recall.recall(conn, "treacle", feature="auth")
    assert len(hits) == 1 and hits[0]["feature"] == "auth"
    conn.close()


def test_recall_freshness_tiebreak(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    old = _lesson(conn, sid, "worktree old case", kind=store.FEATURE)
    new = _lesson(conn, sid, "worktree fresh case", kind=store.FEATURE)
    assert new > old
    hits = recall.recall(conn, "worktree")
    assert [h["lesson_id"] for h in hits] == [new, old]
    conn.close()



def test_always_load_only_principles_and_context(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    p = _lesson(conn, sid, "worktree provides isolation file", kind=store.PRINCIPLE, gt=True)
    fb = store.write_feedback(conn, text="not start straight without emoji", session_id=sid)
    feat = _lesson(conn, sid, "feature-lesson", kind=store.FEATURE)
    rows = recall.always_load(conn)
    kinds = {l["kind"] for l in rows}
    ids = {l["lesson_id"] for l in rows}
    assert kinds <= {store.PRINCIPLE, store.FEEDBACK}
    assert p in ids and fb in ids and feat not in ids
    conn.close()


def test_always_load_gt_priority_under_cap(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    judgment = _lesson(conn, sid, "heightened judgment", kind=store.PRINCIPLE, gt=False, status="active")
    gt = _lesson(conn, sid, "ground truth rule", kind=store.PRINCIPLE, gt=True)
    rows = recall.always_load(conn, cap=1)
    assert len(rows) == 1 and rows[0]["lesson_id"] == gt
    assert judgment != gt
    conn.close()


def test_load_separates_always_from_selected(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    p = _lesson(conn, sid, "worktree provides isolation file", kind=store.PRINCIPLE, gt=True)
    store.write_feedback(conn, text="write to short", session_id=sid)
    feat = _lesson(conn, sid, "worktree live before attempt", kind=store.FEATURE, feature="wt")
    r = recall.load(conn, "worktree")
    always_ids = {l["lesson_id"] for l in r.always}
    selected_ids = {h["lesson_id"] for h in r.selected}
    assert p in always_ids
    assert feat in selected_ids
    assert p not in selected_ids
    assert not (always_ids & selected_ids)
    assert {l["kind"] for l in r.always} == {store.PRINCIPLE, store.FEEDBACK}
    conn.close()


def test_load_empty_memory(tmp_path):
    conn = _db(tmp_path)
    _sid(conn)
    r = recall.load(conn, "that what would you like, what can i do for you")
    assert r.always == [] and r.selected == []
    conn.close()



def test_manifest_counts_by_kind_and_feature(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    _lesson(conn, sid, "p1", kind=store.PRINCIPLE, gt=True)
    _lesson(conn, sid, "p2", kind=store.PRINCIPLE, gt=True)
    _lesson(conn, sid, "f1", kind=store.FEATURE, feature="auth")
    dao.add_lesson(conn, text="waits human", kind=store.FREE, session_id=sid, status="pending")
    mf = recall.build_manifest(conn)
    assert mf.total_active == 3
    assert mf.kinds["principle"] == 2 and mf.kinds["feature"] == 1
    assert mf.features["auth"] == 1
    assert mf.pending == 1
    assert any("(!)" in ln for ln in mf.lines)
    assert any("active resolve: 3" in ln for ln in mf.lines)
    conn.close()


def test_manifest_empty_memory_no_crash(tmp_path):
    conn = _db(tmp_path)
    _sid(conn)
    mf = recall.build_manifest(conn)
    assert mf.total_active == 0 and mf.pending == 0
    assert mf.kinds == {} and mf.features == {} and mf.lines == [] and mf.names == []
    conn.close()


def test_manifest_respects_cap(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    for i in range(12):
        _lesson(conn, sid, f"lesson {i}", kind=store.FEATURE, feature=f"feat{i:02d}")
    mf = recall.build_manifest(conn, cap=4)
    assert len(mf.lines) <= 4
    assert len(mf.names) >= 12
    conn.close()



def test_validate_names_drops_hallucinated(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    dao.upsert_manifest(conn, name="auth-module", summary="modulus authorization")
    _lesson(conn, sid, "lesson about base", kind=store.FEATURE, feature="db")
    out = recall.validate_names(conn, ["auth-module", "db", "HALLUCINATION", "db"])
    assert out == ["auth-module", "db"]
    assert recall.validate_names(conn, []) == [] and recall.validate_names(conn, None) == []
    conn.close()


def test_model_recall_hook_validates_and_caps(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    dao.upsert_manifest(conn, name="auth-module")
    _lesson(conn, sid, "lesson about base", kind=store.FEATURE, feature="db")
    seen = {}

    def fake_agent(query, manifest):
        seen["query"] = query
        seen["manifest"] = manifest
        return ["auth-module", "INESSENTIAL", "db"]

    out = recall.model_recall(conn, fake_agent, "initiative authorization", k=5)
    assert out == ["auth-module", "db"]
    assert isinstance(seen["manifest"], recall.Manifest) and seen["query"] == "initiative authorization"
    assert recall.model_recall(conn, fake_agent, "q", k=1) == ["auth-module"]
    conn.close()
