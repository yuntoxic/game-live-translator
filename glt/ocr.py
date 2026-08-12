"""OCR engines.

Default is the OCR engine built into Windows. On this machine it reads a
Japanese subtitle line in ~14 ms and an English one in ~4 ms, against ~600 ms
for an ONNX detector-plus-recogniser pipeline -- and it was also the more
accurate of the two on Japanese. At 20 fps across several regions that gap is
the difference between keeping up and falling behind, so Windows OCR is the
default and RapidOCR is the fallback for machines missing the language pack.

Windows OCR ships language packs separately. Check what you have with:
    python main.py languages
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

import cv2
import numpy as np

_CJK = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ]"
)


@dataclass
class OcrConfig:
    engine: str = "windows"          # windows | rapidocr
    upscale: float = 2.0             # small text reads far better enlarged
    contrast: float = 1.0            # >1 pushes text away from background
    binarize: bool = False           # last resort for heavy outline fonts
    invert: bool = False             # for dark-text-on-light subtitle boxes
    min_chars: int = 2
    drop_regex: str = ""
    coalesce_ms: int = 250           # hold a new line briefly to let it finish
    reject_noise: bool = True        # discard lines read out of background texture
    skip_numeric: bool = True        # lines with no letters at all
    skip_target_script: str = ""     # drop lines already written in this script


@dataclass
class TextBlock:
    """One recognised line, with where it sat in the image it came from.

    Positions are what makes in-place display possible: the translation can be
    drawn over the words it replaces instead of collected into a bar at the
    bottom, which matters as soon as a screen has text in several places at
    once and a single bar cannot say which one it is translating.
    """
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    # How many wrapped rows were joined into this one. The box then spans all
    # of them, so anything sizing a caption from the box has to divide by it.
    rows: int = 1
    # The colour behind this text, as a Tk colour string, so a caption drawn
    # over it can match instead of patching the picture with a fixed dark.
    bg: str = ""

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def row_height(self) -> float:
        return self.height / max(1, self.rows)

    def scaled(self, factor: float) -> "TextBlock":
        return TextBlock(self.text, self.x0 / factor, self.y0 / factor,
                         self.x1 / factor, self.y1 / factor, self.rows,
                         self.bg)

    def offset(self, dx: float, dy: float) -> "TextBlock":
        return TextBlock(self.text, self.x0 + dx, self.y0 + dy,
                         self.x1 + dx, self.y1 + dy, self.rows, self.bg)


def cluster_blocks(blocks: List[TextBlock], gap_factor: float = 1.8
                   ) -> List[Tuple[float, float, float, float]]:
    """Group recognised lines into the blocks of text a screen is built from.

    A menu is rows at an even pitch; a hint bar is one line on its own far
    below them. Splitting on vertical gaps larger than a line pitch separates
    those without knowing anything about the game: consecutive rows of the
    same list stay together, and a run of them ends where the layout leaves
    real space.

    Returns boxes in the same coordinates as the blocks.
    """
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: b.y0)
    heights = sorted(b.height for b in ordered)
    pitch = max(1.0, heights[len(heights) // 2])

    clusters: List[List[TextBlock]] = [[ordered[0]]]
    for block in ordered[1:]:
        bottom = max(b.y1 for b in clusters[-1])
        if block.y0 - bottom <= pitch * gap_factor:
            clusters[-1].append(block)
        else:
            clusters.append([block])
    return [union_box(group) for group in clusters]


def sample_background(bgra: np.ndarray, block: TextBlock,
                      segments: int = 0) -> str:
    """The colour(s) behind a line of text, as Tk colour strings.

    A caption drawn on a fixed dark rectangle reads as a patch stuck over the
    game. Taking the colour from the picture instead makes it read as the game
    having been in the target language all along, which is what a good
    screenshot translator looks like.

    Sampled from thin bands just above and below the line, not from the box
    itself. The box is full of glyph pixels and they drag the median: measured
    against the true background of a message popup, sampling the box was 39
    levels off and the band was exact. Between two stacked lines the band is
    the leading, which is background as well.

    One flat colour is only right when the field is flat. A dialog box shaded
    toward its edges, or a translucent panel with art behind it, changes
    along the line, and a flat patch over it has visible seams. So the line
    is sampled in segments across its width -- a median per segment tracks
    the gradient while still ignoring the glyphs -- and the result is one
    colour per segment, space separated. Zero segments picks enough for one
    every ~2 line-heights.
    """
    height = bgra.shape[0]
    x0 = max(0, int(block.x0))
    x1 = min(bgra.shape[1], int(block.x1))
    if x1 - x0 < 2:
        return ""
    band = max(2, int(block.row_height * 0.18))
    above = bgra[max(0, int(block.y0) - band):max(1, int(block.y0)), x0:x1]
    below = bgra[min(height - 1, int(block.y1)):
                 min(height, int(block.y1) + band), x0:x1]
    parts = [p for p in (above, below) if p.size]
    if not parts:
        return ""
    strip = np.concatenate(parts, axis=0)      # rows x width x channels

    if segments <= 0:
        segments = max(1, int((x1 - x0) / max(8.0, block.row_height * 2)))

    def seg_median(band_img, lo, hi):
        if band_img is None or not band_img.size or hi - lo < 1:
            return None
        piece = band_img[:, lo:hi].reshape(-1, band_img.shape[2])
        return tuple(int(np.median(piece[:, i])) for i in range(3))

    # The whole line's median, as the referee. Either band can be lying for
    # part of the line -- the band above a dialog box's first row reaches
    # through the translucent edge into whatever scenery is behind it, and a
    # flower bed there turned several segments olive. A true gradient moves
    # both bands together, so per segment the band closer to the global
    # colour is the one still sampling the field the text sits on.
    everything = np.concatenate([p.reshape(-1, strip.shape[2])
                                 for p in parts], axis=0)
    global_bgr = tuple(int(np.median(everything[:, i])) for i in range(3))
    above_img = above if above.size else None
    below_img = below if below.size else None

    bounds = np.linspace(0, strip.shape[1], segments + 1).astype(int)
    colours = []
    for lo, hi in zip(bounds, bounds[1:]):
        candidates = [c for c in (seg_median(above_img, lo, hi),
                                  seg_median(below_img, lo, hi))
                      if c is not None]
        if not candidates:
            continue
        blue, green, red = min(
            candidates,
            key=lambda c: sum(abs(a - b) for a, b in zip(c, global_bgr)))
        colours.append(f"#{red:02x}{green:02x}{blue:02x}")
    return " ".join(colours)


def join_wrapped_rows(blocks: List[TextBlock], gap_factor: float = 0.4,
                      align_factor: float = 1.2,
                      min_width_chars: float = 8.0) -> List[TextBlock]:
    """Rejoin text the game wrapped across rows, before anything translates it.

    A description too long for its panel is wrapped, and each row comes back
    as its own line. Translated separately the second row has nothing to
    resolve against: 「それはゲージを必要としない」 on its own becomes 它不需要
    能量槽, where the whole sentence would have dropped the pronoun. A row
    wrapped mid-word -- 「…必要があ」 / 「ります。」 -- is worse still, and
    comes back as nonsense.

    Menu rows are also consecutive lines at a regular pitch, and merging those
    would be worse than the problem being fixed. Measured on a Nightreign
    character screen the two separate with room to spare: wrapped rows sit
    0.33 of a row height apart, a name and the description beneath it sit
    1.05 apart, and anything across a section break is 2.3 or more. Left edges
    must line up too, which is what keeps a second column from being stitched
    onto the first -- to within `align_factor`, about one character, because
    a wrapped continuation is very often indented by one. A visual novel's
    second row sat 20px right of the first at a row height of 36, and a half
    character of tolerance rejected it by two pixels: the sentence went to the
    model in halves and came back as two captions over one line of dialogue.
    A neighbouring column is hundreds of pixels away, not one character.

    Spacing alone is not enough, though, and a tightly set stat list proved
    it -- 生命力 above 集中力 has exactly the geometry of a wrapped pair. The
    physical constraint settles it: a line only wraps because it filled the
    width available. A row narrower than `min_width_chars` of its own height
    did not run out of room, so whatever follows it is a new item.
    """
    if len(blocks) < 2:
        return list(blocks)

    ordered = sorted(blocks, key=lambda b: (b.y0, b.x0))
    # ponytail: O(n^2) over the blocks of one screen, which is tens of items.
    # Bucket by left edge first if a screen ever holds thousands.
    successor: dict = {}
    taken = set()
    for i, block in enumerate(ordered):
        if (block.x1 - block.x0) < block.row_height * min_width_chars:
            continue            # never filled its line, so it never wrapped
        best, best_gap = None, None
        for j, other in enumerate(ordered):
            if i == j or j in taken:
                continue
            gap = other.y0 - block.y1
            if gap < 0 or gap >= block.row_height * gap_factor:
                continue
            if abs(other.x0 - block.x0) >= block.row_height * align_factor:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = j, gap
        if best is not None:
            successor[i] = best
            taken.add(best)

    out: List[TextBlock] = []
    for i, block in enumerate(ordered):
        if i in taken:
            continue            # picked up as somebody's continuation
        chain = [block]
        cursor = i
        while cursor in successor:
            cursor = successor[cursor]
            chain.append(ordered[cursor])
        if len(chain) == 1:
            out.append(block)
            continue
        # No separator: the game broke a continuous string, so putting one
        # back would be inventing a space Japanese never had.
        out.append(TextBlock(
            "".join(b.text for b in chain),
            min(b.x0 for b in chain), min(b.y0 for b in chain),
            max(b.x1 for b in chain), max(b.y1 for b in chain),
            sum(b.rows for b in chain)))
    return out


def dominant_cluster(blocks: List[TextBlock],
                     gap_factor: float = 1.8) -> List[TextBlock]:
    """The main body of text, with stray fragments dropped as a group.

    A dialogue region is translated as one utterance, so everything inside
    it lands in the sentence. Framed a little generously it also contains
    scenery, and ornate scenery reads as text: a brick wall above one dialog
    box came back as six two-character fragments -- 第一い, 働ーロ -- each too
    short for the noise filter to judge, and the model dutifully translated
    the wall along with the line.

    The real dialogue is one vertical run of adjacent rows; the debris is
    scattered. Cluster rows on the same vertical-gap rule the region picker
    uses, keep the cluster with the most characters, and the wall goes away
    as a group even though no fragment was suspicious on its own.
    """
    if len(blocks) < 2:
        return list(blocks)
    ordered = sorted(blocks, key=lambda b: b.y0)
    heights = sorted(b.row_height for b in ordered)
    pitch = max(1.0, heights[len(heights) // 2])

    clusters: List[List[TextBlock]] = [[ordered[0]]]
    for block in ordered[1:]:
        bottom = max(b.y1 for b in clusters[-1])
        if block.y0 - bottom <= pitch * gap_factor:
            clusters[-1].append(block)
        else:
            clusters.append([block])
    return max(clusters, key=lambda c: sum(len(b.text) for b in c))


def union_box(blocks: List[TextBlock]):
    return (min(b.x0 for b in blocks), min(b.y0 for b in blocks),
            max(b.x1 for b in blocks), max(b.y1 for b in blocks))


class OcrEngine(Protocol):
    def read(self, bgra: np.ndarray, lang: str) -> List[TextBlock]: ...


def preprocess(bgra: np.ndarray, cfg: OcrConfig) -> np.ndarray:
    """Cheap contrast/scale work that buys real accuracy on game fonts."""
    img = bgra
    if cfg.upscale and cfg.upscale != 1.0:
        img = cv2.resize(img, None, fx=cfg.upscale, fy=cfg.upscale,
                         interpolation=cv2.INTER_CUBIC)
    if cfg.contrast and cfg.contrast != 1.0:
        img = cv2.convertScaleAbs(img, alpha=cfg.contrast, beta=0)
    if cfg.invert:
        bgr = cv2.bitwise_not(img[:, :, :3])
        img = np.dstack([bgr, img[:, :, 3]]) if img.shape[2] == 4 else bgr
    if cfg.binarize:
        gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY) if img.shape[2] == 4 \
            else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        img = cv2.cvtColor(th, cv2.COLOR_GRAY2BGRA)
    if img.shape[2] == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    return np.ascontiguousarray(img)


# What the recogniser reaches for when it is matching ornament rather than
# reading: dots, dashes, the middle dot, brackets.
#
# Expressive punctuation is deliberately absent -- … ‥ ! ！ ? ？ and the
# quotation brackets 「」『』. Dialogue is full of them and ornament produces
# none: the hallucinated sample this threshold was measured against,
# 「、-。ー画履い・・をツ、、第日を一・男第、い箒物をツ Ｐ」, contains not one.
# Counting them dropped 「な、なんだ……!!!」 as texture at a ratio of 0.55.
_PUNCT = re.compile(
    r"[、。・ー―‐\-,.·（）()\[\]{}:：;；~〜=＝*＊/／\\|｜'\"`^_+＋<>＜＞]"
)
def _punct_density(text: str) -> float:
    """Punctuation marks per character, counting a run of one mark once.

    An ellipsis is a single mark written as several glyphs, and the
    recogniser often returns it as middle dots -- 「……」 comes back as
    「・・・・・」. Ornament is the opposite shape: separate marks scattered
    between characters. Counting glyphs cannot tell them apart at all;
    measured over real dialogue and the hallucinated lines this threshold was
    built from, the two ranges overlap outright (real text reaches 0.71,
    texture starts at 0.37). Counting runs separates them cleanly: real text
    tops out at 0.22 and texture starts at 0.30.

    This replaced a rule that dropped any doubled 、 。 or ・ outright, on the
    grounds that Japanese writing does not double them. It does, once the
    recogniser has turned an ellipsis into dots, and that rule was quietly
    deleting whole lines of dialogue.
    """
    marks, previous = 0, ""
    for char in text:
        if _PUNCT.match(char) and char != previous:
            marks += 1
        previous = char
    return marks / max(1, len(text))

# Between the two measured ranges: real text tops out at 0.22 (a button-hint
# row, ":back:switch tab:help"), texture starts at 0.30.
_NOISE_PUNCT_RATIO = 0.26

# Ornament repeats -- a row of studs, a filigree border, a strip of icons --
# and the recogniser returns the same character once per motif: 把把把.
#
# Han only, and that is not timidity. "Three of anything running" was the
# first version and it dropped a whole line of dialogue,
# 「うおぉ...いいぞ中がぎゅうぎゅう収縮して気持ちいい...。」, because of the
# ellipsis -- while the rule right below it already carried a note saying
# ...... and --- are ordinary writing. Kana is no safer: あああ and うおぉぉぉ
# are how a game writes a shout. Dropping a real line is worse than drawing
# an occasional junk one, so ムムム now gets through and 把把把 does not.
_RUN_OF_THREE = re.compile(r"([㐀-䶿一-鿿豈-﫿])\1{2,}")
# A two-character unit repeated to fill the line: 我第我第. Restricted to Han
# because Japanese reduplicates in kana for real -- いろいろ, だんだん,
# そろそろ are words, and 我第我第 is a border.
_DOUBLED_HAN_PAIR = re.compile(
    r"^([㐀-䶿一-鿿豈-﫿]{2})\1+$"
)


_KANA = re.compile(r"[぀-ヿｦ-ﾟ]")
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_HANGUL = re.compile(r"[가-힣]")


def has_no_letters(text: str) -> bool:
    """True for lines that are only digits, punctuation or symbols.

    Stat screens are mostly numbers - "80.0 / 126.4", "63.3 %", "1854/1854" -
    and sending those to a translator costs a request each and returns them
    unchanged. On a Dark Souls equipment screen they are the majority of what
    gets recognised.
    """
    return not _LETTER.search(text)


# What each source language legitimately writes with. A line is only foreign
# to it if it uses a script that is not on this list.
_SOURCE_SCRIPTS = {
    "ja": ("kana", "han", "latin"),
    "zh": ("han", "latin"),
    "ko": ("hangul", "han", "latin"),
    "en": ("latin",),
}


def is_already_target(text: str, target: str, source: str = "") -> bool:
    """True only when a line is in the target and could not be source text.

    The tempting version of this - "has han, has no kana, so it is Chinese" -
    is wrong and dangerously so. Japanese writes plenty of words in kanji
    alone: 装備重量, 初期化, 攻撃力. Skipping those as "already Chinese" drops a
    large share of a Japanese game's UI silently, which looks exactly like the
    tool failing to see the text at all.

    Short CJK strings simply do not carry enough signal to tell the languages
    apart, so the question asked here is the answerable one: does the line use
    a script the source language never uses? For Japanese source and Chinese
    target the answer is always no, and nothing gets skipped.
    """
    if not target or not source or not text.strip():
        return False
    allowed = _SOURCE_SCRIPTS.get(source.lower().split("-")[0])
    if not allowed:
        return False
    scripts = set()
    if _KANA.search(text):
        scripts.add("kana")
    if _HAN.search(text):
        scripts.add("han")
    if _HANGUL.search(text):
        scripts.add("hangul")
    if re.search(r"[A-Za-z]", text):
        scripts.add("latin")
    if not scripts or not scripts.isdisjoint(allowed):
        return False
    # Written entirely in scripts the source language does not use - and in
    # one the target does.
    target_script = {"zh": "han", "ja": "kana", "ko": "hangul",
                     "en": "latin"}.get(target.lower().split("-")[0])
    return target_script in scripts


def looks_like_noise(text: str) -> bool:
    """True when a line is texture that OCR turned into characters.

    Ornate UI - parchment, stone, filigree - gives the recogniser plenty of
    edges that are not writing, and it dutifully returns characters for them.
    The signal is punctuation density, counting runs rather than glyphs: the
    recogniser reaches for dots and dashes when it is matching ornament, and
    scatters them, while writing puts them in runs.

    Two earlier signals were dropped after measurement rather than kept as
    tiebreakers. Lone characters between punctuation: the hallucinated sample
    scored 0.00 on it and a legitimate line 0.33, worse than nothing. Doubled
    、 。 or ・, on the grounds that Japanese does not double them: it does
    once the recogniser turns an ellipsis into dots, and that rule was
    deleting whole lines of dialogue.

    Repetition is judged separately and at any length, because the ornament
    the recogniser invents characters from is usually a short strip: 把把把
    and 我第我第 are three and four characters, well under the six this
    function needs before it will judge punctuation density at all.
    """
    stripped = re.sub(r"\s", "", text)
    if len(stripped) >= 3 and (_RUN_OF_THREE.search(stripped)
                               or _DOUBLED_HAN_PAIR.match(stripped)):
        return True
    if len(stripped) < 6:
        return False           # too short to judge; min_chars already applies
    return _punct_density(stripped) > _NOISE_PUNCT_RATIO


# Cut a recognised line where the spacing is too wide to be one run of text.
# Windows OCR returns one "word" per CJK character, so this is really a
# character-spacing test. Measured on a Nightreign character screen: gaps
# inside real text reach 0.85 of the line's glyph height (the space after 、),
# while a stray mark that had been merged into 力の感応 sat 1.49 away. The
# threshold goes between the two.
_LINE_GAP_FACTOR = 1.2

# Japanese does not begin a word with a small kana or a long vowel mark, so a
# line that starts with one starts with something the recogniser invented --
# ッスキル is the icon left of スキル.
_CANNOT_START = "ァィゥェォッャュョヮぁぃぅぇぉっゃゅょゎー・"


def split_on_gaps(rects, gap_factor: float = _LINE_GAP_FACTOR) -> List[int]:
    """Indices where a line should be cut, from its per-word rectangles.

    An ornate panel puts an icon close enough to a label that the two come
    back as one line, and the icon's characters then corrupt the translation
    of a word that was recognised perfectly well. Splitting on distance keeps
    the label intact and leaves the mark on its own, where the existing
    punctuation and length filters deal with it.
    """
    if len(rects) < 2:
        return []
    heights = sorted(r.height for r in rects)
    line_height = max(1.0, heights[len(heights) // 2])
    cuts = []
    for i in range(1, len(rects)):
        gap = rects[i].x - (rects[i - 1].x + rects[i - 1].width)
        if gap > line_height * gap_factor:
            cuts.append(i)
    return cuts


def strip_leading_artifact(text: str) -> str:
    text = text.lstrip()
    while len(text) > 1 and text[0] in _CANNOT_START:
        text = text[1:]
    return text


def _join_words(words: List[str]) -> str:
    """Windows OCR splits CJK into per-character 'words'; Latin into real ones.

    Joining on a space unconditionally turns a Japanese line into spaced-out
    characters, so decide per line based on script.
    """
    if not words:
        return ""
    joined = "".join(words)
    cjk = len(_CJK.findall(joined))
    if cjk >= max(2, len(joined) * 0.3):
        return joined
    return " ".join(words)


class WindowsOcr:
    """Windows.Media.Ocr. Engines are per-language and cached.

    winsdk's API is async, so we own one event loop for the calling thread
    rather than paying `asyncio.run` setup on every frame.
    """

    def __init__(self) -> None:
        import winsdk.windows.globalization as wg
        import winsdk.windows.graphics.imaging as wgi
        import winsdk.windows.media.ocr as wocr
        import winsdk.windows.security.cryptography as wsc

        self._wg, self._wgi, self._wocr, self._wsc = wg, wgi, wocr, wsc
        self._engines: dict = {}
        self._loop = asyncio.new_event_loop()

    @staticmethod
    def available_languages() -> List[str]:
        import winsdk.windows.media.ocr as wocr
        return [l.language_tag for l in wocr.OcrEngine.available_recognizer_languages]

    def _engine(self, lang: str):
        if lang not in self._engines:
            eng = self._wocr.OcrEngine.try_create_from_language(self._wg.Language(lang))
            if eng is None:
                have = ", ".join(self.available_languages()) or "(none)"
                raise RuntimeError(
                    f"Windows OCR has no '{lang}' pack installed. Available: {have}. "
                    f"Add it under Settings > Time & Language > Language, then tick "
                    f"Optional features > Optical character recognition."
                )
            self._engines[lang] = eng
        return self._engines[lang]

    def read(self, bgra: np.ndarray, lang: str) -> List[TextBlock]:
        engine = self._engine(lang)
        h, w = bgra.shape[:2]
        if w < 8 or h < 8:
            return []
        buf = self._wsc.CryptographicBuffer.create_from_byte_array(bgra.tobytes())
        bitmap = self._wgi.SoftwareBitmap.create_copy_from_buffer(
            buf, self._wgi.BitmapPixelFormat.BGRA8, w, h)
        result = self._loop.run_until_complete(engine.recognize_async(bitmap))

        blocks: List[TextBlock] = []
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            rects = [word.bounding_rect for word in words]
            bounds = [0, *split_on_gaps(rects), len(words)]
            for start, end in zip(bounds, bounds[1:]):
                part = words[start:end]
                text = _join_words([word.text for word in part])
                if not text.strip():
                    continue
                spans = rects[start:end]
                blocks.append(TextBlock(
                    text,
                    min(r.x for r in spans), min(r.y for r in spans),
                    max(r.x + r.width for r in spans),
                    max(r.y + r.height for r in spans)))
        return blocks


class RapidOcr:
    """ONNX fallback. Slower, no OS language packs needed."""

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self._engine = RapidOCR()

    def read(self, bgra: np.ndarray, lang: str) -> List[TextBlock]:  # noqa: ARG002
        bgr = bgra[:, :, :3] if bgra.shape[2] == 4 else bgra
        result, _ = self._engine(np.ascontiguousarray(bgr))
        if not result:
            return []
        blocks = []
        for quad, text, _score in result:
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
            blocks.append(TextBlock(text, min(xs), min(ys), max(xs), max(ys)))
        return blocks


def build_engine(name: str) -> OcrEngine:
    name = (name or "windows").lower()
    if name in ("windows", "winocr", "win"):
        return WindowsOcr()
    if name in ("rapidocr", "rapid", "onnx"):
        return RapidOcr()
    raise ValueError(f"unknown OCR engine: {name!r} (use 'windows' or 'rapidocr')")


class OcrReader:
    """Engine plus preprocessing plus the junk filter."""

    def __init__(self, cfg: OcrConfig) -> None:
        self.cfg = cfg
        self.engine = build_engine(cfg.engine)
        self._drop: Optional[re.Pattern] = re.compile(cfg.drop_regex) if cfg.drop_regex else None

    def read_blocks(self, crop_bgra: np.ndarray, lang: str) -> List[TextBlock]:
        """Recognised lines, with boxes back in the crop's own pixel space.

        Preprocessing enlarges the crop before recognition, so every box comes
        back in enlarged coordinates and has to be divided back down or the
        in-place overlay lands at the wrong place.
        """
        blocks = self.engine.read(preprocess(crop_bgra, self.cfg), lang)
        factor = self.cfg.upscale or 1.0
        out: List[TextBlock] = []
        for block in blocks:
            text = strip_leading_artifact(block.text)
            if not text:
                continue
            if self._drop and self._drop.search(text):
                continue
            # Drop lines that are only punctuation or stray marks.
            if len(re.sub(r"[\s\W_]", "", text, flags=re.UNICODE)) < 1:
                continue
            if self.cfg.reject_noise and looks_like_noise(text):
                continue
            if self.cfg.skip_numeric and has_no_letters(text):
                continue
            if is_already_target(text, self.cfg.skip_target_script, lang):
                continue
            out.append(TextBlock(text, block.x0, block.y0, block.x1, block.y1)
                       .scaled(factor))
        total = len(re.sub(r"\s", "", "".join(b.text for b in out)))
        return out if total >= self.cfg.min_chars else []

    def read(self, crop_bgra: np.ndarray, lang: str) -> str:
        return "\n".join(b.text for b in self.read_blocks(crop_bgra, lang))
