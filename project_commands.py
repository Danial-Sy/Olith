# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Evidence-based project verification commands with persistent manual overrides."""
from __future__ import annotations

import hashlib
import ast
import json
import os
import re
import shlex
import sys
import threading
from pathlib import Path
import tomllib

from config import DATA
from repo_index import ProjectIndex

COMMAND_DIR = DATA / "project-commands"
KINDS = {"test", "build", "typecheck"}
_lock = threading.RLock()


def _path(root):
    key = hashlib.sha256(os.fsencode(Path(root).resolve())).hexdigest()[:24]
    return COMMAND_DIR / (key + ".json")


def _read(root):
    try:
        return json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"discovered": [], "overrides": []}


def _save(root, data):
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def quote(value):
    if os.name == "nt":
        return "'" + str(value).replace("'", "''") + "'"
    return shlex.quote(str(value))


def discover(root: Path, *, refresh=True) -> list[dict]:
    root = Path(root).resolve()
    index = ProjectIndex(root)
    if refresh:
        index.refresh()
    with index.db() as db:
        paths = {r[0] for r in db.execute("SELECT path FROM files")}
    found = {}

    def add(kind, command, cwd=".", evidence="", script=""):
        ident = cwd + ":" + kind
        found.setdefault(ident, {"id": ident, "kind": kind, "command": command, "cwd": cwd,
                                 "source": "discovered", "evidence": evidence, "script": script,
                                 "enabled": True, "timeout_sec": 120})

    for rel in sorted(paths, key=lambda p: (p.count("/"), p)):
        path = root / rel
        cwd = path.parent.relative_to(root).as_posix()
        if path.name == "package.json":
            try:
                package = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            scripts = package.get("scripts") or {}
            manager = "pnpm" if (path.parent / "pnpm-lock.yaml").exists() else "yarn" if (path.parent / "yarn.lock").exists() else "bun" if (path.parent / "bun.lock").exists() else "npm"
            for kind, choices in {"test": ("test:ci", "test:unit", "test"), "build": ("build",),
                                  "typecheck": ("typecheck", "type-check", "check:types")}.items():
                name = next((n for n in choices if isinstance(scripts.get(n), str) and scripts[n].strip()
                             and "no test specified" not in scripts[n].lower()
                             and not re.search(r"--watch(?:All)?(?:\s|$|=true)", scripts[n])), None)
                if name:
                    raw = scripts[name]
                    command = f"{manager} run {name}"
                    if "vitest" in raw and not re.search(r"\bvitest\s+run\b", raw):
                        command += " --run" if manager == "yarn" else " -- --run"
                    add(kind, command, cwd, rel + ": scripts." + name, raw)
            if "typecheck" not in {v["kind"] for v in found.values() if v["cwd"] == cwd} and (path.parent / "tsconfig.json").is_file():
                binary = path.parent / "node_modules" / ".bin" / ("tsc.cmd" if os.name == "nt" else "tsc")
                if binary.is_file():
                    add("typecheck", quote(binary) + " --noEmit", cwd, rel + " + tsconfig.json")
        elif path.name == "Cargo.toml":
            for kind, args in {"test": "test", "build": "build", "typecheck": "check"}.items():
                add(kind, "cargo " + args, cwd, rel)
        elif path.name == "go.mod":
            for kind, args in {"test": "test ./...", "build": "build ./...", "typecheck": "vet ./..."}.items():
                add(kind, "go " + args, cwd, rel)
        elif path.name in {"Makefile", "makefile"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            for kind, names in {"test": ("test", "check"), "build": ("build", "all"), "typecheck": ("typecheck", "type-check")}.items():
                name = next((n for n in names if re.search(r"^" + re.escape(n) + r"\s*:", text, re.M)), None)
                if name:
                    add(kind, "make " + name, cwd, rel + ": " + name)

    python = next((p for p in (root / ".venv/Scripts/python.exe", root / ".venv/bin/python") if p.is_file()), Path(sys.executable))
    py = ("& " if os.name == "nt" else "") + quote(python)
    pyproject = {}
    if "pyproject.toml" in paths:
        try:
            pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if pyproject.get("build-system"):
            add("build", py + " -m build", evidence="pyproject.toml: build-system")
    test_paths = sorted(p for p in paths if p.endswith(".py") and (Path(p).name.startswith("test_") or Path(p).name.endswith("_test.py")))
    test_modules = set()
    for rel in test_paths[:20]:
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                test_modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                test_modules.add(node.module.split(".")[0])
    if test_paths:
        if "pytest" in test_modules or "pytest" in (pyproject.get("tool") or {}):
            add("test", py + " -m pytest", evidence=test_paths[0] + ": pytest")
        elif "unittest" in test_modules:
            folder = "tests" if any(p.startswith("tests/") for p in test_paths) else "."
            add("test", py + " -m unittest discover -s " + quote(folder), evidence=test_paths[0] + ": unittest")
    if "pyrightconfig.json" in paths or "pyright" in (pyproject.get("tool") or {}):
        add("typecheck", py + " -m pyright", evidence="pyright configuration")
    with _lock:
        data = _read(root)
        data["discovered"] = list(found.values())
        _save(root, data)
        return effective(data)


def effective(data):
    commands = {c["id"]: c for c in data.get("discovered", [])}
    for command in data.get("learned", []):
        commands.setdefault(command["id"], command)
    for command in data.get("overrides", []):
        commands.pop(command.get("replaces", command["id"]), None)
    commands.update({c["id"]: c for c in data.get("overrides", [])})
    return sorted(commands.values(), key=lambda c: (c["cwd"].count("/"), c["cwd"], c["kind"]))


def overrides(root: Path, commands: list[dict]) -> list[dict]:
    validated = []
    root = Path(root).resolve()
    for command in commands:
        kind = command.get("kind")
        cwd = str(command.get("cwd") or ".")
        resolved = (root / cwd).resolve()
        resolved.relative_to(root)
        text = str(command.get("command") or "").strip()
        if kind not in KINDS or not text or len(text) > 4000 or not resolved.is_dir():
            raise ValueError("Commands need a test/build/typecheck kind, a command, and a project directory")
        cwd = resolved.relative_to(root).as_posix()
        validated.append({"id": cwd + ":" + kind, "kind": kind, "cwd": cwd, "command": text,
                          "replaces": str(command.get("replaces") or command.get("id") or cwd + ":" + kind),
                          "source": "manual", "enabled": bool(command.get("enabled", True)),
                          "timeout_sec": max(10, min(int(command.get("timeout_sec", 120)), 600)), "evidence": "Manual override"})
    if len(validated) > 30:
        raise ValueError("At most 30 command overrides")
    with _lock:
        data = _read(root)
        data["overrides"] = validated
        _save(root, data)
        return effective(data)


def applicable(commands, changed):
    source = [p for p in changed if Path(p).suffix not in {".md", ".rst", ".txt"}]
    if not source:
        return []
    return [c for c in commands if c.get("enabled", True) and any((not c.get("paths") or p in c["paths"]) and (c["cwd"] == "." or p.startswith(c["cwd"] + "/")) for p in source)][:9]


def learn_build(root: Path, command: str) -> bool:
    """Remember an observed successful, project-local compiler invocation."""
    candidate = command.split("&&", 1)[0].strip()
    if any(token in candidate for token in ("|", ";", ">", "<", "\n", "`", "$")):
        return False
    try:
        args = shlex.split(candidate)
    except ValueError:
        return False
    if not args or Path(args[0]).name not in {"g++", "gcc", "clang++", "clang"}:
        return False
    root = Path(root).resolve()
    sources = []
    for arg in args[1:]:
        if arg.startswith("-") or Path(arg).suffix.lower() not in {".c", ".cc", ".cpp", ".cxx"}:
            continue
        path = (root / arg).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return False
        sources.append(path.relative_to(root).as_posix())
    if not sources:
        return False
    if "-o" in args:
        index = args.index("-o")
        if index + 1 >= len(args) or not (root / args[index + 1]).resolve().is_relative_to(root):
            return False
    if any(arg.startswith("-o") and len(arg) > 2 and not (root / arg[2:]).resolve().is_relative_to(root) for arg in args[1:]):
        return False
    with _lock:
        data = _read(root)
        data["learned"] = [{"id": ".:build", "kind": "build", "command": candidate, "cwd": ".",
                            "source": "learned", "evidence": "Successful project-local compiler command", "paths": sources,
                            "enabled": True, "timeout_sec": 120}]
        _save(root, data)
    return True


def shell_command(root, command):
    if command["cwd"] == ".":
        return command["command"]
    prefix = "Set-Location -LiteralPath " if os.name == "nt" else "cd "
    return prefix + quote(Path(root) / command["cwd"]) + ("; if ($?) { " + command["command"] + " }" if os.name == "nt" else " && " + command["command"])


def prompt(commands):
    if not commands:
        return ""
    return "# Project verification commands\nDiscovered from project configuration; manual overrides take precedence. Olith runs applicable checks before the final reply.\n" + "\n".join(
        f"{c['kind']} (cwd {c['cwd']}): {c['command']}" for c in commands if c.get("enabled", True))
