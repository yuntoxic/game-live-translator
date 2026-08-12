"""Translation backends, with the bits that matter for live game text.

Three things separate this from calling an API in a loop:

* **Latest-wins.** During a fast scene the OCR stage can outrun the network.
  Queueing every line would put the overlay minutes behind the picture, so a
  pending line for a region is simply replaced by the newer one.
* **Rolling context.** Game dialogue is full of dropped subjects and pronouns.
  LLM backends get the last few lines so "she" and "it" resolve to the right
  thing and proper nouns stay spelled the same way.
* **Cache.** Menus, item names and repeated barks come back constantly; a hit
  costs nothing and shows up instantly.
"""

from __future__ import annotations

import json
import os
import re
import base64
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Protocol, Union

DEFAULT_PROMPT = (
    "You are translating live on-screen text from a video game into {target}.\n"
    "Rules:\n"
    "- Reply with the translation ONLY. No notes, no romanisation, no quotes,"
    " no explanations.\n"
    "- The text is either dialogue or a UI label: a menu entry, stat, item,"
    " skill or button. For UI labels use the term this concept conventionally"
    " has in games in {target}, not a literal word-by-word rendering. A short"
    " noun on its own is almost always a UI label, not a sentence.\n"
    "- Obey the GLOSSARY exactly when one is given.\n"
    "- Keep proper nouns consistent with the CONTEXT lines.\n"
    "- Preserve the register of the original (archaic, casual, formal).\n"
    "- Fragments stay fragments. Never invent content that is not there.\n"
    "- The text came from OCR and may hold a misread character. Translate what"
    " the line evidently means in a game; do not force a reading that makes no"
    " sense there.\n"
    "- If the text is already in {target}, return it unchanged."
)


@dataclass
class TranslateConfig:
    backend: str = "google"          # google | openai | anthropic | deepl | none
    target_lang: str = "zh-CN"
    source_lang: str = "auto"
    context_lines: int = 4
    cache_size: int = 2000
    timeout_s: float = 8.0           # budget for one line; batches scale it
    # Most items per request. A game screen can yield forty text blocks at
    # once, and putting them all in one request makes a single slow response
    # that times out and loses the whole screen. Small batches run concurrently
    # and each one either lands or fails on its own.
    batch_size: int = 10
    # Send the dialogue region's pixels to the model instead of local OCR
    # text. A vision model reads what a 2015-era local recogniser cannot:
    # measured on one stylised dialog over a brick wall, local OCR produced
    # misreads and wall debris while two vision models returned a perfect
    # translation, hearts included, for about one second more than their
    # text-mode latency. Costs image tokens per line; menus and stat panels
    # keep local OCR, whose per-line boxes vision cannot give.
    vision: bool = False
    prompt: str = DEFAULT_PROMPT
    # Terms this game renders a particular way. Only entries that actually
    # occur in the current text are sent, so a long glossary costs nothing.
    glossary: Dict[str, str] = field(default_factory=dict)
    # One shareable glossary file, or a list of them. Files are merged in
    # order with later ones winning, and all of them sit under `glossary`, so
    # an inline entry always wins over any file.
    glossary_file: Union[str, List[str]] = ""
    # Plain replacements applied to the TRANSLATION before it is shown, for a
    # word the model keeps getting wrong no matter how it is prompted. The
    # glossary works on the source side and needs the term to be recognised;
    # this works on the output side and needs nothing -- the model answers
    # however it likes and the wrong word is swapped on its way to the screen.
    fixes: Dict[str, str] = field(default_factory=dict)
    fixes_file: Union[str, List[str]] = ""
    # Model per region role, overriding the backend's own `model`. A screen
    # holds two different jobs: dialogue wants context, register and a model
    # willing to translate what the game actually says, while a menu wants a
    # short noun rendered with the conventional term -- and menus are the
    # bulk of the traffic. Sending both to one model means either paying the
    # strong model's latency on every UI label or accepting its price on the
    # lines that need it. Empty (the default) sends everything to `model`,
    # which is exactly the behaviour this had before.
    role_models: Dict[str, str] = field(default_factory=dict)
    openai: Dict = field(default_factory=lambda: {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "api_key": "",
        "model": "gpt-4o-mini",
        # Tried once, only for a line the main model declined to translate.
        # Models differ sharply on content they treat as sensitive: measured
        # on one such line, the fastest model refused it while three others
        # translated it, so the fast one stays primary and the willing one
        # catches its refusals.
        "fallback_model": "",
        "max_tokens": 512,
    })
    anthropic: Dict = field(default_factory=lambda: {
        "base_url": "https://api.anthropic.com/v1",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key": "",
        "model": "claude-sonnet-5",
        "max_tokens": 512,
    })
    deepl: Dict = field(default_factory=lambda: {
        "api_key_env": "DEEPL_API_KEY",
        "api_key": "",
        "free_tier": True,
    })


_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")


def numeric_key(text: str) -> str:
    """Collapse a line to its shape, ignoring the numbers and the spacing.

    A game HUD is full of text that only ever changes by a number: an FPS
    counter, a timer, a soul count, an item quantity. Each tick is a different
    string, so a plain cache misses every time and the translator is called
    once a second forever. Whitespace goes too, because OCR is inconsistent
    about it -- "40 FPS" and "60FPS" came off the same counter one frame apart.
    """
    return _DIGITS.sub("#", _SPACE.sub("", text))


def respan_digits(translation: str, old: List[str], new: List[str]) -> Optional[str]:
    """Reuse a translation for the same line with different numbers.

    Only when the translation carries exactly the source's numbers, in order:
    then swapping them is safe and the caption stays accurate without another
    request. Any other shape -- a reformatted number, a spelled-out one, a
    different count -- returns None and the caller translates properly.
    """
    if len(old) != len(new) or _DIGITS.findall(translation) != old:
        return None
    replacements = iter(new)
    return _DIGITS.sub(lambda _m: next(replacements), translation)


def _resolve_key(section: Dict) -> str:
    """Prefer the environment variable so config files stay safe to commit."""
    env_name = section.get("api_key_env") or ""
    return (os.environ.get(env_name, "") or section.get("api_key", "")).strip()


class ApiError(RuntimeError):
    """An HTTP failure that carries what the server actually said.

    urllib raises HTTPError with the status only, and the response body is
    discarded when the exception propagates. That body is where every useful
    detail lives -- which upstream a gateway could not reach, that a model id
    is unknown, that a key lacks access to it. Without it a 502 says nothing
    at all beyond "something on the far side broke".
    """

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = (body or "").strip()
        detail = self._extract(self.body)
        super().__init__(f"HTTP {status} from {url}"
                         + (f"\n{detail}" if detail else ""))

    @staticmethod
    def _extract(body: str) -> str:
        """Pull the message out of the usual error envelope, else show raw."""
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001 - html error pages are normal here
            return body[:400]
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
            if isinstance(error, str):
                return error
            for field in ("message", "detail", "msg"):
                if data.get(field):
                    return str(data[field])
        return body[:400]


def _request(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        raise ApiError(exc.code, req.full_url, body) from None


# Characters the recogniser genuinely confuses, folded to one representative
# so a glossary term still matches a line it misread. Each group is a set of
# near-identical glyphs -- the kanji/katakana homoglyphs every Japanese OCR
# mixes up. 力 and カ are the famous pair: 防御力 read as 防御カ is why a stat
# line once came back as "national defence".
_HOMOGLYPHS = ("力カ", "口ロ", "日目", "一ー－ｰ", "二ニ", "八ハ", "工エ",
               "才オ", "千チ", "卜ト", "厶ム", "夕タ", "へヘ", "小ぃ",
               "大犬", "土士", "未末", "干千")
_FOLD_MAP = {ord(ch): group[0] for group in _HOMOGLYPHS for ch in group}
# Combining dakuten and handakuten. OCR drops them, or reads one as a comma:
# セーブ came back as セーフ、 at every setting tried.
_FOLD_MAP[0x3099] = None
_FOLD_MAP[0x309A] = None


def _ocr_fold(text: str) -> str:
    """Normalise away the differences a recogniser invents, and nothing else."""
    return unicodedata.normalize("NFD", text).translate(_FOLD_MAP)


_CJK_ANY = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힣]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")


def is_wrong_language(reply: str, target: str) -> bool:
    """True when the reply is not written in the target language at all.

    A model that declines says so in English -- "I appreciate you reaching
    out, but I'm not able to help with this translation" -- and that is not a
    translation of anything, but it is shorter than its source, so the length
    rule below never sees it. Asking whether a Chinese translation contains
    any Chinese catches it, and catches an answer that stayed in English for
    any other reason too.

    The letter floor is what keeps real text: a stat line translates to
    "HP MP Lv 5,000" and a skill can be called Mission. Twelve Latin letters
    with no CJK at all is a sentence in the wrong language.
    """
    if not target.lower().startswith(("zh", "ja", "ko")):
        return False
    if len(_LATIN_LETTER.findall(reply)) < 12:
        return False
    return not _CJK_ANY.search(reply)


def is_commentary(source: str, reply: str) -> bool:
    """True when the model answered *about* the line instead of translating it.

    Every prompt here says to reply with the translation only, and models
    mostly obey -- except on a line they find dubious, where they explain
    themselves instead. A two-character UI label came back as a hundred and
    fifty characters starting "I cannot provide a translation because ...",
    and the overlay painted the apology straight across the game.

    Length decides it, which needs no phrase list to keep current and works
    whatever language the refusal is written in. Japanese to Chinese
    contracts if anything, so four times the source is already absurd; the
    floor keeps two-character labels from tripping on a normal answer.
    """
    return len(reply) > 40 and len(reply) > 4 * len(source)


def _timeout_for(cfg: "TranslateConfig", max_tokens: int) -> float:
    """Scale the wait with the size of the answer being asked for.

    `timeout_s` is a budget for one line. A batch of ten asks for ten times
    the output and measured about six times the latency -- 7s typical, 11s at
    the tail, against a flat 8s budget, so roughly half of every screen's
    groups timed out. That is the expensive failure: a timed-out group costs
    the full wait AND ten single retries, which is more traffic than batching
    saved, and it is where a twenty-second screen came from.

    `timeout_scale` is how the budget follows the model. Models on one
    endpoint measured five times apart on the same lines -- 1.6s median
    against 7.8s -- and a fixed budget tuned to the fast one loses half of
    everything the slow one answers. The Translator raises the scale on each
    timeout and lets it decay on comfortably fast answers, so switching
    models needs no configuration: a slow model earns a longer leash within
    a few lines, and a fast one pulls it back.
    """
    scale = getattr(cfg, "timeout_scale", 1.0)
    return cfg.timeout_s * max(1.0, max_tokens / 512.0) * scale


def _post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    return _request(req, timeout)


_NUMBERED = re.compile(r"^\s*(\d+)\s*[.)、:]\s*(.*)$")

BATCH_RULES = (
    "You will be given numbered lines read off one screen of a game. Read all "
    "of them before translating any.\n"
    "They share a context: a line is often the description belonging to the "
    "name above it. Resolve a pronoun or a dropped subject from the "
    "surrounding lines rather than rendering it literally, and use one "
    "wording for a term everywhere it appears on the screen.\n"
    "Do not merge them. Reply with exactly the same numbering, one line each, "
    "nothing else, and keep each answer to the content of its own line.\n"
)


def _instruction(cfg: "TranslateConfig") -> str:
    """Repeat the essentials in the user turn, not only the system prompt.

    A relay's OpenAI-compatible layer may drop the system message when it
    forwards to another provider -- or replace it with one of its own. Both
    were observed: models answered in English with commentary because the only
    place the target language appeared had been discarded. Restating it here
    costs a few tokens and survives that.
    """
    return (f"Translate into {cfg.target_lang}. "
            f"Reply with the translation only — no notes, no romanisation, "
            f"no quotes, no markdown.\n\n")


def _glossary_block(glossary: Dict[str, str], texts: List[str]) -> str:
    """The glossary entries that the current text actually contains.

    Sending the whole table on every line would grow the prompt without
    helping; a game glossary can run to hundreds of terms.
    """
    if not glossary:
        return ""
    haystack = "\n".join(texts)
    hits = [(src, dst) for src, dst in glossary.items() if src and src in haystack]
    if not hits:
        return ""
    return ("GLOSSARY (use these exact translations):\n"
            + "\n".join(f"{src} = {dst}" for src, dst in hits) + "\n\n")


def _batch_prompt(texts: List[str]) -> str:
    return "\n".join(f"{i + 1}. " + " ".join(t.splitlines())
                     for i, t in enumerate(texts))


def _parse_numbered(reply: str, count: int) -> Optional[List[str]]:
    """Map a numbered reply back onto the inputs, or give up cleanly.

    Returning None rather than a best guess matters: a mis-aligned batch would
    silently caption every item with its neighbour's translation, which is
    worse than paying for one request per item.
    """
    found: Dict[int, str] = {}
    for line in reply.splitlines():
        match = _NUMBERED.match(line)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < count:
                found[index] = match.group(2).strip()
    if len(found) != count:
        return None
    return [found[i] for i in range(count)]


def _parse_numbered_at(reply: str, numbers: List[int]) -> Optional[List[str]]:
    """Like _parse_numbered, for lines that keep their on-image numbers.

    A vision batch numbers its lines by their box on the screenshot, so a
    group holding boxes 12, 15 and 18 asks for exactly those numbers back --
    renumbering them 1..3 would break the pairing with the drawn boxes,
    which is the whole point of drawing them.
    """
    wanted = set(numbers)
    found: Dict[int, str] = {}
    for line in reply.splitlines():
        match = _NUMBERED.match(line)
        if match:
            number = int(match.group(1))
            if number in wanted:
                found[number] = match.group(2).strip()
    if set(found) != wanted:
        return None
    return [found[n] for n in numbers]


class Backend(Protocol):
    def translate(self, text: str, context: List[str]) -> str: ...


class NullBackend:
    """Passthrough. Useful for tuning OCR without spending API calls."""

    def translate(self, text: str, context: List[str]) -> str:  # noqa: ARG002
        return text


class GoogleWebBackend:
    """Keyless endpoint behind the Google Translate web UI.

    Zero setup, which makes it a good first run, but it is undocumented and
    rate-limits under load. For actual play use an LLM backend: game dialogue
    without context translates badly no matter how good the engine is.
    """

    URL = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, cfg: TranslateConfig) -> None:
        self.cfg = cfg

    def translate(self, text: str, context: List[str]) -> str:  # noqa: ARG002
        params = urllib.parse.urlencode({
            "client": "gtx",
            "sl": self.cfg.source_lang or "auto",
            "tl": self.cfg.target_lang,
            "dt": "t",
            "q": text,
        })
        req = urllib.request.Request(
            f"{self.URL}?{params}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(chunk[0] for chunk in data[0] if chunk and chunk[0]).strip()


class DeepLBackend:
    def __init__(self, cfg: TranslateConfig) -> None:
        self.cfg = cfg
        self.key = _resolve_key(cfg.deepl)
        if not self.key:
            raise RuntimeError("DeepL backend needs an API key (DEEPL_API_KEY)")
        host = "api-free.deepl.com" if cfg.deepl.get("free_tier", True) else "api.deepl.com"
        self.url = f"https://{host}/v2/translate"

    def translate(self, text: str, context: List[str]) -> str:
        return self.translate_batch([text], context)[0]

    def translate_batch(self, texts: List[str], context: List[str]) -> List[str]:
        payload = {"text": list(texts),
                   "target_lang": self.cfg.target_lang.upper().replace("-CN", "")}
        if context:
            payload["context"] = "\n".join(context)
        if self.cfg.source_lang and self.cfg.source_lang != "auto":
            payload["source_lang"] = self.cfg.source_lang.upper()
        data = _post_json(self.url, payload,
                          {"Authorization": f"DeepL-Auth-Key {self.key}"},
                          self.cfg.timeout_s)
        return [item["text"].strip() for item in data["translations"]]


class OpenAIBackend:
    """Any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, cfg: TranslateConfig) -> None:
        self.cfg = cfg
        self.sec = cfg.openai
        self.key = _resolve_key(self.sec)
        if not self.key:
            raise RuntimeError(
                f"OpenAI backend needs a key in ${self.sec.get('api_key_env')}")
        self.url = self.sec["base_url"].rstrip("/") + "/chat/completions"

    def translate(self, text: str, context: List[str]) -> str:
        system = self.cfg.prompt.format(target=self.cfg.target_lang)
        user = _instruction(self.cfg) + _glossary_block(self.cfg.glossary, [text])
        if context:
            user += ("CONTEXT (already translated, for reference only):\n"
                     + "\n".join(context) + "\n\n")
        user += "TRANSLATE THIS LINE:\n" + text
        return self._chat(system, user)

    def _chat(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        tokens = max_tokens or self.sec.get("max_tokens", 512)
        data = _post_json(self.url, {
            "model": self.sec["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": tokens,
            "temperature": 0.2,
        }, {"Authorization": f"Bearer {self.key}"}, _timeout_for(self.cfg, tokens))
        return data["choices"][0]["message"]["content"].strip()

    def translate_batch(self, texts: List[str], context: List[str]) -> Optional[List[str]]:
        system = self.cfg.prompt.format(target=self.cfg.target_lang) + "\n" + BATCH_RULES
        user = _instruction(self.cfg) + BATCH_RULES + "\n"
        user += _glossary_block(self.cfg.glossary, texts)
        if context:
            user += ("CONTEXT (already translated, for reference only):\n"
                     + "\n".join(context) + "\n\n")
        user += _batch_prompt(texts)
        reply = self._chat(system, user, max_tokens=256 + 64 * len(texts))
        return _parse_numbered(reply, len(texts))

    def translate_image(self, png: bytes, context: List[str],
                        ocr_text: str = "") -> str:
        """Read and translate the region's pixels in one call.

        The pixels are the authority and the local OCR's read rides along as
        a draft: checking a draft against the picture is an easier job than
        reading from scratch, and the draft is also what lets the glossary
        match -- with no text at all, no term can ever hit. The rest of the
        machinery is unchanged: same prompt, context and glossary, and the
        reply goes through the same commentary and language checks as any
        other.
        """
        system = self.cfg.prompt.format(target=self.cfg.target_lang)
        user = (_instruction(self.cfg)
                + _glossary_block(self.cfg.glossary, [ocr_text]))
        if context:
            user += ("CONTEXT (already translated, for reference only):\n"
                     + "\n".join(context) + "\n\n")
        user += ("The image is a piece of a game screen. Reply with the "
                 f"{self.cfg.target_lang} translation of the text in it, and "
                 "nothing else. If it holds no text, reply with an empty "
                 "message.")
        if ocr_text:
            user += ("\n\nA local OCR read the text as follows; it may hold "
                     "misread look-alike characters. Trust the image where "
                     "they disagree:\n" + ocr_text)
        tokens = self.sec.get("max_tokens", 512)
        return self._chat_image(system, user, png, tokens)

    # Every clause here was earned on a real screen. Told merely to "check
    # the line against its box and translate what is written there", the
    # model read the pixels correctly and then answered with them: CONFIG
    # came back as CONFIG and a misread EXIT as EXIT -- transcription, not
    # translation, on exactly the labels the picture had just rescued. So
    # correcting and translating are named as two ordered steps, the second
    # is stated as an absolute, and the boxes that hold no words are given
    # somewhere to go other than an invented answer.
    VISION_BATCH_NOTE = (
        "The attached image is the game screen these lines came from, with "
        "each line's box drawn on it under the matching number. The lines "
        "are a local OCR's read of those boxes and may hold misread "
        "look-alike characters.\n"
        "A box is drawn where the recogniser found the line and may take in "
        "an icon or ornament beside it; translate the words only, and do "
        "not let neighbouring artwork colour the wording.\n"
        "For each numbered line: first look at the pixels inside that "
        "number's box and work out what the text really says, then reply "
        "with the TRANSLATION of it. Never reply with the original text or "
        "a transcription -- every answer must be in {target}, including "
        "boxes whose text is English. If a box holds no readable text, "
        "leave its answer empty after the number rather than guessing. "
        "Answer only the numbered lines listed below; ignore any other box "
        "on the image.\n"
    )

    def translate_batch_image(self, items: List[tuple], png: bytes,
                              context: List[str]) -> Optional[List[str]]:
        """One request for several boxes of one screen, pixels included.

        `items` pairs each line with the number drawn on its box, and the
        numbers survive the round trip untouched -- they are how the model
        ties a line of text to a patch of pixels, and how the reply finds its
        way back to the right caption. Menus are where OCR misreads hurt
        most (a one-character label has no sentence around it to absorb the
        error), and the numbered screenshot is what lets the model correct
        the read instead of translating the misread.
        """
        system = (self.cfg.prompt.format(target=self.cfg.target_lang)
                  + "\n" + BATCH_RULES)
        user = (_instruction(self.cfg) + BATCH_RULES
                + self.VISION_BATCH_NOTE.format(target=self.cfg.target_lang))
        user += _glossary_block(self.cfg.glossary, [t for _, t in items])
        if context:
            user += ("CONTEXT (already translated, for reference only):\n"
                     + "\n".join(context) + "\n\n")
        user += "\n".join(f"{n}. " + " ".join(t.splitlines())
                          for n, t in items)
        reply = self._chat_image(system, user, png,
                                 256 + 64 * len(items))
        return _parse_numbered_at(reply, [n for n, _ in items])

    def _chat_image(self, system: str, user: str, png: bytes,
                    tokens: int) -> str:
        mime = "image/png" if png[:4] == b"\x89PNG" else "image/jpeg"
        data_url = (f"data:{mime};base64,"
                    + base64.b64encode(png).decode("ascii"))
        data = _post_json(self.url, {
            "model": self.sec["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
            "max_tokens": tokens,
            "temperature": 0.2,
        }, {"Authorization": f"Bearer {self.key}"},
            # Reading pixels costs more than reading tokens: measured about
            # a second over the same model's text latency.
            _timeout_for(self.cfg, tokens) * 1.5)
        return data["choices"][0]["message"]["content"].strip()


class AnthropicBackend:
    def __init__(self, cfg: TranslateConfig) -> None:
        self.cfg = cfg
        self.sec = cfg.anthropic
        self.key = _resolve_key(self.sec)
        if not self.key:
            raise RuntimeError(
                f"Anthropic backend needs a key in ${self.sec.get('api_key_env')}")
        self.url = self.sec.get("base_url", "https://api.anthropic.com/v1").rstrip("/") + "/messages"

    def translate(self, text: str, context: List[str]) -> str:
        system = self.cfg.prompt.format(target=self.cfg.target_lang)
        user = _instruction(self.cfg) + _glossary_block(self.cfg.glossary, [text])
        if context:
            user += ("CONTEXT (already translated, for reference only):\n"
                     + "\n".join(context) + "\n\n")
        user += "TRANSLATE THIS LINE:\n" + text
        return self._message(system, user)

    def _message(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        tokens = max_tokens or self.sec.get("max_tokens", 512)
        payload = {
            "model": self.sec["model"],
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": tokens,
        }
        # Turn thinking off explicitly. On the current Claude models it is on
        # whenever the field is omitted, and a subtitle line is exactly the
        # workload it does not help: one short sentence, no reasoning to do,
        # and a latency budget measured in a few hundred milliseconds. Left at
        # the default, every line would think first.
        if self.sec.get("thinking", "disabled") == "disabled":
            payload["thinking"] = {"type": "disabled"}
        return self._extract(_post_json(
            self.url, payload,
            {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            _timeout_for(self.cfg, tokens)))

    @staticmethod
    def _extract(data: dict) -> str:
        return "".join(block.get("text", "") for block in data["content"]).strip()

    def translate_batch(self, texts: List[str], context: List[str]) -> Optional[List[str]]:
        system = self.cfg.prompt.format(target=self.cfg.target_lang) + "\n" + BATCH_RULES
        user = _instruction(self.cfg) + BATCH_RULES + "\n"
        user += _glossary_block(self.cfg.glossary, texts)
        if context:
            user += ("CONTEXT (already translated, for reference only):\n"
                     + "\n".join(context) + "\n\n")
        user += _batch_prompt(texts)
        reply = self._message(system, user, max_tokens=256 + 64 * len(texts))
        return _parse_numbered(reply, len(texts))


_BACKENDS = {
    "none": lambda c: NullBackend(),
    "google": GoogleWebBackend,
    "deepl": DeepLBackend,
    "openai": OpenAIBackend,
    "anthropic": AnthropicBackend,
}


def list_models(cfg: TranslateConfig) -> List[str]:
    """Ask the configured endpoint what models it serves.

    Both OpenAI-compatible servers and Anthropic answer GET /models with
    {"data": [{"id": ...}]}, so the same call covers a hosted API, a company
    gateway and a local server on some other port. Typing a model name by
    hand is guesswork; this turns it into a list.
    """
    name = (cfg.backend or "").lower()
    section = getattr(cfg, name, None)
    if not isinstance(section, dict) or "base_url" not in section:
        raise RuntimeError(f"{name} 后端没有可查询的模型列表")
    base = section["base_url"].rstrip("/")
    key = _resolve_key(section)
    if not key:
        raise RuntimeError("先填密钥再拉取模型列表")
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
               if name == "anthropic" else {"Authorization": f"Bearer {key}"})
    data = _request(urllib.request.Request(base + "/models", headers=headers),
                    cfg.timeout_s)
    items = data.get("data") if isinstance(data, dict) else None
    if not items:
        raise RuntimeError("端点没有返回模型列表")
    return sorted({str(item.get("id")) for item in items if item.get("id")})


def probe_endpoint(cfg: TranslateConfig) -> List[tuple]:
    """Walk the endpoint one layer at a time. Returns (step, ok, detail).

    A single failed translation cannot tell a dead proxy from a good proxy
    that cannot reach its upstream, or from a right host at the wrong path -
    all three surface as one 502. Testing reachability, then the listing
    endpoint, then the chat endpoint separates them, because each stage
    involves one more party than the last.
    """
    name = (cfg.backend or "").lower()
    section = getattr(cfg, name, None)
    if not isinstance(section, dict) or "base_url" not in section:
        raise RuntimeError(f"{name} 后端没有可诊断的地址")
    base = section["base_url"].rstrip("/")
    key = _resolve_key(section)
    auth = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
            if name == "anthropic" else {"Authorization": f"Bearer {key}"})
    steps: List[tuple] = []

    def reach(url: str, headers: dict, data: Optional[bytes] = None):
        """Did we get any HTTP response at all, and which one."""
        req = urllib.request.Request(
            url, data=data, headers=dict(headers),
            method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout_s) as resp:
                return True, resp.status, ""
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            return True, exc.code, ApiError._extract(body)
        except Exception as exc:  # noqa: BLE001 - DNS, TLS, refused, timeout
            return False, 0, f"{type(exc).__name__}: {exc}"

    ok, status, detail = reach(base, {})
    steps.append(("能连上这个地址吗", ok,
                  f"HTTP {status}" if ok else detail))
    if not ok:
        steps.append(("结论", False,
                      "根本没连上：域名解析不了、端口不通、TLS 失败，或者被墙。"
                      "这一步跟密钥和模型都无关。"))
        return steps

    ok, status, detail = reach(base + "/models", auth)
    steps.append((f"GET {base}/models", status == 200,
                  f"HTTP {status}" + (f" — {detail}" if detail else "")))
    models_ok = status == 200

    payload = json.dumps({
        "model": section.get("model", ""),
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }).encode("utf-8")
    chat_path = "/messages" if name == "anthropic" else "/chat/completions"
    ok, chat_status, detail = reach(base + chat_path,
                                    {**auth, "Content-Type": "application/json"},
                                    payload)
    steps.append((f"POST {base}{chat_path}", chat_status == 200,
                  f"HTTP {chat_status}" + (f" — {detail}" if detail else "")))

    steps.append(("结论", chat_status == 200, _verdict(
        models_ok, status, chat_status, base, section.get("model", ""))))
    return steps


def _verdict(models_ok: bool, models_status: int, chat_status: int,
             base: str, model: str) -> str:
    if chat_status == 200:
        return "全部通过。"
    if models_status in (401, 403) or chat_status in (401, 403):
        return "服务器活着，但拒绝了这个密钥。反代通常要求它自己的令牌，" \
               "而不是上游供应商的原始密钥——确认你填的是反代签发的那一个。"
    if models_ok and chat_status >= 500:
        return (f"反代本身是通的（模型列表拿得到），但转发聊天接口时失败了。"
                f"这一层只剩「反代到上游」这一段：上游不通、上游密钥失效、"
                f"或者这个模型（{model or '未填'}）在上游不可用。日志要去反代那边看。")
    if not models_ok and chat_status >= 500:
        return (f"两个接口都是 5xx：反代在监听，但转发到上游全部失败。"
                f"自建 nginx 反代最常见的原因是 proxy_pass 到 https 上游时"
                f"没开 proxy_ssl_server_name on，TLS 握手失败就是 502。")
    # 404 / 405 / 400 on both stages means the host answered but does not
    # serve this API here - the address points somewhere, just not at the API.
    if not models_ok and 400 <= chat_status < 500:
        return (f"主机是通的，但这个地址下没有这套接口"
                f"（两个路径都返回 {models_status} / {chat_status}）。"
                f"地址填到 .../v1 为止，别多带路径也别少带；"
                f"反代挂在子路径下的话要连子路径一起写，例如 "
                f"https://你的域名/openai/v1。")
    return "看上面每一步的状态码和服务器原话。"


def build_backend(cfg: TranslateConfig) -> Backend:
    name = (cfg.backend or "google").lower()
    if name not in _BACKENDS:
        raise ValueError(f"unknown translate backend {name!r}; "
                         f"pick one of {', '.join(sorted(_BACKENDS))}")
    return _BACKENDS[name](cfg)


class Translator:
    """Thread-safe cache + rolling context in front of a backend."""

    # How many batch groups may be in flight at once. Raising this to cover a
    # whole screen in one wave was the obvious speed-up and it measured worse:
    # against a relay, eight concurrent groups produced more 503s and more
    # timeouts than four, and the retries cost more than the wave saved.
    MAX_GROUP_WORKERS = 4

    def __init__(self, cfg: TranslateConfig) -> None:
        self.cfg = cfg
        self.backend = build_backend(cfg)
        self._cache: "OrderedDict[str, str]" = OrderedDict()
        # numeric_key -> (source digits, translation), for lines that differ
        # from one already translated only in their numbers.
        self._shapes: "OrderedDict[str, tuple]" = OrderedDict()
        self._context: Deque[str] = deque(maxlen=max(0, cfg.context_lines))
        # Folded-key index of the glossary, and the dict it was built from.
        self._folded: Dict[str, Optional[str]] = {}
        self._folded_src: Optional[Dict[str, str]] = None
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "cache_hits": 0, "glossary_hits": 0,
                      "numeric_hits": 0, "errors": 0, "dropped": 0,
                      "fallbacks": 0}
        # Backend for lines the primary declines; built on first need.
        # False = checked and not configured.
        self._fallback = None
        # role -> backend, for roles `role_models` sends elsewhere. Built on
        # first need and kept, because building one parses config and opens
        # nothing; it is the requests that cost.
        self._role_backends: Dict[str, Backend] = {}

    def _backend_for(self, role: str) -> "Backend":
        """The backend this role's model asks for, or the shared default.

        Only the model name changes: address, key and limits stay whatever
        the backend section says, so switching a role costs one config line
        and no second set of credentials. Backends with no model at all
        (google, deepl) have nothing to override and always get the default.

        The cache above this is deliberately shared across roles. The same
        source text has the same translation whichever model produced it,
        and a per-role cache would pay twice for every label a menu and a
        dialogue box happen to share.
        """
        model = (self.cfg.role_models or {}).get(role, "")
        if not model:
            return self.backend
        section = getattr(self.cfg, self.cfg.backend, None)
        if not isinstance(section, dict) or "model" not in section \
                or model == section.get("model"):
            return self.backend
        if role not in self._role_backends:
            from dataclasses import replace
            self._role_backends[role] = build_backend(replace(
                self.cfg, **{self.cfg.backend: dict(section, model=model)}))
        return self._role_backends[role]

    def _cache_get(self, key: str) -> Optional[str]:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        return None

    def _cache_put(self, key: str, value: str) -> None:
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.cfg.cache_size:
                self._cache.popitem(last=False)

    def _numeric_variant(self, text: str) -> Optional[str]:
        """The same line seen before with different numbers, respanned."""
        with self._lock:
            remembered = self._shapes.get(numeric_key(text))
        if remembered is None:
            return None
        old_digits, translation = remembered
        return respan_digits(translation, old_digits, _DIGITS.findall(text))

    def _remember_shape(self, text: str, translation: str) -> None:
        with self._lock:
            self._shapes[numeric_key(text)] = (_DIGITS.findall(text), translation)
            while len(self._shapes) > self.cfg.cache_size:
                self._shapes.popitem(last=False)

    def _glossary_exact(self, text: str) -> Optional[str]:
        """A whole line that is exactly a glossary term needs no engine.

        UI labels are single terms, and this is where generic engines go
        wrong most visibly -- they read ステータス as a person's standing
        rather than the status screen. An exact hit is also free and instant,
        which matters when a menu puts a dozen of them on screen at once.
        """
        if not self.cfg.glossary:
            return None
        hit = self.cfg.glossary.get(text)
        return hit if hit is not None else self._glossary_near(text)

    def _glossary_near(self, text: str) -> Optional[str]:
        """A glossary term the recogniser only appears to have missed.

        Windows OCR confuses characters that look alike, and no setting fixes
        it: measured on a rendered RPG Maker menu, セーブ came back as セーフ、
        at every upscale from 1 to 4 and under every contrast and binarisation
        setting tried, the aggressive ones reading nothing at all. The line
        then misses the glossary and reaches the model as nonsense, which is
        where a 設定 button captioned -三口 came from.

        Matching is on a folded form, not an edit distance. Distance was the
        first attempt and it is far too loose for Japanese, where two-kanji
        compounds share a character constantly: it rewrote 筋力 to 体力, 技量
        to 音量 and はずす to 交谈, all of which had been read perfectly. Only
        differences the recogniser actually produces are folded away -- a lost
        dakuten, and the kanji/katakana homoglyphs -- so a line has to be the
        same word to match.
        """
        probe = _ocr_fold(text.rstrip("、。,.・ "))
        if len(probe) < 2:
            return None
        return self._folded_glossary().get(probe)

    def _folded_glossary(self) -> Dict[str, Optional[str]]:
        """The glossary indexed by folded key, built once.

        Folding every term for every line that missed took 99ms a line
        against a glossary of 88,711 entries -- six seconds for one screen.
        A game's whole script is a reasonable thing to load here (MTool
        extracts exactly that, as a {japanese: chinese} dict), so the scan had
        to go.

        Two terms folding together store None, so an ambiguous line abstains
        and goes to the model rather than being guessed at.
        """
        if self._folded_src is not self.cfg.glossary:
            folded: Dict[str, Optional[str]] = {}
            for term, translation in self.cfg.glossary.items():
                key = _ocr_fold(term)
                if key in folded and folded[key] != translation:
                    folded[key] = None
                else:
                    folded[key] = translation
            self._folded = folded
            self._folded_src = self.cfg.glossary
        return self._folded

    # Bounds for the learned timeout scale: never below the configured
    # budget, never more than three times it.
    _SCALE_MAX = 3.0

    def _budget_feedback(self, took_s: float, timed_out: bool) -> None:
        """Let the timeout budget follow the model actually answering.

        Grown half again on every timeout, decayed a step on every answer
        that arrived inside half the current budget. A slow model earns its
        leash within a few lines instead of failing half of everything, and
        a fast one pulls the budget back so a hung request cannot stall the
        queue for long. The scale lives as an instance attribute on the
        config object, deliberately outside its dataclass fields: it is
        runtime state, and must never be written to disk as configuration.
        """
        scale = getattr(self.cfg, "timeout_scale", 1.0)
        if timed_out:
            scale = min(self._SCALE_MAX, scale * 1.5)
        elif took_s < (self.cfg.timeout_s * scale) / 2:
            scale = max(1.0, scale * 0.9)
        self.cfg.timeout_scale = scale

    def _try_fallback(self, text: str, context: List[str]) -> Optional[str]:
        """One retry on the fallback model, for a line the primary declined.

        Models differ sharply on content they treat as sensitive: measured
        on one such line, claude-haiku explained why it would not translate
        while three gpt models simply translated. Switching wholesale
        costs three times the latency on every line; a fallback pays it only
        on the declined ones. None means no fallback, or it declined too.
        """
        if self._fallback is None:
            section = getattr(self.cfg, self.cfg.backend, None)
            model = (section.get("fallback_model", "")
                     if isinstance(section, dict) else "")
            if model and model != section.get("model"):
                from dataclasses import replace
                self._fallback = build_backend(replace(
                    self.cfg, **{self.cfg.backend: dict(section, model=model)}))
            else:
                self._fallback = False
        if not self._fallback:
            return None
        try:
            out = (self._fallback.translate(text, context) or "").strip()
            self.stats["calls"] += 1
        except Exception:  # noqa: BLE001 - the primary's answer already failed
            return None
        if not out or is_commentary(text, out) \
                or is_wrong_language(out, self.cfg.target_lang):
            return None
        self.stats["fallbacks"] += 1
        return out

    def _glossary_per_line(self, text: str) -> Optional[str]:
        """Answer a multi-line block from the glossary a line at a time.

        A script pulled out of a game holds one entry per line as the game
        authored it, because that is the unit its engine displays. A caption
        covers a whole box, so the joined text matches nothing even when every
        line inside it is present -- 2 of 6 lines on a screen matched, and
        four of the misses were only there because of the joining.

        All or nothing: a partial answer would put half a translated box on
        screen and half in Japanese, which is worse than sending the lot to
        the model. When it does answer, it costs no request at all, and that
        is the only way a caption arrives before the player advances the text.
        """
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        out = []
        for line in lines:
            hit = self._glossary_exact(line)
            if hit is None:
                return None
            out.append(hit)
        return "".join(out)

    def translate(self, text: str, use_context: bool = True,
                  role: str = "") -> str:
        text = text.strip()
        if not text:
            return ""
        exact = self._glossary_exact(text)
        if exact is None:
            exact = self._glossary_per_line(text)
        if exact is not None:
            self.stats["glossary_hits"] += 1
            return exact
        # The model reads better without the box's line breaks in it; the
        # glossary above needed them, so they are dropped only now.
        text = " ".join(text.split())
        hit = self._cache_get(text)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit
        variant = self._numeric_variant(text)
        if variant is not None:
            self.stats["numeric_hits"] += 1
            return variant
        with self._lock:
            context = list(self._context) if use_context else []
        t0 = time.monotonic()
        try:
            out = self._backend_for(role).translate(text, context)
            self.stats["calls"] += 1
            self._budget_feedback(time.monotonic() - t0, timed_out=False)
        except Exception as exc:  # noqa: BLE001 - surfaced to the overlay
            self.stats["errors"] += 1
            self._budget_feedback(time.monotonic() - t0,
                                  timed_out="timed out" in str(exc).lower()
                                  or isinstance(exc, TimeoutError))
            return f"[translation failed: {type(exc).__name__}: {exc}]"
        out = (out or "").strip()
        if is_commentary(text, out) or is_wrong_language(out, self.cfg.target_lang):
            out = self._try_fallback(text, context)
            if out is None:
                # A visible failure, not a silent one. Dropping the refusal
                # quietly made a declined line indistinguishable from the
                # tool not working at all.
                self.stats["dropped"] += 1
                return "[translation failed: 模型没有给出译文（拒绝或答非所问）；" \
                       "可在配置 openai.fallback_model 里设一个后备模型]"
        if out:
            self._cache_put(text, out)
            self._remember_shape(text, out)
            if use_context and self.cfg.context_lines:
                with self._lock:
                    self._context.append(out)
        return out

    def translate_image(self, key_text: str, png: bytes,
                        use_context: bool = True, role: str = "") -> str:
        """Translate a region from its pixels, with the OCR text as the key.

        The local recogniser still runs -- its text is the cache key, so a
        line that repeats (a backlog, a re-entered room) never pays for the
        image again, and a glossary or full-script hit answers before any
        pixels are sent. Even a misread key works for caching: the same
        picture misreads the same way.
        """
        key_text = key_text.strip()
        backend_call = getattr(self._backend_for(role), "translate_image", None)
        if not key_text or backend_call is None:
            return self.translate(key_text, use_context=use_context, role=role)
        exact = self._glossary_exact(key_text) or self._glossary_per_line(key_text)
        if exact is not None:
            self.stats["glossary_hits"] += 1
            return exact
        flat = " ".join(key_text.split())
        hit = self._cache_get(flat)
        if hit is not None:
            self.stats["cache_hits"] += 1
            return hit
        with self._lock:
            context = list(self._context) if use_context else []
        t0 = time.monotonic()
        try:
            out = backend_call(png, context, ocr_text=flat)
            self.stats["calls"] += 1
            self._budget_feedback(time.monotonic() - t0, timed_out=False)
        except Exception as exc:  # noqa: BLE001 - surfaced to the overlay
            self.stats["errors"] += 1
            self._budget_feedback(time.monotonic() - t0,
                                  timed_out="timed out" in str(exc).lower()
                                  or isinstance(exc, TimeoutError))
            return f"[translation failed: {type(exc).__name__}: {exc}]"
        out = (out or "").strip()
        if is_commentary(key_text, out) \
                or is_wrong_language(out, self.cfg.target_lang):
            # The fallback still reads the OCR text: a second vision call is
            # twice the image cost for a line the local read already carries.
            fallback = self._try_fallback(flat, context)
            if fallback is None:
                self.stats["dropped"] += 1
                return "[translation failed: 模型没有给出译文（拒绝或答非所问）；" \
                       "可在配置 openai.fallback_model 里设一个后备模型]"
            out = fallback
        if out:
            self._cache_put(flat, out)
            self._remember_shape(flat, out)
            if use_context and self.cfg.context_lines:
                with self._lock:
                    self._context.append(out)
        return out

    def translate_many(self, texts: List[str], use_context: bool = False,
                       on_partial: Optional[Callable[[List[Optional[str]]],
                                                     None]] = None,
                       image: Optional[Callable[[], Optional[bytes]]] = None,
                       role: str = "") -> List[str]:
        """Translate several independent items, one request where possible.

        In-place display needs a translation per text block, and a screen full
        of menu entries would otherwise be one HTTP round trip each. Cached
        items never reach the network, and a backend that cannot batch, or a
        batch reply whose numbering does not line up, falls back to individual
        calls rather than risking mismatched captions.

        `on_partial` is handed the answers so far each time a group lands. A
        status screen is sixty blocks and a gateway answers about a line a
        second, so returning only when the last one arrives means twenty
        blank seconds over a menu the player is already reading. Entries not
        answered yet are None.

        `image` builds the whole region's screenshot with box i of `texts`
        drawn on it as number i+1. When the backend can read it, each batch
        group sends the image along and asks for its own numbers, so the
        model corrects OCR misreads against the pixels -- the group
        structure, and the resilience it buys, stays exactly as it is. A
        group whose vision call fails falls back to the same per-item text
        path as any other.

        It is a factory, not the bytes, because most fires send nothing at
        all: a menu that sits still re-OCRs every couple of seconds and is a
        cache hit every time. Measured on the live title screen, seventeen
        screenshots were drawn and encoded for three that were sent. The
        cache path is the one that has to stay instant -- it is what keeps
        captions from blinking on a screen that has not changed -- so the
        picture is built on the way to a request and never otherwise.
        """
        out: List[Optional[str]] = [None] * len(texts)
        pending: List[int] = []
        for i, text in enumerate(texts):
            clean = text.strip()
            if not clean:
                out[i] = ""
                continue
            exact = self._glossary_exact(clean)
            if exact is not None:
                self.stats["glossary_hits"] += 1
                out[i] = exact
                continue
            hit = self._cache_get(clean)
            if hit is not None:
                self.stats["cache_hits"] += 1
                out[i] = hit
            else:
                pending.append(i)
        if not pending:
            return [o or "" for o in out]

        # Everything the cache and the glossary already knew, before a single
        # request goes out. A HUD that stays put while the world moves behind
        # it re-OCRs every couple of seconds and is a cache hit every time, so
        # this lands in the same overlay frame as the clear that preceded it
        # and the captions never visibly blink. Waiting for the first network
        # group instead put a translation-long gap in the middle of text that
        # had not changed at all.
        if on_partial is not None:
            on_partial(out)

        with self._lock:
            context = list(self._context) if use_context else []
        wanted = [texts[i].strip() for i in pending]
        backend = self._backend_for(role)
        batch = getattr(backend, "translate_batch", None)
        batch_image = (getattr(backend, "translate_batch_image", None)
                       if image is not None else None)

        # Built at most once per call, and only if a request is really going
        # out. None means the caller could not produce one, which is not an
        # error: the text batch below is a complete answer without it.
        shot: List[Optional[bytes]] = []

        def screenshot() -> Optional[bytes]:
            if not shot:
                shot.append(image())
            return shot[0]

        # One pending item still wants the picture. Text batching a single
        # line is pure overhead, which is why the old floor was two, but a
        # lone line is the common case on a live menu -- the rest of the
        # screen is already cached, and only the entry under the cursor is
        # new. Measured on the title screen, that path took the text-only
        # route and transliterated a misread つづきから into 罗罗兹拉基卡拉,
        # while the same line inside a group came back as 从中途开始.
        if (batch is not None or batch_image is not None) \
                and (len(wanted) > 1 or batch_image is not None):
            size = max(1, self.cfg.batch_size)
            groups = [(pending[i:i + size], wanted[i:i + size])
                      for i in range(0, len(wanted), size)]

            # `sources`, not `texts`: unpacking a result into a name the
            # enclosing function already uses rebinds it, and the fallback
            # below indexes the full list with indices from before the
            # rebind. It crashed the translate thread outright -- capture
            # and OCR carried on, no line was ever emitted again.
            def run_group(group):
                indices, sources = group
                try:
                    png = screenshot() if batch_image is not None else None
                    if png is not None:
                        # Each group carries the shared screenshot and asks
                        # for its own box numbers, so a big screen keeps the
                        # same small-batch resilience vision or not.
                        got = batch_image(
                            [(i + 1, s) for i, s in zip(indices, sources)],
                            png, context)
                    elif batch is not None:
                        got = batch(sources, context)
                    else:
                        return None
                    self.stats["calls"] += 1
                except Exception:  # noqa: BLE001 - this group falls back alone
                    self.stats["errors"] += 1
                    return None
                return (indices, sources, got) if got and len(got) == len(sources) else None

            unresolved: List[int] = []

            # Called from the loop below, never from a pool thread, so `out`
            # and `unresolved` need no lock.
            def absorb(group, result) -> None:
                if result is None:
                    unresolved.extend(group[0])   # retry these one by one
                    return
                indices, sources, got = result
                for index, source, translated in zip(indices, sources, got):
                    translated = (translated or "").strip()
                    if (is_commentary(source, translated)
                            or is_wrong_language(translated,
                                                 self.cfg.target_lang)):
                        self.stats["dropped"] += 1
                        translated = ""
                    out[index] = translated
                    if translated:
                        self._cache_put(source, translated)
                        self._remember_shape(source, translated)
                if on_partial is not None:
                    on_partial(out)

            if len(groups) == 1:
                absorb(groups[0], run_group(groups[0]))
            else:
                workers = min(self.MAX_GROUP_WORKERS, len(groups))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(run_group, g): g for g in groups}
                    for future in as_completed(futures):
                        absorb(futures[future], future.result())
            if not unresolved:
                return [o or "" for o in out]
            pending = unresolved

        # Backends without batching still have to answer per item, but a menu
        # can easily hold a dozen labels and doing that serially took over
        # five seconds against the keyless endpoint. These are independent
        # requests, so run them at once and bound the fan-out so a big screen
        # cannot open thirty sockets.
        if len(pending) == 1:
            index = pending[0]
            out[index] = self.translate(texts[index], use_context=use_context,
                                        role=role)
            return [o or "" for o in out]

        with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
            futures = {pool.submit(self.translate, texts[i], use_context,
                                   role): i
                       for i in pending}
            for future in as_completed(futures):
                out[futures[future]] = future.result()
                if on_partial is not None:
                    on_partial(out)
        return [o or "" for o in out]

    def fix(self, translation: str) -> str:
        """Output-side replacements, applied to a caption on its way out.

        Kept outside the cache on purpose: the cache stores what the model
        said, and this rewrites it at display time, so editing the fixes and
        restarting corrects even lines the cache already holds. Failure
        notices pass through untouched -- they are diagnostics, not text.
        """
        if not translation or not self.cfg.fixes \
                or translation.startswith("[translation failed"):
            return translation
        for wrong, right in self.cfg.fixes.items():
            if wrong:
                translation = translation.replace(wrong, right)
        return translation

    def reset_context(self) -> None:
        with self._lock:
            self._context.clear()
