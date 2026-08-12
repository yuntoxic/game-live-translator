"""Window capture built on Windows.Graphics.Capture (WGC).

Why WGC and not a screen grab: WGC captures a *specific window* off the
compositor, so the target keeps producing frames even when it is occluded by
another window, sitting on a second monitor, or scrolled off-screen. That is
exactly what we need for an OBS projector window that we do not want to keep
in the foreground.

Portability note: WGC gained options over time. `draw_border` toggling is
Windows 11 only and raises on Windows 10, so instead of hardcoding a flag set
we probe candidate option sets at startup and keep the first one that starts.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np
import win32con
import win32gui

from windows_capture import WindowsCapture


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    width: int
    height: int

    def __str__(self) -> str:
        return f"{self.hwnd:>10}  {self.width:>5}x{self.height:<5}  {self.title}"


def list_windows(min_size: int = 160) -> List[WindowInfo]:
    """Visible top-level windows big enough to plausibly hold a video feed."""
    out: List[WindowInfo] = []

    def _cb(hwnd: int, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        w, h = right - left, bottom - top
        if w < min_size or h < min_size:
            return
        # Skip cloaked windows (UWP ghosts) which capture as pure black.
        if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 0x00000080:
            return  # WS_EX_TOOLWINDOW
        out.append(WindowInfo(hwnd, title, w, h))

    win32gui.EnumWindows(_cb, None)
    return out


def find_window(title_substring: str) -> Optional[WindowInfo]:
    """First visible window whose title contains `title_substring`.

    Matching is case-insensitive and prefers the largest match, because an OBS
    projector and its parent window can both match a loose substring.
    """
    needle = title_substring.lower()
    hits = [w for w in list_windows() if needle in w.title.lower()]
    if not hits:
        return None
    return max(hits, key=lambda w: w.width * w.height)


class WindowCapture:
    """Streams BGRA frames of one window and hands each to `on_frame`.

    `on_frame(frame_bgra)` runs on the capture thread, so it must be cheap.
    Do the crop-and-fingerprint work there; push anything slower onto a queue.
    """

    def __init__(
        self,
        hwnd: int,
        on_frame: Callable[[np.ndarray], None],
        max_fps: int = 20,
        on_closed: Optional[Callable[[], None]] = None,
    ) -> None:
        self.hwnd = hwnd
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._interval_ms = max(1, int(1000 / max(1, max_fps)))
        self._min_gap = 1.0 / max(1, max_fps)
        self._next_due = 0.0
        self._control = None
        self._closed = threading.Event()
        self.frames_seen = 0
        self.frames_dropped = 0
        self.options_used: dict = {}

    def _candidate_options(self) -> List[dict]:
        """Option sets from richest to plainest; first one that starts wins."""
        base = {"window_hwnd": self.hwnd}
        return [
            {**base, "cursor_capture": False, "draw_border": False,
             "minimum_update_interval": self._interval_ms},
            {**base, "cursor_capture": False,
             "minimum_update_interval": self._interval_ms},
            {**base, "cursor_capture": False},
            dict(base),
        ]

    def start(self) -> None:
        last_error: Optional[Exception] = None
        for opts in self._candidate_options():
            try:
                self._control = self._start_with(opts)
                self.options_used = opts
                return
            except Exception as exc:  # noqa: BLE001 - WGC raises plain Exception
                last_error = exc
                self._control = None
        raise RuntimeError(
            f"could not start window capture for hwnd={self.hwnd}: {last_error}"
        )

    def _start_with(self, opts: dict):
        cap = WindowsCapture(**opts)

        @cap.event
        def on_frame_arrived(frame, _ctrl):  # noqa: ANN001
            # WGC's own minimum_update_interval is honoured on Windows 11 but
            # silently ignored on Windows 10, where this arrives at full
            # compositor rate (~240 fps here). Rate-limit in software so the
            # pipeline sees the frame rate it asked for on every OS.
            now = time.monotonic()
            if now < self._next_due:
                self.frames_dropped += 1
                return
            self._next_due = now + self._min_gap
            self.frames_seen += 1
            try:
                self._on_frame(frame.frame_buffer)
            except Exception:
                # A handler crash must not kill the capture thread; the frame
                # is dropped and the next one gets a fresh attempt.
                pass

        @cap.event
        def on_closed():
            self._closed.set()
            if self._on_closed:
                self._on_closed()

        return cap.start_free_threaded()

    def wait_for_first_frame(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.frames_seen > 0:
                return True
            if self._closed.is_set():
                return False
            time.sleep(0.02)
        return False

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def stop(self) -> None:
        if self._control is not None:
            try:
                self._control.stop()
            except Exception:
                pass
            self._control = None


def grab_one_frame(hwnd: int, timeout: float = 5.0) -> Optional[np.ndarray]:
    """Single BGRA frame of a window. Used by the region picker."""
    holder: dict = {}
    got = threading.Event()

    def _keep(buf: np.ndarray) -> None:
        if not got.is_set():
            holder["frame"] = buf.copy()
            got.set()

    cap = WindowCapture(hwnd, _keep, max_fps=30)
    cap.start()
    got.wait(timeout)
    cap.stop()
    return holder.get("frame")
