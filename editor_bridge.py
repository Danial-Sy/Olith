# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pair local editors and check file revisions before sending edits.

Pairing codes are single-use and expire. Tokens stay in VS Code SecretStorage.
If the editor disconnects, do not fall back to writing the file directly.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from config import DATA

_lock = threading.RLock()
_codes: dict[str, float] = {}
_sessions: dict[str, dict] = {}
STORE = DATA / "editor-pairings.json"
_loaded = False
_local = threading.local()
EDIT_TIMEOUT = 90
PAIR_TTL = 120
ACTIVE_TTL = 15


def _load() -> None:
    global _loaded
    if _loaded:
        return
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"version": 1, "pairings": []}
    except (OSError, ValueError) as error:
        raise ValueError("Saved editor pairings could not be read") from error
    try:
        if data["version"] != 1 or not isinstance(data["pairings"], list):
            raise ValueError("Invalid pairing store")
        loaded = {}
        for item in data["pairings"]:
            ident, digest, root = item["id"], item["token_hash"], Path(item["root"])
            if not isinstance(ident, str) or len(ident) != 32 or not isinstance(digest, str) or len(digest) != 64 or not root.is_absolute():
                raise ValueError("Invalid pairing")
            int(ident, 16); int(digest, 16)
            loaded[ident] = {"id": ident, "token_hash": digest, "root": root, "pending": {},
                             "seen": time.monotonic(), "editor_seen": None, "version": None}
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Saved editor pairings are invalid") from error
    _sessions.update(loaded)
    _loaded = True


def _save() -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1, "pairings": [{"id": value["id"], "token_hash": value["token_hash"], "root": str(value["root"])} for value in _sessions.values()]}
    descriptor, name = tempfile.mkstemp(prefix=".editor-pairings-", suffix=".tmp", dir=STORE.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STORE)
    finally:
        temporary.unlink(missing_ok=True)


def pair_code() -> dict:
    with _lock:
        _codes.clear()
        code = secrets.token_hex(4).upper()
        _codes[code] = time.monotonic() + PAIR_TTL
    return {"code": code, "expires_in": PAIR_TTL}


def pair_status(code: str) -> dict:
    with _lock:
        expiry = _codes.get(code.strip().upper())
        if expiry is None:
            return {"status": "unavailable", "expires_in": 0}
        remaining = expiry - time.monotonic()
        if remaining <= 0:
            _codes.pop(code.strip().upper(), None)
            return {"status": "expired", "expires_in": 0}
        return {"status": "ready", "expires_in": math.ceil(remaining)}


def connect(code: str, root: Path) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Open a local project folder in VS Code")
    with _lock:
        _load()
        expiry = _codes.pop(code.strip().upper(), 0)
        if expiry <= time.monotonic():
            raise ValueError("Pair code expired or invalid; generate another in Olith")
        ident = secrets.token_hex(16)
        credential = secrets.token_urlsafe(32)
        _sessions[ident] = {"id": ident, "token_hash": hashlib.sha256(credential.encode()).hexdigest(), "root": root, "pending": {}, "seen": time.monotonic(), "editor_seen": time.monotonic(), "version": None}
        try:
            _save()
        except OSError:
            _sessions.pop(ident, None)
            raise
    return {"id": ident, "token": credential, "root": str(root)}


def session(ident: str, credential: str | None = None) -> dict:
    with _lock:
        _load()
        value = _sessions.get(ident)
        if not value:
            raise ValueError("VS Code pairing removed; pair again")
        if credential is not None and not secrets.compare_digest(value["token_hash"], hashlib.sha256(credential.encode()).hexdigest()):
            raise ValueError("Invalid editor credential")
        value["seen"] = time.monotonic()
        return value


def disconnect(ident: str) -> None:
    with _lock:
        _load()
        value = _sessions.pop(ident, None)
        if value:
            try:
                _save()
            except OSError:
                _sessions[ident] = value
                raise
        for request in (value or {}).get("pending", {}).values():
            request["error"] = "VS Code disconnected; file was not written by Olith"
            request["event"].set()


def pending(ident: str, version: str | None = None) -> list[dict]:
    with _lock:
        value = session(ident)
        value["editor_seen"] = time.monotonic()
        if version and len(version) <= 32 and all(c in "0123456789." for c in version):
            value["version"] = version
        return [dict(p["proposal"]) for p in value["pending"].values() if not p["event"].is_set()]


def forget_root(root: Path) -> None:
    root = root.resolve()
    with _lock:
        _load()
        for ident in [ident for ident, value in _sessions.items() if value["root"] == root]:
            disconnect(ident)


def status() -> list[dict]:
    with _lock:
        _load()
        now = time.monotonic()
        return [{"root": str(value["root"]), "connected": value["editor_seen"] is not None and now - value["editor_seen"] < ACTIVE_TTL,
                 "version": value.get("version")} for value in _sessions.values()]


def acknowledge(ident: str, edit: str, ok: bool, error: str = "") -> None:
    with _lock:
        request = session(ident)["pending"].get(edit)
        if not request or request["event"].is_set():
            raise ValueError("Edit expired or already acknowledged")
        if not ok:
            request["error"] = error[:1000] or "VS Code refused the edit"
        request["event"].set()


def claim(ident: str, edit: str) -> dict:
    with _lock:
        request = session(ident)["pending"].get(edit)
        if not request or request["event"].is_set() or request.get("claimed"):
            raise ValueError("Edit expired, cancelled, or already claimed")
        request["claimed"] = True
        return dict(request["proposal"])


def cancel_pending(ident: str) -> None:
    with _lock:
        for request in session(ident)["pending"].values():
            # Finish a claimed save before the final snapshot releases the lock.
            if not request.get("claimed"):
                request["error"] = "Task stopped before VS Code applied the edit"
                request["event"].set()


@contextlib.contextmanager
def writing(ident: str | None, baseline: tuple | None = None):
    previous = getattr(_local, "session", None)
    previous_baseline = getattr(_local, "baseline", None)
    _local.session = ident
    _local.baseline = baseline
    try:
        yield
    finally:
        _local.session = previous
        _local.baseline = previous_baseline


def take_reformatted() -> bool:
    """True once if the last editor save came back reformatted."""
    value = getattr(_local, "reformatted", False)
    _local.reformatted = False
    return value


def replace(path: Path, content: str) -> bool:
    ident = getattr(_local, "session", None)
    if not ident:
        return False
    value = session(ident)
    path = path.resolve()
    if not path.is_relative_to(value["root"]) or ".git" in path.relative_to(value["root"]).parts:
        raise ValueError("VS Code edits must stay inside the paired workspace")
    before = path.read_bytes() if path.exists() else None
    baseline = getattr(_local, "baseline", None)
    if baseline and (baseline[0] != path or baseline[1] != before):
        raise ValueError("File changed while the tool prepared its edit; read current source and retry")
    if before is not None:
        before.decode("utf-8")  # Refuse binary/non-UTF8 editing.
    ident_edit = secrets.token_hex(16)
    proposal = {"id": ident_edit, "path": path.relative_to(value["root"]).as_posix(),
                "before_sha256": hashlib.sha256(before).hexdigest() if before is not None else None,
                "content": content, "expires_at": time.time() + EDIT_TIMEOUT}
    request = {"proposal": proposal, "event": threading.Event(), "error": ""}
    with _lock:
        value["pending"][ident_edit] = request
    try:
        if not request["event"].wait(EDIT_TIMEOUT):
            raise ValueError("VS Code did not acknowledge the edit; reconnect before retrying")
        if request["error"]:
            raise ValueError(request["error"])
        if not path.is_file():
            raise ValueError("VS Code did not save the file; inspect the workspace")
        # Format-on-save and newline settings change the bytes. The edit still landed.
        if path.read_bytes() != content.encode("utf-8"):
            _local.reformatted = True
        return True
    finally:
        with _lock:
            value["pending"].pop(ident_edit, None)
