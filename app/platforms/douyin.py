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

## 地址形态（2026-10-27 用户实测）

抖音网页版**点开视频时地址栏不变**，只在 query 上加 `modal_id=<id>`。所以用户从搜索结果页
复制出来的链接长这样：

    https://www.douyin.com/search/凯心?aid=…&modal_id=7700460182866331855&type=general

而 yt-dlp 的 `Douyin` 提取器只认 `/video/<id>`（前者连 `[generic]` 都认不出来，直接
`Unsupported URL`）。归一化统一在 `app/core/urls.py::canonical_url()` 里做、并由
`fetcher.download()` 在**分发点**执行 —— 本类不重复实现，否则加新平台时又会漏。
本类只保留「这地址到底是什么」的判定（`douyin_url_kind`），用来在开浏览器之前就拒绝
注定失败的任务。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core import paths as paths_mod
from app.core.errors import StageError
from app.core.ffmpeg_locator import find_ffmpeg
from app.core.fetcher import probe_duration_sec
from app.core.fetcher import DownloadResult
from app.core.urls import canonical_url, douyin_url_kind
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
        url = canonical_url(str(getattr(task, "url", "") or ""))
        if not url:
            raise NotSupported("任务没有 url")

        # 先认清这条地址是什么，再决定要不要开浏览器。
        # 开一个搜索页 / 作者主页永远抓不到播放流 —— 现在的实现要白等 6+9+12 秒
        # 的三次重试，然后回退 yt-dlp 再失败一次，用户拿到的是
        # `Unsupported URL: https://www.douyin.com/search/...` 这种英文原文。
        # 形态判定是纯字符串运算，早失败 0 成本，且提示能直接照着做。
        kind = douyin_url_kind(url)
        if kind == "author":
            raise StageError(
                "download",
                "这是一条**作者主页**链接，不是单个视频。\n"
                "  想下这位作者的作品：用「批量」入口粘贴主页链接再勾选；\n"
                "  想下其中一条：在抖音里点开那个视频，再复制地址栏"
                "（地址里会带 modal_id=<一串数字>）。",
            )
        if kind == "note":
            raise StageError(
                "download",
                "这是**图文作品**（抖音「图文 / 图集」），里面没有视频流，无法转写。\n"
                "  请换一条视频作品。",
            )
        if kind == "page":
            raise StageError(
                "download",
                "这条抖音链接里没有视频 id —— 看起来是搜索结果页 / 发现页 / 直播页。\n"
                "  请先点开具体的视频，再复制地址栏：那时地址里会带 modal_id=<一串数字>，\n"
                "  我们据此定位到单条视频。也可以直接用 App 的「分享 → 复制链接」短链。",
            )

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
