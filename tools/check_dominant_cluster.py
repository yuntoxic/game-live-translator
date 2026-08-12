"""Scenery fragments must not be read into the dialogue's sentence.

A brick wall above one dialog box came back as six short fragments -- each
too short for any per-line filter to judge -- and the dialogue caption began
with their translation: ■坏的 第一的 第一员 第一口 工作口. The real line is
one vertical run of adjacent rows; debris is scattered above it.

    python tools/check_dominant_cluster.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.ocr import TextBlock, dominant_cluster           # noqa: E402


def block(text, x0, y0, height=34, width=None):
    return TextBlock(text, x0, y0, x0 + (width or len(text) * height), y0 + height)


failures = []

# The measured shape: wall debris scattered well above the box, then the
# dialogue's two adjacent rows inside it.
WALL = [block("■ーい悪し", 60, 40), block("第一い", 300, 55),
        block("第一員", 520, 30), block("第一ロ", 760, 62),
        block("働ーロ", 950, 45), block("せ", 1150, 58, height=30)]
DIALOGUE = [block("「俺に一々口出しするなと言っただろう？", 80, 250),
            block("じゃないと……こういう目に遭うからな」", 110, 292)]

kept = dominant_cluster(WALL + DIALOGUE)
texts = [b.text for b in kept]
print(f"  墙纹 6 块 + 对话 2 行 → 保留 {len(kept)} 块: {[t[:12] for t in texts]}")
if set(texts) != {b.text for b in DIALOGUE}:
    failures.append(f"保留的不是对话本身: {texts}")

# Two-line dialogue alone survives untouched.
alone = dominant_cluster(list(DIALOGUE))
print(f"  只有对话 → 保留 {len(alone)} 块")
if len(alone) != 2:
    failures.append("干净的两行对话被误删了")

# A single block is never touched.
single = dominant_cluster([DIALOGUE[0]])
if len(single) != 1:
    failures.append("单块也被动了")

# Debris BELOW the dialogue goes too: position must not matter.
below = [block("笫一ロ", 200, 520), block("ーい", 700, 560)]
kept2 = dominant_cluster(DIALOGUE + below)
print(f"  对话 + 下方碎块 → 保留 {len(kept2)} 块")
if {b.text for b in kept2} != {b.text for b in DIALOGUE}:
    failures.append(f"下方的碎块没被丢掉: {[b.text for b in kept2]}")

# When the debris cluster is adjacent enough to merge with the dialogue,
# nothing is dropped -- the rule only removes whole distant clusters, and
# must never eat part of the sentence.
NEAR = [block("第一い", 300, 214)]           # within 1.8 pitch of row one
kept3 = dominant_cluster(NEAR + DIALOGUE)
print(f"  紧贴对话的碎块 → 保留 {len(kept3)} 块（并入，不误删对话）")
if not {b.text for b in DIALOGUE} <= {b.text for b in kept3}:
    failures.append("为了丢碎块把对话也丢了")


# --- the ornament next to the dialogue must not drag the caption ------------
# A dialog box's ornament column reads as a one-character block on the same
# rows as the dialogue -- too close for the cluster filter to drop -- and it
# slid the caption's left edge off the original's. The words position the
# text; the plate still covers everything.
from glt.config import AppConfig, Region                   # noqa: E402
from glt.pipeline import Pipeline                          # noqa: E402

cfg = AppConfig()
cfg.translate.backend = "none"
cfg.regions = [Region(name="d", role="dialogue", lang="ja", translate=True,
                      box=(0.0, 0.0, 1.0, 1.0))]
pipe = Pipeline(cfg)
pipe.frame_size = (1539, 1189)
rt = pipe.runtimes["d"]

ORNAMENT = block("|", 8, 60, 40, width=14)          # the purple column
DIALOGUE_LINE = block("「ほう、これはまた見事なものだ。よく出来ている」", 65, 58,
                      40, width=830)
place = pipe._place(rt, [ORNAMENT, DIALOGUE_LINE], ["译文"])   # noqa: SLF001
p = place[0]
print(f"  文字锚点 x={p.box[0]:.0f}（原文在 65）  底板 x={p.plate_box[0]:.0f}"
      f"..{p.plate_box[2]:.0f}（要盖到 8）")
if p.box[0] != 65:
    failures.append(f"装饰柱把字幕锚点拽到了 {p.box[0]:.0f}，应为 65")
if p.plate_box and p.plate_box[0] > 8:
    failures.append(f"底板没盖住装饰柱: 左缘 {p.plate_box[0]:.0f}")

print("\nRESULT:", "PASS - 远处的碎块整簇丢掉，对话一行不少，装饰不拽锚点"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
