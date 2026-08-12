"""The program must never offer its own windows as a capture target.

Double-clicking the built executable opens a console titled with the
executable's own path. A tester's config came back pointing at
实时翻译器.exe -- the picker had listed that console and the fallback took
it. Capturing it gives a black picture that never changes: frames arriving,
nothing ever recognised, and nothing on screen saying why.

Two nets, because the first one silently failed once already:

* by handle -- and ctypes must be told the return type, or a 64-bit HWND
  comes back truncated to a signed 32-bit int and matches nothing;
* by title, which catches the console of a built executable even when the
  handle comparison cannot.

    python tools/check_own_window.py
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures = []

# --- the handle, with and without the restype that was missing -------------
raw = ctypes.windll.kernel32.GetConsoleWindow          # default restype: c_int
typed = ctypes.windll.kernel32.GetConsoleWindow
typed.restype = ctypes.c_void_p
truncating, correct = raw(), typed()
print(f"  GetConsoleWindow  截断版 {truncating}   正确版 {correct}")
if correct is not None and correct < 0:
    failures.append("拿到的句柄是负数，restype 没设对")

# --- the picker's own filtering, without opening a window ------------------
from glt.control import ControlWindow                  # noqa: E402


class Fake:
    """Just enough of the panel to exercise the two filters."""
    _own_windows = ControlWindow._own_windows
    _own_titles = ControlWindow._own_titles


panel = Fake()
titles = panel._own_titles()
print(f"  会被按标题挡掉的: {sorted(titles)}")

exe = str(Path(sys.executable).resolve())
if exe not in titles:
    failures.append(f"没把自己的可执行文件路径 {exe!r} 列进去")
if "Game Live Translator" not in titles:
    failures.append("没把悬浮窗标题列进去")


class Win:
    def __init__(self, hwnd, title):
        self.hwnd, self.title = hwnd, title


# What the tester actually saw, plus the windows that must survive.
CANDIDATES = [
    Win(1, exe),                                   # its own console: drop
    Win(2, f"管理员: {exe}"),                        # run as admin: drop
    Win(3, "Game Live Translator Overlay"),        # the overlay: drop
    Win(4, "ELDEN RING NIGHTREIGN"),               # the game: keep
    Win(5, "Projector (Preview)"),                 # an OBS projector: keep
    Win(6, "DARK SOULS III"),                      # keep
]
kept = [w.title for w in CANDIDATES
        if w.hwnd not in panel._own_windows()
        and not any(t in w.title for t in titles)]
print(f"  {len(CANDIDATES)} 个候选窗口 → 留下 {len(kept)}: {kept}")

for gone in (exe, f"管理员: {exe}", "Game Live Translator Overlay"):
    if gone in kept:
        failures.append(f"自己的窗口没被挡掉: {gone}")
for stays in ("ELDEN RING NIGHTREIGN", "Projector (Preview)", "DARK SOULS III"):
    if stays not in kept:
        failures.append(f"误伤了真窗口: {stays}")

print("\nRESULT:", "PASS - 自己的控制台和悬浮窗都挡掉了，游戏窗口没误伤"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
