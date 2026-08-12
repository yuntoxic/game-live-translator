"""An empty OCR pass must not clear a caption whose text is still on screen.

Outlined glyphs over scenery read on one pass and not the next. The empty
pass used to be taken as "the screen moved on" and cleared the caption --
it flashed and vanished while the line sat there in plain sight. The pixels
arbitrate now: if the caption's own area still holds the edge density it
held at dispatch, the read was a flake.

    python tools/check_caption_hold.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.config import AppConfig, Region                   # noqa: E402
from glt.ocr import TextBlock                              # noqa: E402
from glt.pipeline import Pipeline                          # noqa: E402


def scene(with_text: bool) -> np.ndarray:
    """A busy brick-ish background; optionally a line of text on it."""
    rng = np.random.default_rng(3)
    img = rng.integers(96, 128, (300, 900, 4), dtype=np.uint8)
    img[:, :, 3] = 255
    if with_text:
        for x in range(60, 840, 6):        # dense strokes = a text line
            img[120:160, x:x + 2, :3] = 250
    return img


cfg = AppConfig()
cfg.translate.backend = "none"
cfg.regions = [Region(name="box", role="dialogue", lang="ja", translate=True,
                      box=(0.0, 0.0, 1.0, 1.0))]
pipe = Pipeline(cfg)
rt = pipe.runtimes["box"]

pushed = []
pipe._push = lambda line, to_overlay=True: pushed.append(line)   # noqa: SLF001
pipe.tr_slot.put = lambda name, payload: None

blocks = [TextBlock("「等一下……这种地方太危险了……」", 60, 118, 842, 162)]
pipe._dispatch("box", "「等一下……", blocks, scene(True))            # noqa: SLF001
print(f"  派发后记录: box={rt.caption_box} 指纹={'有' if rt.caption_fp is not None else '无'}")

failures = []
if not rt.caption_box or rt.caption_fp is None:
    failures.append("派发时没有记录字幕区域的指纹")

# The text is still there; an empty read must be treated as a flake.
still = pipe._caption_stale(rt, scene(True))                     # noqa: SLF001
print(f"  文字还在，空识别 → 判定为翻页: {still}（应为 False，即保住字幕）")
if still:
    failures.append("文字还在屏幕上，却被当成翻页清掉了")

# The text is gone; the same empty read must clear.
gone = pipe._caption_stale(rt, scene(False))                     # noqa: SLF001
print(f"  文字消失，空识别 → 判定为翻页: {gone}（应为 True，即清掉）")
if not gone:
    failures.append("文字真没了还不清，字幕会永远赖着")

# A busy background alone must not keep the caption alive: this is the case
# an edge-density comparison failed outright, because bricks are edge-dense
# with or without text on them.
noisy_only = scene(False)
if not pipe._caption_stale(rt, noisy_only):                      # noqa: SLF001
    failures.append("嘈杂背景把「文字已消失」藏住了")

# A region that never recorded a box keeps the old behaviour: clear.
rt2 = pipe.runtimes["box"]
rt2.caption_box, rt2.caption_fp = (), None
if not pipe._caption_stale(rt2, scene(False)):                   # noqa: SLF001
    failures.append("没有记录时应按老规矩清屏")


# The wipe above emptied the recorded fingerprint (rt2 is rt); restore it by
# re-dispatching the same line before testing the variant path.
pipe._dispatch("box", "「等一下……", blocks, scene(True))            # noqa: SLF001

# A variant re-read of the same line must not clear either: outlined glyphs
# come back spelt differently on every pass, and each variant used to clear
# a good caption and leave a translation-long gap.
pushed.clear()
pipe._dispatch("box", "謇「等一下・=这种地方", blocks, scene(True))    # noqa: SLF001
cleared = [p for p in pushed if not p.translation and not p.source]
print(f"  同一行的变体重读 → 清屏 {len(cleared)} 次（应为 0）")
if cleared:
    failures.append("识别出变体杂字就把好字幕清掉了")

# A real page turn -- new text, changed pixels -- still clears at once.
pushed.clear()
pipe._dispatch("box", "「全新的一句」", blocks, scene(False))      # noqa: SLF001
cleared = [p for p in pushed if not p.translation and not p.source]
print(f"  真换行 → 清屏 {len(cleared)} 次（应为 1）")
if len(cleared) != 1:
    failures.append(f"真换行时应清 1 次，实际 {len(cleared)}")

print("\nRESULT:", "PASS - 识别抽风保住字幕，文字真走了照常清"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
