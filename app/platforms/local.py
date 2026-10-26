"""本地文件：没有"平台"可言，也没有作者作品。

存在的意义是让 `registry.resolve()` 对所有输入都能给出一个 provider ——
调用方不必再写 `if platform == "local"` 这种分支（`fetcher.download()` 里原先
就有这样一处，加平台时最容易被漏掉）。
"""

from __future__ import annotations

from pathlib import Path

from app.platforms.base import AuthorPage, NotSupported
from app.platforms.ytdlp import YtDlpProvider


class LocalProvider(YtDlpProvider):
    name = "local"
    #: 本地文件当然没有"作者作品"。
    supports_author = False

    def list_author(self, url: str, cfg: object, limit: int = 0) -> AuthorPage:
        raise NotSupported("本地文件没有作者作品列表。")

    def download(self, task, cfg, out_dir, on_progress=None, cancel_event=None):
        """走 `fetcher.take_local()`（硬链接优先），**不回调 `fetcher.download()`**。"""
        from app.core import fetcher

        return fetcher.take_local(task, cfg, Path(out_dir), on_progress, cancel_event)
