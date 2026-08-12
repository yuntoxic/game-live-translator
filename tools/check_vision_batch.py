"""A menu batch can send its screenshot, and the numbering survives it.

Menus are where OCR misreads hurt most -- a one-character label has no
sentence around it to absorb the error -- and the per-line path used to be
text-only, so the model could only ever translate the misread. Now each
batch group carries the region's screenshot with every block's box drawn on
it under its number, and asks for its own numbers back. The numbers are the
whole contract: they tie a line to a patch of pixels on the way out and a
translation to a caption on the way back, so this checks they survive a
partial cache, group splitting, and a failed group's fallback.

    python tools/check_vision_batch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import glt.translate as tr_mod                            # noqa: E402
from glt.pipeline import _numbered_jpeg                   # noqa: E402
from glt.ocr import TextBlock                             # noqa: E402
from glt.translate import (TranslateConfig, Translator,   # noqa: E402
                           _parse_numbered_at)

failures = []


def check(label, ok, detail=""):
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


# -- the parser keeps on-image numbers, and gives up cleanly ---------------
check("parse keeps global numbers",
      _parse_numbered_at("3. 状态\n1. 道具\n12. 装备", [1, 3, 12])
      == ["道具", "状态", "装备"])
check("parse refuses a missing number",
      _parse_numbered_at("1. 道具\n3. 状态", [1, 3, 12]) is None)
check("parse ignores numbers nobody asked for",
      _parse_numbered_at("1. 道具\n2. 多余\n3. 状态", [1, 3])
      == ["道具", "状态"])

# -- the wire: image attached, subset numbering, cache respected -----------
requests = []


def fake_post_json(url, payload, headers, timeout):
    content = payload["messages"][-1]["content"]
    if isinstance(content, list):        # a vision request
        user_text = next(p["text"] for p in content if p["type"] == "text")
        requests.append(("image", user_text))
        # Answer exactly the numbers that were asked for.
        import re
        numbers = re.findall(r"^(\d+)\. ", user_text, re.M)
        reply = "\n".join(f"{n}. 译{n}" for n in numbers)
        return {"choices": [{"message": {"content": reply}}]}
    requests.append(("text", content))
    return {"choices": [{"message": {"content": "单条译文"}}]}


tr_mod._post_json = fake_post_json

cfg = TranslateConfig(backend="openai",
                      openai={"base_url": "http://x", "api_key": "k",
                              "api_key_env": "", "model": "m",
                              "max_tokens": 512})
tr = Translator(cfg)
tr._cache_put("持ち物", "已缓存的道具")     # box 2 is already known

built = []


def make_image():
    built.append(1)
    return b"jpg_bytes"


texts = ["ステータス", "持ち物", "装備", "セーブ"]
got = tr.translate_many(texts, image=make_image)

check("cached item kept, not re-sent", got[1] == "已缓存的道具")
check("pending items answered by their box numbers",
      got == ["译1", "已缓存的道具", "译3", "译4"], detail=str(got))
image_reqs = [u for kind, u in requests if kind == "image"]
check("one vision request went out", len(image_reqs) == 1)
check("lines numbered by box, not renumbered",
      image_reqs and "1. ステータス" in image_reqs[0]
      and "3. 装備" in image_reqs[0] and "4. セーブ" in image_reqs[0]
      and "2. 持ち物" not in image_reqs[0])
check("screenshot note included",
      image_reqs and "number's box" in image_reqs[0])
# Measured against a real title screen: told only to "translate what is
# written there", the model transcribed instead -- CONFIG came back as
# CONFIG. The note must forbid that in as many words, and must name the
# target language, which means it has to have been formatted.
check("transcription forbidden in as many words",
      image_reqs and "Never reply with the original text" in image_reqs[0])
check("target language substituted into the note",
      image_reqs and "{target}" not in image_reqs[0]
      and "in zh-CN" in image_reqs[0])
check("answers were cached", tr._cache_get("装備") == "译3")
check("screenshot built once, not per group", len(built) == 1,
      detail=f"{len(built)}x")

# Measured on the live title screen: 17 screenshots drawn for 3 sent,
# because a still menu re-OCRs constantly and answers from the cache.
built.clear()
requests.clear()
tr.translate_many(texts, image=make_image)
check("nothing to send, no screenshot built", not built and not requests)

# And the case the live run actually spends most of its time in: one new
# entry among cached ones. This used to skip batching entirely and take the
# text-only single path, losing the picture on exactly the line that needed
# it -- a misread つづきから came back transliterated.
built.clear()
requests.clear()
tr5 = Translator(cfg)
for known in ("ステータス", "持ち物", "装備"):
    tr5._cache_put(known, "已缓存")
lone = tr5.translate_many(texts, image=make_image)
check("a lone pending item still sends the picture",
      len(built) == 1 and [k for k, _ in requests] == ["image"],
      detail=str([k for k, _ in requests]))
check("lone item keeps its box number",
      requests and "4. セーブ" in requests[0][1] and lone[3] == "译4")

# -- a failed vision group falls back to per-item text calls ---------------
def broken_post_json(url, payload, headers, timeout):
    content = payload["messages"][-1]["content"]
    if isinstance(content, list):
        raise RuntimeError("vision endpoint down")
    requests.append(("text", content))
    return {"choices": [{"message": {"content": "文字兜底"}}]}


tr_mod._post_json = broken_post_json
tr2 = Translator(cfg)
got2 = tr2.translate_many(["ライフ", "マナ"], image=b"jpg_bytes")
check("vision failure falls back to text", got2 == ["文字兜底", "文字兜底"])

# -- without an image nothing changes shape --------------------------------
tr_mod._post_json = fake_post_json
requests.clear()
tr3 = Translator(cfg)
tr3.translate_many(["ライフ", "マナ"])
check("no image, plain text batch as before",
      requests and requests[0][0] == "text")

# -- the drawn screenshot: capped size, valid jpeg -------------------------
crop = np.zeros((400, 2000, 4), dtype=np.uint8)
blocks = [TextBlock("ステータス", 60, 40, 300, 80),
          TextBlock("持ち物", 60, 120, 300, 160)]
jpeg = _numbered_jpeg(crop, blocks, offset=(50, 30))
check("numbered screenshot is a jpeg", jpeg is not None
      and jpeg[:3] == b"\xff\xd8\xff")
if jpeg is not None:
    import cv2
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    check("width capped at 1280", decoded.shape[1] <= 1280,
          detail=f"{decoded.shape[1]}px")
    check("numbers drawn on the image", (decoded != 0).any())

if failures:
    print(f"\nFAIL: {len(failures)}")
    raise SystemExit(1)
print("\nall good")
