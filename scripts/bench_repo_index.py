# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Measure retrieval, optional CPU embeddings, and a trusted local Git-ref baseline."""
import argparse
import importlib.util
import json
import math
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import symbols
from ollama_client import estimate_tokens
from repo_index import OllamaEmbedder, ProjectIndex


def timed(function, repeats):
    durations, result = [], None
    for _ in range(repeats):
        started = time.perf_counter()
        result = function()
        durations.append((time.perf_counter() - started) * 1000)
    ordered = sorted(durations)
    return {"median_ms": round(statistics.median(ordered), 3),
            "p95_ms": round(ordered[math.ceil(.95 * len(ordered)) - 1], 3),
            "samples": len(ordered)}, result


def baseline(root, ref, folder):
    if ref.startswith("-"):
        raise ValueError("Use a local Git ref, such as v1.5.0")
    modules = {}
    for name in ("symbols", "agent"):
        source = subprocess.run(["git", "show", f"{ref}:{name}.py"], cwd=root,
                                capture_output=True, text=True, check=True).stdout
        path = folder / (name + ".py")
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("olith_bench_old_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules[name] = module
    modules["agent"].symbols = modules["symbols"]
    return modules


def rank(result, case):
    for n, hit in enumerate(result.get("hits", []), 1):
        file = hit.get("file") or hit.get("path") or ""
        name = hit.get("symbol") or hit.get("name") or ""
        if file == case.get("file") and (not case.get("symbol") or name == case["symbol"] or case["symbol"] in hit.get("text", "")):
            return n
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--query", default="code search")
    parser.add_argument("--cases", type=Path, help="JSON list with query, regex, file and symbol fields")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--baseline-ref", help="Execute read-only tools from this trusted local Git ref")
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--embedding-budget-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or args.embedding_budget_seconds < 1:
        parser.error("Repeats and embedding budget must be positive")
    root = Path(args.root).resolve()
    cases = json.loads(args.cases.read_text()) if args.cases else [{"query": args.query}]
    if not isinstance(cases, list) or not cases or any(not isinstance(c, dict) or not isinstance(c.get("query"), str) for c in cases):
        parser.error("Cases must be a nonempty list of query objects")
    report = {"platform": platform.platform(), "python": platform.python_version(),
              "baseline_ref": args.baseline_ref, "model_inference_benchmarked": False,
              "note": "Current Git-eligible text source for both tool versions; old regex uses a temporary source-only copy so ignored runtime data cannot inflate its time. Warm OS/filesystem cache. This is not a whole-task benchmark."}
    with tempfile.TemporaryDirectory(prefix="olith-index-bench-") as cache:
        folder = Path(cache)
        old = baseline(root, args.baseline_ref, folder) if args.baseline_ref else None
        index = ProjectIndex(root, folder / "index")
        report["empty_index"], cold = timed(index.refresh, 1)
        report["files"], report["chunks"] = cold["files"], cold["chunks"]
        old_root = folder / "source"
        if old:
            with index.db() as db:
                paths = [row[0] for row in db.execute("SELECT path FROM files")]
            for relative in paths:
                target = old_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / relative, target)
        report["unchanged_index"], warm = timed(index.refresh, args.repeats)
        report["warm_reparsed"] = warm["changed"]
        source_path = root / "server.py"
        if old and source_path.is_file():
            source = source_path.read_text()
            report["old_repeated_outline"], _ = timed(lambda: old["symbols"].outline(source, "server.py"), args.repeats)
            symbols.outline(source, "server.py")
            report["cached_repeated_outline"], _ = timed(lambda: symbols.outline(source, "server.py"), args.repeats)
        embedder = None
        if args.semantic:
            embedder = OllamaEmbedder({"embedding_model": "nomic-embed-text:latest", "embedding_device": "cpu"})
            started = last_progress = time.perf_counter()
            count = 0
            while time.perf_counter() - started < args.embedding_budget_seconds:
                done = index.embedding_batch(embedder)
                if not done:
                    break
                count += done
                if time.perf_counter() - last_progress >= 15:
                    print(f"Embedded {count}/{cold['chunks']} chunks in {time.perf_counter() - started:.1f}s", file=sys.stderr, flush=True)
                    last_progress = time.perf_counter()
            report["embedding_build"] = {"seconds": round(time.perf_counter() - started, 2),
                                         "chunks": count, "total_chunks": cold["chunks"], "complete": count == cold["chunks"],
                                         "device": "cpu", "model": embedder.model, "identity": embedder.identity()}
        rows = []
        for case in cases:
            row = {**case}
            row["lexical"], lexical = timed(lambda: index.search(case["query"], limit=8), args.repeats)
            row["lexical"]["expected_rank"] = rank(lexical, case)
            row["lexical"]["top_matches"] = [{"file": h["file"], "symbol": h["symbol"]} for h in lexical["hits"]]
            row["map"], context = timed(lambda: index.repo_map(case["query"], 1400), args.repeats)
            row["map"]["tokens_estimate"] = estimate_tokens(context)
            if case.get("symbol"):
                row["known_symbol"], result = timed(lambda: index.search(case["symbol"], "symbols", limit=8), args.repeats)
                row["known_symbol"]["expected_rank"] = rank(result, case)
            if old and case.get("regex"):
                row["old_regex"], result = timed(lambda: json.loads(old["agent"].run_tool("search_files", {"pattern": case["regex"]}, old_root)), args.repeats)
                row["old_regex"]["expected_rank"] = rank(result, case)
                row["old_regex"]["hits"] = len(result.get("hits", []))
            if embedder:
                row["semantic"], result = timed(lambda: index.search(case["query"], limit=8, embedder=embedder), args.repeats)
                row["semantic"]["expected_rank"] = rank(result, case)
                row["semantic"]["top_matches"] = [{"file": h["file"], "symbol": h["symbol"]} for h in result["hits"]]
                row["semantic"]["status"] = result["semantic"]
            rows.append(row)
        report["cases"] = rows
        report["summary"] = {}
        for mode in ("old_regex", "known_symbol", "lexical", "semantic", "map"):
            values = [r[mode] for r in rows if mode in r]
            if values:
                report["summary"][mode] = {"median_of_case_medians_ms": round(statistics.median(v["median_ms"] for v in values), 3),
                                           "expected_symbol_in_top_8": sum(v.get("expected_rank") is not None for v in values) if mode != "map" else None,
                                           "cases": len(values)}
        report["cache_bytes"] = sum(p.stat().st_size for p in (folder / "index").rglob("*") if p.is_file())
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
