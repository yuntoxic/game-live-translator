"""A line the primary model declines goes to the fallback, once.

Measured on one explicit line from a live game: the fastest model on the
endpoint explained why it would not translate while three others simply
translated it. Refusals used to be dropped silently, which from the outside
is indistinguishable from the tool being broken.

    python tools/check_fallback_model.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.translate import TranslateConfig, Translator     # noqa: E402

REFUSAL = ("I appreciate you reaching out, but I'm not able to help with "
           "this translation request.")


class Scripted:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    def translate(self, text, context):           # noqa: ARG002
        self.calls += 1
        return self.reply


failures = []


# Assemble a Translator without build_backend hitting the network: construct
# it with the harmless "none" backend, then swap in the scripted one and the
# config the fallback logic reads.
def make(primary, fallback_model=""):
    cfg = TranslateConfig(backend="openai", cache_size=1, context_lines=0)
    cfg.openai = dict(cfg.openai, model="fast",
                      fallback_model=fallback_model, api_key="k")
    cfg_none = TranslateConfig(backend="none", cache_size=1, context_lines=0)
    tr = Translator(cfg_none)
    tr.cfg = cfg
    tr.backend = primary
    return tr


# 1. Primary refuses, fallback translates: the line survives.
primary, backup = Scripted(REFUSAL), Scripted("站住，前面危险")
tr = make(primary, "willing")
tr._fallback = backup
got = tr.translate("「待って。その先は危険だわ。」", use_context=False)
print(f"  主模型拒绝 → 后备接住: {got!r}  fallbacks={tr.stats['fallbacks']}")
if got != "站住，前面危险" or tr.stats["fallbacks"] != 1:
    failures.append(f"后备没接住: {got!r}")

# 2. Both refuse: a visible failure marker, not silence.
tr2 = make(Scripted(REFUSAL), "willing")
tr2._fallback = Scripted(REFUSAL)
got2 = tr2.translate("「ほら」", use_context=False)
print(f"  两边都拒绝 → {got2[:42]!r}")
if not got2.startswith("[translation failed"):
    failures.append(f"双双拒绝时应给出可见的失败标记: {got2!r}")

# 3. No fallback configured: visible failure as well, and only one call.
tr3 = make(Scripted(REFUSAL), "")
got3 = tr3.translate("「ほら」", use_context=False)
print(f"  没配后备 → {got3[:42]!r}  calls={tr3.stats['calls']}")
if not got3.startswith("[translation failed"):
    failures.append("没配后备时也该有失败标记")
if tr3.stats["calls"] != 1:
    failures.append(f"没配后备却发了 {tr3.stats['calls']} 次请求")

# 4. Primary answers normally: the fallback is never consulted.
ok_primary, spy = Scripted("正常译文"), Scripted("不该被叫到")
tr4 = make(ok_primary, "willing")
tr4._fallback = spy
got4 = tr4.translate("ステータス画面", use_context=False)
print(f"  主模型正常 → {got4!r}  后备被调用 {spy.calls} 次")
if got4 != "正常译文" or spy.calls != 0:
    failures.append("主模型正常时后备不该被调用")

print("\nRESULT:", "PASS - 被拒的句子走后备，双拒可见，正常句不多花钱"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
