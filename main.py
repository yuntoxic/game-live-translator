"""Command line entry point.

    python main.py gui                           control panel (start here)
    python main.py windows                       list capturable windows
    python main.py languages                     list installed OCR languages
    python main.py shot   --window "Projector"   save one frame to disk
    python main.py pick   --window "Projector"   draw the regions, write config
    python main.py tune                          live trigger numbers
    python main.py run                           go
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = "config.json"
LOG_NAME = "运行日志.txt"


def _capture_output_when_windowed() -> None:
    """Give print() somewhere to go when there is no console.

    Launched without one -- pythonw, or a build made with --noconsole -- both
    sys.stdout and sys.stderr are None, and the first print anywhere raises
    AttributeError and takes the process with it. The console was hiding
    nothing a player needs: the panel shows the counters live and reports
    failures in its own log pane. A traceback still has to land somewhere it
    can be asked for, though, so it goes to a file beside the program.
    """
    if sys.stdout is not None and sys.stderr is not None:
        # The console is usually GBK on this locale and cannot encode all of
        # what the OCR reads -- one katakana middle dot in a --debug print
        # raised UnicodeEncodeError and took the whole OCR thread with it,
        # which looks like the translator freezing. Keep the console's
        # encoding; just replace what it cannot say.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except Exception:   # not a TextIOWrapper; leave it be
                pass
        return
    # Typed into a terminal, a windowed build still has that terminal's
    # console to borrow. Attach to it so `doctor` and the other subcommands
    # answer where they were asked, rather than silently into a file nobody
    # was told about.
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
            return
    except Exception:       # noqa: BLE001 - fall through to the file
        pass

    base = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    try:
        stream = open(base.parent / LOG_NAME, "a", encoding="utf-8",
                      buffering=1)
    except OSError:
        import os
        stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stderr = stream
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====")


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def cmd_windows(_args) -> int:
    from glt.capture import list_windows
    print(f"{'HWND':>10}  {'SIZE':>11}  TITLE")
    for info in list_windows():
        print(info)
    print("\nTip: in OBS, right-click the preview -> Windowed Projector (Preview),")
    print("then match that window's title here.")
    return 0


def cmd_languages(_args) -> int:
    try:
        from glt.ocr import WindowsOcr
        langs = WindowsOcr.available_languages()
    except Exception as exc:  # noqa: BLE001
        return _fail(f"could not query Windows OCR: {exc}")
    print("Windows OCR languages installed:", ", ".join(langs) or "(none)")
    print("\nMissing one? Settings > Time & Language > Language & region >")
    print("add the language > its ... menu > Language options >")
    print("Optional features > add 'Optical character recognition'.")
    return 0


def _resolve_hwnd(args) -> int:
    from glt.capture import find_window
    if getattr(args, "hwnd", None):
        return int(args.hwnd)
    if not getattr(args, "window", None):
        raise SystemExit("need --window <title substring> or --hwnd <id>")
    info = find_window(args.window)
    if info is None:
        raise SystemExit(f"no visible window title contains {args.window!r}; "
                         f"run `python main.py windows`")
    print(f"matched: {info}")
    return info.hwnd


def cmd_shot(args) -> int:
    import cv2
    from glt.capture import grab_one_frame
    frame = grab_one_frame(_resolve_hwnd(args))
    if frame is None:
        return _fail("no frame captured (window minimised or closed?)")
    cv2.imwrite(args.out, frame)
    print(f"wrote {args.out}  ({frame.shape[1]}x{frame.shape[0]})")
    return 0


def cmd_pick(args) -> int:
    import cv2
    from glt import config as cfgmod
    from glt.capture import grab_one_frame

    hwnd = _resolve_hwnd(args)
    frame = grab_one_frame(hwnd)
    if frame is None:
        return _fail("no frame captured (window minimised or closed?)")
    height, width = frame.shape[:2]
    print(f"frame is {width}x{height}")

    # Fit the picker window on screen; boxes are mapped back to frame pixels.
    scale = min(1.0, 1600 / width, 900 / height)
    shown = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
        if scale < 1.0 else frame

    print("\nDrag a box, press ENTER/SPACE to confirm it, drag the next one.")
    print("Press ESC when you are done. Order matters: box the DIALOGUE first.")
    boxes = cv2.selectROIs("pick regions - ENTER to confirm, ESC when done",
                           shown, showCrosshair=False, fromCenter=False)
    cv2.destroyAllWindows()
    if len(boxes) == 0:
        return _fail("no regions selected")

    path = Path(args.config)
    cfg = cfgmod.load(path) if path.exists() else cfgmod.AppConfig()
    cfg.source.window_hwnd = None
    cfg.source.window_title = args.window or cfg.source.window_title

    regions = []
    defaults = [("subtitle", "dialogue"), ("speaker", "name"), ("choice", "choice")]
    for i, (bx, by, bw, bh) in enumerate(boxes):
        d_name, d_role = defaults[i] if i < len(defaults) else (f"region{i+1}", "info")
        name = input(f"  box {i+1} name [{d_name}]: ").strip() or d_name
        role = input(f"  box {i+1} role (dialogue/name/choice/info) [{d_role}]: ").strip() or d_role
        lang = input(f"  box {i+1} OCR language [{args.lang}]: ").strip() or args.lang
        regions.append(cfgmod.Region(
            name=name, role=role, lang=lang,
            translate=role != "name",
            box=(round(bx / scale / width, 5), round(by / scale / height, 5),
                 round((bx + bw) / scale / width, 5), round((by + bh) / scale / height, 5)),
        ))
    cfg.regions = regions
    cfgmod.save(cfg, path)
    print(f"\nwrote {path} with {len(regions)} region(s):")
    for r in regions:
        print(f"  {r.name:<10} {r.role:<9} {r.lang:<6} box={r.box}")
    print(f"\nNext:  python main.py run --config {path}")
    return 0


def cmd_tune(args) -> int:
    """Print live trigger numbers so thresholds can be set from evidence."""
    from glt import config as cfgmod
    from glt.capture import WindowCapture
    from glt.pipeline import Pipeline

    cfg = cfgmod.load(args.config)
    pipe = Pipeline(cfg)
    hwnd = pipe.resolve_window()

    stats = {r.name: {"max": 0.0, "fires": 0} for r in cfg.regions}
    last_print = [0.0]

    def on_frame(frame):
        pipe._on_frame(frame)
        now = time.monotonic()
        if now - last_print[0] < 0.25:
            return
        last_print[0] = now
        parts = []
        for name, rt in pipe.runtimes.items():
            s = stats[name]
            s["max"] = max(s["max"], rt.trigger.last_delta)
            s["fires"] = rt.trigger.state.stats["fires"]
            parts.append(f"{name}: d={rt.trigger.last_delta:6.2f} "
                         f"peak={s['max']:6.2f} edge={rt.trigger.last_edges:.4f} "
                         f"fires={s['fires']}")
        print("  " + " | ".join(parts) + "        ", end="\r", flush=True)

    cap = WindowCapture(hwnd, on_frame, max_fps=cfg.source.max_fps)
    cap.start()
    if not cap.wait_for_first_frame(5.0):
        cap.stop()
        return _fail("no frames captured")
    print("Watching. Let the game sit still, then let it move.\n"
          "  d      = how much the region changed this frame\n"
          "  peak   = the largest change seen so far\n"
          "  edge   = text density (below trigger.blank_edge_ratio = 'empty')\n"
          "Set trigger.motion_threshold between the still value and the moving\n"
          "value. Ctrl-C to stop.\n")
    try:
        while not cap.closed:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        cap.stop()
        pipe.shutdown()
    return 0


def cmd_run(args) -> int:
    from glt import config as cfgmod
    from glt.pipeline import Pipeline

    cfg = cfgmod.load(args.config)
    if args.no_overlay:
        cfg.overlay.enabled = False
    if args.backend:
        cfg.translate.backend = args.backend
    if args.target:
        cfg.translate.target_lang = args.target
    Pipeline(cfg, debug=args.debug, duration=args.seconds).run()
    return 0


def cmd_learn(args) -> int:
    """Propose glossary entries from a played session, for review."""
    import json
    from glt import config as cfgmod
    from glt.learn import extract_terms, read_pairs

    cfg = cfgmod.load(args.config)
    log = Path(args.log or cfg.log_file)
    if not log.exists():
        return _fail(f"找不到会话日志 {log}。先玩一局（配置里的 log_file 决定"
                     f"写到哪），或用 --log 指定别的日志文件。")
    pairs = read_pairs(log, cfg.translate.glossary)
    if not pairs:
        print("日志里没有可学习的行：都是空行、失败行，或已在术语表里。")
        return 0
    print(f"从 {log} 读到 {len(pairs)} 对新译文，请求模型提取专有名词……")
    try:
        terms = extract_terms(cfg.translate, pairs,
                              progress=lambda m: print(f"  {m}"))
    except Exception as exc:  # noqa: BLE001 - key missing, endpoint down
        return _fail(f"{type(exc).__name__}: {exc}")

    out = Path(args.out)
    existing = {}
    if out.exists():
        existing = {k: v for k, v in json.loads(
            out.read_text(encoding="utf-8-sig")).items()
            if not k.startswith("//")}
    # Existing suggestions survive a re-run; anything meanwhile promoted
    # into a real glossary drops out so the file only ever holds open items.
    merged = {k: v for k, v in {**existing, **terms}.items()
              if k not in cfg.translate.glossary}
    if not merged:
        print("模型没有提出新词条（宁缺毋滥是设计使然）。")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    fresh = len([k for k in terms if k not in existing])
    print(f"\n写入 {out}：共 {len(merged)} 条待审（本次新增 {fresh} 条）。")
    print("这个文件不会被自动使用。打开它删掉不要的词条，然后把它加进")
    print("translate.glossary_file（或把词条并进游戏自己的术语表）。")
    return 0


def cmd_gui(args) -> int:
    from glt.control import launch
    return launch(args.config)


def cmd_doctor(args) -> int:
    """Check every moving part and say which one is broken."""
    ok = True

    def check(label, fn):
        nonlocal ok
        try:
            print(f"  {label:<28} {fn()}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {label:<28} FAIL: {type(exc).__name__}: {exc}")

    import platform
    print("environment")
    print(f"  {'python':<28} {platform.python_version()}")
    print(f"  {'windows':<28} {platform.release()} build {platform.version()}")
    print("dependencies")
    check("windows-capture", lambda: __import__("windows_capture") and "ok")
    check("opencv", lambda: __import__("cv2").__version__)
    check("pywin32", lambda: __import__("win32gui") and "ok")
    check("winsdk (Windows OCR)", lambda: __import__("winsdk") and "ok")
    check("OCR languages", lambda: ", ".join(
        __import__("glt.ocr", fromlist=["WindowsOcr"]).WindowsOcr.available_languages()))
    print("config")
    path = Path(args.config)
    if not path.exists():
        print(f"  {'config file':<28} missing ({path}) - run `pick` first")
        ok = False
    else:
        from glt import config as cfgmod
        try:
            cfg = cfgmod.load(path)
            print(f"  {'config file':<28} ok ({len(cfg.regions)} region(s))")
            print(f"  {'target window':<28} {cfg.source.window_title or cfg.source.window_hwnd}")
            from glt.capture import find_window
            wanted = cfg.source.window_title or ""
            found = find_window(wanted) if wanted else None
            print(f"  {'window found':<28} "
                  f"{found.title if found else 'NOT FOUND - is the game window open?'}")
            # A console opened by double-clicking is titled with the path of
            # the executable that opened it, so a config pointing at our own
            # path means the picker selected this program's console. Capturing
            # it produces a black picture that never changes: frames arriving,
            # nothing ever recognised, and no clue as to why.
            if wanted and Path(wanted).name.lower() == Path(sys.executable).name.lower():
                ok = False
                print(f"  {'':<28} ^ that is this program's own console window."
                      f"\n  {'':<28}   Open the panel and pick the game instead.")
            key_ok = "ok"
            try:
                from glt.translate import build_backend
                build_backend(cfg.translate)
            except Exception as exc:  # noqa: BLE001
                key_ok, ok = f"FAIL: {exc}", False
            print(f"  {'translate backend':<28} {cfg.translate.backend} -> {key_ok}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  {'config file':<28} FAIL: {exc}")
    print("\n" + ("all good" if ok else "problems above"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="glt", description="Real-time OCR translation of a captured window.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("windows", help="list capturable windows").set_defaults(fn=cmd_windows)
    sub.add_parser("languages", help="list installed Windows OCR languages").set_defaults(fn=cmd_languages)

    s = sub.add_parser("shot", help="save one frame from a window")
    s.add_argument("--window"); s.add_argument("--hwnd", type=int)
    s.add_argument("-o", "--out", default="frame.png")
    s.set_defaults(fn=cmd_shot)

    s = sub.add_parser("pick", help="draw regions and write the config")
    s.add_argument("--window"); s.add_argument("--hwnd", type=int)
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.add_argument("--lang", default="ja", help="default OCR language for new regions")
    s.set_defaults(fn=cmd_pick)

    s = sub.add_parser("tune", help="live trigger numbers for threshold tuning")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_tune)

    s = sub.add_parser("run", help="start translating")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.add_argument("--debug", action="store_true", help="print every OCR and translation")
    s.add_argument("--no-overlay", action="store_true", help="console output only")
    s.add_argument("--backend", help="override translate.backend")
    s.add_argument("--target", help="override translate.target_lang")
    s.add_argument("--seconds", type=float, default=0.0,
                   help="stop after N seconds (console mode only; for smoke tests)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("learn", help="propose glossary entries from a session "
                                     "log (you review; never auto-applied)")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.add_argument("--log", help="session log to read (default: the config's log_file)")
    s.add_argument("-o", "--out", default="glossaries/suggested-terms.json")
    s.set_defaults(fn=cmd_learn)

    s = sub.add_parser("gui", help="open the control panel (recommended)")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_gui)

    s = sub.add_parser("doctor", help="check the install and the config")
    s.add_argument("--config", default=DEFAULT_CONFIG)
    s.set_defaults(fn=cmd_doctor)
    return p


if __name__ == "__main__":
    _capture_output_when_windowed()
    # Bare invocation opens the panel. Someone who double-clicks the built
    # executable passes no arguments at all, and a usage error followed by an
    # immediately closing window tells them nothing; the panel is what they
    # wanted anyway. The subcommands stay exactly as they were.
    argv = sys.argv[1:] or ["gui"]
    args = build_parser().parse_args(argv)
    try:
        raise SystemExit(args.fn(args))
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
    except SystemExit:
        raise
    except BaseException:
        # Without a console this is the only way the user learns anything: the
        # window would otherwise vanish with no message and no window.
        import traceback
        traceback.print_exc()
        where = Path(sys.executable if getattr(sys, "frozen", False)
                     else __file__).parent / LOG_NAME
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "启动失败",
                f"程序出错退出了。\n\n详细信息写在：\n{where}\n\n"
                f"把那个文件发出来就能定位问题。")
            root.destroy()
        except Exception:       # noqa: BLE001 - already failing; do not mask
            pass
        raise SystemExit(1)
