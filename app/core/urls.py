"""输入解析：从用户粘贴的文本中抽出可下载目标（PLAN.md §7.3）。

用户实际粘贴的往往是**分享文案**而不是干净链接，典型形态：

    8.25 复制打开抖音，看看【某创作者的作品】【标题…】…
    https://v.douyin.com/AbCdEf12345/ vFh:/ 03/30 :3pm b@n.qR

所以先按平台正则抽取，**全部失败再把整行原样交给 yt-dlp**——它是通用
extractor，可能能处理我们没覆盖的站内形态。

本模块是纯函数、无外部依赖，`cli.py` / `rpc.py` / `fetcher.py` 共用。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

#: 平台 → 正则。按顺序尝试，**先匹配到的胜出**。
#: 每个平台都要覆盖短链与站内长链两种形态，否则用户从 App 分享和从网页复制的链接
#: 会走两条不同路径。
URL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        (
            "douyin",
            r"https?://(?:v\.douyin\.com/[A-Za-z0-9_\-]+"
            r"|(?:www\.)?douyin\.com/video/\d+"
            # 作者主页（批量下载的入口）。2026-10-26 漏了这一条：作者 URL 落到
            # unknown ⇒ 被丢给 yt-dlp ⇒ 超时 120 秒，而浏览器取流那条路根本没被调用。
            r"|(?:www\.)?douyin\.com/user/[A-Za-z0-9_\-]+"
            r"|(?:www\.)?iesdouyin\.com/share/video/\d+)",
        ),
        (
            "bilibili",
            r"https?://(?:b23\.tv/[A-Za-z0-9]+"
            r"|(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]+|av\d+))",
        ),
        (
            "youtube",
            r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)"
            r"[A-Za-z0-9_\-]+",
        ),
        (
            # X（原 Twitter）。yt-dlp 对 twitter/x 都支持，所以只需识别、不需要专用实现。
            # 注意 `x.com` 与 `twitter.com` 是同一站点，别只写一个。
            "twitter",
            r"https?://(?:www\.)?(?:twitter\.com|x\.com)/[A-Za-z0-9_]+/status/\d+",
        ),
    )
)

#: 本地视频文件的常见扩展名。
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".mov", ".webm", ".flv", ".avi", ".m4v", ".ts", ".wmv"}
)

LOCAL_PLATFORM = "local"
#: 正则全部失败时的标记。**不代表失败**——调用方应把整行交给 yt-dlp 再试一次。
UNKNOWN_PLATFORM = "unknown"

#: 从任意文本里抓一个 URL。只用于 `URL_PATTERNS` 不中之后的**主机名兜底**：
#: 站内页形态（搜索结果页、发现页、带 `modal_id` 的主页）无法用「路径正则」穷举，
#: 但主机名是稳定的。
#:
#: 排除中文标点是因为分享文案里 URL 后面常常直接跟中文（`…/AbCd 复制此链接`），
#: 不排除就会把后半句吞进 URL。
_ANY_URL_RE = re.compile(r"https?://[^\s<>\"'`，。、；：！？（）【】「」《》]+", re.IGNORECASE)

#: 主机后缀 → 平台。用 `endswith` 匹配，所以 `v.douyin.com` / `m.bilibili.com`
#: 都自动覆盖，不用逐个子域写一遍。
_HOST_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("douyin.com", "douyin"),
    ("iesdouyin.com", "douyin"),
    ("bilibili.com", "bilibili"),
    ("b23.tv", "bilibili"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("twitter.com", "twitter"),
    ("x.com", "twitter"),
)

#: 抖音视频页的规范形态。yt-dlp 的 `Douyin` 提取器只认这一种（见 `canonical_url`）。
_DOUYIN_VIDEO_BASE = "https://www.douyin.com/video/"
#: `/video/<id>`、`/share/video/<id>`、`/root/share/video/<id>` 都能被 `search` 命中。
_DOUYIN_VIDEO_PATH_RE = re.compile(r"/(?:share/)?video/(\d{6,})")
#: 网页端点开视频后，地址栏**不变**，只在 query 上加 `modal_id=<id>`。
_DOUYIN_MODAL_RE = re.compile(r"(?:^|[?&])modal_id=(\d{6,})(?:&|$)")


@dataclass(frozen=True)
class InputItem:
    """一条解析结果。"""

    raw: str
    """用户原始输入行，用于错误提示时回显。"""

    target: str
    """抽取出的 URL，或本地文件的绝对路径。"""

    platform: str
    """`douyin` | `bilibili` | `youtube` | `local` | `unknown`。"""


def _split(url: str):
    """`urlsplit` 的容错包装：畸形 URL 返回 None 而不是抛异常。"""
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _host_platform(url: str) -> str | None:
    """按主机名判平台。判不出返回 None。"""
    parts = _split(url)
    host = (parts.hostname or "").lower() if parts else ""
    if not host:
        return None
    for suffix, platform in _HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return platform
    return None


def canonical_url(url: str) -> str:
    """把「同一段视频的不同地址形态」收敛成一种。

    ## 为什么必须有这一步（2026-10-27 用户实测）

    抖音网页版**在搜索结果页点开视频时，地址栏是不变的** —— 只是 query 上多了
    `modal_id=<视频id>`：

        https://www.douyin.com/search/凯心?aid=…&modal_id=7700460182866331855&type=general

    用户从 App 分享得到的是 `v.douyin.com/xxx` 短链，从网页复制得到的是上面这种。
    两种都是"能看的链接"，但 yt-dlp 的 `Douyin` 提取器只认 `/video/<id>`：

        $ yt-dlp --simulate 'https://www.douyin.com/search/x?modal_id=7700460182866331855'
        [generic] … ERROR: Unsupported URL: …            ← 提取器压根没认领
        $ yt-dlp --simulate 'https://www.douyin.com/video/7700460182866331855'
        [Douyin] … ERROR: [Douyin] Fresh cookies are needed ← 认了，只差会话

    **同一个视频 id，两种形态，前者连"这是抖音视频"都没识别出来。** 于是不管
    走哪条下载路（浏览器取流也只打 `/video/` 页），都必须先归一。

    短链刻意**不展开**：`v.douyin.com/xxx` 必须靠一次 302 才知道真实 id，
    本地无法凭空算出，强行改写只会造出一个不存在的地址。

    幂等：`canonical_url(canonical_url(x)) == canonical_url(x)`。
    """
    text = (url or "").strip()
    if not text or "://" not in text:
        return text
    if _host_platform(text) != "douyin":
        return text

    parts = _split(text)
    if parts is None:
        return text
    host = (parts.hostname or "").lower()

    # 短链：只有网络跳转才能拿到 id
    if host == "v.douyin.com":
        return text

    # 路径里已经有 id（`/video/<id>` / `/share/video/<id>`）→ 去掉 query 收敛
    match = _DOUYIN_VIDEO_PATH_RE.search(parts.path)
    if not match:
        # 搜索页 / 发现页 / 作者主页点开视频后：id 在 query 里
        match = _DOUYIN_MODAL_RE.search(parts.query)
    if match:
        return f"{_DOUYIN_VIDEO_BASE}{match.group(1)}"
    return text


def douyin_url_kind(url: str) -> str:
    """抖音地址的形态，用于**给出能照做的错误提示**。

    返回 `video` / `short` / `author` / `note` / `page`：

    - `video`：已归一（或本来就是）单条视频页，可以取流
    - `short`：短链，要打开才知道是视频还是主页
    - `author`：作者主页 —— 想下全部作品该走「批量」，不该走单条下载
    - `note`：图文作品（`/note/<id>` / `/slides/<id>`），**没有视频流**
    - `page`：搜索页 / 发现页 / 直播页等等，没有视频 id，下不了
    """
    text = (url or "").strip()
    parts = _split(text)
    if parts is None:
        return "page"
    host = (parts.hostname or "").lower()
    if host == "v.douyin.com":
        return "short"
    if _DOUYIN_VIDEO_PATH_RE.search(parts.path) or _DOUYIN_MODAL_RE.search(parts.query):
        return "video"
    if re.search(r"/(?:note|slides)/\d{6,}", parts.path):
        return "note"
    if re.search(r"^/user/[A-Za-z0-9_\-]+", parts.path):
        return "author"
    return "page"


def detect_platform(text: str) -> tuple[str, str] | None:
    """从任意文本中抽取第一个平台 URL，返回 `(平台, url)`；无匹配返回 None。

    两段式：**先用路径正则**（能精确截断 query、能认出短链），**再用主机名兜底**。
    第二段是 2026-10-27 补的：抖音的搜索结果页 `/search/<关键词>?modal_id=<id>`
    任何路径正则都没覆盖，于是落到 `unknown` 被丢给 yt-dlp 的 generic 提取器，
    用户看到的是一句英文的 `Unsupported URL` —— 而它其实**是一条正常的抖音视频**。
    """
    for name, pattern in URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return name, match.group(0)

    match = _ANY_URL_RE.search(text)
    if match:
        url = match.group(0)
        platform = _host_platform(url)
        if platform:
            return platform, url
    return None


def looks_like_local_file(text: str) -> bool:
    """判断一行是否指向本地视频文件。

    先看扩展名，再看文件是否真的存在——两者任一成立即认为「用户想传本地文件」，
    这样即便文件已被移走，也能给出「文件不存在」而不是「链接无法识别」。
    """
    if not text or "://" in text:
        return False
    try:
        path = Path(text.strip().strip('"').strip("'")).expanduser()
    except (OSError, ValueError):
        return False
    if path.suffix.lower() in VIDEO_SUFFIXES:
        return True
    try:
        return path.is_file()
    except OSError:
        return False


def extract(line: str) -> InputItem | None:
    """解析一行输入。空行返回 None。

    ⚠️ `InputItem.target` 是**归一化后**的地址（见 `canonical_url`），不是文中的
    原文；`InputItem.raw` 才保留原文。落库、去重、下载都用 `target` —— 同一个
    视频的「搜索页形态」和「短链形态」因此能被认成两条不同的输入，但
    「搜索页形态」与「`/video/` 形态」会收敛成同一条。
    """
    raw = line.strip()
    if not raw:
        return None

    # 本地文件优先：路径里通常不会有 `https://`，但先判可以少走正则
    if looks_like_local_file(raw):
        return InputItem(raw=raw, target=str(Path(raw).expanduser()), platform=LOCAL_PLATFORM)

    hit = detect_platform(raw)
    if hit:
        platform, url = hit
        target = canonical_url(url)
        if target != url:
            logging.info("URL 归一化：%s → %s", url, target)
        return InputItem(raw=raw, target=target, platform=platform)

    # 兜底：整行原样交出，由 yt-dlp 的通用 extractor 尝试
    return InputItem(raw=raw, target=raw, platform=UNKNOWN_PLATFORM)


def extract_many(lines: Iterable[str], dedupe: bool = True) -> list[InputItem]:
    """批量解析。`dedupe=True` 时按 `target` 去重（批内重复没必要入队两次）。

    跨批次的重复由 `store` 的 `url UNIQUE` 约束负责。
    """
    items: list[InputItem] = []
    seen: set[str] = set()
    for line in lines:
        item = extract(line)
        if item is None:
            continue
        if dedupe:
            if item.target in seen:
                continue
            seen.add(item.target)
        items.append(item)
    return items


def extract_file(path: str | Path) -> list[InputItem]:
    """从文本文件（每行一条）批量解析。PLAN.md §1 的输入形态③。"""
    p = Path(path).expanduser()
    text = p.read_text(encoding="utf-8", errors="replace")
    return extract_many(text.splitlines())
