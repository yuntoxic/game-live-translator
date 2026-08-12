"""A glossary term one misread character away should still be found.

Windows OCR substitutes look-alike characters and no setting fixes it.
Measured on a rendered RPG Maker menu at 1287x759: 道具 came back as 道旦 and
セーブ as セーフ、 at every upscale from 1 to 4, and under every contrast and
binarisation setting tried -- aggressive ones read nothing at all. Those
lines miss the glossary and reach the model as nonsense, which is where a
設定 button captioned -三口 came from.

The risk is snapping a word that was read perfectly well onto a neighbour, so
this checks both directions: the misreads are recovered, and none of the 86
real game lines the filters test already carries gets rewritten.

    python tools/check_glossary_near.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glt.translate import TranslateConfig, Translator     # noqa: E402
from check_filters import KEEP                            # noqa: E402

terms = {k: v for k, v in json.loads(
    (ROOT / "glossaries" / "common-ja-zh.json").read_text(encoding="utf-8-sig")
).items() if not k.startswith("//")}

tr = Translator(TranslateConfig(backend="none", glossary=terms))

# (what OCR returned, what it should resolve to). All observed, not invented.
MISREAD = [
    ("セーフ、", "セーブ"),      # dakuten lost and read as a comma: measured
    ("防御カ", "防御力"),        # the classic kanji/katakana homoglyph
    # Not recovered, and deliberately so: 旦 is not a homoglyph of 具, it is
    # the recogniser losing strokes. Reaching it needs an edit distance, and
    # an edit distance is what rewrote 筋力 to 体力 and 技量 to 音量 below.
    ("道旦", None),
    ("スキ丿レ", None),
]

failures = []
print(f"  术语表 {len(terms)} 条")
for seen, truth in MISREAD:
    got = tr._glossary_exact(seen)
    want = terms.get(truth) if truth else None
    mark = "✓" if got == want else "✗"
    print(f"  {mark} {seen!r:<12} → {got!r}   （期望 {want!r}）")
    if got != want:
        failures.append(f"{seen!r} 解析成 {got!r}，应为 {want!r}")

# Nothing that was read correctly may be rewritten. KEEP is 86 lines lifted
# off real Dark Souls III screenshots.
rewritten = []
for line in KEEP:
    if line in terms:
        continue                    # an exact hit is not what this is testing
    got = tr._glossary_exact(line)
    if got is not None:
        rewritten.append((line, got))
print(f"\n  86 条真实游戏文本里被就近改写的: {len(rewritten)}")
for line, got in rewritten[:6]:
    print(f"    {line!r} → {got!r}")
if rewritten:
    failures.append(f"{len(rewritten)} 条读对的文本被就近改写了")

# Ambiguity must abstain rather than guess.
pair = Translator(TranslateConfig(backend="none",
                                  glossary={"武器": "武器", "武具": "防具"}))
print(f"  两个候选时: {pair._glossary_exact('武⼝')!r}（应为 None）")
if pair._glossary_exact("武⼝") is not None:
    failures.append("有两个候选时没有弃权")

print("\nRESULT:", "PASS - 认错一个字仍能命中，读对的不动，含糊的不猜"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
