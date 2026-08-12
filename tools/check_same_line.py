"""Variant re-reads of one dialogue line are the same line; new lines are not.

Every string here is a real recogniser output captured off the live game --
the same sentence spelt three different ways across passes -- plus real
consecutive story lines that must never be merged.

    python tools/check_same_line.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.config import Region                              # noqa: E402
from glt.pipeline import Pipeline                          # noqa: E402


class RT:
    def __init__(self, role, last):
        self.region = Region(name="r", role=role, lang="ja", translate=True,
                             box=(0, 0, 1, 1))
        self.last_text = last


failures = []

# One line, three passes. The damage is verbatim from the session log --
# a leading junk glyph that comes and goes, ellipses read as ・=, a closing
# 」 read as :ツ・ロ, a stray 言 -- carried onto a neutral sentence. Worst
# pair scores 0.714, against a 0.55 threshold.
VARIANTS = [
    "「待って・=言こんな所に居ては危険だ:ツ・ロ",
    "謇「待って・=こんな所に居ては危険だ:ツ・ロ",
    "「待って……こんな所に居ては危険だ……」",
]
for a in VARIANTS:
    for b in VARIANTS:
        if a == b:
            continue
        if not Pipeline._same_line(RT("dialogue", b), a):
            failures.append(f"同一行的变体被当成了新行:\n    {a!r}\n    {b!r}")

# Consecutive story lines: different sentences, must re-dispatch.
STORY = [
    ("「待って……こんな所に居ては危険だ……」",
     "「俺に一々口出しするなと言っただろう？」"),
    ("「今日も眠っているなら、私がここに居ても仕方ないか……。",
     "早く元気になってね」"),
]
for old, new in STORY:
    if Pipeline._same_line(RT("dialogue", old), new):
        failures.append(f"真换行被相似度吞掉了: {new[:20]!r}")

# Per-line regions keep exact matching: one item changing in a long joined
# menu is a high-similarity edit and must not be skipped.
menu_old = "ステータス\n装備\n持ち物\n魔法\n設定\n終了"
menu_new = "ステータス\n装備\n持ち物\n魔法\n設定\nセーブ"
if Pipeline._same_line(RT("info", menu_old), menu_new):
    failures.append("菜单里换了一项却被当成没变")

ok = sum(1 for a in VARIANTS for b in VARIANTS if a != b)
print(f"  变体两两互认 {ok} 对，真换行 {len(STORY)} 对不误吞，菜单精确匹配")
print("\nRESULT:", "PASS - 杂字变体认作同一行，真换行和菜单不受影响"
      if not failures else "FAIL\n  " + "\n  ".join(set(failures)))
raise SystemExit(1 if failures else 0)
