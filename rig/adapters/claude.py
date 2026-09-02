"""Claude Code CLI adapter. Invariants: G1, G2, G5, A3, B1, and D11.

Verified CLI shape as of 2026-07-07: ``claude -p --output-format stream-json --verbose
--model M --permission-mode MODE [--allowedTools "..."] [--effort E]
[--append-system-prompt-file CTX] [--resume ID] [--json-schema S] "prompt"``. Standard output is
a JSONL event stream ending with a ``result`` event. The final event uses the same fields as the
legacy single-JSON format.

Stream output preserves the full invocation transcript in ``runs.transcript`` [D11]. Permission
modes never use ``bypassPermissions`` [A7]. Read mode adds a physical tool allowlist, and schemas
are passed inline even though the adapter API accepts a file path.
"""
from __future__ import annotations

import json
import os

from ..core.result import Result, parse_claude_stream
from .base import ENGINE_DIR, Capabilities, HarnessAdapter, SchemaNotApplied, system_prompt_path

# Map abstract permissions to Claude modes. Never use bypassPermissions [A2].
_PERMS_TO_MODE = {
    "read": "default",        # enforced by --allowedTools below, not by the mode itself
    "plan": "plan",           # native planning mode
    "auto": "acceptEdits",    # accept edits automatically without bypassing all permissions
    "default": "default",     # request permission when needed
}
# Read mode has a physical read-only tool allowlist.
_READ_ONLY_TOOLS = "Read Grep Glob LS WebFetch WebSearch"


class ClaudeAdapter(HarnessAdapter):
    name = "claude"

    def __init__(self, runner, *, binary: str = "claude"):
        super().__init__(runner)
        # Permission mode belongs to each invocation, not to the adapter instance.
        self.binary = binary

    def capabilities(self) -> Capabilities:
        return Capabilities(effort=True, schema_output=True, native_resume=True, agents_md_native=False)

    def build_cmd(self, prompt, *, model, perms="default", effort=None,
                  system_context_path=None, resume_session_id=None, schema_path=None,
                  allowed_tools=None):
        try:
            mode = _PERMS_TO_MODE[perms]
        except KeyError:
            raise ValueError(f"claude: unknown perms {perms!r} (expected one of {tuple(_PERMS_TO_MODE)})")
        # --verbose is required with stream-json. The final result event matches the legacy format.
        cmd = [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
               "--permission-mode", mode]
        # An explicit allowlist supports restricted lead sessions. Otherwise read mode uses the
        # read-only list above. In non-interactive mode, tools outside the list cannot prompt.
        tools = allowed_tools or (_READ_ONLY_TOOLS if perms == "read" else None)
        if tools:
            cmd += ["--allowedTools", tools]
        if effort:
            cmd += ["--effort", effort]
        if system_context_path:
            cmd += ["--append-system-prompt-file", system_context_path]  # preserve project CLAUDE.md [G5]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]  # Claude uses a flag; Codex uses a subcommand
        if schema_path:
            # Claude expects an inline schema, so read and inline the requested file. Missing or
            # empty schemas fail closed instead of silently running unconstrained [B1].
            try:
                schema = self.runner.read_file(schema_path)
            except (OSError, ValueError) as e:
                raise SchemaNotApplied(f"claude: could not read schema {schema_path}: {e}") from e
            if not schema:
                raise SchemaNotApplied(f"claude: empty schema {schema_path}")
            cmd += ["--json-schema", schema]
        # ``--`` terminates option parsing. Claude 2.1.193 can otherwise consume the prompt as part
        # of --allowedTools and report "Input must be provided". It also protects prompts starting with ``-``.
        cmd += ["--", prompt]
        return cmd

    def parse_output(self, stdout, exit_code) -> Result:
        # core.result owns parsing and preserves the entire stream as the transcript [D11].
        return parse_claude_stream(stdout, exit_code)

    def inject_context(self, method_text, cwd) -> str:
        # Claude does not load AGENTS.md natively. Use a content-addressed prompt file so concurrent
        # sessions cannot overwrite one another or the project's CLAUDE.md [G5].
        path = system_prompt_path(cwd, method_text)
        self.runner.write_file(path, method_text)
        return path

    def auth_env(self) -> dict:
        # Restore only Claude credentials after the runner scrubs worker environments [A3]. OAuth
        # continues to work through HOME; API credentials must be restored explicitly.
        env = {}
        for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            v = os.environ.get(k)
            if v:
                env[k] = v
        return env


# --- Render the raw provider stream as readable transcript text [D11] ---

_SKIP_SYSTEM = {"hook_started", "hook_response", "hook_progress", "thinking_tokens"}
_RESULT_CUT = 700   # rendering limit only; the database retains the complete stream


def _one_line(val, cut: int) -> str:
    s = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= cut else s[: cut - 1] + "…"


def render_transcript(raw: str) -> str:
    """Render stream-json events as a readable transcript without raising on malformed lines.

    Hook and rate-limit noise is omitted. Historical labels such as ``[to invent]`` remain part of
    the public transcript format.
    """
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "system":
            sub = ev.get("subtype", "")
            if sub == "init":
                out.append(f"[start] model={ev.get('model', '?')} cwd={ev.get('cwd', '?')}")
            elif sub not in _SKIP_SYSTEM:
                out.append(f"[system:{sub}]")
        elif t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking" and b.get("thinking"):
                    out.append(f"[to invent] {_one_line(b['thinking'], 2000)}")
                elif bt == "tool_use":
                    out.append(f"[tool] {b.get('name', '?')} {_one_line(b.get('input', {}), 400)}")
                elif bt == "text" and b.get("text"):
                    out.append(f"[text] {b['text']}")
        elif t == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out.append(f"[sum tools] {_one_line(b.get('content', ''), _RESULT_CUT)}")
        elif t == "result":
            cost = ev.get("total_cost_usd")
            out.append(f"[finale] {ev.get('subtype', '?')} · ${cost if cost is not None else '?'} · "
                       f"{_one_line(ev.get('result') or '', 400)}")
        # Other event types, including rate-limit events, are presentation noise.
    return "\n".join(out) if out else "(stream empty or unrecognized - inspect the raw transcript)"
