"""Compare models on game text, on your own endpoint.

Which model is best is not answerable in the abstract: it depends on what
your gateway actually serves, how far away it is, and what your game's text
looks like. This runs the same lines through several models and prints
latency next to output so the choice can be made on evidence.

    python tools/bench_models.py --models gpt-4o-mini,deepseek-chat

Lines are chosen to expose the failure modes that actually bite:

* a UI label that generic engines mistranslate as an ordinary noun;
* a line whose idiomatic reading differs from its literal one;
* a settings description, which is long, dry and easy to garble;
* dialogue with a dropped subject, where context decides the pronoun.

Every run costs real requests. The count is printed before anything is sent.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt import config as cfgmod          # noqa: E402
from glt.translate import Translator      # noqa: E402

# (source, what a good answer has to get right)
SAMPLES = [
    ("ステータス", "界面标签，应是「状态」而非「地位」"),
    ("持ち物", "界面标签，应是「道具/持有物」而非「财物」"),
    ("装備", "界面标签，应是「装备」而非「设备」"),
    ("待って。その先は危険だわ。", "「站住/等等」，不是打电话的「别挂」"),
    ("かがり火で休息しますか？", "「篝火」是专有概念，不是普通火堆"),
    ("彼女は黙って窓の外を見つめていた。", "省略主语的叙述句，要通顺"),
    ("ロックオン対象死亡時の、対象の自動切替を設定します",
     "设置项说明，长句要读得通"),
    ("両手持ちで攻撃力が上がる", "战斗术语，「双手持」「攻击力」"),
]

MENU_BATCH = ["ステータス", "持ち物", "装備", "素早さ", "初期化", "戻る"]


SCREEN_LINE = "待って。その先は危険だわ。"


def screen(base, candidates: list, keep: int) -> list:
    """One line through every model, to find the few worth measuring properly.

    An endpoint's catalogue is mostly irrelevant here - image models, embedding
    models, flagships too slow to caption with. Paying the full suite for each
    of them wastes both time and money, so spend one request apiece first and
    carry only the survivors forward.
    """
    print(f"筛选：{len(candidates)} 个模型各发 1 句，淘汰不可用和过慢的\n")
    results = []
    for name in candidates:
        section = dict(getattr(base, base.backend))
        section["model"] = name
        cfg = replace(base, cache_size=1, context_lines=0, glossary={},
                      timeout_s=min(base.timeout_s, 20.0),
                      **{base.backend: section})
        start = time.perf_counter()
        try:
            out = Translator(cfg).backend.translate(SCREEN_LINE, [])
            elapsed = (time.perf_counter() - start) * 1000
        except Exception as exc:  # noqa: BLE001 - an unusable model is a result
            reason = str(exc).splitlines()[0][:60]
            print(f"  --      {name:<34} {reason}")
            continue
        if not out.strip():
            print(f"  --      {name:<34} 返回空内容")
            continue
        results.append((elapsed, name, out.strip()))
        print(f"  {elapsed:6.0f}ms {name:<34} {out.strip()[:30]}")

    results.sort()
    picked = [name for _elapsed, name, _out in results[:keep]]
    print(f"\n{len(results)} 个可用，取最快的 {len(picked)} 个做完整对比："
          f"{', '.join(picked)}\n")
    return picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--models",
                    help="逗号分隔；填 all 表示全测（先筛后比）。省略则只列出"
                         "该端点提供的模型——中转站的模型名常和官方不同")
    ap.add_argument("--top", type=int, default=4,
                    help="全测时，筛选后取前几名做完整对比")
    ap.add_argument("--backend", default=None, help="覆盖 translate.backend")
    ap.add_argument("--base-url", default=None,
                    help="覆盖端点地址，省得依赖配置文件当前的状态")
    ap.add_argument("--repeat", type=int, default=1, help="每句重复几次取均值")
    args = ap.parse_args()

    base = cfgmod.load(args.config).translate
    if args.backend:
        base = replace(base, backend=args.backend)
    # Order matters: google and none are real backends that simply carry no
    # config section, so they must be answered before the has-a-section check
    # below, or they get reported as unknown.
    if base.backend in ("none", "google"):
        print(f"backend 是 {base.backend}，它不吃模型参数。"
              f"用 --backend openai 或 anthropic。", file=sys.stderr)
        return 1
    # Validate before any getattr on the backend name: an unrecognised value
    # reaching getattr raises an AttributeError whose message quotes it, and
    # the value that actually turned up was a credential typed into the wrong
    # prompt - so echo only a short prefix, never the whole thing.
    if not isinstance(getattr(base, base.backend, None), dict):
        shown = base.backend[:8] + ("…" if len(base.backend) > 8 else "")
        print(f"未知后端 {shown!r}。可选：openai、anthropic、deepl。",
              file=sys.stderr)
        return 1
    if args.base_url:
        section = dict(getattr(base, base.backend))
        section["base_url"] = args.base_url
        base = replace(base, **{base.backend: section})

    if not args.models:
        # Knowing which models exist is the first thing anyone needs here, and
        # a relay's names rarely match the upstream provider's.
        from glt.translate import list_models
        try:
            available = list_models(base)
        except Exception as exc:  # noqa: BLE001
            print(f"拉取模型列表失败：{exc}\n"
                  f"直接用 --models 手动指定要测的模型。", file=sys.stderr)
            return 1
        print(f"{base.backend} 端点提供 {len(available)} 个模型：\n")
        for name in available:
            print(f"  {name}")
        print(f"\n挑几个再跑：\n"
              f"  python tools/bench_models.py --backend {base.backend} "
              f"--models {','.join(available[:3])}")
        return 0

    if args.models.strip().lower() in ("all", "全部"):
        from glt.translate import list_models
        try:
            candidates = list_models(base)
        except Exception as exc:  # noqa: BLE001
            print(f"要全测就得先拿到模型列表，但拉取失败：{exc}", file=sys.stderr)
            return 1
        models = screen(base, candidates, args.top)
        if not models:
            print("没有模型通过筛选。", file=sys.stderr)
            return 1
    else:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    calls = len(models) * (len(SAMPLES) * args.repeat + 1)
    print(f"{len(models)} 个模型 x ({len(SAMPLES)} 句 x {args.repeat} 次 + 1 次批量)"
          f" = 约 {calls} 次请求\n")

    results = {}
    for model in models:
        section = dict(getattr(base, base.backend))
        section["model"] = model
        # Glossary off on purpose: an exact hit returns without calling the
        # model at all, so leaving it on would score the glossary and hide
        # exactly the differences this is meant to expose.
        cfg = replace(base, cache_size=1, context_lines=0, glossary={},
                      **{base.backend: section})
        print(f"=== {model} ===")
        rows, total = [], 0.0
        translator = Translator(cfg)
        for source, expectation in SAMPLES:
            best = None
            for _ in range(args.repeat):
                translator._cache.clear()
                start = time.perf_counter()
                out = translator.translate(source, use_context=False)
                elapsed = (time.perf_counter() - start) * 1000
                best = elapsed if best is None else min(best, elapsed)
            total += best
            rows.append((source, out, best, expectation))
            flag = "!" if out.startswith("[translation failed") else " "
            print(f" {flag}{best:6.0f}ms  {source}")
            print(f"          -> {out}")
        translator._cache.clear()
        start = time.perf_counter()
        batch = translator.translate_many(MENU_BATCH, use_context=False)
        batch_ms = (time.perf_counter() - start) * 1000
        print(f"  批量 {len(MENU_BATCH)} 项 {batch_ms:6.0f}ms  "
              f"-> {' / '.join(batch)}")
        print(f"  单句平均 {total / len(SAMPLES):.0f}ms   "
              f"调用 {translator.stats['calls']}  错误 {translator.stats['errors']}\n")
        results[model] = (total / len(SAMPLES), batch_ms, rows)

    print("=" * 72)
    print(f"{'模型':<28}{'单句均值':>10}{'批量6项':>12}")
    for model, (avg, batch_ms, _) in sorted(results.items(), key=lambda kv: kv[1][0]):
        print(f"{model:<28}{avg:>8.0f}ms{batch_ms:>10.0f}ms")
    print("\n延迟只是一半。逐句对照上面的输出，看哪个把界面标签和惯用语翻对了——"
          "\n实时字幕里,一个准确但慢 200ms 的模型,几乎总比又快又错的强。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
