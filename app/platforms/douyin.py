"""抖音平台：**浏览器取流**为主，yt-dlp + cookie 为回退。

## 两条路的取舍

| 路 | 需要什么 | 状态 |
|---|---|---|
| **浏览器取流**（默认） | 系统 Edge（Win10/11 自带） | ✅ 2026-10-26 端到端验证通过 |
| yt-dlp + cookies.txt（回退） | 用户手动导出 cookie | 已有能力，保留 |

为什么浏览器取流是默认：抖音对匿名请求要求新鲜会话（`ttwid` 24h 过期），
手动导出 cookie 到期就得重来；而"用真浏览器打开、让它自己播、我们抓它请求的
两路 DASH 流"这条路**不需要任何账号**（实测匿名可播）。

实现细节见 `douyin_browser.py` 的模块 docstring。

## 关于"用户网页端已登录"

playwright 启动的是**独立 profile**，看不到用户 Edge/Chrome 里的登录态
（Chrome 127+ 的 App-Bound Encryption 也让直接读 cookie 库不可行）。
所以产品上的做法是：**首次弹一个有头窗口让用户扫码登录一次**，登录态存在
`<app_data>/browser-profile/`，之后无头复用。这就是"请先登录"提示的由来。

⚠️ 即使登录了，**匿名也能下单个视频**；登录真正影响的是「作者作品列表能翻多少页」。
实测匿名只拿到首屏约 14 条，登录后是否改善**未验证**（矩阵里如实标 ⚠️）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core import paths as paths_mod
from app.core.ffmpeg_locator import find_ffmpeg
from app.core.fetcher import probe_duration_sec
from app.core.fetcher import DownloadResult
from app.platforms import douyin_browser as db
from app.platforms.base import AuthorItem, AuthorPage, NotSupported
from app.platforms.ytdlp import YtDlpProvider


class DouyinProvider(YtDlpProvider):
    name = "douyin"
    #: 现在**声称支持**作者列表 —— 但只保证首屏（见 list_author 的注释）。
    supports_author = True

    # ------------------------------------------------------------ 内部

    def _profile(self) -> Path:
        return db.profile_dir(paths_mod.app_data_dir())

    def _ffmpeg(self, cfg: object) -> Path:
        explicit = getattr(getattr(cfg, "paths", None), "ffmpeg", None)
        return find_ffmpeg(explicit)

    # ------------------------------------------------------------ 下载

    def download(self, task, cfg, out_dir, on_progress=None, cancel_event=None):
        """先走浏览器取流；**失败再回退** yt-dlp（有 cookie 时才有意义）。"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        url = str(getattr(task, "url", "") or "")
        if not url:
            raise NotSupported("任务没有 url")

        streams = None
        reason = ""
        try:
            streams = db.capture_streams(url, self._profile())
        except db.BrowserUnavailable as exc:
            reason = str(exc)
            logging.warning("抖音取流失败：%s", reason)
            streams = None

        if streams is not None and streams.usable:
            ffmpeg = self._ffmpeg(cfg)
            dst = out / "video.mp4"
            db.download_streams(streams, dst, str(ffmpeg), on_progress, cancel_event)
            probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            duration = probe_duration_sec(dst, str(probe) if probe.is_file() else None) or 0
            return DownloadResult(
                video_path=dst,
                title=streams.title or None,
                duration_sec=duration or None,
                video_id=_video_id(streams.final_url or url),
                platform=self.name,
            )

        # ---- 回退：yt-dlp + cookie ----
        cookies = getattr(getattr(cfg, "paths", None), "cookies_file", None)
        if not (cookies and Path(cookies).is_file()):
            raise NotSupported(
                "抖音取流失败，且没有可用的 cookies.txt 作为回退。\n"
                + (f"  原因：{reason}\n" if reason else "")
                + "  1) 先在设置里点「抖音登录」（只需一次，之后无头复用）；\n"
                "  2) 或在设置里指定 cookies.txt；\n"
                "  3) 用 python tools/probe_douyin.py <链接> --headed 看现场。"
            )
        logging.warning("抖音浏览器取流失败，回退到 yt-dlp + cookies")
        return super().download(task, cfg, out, on_progress, cancel_event)

    # ------------------------------------------------------------ 作者作品

    def list_author(self, url: str, cfg: object, limit: int = 0) -> AuthorPage:
        profile = self._profile()
        logged = db.has_login(profile)
        try:
            name, pairs = db.list_author_items(url, profile, limit=limit)
        except db.BrowserUnavailable as exc:
            raise NotSupported(f"抖音作者列表需要浏览器：{exc}") from exc

        items = [
            AuthorItem(url=u, title=t, item_id=_video_id(u), duration=0, thumb="")
            for u, t in pairs
        ]
        if not items:
            raise NotSupported(
                "没有从作者主页读到作品列表。\n"
                + (
                    "当前是**未登录**状态：抖音对匿名访问只给首屏、且常被导到别处。\n"
                    "  请先在设置里点「抖音登录」，再试一次。"
                    if not logged
                    else "已登录但仍是 0 条 —— 该主页可能设了私密，或抖音改版了选择器。"
                )
            )

        # 如实标注：匿名只能拿到首屏，翻页能力未验证。
        truncated = True if (limit and len(items) >= limit) else (not logged)
        return AuthorPage(
            platform=self.name,
            title=name,
            items=items,
            total=len(items),
            truncated=truncated,
        )


def _video_id(url: str) -> str | None:
    """从 `/video/<id>` 里取 id；短链没有 id，返回 None。"""
    parts = [p for p in url.split("?")[0].split("/") if p]
    for i, p in enumerate(parts):
        if p == "video" and i + 1 < len(parts) and parts[i + 1].isdigit():
            return parts[i + 1]
    return None
