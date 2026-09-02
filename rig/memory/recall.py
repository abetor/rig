"""Select relevant durable lessons for a task."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..db import dao
from .store import FEATURE, FEEDBACK, FREE, PRINCIPLE, TECHDEBT

ALWAYS_KINDS = (PRINCIPLE, FEEDBACK)
TASK_KINDS = (FEATURE, TECHDEBT, FREE)

MANIFEST_CAP = 200
ALWAYS_CAP = 50     # Bound always-loaded context; ground-truth principles take priority.
_MIN_TERM_LEN = 2   # Ignore short query terms unless every term is short.


@dataclass
class Manifest:
    """Bounded index of active project memory."""
    total_active: int = 0
    pending: int = 0                              # Human-labelled items awaiting review [D1].
    kinds: dict = field(default_factory=dict)     # Active-item counts by kind.
    features: dict = field(default_factory=dict)  # Active-item counts by feature.
    names: list = field(default_factory=list)     # Sorted manifest names and lesson features.
    lines: list = field(default_factory=list)     # Human-readable bounded index.


def build_manifest(conn, *, cap: int = MANIFEST_CAP) -> Manifest:
    """Build a bounded manifest from active and pending lessons."""
    active = dao.list_lessons(conn, status="active")
    kinds = dict(sorted(Counter(l["kind"] for l in active).items()))
    features = dict(sorted(Counter(l["feature"] for l in active if l["feature"]).items()))
    pending = len(dao.list_lessons(conn, status="pending"))
    names = sorted(_known_set(conn))
    summaries = {m["name"]: m["summary"] for m in dao.list_manifest(conn) if m.get("summary")}

    lines: List[str] = []
    if active or pending:
        lines.append(f"- active resolve: {len(active)}")
        if pending:
            lines.append(f"- awaiting labels (pending): {pending} (!)")  # Requires human attention [D1].
        for kind, n in kinds.items():
            lines.append(f"- type {kind}: {n}")
        for feat, n in features.items():
            s = summaries.get(feat)
            lines.append(f"- feature [{feat}]: {n}" + (f" — {s}" if s else ""))

    return Manifest(
        total_active=len(active),
        pending=pending,
        kinds=kinds,
        features=features,
        names=names,
        lines=lines[: max(0, cap)],
    )


def _known_set(conn) -> set:
    """Return known manifest names and active lesson features."""
    names = {m["name"] for m in dao.list_manifest(conn)}
    names |= {l["feature"] for l in dao.list_lessons(conn, status="active") if l["feature"]}
    return names


def validate_names(conn, names) -> list:
    """Validate names."""
    ks = _known_set(conn)
    out, seen = [], set()
    for n in names or []:
        if n in ks and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _terms(query: str) -> list:
    """Tokenize a query while suppressing unhelpful short terms."""
    raw = (query or "").lower().split()
    return [t for t in raw if len(t) > _MIN_TERM_LEN] or raw


def recall(
    conn,
    query: str,
    *,
    k: int = 5,
    session_id: Optional[int] = None,
    feature: Optional[str] = None,
    status: Optional[str] = "active",
    kinds=None,
) -> list:
    """Return the highest-scoring lessons for a query."""
    if k <= 0:
        return []
    terms = _terms(query)
    if not terms:
        return []

    rows = dao.list_lessons(conn, status=status, session_id=session_id)
    hits, seen = [], set()
    for l in rows:
        if feature is not None and l["feature"] != feature:
            continue
        if kinds is not None and l["kind"] not in kinds:
            continue
        text = l["text"]
        if text in seen:                       # Do not spend the context budget twice on identical text.
            continue
        low = text.lower()
        score = sum(1 for t in terms if t in low)
        if score:
            seen.add(text)
            hits.append({
                "lesson_id": l["lesson_id"], "text": text, "kind": l["kind"],
                "feature": l["feature"], "score": score,
            })
    hits.sort(key=lambda h: (-h["score"], -h["lesson_id"]))   # Descending score, then freshness.
    return hits[:k]


def always_load(conn, *, session_id: Optional[int] = None, cap: int = ALWAYS_CAP) -> list:
    """Return bounded principles and feedback loaded for every task."""
    rows = [l for l in dao.list_lessons(conn, status="active", session_id=session_id)
            if l["kind"] in ALWAYS_KINDS]
    rows.sort(key=lambda l: (0 if l["ground_truth"] else 1, -l["lesson_id"]))  # Ground truth first, then newest.
    return rows[: max(0, cap)]


@dataclass
class Recalled:
    """Always-loaded and query-selected memory."""
    always: list = field(default_factory=list)
    selected: list = field(default_factory=list)


def load(conn, query: str, *, k: int = 5, session_id: Optional[int] = None,
         feature: Optional[str] = None) -> Recalled:
    """Load durable memory relevant to a query."""
    return Recalled(
        always=always_load(conn, session_id=session_id),
        selected=recall(conn, query, k=k, session_id=session_id, feature=feature, kinds=TASK_KINDS),
    )


def model_recall(
    conn,
    agent_fn: Callable[[str, "Manifest"], list],
    query: str,
    *,
    k: int = 5,
    manifest: Optional["Manifest"] = None,
) -> list:
    """Render recalled memory into a model context block."""
    mf = manifest if manifest is not None else build_manifest(conn)
    proposed = agent_fn(query, mf)
    return validate_names(conn, proposed)[: max(0, k)]
