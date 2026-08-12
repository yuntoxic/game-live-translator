"""A stand-in for a real game feed, for testing without a capture card.

Draws a window that behaves like the hard case: a background that animates
continuously while subtitles appear, hold, and change. If the trigger is
working, OCR fires once per subtitle rather than once per animation frame.

    python tools/fake_game.py                 typewriter reveal, 3s per line
    python tools/fake_game.py --instant       whole line at once
    python tools/fake_game.py --menu          scattered UI labels, for testing
                                              the in-place overlay
"""

from __future__ import annotations

import argparse
import random
import tkinter as tk

LINES = [
    ("アリス", "彼女は黙って窓の外を見つめていた。"),
    ("青子", "この屋敷には、まだ何かが残っている。"),
    ("アリス", "夜が明ける前に、答えを見つけなさい。"),
    ("青子", "待って。その先は危険だわ。"),
    ("", "――遠くで、鐘が鳴った。"),
]

W, H = 1280, 720

# Labels scattered the way a real menu scatters them: several independent
# items on screen at once, which is exactly the case a single subtitle bar
# cannot caption unambiguously.
MENU = [
    (90, 90, "ステータス", 22),
    (90, 150, "攻撃力", 18),
    (300, 150, "防御力", 18),
    (510, 150, "素早さ", 18),
    (90, 300, "持ち物", 22),
    (90, 360, "医療キット", 18),
    (90, 410, "魔力の残り火", 18),
    (700, 300, "装備", 22),
    (700, 360, "鐵の剣", 18),
    (700, 410, "旅人の外套", 18),
    (960, 620, "戻る", 20),
    (90, 620, "決定", 20),
]


def menu_screen() -> None:
    """A static menu screen with text in many places."""
    root = tk.Tk()
    root.title("FakeGame Projector")
    root.geometry(f"{W}x{H}")
    canvas = tk.Canvas(root, width=W, height=H, bg="#141821", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_rectangle(60, 60, W - 60, H - 60, outline="#2b3446", width=2)
    for x, y, text, size in MENU:
        canvas.create_text(x, y, text=text, anchor="nw", fill="#f0f0ec",
                           font=("Yu Gothic UI", size))
    root.mainloop()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instant", action="store_true", help="no typewriter reveal")
    ap.add_argument("--hold", type=int, default=3000, help="ms each line stays up")
    ap.add_argument("--menu", action="store_true",
                    help="scattered UI labels instead of subtitles")
    args = ap.parse_args()

    if args.menu:
        menu_screen()
        return

    root = tk.Tk()
    root.title("FakeGame Projector")
    root.geometry(f"{W}x{H}")
    root.configure(bg="#000000")
    canvas = tk.Canvas(root, width=W, height=H, bg="#10131a", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    # Background that never stops moving - the thing a naive interval-based
    # trigger would happily OCR sixty times a second.
    blobs = []
    for _ in range(14):
        x, y = random.randint(0, W), random.randint(0, 420)
        r = random.randint(30, 90)
        colour = random.choice(["#1b2436", "#25304a", "#161d2c", "#2d3a55"])
        blobs.append([canvas.create_oval(x - r, y - r, x + r, y + r,
                                         fill=colour, outline=""),
                      random.uniform(-2.5, 2.5), random.uniform(-1.5, 1.5)])

    canvas.create_rectangle(0, 470, W, H, fill="#05070c", outline="")
    name_item = canvas.create_text(96, 508, text="", anchor="w", fill="#e8c07d",
                                   font=("Yu Gothic UI", 20, "bold"))
    text_item = canvas.create_text(96, 580, text="", anchor="nw", fill="#f2f2ee",
                                   font=("Yu Gothic UI", 26), width=W - 190)

    state = {"i": 0, "shown": 0}

    def animate() -> None:
        for blob in blobs:
            item, dx, dy = blob
            canvas.move(item, dx, dy)
            x0, y0, x1, y1 = canvas.coords(item)
            if x0 < -200 or x1 > W + 200:
                blob[1] = -dx
            if y0 < -200 or y1 > 520:
                blob[2] = -dy
        root.after(33, animate)

    def advance() -> None:
        state["i"] = (state["i"] + 1) % len(LINES)
        state["shown"] = 0
        speaker, _ = LINES[state["i"]]
        canvas.itemconfig(name_item, text=speaker)
        canvas.itemconfig(text_item, text="")
        reveal()

    def reveal() -> None:
        _, line = LINES[state["i"]]
        if args.instant:
            canvas.itemconfig(text_item, text=line)
            root.after(args.hold, advance)
            return
        state["shown"] += 1
        canvas.itemconfig(text_item, text=line[:state["shown"]])
        if state["shown"] < len(line):
            root.after(45, reveal)
        else:
            root.after(args.hold, advance)

    animate()
    advance()
    root.mainloop()


if __name__ == "__main__":
    main()
