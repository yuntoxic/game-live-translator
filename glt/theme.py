"""Dark theme for the control panel.

Tk's native Windows themes ('vista', 'xpnative') draw through the OS, which
means background colours on those widgets are mostly ignored. 'clam' is the
one bundled theme drawn entirely by Tk, so it is the only one that can be
recoloured properly -- everything here depends on that.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

BG = "#0f1319"        # page
CARD = "#171c25"      # section background
CARD_HI = "#212936"   # buttons, inputs
BORDER = "#2a3342"
FG = "#e8ecf2"
MUTED = "#8b93a5"
FAINT = "#5d6675"
ACCENT = "#4ea1ff"
GOLD = "#e8c07d"      # speaker names, matches the overlay
OK = "#4ecb8a"
WARN = "#ffb454"
ERR = "#ff7a7a"

UI = "Microsoft YaHei UI"   # has CJK glyphs; Tk's default does not
MONO = "Consolas"


def fonts() -> dict:
    return {
        "h1": (UI, 15, "bold"),
        "h2": (UI, 10, "bold"),
        "body": (UI, 9),
        "small": (UI, 8),
        "value": (MONO, 10),
        "log": (UI, 10),
    }


def apply(root: tk.Misc) -> ttk.Style:
    style = ttk.Style(root)
    style.theme_use("clam")
    f = fonts()

    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=FG, font=f["body"],
                    bordercolor=BORDER, focuscolor=BG)

    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=FG, font=f["body"])
    style.configure("Card.TLabel", background=CARD, foreground=FG)
    style.configure("Head.TLabel", background=CARD, foreground=FG, font=f["h2"])
    style.configure("Hint.TLabel", background=CARD, foreground=FAINT, font=f["small"])
    style.configure("Value.TLabel", background=CARD, foreground=ACCENT, font=f["value"])

    style.configure("TButton", background=CARD_HI, foreground=FG, relief="flat",
                    padding=(12, 6), borderwidth=0, font=f["body"])
    style.map("TButton",
              background=[("active", "#2c3748"), ("disabled", "#161b23")],
              foreground=[("disabled", "#4a5262")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#08111d",
                    font=f["h2"])
    style.map("Accent.TButton",
              background=[("active", "#6fb4ff"), ("disabled", "#1c2735")],
              foreground=[("disabled", "#47596e")])

    style.configure("TCombobox", fieldbackground=CARD_HI, background=CARD_HI,
                    foreground=FG, arrowcolor=MUTED, bordercolor=BORDER,
                    lightcolor=CARD_HI, darkcolor=CARD_HI, padding=5)
    # clam draws a 3D bevel from lightcolor/darkcolor, which default to near
    # white. Every styled widget has to pin both or it gets a bright edge.
    style.map("TCombobox",
              fieldbackground=[("readonly", CARD_HI)],
              background=[("readonly", CARD_HI), ("active", CARD_HI),
                          ("pressed", CARD_HI)],
              selectbackground=[("readonly", CARD_HI)],
              selectforeground=[("readonly", FG)],
              lightcolor=[("focus", ACCENT), ("!focus", CARD_HI)],
              darkcolor=[("focus", ACCENT), ("!focus", CARD_HI)],
              bordercolor=[("focus", ACCENT)],
              arrowcolor=[("active", FG)])
    root.option_add("*TCombobox*Listbox.background", CARD_HI)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#08111d")
    root.option_add("*TCombobox*Listbox.font", f["body"])

    style.configure("TEntry", fieldbackground=CARD_HI, foreground=FG,
                    bordercolor=BORDER, lightcolor=CARD_HI, darkcolor=CARD_HI,
                    insertcolor=FG, padding=5)
    style.map("TEntry", bordercolor=[("focus", ACCENT)])

    style.configure("Horizontal.TScale", background=ACCENT, troughcolor="#0a0d13",
                    bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)

    style.configure("TCheckbutton", background=CARD, foreground=FG,
                    indicatorcolor=CARD_HI, focuscolor=CARD)
    style.map("TCheckbutton",
              background=[("active", CARD)],
              indicatorcolor=[("selected", ACCENT)])

    style.configure("TNotebook", background=BG, borderwidth=0,
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                    tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                    padding=(18, 8), borderwidth=0, font=f["h2"],
                    lightcolor=BG, darkcolor=BG, bordercolor=BG)
    style.map("TNotebook.Tab",
              background=[("selected", CARD)],
              lightcolor=[("selected", CARD)],
              darkcolor=[("selected", CARD)],
              foreground=[("selected", FG), ("active", FG)])

    style.configure("TSeparator", background=BORDER)
    style.configure("Vertical.TScrollbar", background=CARD_HI, troughcolor="#0a0d13",
                    bordercolor="#0a0d13", arrowcolor=FAINT, relief="flat",
                    lightcolor=CARD_HI, darkcolor=CARD_HI, borderwidth=0,
                    width=12)
    style.map("Vertical.TScrollbar",
              background=[("active", "#2c3748"), ("pressed", ACCENT)],
              arrowcolor=[("active", FG)])

    style.configure("Horizontal.TScale", lightcolor=ACCENT, darkcolor=ACCENT,
                    bordercolor="#0a0d13")
    return style


def card(parent, **kw) -> tk.Frame:
    """A section panel: flat, slightly lighter than the page, thin border."""
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                    highlightcolor=BORDER, highlightthickness=1, **kw)


def section_title(parent, step: str, text: str, note: str = "") -> tk.Frame:
    """Numbered heading, so the panel reads as an ordered procedure."""
    row = tk.Frame(parent, bg=CARD)
    badge = tk.Label(row, text=step, bg=ACCENT, fg="#08111d",
                     font=(UI, 9, "bold"), width=3)
    badge.pack(side="left", ipady=1)
    tk.Label(row, text=text, bg=CARD, fg=FG, font=(UI, 10, "bold")).pack(
        side="left", padx=(8, 0))
    if note:
        tk.Label(row, text=note, bg=CARD, fg=FAINT, font=(UI, 8)).pack(
            side="left", padx=(8, 0), pady=(2, 0))
    return row
