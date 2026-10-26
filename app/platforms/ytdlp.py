"""用 yt-dlp 实现的通用平台（YouTube / Bilibili / Twitter(X) / 其他 yt-dlp 支持的站点）。

## 下载

沿用 `fetcher.py` 里已经验证过的那套命令（限速 / 合并 mp4 / newline 进度解析），
本模块**不重复实现**，只把「这个平台需要什么会话」这件事抽出来。
真正搬代码留到下一轮，避免一次性动下载路径 —— 那是唯一有 7 条判据护着的热路径。

## 作者作品（批量下载的核心）

`yt-dlp --flat-playlist -J <url>` 对 YouTube 频道、Bilibili 空间、Twitter 用户时间线
都能返回条目列表，且**不逐个解析视频详情**（`-J` + `--flat-playlist` 只取列表页），
所以列 200 条也很快。

⚠️ 两个必须处理的现实：

1. **条目可能没有可直接下载的 url**。`--flat-playlist` 给的是 `id` + `url`(有时是相对
   形态) + `ie_key`。归一化规则：优先 `url`（绝对化），否则按平台拼标准地址
   （YouTube `watch?v=`、Bilibili `video/BVxxx`）。拼不出就**丢弃该条** ——
   列出来却下不了，比不列更糟（见 `base.AuthorItem` 的注释）。
2. **`entries` 可能嵌套**（播放列表里套播放列表，`_type == 'playlist'`）。
   只取一层的 `url` 型条目，嵌套的跳过并计数，日志如实报出来。
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from urllib.parse import urljoin

from app.platforms.base import AuthorItem, AuthorPage, NotSupported

#: yt-dlp 列表页超时（秒）。列 200 条也够用；卡住时宁可失败也别无限等。
LIST_TIMEOUT_SEC = 120


def _ytdlp_bin(explicit: str | None) -> str:
    """定位 yt-dlp —— 复用 `fetcher.find_yt_dlp()` 的既有逻辑，不抄第二份。"""
    from app.core.fetcher import find_yt_dlp

    return find_yt_dlp(explicit)


def _abs_url(raw: str | None, base: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if not raw:
        return ""
    # 相对形态（少见）：拼到列表页上
    return urljoin(base, raw)


def _watch_url(platform: str, item_id: str) -> str:
    """按平台拼标准观看地址 —— 只在 yt-dlp 没给可用 url 时用。"""
    if not item_id:
        return ""
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={item_id}"
    if platform == "bilibili":
        return f"https://www.bilibili.com/video/{item_id}"
    if platform in ("douyin",):
        return f"https://www.douyin.com/video/{item_id}"
    if platform == "twitter":
        return ""  # 需要用户名，拼不出来，交给调用方丢弃
    return ""


def list_author_with_ytdlp(
    url: str,
    platform: str,
    ytdlp_path: str | None = None,
    limit: int = 0,
) -> AuthorPage:
    """列出一个作者/频道/合集的作品。`limit=0` 表示不限。

    解析规则见模块 docstring。任何一条 `yt-dlp` 的异常都**如实上抛**：
    「列不出来」和「这个作者没有作品」是两件不同的事，混成空列表会让用户以为
    作者没发过东西。
    """
    bin_path = _ytdlp_bin(ytdlp_path)
    cmd = [
        bin_path,
        "--flat-playlist",  # 只取列表，不解析每条详情（快一个数量级）
        "--no-warnings",
        "--no-color",
        "-J",  # 输出单个 JSON
        url,
    ]
    logging.info("列举作者作品：%s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=LIST_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise NotSupported(f"yt-dlp 无法列举该地址：{detail[-1] if detail else '未知错误'}")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise NotSupported(f"yt-dlp 输出不是 JSON：{exc}") from exc

    entries = data.get("entries") or []
    if not isinstance(entries, list):
        raise NotSupported("yt-dlp 返回的 entries 形态不认识")

    items: list[AuthorItem] = []
    skipped = 0
    for raw in entries:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        # 嵌套播放列表：跳过（一层就够，嵌套要靠用户自己点进去）
        if raw.get("_type") in ("playlist", "multi_video"):
            skipped += 1
            continue
        item_id = str(raw.get("id") or "")
        target = _abs_url(raw.get("url") or raw.get("webpage_url"), url) or _watch_url(
            platform, item_id
        )
        if not target:
            skipped += 1
            continue
        items.append(
            AuthorItem(
                url=target,
                title=str(raw.get("title") or "").strip(),
                duration=int(raw.get("duration") or 0),
                item_id=item_id,
                thumb=_pick_thumb(raw),
            )
        )
        if limit and len(items) >= limit:
            break

    truncated = bool(limit) and len(items) >= limit
    if skipped:
        logging.warning("列举时跳过 %d 条（嵌套列表或拼不出可下载地址）", skipped)

    return AuthorPage(
        platform=platform,
        title=str(data.get("title") or data.get("uploader") or data.get("channel") or ""),
        items=items,
        total=int(data.get("playlist_count") or len(entries) or 0),
        truncated=truncated,
    )


def _pick_thumb(raw: dict) -> str:
    thumbs = raw.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        last = thumbs[-1]
        if isinstance(last, dict) and last.get("url"):
            return str(last["url"])
    if raw.get("thumbnail"):
        return str(raw["thumbnail"])
    return ""


class YtDlpProvider:
    """通用平台实现。子类只在**确有不同**时覆写（例如抖音的会话预检）。"""

    name = "unknown"
    supports_author = True

    def list_author(self, url: str, cfg: object, limit: int = 0) -> AuthorPage:
        paths = getattr(cfg, "paths", None)
        return list_author_with_ytdlp(
            url,
            self.name,
            ytdlp_path=getattr(paths, "yt_dlp", None),
            limit=limit,
        )

    def download(self, task, cfg, out_dir, on_progress=None, cancel_event=None):
        """走 `fetcher.download_ytdlp()` —— **不能回调 `fetcher.download()`**。

        `fetcher.download()` 现在是分发器（会调回 provider），回调它就是无限递归。
        """
        from app.core import fetcher

        return fetcher.download_ytdlp(task, cfg, Path(out_dir), on_progress, cancel_event)
