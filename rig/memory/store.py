"""Sanitize, store, promote, retire, and consolidate lessons."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from ..db import dao, schema


LEAF = "leaf"
ORCHESTRATOR = "orchestrator"
SCOPES = (LEAF, ORCHESTRATOR)

PRINCIPLE = "principle"
FEEDBACK = "feedback"
FEATURE = "feature"
TECHDEBT = "techdebt"
FREE = "free"
KINDS = schema.LESSON_KIND  # Complete enum used by validation and contract tests.

_MAX_LESSON_CHARS = 200


def sanitize(text) -> str:
    """Normalize and bound lesson text."""
    return " ".join((text or "").split())[:_MAX_LESSON_CHARS]


def _resolve_target(scope, session_id, target_session_id):
    """Resolve target."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (expected '{LEAF}' or '{ORCHESTRATOR}')")
    target = session_id if target_session_id is None else target_session_id
    if scope == LEAF:
        if session_id is None:
            raise ValueError("leaf-scope to obtain (ones) own session_id (no identical) [D5]")
        if target != session_id:
            raise PermissionError(
                f"leaf-scope writes ONLY own session ({session_id}), not {target} [D5]"
            )
    return target


def write_lesson(
    conn,
    *,
    text: str,
    scope: str,
    session_id: Optional[int],
    kind: str = FREE,
    target_session_id: Optional[int] = None,
    ground_truth: bool = False,
    feature: str = "",
) -> int:
    """Write lesson."""
    if kind not in KINDS:
        raise ValueError(f"unknown lesson type: {kind!r} (expected one of {KINDS})")
    if kind == FEEDBACK:
        raise ValueError("kind='feedback' — ADVISORY-channel: use write_feedback (not write_lesson)")
    target = _resolve_target(scope, session_id, target_session_id)
    status = "active" if ground_truth else "pending"
    clean = sanitize(text)
    lid = dao.add_lesson(
        conn,
        text=clean,
        kind=kind,
        session_id=target,
        feature=feature,
        ground_truth=bool(ground_truth),
        status=status,
    )
    if feature and status == "active":
        dao.upsert_manifest(conn, name=feature, summary=clean, kind="feature",
                            feature=feature, ref_table="lessons", ref_id=lid)
    return lid


def retire_active_by_prefix(conn, prefix: str, *, feature: str = "") -> List[int]:
    """Retire active by prefix."""
    key = sanitize(prefix)
    if not key:
        return []
    retired: List[int] = []
    for l in dao.list_lessons(conn, status="active"):
        if feature and (l.get("feature") or "") not in ("", feature):
            continue
        if (l["text"] or "").startswith(key):
            dao.set_lesson_status(conn, l["lesson_id"], "retired")
            retired.append(l["lesson_id"])
    if feature and retired:
        _repair_manifest(conn, feature, retired)
    return retired


def _repair_manifest(conn, feature: str, removed_ids: List[int]) -> None:
    """Rebuild manifest entries from active lessons."""
    man = dao.get_manifest(conn, feature)
    if not (man and man.get("ref_id") in removed_ids):
        return
    rest = [l for l in dao.list_lessons(conn, status="active") if l["feature"] == feature]
    if rest:
        rep = rest[-1]
        dao.upsert_manifest(conn, name=feature, summary=rep["text"], kind="feature",
                            feature=feature, ref_table="lessons", ref_id=rep["lesson_id"])
    else:
        dao.delete_manifest(conn, feature)


def promote_lesson(conn, lesson_id: int) -> dict:
    """Promote lesson."""
    lesson = dao.get_lesson(conn, lesson_id)
    if lesson is None:
        raise ValueError(f"lesson #{lesson_id} no")
    if lesson["status"] != "active":
        dao.set_lesson_status(conn, lesson_id, "active")
        lesson = dao.get_lesson(conn, lesson_id)
    feature = lesson.get("feature") or ""
    if feature:
        dao.upsert_manifest(conn, name=feature, summary=lesson["text"], kind="feature",
                            feature=feature, ref_table="lessons", ref_id=lesson_id)
    return lesson


def retire_lesson(conn, lesson_id: int) -> dict:
    """Retire lesson."""
    lesson = dao.get_lesson(conn, lesson_id)
    if lesson is None:
        raise ValueError(f"lesson #{lesson_id} no")
    if lesson["status"] != "retired":
        dao.set_lesson_status(conn, lesson_id, "retired")
    feature = lesson.get("feature") or ""
    if feature:
        _repair_manifest(conn, feature, [lesson_id])
    return dao.get_lesson(conn, lesson_id)


def write_feedback(
    conn,
    *,
    text: str,
    session_id: Optional[int] = None,
    scope: str = ORCHESTRATOR,
    target_session_id: Optional[int] = None,
) -> int:
    """Write feedback."""
    if not (text or "").strip():
        raise ValueError("feedback: empty text")
    target = _resolve_target(scope, session_id, target_session_id)
    return dao.add_lesson(
        conn,
        text=sanitize(text),
        kind=FEEDBACK,
        session_id=target,
        feature="",
        ground_truth=False,
        status="active",
    )




@dataclass
class Item:
    """Lesson candidate used during consolidation."""
    text: str
    ts_days: Optional[float] = None
    ground_truth: bool = False


@dataclass
class Plan:
    keep: list = field(default_factory=list)   # Items retained in the final set.
    drop: list = field(default_factory=list)   # Items discarded from the final set.
    reason: dict = field(default_factory=dict)


def _norm(text: str) -> str:
    """Normalize lesson text for near-duplicate detection."""
    t = text.strip()
    t = re.sub(r"^- ", "", t)                       # Strip a leading bullet marker.
    t = re.sub(r"\[[^\]]*\]", " ", t, count=1)      # Strip the first feature or class marker.
    t = re.sub(r"[^0-9a-z\u0400-\u04ff]+", " ", t.lower())
    return " ".join(t.split())


def _near_key(text: str) -> str:
    return _norm(text)


def _age_key(ts_days):
    """Convert an optional timestamp to a sortable age key."""
    return float("-inf") if ts_days is None else ts_days


def _pick_near_winner(a: Item, b: Item):
    """Choose the stronger item from a near-duplicate pair."""
    if a.ground_truth != b.ground_truth:
        return (a, b) if a.ground_truth else (b, a)
    aa, bb = _age_key(a.ts_days), _age_key(b.ts_days)
    if aa != bb:
        return (a, b) if aa >= bb else (b, a)   # The fresher item wins.
    return (a, b)


def _as_item(x) -> Item:
    if isinstance(x, Item):
        return x
    return Item(text=x["text"], ts_days=x.get("ts_days"), ground_truth=bool(x.get("ground_truth", False)))


def plan_consolidation(items, *, max_items, max_age_days=None, now_ts=None) -> Plan:
    """Plan age, duplicate, and capacity-based lesson retirement."""
    src = [_as_item(x) for x in items]
    plan = Plan()
    now = now_ts

    after_age = []
    for it in src:
        too_old = (max_age_days is not None and now is not None
                   and it.ts_days is not None and (now - it.ts_days) > max_age_days)
        if too_old and not it.ground_truth:   # Ground truth does not decay with age [D-29].
            plan.drop.append(it)
            plan.reason[it.text] = "age"
        else:
            after_age.append(it)

    winners: dict = {}
    order: list = []          # Preserve deterministic first-seen key order.
    for it in after_age:
        k = _near_key(it.text)
        if not k:
            k = f"\0{len(order)}"
        if k not in winners:
            winners[k] = it
            order.append(k)
            continue
        cur = winners[k]
        win, lose = _pick_near_winner(cur, it)
        winners[k] = win
        plan.drop.append(lose)
        plan.reason[lose.text] = "near-dup"
    survived = [winners[k] for k in order]

    if len(survived) > max_items:
        ranked = sorted(
            enumerate(survived),
            key=lambda p: (p[1].ground_truth, _age_key(p[1].ts_days), p[0]),
        )
        n_drop = len(survived) - max_items
        drop_ids = {id(survived[i]) for i, _ in ranked[:n_drop]}
        kept = []
        for it in survived:
            if id(it) in drop_ids:
                plan.drop.append(it)
                plan.reason[it.text] = "cap"
            else:
                kept.append(it)
        survived = kept

    plan.keep = survived
    return plan


@dataclass
class ConsolidateResult:
    kept: int
    dropped: int
    reason: dict = field(default_factory=dict)    # Text mapped to age, near-duplicate, or cap.
    retired_ids: list = field(default_factory=list)  # Lesson IDs moved to retired state.


def _iso_to_days(iso) -> Optional[float]:
    """Convert an ISO timestamp to fractional UTC days."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() / 86400.0


def now_days() -> float:
    """Return the current UTC timestamp in fractional days."""
    return datetime.now(timezone.utc).timestamp() / 86400.0


def consolidate(
    conn,
    *,
    max_items: int = 200,
    max_age_days=None,
    now_ts=None,
    session_id: Optional[int] = None,
) -> ConsolidateResult:
    """Retire lessons selected by the consolidation plan."""
    lessons = [l for l in dao.list_lessons(conn, status="active", session_id=session_id)
               if l["kind"] != FEEDBACK]
    items: list = []
    id_to_lesson: dict = {}
    for l in lessons:
        it = Item(text=l["text"], ts_days=_iso_to_days(l["created_ts"]),
                  ground_truth=bool(l["ground_truth"]))
        items.append(it)
        id_to_lesson[id(it)] = l["lesson_id"]   # Map planned items back to stable lesson IDs.
    plan = plan_consolidation(items, max_items=max_items, max_age_days=max_age_days, now_ts=now_ts)
    retired: List[int] = []
    for it in plan.drop:
        lid = id_to_lesson[id(it)]
        dao.set_lesson_status(conn, lid, "retired")
        retired.append(lid)
    return ConsolidateResult(kept=len(plan.keep), dropped=len(plan.drop),
                             reason=dict(plan.reason), retired_ids=retired)
