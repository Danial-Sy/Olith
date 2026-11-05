# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Show when Olith is using the computer.

A click-through border and action label mark the display. Try GTK first,
then Tk. Report an error if neither can draw the overlay.
"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_thread: threading.Thread | None = None
_enabled = False
_label = "Olith is using your computer"
_backend = ""
_reason = ""
_ui: Any = None

# Brand palette: red -> purple -> blue -> cyan, the order the logo ring runs in.
_COLORS = ((1.0, 0.176, 0.333), (0.486, 0.361, 1.0), (0.31, 0.486, 1.0), (0.0, 0.831, 1.0))
_BORDER = 6
_PILL_W, _PILL_H = 300, 38


def is_active() -> bool:
    return _enabled


def status() -> dict:
    return {"enabled": _enabled, "backend": _backend, "reason": _reason, "label": _label}


def set_overlay(enabled: bool, label: str | None = None) -> dict:
    """Show or hide the marker. Returns which backend drew it, or why none did."""
    global _enabled, _label
    with _lock:
        _enabled = bool(enabled)
        if label:
            _label = str(label)[:80]
        if _enabled:
            _ensure_thread()
        else:
            _close()
    return {"ok": bool(_backend) or not _enabled, **status()}


def set_label(text: str) -> None:
    """Say what the agent is doing right now, under the title on the pill."""
    global _label
    _label = str(text or "")[:80] or "Olith is using your computer"
    ui = _ui
    if ui is not None:
        ui.relabel(_label)


def _ensure_thread() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, name="olith-cu-overlay", daemon=True)
    _thread.start()
    # Wait briefly for the toolkit to report whether the overlay opened.
    _thread.join(timeout=1.5)


def _close() -> None:
    ui = _ui
    if ui is not None:
        ui.close()


def _run() -> None:
    global _backend, _reason, _ui
    errors = []
    for name, factory in (("gtk", _GtkOverlay), ("tk", _TkOverlay)):
        try:
            ui = factory()
        except Exception as e:  # toolkit missing, or no display to draw on
            errors.append(f"{name}: {e}")
            continue
        _ui, _backend, _reason = ui, name, ""
        try:
            ui.run()
        except Exception as e:
            # Try the next toolkit if this one imports but cannot draw.
            errors.append(f"{name}: {e}")
            _ui, _backend = None, ""
            continue
        finally:
            if _backend == name:
                _ui, _backend = None, ""
        return
    _backend = ""
    _reason = "; ".join(errors) or "no GUI toolkit"


def _geometry() -> tuple[int, int, int, int]:
    """The whole desktop, so a multi-monitor setup is framed as one screen."""
    try:
        from PIL import ImageGrab

        w, h = ImageGrab.grab().size
        return 0, 0, w, h
    except Exception:
        return 0, 0, 1920, 1080


class _GtkOverlay:
    """Draw four edge strips and an action label with GTK CSS.

    This avoids requiring python3-gi-cairo for a draw handler.
    """

    CSS = """
    .cu-edge-top    { background-image: linear-gradient(to right, %(c0)s, %(c1)s); }
    .cu-edge-right  { background-image: linear-gradient(to bottom, %(c1)s, %(c2)s); }
    .cu-edge-bottom { background-image: linear-gradient(to left, %(c2)s, %(c3)s); }
    .cu-edge-left   { background-image: linear-gradient(to top, %(c3)s, %(c0)s); }
    .cu-pill {
      background-color: %(pill)s;
      border: 1px solid rgba(79,124,255,0.55);
      border-radius: 19px;
      padding: 0 16px;
    }
    .cu-text { color: #edf2ff; font-size: 12.5px; font-weight: 600; }
    .cu-dot  { color: #ff2d55; font-size: 15px; }
    .cu-dot.dim { color: rgba(255,45,85,0.35); }
    """

    def __init__(self) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        # Gdk defaults to 4.0 when a GTK4 typelib is installed, and mixing the
        # two aborts the import with "version 4.0 is already loaded".
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk

        self.Gtk, self.Gdk = Gtk, Gdk
        if Gdk.Display.get_default() is None and not Gtk.init_check()[0]:
            raise OSError("no display")
        self.windows: list = []
        self.composited = True
        self._built = False
        self._text = None
        self._dot = None

    def _style(self) -> None:
        from gi.repository import Gdk, Gtk

        hexes = ["#ff2d55", "#7c5cff", "#4f7cff", "#00d4ff"]
        css = self.CSS % {
            "c0": hexes[0], "c1": hexes[1], "c2": hexes[2], "c3": hexes[3],
            # Use an opaque label when the desktop has no compositor.
            "pill": ("rgba(11,13,20,0.94)" if self.composited else "#0b0d14"),
        }
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _window(self, w: int, h: int, x: int, y: int, child, css_class: str):
        from gi.repository import Gdk, Gtk

        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_decorated(False)
        win.set_keep_above(True)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        self.composited = bool(visual and screen.is_composited())
        if self.composited:
            # Use the RGBA visual for transparent corners. Leave app_paintable off
            # so GTK still draws the CSS background.
            win.set_visual(visual)
        win.set_size_request(w, h)
        win.get_style_context().add_class(css_class)
        win.add(child)
        win.move(x, y)
        win.show_all()
        win.resize(w, h)
        gdk_win = win.get_window()
        if gdk_win is not None:
            # Let input pass through the overlay to the desktop.
            try:
                gdk_win.set_pass_through(True)
            except Exception:
                pass
        win.stick()
        self.windows.append(win)
        return win

    def run(self) -> None:
        from gi.repository import GLib, Gtk

        self.GLib = GLib
        x, y, w, h = _geometry()
        b = _BORDER
        # Probe the visual once before styling, so the pill knows whether it
        # can use alpha.
        probe = Gtk.Window(type=Gtk.WindowType.POPUP)
        screen = probe.get_screen()
        self.composited = bool(screen.get_rgba_visual() and screen.is_composited())
        probe.destroy()
        self._style()

        for cls, (sw, sh, sx, sy) in (
            ("cu-edge-top", (w, b, x, y)),
            ("cu-edge-right", (b, h, x + w - b, y)),
            ("cu-edge-bottom", (w, b, x, y + h - b)),
            ("cu-edge-left", (b, h, x, y)),
        ):
            self._window(sw, sh, sx, sy, Gtk.Box(), cls)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        self._dot = Gtk.Label(label="\u25cf")
        self._dot.get_style_context().add_class("cu-dot")
        self._text = Gtk.Label(label=_label)
        self._text.get_style_context().add_class("cu-text")
        box.pack_start(self._dot, False, False, 0)
        box.pack_start(self._text, False, False, 0)
        self._window(_PILL_W, _PILL_H, x + (w - _PILL_W) // 2, y + 12, box, "cu-pill")
        self._built = True

        def blink():
            ctx = self._dot.get_style_context() if self._dot else None
            if ctx is None:
                return False
            if ctx.has_class("dim"):
                ctx.remove_class("dim")
            else:
                ctx.add_class("dim")
            return True

        # Slow on purpose: a recording light, not a strobe.
        self._tick = GLib.timeout_add(700, blink)
        Gtk.main()

    def relabel(self, text: str) -> None:
        if not self._built or self._text is None:
            return
        self.GLib.idle_add(lambda: (self._text.set_text(text), False)[1])

    def close(self) -> None:
        if not self._built:
            return
        from gi.repository import GLib, Gtk

        def shut():
            try:
                GLib.source_remove(self._tick)
            except Exception:
                pass
            for win in self.windows:
                win.destroy()
            self.windows = []
            self._built = False
            Gtk.main_quit()
            return False

        GLib.idle_add(shut)


class _TkOverlay:
    """Use Tk edge strips when GTK is unavailable. They need no transparency support."""

    def __init__(self) -> None:
        import tkinter as tk

        self.tk = tk
        self.root = None

    def run(self) -> None:
        tk = self.tk
        x, y, w, h = _geometry()
        root = tk.Tk()
        self.root = root
        root.withdraw()
        b = _BORDER
        hexes = ["#ff2d55", "#7c5cff", "#4f7cff", "#00d4ff"]
        strips = (
            (w, b, x, y), (b, h, x + w - b, y), (w, b, x, y + h - b), (b, h, x, y),
        )
        self.tops = []
        for i, (sw, sh, sx, sy) in enumerate(strips):
            top = tk.Toplevel(root)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg=hexes[i])
            top.geometry(f"{sw}x{sh}+{sx}+{sy}")
            self.tops.append(top)
        pill = tk.Toplevel(root)
        pill.overrideredirect(True)
        pill.attributes("-topmost", True)
        pill.configure(bg="#0d0f17")
        pill.geometry(f"{_PILL_W}x{_PILL_H}+{x + (w - _PILL_W) // 2}+{y + 12}")
        tk.Label(
            pill, text=f"● {_label}", bg="#0d0f17", fg="#edf2ff", font=("TkDefaultFont", 10)
        ).pack(expand=True, fill="both")
        self.tops.append(pill)
        self.pill_label = pill
        root.mainloop()

    def relabel(self, text: str) -> None:
        pill = getattr(self, "pill_label", None)
        if pill is None:
            return
        try:
            pill.after(0, lambda: [c.configure(text=f"● {text}") for c in pill.winfo_children()])
        except Exception:
            pass

    def close(self) -> None:
        root = self.root
        if root is None:
            return
        try:
            root.after(0, root.destroy)
        except Exception:
            pass
