"""抖音：会话要求特殊，其余委托通用实现。

## 与其他平台的区别（只有这一条）

抖音对**匿名请求也要求新鲜会话**（`ttwid` 约 24 小时过期），缺失或过期时 yt-dlp
报 `Fresh cookies (not necessarily logged in) are needed`。所以本类只做一件事：
**把「缺会话」提前成一个可操作的错误**，省掉一次注定失败的网络往返（还占风控配额）。

下载本身仍走 yt-dlp（`YtDlpProvider.download`）——抖音并没有"yt-dlp 完全不会下"的问题，
它只是需要一个 cookie 文件。

## 作者作品（批量下载）为什么还不支持

2026-10-26 实测：
- 纯 HTTP 无 cookie：短链一律 302 回首页，`iteminfo` 老接口对任何 id 都返回空 body；
- 真实浏览器（playwright，匿名）能拿到 `ttwid` 等 35 条 cookie，但**短链仍落到
  `/jingxuan`**，抓不到 `<video>` 或 `.mp4` 请求。

也就是说「列出某个作者的全部作品」目前**没有验证过任何一条通路**。按本项目约定
（不要把未验证写成已验证），这里如实返回 `NotSupported`，并给出可操作提示 ——
而不是返回一个空列表让用户以为该作者没作品。

**验证入口**：`tools/probe_douyin.py <链接>`。等它能稳定拦到直链，再来实现这里。
"""

from __future__ import annotations

from pathlib import Path

from app.core.fetcher import CookieExpired, cookie_hint
from app.platforms.base import AuthorPage, NotSupported
from app.platforms.ytdlp import YtDlpProvider


class DouyinProvider(YtDlpProvider):
    name = "douyin"
    #: 作者作品列举尚未打通（见模块 docstring），明确关掉这个能力位。
    supports_author = False

    def list_author(self, url: str, cfg: object, limit: int = 0) -> AuthorPage:
        raise NotSupported(
            "抖音的「列出作者全部作品」还没打通：\n"
            "  实测匿名访问会被导向首页，抓不到作品列表；老接口（iteminfo）已下线。\n"
            "  临时办法：在抖音里打开作者主页，把要下载的作品链接逐条复制进来。\n"
            "  排查工具：python tools/probe_douyin.py <作品链接>"
        )

    def require_session(self, cfg: object) -> None:
        """缺 cookie 文件就抛 `CookieExpired`（带可操作提示）。

        放在下载前而不是下载中：yt-dlp 的同类报错埋在几百行输出里，
        用户很难看出"其实是没配 cookie"。
        """
        cookies = getattr(getattr(cfg, "paths", None), "cookies_file", None)
        if cookies and Path(cookies).is_file():
            return
        raise CookieExpired(cookie_hint(cookies))
