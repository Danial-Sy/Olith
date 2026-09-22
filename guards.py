# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""What a run is not allowed to do to a document, whichever tool it uses.

The desktop and browser tools have the same reach into the same document, so
they share both the rules and the state: a selection made with one is still a
selection when the other one types. See ARCHITECTURE.md for what each refusal
is there to stop.
"""
from __future__ import annotations

# Keys that select the whole document.
SELECT_ALL_KEYS = frozenset({"ctrl+a", "control+a", "cmd+a", "super+a", "meta+a"})
# Keys that overwrite a selection whatever is held with them. Tab is not one:
# it indents, which also keeps alt+Tab out of the way.
REPLACING_KEYS = frozenset({"return", "enter", "delete", "backspace", "space"})
# Keys that move the caret, which drops any selection with it.
CARET_KEYS = frozenset({
    "left", "right", "up", "down", "home", "end", "escape", "esc",
    "prior", "next", "pageup", "pagedown", "ctrl+home", "ctrl+end",
})
CONFIRM_REPLACE = "replace_selection"
# Returns accepted with no text typed in between.
BLANK_LINE_BUDGET = 4

_select_all = False
_returns_since_text = 0


def begin_run() -> None:
    """Forget the last run's state. A selection does not survive one."""
    global _select_all, _returns_since_text
    _select_all = False
    _returns_since_text = 0


def state() -> dict:
    return {"select_all": _select_all, "returns_since_text": _returns_since_text}


def combo_of(args: dict) -> str:
    return str((args or {}).get("text") or (args or {}).get("key") or "").strip().lower().replace(" ", "")


def is_replacing_key(combo: str) -> bool:
    """A keypress that would overwrite a selection rather than act on it.

    Only the key at the end decides: ctrl+b keeps the selection, ctrl+End moves
    off it, shift+Delete destroys it.
    """
    return combo.split("+")[-1] in REPLACING_KEYS


def check(action: str, args: dict, count: int = 1) -> dict | None:
    """Refuse the two moves that destroy a document, or None to go ahead.

    `action` is the tool's own name for it. Both tools use the same names.
    """
    global _select_all, _returns_since_text
    args = args or {}
    combo = combo_of(args)

    if action == "key" and combo in SELECT_ALL_KEYS:
        _select_all = True
        return None

    replacing = action == "type" or (action == "key" and is_replacing_key(combo))
    if _select_all and replacing:
        if str(args.get("confirm") or "").strip().lower() != CONFIRM_REPLACE:
            return {
                "error": "Refused: everything is selected, and this would replace all of it.",
                "hint": (
                    "ctrl+a selects the whole document, not the line you just typed. "
                    "Typing now, or pressing Return, Delete or Backspace, overwrites "
                    "everything with what you send. Press End to deselect first: "
                    "clicking a toolbar button does not deselect anything. To select "
                    "one line instead: click in it, Home, then shift+End. If you really "
                    "do mean to replace the entire selection, send the same call again "
                    f'with "confirm": "{CONFIRM_REPLACE}".'
                ),
            }
        # Confirmed: the selection is consumed by what happens next.
        _select_all = False

    if action in ("type", "fill"):
        _returns_since_text = 0
        # Filling a named field puts the caret in it, so nothing is selected.
        if action == "fill":
            _select_all = False
    elif action == "key" and combo in ("return", "enter"):
        _returns_since_text += max(1, min(10, int(count or 1)))
        if _returns_since_text > BLANK_LINE_BUDGET:
            return {
                "error": (
                    f"Refused: {_returns_since_text} Returns with nothing typed between them."
                ),
                "hint": (
                    "Blank lines are not layout, and pressing Return again will not move "
                    "you anywhere useful. To reach the end of the document press ctrl+End "
                    "once and then type. To put the caret somewhere specific, click there. "
                    "If you cannot tell where the caret is, take a screenshot with a "
                    "region around that part of the page and look."
                ),
            }
    elif action in ("click", "double_click", "right_click", "middle_click", "move", "drag"):
        # A click does not clear the flag: a toolbar click keeps the selection.
        pass
    elif action == "key" and combo in CARET_KEYS:
        _select_all = False
    return None
