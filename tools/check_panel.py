"""Press the control panel's buttons and check it recovers from failure.

Constructing the window proves almost nothing. The handlers only run on a
click, so a NameError in one of them stays invisible until a user hits it -
and because the panel is launched without a console, the traceback goes
nowhere and the only symptom is a button stuck reading "loading" forever.
Two real bugs reached a user that way; this catches that shape.

    python tools/check_panel.py

Points the panel at a hostname that cannot resolve, so the workers fail fast
and without touching anyone's endpoint, then asserts the UI came back.
"""

from __future__ import annotations

import sys
import tkinter.messagebox as mb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from glt.control import ControlWindow  # noqa: E402

DEAD_HOST = "http://this-host-does-not-exist-glt.invalid:8317/v1"

seen: list = []
mb.showerror = lambda t, m, **k: seen.append(("error", t, str(m)))
mb.showinfo = lambda t, m, **k: seen.append(("info", t, str(m)))
mb.showwarning = lambda t, m, **k: seen.append(("warn", t, str(m)))
mb.askyesno = lambda t, m, **k: seen.append(("ask", t, str(m))) or False


def main() -> int:
    panel = ControlWindow("config.example.json")
    panel.v_backend.set("openai")
    panel._update_key_status()
    panel.v_base.set(DEAD_HOST)
    panel.v_key.set("not-a-real-token")
    panel.cfg.translate.timeout_s = 3.0

    failures: list = []

    def check_fetch():
        state, text = str(panel.btn_models["state"]), panel.btn_models["text"]
        print(f"  fetch: state={state} text={text}")
        if state != "normal" or text != "拉取模型":
            failures.append("按钮没有从「拉取中…」恢复")
        if not any(kind == "error" for kind, _, _ in seen):
            failures.append("失败了却没有弹出错误")
        # The worker must have targeted the selected backend, not the one the
        # config file names.
        if any("google" in message for _, _, message in seen):
            failures.append("查询了下拉框以外的后端")
        panel._diagnose_endpoint()
        panel.root.after(16000, check_probe)

    def check_probe():
        if not any(title == "端点诊断" or "诊断" in title
                   for _, title, _ in seen):
            failures.append("诊断没有给出结果")
        for kind, title, message in seen:
            print(f"  [{kind}] {title}: {message.splitlines()[0][:90]}")
        panel.root.destroy()

    panel.root.after(400, panel._fetch_models)
    panel.root.after(11000, check_fetch)
    panel.run()

    if failures:
        print("\nFAIL")
        for item in failures:
            print("  -", item)
        return 1
    print("\nPASS - 后台任务失败后界面能恢复")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
