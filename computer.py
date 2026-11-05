# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Read the screen and control the mouse and keyboard.

Each action returns a screenshot through pop_frame so the model can see the
result. Input uses XTest on X11 and SendInput on Windows through ctypes.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import io
import json
import platform
import sys
import threading
import time
from typing import Any

import guards

# Limit screenshots to a 2000 px long edge to keep text readable.
# A frame at this size costs about 1.9k tokens.
MAX_EDGE = 2000

# Warn when scaling makes body text too small to read.
LEGIBLE_SCALE = 0.6

# The editing guards live in `guards`, shared with the browser tool along with
# their state: a selection made with one is still a selection for the other.
SELECT_ALL_KEYS = guards.SELECT_ALL_KEYS
REPLACING_KEYS = guards.REPLACING_KEYS
CARET_KEYS = guards.CARET_KEYS
CONFIRM_REPLACE = guards.CONFIRM_REPLACE
BLANK_LINE_BUDGET = guards.BLANK_LINE_BUDGET

# How long the desktop gets to repaint before the frame is taken. Under this a
# click on a menu returns the screen from before the menu opened.
SETTLE = 0.6
SETTLE_MAX = 10.0

ACTIONS = (
    "screenshot", "elements", "click", "double_click", "right_click",
    "middle_click", "move", "drag", "type", "key", "scroll", "wait",
)

# How many widgets one `elements` call reports. A file manager's tree runs to
# thousands of nodes and a model needs the twenty that are on screen.
MAX_ELEMENTS = 60
# Roles worth naming even when they carry no label of their own.
_INTERESTING_ROLES = frozenset({
    "push button", "toggle button", "check box", "radio button", "menu item",
    "check menu item", "radio menu item", "menu", "text", "entry", "combo box",
    "list item", "tab", "link", "slider", "spin button", "table cell",
})

_lock = threading.Lock()
_frame: dict | None = None
_frame_id = 0
_last_signature: bytes | None = None
_backend: Any = None
_backend_error: str = ""
# Widgets from the last `elements` call: ref -> (x, y, w, h, name, role), in
# screen pixels. Aiming at one beats reading a coordinate off a scaled image.
_refs: dict[str, tuple] = {}


def begin_run() -> None:
    """Forget the last run's desktop state. A selection does not survive it."""
    guards.begin_run()
    _refs.clear()


def _atspi():
    """The accessibility bus, or None on a desktop that does not run one."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        Atspi.init()
        return Atspi
    except Exception:
        return None


def _inside(box: tuple, bounds: tuple | None) -> bool:
    """Check that a widget's reported box is inside its window."""
    if not bounds:
        return True
    x, y, w, h = box
    bx, by, bw, bh = bounds
    cx, cy = x + w / 2, y + h / 2
    return bx - 2 <= cx <= bx + bw + 2 and by - 2 <= cy <= by + bh + 2


def _walk(node, atspi, out: list, depth: int = 0, bounds: tuple | None = None) -> None:
    if len(out) >= MAX_ELEMENTS or depth > 24:
        return
    try:
        name = (node.get_name() or "").strip()
        role = node.get_role_name() or ""
        component = node.get_component_iface()
        box = None
        if component is not None:
            extents = component.get_extents(atspi.CoordType.SCREEN)
            if extents.width > 1 and extents.height > 1:
                box = (extents.x, extents.y, extents.width, extents.height)
        if depth == 0 and box:
            bounds = box
        if box and (name or role in _INTERESTING_ROLES) and _inside(box, bounds):
            out.append((name[:70], role, box))
        for i in range(min(node.get_child_count(), 80)):
            _walk(node.get_child_at_index(i), atspi, out, depth + 1, bounds)
    except Exception:
        # One unreachable node is normal: applications come and go while the
        # tree is being read, and a dead node must not lose the whole snapshot.
        return


def _active_frames(atspi) -> list:
    """The window the user is in, or every window when none says it is active."""
    desktop = atspi.get_desktop(0)
    frames, active = [], []
    for i in range(desktop.get_child_count()):
        try:
            app = desktop.get_child_at_index(i)
            for j in range(app.get_child_count()):
                frame = app.get_child_at_index(j)
                if frame.get_role_name() != "frame":
                    continue
                frames.append(frame)
                states = frame.get_state_set()
                if states.contains(atspi.StateType.ACTIVE):
                    active.append(frame)
        except Exception:
            continue
    return active or frames


def elements(everything: bool = False) -> dict:
    """List named widgets in the active window with refs for clicking."""
    atspi = _atspi()
    if atspi is None:
        return {
            "error": "No accessibility bus on this desktop.",
            "hint": (
                "Aim with coordinates from the screenshot instead, or use the browser "
                "tool if the target is a web page."
            ),
        }
    found: list = []
    try:
        frames = _active_frames(atspi)
        for frame in (frames if everything else frames[:1]):
            _walk(frame, atspi, found)
    except Exception as e:
        return {"error": f"the accessibility tree could not be read ({e})"}

    sw, sh = screen_size()
    scale = _frame_scale(sw, sh)
    _refs.clear()
    items = []
    for index, (name, role, box) in enumerate(found, start=1):
        ref = f"a{index}"
        _refs[ref] = (*box, name, role)
        x, y, w, h = box
        items.append({
            "ref": ref, "role": role, "name": name,
            # Use the same coordinate space as the screenshot.
            "at": [int((x + w / 2) * scale), int((y + h / 2) * scale)],
        })
    if not items:
        return {
            "elements": [],
            "note": (
                "This window exposes nothing to the accessibility bus. Chrome and "
                "Electron applications often do not; use the browser tool for a web "
                "page, or aim with coordinates from the screenshot."
            ),
        }
    return {
        "elements": items,
        "note": (
            f"{len(items)} widgets. Act on one with "
            '{"action": "click", "ref": "a3"} and the click lands in its middle. '
            "Refs last until the window changes."
        ),
    }


# --------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------

def _grab():
    from PIL import ImageGrab

    return ImageGrab.grab()


def screen_size() -> tuple[int, int]:
    try:
        return _grab().size
    except Exception:
        return (0, 0)


def _encode(img, quality: int = 72) -> tuple[str, bytes]:
    """Encode screenshots as WebP to keep stored and resent images small."""
    buf = io.BytesIO()
    try:
        img.save(buf, format="WEBP", quality=quality, method=4)
        return "webp", buf.getvalue()
    except Exception:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        return "png", buf.getvalue()


# Compare coarse brightness cells to ignore cursor blinks while catching
# changes such as a button changing color.
_GRID = (48, 27)
_CHANGE_LEVELS = 3


def _signature(img) -> bytes:
    """Per-cell brightness, the thing two frames are compared on."""
    from PIL import Image

    return img.convert("L").resize(_GRID, Image.Resampling.BOX).tobytes()


def _changed(before: bytes | None, after: bytes) -> bool:
    """Check whether the screen changed. Treat uncertain differences as a change."""
    if before is None or len(before) != len(after):
        return True
    return any(abs(a - b) > _CHANGE_LEVELS for a, b in zip(after, before))


def _capture(region: list | None = None) -> dict:
    """Take a frame. `region` is [x, y, w, h] in the coordinates of the last
    full frame, and yields a zoomed crop for reading small text."""
    global _frame_id, _last_signature

    native = _grab()
    sw, sh = native.size
    origin = (0, 0)
    zoom = 1.0

    if region:
        # Convert the requested region from frame coordinates to screen pixels.
        scale = _frame_scale(sw, sh)
        rx, ry, rw, rh = (int(round(float(v) / scale)) for v in region[:4])
        rw = max(16, min(sw, rw))
        rh = max(16, min(sh, rh))
        # Keep the crop inside the screen to avoid black padding.
        rx = max(0, min(sw - rw, rx))
        ry = max(0, min(sh - rh, ry))
        native = native.crop((rx, ry, rx + rw, ry + rh))
        origin = (rx, ry)

    img = native
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_EDGE:
        ratio = MAX_EDGE / float(longest)
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))))
    fw, fh = img.size
    if region:
        # A crop is usually shown larger than it appears on the full frame; the
        # model needs that factor to convert a point back to a click target.
        zoom = (fw / float(w)) / _frame_scale(sw, sh) if w else 1.0

    fmt, raw = _encode(img)
    signature = _signature(img)
    with _lock:
        # Compare full frames only; a crop has different dimensions.
        if not region and _changed(_last_signature, signature):
            _frame_id += 1
            _last_signature = signature
    return {
        "b64": base64.b64encode(raw).decode("ascii"),
        "format": fmt,
        "w": fw,
        "h": fh,
        "bytes": len(raw),
        "screen": [sw, sh],
        "origin": list(origin),
        "zoom": round(zoom, 3),
        "id": _frame_id,
    }


def _frame_scale(sw: int, sh: int) -> float:
    """Frame pixels per screen pixel for a full-screen grab of this size."""
    longest = max(sw, sh)
    if longest <= 0:
        return 1.0
    return min(1.0, MAX_EDGE / float(longest))


def to_screen(x: float, y: float, sw: int, sh: int) -> tuple[int, int]:
    """A point the model gave, in frame coordinates, as a screen pixel."""
    scale = _frame_scale(sw, sh)
    px = int(round(float(x) / scale))
    py = int(round(float(y) / scale))
    return max(0, min(sw - 1, px)), max(0, min(sh - 1, py))


def frame_id() -> int:
    """Advance only when the screen changes, so a repeat click can be checked against its result."""
    return _frame_id


def pop_frame() -> dict | None:
    """Return the latest frame once, then clear it to avoid resending an old screenshot."""
    global _frame
    with _lock:
        out, _frame = _frame, None
    return out


# --------------------------------------------------------------------------
# Input backends
# --------------------------------------------------------------------------

class _X11:
    """XTest over ctypes. Same mechanism xdotool uses, without the binary."""

    BUTTONS = {"left": 1, "middle": 2, "right": 3}
    # Names the model is likely to write, mapped onto X keysym names.
    KEYSYMS = {
        "enter": "Return", "return": "Return", "esc": "Escape",
        "escape": "Escape", "tab": "Tab", "space": "space",
        "backspace": "BackSpace", "delete": "Delete", "del": "Delete",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
        "insert": "Insert", "menu": "Menu", "print": "Print",
    }
    MODIFIERS = {
        "ctrl": "Control_L", "control": "Control_L", "alt": "Alt_L",
        "shift": "Shift_L", "super": "Super_L", "win": "Super_L",
        "meta": "Super_L", "cmd": "Super_L",
    }

    def __init__(self) -> None:
        x11 = ctypes.util.find_library("X11")
        xtst = ctypes.util.find_library("Xtst")
        if not x11 or not xtst:
            raise OSError("libX11/libXtst not found")
        self.x = ctypes.CDLL(x11)
        self.t = ctypes.CDLL(xtst)
        # Default restype is c_int, which truncates the Display* on 64-bit and
        # segfaults on the first call that uses it.
        self.x.XOpenDisplay.restype = ctypes.c_void_p
        self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x.XStringToKeysym.restype = ctypes.c_ulong
        self.x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x.XKeycodeToKeysym.restype = ctypes.c_ulong
        self.x.XKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int]
        self.x.XFlush.argtypes = [ctypes.c_void_p]
        self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x.XDisplayKeycodes.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
        ]
        self.x.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        self.x.XGetKeyboardMapping.argtypes = [
            ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]
        self.x.XFree.argtypes = [ctypes.c_void_p]
        self.x.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
        ]
        self.t.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong
        ]
        self.t.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong
        ]
        self.t.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong
        ]
        self.dpy = self.x.XOpenDisplay(None)
        if not self.dpy:
            raise OSError("cannot open the X display")
        lo, hi = ctypes.c_int(), ctypes.c_int()
        self.x.XDisplayKeycodes(self.dpy, ctypes.byref(lo), ctypes.byref(hi))
        self._lo, self._hi = lo.value, hi.value
        # Scratch keycode for characters the layout cannot produce, remapped
        # per press. It has to be one the layout is not already using.
        self._scratch = self._unused_keycode()

    def _unused_keycode(self) -> int:
        """The highest keycode this layout maps to nothing."""
        count = self._hi - self._lo + 1
        per_code = ctypes.c_int()
        syms = self.x.XGetKeyboardMapping(
            self.dpy, self._lo, count, ctypes.byref(per_code)
        )
        if not syms:
            return self._hi
        try:
            width = per_code.value
            for i in range(count - 1, -1, -1):
                if not any(syms[i * width + j] for j in range(width)):
                    return self._lo + i
        finally:
            self.x.XFree(ctypes.cast(syms, ctypes.c_void_p))
        return self._hi

    # -- keysym plumbing ---------------------------------------------------

    def _keysym(self, name: str) -> int:
        lowered = name.lower()
        mapped = self.KEYSYMS.get(lowered) or self.MODIFIERS.get(lowered)
        if mapped:
            return int(self.x.XStringToKeysym(mapped.encode()))
        if len(name) == 1:
            return self._char_keysym(name)
        # F-keys and anything the model spelled the X way already.
        sym = int(self.x.XStringToKeysym(name.encode()))
        if sym:
            return sym
        return int(self.x.XStringToKeysym(name.capitalize().encode()))

    @staticmethod
    def _char_keysym(ch: str) -> int:
        code = ord(ch)
        # Latin-1 doubles as the low keysym range; everything else uses the
        # Unicode block libX11 defines at 0x01000000.
        return code if code < 0x100 else 0x01000000 + code

    def _resolve(self, keysym: int) -> tuple[int, bool] | None:
        """(keycode, needs_shift) for a keysym the current layout can type."""
        kc = int(self.x.XKeysymToKeycode(self.dpy, ctypes.c_ulong(keysym)))
        if not kc:
            return None
        for level in (0, 1):
            if int(self.x.XKeycodeToKeysym(self.dpy, ctypes.c_ubyte(kc), level)) == keysym:
                return kc, bool(level)
        return kc, False

    # Wait for the toolkit to process each keymap change. GTK lost accented
    # characters at 10 ms; 40 ms worked in the check.
    REMAP_SETTLE = 0.04

    def _remap(self, keysym: int) -> int:
        arr = (ctypes.c_ulong * 2)(keysym, keysym)
        self.x.XChangeKeyboardMapping(self.dpy, self._scratch, 2, arr, 1)
        self.x.XSync(self.dpy, 0)
        time.sleep(self.REMAP_SETTLE)
        return self._scratch

    def _restore(self) -> None:
        time.sleep(self.REMAP_SETTLE)
        arr = (ctypes.c_ulong * 2)(0, 0)
        self.x.XChangeKeyboardMapping(self.dpy, self._scratch, 2, arr, 1)
        self.x.XSync(self.dpy, 0)

    def _tap(self, keysym: int, mods: list[int] | None = None) -> None:
        mods = mods or []
        found = self._resolve(keysym)
        scratch = False
        if found is None:
            kc, shift = self._remap(keysym), False
            scratch = True
        else:
            kc, shift = found
        held = list(mods)
        if shift:
            held.append(int(self.x.XStringToKeysym(b"Shift_L")))
        codes = []
        for m in held:
            mc = self._resolve(m)
            if mc:
                codes.append(mc[0])
        try:
            for mc in codes:
                self.t.XTestFakeKeyEvent(self.dpy, mc, 1, 0)
            self.t.XTestFakeKeyEvent(self.dpy, kc, 1, 0)
        finally:
            # Never leave a key or modifier held: a stuck Ctrl turns every
            # later keystroke into a shortcut.
            self.t.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
            for mc in reversed(codes):
                self.t.XTestFakeKeyEvent(self.dpy, mc, 0, 0)
            self.x.XFlush(self.dpy)
            if scratch:
                self._restore()

    # -- the operations the tool exposes -----------------------------------

    def move(self, x: int, y: int) -> None:
        self.t.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0)
        self.x.XFlush(self.dpy)

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        self.move(x, y)
        time.sleep(0.05)
        b = self.BUTTONS.get(button, 1)
        for i in range(count):
            self.t.XTestFakeButtonEvent(self.dpy, b, 1, 0)
            self.t.XTestFakeButtonEvent(self.dpy, b, 0, 0)
            self.x.XFlush(self.dpy)
            if i + 1 < count:
                time.sleep(0.08)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.move(x1, y1)
        time.sleep(0.05)
        self.t.XTestFakeButtonEvent(self.dpy, 1, 1, 0)
        self.x.XFlush(self.dpy)
        # A single jump is ignored by anything that tracks motion, so walk it.
        steps = 12
        for i in range(1, steps + 1):
            self.move(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
            time.sleep(0.012)
        self.t.XTestFakeButtonEvent(self.dpy, 1, 0, 0)
        self.x.XFlush(self.dpy)

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        self.move(x, y)
        button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(direction, 5)
        for _ in range(max(1, amount)):
            self.t.XTestFakeButtonEvent(self.dpy, button, 1, 0)
            self.t.XTestFakeButtonEvent(self.dpy, button, 0, 0)
            self.x.XFlush(self.dpy)
            time.sleep(0.02)

    def type_text(self, text: str) -> None:
        for ch in text:
            if ch == "\n":
                self._tap(int(self.x.XStringToKeysym(b"Return")))
            elif ch == "\t":
                self._tap(int(self.x.XStringToKeysym(b"Tab")))
            else:
                self._tap(self._char_keysym(ch))
            time.sleep(0.012)

    def key(self, combo: str) -> None:
        parts = [p for p in str(combo).replace(" ", "").split("+") if p]
        if not parts:
            return
        *mods, final = parts
        mod_syms = [
            int(self.x.XStringToKeysym(self.MODIFIERS.get(m.lower(), m).encode()))
            for m in mods
        ]
        self._tap(self._keysym(final), [m for m in mod_syms if m])


class _Windows:
    """SendInput over ctypes. Unicode typing needs no layout lookup here."""

    VK = {
        "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
        "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
        "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
        "insert": 0x2D, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
        "shift": 0x10, "super": 0x5B, "win": 0x5B, "meta": 0x5B, "cmd": 0x5B,
    }

    def __init__(self) -> None:
        if not sys.platform.startswith("win"):
            raise OSError("not Windows")
        self.u = ctypes.windll.user32
        self.u.SetProcessDPIAware()

    def _vk(self, name: str) -> int:
        lowered = name.lower()
        if lowered in self.VK:
            return self.VK[lowered]
        if lowered.startswith("f") and lowered[1:].isdigit():
            return 0x70 + int(lowered[1:]) - 1
        if len(name) == 1:
            return self.u.VkKeyScanW(ord(name)) & 0xFF
        return 0

    def move(self, x: int, y: int) -> None:
        self.u.SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        self.move(x, y)
        time.sleep(0.05)
        down, up = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }.get(button, (0x0002, 0x0004))
        for i in range(count):
            self.u.mouse_event(down, 0, 0, 0, 0)
            self.u.mouse_event(up, 0, 0, 0, 0)
            if i + 1 < count:
                time.sleep(0.08)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.move(x1, y1)
        time.sleep(0.05)
        self.u.mouse_event(0x0002, 0, 0, 0, 0)
        steps = 12
        for i in range(1, steps + 1):
            self.move(int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
            time.sleep(0.012)
        self.u.mouse_event(0x0004, 0, 0, 0, 0)

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        self.move(x, y)
        horizontal = direction in ("left", "right")
        sign = -1 if direction in ("down", "left") else 1
        for _ in range(max(1, amount)):
            self.u.mouse_event(0x01000 if horizontal else 0x0800, 0, 0, sign * 120, 0)
            time.sleep(0.02)

    def type_text(self, text: str) -> None:
        KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
        for ch in text:
            if ch == "\n":
                self.key("enter")
                continue
            self.u.keybd_event(0, ord(ch), KEYEVENTF_UNICODE, 0)
            self.u.keybd_event(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
            time.sleep(0.012)

    def key(self, combo: str) -> None:
        parts = [p for p in str(combo).replace(" ", "").split("+") if p]
        if not parts:
            return
        codes = [self._vk(p) for p in parts]
        codes = [c for c in codes if c]
        for c in codes:
            self.u.keybd_event(c, 0, 0, 0)
        for c in reversed(codes):
            self.u.keybd_event(c, 0, 0x0002, 0)


def backend():
    """Create and cache the input driver. If unavailable, store the reason in _backend_error."""
    global _backend, _backend_error
    if _backend is not None or _backend_error:
        return _backend
    try:
        _backend = _Windows() if sys.platform.startswith("win") else _X11()
    except Exception as e:
        _backend_error = str(e)
        _backend = None
    return _backend


_available: tuple[bool, str] | None = None


def available() -> tuple[bool, str]:
    """Whether this machine can be driven, and why not when it cannot.

    Cached, because the probe grabs the screen and every action calls this.
    """
    global _available
    if _available is not None:
        return _available
    try:
        _grab()
    except Exception as e:
        _available = (False, f"the screen cannot be captured ({e})")
        return _available
    if backend() is None:
        if not sys.platform.startswith("win") and platform.system() == "Linux":
            _available = (False, (
                f"no input backend ({_backend_error}). On Wayland, log in with "
                "an X11 session; X11 input injection is what this uses."
            ))
        else:
            _available = (False, f"no input backend ({_backend_error})")
        return _available
    _available = (True, "")
    return _available


# --------------------------------------------------------------------------
# The tool entry point
# --------------------------------------------------------------------------

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _guard(action: str, args: dict) -> str | None:
    """The shared editing rules, in this tool's own return shape."""
    refusal = guards.check(action, args, count=_as_int((args or {}).get("count"), 1))
    return json.dumps(refusal) if refusal else None


def status_line(args: dict) -> str:
    """Describe the current action in the desktop overlay."""
    args = args or {}
    steps = _steps(args)
    if steps:
        # The pill names what the person is watching happen, and in a batch
        # that is the step that takes long enough to watch.
        typed = next((a for a in steps if str(a.get("action")).lower() == "type"), None)
        if typed or len(steps) == 1:
            return status_line(typed or steps[0])
        return f"Olith is running {len(steps)} steps on your computer"
    action = str(args.get("action") or "").lower()
    where = ""
    if args.get("x") is not None and args.get("y") is not None:
        where = f" at {int(float(args['x']))}, {int(float(args['y']))}"
    text = str(args.get("text") or "")
    if action == "screenshot":
        return "Olith is looking at your screen"
    if action == "type":
        snippet = text[:24] + ("…" if len(text) > 24 else "")
        return f'Olith is typing "{snippet}"'
    if action == "key":
        return f"Olith is pressing {text}"
    if action == "scroll":
        return f"Olith is scrolling {args.get('direction') or 'down'}"
    if action == "wait":
        return "Olith is waiting for the screen"
    if action in ("click", "double_click", "right_click", "middle_click"):
        return f"Olith is clicking{where}"
    if action == "move":
        return f"Olith is moving the pointer{where}"
    if action == "drag":
        return "Olith is dragging"
    return "Olith is using your computer"


# Batch actions to reduce model round trips. A measured step took 15.5s,
# with 0.7s spent on the action.
MAX_BATCH = 20
# Between actions inside a batch. Long enough for a menu to open under the
# click that follows it, short enough that a batch is still one turn.
STEP_PAUSE = 0.25


def _perform(args: dict, drv: Any, sw: int, sh: int) -> dict:
    """One action, no capture. Returns what it did, or an `error` it refused."""
    action = _canonical(args)
    if action not in ACTIONS:
        return _unknown_action(action)

    x, y = args.get("x"), args.get("y")
    point = None
    ref = str(args.get("ref") or "").strip()
    if ref:
        known = _refs.get(ref)
        if known is None:
            return {
                "error": f"No widget {ref}. Refs come from the last `elements` call.",
                "hint": "Call elements again; the window may have changed since.",
            }
        rx, ry, rw, rh, ref_name, _role = known
        # The middle of the widget, in real screen pixels: a ref does not go
        # through the scaled frame at all, which is the point of having one.
        point = (int(rx + rw / 2), int(ry + rh / 2))
    elif x is not None and y is not None:
        point = to_screen(_as_int(x), _as_int(y), sw, sh)

    refusal = _guard(action, args)
    if refusal:
        return json.loads(refusal)

    needs_point = action in ("click", "double_click", "right_click", "middle_click", "move", "drag")
    if needs_point and point is None:
        return {
            "error": f"{action} needs x and y",
            "hint": "Take a screenshot first and read the coordinates off it.",
        }

    done: dict[str, Any] = {"action": action, "settle": SETTLE}
    if ref:
        done["ref"], done["clicked"] = ref, ref_name
    elif point:
        done["clicked_at"] = [_as_int(x), _as_int(y)]
    try:
        if action == "screenshot":
            done["settle"] = 0.0
        elif action == "elements":
            done["settle"] = 0.0
            found = elements(bool(args.get("all")))
            if found.get("error"):
                return found
            done.update(found)
        elif action == "move":
            drv.move(*point)
        elif action == "click":
            drv.click(*point, button="left")
        elif action == "double_click":
            drv.click(*point, button="left", count=2)
        elif action == "right_click":
            drv.click(*point, button="right")
        elif action == "middle_click":
            drv.click(*point, button="middle")
        elif action == "drag":
            to = to_screen(_as_int(args.get("to_x")), _as_int(args.get("to_y")), sw, sh)
            drv.drag(point[0], point[1], to[0], to[1])
        elif action == "type":
            text = str(args.get("text") or "")
            if not text:
                return {"error": "type needs text"}
            if point:
                drv.click(*point, button="left")
                time.sleep(0.15)
            drv.type_text(text)
            # Report what was sent, not what arrived: the tool cannot see
            # what a lagging editor or a stray caret did with it.
            done["note"] = (
                f"Sent {len(text)} characters to the focused window. Check what "
                "actually landed before building on it."
            )
        elif action == "key":
            combo = str(args.get("text") or args.get("key") or "")
            if not combo:
                return {
                    "error": "key needs the combination in text",
                    "hint": 'e.g. {"action": "key", "text": "ctrl+l"}',
                }
            # Capped, because a long run of identical actions is
            # indistinguishable from being stuck.
            count = max(1, min(10, _as_int(args.get("count"), 1)))
            for i in range(count):
                drv.key(combo)
                if i + 1 < count:
                    time.sleep(0.05)
            done["note"] = f"Pressed {combo}" + (f" x{count}" if count > 1 else "") + "."
        elif action == "scroll":
            where = point or (sw // 2, sh // 2)
            direction = str(args.get("direction") or "down").lower()
            drv.scroll(where[0], where[1], direction, _as_int(args.get("amount"), 3))
        elif action == "wait":
            done["settle"] = 0.0
            seconds = min(SETTLE_MAX, max(0.2, float(args.get("seconds") or 2)))
            time.sleep(seconds)
            done["note"] = f"Waited {seconds:g}s."
    except Exception as e:
        return {"error": f"{action} failed: {e}"}
    return done


def _canonical(args: dict) -> str:
    """The action a step names, under the spelling this module uses."""
    action = str((args or {}).get("action") or "").strip().lower()
    return "screenshot" if action in ("screen_shot", "capture", "look") else action


def _unknown_action(action: str) -> dict:
    return {
        "error": f"Unknown action {action!r}",
        "hint": f"Use one of: {', '.join(ACTIONS)}",
    }


def _steps(args: dict) -> list[dict] | None:
    """The actions a call carries, when it carries more than one."""
    raw = (args or {}).get("actions")
    if not isinstance(raw, list):
        return None
    steps = [a for a in raw if isinstance(a, dict) and a.get("action")]
    return steps or None


def run(args: dict) -> str:
    """Run one action or a batch, then leave the final screenshot in pop_frame."""
    global _frame

    args = args or {}
    # Validate the action before checking for a display.
    for step in _steps(args) or [args]:
        action = _canonical(step)
        if action != "batch" and action not in ACTIONS:
            return json.dumps(_unknown_action(action))

    ok, why = available()
    if not ok:
        return json.dumps({
            "error": f"Computer use is unavailable: {why}",
            "hint": "Tell the user; do not retry this tool.",
        })

    drv = backend()
    sw, sh = screen_size()
    steps = _steps(args)
    batched = steps is not None
    if batched and len(steps) > MAX_BATCH:
        return json.dumps({
            "error": f"{len(steps)} actions in one call, the limit is {MAX_BATCH}.",
            "hint": (
                "Send the first part, look at what came back, then send the rest. "
                "A batch longer than this is a plan made without seeing the screen."
            ),
        })
    if not batched:
        if str(args.get("action") or "").strip().lower() == "batch":
            return json.dumps({
                "error": "batch needs an actions list",
                "hint": (
                    'e.g. {"actions": [{"action": "key", "text": "ctrl+alt+1"}, '
                    '{"action": "type", "text": "Title"}]}'
                ),
            })
        steps = [args]

    performed: list[dict] = []
    stopped: dict | None = None
    settle = SETTLE
    for index, step in enumerate(steps):
        done = _perform(step, drv, sw, sh)
        if done.get("error"):
            # Stop at the first refusal, so the model is never left reading a
            # screen whose earlier steps did not happen.
            stopped = {"at": index, **done}
            break
        settle = done.pop("settle", SETTLE)
        performed.append(done)
        if index + 1 < len(steps):
            time.sleep(STEP_PAUSE)

    if stopped is not None and not performed:
        # Do not resend a screenshot when no action reached the desktop.
        return json.dumps(stopped if not batched else {**stopped, "actions_run": f"0 of {len(steps)}"})

    if settle:
        time.sleep(min(SETTLE_MAX, float(args.get("settle") or settle)))

    last = steps[len(performed) - 1] if performed else args
    region = last.get("region") if last.get("action") == "screenshot" else None
    if region is not None and not (isinstance(region, (list, tuple)) and len(region) >= 4):
        region = None
    before = _frame_id
    frame = _capture(list(region) if region else None)
    with _lock:
        _frame = frame

    looked = any(p.get("action") == "screenshot" for p in performed)
    out: dict[str, Any] = {
        "ok": stopped is None,
        "frame": {"w": frame["w"], "h": frame["h"]},
        "screen_changed": frame["id"] != before or looked,
    }
    if batched:
        out["performed"] = performed
        out["actions_run"] = f"{len(performed)} of {len(steps)}"
    elif performed:
        out.update({k: v for k, v in performed[0].items() if k != "settle"})
    if stopped is not None:
        out["error"] = stopped.get("error")
        if stopped.get("hint"):
            out["hint"] = stopped["hint"]
        if batched:
            out["stopped_at"] = stopped["at"]
            out["note"] = (
                f"Actions 0-{stopped['at'] - 1} ran; action {stopped['at']} was "
                "refused and nothing after it was sent."
                if stopped["at"] else "Nothing ran: the first action was refused."
            )
    if region:
        out["zoom"] = frame["zoom"]
        out["origin"] = frame["origin"]
        out["coordinates"] = (
            "This is a zoomed crop. A point (cx, cy) in it is "
            f"({frame['origin'][0]} + cx/{frame['zoom']:g}, "
            f"{frame['origin'][1]} + cy/{frame['zoom']:g}) on the full frame."
        )
    else:
        out["coordinates"] = (
            f"The screenshot below is {frame['w']}x{frame['h']}. Give x and y in "
            "those pixels; they are scaled to the real display for you."
        )
        scale = _frame_scale(sw, sh)
        if scale < LEGIBLE_SCALE:
            out["legibility"] = (
                f"This display is {sw}x{sh}, so the frame is scaled to "
                f"{scale:.0%}. Body text will not be readable in it. Before "
                "judging what you have written, screenshot again with a region "
                "around that part of the screen."
            )
    if not out["screen_changed"] and stopped is None:
        out["hint"] = (
            "The screen is identical to before this action, so it did not land. "
            "Check the target coordinates against the screenshot, or the window "
            "may not have had focus. Do not repeat the same action unchanged."
        )
    return json.dumps(out)
