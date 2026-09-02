"""Agent-facing wrappers around durable memory operations."""
from __future__ import annotations

from typing import Optional

from ..memory import recall, store


class MemoryTool:
    """High-level interface for writing and recalling durable lessons."""

    def __init__(self, conn, *, scope: str, session_id: Optional[int] = None):
        if scope not in store.SCOPES:
            raise ValueError(f"unknown scope: {scope!r} (expected '{store.LEAF}' or '{store.ORCHESTRATOR}')")
        self.conn = conn
        self.scope = scope
        self.session_id = session_id

    def remember(self, text: str, *, kind: str = store.FREE,
                 feature: str = "", target_session_id: Optional[int] = None) -> int:
        """Store a sanitized durable lesson."""
        return store.write_lesson(
            self.conn, text=text, scope=self.scope, session_id=self.session_id, kind=kind,
            target_session_id=target_session_id, ground_truth=False, feature=feature,
        )

    def feedback(self, text: str, *, target_session_id: Optional[int] = None) -> int:
        """Store feedback as an orchestrator-scoped lesson."""
        return store.write_feedback(
            self.conn, text=text, session_id=self.session_id, scope=self.scope,
            target_session_id=target_session_id,
        )

    def recall(self, query: str, *, k: int = 5, feature: Optional[str] = None) -> recall.Recalled:
        """Recall lessons relevant to a query."""
        return recall.load(self.conn, query, k=k, feature=feature)
