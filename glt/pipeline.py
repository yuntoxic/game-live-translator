"""Wiring: capture -> trigger -> OCR -> translate -> overlay.

Four stages on three threads, joined by mailboxes that keep only the newest
item per region. Dropping stale work is deliberate: when the picture has moved
on, a translation of the line before last is worse than useless, because it
shows up on screen underneath the wrong scene.

    capture thread   crop + fingerprint + trigger      (microseconds/frame)
    ocr thread       Windows OCR + dedupe              (~15 ms/fire)
    translate thread network call, latest-wins         (100 ms - 2 s)
    main thread      Tk overlay
"""

from __future__ import annotations

import difflib
import json
import re
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

from .capture import WindowCapture, find_window, list_windows
from .config import AppConfig, Region
from .inplace import InPlaceOverlay
from .ocr import (OcrReader, TextBlock, dominant_cluster, join_wrapped_rows,
                  sample_background, union_box)
from .overlay import Line, Overlay, Placement
from .translate import Translator
from .trigger import RegionTrigger, diff_ratio, fingerprint


# Kana, han, hangul, or letters -- the characters that carry meaning, as
# opposed to the digits, brackets and stray marks OCR sprinkles around them.
_SUBSTANTIVE = re.compile(r"[぀-ヿｦ-ﾟ㐀-䶿一-鿿豈-﫿가-힣]|[^\W\d_]")


def _folded(text: str) -> str:
    """Text as material for an is-it-the-same comparison: width-normalised,
    whitespace gone."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _numbered_jpeg(crop: np.ndarray, blocks: List[TextBlock],
                   offset: Tuple[int, int]) -> Optional[bytes]:
    """The region's screenshot with block i's box drawn on it as number i+1.

    The numbers are the contract with translate_batch_image: they tie each
    OCR line to its patch of pixels, so the model can correct a misread
    label against what is actually drawn there. Boxes are outlined, not
    filled, and the number sits just above its box where a menu usually has
    padding -- a label covered by its own annotation cannot be proofread.

    Wider ceiling than the dialogue path's 800: a menu's labels are small
    and the model has to re-read them, where a dialogue crop is one big
    line whose draft mostly survives.
    """
    image = cv2.cvtColor(crop, cv2.COLOR_BGRA2BGR)
    scale = min(1.0, 1280 / image.shape[1])
    if scale < 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA)
    ox, oy = offset
    for i, b in enumerate(blocks):
        x0, y0 = int((b.x0 - ox) * scale), int((b.y0 - oy) * scale)
        x1, y1 = int((b.x1 - ox) * scale), int((b.y1 - oy) * scale)
        cv2.rectangle(image, (x0, y0), (x1, y1), (0, 220, 0), 1)
        spot = (x0, max(14, y0 - 4))
        # Twice, dark under bright, so the number reads on any background.
        cv2.putText(image, str(i + 1), spot, cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(image, str(i + 1), spot, cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (0, 220, 0), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


class LatestSlot:
    """Mailbox holding at most one pending item per key; newest wins."""

    def __init__(self) -> None:
        self._items: Dict[str, object] = {}
        self._cv = threading.Condition()
        self._closed = False
        self.dropped = 0

    def put(self, key: str, value: object) -> None:
        with self._cv:
            if key in self._items:
                self.dropped += 1
            self._items[key] = value
            self._cv.notify()

    def get(self, timeout: float = 0.25) -> Optional[Tuple[str, object]]:
        with self._cv:
            if not self._items and not self._closed:
                self._cv.wait(timeout)
            if not self._items:
                return None
            key = next(iter(self._items))
            return key, self._items.pop(key)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()


@dataclass
class RegionRuntime:
    region: Region
    trigger: RegionTrigger
    reader: OcrReader
    last_text: str = ""
    last_dispatch: float = 0.0
    offset: Tuple[int, int] = (0, 0)   # region origin in frame pixels
    # Whether the coalesce hold has ever caught a longer read of a line here,
    # and how many lines have gone out without it doing so. See `_hold_ms`.
    coalesce_earned: int = 0
    coalesce_missed: int = 0
    # Where the dispatched text sat (crop coordinates) and what those pixels
    # looked like, so an empty read can be told apart from a vanished line.
    # See `_caption_stale`.
    caption_box: Tuple[int, int, int, int] = ()
    caption_fp: object = None


class Pipeline:
    def __init__(self, cfg: AppConfig, debug: bool = False, duration: float = 0.0) -> None:
        self.cfg = cfg
        self.debug = debug
        self.duration = duration
        self.stop_event = threading.Event()
        self.ocr_slot = LatestSlot()
        self.tr_slot = LatestSlot()
        self.overlay = None
        self.capture: Optional[WindowCapture] = None
        self.frame_size: Optional[Tuple[int, int]] = None
        # Set by the control panel to mirror every line into its log view.
        self.on_line: Optional[Callable[[Line], None]] = None
        self._threads: List[threading.Thread] = []
        self._log_fp = None
        self._speaker = ""
        self.frames = 0
        self.started = time.monotonic()

        self.runtimes: Dict[str, RegionRuntime] = {}
        readers: Dict[str, OcrReader] = {}
        for region in cfg.regions:
            ocr_cfg = cfg.region_ocr(region)
            # Text already in the target language needs no translating, and a
            # game screen is full of it. The OCR stage cannot know the target
            # on its own, so hand it over here unless a region overrides it.
            if not (region.ocr or {}).get("skip_target_script"):
                ocr_cfg.skip_target_script = cfg.translate.target_lang
            # One reader per distinct OCR config; engines are not free to build.
            key = repr(sorted(ocr_cfg.__dict__.items()))
            if key not in readers:
                readers[key] = OcrReader(ocr_cfg)
            self.runtimes[region.name] = RegionRuntime(
                region=region,
                trigger=RegionTrigger(cfg.region_trigger(region)),
                reader=readers[key],
            )

        self.translator = Translator(cfg.translate)

    # -- resolution -------------------------------------------------------
    def resolve_window(self) -> int:
        src = self.cfg.source
        if src.window_hwnd:
            return int(src.window_hwnd)
        if not src.window_title:
            raise RuntimeError(
                "config.source needs window_title or window_hwnd. "
                "Run `python main.py windows` to see the options.")
        info = find_window(src.window_title)
        if info is None:
            titles = "\n  ".join(w.title for w in list_windows()[:25])
            raise RuntimeError(
                f"no visible window title contains {src.window_title!r}.\n"
                f"Open windows:\n  {titles}")
        return info.hwnd

    # -- stage 1: capture thread -----------------------------------------
    def _on_frame(self, frame_bgra: np.ndarray) -> None:
        self.frames += 1
        h, w = frame_bgra.shape[:2]
        if (w, h) != self.frame_size:
            self.frame_size = (w, h)
            if self.overlay is not None and hasattr(self.overlay, "set_frame_size"):
                self.overlay.set_frame_size(w, h)
        now = time.monotonic()
        for name, rt in self.runtimes.items():
            x0, y0, x1, y1 = rt.region.pixels(w, h)
            rt.offset = (x0, y0)
            crop = frame_bgra[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            if rt.trigger.feed(crop, now):
                # Copy only on a fire: the buffer is reused by the next frame.
                self.ocr_slot.put(name, (crop.copy(), rt.trigger.is_blank))

    # -- stage 2: OCR thread ----------------------------------------------
    def _ocr_loop(self) -> None:
        # A line is held for coalesce_ms before it goes out. If a longer read
        # of the same line lands during the hold - which is what a typewriter
        # reveal looks like once its last characters fall under the motion
        # threshold - it replaces the shorter one and the clock restarts. That
        # costs a fixed sub-frame delay and removes half-sentence translations
        # without having to tune a threshold per game.
        # region -> (text, blocks, crop, when it may go out)
        pending: Dict[str, tuple] = {}

        while not self.stop_event.is_set():
            item = self.ocr_slot.get(timeout=0.05)
            now = time.monotonic()

            if item is not None:
                name, payload = item
                crop, blank = payload  # type: ignore[misc]
                rt = self.runtimes[name]

                if blank:
                    # Say so, rather than just going quiet: an in-place
                    # overlay keeps showing the last thing it was told, so a
                    # region emptying out has to be an event of its own or
                    # closing a menu leaves its captions over the game.
                    pending.pop(name, None)
                    if rt.last_text:
                        rt.last_text = ""
                        self._push(Line(name, rt.region.role, "", "",
                                        self._speaker, []))
                else:
                    t0 = time.perf_counter()
                    try:
                        blocks = rt.reader.read_blocks(crop, rt.region.lang)
                    except Exception as exc:  # noqa: BLE001
                        self._emit_error(name, f"OCR failed: {exc}")
                        blocks = []
                    # Sample the colour behind each line while the pixels are
                    # still here, so a caption can match the game's own
                    # background instead of patching it with a fixed dark.
                    blocks = [replace(b, bg=sample_background(crop, b))
                              for b in blocks]
                    # Boxes arrive in crop coordinates; shift them into frame
                    # coordinates now, while the region offset is at hand.
                    ox, oy = rt.offset
                    blocks = [b.offset(ox, oy) for b in blocks]
                    # Before anything downstream sees them: a description the
                    # game wrapped is one thing to translate, not two.
                    blocks = join_wrapped_rows(blocks)
                    if rt.region.role == "dialogue":
                        # One utterance per dispatch, so scenery the region
                        # accidentally contains must not be read into the
                        # sentence. Per-line regions keep everything: their
                        # items are independent and debris only costs its
                        # own caption, not the line's.
                        blocks = dominant_cluster(blocks)
                    text = "\n".join(b.text for b in blocks)
                    if self.debug:
                        print(f"[ocr] {name:<10} {(time.perf_counter()-t0)*1000:5.1f}ms  "
                              f"delta={rt.trigger.last_delta:.4f} "
                              f"edges={rt.trigger.last_edges:.4f}  {text!r}")
                    if not text:
                        # Recognised nothing where there used to be text.
                        # Either the screen moved on, or the recogniser
                        # flaked -- outlined text on scenery is exactly where
                        # it does, and clearing on a flake made the caption
                        # flash and vanish while the line was still on
                        # screen. Only the pixels can arbitrate: if the area
                        # the caption covers still holds the edge density it
                        # held when dispatched, the text is still there.
                        pending.pop(name, None)
                        if rt.last_text and self._caption_stale(rt, crop):
                            rt.last_text = ""
                            self._push(Line(name, rt.region.role, "", "",
                                            self._speaker, []))
                    elif not self._same_line(rt, text):
                        held = pending.get(name)
                        if held is None or held[0] != text:
                            if held is not None:
                                # The hold just earned its keep: a longer read
                                # of the same line arrived before the deadline,
                                # which is what a typewriter reveal looks like.
                                rt.coalesce_earned += 1
                            hold_ms = self._hold_ms(rt)
                            pending[name] = (text, blocks, crop,
                                             now + hold_ms / 1000.0)

            # Distinct names from the ones above: unpacking into `text` and
            # `blocks` here rebinds what the branch above is still working
            # with. That exact shadowing killed the translate thread once
            # already, in translate_many, and it costs nothing to avoid.
            for region, (held_text, held_blocks, held_crop, due) \
                    in list(pending.items()):
                if now >= due:
                    del pending[region]
                    self.runtimes[region].coalesce_missed += 1
                    self._dispatch(region, held_text, held_blocks, held_crop)

    # How many lines a region may go without the hold catching a longer read
    # before the hold is treated as dead weight, and what it shrinks to.
    _COALESCE_PROBATION = 6
    _COALESCE_FLOOR_MS = 40

    def _hold_ms(self, rt: "RegionRuntime") -> int:
        """How long to sit on a freshly recognised line before sending it.

        The hold exists for games that reveal text a character at a time: the
        picture stops moving between characters, so the first read is half a
        sentence, and holding lets a longer read replace it. It costs its full
        length on every line, and a game that shows a whole line at once never
        collects on it -- a quarter of a second added to every caption for
        nothing.

        So it is on probation. Every line that goes out without the hold ever
        having caught a longer read counts against it, and after a few the
        region drops to a token wait. One catch reinstates it, because the
        cost of being wrong here is half a sentence on screen.
        """
        full = rt.reader.cfg.coalesce_ms
        if rt.coalesce_earned or rt.coalesce_missed < self._COALESCE_PROBATION:
            return full
        return min(full, self._COALESCE_FLOOR_MS)

    # Fraction of fingerprint cells that must change inside the caption's
    # own box before an empty read counts as the line leaving the screen.
    _CAPTION_GONE = 0.12

    # How alike two dialogue reads must be to count as the same line.
    # Measured on real recogniser damage: variants of one line score 0.714
    # at worst against each other (a clean read against a junk-heavy one),
    # while consecutive story lines top out at 0.200. The threshold sits
    # between them with room on both sides. tools/check_same_line.py holds
    # the fixtures and re-measures both bounds.
    _SAME_LINE = 0.55

    @staticmethod
    def _same_line(rt: "RegionRuntime", text: str) -> bool:
        """Is this read the line already dispatched, spelt differently?

        Outlined dialogue re-reads with different debris on every pass --
        「待って……こんな所に」 one time, 謇「待って・=こんな所に」 the next --
        and every variant re-dispatched: cleared or re-translated a caption
        that was already right, and translated the same hint two different
        ways. Similarity, not equality, decides for dialogue; per-line
        regions keep exact matching, because a one-item change in a menu is
        a small edit to a long joined string and must not be skipped.
        """
        if text == rt.last_text:
            return True
        if rt.region.role != "dialogue" or not rt.last_text:
            return False
        a = " ".join(text.split())
        b = " ".join(rt.last_text.split())
        return difflib.SequenceMatcher(None, a, b).ratio() >= Pipeline._SAME_LINE

    @staticmethod
    def _caption_stale(rt: "RegionRuntime", crop) -> bool:
        """Has the text the caption covers actually left the screen?

        The recogniser is the flakiest part of the chain -- outlined glyphs
        over scenery read on one pass and not the next -- and an empty pass
        used to clear the caption while the line was still visibly there.
        The pixels say which happened, compared structurally rather than by
        edge density: a busy background is edge-dense with or without text
        on it (a first draft compared densities and a caption over bricks
        could never expire), while a fingerprint of the caption's own box
        changes exactly where the glyphs were.
        """
        if not rt.caption_box or rt.caption_fp is None:
            return True
        x0, y0, x1, y1 = rt.caption_box
        x0, y0 = max(0, x0), max(0, y0)
        y1, x1 = min(crop.shape[0], y1), min(crop.shape[1], x1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            return True
        now = fingerprint(crop[y0:y1, x0:x1])
        return diff_ratio(now, rt.caption_fp) > Pipeline._CAPTION_GONE

    def _dispatch(self, name: str, text: str, blocks: List[TextBlock],
                  crop=None) -> None:
        rt = self.runtimes[name]
        # The captions up now describe the text being replaced, and their
        # replacements are a translation away. Drop them here, where the text
        # is known to have changed -- not on a trigger fire. A scene with fire
        # or spell effects fires constantly while the text underneath never
        # moves, and clearing on a fire made those captions strobe.
        #
        # "The text changed" needs the same pixel arbitration as the empty
        # read: outlined glyphs re-read as a slightly different string on
        # every pass, and each variant cleared a good caption and then paid
        # a full translation before anything replaced it. If the caption's
        # own pixels have not changed, the line is the same line however the
        # recogniser spelt it today; the fresh translation lands as a
        # seamless replacement instead.
        if rt.last_text and rt.last_text != text and rt.region.translate \
                and rt.region.role != "name" \
                and (crop is None or self._caption_stale(rt, crop)):
            self._push(Line(name, rt.region.role, "", "", self._speaker, []))
        rt.last_text = text
        rt.last_dispatch = time.monotonic()
        # Remember where this text sits and what those pixels look like, so
        # a later empty read can ask whether the line truly left the screen.
        rt.caption_box, rt.caption_fp = (), None
        if blocks and crop is not None:
            ox, oy = rt.offset
            ux0, uy0, ux1, uy1 = union_box(blocks)
            box = (max(0, int(ux0 - ox)), max(0, int(uy0 - oy)),
                   min(crop.shape[1], int(ux1 - ox)),
                   min(crop.shape[0], int(uy1 - oy)))
            if box[2] - box[0] >= 4 and box[3] - box[1] >= 4:
                rt.caption_box = box
                rt.caption_fp = fingerprint(
                    crop[box[1]:box[3], box[0]:box[2]])
        if rt.region.role == "name":
            self._speaker = text.replace("\n", " ")
            self._push(Line(name, "name", self._speaker, "", self._speaker))
        elif not rt.region.translate:
            self._push(Line(name, rt.region.role, text, text, self._speaker,
                            self._place(rt, blocks, [b.text for b in blocks])))
        else:
            self.tr_slot.put(name, (text, blocks, crop))

    def _place(self, rt: RegionRuntime, blocks: List[TextBlock],
               translations: List[str]) -> List[Placement]:
        """Pair translations with the boxes they should be drawn over.

        Whole-region translations get the union of every line's box, so a
        two-line sentence is captioned once across both lines rather than
        arbitrarily pinned to the first.
        """
        if not blocks:
            return []
        if len(translations) == len(blocks):
            # A caption identical to the text under it is pure occlusion: a
            # character name written in kanji translates to itself, and
            # drawing 姫野彩羽 on top of 姫野彩羽 hides the picture to say
            # nothing. Same for a line the model returned unchanged --
            # compared folded, because the recogniser spaces CJK ("口 7 「")
            # while the model does not ("口7「"), and that difference is not
            # a translation. Blocks with fewer than two substantive
            # characters go too: they are artwork read as text -- a sprite's
            # armband, a monument's carving -- and their "translations" were
            # landing as junk captions in the middle of the scene.
            return [Placement(b.text, t, (b.x0, b.y0, b.x1, b.y1),
                              line_height=b.row_height, bg=b.bg)
                    for b, t in zip(blocks, translations)
                    if t and _folded(t) != _folded(b.text)
                    and len(_SUBSTANTIVE.findall(b.text)) >= 2]
        joined = " ".join(t for t in translations if t)
        if not joined:
            return []
        # One caption across several lines: size it from a typical line, not
        # from the union box, which is as tall as the whole passage.
        heights = sorted(b.height for b in blocks)
        backgrounds = [b.bg for b in blocks if b.bg]
        union = union_box(blocks)
        # The caption starts where the WORDS start. A dialog box's ornament
        # column reads as a one-character block on the same rows as the
        # dialogue -- too close for the cluster filter -- and taking the
        # union's left edge as the anchor slid the whole caption off the
        # original's left margin. Substantial blocks position the text; the
        # plate still spans everything, so the ornament junk stays covered.
        solid = [b for b in blocks if len(b.text.strip()) >= 3] or blocks
        anchor = union_box(solid)
        # A dialogue plate hugs its text with a two-character allowance on
        # the right, clamped to the region. Spanning the whole region was
        # tried and covers far too much on a game that draws its dialogue
        # straight onto the scene with no box at all; the tails the
        # recogniser misses measured one or two glyphs (a closing 」 and the
        # mark before it), so two
        # character-widths of allowance covers them without painting a bar
        # across the screen.
        plate = ()
        if rt.region.role == "dialogue" and self.frame_size:
            rx0, _, rx1, _ = rt.region.pixels(*self.frame_size)
            lh = sorted(b.row_height for b in blocks)[len(blocks) // 2]
            plate = (max(rx0, union[0] - lh * 0.5), union[1],
                     min(rx1, union[2] + lh * 2.2), union[3])
        return [Placement("\n".join(b.text for b in blocks), joined,
                          anchor,
                          line_height=heights[len(heights) // 2],
                          # Every line of one passage sits on the same field,
                          # so any of them describes the whole box.
                          bg=backgrounds[0] if backgrounds else "",
                          plate_box=plate)]

    # -- stage 3: translate thread ----------------------------------------
    def _translate_loop(self) -> None:
        while not self.stop_event.is_set():
            item = self.tr_slot.get()
            if item is None:
                continue
            try:
                self._translate_one(item)
            except Exception as exc:  # noqa: BLE001
                # The thread has to outlive a bad item. It used to die here,
                # and nothing said so: capture and OCR carried on at full
                # speed, the fps and 识别 counters kept climbing, and not one
                # caption ever appeared again. A crash that is indistinguishable
                # from idleness is worse than a crash.
                traceback.print_exc()
                self._emit_error("translate", f"{type(exc).__name__}: {exc}")

    def _translate_one(self, item) -> None:
        name, payload = item  # type: ignore[misc]
        text, blocks, crop = payload
        rt = self.runtimes[name]
        t0 = time.perf_counter()

        # A screenful takes twenty seconds and the player does not wait for it.
        # `last_text` is this dispatch's text until the trigger fires again, so
        # comparing against it says whether the picture these captions describe
        # is still the one on screen. Without it, leaving a menu mid-translation
        # brings the menu's captions back over whatever came next.
        def current() -> bool:
            return rt.last_text == text

        if self.cfg.region_per_line(rt.region) and len(blocks) > 1:
            # Separate on-screen items: each needs its own translation and
            # its own position, in one batched request where the backend
            # supports it.
            sources = [b.text for b in blocks]

            def draw(partial: List[Optional[str]]) -> List[str]:
                """Put what has arrived on screen and keep the rest blank.

                One item out of fifty failing is normal against a flaky
                gateway, and the notice must never become a caption: the
                whole-region path below refuses to draw at all on failure,
                but here the line is one of many, so blank it and it falls
                out of both the placements and the log line.
                """
                ready = ["" if not t or t.startswith("[translation failed")
                         else self.translator.fix(t) for t in partial]
                if self.overlay is not None and current():
                    self.overlay.push(Line(name, rt.region.role, "", "",
                                           self._speaker,
                                           self._place(rt, blocks, ready)))
                return ready

            # The same vision switch as dialogue, in the shape menus need:
            # per-line boxes survive because the numbering carries them.
            # Handed over unbuilt -- most fires are answered entirely from
            # the cache and never send a picture at all.
            image = None
            if (self.cfg.translate.vision and crop is not None
                    and getattr(self.translator.backend,
                                "translate_batch_image", None)):
                image = lambda: _numbered_jpeg(crop, blocks, rt.offset)  # noqa: E731
            translations = draw(self.translator.translate_many(
                sources, use_context=False, on_partial=draw, image=image,
                role=rt.region.role))
            flat = " / ".join(s for s in sources)
            shown = " / ".join(t for t in translations if t)
        else:
            # Hand over the line breaks: a game script is stored one line at
            # a time, so the glossary can answer a whole box from its parts
            # only if it can still see where the parts were. The translator
            # flattens them itself before anything reaches the model.
            flat = " ".join(str(text).splitlines()).strip()
            if (self.cfg.translate.vision and crop is not None
                    and rt.region.role == "dialogue"):
                # The model reads the pixels; the OCR text stays on as the
                # cache key and the glossary key. Half-scale JPEG, because
                # measured against the full-resolution PNG the reading was
                # equally good and the latency identical -- the time is model
                # thinking, not transfer -- while the bytes drop from ~670KB
                # to ~29KB, and image tokens are billed by size.
                image = cv2.cvtColor(crop, cv2.COLOR_BGRA2BGR)
                if image.shape[1] > 800:
                    factor = 800 / image.shape[1]
                    image = cv2.resize(image, None, fx=factor, fy=factor,
                                       interpolation=cv2.INTER_AREA)
                ok, buf = cv2.imencode(
                    ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
                shown = (self.translator.translate_image(
                            str(text).strip(), buf.tobytes(),
                            use_context=True, role=rt.region.role)
                         if ok else self.translator.translate(
                            str(text).strip(), use_context=True,
                            role=rt.region.role))
            else:
                shown = self.translator.translate(
                    str(text).strip(),
                    use_context=rt.region.role == "dialogue",
                    role=rt.region.role)
            # Display time, not cache time: see Translator.fix.
            shown = self.translator.fix(shown)
            translations = [shown]

        if self.debug:
            print(f"[tr ] {name:<10} {(time.perf_counter()-t0)*1000:5.0f}ms  "
                  f"{shown!r}")

        # A failure is reported, never drawn. A failure notice painted over
        # the game covers the picture and tells the player nothing they can
        # act on; the panel's log is where it belongs.
        failed = shown.startswith("[translation failed")
        self._push(Line(name, rt.region.role, flat, shown, self._speaker,
                        [] if failed else self._place(rt, blocks, translations)),
                   to_overlay=not failed and current())

    # -- sinks ------------------------------------------------------------
    def _push(self, line: Line, to_overlay: bool = True) -> None:
        if self.overlay is not None and to_overlay:
            self.overlay.push(line)
        elif self.on_line is None and line.source:
            # A line with no source is a clear signal for the overlay; there
            # is nothing for a console reader to see in it.
            prefix = f"[{line.speaker}] " if line.speaker else ""
            print(f"{prefix}{line.source}\n  -> {line.translation}")
        if self.on_line is not None:
            self.on_line(line)
        self._log(line)

    def _emit_error(self, region: str, message: str) -> None:
        print(f"[error] {region}: {message}")
        # Under the panel there is no console to print to -- the launcher's
        # window is behind the game, and a bug that only shows up there is a
        # bug nobody sees. Send it to the log pane, where a line beginning
        # this way is already drawn in red.
        if self.on_line is not None:
            self.on_line(Line(region, "info", "",
                              f"[translation failed] {message}", "", []))

    def _log(self, line: Line) -> None:
        if self._log_fp is None:
            return
        self._log_fp.write(json.dumps({
            "t": round(time.monotonic() - self.started, 3),
            "region": line.region,
            "speaker": line.speaker,
            "src": line.source,
            "dst": line.translation,
        }, ensure_ascii=False) + "\n")
        self._log_fp.flush()

    # -- lifecycle --------------------------------------------------------
    def start(self, overlay_master=None) -> None:
        """Bring up capture and the worker threads without blocking.

        The control panel needs this separate from `run()` because it already
        owns the Tk loop and only wants the machinery underneath started.
        """
        hwnd = self.resolve_window()
        if self.cfg.log_file:
            self._log_fp = Path(self.cfg.log_file).open("a", encoding="utf-8")

        capture = WindowCapture(hwnd, self._on_frame, max_fps=self.cfg.source.max_fps)
        capture.start()
        if not capture.wait_for_first_frame(5.0):
            capture.stop()
            raise RuntimeError(
                f"captured no frames from hwnd={hwnd}. The window may be minimised "
                f"(WGC cannot capture minimised windows), running in exclusive "
                f"fullscreen, or already closed.")
        self.capture = capture
        self.started = time.monotonic()

        for fn in (self._ocr_loop, self._translate_loop):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)

        if self.cfg.overlay.enabled and overlay_master is not None:
            self.overlay = self._make_overlay(hwnd, overlay_master)

    def _make_overlay(self, hwnd: int, master):
        if self.cfg.overlay.mode == "bar":
            return Overlay(self.cfg.overlay, master=master)
        overlay = InPlaceOverlay(self.cfg.overlay, hwnd, master=master)
        if self.frame_size:
            overlay.set_frame_size(*self.frame_size)
        return overlay

    def run(self) -> None:
        self.start()
        capture = self.capture
        opts = {k: v for k, v in capture.options_used.items() if k != "window_hwnd"}
        print(f"capturing hwnd={capture.hwnd}  options={opts}")
        print(f"regions: {', '.join(r.name for r in self.cfg.regions)}   "
              f"ocr={self.cfg.ocr.engine}  translate={self.cfg.translate.backend}"
              f" -> {self.cfg.translate.target_lang}")
        try:
            if self.cfg.overlay.enabled:
                self.overlay = self._make_overlay(capture.hwnd, None)
                self.overlay.run()          # blocks on the Tk main loop
            else:
                deadline = time.monotonic() + self.duration if self.duration else None
                print("overlay disabled; printing to console. Ctrl-C to stop.")
                while not capture.closed:
                    if deadline and time.monotonic() > deadline:
                        break
                    time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self, capture: Optional[WindowCapture] = None) -> None:
        self.stop_event.set()
        self.ocr_slot.close()
        self.tr_slot.close()
        capture = capture or self.capture
        if capture is not None:
            capture.stop()
            self.capture = None
        if self._log_fp is not None:
            self._log_fp.close()
            self._log_fp = None
        elapsed = max(1e-6, time.monotonic() - self.started)
        fires = sum(rt.trigger.state.stats["fires"] for rt in self.runtimes.values())
        suppressed = sum(rt.trigger.state.stats["suppressed"] for rt in self.runtimes.values())
        print(f"\nframes={self.frames} ({self.frames/elapsed:.1f}/s)  "
              f"ocr_fires={fires}  suppressed_repeats={suppressed}  "
              f"stale_dropped={self.ocr_slot.dropped + self.tr_slot.dropped}\n"
              f"translate: calls={self.translator.stats['calls']} "
              f"cache_hits={self.translator.stats['cache_hits']} "
              f"errors={self.translator.stats['errors']} "
              f"dropped={self.translator.stats['dropped']}")
