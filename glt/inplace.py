"""In-place overlay: the translation is drawn on top of the words it replaces.

A single subtitle bar has two problems once a screen holds more than one piece
of text. It sits over the picture whether or not it has anything to say, and
when several things are on screen at once nothing tells you which one a line
belongs to. Windows OCR already returns a box per recognised line, so drawing
each translation at its own position removes both problems at once.

How it stays out of the way:

* the window is sized to the target window and made transparent except where
  something is drawn, using Windows' colour-key transparency (`-transparentcolor`);
* WS_EX_TRANSPARENT lets every click through to the game underneath, so it is
  never in the way of playing;
* nothing is drawn at all when no text is on screen.

It cannot feed back into the capture: the pipeline captures one window by
handle, and this is a different window, so the overlay is never in frame.
"""

from __future__ import annotations

import ctypes
import queue
import time
import tkinter as tk
from ctypes import wintypes
from typing import List, Optional, Tuple

import win32con
import win32gui

from .config import OverlayConfig
from .overlay import Line, Placement

# Any pixel of this exact colour becomes a hole in the window.
#
# It has to be near-black rather than the usual magenta. Colour keying is all
# or nothing, but Tk anti-aliases text, so the partly-covered pixels along
# every glyph edge are blends of the text colour and this one -- close to the
# key but not equal to it, so they stay opaque. Against magenta that shows up
# as a bright pink fringe around every character. Against near-black the same
# fringe is dark and reads as part of the text's own shadow.
#
# Nothing we draw may use this exact value or it would punch a hole.
COLOUR_KEY = "#010203"

# The seamless mask, per the accepted reference effect: rgba(35,35,35,0.42).
# The window carries the alpha; the erase strip on the canvas simulates the
# same blend, so the two must share these numbers or they show as layers.
BAND_COLOUR = "#232323"
BAND_ALPHA = 0.42


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _no_key(colour: str) -> str:
    """Any colour except the transparency key.

    A sampled background can land exactly on the key -- a near-black dialog
    box is one dark median away from it -- and a plate drawn in the key is a
    hole: the caption floats on the bare game with nothing covered at all.
    One blue level is invisible and safe.
    """
    return "#010204" if colour.lower() == COLOUR_KEY else colour


def _lerp_colour(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colours, never the transparency key.

    The endpoints are clamped away from the key before they get here, but a
    blend BETWEEN two safe colours can still land exactly on it -- #010203
    sits between #000000 and #020406 -- and one strip of the plate would be
    a transparent hole.
    """
    va = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    vb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return _no_key("#" + "".join(f"{round(x + (y - x) * t):02x}"
                                 for x, y in zip(va, vb)))


def can_step_above(box, others, gap: float = 4.0) -> bool:
    """Is the strip directly above this line clear of other text?

    Drawing an outlined caption over its original leaves two scripts on the
    same pixels and neither can be read, so the caption steps above instead.
    That works for text with room around it -- an item name, a HUD label --
    and fails badly for a paragraph, where the space above a line is the
    previous line. A dialog full of body text came out as every Chinese line
    printed across the Japanese line above it.

    So the step is conditional: take it when the strip is free, and cover the
    original with a plate when it is not. Dense text has to be covered; there
    is nowhere else for it to go.
    """
    x0, y0, x1, y1 = box
    height = max(8.0, y1 - y0)
    strip = (x0, y0 - height - gap, x1, y0 - gap)
    return not any(_overlaps(strip, other) for other in others)


class InPlaceOverlay:
    """Draws translations at the screen position of the text they replace."""

    def __init__(self, cfg: OverlayConfig, target_hwnd: int,
                 master: Optional[tk.Misc] = None) -> None:
        self.cfg = cfg
        self.hwnd = target_hwnd
        self.queue: "queue.Queue[Optional[Line]]" = queue.Queue()
        self.owns_root = master is None
        self._closed = False
        self._placements: List[Placement] = []
        self._shown_at: dict = {}
        self._frame_size: Optional[Tuple[int, int]] = None
        self._geometry: Optional[Tuple[int, int, int, int]] = None

        self.root = tk.Tk() if master is None else tk.Toplevel(master)
        self.root.title("Game Live Translator Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COLOUR_KEY)
        try:
            self.root.attributes("-transparentcolor", COLOUR_KEY)
        except tk.TclError:
            # Without colour-key support the window would be an opaque slab
            # over the game, which is worse than no overlay at all.
            raise RuntimeError(
                "this Windows build does not support transparent overlays; "
                "set overlay.mode to \"bar\" in the config")

        self.canvas = tk.Canvas(self.root, bg=COLOUR_KEY, highlightthickness=0,
                                borderwidth=0)
        self.canvas.pack(fill="both", expand=True)

        # The bands behind seamless captions are their own windows, not
        # canvas rectangles. A canvas rectangle cannot be translucent -- the
        # stipple fake reads as a checkerboard and was rejected against a
        # reference screenshot -- while a whole window carries a real alpha
        # channel: flat rgba(35,35,35,0.62) with no texture at all. A POOL
        # of them, one per caption cluster, grown on demand: unioning every
        # caption into one window put a corner hint and the dialogue line in
        # the same rectangle and veiled the entire screen between them.
        self._bands: List[tk.Toplevel] = []

        self.root.update_idletasks()
        self._set_click_through()
        self._follow_target()
        self.root.after(40, self._drain)
        self.root.after(200, self._track)

    # -- window plumbing --------------------------------------------------
    def _set_click_through(self, window: Optional[tk.Misc] = None) -> None:
        target = window if window is not None else self.root
        hwnd = win32gui.GetParent(target.winfo_id()) or target.winfo_id()
        if window is None:
            # Kept: this is the OS handle of the overlay, and the only honest
            # source for where the window really is (see _follow_target).
            self._os_hwnd = hwnd
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE,
                               style | win32con.WS_EX_LAYERED
                               | win32con.WS_EX_TRANSPARENT)

    def _band_window(self, index: int) -> tk.Toplevel:
        """The pool's index-th translucent band, created on first use."""
        while len(self._bands) <= index:
            band = tk.Toplevel(self.root)
            band.overrideredirect(True)
            band.configure(bg=BAND_COLOUR)
            band.attributes("-alpha", BAND_ALPHA)
            band.attributes("-topmost", True)
            band.withdraw()
            band.update_idletasks()
            self._set_click_through(band)
            self._bands.append(band)
        return self._bands[index]

    def _target_rect(self) -> Optional[Tuple[int, int, int, int]]:
        """The on-screen rectangle the capture frame corresponds to.

        There are two candidates and the difference put every caption half a
        line low. GetWindowRect includes Win10's invisible resize borders;
        DWM's extended frame bounds are the visible window. Measured on one
        window at 150% scale: rect 1039x799 against a frame of 1539x1189 --
        the frame is the DWM bounds, and mapping it onto the rect makes the
        scale 1.3% small, which is ten pixels by the bottom of the screen,
        cutting through the dialog's glyphs.

        Which one the frame matches is not assumed: both are computed and the
        one agreeing with the frame's size wins, so a capture backend that
        includes the borders keeps working too.
        """
        try:
            if not win32gui.IsWindow(self.hwnd) or not win32gui.IsWindowVisible(self.hwnd):
                return None
            rect = win32gui.GetWindowRect(self.hwnd)
        except Exception:
            return None
        if not self._frame_size:
            return rect
        try:
            bounds = wintypes.RECT()
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(self.hwnd), 9,          # EXTENDED_FRAME_BOUNDS
                ctypes.byref(bounds), ctypes.sizeof(bounds))
            # DWM answers in physical pixels regardless of this process's
            # awareness; Tk geometry wants the virtual ones GetWindowRect
            # speaks. The desktop DC knows both resolutions.
            hdc = win32gui.GetDC(0)
            try:
                horz = ctypes.windll.gdi32.GetDeviceCaps(hdc, 8)
                full = ctypes.windll.gdi32.GetDeviceCaps(hdc, 118)
            finally:
                win32gui.ReleaseDC(0, hdc)
            scale = (full / horz) if horz else 1.0
            fw, fh = self._frame_size
            if (abs((bounds.right - bounds.left) - fw) <= 2
                    and abs((bounds.bottom - bounds.top) - fh) <= 2):
                # ponytail: primary-monitor scale; a game on a second monitor
                # with a different factor would need per-monitor DPI here.
                return (round(bounds.left / scale), round(bounds.top / scale),
                        round(bounds.right / scale),
                        round(bounds.bottom / scale))
        except Exception:
            pass
        return rect

    def _follow_target(self) -> bool:
        """Match the overlay to the target window. True if it moved or resized.

        Compared against where the window actually is, never against a memory
        of what was requested. The first geometry() goes out before an
        override-redirect window is mapped and Windows drops it; a cache then
        says "already there" on every later pass and the request is never
        made again. Measured: the overlay sat at its default rectangle for an
        entire session, every caption offset by the difference, while the
        game's rectangle never changed and so never disturbed the cache. The
        OS is the only party that knows where the window is; ask it.
        """
        rect = self._target_rect()
        if rect is None:
            return False
        left, top, right, bottom = rect
        width, height = max(1, right - left), max(1, bottom - top)
        # Win32, not winfo: for an override-redirect window Tk's winfo echoes
        # the geometry that was REQUESTED, not where the window is. When the
        # first request is dropped -- launched under `main.py run` it goes
        # out before the window is mapped, and Windows drops it -- winfo
        # reports the intended rectangle while the window sits at Tk's
        # default, and a check built on winfo believes everything is fine.
        # Verified three ways on a live overlay: Tk said 76,76 while both
        # this process's GetWindowRect and another process's agreed on the
        # real, wrong place.
        try:
            actual = win32gui.GetWindowRect(self._os_hwnd)
        except Exception:
            actual = None
        if actual == rect:
            self._geometry = (left, top, width, height)
            return False
        moved = self._geometry != (left, top, width, height)
        self._geometry = (left, top, width, height)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        return moved

    def _scale(self) -> float:
        """Capture-frame pixels per window pixel.

        The capture buffer is in physical pixels while Tk and GetWindowRect
        both work in the logical pixels of this DPI-unaware process, so on a
        scaled display the two differ by exactly this factor. Deriving it from
        the two sizes avoids having to ask Windows about DPI at all.
        """
        if not self._frame_size or not self._geometry:
            return 1.0
        return max(0.05, self._frame_size[0] / max(1, self._geometry[2]))

    # -- content ----------------------------------------------------------
    def push(self, line: Optional[Line]) -> None:
        """Thread-safe. `None` asks the overlay to close."""
        self.queue.put(line)

    def set_frame_size(self, width: int, height: int) -> None:
        self._frame_size = (width, height)

    def _drain(self) -> None:
        changed = False
        try:
            while True:
                line = self.queue.get_nowait()
                if line is None:
                    self.close()
                    return
                self._absorb(line)
                changed = True
        except queue.Empty:
            pass
        if changed:
            self._redraw()
        if not self._closed:
            self.root.after(40, self._drain)

    def _absorb(self, line: Line) -> None:
        """Replace whatever this region was showing with its new placements.

        A line carrying no placements is how a region says its text is gone,
        and it must be able to say that: otherwise closing a menu leaves its
        captions floating over whatever the game shows next.
        """
        self._placements = [p for p in self._placements if p.region != line.region]
        now = time.monotonic()
        for placement in line.placements:
            placement.region = line.region
            self._shown_at[id(placement)] = now
            self._placements.append(placement)

    def _expire(self) -> bool:
        """Drop captions the pipeline has stopped refreshing.

        Belt and braces for the case the pipeline never gets to say the text
        is gone -- a scene change during a slow translation, a region that
        stops triggering. A caption stuck over live gameplay is worse than a
        missing one, so anything past its lifetime goes.
        """
        if self.cfg.label_ttl_s <= 0:
            return False
        cutoff = time.monotonic() - self.cfg.label_ttl_s
        keep = [p for p in self._placements
                if self._shown_at.get(id(p), 0.0) > cutoff]
        if len(keep) == len(self._placements):
            return False
        self._placements = keep
        return True

    def _track(self) -> None:
        if self._closed:
            return
        moved = self._follow_target()
        if self._expire() or moved:
            self._redraw()
        if self._target_rect() is None:
            self.canvas.delete("all")
            for win in self._bands:
                win.withdraw()
        self.root.after(200, self._track)

    # -- drawing ----------------------------------------------------------
    def _paint_backing(self, px0: float, py0: float, px1: float, py1: float,
                       placement: Placement, x0: float, x1: float,
                       line: float, fallback: str, tint: float = 0.0) -> None:
        """Fill a rectangle with the colours sampled behind the original,
        lowered under everything already drawn.

        One colour paints flat; several blend as a gradient -- adjacent
        blocks of the sampled colours had visible seams, which is exactly
        the patch look this exists to avoid. The colour stops stay anchored
        to the original box (x0..x1) the colours were sampled from: the
        rectangle may be wider, and stretching the colours over it shifted
        them sideways. Past the ends the gradient extends flat.

        `tint` pre-darkens every fill toward the band colour by the band's
        own alpha. The canvas sits ABOVE the translucent band window -- the
        text must -- so a patch on it is never dimmed by the veil; painted
        at full strength it glowed inside the grey like a second plate. Lit
        the way the veil would lit it, it disappears into the sheet.
        """
        colours = ([_no_key(c) for c in placement.bg.split()]
                   if placement.bg else [])
        if tint > 0.0:
            colours = [_lerp_colour(c, "#232323", tint) for c in colours]
        if len(colours) < 2:
            fill = colours[0] if colours else fallback
            rect = self.canvas.create_rectangle(
                px0, py0, px1, py1, fill=fill, outline="")
            self.canvas.tag_lower(rect)
            return
        seg = (x1 - x0) / len(colours)
        stops = [x0 + seg * (i + 0.5) for i in range(len(colours))]
        step = max(4.0, line / 4)
        x = px0
        while x < px1:
            mid = min(max(x + step / 2, stops[0]), stops[-1])
            i = min(len(stops) - 2,
                    max(0, int((mid - stops[0]) / max(1e-6, seg))))
            t = (mid - stops[i]) / max(1e-6, stops[i + 1] - stops[i])
            fill = _lerp_colour(colours[i], colours[i + 1],
                                min(1.0, max(0.0, t)))
            rect = self.canvas.create_rectangle(
                x, py0, min(x + step, px1), py1, fill=fill, outline="")
            self.canvas.tag_lower(rect)
            x += step

    def _redraw(self) -> None:
        self.canvas.delete("all")
        if not self._geometry:
            return
        scale = self._scale()
        pad_x, pad_y = 6, 2
        drawable = [p for p in self._placements if p.text]
        # Where the replaced text sits, so a caption can tell whether stepping
        # above it would land on the line before rather than on background.
        boxes = [(p.box[0] / scale, p.box[1] / scale,
                  p.box[2] / scale, p.box[3] / scale) for p in drawable]
        # Three styles, each doing one thing:
        #   plate    -- a solid backing in the sampled colours; clearest.
        #   seamless -- a flat dark translucent band over the line, sized
        #               with air around the text. ("outline" is the same
        #               style under its old config name.)
        #   hover    -- the caption above the original, both visible, for
        #               reading along with the Japanese.
        screen_style = self.cfg.label_style
        if screen_style == "outline":
            screen_style = "seamless"
        # One decision for the whole screen, not one per caption. Deciding per
        # caption made a panel where names have space above them and their
        # descriptions do not come out half hovering and half covered, which
        # reads as two different subtitle modes running at once. If any of
        # them has nowhere to step, the whole screen falls back to seamless.
        if screen_style == "hover" and not (
                self.cfg.placement == "over"
                and all(can_step_above(box, boxes[:i] + boxes[i + 1:])
                        for i, box in enumerate(boxes))):
            screen_style = "seamless"
        # Every seamless band this pass, in canvas coordinates. Overlapping
        # ones merge into one window afterwards; distant ones stay separate
        # windows -- a union across the screen is a veil, not a band.
        band_rects: List[Tuple[float, float, float, float]] = []
        for index, placement in enumerate(drawable):
            x0, y0, x1, y1 = boxes[index]
            height = max(8.0, y1 - y0)

            # Match one line of the replaced text, not the height of the whole
            # box: a caption covering a six-line passage must not be drawn six
            # lines tall.
            line = (placement.line_height / scale) if placement.line_height else height
            size = max(9, min(28, int(max(8.0, line) * 0.62)))
            placement_mode = self.cfg.placement
            style = screen_style
            backing = ""            # solid; a stipple name makes it half-tone
            if style == "hover":
                # Both texts on purpose: the caption rides above its line.
                placement_mode = "above"
            elif style == "seamless":
                # A flat dark translucent band over the line, per the
                # reference screenshot the user supplied. The sampled-colour
                # erase was tried and rejected in favour of this.
                backing = "gray50"
            if placement_mode == "above":
                y0, y1 = y0 - height - 4, y0 - 4
            elif placement_mode == "below":
                y0, y1 = y1 + 4, y1 + height + 4

            font = (self.cfg.font_family, size)
            # The translation replaces the original IN PLACE, its left edge
            # on the original's left edge -- the reference effect. Every
            # style lays text out the same way; a fixed-width frame with the
            # text pushed 2H right was tried and rejected against it.
            if height > line * 1.6:
                # The original spans several rows, so its translation is
                # a paragraph too: wrap it inside the same width and
                # start at the top, the way the text it replaces was
                # laid out.
                anchor, cx, cy = "nw", x0 + pad_x, y0
                wrap = int(max(x1 - x0, size * 4))
            else:
                # One row stays one row. Wrapping it to the space
                # remaining would drop the overflow onto the row below,
                # which is another caption; a line that does not fit is
                # moved left instead.
                anchor, cx, cy = "w", x0 + pad_x, (y0 + y1) / 2
                wrap = 0

            text_id = self.canvas.create_text(
                cx, cy, anchor=anchor, text=placement.text, width=wrap,
                fill=self.cfg.fg, font=font)
            bounds = self.canvas.bbox(text_id)
            if bounds is None:
                continue
            bx0, by0, bx1, by1 = bounds
            # A translation is usually longer than the text it replaces,
            # so a line starting near the right margin runs off the
            # screen.
            limit = self._geometry[2] - pad_x
            room = limit - pad_x
            if bx1 > limit and (bx1 - bx0) > room:
                # Wider than the whole window: no amount of moving left
                # will fit it, so it has to wrap. One caption on two
                # lines inside the window beats one line half off it.
                self.canvas.itemconfigure(text_id, width=int(room))
                bounds = self.canvas.bbox(text_id)
                if bounds is None:
                    continue
                bx0, by0, bx1, by1 = bounds
                wrap = int(room)
            if bx1 > limit:
                # Fits, but not where it starts. Pull it back, never
                # past the left edge.
                shift = min(bx1 - limit, max(0.0, bx0 - pad_x))
                if shift > 0:
                    self.canvas.move(text_id, -shift, 0)
                    cx -= shift
                    bx0, bx1 = bx0 - shift, bx1 - shift

            # Backing first, lowered under the text that was drawn before it.
            # It has to cover the original, not just the translation: sizing it
            # to the text alone leaves the tail of a longer original sticking
            # out, and "システム" under "系统" reads as "系统ム".
            if backing:
                # The mask hugs the line it replaces: it covers the
                # original's own box (a little extra on the right, where
                # the recogniser drops a trailing heart or bracket) and
                # whatever the translation itself needs -- and nothing
                # more. A frame at 70% of the window, and before that one
                # sized with 2H margins, both read as a slab across the
                # scene and were rejected.
                #
                # Two layers make it. The erase strip on the canvas paints
                # the original's box in the colours sampled behind it,
                # pre-darkened to what they look like through the veil
                # (the canvas rides ABOVE the band window -- the text must
                # -- so the veil never dims this patch). The band window
                # under everything is the translucent grey itself, padded
                # just past the strip so its true transparency shows the
                # scene's texture at the rim.
                sx0 = x0 - line * 0.4
                sx1 = x1 + line * 1.2
                sy0, sy1 = y0 - line * 0.15, y1 + line * 0.15
                self._paint_backing(sx0, sy0, sx1, sy1, placement,
                                    x0, x1, line,
                                    fallback=BAND_COLOUR, tint=BAND_ALPHA)
                band_rects.append((
                    max(0.0, min(sx0, bx0) - line * 0.3),
                    max(0.0, min(sy0, by0) - line * 0.25),
                    min(float(self._geometry[2]),
                        max(sx1, bx1) + line * 0.3),
                    min(float(self._geometry[3]),
                        max(sy1, by1) + line * 0.25)))
            elif style == "plate":
                # The colours sampled from behind the original, when there
                # are any: the game's own background reads as the game having
                # been in the target language all along.
                #
                # Half a character of margin past both ends: the recogniser
                # regularly misses a trailing heart or closing bracket, its
                # box stops short, and the missed characters poked out beside
                # the caption. Plates go to the bottom of the stack, so an
                # enlarged plate can never sit on a neighbouring caption's
                # text -- overlapping plates are just background either way.
                margin = line * 0.5
                px0 = min(bx0 - pad_x, x0) - margin
                px1 = max(bx1 + pad_x, x1) + margin
                py0 = min(by0 - pad_y, y0 - line * 0.12)
                py1 = max(by1 + pad_y, y1 + line * 0.12)
                if placement.plate_box:
                    # A dialogue plate fills the region's width: the frame
                    # the user drew is the dialog box, and the tail the
                    # recogniser missed is inside it.
                    px0 = min(px0, placement.plate_box[0] / scale)
                    px1 = max(px1, placement.plate_box[2] / scale)
                self._paint_backing(px0, py0, px1, py1, placement,
                                    x0, x1, line, fallback=self.cfg.bg)

            if style in ("seamless", "hover"):
                # Tk has no text stroke, so draw the dark copy eight ways
                # around the light one. Costs nothing at these sizes and keeps
                # the translation legible over any background -- including a
                # half-tone backing, which is why this runs for that case too.
                # Drawn after the shift so the halo lands where the text is.
                halo = max(1, size // 12)
                for dx in (-halo, 0, halo):
                    for dy in (-halo, 0, halo):
                        if dx or dy:
                            self.canvas.create_text(
                                cx + dx, cy + dy, anchor=anchor,
                                text=placement.text, width=wrap,
                                fill=self.cfg.bg, font=font)
            self.canvas.tag_raise(text_id)

        # Overlapping bands become one window -- a stats panel's rows sit
        # closer than their minimum heights and separate windows there would
        # shingle -- while bands that do not touch stay apart: the whole
        # point of the pool. Repeat until stable; a merge can create a new
        # overlap.
        merged = [(r[0], max(0.0, r[1]), r[2], r[3]) for r in band_rects]
        changed = True
        while changed:
            changed = False
            for i in range(len(merged)):
                for j in range(i + 1, len(merged)):
                    a, b = merged[i], merged[j]
                    if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                        merged[i] = (min(a[0], b[0]), min(a[1], b[1]),
                                     max(a[2], b[2]), max(a[3], b[3]))
                        del merged[j]
                        changed = True
                        break
                if changed:
                    break

        # Publish for the checks, then place one translucent window under
        # each band: screen position is overlay origin plus canvas
        # coordinates. The overlay is lifted back above so the text stays on
        # top of its own backing. Instances built without __init__ in the
        # checks have no pool; they read band_rects off the canvas instead.
        self.canvas.band_rects = merged
        if getattr(self, "_bands", None) is None:
            return
        for i, (px0, py0, px1, py1) in enumerate(merged):
            win = self._band_window(i)
            left = self._geometry[0] + int(px0)
            top = self._geometry[1] + int(py0)
            width = max(1, int(px1 - px0))
            height = max(1, int(py1 - py0))
            win.geometry(f"{width}x{height}+{left}+{top}")
            win.deiconify()
        for win in self._bands[len(merged):]:
            win.withdraw()
        if merged:
            self.root.lift()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        if not self.owns_root:
            raise RuntimeError("overlay does not own the Tk loop")
        self.root.mainloop()
