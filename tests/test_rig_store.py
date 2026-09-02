"""Tests for memory storage, promotion, scoping, deduplication, and consolidation."""












from __future__ import annotations

import sqlite3

import pytest

from rig.db import dao, schema
from rig.memory import store


def _db(tmp_path, name="s.db"):
    return dao.connect(str(tmp_path / name))


def _sid(conn, name="proj"):
    return dao.create_session(conn, name=name)



def test_types_match_lessons_kind_enum():

    assert set(store.KINDS) == set(schema.LESSON_KIND)
    assert {store.PRINCIPLE, store.FEEDBACK, store.FEATURE, store.TECHDEBT, store.FREE} == set(schema.LESSON_KIND)

    assert store.KINDS is schema.LESSON_KIND



def test_ground_truth_fail_becomes_active_rule(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="approach X endorsement test", kind=store.PRINCIPLE,
                             scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    row = dao.get_lesson(conn, lid)
    assert row["status"] == "active" and row["ground_truth"] == 1
    assert len(dao.list_lessons(conn, status="active")) == 1
    assert dao.list_lessons(conn, status="pending") == []
    conn.close()


def test_active_feature_lesson_indexes_manifest(tmp_path):


    from rig.memory import recall
    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="placeholder authorization rule: always hash password", kind=store.FEATURE,
                             scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True, feature="auth")
    m = dao.get_manifest(conn, "auth")
    assert m and m["ref_table"] == "lessons" and m["ref_id"] == lid
    assert "hash password" in m["summary"]
    assert "auth" in recall.build_manifest(conn).names
    assert any("hash password" in ln for ln in recall.build_manifest(conn).lines)
    conn.close()


def test_pending_feature_lesson_not_indexed(tmp_path):


    conn = _db(tmp_path)
    sid = _sid(conn)
    store.write_lesson(conn, text="possibly here haste", kind=store.FEATURE,
                       scope=store.ORCHESTRATOR, session_id=sid, ground_truth=False, feature="race")
    assert dao.get_manifest(conn, "race") is None
    conn.close()


def test_retire_by_prefix_scoped_to_feature(tmp_path):



    conn = _db(tmp_path)
    sid = _sid(conn)
    a = store.write_lesson(conn, text="unit u1: gate FAIL - plan A", kind=store.PRINCIPLE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True, feature="feat-a")
    b = store.write_lesson(conn, text="unit u1: gate FAIL - plan B", kind=store.PRINCIPLE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True, feature="feat-b")
    legacy = store.write_lesson(conn, text="unit u1: gate FAIL - without feature", kind=store.PRINCIPLE,
                                scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True, feature="")
    retired = store.retire_active_by_prefix(conn, "unit u1: gate FAIL", feature="feat-b")
    assert set(retired) == {b, legacy}
    assert dao.get_lesson(conn, a)["status"] == "active"
    assert dao.get_lesson(conn, b)["status"] == "retired"

    assert set(store.retire_active_by_prefix(conn, "unit u1: gate FAIL")) == {a}
    conn.close()


def test_judgment_becomes_pending_not_rule(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="judge countless (numbers of) tautological", scope=store.ORCHESTRATOR,
                             session_id=sid, ground_truth=False)
    row = dao.get_lesson(conn, lid)
    assert row["status"] == "pending" and row["ground_truth"] == 0
    assert dao.list_lessons(conn, status="active") == []
    conn.close()


def test_wiring_gate_vs_judge_delegate(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    store.write_lesson(conn, text="gate FAIL — assert fail", scope=store.ORCHESTRATOR,
                       session_id=sid, ground_truth=True)
    store.write_lesson(conn, text="delegate FAIL — worker fail", scope=store.ORCHESTRATOR,
                       session_id=sid, ground_truth=False)
    store.write_lesson(conn, text="judge deviation — not then", scope=store.ORCHESTRATOR,
                       session_id=sid, ground_truth=False)
    store.write_lesson(conn, text="gate not execute (TIMEOUT)", scope=store.ORCHESTRATOR,
                       session_id=sid, ground_truth=False)
    assert len(dao.list_lessons(conn, status="active")) == 1
    assert len(dao.list_lessons(conn, status="pending")) == 3
    conn.close()



def test_sanitize_collapses_whitespace_and_truncates():
    assert store.sanitize("a\n\t  b   c") == "a b c"
    assert store.sanitize(None) == ""
    assert len(store.sanitize("x" * 500)) == 200


def test_write_lesson_anti_injection_no_fake_bullet(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="real\n- [gate] FORGERY\n# heading",
                             scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    text = dao.get_lesson(conn, lid)["text"]
    assert "\n" not in text
    assert "FORGERY" in text and "heading" in text
    assert len(dao.list_lessons(conn)) == 1
    conn.close()



def test_dedup_same_text_kind_noop(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    a = store.write_lesson(conn, text="that same lesson", kind=store.PRINCIPLE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    b = store.write_lesson(conn, text="that same lesson", kind=store.PRINCIPLE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    assert a == b
    assert len(dao.list_lessons(conn)) == 1

    c = store.write_lesson(conn, text="that same lesson", kind=store.FREE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    assert c != a and len(dao.list_lessons(conn)) == 2
    conn.close()






def test_dedup_pending_first_then_gt_fail_promotes_to_active(tmp_path):




    conn = _db(tmp_path)
    sid = _sid(conn)

    p = store.write_lesson(conn, text="approach X endorsement test", kind=store.PRINCIPLE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=False)
    assert dao.get_lesson(conn, p)["status"] == "pending"

    g = store.write_lesson(conn, text="approach X endorsement test", kind=store.PRINCIPLE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    assert g == p
    row = dao.get_lesson(conn, g)
    assert row["status"] == "active" and row["ground_truth"] == 1
    assert len(dao.list_lessons(conn, status="active")) == 1
    assert dao.list_lessons(conn, status="pending") == []
    conn.close()


def test_dedup_retired_relearned_by_gt_fail(tmp_path):



    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="cache is invalidated incredibly", kind=store.PRINCIPLE,
                             scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    dao.set_lesson_status(conn, lid, "retired")
    assert dao.get_lesson(conn, lid)["status"] == "retired"

    again = store.write_lesson(conn, text="cache is invalidated incredibly", kind=store.PRINCIPLE,
                               scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)
    assert again == lid
    row = dao.get_lesson(conn, again)
    assert row["status"] == "active" and row["ground_truth"] == 1
    assert len(dao.list_lessons(conn, status="active")) == 1
    conn.close()


def test_dedup_noop_when_nothing_to_promote(tmp_path):




    conn = _db(tmp_path)
    sid = _sid(conn)
    a = store.write_lesson(conn, text="rule ground truth", kind=store.PRINCIPLE,
                           scope=store.ORCHESTRATOR, session_id=sid, ground_truth=True)

    again = dao.add_lesson(conn, text="rule ground truth", kind=store.PRINCIPLE,
                           session_id=sid, ground_truth=True, status="active")
    assert again == a and dao.get_lesson(conn, a)["status"] == "active"

    weak = dao.add_lesson(conn, text="rule ground truth", kind=store.PRINCIPLE,
                          session_id=sid, ground_truth=False, status="pending")
    assert weak == a
    row = dao.get_lesson(conn, a)
    assert row["status"] == "active" and row["ground_truth"] == 1
    assert len(dao.list_lessons(conn)) == 1
    conn.close()



def test_scope_leaf_writes_own_session(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="mine lesson", scope=store.LEAF, session_id=sid, ground_truth=True)
    assert dao.get_lesson(conn, lid)["session_id"] == sid
    conn.close()


def test_scope_leaf_rejects_foreign_session(tmp_path):

    conn = _db(tmp_path)
    mine = dao.create_session(conn, name="mine")
    other = dao.create_session(conn, name="other")
    with pytest.raises(PermissionError):
        store.write_lesson(conn, text="sb elses lesson", scope=store.LEAF, session_id=mine,
                           target_session_id=other, ground_truth=True)
    assert dao.list_lessons(conn) == []
    conn.close()


def test_scope_leaf_forces_own_ignoring_no_target(tmp_path):

    conn = _db(tmp_path)
    mine = dao.create_session(conn, name="mine")
    dao.create_session(conn, name="other")
    lid = store.write_lesson(conn, text="lesson", scope=store.LEAF, session_id=mine, ground_truth=True)
    assert dao.get_lesson(conn, lid)["session_id"] == mine
    conn.close()


def test_scope_leaf_requires_identity(tmp_path):

    conn = _db(tmp_path)
    with pytest.raises(ValueError):
        store.write_lesson(conn, text="lesson", scope=store.LEAF, session_id=None, ground_truth=True)
    conn.close()


def test_scope_orchestrator_writes_any_session(tmp_path):

    conn = _db(tmp_path)
    me = dao.create_session(conn, name="lead")
    leaf = dao.create_session(conn, name="leaf")
    lid = store.write_lesson(conn, text="for having prominent veins", scope=store.ORCHESTRATOR, session_id=me,
                             target_session_id=leaf, ground_truth=True)
    assert dao.get_lesson(conn, lid)["session_id"] == leaf
    conn.close()


def test_scope_unknown_rejected(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    with pytest.raises(ValueError):
        store.write_lesson(conn, text="x", scope="admin", session_id=sid, ground_truth=True)
    conn.close()



def test_write_lesson_rejects_feedback_kind(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    with pytest.raises(ValueError):
        store.write_lesson(conn, text="x", kind=store.FEEDBACK, scope=store.ORCHESTRATOR,
                           session_id=sid, ground_truth=True)
    conn.close()


def test_write_lesson_rejects_unknown_kind(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    with pytest.raises(ValueError):
        store.write_lesson(conn, text="x", kind="bogus", scope=store.ORCHESTRATOR,
                           session_id=sid, ground_truth=True)
    conn.close()



def test_feedback_is_advisory_active_never_rule(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_feedback(conn, text="not start straight, without emoji", session_id=sid)
    row = dao.get_lesson(conn, lid)
    assert row["kind"] == "feedback" and row["status"] == "active" and row["ground_truth"] == 0
    conn.close()


def test_feedback_empty_rejected_no_write(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    with pytest.raises(ValueError):
        store.write_feedback(conn, text="", session_id=sid)
    with pytest.raises(ValueError):
        store.write_feedback(conn, text="   \n\t ", session_id=sid)
    assert dao.list_lessons(conn) == []
    conn.close()


def test_feedback_dedup_and_sanitize(tmp_path):
    conn = _db(tmp_path)
    sid = _sid(conn)
    a = store.write_feedback(conn, text="write to short", session_id=sid)
    b = store.write_feedback(conn, text="write to short", session_id=sid)
    assert a == b and len(dao.list_lessons(conn)) == 1
    c = store.write_feedback(conn, text="line1\n- forgery\n# heading", session_id=sid)
    assert "\n" not in dao.get_lesson(conn, c)["text"]
    conn.close()


def test_feedback_scope_enforced(tmp_path):

    conn = _db(tmp_path)
    mine = dao.create_session(conn, name="mine")
    other = dao.create_session(conn, name="other")
    with pytest.raises(PermissionError):
        store.write_feedback(conn, text="stranger", scope=store.LEAF, session_id=mine, target_session_id=other)
    conn.close()



def test_plan_age_decay_drops_old_non_ground_truth():
    items = [
        store.Item(text="old advice", ts_days=0, ground_truth=False),
        store.Item(text="fresh advice", ts_days=9, ground_truth=False),
    ]
    plan = store.plan_consolidation(items, max_items=100, max_age_days=5, now_ts=10)
    keep = [it.text for it in plan.keep]
    assert "fresh advice" in keep and "old advice" not in keep
    assert plan.reason["old advice"] == "age"


def test_plan_ground_truth_survives_age():

    items = [
        store.Item(text="old BUT ground truth", ts_days=0, ground_truth=True),
        store.Item(text="old advice", ts_days=0, ground_truth=False),
    ]
    plan = store.plan_consolidation(items, max_items=100, max_age_days=5, now_ts=100)
    keep = [it.text for it in plan.keep]
    assert "old BUT ground truth" in keep
    assert "old advice" not in keep


def test_plan_near_dedup_merges_reformulations():
    items = [
        store.Item(text="approach X endorsement test on idle talker bringing in(to)", ts_days=1),
        store.Item(text="Approach X endorsement test on idle talker bringing in(to)!!!", ts_days=2),
    ]
    plan = store.plan_consolidation(items, max_items=100)
    assert len(plan.keep) == 1 and len(plan.drop) == 1
    assert plan.reason[plan.drop[0].text] == "near-dup"


def test_plan_near_dedup_ground_truth_wins_over_judgment():
    items = [
        store.Item(text="cache is invalidated incredibly when removal key", ts_days=5, ground_truth=False),
        store.Item(text="cache is invalidated incredibly when removal key", ts_days=1, ground_truth=True),
    ]
    plan = store.plan_consolidation(items, max_items=100)
    assert len(plan.keep) == 1 and plan.keep[0].ground_truth is True


def test_plan_cap_drops_oldest_keeps_ground_truth():
    items = [
        store.Item(text="effort1", ts_days=1, ground_truth=False),
        store.Item(text="effort2", ts_days=2, ground_truth=False),
        store.Item(text="ground old", ts_days=0, ground_truth=True),
        store.Item(text="fresh item", ts_days=9, ground_truth=False),
    ]
    plan = store.plan_consolidation(items, max_items=2)
    keep = [it.text for it in plan.keep]
    assert len(keep) == 2
    assert "ground old" in keep and "fresh item" in keep
    assert "effort1" not in keep and plan.reason["effort1"] == "cap"


def test_plan_deterministic_same_input_same_output():

    items = [
        store.Item(text="a", ts_days=0, ground_truth=False),
        store.Item(text="b", ts_days=3, ground_truth=False),
        store.Item(text="c", ts_days=1, ground_truth=True),
    ]
    p1 = store.plan_consolidation(items, max_items=2, max_age_days=2, now_ts=5)
    p2 = store.plan_consolidation(items, max_items=2, max_age_days=2, now_ts=5)
    assert [it.text for it in p1.keep] == [it.text for it in p2.keep]
    assert [it.text for it in p1.drop] == [it.text for it in p2.drop]
    assert p1.reason == p2.reason


def test_plan_accepts_dict_items():
    plan = store.plan_consolidation(
        [{"text": "x", "ground_truth": True}, {"text": "x", "ground_truth": True}], max_items=100)
    assert len(plan.keep) == 1


def test_plan_keep_preserves_order():
    items = [store.Item(text=f"lesson {i}", ts_days=i, ground_truth=True) for i in range(5)]
    plan = store.plan_consolidation(items, max_items=100)
    assert [it.text for it in plan.keep] == [it.text for it in items]


def test_plan_cap_undated_keeps_newest_tail_like_ring():

    items = [store.Item(text=f"lesson {i}", ts_days=None, ground_truth=True) for i in range(5)]
    plan = store.plan_consolidation(items, max_items=2)
    assert [it.text for it in plan.keep] == ["lesson 3", "lesson 4"]
    assert plan.reason["lesson 0"] == "cap" and "lesson 4" not in plan.reason


def test_plan_near_dedup_keeps_different_lessons_sharing_prefix():

    a = "parse_config endorsement on idle talker configuration without obligatory key podklyucheniya host"
    b = "parse_config endorsement on idle talker configuration without obligatory key podklyucheniya port"
    plan = store.plan_consolidation(
        [store.Item(text=a, ground_truth=True), store.Item(text=b, ground_truth=True)], max_items=100)
    keep = [it.text for it in plan.keep]
    assert a in keep and b in keep and len(plan.keep) == 2 and not plan.drop



def _active_lesson(conn, sid, text, *, gt):


    return dao.add_lesson(conn, text=text, kind=store.PRINCIPLE, session_id=sid,
                          ground_truth=gt, status="active")


def test_consolidate_cap_retires_keeps_ground_truth(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    gt = store.write_lesson(conn, text="ground truth rule", scope=store.ORCHESTRATOR,
                            session_id=sid, ground_truth=True)
    j1 = _active_lesson(conn, sid, "heightened judgment 1", gt=False)
    j2 = _active_lesson(conn, sid, "heightened judgment 2", gt=False)
    res = store.consolidate(conn, max_items=1)
    assert res.kept == 1 and res.dropped == 2
    assert dao.get_lesson(conn, gt)["status"] == "active"
    assert {j1, j2} == set(res.retired_ids)
    assert dao.get_lesson(conn, j1)["status"] == "retired"
    assert len(dao.list_lessons(conn, status="active")) == 1
    conn.close()


def test_consolidate_age_uses_real_created_ts(tmp_path):


    conn = _db(tmp_path)
    sid = _sid(conn)
    gt = store.write_lesson(conn, text="ground old", scope=store.ORCHESTRATOR,
                            session_id=sid, ground_truth=True)
    judg = _active_lesson(conn, sid, "effort judgment", gt=False)
    base = store._iso_to_days(dao.get_lesson(conn, judg)["created_ts"])


    res_young = store.consolidate(conn, max_age_days=5, now_ts=base + 1)
    assert res_young.dropped == 0 and dao.get_lesson(conn, judg)["status"] == "active"


    res_old = store.consolidate(conn, max_age_days=5, now_ts=base + 10)
    assert dao.get_lesson(conn, judg)["status"] == "retired"
    assert res_old.reason[dao.get_lesson(conn, judg)["text"]] == "age"
    assert dao.get_lesson(conn, gt)["status"] == "active"
    conn.close()


def test_consolidate_near_dedup_over_sqlite(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    a = _active_lesson(conn, sid, "approach endorsement test on idle talker bringing in(to)", gt=False)
    b = _active_lesson(conn, sid, "Approach endorsement test on idle talker bringing in(to).", gt=False)
    c = _active_lesson(conn, sid, "approach endorsement test on idle talker bringing in(to) host", gt=False)
    res = store.consolidate(conn, max_items=100)
    assert res.dropped == 1 and len(res.retired_ids) == 1
    survivors = {l["lesson_id"] for l in dao.list_lessons(conn, status="active")}
    assert c in survivors
    assert len({a, b} & survivors) == 1
    conn.close()


def test_consolidate_touches_only_active_rules(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    r1 = store.write_lesson(conn, text="rule one", scope=store.ORCHESTRATOR,
                            session_id=sid, ground_truth=True)
    store.write_lesson(conn, text="rule two", scope=store.ORCHESTRATOR,
                       session_id=sid, ground_truth=True)
    fb = store.write_feedback(conn, text="advice human", session_id=sid)
    pend = store.write_lesson(conn, text="judgment in expectation", scope=store.ORCHESTRATOR,
                              session_id=sid, ground_truth=False)
    res = store.consolidate(conn, max_items=1)
    assert res.kept == 1 and res.dropped == 1
    assert dao.get_lesson(conn, fb)["status"] == "active"
    assert dao.get_lesson(conn, pend)["status"] == "pending"
    assert dao.get_lesson(conn, r1)["status"] == "retired"
    conn.close()


def test_consolidate_noop_empty_store(tmp_path):
    conn = _db(tmp_path)
    _sid(conn)
    res = store.consolidate(conn)
    assert res.kept == 0 and res.dropped == 0 and res.retired_ids == []
    conn.close()


def test_consolidate_scoped_to_session(tmp_path):

    conn = _db(tmp_path)
    a = dao.create_session(conn, name="A")
    b = dao.create_session(conn, name="B")
    _active_lesson(conn, a, "A-rule-1", gt=False)
    _active_lesson(conn, a, "A-rule-2", gt=False)
    keep_b = _active_lesson(conn, b, "B-rule", gt=False)
    res = store.consolidate(conn, max_items=1, session_id=a)
    assert res.dropped == 1
    assert dao.get_lesson(conn, keep_b)["status"] == "active"
    conn.close()



def test_promote_pending_becomes_active_and_recallable(tmp_path):



    from rig.memory import recall
    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="gate in run ( repository only through make test",
                             scope=store.ORCHESTRATOR, session_id=sid)
    before = recall.load(conn, "gate make test")
    assert all("make test" not in l["text"] for l in before.always + before.selected)
    l = store.promote_lesson(conn, lid)
    assert l["status"] == "active" and not l["ground_truth"]
    after = recall.load(conn, "gate make test")
    assert any("make test" in x["text"] for x in after.always + after.selected)
    conn.close()


def test_promote_missing_lesson_raises(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(ValueError, match="999"):
        store.promote_lesson(conn, 999)
    with pytest.raises(ValueError, match="999"):
        store.retire_lesson(conn, 999)
    conn.close()


def test_promote_active_is_idempotent_and_revives_retired(tmp_path):

    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="rule", scope=store.ORCHESTRATOR, session_id=sid,
                             ground_truth=True)
    l = store.promote_lesson(conn, lid)
    assert l["status"] == "active" and l["ground_truth"]
    dao.set_lesson_status(conn, lid, "retired")
    assert store.promote_lesson(conn, lid)["status"] == "active"
    conn.close()


def test_promote_indexes_feature_into_manifest(tmp_path):


    conn = _db(tmp_path)
    sid = _sid(conn)
    lid = store.write_lesson(conn, text="feature-lesson", scope=store.ORCHESTRATOR, session_id=sid,
                             kind=store.FEATURE, feature="login")
    assert dao.get_manifest(conn, "login") is None
    store.promote_lesson(conn, lid)
    man = dao.get_manifest(conn, "login")
    assert man and man["ref_id"] == lid
    conn.close()


def test_retire_lesson_repoints_manifest(tmp_path):


    conn = _db(tmp_path)
    sid = _sid(conn)
    old = store.write_lesson(conn, text="effort rule feature", scope=store.ORCHESTRATOR,
                             session_id=sid, kind=store.FEATURE, feature="pay", ground_truth=True)
    new = store.write_lesson(conn, text="new rule feature", scope=store.ORCHESTRATOR,
                             session_id=sid, kind=store.FEATURE, feature="pay", ground_truth=True)
    assert dao.get_manifest(conn, "pay")["ref_id"] == new
    l = store.retire_lesson(conn, new)
    assert l["status"] == "retired"
    assert dao.get_manifest(conn, "pay")["ref_id"] == old
    store.retire_lesson(conn, old)
    assert dao.get_manifest(conn, "pay") is None
    conn.close()
