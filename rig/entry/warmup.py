"""Initialize project storage and optionally scan a new project.

The first ``rig "task"`` call creates ``.rig/memory.db`` and may run an injected
system scan to seed baseline memory. Existing projects skip the scan. Keeping the
scanner injectable makes freshness detection and orchestration deterministic in tests.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable, Optional

from ..db import convention, dao

# The content prompt and scout choose profile fields; the neutral engine only stores
# the resulting object under a stable metadata key [§10.2, §9.1].
PROFILE_META_KEY = "project_profile"
# Record the scanned HEAD so callers can detect a stale baseline later.
PROFILE_HEAD_META_KEY = "project_profile_head"


@dataclass
class WarmupResult:
    conn: object                  # Open project database connection.
    created: bool                 # Whether this call created the database.
    scanned: bool = False         # Whether a supplied scanner ran for a new database.


def is_fresh(project: str) -> bool:
    """Return whether the conventional project database is absent [O4]."""
    return not os.path.exists(convention.db_path(project))


def warmup(project: str, *, scan_fn: Optional[Callable] = None) -> WarmupResult:
    """Ensure project storage and run ``scan_fn`` once for a new database."""
    fresh = is_fresh(project)
    conn = dao.open_project(project)              # Create storage and apply migrations [E5].
    # Memory decay belongs to ``start_session``, shared by CLI and lead paths [D4].
    scanned = False
    if fresh and scan_fn is not None:
        scan_fn(conn, project)                    # Seed the baseline through the system scout.
        scanned = True
    return WarmupResult(conn=conn, created=fresh, scanned=scanned)


def parse_profile(scout_text: str) -> dict:
    """Extract the last structured profile from scout output.

    Fall back to a bounded prose summary when no JSON object exists, and return an
    empty mapping for empty output.
    """
    from ..core.jsonscan import balanced_objects
    for raw in reversed(balanced_objects(scout_text or "")):    # The latest valid object is authoritative.
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj:
            return obj
    text = (scout_text or "").strip()
    return {"summary": text[:2000]} if text else {}


def persist_profile(conn, scout_text: str) -> dict:
    """Persist a nonempty project profile and return the parsed value [§10.2]."""
    prof = parse_profile(scout_text)
    if prof:
        dao.set_meta(conn, PROFILE_META_KEY, json.dumps(prof, ensure_ascii=False))
    return prof
