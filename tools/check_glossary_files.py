"""Every shipped glossary must load, and its overrides must be deliberate.

A glossary entry is a standing order: it rewrites every line it matches with
no model in the loop to notice. The failures that matter are silent ones --
a file that will not parse (load() raises and the app will not start), a key
carrying a character from the wrong script (it can never match anything the
recogniser produces), a value left as a placeholder.

Disagreement between files is NOT one of them. The files are layered, and
the whole point of the layering is that a later file overrides an earlier
one: 「インベントリ」 is 物品栏 generally and 持有物品 in Dark Souls, and
_merge_glossary_file resolves that by config order. So overrides are listed
for a human to confirm, and only a disagreement inside one layer -- two
files that sit at the same level and could stack in either order -- is
reported as a defect, because there the winner depends on nothing.

    python tools/check_glossary_files.py
"""
from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Lowest first. A file's layer decides who is expected to win: anything in a
# later layer may override an earlier one, and does so by design.
LAYER = {"common": 0, "galgame": 1, "souls-series": 1}


def layer_of(name: str) -> int:
    return LAYER.get(name.replace("-ja-zh.json", ""), 2)   # per-game = 2


failures = []
loaded = {}

for path in sorted((ROOT / "glossaries").glob("*.json")):
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        failures.append(f"{path.name} 不是合法 JSON: {exc}")
        continue
    terms = {k: v for k, v in raw.items()
             if not k.startswith("//") and isinstance(v, str)}
    loaded[path.name] = terms
    print(f"  {path.name:28} {len(terms):4} 条   第 {layer_of(path.name)} 层")

    for key, value in terms.items():
        if not key.strip() or not value.strip():
            failures.append(f"{path.name}: 空的键或值 {key!r} -> {value!r}")
        for ch in key:
            if "가" <= ch <= "힯" or "Ѐ" <= ch <= "ӿ":
                failures.append(
                    f"{path.name}: 键 {key!r} 混入了非日文字符 "
                    f"{ch!r} ({unicodedata.name(ch, '?')})")
                break
        if value.strip() == "——":
            failures.append(f"{path.name}: {key!r} 的值还是占位符")

overrides, agree = [], 0
names = sorted(loaded)
for i, a in enumerate(names):
    for b in names[i + 1:]:
        shared = loaded[a].keys() & loaded[b].keys()
        if not shared:
            continue
        clash = {k for k in shared if loaded[a][k] != loaded[b][k]}
        agree += len(shared) - len(clash)
        for term in sorted(clash):
            if layer_of(a) == layer_of(b):
                # Same layer. Two per-game files never stack (nobody plays
                # two games at once), so that is noted, not failed; anything
                # else at one level has no defined winner.
                if layer_of(a) == 2:
                    overrides.append(
                        f"  · {term!r}: {a}={loaded[a][term]!r} / "
                        f"{b}={loaded[b][term]!r} —— 两个游戏专属表，不会同时加载")
                else:
                    failures.append(
                        f"同层冲突 {term!r}: {a} 作 {loaded[a][term]!r}，"
                        f"{b} 作 {loaded[b][term]!r}，谁赢取决于配置顺序")
            else:
                low, high = (a, b) if layer_of(a) < layer_of(b) else (b, a)
                overrides.append(
                    f"  · {term!r}: {high} 用 {loaded[high][term]!r} "
                    f"覆盖 {low} 的 {loaded[low][term]!r}")

print(f"\n跨表重复且译法一致: {agree} 条")
if overrides:
    print(f"\n按分层生效的覆盖（设计如此，确认一眼即可）:")
    for line in overrides:
        print(line)

total = sum(len(t) for t in loaded.values())
merged = len({k for t in loaded.values() for k in t})
print(f"\n合计 {total} 条，去重后 {merged} 条，跨 {len(loaded)} 个文件")

if failures:
    print(f"\nFAIL: {len(failures)}")
    for f in failures:
        print("  " + f)
    raise SystemExit(1)
print("\nall good")
