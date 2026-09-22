# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Accounts Aether is allowed to act in, and how it holds them.

A *session* connection is a sign-in the user performs themselves in Aether's
browser window. Nothing is stored here: what persists is the cookie in that
profile. A *secret* connection is an API key, and it lives in the operating
system's keyring. The model is told which connections exist, by name, and never
sees a value. ARCHITECTURE.md has the reasoning.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from config import DATA

STORE = DATA / "connections.json"
KEYRING_SERVICE = "aether"
_lock = threading.Lock()

# What the Connections panel offers out of the box. Anything not here can still
# be added: a site to sign in to, or a named key to keep.
CATALOG: dict[str, dict] = {
    "google": {
        "label": "Google",
        "kind": "session",
        "note": "Docs, Drive, Gmail and anything else you use signed in to Google.",
        "sign_in_url": "https://accounts.google.com/ServiceLogin",
        "check_url": "https://myaccount.google.com/",
        "signed_in_host": "myaccount.google.com",
    },
    "microsoft": {
        "label": "Microsoft",
        "kind": "session",
        "note": "Outlook, Office and OneDrive in the browser.",
        "sign_in_url": "https://login.microsoftonline.com/",
        "check_url": "https://outlook.office.com/mail/",
        "signed_in_host": "outlook.office.com",
    },
    "github": {
        "label": "GitHub",
        "kind": "session",
        "note": "Issues, pull requests and code review in the browser.",
        "sign_in_url": "https://github.com/login",
        "check_url": "https://github.com/settings/profile",
        "signed_in_host": "github.com",
        "signed_out_path": "/login",
    },
}


def _load() -> dict:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"connections": {}}


def _save(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

def keyring_backend() -> tuple[bool, str]:
    """Whether a real keyring is available, and what it is. A missing one is
    reported rather than worked around."""
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring):
            return False, "no system keyring is available on this machine"
        return True, type(backend).__name__
    except Exception as e:
        return False, f"the keyring package is unavailable ({e})"


def set_secret(cid: str, value: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, f"connection:{cid}", value)


def secret(cid: str) -> str | None:
    """The stored value. Never rendered into a prompt."""
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, f"connection:{cid}")
    except Exception:
        return None


def _forget_secret(cid: str) -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, f"connection:{cid}")
    except Exception:
        pass


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

def _entry(cid: str, stored: dict) -> dict:
    spec = {**CATALOG.get(cid, {}), **stored}
    out = {
        "id": cid,
        "label": spec.get("label") or cid,
        "kind": spec.get("kind") or "session",
        "note": spec.get("note") or "",
        "status": spec.get("status") or "not connected",
        "connected_at": spec.get("connected_at"),
        "checked_at": spec.get("checked_at"),
    }
    if out["kind"] == "session":
        out["sign_in_url"] = spec.get("sign_in_url")
    else:
        # Presence only. The value never leaves this module.
        out["has_secret"] = bool(secret(cid))
    return out


def listing() -> dict:
    """Every connection, plus what could still be added."""
    data = _load()
    stored = data.get("connections") or {}
    connections = [_entry(cid, entry) for cid, entry in sorted(stored.items())]
    available = [
        {"id": cid, "label": spec["label"], "kind": spec["kind"], "note": spec["note"]}
        for cid, spec in CATALOG.items()
        if cid not in stored
    ]
    ok, backend = keyring_backend()
    return {
        "connections": connections,
        "available": available,
        "keyring": {"ok": ok, "backend": backend},
    }


def add(cid: str, label: str | None = None, kind: str | None = None,
        sign_in_url: str | None = None, note: str | None = None) -> dict:
    """Register a connection. Establishing it is a separate, deliberate step."""
    cid = str(cid or "").strip().lower().replace(" ", "-")
    if not cid:
        raise ValueError("a connection needs a name")
    with _lock:
        data = _load()
        entry = dict(CATALOG.get(cid) or {})
        if label:
            entry["label"] = label
        if kind:
            entry["kind"] = kind
        if sign_in_url:
            entry["sign_in_url"] = sign_in_url
            entry.setdefault("check_url", sign_in_url)
            entry.setdefault("signed_in_host", urlsplit(sign_in_url).netloc)
        if note:
            entry["note"] = note
        entry.setdefault("kind", "session" if sign_in_url else "secret")
        entry.setdefault("label", cid.replace("-", " ").title())
        entry.setdefault("status", "not connected")
        data.setdefault("connections", {})[cid] = entry
        _save(data)
    return _entry(cid, _load()["connections"][cid])


def forget(cid: str) -> dict:
    with _lock:
        data = _load()
        existed = (data.get("connections") or {}).pop(cid, None)
        _save(data)
    _forget_secret(cid)
    return {"ok": bool(existed), "id": cid}


def _mark(cid: str, **fields: Any) -> dict:
    with _lock:
        data = _load()
        entry = (data.get("connections") or {}).get(cid)
        if entry is None:
            raise KeyError(cid)
        entry.update(fields)
        _save(data)
    return _entry(cid, entry)


# A sign-in starts on Aether's own page, never on the service's: a window that
# opens straight onto someone else's login form has the shape of a phishing page.
CONSENT_HOST = "http://127.0.0.1"
# Read at call time, so a server bound elsewhere points its browser at itself.
DEFAULT_PORT = 7878


def consent_url(cid: str) -> str:
    import os

    port = os.environ.get("AETHER_PORT") or DEFAULT_PORT
    return f"{CONSENT_HOST}:{port}/connect/{cid}"


def spec(cid: str) -> dict | None:
    """Everything known about one connection, stored or built in."""
    entry = (_load().get("connections") or {}).get(cid)
    if entry is None and cid not in CATALOG:
        return None
    return {**CATALOG.get(cid, {}), **(entry or {})}


def sign_in(cid: str) -> dict:
    """Open Aether's browser on Aether's own page, which offers the sign-in.

    Aether never drives the form and never sees the password.
    """
    import browser

    data = _load()
    entry = (data.get("connections") or {}).get(cid) or CATALOG.get(cid)
    if entry is None:
        raise KeyError(cid)
    if entry.get("kind") != "session":
        raise ValueError(f"{cid} is not a browser sign-in")
    url = entry.get("sign_in_url")
    if not url:
        raise ValueError(f"{cid} has no sign-in page")
    result = json.loads(browser.run({"action": "open", "url": consent_url(cid)}))
    if result.get("error"):
        return {"ok": False, "error": result["error"]}
    if cid not in (data.get("connections") or {}):
        add(cid)
    return {
        **_mark(cid, status="signing in", started_at=time.time()),
        "ok": True,
        "next": (
            "Aether opened its own browser window. Read what it says, then continue to "
            f"the {entry.get('label') or cid} sign-in from there and press Check when "
            "you are done. Aether never sees the password."
        ),
    }


def check(cid: str) -> dict:
    """Ask the service whether the session took, by visiting a page only a
    signed-in user reaches."""
    data = _load()
    entry = (data.get("connections") or {}).get(cid)
    if entry is None:
        raise KeyError(cid)
    if entry.get("kind") != "session":
        connected = bool(secret(cid))
        return _mark(
            cid,
            status="connected" if connected else "not connected",
            checked_at=time.time(),
        )

    import browser

    target = entry.get("check_url") or entry.get("sign_in_url")
    result = json.loads(browser.run({"action": "open", "url": target}))
    if result.get("error"):
        return {**_entry(cid, entry), "ok": False, "error": result["error"]}
    landed = str(result.get("url") or "")
    host = urlsplit(landed).netloc
    path = urlsplit(landed).path
    # Signed out, every service sends you to its login page. Where you land is a
    # steadier signal than any element on the page.
    signed_in = host == (entry.get("signed_in_host") or host)
    if signed_in and entry.get("signed_out_path"):
        signed_in = not path.startswith(entry["signed_out_path"])
    return {
        **_mark(
            cid,
            status="connected" if signed_in else "not connected",
            checked_at=time.time(),
            connected_at=time.time() if signed_in else entry.get("connected_at"),
        ),
        "ok": True,
        "landed_on": landed,
    }


def store_secret(cid: str, value: str) -> dict:
    ok, backend = keyring_backend()
    if not ok:
        raise RuntimeError(
            f"Nothing can hold this safely here: {backend}. Install a system "
            "keyring, or use a browser sign-in instead."
        )
    if cid not in (_load().get("connections") or {}):
        add(cid, kind="secret")
    set_secret(cid, value)
    return _mark(cid, status="connected", connected_at=time.time())


def connected() -> list[dict]:
    return [c for c in listing()["connections"] if c["status"] == "connected"]


def prompt_block() -> str:
    """What the model is told: which accounts it can act in, by name."""
    live = connected()
    if not live:
        return ""
    sessions = [c["label"] for c in live if c["kind"] == "session"]
    keys = [c["label"] for c in live if c["kind"] != "session"]
    lines = ["# Connections"]
    if sessions:
        lines.append(
            f"Aether's browser is signed in to: {', '.join(sessions)}. Work in those "
            "accounts directly, and do not ask the user to sign in again."
        )
    if keys:
        # Named, never valued, so the key cannot be repeated into a reply.
        lines.append(
            f"Aether holds a key for: {', '.join(keys)}. You do not see the key itself "
            "and never need to: the tool that uses it has it."
        )
    lines.append(
        "Anything not listed is not connected. Say so rather than trying to get past "
        "a login page, and never ask the user for a password."
    )
    return "\n".join(lines)
