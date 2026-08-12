"""The vision request carries the OCR draft, and the glossary can hit again.

Vision mode used to send pixels INSTEAD of the OCR text, which had a hidden
cost: _glossary_block matches terms against the outgoing text, and with no
text going out, no glossary term could ever reach the model. Now the local
read rides along as a draft to be checked against the picture, which both
restores the glossary and turns the model's job from reading into
proofreading.

    python tools/check_vision_prompt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import glt.translate as tr_mod                            # noqa: E402
from glt.translate import TranslateConfig, Translator     # noqa: E402

captured = {}


def fake_post_json(url, payload, headers, timeout):
    captured["payload"] = payload
    return {"choices": [{"message": {"content": "翻译结果"}}]}


tr_mod._post_json = fake_post_json

cfg = TranslateConfig(backend="openai",
                      glossary={"ステータス": "状态", "装備": "装备"},
                      openai={"base_url": "http://x", "api_key": "k",
                              "api_key_env": "", "model": "m",
                              "max_tokens": 512})
tr = Translator(cfg)

PNG = b"\x89PNG_fake_bytes"
got = tr.translate_image("ステータス を開く", PNG, use_context=False)

parts = captured["payload"]["messages"][1]["content"]
user_text = next(p["text"] for p in parts if p["type"] == "text")
has_image = any(p["type"] == "image_url" for p in parts)

failures = []


def check(label, ok):
    print(f"  {'✓' if ok else '✗'} {label}")
    if not ok:
        failures.append(label)


check("reply comes back", got == "翻译结果")
check("image attached", has_image)
check("OCR draft in the prompt", "ステータス を開く" in user_text)
check("draft marked as fallible", "misread" in user_text)
check("glossary term matched off the draft", "ステータス = 状态" in user_text)
check("unrelated term stays home", "装備" not in user_text)

# The second call must be a cache hit: no request, same answer.
captured.clear()
again = tr.translate_image("ステータス を開く", PNG, use_context=False)
check("repeat is a cache hit", again == "翻译结果" and not captured)

if failures:
    print(f"\nFAIL: {len(failures)}")
    raise SystemExit(1)
print("\nall good")
