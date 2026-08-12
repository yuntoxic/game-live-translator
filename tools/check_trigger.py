"""The trigger must fire on a picture that never stops moving.

A game hub -- fire effects, walking NPCs, swaying foliage -- is never still
for stable_ms, and a full-screen region over one produced no captions at all:
the motion branch returned before max_hold_ms was ever consulted, so the
escape hatch written for exactly this case could not be reached.

    python tools/check_trigger.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.trigger import RegionTrigger, TriggerConfig     # noqa: E402

W, H = 320, 120
rng = np.random.default_rng(7)


def scene(busy: float, seed_text: bool = True) -> np.ndarray:
    """A frame with static text on it and `busy` fraction of moving pixels."""
    img = np.zeros((H, W, 4), np.uint8)
    if seed_text:
        img[20:34, 10:300] = 220          # a HUD line that never moves
        img[50:64, 10:200] = 220
    noise = rng.random((H, W)) < busy
    img[noise] = rng.integers(0, 255, (int(noise.sum()), 4), dtype=np.uint8)
    return img


def run(cfg: TriggerConfig, frames: int, busy: float, fps: int = 20):
    trig = RegionTrigger(cfg)
    fires, t = [], 0.0
    for _ in range(frames):
        if trig.feed(scene(busy), now=t):
            fires.append(t)
        t += 1.0 / fps
    return fires, t


cfg = TriggerConfig()
failures = []

# 10 seconds of a scene that never holds still for a moment.
fires, elapsed = run(cfg, frames=200, busy=0.25)
gap = cfg.max_hold_ms / 1000.0
print(f"一直在动的画面 {elapsed:.0f}s：触发 {len(fires)} 次"
      f"  时刻 {[round(f, 1) for f in fires[:6]]}")
if not fires:
    failures.append("画面一直动就一次都不触发 —— max_hold_ms 兜不住")
elif fires[0] > gap * 1.5:
    failures.append(f"第一次触发等了 {fires[0]:.1f}s，应在 {gap:.1f}s 左右")
elif len(fires) > elapsed / (gap * 0.5):
    failures.append(f"触发太频繁：{len(fires)} 次 / {elapsed:.0f}s")

# A still picture must still behave: one fire, then quiet.
still = RegionTrigger(TriggerConfig())
frame, t, still_fires = scene(0.0), 0.0, 0
for _ in range(120):
    if still.feed(frame, now=t):
        still_fires += 1
    t += 0.05
print(f"完全静止的画面 6s：触发 {still_fires} 次")
if still_fires != 1:
    failures.append(f"静止画面应只触发 1 次，实际 {still_fires} 次")

print("\nRESULT:", "PASS - 动的画面兜得住，静的画面不重复"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
