"""Switching to a slower model must not mean losing half its answers.

Models on one endpoint measured five times apart on the same eighteen lines:
1.6s median against 7.8s, with a fixed 8s budget losing eight of eighteen on
the slow one and none on the fast one. The budget now follows the model --
grown on timeouts, decayed on comfortably fast answers -- so picking a model
is a quality choice, not a configuration project.

    python tools/check_timeout_budget.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.translate import (TranslateConfig, Translator,   # noqa: E402
                           _timeout_for)


class Timing:
    """A backend that is exactly as slow as told."""

    def __init__(self, tr, behaviour):
        self.tr = tr
        self.behaviour = iter(behaviour)

    def translate(self, text, context):           # noqa: ARG002
        kind = next(self.behaviour)
        if kind == "timeout":
            raise TimeoutError("timed out")
        return "译文"


cfg = TranslateConfig(backend="none", timeout_s=8.0, cache_size=1,
                      context_lines=0)
tr = Translator(cfg)
failures = []

print(f"  配置预算 {cfg.timeout_s:.0f}s，上限 {cfg.timeout_s * Translator._SCALE_MAX:.0f}s\n")

# A slow model: repeated timeouts must grow the budget past its latency.
tr.backend = Timing(tr, ["timeout"] * 4)
budgets = []
for i in range(4):
    tr.translate(f"行{i}", use_context=False)
    budgets.append(_timeout_for(cfg, 512))
print(f"  连续超时后的预算: {[f'{b:.1f}s' for b in budgets]}")
if budgets != sorted(budgets):
    failures.append("超时之后预算没有增长")
if budgets[-1] < 12.0:
    failures.append(f"四次超时后预算仍只有 {budgets[-1]:.1f}s，慢模型还是活不了")
if budgets[-1] > cfg.timeout_s * Translator._SCALE_MAX + 0.01:
    failures.append(f"预算超过了上限: {budgets[-1]:.1f}s")

# Fast answers pull it back down.
tr.backend = Timing(tr, ["ok"] * 30)
for i in range(30):
    tr.translate(f"快{i}", use_context=False)
recovered = _timeout_for(cfg, 512)
print(f"  换回快模型 30 句后: {recovered:.1f}s")
if recovered > cfg.timeout_s + 0.01:
    failures.append(f"快答案没有把预算收回来: {recovered:.1f}s")

# The scale must never reach the config file: it is runtime state.
from dataclasses import asdict                            # noqa: E402
if "timeout_scale" in asdict(cfg):
    failures.append("timeout_scale 混进了 dataclass 字段，会被写进配置")
print(f"  asdict(cfg) 里有 timeout_scale: {'timeout_scale' in asdict(cfg)}"
      f"（必须为 False，不能写盘）")

print("\nRESULT:", "PASS - 预算跟着模型走：慢了会放宽，快了会收回，不落盘"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
