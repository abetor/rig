"""Data types and contracts for one flow phase."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import List, Optional

from ..core.types import VALID_PERMS

WAIT = ("sync", "parallel", "background")
PERMS = VALID_PERMS
ON_FAIL = ("stop", "continue", "retry")
KIND = ("writer", "readonly", "validator")


@dataclass
class Phase:
    name: str
    agent: str = ""
    prompt: str = ""                   # Content path or inline template.
    wait: str = "sync"
    perms: str = "default"             # Abstract permission mode; ``auto`` is not the default [A7].
    isolated: bool = False
    schema: Optional[str] = None
    about: str = ""                    # Phase whose artifact a converse response revises.
    family: str = ""                   # Preferred judge family for cross-family review [G6/B4].
    kind: str = "writer"               # Writers are serial; other kinds may run in parallel [F1].
    on_fail: str = "stop"              # [F4] stop | continue | retry
    retry_cap: int = 0                 # Maximum additional attempts when retrying [F4].
    advances_base: bool = False        # A green result advances the integration base [F2].
    action: str = ""                   # Human continuation action: reply or approve.
    dimensions: Optional[List[str]] = None  # Review dimensions for units and review phases.
    judge_votes: int = 1               # Judges per dimension for self-consistency.
    model: str = ""                    # Optional model override for this phase.
    effort: str = ""                   # Optional reasoning-effort override.

    def __post_init__(self):
        if not (self.name or "").strip():
            raise ValueError("Phase: empty name cannot be addressed")
        if self.wait not in WAIT:
            raise ValueError(f"Phase {self.name!r}: wait={self.wait!r} outside {WAIT}")
        if self.perms not in PERMS:
            raise ValueError(f"Phase {self.name!r}: perms={self.perms!r} outside {PERMS}")
        if self.kind not in KIND:
            raise ValueError(f"Phase {self.name!r}: kind={self.kind!r} outside {KIND}")
        if self.on_fail not in ON_FAIL:
            raise ValueError(f"Phase {self.name!r}: on_fail={self.on_fail!r} outside {ON_FAIL}")
        for b in ("isolated", "advances_base"):
            if not isinstance(getattr(self, b), bool):
                raise ValueError(f"Phase {self.name!r}: {b} must to be bool (JSON true/false), "
                                 f"to know {type(getattr(self, b)).__name__}")
        self.retry_cap = max(0, int(self.retry_cap))
        if self.retry_cap and self.on_fail != "retry":
            raise ValueError(f"Phase {self.name!r}: retry_cap={self.retry_cap} requires on_fail='retry'")
        if self.action not in ("", "reply", "approve"):
            raise ValueError(f"Phase {self.name!r}: action={self.action!r} outside ('', 'reply', 'approve')")
        if self.dimensions is not None and (
                not isinstance(self.dimensions, list) or not self.dimensions
                or not all(isinstance(x, str) and x.strip() for x in self.dimensions)):
            raise ValueError(f"Phase {self.name!r}: dimensions must be a nonempty list of strings "
                             f"(or omit the key to use engine defaults)")
        self.judge_votes = int(self.judge_votes)
        if not (1 <= self.judge_votes <= 5):
            raise ValueError(f"Phase {self.name!r}: judge_votes={self.judge_votes} outside 1..5 "
                             f"(anti cost-blowup [F7])")
        for s in ("model", "effort"):
            if not isinstance(getattr(self, s), str):
                raise ValueError(f"Phase {self.name!r}: {s} must be a string")


_FIELDS = {f.name for f in fields(Phase)}


def phase_from_dict(d: dict) -> Phase:
    """Validate and construct a phase from decoded configuration."""
    if not isinstance(d, dict):
        raise ValueError(f"phase_from_dict: expectation dict, to know {type(d).__name__}")
    unknown = set(d) - _FIELDS
    if unknown:
        raise ValueError(f"Phase: unknown key {sorted(unknown)} (exists {sorted(_FIELDS)})")
    return Phase(**d)


def load_phases(flow) -> List[Phase]:
    """Load phases."""
    if isinstance(flow, list):
        raw = flow                                    # Bare phase list from --flow-json.
    elif isinstance(flow, dict):
        raw = flow.get("phases", [])
    elif flow is None:
        raw = []
    else:
        raise ValueError(f"load_phases: expectation dict {{'phases':[...]}} or list phase, to know {type(flow).__name__}")
    if not isinstance(raw, list):
        raise ValueError(f"flow.phases: expected list, got {type(raw).__name__}")
    return [phase_from_dict(p) for p in raw]
