"""Normalized harness results and honest output parsing.

Adapters delegate parsing here so completion semantics cannot drift [G2/G3]. A
successful result requires a clean process exit, parseable output, and no error
events. Timeouts fail even if partial output claims completion. Agent-reported
``status`` is only a flow input; the last embedded status object wins [B2].
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from .jsonscan import last_object_with_key


@dataclass
class Result:
    """Normalized result returned by every harness.

    ``ok`` reflects process and parser evidence, not an agent's self-report [G3].
    ``schema_applied`` is ``None`` when no schema was requested, ``True`` when it
    was enforced, and ``False`` when a constrained call must abstain [B1]. The raw
    harness transcript is retained for independent auditing [D11].
    """
    ok: bool
    text: str
    exit_code: int
    cost_usd: Optional[float] = None
    session_id: Optional[str] = None
    raw: Optional[dict] = None
    error: Optional[str] = None
    schema_applied: Optional[bool] = None
    timed_out: bool = False
    status: Optional[dict] = None
    transcript: str = ""


# Compatibility alias: the engine deliberately has one normalized result type.
InvokeResult = Result


def parse_status(text: Optional[str]) -> Optional[dict]:
    """Return the last self-reported status object, or ``None`` [B2]."""
    return last_object_with_key(text or "", "status")


def parse_claude_json(stdout: str, exit_code: int) -> Result:
    """Parse Claude's single JSON result without raising on malformed output [G2/G3]."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError, TypeError):
        return Result(ok=False, text=stdout or "", exit_code=exit_code,
                      error="non-json output from claude")
    if not isinstance(data, dict):
        return Result(ok=False, text=str(data), exit_code=exit_code, error="unexpected json shape")

    # ``is_error`` carries refusal, budget, and API failures even when the process exits 0.
    is_error = bool(data.get("is_error"))
    res_val = data.get("result")
    text = "" if res_val is None else str(res_val)  # JSON null means no text, not the string "None".
    error = None
    if is_error or exit_code != 0:
        error = str(
            data.get("subtype")
            or data.get("api_error_status")
            or data.get("error")
            or "claude error"
        )
    return Result(
        ok=(exit_code == 0 and not is_error),  # Terminal evidence, not self-report [G3].
        text=text,
        exit_code=exit_code,
        cost_usd=data.get("total_cost_usd"),
        session_id=data.get("session_id"),
        raw=data,
        error=error,
        status=parse_status(text),  # Flow input only; does not determine ``ok`` [B2].
    )


def parse_claude_stream(stdout: str, exit_code: int) -> Result:
    """Parse Claude stream JSON, retaining the full transcript [D11].

    The terminal ``result`` event uses the same fields as single-JSON output. Legacy
    single-object output remains supported; a truncated stream fails gracefully.
    """
    result_event = None
    parsed_any = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict):
            parsed_any = True
            if ev.get("type") == "result":
                result_event = ev  # Prefer the last terminal event defensively.
    if result_event is not None:
        res = parse_claude_json(json.dumps(result_event, ensure_ascii=False), exit_code)
        res.transcript = stdout
        return res
    # Accept legacy single-object output, including pretty-printed JSON. Do not
    # mistake one nonterminal stream event for a complete legacy response.
    try:
        whole = json.loads(stdout.strip()) if stdout.strip() else None
    except (json.JSONDecodeError, ValueError):
        whole = None
    if isinstance(whole, dict) and whole.get("type") in (None, "result"):
        return parse_claude_json(json.dumps(whole, ensure_ascii=False), exit_code)
    if not parsed_any and whole is None:
        # Preserve the same graceful failure used by the single-JSON parser.
        return Result(ok=False, text=stdout or "", exit_code=exit_code,
                      error="non-json output from claude")
    return Result(ok=False, text="", exit_code=exit_code,
                  error="stream ended without a terminal result event (truncated or killed)",
                  transcript=stdout)


def parse_codex_jsonl(stdout: str, exit_code: int) -> Result:
    """Parse Codex JSONL events into one normalized result [G2/G3]."""
    text_parts: list = []
    usage = None
    err = None
    session_id = None
    parsed_any = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        parsed_any = True
        t = ev.get("type")
        if t == "thread.started":
            session_id = ev.get("thread_id")  # Native session ID for resume.
            continue
        item = ev.get("item") if isinstance(ev.get("item"), dict) else ev
        itype = item.get("item_type") or item.get("type")
        if t == "item.completed" and itype == "agent_message":
            txt = item.get("text")
            if txt is None:
                txt = item.get("content")
            if isinstance(txt, list):
                # Join text blocks instead of exposing a Python list representation.
                txt = "".join(b.get("text", "") for b in txt if isinstance(b, dict))
            if txt:
                text_parts.append(str(txt))
        elif t == "turn.completed":
            usage = ev.get("usage")
        elif t == "error" or ev.get("is_error"):
            err = str(ev.get("message") or ev.get("error") or "codex error")
    if not parsed_any:
        return Result(ok=False, text=stdout or "", exit_code=exit_code,
                      error="non-jsonl output from codex")
    ok = (exit_code == 0 and err is None)  # Clean exit with no error events [G3].
    text = "\n".join(text_parts)
    return Result(
        ok=ok,
        text=text,
        exit_code=exit_code,
        cost_usd=None,  # Codex reports tokens rather than a dollar cost [G4].
        session_id=session_id,
        raw={"usage": usage},
        error=err if not ok else None,
        status=parse_status(text),  # Flow input only; does not determine ``ok`` [B2].
        transcript=stdout,  # The JSONL event stream is the native transcript [D11].
    )


def apply_timeout(result: Result, *, timed_out: bool, exit_code: Optional[int] = None) -> Result:
    """Apply a wall timeout in place and return the same result [G3]."""
    if not timed_out:
        return result
    result.ok = False
    result.timed_out = True
    if exit_code is not None:
        result.exit_code = exit_code
    result.error = result.error or (
        f"wall-timeout: harness hung without a terminal state (exit {result.exit_code})"
    )
    return result
