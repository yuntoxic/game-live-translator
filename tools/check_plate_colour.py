"""A plate takes its colour from the picture, not from a fixed dark.

Drawn in the game's own background a caption reads as the game having been
in the target language all along; drawn in a fixed colour it reads as a patch
stuck over it. So the sample has to be the field the text sits on, and must
not be dragged toward the glyphs -- sampling the text box itself was 39
levels off the true background of a message popup.

    python tools/check_plate_colour.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.ocr import TextBlock, sample_background          # noqa: E402


def scene(bg, ink, coverage=0.45):
    """A 200x120 field of `bg` with a line of `ink` glyphs across the middle."""
    img = np.zeros((120, 200, 4), np.uint8)
    img[:, :, 0], img[:, :, 1], img[:, :, 2] = bg[0], bg[1], bg[2]
    img[:, :, 3] = 255
    # Vertical strokes over the middle band, as a line of text would be.
    step = max(2, int(1 / coverage))
    for x in range(20, 180, step):
        img[46:74, x:x + 1, 0] = ink[0]
        img[46:74, x:x + 1, 1] = ink[1]
        img[46:74, x:x + 1, 2] = ink[2]
    return img


def rgb(colour):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


LINE = TextBlock("あ", 20, 44, 180, 76)
failures = []

CASES = [
    # (background BGR, ink BGR, what it is)
    ((0, 0, 0), (255, 255, 255), "白字黑底（对话框）"),
    ((240, 238, 235), (20, 20, 20), "黑字浅底（说明面板）"),
    ((120, 90, 70), (250, 250, 250), "白字彩色底（半透明框）"),
]
for bg, ink, label in CASES:
    got = sample_background(scene(bg, ink), LINE)
    want = (bg[2], bg[1], bg[0])            # BGR stored, RGB reported
    diff = max(abs(a - b) for a, b in zip(rgb(got), want))
    print(f"  {label:<18} 取到 {got}  实际 #{want[0]:02x}{want[1]:02x}"
          f"{want[2]:02x}  差 {diff}")
    if diff > 6:
        failures.append(f"{label}: 取色差了 {diff}")

# Dense text must not drag it: this is what sampling the box itself did.
dense = sample_background(scene((0, 0, 0), (255, 255, 255), coverage=0.9), LINE)
print(f"  笔画很密的一行        取到 {dense}  应仍为黑")
if max(rgb(dense)) > 6:
    failures.append(f"密集笔画把底色带偏了: {dense}")

# A field that changes along the line has to come back as more than one
# colour, in order: a shaded dialog box flattened to one tone shows seams at
# both ends of the plate.
grad = np.zeros((120, 200, 4), np.uint8)
for x in range(200):
    value = 40 + int(x * 0.8)                 # dark left, light right
    grad[:, x, 0] = grad[:, x, 1] = grad[:, x, 2] = value
grad[:, :, 3] = 255
got = sample_background(grad, LINE, segments=4)
parts = got.split()
levels = [int(p[1:3], 16) for p in parts]
print(f"  左深右浅的渐变底      取到 {got}")
if len(parts) != 4:
    failures.append(f"渐变底应取 4 段，实际 {len(parts)}")
elif levels != sorted(levels):
    failures.append(f"分段颜色没有跟着渐变走: {levels}")
elif levels[-1] - levels[0] < 60:
    failures.append(f"分段颜色差太小，没抓住渐变: {levels}")

# A contaminated band must lose to the clean one. The band above a dialog
# box's first row reaches through the translucent edge into the scenery --
# a flower bed there turned several plate segments olive. Green above, box
# grey below: every segment must come back grey.
tainted = np.zeros((120, 200, 4), np.uint8)
tainted[:, :, 0], tainted[:, :, 1], tainted[:, :, 2] = 90, 96, 100   # box
tainted[:40, :, 0], tainted[:40, :, 1], tainted[:40, :, 2] = 40, 180, 60
tainted[:, :, 3] = 255
got = sample_background(tainted, TextBlock("あ", 20, 44, 180, 76), segments=4)
greens = [int(p[3:5], 16) for p in got.split()]
print(f"  上方被花坛污染        取到 {got}")
if any(g > 140 for g in greens):
    failures.append(f"花坛的绿色混进了底板: {got}")

# Nothing to sample from is an empty answer, not a wrong one.
edge = sample_background(scene((0, 0, 0), (255, 255, 255)),
                         TextBlock("あ", 0, 0, 1, 1))
print(f"  太小的框              取到 {edge!r}（应为空，交给配置的颜色）")
if edge:
    failures.append("太小的框也报了颜色")

print("\nRESULT:", "PASS - 底色取自画面，笔画带不偏，取不到就留空"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
