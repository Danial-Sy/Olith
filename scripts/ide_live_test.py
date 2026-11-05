# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test the real backend, model and VS Code with format-on-save enabled.

Opens a VS Code window and checks saves whose formatting changes the proposed text.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = Path(__file__).resolve().parents[1]

CART = """function subtotal(items) {
  let total = 0;
  for (let i = 1; i < items.length; i++) {
    total += items[i].price;
  }
  return total;
}

function applyDiscount(amount, code) {
  if (code === "SAVE10") return amount * 0.1;
  if (code === "HALF") return amount / 2;
  return amount;
}

function shipping(amount) {
  return amount > 50 ? 5 : 0;
}

function total(items, code) {
  const discounted = applyDiscount(subtotal(items), code);
  return Math.round((discounted + shipping(discounted)) * 100) / 100;
}

module.exports = { subtotal, applyDiscount, shipping, total };
"""

TEST = """const assert = require("node:assert/strict");
const { subtotal, applyDiscount, shipping, total } = require("./cart");
const items = [{ price: 10, qty: 2 }, { price: 15, qty: 1 }, { price: 4, qty: 5 }];
assert.equal(subtotal(items), 55);
assert.equal(applyDiscount(100, "SAVE10"), 90);
assert.equal(applyDiscount(100, "HALF"), 50);
assert.equal(shipping(60), 0);
assert.equal(shipping(20), 5);
assert.equal(total(items), 55);
assert.equal(total([{ price: 20, qty: 1 }], "SAVE10"), 23);
console.log("ok");
"""

SETTINGS = {
    "editor.formatOnSave": True, "files.trimTrailingWhitespace": True, "files.insertFinalNewline": True,
    "editor.tabSize": 4, "editor.insertSpaces": True, "editor.detectIndentation": False,
    "[javascript]": {"editor.defaultFormatter": "vscode.typescript-language-features"},
}

TASK = ("node test.js fails. Fix the bugs in cart.js so node test.js passes. "
        "Do not change test.js. Run the test and tell me what you fixed.")


def wait_for(url, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except OSError:
            time.sleep(1)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen3.8:27b")
    parser.add_argument("--port", type=int, default=7880)
    parser.add_argument("--timeout", type=int, default=1500)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--settings", type=Path, help="Settings file for the backend, default this checkout's")
    parser.add_argument("--extension", type=Path, default=HERE / "extensions" / "olith-vscode")
    args = parser.parse_args()
    extension = args.extension.resolve()
    base = Path(tempfile.mkdtemp(prefix="olith-vscode-live-"))
    root = base / "project"; (root / ".vscode").mkdir(parents=True)
    (root / "cart.js").write_text(CART); (root / "test.js").write_text(TEST)
    (root / ".vscode" / "settings.json").write_text(json.dumps(SETTINGS, indent=2))
    url = f"http://127.0.0.1:{args.port}"
    (base / "live-config.json").write_text(json.dumps({"url": url, "model": args.model, "task": TASK}))
    server = subprocess.Popen([sys.executable, str(HERE / "scripts" / "ide_live_server.py"), "--port", str(args.port), "--model", args.model,
                               *(["--settings", str(args.settings)] if args.settings else [])],
                              stdout=open(base / "server.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    editor = None
    try:
        if not wait_for(url + "/api/health", 60):
            raise SystemExit("Backend did not start; see " + str(base / "server.log"))
        editor = subprocess.Popen(["code", "--wait", "--new-window", f"--user-data-dir={base / 'user-data'}", f"--extensions-dir={base / 'extensions'}",
                                   "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes",
                                   f"--extensionDevelopmentPath={extension}", f"--extensionTestsPath={extension / 'test' / 'live.js'}", str(root)],
                                  stdout=open(base / "code.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        results_file = base / "live-results.json"
        end = time.monotonic() + args.timeout
        while time.monotonic() < end and not results_file.exists():
            time.sleep(2)
        results = json.loads(results_file.read_text()) if results_file.exists() else {"finished": False, "error": "timed out"}
    finally:
        for process in (editor, server):
            if process and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        subprocess.run(["pkill", "-f", f"user-data-dir={base / 'user-data'}"], check=False)
    test = subprocess.run(["node", "test.js"], cwd=root, capture_output=True, text=True, timeout=30)
    cart = (root / "cart.js").read_text()
    events = results.get("events") or []
    summaries = [e.get("summary") or "" for e in events if e.get("type") == "tool_result"]
    report = {
        **results,
        "test_passes": test.returncode == 0, "test_output": (test.stdout + test.stderr)[-1500:],
        "test_unchanged": (root / "test.js").read_text() == TEST,
        "reformatted_by_vscode": "\n    let total" in cart or "\n    return" in cart,
        "tool_calls": sum(e.get("type") == "tool_start" for e in events),
        "failed_tool_results": sum(e.get("type") == "tool_result" and e.get("ok") is False for e in events),
        "save_mismatch_errors": sum("did not match" in s for s in summaries),
        "anchor_misses": sum("not found" in s.lower() for s in summaries),
        "cart_after": cart,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    keys = ("finished", "error", "elapsed_s", "tool_calls", "failed_tool_results", "save_mismatch_errors", "anchor_misses", "test_passes", "test_unchanged", "reformatted_by_vscode")
    print(json.dumps({k: report.get(k) for k in keys}), flush=True)
    shutil.rmtree(base / "user-data", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
