"""平台能力层的**接口定义**（多平台 / 批量下载的底座）。

## 为什么需要这一层

产品要支持的不止抖音：YouTube / Bilibili / Twitter(X) 等主流平台，各自
"拿到视频"的方式不同（有的纯 yt-dlp 就够，有的必须先有会话 cookie，有的还要
浏览器兜底）。如果把这些差异散在 `fetcher.py` 的 if/else 里，加一个平台就要改
三四处，而且每处都可能漏。

所以把差异收敛到**两个能力**上：

| 能力 | 谁需要 | 差异点 |
|---|---|---|
| `download(task)` | 全部 | 视频字节怎么拿到（yt-dlp / 直链 / 本地文件） |
| `list_author(url)` | 批量下载 | 作者的作品列表怎么枚举 |

**不做「每个平台一个类」**：yt-dlp 已覆盖 1800+ 站点，为 B 站/YouTube 各写一份
下载实现是重复劳动，而且它们的行为本该由 yt-dlp 统一维护。真正的差异只有
「会话」与「作者枚举」两处，所以默认实现（`ytdlp.py`）能覆盖大多数平台，
`douyin.py` 这类只在**确有不同**时才覆写。

本模块只放**数据结构与协议**，不放任何网络或进程调用 —— 这样判据可以脱离
网络验证「平台匹配」「条目归一化」这些最容易写错的部分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

# ---------------------------------------------------------------- 作者作品


@dataclass(frozen=True)
class AuthorItem:
    """作者作品列表里的一条。`url` 必须能**直接**交给下载流程。

    这是刻意的：勾选后批量加入队列时，直接把 `url` 丢给现有的 `add_tasks`，
    不需要"从 id 反查地址"的第二套逻辑。凡是拿不到可下载地址的条目，
    **宁可不列出来** —— 列出来却下不了，比不列更糟。
    """

    url: str
    title: str = ""
    #: 秒；未知填 0（UI 显示 `--`）
    duration: int = 0
    #: 平台内部 id（去重与展示用）
    item_id: str = ""
    #: 缩略图地址，可为空
    thumb: str = ""


@dataclass
class AuthorPage:
    """一次作者作品列举的结果。"""

    platform: str
    #: 作者/频道/合集的显示名
    title: str = ""
    items: list[AuthorItem] = field(default_factory=list)
    #: 平台声称的总数（拿不到就是 0）
    total: int = 0
    #: 因为 `limit` 被截断了吗 —— UI 要如实说"还有更多"，而不是假装就这么多
    truncated: bool = False


class NotSupported(Exception):
    """该平台不支持这个能力（例如「本地文件」没有作者作品）。

    调用方应把它翻译成用户可读的提示，而不是当成错误弹栈。
    """


# ---------------------------------------------------------------- 平台协议


class PlatformProvider(Protocol):
    """一个平台的获取方式。

    实现者只需要保证：`download()` 落一个视频文件到 `out_dir`；`list_author()`
    给出**可直接下载**的条目。其余（抽音频、转写、抽帧）与平台无关，不要放进来。
    """

    #: 与 `urls.py` 里的平台名一致（`douyin` / `bilibili` / `youtube` / `local`）
    name: str

    #: 是否支持「列出作者作品」。UI 据此决定要不要显示批量入口。
    supports_author: bool

    def download(
        self,
        task: object,
        cfg: object,
        out_dir: object,
        on_progress: Callable[[str, float], None] | None = None,
        cancel_event: object | None = None,
    ) -> object:
        ...

    def list_author(self, url: str, cfg: object, limit: int = 0) -> AuthorPage:
        ...


@runtime_checkable
class SupportsAuthor(Protocol):
    """仅用于类型标注：具备 `list_author` 的实现。"""

    def list_author(self, url: str, cfg: object, limit: int = 0) -> AuthorPage:
        ...
