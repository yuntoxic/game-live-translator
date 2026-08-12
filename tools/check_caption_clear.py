"""Captions must clear when the text changes, and only then.

Two failures, one on each side of this line, both seen on screen:

* clearing too late -- captions were only replaced when their replacement
  arrived, a screenful of gateway time later, so leaving a menu left its
  captions over whatever came next;
* clearing too eagerly -- clearing on every trigger fire made captions strobe
  in any scene with fire or spell effects, because the effects move the
  picture constantly while the text underneath never changes.

    python tools/check_caption_clear.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.config import AppConfig, Region          # noqa: E402
from glt.pipeline import Pipeline                 # noqa: E402

cfg = AppConfig()
cfg.translate.backend = "none"
cfg.regions = [Region(name="screen", role="info", lang="ja", translate=True,
                      box=(0.0, 0.0, 1.0, 1.0))]

pipe = Pipeline(cfg)
cleared: list = []
sent: list = []
pipe._push = lambda line, to_overlay=True: cleared.append(line.region)  # noqa: SLF001
pipe.tr_slot.put = lambda name, payload: sent.append(payload[0])

# A menu opens, then the scene behind it burns for a while, then it closes.
STORY = [
    ("ステータス\n生命力",      "菜单打开"),
    ("ステータス\n生命力",      "特效在动，文字没变"),
    ("ステータス\n生命力",      "特效还在动"),
    ("持ち物\n黒火炎壺",        "换到道具栏"),
    ("持ち物\n黒火炎壺",        "特效在动，文字没变"),
]

for text, note in STORY:
    before = len(cleared)
    pipe._dispatch("screen", text, [])            # noqa: SLF001
    did = len(cleared) > before
    print(f"  {note:<16} 清空字幕: {'是' if did else '否'}")

# _dispatch only runs when the OCR text differs, so the repeats above stand in
# for what the OCR loop already filters; what is under test is that a genuine
# change clears and a redispatch of identical text does not.
failures = []
if len(cleared) != 1:
    failures.append(f"应该只在换界面时清一次，实际清了 {len(cleared)} 次")
if len(sent) != len(STORY):
    failures.append(f"每次都该送去翻译，实际 {len(sent)} 次")

print("\nRESULT:", "PASS - 文字变了才清，特效再动也不清"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
