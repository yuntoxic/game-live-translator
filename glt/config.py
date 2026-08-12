"""Config loading. Regions are stored as fractions of the frame, never pixels.

A capture card feed can change resolution and an OBS projector gets resized
all the time; normalised boxes survive both, so a config picked at 1080p still
works when the window is dragged to a 4K monitor.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .ocr import OcrConfig
from .translate import TranslateConfig
from .trigger import TriggerConfig


@dataclass
class Region:
    name: str = "subtitle"
    box: Tuple[float, float, float, float] = (0.10, 0.76, 0.90, 0.96)
    lang: str = "ja"
    role: str = "dialogue"      # dialogue | name | choice | info
    translate: bool = True
    # Whether the region's lines are independent items or one running text.
    # Dialogue is prose and must be translated whole; a menu is a list of
    # separate labels that each need their own translation and position.
    per_line: Optional[bool] = None
    trigger: Optional[dict] = None   # per-region TriggerConfig overrides
    ocr: Optional[dict] = None       # per-region OcrConfig overrides

    def pixels(self, width: int, height: int) -> Tuple[int, int, int, int]:
        x0, y0, x1, y1 = self.box
        px0, px1 = sorted((int(x0 * width), int(x1 * width)))
        py0, py1 = sorted((int(y0 * height), int(y1 * height)))
        px0, py0 = max(0, px0), max(0, py0)
        px1, py1 = min(width, max(px0 + 1, px1)), min(height, max(py0 + 1, py1))
        return px0, py0, px1, py1


@dataclass
class SourceConfig:
    window_title: str = ""       # substring match, e.g. "Projector"
    window_hwnd: Optional[int] = None
    max_fps: int = 20


@dataclass
class OverlayConfig:
    enabled: bool = True
    # "inplace" draws each translation over the words it replaces; "bar" is a
    # fixed caption strip at the bottom. In-place is the default because a bar
    # cannot say which of several on-screen texts it is translating.
    mode: str = "inplace"           # inplace | bar
    placement: str = "over"         # over | above | below  (inplace only)
    # "plate" backs each translation with a small solid rectangle, which hides
    # the original and always reads. "outline" draws the text with a dark
    # halo and no rectangle: less in the way, but the original shows through.
    label_style: str = "plate"      # plate | outline
    # Seconds a caption stays up without being refreshed; 0 disables it.
    #
    # Off by default, because it caused the very problem it was meant to
    # prevent. A menu that sits still never re-triggers OCR, so nothing
    # refreshes the caption and the whole screen's translation vanished
    # mid-read. Regions already announce when their text goes blank or stops
    # being recognised, which covers the stale-caption case properly.
    label_ttl_s: float = 0.0
    x: int = 120
    y: int = 820
    width: int = 900
    font_family: str = "Microsoft YaHei UI"
    font_size: int = 24
    src_font_size: int = 13
    show_source: bool = True
    opacity: float = 0.88
    bg: str = "#0b0e14"
    fg: str = "#f5f5f2"
    src_fg: str = "#8b93a5"
    name_fg: str = "#e8c07d"
    click_through: bool = False
    always_on_top: bool = True


@dataclass
class AppConfig:
    source: SourceConfig = field(default_factory=SourceConfig)
    regions: List[Region] = field(default_factory=lambda: [Region()])
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    translate: TranslateConfig = field(default_factory=TranslateConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    log_file: str = "session.jsonl"

    def region_trigger(self, region: Region) -> TriggerConfig:
        return TriggerConfig(**{**asdict(self.trigger), **(region.trigger or {})})

    def region_ocr(self, region: Region) -> OcrConfig:
        return OcrConfig(**{**asdict(self.ocr), **(region.ocr or {})})

    @staticmethod
    def region_per_line(region: Region) -> bool:
        if region.per_line is not None:
            return region.per_line
        return region.role != "dialogue"


def _merge_term_files(section: dict, base_dir: Path,
                      file_key: str, dict_key: str) -> None:
    names = section.get(file_key)
    if not names:
        return
    if isinstance(names, str):
        names = [names]

    terms: dict = {}
    for name in names:
        path = Path(name)
        if not path.is_absolute():
            path = base_dir / path
        if not path.exists():
            raise FileNotFoundError(f"{file_key} not found: {path}")
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        terms.update({k: v for k, v in loaded.items()
                      if not k.startswith("//") and isinstance(v, str)})
    terms.update(section.get(dict_key) or {})
    section[dict_key] = terms


def _merge_glossary_file(section: Optional[dict], base_dir: Path) -> None:
    """Fold glossary and fixes files into their inline dicts.

    Glossaries are worth sharing - they are the accumulated result of playing
    a game and correcting it - so they live in their own files, while an
    inline entry stays the last word for one-off overrides.

    A list stacks, later files winning, which is what makes a shared common
    glossary useful: the terms every Japanese game words the same way
    (ステータス, 装備, 持ち物 - the ones generic engines reliably get wrong)
    go in one file that never needs changing, and a game only has to supply
    what it words differently. Fixes follow the same rule for the same
    reason: they are per-game corrections that outlive any one config.
    """
    if not isinstance(section, dict):
        return
    _merge_term_files(section, base_dir, "glossary_file", "glossary")
    _merge_term_files(section, base_dir, "fixes_file", "fixes")


def _merge(cls, data: Optional[dict]):
    """Build a dataclass from partial JSON, ignoring keys we do not know."""
    if not data:
        return cls()
    fields = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in data.items() if k in fields})


def load(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"config not found: {path}\n"
            f"Copy config.example.json to {path.name} and edit it, or run:\n"
            f"    python main.py pick --window \"<part of the window title>\"")
    # utf-8-sig, not utf-8: Notepad and PowerShell's Set-Content both write a
    # BOM, and json.loads rejects it outright.
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    _merge_glossary_file(raw.get("translate"), path.parent)

    cfg = AppConfig(
        source=_merge(SourceConfig, raw.get("source")),
        trigger=_merge(TriggerConfig, raw.get("trigger")),
        ocr=_merge(OcrConfig, raw.get("ocr")),
        translate=_merge(TranslateConfig, raw.get("translate")),
        overlay=_merge(OverlayConfig, raw.get("overlay")),
        log_file=raw.get("log_file", "session.jsonl"),
    )
    regions = raw.get("regions") or []
    if regions:
        cfg.regions = [_merge(Region, r) for r in regions]
    for r in cfg.regions:
        r.box = tuple(float(v) for v in r.box)  # JSON gives lists
    return cfg


# Fields of `translate` that nothing in the app can edit. Writing them back
# is how a config quietly stops tracking the program:
#
# * `glossary` -- load() merges every glossary_file into it, so writing it out
#   pins those terms inline where they outrank the files they came from.
#   Editing a glossary then does nothing, and the last game's terms follow the
#   config into the next game.
# * `prompt` -- absent from the file it comes from the default, and writing
#   that default out freezes it. Every later improvement to the prompt would
#   reach new users only.
#
# So the file stays authoritative: a value present on disk is kept as it was,
# and one that was never there is left out rather than materialised.
# `fixes` is merged from fixes_file exactly as the glossary is, and freezing
# it inline would break its files the same way.
_NOT_OURS_TO_WRITE = ("glossary", "glossary_file", "prompt",
                      "fixes", "fixes_file")


def save(cfg: AppConfig, path: str | Path) -> None:
    """Write the config without claiming the fields the app does not edit."""
    path = Path(path)
    data = asdict(cfg)
    previous = {}
    if path.exists():
        try:
            previous = json.loads(
                path.read_text(encoding="utf-8-sig")).get("translate", {})
        except (ValueError, OSError):
            previous = {}
    # Outside the exists() check on purpose: a config written from scratch
    # would otherwise be born with the default prompt baked in, frozen from
    # its first day. config.example.json is where these are documented.
    for field in _NOT_OURS_TO_WRITE:
        if field in previous:
            data["translate"][field] = previous[field]
        else:
            data["translate"].pop(field, None)
    # Same rule one level down: the panel edits a backend's model and
    # address, but fallback_model has no widget, so the file's value is the
    # only one there is and a save must not erase it.
    for section in ("openai", "anthropic"):
        stored = previous.get(section)
        if isinstance(stored, dict) and "fallback_model" in stored:
            data["translate"].setdefault(section, {})["fallback_model"] = \
                stored["fallback_model"]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")
