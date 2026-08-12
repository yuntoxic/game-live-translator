"""Vision mode reads pixels but keeps every text-keyed shortcut.

The point of keying the image call on the OCR text: a repeated line -- a
backlog page, a re-entered room -- must never pay for the image twice, and a
glossary or full-script hit must answer before any pixels are sent. Even a
misread key works, because the same picture misreads the same way.

    python tools/check_vision.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.translate import TranslateConfig, Translator     # noqa: E402

PNG = b"\x89PNG fake bytes"


class VisionBackend:
    def __init__(self):
        self.image_calls, self.text_calls = 0, 0
        self.drafts = []

    # `ocr_text` is the local read, handed over as a draft for the model to
    # check against the pixels. It is not optional here on purpose: the
    # Translator must always pass it, because a vision call without it also
    # loses the glossary, which matches nothing against an empty string.
    def translate_image(self, png, context, ocr_text):   # noqa: ARG002
        self.image_calls += 1
        self.drafts.append(ocr_text)
        return "看图翻出来的"

    def translate(self, text, context):           # noqa: ARG002
        self.text_calls += 1
        return "按文字翻出来的"


def make(glossary=None):
    tr = Translator(TranslateConfig(backend="none", cache_size=100,
                                    context_lines=0,
                                    glossary=glossary or {}))
    tr.backend = VisionBackend()
    return tr


failures = []

# First sight pays for the image; the repeat is answered from the cache.
tr = make()
first = tr.translate_image("「待って。その先は危険だわ。」", PNG)
again = tr.translate_image("「待って。その先は危険だわ。」", PNG)
print(f"  首次: {first!r}（图片调用 {tr.backend.image_calls}）   "
      f"重复: {again!r}（图片调用 {tr.backend.image_calls}）")
if first != "看图翻出来的" or tr.backend.image_calls != 1:
    failures.append("首次没有走图片，或重复又付了一次图片")
if again != "看图翻出来的" or tr.stats["cache_hits"] != 1:
    failures.append("重复的行没吃到缓存")
print(f"  随图带上的草稿: {tr.backend.drafts!r}")
if tr.backend.drafts != ["「待って。その先は危険だわ。」"]:
    failures.append(f"OCR 草稿没有随图片一起发出去: {tr.backend.drafts!r}")

# A glossary hit answers before any pixels are sent.
tr2 = make(glossary={"セーブ": "保存"})
got = tr2.translate_image("セーブ", PNG)
print(f"  术语命中: {got!r}（图片调用 {tr2.backend.image_calls}）")
if got != "保存" or tr2.backend.image_calls != 0:
    failures.append("术语命中还发了图片")

# A backend without vision falls back to the text path, not a crash.
tr3 = Translator(TranslateConfig(backend="none", cache_size=1,
                                 context_lines=0))

class TextOnly:
    def translate(self, text, context):           # noqa: ARG002
        return "纯文字后端"

tr3.backend = TextOnly()
got3 = tr3.translate_image("何か", PNG)
print(f"  后端不支持图片: {got3!r}")
if got3 != "纯文字后端":
    failures.append(f"不支持图片的后端没有退回文字路径: {got3!r}")

print("\nRESULT:", "PASS - 图片只在缓存和术语表都没答案时才发，重复不重付"
      if not failures else "FAIL\n  " + "\n  ".join(failures))
raise SystemExit(1 if failures else 0)
