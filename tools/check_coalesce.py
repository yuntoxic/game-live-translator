"""The coalesce hold has to earn the delay it costs.

It exists for games that reveal text a character at a time: the picture stops
moving between characters, the first read is half a sentence, and holding the
line lets a longer read replace it. It costs its full length on every caption,
and a game that shows a whole line at once never collects -- a quarter second
added to every line for nothing, on top of a translation that is already the
slowest part.

So it is on probation, and one catch reinstates it: half a sentence on screen
is a worse failure than a slow caption.

    python tools/check_coalesce.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.config import AppConfig, Region                   # noqa: E402
from glt.pipeline import Pipeline                          # noqa: E402

cfg = AppConfig()
cfg.translate.backend = "none"
cfg.ocr.coalesce_ms = 250
cfg.regions = [Region(name="box", role="dialogue", lang="ja", translate=True,
                      box=(0.0, 0.0, 1.0, 1.0))]
pipe = Pipeline(cfg)
rt = pipe.runtimes["box"]

failures = []
full, floor = cfg.ocr.coalesce_ms, Pipeline._COALESCE_FLOOR_MS

print(f"  完整等待 {full}ms，缩到 {floor}ms 之前先观察 "
      f"{Pipeline._COALESCE_PROBATION} 行")

# A game that shows whole lines: the hold never catches anything.
held = [pipe._hold_ms(rt)]
for _ in range(Pipeline._COALESCE_PROBATION + 2):
    rt.coalesce_missed += 1
    held.append(pipe._hold_ms(rt))
print(f"  整句显示的游戏：{held}")
if held[0] != full:
    failures.append(f"一开始就该等满 {full}ms，实际 {held[0]}")
if held[-1] != floor:
    failures.append(f"观察期过后该缩到 {floor}ms，实际 {held[-1]}")
if any(h != full for h in held[:Pipeline._COALESCE_PROBATION]):
    failures.append("观察期内就提前缩短了")

# One catch and it is reinstated: this is the failure that matters.
rt.coalesce_earned += 1
after = pipe._hold_ms(rt)
print(f"  抓到过一次半句话之后：{after}ms")
if after != full:
    failures.append(f"抓到过就该恢复成 {full}ms，实际 {after}")

# A region configured with no hold at all keeps none.
cfg2 = AppConfig()
cfg2.translate.backend = "none"
cfg2.ocr.coalesce_ms = 0
cfg2.regions = list(cfg.regions)
pipe2 = Pipeline(cfg2)
rt2 = pipe2.runtimes["box"]
rt2.coalesce_missed = 99
if pipe2._hold_ms(rt2) != 0:
    failures.append("配置成 0 的等待被改大了")
print(f"  配置成 0 的：{pipe2._hold_ms(rt2)}ms")

print("\nRESULT:", "PASS - 用不上的等待会自己缩掉，用得上的一次就恢复"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
