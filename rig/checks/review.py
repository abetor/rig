"""Parse and aggregate deterministic review-panel verdicts [B1, B5, B6].

Judges run elsewhere with an isolated context and a constrained ``{ok, why}``
schema. This module refuses unconstrained or malformed verdicts instead of
extracting forgeable free text, and excludes abstentions from actionable fixes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from ..core.jsonscan import last_object_with_key

# Judges must return this constrained shape. Flows may override the schema file.
VERDICT_SCHEMA = (
    '{"type":"object","properties":'
    '{"ok":{"type":"boolean"},"why":{"type":"string"}},'
    '"required":["ok","why"],"additionalProperties":false}'
)

# [neutrality §9.1] These defaults are code-oriented. Dimensions should
# eventually move into flow configuration instead of applying to every flow.
DEFAULT_DIMENSIONS = ("correctness", "security")


@dataclass
class Verdict:
    """Verdict for one dimension, with abstentions marked as non-actionable."""
    dimension: str
    ok: bool
    why: str = ""
    abstained: bool = False


@dataclass
class PanelResult:
    """Combined panel result and actionable fix items [F4]."""
    ok: bool
    verdicts: list = field(default_factory=list)
    fix_items: list = field(default_factory=list)
    detail: str = ""


def parse_verdict(result, *, dimension: str = "") -> Verdict:
    """Parse a schema-constrained judge result, failing closed to abstention [B1]."""
    if getattr(result, "schema_applied", None) is not True:
        return Verdict(dimension, ok=False, abstained=True,
                       why="verdict was not schema-constrained; abstaining (anti-forgery [B1])")
    if result is None or not getattr(result, "ok", False):
        # A judge execution failure is a harness problem, not an actionable code defect.
        why = getattr(result, "error", None) or "judge did not complete"
        return Verdict(dimension, ok=False, abstained=True, why=f"abstain: {str(why)[:200]}")
    obj = last_object_with_key(getattr(result, "text", "") or "", "ok")
    if obj is None:
        return Verdict(dimension, ok=False, abstained=True,
                       why=f"judge returned no verdict: {(getattr(result, 'text', '') or '')[:300]}")
    return Verdict(dimension, ok=bool(obj.get("ok")), why=str(obj.get("why", ""))[:800])


def combine_votes(dimension: str, verdicts) -> Verdict:
    """Combine votes for one dimension using a strict majority [B1].

    Abstentions count against approval. Real rejections contribute actionable
    detail; an all-abstention failure remains non-actionable [B5].
    """
    vs: List[Verdict] = list(verdicts)
    if len(vs) == 1:
        return vs[0]
    n = len(vs)
    ok_n = sum(1 for v in vs if v.ok)
    if ok_n * 2 > n:
        return Verdict(dimension, ok=True, why=f"majority ok ({ok_n}/{n})")
    rejects = []
    for v in vs:                                       # Stable vote order preserves determinism [B6].
        if not v.ok and not v.abstained and v.why and v.why not in rejects:
            rejects.append(v.why)
    if not rejects:
        return Verdict(dimension, ok=False, abstained=True,
                       why=f"abstain: {n - ok_n}/{n} votes had no verdict")
    return Verdict(dimension, ok=False, why="; ".join(rejects)[:800])


def aggregate(verdicts) -> PanelResult:
    """Aggregate dimensions; every nonempty verdict must pass."""
    vs: List[Verdict] = list(verdicts)
    ok = bool(vs) and all(v.ok for v in vs)
    fix_items = [f"[{v.dimension}] {v.why}" for v in vs if not v.ok and not v.abstained]  # Stable order [B6].
    detail = "; ".join(
        f"{v.dimension}:{'ok' if v.ok else ('ABSTAIN' if v.abstained else 'REJECT')}" for v in vs
    ) or "no review dimensions"
    return PanelResult(ok=ok, verdicts=vs, fix_items=fix_items, detail=detail)
