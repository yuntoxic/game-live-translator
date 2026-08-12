"""A screen holds two jobs, and they need not go to the same model.

Menus are the bulk of the traffic and want a short noun rendered with the
conventional game term; dialogue is the minority and wants context,
register, and a model willing to translate what the game actually says.
`translate.role_models` sends a role elsewhere by name, changing only the
model -- address, key and limits stay whatever the backend section says.

The cache stays shared on purpose, so this checks that too: the same line
seen in a menu and then in dialogue must not be paid for twice.

    python tools/check_role_models.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import glt.translate as tr_mod                            # noqa: E402
from glt.translate import TranslateConfig, Translator     # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


seen = []


def fake_post_json(url, payload, headers, timeout):
    seen.append(payload["model"])
    # Answer a numbered request in kind, so a batch resolves in one call
    # instead of falling back to per-item retries and muddying the count.
    content = payload["messages"][-1]["content"]
    text = (content if isinstance(content, str)
            else next(p["text"] for p in content if p["type"] == "text"))
    import re
    numbers = re.findall(r"^(\d+)\. ", text, re.M)
    if numbers:
        return {"choices": [{"message": {"content": "\n".join(
            f"{n}. 译文" for n in numbers)}}]}
    return {"choices": [{"message": {"content": "译文"}}]}


tr_mod._post_json = fake_post_json


def make(role_models=None, backend="openai"):
    cfg = TranslateConfig(
        backend=backend, role_models=role_models or {},
        openai={"base_url": "http://x", "api_key": "k", "api_key_env": "",
                "model": "strong", "max_tokens": 512})
    return Translator(cfg)


# -- a mapped role goes elsewhere, an unmapped one does not ---------------
tr = make({"info": "fast"})
seen.clear()
tr.translate("ステータス", role="info")
check("mapped role uses its own model", seen == ["fast"], detail=str(seen))

seen.clear()
tr.translate("待って。その先は危険だわ。", role="dialogue")
check("unmapped role keeps the main model", seen == ["strong"],
      detail=str(seen))

seen.clear()
tr.translate("何か", role="")
check("no role at all keeps the main model", seen == ["strong"],
      detail=str(seen))

# -- the batch and image paths honour it too ------------------------------
seen.clear()
tr2 = make({"info": "fast"})
tr2.translate_many(["装備", "持ち物"], role="info")
check("batch path uses the role's model", seen == ["fast"], detail=str(seen))

seen.clear()
tr3 = make({"dialogue": "eyes"})
tr3.translate_image("セリフ", b"\x89PNG", role="dialogue")
check("image path uses the role's model", seen == ["eyes"], detail=str(seen))

# -- the backend is built once and reused ---------------------------------
tr4 = make({"info": "fast"})
tr4.translate("A", role="info")
first = tr4._role_backends.get("info")
tr4.translate("B", role="info")
check("role backend built once", first is tr4._role_backends.get("info"))

# -- mapping to the model already in use builds nothing extra -------------
tr5 = make({"info": "strong"})
seen.clear()
tr5.translate("装備", role="info")
check("mapping to the same model reuses the default",
      seen == ["strong"] and not tr5._role_backends)

# -- one cache for every role: a menu label seen again in dialogue is free -
tr6 = make({"info": "fast"})
seen.clear()
tr6.translate("セーブ", role="info")
again = tr6.translate("セーブ", role="dialogue")
check("cache is shared across roles",
      again == "译文" and seen == ["fast"] and tr6.stats["cache_hits"] == 1,
      detail=str(seen))

# -- backends with no model of their own are left alone -------------------
tr7 = Translator(TranslateConfig(backend="none", role_models={"info": "fast"}))
check("model-less backend ignores role_models",
      tr7._backend_for("info") is tr7.backend)

# -- the default is exactly the old behaviour -----------------------------
tr8 = make()
seen.clear()
for role in ("info", "dialogue", "choice", "name"):
    tr8.translate(f"line-{role}", role=role)
check("empty mapping sends everything to the main model",
      seen == ["strong"] * 4, detail=str(seen))

if failures:
    print(f"\nFAIL: {len(failures)}")
    raise SystemExit(1)
print("\nall good")
