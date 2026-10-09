"""fetcher 失败翻译自测：cookie 嗅探与抖音预检（纯逻辑，不联网、不需要 yt-dlp）。

判据来源：2026-10-10 真实事故——抖音链接在未配置 cookie 时裸跑 yt-dlp，报
"ERROR: [Douyin] <id>: Fresh cookies (not necessarily logged in) are needed"，
用户看不出下一步该做什么。修复 = 预检（douyin 缺 cookie 直接给动作）+
输出嗅探（配了但过期 → 同样的动作提示），见 `app/core/fetcher.py`。

用法：python tools/fetcher_selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import fetcher  # noqa: E402
from app.core.errors import CookieExpired, StageError  # noqa: E402
from app.models.config import Config  # noqa: E402
from app.models.task import Task  # noqa: E402

#: 2026-10-10 事故的 yt-dlp 输出尾（原文，逐字）。
_REAL_TAIL = [
    "[generic] Extracting URL: https://v.douyin.com/ye_7vRWF3tc",
    "[generic] ye_7vRWF3tc: Downloading webpage",
    "[redirect] Following redirect to https://www.douyin.com/video/7685636503682157887",
    "[Douyin] Extracting URL: https://www.douyin.com/video/7685636503682157887",
    "[Douyin] 7685636503682157887: Downloading web detail JSON",
    "ERROR: [Douyin] 7685636503682157887: Fresh cookies (not necessarily logged in) are needed",
]

_PASS = 0
_FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS
    if ok:
        _PASS += 1
        print(f"  PASS  {name}" + (f"  — {detail}" if detail else ""))
    else:
        _FAIL.append(name)
        print(f"  FAIL  {name}" + (f"  — {detail}" if detail else ""))


def _sniff(lines: list[str]) -> bool:
    return any(fetcher._COOKIE_ERROR_RE.search(line) for line in lines)


def _sentinel_yt_dlp(explicit: str | None) -> str:
    """占位：证明调用方已越过 cookie 预检、到达 yt-dlp 定位这一步。"""
    raise StageError("download", "sentinel-no-ytdlp")


def main() -> int:
    print("fetcher 自测 —— cookie 失败翻译（纯逻辑，不联网）")

    # ---- 嗅探 ----
    check("嗅探命中 2026-10-10 事故原文", _sniff(_REAL_TAIL))
    check("嗅探命中 'Cookies may be stale'（大小写不敏感）", _sniff(["ERROR: Cookies may be stale for this domain"]))
    check("嗅探不误报普通网络失败", not _sniff(["ERROR: Unable to download webpage: HTTP 403"]))
    check("嗅探不误报正常输出", not _sniff(["[download] 100% of 44.11MiB"]))

    # ---- 预检：把 find_yt_dlp 换成哨兵，越过预检即抛 sentinel ----
    orig = fetcher.find_yt_dlp
    fetcher.find_yt_dlp = _sentinel_yt_dlp
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            out = root / "out"
            cfg = Config.from_dict({}, root=root)
            douyin = Task(url="https://v.douyin.com/ye_7vRWF3tc", platform="douyin")
            bili = Task(url="https://www.bilibili.com/video/BV1xx411c7mD", platform="bilibili")

            # 抖音 + 未配置 cookie → CookieExpired，首行即动作
            try:
                fetcher._download_remote(douyin, out, cfg)
                check("抖音缺 cookie 抛 CookieExpired", False, "没有抛异常")
            except CookieExpired as exc:
                check(
                    "抖音缺 cookie 抛 CookieExpired，首行含动作",
                    "refresh_cookies" in str(exc),
                    str(exc).splitlines()[0][:60],
                )
            except StageError as exc:
                check("抖音缺 cookie 抛 CookieExpired", False, f"抛了别的：{exc}")

            # 抖音 + cookie 文件在 → 越过预检，到达 yt-dlp 定位（哨兵）
            cookies = root / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            cfg.paths.cookies_file = str(cookies)
            try:
                fetcher._download_remote(douyin, out, cfg)
                check("抖音 cookie 在位时越过预检", False, "没有抛异常")
            except StageError as exc:
                check("抖音 cookie 在位时越过预检", "sentinel" in str(exc), str(exc))

            # 其它平台不设 cookie 门槛
            cfg.paths.cookies_file = None
            try:
                fetcher._download_remote(bili, out, cfg)
                check("B站无 cookie 不受预检拦截", False, "没有抛异常")
            except StageError as exc:
                check("B站无 cookie 不受预检拦截", "sentinel" in str(exc), str(exc))
    finally:
        fetcher.find_yt_dlp = orig

    print(f"\nfetcher 自测: {_PASS}/{_PASS + len(_FAIL)} 通过")
    for name in _FAIL:
        print(f"  ✗ {name}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
