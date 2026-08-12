"""Control panel: pick a window, draw regions, tune, start, watch.

Tkinter rather than a nicer toolkit for one hard reason: the subtitle overlay
is already Tk, a process gets exactly one Tk root, and every widget has to be
touched from the thread that owns the loop. A second toolkit would mean a
second event loop and no way to parent the overlay. So this window owns the
root and the overlay becomes a Toplevel of it.

Worker threads never touch a widget. Lines arrive on a queue that a periodic
`after()` drains, the same way the overlay does it.
"""

from __future__ import annotations

import base64
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import messagebox, ttk
from typing import List, Optional

import cv2

from . import config as cfgmod
from . import theme
from .capture import WindowInfo, grab_one_frame, list_windows
from .config import AppConfig, Region
from .helptext import HELP
from .overlay import Line
from .pipeline import Pipeline

ROLES = ["dialogue", "name", "choice", "info"]
ROLE_LABEL = {"dialogue": "对话正文", "name": "说话人姓名",
              "choice": "选项", "info": "其他信息"}
# The config keeps the English values -- they are what the file and the docs
# use -- but nothing on screen should say `dialogue` to someone picking a
# region. The dropdown and the region list show the label.
ROLE_LABELS = [ROLE_LABEL[r] for r in ROLES]
ROLE_BY_LABEL = {v: k for k, v in ROLE_LABEL.items()}

# Windows OCR language tags, with the name of the language rather than the tag.
LANGS = ["ja", "en-US", "zh-Hans-CN", "ko"]
LANG_LABEL = {"ja": "日文 (ja)", "en-US": "英文 (en-US)",
              "zh-Hans-CN": "简体中文 (zh-Hans-CN)", "ko": "韩文 (ko)"}
LANG_BY_LABEL = {v: k for k, v in LANG_LABEL.items()}
BACKENDS = ["none", "google", "openai", "anthropic", "deepl"]
BACKEND_NOTE = {
    "none": "只识别不翻译，用来调区域和参数",
    "google": "免密钥，开箱即用，但没有上下文",
    "openai": "大模型，带上下文，游戏台词推荐",
    "anthropic": "大模型，带上下文，游戏台词推荐",
    "deepl": "DeepL API，需要密钥",
}
TARGETS = ["zh-CN", "zh-TW", "en", "ja", "ko"]
# One choice, named for what the player sees. Two dropdowns (方式 x 样式)
# grew conditional behaviours nobody could predict from the labels.
DISPLAY_OPTIONS = {
    "半透明字幕（推荐）": ("inplace", "seamless"),
    "衬底色块（实心底）": ("inplace", "plate"),
    "悬浮对照（原文+译文）": ("inplace", "hover"),
    "底部字幕条": ("bar", "plate"),
}
DISPLAY_NOTE = {
    "半透明字幕（推荐）": "译文原位替换原文、左端对齐，深灰半透明遮罩只盖住"
                         "原文那一段，画面其余部分不动",
    "衬底色块（实心底）": "译文下面垫实心底色（颜色取自画面），任何背景上都最清晰，"
                         "但会盖住底下那一条画面",
    "悬浮对照（原文+译文）": "译文悬浮在原文上方，两种语言都看得见，适合边玩边学。"
                             "文字太密没地方悬浮时自动退回半透明字幕",
    "底部字幕条": "所有译文集中到一条可拖动的字幕条上，对话为主的游戏读起来连贯，"
                 "但会一直占着一块画面",
}


def _display_label(mode: str, style: str) -> str:
    if mode == "bar":
        return "底部字幕条"
    if style in ("seamless", "outline"):    # outline: the style's old name
        return "半透明字幕（推荐）"
    if style == "hover":
        return "悬浮对照（原文+译文）"
    return "衬底色块（实心底）"

# Marks a background slot as still running. Not None: a task is allowed to
# return None, and that must not read as "not finished yet".
_PENDING = object()


def _diagnose(exc) -> str:
    """Say what to go and check, reading the body before the status code.

    Relay gateways are careless with status codes -- one answers "unknown
    provider for model X" with a 502, which by the code alone reads as a dead
    upstream and sends you to look at the wrong machine entirely. The body
    says what actually happened, so it wins wherever it is specific.
    """
    body = getattr(exc, "body", "") or ""
    lowered = body.lower()

    if any(word in lowered for word in
           ("unknown provider", "no provider", "model not found",
            "does not exist", "unsupported model", "invalid model",
            "no such model")):
        return ("网关不认识你填的模型名——它本身是通的，只是不提供这个模型。"
                "点「拉取模型」看它到底有哪些，然后从下拉框里选一个。")
    if any(word in lowered for word in
           ("quota", "balance", "insufficient", "exceeded", "欠费", "余额")):
        return "额度或余额用完了，去网关那边充值或换个令牌。"
    if any(word in lowered for word in
           ("api key", "unauthorized", "invalid token", "authentication")):
        return ("令牌不对。反代通常签发它自己的令牌，"
                "而不是接受上游供应商的原始密钥——确认填的是反代给你的那一个。")

    hints = {
        401: "密钥不对或已失效。检查有没有粘全、有没有多余空格。",
        403: "密钥有效但没有这个模型或这个接口的权限。",
        404: "地址或模型名不对。OpenAI 兼容的地址通常要以 /v1 结尾，"
             "而且模型名要用「拉取模型」确认存在。",
        429: "被限流了，或者余额/额度用完了。",
        500: "服务端自己出错了，跟你的配置无关，过一会儿再试。",
        502: "网关到上游的连接失败——请求确实打到了服务器，但它后面那一层没响应。"
             "这几乎总是服务端的事：中转站挂了、上游供应商不通、或者地址指向了"
             "一个不提供这个接口的主机。先确认地址填对（含 /v1），再问网关方。",
        503: "服务暂时不可用，通常是过载或维护中。",
        504: "网关等上游超时了。上游可能很慢或已经挂了。",
    }
    return hints.get(getattr(exc, "status", None), "")


def _set_user_env(name: str, value: str) -> None:
    """Persist a user-level environment variable and tell Windows about it.

    Without the broadcast the value only appears in processes started after
    the next sign-in, which would make the panel look like it had not saved.
    """
    import ctypes
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 5000, None)
BOX_COLOURS = {"dialogue": theme.ACCENT, "name": theme.GOLD,
               "choice": theme.OK, "info": "#b98cff"}


# --------------------------------------------------------------------------
# Region editor
# --------------------------------------------------------------------------
class RegionEditor(tk.Toplevel):
    """Draw region boxes straight onto a captured frame.

    Boxes are held as fractions of the frame the whole way through, so the
    result stays correct if the window is later resized or the feed changes
    resolution.
    """

    def __init__(self, master, hwnd: int, regions: List[Region], default_lang: str):
        super().__init__(master, bg=theme.BG)
        self.title("编辑区域")
        self.transient(master)
        self.grab_set()

        self.hwnd = hwnd
        self.result: Optional[List[Region]] = None
        self.regions: List[Region] = [Region(**asdict(r)) for r in regions]
        self.default_lang = default_lang
        self.selected: Optional[int] = None
        self._drag_start: Optional[tuple] = None
        self._temp_rect = None
        self._photo = None
        self._loading = False
        self.scale = 1.0
        self.frame_w = self.frame_h = 1

        bar = tk.Frame(self, bg=theme.CARD)
        bar.pack(fill="x")
        tk.Label(bar, text="在画面上拖动画框", bg=theme.CARD, fg=theme.FG,
                 font=(theme.UI, 10, "bold")).pack(side="left", padx=12, pady=8)
        tk.Label(bar, text="只框文字所在的那一条，框越小越准越快 · 点已有的框可以选中修改 · Del 删除",
                 bg=theme.CARD, fg=theme.FAINT, font=(theme.UI, 8)).pack(
            side="left", pady=8)

        body = tk.Frame(self, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=10, pady=10)

        self.canvas = tk.Canvas(body, bg="#0a0d13", highlightthickness=1,
                                highlightbackground=theme.BORDER, cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        side = tk.Frame(body, bg=theme.BG)
        side.grid(row=0, column=1, sticky="ns", padx=(10, 0))

        tk.Label(side, text="已画的区域", bg=theme.BG, fg=theme.FG,
                 font=(theme.UI, 9, "bold")).pack(anchor="w")
        self.listbox = tk.Listbox(side, width=26, height=7, exportselection=False,
                                  bg=theme.CARD_HI, fg=theme.FG, relief="flat",
                                  highlightthickness=1, font=(theme.UI, 9),
                                  highlightbackground=theme.BORDER,
                                  selectbackground=theme.ACCENT,
                                  selectforeground="#08111d")
        self.listbox.pack(pady=(4, 10), fill="x")
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        panel = theme.card(side)
        panel.pack(fill="x")
        inner = tk.Frame(panel, bg=theme.CARD)
        inner.pack(fill="x", padx=10, pady=10)
        inner.columnconfigure(1, weight=1)

        self.v_name = tk.StringVar()
        self.v_role = tk.StringVar()
        self.v_lang = tk.StringVar()
        self.v_translate = tk.BooleanVar(value=True)

        widgets = [
            ("名称", ttk.Entry(inner, textvariable=self.v_name, width=15)),
            ("类型", ttk.Combobox(inner, textvariable=self.v_role,
                                  values=ROLE_LABELS, width=12,
                                  state="readonly")),
            ("识别语言", ttk.Combobox(inner, textvariable=self.v_lang,
                                    values=list(LANG_LABEL.values()),
                                    width=18)),
        ]
        for row, (label, widget) in enumerate(widgets):
            ttk.Label(inner, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=3)
            widget.grid(row=row, column=1, sticky="we", pady=3, padx=(8, 0))
        self.lbl_role_note = ttk.Label(inner, text="", style="Hint.TLabel",
                                       wraplength=190)
        self.lbl_role_note.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(inner, text="翻译这块", variable=self.v_translate).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        for var in (self.v_name, self.v_role, self.v_lang):
            var.trace_add("write", lambda *_: self._apply_fields())
        self.v_translate.trace_add("write", lambda *_: self._apply_fields())

        ttk.Button(side, text="自动找文字", style="Accent.TButton",
                   command=self._autodetect).pack(fill="x", pady=(12, 4))
        ttk.Button(side, text="删除选中 (Del)", command=self._delete).pack(
            fill="x", pady=(4, 4))
        ttk.Button(side, text="重新截图", command=self._reload_frame).pack(fill="x")

        ttk.Label(side, wraplength=200, foreground=theme.FAINT, background=theme.BG,
                  font=(theme.UI, 8), text=(
                      "换游戏一定要重新框。框存的是相对比例，"
                      "窗口缩放或换分辨率都不用重框。")
                  ).pack(pady=(14, 0), anchor="w")

        buttons = tk.Frame(self, bg=theme.BG)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="确定", style="Accent.TButton",
                   command=self._ok).pack(side="right")
        ttk.Button(buttons, text="取消", command=self._cancel).pack(
            side="right", padx=8)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Delete>", lambda _e: self._delete())
        self.bind("<Escape>", lambda _e: self._cancel())

        self._reload_frame()
        self._refresh_list()

    # -- frame ------------------------------------------------------------
    def _reload_frame(self) -> None:
        frame = grab_one_frame(self.hwnd)
        if frame is None:
            messagebox.showerror(
                "抓不到画面",
                "没有拿到帧。窗口可能被最小化了，或者游戏在独占全屏——"
                "切成窗口化 / 无边框窗口再试。", parent=self)
            return
        self.frame_h, self.frame_w = frame.shape[:2]
        self.scale = min(1.0, 1180 / self.frame_w, 610 / self.frame_h)
        shown = cv2.resize(frame, None, fx=self.scale, fy=self.scale,
                           interpolation=cv2.INTER_AREA) if self.scale < 1.0 else frame
        # Tk 8.6 reads PNG itself, and OpenCV is already here to write one, so
        # the one thing Pillow was doing costs two lines without it. Worth the
        # swap on its own, and it also unblocks building a standalone exe:
        # PyInstaller cannot scan PIL/Image.py under Python 3.10.0, whose dis
        # module has a bug fixed in 3.10.1.
        ok, buf = cv2.imencode(".png", cv2.cvtColor(shown, cv2.COLOR_BGRA2BGR))
        if not ok:
            messagebox.showerror("截图失败", "画面无法编码。", parent=self)
            return
        self._photo = tk.PhotoImage(          # must outlive the canvas item
            data=base64.b64encode(buf).decode("ascii"))
        self.canvas.config(width=self._photo.width(), height=self._photo.height())
        self._redraw()

    def _to_canvas(self, region: Region) -> tuple:
        x0, y0, x1, y1 = region.pixels(self.frame_w, self.frame_h)
        s = self.scale
        return x0 * s, y0 * s, x1 * s, y1 * s

    def _redraw(self) -> None:
        self.canvas.delete("all")
        if self._photo is not None:
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        for i, region in enumerate(self.regions):
            x0, y0, x1, y1 = self._to_canvas(region)
            active = i == self.selected
            colour = BOX_COLOURS.get(region.role, theme.ACCENT)
            self.canvas.create_rectangle(x0, y0, x1, y1, outline=colour,
                                         width=3 if active else 2,
                                         dash=() if active else (5, 3))
            label = f"{region.name} · {region.lang}"
            self.canvas.create_rectangle(x0, y0 - 18, x0 + 9 * len(label) + 8, y0,
                                         fill=colour, outline="")
            self.canvas.create_text(x0 + 5, y0 - 16, anchor="nw", fill="#08111d",
                                    text=label, font=(theme.UI, 8, "bold"))

    # -- mouse ------------------------------------------------------------
    def _hit(self, x: float, y: float) -> Optional[int]:
        for i, region in enumerate(self.regions):
            x0, y0, x1, y1 = self._to_canvas(region)
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        return None

    def _on_press(self, event) -> None:
        hit = self._hit(event.x, event.y)
        if hit is not None:
            self.selected = hit
            self._load_fields()
            self._refresh_list(keep=True)
            self._redraw()
            self._drag_start = None
            return
        self._drag_start = (event.x, event.y)

    def _on_drag(self, event) -> None:
        if not self._drag_start:
            return
        if self._temp_rect:
            self.canvas.delete(self._temp_rect)
        self._temp_rect = self.canvas.create_rectangle(
            *self._drag_start, event.x, event.y, outline="#ffffff", width=2)

    def _on_release(self, event) -> None:
        if not self._drag_start:
            return
        x0, y0 = self._drag_start
        self._drag_start = None
        if self._temp_rect:
            self.canvas.delete(self._temp_rect)
            self._temp_rect = None
        if abs(event.x - x0) < 12 or abs(event.y - y0) < 8:
            return                                  # a click, not a box
        s, w, h = self.scale, self.frame_w, self.frame_h
        box = (round(min(x0, event.x) / s / w, 5), round(min(y0, event.y) / s / h, 5),
               round(max(x0, event.x) / s / w, 5), round(max(y0, event.y) / s / h, 5))
        default = ("subtitle", "dialogue") if not self.regions else \
            (f"region{len(self.regions) + 1}", "info")
        self.regions.append(Region(name=default[0], role=default[1],
                                   lang=self.default_lang, box=box,
                                   translate=default[1] != "name"))
        self.selected = len(self.regions) - 1
        self._load_fields()
        self._refresh_list(keep=True)
        self._redraw()

    # -- fields -----------------------------------------------------------
    def _load_fields(self) -> None:
        if self.selected is None:
            return
        region = self.regions[self.selected]
        self._loading = True
        self.v_name.set(region.name)
        self.v_role.set(ROLE_LABEL.get(region.role, region.role))
        self.v_lang.set(LANG_LABEL.get(region.lang, region.lang))
        self.v_translate.set(region.translate)
        self._loading = False
        self._role_note()

    def _role_note(self) -> None:
        role = ROLE_BY_LABEL.get(self.v_role.get(), self.v_role.get())
        note = {
            "dialogue": "整块当一句话翻，前后文连得上",
            "name": "只更新字幕上的说话人，不翻译、不占额度",
            "choice": "每个选项分别翻，贴回各自位置",
            "info": "框里每行分别翻、分别贴回原位。菜单和属性面板用这个",
        }.get(role, "")
        self.lbl_role_note.config(text=note)

    def _apply_fields(self) -> None:
        if self.selected is None or self._loading:
            return
        region = self.regions[self.selected]
        # Back to the values the config file uses. A typed-in language tag is
        # kept as typed, so an OCR pack the dropdown does not list still works.
        label_role, label_lang = self.v_role.get(), self.v_lang.get()
        region.name = self.v_name.get() or region.name
        region.role = ROLE_BY_LABEL.get(label_role, label_role) or region.role
        region.lang = LANG_BY_LABEL.get(label_lang, label_lang) or region.lang
        region.translate = self.v_translate.get()
        self._role_note()
        self._refresh_list(keep=True)
        self._redraw()

    def _refresh_list(self, keep: bool = False) -> None:
        self.listbox.delete(0, "end")
        for region in self.regions:
            mark = "" if region.translate else "   不翻译"
            role = ROLE_LABEL.get(region.role, region.role)
            lang = LANG_LABEL.get(region.lang, region.lang)
            self.listbox.insert("end", f" {region.name} · {role} · {lang}{mark}")
        if keep and self.selected is not None:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(self.selected)

    def _on_list_select(self, _event) -> None:
        sel = self.listbox.curselection()
        if sel:
            self.selected = sel[0]
            self._load_fields()
            self._redraw()

    def _autodetect(self) -> None:
        """OCR the whole frame and propose a region per block of text.

        Framing by hand is the step people get wrong: a menu screen has text
        in several places, it is easy to box one of them and conclude the tool
        only reads that much. Recognising the frame first shows where the text
        actually is, and the boxes come from the same engine that will be
        reading them, so anything it proposes is something it can see.
        """
        frame = grab_one_frame(self.hwnd)
        if frame is None:
            messagebox.showerror("抓不到画面", "没有拿到帧，重试或检查窗口。",
                                 parent=self)
            return
        from .config import OcrConfig
        from .ocr import OcrReader, cluster_blocks

        reader = OcrReader(OcrConfig(engine="windows", upscale=1.5,
                                     min_chars=1))
        height, width = frame.shape[:2]
        try:
            blocks = reader.read_blocks(frame, self.default_lang)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("识别失败", str(exc), parent=self)
            return
        boxes = cluster_blocks(blocks)
        if not boxes:
            messagebox.showinfo(
                "没找到文字",
                f"整屏识别没有找到 {self.default_lang} 文字。\n"
                f"确认游戏语言和右侧「识别语言」一致，或者手动拖框。", parent=self)
            return

        pad_x, pad_y = width * 0.004, height * 0.004
        existing = len(self.regions)
        for index, (x0, y0, x1, y1) in enumerate(boxes):
            self.regions.append(Region(
                name=f"auto{existing + index + 1}", role="info",
                lang=self.default_lang, translate=True, per_line=True,
                box=(round(max(0.0, x0 - pad_x) / width, 5),
                     round(max(0.0, y0 - pad_y) / height, 5),
                     round(min(width, x1 + pad_x) / width, 5),
                     round(min(height, y1 + pad_y) / height, 5))))
        self.selected = len(self.regions) - 1
        self._load_fields()
        self._refresh_list(keep=True)
        self._redraw()
        messagebox.showinfo(
            "找到了",
            f"识别到 {len(blocks)} 行文字，归成 {len(boxes)} 块，已加为区域。\n\n"
            f"不需要的直接选中删掉。类型都设成了 info，"
            f"每行会分别翻译并贴回各自位置。", parent=self)

    def _delete(self) -> None:
        if self.selected is None:
            return
        del self.regions[self.selected]
        self.selected = None
        self._refresh_list()
        self._redraw()

    def _ok(self) -> None:
        if not self.regions:
            messagebox.showwarning("还没有区域", "至少画一个框。", parent=self)
            return
        names = [r.name for r in self.regions]
        if len(set(names)) != len(names):
            messagebox.showwarning("名称重复", "每个区域要有不同的名称。", parent=self)
            return
        self.result = self.regions
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# --------------------------------------------------------------------------
# Help tab
# --------------------------------------------------------------------------
def build_help(parent) -> tk.Frame:
    frame = tk.Frame(parent, bg=theme.CARD)
    text = tk.Text(frame, wrap="word", bg=theme.CARD, fg=theme.FG, relief="flat",
                   padx=22, pady=18, spacing1=1, spacing3=4, cursor="arrow",
                   font=(theme.UI, 9), highlightthickness=0)
    scroll = ttk.Scrollbar(frame, command=text.yview, style="Vertical.TScrollbar")
    text.config(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)

    text.tag_config("h1", font=(theme.UI, 11, "bold"), foreground=theme.ACCENT,
                    spacing1=16, spacing3=6)
    text.tag_config("p", font=(theme.UI, 9), foreground="#c8cfda",
                    spacing3=6, lmargin1=2, lmargin2=2)
    text.tag_config("p2", font=(theme.UI, 9), foreground=theme.MUTED,
                    spacing3=6, lmargin1=2, lmargin2=2)
    text.tag_config("li", font=(theme.UI, 9), foreground="#c8cfda",
                    lmargin1=14, lmargin2=26, spacing3=3)
    text.tag_config("code", font=(theme.MONO, 9), foreground=theme.MUTED,
                    background="#0c1017", spacing1=6, spacing3=8,
                    lmargin1=14, lmargin2=14)
    text.tag_config("key", font=(theme.UI, 9, "bold"), foreground=theme.GOLD,
                    lmargin1=14, lmargin2=26)
    text.tag_config("val", font=(theme.UI, 9), foreground="#c8cfda",
                    lmargin1=14, lmargin2=26, spacing3=5)

    for tag, body in HELP:
        if tag == "li":
            text.insert("end", "·  " + body + "\n", "li")
        elif tag == "kv":
            key, _, value = body.partition("|")
            text.insert("end", key + "  ", "key")
            text.insert("end", value + "\n", "val")
        else:
            text.insert("end", body + "\n", tag)
    text.config(state="disabled")
    return frame


# --------------------------------------------------------------------------
# Control panel
# --------------------------------------------------------------------------
class ControlWindow:
    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path)
        self.cfg: AppConfig = cfgmod.load(self.config_path) \
            if self.config_path.exists() else AppConfig()
        self.pipeline: Optional[Pipeline] = None
        self.windows: List[WindowInfo] = []
        self.lines: "queue.Queue[Line]" = queue.Queue()
        self._closed = False
        self._idle_warned = False
        self._timeouts = 0
        self._slow_model_warned = False

        self.root = tk.Tk()
        self.root.title("Game Live Translator")
        self.root.configure(bg=theme.BG)
        self.root.minsize(760, 700)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        theme.apply(self.root)

        self._build_header()
        book = ttk.Notebook(self.root)
        book.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        run_tab = tk.Frame(book, bg=theme.CARD)
        book.add(run_tab, text="  运行  ")
        book.add(build_help(book), text="  说明  ")
        self._build_run_tab(run_tab)

        self._refresh_windows()
        self._update_region_summary()
        self.root.after(400, self._tick)

    # -- chrome -----------------------------------------------------------
    def _build_header(self) -> None:
        head = tk.Frame(self.root, bg=theme.BG)
        head.pack(fill="x", padx=14, pady=(14, 10))
        tk.Frame(head, bg=theme.ACCENT, width=4).pack(side="left", fill="y")
        box = tk.Frame(head, bg=theme.BG)
        box.pack(side="left", padx=(10, 0))
        tk.Label(box, text="Game Live Translator", bg=theme.BG, fg=theme.FG,
                 font=(theme.UI, 15, "bold")).pack(anchor="w")
        tk.Label(box, text="按画面变化触发的实时翻译 · 不注入进程，只读屏幕",
                 bg=theme.BG, fg=theme.FAINT, font=(theme.UI, 8)).pack(anchor="w")

    def _build_run_tab(self, parent) -> None:
        wrap = tk.Frame(parent, bg=theme.CARD)
        wrap.pack(fill="both", expand=True, padx=16, pady=16)
        self._build_source(wrap)
        self._build_translate(wrap)
        self._build_display(wrap)
        self._build_tuning(wrap)
        self._build_controls(wrap)
        self._build_log(wrap)

    # -- sections ---------------------------------------------------------
    def _build_source(self, parent) -> None:
        box = theme.card(parent)
        box.pack(fill="x", pady=(0, 10))
        pad = tk.Frame(box, bg=theme.CARD)
        pad.pack(fill="x", padx=14, pady=12)
        pad.columnconfigure(1, weight=1)

        theme.section_title(pad, " 1 ", "选择画面来源",
                            "游戏要窗口化或无边框，独占全屏抓不到").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(pad, text="窗口", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        self.v_window = tk.StringVar()
        self.cb_window = ttk.Combobox(pad, textvariable=self.v_window,
                                      state="readonly", width=48)
        self.cb_window.grid(row=1, column=1, sticky="we", padx=10)
        ttk.Button(pad, text="刷新", width=7,
                   command=self._refresh_windows).grid(row=1, column=2)

        ttk.Label(pad, text="区域", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=(10, 0))
        self.lbl_regions = ttk.Label(pad, text="-", style="Hint.TLabel")
        self.lbl_regions.grid(row=2, column=1, sticky="w", padx=10, pady=(10, 0))
        ttk.Button(pad, text="编辑区域", width=10,
                   command=self._edit_regions).grid(row=2, column=2, pady=(10, 0))

    def _build_translate(self, parent) -> None:
        box = theme.card(parent)
        box.pack(fill="x", pady=(0, 10))
        pad = tk.Frame(box, bg=theme.CARD)
        pad.pack(fill="x", padx=14, pady=12)
        pad.columnconfigure(4, weight=1)

        theme.section_title(pad, " 2 ", "翻译").grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 10))

        ttk.Label(pad, text="后端", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        self.v_backend = tk.StringVar(value=self.cfg.translate.backend)
        cb = ttk.Combobox(pad, textvariable=self.v_backend, values=BACKENDS,
                          state="readonly", width=12)
        cb.grid(row=1, column=1, padx=(10, 18))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._update_key_status())

        ttk.Label(pad, text="译成", style="Card.TLabel").grid(row=1, column=2, sticky="w")
        self.v_target = tk.StringVar(value=self.cfg.translate.target_lang)
        ttk.Combobox(pad, textvariable=self.v_target, values=TARGETS,
                     width=10).grid(row=1, column=3, padx=(10, 18))

        self.lbl_key = ttk.Label(pad, text="", style="Card.TLabel")
        self.lbl_key.grid(row=1, column=4, sticky="w")

        # Shown only for backends that need credentials.
        self.creds = tk.Frame(pad, bg=theme.CARD)
        self.creds.grid(row=2, column=0, columnspan=5, sticky="we", pady=(10, 0))
        self.creds.columnconfigure(1, weight=1)

        ttk.Label(self.creds, text="地址", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=3)
        self.v_base = tk.StringVar()
        ttk.Entry(self.creds, textvariable=self.v_base).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=(10, 0), pady=3)

        ttk.Label(self.creds, text="密钥", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=3)
        self.v_key = tk.StringVar()
        self.entry_key = ttk.Entry(self.creds, textvariable=self.v_key, show="•")
        self.entry_key.grid(row=1, column=1, sticky="we", padx=(10, 8), pady=3)
        ttk.Button(self.creds, text="保存并启用", width=11,
                   command=self._save_key).grid(row=1, column=2, pady=3)
        ttk.Button(self.creds, text="测试", width=7,
                   command=self._test_key).grid(row=1, column=3, padx=6, pady=3)
        ttk.Button(self.creds, text="诊断", width=7,
                   command=self._diagnose_endpoint).grid(row=2, column=3, padx=6, pady=3)

        ttk.Label(self.creds, text="模型", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=3)
        self.v_model = tk.StringVar()
        self.cb_model = ttk.Combobox(self.creds, textvariable=self.v_model)
        self.cb_model.grid(row=2, column=1, sticky="we", padx=(10, 8), pady=3)
        self.btn_models = ttk.Button(self.creds, text="拉取模型", width=11,
                                     command=self._fetch_models)
        self.btn_models.grid(row=2, column=2, pady=3)

        self.lbl_backend_note = ttk.Label(pad, text="", style="Hint.TLabel",
                                          wraplength=640)
        self.lbl_backend_note.grid(row=3, column=0, columnspan=5, sticky="w",
                                   pady=(8, 0))
        self._update_key_status()

    def _build_display(self, parent) -> None:
        box = theme.card(parent)
        box.pack(fill="x", pady=(0, 10))
        pad = tk.Frame(box, bg=theme.CARD)
        pad.pack(fill="x", padx=14, pady=12)
        pad.columnconfigure(4, weight=1)

        theme.section_title(pad, " 3 ", "译文显示在哪").grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 10))

        ttk.Label(pad, text="显示方式", style="Card.TLabel").grid(
            row=1, column=0, sticky="w")
        self.v_display = tk.StringVar(value=_display_label(
            self.cfg.overlay.mode, self.cfg.overlay.label_style))
        cb = ttk.Combobox(pad, textvariable=self.v_display,
                          values=list(DISPLAY_OPTIONS), state="readonly",
                          width=26)
        cb.grid(row=1, column=1, columnspan=2, padx=(10, 18), sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._update_mode_note())

        self.lbl_mode_note = ttk.Label(pad, text="", style="Hint.TLabel",
                                       wraplength=620)
        self.lbl_mode_note.grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self._update_mode_note()

    def _update_mode_note(self) -> None:
        self.lbl_mode_note.config(
            text=DISPLAY_NOTE.get(self.v_display.get(), ""))

    def _build_tuning(self, parent) -> None:
        box = theme.card(parent)
        box.pack(fill="x", pady=(0, 10))
        pad = tk.Frame(box, bg=theme.CARD)
        pad.pack(fill="x", padx=14, pady=12)
        pad.columnconfigure(1, weight=1)

        theme.section_title(pad, " 4 ", "触发参数", "运行中可以直接拧，改完点保存").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.v_motion = tk.DoubleVar(value=self.cfg.trigger.motion_threshold)
        self.v_stable = tk.IntVar(value=self.cfg.trigger.stable_ms)
        self.v_coalesce = tk.IntVar(value=self.cfg.ocr.coalesce_ms)
        # Symptom first, then which way to turn it. Describing what a knob
        # measures reads fine and helps nobody decide anything.
        specs = [
            ("变化阈值", self.v_motion, 0.001, 0.05, "{:.3f}",
             "只翻出半句话 → 调小到 0.003　　同一句反复翻 → 调大到 0.008"),
            ("稳定判定 ms", self.v_stable, 60, 900, "{:.0f}",
             "等好几秒才出字幕 → 调小到 150　　台词逐字蹦出来 → 调大到 400"),
            ("合并窗口 ms", self.v_coalesce, 0, 900, "{:.0f}",
             "还是会出半句话 → 调大到 400　　想更快出字 → 调小到 120"),
        ]
        for i, (label, var, lo, hi, fmt, hint) in enumerate(specs):
            row = 1 + i * 2
            ttk.Label(pad, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w")
            ttk.Scale(pad, from_=lo, to=hi, variable=var, orient="horizontal",
                      command=lambda _v: self._apply_tuning()).grid(
                row=row, column=1, sticky="we", padx=12)
            value = ttk.Label(pad, width=6, style="Value.TLabel", anchor="e")
            value.grid(row=row, column=2, sticky="e")
            var.trace_add("write",
                          lambda *_a, v=var, l=value, f=fmt: l.config(text=f.format(v.get())))
            value.config(text=fmt.format(var.get()))
            ttk.Label(pad, text=hint, style="Hint.TLabel").grid(
                row=row + 1, column=1, columnspan=2, sticky="w", padx=12, pady=(1, 8))

    def _build_controls(self, parent) -> None:
        box = tk.Frame(parent, bg=theme.CARD)
        box.pack(fill="x", pady=(2, 12))
        self.btn_start = ttk.Button(box, text="▶   开始", width=14,
                                    style="Accent.TButton", command=self._start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(box, text="■   停止", width=12,
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Button(box, text="保存配置", width=11,
                   command=self._save).pack(side="left")
        self.lbl_dot = tk.Label(box, text="●", bg=theme.CARD, fg=theme.FAINT,
                                font=(theme.UI, 11))
        self.lbl_dot.pack(side="left", padx=(18, 4))
        self.lbl_status = tk.Label(box, text="未运行", bg=theme.CARD,
                                   fg=theme.MUTED, font=(theme.UI, 9))
        self.lbl_status.pack(side="left")

    def _build_log(self, parent) -> None:
        head = tk.Frame(parent, bg=theme.CARD)
        head.pack(fill="x")
        tk.Label(head, text="最近的译文", bg=theme.CARD, fg=theme.FG,
                 font=(theme.UI, 10, "bold")).pack(side="left")
        tk.Label(head, text="灰色是识别到的原文，白色是译文",
                 bg=theme.CARD, fg=theme.FAINT, font=(theme.UI, 8)).pack(
            side="left", padx=10)

        box = tk.Frame(parent, bg=theme.BORDER, highlightthickness=0)
        box.pack(fill="both", expand=True, pady=(6, 0))
        # A CJK-capable font is named explicitly: Tk's default has no CJK
        # glyphs and falls back per character, which renders Japanese with a
        # visible gap between every character.
        self.text = tk.Text(box, height=9, wrap="word", font=(theme.UI, 10),
                            bg="#0a0d13", fg=theme.FG, relief="flat",
                            insertbackground=theme.FG, padx=12, pady=10,
                            highlightthickness=0, spacing3=2)
        self.text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        scroll = ttk.Scrollbar(box, command=self.text.yview,
                               style="Vertical.TScrollbar")
        scroll.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.text.config(yscrollcommand=scroll.set, state="disabled")
        self.text.tag_config("speaker", foreground=theme.GOLD,
                             font=(theme.UI, 9, "bold"), spacing1=6)
        self.text.tag_config("src", foreground=theme.MUTED, font=(theme.UI, 9))
        self.text.tag_config("dst", foreground=theme.FG, font=(theme.UI, 11))
        self.text.tag_config("err", foreground=theme.ERR, font=(theme.UI, 9))
        self.text.tag_config("note", foreground=theme.FAINT, font=(theme.UI, 8),
                             spacing1=4)

    # -- actions ----------------------------------------------------------
    def _own_windows(self) -> set:
        """Windows belonging to this process, which must never be a target.

        The overlay is caught by title, but the console is not: it is an
        ordinary window sitting in the list like any other, and being first it
        is what the fallback below picks. Once picked it is written to the
        config, matched again on the next start, and the tool settles into
        capturing its own black console for ever -- running, never firing,
        with nothing on screen to say why.
        """
        try:
            # restype, or ctypes truncates the HWND to a signed 32-bit int and
            # the handle no longer matches the one win32gui reports. A built
            # executable shipped with this wrong and a tester's config came
            # back pointing at 实时翻译器.exe: its own console window.
            get_console = ctypes.windll.kernel32.GetConsoleWindow
            get_console.restype = ctypes.c_void_p
            return {int(get_console() or 0)} - {0}
        except Exception:
            return set()

    def _own_titles(self) -> set:
        """Titles this process puts on screen, as a second net.

        Double-clicking the built executable opens a console titled with the
        executable's own path, so matching that catches it even where the
        handle comparison cannot -- and costs nothing when running from
        source, where the path is python.exe and never appears as a target.
        """
        names = {"Game Live Translator"}
        for path in (sys.executable, sys.argv[0]):
            if path:
                names.add(str(Path(path).resolve()))
        return names

    def _refresh_windows(self) -> None:
        mine, titles = self._own_windows(), self._own_titles()
        self.windows = [w for w in list_windows()
                        if w.hwnd not in mine
                        and not any(t in w.title for t in titles)]
        labels = [f"{w.title}    ({w.width}x{w.height})" for w in self.windows]
        self.cb_window["values"] = labels
        wanted = self.cfg.source.window_title
        if wanted:
            for label, info in zip(labels, self.windows):
                if wanted.lower() in info.title.lower():
                    self.v_window.set(label)
                    return
        if labels and not self.v_window.get():
            self.v_window.set(labels[0])

    def _selected_window(self) -> Optional[WindowInfo]:
        label = self.v_window.get()
        for candidate, info in zip(self.cb_window["values"], self.windows):
            if candidate == label:
                return info
        return None

    def _update_region_summary(self) -> None:
        if not self.cfg.regions:
            self.lbl_regions.config(text="还没画区域 —— 点右边「编辑区域」")
            return
        self.lbl_regions.config(text="    ".join(
            f"{r.name} ({r.lang}{'' if r.translate else ', 不翻译'})"
            for r in self.cfg.regions))

    def _key_section(self) -> dict:
        return getattr(self.cfg.translate, self.v_backend.get(), {}) or {}

    def _update_key_status(self) -> None:
        backend = self.v_backend.get()
        note = BACKEND_NOTE.get(backend, "")
        if backend == "google":
            note += "。游戏术语会翻错（ステータス→「地位」、持ち物→「财物」）," \
                    "正式玩建议换成 openai 或 anthropic"
        needs_key = backend not in ("none", "google")
        if needs_key:
            note += "。密钥存进 Windows 用户环境变量，不会写进配置文件"
        self.lbl_backend_note.config(
            text=note,
            foreground=theme.WARN if backend == "google" else theme.FAINT)

        if not needs_key:
            self.creds.grid_remove()
            self.lbl_key.config(text="● 不需要密钥", foreground=theme.OK)
            return

        self.creds.grid()
        section = self._key_section()
        self.v_model.set(section.get("model", ""))
        self.v_base.set(section.get("base_url", ""))
        # DeepL picks its host from the free/paid tier, so there is nothing to
        # point elsewhere and no model to choose.
        for widget in (self.cb_model, self.btn_models):
            if "base_url" in section:
                widget.grid()
            else:
                widget.grid_remove()
        env = section.get("api_key_env", "")
        existing = os.environ.get(env) or section.get("api_key", "")
        # Never put the real key back in the box - show that one is stored.
        self.v_key.set("")
        self.entry_key.config(foreground=theme.FG)
        if existing:
            self.lbl_key.config(text=f"● 密钥已就绪 ({env})", foreground=theme.OK)
        else:
            self.lbl_key.config(text=f"● 还没有密钥 ({env})", foreground=theme.WARN)

    def _save_key(self) -> None:
        """Store the key the user typed in their own environment.

        Not in config.json: that file is meant to be shareable, and a key in
        it would follow the project into any copy or repository. Writing the
        user-level environment variable keeps it with the account, and setting
        it in this process too means it works immediately without a restart.
        """
        backend = self.v_backend.get()
        section = self._key_section()
        env = section.get("api_key_env", "")
        key = self.v_key.get().strip()
        if not env:
            return
        if not key:
            messagebox.showwarning("还没输入", "先把密钥粘贴到输入框里。",
                                   parent=self.root)
            return
        try:
            _set_user_env(env, key)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        os.environ[env] = key
        self._apply_endpoint()
        # The address and model sit in the same row under the same button, so
        # saving only the key left them in memory and lost on exit - the
        # button says 保存, and it has to mean all three.
        self._persist()
        self.v_key.set("")
        self._update_key_status()
        self._log(f"密钥已存进用户环境变量 {env}，地址和模型已写入 "
                  f"{self.config_path.name}，都已生效。", "note")

    def _persist(self) -> bool:
        """Write the config file, reporting failure rather than swallowing it."""
        try:
            self._collect()
            hwnd, self.cfg.source.window_hwnd = self.cfg.source.window_hwnd, None
            cfgmod.save(self.cfg, self.config_path)
            self.cfg.source.window_hwnd = hwnd
            return True
        except Exception as exc:  # noqa: BLE001
            self._log(f"保存配置失败：{exc}", "err")
            return False

    def _apply_endpoint(self) -> dict:
        """Push the address/model boxes into the backend's config section."""
        section = self._key_section()
        if self.v_base.get().strip() and "base_url" in section:
            section["base_url"] = self.v_base.get().strip()
        if self.v_model.get().strip():
            section["model"] = self.v_model.get().strip()
        typed = self.v_key.get().strip()
        if typed and section.get("api_key_env"):
            os.environ[section["api_key_env"]] = typed
        return section

    def _run_bg(self, work, done, deadline_s: float, timeout_message: str) -> None:
        """Run `work()` off the UI thread and hand the result to `done()`.

        Two things the earlier per-button versions got wrong. The worker's
        import sat outside its try, so an import failure killed the thread
        without ever publishing a result; and the poll had no end, so any
        worker that failed to publish left the button reading "loading" for
        the rest of the session with no way back. Here the worker catches
        BaseException - a dead worker must never be able to hang the UI - and
        the poll gives up on a deadline regardless.

        A sentinel rather than None marks "not finished", so a legitimate None
        result cannot be mistaken for one still running.
        """
        slot = {"value": _PENDING}

        def worker():
            try:
                slot["value"] = work()
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                slot["value"] = exc

        threading.Thread(target=worker, daemon=True).start()
        end = time.monotonic() + deadline_s

        def poll():
            if slot["value"] is not _PENDING:
                done(slot["value"])
            elif time.monotonic() > end:
                done(TimeoutError(timeout_message))
            else:
                self.root.after(150, poll)

        self.root.after(150, poll)

    def _fetch_models(self) -> None:
        """Ask the endpoint for its model list, off the UI thread."""
        # _collect first: the worker reads cfg.translate.backend, which still
        # holds the value loaded from disk until the widgets are folded back
        # in. Without this, switching the dropdown and pressing fetch queries
        # whichever backend the config file happened to name.
        self._collect()
        self._apply_endpoint()
        self.btn_models.config(state="disabled", text="拉取中…")
        timeout = self.cfg.translate.timeout_s

        def work():
            from .translate import list_models
            return list_models(self.cfg.translate)

        self._run_bg(work, self._models_done, deadline_s=timeout + 5,
                     timeout_message=(
                         f"等了 {timeout + 5:.0f} 秒没有响应。"
                         f"不少中转站只开放 chat/completions，没有 /v1/models 这个接口——"
                         f"这种情况直接把模型名手打进去就行，向中转站方要一份可用模型列表。"))

    def _models_done(self, result) -> None:
        self.btn_models.config(state="normal", text="拉取模型")
        if isinstance(result, BaseException):
            messagebox.showerror("拉取失败",
                                 f"{type(result).__name__}: {result}",
                                 parent=self.root)
            self._log(f"拉取模型失败：{result}", "err")
            return
        self.cb_model["values"] = result
        self._log(f"从 {self.v_base.get()} 拉到 {len(result)} 个模型。", "note")
        if result and self.v_model.get() not in result:
            messagebox.showinfo(
                "拉取成功",
                f"共 {len(result)} 个模型。当前填的 “{self.v_model.get()}” "
                f"不在列表里，点开下拉框选一个。", parent=self.root)

    def _diagnose_endpoint(self) -> None:
        """Layer-by-layer probe, run off the UI thread."""
        self._collect()
        self._apply_endpoint()
        self._log("正在分步诊断端点…", "note")
        # Three requests at the configured timeout, plus room to spare.
        budget = self.cfg.translate.timeout_s * 3 + 6

        def work():
            from .translate import probe_endpoint
            return probe_endpoint(self.cfg.translate)

        self._run_bg(work, self._probe_done, deadline_s=budget,
                     timeout_message=f"诊断超过 {budget:.0f} 秒还没跑完，已放弃。")

    def _probe_done(self, result) -> None:
        if isinstance(result, BaseException):
            messagebox.showerror("诊断失败",
                                 f"{type(result).__name__}: {result}",
                                 parent=self.root)
            return
        lines = []
        for step, ok, detail in result:
            mark = "OK  " if ok else "失败"
            lines.append(f"[{mark}] {step}\n        {detail}")
            self._log(f"[{mark}] {step} — {detail}", "note" if ok else "err")
        messagebox.showinfo("端点诊断", "\n\n".join(lines), parent=self.root)

    def _test_key(self) -> None:
        """One real request, so the answer is not a guess."""
        self._collect()
        self._apply_endpoint()
        from .translate import ApiError, Translator
        try:
            probe = Translator(self.cfg.translate)
            # Bypass the pipeline's catch-all so the real error survives.
            out = probe.backend.translate("待って。その先は危険だわ。", [])
        except ApiError as exc:
            hint = _diagnose(exc)
            if messagebox.askyesno(
                    "测试失败",
                    f"{exc}\n\n{hint}\n\n要现在分步诊断一下吗？"
                    f"（会逐层测试连通性、密钥和翻译接口）", parent=self.root):
                self._diagnose_endpoint()
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("测试失败", f"{type(exc).__name__}: {exc}",
                                 parent=self.root)
            return
        if not out.strip():
            messagebox.showerror("测试失败", "请求成功但返回了空内容。",
                                 parent=self.root)
            return
        self._log(f"测试通过：待って。その先は危険だわ。 → {out}", "note")
        messagebox.showinfo("测试通过",
                            f"待って。その先は危険だわ。\n→ {out}", parent=self.root)

    def _edit_regions(self) -> None:
        info = self._selected_window()
        if info is None:
            messagebox.showwarning("先选窗口", "上面先选一个窗口。", parent=self.root)
            return
        lang = self.cfg.regions[0].lang if self.cfg.regions else "ja"
        editor = RegionEditor(self.root, info.hwnd, self.cfg.regions, lang)
        self.root.wait_window(editor)
        if editor.result is None:
            return
        changed = editor.result != self.cfg.regions
        self.cfg.regions = editor.result
        self._update_region_summary()
        if not changed:
            return
        self._warn_overlapping_regions()
        if self.pipeline is not None:
            # The pipeline builds one runtime per region when it starts, so
            # editing them while it runs changed nothing whatsoever -- the old
            # boxes kept firing, the new ones never did, and the panel said
            # nothing. Deleting a box and watching its captions carry on is
            # how that looks from the outside.
            self._log("区域改了，重新开始抓取让新框生效。", "note")
            self._stop()
            self._start()

    def _collect(self) -> None:
        """Pull the widgets back into the config object."""
        info = self._selected_window()
        if info is not None:
            self.cfg.source.window_title = info.title
            self.cfg.source.window_hwnd = None
        self.cfg.translate.backend = self.v_backend.get()
        self.cfg.translate.target_lang = self.v_target.get()
        mode, style = DISPLAY_OPTIONS.get(self.v_display.get(),
                                          ("inplace", "seamless"))
        self.cfg.overlay.mode = mode
        self.cfg.overlay.label_style = style
        if self.cfg.translate.backend not in ("none", "google"):
            self._apply_endpoint()
        self.cfg.trigger.motion_threshold = round(self.v_motion.get(), 4)
        self.cfg.trigger.stable_ms = int(self.v_stable.get())
        self.cfg.ocr.coalesce_ms = int(self.v_coalesce.get())

    def _apply_tuning(self) -> None:
        """Push slider values into a running pipeline.

        Live tuning is the point of having sliders: you watch the counters
        while the game runs instead of stopping, editing JSON and restarting.
        A region carrying its own override for a key keeps it.
        """
        self.cfg.trigger.motion_threshold = round(self.v_motion.get(), 4)
        self.cfg.trigger.stable_ms = int(self.v_stable.get())
        self.cfg.ocr.coalesce_ms = int(self.v_coalesce.get())
        if self.pipeline is None:
            return
        for runtime in self.pipeline.runtimes.values():
            overrides = runtime.region.trigger or {}
            if "motion_threshold" not in overrides:
                runtime.trigger.cfg.motion_threshold = self.cfg.trigger.motion_threshold
            if "stable_ms" not in overrides:
                runtime.trigger.cfg.stable_ms = self.cfg.trigger.stable_ms
            if not (runtime.region.ocr or {}).get("coalesce_ms"):
                runtime.reader.cfg.coalesce_ms = self.cfg.ocr.coalesce_ms

    def _start(self) -> None:
        if self.pipeline is not None:
            return
        info = self._selected_window()
        if info is None:
            messagebox.showwarning("先选窗口", "上面先选一个窗口。", parent=self.root)
            return
        if not self.cfg.regions:
            messagebox.showwarning("还没有区域", "点「编辑区域」先框出字幕位置。",
                                   parent=self.root)
            return
        self._collect()
        self.cfg.source.window_hwnd = info.hwnd   # exact window, not a re-match
        self.cfg.overlay.enabled = True
        try:
            pipeline = Pipeline(self.cfg)
            pipeline.on_line = self.lines.put      # worker threads: queue only
            pipeline.start(overlay_master=self.root)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self.pipeline = pipeline
        self._idle_warned = False
        self._timeouts = 0
        self._slow_model_warned = False
        self._apply_tuning()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._log(f"开始抓取：{info.title}", "note")
        self._warn_overlapping_regions()

    def _warn_overlapping_regions(self) -> None:
        """Two boxes over the same text is a config nobody means to have.

        Both read it, both translate it and both draw over it, so the screen
        carries two captions for one line and whichever is drawn second wins.
        The symptom is "half of it is translated" -- the other half is there,
        underneath. It costs double as well.

        A leftover box from the shipped example plus a full-screen one drawn
        later is how it happens, and nothing said a word about it.
        """
        regions = self.cfg.regions
        for i, a in enumerate(regions):
            for b in regions[i + 1:]:
                ax0, ay0, ax1, ay1 = a.box
                bx0, by0, bx1, by1 = b.box
                wide = max(0.0, min(ax1, bx1) - max(ax0, bx0))
                tall = max(0.0, min(ay1, by1) - max(ay0, by0))
                overlap = wide * tall
                smaller = min((ax1 - ax0) * (ay1 - ay0),
                              (bx1 - bx0) * (by1 - by0)) or 1.0
                if overlap / smaller < 0.5:
                    continue
                self._log(f"「{a.name}」和「{b.name}」两个框盖着同一片文字"
                          f"（重叠 {overlap / smaller:.0%}）。", "err")
                self._log("  同一句会被翻两遍、画两遍，看起来就像只翻了一半。"
                          "点「编辑区域」删掉多余的那个。", "note")
                return

    def _stop(self) -> None:
        if self.pipeline is None:
            return
        if self.pipeline.overlay is not None:
            self.pipeline.overlay.close()
        self.pipeline.shutdown()
        self.pipeline = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.lbl_status.config(text="已停止", fg=theme.MUTED)
        self.lbl_dot.config(fg=theme.FAINT)
        self._log("已停止。", "note")

    def _save(self) -> None:
        if self._persist():
            self._log(f"配置已保存到 {self.config_path}", "note")

    # -- periodic ---------------------------------------------------------
    def _log(self, text: str, tag: str) -> None:
        self.text.config(state="normal")
        self.text.insert("end", text + "\n", tag)
        # Keep the buffer from growing without bound over a long session.
        if int(self.text.index("end-1c").split(".")[0]) > 400:
            self.text.delete("1.0", "120.0")
        self.text.see("end")
        self.text.config(state="disabled")

    def _tick(self) -> None:
        if self._closed:
            return          # a pending after() outlives destroy() otherwise
        try:
            while True:
                line = self.lines.get_nowait()
                if line.role == "name":
                    self._log(line.source, "speaker")
                else:
                    self._log(line.source, "src")
                    failed = line.translation.startswith("[translation failed")
                    self._log(line.translation, "err" if failed else "dst")
                    if failed and "TimeoutError" in line.translation:
                        self._timeouts += 1
                        self._warn_if_model_too_slow()
        except queue.Empty:
            pass

        pipeline = self.pipeline
        if pipeline is not None:
            if pipeline.capture is not None and pipeline.capture.closed:
                self._log("目标窗口已关闭。", "err")
                self._stop()
            else:
                elapsed = max(1e-6, time.monotonic() - pipeline.started)
                fires = sum(r.trigger.state.stats["fires"]
                            for r in pipeline.runtimes.values())
                stats = pipeline.translator.stats
                self.lbl_status.config(
                    text=(f"运行中     {pipeline.frames / elapsed:4.1f} fps      "
                          f"识别 {fires}      翻译 {stats['calls']}      "
                          f"缓存命中 {stats['cache_hits']}      "
                          f"错误 {stats['errors']}"),
                    fg=theme.FG)
                self.lbl_dot.config(fg=theme.ERR if stats["errors"] else theme.OK)
                self._warn_if_idle(elapsed, fires)
        self.root.after(400, self._tick)

    # How many timed-out lines before the model itself is the suspect.
    _TIMEOUT_PATIENCE = 3

    def _warn_if_model_too_slow(self) -> None:
        """Repeated timeouts are a model choice, not a network hiccup.

        Measured on one endpoint over the same eighteen lines: one model ran
        to a median of 7.8s and an 8s budget cut half of them, another to
        1.6s and cut none. The panel showed a growing error count either way,
        which does not point anywhere. A timeout also costs more than its own
        line, because the translate stage takes one at a time and everything
        behind it waits.
        """
        if self._slow_model_warned or self._timeouts < self._TIMEOUT_PATIENCE:
            return
        self._slow_model_warned = True
        section = self._key_section()
        model = section.get("model", "?") if isinstance(section, dict) else "?"
        budget = self.cfg.translate.timeout_s
        self._log(f"已经有 {self._timeouts} 句因为超时失败了。", "err")
        self._log(f"  多半是模型太慢，不是网络抖动——现在用的是「{model}」，"
                  f"单句预算 {budget:.0f} 秒。", "note")
        self._log("  在上面第 2 区把模型换一个更快的，点「拉取模型」能看到"
                  "这个地址提供哪些。超时的那句还会连累排在它后面的句子。", "note")

    def _warn_if_idle(self, elapsed: float, fires: int) -> None:
        """Frames arriving and nothing ever firing is a setup mistake.

        The counters already say 识别 0, but a number that never moves reads
        as "still warming up" rather than "wrong window". Say it in words,
        once, with the three things it is actually ever caused by.
        """
        if self._idle_warned or fires or elapsed < 25:
            return
        self._idle_warned = True
        title = self.cfg.source.window_title or "?"
        self._log(f"跑了 {elapsed:.0f} 秒，一次都没识别。通常是这三件事之一：", "err")
        self._log(f"  · 抓的窗口不是游戏——现在抓的是「{title}」", "note")
        self._log(f"  · 区域框在没有文字的地方——现在 {len(self.cfg.regions)} 个框，"
                  f"文字会变位置的界面框一个覆盖整屏的大框，类型选 info", "note")
        self._log("  · 画面一直在动，从没静止够久——把「稳定判定」调小", "note")

    def _on_close(self) -> None:
        self._stop()
        # Save on the way out. Setting a window, drawing regions, picking a
        # backend and tuning three sliders is too much work to lose because
        # the save button was never pressed; the file is the user's own and
        # holds nothing secret, since keys live in the environment.
        self._persist()
        self._closed = True
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch(config_path: str) -> int:
    ControlWindow(config_path).run()
    return 0
