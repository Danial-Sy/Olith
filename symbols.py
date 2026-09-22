# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Find one function or class in a file without reading the whole thing.

A symbol has a name the model already knows from the request, so it does not
have to guess line numbers. Python is parsed with `ast` and is exact.
JavaScript and TypeScript are brace-counted, and say so with `exact: false`.
"""
from __future__ import annotations

import ast
import re

PY_SUFFIXES = {".py", ".pyi"}
JS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}

# Bounded so a pathological file cannot make signature reconstruction run long.
MAX_SIGNATURE_LINES = 12


def language_for(path: str) -> str | None:
    dot = path.rfind(".")
    suffix = path[dot:].lower() if dot >= 0 else ""
    if suffix in PY_SUFFIXES:
        return "python"
    if suffix in JS_SUFFIXES:
        return "javascript"
    return None


def _signature(lines: list[str], start: int) -> str:
    """The declaration line, continued until it looks complete.

    Signatures wrap often, and one cut mid parameter list is worse than none.
    """
    out = []
    for offset in range(MAX_SIGNATURE_LINES):
        if start - 1 + offset >= len(lines):
            break
        line = lines[start - 1 + offset]
        out.append(line.strip())
        if line.rstrip().endswith((":", "{", ")")) and offset:
            break
        if line.rstrip().endswith(":") or "{" in line:
            break
    return " ".join(out).strip()


# ── python ────────────────────────────────────────────────────────────────

_PY_KINDS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _py_start(node) -> int:
    """Decorators belong to the definition; a copy without them is wrong."""
    lines = [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
    return min(lines)


def _py_entries(text: str) -> list[dict]:
    tree = ast.parse(text)
    lines = text.splitlines()
    out = []

    def record(node, prefix=""):
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        name = f"{prefix}{node.name}"
        start = _py_start(node)
        out.append({
            "name": name,
            "kind": kind,
            "line": start,
            "end_line": getattr(node, "end_lineno", start) or start,
            "signature": _signature(lines, node.lineno),
            "exact": True,
        })
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, _PY_KINDS):
                    record(child, f"{name}.")

    for node in tree.body:
        if isinstance(node, _PY_KINDS):
            record(node)
    return out


# ── javascript / typescript ───────────────────────────────────────────────

_JS_PATTERNS = (
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")),
)


def _js_end(lines: list[str], start_idx: int) -> int:
    """Brace-match forward from the declaration. Best effort by construction.

    Braces inside strings, template literals, regex literals and comments are
    not excluded. Getting that right needs a real parser, which is the
    dependency this module exists to avoid, so the result is flagged inexact
    rather than pretended to be reliable.
    """
    depth = 0
    seen = False
    for i in range(start_idx, len(lines)):
        for ch in lines[i]:
            if ch == "{":
                depth += 1
                seen = True
            elif ch == "}":
                depth -= 1
                if seen and depth <= 0:
                    return i + 1
        if seen and depth <= 0 and i > start_idx:
            return i + 1
    return len(lines)


def _js_entries(text: str) -> list[dict]:
    lines = text.splitlines()
    out = []
    for idx, line in enumerate(lines):
        for kind, pattern in _JS_PATTERNS:
            m = pattern.match(line)
            if not m:
                continue
            out.append({
                "name": m.group(1),
                "kind": kind,
                "line": idx + 1,
                "end_line": _js_end(lines, idx),
                "signature": line.strip()[:200],
                "exact": False,
            })
            break
    return out


# ── public ────────────────────────────────────────────────────────────────

def outline(text: str, path: str) -> list[dict] | None:
    """Every top-level definition, cheapest possible orientation for a file.

    None when the language is unsupported or the source does not parse, which
    the caller turns into a line-range fallback rather than an error.
    """
    lang = language_for(path)
    try:
        if lang == "python":
            return _py_entries(text)
        if lang == "javascript":
            return _js_entries(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return None


def find(text: str, path: str, name: str) -> dict | None:
    """One definition by name, with its source.

    Accepts `method` as well as `Class.method`: a model that has read a
    traceback knows the bare name and should not have to work out the class.
    """
    entries = outline(text, path)
    if not entries:
        return None
    wanted = (name or "").strip()
    match = next((e for e in entries if e["name"] == wanted), None)
    if match is None:
        match = next((e for e in entries if e["name"].split(".")[-1] == wanted), None)
    if match is None:
        return None
    lines = text.splitlines()
    body = lines[match["line"] - 1 : match["end_line"]]
    return {**match, "source": "\n".join(body)}


def names(text: str, path: str) -> list[str]:
    """What the file does define, for when the requested name is not there."""
    return [e["name"] for e in (outline(text, path) or [])]
