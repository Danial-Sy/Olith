# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Browser control that does not go through pixels.

Chrome over the DevTools protocol. Elements come back as refs with roles and
names, text comes back as text, and typing is one atomic insert, so nothing
inside a page has to be aimed at. The desktop tool stays for everything that is
not a browser.

Aether drives its own Chrome profile in data/browser rather than the window the
user has open. Sign in there once and it stays signed in.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import guards
from config import DATA

PROFILE = DATA / "browser"
PORT = 9422
# Chrome under another name on other distributions. First one that exists wins.
CHROME_BINARIES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "brave-browser", "microsoft-edge",
)
# A snapshot is meant to be read by a model, so it is capped like any other
# tool result rather than handing back the whole DOM of a web application.
MAX_ELEMENTS = 120
MAX_TEXT = 6000
NAV_TIMEOUT = 20.0

_lock = threading.RLock()
_ws: Any = None
_proc: subprocess.Popen | None = None
# The tab Aether drives. Pinned so it cannot wander onto one of the user's.
_target_id: str | None = None
# Advances whenever an action changes the page. The loop's duplicate-call guard
# folds it in, so a click that worked makes the next identical click a new call.
_state_id = 0


def state_id() -> int:
    return _state_id


def _moved() -> None:
    global _state_id
    _state_id += 1
_next_id = 1


def binary() -> str | None:
    for name in CHROME_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def available() -> tuple[bool, str]:
    if binary() is None:
        return False, (
            "no Chrome-family browser is installed. Install Chrome or Chromium, "
            "or use the computer tool to drive the browser on screen."
        )
    try:
        from websockets.sync.client import connect  # noqa: F401
    except Exception as e:
        return False, f"the websockets package is missing ({e})"
    return True, ""


# --------------------------------------------------------------------------
# The connection
# --------------------------------------------------------------------------

def _port_open(port: int | None = None) -> bool:
    # Read at call time, not bound as a default: a default freezes the value at
    # import and the port then cannot be changed by anything, tests included.
    port = PORT if port is None else port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _targets() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=5) as r:
        return json.load(r)


def _launch() -> None:
    """Start Chrome with the debugging port open, or reuse one already up."""
    global _proc
    if _port_open():
        return
    exe = binary()
    if exe is None:
        raise RuntimeError("no Chrome-family browser installed")
    PROFILE.mkdir(parents=True, exist_ok=True)
    _proc = subprocess.Popen(
        [
            exe,
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            # Signing in to a site is the point. Signing in to Chrome itself
            # is not: it pulls in sync, profile prompts and restored tabs.
            "--disable-sync",
            "--disable-features=SigninInterceptBubble,SyncPromoOnSignin,ChromeSigninInterceptBubble",
            # Aether's window, not a hijack of the user's session.
            "--new-window",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ},
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if _port_open():
            return
        time.sleep(0.2)
    raise RuntimeError("the browser did not open its debugging port")


def _page() -> dict:
    """The tab Aether drives, created if it does not have one yet.

    Its own tab, not whichever page is first, so a run cannot navigate one of
    the user's.
    """
    global _target_id
    pages = [t for t in _targets() if t.get("type") == "page"
             and not str(t.get("url", "")).startswith("devtools://")]
    if _target_id:
        mine = [t for t in pages if t.get("id") == _target_id]
        if mine:
            return mine[0]
        # The tab was closed. The connection to it is dead too.
        _target_id = None
        _drop()
    if not pages:
        raise RuntimeError("the browser has no open page")
    page = pages[0]
    _target_id = page.get("id")
    return page


def _connect() -> Any:
    global _ws
    from websockets.sync.client import connect

    if _ws is not None:
        return _ws
    _launch()
    _ws = connect(_page()["webSocketDebuggerUrl"], max_size=32_000_000,
                  open_timeout=15, close_timeout=2)
    return _ws


def _drop() -> None:
    global _ws
    ws, _ws = _ws, None
    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass


def cmd(method: str, **params: Any) -> dict:
    """One DevTools call. Events arriving in between are not ours; skip them."""
    global _next_id
    with _lock:
        ws = _connect()
        ident = _next_id
        _next_id += 1
        try:
            ws.send(json.dumps({"id": ident, "method": method, "params": params}))
            deadline = time.time() + NAV_TIMEOUT
            while time.time() < deadline:
                message = json.loads(ws.recv(timeout=NAV_TIMEOUT))
                if message.get("id") != ident:
                    continue
                if message.get("error"):
                    raise RuntimeError(message["error"].get("message") or "devtools error")
                return message.get("result") or {}
            raise RuntimeError(f"{method} timed out")
        except Exception:
            # A closed socket is the usual cause, and the next call should
            # reconnect rather than inherit a dead one.
            _drop()
            raise


def evaluate(expression: str, await_promise: bool = False) -> Any:
    result = cmd(
        "Runtime.evaluate",
        expression=expression,
        returnByValue=True,
        awaitPromise=await_promise,
    )
    if result.get("exceptionDetails"):
        detail = result["exceptionDetails"]
        raise RuntimeError(
            (detail.get("exception") or {}).get("description") or detail.get("text") or "script error"
        )
    return (result.get("result") or {}).get("value")


def close() -> dict:
    """Shut the browser Aether opened. One it merely attached to is left alone."""
    global _proc, _target_id
    _drop()
    _target_id = None
    proc, _proc = _proc, None
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return {"ok": True, "closed": proc is not None}


def status() -> dict:
    ok, why = available()
    out = {"available": ok, "running": _port_open(), "profile": str(PROFILE)}
    if not ok:
        out["reason"] = why
    if out["running"]:
        try:
            page = _page()
            out["url"] = page.get("url")
            out["title"] = page.get("title")
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------
# Looking at the page
# --------------------------------------------------------------------------

# Everything a person could act on. Roles carry the rest: a div with
# role="button" is a button however it was built.
_SNAPSHOT_JS = """
(() => {
  const MAX = %d;
  const sel = 'a,button,input,select,textarea,summary,label,[role],[onclick],' +
              '[contenteditable],[tabindex]:not([tabindex="-1"])';
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 90);
  const nameOf = (el) => clean(
    el.getAttribute('aria-label') ||
    (el.labels && el.labels[0] ? el.labels[0].innerText : '') ||
    el.getAttribute('title') || el.getAttribute('placeholder') ||
    el.getAttribute('alt') || el.innerText || el.value ||
    el.getAttribute('name') || ''
  );
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) continue;
    // A label and the field it names are two refs for one thing, and the model
    // picks whichever it reads first. Keep the field: it is what you act on.
    if (el.tagName === 'LABEL' && el.control && el.control.getBoundingClientRect().width > 1) continue;
    const name = nameOf(el);
    const role = el.getAttribute('role') || el.tagName.toLowerCase();
    if (!name && !['input', 'textarea', 'select'].includes(el.tagName.toLowerCase())) continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-aether-ref', ref);
    const item = {ref, role, name};
    if (el.type === 'password') { item.secret = true; item.name = name || 'password'; }
    if (el.value !== undefined && el.value !== '') item.value = clean(String(el.value));
    if (el.disabled) item.disabled = true;
    if (el.checked !== undefined && el.type === 'checkbox') item.checked = !!el.checked;
    const inView = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    if (!inView) item.offscreen = true;
    out.push(item);
    if (out.length >= MAX) break;
  }
  const signIn = !!document.querySelector('input[type="password"]');
  return {url: location.href, title: document.title, elements: out, signIn,
          focused: document.activeElement ? (document.activeElement.getAttribute('data-aether-ref') ||
                   document.activeElement.tagName.toLowerCase()) : null};
})()
""" % MAX_ELEMENTS

_TEXT_JS = """
(() => {
  const main = document.querySelector('main,article,[role="main"]') || document.body;
  return {url: location.href, title: document.title,
          canvas: !!document.querySelector('canvas'),
          text: (main.innerText || '').replace(/\\n{3,}/g, '\\n\\n').slice(0, %d)};
})()
""" % MAX_TEXT

# The point of a ref is that the click lands on the thing the model named, so
# the box is read back and what is actually on top of it is checked first.
_LOCATE_JS = """
(() => {
  const el = document.querySelector('[data-aether-ref="%s"]');
  if (!el) return {missing: true};
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  const mine = (node) => node && (node === el || el.contains(node) || node.contains(el));
  // A widget whose own wrapper sits over its middle is not covered. Only when
  // no point on it is reachable is something really on top of it.
  const spots = [[0.5,0.5],[0.5,0.25],[0.5,0.75],[0.25,0.5],[0.75,0.5],[0.12,0.5],[0.88,0.5]];
  let blocker = null;
  for (const [fx, fy] of spots) {
    const x = r.left + r.width * fx, y = r.top + r.height * fy;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
    const top = document.elementFromPoint(x, y);
    if (mine(top)) {
      return {x, y, w: r.width, h: r.height, covered: false,
              name: (el.getAttribute('aria-label') || el.innerText || '')
                      .replace(/\\s+/g, ' ').trim().slice(0, 60)};
    }
    blocker = blocker || top;
  }
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height,
          covered: true,
          over: blocker ? (blocker.getAttribute('aria-label') || blocker.getAttribute('role') ||
                           blocker.tagName.toLowerCase()) : null,
          name: (el.getAttribute('aria-label') || el.innerText || '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 60)};
})()
"""

# Clearing and notifying are separate steps, with the text going in between
# through the browser's own input path. A framework watching for real input
# events ignores a value assigned to the element.
_CLEAR_JS = """
(() => {
  const el = document.querySelector('[data-aether-ref="%s"]');
  if (!el) return false;
  el.focus();
  if (el.value !== undefined) el.value = '';
  else if (el.isContentEditable) el.textContent = '';
  return true;
})()
"""

_NOTIFY_JS = """
(() => {
  const el = document.querySelector('[data-aether-ref="%s"]');
  if (!el) return false;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
})()
"""

_IS_SECRET_JS = """
(() => {
  const el = document.querySelector('[data-aether-ref="%s"]');
  return !!el && (el.type === 'password' || el.autocomplete === 'current-password');
})()
"""

_KEYS = {
    "enter": ("Enter", "Enter", 13), "return": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9), "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27), "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46), "space": (" ", "Space", 32),
    "up": ("ArrowUp", "ArrowUp", 38), "down": ("ArrowDown", "ArrowDown", 40),
    "left": ("ArrowLeft", "ArrowLeft", 37), "right": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36), "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33), "pagedown": ("PageDown", "PageDown", 34),
}
_MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "super": 4,
              "cmd": 4, "win": 4, "shift": 8}


def _key_spec(part: str) -> tuple[str, str, int]:
    lowered = part.lower()
    if lowered in _KEYS:
        return _KEYS[lowered]
    if lowered.startswith("f") and lowered[1:].isdigit() and 1 <= int(lowered[1:]) <= 12:
        n = int(lowered[1:])
        return (f"F{n}", f"F{n}", 111 + n)
    if len(part) == 1:
        ch = part
        if ch.isalpha():
            return (ch.lower(), f"Key{ch.upper()}", ord(ch.upper()))
        if ch.isdigit():
            return (ch, f"Digit{ch}", ord(ch))
        return (ch, "", ord(ch))
    return (part, "", 0)


# --------------------------------------------------------------------------
# Acting on the page
# --------------------------------------------------------------------------

ACTIONS = ("open", "snapshot", "read", "click", "fill", "type", "key",
           "scroll", "back", "close", "status")


def _wait_loaded(timeout: float = NAV_TIMEOUT) -> str:
    deadline = time.time() + timeout
    state = "unknown"
    while time.time() < deadline:
        try:
            state = evaluate("document.readyState")
        except Exception:
            state = "unknown"
        if state == "complete":
            return state
        time.sleep(0.25)
    return state


def _mouse(x: float, y: float, button: str = "left", clicks: int = 1) -> None:
    for kind in ("mousePressed", "mouseReleased"):
        cmd("Input.dispatchMouseEvent", type=kind, x=x, y=y, button=button,
            clickCount=clicks, buttons=1 if button == "left" else 2)


def _send_key(combo: str) -> None:
    parts = [p for p in str(combo).replace(" ", "").split("+") if p]
    if not parts:
        return
    *mods, final = parts
    mask = 0
    for m in mods:
        mask |= _MODIFIERS.get(m.lower(), 0)
    key, code, vk = _key_spec(final)
    base = {"modifiers": mask, "key": key, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    # A plain printable key needs the char event too, or the page sees the
    # keydown and never receives the character.
    kind = "keyDown" if (not mask and len(key) == 1) else "rawKeyDown"
    cmd("Input.dispatchKeyEvent", type=kind, text=key if kind == "keyDown" else "", **base)
    cmd("Input.dispatchKeyEvent", type="keyUp", **base)


def run(args: dict) -> str:
    """One browser action. Returns JSON; never a screenshot."""
    args = args or {}
    action = str(args.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return json.dumps({
            "error": f"Unknown browser action {action!r}",
            "hint": f"Use one of: {', '.join(ACTIONS)}",
        })
    ok, why = available()
    if not ok:
        return json.dumps({"error": f"Browser control is unavailable: {why}"})

    # Same document, same reach, same rules as the desktop tool.
    refusal = guards.check(action, args, count=int(args.get("count") or 1))
    if refusal:
        return json.dumps(refusal)

    try:
        if action == "status":
            return json.dumps(status())
        if action == "close":
            return json.dumps(close())
        if action == "open":
            url = str(args.get("url") or "").strip()
            if not url:
                return json.dumps({"error": "open needs a url"})
            if not url.startswith(("http://", "https://", "about:", "file://")):
                url = "https://" + url
            cmd("Page.navigate", url=url)
            _moved()
            state = _wait_loaded()
            page = evaluate("({url: location.href, title: document.title})") or {}
            if str(page.get("url", "")).startswith("chrome-error://"):
                # Chrome serves its own error page and reports it as loaded,
                # so "ok" here would let the model believe it reached the site.
                return json.dumps({
                    "error": f"{url} did not load.",
                    "hint": "Check the address, or whether the machine is online.",
                })
            return json.dumps({"ok": True, "action": "open", "state": state, **page,
                               "next": "Call snapshot to see what is on it."})
        if action == "snapshot":
            snap = evaluate(_SNAPSHOT_JS)
            elements = (snap or {}).get("elements") or []
            out = {
                "ok": True, "url": snap.get("url"), "title": snap.get("title"),
                "focused": snap.get("focused"), "elements": elements,
                "note": (
                    f"{len(elements)} elements, capped at {MAX_ELEMENTS}. Act on one by "
                    "its ref; refs are only valid until the page changes."
                ),
            }
            if snap.get("signIn"):
                # This profile is signed in to nothing until the user signs it
                # in, and saying so beats a run trying to get past the wall.
                out["sign_in_required"] = (
                    "This page is asking for a password. Aether's browser has its own "
                    "profile and does not share the session in the browser the user "
                    "already has open. Stop here and tell them to sign in once in the "
                    "Aether browser window; it is remembered after that. Never type "
                    "someone's password yourself."
                )
            return json.dumps(out)
        if action == "read":
            page = evaluate(_TEXT_JS) or {}
            if page.get("canvas") and len((page.get("text") or "").strip()) < 200:
                # Google Docs and its like draw the document rather than
                # writing it into the page, so there is nothing here to read.
                page["cannot_read"] = (
                    "This page draws its content on a canvas, so its text is not in the "
                    "page and cannot be read here. The toolbar and menus still are: use "
                    "snapshot for those. To see the document itself, use the computer "
                    "tool's screenshot with a region around it."
                )
            return json.dumps({"ok": True, **page})
        if action == "back":
            evaluate("history.back()")
            _moved()
            _wait_loaded(5)
            return json.dumps({"ok": True, "url": evaluate("location.href")})
        if action == "scroll":
            amount = int(args.get("amount") or 3) * 120
            if str(args.get("direction") or "down").lower() == "up":
                amount = -amount
            evaluate(f"window.scrollBy(0, {amount})")
            _moved()
            return json.dumps({"ok": True, "scrolled": amount})
        if action == "type":
            text = str(args.get("text") or "")
            if not text:
                return json.dumps({"error": "type needs text"})
            # One insert, not one keystroke per character: this is the half of
            # the desktop path that lost a quarter of a paragraph.
            cmd("Input.insertText", text=text)
            _moved()
            return json.dumps({"ok": True, "action": "type", "characters": len(text)})
        if action == "key":
            combo = str(args.get("text") or args.get("key") or "")
            if not combo:
                return json.dumps({"error": "key needs the combination in text"})
            count = max(1, min(10, int(args.get("count") or 1)))
            for _ in range(count):
                _send_key(combo)
                time.sleep(0.03)
            _moved()
            return json.dumps({"ok": True, "action": "key", "pressed": combo, "count": count})

        # The two that need a ref.
        ref = str(args.get("ref") or "").strip()
        if not ref:
            return json.dumps({
                "error": f"{action} needs a ref",
                "hint": "Take a snapshot and use the ref of the element you mean.",
            })
        found = evaluate(_LOCATE_JS % ref.replace('"', ""))
        if not found or found.get("missing"):
            return json.dumps({
                "error": f"No element {ref} on this page.",
                "hint": "The page changed, so the refs did. Take a new snapshot.",
            })
        if found.get("covered"):
            # A ref means the click lands on the thing it names, so a covered
            # element is refused rather than clicked through.
            return json.dumps({
                "error": f"{ref} is behind something else ({found.get('over')}).",
                "hint": "Deal with what is on top of it first, then snapshot again.",
            })
        if action == "click":
            _mouse(found["x"], found["y"])
            _moved()
            time.sleep(0.2)
            return json.dumps({"ok": True, "action": "click", "ref": ref,
                               "clicked": found.get("name"), "url": evaluate("location.href")})
        if action == "fill":
            text = str(args.get("text") or "")
            if evaluate(_IS_SECRET_JS % ref.replace('"', "")):
                return json.dumps({
                    "error": "Refused: that is a password field.",
                    "hint": (
                        "Credentials are the user's to type. Tell them to sign in "
                        "themselves in the Aether browser window."
                    ),
                })
            _mouse(found["x"], found["y"])
            time.sleep(0.1)
            evaluate(_CLEAR_JS % ref.replace('"', ""))
            cmd("Input.insertText", text=text)
            evaluate(_NOTIFY_JS % ref.replace('"', ""))
            _moved()
            return json.dumps({"ok": True, "action": "fill", "ref": ref,
                               "characters": len(text)})
    except Exception as e:
        return json.dumps({"error": f"{action} failed: {e}"})
    return json.dumps({"error": f"{action} did nothing"})
