"""Wrapped rows rejoin; menu rows do not.

A description too long for its panel is wrapped by the game and comes back as
one OCR line per row. Translated separately the second row has nothing to
resolve against -- 「それはゲージを必要としない」 alone came back as 它不需要
仪表, and a row wrapped mid-word (「…必要があ」 / 「ります。」) came back as
「。」. Joined, both are correct.

Menu rows are also consecutive lines at a regular pitch, so the risk of the
fix is stitching a menu into a paragraph. The coordinates here are measured
off a live Nightreign character screen and a Dark Souls III status panel.

    python tools/check_wrapped_rows.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.ocr import TextBlock, join_wrapped_rows      # noqa: E402


def block(text, x0, y0, height, width=800):
    return TextBlock(text, x0, y0, x0 + width, y0 + height)


# Right-hand panel of a Nightreign character screen, verbatim coordinates.
NIGHTREIGN = [
    block("針アビリティ", 2638, 442, 47, width=280),
    block("力の感応", 2835, 561, 43, width=190),
    block("他者がアーツを発動した時、アーツを発動できる", 2834, 644, 36, width=760),
    block("それはゲージを必要としない", 2836, 692, 36, width=470),   # 0.33: join
    block("スキル", 2634, 854, 54, width=170),
    block("トランス", 2846, 978, 42, width=200),
    block("自ら瀉血し、不吉の力を呼び覚ます", 2838, 1060, 37, width=590),  # 1.05: keep
    block("不吉の一撃", 2834, 1397, 43, width=230),
    block("身体から異形の骨を取り出して", 2834, 1480, 36, width=500),
    block("目標へ素早く移動し、穿つ", 2838, 1528, 36, width=430),    # 0.33: join
]
WANT_JOINED = {
    "他者がアーツを発動した時、アーツを発動できるそれはゲージを必要としない",
    "身体から異形の骨を取り出して目標へ素早く移動し、穿つ",
}

# A visual novel's dialogue box, verbatim off the screen: the second row is
# indented one character, which half a character of tolerance rejected by two
# pixels. The sentence then went to the model in halves.
INDENTED = [block("「そんなに慌てなくても", 197, 920, 36, width=446),
            block("時間はまだ十分に残っているわよ？」", 217, 969, 35,
                  width=500)]

# A menu list: same left edge, even pitch, every row its own item.
MENU = [block(t, 400, 300 + 70 * i, 40, width=260) for i, t in enumerate(
    ["ステータス", "装備", "持ち物", "魔法", "設定", "タイトルへ戻る"])]

# Two columns of a tightly set stat list: rows 52 apart with 40-tall glyphs,
# which is the exact spacing of a wrapped pair. Only the width tells them
# apart, and this case is why the width rule exists.
COLUMNS = [block("生命力", 200, 400, 40, width=180),
           block("集中力", 200, 452, 40, width=180),
           block("攻撃力", 900, 400, 40, width=180),
           block("防御力", 900, 452, 40, width=180)]

failures = []

joined = join_wrapped_rows(NIGHTREIGN)
texts = {b.text for b in joined}
print(f"  角色面板 {len(NIGHTREIGN)} 行 → {len(joined)} 块")
for want in WANT_JOINED:
    if want not in texts:
        failures.append(f"该合并却没合: {want[:24]}…")
if len(joined) != len(NIGHTREIGN) - 2:
    failures.append(f"应合并 2 处，实际 {len(NIGHTREIGN) - len(joined)} 处")
for b in joined:
    if b.text in WANT_JOINED and b.rows != 2:
        failures.append(f"合并块的行数应为 2，实际 {b.rows}")
    # The caption is sized from row_height; a two-row block must not report
    # the height of both rows or the font comes out twice too big.
    if b.text in WANT_JOINED and not (30 <= b.row_height <= 45):
        failures.append(f"合并块的单行高不对: {b.row_height}")

indented = join_wrapped_rows(INDENTED)
print(f"  缩进的对白 {len(INDENTED)} 行 → {len(indented)} 块")
if len(indented) != 1:
    failures.append(f"首行缩进的对白没合并：{[b.text for b in indented]}")
elif indented[0].rows != 2:
    failures.append(f"合并块的行数应为 2，实际 {indented[0].rows}")

menu = join_wrapped_rows(MENU)
print(f"  菜单 {len(MENU)} 行 → {len(menu)} 块")
if len(menu) != len(MENU):
    failures.append(f"菜单被合并了: {[b.text for b in menu]}")

cols = join_wrapped_rows(COLUMNS)
print(f"  双栏 {len(COLUMNS)} 行 → {len(cols)} 块")
if len(cols) != len(COLUMNS):
    failures.append(f"跨栏被缝在一起了: {[b.text for b in cols]}")

print("\nRESULT:", "PASS - 换行续接合并，菜单和分栏不动"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
