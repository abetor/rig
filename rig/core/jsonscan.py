"""Find balanced JSON objects embedded in free-form text.

A greedy regular expression fails on nesting and quoted braces. This scanner tracks
strings and escapes while extracting top-level ``{...}`` spans. Consumers choose
their own selection policy; status and verdict parsing use the last matching object
to resist an earlier injected decoy [B2].
"""
from __future__ import annotations

import json
from typing import Optional


def balanced_objects(text: str) -> list:
    """Return all balanced top-level ``{...}`` spans in source order."""
    if not text:
        return []
    spans, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                spans.append(text[start:i + 1])
    return spans


def objects_with_key(text: str, key: str) -> list:
    """Return parsed objects containing ``key`` in source order."""
    out = []
    for span in balanced_objects(text):
        try:
            v = json.loads(span)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(v, dict) and key in v:
            out.append(v)
    return out


def last_object_with_key(text: str, key: str) -> Optional[dict]:
    """Return the last parsed object containing ``key``, or ``None`` [B2]."""
    objs = objects_with_key(text, key)
    return objs[-1] if objs else None
