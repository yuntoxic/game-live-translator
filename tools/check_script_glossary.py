"""A whole game's script, used as a glossary, must be fast and must not call out.

MTool and similar tools pull every string an RPG Maker game holds, translate
them offline, and write a plain {japanese: chinese} dict -- the same shape
this project's glossaries already use. Pointed at one, a line the recogniser
reads correctly is answered from the file: no request, no latency, and the
same wording every time it appears.

That only works if the lookup scales. Folding every term for every line that
missed cost 99ms per line against 88,711 entries, six seconds for one screen.

    python tools/check_script_glossary.py [path-to-script.json]

With no argument it builds a synthetic script of the same size, so the check
runs anywhere; pass a real MTool file to exercise that.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glt.translate import TranslateConfig, Translator      # noqa: E402

SIZE = 88_711


def synthetic() -> dict:
    kana = "あいうえおかきくけこさしすせそたちつてとなにぬねの"
    out = {}
    for i in range(SIZE):
        a, b, c = kana[i % 25], kana[(i // 25) % 25], kana[(i // 625) % 25]
        out[f"{a}{b}{c}のセリフ{i}"] = f"第{i}句台词"
    return out


path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if path and path.exists():
    terms = {k: v for k, v in
             json.loads(path.read_text(encoding="utf-8-sig")).items()
             if isinstance(v, str) and k and v and not k.startswith("//")}
    print(f"  真实脚本 {path.name}: {len(terms):,} 条")
else:
    terms = synthetic()
    print(f"  合成脚本: {len(terms):,} 条")

failures = []


class NoNetwork:
    """Any call here means the glossary was bypassed."""

    def translate(self, text, context):               # noqa: ARG002
        raise AssertionError(f"不该发请求: {text!r}")

    def translate_batch(self, texts, context):        # noqa: ARG002
        raise AssertionError(f"不该发请求: {texts!r}")


tr = Translator(TranslateConfig(backend="none", glossary=terms))
tr.backend = NoNetwork()

lines = [k for k in list(terms)[:5000] if 4 <= len(k) <= 40][:40]
t0 = time.perf_counter()
out = tr.translate_many(lines, use_context=False)
hit_ms = (time.perf_counter() - t0) * 1000
print(f"  {len(lines)} 行全部来自脚本: {hit_ms:.0f}ms，请求 {tr.stats['calls']} 次，"
      f"术语命中 {tr.stats['glossary_hits']}")
if tr.stats["calls"]:
    failures.append("命中脚本的行仍然发了请求")
if any(out[i] != terms[lines[i]] for i in range(len(lines))):
    failures.append("译文和脚本里的不一致")

# The expensive path: lines that are not in the script at all.
tr2 = Translator(TranslateConfig(backend="none", glossary=terms))
tr2._folded_glossary()                    # pay the one-off build
misses = [line + "×" for line in lines]
t0 = time.perf_counter()
for line in misses:
    tr2._glossary_exact(line)
miss_ms = (time.perf_counter() - t0) * 1000
print(f"  {len(misses)} 行都不在脚本里: {miss_ms:.1f}ms 总计")
if miss_ms > 200:
    failures.append(f"没命中的查找太慢: {len(misses)} 行 {miss_ms:.0f}ms")


# --- a box answered from its lines -------------------------------------------
# The script stores one entry per line, because that is the unit the game
# displays. A caption covers a whole box, so the joined text matched nothing
# even when every line inside it was present.
pair = dict(list(terms.items())[:2])
(first, first_zh), (second, second_zh) = list(pair.items())
box = f"{first}\n{second}"

tr3 = Translator(TranslateConfig(backend="none", glossary=pair, cache_size=1))
tr3.backend = NoNetwork()
got = tr3.translate(box, use_context=False)
print(f"\n  两行的框: {box[:40]!r}".replace("\\n", " ⏎ "))
print(f"    -> {got[:56]!r}")
if got != first_zh + second_zh:
    failures.append(f"整框没能由各行拼出来: {got!r}")

# All or nothing: half a box translated and half left in Japanese is worse
# than sending the whole thing to the model.
half = TranslateConfig(backend="none", glossary={first: first_zh},
                       cache_size=1)
tr4 = Translator(half)
sent = []
tr4.backend = type("B", (), {
    "translate": lambda self, t, c: sent.append(t) or "模型译文",
    "translate_batch": lambda self, t, c: None})()
out4 = tr4.translate(box, use_context=False)
print(f"  只有一行在表里 → {'交给模型' if sent else '拼了半截'}")
if not sent:
    failures.append("只命中一半时不该自己拼，应整块交给模型")

print("\nRESULT:", "PASS - 整本剧本可当术语表：命中零请求，整框可由各行拼出"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
