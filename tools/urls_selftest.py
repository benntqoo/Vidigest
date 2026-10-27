"""输入地址解析自测：URL 归一化 + 平台识别 + 分发层落地（纯函数，不联网）。

## 判据来源（2026-10-27 用户实测）

用户在抖音网页版**搜索结果页**点开一个视频，直接复制了地址栏：

    https://www.douyin.com/search/凯心?aid=…&modal_id=7700460182866331855&type=general

任务失败，原文是：

    [generic] Extracting URL: …/search/…
    ERROR: Unsupported URL: https://www.douyin.com/search/…

两个原因叠在一起：

1. `urls.URL_PATTERNS` 的抖音正则只覆盖 `v.douyin.com/…`、`/video/<id>`、
   `/user/<sec_uid>`、`iesdouyin/share/video/<id>`，**搜索结果页一条都不沾** ⇒
   平台判成 `unknown` ⇒ 交给 yt-dlp 的 generic 提取器。
2. 即使平台认对了，URL 形态也不对。对照实测（同一个视频 id）：

       $ yt-dlp --simulate '…/search/x?modal_id=7700460182866331855'
       [generic] … ERROR: Unsupported URL                       ← 没有提取器认领
       $ yt-dlp --simulate '…/video/7700460182866331855'
       [Douyin] … ERROR: Fresh cookies are needed               ← 认了，只差会话

   而抖音的主路径是**浏览器取流**（不需要 cookie），它也只打 `/video/` 页。

修复 = `urls.canonical_url()` 把 `?modal_id=<id>` 归一成 `/video/<id>`，
`detect_platform()` 增加主机名兜底，`fetcher._normalized()` 在分发点落地。

用法：python tools/urls_selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import fetcher  # noqa: E402
from app.core.errors import StageError  # noqa: E402
from app.core.urls import (  # noqa: E402
    LOCAL_PLATFORM,
    UNKNOWN_PLATFORM,
    canonical_url,
    detect_platform,
    douyin_url_kind,
    extract,
    extract_many,
)
from app.models.task import Task  # noqa: E402
from app.platforms import registry  # noqa: E402
from app.platforms.douyin import DouyinProvider  # noqa: E402

#: 用户实测的那条（逐字复制，未做任何"善意改写"）。
_ACCIDENT_URL = (
    "https://www.douyin.com/search/%E5%87%AF%E5%BF%83"
    "?aid=96805f84-a83e-43e3-a57a-ce4331044fdf"
    "&modal_id=7700460182866331855&type=general"
)
_ACCIDENT_ID = "7700460182866331855"
_ACCIDENT_CANON = f"https://www.douyin.com/video/{_ACCIDENT_ID}"

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


# --------------------------------------------------------------- A 事故复现


def check_accident() -> None:
    print("[A] 用户实测链接（搜索结果页 + modal_id）")
    item = extract(_ACCIDENT_URL)
    assert item is not None
    check("平台判成 douyin（不再是 unknown）", item.platform == "douyin", item.platform)
    check("目标归一成 /video/<id>", item.target == _ACCIDENT_CANON, item.target)
    # 归一后的地址**不带 query** —— 这是 yt-dlp 的 Douyin 提取器能认领的必要条件
    # （它的 _VALID_URL 是 ^https?://(www\.)?douyin\.com/video/(?P<id>\d+)）。
    check("归一后的地址不含 modal_id", "modal_id" not in item.target, item.target)
    check("形态判定为 video", douyin_url_kind(item.target) == "video")
    check("原文保留", item.raw == _ACCIDENT_URL)


# --------------------------------------------------------------- B 各形态归一


def check_forms() -> None:
    print("\n[B] 抖音各形态")
    cases: list[tuple[str, str, str]] = [
        # (输入, 期望归一结果, 说明)
        (
            f"https://www.douyin.com/discover?modal_id={_ACCIDENT_ID}",
            _ACCIDENT_CANON,
            "发现页 + modal_id",
        ),
        (
            f"https://www.douyin.com/user/MS4wLjABAAAAxyz?modal_id={_ACCIDENT_ID}",
            _ACCIDENT_CANON,
            "作者主页点开视频（曾会被误判成作者主页）",
        ),
        (
            f"https://www.iesdouyin.com/share/video/{_ACCIDENT_ID}/?region=CN",
            _ACCIDENT_CANON,
            "iesdouyin 分享页",
        ),
        (
            f"https://www.douyin.com/video/{_ACCIDENT_ID}?previous_page=app_code_link",
            _ACCIDENT_CANON,
            "/video/ 带追踪参数",
        ),
        (
            f"https://www.douyin.com/root/share/video/{_ACCIDENT_ID}",
            _ACCIDENT_CANON,
            "/root/share/video/ 嵌套路径",
        ),
        (
            "https://v.douyin.com/ye_7rRWF3tc/",
            "https://v.douyin.com/ye_7rRWF3tc/",
            "短链必须原样（id 要联网跳转才知道）",
        ),
        (
            "https://www.douyin.com/user/MS4wLjABAAAAxyz",
            "https://www.douyin.com/user/MS4wLjABAAAAxyz",
            "作者主页本身不改写（批量下载的入口）",
        ),
        (
            f"https://www.douyin.com/note/{_ACCIDENT_ID}",
            f"https://www.douyin.com/note/{_ACCIDENT_ID}",
            "图文作品不改写成 /video/（那会变成不存在的地址）",
        ),
        (
            "https://www.douyin.com/search/abc",
            "https://www.douyin.com/search/abc",
            "没有 video id 的搜索页：保持原样，由形态诊断给提示",
        ),
        (
            "https://www.douyin.com/search/abc?modal_id=not-a-number",
            "https://www.douyin.com/search/abc?modal_id=not-a-number",
            "modal_id 非数字不拼地址（否则造出 /video/not-a-number）",
        ),
    ]
    for src, want, why in cases:
        got = canonical_url(src)
        check(why, got == want, f"{got}")

    # 幂等：下载层会对同一个 task 归一多次（分发点 + download_ytdlp）
    once = canonical_url(_ACCIDENT_URL)
    check("幂等（归一两次结果相同）", canonical_url(once) == once, canonical_url(once))

    kinds = {
        f"https://www.douyin.com/user/MS4wLjABAAAAxyz": "author",
        f"https://www.douyin.com/note/{_ACCIDENT_ID}": "note",
        "https://www.douyin.com/search/abc": "page",
        "https://v.douyin.com/ye_7rRWF3tc/": "short",
    }
    for url, want in kinds.items():
        got = douyin_url_kind(url)
        check(f"形态判定 {want}", got == want, got)


# --------------------------------------------------------------- C 别的平台


def check_other_platforms() -> None:
    print("\n[C] 其它平台与兜底（回归）")
    # 路径正则仍然优先：B 站 / YouTube / X 的 query 在**抽取阶段**就被截掉了。
    # 注意断言的是 `extract().target`（真实链路），而不是对原始串直接调
    # `canonical_url` —— 后者只负责抖音，对别的平台必须原样放过。
    for src, want_platform, want_target in [
        (
            "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333.999&vd_source=abc",
            "bilibili",
            "https://www.bilibili.com/video/BV1xx411c7mD",
        ),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=4",
            "youtube",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://x.com/someone/status/1234567890?s=20",
            "twitter",
            "https://x.com/someone/status/1234567890",
        ),
    ]:
        hit = detect_platform(src)
        check(f"识别 {want_platform}", hit is not None and hit[0] == want_platform, str(hit))
        item = extract(src)
        check(f"{want_platform} 去掉追踪参数", item is not None and item.target == want_target,
              item.target if item else "None")
        check(
            f"canonical_url 不越界改 {want_platform}",
            canonical_url(src) == src,
            canonical_url(src),
        )

    # 主机兜底不能把「未知站」也吞掉 —— unknown 的语义是「交给 yt-dlp 通用提取器」
    check(
        "未知站点仍是 unknown",
        detect_platform("https://example.com/watch/1") is None,
        "应返回 None",
    )
    check(
        "未知站点 extract 落到 unknown",
        (extract("https://example.com/watch/1") or Task(url="")).platform == UNKNOWN_PLATFORM,
    )

    # 分享文案：中文前缀 + 链接 + 尾部中文（URL 不能被中文标点/汉字粘住）
    share = "8.25 复制打开抖音，看看【凯心的作品】https://v.douyin.com/ye_7rRWF3tc/ 复制此链接"
    item = extract(share)
    check(
        "分享文案里抽出干净短链",
        item is not None and item.target == "https://v.douyin.com/ye_7rRWF3tc",
        item.target if item else "None",
    )
    check("分享文案识别为 douyin", item is not None and item.platform == "douyin")

    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "clip.mp4"
        video.write_bytes(b"")
        item = extract(str(video))
        check(
            "本地文件仍是 local",
            item is not None and item.platform == LOCAL_PLATFORM,
            item.platform if item else "None",
        )


# --------------------------------------------------------------- D 分发层


def check_dispatch() -> None:
    print("\n[D] 分发层（老任务也能被修好）")
    dirty = Task(url=_ACCIDENT_URL, platform=UNKNOWN_PLATFORM, id=3)

    effective = fetcher._normalized(dirty)
    check("_normalized 归一 URL", effective.url == _ACCIDENT_CANON, effective.url)
    check("_normalized 重判平台", effective.platform == "douyin", str(effective.platform))
    check("_normalized 不改动原对象（DB 不产生副作用）", dirty.url == _ACCIDENT_URL)
    check("_normalized 保留 id", effective.id == 3)

    clean = Task(url=_ACCIDENT_CANON, platform="douyin", id=4)
    check("已干净的 task 原样返回（不当成新对象）", fetcher._normalized(clean) is clean)

    # 库里落着 unknown 的老任务 → 必须走抖音 provider，否则重试永远是同一句英文
    provider = registry.resolve(_ACCIDENT_URL, UNKNOWN_PLATFORM)
    check("resolve(dirty, unknown) → DouyinProvider", provider.name == "douyin", provider.name)
    provider = registry.resolve(_ACCIDENT_CANON, None)
    check("resolve(clean, None) → DouyinProvider", provider.name == "douyin", provider.name)

    # 去重：同一个视频的两种形态应被认成一条（不同视频不能合并）
    deduped = extract_many(
        [
            _ACCIDENT_URL,
            _ACCIDENT_CANON,
            f"https://www.douyin.com/video/7699999999999999999",
        ]
    )
    check("同一视频两种形态去重成 1 条 + 另一条", len(deduped) == 2, str(len(deduped)))
    check(
        "去重后保留的是归一形态",
        [d.target for d in deduped][0] == _ACCIDENT_CANON,
        str([d.target for d in deduped]),
    )


# --------------------------------------------------------------- E 形态诊断


def check_diagnosis() -> None:
    print("\n[E] 形态诊断的错误提示（要能照着做）")
    provider = DouyinProvider()
    with tempfile.TemporaryDirectory() as tmp:
        for url, must_contain, why in [
            ("https://www.douyin.com/search/abc", "modal_id", "搜索页 → 提示去复制带 modal_id 的地址"),
            ("https://www.douyin.com/user/MS4wLjABAAAAxyz", "批量", "作者主页 → 指向批量入口"),
            (f"https://www.douyin.com/note/{_ACCIDENT_ID}", "图文", "图文作品 → 说明没有视频流"),
        ]:
            task = Task(url=url, platform="douyin")
            try:
                provider.download(task, None, tmp)
                check(why, False, "没有抛异常（会去开浏览器白等）")
            except StageError as exc:
                msg = str(exc)
                check(why, must_contain in msg, msg.replace("\n", " / ")[:90])
            except Exception as exc:  # noqa: BLE001
                check(why, False, f"抛了别的：{type(exc).__name__}: {exc}")


def main() -> int:
    check_accident()
    check_forms()
    check_other_platforms()
    check_dispatch()
    check_diagnosis()

    print(f"\n地址解析自测: {_PASS}/{_PASS + len(_FAIL)} 通过")
    for name in _FAIL:
        print(f"  ✗ {name}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
