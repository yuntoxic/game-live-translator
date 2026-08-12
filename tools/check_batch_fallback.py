"""A group that fails must fall back on the right text, and not kill the thread.

The bug this pins down: unpacking a batch result into `indices, texts, got`
rebound `texts`, the enclosing function's full input list, to the ten strings
of whichever group was unpacked last. The per-item fallback then indexed that
short list with positions from the original one -- IndexError on a big screen,
and on a small one something worse, a caption translated from the wrong line.

It only bites when some group fails, so the successful runs looked fine.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.translate import Translator, TranslateConfig     # noqa: E402

# Deliberately digit-free. Numbered labels would all share one numeric key
# and the variant cache would answer nine of the ten fallbacks without a
# call, which is correct behaviour but hides what this test is measuring.
_KANA = "アイウエオカキクケコサシスセソタチツテトナニヌネノ"
SCREEN = [f"項目{k}" for k in _KANA[:25]]         # three groups of ten
FAILING_GROUP = 1                                 # the middle one


class FlakyBatch:
    """Batches, except for one group that always fails."""

    def __init__(self):
        self.groups = 0
        self.singles = []

    def translate(self, text, context):           # noqa: ARG002
        self.singles.append(text)
        events.append("请求")
        return "单:" + text

    def translate_batch(self, texts, context):    # noqa: ARG002
        index = self.groups
        self.groups += 1
        events.append("请求")
        if index == FAILING_GROUP:
            raise TimeoutError("timed out")
        return [f"{i + 1}. 批:{t}" for i, t in enumerate(texts)]


tr = Translator(TranslateConfig(backend="none", context_lines=0))
backend = FlakyBatch()
tr.backend = backend

progress = []                   # how much was on screen at each step
events = []                     # 请求 / 上屏, in the order they happened


def on_partial(partial):
    # The overlay draws from this, so it must only ever gain entries. A
    # snapshot that loses one would blink captions off mid-screen.
    progress.append(sum(1 for p in partial if p))
    events.append("上屏")


try:
    out = tr.translate_many(SCREEN, use_context=False, on_partial=on_partial)
except Exception as exc:                          # noqa: BLE001
    print(f"崩了: {type(exc).__name__}: {exc}")
    print("\nRESULT: FAIL - 一组失败就把整个翻译线程带走了")
    raise SystemExit(1)

# Groups are submitted concurrently, so which one fails is fixed by call
# order, not by position; find the fallback set from the batch that is missing.
fell_back = sorted(backend.singles)
print(f"批请求 {backend.groups} 次，逐条回退 {len(fell_back)} 条")
print(f"  回退的头尾: {fell_back[0]}  ...  {fell_back[-1]}")

covered = len(out) == len(SCREEN) and all(out)
# Every output must derive from its own input -- the corruption this catches
# is a translation attached to the wrong line, which still looks well-formed.
aligned = all(src in got for src, got in zip(SCREEN, out))

print(f"\n  条数完整: {covered}")
print(f"  每条译文对得上自己的原文: {aligned}")
if not aligned:
    for src, got in zip(SCREEN, out):
        if src not in got:
            print(f"    错位: {src!r} -> {got!r}")
            break


# Nothing may appear on screen only at the very end: a screenful takes about
# a minute of gateway time, and drawing it all at once means a minute of a
# menu with no captions on it.
incremental = len(progress) > 1 and progress == sorted(progress) \
    and progress[0] < len(SCREEN)
print(f"  边翻边画的进度: {progress}")

# Whatever the cache and the glossary already knew has to go up before the
# first request, not after it. A HUD over a moving scene re-OCRs every couple
# of seconds and is a cache hit every time; drawing it only once the network
# answered put a translation-long blank in the middle of unchanged text, and
# that is what the captions blinking looked like.
cache_first = bool(events) and events[0] == "上屏"
print(f"  先上屏再请求: {cache_first}  ({' → '.join(events[:4])} …)")

ok = covered and aligned and len(fell_back) == 10 and incremental and cache_first
print("\nRESULT:", "PASS - 失败的那组独自回退，其余各归各位，且逐组上屏"
      if ok else "FAIL")
raise SystemExit(0 if ok else 1)
