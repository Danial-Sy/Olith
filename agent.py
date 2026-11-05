# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Agentic tools: project-scoped file and shell operations."""
from __future__ import annotations

import difflib
import hashlib
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import browser
import computer
import connections
import policy as policy_mod
import symbols
import repo_index
import language_servers
from browser import ACTIONS as BROWSER_ACTIONS
from computer import ACTIONS as COMPUTER_ACTIONS
from config import DATA
from storage import load_settings

READ_FILE_CONTENT_MAX_CHARS = 7200
# Keep outlines short; leading parameters are enough to identify a definition.
OUTLINE_SIGNATURE_CHARS = 90
MAX_EDIT_RANGES = 64

# Every tool overwrite snapshots the previous contents here first.
BACKUP_DIR = DATA / "backups"
BACKUP_KEEP = 10
# Save large tool results to disk. Keep the head, tail and file path in the prompt.
TOOLOUT_DIR = DATA / "toolout"
TOOLOUT_KEEP = 60
# Save tool results above the render limit to disk.
OFFLOAD_CHARS = 8000
OFFLOAD_HEAD_CHARS = 3000
OFFLOAD_TAIL_CHARS = 1200
# Shell output is captured whole and offloaded, not sliced at capture time.
# This cap only guards against a runaway command eating memory.
SHELL_OUTPUT_HARD_CAP = 400_000
MAX_EDIT_TOTAL_CHARS = 120_000
MAX_WRITE_CHARS = 12_000
INTERNAL_VERIFICATION_COMMAND = "[Olith automatic project verification]"
REPLAY_ELISION_PREFIX = "[Olith replay elision:"

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders in a directory (relative to project root unless absolute).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            # Two sentences, and the length matters: a four-sentence version
            # cost qwen3-coder:30b two prompts, reproducibly.
            "description": (
                "Read a text file, one named function or class from it, or just its "
                "outline. Prefer symbol to a line range when you know the name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "symbol": {
                        "type": "string",
                        "description": (
                            "Function or class to return, e.g. parse_version or "
                            "Config.load. Parser-backed boundaries where supported."
                        ),
                    },
                    "outline": {
                        "type": "boolean",
                        "description": "List what the file defines instead of reading it.",
                    },
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "language_action": {"type": "string", "enum": ["diagnostics", "definition", "references", "hover", "rename"], "description": "Use the project's language server at start_line and character. Rename returns proposed edits."},
                    "character": {"type": "integer", "minimum": 0, "description": "Zero-based UTF-16 column for language navigation."},
                    "new_name": {"type": "string", "description": "New identifier for a language-server rename preview."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a file, or replace an existing one with complete new content. For a "
                "small change to an existing file, edit_file is cheaper. Python, JavaScript, "
                "JSON and HTML are syntax-checked automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": (
                            "Complete file content. A new file is at most 12,000 characters; "
                            "continue it with append_file. Replacing a file sends all of it."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "Add one coherent chunk to an existing text file without resending its earlier "
                "contents. Omit before to append at EOF. For a closed HTML document, set before "
                "to a closing marker such as </script>, </style>, or </body> so the chunk remains "
                "inside the document. Keep each chunk comfortably below the generation limit, "
                "then call validate_file after the final chunk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "maxLength": 12000,
                        "description": "One coherent source chunk, at most 12,000 characters.",
                    },
                    "before": {
                        "type": "string",
                        "description": (
                            "Optional marker. Insert immediately before its final occurrence "
                            "instead of appending at EOF."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Change an existing text file. PREFERRED: anchored edits. Give find (text "
                "copied verbatim from the file, including indentation) and replace. find must "
                "match exactly one place, so include enough surrounding lines to be unique. "
                "Anchored edits survive each other, so several can be batched and none of them "
                "go stale as the file changes. Line ranges (start_line/end_line) still work but "
                "refer to the file as it is right now, so every applied edit shifts the lines "
                "below it and forces a reread. Do not mix the two forms in one call. The result "
                "is syntax-checked before it replaces the original."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "find": {
                        "type": "string",
                        "description": "Exact text to locate, copied byte for byte from the file.",
                    },
                    "replace": {"type": "string", "description": "Text to put in its place."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "content": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                                "start_line": {"type": "integer", "minimum": 1},
                                "end_line": {"type": "integer", "minimum": 1},
                                "content": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search project files: regex by default; hybrid for concepts, symbols for definitions, references for identifier occurrences, map for dependencies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string", "description": "e.g. **/*.py"},
                    "path": {"type": "string", "description": "Subfolder to search"},
                    "mode": {"type": "string", "enum": ["regex", "hybrid", "symbols", "references", "map"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command inside the project root. Prefer non-interactive commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_sec": {"type": "integer", "default": 60},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_toolchains",
            "description": (
                "List which language toolchains are installed (Python, Node/TypeScript, Java, "
                "C/C++, Rust, Go, .NET, PHP, Ruby) and how to obtain missing ones. Use this for "
                "projects that require a compiler or external runtime; self-contained HTML and "
                "built-in validate_file checks do not need a separate toolchain probe."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_file",
            "description": (
                "Run a non-destructive syntax check on a Python, JavaScript, JSON, or HTML file. "
                "Use this instead of run_shell when verifying a file you just wrote."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Inspect Git or manage GitHub for the active repository, signed in as the connected GitHub account. Shell git may have no GitHub login, so use this for remote work instead of shell git push, gh, or looking for tokens. Olith handles mutation approvals according to the user's Approval mode setting. To publish a committed local repo, call create_repo with options name, description and publish=true: it adds origin and pushes under one approval. New repos are private; pass private=false only if the user asked for a public one. Use pull/checks before merge_pull and supply the reviewed head sha. Never force push. options holds action fields (branch, paths, message, number, title, body, head, base, sha).",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["status", "diff", "branches", "fetch", "pull_ff", "push", "stage", "unstage", "commit", "branch", "checkout", "merge", "abort_merge", "repos", "create_repo", "pulls", "pull", "checks", "create_pull", "update_pull", "merge_pull", "issues", "create_issue", "comment", "review"]},
                "options": {"type": "object", "additionalProperties": True}
            }},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a lasting memory note about the user or project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "scope": {"type": "string", "enum": ["global", "project"]},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Create or update the session task list (Cursor-style). "
                "Use for multi-step jobs (≈3+ steps). Plan distinct tool phases, not separate "
                "features that will all be implemented inside one file-write call. Keep exactly "
                "one item in_progress. Complete at most one milestone per update. "
                "When a step is done, call todo_write with merge=true and status=completed for that id "
                "BEFORE starting the next step or giving the final answer. "
                "Never recreate the same open plan with merge=false; that does not mark work done. "
                "merge=true upserts by id and is what you want for almost every call, including "
                "adding a step to an existing plan. merge=false replaces the whole list and "
                "discards every status in it, so use it only for a genuinely different plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "merge": {
                        "type": "boolean",
                        "description": "If true, upsert by id into the existing list. If false, replace the entire list.",
                    },
                    "todos": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string", "description": "Imperative task, e.g. Fix auth bug"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "Present-continuous label while in_progress, e.g. Fixing auth bug",
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                },
                "required": ["merge", "todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a decision you genuinely cannot make yourself, and WAIT. "
                "Use it only when guessing wrong would waste or destroy real work, or when "
                "the answer is something only they can know (a credential, an external "
                "constraint, which of two existing files to change). "
                "Do NOT use it for taste, naming, layout, styling, difficulty, scope of "
                "flourish, or anything you can reasonably infer from the request. Decide, "
                "and say what you assumed in your debrief. "
                "Ask at most once, before you start building. Once you have begun writing "
                "files it is too late: finish on your best judgement instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Short stable id"},
                                "question": {"type": "string", "description": "The full question"},
                                "header": {
                                    "type": "string",
                                    "description": "Very short chip label, max 12 chars (e.g. Scope, Library)",
                                },
                                "multi_select": {"type": "boolean", "default": False},
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 4,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string", "description": "Concise choice, 1-5 words"},
                                            "description": {"type": "string", "description": "What picking this means"},
                                        },
                                        "required": ["label"],
                                    },
                                },
                            },
                            "required": ["question", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": (
                "Run a focused sub-task in a FRESH context and get back only its "
                "findings. The sub-task sees none of this conversation, so `task` "
                "must be completely self-contained. Use this when answering a "
                "question would mean reading a lot of material you do not need to "
                "keep: surveying an unfamiliar area of a codebase, tracing where "
                "something is used across many files, digging through a long log. "
                "Its reading costs you nothing but the summary it returns. "
                "Do not use it for work you can do in two or three tool calls, and "
                "do not use it to make edits: it reports, you decide."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Self-contained instructions. Name the paths or areas to "
                            "look at and state exactly what to report back. The "
                            "sub-agent cannot see your conversation, your plan, or "
                            "anything you have already read."
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional tool allowlist for the sub-task. Defaults to "
                            "read-only inspection tools."
                        ),
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer",
            "description": (
                "Look at this machine's screen and drive its mouse and keyboard. Send "
                "`actions` with every step you are already sure of, in order: one "
                "screenshot comes back at the end, and each separate call costs a whole "
                "turn. Coordinates are pixels on the screenshot you were last shown, not "
                "on the physical display. Use run_shell to launch an application; use "
                "this to work inside it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Several steps in one turn, each the same shape as a single "
                            'call: [{"action": "key", "text": "ctrl+alt+1"}, '
                            '{"action": "type", "text": "The Title"}, '
                            '{"action": "key", "text": "Return"}]. They run in order and '
                            "stop at the first one refused. Use this whenever the next few "
                            "steps do not depend on what the screen does in between."
                        ),
                    },
                    "action": {
                        "type": "string",
                        # From the module that performs them, so the schema the
                        # model reads and the dispatcher cannot disagree.
                        "enum": list(COMPUTER_ACTIONS),
                        "description": (
                            "elements: the named widgets of the active window, each with a "
                            "ref. screenshot: look, optionally at a region. click/"
                            "double_click/right_click/middle_click/move: at a ref, or at "
                            "x,y. drag: x,y to to_x,to_y. type: text at x,y (clicks there "
                            "first when given). key: a combination in text. scroll: "
                            "direction at x,y. wait: seconds."
                        ),
                    },
                    "ref": {
                        "type": "string",
                        "description": (
                            "A widget ref from the last `elements` call, e.g. a3. Aim with "
                            "this rather than x,y wherever the window offers it: the click "
                            "lands in the middle of that widget, with nothing to estimate."
                        ),
                    },
                    "x": {"type": "integer", "description": "Horizontal pixel on the last screenshot."},
                    "y": {"type": "integer", "description": "Vertical pixel on the last screenshot."},
                    "to_x": {"type": "integer", "description": "Drag destination."},
                    "to_y": {"type": "integer", "description": "Drag destination."},
                    "text": {
                        "type": "string",
                        "description": (
                            "Text to type, or for action=key the combination, "
                            "e.g. ctrl+l, Return, alt+Tab, F5."
                        ),
                    },
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "amount": {"type": "integer", "description": "Scroll notches, default 3."},
                    "confirm": {
                        "type": "string",
                        "description": (
                            "Pass \"replace_selection\" to type or press Return over a "
                            "selection on purpose. Without it, acting on a select-all is "
                            "refused, because it overwrites the whole document."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": (
                            "Times to repeat a key press, 1-10. Use this instead of "
                            "sending the same key again and again."
                        ),
                    },
                    "seconds": {"type": "number", "description": "Seconds to wait, for action=wait."},
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "[x, y, width, height] on the last screenshot. With "
                            "action=screenshot this returns that area magnified, which is how "
                            "to read text too small to make out."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser",
            "description": (
                "Drive a web page through the browser itself rather than through pixels: "
                "elements come back as refs with names, text comes back as text, and typing "
                "is one insert. Use this for anything in a browser, including web "
                "applications like Google Docs, and the computer tool only for the desktop "
                "around it. Olith opens its own browser window; sign in there once and it "
                "stays signed in."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(BROWSER_ACTIONS),
                        "description": (
                            "open: go to url. snapshot: the clickable elements, each with a "
                            "ref. read: the page as text. click/fill: act on a ref (fill "
                            "replaces the field's contents with text). type: insert text "
                            "wherever the caret is. key: a combination, e.g. ctrl+alt+1. "
                            "scroll, back, status, close."
                        ),
                    },
                    "url": {"type": "string"},
                    "ref": {
                        "type": "string",
                        "description": "An element ref from the last snapshot, e.g. e12.",
                    },
                    "text": {"type": "string", "description": "Text to type, or the key combination."},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer"},
                    "count": {"type": "integer", "description": "Repeat a key press, 1-10."},
                },
                "required": ["action"],
            },
        },
    },
]

# Sub-tasks inspect and report. The parent makes all changes.
SUBAGENT_DEFAULT_TOOLS = ("list_dir", "read_file", "search_files", "run_shell", "git_status")
# End a sub-task with a summary when its tool budget is spent.
SUBAGENT_MAX_STEPS = 16
# Offer delegation only on large projects, where a fresh context window helps.
DELEGATE_MIN_FILES = 300
_large_project_cache: dict[str, bool] = {}


def project_is_large(project_root: Path | None) -> bool:
    """Check project size for delegation. Stop at the threshold and cache the result per root."""
    if project_root is None:
        return False
    key = str(project_root)
    cached = _large_project_cache.get(key)
    if cached is not None:
        return cached
    count = 0
    try:
        for path in project_root.rglob("*"):
            if path.is_file() and not _is_ignored(path, project_root):
                count += 1
                if count >= DELEGATE_MIN_FILES:
                    break
    except OSError:
        count = 0
    result = count >= DELEGATE_MIN_FILES
    _large_project_cache[key] = result
    return result


TOOL_DEFS.extend([
    {"type": "function", "function": {"name": "connection_tools",
        "description": "Discover tools and input schemas from enabled direct service connections. Pass a service ID or a focused query.",
        "parameters": {"type": "object", "properties": {"server": {"type": "string"}, "query": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "connection_call",
        "description": "Call a service tool discovered with connection_tools. Olith handles action approvals according to the user's Approval mode setting.",
        "parameters": {"type": "object", "properties": {"server": {"type": "string"}, "tool": {"type": "string"},
            "arguments": {"type": "object"}}, "required": ["server", "tool", "arguments"]}}},
])
_TOOL_DEFS_NO_DELEGATE = [t for t in TOOL_DEFS if t["function"]["name"] != "delegate"]


# Use the same measured priority order for tool selection and display.
TOOL_PRIORITY = (
    "read_file", "edit_file", "search_files", "run_shell", "browser", "computer",
    "write_file",
    "list_dir", "todo_write",
    "validate_file", "append_file", "git_status", "check_toolchains",
    "remember", "ask_user", "delegate", "connection_tools", "connection_call",
)

# Accept common tool names from other models without changing our own names.
TOOL_ALIASES = {
    "bash": "run_shell", "shell": "run_shell", "terminal": "run_shell",
    "execute_command": "run_shell", "execute_bash": "run_shell",
    "run_command": "run_shell", "run_terminal_cmd": "run_shell",
    "cat": "read_file", "open_file": "read_file", "view_file": "read_file",
    "get_file": "read_file", "read": "read_file",
    "create_file": "write_file", "new_file": "write_file",
    "save_file": "write_file",
    "str_replace": "edit_file", "replace_in_file": "edit_file",
    "apply_patch": "edit_file", "modify_file": "edit_file",
    "update_file": "edit_file", "patch_file": "edit_file",
    "ls": "list_dir", "dir": "list_dir", "list_files": "list_dir",
    "list_directory": "list_dir",
    "grep": "search_files", "search": "search_files", "find": "search_files",
    "ripgrep": "search_files", "search_code": "search_files",
    "lint": "validate_file", "check_syntax": "validate_file",
    "git": "git_status",
    "todo": "todo_write", "update_todo": "todo_write",
    "write_todo": "todo_write", "update_plan": "todo_write",
    "save_memory": "remember", "memorize": "remember",
    "ask": "ask_user", "ask_question": "ask_user",
    "spawn_agent": "delegate", "subagent": "delegate",
    **{k: "computer" for k in (
        "screenshot", "take_screenshot", "capture_screen", "screen_capture",
        "click", "mouse_click", "left_click", "double_click", "right_click",
        "move_mouse", "mouse_move", "type_text", "keyboard_type", "press_key",
        "key_press", "hotkey", "mouse_scroll", "mouse_drag", "computer_use",
        "desktop", "gui",
    )},
}

# An alias like `press_key` names the action too, and the arguments cannot
# recover it: typing and a hotkey both carry only `text`.
COMPUTER_ALIAS_ACTIONS = {
    "screenshot": "screenshot", "take_screenshot": "screenshot",
    "capture_screen": "screenshot", "screen_capture": "screenshot",
    "click": "click", "mouse_click": "click", "left_click": "click",
    "double_click": "double_click", "right_click": "right_click",
    "move_mouse": "move", "mouse_move": "move",
    "type_text": "type", "keyboard_type": "type",
    "press_key": "key", "key_press": "key", "hotkey": "key",
    "mouse_scroll": "scroll", "mouse_drag": "drag",
}


def apply_alias_defaults(raw_name: str, name: str, args: dict) -> dict:
    """Fill in what the canonical name dropped when an alias carried it."""
    if name != "computer" or not isinstance(args, dict) or args.get("action"):
        return args
    action = COMPUTER_ALIAS_ACTIONS.get(str(raw_name or "").strip().lower())
    if not action:
        return args
    return {**args, "action": action}


def canonical_tool_name(name: str) -> str:
    """The registered tool a model meant. A real tool name always wins, so an
    alias can never shadow one."""
    if not isinstance(name, str):
        return name
    if name in _known_tool_names():
        return name
    return TOOL_ALIASES.get(name.strip().lower(), name)


# Drop optional tools in this order when context is tight. Keep ask_user until last.
TRIM_ORDER = ("connection_call", "connection_tools", "delegate", "remember", "git_status", "check_toolchains",
              "validate_file", "append_file", "ask_user")
# Base schemas cost about 4.1k tokens. Trim optional tools for small windows.
SCHEMA_BUDGET_FRACTION = 0.15


def schema_tokens(defs: list[dict]) -> int:
    from ollama_client import estimate_tokens
    return estimate_tokens(json.dumps(defs, ensure_ascii=False))


def trim_tool_defs(defs: list[dict], ctx_limit: int | None) -> list[dict]:
    """Drop the least essential schemas until they fit the window's share."""
    if not ctx_limit or ctx_limit <= 0:
        return defs
    budget = int(int(ctx_limit) * SCHEMA_BUDGET_FRACTION)
    if schema_tokens(defs) <= budget:
        return defs
    kept = list(defs)
    for name in TRIM_ORDER:
        if schema_tokens(kept) <= budget:
            break
        kept = [t for t in kept if t["function"]["name"] != name]
    return kept


ESSENTIAL_TOOLS = tuple(
    t["function"]["name"] for t in TOOL_DEFS if t["function"]["name"] not in TRIM_ORDER
)


# Splitting on ". " alone cuts "e.g. parse_version" down to "e.g.", throwing
# away the example that made the description worth keeping.
_ABBREVIATIONS = ("e.g", "i.e", "etc", "vs", "cf", "approx", "ie", "eg")


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    parts = text.split(". ")
    head, i = parts[0], 1
    while i < len(parts) and head.rsplit(" ", 1)[-1].lower() in _ABBREVIATIONS:
        head += ". " + parts[i]
        i += 1
    return head.rstrip(".") + "."


def _lean_prose(node: object) -> None:
    """Shorten schema descriptions in place. Keep types, required fields, enums and items unchanged."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                node[key] = _first_sentence(value)
            else:
                _lean_prose(value)
    elif isinstance(node, list):
        for item in node:
            _lean_prose(item)


def _lean_def(spec: dict) -> dict:
    """Return a schema copy with each description shortened to one sentence."""
    out = copy.deepcopy(spec)
    fn = out["function"]
    if fn.get("description"):
        fn["description"] = _first_sentence(fn["description"]) or fn["description"]
    _lean_prose(fn.get("parameters"))
    return out if out != spec else spec


def apply_policy(defs: list[dict], policy: dict | None) -> list[dict]:
    """Budget and order the schemas for one model. The full-tier policy is the
    identity, so a run on the tuned pair gets back exactly what it passed in."""
    if not policy:
        return defs
    budget = policy.get("tool_budget")
    kept = defs
    if budget:
        by_name = {t["function"]["name"]: t for t in defs}
        # Rank present tools, essentials first so a budget can never drop one.
        ranked = sorted(
            by_name,
            key=lambda n: (n not in ESSENTIAL_TOOLS, TOOL_PRIORITY.index(n)
                           if n in TOOL_PRIORITY else len(TOOL_PRIORITY)),
        )
        chosen = set(ranked[:max(int(budget), len(ESSENTIAL_TOOLS))])
        kept = [t for t in defs if t["function"]["name"] in chosen]
    if policy.get("order_tools"):
        rank = {n: i for i, n in enumerate(TOOL_PRIORITY)}
        by_priority = sorted(kept, key=lambda t: rank.get(t["function"]["name"], len(rank)))
        kept = policy_mod.edge_order(by_priority)
    if policy.get("lean_schemas"):
        kept = [_lean_def(t) for t in kept]
    return kept


def tool_defs_for(chat: dict | None, ctx_limit: int | None = None,
                  profile: dict | None = None) -> list[dict]:
    """Tool schemas for this run. Sub-agents get a restricted, non-recursive set."""
    # Presence of the key marks a sub-run, not its truthiness. An empty spec is
    # falsy, and reading that as "not a sub-agent" hands the child delegate.
    if not isinstance(chat, dict) or "subagent" not in chat:
        # Set once per run by the loop, the only place that knows the project
        # root. Absent means gated off, which is the safe default.
        base = TOOL_DEFS if (chat or {}).get("delegate_available") else _TOOL_DEFS_NO_DELEGATE
        # 821 tokens of schema for a desktop nobody asked it to touch, and a
        # standing invitation to click on one. Off unless the chat turned it on.
        if not (chat or {}).get("computer_use"):
            base = [t for t in base if t["function"]["name"] not in ("computer", "browser")]
        import mcp_connections
        if not mcp_connections.active():
            base = [t for t in base if t["function"]["name"] not in ("connection_tools", "connection_call")]
        return trim_tool_defs(apply_policy(base, policy_mod.policy_for(profile)), ctx_limit)
    spec = chat.get("subagent") or {}
    allowed = {str(t) for t in (spec.get("tools") or SUBAGENT_DEFAULT_TOOLS)}
    # Never recursive: a sub-agent that can delegate can spawn without bound.
    allowed.discard("delegate")
    # Keep desktop and browser actions in the parent run.
    allowed.discard("computer")
    allowed.discard("browser")
    allowed.discard("connection_call")
    allowed.discard("connection_tools")
    picked = [t for t in TOOL_DEFS if t["function"]["name"] in allowed]
    # An empty allowlist would leave the sub-agent unable to do anything at all.
    picked = picked or [
        t for t in TOOL_DEFS if t["function"]["name"] in SUBAGENT_DEFAULT_TOOLS
    ]
    return trim_tool_defs(apply_policy(picked, policy_mod.policy_for(profile)), ctx_limit)


# Skip dependency folders when suggesting paths.
IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    "site-packages", "dist-info", "build", "dist", ".next", ".cache",
    # ML project noise: HF caches, checkpoints and vendored C++ trees swamp every
    # search in a training repo.
    "blobs", "snapshots", "checkpoints", ".ollama", "wandb",
})

# Skip text-decodable binary data such as tokenizer files and Arrow caches.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".gguf", ".pt", ".pth",
    ".safetensors", ".onnx", ".arrow", ".model", ".npy", ".npz",
    ".h5", ".ckpt", ".pack", ".idx", ".zip", ".tar", ".gz", ".7z",
})


def _is_ignored(path: Path, base: Path) -> bool:
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        parts = path.parts
    for part in parts:
        if part in IGNORE_DIRS:
            return True
        # HuggingFace cache dirs: models--org--name/
        if part.startswith("models--"):
            return True
    return False


def _allowed_roots(project_root: Path | None) -> list[Path]:
    from config import UPLOADS

    settings = load_settings()
    roots = [Path(r).resolve() for r in settings.get("allowed_roots", [])]
    if project_root:
        roots.insert(0, project_root.resolve())
    # Always allow reading user uploads / attachments, and offloaded tool output
    # A pointer the model cannot follow is worse than a truncated result.
    try:
        roots.append(UPLOADS.resolve())
    except Exception:
        pass
    try:
        roots.append(TOOLOUT_DIR.resolve())
    except Exception:
        pass
    return roots


def resolve_path(path: str, project_root: Path | None) -> Path:
    raw = Path(path or ".").expanduser()
    if not raw.is_absolute():
        # Resolve relative paths under the project, or the home folder when no project is set.
        base = project_root or Path.home()
        candidate = (base / raw).resolve()
    else:
        candidate = raw.resolve()
    if load_settings().get("unrestricted_fs"):
        return candidate
    allowed = _allowed_roots(project_root)
    for root in allowed:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise PermissionError(
        f"Path not in allowed roots: {candidate}. "
        "Enable 'Unrestricted file access' in Settings, or add the folder to allowed roots."
    )


def _path_suggestions(rel: str, project_root: Path, limit: int = 6) -> list[str]:
    """Find likely paths when the model omits a parent folder (e.g. reedus_llm/)."""
    if not project_root or not rel:
        return []
    needle = rel.strip().lstrip("./")
    if not needle or ".." in Path(needle).parts:
        return []
    hits: list[str] = []
    scanned = 0
    # Exact suffix match first: **/reedus_companion/scripts/foo.py
    for p in project_root.rglob(Path(needle).name):
        # Cap path suggestions to avoid scanning large dependency trees.
        scanned += 1
        if scanned > 20000:
            break
        if _is_ignored(p, project_root):
            continue
        try:
            if not p.exists():
                continue
            rp = str(p.relative_to(project_root)).replace("\\", "/")
            if rp == needle or rp.endswith("/" + needle) or rp.endswith(needle):
                hits.append(rp)
        except ValueError:
            continue
        if len(hits) >= 40:
            break
    # Prefer shorter / closer matches
    hits = sorted(set(hits), key=lambda s: (len(s), s))
    return hits[:limit]


def resolve_existing(path: str, project_root: Path | None, *, want_dir: bool | None = None) -> tuple[Path | None, str | None]:
    """Resolve a path; if missing, try unique project-relative fuzzy match.

    Returns (path, error_json_or_None).
    """
    try:
        candidate = resolve_path(path, project_root)
    except PermissionError as e:
        return None, json.dumps({"error": str(e)})
    if candidate.exists():
        if want_dir is True and not candidate.is_dir():
            return None, json.dumps({"error": "Not a directory", "path": str(candidate)})
        if want_dir is False and not candidate.is_file():
            return None, json.dumps({"error": "Not a file", "path": str(candidate)})
        return candidate, None
    # Fuzzy: model often uses paths relative to a subfolder while project root is parent
    if project_root and not Path(path or "").is_absolute():
        suggestions = _path_suggestions(str(path), project_root)
        filtered = []
        for s in suggestions:
            p = (project_root / s).resolve()
            if want_dir is True and not p.is_dir():
                continue
            if want_dir is False and not p.is_file():
                continue
            filtered.append(s)
        if len(filtered) == 1:
            return (project_root / filtered[0]).resolve(), None
        if filtered:
            return None, json.dumps({
                "error": "Not found",
                "tried": str(candidate),
                "suggestions": filtered,
                "hint": "Paths are relative to the project root. Use one of the suggestions.",
            })
    return None, json.dumps({
        "error": "Not found" if want_dir is not False else "Not a file",
        "tried": str(candidate),
        "hint": "Paths are relative to the project root (see Active project Root).",
    })


def run_tool(name: str, args: dict, project_root: Path | None,
             project_id: str | None = None, chat: dict | None = None) -> str:
    import editor_bridge
    ident = (chat or {}).get("editor_session_id")
    baseline = None
    if ident and name in {"write_file", "edit_file", "append_file"}:
        try:
            target = resolve_path(args.get("path") or "", project_root)
            baseline = (target.resolve(), target.read_bytes() if target.exists() else None)
        except (ValueError, OSError) as error:
            return json.dumps({"error": str(error)})
    with editor_bridge.writing(ident, baseline):
        if name == "run_shell":
            blocked = _rewrite_shell_guard(args.get("command") or "", project_root, chat)
            if blocked:
                return json.dumps(blocked)
        editor_bridge.take_reformatted()
        result = _run_tool(name, args, project_root, project_id, chat)
        if editor_bridge.take_reformatted():
            try:
                payload = json.loads(result)
                payload["note"] = "VS Code reformatted the file on save. Re-read the range before anchoring another edit there."
                result = json.dumps(payload)
            except (TypeError, ValueError):
                pass
        return result


def is_internal_verification_command(command: str) -> bool:
    return str(command or "").strip().casefold() == INTERNAL_VERIFICATION_COMMAND.casefold()


def _rewrite_shell_guard(command: str, root: Path | None, chat: dict | None) -> dict | None:
    if is_internal_verification_command(command):
        return {"error": "Internal verification label is not a shell command", "changed": False,
                "verification": (chat or {}).get("verification"),
                "hint": "Use the verification report as evidence. Run only actual project commands for failed or missing checks; otherwise finish the task."}
    return None


def _run_tool(
    name: str,
    args: dict,
    project_root: Path | None,
    project_id: str | None = None,
    chat: dict | None = None,
) -> str:
    try:
        if name == "list_dir":
            return _list_dir(args, project_root)
        if name == "read_file":
            return _read_file(args, project_root)
        if name == "write_file":
            return _write_file(args, project_root)
        if name == "append_file":
            return _append_file(args, project_root)
        if name == "edit_file":
            return _edit_file(args, project_root)
        if name == "search_files":
            return _search_files(args, project_root)
        if name == "run_shell":
            return _run_shell(args, project_root)
        if name in {"connection_tools", "connection_call"}:
            import mcp_connections
            if (chat or {}).get("subagent"):
                return json.dumps({"error": "Direct service connections are unavailable in sub-tasks"})
            return json.dumps(mcp_connections.discover(args) if name == "connection_tools" else mcp_connections.call(args))
        if name == "check_toolchains":
            return _check_toolchains()
        if name == "validate_file":
            return _validate_file(args, project_root)
        if name == "git_status":
            if not args.get("action") or args.get("action") == "status":
                return _git_status(project_root)
            import github_tools
            return json.dumps(github_tools.execute(project_root, args["action"], **(args.get("options") or {})))
        if name == "remember":
            return _remember(args, project_id)
        if name == "todo_write":
            return _todo_write(args, chat)
        if name == "computer":
            # Withholding the schema is only advice, and a model that names
            # the tool anyway must not get the mouse.
            if not (chat or {}).get("computer_use"):
                return json.dumps({
                    "error": "Computer use is off for this chat.",
                    "hint": "Tell the user to turn it on in the model menu. Do not retry.",
                })
            return computer.run(args)
        if name == "browser":
            if not (chat or {}).get("computer_use"):
                return json.dumps({
                    "error": "Computer use is off for this chat.",
                    "hint": "Tell the user to turn it on in the model menu. Do not retry.",
                })
            return _flag_expired_sign_in(browser.run(args))
        return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _flag_expired_sign_in(raw: str) -> str:
    result = json.loads(raw)
    note = connections.expired_sign_in_note(str(result.get("url") or ""))
    if not note:
        return raw
    # First, so a long snapshot being cut down cannot take it with it.
    return json.dumps({"sign_in_expired": note, **result})


_TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})


def _normalize_todo(raw: dict, allow_partial: bool = False) -> dict | None:
    """Normalize a todo. Allow status-only changes when allow_partial is set."""
    if not isinstance(raw, dict):
        return None
    tid = str(raw.get("id") or "").strip()
    content = str(raw.get("content") or "").strip()
    status = str(raw.get("status") or "pending").strip().lower()
    if status == "in-progress":
        status = "in_progress"
    if not tid or status not in _TODO_STATUSES:
        return None
    if not content:
        if not allow_partial:
            return None
        item = {"id": tid, "status": status}
        active = raw.get("activeForm") or raw.get("active_form")
        if active:
            item["activeForm"] = str(active).strip()
        return item
    item = {"id": tid, "content": content, "status": status}
    active = raw.get("activeForm") or raw.get("active_form")
    if active:
        item["activeForm"] = str(active).strip()
    return item


def _todo_match_key(item: dict) -> str:
    """Loose identity for a todo across a plan rewrite (ids get regenerated)."""
    return re.sub(r"[^a-z0-9]+", "", str(item.get("content") or "").lower())


def carry_forward_completions(old: list[dict], new_todos: list[dict]) -> list[str]:
    """Keep finished milestones when a rewritten plan resets all of them.

    Match by ID, then normalized content. Leave deliberate reopens alone when the
    new plan still keeps at least one finished item.
    """
    finished_by_id = {
        t["id"]: t["status"]
        for t in old
        if t.get("id") and t.get("status") in ("completed", "cancelled")
    }
    if not finished_by_id:
        return []
    finished_by_content: dict[str, str] = {}
    for t in old:
        if t.get("status") in ("completed", "cancelled"):
            key = _todo_match_key(t)
            if key:
                finished_by_content.setdefault(key, t["status"])

    def prior_status(item: dict) -> str | None:
        by_id = finished_by_id.get(item.get("id"))
        if by_id:
            return by_id
        return finished_by_content.get(_todo_match_key(item))

    if any(
        t.get("status") in ("completed", "cancelled") and prior_status(t)
        for t in new_todos
    ):
        return []

    restored = []
    for t in new_todos:
        prior = prior_status(t)
        if prior and t.get("status") in ("pending", "in_progress"):
            t["status"] = prior
            restored.append(t.get("id"))
    return restored


def todos_progress(todos: list[dict] | None) -> dict:
    items = list(todos or [])
    total = len(items)
    done = sum(1 for t in items if t.get("status") in ("completed", "cancelled"))
    open_n = sum(1 for t in items if t.get("status") in ("pending", "in_progress"))
    active = next((t for t in items if t.get("status") == "in_progress"), None)
    pct = int(round(100 * done / total)) if total else 0
    return {
        "todos": items,
        "total": total,
        "done": done,
        "open": open_n,
        "pct": pct,
        "active": active,
    }


def todos_incomplete(chat: dict | None) -> bool:
    return todos_progress((chat or {}).get("agent_todos")).get("open", 0) > 0


_GENERIC_TODO_IDS = frozenset({
    "build", "test", "tests", "verify", "validation", "wire", "wiring",
    "finish", "done", "implement", "implementation", "inspect", "review",
})


def todos_fingerprint(todos: list[dict] | None) -> str:
    """Stable signature of open todo ids and status, for detecting stalled plans."""
    items = []
    for t in todos or []:
        st = t.get("status") or "pending"
        if st in ("pending", "in_progress"):
            items.append(f"{t.get('id')}:{st}")
    return "|".join(items)


def complete_open_todos(chat: dict | None, reason: str = "auto") -> dict:
    """Mark every open todo completed. Used when the model finished but forgot to check off."""
    if chat is None:
        return todos_progress([])
    items = list(chat.get("agent_todos") or [])
    for t in items:
        if t.get("status") in ("pending", "in_progress"):
            t["status"] = "completed"
    chat["agent_todos"] = items
    prog = todos_progress(items)
    prog["auto_completed"] = reason
    return prog


def todos_prompt_block(chat: dict | None) -> str:
    todos = (chat or {}).get("agent_todos") or []
    if not todos:
        return ""
    lines = [
        "# Agent plan (live)",
        "When a step is finished, todo_write merge=true with status=completed for that id. "
        "Do not stop while open items remain, and do not recreate the same open plan.",
    ]
    for t in todos:
        st = t.get("status") or "pending"
        mark = {
            "completed": "[x]",
            "cancelled": "[-]",
            "in_progress": "[>]",
            "pending": "[ ]",
        }.get(st, "[ ]")
        label = t.get("activeForm") if st == "in_progress" and t.get("activeForm") else t.get("content")
        lines.append(f"- {mark} {t.get('id')}: {label}")
    return "\n".join(lines)


def _todo_write(args: dict, chat: dict | None) -> str:
    if chat is None:
        return json.dumps({"error": "No active chat for todos"})
    incoming = args.get("todos") or []
    if not isinstance(incoming, list) or not incoming:
        return json.dumps({"error": "todos must be a non-empty array"})
    merge = bool(args.get("merge"))
    old = list(chat.get("agent_todos") or [])
    messages = chat.get("messages") or []
    user_index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user" and not messages[i].get("agent_nudge") and not messages[i].get("computer_frame")), None)
    scope = None
    if user_index is not None:
        user = messages[user_index]
        scope = str(user.get("id") or user.get("ts") or user_index) + ":" + hashlib.sha256((user.get("content") or "").encode()).hexdigest()[:16]
        previous = chat.get("agent_plan_scope")
        previous_result = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "tool" and messages[i].get("name") == "todo_write"), None)
        resume = user.get("resume_job") or user.get("course_correct") or re.fullmatch(r"\s*(?:continue|resume|go on|keep going|finish it)[.!]*\s*", (user.get("content") or "").split("\n\n")[0], re.I)
        if not resume and ((previous and previous != scope) or (previous is None and previous_result is not None and previous_result < user_index)):
            old = []
    known_ids = {t.get("id") for t in old if t.get("id")}
    normalized: list[dict] = []
    for raw in incoming:
        # A merge update to a todo that already exists may carry status alone.
        partial = merge and isinstance(raw, dict) and str(
            raw.get("id") or ""
        ).strip() in known_ids
        item = _normalize_todo(raw, allow_partial=partial)
        if item:
            normalized.append(item)
    if not normalized:
        return json.dumps({
            "error": "No valid todos",
            "hint": (
                "A new todo needs id, content and status. Updating an existing one with "
                "merge=true needs only id and status."
            ),
        })

    old_fp = todos_fingerprint(old)
    old_open = todos_progress(old).get("open", 0)
    if merge:
        by_id = {t["id"]: dict(t) for t in old if t.get("id")}
        order = [t["id"] for t in old if t.get("id")]
        for item in normalized:
            tid = item["id"]
            if tid in by_id:
                by_id[tid].update(item)
            else:
                by_id[tid] = item
                order.append(tid)
            if tid not in order:
                order.append(tid)
        new_todos = [by_id[i] for i in order if i in by_id]
        restored_ids: list[str] = []
    else:
        new_todos = normalized
        restored_ids = carry_forward_completions(old, new_todos)
    if scope is not None:
        for item in new_todos:
            prior = next((task for task in old if task.get("id") == item.get("id") and _todo_match_key(task) == _todo_match_key(item)), None)
            if prior and prior.get("status") in {"completed", "cancelled"} and item.get("status") in {"pending", "in_progress"}:
                item["status"] = prior["status"]
                if item["id"] not in restored_ids:
                    restored_ids.append(item["id"])
        chat["agent_plan_scope"] = scope

    old_by_id = {t.get("id"): t for t in old if t.get("id")}
    partial_note: dict | None = None
    if merge and old_by_id:
        newly_finished = [
            t
            for t in new_todos
            if t.get("status") in ("completed", "cancelled")
            and (old_by_id.get(t.get("id")) or {}).get("status")
            in ("pending", "in_progress")
        ]
        if len(newly_finished) > 1:
            active_id = next(
                (t.get("id") for t in old if t.get("status") == "in_progress"),
                None,
            )
            attempted = [t.get("id") for t in newly_finished]
            if active_id in attempted:
                # Complete the active milestone and leave the others for later calls.
                deferred = [tid for tid in attempted if tid != active_id]
                deferred_set = set(deferred)
                for t in new_todos:
                    if t.get("id") in deferred_set:
                        t["status"] = (old_by_id.get(t.get("id")) or {}).get(
                            "status", "pending"
                        )
                partial_note = {
                    "completed_now": active_id,
                    "deferred": deferred,
                    "why": (
                        "One milestone completes per call. Verify each deferred item's work "
                        "actually landed, then complete it on its own call."
                    ),
                }
            else:
                return json.dumps({
                    "error": "Complete only one plan milestone per todo_write call",
                    "changed": False,
                    "attempted_ids": attempted,
                    "todos": old,
                    "send_exactly": {
                        "merge": True,
                        "todos": [{"id": active_id or attempted[0], "status": "completed"}],
                    },
                    "hint": (
                        f"None of these is the active milestone ({active_id!r}). Complete that "
                        "one first; Olith promotes the next pending item automatically."
                    ),
                })
        old_active = next(
            (t for t in old if t.get("status") == "in_progress"),
            None,
        )
        if (
            newly_finished
            and old_active
            and newly_finished[0].get("id") != old_active.get("id")
        ):
            return json.dumps({
                "error": "Complete the current in_progress milestone before a pending one",
                "changed": False,
                "active_id": old_active.get("id"),
                "attempted_id": newly_finished[0].get("id"),
                "todos": old,
                "send_exactly": {
                    "merge": True,
                    "todos": [{"id": old_active.get("id"), "status": "completed"}],
                },
                "hint": (
                    f"The plan is ordered: {old_active.get('id')!r} is in progress and must be "
                    f"completed before {newly_finished[0].get('id')!r}. If its work is already "
                    "done, send the send_exactly call above."
                ),
            })

    # Prefer a single in_progress item (Cursor rule). Keep first; demote extras to pending.
    seen_ip = False
    for t in new_todos:
        if t.get("status") == "in_progress":
            if seen_ip:
                t["status"] = "pending"
            else:
                seen_ip = True
    if not seen_ip:
        next_pending = next(
            (t for t in new_todos if t.get("status") == "pending"),
            None,
        )
        if next_pending:
            next_pending["status"] = "in_progress"

    chat["agent_todos"] = new_todos
    prog = todos_progress(new_todos)
    new_fp = todos_fingerprint(new_todos)
    stalled = prog["open"] > 0 and new_fp == old_fp and old_open > 0
    instructions = (
        "Todos updated. Keep exactly one item in_progress. "
        "Mark completed immediately after each distinct tool phase with merge=true; "
        "complete only the current milestone and do not batch later milestones. "
        "Continue until open=0, then give the final answer."
    )
    if stalled:
        instructions = (
            "WARNING: Plan did not advance. Same open items as before. "
            "This does NOT mark work done. If the step (or whole task) is finished, call "
            'todo_write with merge=true and status="completed" for those ids, e.g. '
            '{"merge":true,"todos":[{"id":"STEP_ID","content":"...","status":"completed"}]}. '
            "Do not call merge=false to recreate the same in_progress plan."
        )
    elif prog["open"] == 0:
        instructions = "All todos completed (open=0). Give the final answer now. Do not restart the plan."
    if restored_ids:
        instructions = (
            f"This plan update reopened {len(restored_ids)} milestone(s) you had already "
            f"finished ({', '.join(str(i) for i in restored_ids)}); their completed status was "
            "restored. That work is done, do not redo it. Resume from the item now in_progress. "
            "Use merge=true to change statuses; merge=false is only for a genuinely new plan. "
        ) + instructions
    return json.dumps(
        {
            "ok": True,
            "merge": merge,
            "restored": restored_ids,
            "todos": new_todos,
            "done": prog["done"],
            "total": prog["total"],
            "open": prog["open"],
            "pct": prog["pct"],
            "stalled": stalled,
            "partial": partial_note,
            "instructions": instructions,
        },
        indent=2,
    )


def _list_dir(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or ".", project_root, want_dir=True)
    if err:
        return err
    assert path is not None
    recursive = bool(args.get("recursive"))
    items = []
    if recursive:
        for p in path.rglob("*"):
            if any(part.startswith(".") for part in p.parts):
                continue
            if _is_ignored(p, path):
                continue
            if p.is_file():
                items.append(str(p.relative_to(path)))
            if len(items) >= 400:
                items.append("…truncated…")
                break
    else:
        for p in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue
            items.append(("📁 " if p.is_dir() else "📄 ") + p.name)
    return json.dumps({"path": str(path), "entries": items}, indent=2)


def _read_symbol(args: dict, path: Path, text: str) -> str | None:
    """Return an outline or definition. Return None for a normal line read if parsing is unavailable."""
    wants_outline = bool(args.get("outline"))
    wanted = str(args.get("symbol") or "").strip()
    if not wants_outline and not wanted:
        return None

    entries = symbols.outline(text, str(path))
    if entries is None:
        return json.dumps({
            "path": str(path),
            "symbols_supported": False,
            "hint": ("Symbols are available for Python and JavaScript/TypeScript, and "
                     "only when the file parses. Re-read with a line range, or use "
                     "search_files to locate it."),
            "revision": _file_revision(path),
        })

    if wants_outline and not wanted:
        # One compact line per definition, not JSON objects. On server.py the
        # object form cost 11,868 tokens against a 2,597 token whole-file read.
        rendered: list[str] = []
        used = 0
        for e in entries:
            line = f"{e['line']}-{e['end_line']} {e['kind'][:5]} {e['signature'][:OUTLINE_SIGNATURE_CHARS]}"
            if used + len(line) + 1 > READ_FILE_CONTENT_MAX_CHARS:
                break
            rendered.append(line)
            used += len(line) + 1
        result = {
            "path": str(path),
            "outline": "\n".join(rendered),
            "count": len(entries),
            "lines": len(text.splitlines()),
            "exact": all(e["exact"] for e in entries) if entries else True,
            "revision": _file_revision(path),
        }
        if len(rendered) < len(entries):
            result["truncated"] = True
            result["shown"] = len(rendered)
            result["hint"] = (f"{len(entries) - len(rendered)} more definitions. Use "
                              "search_files to find one by name.")
        return json.dumps(result)

    found = symbols.find(text, str(path), wanted)
    if not found:
        # Naming what is there turns a dead end into the next call, the same
        # way a bad path returns suggestions instead of just failing.
        available = [e["name"] for e in entries]
        return json.dumps({
            "path": str(path),
            "error": f"No function or class named {wanted!r} in this file.",
            "available": available[:40],
            "truncated_names": len(available) > 40,
            "revision": _file_revision(path),
        })

    body = found["source"]
    numbered = "\n".join(
        f"{found['line'] + i}|{line}" for i, line in enumerate(body.splitlines()))
    result = {
        "path": str(path),
        "symbol": found["name"],
        "kind": found["kind"],
        "start": found["line"],
        "end": found["end_line"],
        "exact": found["exact"],
        "content": numbered[:READ_FILE_CONTENT_MAX_CHARS],
        "revision": _file_revision(path),
    }
    if len(numbered) > READ_FILE_CONTENT_MAX_CHARS:
        result["truncated"] = True
        result["hint"] = (f"{found['name']} is longer than the output cap; continue with "
                          f"a line range from {found['line']}.")
    if not found["exact"]:
        result["hint_boundaries"] = ("Boundaries for this language are matched by brace "
                                     "counting and may be off. Verify before editing.")
    return json.dumps(result)


def _read_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args["path"], project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    if path.stat().st_size > 2_000_000:
        return json.dumps({"error": "File too large (>2MB)", "path": str(path), "bytes": path.stat().st_size})
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        return json.dumps({
            "path": str(path),
            "binary": True,
            "bytes": len(raw),
            "hint": "Binary file, contents not shown as text. Use run_shell (e.g. file, strings) if needed.",
        })
    text = raw.decode("utf-8", errors="replace")
    if args.get("language_action"):
        if not project_root or not load_settings().get("language_servers", True):
            return json.dumps({"available": False, "reason": "Language navigation needs an active project and enabled language servers."})
        path.relative_to(project_root.resolve())
        return json.dumps(language_servers.inspect_file(project_root, path, args["language_action"],
                                                       int(args.get("start_line") or 1), int(args.get("character") or 0), str(args.get("new_name") or "")))
    symbol_result = _read_symbol(args, path, text)
    if symbol_result is not None:
        return symbol_result
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    end = int(args.get("end_line") or len(lines))
    end = min(end, len(lines))
    sliced = lines[start - 1 : end]
    rendered: list[str] = []
    rendered_chars = 0
    partial_line = False
    for offset, line in enumerate(sliced):
        numbered_line = f"{offset + start}|{line}"
        added = len(numbered_line) + (1 if rendered else 0)
        if rendered and rendered_chars + added > READ_FILE_CONTENT_MAX_CHARS:
            break
        # A single minified line cannot be safely addressed with line edits, but
        # still return a bounded diagnostic instead of flooding prompt storage.
        if not rendered and added > READ_FILE_CONTENT_MAX_CHARS:
            numbered_line = numbered_line[:READ_FILE_CONTENT_MAX_CHARS]
            rendered.append(numbered_line)
            rendered_chars = len(numbered_line)
            partial_line = True
            break
        rendered.append(numbered_line)
        rendered_chars += added
    numbered = "\n".join(rendered)
    actual_end = start + len(rendered) - 1 if rendered else end
    result = {
        "path": str(path),
        "start": start,
        "end": actual_end,
        "content": numbered,
        # Lets the controller safely reuse read coverage on later agent turns.
        # Size alone is not sufficient: an in-place edit can keep it unchanged.
        "revision": _file_revision(path),
    }
    if partial_line:
        result["truncated"] = True
        result["line_truncated"] = True
        result["hint"] = (
            f"Line {start} alone exceeds the output cap. Use search_files or run_shell "
            "to inspect this minified/generated file."
        )
    elif actual_end < end:
        result["truncated"] = True
        result["requested_end"] = end
        result["hint"] = f"Output capped at line {actual_end}; continue at line {actual_end + 1}."
    return json.dumps(result)


def offload_tool_output(name: str, content: str, chat_id: str, seq: int) -> str | None:
    """Save a large result to disk and return its head, tail and path.

    Return None if it fits in the prompt as it is.
    """
    if len(content or "") <= OFFLOAD_CHARS:
        return None

    def _write(body: str, field: str = "") -> str | None:
        try:
            out_dir = TOOLOUT_DIR / (chat_id or "unknown")
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-zA-Z0-9_-]", "", name) or "tool"
            suffix = f"-{re.sub(r'[^a-zA-Z0-9_-]', '', field)}" if field else ""
            path = out_dir / f"{seq:04d}-{safe}{suffix}.txt"
            path.write_text(body, encoding="utf-8")
            # Bound growth: a long run can produce hundreds of these.
            for old in sorted(out_dir.iterdir(), key=lambda f: f.name)[:-TOOLOUT_KEEP]:
                old.unlink(missing_ok=True)
            return str(path)
        except OSError:
            # If saving output fails, return it normally and let rendering truncate it.
            return None

    def _pointer(path: str, body: str) -> dict:
        return {
            "path": path,
            "chars": len(body),
            "lines": body.count("\n") + 1,
            "note": (
                "This output was large. The head and tail are shown here and the "
                "complete text was saved to `path`, nothing was lost. To see a "
                "part that is not shown, call read_file on that path with a line "
                "range."
            ),
        }

    # Keep the envelope and offload only the big payload field. Offloading the
    # whole JSON hands the model the head and tail of truncated JSON.
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        # Every oversized field, not just the biggest: run_shell returns both
        # stdout and stderr, and either can be at the full capture cap.
        offloaded: dict[str, dict] = {}
        for key in ("content", "stdout", "stderr", "output", "text", "matches"):
            body = payload.get(key)
            if not isinstance(body, str):
                continue
            if len(body) <= OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS:
                continue
            path = _write(body, key)
            if path is None:
                return None
            payload[key] = (
                body[:OFFLOAD_HEAD_CHARS]
                + "\n…[middle saved to the offload file]…\n"
                + body[-OFFLOAD_TAIL_CHARS:]
            )
            offloaded[key] = _pointer(path, body)
        if offloaded:
            payload["offloaded"] = offloaded
            return json.dumps(payload)

    path = _write(content)
    if path is None:
        return None
    return json.dumps({
        "offloaded": _pointer(path, content),
        "tool": name,
        "head": content[:OFFLOAD_HEAD_CHARS],
        "tail": content[-OFFLOAD_TAIL_CHARS:],
    })


def _snapshot_before_write(path: Path) -> None:
    """Back up the current file before a tool replaces it."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return
        backups = BACKUP_DIR / path.name
        backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # Hash, not size: two edits in the same second that happen to produce the
        # same length are different versions and both are worth keeping.
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        target = backups / f"{stamp}-{digest}{path.suffix or '.bak'}"
        if target.exists():
            return
        shutil.copy2(path, target)
        keep = sorted(backups.iterdir(), key=lambda f: f.name)[-BACKUP_KEEP:]
        for stale in backups.iterdir():
            if stale not in keep:
                stale.unlink(missing_ok=True)
    except OSError:
        # A backup must never be the reason an edit fails.
        return


def _save_source(path: Path, content: str, candidate: Path | None = None) -> None:
    import editor_bridge
    if editor_bridge.replace(path, content):
        if candidate:
            candidate.unlink(missing_ok=True)
    elif candidate:
        os.replace(candidate, path)
    else:
        path.write_text(content, encoding="utf-8")


def _write_file(args: dict, project_root: Path | None) -> str:
    path = resolve_path(args["path"], project_root)
    content = args.get("content") or ""
    if REPLAY_ELISION_PREFIX in content:
        return json.dumps({
            "error": "Refusing to write an internal replay-elision marker",
            "path": str(path),
            "changed": False,
            "hint": "Use read_file to inspect the current file, then send only real source text.",
        })
    previous = None
    if path.is_file():
        try:
            previous = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            previous = None
    # A rewrite has to carry the whole file, so the chunk cap only applies to new files.
    if len(content) > MAX_WRITE_CHARS and not path.is_file():
        return json.dumps({"error": "File content exceeds the per-call limit", "changed": False,
                           "path": str(path),
                           "maximum_chars": MAX_WRITE_CHARS, "requested": len(content),
                           "hint": "Create a compact valid foundation, then add coherent chunks with append_file."})
    path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "bytes": len(content.encode("utf-8")),
    }
    if previous is not None:
        result["replaced"] = {"lines_before": previous.count("\n") + 1, "lines_after": content.count("\n") + 1}
    validated_suffixes = {".py", ".js", ".mjs", ".cjs", ".json", ".html", ".htm"}
    if path.suffix.lower() in validated_suffixes:
        candidate: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.olith-write-",
                suffix=path.suffix,
                delete=False,
            ) as handle:
                handle.write(content)
                candidate = Path(handle.name)
            validation = json.loads(_validate_file({"path": str(candidate)}, project_root))
            validation["path"] = str(path)
            # The check ran on a temp copy; naming it sends the model looking for
            # a file it cannot open.
            if isinstance(validation.get("error"), str):
                validation["error"] = validation["error"].replace(str(candidate), str(path))
            result["validation"] = validation
            if not validation.get("ok"):
                result.update({
                    "ok": False,
                    "changed": False,
                    "error": "Write validation failed; the original file was preserved",
                })
                return json.dumps(result)
            if path.exists():
                candidate.chmod(path.stat().st_mode)
            _snapshot_before_write(path)
            _save_source(path, content, candidate)
            candidate = None
        finally:
            if candidate:
                candidate.unlink(missing_ok=True)
    else:
        _snapshot_before_write(path)
        _save_source(path, content)
    result["changed"] = True
    return json.dumps(result)


def _append_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    chunk = str(args.get("content") or "")
    if len(chunk) > MAX_WRITE_CHARS:
        return json.dumps({"error": "Chunk exceeds the per-call limit", "changed": False,
                           "maximum_chars": MAX_WRITE_CHARS, "requested": len(chunk),
                           "hint": "Append a smaller coherent chunk instead of regenerating the entire file."})
    if REPLAY_ELISION_PREFIX in chunk:
        return json.dumps({
            "error": "Refusing to write an internal replay-elision marker",
            "path": str(path),
            "changed": False,
            "hint": "Use read_file to inspect the current file, then send only the next real chunk.",
        })
    existing = path.read_text(encoding="utf-8", errors="replace")
    before = str(args.get("before") or "")
    if (
        not before
        and path.suffix.lower() in {".html", ".htm"}
        and "</html>" in existing.lower()
        and chunk.strip()
    ):
        return json.dumps({
            "error": "Refusing to append source after a closed HTML document",
            "path": str(path),
            "changed": False,
            "hint": "Set before to </script>, </style>, or </body> as appropriate.",
        })
    if before:
        insert_at = existing.rfind(before)
        if insert_at < 0:
            return json.dumps({
                "error": "Insertion marker was not found",
                "path": str(path),
                "before": before,
                "changed": False,
                "hint": "Read the end of the file and use an exact closing marker.",
            })
        updated = existing[:insert_at] + chunk + existing[insert_at:]
        mode = "insert"
    else:
        updated = existing + chunk
        mode = "append"
    if len(updated.encode("utf-8")) > 2_000_000:
        return json.dumps({
            "error": "Resulting file is too large (>2MB)",
            "path": str(path),
            "changed": False,
        })
    _snapshot_before_write(path)
    _save_source(path, updated)
    return json.dumps({
        "ok": True,
        "path": str(path),
        "mode": mode,
        "written_bytes": len(chunk.encode("utf-8")),
        "appended_bytes": len(chunk.encode("utf-8")) if mode == "append" else 0,
        "inserted_bytes": len(chunk.encode("utf-8")) if mode == "insert" else 0,
        "total_bytes": len(updated.encode("utf-8")),
        "revision": _file_revision(path),
        "hint": "Chunk write succeeded. Validate the completed file after the final chunk.",
    })


def _commit_edit(
    path: Path, updated_source: str, result: dict, project_root: Path | None
) -> str:
    """Validate a candidate file and swap it in, or keep the original untouched."""
    if not updated_source.strip() and path.read_text(encoding="utf-8", errors="replace").strip():
        return json.dumps({"error": "Refusing to blank an existing file", "changed": False,
                           "hint": "Keep the source and apply a targeted edit."})
    validated_suffixes = {".py", ".js", ".mjs", ".cjs", ".json", ".html", ".htm"}
    if path.suffix.lower() in validated_suffixes:
        candidate: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.olith-",
                suffix=path.suffix,
                delete=False,
            ) as handle:
                handle.write(updated_source)
                candidate = Path(handle.name)
            validation = json.loads(_validate_file({"path": str(candidate)}, project_root))
            validation["path"] = str(path)
            # The check ran on a temp copy; naming it sends the model looking for
            # a file it cannot open.
            if isinstance(validation.get("error"), str):
                validation["error"] = validation["error"].replace(str(candidate), str(path))
            result["validation"] = validation
            if not validation.get("ok"):
                result.update({
                    "ok": False,
                    "changed": False,
                    "error": "Edit validation failed; the original file was preserved",
                })
                return json.dumps(result)
            candidate.chmod(path.stat().st_mode)
            _snapshot_before_write(path)
            _save_source(path, updated_source, candidate)
            candidate = None
        finally:
            if candidate:
                candidate.unlink(missing_ok=True)
    else:
        _snapshot_before_write(path)
        _save_source(path, updated_source)
    result["changed"] = True
    return json.dumps(result)


def _edit_file_anchored(
    path: Path, source: str, specs: list[dict], project_root: Path | None
) -> str:
    """Apply find/replace edits in order, each against the evolving buffer."""
    updated_source = source
    applied = []
    for index, spec in enumerate(specs):
        find = str(spec.get("find") or "")
        # Require the replace key. A missing key must not silently delete text.
        if spec.get("replace") is not None:
            replace = str(spec["replace"])
        elif spec.get("content") is not None:
            # `content` is the line-range form's field. A model that sends
            # find + content plainly means it as the replacement.
            replace = str(spec["content"])
        else:
            return json.dumps({
                "error": "Anchored edit has no replacement text",
                "path": str(path),
                "changed": False,
                "edit_index": index,
                "find_preview": find[:200],
                "hint": (
                    "Send the new text as \"replace\". To delete the anchor on purpose, "
                    "pass \"replace\": \"\" explicitly."
                ),
            })
        if REPLAY_ELISION_PREFIX in replace:
            return json.dumps({
                "error": "Refusing to write an internal replay-elision marker",
                "path": str(path),
                "changed": False,
                "hint": "Use read_file to inspect the current file, then send only real source text.",
            })
        count = updated_source.count(find)
        if count == 0:
            # Return the closest matching lines so the model can fix its anchor.
            probe = next((ln for ln in find.splitlines() if ln.strip()), "")
            nearest = []
            if probe:
                scored = sorted(
                    (
                        (difflib.SequenceMatcher(None, probe, line).ratio(), number, line)
                        for number, line in enumerate(updated_source.splitlines(), 1)
                    ),
                    key=lambda item: (-item[0], item[1]),
                )[:3]
                nearest = [
                    {"line": number, "text": line[:200], "similarity": round(ratio, 2)}
                    for ratio, number, line in scored if ratio >= 0.5
                ]
            return json.dumps({
                "error": "Anchor text not found",
                "path": str(path),
                "changed": False,
                "edit_index": index,
                "find_preview": find[:200],
                "nearest": nearest,
                "hint": (
                    "find must match the file byte for byte, including indentation and line "
                    "breaks. Earlier edits in this same call have already been applied to the "
                    "buffer. 'nearest' shows the closest lines actually in the file right now. "
                    "copy from those rather than rereading."
                    if nearest else
                    "find must match the file byte for byte, including indentation and line "
                    "breaks. Read the exact lines and copy them verbatim. Earlier edits in "
                    "this same call have already been applied to the buffer."
                ),
            })
        if count > 1:
            return json.dumps({
                "error": "Anchor text is not unique",
                "path": str(path),
                "changed": False,
                "edit_index": index,
                "occurrences": count,
                "find_preview": find[:200],
                "hint": (
                    f"This text appears {count} times. Include more surrounding lines so the "
                    "anchor identifies exactly one place."
                ),
            })
        updated_source = updated_source.replace(find, replace, 1)
        applied.append({
            "edit_index": index,
            "removed_lines": len(find.splitlines()),
            "inserted_lines": len(replace.splitlines()),
        })
    if updated_source == source:
        # Report no change when the matched edit leaves the file unchanged.
        return json.dumps({
            "error": "Edit changed nothing",
            "path": str(path),
            "changed": False,
            "edits_applied": 0,
            "hint": (
                "Every anchor matched but replace was identical to find, so the file is "
                "unchanged. Send the new text you actually want in place of the anchor."
            ),
        })
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "mode": "anchored",
        "edits_applied": len(applied),
        "replaced": applied,
        "file_lines": len(updated_source.splitlines()),
    }
    return _commit_edit(path, updated_source, result, project_root)


def _edit_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    if path.stat().st_size > 2_000_000:
        return json.dumps({"error": "File too large to edit (>2MB)", "path": str(path)})

    source = path.read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines(keepends=True)
    raw_edits = args.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        specs = raw_edits
    elif args.get("find"):
        specs = [{
            "find": args.get("find"),
            "replace": args.get("replace"),
            "content": args.get("content"),
        }]
    else:
        specs = [{
            "start_line": args.get("start_line"),
            "end_line": args.get("end_line"),
            "content": args.get("content"),
        }]
    if len(specs) > MAX_EDIT_RANGES:
        return json.dumps({
            "error": "Too many edit ranges in one batch",
            "path": str(path),
            "requested": len(specs),
            "maximum": MAX_EDIT_RANGES,
            "hint": (
                f"Split this into coherent batches of at most {MAX_EDIT_RANGES} ranges, "
                "validating each batch before continuing."
            ),
        })
    # Match edits by surrounding text so earlier changes do not invalidate line numbers.
    anchored = [sp for sp in specs if isinstance(sp, dict) and sp.get("find")]
    if anchored:
        if len(anchored) != len(specs):
            return json.dumps({
                "error": "Mixed anchored and line-numbered edits in one call",
                "path": str(path),
                "hint": (
                    "Line ranges refer to the original file while anchored edits apply in "
                    "order, so mixing them is ambiguous. Send one kind per call, prefer find/replace."
                ),
            })
        return _edit_file_anchored(path, source, anchored, project_root)

    total_edit_chars = sum(
        len(str(spec.get("content") or ""))
        for spec in specs
        if isinstance(spec, dict)
    )
    if total_edit_chars > MAX_EDIT_TOTAL_CHARS:
        return json.dumps({
            "error": "Edit batch content is too large",
            "path": str(path),
            "requested_chars": total_edit_chars,
            "maximum_chars": MAX_EDIT_TOTAL_CHARS,
            "changed": False,
            "hint": "Split the edit into smaller coherent phases and validate each phase.",
        })

    normalized = []
    for spec in specs:
        if not isinstance(spec, dict):
            return json.dumps({"error": "Each edit must be an object", "path": str(path)})
        start = int(spec.get("start_line") or 0)
        end = int(spec.get("end_line") or 0)
        if start < 1 or end < start or end > len(lines):
            return json.dumps({
                "error": "Invalid line range",
                "path": str(path),
                "start_line": start,
                "end_line": end,
                "file_lines": len(lines),
                "hint": "Read the current range again; earlier edits may have shifted line numbers.",
            })
        replacement = str(spec.get("content") or "")
        if REPLAY_ELISION_PREFIX in replacement:
            return json.dumps({
                "error": "Refusing to write an internal replay-elision marker",
                "path": str(path),
                "changed": False,
                "hint": "Use read_file to inspect the current file, then send only real source text.",
            })
        selected_had_newline = bool(lines[end - 1].endswith(("\n", "\r")))
        if replacement and selected_had_newline and not replacement.endswith(("\n", "\r")):
            replacement += "\n"
        normalized.append({
            "start": start,
            "end": end,
            "lines": replacement.splitlines(keepends=True),
        })

    ordered = sorted(normalized, key=lambda item: item["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if current["start"] <= previous["end"]:
            return json.dumps({
                "error": "Edit ranges overlap",
                "path": str(path),
                "first": [previous["start"], previous["end"]],
                "second": [current["start"], current["end"]],
            })

    updated = list(lines)
    for edit in reversed(ordered):
        updated[edit["start"] - 1 : edit["end"]] = edit["lines"]
    updated_source = "".join(updated)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "edits_applied": len(ordered),
        "replaced": [
            {
                "start_line": edit["start"],
                "end_line": edit["end"],
                "inserted_lines": len(edit["lines"]),
            }
            for edit in ordered
        ],
        "file_lines": len(updated),
    }
    return _commit_edit(path, updated_source, result, project_root)


def _search_files_grep(grep_bin: str, base: Path, pattern: str, glob: str) -> str | None:
    """grep-backed search. Returns None if grep could not run, so caller can fall back."""
    # -H keeps the filename when base is a single file, so the parser below still works.
    cmd = [grep_bin, "-r", "-I", "-n", "-i", "-H"]
    anchor = base.parent if base.is_file() else base
    # Prefer PCRE so Python-style patterns (\d, \b, lookarounds) behave the same.
    probe = subprocess.run([grep_bin, "-P", "x", "-q"], input="x", text=True, capture_output=True)
    cmd.append("-P" if probe.returncode in (0, 1) else "-E")
    # Use grep only for directory exclusions. Apply suffix and glob filters in
    # Python because grep versions handle their precedence differently.
    for d in sorted(IGNORE_DIRS):
        cmd.append(f"--exclude-dir={d}")
    cmd.append("--exclude-dir=models--*")
    cmd += ["-e", pattern, str(base)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except Exception:
        return None
    # 0 = matches, 1 = no matches; anything else means grep itself failed.
    if proc.returncode not in (0, 1):
        return None
    from fnmatch import fnmatch
    inc = None
    if glob and glob not in ("**/*", "**"):
        inc = glob[3:] if glob.startswith("**/") else glob
    hits = []
    truncated = False
    skipped = 0
    for line in (proc.stdout or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        fpath, lno, text = parts
        fp = Path(fpath)
        if fp.suffix.lower() in BINARY_SUFFIXES:
            skipped += 1
            continue
        try:
            rel = str(fp.relative_to(anchor)).replace("\\", "/")
        except ValueError:
            rel = fpath
        if inc and not (fnmatch(rel, glob) or fnmatch(rel, inc) or fnmatch(fp.name, inc)):
            skipped += 1
            continue
        try:
            lineno = int(lno)
        except ValueError:
            continue
        hits.append({"file": rel, "line": lineno, "text": text.strip()[:200]})
        if len(hits) >= 80:
            truncated = True
            break
    out: dict = {"hits": hits}
    if skipped:
        out["skipped_vendor_files"] = skipped
    if truncated:
        out["truncated"] = True
        out["hint"] = "80-hit cap reached, narrow with a stricter pattern, `path`, or `glob`."
    return json.dumps(out, indent=2)


def _search_files(args: dict, project_root: Path | None) -> str:
    base = resolve_path(args.get("path") or ".", project_root)
    mode = args.get("mode") or "regex"
    if mode != "regex":
        if not project_root:
            return json.dumps({"error": "Indexed search needs an active project."})
        root = resolve_path(str(project_root), project_root)
        try:
            subpath = base.relative_to(root).as_posix()
        except ValueError:
            return json.dumps({"error": "Indexed search stays inside the active project."})
        index = repo_index.ProjectIndex(root)
        query = str(args.get("pattern") or "")
        if mode == "map":
            return json.dumps({"map": index.repo_map(query), "hint": "Read current source before editing."})
        settings = load_settings()
        if mode == "references" and settings.get("language_servers", True):
            definitions = index.search(query, "symbols", 20, subpath=subpath, glob=args.get("glob") or "")["hits"]
            if len(definitions) == 1:
                entry = definitions[0]
                target = root / entry["file"]
                source = target.read_text(encoding="utf-8").splitlines()
                wanted = entry["name"].split(".")[-1]
                for line in range(entry["line"] - 1, min(entry["end_line"], entry["line"] + 12)):
                    match = re.search(r"\b" + re.escape(wanted) + r"\b", source[line])
                    if match:
                        try:
                            position = len(source[line][:match.start()].encode("utf-16-le")) // 2
                            result = language_servers.inspect_file(root, target, "references", line + 1, position)
                            if result.get("available"):
                                return json.dumps(result)
                        except Exception:
                            pass
                        break
        embedder = repo_index.OllamaEmbedder(settings) if settings.get("semantic_search") and mode == "hybrid" else None
        return json.dumps(index.search(query, mode, args.get("limit", 8), subpath=subpath,
                                       glob=args.get("glob") or "", embedder=embedder))
    pattern = args["pattern"]
    glob = args.get("glob") or "**/*"
    rx = re.compile(pattern, re.IGNORECASE)
    hits = []
    skipped_vendor = 0
    # Prune ignored folders before entering them. Use grep when available,
    # with a Python walk as the fallback.
    grep_bin = shutil_which("grep")
    if grep_bin:
        got = _search_files_grep(grep_bin, base, pattern, glob)
        if got is not None:
            return got

    deadline = time.monotonic() + 12.0  # soft budget: never stall the agent loop
    matcher = None
    if glob and glob not in ("**/*", "**"):
        from fnmatch import fnmatch as _fnmatch
        pat = glob[3:] if glob.startswith("**/") else glob
        matcher = lambda rel: _fnmatch(rel, glob) or _fnmatch(Path(rel).name, pat)

    anchor = base.parent if base.is_file() else base
    walk = [(str(anchor), [], [base.name])] if base.is_file() else os.walk(base)
    for dirpath, dirnames, filenames in walk:
        here = Path(dirpath)
        # Prune before descending.
        keep = []
        for d in dirnames:
            if d in IGNORE_DIRS or d.startswith("models--") or d.startswith("."):
                skipped_vendor += 1
                continue
            keep.append(d)
        dirnames[:] = keep

        if time.monotonic() > deadline:
            return json.dumps({
                "hits": hits, "truncated": True, "timed_out": True,
                "skipped_vendor_files": skipped_vendor,
                "hint": "Search budget exceeded, narrow it with `path` or a stricter `glob` (e.g. **/*.py).",
            }, indent=2)
        for fn in filenames:
            p = here / fn
            if p.suffix.lower() in BINARY_SUFFIXES:
                skipped_vendor += 1
                continue
            try:
                rel = str(p.relative_to(anchor)).replace("\\", "/")
            except ValueError:
                continue
            if matcher and not matcher(rel):
                continue
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append({"file": rel, "line": i, "text": line.strip()[:200]})
                    if len(hits) >= 80:
                        return json.dumps({
                            "hits": hits, "truncated": True,
                            "skipped_vendor_files": skipped_vendor,
                            "hint": "80-hit cap reached, narrow with a stricter pattern or a path/glob.",
                        }, indent=2)
    out = {"hits": hits}
    if skipped_vendor:
        out["skipped_vendor_files"] = skipped_vendor
    return json.dumps(out, indent=2)


# Commands that need review in safe_auto. Other commands can run automatically.
_DESTRUCTIVE_BINS = frozenset({
    # irreversible data loss
    "rm", "rmdir", "shred", "truncate",
    # disks and filesystems
    "dd", "mkfs", "fdisk", "parted", "mkswap", "wipefs",
    # privilege escalation and accounts
    "sudo", "su", "doas", "passwd", "usermod", "userdel", "groupdel", "visudo",
    # machine state
    "shutdown", "reboot", "halt", "poweroff", "init",
})

# Commands that write where they are pointed. Harmless in a project, not in /etc.
_WRITES_FILES = frozenset({
    "cp", "mv", "tee", "install", "ln", "chmod", "chown", "chgrp", "rsync",
})

# git subcommands that publish work or discard it irreversibly
_GIT_REVIEW = frozenset({"push", "reset", "clean"})

# `curl … | sh` is remote code execution however friendly the URL looks.
_PIPE_TO_SHELL = re.compile(
    r"\b(?:curl|wget|fetch)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b"
)

# Commands that run another command, so their arguments are program names too.
_COMMAND_RUNNERS = frozenset({
    "find", "xargs", "sh", "bash", "zsh", "env", "nohup", "time", "watch", "parallel",
})

# Raw text that is dangerous regardless of tokenization.
_REVIEW_PATTERNS = ("> /etc", "> /usr", "> /bin", ">/etc", ">/usr", ">/bin", ":(){")

# Writing into any of these needs a human, whatever the command claims to do.
_SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/opt",
    "/var", "/root", "/sys", "/proc", "/dev",
)


def _is_destructive(binary: str) -> bool:
    """Match programs on the review list, including variants such as mkfs.ext4."""
    name = (binary or "").lower()
    return name in _DESTRUCTIVE_BINS or name.split(".", 1)[0] in {"mkfs", "fsck"}


def _writes_system_path(words: list[str]) -> bool:
    """Whether a write target lands outside the user's own files."""
    return any(
        not word.startswith("-") and word.startswith(_SYSTEM_PREFIXES)
        for word in words
    )


# Operators that end one command segment and begin the next.
_SHELL_SEPARATORS = frozenset({"|", "||", "&&", "&", ";", ";;", "\n", "(", ")"})


def _shell_segments(cmd: str) -> list[list[str]] | None:
    """Split shell commands into argument lists while preserving quoted separators.

    Return None if the command cannot be parsed.
    """
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        # Remove redirection operators and targets before checking program names.
        if tok and ("<" in tok or ">" in tok) and all(ch in "<>&" for ch in tok):
            skip_next = True
            continue
        if tok in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments

_INSPECTION_SHELL_BINS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "find", "grep", "rg",
    "which", "pwd", "env", "date", "tree", "du", "df", "basename", "dirname",
    "realpath", "sort", "uniq", "cut", "sed", "awk", "diff", "cmp", "md5sum",
    "sha256sum", "jq", "git",
})


def shell_is_inspection(cmd: str) -> bool:
    """Return whether every shell segment only reads/searches existing state."""
    text = (cmd or "").strip()
    if not text:
        return False
    segments = _shell_segments(text)
    if segments is None:
        return False
    saw_segment = False
    for words in segments:
        while words and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]):
            words = words[1:]
        if not words or Path(words[0]).name not in _INSPECTION_SHELL_BINS:
            return False
        saw_segment = True
    return saw_segment


def _line_review(text: str) -> str | None:
    """Risk in the command line as a whole, which no single program can vouch for."""
    low = text.lower()
    for bad in _REVIEW_PATTERNS:
        if bad in low:
            return f"writes to a system path ({bad.strip()!r})"
    if _PIPE_TO_SHELL.search(text):
        return "pipes downloaded content into a shell"
    if _shell_segments(text) is None:
        return "unbalanced quotes"
    # Inspect command substitutions for commands that need review.
    for inner in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", text):
        nested = (inner[0] or inner[1] or "").strip()
        if nested:
            verdict, why = classify_shell(nested)
            if verdict == "review":
                return f"command substitution runs something risky: {why}"
    return None


def _segment_review(words: list[str]) -> tuple[str, str] | None:
    """The (program, reason) when one segment of a command needs review."""
    while words and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]):
        words = words[1:]
    if not words:
        return None
    binary = Path(words[0]).name
    rest = words[1:]
    if _is_destructive(binary):
        return binary, f"{binary!r} can destroy data or change the system"
    # A runner's arguments are program names too (find -exec rm, xargs shred).
    if binary in _COMMAND_RUNNERS and any(
        _is_destructive(Path(word).name) for word in rest
    ):
        risky = next(word for word in rest if _is_destructive(Path(word).name))
        return binary, f"runs {risky!r} through {binary!r}"
    if binary in _WRITES_FILES and _writes_system_path(rest):
        return binary, f"{binary} writes to a system path"
    if binary == "git":
        sub = rest[0] if rest else ""
        if sub in _GIT_REVIEW:
            return binary, f"git {sub} publishes or discards work"
        if sub == "reset" and any(a == "--hard" for a in rest):
            return binary, "git reset --hard discards uncommitted work"
        if sub == "clean" and any(a.startswith("-") and "f" in a for a in rest):
            return binary, "git clean -f deletes untracked files"
    return None


def shell_review_binaries(cmd: str) -> list[str]:
    """Return the programs that need approval. Return none when the command text itself needs review."""
    text = (cmd or "").strip()
    if not text or _line_review(text):
        return []
    hits = (_segment_review(words) for words in _shell_segments(text))
    return list(dict.fromkeys(hit[0] for hit in hits if hit))


def classify_shell(cmd: str) -> tuple[str, str]:
    """Return a shell command's classification and reason.

    In safe_auto mode, review destructive commands, privilege changes, system
    changes and downloaded code. Other commands can run without a prompt.
    """
    text = (cmd or "").strip()
    if not text:
        return "review", "empty command"
    why = _line_review(text)
    if why:
        return "review", why
    for words in _shell_segments(text):
        hit = _segment_review(words)
        if hit:
            return "review", hit[1]
    return "safe", "no destructive or privileged operation"


_GIT_LOGIN_FAILURE = re.compile(
    r"(?:could not read (?:Username|Password) for|Authentication failed for) 'https://github\.com"
)


def _run_shell(args: dict, project_root: Path | None) -> str:
    cwd = project_root or Path.home()
    cmd = args.get("command") or ""
    banned = ["format ", "shutdown", "rm -rf /", "del /s /q C:\\", "Remove-Item -Recurse -Force C:\\"]
    low = cmd.lower()
    if any(b.lower() in low for b in banned):
        return json.dumps({"error": "Command blocked for safety"})
    timeout = int(args.get("timeout_sec") or 60)
    env = os.environ.copy()
    if os.name == "nt":
        shell = shutil_which("powershell.exe") or shutil_which("powershell")
        command = (
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd]
            if shell
            else ["cmd.exe", "/d", "/s", "/c", cmd]
        )
    else:
        # Use the same Python for pip and tests. Prefer a conda install with pip
        # when system Python lacks it.
        env["PATH"] = _toolchain_path(env.get("PATH", ""))
        # Lets `npm i -g` succeed without sudo instead of failing on /usr/lib.
        env.setdefault("npm_config_prefix", str(Path.home() / ".npm-global"))
        if re.search(r"(^|[;&|]\s*)python(\s|$)", cmd) and not shutil_which("python", env["PATH"]):
            cmd = re.sub(r"(^|[;&|]\s*)python(\s|$)", r"\1python3\2", cmd)
        command = ["/bin/bash", "-lc", "set -o pipefail\n" + cmd]
    try:
        proc = subprocess.run(
            command,
            shell=False,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        # Keep the output up to the cap, then let the loop save it to disk.
        # Build errors near the start must stay available.
        out = (proc.stdout or "")[-SHELL_OUTPUT_HARD_CAP:]
        err = (proc.stderr or "")[-SHELL_OUTPUT_HARD_CAP:]
        result = {"exit_code": proc.returncode, "stdout": out, "stderr": err, "cwd": str(cwd)}
        if proc.returncode and _GIT_LOGIN_FAILURE.search(out + err):
            result["hint"] = "Shell git has no GitHub login. Use git_status with action push, which signs in with the connected account."
        return json.dumps(result)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Timed out after {timeout}s"})


def _map_inline_line(line_no: int, line_map: list[tuple[int, int]]) -> int:
    """Translate a concatenated-script line back to its line in the HTML file."""
    html_line = line_no
    for concat_start, html_start in line_map:
        if line_no >= concat_start:
            html_line = html_start + (line_no - concat_start)
        else:
            break
    return html_line


# Drop Node's internal stack after the useful syntax error.
_NODE_NOISE = re.compile(
    r"^[ \t]*(?:at\s+.*|Node\.js\s+v[\d.]+.*)$", re.MULTILINE
)


def _clean_node_error(
    raw: str, target: Path, real: Path, line_map: list[tuple[int, int]]
) -> str:
    """Map node --check errors in combined inline scripts back to the HTML file's lines."""
    text = raw
    if line_map:
        pattern = re.escape(str(target)) + r":(\d+)"

        def _fix(match: re.Match) -> str:
            return f"{real}:{_map_inline_line(int(match.group(1)), line_map)}"

        text = re.sub(pattern, _fix, text)
    text = text.replace(str(target), str(real))
    text = _NODE_NOISE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _validate_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    if path.stat().st_size > 2_000_000:
        return json.dumps({"ok": False, "error": "File too large to validate", "path": str(path)})

    suffix = path.suffix.lower()
    source = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".py":
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            return json.dumps({
                "ok": False,
                "path": str(path),
                "error": f"line {exc.lineno}: {exc.msg}",
            })
        return json.dumps({"ok": True, "path": str(path), "validator": "Python compile"})

    if suffix == ".json":
        try:
            json.loads(source)
        except json.JSONDecodeError as exc:
            return json.dumps({
                "ok": False,
                "path": str(path),
                "error": f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
            })
        return json.dumps({"ok": True, "path": str(path), "validator": "JSON parser"})

    javascript = source
    validator = "Node.js syntax check"
    # Maps a line in the concatenated script back to its line in the HTML file.
    line_map: list[tuple[int, int]] = []
    if suffix in {".html", ".htm"}:
        blocks: list[tuple[str, int]] = []
        for match in re.finditer(
            r"<script(?:\s[^>]*)?>(.*?)</script\s*>", source, re.DOTALL | re.IGNORECASE
        ):
            body = match.group(1)
            if not body.strip():
                continue
            blocks.append((body, source.count("\n", 0, match.start(1)) + 1))
        opened = len(re.findall(r"<script(?:\s[^>]*)?>", source, re.IGNORECASE))
        closed = len(re.findall(r"</script\s*>", source, re.IGNORECASE))
        if opened > closed:
            # Reject broken script tags so damaged HTML cannot skip the JavaScript check.
            return json.dumps({
                "ok": False,
                "path": str(path),
                "validator": "HTML scan",
                "error": (
                    f"Unclosed <script> block: {opened} opening tag(s), {closed} closing tag(s). "
                    "The file's JavaScript cannot be checked and the document is malformed."
                ),
                "hint": "Restore the missing </script> (and </body></html>) before editing further.",
            })
        if not blocks:
            return json.dumps({"ok": True, "path": str(path), "validator": "HTML scan", "note": "No inline scripts"})
        # Track each inline script's offset to map errors back to the HTML file.
        parts: list[str] = []
        cursor = 1
        for index, (body, html_start) in enumerate(blocks):
            if index:
                parts.append("\n;\n")
                cursor += 2
            line_map.append((cursor, html_start))
            parts.append(body)
            cursor += body.count("\n")
        javascript = "".join(parts)
        validator = "Inline JavaScript syntax check"
    elif suffix not in {".js", ".mjs", ".cjs"}:
        return json.dumps({
            "ok": False,
            "path": str(path),
            "error": f"No built-in validator for {suffix or 'files without an extension'}",
        })

    node = shutil_which("node")
    if not node:
        return json.dumps({"ok": False, "path": str(path), "error": "Node.js is not installed"})
    temp_path: Path | None = None
    target = path
    try:
        if suffix in {".html", ".htm"}:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.olith-inline-",
                suffix=".js",
                delete=False,
            ) as handle:
                handle.write(javascript)
                temp_path = Path(handle.name)
            target = temp_path
        proc = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raw = (proc.stderr or proc.stdout or "").strip()
            return json.dumps({
                "ok": False,
                "path": str(path),
                "validator": validator,
                "error": _clean_node_error(raw, target, path, line_map)[-2000:] or None,
            })

        # Use ESLint's no-undef check when available. Ignore unrelated ESLint errors.
        eslint = shutil_which("eslint")
        if eslint:
            lint_proc = subprocess.run(
                [
                    eslint,
                    "--no-eslintrc",
                    "--no-ignore",
                    "--env", "browser,es6,node",
                    "--parser-options", "ecmaVersion:2020",
                    "--rule", "no-undef: error",
                    target.name,
                ],
                cwd=str(target.parent),
                capture_output=True,
                text=True,
                timeout=30,
            )
            lint_output = (lint_proc.stdout or "") + (lint_proc.stderr or "")
            if lint_proc.returncode != 0 and "no-undef" in lint_output:
                return json.dumps({
                    "ok": False,
                    "path": str(path),
                    "validator": f"{validator} + ESLint no-undef",
                    "error": lint_output.strip()[-3000:],
                })
        return json.dumps({
            "ok": True,
            "path": str(path),
            "validator": validator if not eslint else f"{validator} + ESLint no-undef",
            "error": None,
        })
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def shutil_which(name: str, path: str | None = None) -> str | None:
    from shutil import which
    return which(name, path=path)


def _conda_bin() -> str | None:
    """Bin dir of a conda install whose python3 and pip are the same install."""
    roots: list[Path] = []
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        roots.append(Path(prefix))
    roots += [Path.home() / "anaconda3", Path.home() / "miniconda3"]
    for root in roots:
        bin_dir = root / "bin"
        if (bin_dir / "python3").exists() and (bin_dir / "pip").exists():
            return str(bin_dir)
    return None


def _toolchain_path(base: str = "") -> str:
    """Build the PATH used by shell commands and toolchain checks.

    Desktop launches may need common user toolchain folders added explicitly.
    """
    extra = [p for p in (_conda_bin(), "/usr/bin", "/usr/local/bin") if p]
    extra += [
        str(Path.home() / d)
        for d in (".local/bin", ".npm-global/bin", ".cargo/bin", "go/bin", ".deno/bin", ".bun/bin")
    ]
    return os.pathsep.join(extra + [base or os.environ.get("PATH", "")])


# name → (binaries, how to get it without breaking the user's machine)
TOOLCHAINS = (
    ("Python", ("python3", "pip"), "Already present. pip comes from the same interpreter as python3."),
    ("Node / JavaScript", ("node", "npm", "npx"), "sudo apt install nodejs npm. Ask the user to run it."),
    ("TypeScript", ("tsc", "tsx"), "npm i -g typescript tsx. No sudo needed, Olith points npm at ~/.npm-global. Or run one-off with npx tsx file.ts."),
    ("C / C++", ("gcc", "g++", "make", "cmake"), "sudo apt install build-essential cmake. Ask the user to run it."),
    ("Java", ("java", "javac"), "sudo apt install default-jdk. Ask the user to run it."),
    ("Rust", ("cargo", "rustc"), "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh. Installs to ~/.cargo/bin, no sudo."),
    ("Go", ("go",), "sudo apt install golang. Ask the user to run it."),
    (".NET", ("dotnet",), "sudo apt install dotnet-sdk-8.0. Ask the user to run it."),
    ("PHP", ("php",), "sudo apt install php-cli. Ask the user to run it."),
    ("Ruby", ("ruby",), "sudo apt install ruby. Ask the user to run it."),
    ("Perl", ("perl",), "Ships with Ubuntu."),
    ("SQLite", ("sqlite3",), "sudo apt install sqlite3. Ask the user to run it."),
)


def _check_toolchains() -> str:
    """Report which language toolchains this machine can actually build and run."""
    path = _toolchain_path()
    ready, missing = [], []
    for label, bins, hint in TOOLCHAINS:
        found = {}
        for b in bins:
            exe = shutil_which(b, path)
            if not exe:
                continue
            version = ""
            try:
                pr = subprocess.run(
                    [exe, "--version"], capture_output=True, text=True, timeout=8,
                    env={**os.environ, "PATH": path},
                )
                version = ((pr.stdout or pr.stderr or "").strip().splitlines() or [""])[0][:60]
            except Exception:
                pass
            found[b] = version or exe
        if found:
            ready.append({"toolchain": label, "available": found,
                          "incomplete": [b for b in bins if b not in found] or None})
        else:
            missing.append({"toolchain": label, "install": hint})
    return json.dumps({
        "ready": ready,
        "missing": missing,
        "note": (
            "Olith cannot run sudo. For anything marked 'ask the user', use ask_user to "
            "request it rather than attempting the install. npm -g, pip and rustup work "
            "without sudo and may be used directly."
        ),
    }, indent=2)


def _git_status(project_root: Path | None) -> str:
    if not project_root:
        return json.dumps({"error": "No project attached; git status needs a project root."})
    def run(cmd: list[str]) -> str:
        try:
            p = subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True, timeout=20)
            return (p.stdout or p.stderr or "").strip()
        except Exception as e:
            return str(e)
    report = {}
    pathspec = []
    top = run(["git", "rev-parse", "--show-toplevel"])
    if top.startswith("/") and Path(top).resolve() != Path(project_root).resolve():
        # Otherwise a stray parent repo lists its whole tree as untracked here.
        pathspec = ["--", "."]
        report["warning"] = (
            f"This project is a subfolder of the repository at {top}, not its own repository. "
            f"Commits here go into {top} and GitHub actions will refuse. "
            "To give it its own repository, run git init -b main in the project."
        )
    report["status"] = run(["git", "status", "-sb", *pathspec])
    report["log"] = run(["git", "log", "-5", "--oneline"])
    report["remotes"] = run(["git", "remote", "-v"])
    return json.dumps(report, indent=2)


def _remember(args: dict, project_id: str | None) -> str:
    from storage import add_memory_note
    scope = args.get("scope") or "global"
    if scope == "project":
        if not project_id:
            return json.dumps({"error": "No project attached"})
        scope = f"project_{project_id}"
    add_memory_note(scope, args.get("text") or "")
    return json.dumps({"ok": True, "scope": scope})


# Parse native tool calls and calls written into the reply body.
TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*\{(.*?)\}\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)
# Mistral's template; the payload is a JSON array.
TOOL_CALLS_PREFIX_RE = re.compile(r"\[TOOL_CALLS\]\s*(\[.*\])\s*$", re.DOTALL)
FENCED_JSON_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def _known_tool_names() -> set[str]:
    return {t["function"]["name"] for t in TOOL_DEFS}


def _as_tool_call(obj: object) -> dict | None:
    """One parsed object, if it actually names a registered tool."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or obj.get("function")
    if isinstance(name, dict):
        obj, name = name, name.get("name")
    raw_name, name = name, canonical_tool_name(name)
    if not isinstance(name, str) or name not in _known_tool_names():
        return None
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters")
    if args is None:
        args = obj.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    return {
        "id": name,
        "name": name,
        "arguments": apply_alias_defaults(raw_name, name, args),
    }


def _calls_from_payload(payload: object) -> list[dict]:
    items = payload if isinstance(payload, list) else [payload]
    return [c for c in (_as_tool_call(i) for i in items) if c]


def _trailing_json(text: str) -> str | None:
    """Read a JSON object or array that reaches the end of the reply.

    Ignore calls quoted inside prose that continues after the JSON.
    """
    text = (text or "").rstrip()
    if not text or text[-1] not in "}]":
        return None
    for i, ch in enumerate(text):
        if ch in "{[":
            return text[i:]
    return None


def parse_content_tool_calls(content: str) -> list[dict]:
    """Read tool calls from reply text. Accept explicit markers or trailing JSON."""
    text = (content or "").strip()
    if not text:
        return []
    calls: list[dict] = []
    marker = TOOL_CALLS_PREFIX_RE.search(text)
    if marker:
        try:
            calls.extend(_calls_from_payload(json.loads(marker.group(1))))
        except json.JSONDecodeError:
            pass
    for block in FENCED_JSON_RE.findall(text):
        try:
            calls.extend(_calls_from_payload(json.loads(block)))
        except json.JSONDecodeError:
            pass
    if not calls:
        tail = _trailing_json(text)
        if tail:
            try:
                calls.extend(_calls_from_payload(json.loads(tail)))
            except json.JSONDecodeError:
                pass
    return calls


def extract_tool_calls(message: dict) -> list[dict]:
    calls = []
    native = message.get("tool_calls") or []
    for tc in native:
        fn = tc.get("function") or {}
        name = fn.get("name")
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {"raw": raw_args}
        else:
            args = raw_args or {}
        if name:
            raw_name, name = name, canonical_tool_name(name)
            calls.append({
                "id": tc.get("id") or name,
                "index": fn.get("index", tc.get("index")),
                "name": name,
                "arguments": apply_alias_defaults(raw_name, name, args),
            })
    content = message.get("content") or ""
    for m in TOOL_CALL_RE.finditer(content):
        try:
            obj = json.loads("{" + m.group(1) + "}")
            raw_name = obj.get("name") or obj.get("tool")
            name = canonical_tool_name(raw_name)
            args = obj.get("arguments") or obj.get("parameters") or {}
            if name:
                calls.append({
                    "id": name,
                    "name": name,
                    "arguments": apply_alias_defaults(raw_name, name, args),
                })
        except Exception:
            pass
    # Alternative: ```tool ... ```
    alt = re.findall(r"```tool\s*(\{.*?\})\s*```", content, re.DOTALL)
    for block in alt:
        try:
            obj = json.loads(block)
            raw_name = obj.get("name")
            name = canonical_tool_name(raw_name)
            if name:
                calls.append({
                    "id": name,
                    "name": name,
                    "arguments": apply_alias_defaults(
                        raw_name, name, obj.get("arguments") or {}
                    ),
                })
        except Exception:
            pass
    # Use recovered calls only when no native calls were usable, avoiding duplicate execution.
    if not calls:
        calls.extend(parse_content_tool_calls(content))
    return calls


def looks_like_broken_tool_call(content: str) -> str | None:
    """Find the tool name in a malformed call so the loop can ask for a retry. Never guess its arguments."""
    text = (content or "").strip()
    # Either the reply is the call, or the call runs to the end of it. A call
    # quoted mid-sentence has prose after it and is left alone.
    candidate = _trailing_json(text) or (text if text and text[0] in "{[" else None)
    if not candidate or '"name"' not in candidate:
        return None
    if parse_content_tool_calls(text):
        return None
    text = candidate
    # Recognize aliases when reporting malformed calls too.
    for name in list(_known_tool_names()) + list(TOOL_ALIASES):
        if f'"{name}"' in text:
            return canonical_tool_name(name)
    return None


def normalize_questions(args: dict) -> list[dict]:
    """Validate/clean an ask_user payload. Returns [] if nothing usable."""
    raw = (args or {}).get("questions")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, q in enumerate(raw[:4]):
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or q.get("text") or "").strip()
        if not text:
            continue
        opts = []
        seen = set()
        for o in (q.get("options") or [])[:4]:
            if isinstance(o, str):
                label, desc = o.strip(), ""
            elif isinstance(o, dict):
                label = str(o.get("label") or o.get("value") or "").strip()
                desc = str(o.get("description") or "").strip()
            else:
                continue
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            opts.append({"label": label, "description": desc})
        # A question with no real choices is still askable as free text.
        out.append({
            "id": str(q.get("id") or f"q{i+1}"),
            "question": text,
            "header": str(q.get("header") or "")[:12],
            "multi_select": bool(q.get("multi_select") or q.get("multiSelect")),
            "options": opts,
        })
    return out


def dedupe_tool_calls(calls: list[dict], limit: int = 8) -> list[dict]:
    """Drop duplicate (name, arguments) pairs and cap tools per model step."""
    out: list[dict] = []
    seen: set[str] = set()
    for c in calls or []:
        name = c.get("name") or ""
        if not name:
            continue
        key = name + "::" + json.dumps(c.get("arguments") or {}, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _file_revision(path: Path) -> str:
    """Cheap identity for the exact on-disk version a read came from."""
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def normalize_read_request(
    args: dict, project_root: Path | None
) -> tuple[str, int, int, str] | None:
    """Normalize a read's path, range and revision for comparison.

    Leave invalid or binary reads to the file tool's normal error handling.
    """
    try:
        path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
        if err or path is None or path.stat().st_size > 2_000_000:
            return None
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return None
        line_count = len(raw.decode("utf-8", errors="replace").splitlines())
        start = max(1, int(args.get("start_line") or 1))
        end = min(int(args.get("end_line") or line_count), line_count)
        if end < start:
            return None
        return str(path), start, end, _file_revision(path)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of line ranges, coalescing anything adjacent or overlapping."""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def intersection_size(start: int, end: int, intervals: list[tuple[int, int]]) -> int:
    """How many lines of [start, end] are already covered by `intervals`."""
    return sum(
        max(0, min(end, covered_end) - max(start, covered_start) + 1)
        for covered_start, covered_end in intervals
    )


_INCOMPLETE_RE = re.compile(
    r"(?i)\b("
    r"let me|i('ll| will)|i('m| am) going to|next i|need to|"
    r"checking|looking (at|into|for)|examining|inspecting|reading|"
    r"first[,.]?\s|then i|after (that|this)|continue|todo|to[- ]do|"
    r"i('ll| will) (check|look|read|open|run|search|try)|"
    r"one (moment|sec)|hang on|give me a (sec|moment)|"
    # Treat present-continuous promises such as "I'm writing it now" as unfinished work.
    r"i('m| am) (writing|creating|building|making|adding|implementing|updating|fixing|working)|"
    r"not yet\b"
    r")\b"
)
# A reply that is only a promise to act, e.g. "Writing it now."
_PROMISE_START_RE = re.compile(
    r"(?i)^(writing|creating|building|making|adding|implementing|updating|fixing|"
    r"generating|drafting|starting)\b"
)
# A long reply may still be a plan. Check its ending for a promised action.
_PROMISE_TAIL_RE = re.compile(
    r"(?i)\b("
    r"let me (start|begin|now|first|go ahead|proceed|get started)|"
    r"let('s| us) (start|begin|get started|proceed|do this)|"
    r"i('ll| will) (start|begin|now|go ahead|proceed|create|write|build|implement|"
    r"add|update|fix|run|generate|draft|set up)|"
    r"i('m| am) going to (start|begin|create|write|build|implement|add|update|fix|run)|"
    r"(starting|beginning|proceeding) (with|by|now)|"
    r"first[,]? (i|let me|we)('ll| will| need)?"
    r")\b"
)
# Only the closing lines count: "I'll create the file" in the middle of a report
# that then shows the created file is a narration of work already done.
_PROMISE_TAIL_WINDOW = 300


def ends_with_promise(content: str) -> bool:
    """True when a reply signs off by promising the next action instead of taking it."""
    text = (content or "").strip()
    return bool(text) and bool(_PROMISE_TAIL_RE.search(text[-_PROMISE_TAIL_WINDOW:]))


def looks_like_final_answer(content: str) -> bool:
    """True when the model already wrote a substantial deliverable (not mid-narration)."""
    text = (content or "").strip()
    if len(text) < 400:
        return False
    if text.endswith(("...", "…", ":")):
        return False
    # Short planning openers are not final even if long later. Rare, so length wins.
    return True


# Sign-offs that contain trigger words but mean the opposite of "still working".
# "Let me know if you want" used to kick a finished run back into the loop.
_SIGNOFF_RE = re.compile(
    r"(?i)\b(let me know|lmk|let me know if|if you (want|need|would like)|"
    r"happy to|want me to|shall i|anything else|hope (this|that) helps)\b"
)


def looks_incomplete(content: str) -> bool:
    """True when the model is still working / planning instead of finishing."""
    text = (content or "").strip()
    if not text:
        return True
    # Checked before the sign-off and length bail-outs: a plan that ends on a
    # promise is a failed action no matter how long or how politely it closes.
    if ends_with_promise(text):
        return True
    # A sign-off is a finished answer, however short.
    if _SIGNOFF_RE.search(text):
        return False
    # Do not retry a long answer just because it mentions planning words.
    if looks_like_final_answer(text):
        return False
    if _INCOMPLETE_RE.search(text):
        return True
    if len(text) < 200 and _PROMISE_START_RE.search(text):
        return True
    # Trailing ellipsis / colon often means "more coming"
    if text.endswith(("...", "…", ":")):
        return True
    # Accept short replies that deliver a path or command after doing the work.
    delivers = re.search(r"(?i)(^|\s)(/|~/|\./)\S|\b(python3?|node|npx|bash|sh|cargo|go|java|make)\b", text)
    if len(text) < 80 and not text.endswith((".", "!", "?")) and not delivers:
        return True
    return False


_INTERNAL_CONTINUATION_PLACEHOLDERS = {
    "(continuing tools for the same job)",
    "(working notes, continue the same job; not a final answer yet)",
}


def is_internal_continuation_placeholder(content: str) -> bool:
    """Recognize internal loop instructions, including older dash variants."""
    text = re.sub(r"\s*[-\u2013\N{EM DASH},]\s*", ", ", (content or "").strip().lower())
    return text in _INTERNAL_CONTINUATION_PLACEHOLDERS


def to_ollama_tool_calls(calls: list[dict], *, elide_large_inputs: bool = False) -> list[dict]:
    """Normalize saved calls for Ollama. Keep function.arguments as an object."""
    out = []
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        if c.get("function"):
            fn = dict(c.get("function") or {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"raw": args}
            fn["arguments"] = copy.deepcopy(args) if isinstance(args, dict) else {}
            if elide_large_inputs:
                _elide_large_replay_input(fn)
            entry = {k: v for k, v in c.items() if k != "function"}
            entry["function"] = fn
            if "type" not in entry:
                entry["type"] = "function"
            out.append(entry)
            continue
        name = c.get("name")
        if not name:
            continue
        args = c.get("arguments") if "arguments" in c else c.get("parameters")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {}
        fn = {"name": name, "arguments": copy.deepcopy(args)}
        if elide_large_inputs:
            _elide_large_replay_input(fn)
        function = fn
        if c.get("index") is not None:
            function["index"] = c["index"]
        entry = {
            "type": "function",
            "function": function,
        }
        if c.get("id") and c.get("id") != name:
            entry["id"] = c["id"]
        out.append(entry)
    return out


def _elide_large_replay_input(fn: dict) -> None:
    """Bound large historical tool inputs without modifying the stored transcript."""
    if fn.get("name") not in {"write_file", "append_file"}:
        return
    args = fn.get("arguments")
    if not isinstance(args, dict):
        return
    body = args.get("content")
    bodies = [(args, "content", body)]
    for edit in args.get("edits") or []:
        if isinstance(edit, dict):
            bodies.append((edit, "content", edit.get("content")))
    for owner, key, value in bodies:
        if not isinstance(value, str) or len(value) <= 1200:
            continue
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        owner[key] = (
            f"[Olith replay elision: {len(value)} characters, sha256={digest}. "
            "The following tool result records whether the original edit succeeded; "
            "use read_file to inspect the current file.]"
        )


ASK_USER_GUIDANCE = (
    "Clarifying questions (ask_user):\n"
    "- ask_user is the ONLY way to talk to the user mid-job. It blocks until they answer.\n"
    "- Ask when a choice would change what you build: ambiguous scope, two valid approaches,\n"
    "  an unstated preference, an unclear target file. Ask EARLY, before writing code, not after.\n"
    "- Batch related questions into one ask_user call (up to 4). Give 2-4 concrete options each.\n"
    "- Read the Established canon block first. If it already answers something, use that answer\n"
    "  and do NOT ask again.\n"
    "- Do not ask about things you can find out yourself with a tool. Read the file instead.\n"
    "- After they answer, keep going in the same job. Do not restart.\n"
)


def tools_system_addon(defs: list[dict] | None = None) -> str:
    """Guidance for the tools this run actually has, so the prompt never names
    one that was trimmed away."""
    defs = defs if defs is not None else TOOL_DEFS
    names = [t["function"]["name"] for t in defs]
    listed = ", ".join(names)
    return (
        f"\n\n# Tools\nAvailable tools: {listed}.\n"
        "Use native function calling whenever an action or more information is needed. "
        "Never print a tool call as XML, JSON, or a code block. The runtime executes native calls, "
        "appends each named result, and samples you again. A normal assistant message with no tool "
        "calls ends the turn, so emit one only when you are ready to hand the result to the user.\n"
        "Work as one continuous turn: inspect, change, and verify with tools, then give one concise debrief.\n"
        "Finding code: search_files mode=hybrid accepts a natural-language concept; mode=symbols finds "
        "definitions; mode=references finds identifier occurrences; mode=map shows dependencies. "
        "Regex is the default. Read the current source before editing; reference results are not a safe rename operation.\n"
        "Use read_file language_action=references/definition/hover with start_line and character for binding-aware navigation. Language servers start automatically when available.\n"
        "For renames, read_file language_action=rename with new_name returns proposed binding-aware edits; inspect and apply them with edit_file. For new projects, record noninteractive checks in standard project configuration so verification can discover them.\n"
        "Editing existing files:\n"
        "- Prefer edit_file for an existing file, especially anything over 200 lines. Read only the "
        "relevant range. Batch independent ranges in one edits[] call when you already know them. "
        "Do not repeatedly read the whole file.\n"
        "- write_file can replace an existing file, but it must contain the complete new file. "
        "Anything you leave out is deleted.\n"
        "- Dependency names can be case-sensitive. Inspect installed package metadata once before concluding a library is missing. Do not spend tool calls sleeping and polling an installation.\n"
        "- A clean build or a window that exits is not a gameplay test. Check timed state updates and collisions with deterministic logic tests when changing game behavior.\n"
        "- For a large NEW artifact, never attempt one enormous write_file call. Write a compact, valid "
        "foundation first, then use append_file for coherent chunks at syntax-safe boundaries. For a "
        "standalone HTML file that already has closing tags, insert JavaScript before </script>, CSS before "
        "</style>, or markup before </body>; never append source after </html>. Keep each call below roughly "
        "500 lines / 12,000 characters and validate_file after the final chunk.\n"
        "Writing code in another language:\n"
        "- Use check_toolchains when a project needs a compiler or external runtime. Skip that probe "
        "for self-contained HTML and for formats validate_file handles directly.\n"
        "- write_file automatically validates Python, JavaScript, JSON, and HTML. If that result says "
        "validation.ok=true, do not spend another tool step rechecking the same file. Otherwise use "
        "validate_file after a later edit; do not build an ad-hoc run_shell syntax check.\n"
        "- edit_file is transactional for those formats: when validation fails it returns changed=false "
        "and preserves the original file. Correct the edit against the same original line numbers; do not "
        f"reread the entire file. Keep batches to {MAX_EDIT_RANGES} ranges or fewer.\n"
        "- If the toolchain is missing and the install needs sudo, ask_user for it; you cannot run sudo.\n"
        "- npm -g, pip, npx and rustup work without sudo, so use them directly. For a one-off "
        "TypeScript run prefer `npx tsx file.ts` over a global install.\n"
        + (ASK_USER_GUIDANCE if "ask_user" in names else "")
        + "Paths: all file paths are relative to the Active project Root. "
        "If the repo has nested folders (e.g. reedus_llm/...), include that prefix. "
        "If a tool returns suggestions, use one of them instead of retrying the same bad path.\n"
        "Shell: use python3 (not python). Prefer bash. conda may be unavailable, use full env paths if needed.\n"
        "Planning before code:\n"
        "- Anchor steps to functions or marker lines, never bare line numbers: earlier edits "
        "shift every line below them.\n"
        "Task list:\n"
        "- todo_write is optional UI state for genuinely multi-step work; it never controls whether the runtime continues.\n"
        "- If you use it, update it at meaningful milestones and do not recreate the same plan.\n"
        "- Plan items must represent distinct tool phases. Do not split one write_file/edit_file action "
        "into several feature todos, and never batch-complete multiple milestones in one todo_write call.\n"
        "You may call multiple tools per step. Do not claim you edited a file unless write_file, append_file, or edit_file succeeded.\n"
        "Only call something verified if a command you ran after your last edit shows it. Call everything else untested. "
        "A test you wrote yourself only checks your own reading of the request, so say that, and name any behavior you changed that the user did not ask for."
    )


CONTINUE_NUDGE = (
    "[agent-loop] Same user job, not a new request. "
    "Continue from the active milestone and newest tool result. Do not restart repository "
    "inspection or reread an entire file; read only a precise missing range if it is essential. "
    "If a plan step is done, todo_write merge=true with status=completed (do NOT recreate the plan). "
    "When open todos are 0, write ONE final debrief only. "
    "Do not restart from scratch. Do not ask the user to continue."
)

ACTION_ONLY_NUDGE = (
    "[agent-loop] ACTION-ONLY RETRY for the same job. Do not narrate, plan, or reason. "
    "The previous sample exhausted its output allowance before completing an action. "
    "Emit the next required tool call now. If a new artifact is large, write a compact foundation "
    "and continue it with append_file chunks instead of one enormous call. The native schema limits "
    "write_file and append_file content to 12,000 characters; do not attempt to exceed it. For closed HTML, use "
    "append_file.before with </script>, </style>, or </body>; never append after </html>. Keep each chunk below "
    "roughly 500 lines / 12,000 characters. When changing an existing large file, use edit_file "
    "with find/replace anchors instead of rereading or regenerating the whole file; batch "
    "independent edits in edits[] when possible. "
    "If the implementation is genuinely complete, "
    "call todo_write with merge=true to update the existing open items before the final debrief."
)

COURSE_CORRECT_PREFIX = (
    "[User course-correction. SAME continuous agent job, not a new task.]\n"
    "Incorporate the constraint below. Call todo_write (merge=true) to cancel obsolete steps "
    "and adjust the plan, then continue from where you left off. "
    "Do not re-read huge docs you already loaded unless the user asks. "
    "Do not restart from scratch.\n\n"
)


def tool_activity_line(name: str, arguments: dict | None = None, result: str | None = None) -> str:
    """Short line for the thinking dropdown, not a full tool dump."""
    args = arguments or {}
    hint = ""
    if name == "run_shell":
        hint = str(args.get("command") or "")[:80]
    elif name in ("read_file", "write_file", "append_file", "list_dir", "search_files"):
        hint = str(args.get("path") or args.get("query") or "")[:80]
    elif name == "delegate":
        hint = str(args.get("task") or "")[:80]
    elif name == "browser":
        hint = str(args.get("action") or "")
        for key in ("url", "ref", "text"):
            if args.get(key):
                hint += f" {str(args[key])[:40]}"
                break
    elif name == "computer":
        action = str(args.get("action") or "")
        where = f" at {args.get('x')},{args.get('y')}" if args.get("x") is not None else ""
        detail = str(args.get("text") or "")[:40]
        hint = action + where + (f' "{detail}"' if detail else "")
    elif name == "todo_write":
        todos = args.get("todos") or []
        hint = ", ".join(
            f"{t.get('id')}={t.get('status')}" for t in todos[:4] if isinstance(t, dict)
        )
    else:
        hint = str(list(args.keys())[:4])
    line = f"→ {name}" + (f" ({hint})" if hint else "")
    if result is not None:
        err = False
        try:
            parsed = json.loads(result) if isinstance(result, str) and result.strip().startswith("{") else None
            if isinstance(parsed, dict) and parsed.get("error"):
                err = True
                line += f" ✗ {str(parsed.get('error'))[:60]}"
            elif isinstance(parsed, dict) and name == "run_shell" and parsed.get("exit_code") not in (None, 0):
                err = True
                line += f" ✗ exit {parsed['exit_code']}"
        except Exception:
            pass
        if not err:
            line += " ✓"
    return line
