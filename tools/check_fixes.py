"""A word the model insists on getting wrong is swapped on its way out.

The glossary corrects the source side and needs the term recognised; when
the model itself keeps choosing a wrong word -- a character name it prefers
to spell its own way -- no prompt fixes it reliably. `translate.fixes` is
the output-side answer: plain replacements applied at display time, outside
the cache, so editing them corrects even lines the cache already holds.

    python tools/check_fixes.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glt import config as cfgmod                          # noqa: E402
from glt.translate import TranslateConfig, Translator     # noqa: E402

failures = []


def check(label, got, want):
    mark = "✓" if got == want else "✗"
    print(f"  {mark} {label}: {got!r}")
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


tr = Translator(TranslateConfig(backend="none",
                                fixes={"小明": "阿光", "地位": "状态"}))

check("plain swap", tr.fix("小明拿到了道具"), "阿光拿到了道具")
check("several per line", tr.fix("小明的地位"), "阿光的状态")
check("no hit passes through", tr.fix("今天天气不错"), "今天天气不错")
check("empty passes through", tr.fix(""), "")
check("failure notice untouched",
      tr.fix("[translation failed: timeout 小明]"),
      "[translation failed: timeout 小明]")

# fixes_file merges exactly like glossary_file: later files win, inline wins
# over any file, and a save never writes the merged dict back.
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp / "a.json").write_text(json.dumps(
        {"地位": "状态", "小明": "小光", "//备注": "跳过"},
        ensure_ascii=False), encoding="utf-8")
    (tmp / "b.json").write_text(json.dumps(
        {"小明": "阿光"}, ensure_ascii=False), encoding="utf-8")
    (tmp / "config.json").write_text(json.dumps({
        "translate": {"backend": "none",
                      "fixes_file": ["a.json", "b.json"],
                      "fixes": {"设备": "装备"}},
    }), encoding="utf-8")
    cfg = cfgmod.load(tmp / "config.json")
    check("later file wins", cfg.translate.fixes.get("小明"), "阿光")
    check("earlier file kept", cfg.translate.fixes.get("地位"), "状态")
    check("inline wins over files", cfg.translate.fixes.get("设备"), "装备")
    check("comment keys skipped", "//备注" in cfg.translate.fixes, False)

    cfgmod.save(cfg, tmp / "config.json")
    written = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    check("save keeps fixes_file",
          written["translate"].get("fixes_file"), ["a.json", "b.json"])
    check("save keeps inline fixes only",
          written["translate"].get("fixes"), {"设备": "装备"})

if failures:
    print(f"\nFAIL: {len(failures)}")
    raise SystemExit(1)
print("\nall good")
