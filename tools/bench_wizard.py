"""Interactive front end for the model bench.

Exists so the person running it types the credential and nothing else has to.
The key is read without echo, kept in this process's environment for the child
bench only, and never written to disk or printed. The run's output is teed to
a report file that contains the models, the timings and the translations --
and no credential, so it is safe to share.

    python tools/bench_wizard.py
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from glt import config as cfgmod  # noqa: E402

DEFAULT_BACKEND = "openai"


BACKENDS = ("openai", "anthropic")


def looks_like_a_key(value: str) -> bool:
    """Catch a credential typed into a prompt that is not the key prompt.

    It happens: the wizard says only the key is needed, then asks two other
    things first. Recognising it lets us say so plainly instead of carrying
    the secret into a code path that prints it in a traceback.
    """
    return value.startswith(("sk-", "sk_")) or len(value) > 40


def redact(text: str, secret: str) -> str:
    """Keep a credential out of anything shown or written.

    A traceback from the child process, an error page echoed by a gateway --
    either can contain the key. Nothing is printed or saved without passing
    through here.
    """
    if not secret or len(secret) < 8:
        return text
    return text.replace(secret, f"{secret[:6]}…<已隐藏>")


def ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    while True:
        value = input(shown).strip() or default
        if not looks_like_a_key(value):
            return value
        print("  ↑ 这看起来是密钥。密钥在最后一步单独问（输入时不回显）——"
              "这里填的不是密钥。\n")


def stored_base_url(backend: str) -> str:
    """Whatever the saved config already points at, so it can be accepted."""
    for name in ("config.json", "config.example.json"):
        path = ROOT / name
        if not path.exists():
            continue
        try:
            section = getattr(cfgmod.load(path).translate, backend, {})
        except Exception:  # noqa: BLE001 - a broken config is not fatal here
            continue
        if isinstance(section, dict) and section.get("base_url"):
            return section["base_url"]
    return ""


def run(args: list, env: dict, report, secret: str) -> int:
    """Run the bench, showing output live and copying it into the report.

    Every line is redacted before it reaches the screen or the file — a
    traceback or a gateway error page can carry the key, and this is the one
    place it could otherwise escape.
    """
    # Force the child to write UTF-8. Writing to a pipe on Windows it would
    # otherwise pick the console codepage, and every Chinese message would
    # arrive as mojibake through a UTF-8 decode.
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "bench_models.py"), *args],
        cwd=str(ROOT), env=dict(env, PYTHONIOENCODING="utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines = []
    for line in process.stdout:  # type: ignore[union-attr]
        safe = redact(line, secret)
        sys.stdout.write(safe)
        sys.stdout.flush()
        lines.append(safe)
    process.wait()
    if report is not None:
        report.write("".join(lines))
    return process.returncode


def main() -> int:
    print("=" * 68)
    print("模型对比向导")
    print("=" * 68)
    print("只有密钥需要你输入。密钥不回显、不写入任何文件，只传给这次的子进程。\n")

    while True:
        backend = ask("第1步 后端 (openai / anthropic)，回车用默认", DEFAULT_BACKEND)
        if backend in BACKENDS:
            break
        print(f"  ↑ 只能是 {' 或 '.join(BACKENDS)}。\n")

    while True:
        base_url = ask("第2步 端点地址，回车用检测到的", stored_base_url(backend))
        if base_url.startswith(("http://", "https://")):
            break
        print("  ↑ 地址要以 http:// 或 https:// 开头。\n")

    env_name = "ANTHROPIC_API_KEY" if backend == "anthropic" else "OPENAI_API_KEY"
    saved = os.environ.get(env_name, "").strip()
    if saved:
        # The control panel already stored one, and it demonstrably works
        # there. Reusing it skips a console paste, which is the step most
        # likely to mangle a long key.
        print(f"\n检测到已保存的密钥 ({env_name})。")
        key = getpass.getpass("第3步 直接回车沿用；要换别的就粘贴新的: ").strip() or saved
    else:
        key = getpass.getpass("第3步 密钥 (粘贴后回车，屏幕上不会显示): ").strip()
    if not key:
        print("没有密钥就没法测。", file=sys.stderr)
        return 1
    env = dict(os.environ, **{env_name: key})

    common = ["--backend", backend, "--base-url", base_url]

    print("\n--- 正在向端点索取模型列表 ---\n")
    if run(common, env, None, key) != 0:
        # Listing is a convenience, not a requirement. Plenty of relays serve
        # only chat/completions and answer /v1/models with an error; that is
        # no reason to stop, since the names can simply be typed.
        print("\n拿不到列表（上面是服务器的原话）。很多中转站不提供这个接口，")
        print("不影响测试——直接手填模型名即可，名字向中转站方要或看它的后台。")

    picked = ask("\n要测哪几个？逗号分隔的模型名，或回车全测（先筛后比）", "all")
    if not picked:
        print("没选模型，退出。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = ROOT / f"bench-{stamp}.txt"
    print(f"\n--- 开始对比，结果同时写入 {report_path.name} ---\n")
    with report_path.open("w", encoding="utf-8") as report:
        report.write(f"端点: {base_url}\n后端: {backend}\n模型: {picked}\n\n")
        code = run([*common, "--models", picked], env, report, key)

    print(f"\n报告已保存：{report_path}")
    print("这个文件里没有密钥，可以直接发出去。")
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
