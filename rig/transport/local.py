"""Local subprocess transport with environment confinement."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from typing import Callable, Optional

from .base import ExecResult, Handle, Runner, RiskTier

_TRIPWIRE = [
    re.compile(
        r"\brm\b(?=.*(?:-\w*r|--recursive))(?=.*(?:-\w*f|--force))"
        r"(?=.*(?:\s/(?:\s|$|\*)|~(?:\s|/?$)|\$HOME|"
        r"\s/(?:etc|usr|var|bin|sbin|lib|opt|System|Library|Applications|Users|boot|dev|private|root|home)(?:\s|/?$)))",
        re.I,
    ),
    re.compile(r"\bfind\b.*\s-delete\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\b.*\bof=/dev/"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\bchmod\b.*\s-R\b.*\s(/|~)"),
    re.compile(r":\s*\(\s*\)\s*\{.*\}\s*;\s*:"),  # Whitespace-tolerant fork-bomb signature.
]

_SAFE_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TZ", "PWD", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
}


def _clean_env(env: dict) -> dict:
    """Normalize environment keys and values for subprocess APIs."""
    out = {}
    for k, v in env.items():
        if v is None:
            continue
        out[k] = v if isinstance(v, str) else str(v)
    return out


def build_env(env: Optional[dict], worker: bool) -> dict:
    """Build a full or worker-confined subprocess environment."""
    if worker:
        base = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}  # Environment allowlist.
        base.update(env or {})  # Explicit variables, including harness credentials.
    else:
        base = {**os.environ, **(env or {})}
    return _clean_env(base)


def guard(
    cmd: list[str],
    risk: RiskTier,
    *,
    log: Callable[[dict], None],
    allow_catastrophic: bool,
    allow_high_risk: bool,
) -> None:
    """Reject commands that exceed the requested risk policy."""
    if risk in ("high", "core") and not allow_high_risk:
        log({"ev": "risk_block", "cmd": cmd, "risk": risk})
        raise PermissionError(f"runner: risk={risk!r} to obtain allow_high_risk (v0 not does high/core)")
    if allow_catastrophic:
        return
    line = " ".join(cmd)
    for pat in _TRIPWIRE:
        if pat.search(line):
            log({"ev": "guard_block", "cmd": line, "pat": pat.pattern})
            raise PermissionError(f"runner tripwire blocked: {line!r} (matched {pat.pattern!r})")


def _file_logger(path: str) -> Callable[[dict], None]:
    """Build an append-only JSONL event logger."""
    abspath = os.path.abspath(path)

    def log(ev: dict) -> None:
        os.makedirs(os.path.dirname(abspath), exist_ok=True)
        with open(abspath, "a") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")

    return log


class _LocalHandle(Handle):
    def __init__(self, popen: "subprocess.Popen", log_path: str):
        self._p = popen
        self.log_path = log_path

    def poll(self) -> Optional[int]:
        return self._p.poll()

    def wait(self, timeout: Optional[float] = None) -> int:
        return self._p.wait(timeout)

    def kill(self) -> None:
        self._signal_group(signal.SIGTERM)
        time.sleep(1.0)
        self._signal_group(signal.SIGKILL)
        try:
            self._p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self._p.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                self._p.send_signal(sig)
            except (ProcessLookupError, PermissionError, ValueError):
                pass


class LocalRunner(Runner):
    def __init__(
        self,
        *,
        log: Optional[Callable[[dict], None]] = None,
        log_path: Optional[str] = None,
        allow_catastrophic: bool = False,
        allow_high_risk: bool = False,
    ):
        self._log = log or (_file_logger(log_path) if log_path else (lambda ev: None))
        self._allow = allow_catastrophic
        self._allow_high = allow_high_risk

    def exec(self, cmd, *, cwd, env=None, timeout=None, risk="low", worker=False) -> ExecResult:
        guard(cmd, risk, log=self._log, allow_catastrophic=self._allow, allow_high_risk=self._allow_high)
        full_env = build_env(env, worker=worker)  # Restrict synchronous worker environments [A2].
        self._log({"ev": "exec", "cmd": cmd, "cwd": cwd, "risk": risk, "worker": worker})
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True,
                               text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            dur = time.monotonic() - t0
            self._log({"ev": "exec_timeout", "dur_s": round(dur, 3)})
            return ExecResult(124, e.stdout or "", (e.stderr or "") + "\n[runner: timeout]", dur, timed_out=True)
        except FileNotFoundError as e:
            dur = time.monotonic() - t0
            self._log({"ev": "exec_error", "err": "not_found"})
            return ExecResult(127, "", f"[runner: not found] {e}", dur)
        except (OSError, UnicodeDecodeError, ValueError) as e:
            dur = time.monotonic() - t0
            self._log({"ev": "exec_error", "err": type(e).__name__})
            return ExecResult(126, "", f"[runner: {type(e).__name__}] {e}", dur)
        dur = time.monotonic() - t0
        self._log({"ev": "exec_done", "exit": p.returncode, "dur_s": round(dur, 3)})
        return ExecResult(p.returncode, p.stdout, p.stderr, dur)

    def spawn(self, cmd, *, cwd, log_path, env=None, risk="medium", worker=True) -> Handle:
        guard(cmd, risk, log=self._log, allow_catastrophic=self._allow, allow_high_risk=self._allow_high)
        full_env = build_env(env, worker=worker)  # Workers receive only the allowlisted environment [A2].
        self._log({"ev": "spawn", "cmd": cmd, "cwd": cwd, "worker": worker, "log": log_path})
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        f = open(log_path, "w")
        try:
            p = subprocess.Popen(cmd, cwd=cwd, env=full_env, stdout=f,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        finally:
            f.close()
        return _LocalHandle(p, log_path)

    def read_file(self, path: str) -> str:
        """Read file."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except UnicodeDecodeError as e:
            raise ValueError(
                f"read_file: {path} not UTF-8 text (possibly binary) - project I/O contract"
            ) from e

    def write_file(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)
