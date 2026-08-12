"""A HUD counter must not re-trigger translation on every tick.

An FPS readout, a timer, a soul count, an item quantity: each tick is a
different string, so a plain cache misses every time and the translator is
called once a second for the life of the session. This checks that a line
already translated is reused when only its numbers changed -- and that the
reuse is refused whenever swapping the numbers would not be safe.

    python tools/check_numeric.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.translate import (Translator, TranslateConfig,  # noqa: E402
                           numeric_key, respan_digits)

# Off one counter, one frame apart - OCR spaced them differently.
FPS_A, FPS_B = "DARK SOULS III / 40 FPS", "DARK SOULS III / 60FPS"

TICKS = [
    FPS_A, FPS_B, "DARK SOULS III / 59 FPS", "DARK SOULS III / 61FPS",
    "所持数 5 / 99", "所持数 6 / 99", "所持数 99 / 99",
    "残り時間 12:30", "残り時間 12:29",
    "レベル 655",
]
EXPECTED_CALLS = 4          # one per distinct shape, not per tick

KNOWN = {
    FPS_A: "黑暗之魂III / 40 帧/秒",
    "所持数 5 / 99": "持有数 5 / 99",
    "残り時間 12:30": "剩余时间 12:30",
    "レベル 655": "等级 655",
}


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list = []

    def translate(self, text: str, context: list) -> str:  # noqa: ARG002
        self.calls.append(text)
        return KNOWN.get(text, "译:" + text)


def main() -> int:
    failures = []

    if numeric_key(FPS_A) != numeric_key(FPS_B):
        failures.append("两种间距的同一计数器没有归一到同一形状")

    translator = Translator(TranslateConfig(backend="none", target_lang="zh-CN"))
    backend = RecordingBackend()
    translator.backend = backend

    outputs = [translator.translate(t, use_context=False) for t in TICKS]
    for tick, out in zip(TICKS, outputs):
        print(f"  {tick:<26} -> {out}")
        # Every number in the source must survive into the caption.
        for number in __import__("re").findall(r"\d+", tick):
            if number not in out:
                failures.append(f"数字 {number} 在译文里丢了: {tick} -> {out}")

    print(f"\n{len(TICKS)} 行输入，实际请求 {len(backend.calls)} 次 "
          f"(期望 {EXPECTED_CALLS})")
    if len(backend.calls) != EXPECTED_CALLS:
        failures.append(f"请求次数 {len(backend.calls)}，期望 {EXPECTED_CALLS}")

    # Reuse must be refused when swapping numbers would corrupt the caption.
    if respan_digits("剩余 十二 分", ["12", "30"], ["12", "29"]) is not None:
        failures.append("数字个数不符时仍然替换了")
    if respan_digits("持有数 五 / 99", ["5", "99"], ["6", "99"]) is not None:
        failures.append("译文数字与原文不符时仍然替换了")

    if failures:
        print("\nFAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("\nPASS - 计数器只翻一次，跳数复用且数字准确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
