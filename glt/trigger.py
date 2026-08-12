"""Deciding *when* to run OCR.

This is the part that makes fast-moving scenes workable. A fixed-interval
scheduler has no good setting: fast enough to catch a line that flashes by
means burning OCR and translation calls on every intermediate frame of an
animation, and slow enough to be cheap means missing lines outright.

Instead each region runs a small state machine over a cheap visual
fingerprint:

    quiet ──(pixels moved)──► settling ──(still for stable_ms)──► FIRE
      ▲                          │                                │
      └──────────────────────────┴────────────────────────────────┘

So OCR runs exactly once per visual state, right after the picture stops
moving. A cutscene that churns for two seconds costs nothing until it settles;
a subtitle that appears and holds fires `stable_ms` later. The gate is in
milliseconds rather than frames on purpose -- see the note in `feed()`.

`max_hold_ms` is the escape hatch for regions that never fully settle (video
backgrounds behind text, capture-card noise): after that long in `settling`
we fire anyway rather than starving.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

# Fingerprint resolution. Small enough that diffing is free, large enough that
# a one-word change in a subtitle line still moves it.
_FP_W, _FP_H = 96, 24

# A fingerprint cell must shift by this much luma to count as changed. Sits
# above capture-card compression noise, below real text appearing.
_CELL_DELTA = 12.0


def fingerprint(bgra: np.ndarray) -> np.ndarray:
    """Downscale a region to a small float32 luma signature.

    INTER_AREA averages over source pixels, which doubles as a denoiser for
    the compression artifacts a capture card bakes into every frame.
    """
    if bgra.size == 0:
        return np.zeros((_FP_H, _FP_W), np.float32)
    gray = cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY) if bgra.ndim == 3 and bgra.shape[2] == 4 \
        else cv2.cvtColor(bgra, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (_FP_W, _FP_H), interpolation=cv2.INTER_AREA)
    return small.astype(np.float32)


def diff_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of fingerprint cells that changed.

    Deliberately not a mean absolute difference. On a wide subtitle bar two
    freshly typed characters cover ~1.5% of the area, so their contribution to
    a mean gets diluted to well under one luma level -- indistinguishable from
    noise, which makes a typewriter reveal look like a still picture and gets
    half a sentence sent to the translator. Counting *how many* cells moved
    keeps that signal at 1.5% regardless of how wide the region is, while
    global noise still moves no cells at all.
    """
    return float(np.count_nonzero(np.abs(a - b) > _CELL_DELTA)) / a.size


def edge_ratio(bgra: np.ndarray) -> float:
    """Fraction of pixels sitting on a strong edge.

    Text is edge-dense; an empty subtitle bar or a soft-focus background is
    not. Used to tell "the box is now blank" apart from "OCR found nothing".
    """
    if bgra.size == 0:
        return 0.0
    gray = cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY) if bgra.shape[2] == 4 \
        else cv2.cvtColor(bgra, cv2.COLOR_BGR2GRAY)
    if min(gray.shape) < 8:
        return 0.0
    edges = cv2.Laplacian(gray, cv2.CV_16S, ksize=3)
    return float(np.count_nonzero(np.abs(edges) > 48)) / gray.size


@dataclass
class TriggerConfig:
    # One CJK character covers roughly 0.008 of the fingerprint on a full-width
    # subtitle bar, so the default sits at about half a character. Anything at
    # or above one character is not safe: a typewriter reveal then adds too
    # little between two sampled frames to count as motion, the region looks
    # settled mid-sentence, and half a line goes out. Measured at 0.010 that
    # failure showed up in roughly one line in ten -- it depends on where the
    # sampling lands, which is why it survives a short test run.
    # On a noisy capture-card feed, raise it using `main.py tune`.
    motion_threshold: float = 0.004  # fraction of cells changed = "moving"
    stable_ms: int = 260            # how long the picture must hold still
    min_interval_ms: int = 150      # floor between two fires of one region
    max_hold_ms: int = 2500         # fire anyway if never fully settles
    blank_edge_ratio: float = 0.004  # below this the region counts as empty
    rearm_threshold: float = 0.003  # change vs last OCR'd frame to allow refire


@dataclass
class TriggerState:
    prev_fp: Optional[np.ndarray] = None
    fired_fp: Optional[np.ndarray] = None
    still_since: float = 0.0        # 0 means "currently moving"
    last_fire: float = 0.0
    was_blank: bool = True
    stats: dict = field(default_factory=lambda: {"fires": 0, "frames": 0, "suppressed": 0})


class RegionTrigger:
    """Per-region change detector. `feed()` returns True when OCR should run."""

    def __init__(self, cfg: TriggerConfig) -> None:
        self.cfg = cfg
        self.state = TriggerState()
        self.last_delta = 0.0
        self.last_edges = 0.0

    def feed(self, crop: np.ndarray, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.monotonic()
        st, cfg = self.state, self.cfg
        st.stats["frames"] += 1

        fp = fingerprint(crop)
        self.last_edges = edge_ratio(crop)

        if st.prev_fp is None:
            st.prev_fp = fp
            st.still_since = now
            st.last_fire = now
            return False

        delta = diff_ratio(fp, st.prev_fp)
        self.last_delta = delta
        st.prev_fp = fp

        if self.last_edges < cfg.blank_edge_ratio:
            # Region emptied out: report the transition once, then stay quiet.
            st.still_since = now
            if not st.was_blank:
                st.was_blank = True
                st.fired_fp = fp
                st.last_fire = now
                return True
            return False
        if st.was_blank:
            st.was_blank = False
            st.still_since = now
            return False

        timed_out = (now - st.last_fire) * 1000.0 >= cfg.max_hold_ms

        if delta > cfg.motion_threshold:
            st.still_since = 0.0        # moving; restart the settle clock
            # Falling through when the hold expires is the whole point of
            # max_hold_ms, and returning here skipped it: a region that never
            # stops moving never fired at all. A game hub with fire effects,
            # walking NPCs and swaying foliage is never still for 260ms, so a
            # full-screen region over one produced no captions whatsoever --
            # the exact starvation this escape hatch was written to prevent.
            if not timed_out:
                return False
        else:
            if st.still_since == 0.0:
                st.still_since = now
            # Time-based, not frame-based. A typewriter reveal pauses ~40 ms
            # between characters, so a frame counter at an uncontrolled capture
            # rate reads those pauses as "settled" and OCRs half a sentence.
            # Requiring stable_ms of real stillness makes the whole reveal one
            # event, and keeps behaviour identical at 20 fps or 240 fps.
            settled = (now - st.still_since) * 1000.0 >= cfg.stable_ms
            if not (settled or timed_out):
                return False
        if (now - st.last_fire) * 1000.0 < cfg.min_interval_ms:
            return False

        # Identical picture to the one we already OCR'd - nothing new to say.
        if st.fired_fp is not None:
            if diff_ratio(fp, st.fired_fp) < cfg.rearm_threshold:
                st.stats["suppressed"] += 1
                st.last_fire = now      # also defers the max_hold escape hatch
                return False

        st.fired_fp = fp
        st.last_fire = now
        st.stats["fires"] += 1
        return True

    @property
    def is_blank(self) -> bool:
        return self.last_edges < self.cfg.blank_edge_ratio
