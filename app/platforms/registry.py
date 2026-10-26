"""平台注册表：**一个 URL → 一个 provider**。

## 为什么要有这一层

`fetcher.py` 现在是「if 本地 else 调 yt-dlp + 一句抖音 cookie 预检」。加平台时
最危险的失误不是"新平台不支持"，而是**漏改某处分支**（例如批量下载那边忘了
给抖音加会话要求）。把"哪个平台归谁管"收在一处，新增平台只动这一个文件。

## 匹配顺序

1. 调用方显式给了 `platform`（`Task.platform` 已落库）→ 直接用它
2. 否则用 `urls.detect_platform()` 现判
3. 都不中 → 视为 `unknown`，落到通用 yt-dlp 实现（yt-dlp 支持 1800+ 站点，
   我们没覆盖的站也可能能下 —— 这就是 `UNKNOWN_PLATFORM` 不等于失败的原因）
"""

from __future__ import annotations

from app.core.urls import LOCAL_PLATFORM, UNKNOWN_PLATFORM, detect_platform, looks_like_local_file
from app.platforms.douyin import DouyinProvider
from app.platforms.local import LocalProvider
from app.platforms.ytdlp import YtDlpProvider

def _generic(platform: str) -> YtDlpProvider:
    """按平台名造一个通用实例。

    **每个平台一个独立实例**：`name` 决定「作者列举」时怎么拼标准地址，
    共用同一个实例会让 A 平台的调用看到 B 平台的 name（并发下就是数据竞争）。
    """
    provider = YtDlpProvider()
    provider.name = platform
    return provider


_INSTANCES: dict[str, object] = {
    "youtube": _generic("youtube"),
    "bilibili": _generic("bilibili"),
    "twitter": _generic("twitter"),
    "douyin": DouyinProvider(),
    LOCAL_PLATFORM: LocalProvider(),
}


def resolve(url: str, platform: str | None = None):
    """返回该 URL 对应的 provider。**永不返回 None**（最差也是通用 yt-dlp）。"""
    name = (platform or "").strip()
    if not name:
        # 本地路径不是 URL，正则一定匹配不到 —— 必须先判本地，
        # 否则 `C:/tmp/a.mp4` 会被当成 unknown 丢给 yt-dlp。
        if looks_like_local_file(url or ""):
            name = LOCAL_PLATFORM
        else:
            hit = detect_platform(url or "")
            name = hit[0] if hit else UNKNOWN_PLATFORM

    provider = _INSTANCES.get(name)
    if provider is not None:
        return provider

    # unknown 或未登记的平台：通用实现兜底（yt-dlp 可能认得）
    return _generic(name or UNKNOWN_PLATFORM)


def supports_author(url: str, platform: str | None = None) -> bool:
    """该地址能不能列作者作品。UI 据此决定要不要显示批量入口。"""
    return bool(getattr(resolve(url, platform), "supports_author", False))


def list_author(url: str, cfg: object, platform: str | None = None, limit: int = 0):
    """列作者作品，统一入口。不支持的平台由 provider 抛 `NotSupported`。"""
    return resolve(url, platform).list_author(url, cfg, limit)
