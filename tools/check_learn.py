"""Glossary suggestions come only from what was actually on screen.

`learn` asks a model to pull proper nouns out of a session's log, and a
model asked to extract will sometimes invent -- normalise a spelling,
complete a truncated name, return prose around its JSON. Every failure mode
observed or expected is here: the hallucinated term is dropped because it
never occurred in the sources, the already-known term because the glossary
has it, the failed and repeated lines never reach the model at all, and a
code-fenced reply still parses.

    python tools/check_learn.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import glt.translate as tr_mod                            # noqa: E402
from glt.learn import (_parse_json_reply, extract_terms,  # noqa: E402
                       read_pairs)
from glt.translate import TranslateConfig                 # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# -- the log reader drops what could teach nothing -------------------------
LOG = [
    {"src": "アリスは魔法使いだ", "dst": "爱丽丝是魔法使"},
    {"src": "アリスは魔法使いだ", "dst": "爱丽丝是魔法使"},        # repeat
    {"src": "エクスカリバーを手に入れた", "dst": "获得了誓约胜利之剑"},
    {"src": "セーブ", "dst": "存档"},                              # in glossary
    {"src": "つづく", "dst": "[translation failed: timeout]"},     # failed
    {"src": "", "dst": "空的"},                                    # empty src
]
with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "session.jsonl"
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in LOG)
                   + "\nnot json at all\n", encoding="utf-8")
    pairs = read_pairs(log, {"セーブ": "存档"})

check("repeats, failures, empties and known lines dropped",
      [s for s, _ in pairs] == ["アリスは魔法使いだ",
                                "エクスカリバーを手に入れた"],
      detail=str([s for s, _ in pairs]))

# -- the reply parser survives fences and prose ----------------------------
check("bare object parses",
      _parse_json_reply('{"アリス": "爱丽丝"}') == {"アリス": "爱丽丝"})
check("fenced object parses",
      _parse_json_reply('```json\n{"アリス": "爱丽丝"}\n```')
      == {"アリス": "爱丽丝"})
check("prose around the object parses",
      _parse_json_reply('Here you go: {"アリス": "爱丽丝"} hope it helps')
      == {"アリス": "爱丽丝"})
check("garbage parses to nothing", _parse_json_reply("no json here") == {})
check("non-string values dropped",
      _parse_json_reply('{"アリス": ["爱丽丝"], "ボブ": "鲍勃"}')
      == {"ボブ": "鲍勃"})

# -- extraction: hallucinations and known terms never come back ------------
def fake_post_json(url, payload, headers, timeout):
    reply = json.dumps({
        "アリス": "爱丽丝",              # real: appears in the sources
        "エクスカリバー": "誓约胜利之剑",  # real
        "ボブ": "鲍勃",                  # hallucinated: never on screen
        "セーブ": "存档",                # already in the glossary
        "だ": "是",                      # too short to be a name
    }, ensure_ascii=False)
    return {"choices": [{"message": {"content": reply}}]}


tr_mod._post_json = fake_post_json
cfg = TranslateConfig(backend="openai", glossary={"セーブ": "存档"},
                      openai={"base_url": "http://x", "api_key": "k",
                              "api_key_env": "", "model": "m",
                              "max_tokens": 512})
terms = extract_terms(cfg, pairs)
check("real terms extracted",
      terms.get("アリス") == "爱丽丝"
      and terms.get("エクスカリバー") == "誓约胜利之剑")
check("hallucinated term dropped", "ボブ" not in terms)
check("glossary term not re-proposed", "セーブ" not in terms)
check("single characters dropped", "だ" not in terms)

# -- backends that cannot extract say so, loudly ---------------------------
try:
    extract_terms(TranslateConfig(backend="google"), pairs)
    check("google backend refused", False)
except RuntimeError as exc:
    check("google backend refused", "LLM" in str(exc))

if failures:
    print(f"\nFAIL: {len(failures)}")
    raise SystemExit(1)
print("\nall good")
