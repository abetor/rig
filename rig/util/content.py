"""Resolve and load trusted content files within configured roots."""
from __future__ import annotations

import os
import re
from pathlib import Path

ENV_CONTENT_DIR = "RIG_CONTENT_DIR"  # Override the content root with a filesystem path.


def content_dir(base: str | Path | None = None) -> Path:
    """Resolve the configured content root."""
    if base is not None:
        return Path(base)
    env = os.environ.get(ENV_CONTENT_DIR)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "flows"


def load(name: str, *, default: str = "", base: str | Path | None = None) -> str:
    """Load trusted content by name from the configured root."""
    root = content_dir(base).resolve()
    try:
        target = (root / name).resolve()
    except (OSError, ValueError, RuntimeError):
        return default
    if target != root and root not in target.parents:
        return default
    try:
        return target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, NotADirectoryError):
        return default


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render_text(text: str, **vars: object) -> str:
    """Render text."""
    return _PLACEHOLDER.sub(lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), text)


def render(name: str, *, default: str = "", base: str | Path | None = None, **vars: object) -> str:
    """Render the value."""
    return render_text(load(name, default=default, base=base), **vars)
