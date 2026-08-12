"""Glossary suggestions out of a played session, for review -- never applied.

The glossary is the accumulated result of playing a game and correcting it,
and building it by hand means noticing a wrong name mid-game, alt-tabbing,
and typing it in. Every line of a session is already in the log with its
translation next to it, so the model can do the noticing afterwards: read
the pairs, pull out the proper nouns whose translation must stay consistent
-- character names, places, named skills -- and propose them as entries.

Proposals only. The output is a separate file the user reads and prunes
before wiring it into `glossary_file`; nothing here touches a live glossary,
because a wrong entry does not just miss -- it actively rewrites every line
it matches, forever, without a model in the loop to notice.

Runs after the session, not during it: a live caption has a latency budget
of a few hundred milliseconds and extraction has none at all, so the two
must never share a request.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .translate import TranslateConfig, build_backend

# The discipline here is anti-extraction. A glossary entry is a standing
# order that outlives every prompt, so a common noun that slips in ("魔法")
# pins one wording onto text where the model would have chosen better from
# context. Better to miss a real name -- it can be added by hand -- than to
# admit a term that silently rewrites lines it should not.
PROMPT = (
    "You are building a translation glossary for ONE video game, from lines "
    "already translated during play. From the source/translation pairs "
    "below, extract ONLY proper nouns whose translation must stay "
    "consistent across a playthrough: character names, place names, and the "
    "unique names of skills, items or organisations.\n"
    "Rules:\n"
    "- NO common nouns, UI labels, verbs or adjectives. 学校, 魔法, 剣 are "
    "not glossary terms; a named sword is.\n"
    "- Skip any term a generic engine would translate correctly anyway.\n"
    "- When in doubt, skip it. An empty object is a perfectly good answer.\n"
    "- Strip honorifics (さん, くん, ちゃん, 様) from names.\n"
    "- The source text came from OCR and may hold misread characters; skip "
    "anything that looks garbled.\n"
    "- For the translation, use the wording the pairs themselves used; if "
    "they disagree, pick the most frequent. Target language: {target}.\n"
    'Reply with ONE JSON object, {{"source term": "translation", ...}}, '
    "and nothing else."
)


def read_pairs(log_path: Path,
               glossary: Dict[str, str]) -> List[Tuple[str, str]]:
    """The session's translated lines, minus what could teach nothing.

    Failures carry no translation, glossary hits would teach the glossary
    its own entries back, and a repeated line (menus, barks) says nothing
    the first occurrence did not.
    """
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for raw in log_path.read_text(encoding="utf-8-sig").splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        src = " ".join((rec.get("src") or "").split())
        dst = (rec.get("dst") or "").strip()
        if not src or not dst or dst.startswith("[translation failed"):
            continue
        if src in glossary or src in seen:
            continue
        seen.add(src)
        pairs.append((src, dst))
    return pairs


def _parse_json_reply(reply: str) -> Dict[str, str]:
    """The object out of a reply that may be wearing a code fence."""
    match = re.search(r"\{.*\}", reply, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and k and v}


def extract_terms(cfg: TranslateConfig, pairs: List[Tuple[str, str]],
                  batch_size: int = 80,
                  progress: Optional[Callable[[str], None]] = None
                  ) -> Dict[str, str]:
    """Ask the configured LLM backend for glossary candidates.

    Each proposed term must literally appear in the batch's source text:
    a model asked to extract will sometimes invent -- normalise a spelling,
    complete a truncated name -- and an entry whose key never occurs on
    screen can only ever misfire.
    """
    backend = build_backend(cfg)
    chat = getattr(backend, "_chat", None) or getattr(backend, "_message", None)
    if chat is None:
        raise RuntimeError(
            f"术语提取需要 LLM 后端（openai / anthropic），"
            f"当前是 {cfg.backend!r}")
    # The instruction goes in the user turn, not only the system turn, for
    # the reason _instruction() documents in translate.py: a relay may drop
    # the system message or replace it with one of its own. Measured here --
    # sent as a system prompt alone, a real gateway returned "It looks like
    # you've shared a set of translation examples. How can I help?" and not
    # one term, because the only place the task was stated had been thrown
    # away. Restating it costs a few tokens per batch and survives that.
    system = PROMPT.format(target=cfg.target_lang)
    out: Dict[str, str] = {}
    total = (len(pairs) + batch_size - 1) // batch_size
    for i in range(0, len(pairs), batch_size):
        chunk = pairs[i:i + batch_size]
        if progress is not None:
            progress(f"批次 {i // batch_size + 1}/{total}（{len(chunk)} 行）")
        user = (system + "\n\nSOURCE / TRANSLATION PAIRS:\n"
                + "\n".join(f"{s}\n  => {d}" for s, d in chunk)
                + '\n\nNow reply with the JSON object only.')
        reply = chat(system, user, max_tokens=2000)
        haystack = "\n".join(s for s, _ in chunk)
        for term, translation in _parse_json_reply(reply).items():
            if len(term) >= 2 and term in haystack and term not in cfg.glossary:
                out[term] = translation
    return out
