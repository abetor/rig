"""Resolve named and custom flow definitions from content files."""
from __future__ import annotations

import json
from typing import List, Optional

from ..util import content
from .phase import Phase, load_phases

DEFAULT_FLOW = "base"


def resolve_flow(name: Optional[str] = None, *, base=None) -> dict:
    """Resolve flow."""
    flow_name = (name or DEFAULT_FLOW).strip() or DEFAULT_FLOW
    _MISSING = "\x00__missing__"
    raw = content.load(f"{flow_name}/flow.json", default=_MISSING, base=base)
    if raw == _MISSING:
        if flow_name == DEFAULT_FLOW:
            return {"phases": []}
        raise FileNotFoundError(f"resolve_flow: flow {flow_name!r} was not found (flows/{flow_name}/flow.json)")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"resolve_flow: flow {flow_name!r} has invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"resolve_flow: flow {flow_name!r} has root type {type(data).__name__}, expected an object")
    return data


def resolve_phases(name: Optional[str] = None, *, base=None) -> List[Phase]:
    """Resolve phases."""
    return load_phases(resolve_flow(name, base=base))


CHECKPOINT_AGENT = "checkpoint"
CONVERSE_AGENT = "converse"
GATE_AGENTS = (CHECKPOINT_AGENT, CONVERSE_AGENT)


def split_at_gate(phases: List[Phase]):
    """Split phases before and after the first human gate."""
    for i, ph in enumerate(phases):
        if ph.agent in GATE_AGENTS:
            return phases[:i], phases[i], phases[i + 1:]
    return phases, None, []


def split_at_checkpoint(phases: List[Phase]):
    """Split phases before, at, and after the first checkpoint."""
    pre, _, post = split_at_gate(phases)
    return pre, post
