"""An outlined caption may step above its original only into empty space.

Stepping above avoids printing Chinese on top of Japanese. In a paragraph
the space above a line is the previous line, so the step just moves the
collision up one row: an Elden Ring INFORMATION dialog came out as every
translated line printed across the Japanese line above it, the whole panel
illegible. Dense text has to be covered instead -- there is nowhere to step.

    python tools/check_placement.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.inplace import can_step_above          # noqa: E402

# A body-text dialog: lines 26px tall, one every 30px. Verbatim shape of the
# INFORMATION panel that broke.
PARAGRAPH = [(600, 400 + 30 * i, 1400, 426 + 30 * i) for i in range(8)]

# Text with room around it: an item pickup, a HUD label, a lone menu entry.
SPARSE = [(200, 640, 460, 668), (1500, 240, 1800, 268), (900, 1000, 1100, 1028)]

failures = []

# Every line of the paragraph except the first sits under another one, so the
# screen as a whole cannot step and must be covered -- all of it, or it reads
# as two subtitle modes at once.
stepped = [i for i, box in enumerate(PARAGRAPH)
           if can_step_above(box, PARAGRAPH[:i] + PARAGRAPH[i + 1:])]
print(f"  段落 {len(PARAGRAPH)} 行：可以上移的 {stepped}")
if stepped != [0]:
    failures.append(f"段落里只有第一行上方是空的，实际认为 {stepped} 行都能上移")

# Isolated text has nothing above it, so the step is safe and the original
# stays visible -- which is the whole point of the outline style.
free = [i for i, box in enumerate(SPARSE)
        if can_step_above(box, SPARSE[:i] + SPARSE[i + 1:])]
print(f"  分散的 {len(SPARSE)} 处：可以上移的 {free}")
if len(free) != len(SPARSE):
    failures.append(f"分散的文字本该都能上移，实际只有 {free}")

# Side by side on the same row must not block each other: a stat and its
# number are neighbours horizontally, not vertically.
ROW = [(100, 500, 300, 528), (400, 500, 600, 528)]
if not all(can_step_above(b, [o for o in ROW if o is not b]) for b in ROW):
    failures.append("同一行并排的两块文字互相挡住了上移")
print(f"  同一行并排两块：都能上移 "
      f"{all(can_step_above(b, [o for o in ROW if o is not b]) for b in ROW)}")


# --- what _redraw actually decides, without needing a display ----------------
from glt.config import OverlayConfig               # noqa: E402
from glt.inplace import InPlaceOverlay             # noqa: E402
from glt.overlay import Placement                  # noqa: E402


CHAR = 18        # pretend every glyph is this wide, so bbox is predictable


class FakeCanvas:
    """Records the create_* calls _redraw makes, with a believable bbox."""

    def __init__(self):
        self.texts, self.rects, self.stipples, self.fills = [], [], [], []
        self.band_rects = []

    def delete(self, _which):
        self.texts.clear()
        self.rects.clear()
        self.stipples.clear()
        self.fills.clear()
        self.band_rects = []

    def create_text(self, x, y, **kw):
        self.texts.append({"x": x, "y": y, **kw})
        return len(self.texts)

    def create_rectangle(self, *box, **kw):
        self.rects.append(box)
        self.stipples.append(kw.get("stipple", ""))
        self.fills.append(kw.get("fill", ""))
        return -len(self.rects)         # negative: never collides with a text

    def itemconfigure(self, item, **kw):
        self.texts[item - 1].update(kw)

    def bbox(self, item):
        t = self.texts[item - 1]
        width = len(t["text"]) * CHAR
        rows = 1
        if t.get("width"):                    # wrapped: as wide as allowed
            rows = max(1, -(-width // t["width"]))
            width = min(width, t["width"])
        left = t["x"] if t["anchor"] in ("nw", "w") else t["x"] - width / 2
        return (left, t["y"] - 10, left + width, t["y"] - 10 + 20 * rows)

    def move(self, item, dx, dy):
        self.texts[item - 1]["x"] += dx
        self.texts[item - 1]["y"] += dy

    def coords(self, item, x, y):
        self.texts[item - 1]["x"] = x
        self.texts[item - 1]["y"] = y

    def tag_raise(self, _id):
        pass

    def tag_lower(self, _id, _below=None):
        pass


def draw(placements, **overrides):
    overlay = object.__new__(InPlaceOverlay)
    settings = {"mode": "inplace", "placement": "over", "label_style": "plate"}
    settings.update(overrides)
    overlay.cfg = OverlayConfig(**settings)
    overlay.canvas = FakeCanvas()
    overlay._geometry = (0, 0, 1920, 1080)
    overlay._frame_size = (1920, 1080)
    overlay._placements = placements
    overlay._redraw()
    return overlay.canvas


# One row stays one row: no wrapping, centred on the line it replaces.
one = draw([Placement("装備", "装备", (100, 200, 300, 240), line_height=40)])
if not one.texts:
    failures.append("单行没有画出来")
else:
    first = one.texts[0]
    if first["anchor"] != "w":
        failures.append(f"单行的锚点不对: {first['anchor']}")
    if first["width"] != 0:
        failures.append(f"单行不该换行，实际 width={first['width']}")
    if first["x"] != 100 + 6:
        failures.append(f"放得下的单行不该挪动，实际 x={first['x']}")

# A long translation starting near the right margin, but narrow enough to
# fit somewhere: moved left, not wrapped, because wrapping would drop the
# overflow onto the caption below.
LONG = "撒散禁书的纸片，将周围相互连接，传播、转换伤害"
edge = draw([Placement("あ", LONG, (1700, 400, 1900, 440), line_height=40)])
drawn = edge.texts[0]
right = drawn["x"] + len(LONG) * CHAR
print(f"  贴右边的长句：x {1700 + 6} → {drawn['x']}，右端 {right}（窗口宽 1920）")
if drawn["width"] != 0:
    failures.append(f"单行被折行了: width={drawn['width']}")
if right > 1920:
    failures.append(f"挪完还是冲出屏幕，右端 {right}")
if drawn["x"] < 0:
    failures.append(f"挪过头挪出左边了: x={drawn['x']}")

# Wider than the whole window: moving left cannot help, so it must wrap.
# 4 repeats = 136 chars = 2448px of glyphs against a 1920px window.
HUGE = "呵呵，发出这么没出息的声音。发出这么可爱的声音，会让我更想欺负你了哦" * 4
huge = draw([Placement("あ", HUGE, (100, 400, 900, 440), line_height=40)])
drawn = huge.texts[0]
right = drawn["x"] + min(len(HUGE) * CHAR, drawn["width"] or 10 ** 9)
print(f"  比整个窗口还宽的句子：width={drawn['width']}，右端 {right}")
if not drawn["width"]:
    failures.append("超过窗口宽度了还不换行")
if right > 1920:
    failures.append(f"换行之后右端仍是 {right}，还在窗口外")

# Two joined rows: a paragraph, so it wraps inside the box and starts at
# the top rather than running one long line off the panel.
two = draw([Placement("あ\nい", "他者发动技能时，该技能可以发动且不需要消耗能量条",
                      (100, 200, 900, 280), line_height=40)])
if not two.texts:
    failures.append("多行没有画出来")
else:
    last = two.texts[-1]
    if last["anchor"] != "nw":
        failures.append(f"多行应从顶部排起，实际 anchor={last['anchor']}")
    if last["width"] != 800:
        failures.append(f"多行应按原文宽度换行，实际 width={last['width']}")
print(f"  单行 anchor={one.texts[-1]['anchor']} width={one.texts[-1]['width']}   "
      f"合并两行 anchor={two.texts[-1]['anchor']} width={two.texts[-1]['width']}")

# A dialogue plate widened via plate_box spans the region while the caption
# text stays at the original's left edge -- the recogniser keeps missing
# trailing hearts and brackets, and the region is the dialog box itself.
wide = draw([Placement("あ\nい", "第一行译文换行第二行",
                       (300, 400, 900, 480), line_height=40,
                       plate_box=(100, 400, 1500, 480))])
plate_left = min(r[0] for r in wide.rects)
plate_right = max(r[2] for r in wide.rects)
text_x = wide.texts[0]["x"]
print(f"  对话底板: 铺到 {plate_left}~{plate_right}（region 100~1500），"
      f"文字仍从 {text_x} 起")
if plate_left > 100 or plate_right < 1500:
    failures.append(f"底板没有铺满 plate_box: {plate_left}~{plate_right}")
if text_x != 300 + 6:
    failures.append(f"文字被挪到了底板左缘: x={text_x}")

# Seamless (also reachable under its old config name "outline"), to the
# reference effect: the translation replaces the original IN PLACE, left
# edges aligned, and the mask hugs the line -- the original's own box plus
# a tail allowance, plus whatever the translation needs, and nothing more.
# Two layers draw it. The erase strip on the canvas paints the original's
# box in the colours sampled behind it, pre-darkened to what they look like
# through the rgba(35,35,35,BAND_ALPHA) veil (the canvas rides above the
# band window, so the veil never dims the strip -- undarkened it glowed
# like a second plate). The band windows (canvas.band_rects) are the
# translucent grey itself, padded just past the strip.
from glt.inplace import BAND_ALPHA, _lerp_colour    # noqa: E402

VEILED = _lerp_colour("#c9a084", "#232323", BAND_ALPHA)
for legacy in ("seamless", "outline"):
    solo = draw([Placement("「待って……」", "「等一下……」",
                           (100, 400, 700, 440), line_height=40,
                           bg="#c9a084")],
                label_style=legacy)
    if len(solo.rects) != 1 or solo.stipples[0] or solo.fills[0] != VEILED:
        failures.append(f"{legacy}: 盖原文的应是「隔纱看到的取色」{VEILED}，"
                        f"实际 {len(solo.rects)} 块 "
                        f"fill={solo.fills} stipple={solo.stipples}")
    else:
        sx0, sy0, sx1, sy1 = solo.rects[0]
        if sx0 > 100 or sy0 > 400 or sx1 < 700 or sy1 < 440:
            failures.append(f"{legacy}: 实心条没盖满原文框: {solo.rects[0]}")
    if len(solo.band_rects) != 1:
        failures.append(f"{legacy}: 单条字幕应有一块遮罩，实际 {solo.band_rects}")
        continue
    rx0, ry0, rx1, ry1 = solo.band_rects[0]
    if rx0 > 100 or rx1 < 700 or ry0 > 400 or ry1 < 440:
        failures.append(f"{legacy}: 遮罩没盖住原文: {solo.band_rects[0]}")
    if rx0 < 100 - 40 * 2 or rx1 > 700 + 40 * 2.5:
        failures.append(f"{legacy}: 遮罩向两边延伸过长: {solo.band_rects[0]}"
                        f"（原文 100~700）")
    if ry0 < 400 - 40 or ry1 > 440 + 40:
        failures.append(f"{legacy}: 遮罩上下超出一行太多: {solo.band_rects[0]}")
    body = solo.texts[0]
    if body["x"] != 100 + 6:
        failures.append(f"{legacy}: 译文应原位左对齐原文（x=106），"
                        f"实际 x={body['x']}")
    if abs(body["y"] - 420) > 2:
        failures.append(f"{legacy}: 字幕没有跟着原文行 y={body['y']}")
print("  半透明字幕（新旧两个名字）: 译文原位左对齐、遮罩贴行只盖原文 ✓")

# The mask follows the line it covers: a two-character label gets a small
# lozenge, not a bar. A translation wider than the window still wraps and
# the mask stays inside the window.
short_band = draw([Placement("装備", "装备", (100, 400, 300, 440),
                             line_height=40)], label_style="seamless")
sb = short_band.band_rects[0]
print(f"  两字标签的遮罩: {sb}（原文 100~300，应贴着它）")
if sb[2] - sb[0] > (300 - 100) + 40 * 4:
    failures.append(f"两字标签的遮罩太宽: {sb}")
long_band = draw([Placement("あ", HUGE * 3, (100, 400, 1800, 440),
                            line_height=40)], label_style="seamless")
wrapped = long_band.texts[0]
lb = long_band.band_rects[0]
if not wrapped.get("width"):
    failures.append("比窗口宽的译文没有换行")
if lb[0] < 0 or lb[2] > 1920 or lb[3] > 1080:
    failures.append(f"超长句的遮罩出了窗口: {lb}")

# A hint in the top corner and the dialogue at the bottom must NOT share a
# band. The first cut unioned every band into one window, and on the real
# game that veiled the entire screen between the two captions.
far = draw([Placement("大公に近づく", "靠近大公", (1700, 100, 1900, 140),
                      line_height=40),
            Placement("「待って……」", "「等一下……」",
                      (200, 900, 800, 940), line_height=40)],
           label_style="seamless")
print(f"  相距很远的两条: 灰框 {len(far.band_rects)} 块（必须 2，不许并成整屏纱罩）")
if len(far.band_rects) != 2:
    failures.append(f"相距很远的两条并成了 {len(far.band_rects)} 块: {far.band_rects}")
elif max(r[3] - r[1] for r in far.band_rects) > 1080 * 0.5:
    failures.append(f"灰框高得像整屏纱罩: {far.band_rects}")

# Hover shows both texts: caption above the line, no backing at all --
# unless the screen is too dense to hover, when it falls back to seamless.
hov = draw([Placement("「待って……」", "「等一下……」",
                      (100, 400, 700, 440), line_height=40, bg="#c9a084")],
           label_style="hover")
print(f"  悬浮对照（稀疏）: 底 {len(hov.rects)} 块  文字 y={hov.texts[-1]['y']}"
      f"（应在原文上方 <400）")
if hov.rects or hov.band_rects:
    failures.append("悬浮对照不该有底或灰框")
if hov.texts[-1]["y"] >= 400:
    failures.append(f"悬浮对照没有浮到原文上方: y={hov.texts[-1]['y']}")

hov2 = draw(dense_hover := [
    Placement("説明", "说明", (100, 300, 900, 340), line_height=40),
    Placement("続き", "续行", (100, 344, 900, 384), line_height=40)],
    label_style="hover")
print(f"  悬浮对照（密集）: 实心条 {len(hov2.rects)} 块  "
      f"灰框 {len(hov2.band_rects)} 块（应退回融入：两条实心、灰框相邻合一）")
if len(hov2.rects) != 2 or any(hov2.stipples) or not hov2.band_rects:
    failures.append("密集屏的悬浮对照应退回融入样式（实心条盖原文+灰框窗口）")

# One screen, one mode. A name with space above it and a description without
# must not come out half stepped-above and half covered.
def dense():
    """A name with room above it, then two description rows without."""
    return [Placement("名前", "名称", (100, 100, 400, 140), line_height=40),
            Placement("説明", "说明", (100, 300, 900, 340), line_height=40),
            Placement("続き", "续行", (100, 344, 900, 384), line_height=40)]


half = draw(dense(), label_style="outline")
if len(half.rects) != 3 or any(half.stipples):
    failures.append(f"融入样式应为每条画一条实心遮原文条，实际 "
                    f"{len(half.rects)} 块 stipple={half.stipples}")
if any(f != "#232323" for f in half.fills):
    failures.append(f"没采到背景色时遮原文条应退回 #232323，实际 {half.fills}")
if not half.band_rects:
    failures.append("密集屏的融入样式没有灰框")
else:
    # Every line's centre must sit inside some band; the two adjacent
    # descriptions share one, the name keeps its own.
    for p in dense():
        cy = (p.box[1] + p.box[3]) / 2
        if not any(r[1] <= cy <= r[3] for r in half.band_rects):
            failures.append(f"第 {p.source!r} 行不在任何灰框里: {half.band_rects}")
print(f"  同屏 3 条（其中两条挨着）→ 实心条 {len(half.rects)} 块，"
      f"灰框 {len(half.band_rects)} 块: {half.band_rects}")

# The two styles must not collapse into each other: plate paints sampled
# colours on the canvas and never summons a band window; seamless paints
# only band-colour strips and always does.
solid = draw(dense(), label_style="plate")
print(f"  密集屏：衬底色块 rects={len(solid.rects)} bands={solid.band_rects}   "
      f"融入 rects={len(half.rects)} bands={len(half.band_rects)}")
if any(solid.stipples):
    failures.append("衬底色块不该是半透的")
if solid.band_rects:
    failures.append("衬底色块不该出灰框窗口")
if not solid.rects:
    failures.append("衬底色块没有画底")
# The seamless one keeps its stroke, drawn on top of the band.
if len(half.texts) <= len(solid.texts):
    failures.append("融入样式少了描边那几层")

print("\nRESULT:", "PASS - 密集正文改成盖住原文，分散文字照旧上移，合并段落按框宽换行"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
