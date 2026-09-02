"""Derive storage paths from a project root and read secrets from the environment.

The normalized project path identifies its database at ``<project>/.rig/memory.db``
without requiring configuration. Secrets come only from the process environment or
``~/.rig/.env`` and are never stored in the portable project database [D1].
"""
from __future__ import annotations

import json
import os
import time

from ..core.types import ENGINE_DIR

DB_DIRNAME = ENGINE_DIR      # Engine directory inside a project; defined once in core.types.
DB_FILENAME = "memory.db"    # Project memory and state.
ENV_FILENAME = ".env"        # Optional user secrets at ~/.rig/.env.
REGISTRY_FILENAME = "projects.jsonl"  # Projects known to the global viewer.
# The engine-state directory is not project content [D1]. Its own ``.gitignore``
# hides every generated file without modifying the host project's ignore rules.
GITIGNORE_FILENAME = ".gitignore"
GITIGNORE_BODY = "# rig engine state - not project content (D1)\n*\n"


def _norm(path: str) -> str:
    """Normalize a path by expanding users and resolving relative paths and symlinks."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or ".")))


def rig_dir(project: str) -> str:
    """Directory `<project>/.rig`."""
    return os.path.join(_norm(project), DB_DIRNAME)


def db_path(project: str) -> str:
    """Return the stable project database path: ``<project>/.rig/memory.db``."""
    return os.path.join(rig_dir(project), DB_FILENAME)


def ensure_rig_dir(project: str) -> str:
    """Create ``<project>/.rig`` and its private ignore file, then return the path.

    Existing ignore files are preserved, and exclusive creation handles concurrent
    initializers safely.
    """
    d = rig_dir(project)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, GITIGNORE_FILENAME)
    if not os.path.exists(gi):
        try:
            with open(gi, "x", encoding="utf-8") as f:  # Never clobber during concurrent initialization.
                f.write(GITIGNORE_BODY)
        except FileExistsError:
            pass  # Another process created it after the existence check.
    return d


def ensure_db_path(project: str) -> str:
    """Ensure the engine directory exists and return its database path."""
    ensure_rig_dir(project)
    return db_path(project)


# The project registry feeds ``rig --view``. It is a best-effort discovery index,
# not configuration or a source of truth.

def registry_path() -> str:
    """Return the user-level project registry path."""
    return os.path.join(os.path.expanduser("~"), DB_DIRNAME, REGISTRY_FILENAME)


def read_registry() -> list:
    """Read unique project paths from the registry in first-seen order."""
    out, seen = [], set()
    try:
        # Replace invalid bytes so one damaged line cannot hide the rest of the registry.
        with open(registry_path(), "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                p = rec.get("path") if isinstance(rec, dict) else None
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
    except OSError:
        return []
    return out


def register_project(project: str) -> bool:
    """Append a normalized project path if absent; return whether it was added."""
    p = _norm(project)
    try:
        if p in read_registry():
            return False
        os.makedirs(os.path.dirname(registry_path()), exist_ok=True)
        with open(registry_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": p, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                               ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


# Secrets come from the environment or optional ``~/.rig/.env``, never the database [D1].

def user_env_path() -> str:
    """Return the user secrets-file path."""
    return os.path.join(os.path.expanduser("~"), DB_DIRNAME, ENV_FILENAME)


def _parse_env_file(path: str) -> dict:
    """Parse simple ``KEY=VALUE`` entries, returning an empty mapping on read failure."""
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key:
                    out[key] = val
    except OSError:
        return {}
    return out


def read_env_secrets() -> dict:
    """Read file secrets, with matching process variables taking precedence [D1]."""
    secrets = _parse_env_file(user_env_path())
    for key in list(secrets):
        if key in os.environ:
            secrets[key] = os.environ[key]
    return secrets


def get_secret(name: str, default=None):
    """Read one secret, preferring the process environment over `~/.rig/.env`, never the database [D1]."""
    if name in os.environ:
        return os.environ[name]
    return _parse_env_file(user_env_path()).get(name, default)


def load_env_into_process() -> int:
    """Load missing variables from ``~/.rig/.env`` and return the number added.

    Existing process variables always win. Entry points call this once so harness,
    notification, CLI, and content settings share the same environment.
    """
    n = 0
    for key, val in _parse_env_file(user_env_path()).items():
        if key not in os.environ:
            os.environ[key] = val
            n += 1
    return n
