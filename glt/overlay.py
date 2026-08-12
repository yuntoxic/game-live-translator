"""Borderless always-on-top subtitle overlay (tkinter, no extra deps).

Runs on the main thread because Tk demands it; worker threads hand lines over
through a queue that a periodic `after()` drains.

Click-through mode sets WS_EX_TRANSPARENT so mouse input falls through to
whatever is underneath. That is what you want while playing, but it also means
you can no longer drag the overlay, so it is off by default and toggled from
the right-click menu.
"""

from __future__ import annotations

import queue
import tkinter as tk
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import win32con
import win32gui

from .config import OverlayConfig


@dataclass
class Placement:
    """One translated chunk and where its original sat in the capture frame.

    Boxes are in frame pixels; whoever draws them maps to screen coordinates,
    because only the drawer knows where the target window currently is.
    """
    source: str
    text: str
    box: Tuple[float, float, float, float]
    region: str = ""       # filled in by the overlay, to replace per region
    # Height of one line of the original, in frame pixels. Distinct from the
    # box height: a whole-region caption spans every line it replaces, and
    # sizing the font from that box produced text several times too large.
    line_height: float = 0.0
    # The colour behind the original, sampled from the picture. A plate drawn
    # in it reads as the game's own background rather than a patch stuck over
    # it; empty falls back to the configured colour.
    bg: str = ""
    # A wider rectangle for the plate alone, in frame pixels. The recogniser
    # keeps missing trailing hearts and brackets, so a dialogue plate spans
    # the region -- the user framed the dialog box -- while the text stays at
    # the original's own left edge. Empty means the plate hugs `box`.
    plate_box: Tuple[float, float, float, float] = ()


@dataclass
class Line:
    region: str
    role: str
    source: str
    translation: str
    speaker: str = ""
    placements: List[Placement] = field(default_factory=list)


class Overlay:
    """The subtitle window.

    `master` decides who owns the Tk event loop. Run from the CLI the overlay
    is the only window, so it creates the root and `run()` blocks on it. Under
    the control panel it is a Toplevel of that window instead -- Tk allows
    exactly one root per process and every widget must live on the same
    thread, so sharing the loop is not optional.
    """

    def __init__(self, cfg: OverlayConfig, master: Optional[tk.Misc] = None,
                 on_close: Optional[Callable[[], None]] = None) -> None:
        self.cfg = cfg
        self.queue: "queue.Queue[Optional[Line]]" = queue.Queue()
        self._speaker = ""
        self._drag: Optional[tuple] = None
        self._closed = False
        self._on_close = on_close
        self.owns_root = master is None

        self.root = tk.Tk() if master is None else tk.Toplevel(master)
        self.root.title("Game Live Translator")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", cfg.always_on_top)
        self.root.attributes("-alpha", cfg.opacity)
        self.root.configure(bg=cfg.bg)
        self.root.geometry(f"{cfg.width}x120+{cfg.x}+{cfg.y}")

        pad = {"padx": 16, "fill": "x", "anchor": "w"}
        self.speaker_label = tk.Label(
            self.root, text="", bg=cfg.bg, fg=cfg.name_fg, justify="left", anchor="w",
            font=(cfg.font_family, max(10, cfg.src_font_size + 1), "bold"),
            wraplength=cfg.width - 32)
        self.speaker_label.pack(pady=(10, 0), **pad)

        self.main_label = tk.Label(
            self.root, text="waiting for text...", bg=cfg.bg, fg=cfg.fg,
            justify="left", anchor="w", font=(cfg.font_family, cfg.font_size),
            wraplength=cfg.width - 32)
        self.main_label.pack(pady=(4, 0), **pad)

        self.src_label = tk.Label(
            self.root, text="", bg=cfg.bg, fg=cfg.src_fg, justify="left", anchor="w",
            font=(cfg.font_family, cfg.src_font_size),
            wraplength=cfg.width - 32)
        if cfg.show_source:
            self.src_label.pack(pady=(4, 12), **pad)

        self._build_menu()
        for widget in (self.root, self.speaker_label, self.main_label, self.src_label):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<Button-3>", self._popup)
        self.root.bind("<Escape>", lambda _e: self.close())

        self.root.update_idletasks()
        if cfg.click_through:
            self.set_click_through(True)
        self.root.after(50, self._drain)

    # -- window chrome ----------------------------------------------------
    def _build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Toggle source text", command=self._toggle_source)
        self.menu.add_command(label="Toggle click-through",
                              command=lambda: self.set_click_through(
                                  not self.cfg.click_through))
        self.menu.add_separator()
        self.menu.add_command(label="Quit (Esc)", command=self.close)

    def _popup(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _toggle_source(self) -> None:
        self.cfg.show_source = not self.cfg.show_source
        if self.cfg.show_source:
            self.src_label.pack(padx=16, pady=(4, 12), fill="x", anchor="w")
        else:
            self.src_label.pack_forget()
        self._fit()

    def set_click_through(self, enabled: bool) -> None:
        self.cfg.click_through = enabled
        hwnd = win32gui.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if enabled:
            style |= win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT
        else:
            style &= ~win32con.WS_EX_TRANSPARENT
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(),
                      event.y_root - self.root.winfo_y())

    def _drag_move(self, event) -> None:
        if not self._drag:
            return
        dx, dy = self._drag
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _fit(self) -> None:
        """Shrink-wrap the window around the current text."""
        self.root.update_idletasks()
        self.root.geometry(f"{self.cfg.width}x{self.root.winfo_reqheight()}")

    # -- content ----------------------------------------------------------
    def push(self, line: Optional[Line]) -> None:
        """Thread-safe. `None` asks the overlay to close."""
        self.queue.put(line)

    def _drain(self) -> None:
        try:
            while True:
                line = self.queue.get_nowait()
                if line is None:
                    self.close()
                    return
                self._render(line)
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(40, self._drain)

    def _render(self, line: Line) -> None:
        if line.role == "name":
            # A speaker-name region only updates the caption above the line.
            self._speaker = line.source.strip()
            self.speaker_label.config(text=self._speaker)
            self._fit()
            return
        if line.speaker:
            self._speaker = line.speaker
            self.speaker_label.config(text=self._speaker)
        self.main_label.config(text=line.translation or "...")
        if self.cfg.show_source:
            self.src_label.config(text=line.source)
        self._fit()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.root.destroy()
        except Exception:
            pass
        if self._on_close:
            self._on_close()

    def run(self) -> None:
        if not self.owns_root:
            raise RuntimeError("overlay does not own the Tk loop; "
                               "the control window runs it instead")
        self.root.mainloop()
