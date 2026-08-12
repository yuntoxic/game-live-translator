"""Glossary files stack, and every shipped one is well formed.

The point of stacking is that the terms every Japanese game words the same
way live in one file nobody has to maintain per game, and a game only
supplies what it words differently. So the order has to be: common first,
game on top, and an inline entry in the config beating both.

    python tools/check_glossary.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glt import config as cfgmod                  # noqa: E402

failures = []
GLOSSARIES = sorted((ROOT / "glossaries").glob("*.json"))

for path in GLOSSARIES:
    raw = path.read_text(encoding="utf-8-sig")
    terms = {k: v for k, v in json.loads(raw).items() if not k.startswith("//")}
    # json.loads keeps the last of a repeated key silently, so count in the text.
    keys = re.findall(r'^\s*"([^"]+)"\s*:', raw, re.M)
    dupes = {k for k in keys if not k.startswith("//") and keys.count(k) > 1}
    blanks = [k for k, v in terms.items() if not k.strip() or not str(v).strip()]
    print(f"  {path.name:<28} {len(terms):>4} 条")
    if dupes:
        failures.append(f"{path.name}: 重复的词条 {sorted(dupes)}")
    if blanks:
        failures.append(f"{path.name}: 空的词条 {blanks}")

# Stacking, through the real config loader rather than a reimplementation.
common = ROOT / "glossaries" / "common-ja-zh.json"
game = ROOT / "glossaries" / "darksouls3-ja-zh.json"
tmp = ROOT / "_glossary_stack_check.json"
tmp.write_text(json.dumps({
    "translate": {
        "glossary_file": [f"glossaries/{common.name}", f"glossaries/{game.name}"],
        "glossary": {"ステータス": "内联优先"},
    },
}, ensure_ascii=False), encoding="utf-8")
try:
    merged = cfgmod.load(tmp).translate.glossary
finally:
    tmp.unlink()

common_terms = {k: v for k, v in
                json.loads(common.read_text(encoding="utf-8-sig")).items()
                if not k.startswith("//")}
game_terms = {k: v for k, v in
              json.loads(game.read_text(encoding="utf-8-sig")).items()
              if not k.startswith("//")}
overlap = sorted(set(common_terms) & set(game_terms))

print(f"\n  叠加后 {len(merged)} 条，两表重叠 {len(overlap)} 条")
if merged.get("ステータス") != "内联优先":
    failures.append("配置里内联写的词条没有压过文件")
for key in overlap:
    if key != "ステータス" and merged.get(key) != game_terms[key]:
        failures.append(f"重叠词 {key} 应取游戏表的 {game_terms[key]!r}，"
                        f"实际 {merged.get(key)!r}")
        break
missing = [k for k in common_terms if k not in merged]
if missing:
    failures.append(f"通用表有 {len(missing)} 条没被合进来，例如 {missing[:3]}")

# A single string has to keep working: every existing config uses one.
tmp.write_text(json.dumps(
    {"translate": {"glossary_file": f"glossaries/{game.name}"}},
    ensure_ascii=False), encoding="utf-8")
try:
    single = cfgmod.load(tmp).translate.glossary
finally:
    tmp.unlink()
if single != game_terms:
    failures.append("写一个字符串（旧写法）时结果不对")


# Saving must not fold the loaded files back in. It used to: the panel saves
# on exit, wrote the merged dict into `glossary`, and those 87 terms then
# outranked the files they came from -- editing a glossary changed nothing,
# and the last game's terms followed the config into the next game.
tmp.write_text(json.dumps({
    "translate": {"glossary_file": [f"glossaries/{common.name}"], "glossary": {}},
}, ensure_ascii=False), encoding="utf-8")
try:
    cfg = cfgmod.load(tmp)
    assert len(cfg.translate.glossary) == len(common_terms), "前提不成立"
    cfgmod.save(cfg, tmp)
    written = json.loads(tmp.read_text(encoding="utf-8-sig"))["translate"]
finally:
    tmp.unlink()

print(f"  存回去以后：内联 {len(written['glossary'])} 条，"
      f"glossary_file = {written['glossary_file']}")
if written["glossary"]:
    failures.append(f"保存把 {len(written['glossary'])} 条文件词条钉进了内联表")
if written["glossary_file"] != [f"glossaries/{common.name}"]:
    failures.append("保存把 glossary_file 改掉了")
# A key the file never had must not appear. `prompt` absent means "use the
# program's"; writing the default out freezes it and every later improvement
# stops reaching this config.
if "prompt" in written:
    failures.append("保存把默认提示词写进了配置，以后改代码就不生效了")

# A config written from scratch -- what `main.py pick` does -- must not be
# born with the default prompt baked in either.
fresh = ROOT / "_glossary_fresh_check.json"
if fresh.exists():
    fresh.unlink()
try:
    cfgmod.save(cfgmod.AppConfig(), fresh)
    born = json.loads(fresh.read_text(encoding="utf-8-sig"))["translate"]
finally:
    fresh.unlink(missing_ok=True)
print(f"  新建的配置里有没有 prompt: {'prompt' in born}")
if "prompt" in born:
    failures.append("新建配置就带上了默认提示词，从第一天起就冻住了")

# fallback_model has no widget in the panel, so a save must carry the file's
# value -- same rule as the glossary, one level down.
tmp.write_text(json.dumps({
    "translate": {"openai": {"model": "fast", "fallback_model": "willing"}},
}, ensure_ascii=False), encoding="utf-8")
try:
    cfg2 = cfgmod.load(tmp)
    cfg2.translate.openai["model"] = "panel-changed-this"
    cfg2.translate.openai.pop("fallback_model", None)   # panel never knew it
    cfgmod.save(cfg2, tmp)
    kept = json.loads(tmp.read_text(encoding="utf-8-sig"))["translate"]["openai"]
finally:
    tmp.unlink()
print(f"  面板存回后 openai: model={kept.get('model')!r} "
      f"fallback_model={kept.get('fallback_model')!r}")
if kept.get("fallback_model") != "willing":
    failures.append("保存把 fallback_model 抹掉了")
if kept.get("model") != "panel-changed-this":
    failures.append("保留 fallback_model 时把面板改的 model 也回滚了")

print("\nRESULT:", "PASS - 通用表在下，游戏表在上，配置内联最优先；存回去不污染"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
