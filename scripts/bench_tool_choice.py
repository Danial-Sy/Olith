#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Compare tool-choice options against real models. All ship disabled in policy.py.

Compare each option with baseline. shipped should match baseline.

    python3 scripts/bench_tool_choice.py
    python3 scripts/bench_tool_choice.py llama3.2:3b

Requires Ollama. Does not write to the repo.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import config  # noqa: E402
import policy  # noqa: E402

HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
NUM_CTX = 8192

# Use clear requests with one expected tool. Avoid copying schema wording.
PROMPTS = [
    # Include more reading, searching and editing requests to match normal usage.
    ("what's in the src folder?", "list_dir"),
    ("show me the files in this directory", "list_dir"),
    ("list everything under tests/", "list_dir"),
    ("what files are in the project root?", "list_dir"),
    ("show me the contents of config.py", "read_file"),
    ("open README.md so I can see it", "read_file"),
    ("what does setup.py say?", "read_file"),
    ("print lines 40 through 80 of server.py", "read_file"),
    ("let me see the Dockerfile", "read_file"),
    ("read the first part of storage.py", "read_file"),
    ("where is parse_version defined in this repo?", "search_files"),
    ("find every place we call load_settings", "search_files"),
    ("grep for TODO comments across the project", "search_files"),
    ("which files mention OLLAMA_HOST?", "search_files"),
    ("locate the function that handles retries", "search_files"),
    ("search the codebase for 'deprecated'", "search_files"),
    ("make a new file called notes.txt containing 'hello'", "write_file"),
    ("create utils/math.py with an add function in it", "write_file"),
    ("write a fresh .gitignore for a python project", "write_file"),
    ("start a new file called LICENSE with the MIT text", "write_file"),
    ("change the timeout in server.py from 30 to 60", "edit_file"),
    ("rename the variable `foo` to `bar` in main.py", "edit_file"),
    ("fix the typo 'recieve' in docs.md", "edit_file"),
    ("update the version string in config.py to 2.0.0", "edit_file"),
    ("replace the old logging call in worker.py with logger.info", "edit_file"),
    ("in app.js, swap the hardcoded url for a constant", "edit_file"),
    ("delete the unused import at the top of models.py", "edit_file"),
    ("run the test suite", "run_shell"),
    ("what does `git log --oneline -5` say?", "run_shell"),
    ("install the requests package with pip", "run_shell"),
    ("build the project", "run_shell"),
    ("show me the python version", "run_shell"),
    ("how much disk space is free?", "run_shell"),
    ("check that the python file I just wrote parses", "validate_file"),
    ("does index.html have valid syntax?", "validate_file"),
    ("verify that settings.json is well formed", "validate_file"),
    ("is there anything uncommitted right now?", "git_status"),
    ("what branch am I on and what's changed?", "git_status"),
    ("add another line to the end of changelog.md", "append_file"),
    ("append a new entry to the bottom of TODO.md", "append_file"),
    ("do we have a rust compiler on this machine?", "check_toolchains"),
    ("is node installed, and java?", "check_toolchains"),
    ("remember that I prefer tabs over spaces", "remember"),
    ("note for later: this project deploys on Fridays", "remember"),
]


def post(path: str, payload: dict, timeout: float = 300.0) -> dict:
    req = urllib.request.Request(
        f"{HOST}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def installed_models() -> list[str]:
    try:
        return [m["name"] for m in post_get("/api/tags").get("models", [])]
    except Exception as e:
        sys.exit(f"Ollama not reachable at {HOST}: {e}")


def post_get(path: str) -> dict:
    with urllib.request.urlopen(f"{HOST}{path}", timeout=30) as r:
        return json.loads(r.read().decode())


def profile_for(model: str) -> dict:
    """Just the fields the policy reads, straight from Ollama."""
    show = post("/api/show", {"model": model})
    info = show.get("model_info") or {}
    return {
        "params": int(info.get("general.parameter_count") or 0),
        "tools": "tools" in (show.get("capabilities") or []),
    }


def ask(model: str, prompt: str, tools: list[dict]) -> tuple[str | None, bool]:
    """Return the chosen tool and whether its name was an alias. Use temperature 0 and a fixed seed."""
    system = config.SYSTEM_PROMPTS["agentic"] + agent.tools_system_addon(tools)
    body = post("/api/chat", {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "tools": tools,
        "stream": False,
        "options": {"temperature": 0, "seed": 1, "num_ctx": NUM_CTX},
    })
    msg = body.get("message") or {}
    raw = None
    for tc in msg.get("tool_calls") or []:
        raw = (tc.get("function") or {}).get("name")
        break
    calls = agent.extract_tool_calls(msg)
    if raw is None and calls:
        raw = calls[0]["name"]
    name = calls[0]["name"] if calls else None
    return name, bool(raw and name and raw != name)


# Benchmark variants only. Every size-based option stays off in policy.py.
REFERENCE_BUDGET = {"tiny": 7, "small": 9, "mid": 11, "full": None}


def variants(prof: dict) -> list[tuple[str, list[dict]]]:
    """Compare each option alone, then together, to show which change affects the result."""
    tier = policy.tier_for(prof)
    base = agent.TOOL_DEFS
    only = lambda **kw: {"tool_budget": None, "order_tools": False,  # noqa: E731
                         "lean_schemas": False, **kw}
    budget = REFERENCE_BUDGET.get(tier)
    combined = {"tool_budget": budget, "order_tools": False, "lean_schemas": tier != "full"}
    out = [
        ("baseline", base),
        # Disable context trimming to measure the model-size policy separately.
        ("shipped", agent.tool_defs_for({"delegate_available": True}, None, prof)),
        ("order", agent.apply_policy(base, only(order_tools=True))),
        ("budget", agent.apply_policy(base, only(tool_budget=budget))),
        ("lean", agent.apply_policy(base, only(lean_schemas=tier != "full"))),
        ("budget+lean", agent.apply_policy(base, combined)),
        ("all", agent.apply_policy(base, {**combined, "order_tools": True})),
    ]
    # Skip duplicate full-tier variants. Keep baseline and all as a repeatability check.
    seen: dict[str, str] = {}
    kept = []
    for name, tools in out:
        key = json.dumps(tools)
        if name in ("baseline", "shipped") or key not in seen:
            seen[key] = name
            kept.append((name, tools))
    return kept


def run(model: str) -> dict:
    prof = profile_for(model)
    pol = policy.policy_for(prof)
    vs = variants(prof)
    identical = all(json.dumps(t) == json.dumps(agent.TOOL_DEFS) for _, t in vs)

    print(f"\n{'=' * 96}\n{model}  |  {prof['params'] / 1e9:.1f}B  tier={pol['tier']}")
    print("  " + "   ".join(f"{name}:{len(t)}t/{agent.schema_tokens(t)}tok"
                            for name, t in vs))
    if identical:
        print("  payload is byte-identical across variants: every column must agree")
    print("=" * 96)
    header = "".join(f"{name:<15}" for name, _ in vs)
    print(f"{'prompt':<44}{'expect':<15}{header}")
    print("-" * 96)

    hits = {name: 0 for name, _ in vs}
    nocall = {name: 0 for name, _ in vs}
    aliased = 0

    # Run all 44 prompts for one variant before switching. Interleaving changed
    # results even for identical payloads. Warm up first so every variant starts
    # with a loaded model.
    try:
        ask(model, "hello", agent.TOOL_DEFS)
    except Exception:
        pass

    picks: dict[tuple[str, str], tuple] = {}
    for name, tools in vs:
        offered = {t["function"]["name"] for t in tools}
        for prompt, expected in PROMPTS:
            try:
                got, via_alias = ask(model, prompt, tools)
            except Exception as e:
                got, via_alias = f"<error {type(e).__name__}>", False
            picks[(name, prompt)] = (got, expected in offered)
            if got == expected:
                hits[name] += 1
            if got is None:
                nocall[name] += 1
            if via_alias:
                aliased += 1

    rows = [(prompt, expected, {name: picks[(name, prompt)] for name, _ in vs})
            for prompt, expected in PROMPTS]
    for prompt, expected, row in rows:
        cells = "".join(
            f"{('OK ' if g == expected else ('n/a' if not p else 'X  ')) + str(g or '-')[:11]:<15}"
            for g, p in (row[name] for name, _ in vs))
        print(f"{prompt[:42]:<44}{expected:<15}{cells}")

    n = len(PROMPTS)
    print("-" * 96)
    for name, tools in vs:
        avail = sum(1 for _, exp, row in rows if row[name][1])
        fair = sum(1 for _, exp, row in rows if row[name][1] and row[name][0] == exp)
        print(f"  {name:<10} {hits[name]}/{n} = {100 * hits[name] / n:3.0f}%    "
              f"of the {avail} whose right tool was present: {fair}/{avail} = "
              f"{100 * fair / max(avail, 1):3.0f}%    no call: {nocall[name]}")
    if aliased:
        print(f"  calls rescued by an alias: {aliased}")
    return {"model": model, "tier": pol["tier"], "n": n, "identical": identical,
            "hits": hits, "rows": rows, "vs": [name for name, _ in vs],
            "aliased": aliased,
            "fair": {name: (sum(1 for _, e, r in rows if r[name][1] and r[name][0] == e),
                            sum(1 for _, e, r in rows if r[name][1]))
                     for name, _ in vs}}


def main() -> int:
    wanted = sys.argv[1:] or [m for m in installed_models()
                              if not m.startswith("qwen2.5:0.5b")]
    results = []
    for model in wanted:
        started = time.time()
        results.append(run(model))
        print(f"({time.time() - started:.0f}s)")

    print(f"\n{'=' * 96}\nSUMMARY: raw accuracy, then accuracy over prompts whose "
          f"right tool was still offered\n{'=' * 96}")
    names = results[0]["vs"] if results else []
    print(f"{'model':<20}{'tier':<8}" + "".join(f"{n:<17}" for n in names))
    bad = 0
    for r in results:
        cells = ""
        for name in names:
            f_hit, f_n = r["fair"][name]
            cells += f"{r['hits'][name]}/{r['n']} ({f_hit}/{f_n}){'':<4}"[:17]
        print(f"{r['model']:<20}{r['tier']:<8}{cells}")
        if r["identical"] and len(set(r["hits"].values())) != 1:
            print(f"   REGRESSION: {r['model']} has an identical payload in every "
                  f"variant but did not score identically")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
