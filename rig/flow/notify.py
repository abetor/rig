"""Build and deliver flow notifications through configured channels."""
from __future__ import annotations

import os
import re
import shlex

from .runner import PhaseResult


def build_model(*, session_id=None, repo: str = "", flow: str = "", kind: str = "info",
                title: str = "", body: str = "", action: str = "") -> dict:
    """Build a normalized notification envelope from flow context."""
    repo = (repo or "").rstrip("/")
    return {"session_id": session_id, "project": os.path.basename(repo) if repo else "",
            "flow": flow or "", "kind": kind, "title": title, "body": body,
            "action": action or "", "repo": repo}


def model_from_ctx(ctx: dict, *, kind: str, title: str, body: str = "", action: str = "") -> dict:
    return build_model(session_id=ctx.get("session_id"), repo=str(ctx.get("repo") or ""),
                       flow=str(ctx.get("flow") or ""), kind=kind, title=title, body=body,
                       action=action)


def render_text(msg: dict) -> str:
    """Render text."""
    sid = msg.get("session_id")
    addr = [f"rig #{sid}" if sid is not None else "rig"]
    if msg.get("project"):
        addr.append(str(msg["project"]))
    if msg.get("flow"):
        addr.append(str(msg["flow"]))
    mark = "IS REQUIRED RESPONSE" if msg.get("kind") == "action" else "INFORMATION"
    lines = [" · ".join(addr), f"{mark}: {(msg.get('title') or '').strip()}".rstrip(" :")]
    body = (msg.get("body") or "").strip("\n").rstrip()
    if body:
        lines += ["", body]
    action, repo = msg.get("action") or "", msg.get("repo") or ""
    if action and repo and sid is not None:
        q = shlex.quote(repo)
        if action == "reply":
            lines += ["", "to answer: replaem on this communication — «ok» acting, any text = changes",
                      f'or: rig --path {q} --reply {sid} "ok | yours changes"']
        elif action == "approve":
            lines += ["", f"to confirm: rig --path {q} --approve {sid}"]
    return "\n".join(lines)


def cut(text: str, cap: int) -> str:
    """Truncate text to the notification limit."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap].rsplit(" ", 1)[0] + "…"


def _render(template: str, ctx: dict) -> str:
    """Render the value."""
    out = template
    for key, val in ctx.items():
        if isinstance(val, (str, int, float, bool)):
            out = out.replace("{{" + key + "}}", str(val))
    repo = str(ctx.get("repo") or "").rstrip("/")
    if repo:
        out = out.replace("{{project}}", os.path.basename(repo))
    if "{{plan}}" in out and ctx.get("plan") is not None:
        out = out.replace("{{plan}}", _plan_brief(ctx["plan"]))
    if "{{context_brief}}" in out and str(ctx.get("context") or "").strip():
        out = out.replace("{{context_brief}}", cut(str(ctx["context"]).strip(), 600))
    fi = ctx.get("fix_items")
    if "{{fix_items}}" in out and isinstance(fi, list):
        items = "\n".join(f"  {i}. {cut(' '.join(str(x).split()), 160)}" for i, x in enumerate(fi, 1))
        out = out.replace("{{fix_items}}", items or "remark no")
    return out


def _plan_brief(plan) -> str:
    """Render a compact plan summary for a notification."""
    try:
        from ..core.types import plan_to_dict
        d = plan_to_dict(plan)
    except Exception:
        return "(plan not rendered)"
    L = [f"goal: {d.get('goal') or ''}"]
    for i, u in enumerate(d.get("units") or [], 1):
        instr = " ".join(str(u.get("instruction") or "").split())
        L.append(f"  {i}. [{u.get('id')}] {cut(instr, 120)}")
    gate = d.get("gate_cmd") or []
    L.append("gate: " + (" ".join(str(g) for g in gate) if gate else "(no)"))
    return "\n".join(L)


def _default_model(ctx: dict) -> dict:
    """Build the default completion notification."""
    lines = []
    task = " ".join(str(ctx.get("task") or "").split())
    if task:
        lines.append(f"task: {cut(task, 120)}")
    green = ctx.get("green_branches") or []
    plan = ctx.get("plan")
    if plan is not None:
        try:
            from ..core.types import plan_to_dict
            total = len(plan_to_dict(plan).get("units") or [])
        except Exception:
            total = 0
        if total:
            lines.append(f"units green: {len(green)}/{total}")
    sb = ctx.get("session_branch")
    if sb:
        lines.append(f"branch session (merge its): {sb}")
    elif green:
        lines.append(f"branch: {', '.join(green)}")
    elif plan is not None:
        lines.append("branches no")
    integ = ctx.get("integration_branch")
    if integ and not sb:
        lines.append(f"integration: {integ}")
    return model_from_ctx(ctx, kind="info", title="flow to complete", body="\n".join(lines))


def _build(phase, ctx: dict):
    """Build a notification from explicit text, a template, or defaults."""
    explicit = ctx.get("notify_message")
    if explicit:
        return explicit                                # Legacy text or a structured producer result.
    tmpl = (getattr(phase, "prompt", "") or "").strip()
    if tmpl:
        if not re.search(r"\s", tmpl):
            from ..util import content
            tmpl = content.load(tmpl, default=tmpl)
        rendered = _render(tmpl, ctx)
        title, _, body = rendered.partition("\n")
        act = getattr(phase, "action", "") or ""
        return model_from_ctx(ctx, kind=("action" if act else "info"),
                              title=title, body=body, action=act)
    return _default_model(ctx)


def notify_phase(phase, ctx: dict) -> PhaseResult:
    """Deliver one flow-phase notification."""
    from ..tools import send as send_fn          # Re-exported by tools/__init__.
    channel = ctx.get("notify_channel")
    if not channel:
        return PhaseResult(green=True, output={"notified": False},
                           detail="notification not to inquire (no entrant)")
    msg = _build(phase, ctx)
    if isinstance(msg, dict):
        r = send_fn(channel, render_text(msg), payload=msg)
    else:
        r = send_fn(channel, str(msg))
    if r.sent:
        detail = f"notification [{channel}]: messenger"
    elif r.skipped:
        detail = f"notification [{channel}]: skipped - {r.reason}"
    else:
        detail = f"notification [{channel}]: NOT to deliver — {r.reason}"
    return PhaseResult(green=True, output={"notified": bool(r.sent), "notify_detail": detail}, detail=detail)



NOTIFY_HOOKS_META_KEY = "notify_hooks"


def hooked_kinds(conn) -> set:
    """Return event kinds enabled for notification hooks."""
    if conn is None:
        return set()
    try:
        from ..db import dao
        raw = dao.get_meta(conn, NOTIFY_HOOKS_META_KEY, "") or ""
        return {k.strip() for k in raw.split(",") if k.strip()}
    except Exception:  # noqa: BLE001
        return set()


def maybe_notify(conn, *, kind: str, text: str, session_id=None) -> None:
    """Deliver an event notification when its kind is enabled."""
    if conn is None or kind not in hooked_kinds(conn):
        return
    try:
        from ..core.events import emit
        from ..db import dao
        from ..tools import send as send_fn
        channel = dao.get_meta(conn, "notify_channel", "") or ""
        if not channel:
            return
        r = send_fn(channel, text)
        emit(conn, kind="notify", name=channel, session_id=session_id,
             status=("sent" if r.sent else ("skip" if r.skipped else "fail")),
             params={"hook": kind})
    except Exception:
        pass
