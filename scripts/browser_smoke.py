#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Drive a real headless Chrome through browser.py and check what happened.

The unit tests hold the decisions; this holds the protocol. Run it after
touching browser.py, or on a machine where the browser behaves differently:

    .venv/bin/python scripts/browser_smoke.py

It serves its own page, so nothing here depends on a site staying the same.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser

PAGE = """<!doctype html><meta charset="utf-8"><title>Smoke</title><main>
<h1>Report editor</h1><p>Body text the model should be able to read back.</p>
<label for="t">Title</label><input id="t" placeholder="Document title">
<button id="bold" aria-label="Bold">B</button>
<div id="doc" contenteditable style="min-height:60px;border:1px solid #999">edit me</div>
<button id="show" onclick="overlay.style.display='block'">Show banner</button>
<div id="overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5)"
     aria-label="Cookie banner">banner</div>
<p id="log"></p><script>
bold.onclick = () => log.textContent += 'BOLD ';
doc.addEventListener('keydown', e => {
  if (e.ctrlKey && e.altKey && e.key === '1') log.textContent += 'HEADING1 ';
});
</script></main>"""

PASS, FAIL = "  ok  ", "  FAIL"
failures = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global failures
    if not condition:
        failures += 1
    print(f"{PASS if condition else FAIL}  {name}{'  ' + detail if detail else ''}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="aether-smoke-"))
    (root / "page.html").write_text(PAGE, encoding="utf-8")
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *_args):  # the fixture's traffic is not the output
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/page.html"

    # Headless, and on its own port and profile, so a smoke run never touches
    # the browser Aether is actually using.
    browser.PORT = 9411
    browser.PROFILE = root / "profile"
    real_launch = browser._launch

    def headless_launch():
        if browser._port_open():
            return
        subprocess.Popen(
            [browser.binary(), "--headless=new", f"--remote-debugging-port={browser.PORT}",
             f"--user-data-dir={browser.PROFILE}", "--no-first-run", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            if browser._port_open():
                return
            time.sleep(0.2)
        raise RuntimeError("the browser did not open its debugging port")

    browser._launch = headless_launch
    call = lambda **a: json.loads(browser.run(a))
    try:
        started = time.time()
        check("the page loads", call(action="open", url=url).get("ok"))
        snap = call(action="snapshot")
        refs = {e["name"]: e["ref"] for e in snap.get("elements", [])}
        check("elements come back named", "Bold" in refs, str(sorted(refs)[:4]))

        call(action="fill", ref=refs["Title"], text="Local Models: A Report")
        check("fill puts the exact text in the field",
              browser.evaluate("document.getElementById('t').value") == "Local Models: A Report")

        call(action="click", ref=refs["Bold"])
        check("a click lands on the element it names",
              "BOLD" in browser.evaluate("document.getElementById('log').textContent"))

        call(action="click", ref=refs["edit me"])
        body = "One paragraph. " * 60
        call(action="type", text=body)
        landed = browser.evaluate("document.getElementById('doc').innerText")
        # Counted rather than matched as a substring: the insert goes in at the
        # caret, which is mid-word in the text the element already had.
        check("a long insert arrives whole",
              len(landed) - len("edit me") == len(body),
              f"{len(body)} sent, {len(landed) - len('edit me')} added")

        call(action="key", text="ctrl+alt+1")
        check("an application shortcut reaches the page",
              "HEADING1" in browser.evaluate("document.getElementById('log').textContent"))

        call(action="click", ref=refs["Show banner"])
        snap = call(action="snapshot")
        refs = {e["name"]: e["ref"] for e in snap.get("elements", [])}
        covered = call(action="click", ref=refs["Bold"])
        check("a covered element is refused, not clicked through",
              "behind something else" in (covered.get("error") or ""))

        stale = call(action="click", ref="e9999")
        check("a stale ref asks for a new snapshot",
              "new snapshot" in (stale.get("hint") or ""))

        check("the whole sequence is fast", time.time() - started < 20,
              f"{time.time() - started:.1f}s")
    finally:
        browser.close()
        browser._launch = real_launch
        server.shutdown()
    print("\n" + ("all checks passed" if not failures else f"{failures} check(s) failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
