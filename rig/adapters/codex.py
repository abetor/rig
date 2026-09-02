"""OpenAI Codex CLI adapter. Invariants: G1, G2, G5, and A3.

Verified CLI shape as of Codex 0.137: ``codex exec [resume <id>] --json --model M
-c approval_policy=AP --sandbox SB [-c model_reasoning_effort=E] [-c model_instructions_file=CTX]
[--output-schema PATH] "prompt"``. Standard output is a JSONL event stream.

Codex differs from Claude in command shape, effort configuration, resume syntax, schema handling,
and output format; those differences remain isolated in the adapter. Read and plan modes use the
read-only sandbox, and this adapter never requests ``danger-full-access`` [A7]. Codex reports token
usage but not cost, so normalized cost remains ``None`` [G4].
"""
from __future__ import annotations

import os

from ..core.result import Result, parse_codex_jsonl
from .base import ENGINE_DIR, Capabilities, HarnessAdapter, system_prompt_path

# Map abstract permissions to sandbox and approval policy. Never use danger-full-access [A2].
_PERMS_TO_CODEX = {
    "read":    ("read-only", "never"),
    "plan":    ("read-only", "never"),
    "auto":    ("workspace-write", "never"),
    "default": ("workspace-write", "on-request"),
}


class CodexAdapter(HarnessAdapter):
    name = "codex"

    def __init__(self, runner, *, binary: str = "codex"):
        super().__init__(runner)
        # Sandbox and approval policy belong to each invocation, not to the adapter instance.
        self.binary = binary

    def capabilities(self) -> Capabilities:
        return Capabilities(effort=True, schema_output=True, native_resume=True, agents_md_native=True)

    def build_cmd(self, prompt, *, model, perms="default", effort=None,
                  system_context_path=None, resume_session_id=None, schema_path=None,
                  allowed_tools=None):
        # Codex has no per-tool allowlist, so allowed_tools cannot be enforced here [A2].
        try:
            sandbox, approval = _PERMS_TO_CODEX[perms]
        except KeyError:
            raise ValueError(f"codex: unknown perms {perms!r} (expected one of {tuple(_PERMS_TO_CODEX)})")
        cmd = [self.binary, "exec"]
        if resume_session_id:
            cmd += ["resume", resume_session_id]  # Codex uses a subcommand; Claude uses a flag
        # approval_policy is a configuration override, not an exec flag.
        cmd += ["--json", "-c", f"approval_policy={approval}", "--sandbox", sandbox]
        # Omit --model when empty to use the account default. Some ChatGPT-backed sessions reject
        # explicit model aliases [G1].
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]  # configuration override, not --effort
        if system_context_path:
            # Inject the method through configuration without overwriting project AGENTS.md [G5].
            cmd += ["-c", f"model_instructions_file={system_context_path}"]
        if schema_path:
            # Codex accepts a schema file path; Claude requires inline schema text.
            cmd += ["--output-schema", schema_path]
        cmd.append(prompt)
        return cmd

    def parse_output(self, stdout, exit_code) -> Result:
        # core.result owns JSONL parsing, content-block joining, resume IDs, and usage metadata [G2].
        return parse_codex_jsonl(stdout, exit_code)

    def inject_context(self, method_text, cwd) -> str:
        # Content-addressed names prevent concurrent sessions from overwriting one another [D1/G5].
        path = system_prompt_path(cwd, method_text)
        self.runner.write_file(path, method_text)
        return path

    def auth_env(self) -> dict:
        # Restore only Codex API credentials after worker environment confinement [A3]. OAuth is
        # available through HOME and ~/.codex/auth.json.
        env = {}
        v = os.environ.get("CODEX_API_KEY")
        if v:
            env["CODEX_API_KEY"] = v
        return env


# --- Render the Codex JSONL stream as readable transcript text [D11] ---

def _one_line(val, cut: int) -> str:
    if not isinstance(val, str):
        import json as _json
        val = _json.dumps(val, ensure_ascii=False, default=str)
    val = " ".join(val.split())
    return val if len(val) <= cut else val[: cut - 1] + "…"


def render_transcript(raw: str) -> str:
    """Render Codex JSONL events as a readable transcript, ignoring malformed lines."""
    import json as _json
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except (_json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        itype = item.get("item_type") or item.get("type")
        if t == "thread.started":
            out.append(f"[start] thread={ev.get('thread_id', '?')}")
        elif t == "item.completed" and itype == "reasoning":
            out.append(f"[to invent] {_one_line(item.get('text') or item.get('summary') or '', 2000)}")
        elif t == "item.completed" and itype == "command_execution":
            out.append(f"[command] {_one_line(item.get('command', ''), 400)} → "
                       f"exit={item.get('exit_code', '?')}")
        elif t == "item.completed" and itype == "agent_message":
            txt = item.get("text") or item.get("content") or ""
            if isinstance(txt, list):
                txt = "".join(b.get("text", "") for b in txt if isinstance(b, dict))
            out.append(f"[text] {txt}")
        elif t == "turn.completed":
            out.append(f"[finale] usage={_one_line(ev.get('usage') or {}, 200)}")
        elif t == "error" or ev.get("is_error"):
            out.append(f"[error] {_one_line(ev.get('message') or ev.get('error') or '', 400)}")
    return "\n".join(out) if out else "(stream empty or unrecognized - inspect the raw transcript)"
