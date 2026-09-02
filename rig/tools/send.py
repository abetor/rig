"""Deliver outbound messages through supported transports."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class SendResult:
    """Outcome of one notification delivery attempt."""
    channel: str
    sent: bool = False
    skipped: bool = False
    reason: str = ""


def _telegram_deliver(text: str, target: Optional[str], creds: dict, payload: Optional[dict] = None) -> bool:
    """Deliver a notification through Telegram."""
    token = creds.get("TELEGRAM_BOT_TOKEN", "")
    chat = target or creds.get("TELEGRAM_CHAT_ID", "")
    if not chat:
        return False
    data = json.dumps({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _webhook_deliver(text: str, target: Optional[str], creds: dict, payload: Optional[dict] = None) -> bool:
    """Deliver a notification to an HTTP webhook."""
    url = target or creds.get("WEBHOOK_URL", "")
    if not url:
        return False
    body = {"text": text}
    if payload:
        body["message"] = payload
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


@dataclass
class _Channel:
    cred_keys: tuple
    deliver: Callable          # (text, target, creds, payload) -> bool


CHANNELS = {
    "telegram": _Channel(("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"), _telegram_deliver),
    "webhook": _Channel(("WEBHOOK_URL",), _webhook_deliver),
}


def send(channel: str, text: str, *, target: Optional[str] = None,
         env: Optional[dict] = None, deliver: Optional[Callable] = None,
         payload: Optional[dict] = None) -> SendResult:
    """Deliver text and an optional structured payload to a channel."""
    if channel not in CHANNELS:
        raise ValueError(f"send: unknown channel {channel!r} (exists {tuple(CHANNELS)})")
    if not (text or "").strip():
        raise ValueError("send: empty text")
    spec = CHANNELS[channel]
    src = os.environ if env is None else env

    primary = spec.cred_keys[0]
    if not src.get(primary):
        return SendResult(channel=channel, skipped=True,
                          reason=f"channel not he is in optimistic mood (no env {primary})")

    creds = {k: src.get(k, "") for k in spec.cred_keys}
    fn = deliver or spec.deliver
    try:
        ok = bool(fn(text, target, creds, payload))
    except Exception as e:
        return SendResult(channel=channel, sent=False, reason=f"delivery weak: {type(e).__name__}: {e}")
    return SendResult(channel=channel, sent=ok,
                      reason="" if ok else "delivery backend returned failure")
