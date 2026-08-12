"""Nothing on screen says `dialogue`, and nothing in the config says 对话正文.

The region editor showed the raw config values in its dropdowns -- dialogue,
name, choice, info, and a bare `ja` -- to someone deciding what a region is.
The Chinese labels already existed and were only used for a note underneath.

Showing labels means translating both ways, and the direction that matters is
back: a label written into config.json would be a config no loader
understands. This drives the editor's own mapping in both directions.

    python tools/check_region_labels.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.config import Region                              # noqa: E402
from glt.control import (LANG_BY_LABEL, LANG_LABEL, ROLE_BY_LABEL,  # noqa: E402
                         ROLE_LABEL, ROLE_LABELS, ROLES)

failures = []

print(f"  类型下拉显示: {ROLE_LABELS}")
print(f"  语言下拉显示: {list(LANG_LABEL.values())}")

# Every value the config can hold has a label, and no label is ASCII-only.
for role in ROLES:
    if role not in ROLE_LABEL:
        failures.append(f"类型 {role!r} 没有中文标签")
    elif ROLE_LABEL[role].isascii():
        failures.append(f"类型 {role!r} 的标签还是英文: {ROLE_LABEL[role]!r}")

# Round trip: label -> value -> label, for everything the dropdowns offer.
for label in ROLE_LABELS:
    value = ROLE_BY_LABEL.get(label)
    if value not in ROLES:
        failures.append(f"标签 {label!r} 映射不回配置值，得到 {value!r}")
    elif ROLE_LABEL[value] != label:
        failures.append(f"{label!r} 往返之后变成了 {ROLE_LABEL[value]!r}")
for label, tag in LANG_BY_LABEL.items():
    if LANG_LABEL.get(tag) != label:
        failures.append(f"语言 {label!r} 往返不一致")

# A tag the dropdown does not list must survive being typed in: someone with
# a language pack outside this list should not have it silently rewritten.
for typed in ("de", "fr-FR", "zh-Hant-TW"):
    if LANG_BY_LABEL.get(typed, typed) != typed:
        failures.append(f"手打的语言 {typed!r} 被改掉了")

# And the object that ends up in the config carries values, never labels.
region = Region(name="r1", role=ROLE_BY_LABEL["其他信息"],
                lang=LANG_BY_LABEL["日文 (ja)"], translate=True,
                box=(0.0, 0.0, 1.0, 1.0))
print(f"  选「其他信息 / 日文 (ja)」后写进配置的是: "
      f"role={region.role!r} lang={region.lang!r}")
if region.role != "info" or region.lang != "ja":
    failures.append(f"写进配置的是标签而不是值: {region.role!r} {region.lang!r}")


# --- overlapping regions must be called out ---------------------------------
# Two boxes over the same text read it twice, translate it twice and draw
# twice; whichever caption is drawn second wins and the screen looks half
# translated. A leftover box from the shipped example plus a full-screen one
# drawn later is exactly how a user got there.
from glt.control import ControlWindow                      # noqa: E402


class FakePanel:
    _warn_overlapping_regions = ControlWindow._warn_overlapping_regions

    def __init__(self, boxes):
        self.cfg = type("C", (), {})()
        self.cfg.regions = [Region(name=f"r{i}", role="info", lang="ja",
                                   translate=True, box=b)
                            for i, b in enumerate(boxes)]
        self.said = []

    def _log(self, text, tag):
        self.said.append((tag, text))


def warned(boxes):
    panel = FakePanel(boxes)
    panel._warn_overlapping_regions()
    return any(tag == "err" for tag, _ in panel.said)


FULL = (0.0, 0.05, 1.0, 0.95)
SUBTITLE = (0.1, 0.76, 0.9, 0.96)        # the shipped example's box
NAMEPLATE = (0.1, 0.68, 0.32, 0.75)      # above it, no overlap
cases = [
    ("整屏框 + 模板自带的字幕条", [FULL, SUBTITLE], True),
    ("字幕条 + 上方的人名框", [SUBTITLE, NAMEPLATE], False),
    ("只有一个框", [FULL], False),
    ("左右分开的两个框", [(0.0, 0.1, 0.45, 0.9), (0.55, 0.1, 1.0, 0.9)], False),
]
for label, boxes, want in cases:
    got = warned(boxes)
    print(f"  {label:<24} 提示重叠: {got}（应为 {want}）")
    if got != want:
        failures.append(f"{label}: 重叠判断错了")


# --- editing regions while running has to take effect ------------------------
# The pipeline builds one runtime per region when it starts. Changing the
# regions afterwards updated the config and nothing else: the old boxes kept
# firing and the new ones never did, with nothing on screen saying so.
import glt.control as control                             # noqa: E402


class FakeEditor:
    def __init__(self, master, hwnd, regions, lang, result=None):
        self.result = result


def edit_with(existing, returned, running):
    panel = FakePanel([r.box for r in existing])
    panel.cfg.regions = existing
    panel.pipeline = object() if running else None
    panel.restarted = []
    panel.root = type("R", (), {"wait_window": lambda self, w: None})()
    panel._selected_window = lambda: type("W", (), {"hwnd": 1, "title": "x"})()
    panel._update_region_summary = lambda: None
    panel._stop = lambda: panel.restarted.append("stop")
    panel._start = lambda: panel.restarted.append("start")
    panel._warn_overlapping_regions = lambda: None
    panel._edit_regions = ControlWindow._edit_regions.__get__(panel)
    original = control.RegionEditor
    control.RegionEditor = lambda *a, **k: FakeEditor(*a, result=returned, **k)
    try:
        panel._edit_regions()
    finally:
        control.RegionEditor = original
    return panel.restarted


def region(name, box):
    return Region(name=name, role="info", lang="ja", translate=True, box=box)


old = [region("a", (0.0, 0.0, 1.0, 0.5))]
new = [region("b", (0.0, 0.5, 1.0, 1.0))]

cases = [
    ("运行中改了区域", old, new, True, ["stop", "start"]),
    ("运行中没改", old, [region("a", (0.0, 0.0, 1.0, 0.5))], True, []),
    ("没在运行时改", old, new, False, []),
    ("点了取消", old, None, True, []),
]
for label, existing, returned, running, want in cases:
    got = edit_with(list(existing), returned, running)
    print(f"  {label:<16} 重启动作: {got}（应为 {want}）")
    if got != want:
        failures.append(f"{label}: 期望 {want}，实际 {got}")


# --- repeated timeouts must name the model ----------------------------------
# A slow model and a network hiccup look identical in a rising error count.
# Measured on one endpoint over the same eighteen lines, one model ran to a
# median of 7.8s against an 8s budget and lost half of them; another ran to
# 1.6s and lost none.
class TimeoutPanel:
    _TIMEOUT_PATIENCE = ControlWindow._TIMEOUT_PATIENCE
    _warn_if_model_too_slow = ControlWindow._warn_if_model_too_slow

    def __init__(self):
        self._timeouts = 0
        self._slow_model_warned = False
        self.said = []
        self.cfg = type("C", (), {})()
        self.cfg.translate = type("T", (), {"timeout_s": 8.0})()

    def _key_section(self):
        return {"model": "gpt-5.6-terra"}

    def _log(self, text, tag):
        self.said.append((tag, text))


panel = TimeoutPanel()
warned_at = None
for i in range(1, 6):
    panel._timeouts = i
    panel._warn_if_model_too_slow()
    if panel.said and warned_at is None:
        warned_at = i
errs = [t for tag, t in panel.said if tag == "err"]
print(f"  第 {warned_at} 次超时时提示（耐心值 {ControlWindow._TIMEOUT_PATIENCE}）")
print(f"  提示了 {len(errs)} 次（应为 1，不该刷屏）")
if warned_at != ControlWindow._TIMEOUT_PATIENCE:
    failures.append(f"该在第 {ControlWindow._TIMEOUT_PATIENCE} 次提示，"
                    f"实际第 {warned_at} 次")
if len(errs) != 1:
    failures.append(f"提示重复了 {len(errs)} 次")
if not any("gpt-5.6-terra" in t for _, t in panel.said):
    failures.append("提示里没写是哪个模型")

print("\nRESULT:", "PASS - 界面中文、配置存值、重叠会点破、改完区域生效、"
      "老超时会指向模型"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
