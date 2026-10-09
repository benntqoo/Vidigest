"""手动刷新抖音 cookie —— 阶段 4 自动化（cookie.auto_refresh）之前的过渡手段。

为什么存在：抖音对匿名请求也要求新鲜 cookie（ttwid 约 24 小时过期），缺失或
过期时 yt-dlp 报 "Fresh cookies (not necessarily logged in) are needed"。
`--cookies-from-browser` 在 Windows 新版浏览器上必失败（PLAN.md §9.5），
唯一可行路径是 playwright 会话导出 storage-state，再转 Netscape 格式
（转换逻辑复用已验证原型 `prototypes/state2cookie.py`，不另写一份）。

用法（仓库根目录）：
    python tools/refresh_cookies.py [输出路径]        # 默认 cookies.txt
    python tools/refresh_cookies.py --convert state.json [输出路径]
                                                     # 自动化失败时的人工兜底

前置：npm i -g @playwright/cli（Node.js 仅此用途需要）。

⚠️ 未验证标记：playwright-cli 的 `open` / `state-save` 行为按 PLAN.md §9.5 的
记录实现（open 后会话只活几秒，state-save 必须紧跟——所以 open 放后台进程，
主线程稍候即 save，不依赖人工手速）。首次真实运行如失败，按提示走 --convert。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOUYIN_URL = "https://www.douyin.com"

#: 等页面加载、服务端种下 ttwid 的时间；也兼容 open 前台阻塞的形态。
WAIT_SEC = 8.0


def _die(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def _convert(state_json: Path, out: Path) -> None:
    """state.json → Netscape cookies.txt（复用已验证原型）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "prototypes" / "state2cookie.py"),
         str(state_json), str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(proc.stdout.strip())
    if proc.returncode != 0:
        _die(f"state2cookie 转换失败（exit {proc.returncode}）：\n{proc.stderr.strip()}")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--convert"]
    convert_only = "--convert" in argv[1:]

    if convert_only:
        if not args:
            _die("用法：refresh_cookies.py --convert <state.json> [输出路径]")
        _convert(Path(args[0]), Path(args[1]) if len(args) > 1 else ROOT / "cookies.txt")
        return 0

    out = Path(args[0]) if args else ROOT / "cookies.txt"

    cli = shutil.which("playwright-cli")
    if cli is None:
        _die(
            "未找到 playwright-cli。安装：npm i -g @playwright/cli\n"
            "（装完重跑本脚本；或手动执行 open/state-save 后走 --convert 兜底）"
        )

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        state = Path(tmp) / "state.json"
        # open 可能前台阻塞（浏览器活着），也可能秒退（留守护会话）——两种都兼容：
        proc = subprocess.Popen(
            [cli, "open", DOUYIN_URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(WAIT_SEC)
            save = subprocess.run(
                [cli, "state-save", str(state)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
        finally:
            proc.terminate()

        if save.returncode != 0 or not state.is_file():
            _die(
                "state-save 失败（playwright 会话可能已关）。兜底：开两个终端，"
                "先 `playwright-cli open https://www.douyin.com`，几秒内在另一个终端"
                f"`playwright-cli state-save {state}`，然后重跑本脚本：\n"
                f"  python tools/refresh_cookies.py --convert {state} {out}\n"
                f"原始输出：{save.stdout.strip()} {save.stderr.strip()}"
            )

        _convert(state, out)

    text = out.read_text(encoding="utf-8")
    if "\tttwid\t" not in text:
        print(
            "⚠️ 转换完成但未找到 ttwid —— cookie 可能不含抖音会话，链接大概率仍失败。\n"
            "  重跑本脚本，并确认 open 期间浏览器真的加载了抖音首页。",
            file=sys.stderr,
        )
        return 1

    print(f"完成：{out}")
    print("下一步：config.yaml 的 paths.cookies_file 指向该文件，然后在失败行点「重试」。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
