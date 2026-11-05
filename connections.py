# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Track browser sessions and API keys available to Olith.

Browser sessions stay in Olith's Chrome profile. Keys stay in the OS keyring.
Tell the model which accounts are available without showing their secrets.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from config import DATA
from connection_catalog import BROWSER_CATALOG, preferred_tools

STORE = DATA / "connections.json"
KEYRING_SERVICE = "olith"
_lock = threading.Lock()
_browser_lock = threading.Lock()

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
CATALOG.update(BROWSER_CATALOG)

# Which saved keys a tool really reads, and which tool. A key not listed here is
# only stored, and the prompt has to say so or the model goes looking for it.
KEY_CONSUMERS: dict[str, str] = {
    "github-api": "git_status",
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
        "category": spec.get("category") or "Accounts",
        "status": spec.get("status") or "not connected",
        "connected_at": spec.get("connected_at"),
        "checked_at": spec.get("checked_at"),
        "status_note": spec.get("status_note") or "",
        "tool_preset": preferred_tools(cid),
    }
    if out["kind"] == "session":
        out["sign_in_url"] = spec.get("sign_in_url")
        out["verification"] = spec.get("verification") or (
            "user confirmed" if spec.get("confirmation_required") else "checked"
        ) if out["status"] == "connected" else None
    else:
        # Listing must not unlock or wait for the desktop keyring.
        out["has_secret"] = bool(spec.get("has_secret", out["status"] == "connected"))
    return out


def listing() -> dict:
    """Every connection, plus what could still be added."""
    data = _load()
    stored = data.get("connections") or {}
    connections = [_entry(cid, entry) for cid, entry in sorted(stored.items())]
    available = [
        {"id": cid, "label": spec["label"], "kind": spec["kind"], "note": spec["note"],
         "category": spec.get("category") or "Accounts",
         "tool_preset": preferred_tools(cid)}
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
        if entry["kind"] == "session" and cid not in CATALOG:
            entry["confirmation_required"] = True
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
    if existed and existed.get("kind") != "session":
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


def _observe_mark(cid: str, expected: dict, **fields: Any) -> dict:
    """A slow browser observation cannot overwrite a newer sign-in or state."""
    with _lock:
        data = _load()
        entry = (data.get("connections") or {}).get(cid)
        if entry is None:
            raise KeyError(cid)
        if all(entry.get(key) == expected.get(key) for key in ("status", "started_at", "checked_at")):
            entry.update(fields)
            _save(data)
    return _entry(cid, entry)


# A sign-in starts on Olith's own page, never on the service's: a window that
# opens straight onto someone else's login form has the shape of a phishing page.
CONSENT_HOST = "http://127.0.0.1"
# Read at call time, so a server bound elsewhere points its browser at itself.
DEFAULT_PORT = 7878


def consent_url(cid: str) -> str:
    import os

    port = os.environ.get("OLITH_PORT") or DEFAULT_PORT
    return f"{CONSENT_HOST}:{port}/connect/{cid}"


def spec(cid: str) -> dict | None:
    """Everything known about one connection, stored or built in."""
    entry = (_load().get("connections") or {}).get(cid)
    if entry is None and cid not in CATALOG:
        return None
    return {**CATALOG.get(cid, {}), **(entry or {})}


def sign_in(cid: str) -> dict:
    """Open Olith's browser on Olith's own page, which offers the sign-in.

    Olith never drives the form and never sees the password.
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
    if cid not in (data.get("connections") or {}):
        add(cid)
    with _browser_lock:
        result = json.loads(browser.run({"action": "open", "url": consent_url(cid), "focus": True, "wait": False}))
    if result.get("error"):
        return {**_mark(cid, status="needs attention", status_note=result["error"]), "ok": False, "error": result["error"]}
    return {
        **_mark(cid, status="signing in", started_at=time.time(), verification=None, connected_at=None,
                status_note="Sign in in Olith's browser. This saves a website session; it does not connect service tools."),
        "ok": True,
        "next": (
            "Olith opened its own browser window. Read what it says, then continue to "
            f"the {entry.get('label') or cid} sign-in from there. Return here when done; "
            "Olith watches for your return. Olith never sees the password."
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
    with _browser_lock:
        result = json.loads(browser.run({"action": "open", "url": target}))
    if result.get("error"):
        return {**_entry(cid, entry), "ok": False, "error": result["error"]}
    landed = str(result.get("url") or "")
    host = urlsplit(landed).netloc
    path = urlsplit(landed).path
    # Signed out, every service sends you to its login page. Where you land is a
    # steadier signal than any element on the page.
    signed_in = host == (entry.get("signed_in_host") or host) and not _is_login_page(entry, landed)
    if signed_in and entry.get("signed_out_path"):
        signed_in = not path.startswith(entry["signed_out_path"])
    return {
        **_observe_mark(
            cid, entry,
            status=("confirm sign-in" if signed_in and entry.get("confirmation_required") else "connected") if signed_in else "not connected",
            checked_at=time.time(),
            started_at=time.time(),
            connected_at=time.time() if signed_in and not entry.get("confirmation_required") else None,
            verification="checked" if signed_in and not entry.get("confirmation_required") else None,
            status_note=("Can you see your signed-in account in Olith's browser? Confirm the browser session below. Service tools connect separately."
                         if signed_in and entry.get("confirmation_required") else
                         "Browser session checked. Service tools connect separately." if signed_in else "The browser is not signed in yet."),
        ),
        "ok": True,
        "landed_on": landed,
    }


def session_status(cid: str) -> dict:
    """Check a sign-in without opening or navigating Chrome.

    Ask the user to confirm when the URL alone cannot show whether login worked.
    """
    import browser
    entry = spec(cid)
    if not entry or entry.get("kind") != "session":
        raise KeyError(cid)
    if entry.get("status") not in {"signing in", "confirm sign-in"}:
        return _entry(cid, entry)
    if time.time() - entry.get("started_at", time.time()) > 600:
        return _observe_mark(cid, entry, status="not connected", status_note="Sign-in was not completed. Open the browser to try again.")
    with _browser_lock:
        page = browser.status()
    if not page.get("running"):
        return _observe_mark(cid, entry, status="not connected", status_note="Olith's browser is closed. Open it to sign in.")
    landed = str(page.get("url") or "")
    where = urlsplit(landed)
    target = urlsplit(entry.get("check_url") or entry.get("sign_in_url") or "")
    on_service = (where.scheme == target.scheme and where.netloc == entry.get("signed_in_host")
                  and not _is_login_page(entry, landed) and page.get("ready_state") == "complete")
    if on_service:
        if entry.get("confirmation_required"):
            return _observe_mark(cid, entry, status="confirm sign-in", status_note="Can you see your signed-in account in Olith's browser? Confirm the browser session below. Service tools connect separately.")
        # Protected paths are specific; GitHub's public homepage proves nothing.
        path = target.path.rstrip("/")
        if not path or where.path == path or where.path.startswith(path + "/"):
            return _observe_mark(cid, entry, status="connected", verification="checked", checked_at=time.time(),
                         connected_at=time.time(), status_note="Browser session checked. Service tools connect separately.")
    elif entry.get("status") == "confirm sign-in":
        return _observe_mark(cid, entry, status="signing in", status_note="Return to your signed-in account in Olith's browser before confirming.")
    return _entry(cid, entry)


def confirm_sign_in(cid: str) -> dict:
    entry = spec(cid)
    if not entry or entry.get("status") != "confirm sign-in":
        raise ValueError("Check the browser session before confirming sign-in")
    # Re-check the current tab: a previous Check must not confirm a login page
    # or an unrelated website the browser has moved to in the meantime.
    current = session_status(cid)
    if current["status"] != "confirm sign-in":
        raise ValueError("Open your signed-in account in Olith's browser before confirming")
    return _mark(cid, status="connected", verification="user confirmed", connected_at=time.time(),
                 status_note="Browser session confirmed by you. This enables browser access with computer use on; service tools connect separately.")


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
    return _mark(cid, status="connected", has_secret=True, connected_at=time.time())


def connected() -> list[dict]:
    # Read on every prompt build, so it stays off the keyring, which can block on
    # a locked desktop. Status is all this needs.
    stored = _load().get("connections") or {}
    specs = [(cid, {**CATALOG.get(cid, {}), **entry}) for cid, entry in sorted(stored.items())]
    return [
        {"id": cid, "label": spec.get("label") or cid, "kind": spec.get("kind") or "session"}
        for cid, spec in specs
        if spec.get("status") == "connected"
    ]


def _is_login_page(spec: dict, url: str) -> bool:
    where = urlsplit(url)
    login = urlsplit(spec.get("sign_in_url") or "")
    home = spec.get("signed_in_host")
    # A host that exists only for signing in: every page on it is the login wall.
    if login.netloc and login.netloc != home and where.netloc == login.netloc:
        return True
    if where.netloc != home:
        return False
    # Same host as the service, so only the path can tell. "/" would match it all.
    paths = [spec.get("signed_out_path"), login.path if login.netloc == home else None]
    return any(
        p and p != "/" and (where.path == p or where.path.startswith(p.rstrip("/") + "/"))
        for p in paths
    )


def expired_sign_in_note(url: str) -> str | None:
    """Report an expired login to the model without changing saved connection status.

    A single page load is not enough to update that status.
    """
    for cid, entry in (_load().get("connections") or {}).items():
        spec = {**CATALOG.get(cid, {}), **entry}
        if spec.get("kind") == "session" and spec.get("status") == "connected" \
                and _is_login_page(spec, url):
            label = spec.get("label") or cid
            return (
                f"This is the {label} login page, so Olith's {label} sign-in has expired. "
                "Stop and ask the user to sign in again in the Olith browser window, then "
                "press Check in Connections. Never type a password yourself."
            )
    return None


def prompt_block(browser: bool = True) -> str:
    """What the model is told: which accounts it can act in, by name, and the
    route to each. `browser` is whether this chat has the browser tool."""
    live = connected()
    if not live:
        return ""
    sessions = [c["label"] for c in live if c["kind"] == "session"]
    keys = [c for c in live if c["kind"] != "session"]
    usable = [c for c in keys if c.get("id") in KEY_CONSUMERS]
    stored_only = [c["label"] for c in keys if c.get("id") not in KEY_CONSUMERS]
    lines = ["# Connections"]
    if sessions and browser:
        lines.append(
            f"Olith's browser is signed in to: {', '.join(sessions)}, as of the last check. "
            "Work in those accounts directly with the `browser` tool. If it lands on one "
            "of their login pages, that sign-in has expired: ask the user to sign in again."
        )
    elif sessions:
        lines.append(
            f"The user connected {', '.join(sessions)} in Olith's browser, which this chat "
            "cannot reach because computer use is off. If a task needs one of those "
            "accounts, tell them to turn computer use on in the model menu."
        )
    # Named, never valued, so a key cannot be repeated into a reply.
    for key in usable:
        lines.append(
            f"Olith holds a key for: {key['label']}. You do not see the key itself and "
            f"never need to: the `{KEY_CONSUMERS[key['id']]}` tool signs in with it."
        )
    if stored_only:
        lines.append(
            f"Olith holds a key for: {', '.join(stored_only)}, saved but not usable by any "
            "tool yet. If a task needs it, say so and do not look for the key."
        )
    lines.append(
        "Prefer a connected service tool or CLI over browser automation when it supports "
        "the requested operation. A browser session is a fallback, not an API connection."
    )
    lines.append(
        "Browser sessions do not authorize direct service tools. Those require their own "
        "connected tool integration; never infer tool access from a website login."
    )
    lines.append(
        "Anything not listed is not connected. Say so rather than trying to get past "
        "a login page, and never ask the user for a password."
    )
    return "\n".join(lines)
