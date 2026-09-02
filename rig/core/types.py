"""Engine data types: Unit / Plan / StepResult, plus the single green predicate and the id boundary.

Pure data classes for the primitives (no behaviour, no IO) - this is the data CORE, not a model
choice. Two boundaries live HERE as the SINGLE SOURCE, because copies spread across modules would
drift apart silently.
  [A1] VALID_UNIT_ID - the anti-traversal anchor for ids; every point where an id becomes a path,
                       a ref or an `rm` target validates against it.
  [F3] is_green      - the one green predicate; both base advancement and state writing call it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# [abstractions-1] SINGLE SOURCE for constants that used to be copied into two or three modules and
# would have drifted apart silently (same principle as VALID_UNIT_ID/is_green above):
#   ENGINE_DIR  - the engine directory inside a project or worktree (.rig; db.convention and the
#                 adapters alias it);
#   VALID_PERMS - abstract per-call permission modes (each CLI socket maps them to its own flags;
#                 "auto" is NOT the default [A7]). Copying the values is FORBIDDEN - import them
#                 from here (guard: test_watershed).
ENGINE_DIR = ".rig"
VALID_PERMS = ("read", "plan", "auto", "default")

# [A1] unit.id flows into the workspace PATH, into the name of the output REF and into `rm -rf`, so
# `../..` would wipe files OUTSIDE the workspace (confirmed against a live FS). Starts with
# alnum/`_`, then alnum/dot/dash/`_`. Rejects `.`, `..`, path separators, spaces, empty strings and a
# leading dash. SINGLE SOURCE (do not duplicate): ids are born in DIFFERENT places - when units of
# work are handed out, and in the output of the planner LLM, which may well emit `../../x` - so
# everyone validates. Drift between copies is a hole.
# The anchor is `\Z`, NOT `$`: `$` matches BEFORE a trailing `\n`, so `"u1\n"` would pass (LLMs do
# emit that) and the `\n` would leak into a path or a ref name. `\Z` is the true end of string. This
# is NECESSARY but NOT sufficient for the full ref-name format (`a..`, `a.lock`, `a.` slip through -
# the layer below rejects the BRANCH, which is a contained late failure, NOT traversal: no separator
# is involved). Anti-traversal is closed here; full name-format validation is the lower layer's job
# and is not reproduced here.
VALID_UNIT_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\Z")


@dataclass
class Unit:
    """A unit of work handed to an executor. instruction = an EXACT instruction (what and where),
    not "figure it out and do it" - understanding is the upper phase's job, not the executor's."""
    id: str
    instruction: str
    kind: str = "writer"  # writer | readonly | validator ([F1]: writers run SERIAL, read-only in parallel)


# JSON contract for the plan, used by the constrained-output RETRY of the plan phase (from live
# acceptance, §10: the default model regularly returns PROSE instead of JSON, and two prose attempts
# leave the flow dead on a perfectly valid task). It mirrors checks/validate_plan: it may not be
# softer than the validator; stricter on `required` is fine, because this constrains GENERATION
# rather than rejecting a finished artifact. It is not attached to the FIRST attempt: that one
# carries the planner's own need_human signal ({"status":...}), which the plan schema would kill.
PLAN_SCHEMA = (
    '{"type":"object","properties":{'
    '"goal":{"type":"string","minLength":1},'
    '"units":{"type":"array","minItems":1,"items":{"type":"object","properties":{'
    '"id":{"type":"string","pattern":"^[A-Za-z0-9_][A-Za-z0-9._-]*$"},'
    '"instruction":{"type":"string","minLength":1},'
    '"kind":{"type":"string","enum":["writer","readonly","validator"]}},'
    '"required":["id","instruction"]}},'
    '"gate_cmd":{"type":"array","items":{"type":"string"}}},'
    '"required":["goal","units","gate_cmd"]}'
)


@dataclass
class Plan:
    """The plan artifact (what the orchestrator authors), serializable to the substrate or a file.
    gate_cmd = the command of the deterministic OUTCOME gate (ground truth is the exit code, not an
    agent's narrative).
    context = a prose summary from the preceding (scouting) phase: it travels TOGETHER with the plan
    so the context reaches the executor's brief across the gap between planning and launch
    (otherwise it would be lost at the seam).
    Optional (default ""): a plan without a prior summary is valid; the field carries no plan logic."""
    goal: str
    units: list  # list[Unit]
    gate_cmd: list  # argument vector of the gate command, e.g. ["./run-gate", "-q"]
    context: str = ""  # summary of the preceding phase, for the brief; carries no plan logic


def is_green(d, v, g) -> bool:
    """[F3] The SINGLE green predicate (the gate is GROUND TRUTH). A fully green unit is:
    executor ok, check ok, OUTCOME gate ok. SINGLE SOURCE: both base advancement (the decision to
    move the base, the overall ok) and the writing of handoff/in-flight state call it - two copies
    would drift apart SILENTLY (one saying "done, merge it" about something the other did not
    consider green).
    v/g == None (the check or gate was never called, or failed before producing a result) means NOT
    green (fail-closed)."""
    return bool(d.ok and v is not None and v.ok and g is not None and g.ok)


def plan_to_dict(plan: "Plan") -> dict:
    """Plan -> a JSON-serializable dict. Round-trips with the plan validator: the same keys
    goal / units[id,instruction,kind] / gate_cmd / context that the validator reads back.
    context is OMITTED when empty (a plan without a prior summary is the same artifact, minus a
    noisy "")."""
    d = {
        "goal": plan.goal,
        "units": [{"id": u.id, "instruction": u.instruction, "kind": u.kind} for u in plan.units],
        "gate_cmd": list(plan.gate_cmd),
    }
    if plan.context:
        d["context"] = plan.context
    return d


@dataclass
class StepResult:
    """The structured result of a primitive step - a single contract signal: a step does NOT throw
    outwards, it returns this. The add-on fields are optional: each step fills in the ones that
    apply to it."""
    ok: bool
    detail: str = ""
    branch: Optional[str] = None
    artifact: Optional[str] = None  # diff / ref / output - inspectable material
    cost_usd: Optional[float] = None
    cost_origin: Optional[str] = None  # [G4] 'exact'|'no_pricing' ('estimated' dropped along with harness/cost)
    worktree: Optional[str] = None  # path to the unit's isolated workspace (where edits land, where the gate runs)
    verdict: Optional[str] = None  # [C1] gate: PASS|FAIL|TIMEOUT|INFRA|NO-GATE (ONLY FAIL is ground truth)
    abstained: bool = False  # the check COULD NOT judge (no schema / no verdict) != a real reject:
    # [B1] fail-closed (ok=False), but [B5] NOT actionable -> the retry loop does not spin on it.
