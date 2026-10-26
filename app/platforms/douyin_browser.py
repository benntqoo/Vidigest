"""抖音的浏览器侧实现：用**系统自带 Edge**（playwright `channel="msedge"`）拿流与列表。

## 为什么必须是真浏览器

2026-10-26 实测（详见 `.workbuddy/memory/2026-10-26.md`）：
- 纯 HTTP 拿不到 —— 短链 302 回首页，老 `iteminfo` 接口对任何 id 都返回空 body；
- Tauri 内置 WebView2 也不行 —— `wry-0.57.0/src/webview2/mod.rs:1013` 的
  `AddWebResourceRequestedFilter` 只给 `custom_protocols` 注册过滤器，
  https 子请求不会触发 `on_web_resource_request`，拦不到媒体请求。

所以只能用 playwright。**但不需要下载 chromium**：`channel="msedge"` 直接驱动
Windows 自带的 Edge（Win10/11 默认安装），`ms-playwright` 那 1.3 GB 不进包。

## 为什么是持久化 profile 而不是继承用户浏览器

用户"网页端已经登录"**帮不上忙**：playwright 启动的是**全新隔离 profile**，
看不到用户 Edge/Chrome 里的 cookie（而且 Chrome 127+ 的 App-Bound Encryption
让直接读 cookie 库也不可行）。

所以设计成：**第一次弹一个有头窗口让用户扫码登录一次**，登录态存在
`<app_data>/browser-profile/` 里，之后所有请求都用这个 profile（无头）。
这也是为什么产品上需要提示"请先登录"——不是多余的仪式，是唯一的技术前提。

## 一条视频的完整取法（实测通过）

1. 打开分享链接 → 抖音自己播放 → 我们拦两个 DASH 流：
   `.../media-video-avc1`（视频）+ `.../media-audio-und-mp4a`（音频）
2. 两个地址**只带 UA + Referer 就能下**（不带任何 cookie，实测 200 + `ftypisom`）
3. `ffmpeg -c copy` 合并成 mp4（不重编码，实测 rc=0，h264+aac 正常）

⚠️ 拦流时必须**接受 206 且按 content-type 判断**：DASH 是分段请求，URL 里
根本没有 `.mp4`。第一版按 `status==200 && url含.mp4` 过滤，一条都拦不到 ——
差点把「能下」误判成「抓不到直链」。
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

#: 与 `probe_douyin.py` 保持一致：桌面 Chrome UA，不要用 HeadlessChrome。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
REFERER = "https://www.douyin.com/"

#: 判断"登录态"的 cookie 名。抖音网页端登录后会种这几个之一。
LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")

PROFILE_DIR_NAME = "browser-profile"


class BrowserUnavailable(RuntimeError):
    """playwright 没装、或系统里没有 Edge —— 都是**可诊断**的环境问题。"""


@dataclass
class Streams:
    """一条视频的两条 DASH 流。"""

    video: str = ""
    audio: str = ""
    #: 页面标题（抖音的 h1/title，做文件名用）
    title: str = ""
    duration: float = 0.0
    #: 短链跳转后的最终地址（`/video/<id>` 就在里面）
    final_url: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.video)


def classify(urls: list[str]) -> tuple[str, str]:
    """从拦到的地址里挑出 `(视频流, 音频流)`。

    抽成纯函数是为了能**离线判据**：不开浏览器也能验证挑选规则。
    规则只有两条，但都踩过：
    - 视频流 URL 含 `media-video`（或 `-avc1`），音频流含 `media-audio` / `-mp4a`；
    - **必须去重后取第一条**：一次播放会发几十个 206 分段请求，URL 完全相同。
    """
    video = audio = ""
    for u in urls:
        low = u.lower()
        if not video and ("media-video" in low or "-avc1" in low):
            video = u
        elif not audio and ("media-audio" in low or "-mp4a" in low):
            audio = u
        if video and audio:
            break
    return video, audio


def profile_dir(app_data: Path) -> Path:
    """持久化 profile 目录（登录态就存在这里）。"""
    return Path(app_data) / PROFILE_DIR_NAME


# ---------------------------------------------------------------- 浏览器会话


class _Session:
    """一次 playwright 会话的上下文管理器（持久的 profile）。"""

    def __init__(self, profile: Path, headless: bool = True) -> None:
        self.profile = Path(profile)
        self.headless = headless
        self._pw = None
        self._ctx = None

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise BrowserUnavailable(
                "缺少 playwright（打包版应自带）。开发机执行：\n"
                "  pip install playwright    # 不需要 playwright install chromium"
            ) from exc

        self.profile.mkdir(parents=True, exist_ok=True)
        try:
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile),
                channel="msedge",  # 用系统 Edge：不下载 chromium
                headless=self.headless,
                viewport={"width": 1280, "height": 800},
                user_agent=USER_AGENT,
                args=["--disable-blink-features=AutomationControlled"],
            )
            # 去掉最明显的自动化信号（抖音会拿它当机器人判据）
            self._ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
        except Exception as exc:  # noqa: BLE001 — playwright 的异常类型很杂
            self.__exit__(None, None, None)
            raise BrowserUnavailable(
                "启动 Edge 失败。可能原因：系统没有 Edge（Win11 精简版/被卸载）、"
                f"或 profile 目录被占用。\n  {type(exc).__name__}: {exc}"
            ) from exc
        return self

    def __exit__(self, *exc_info) -> None:
        for closer in (self._ctx, self._pw):
            try:
                if closer is not None:
                    closer.close() if hasattr(closer, "close") else closer.stop()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = self._pw = None

    # -- 便捷访问
    @property
    def context(self):
        return self._ctx

    def is_logged_in(self) -> bool:
        try:
            names = {c.get("name") for c in self._ctx.cookies()}
        except Exception:  # noqa: BLE001
            return False
        return any(n in names for n in LOGIN_COOKIES)

    def page(self, timeout_ms: int = 30000):
        page = self._ctx.new_page()
        page.set_default_timeout(timeout_ms)
        return page


# ---------------------------------------------------------------- 登录


def login_interactive(
    profile: Path,
    timeout_sec: float = 180.0,
    on_state: Callable[[str, str], None] | None = None,
) -> bool:
    """弹出**有头** Edge 让用户扫码登录，登录成功后 cookie 留在 profile 里。

    `on_state(state, message)` 的 `state`：
    `waiting`（窗口已开，等扫码）/ `done` / `timeout` / `error`。

    为什么必须"有头"：扫码是唯一的登录方式，无头窗口用户看不见二维码。
    """
    def emit(state: str, msg: str) -> None:
        logging.info("抖音登录：%s %s", state, msg)
        if on_state:
            on_state(state, msg)

    try:
        with _Session(profile, headless=False) as s:
            page = s.page()
            emit("waiting", "已打开浏览器，请在窗口里扫码登录抖音")
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
            deadline = time.time() + timeout_sec
            while time.time() < deadline:
                if s.is_logged_in():
                    # 登录态落盘需要一点时间，等一下再关
                    time.sleep(2.0)
                    emit("done", "登录成功，已保存登录态")
                    return True
                time.sleep(1.5)
            emit("timeout", f"{int(timeout_sec)} 秒内没有检测到登录")
            return False
    except BrowserUnavailable as exc:
        emit("error", str(exc))
        return False


def has_login(profile: Path) -> bool:
    """无头检查 profile 里是否已有登录态（不启动浏览器就返回 False 更快，
    但准确判断必须起浏览器 —— 所以这里起一个无头会话，约 2 秒）。"""
    try:
        with _Session(profile, headless=True) as s:
            return s.is_logged_in()
    except BrowserUnavailable:
        return False


# ---------------------------------------------------------------- 取流


def capture_streams(
    url: str,
    profile: Path,
    headless: bool = True,
    attempts: int = 3,
) -> Streams:
    """打开一条抖音视频页，拦它自己请求的 DASH 两路流。

    **必须重试**：2026-10-26 实测同一段代码成功率不稳定 —— 同样的链接，
    有时拦到 8 条响应，有时一条都没有（页面照样播起来了，说明流走了缓存或
    播放器复用了上一次的连接）。三次尝试 + 两个来源（response 事件、
    `performance` 资源清单）实测能把成功率拉上来。

    `performance.getEntriesByType('resource')` 是必要的兜底：播放器的 DASH 分段
    是 XHR/fetch 发的，会出现在资源清单里；而 response 事件可能因为监听注册
    时机、或页面走缓存而漏掉。
    """
    last_error = ""
    for attempt in range(1, max(1, attempts) + 1):
        found: list[str] = []
        try:
            with _Session(profile, headless=headless) as s:
                page = s.page()
                page.on("response", _collector(found))
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(6000 + 3000 * attempt)

                # 兜底 1：资源清单里有、response 事件没捕到的
                try:
                    names = page.evaluate(
                        "() => performance.getEntriesByType('resource')"
                        ".map(e => e.name).filter(n => n.indexOf('/video/tos/') >= 0)"
                    )
                except Exception:  # noqa: BLE001
                    names = []
                found.extend(names or [])

                final_url = page.url
                title = (page.title() or "").split(" - 抖音")[0].strip()
                body = ""
                try:
                    body = page.content()[:4000]
                except Exception:  # noqa: BLE001
                    pass

                video, audio = classify(found)

                # 兜底 2：还没出流就点一下播放器再等（有些版本不自动播）
                if not video:
                    try:
                        page.locator("video").first.click(timeout=3000)
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(4000)
                    try:
                        names = page.evaluate(
                            "() => performance.getEntriesByType('resource')"
                            ".map(e => e.name).filter(n => n.indexOf('/video/tos/') >= 0)"
                        )
                        found.extend(names or [])
                    except Exception:  # noqa: BLE001
                        pass
                    video, audio = classify(found)

                if video:
                    logging.info(
                        "抖音取流成功（第 %d 次）：video=%s audio=%s 候选=%d",
                        attempt, bool(video), bool(audio), len(found),
                    )
                    return Streams(
                        video=video, audio=audio, title=title or "",
                        duration=0.0, final_url=final_url,
                    )

                # 没出流：给出可判断的原因，而不是干巴巴地重试
                if any(k in body for k in ("验证", "captcha", "滑块", "环境异常")):
                    last_error = "抖音要求人机验证（页面出现验证/滑块）。请用有头模式完成验证后再试。"
                    logging.warning("抖音取流被要求人机验证（第 %d 次）", attempt)
                else:
                    last_error = f"页面已打开（{final_url}）但没有媒体请求"
                    logging.warning("抖音取流为空（第 %d 次）：%s", attempt, last_error)
        except BrowserUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            logging.warning("抖音取流异常（第 %d 次）：%s", attempt, last_error)

    raise BrowserUnavailable(f"连试 {attempts} 次都没拦到视频流：{last_error}")


def _collector(sink: list[str]):
    def handler(resp) -> None:
        try:
            if resp.status not in (200, 206):
                return
            u = resp.url
            if "/video/tos/" not in u:
                return
            ct = (resp.headers or {}).get("content-type", "")
            if "video" in ct or "audio" in ct or "media-" in u:
                sink.append(u)
        except Exception:  # noqa: BLE001 — 事件回调里抛错会污染页面
            pass

    return handler


# ---------------------------------------------------------------- 下载 + 合并


def download_streams(
    streams: Streams,
    out_path: Path,
    ffmpeg_exe: str,
    on_progress: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """把两条流下下来，用 ffmpeg `-c copy` 合并成 `out_path`。"""
    import requests

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = out_path.parent / ".dash"
    tmpdir.mkdir(parents=True, exist_ok=True)
    headers = {"user-agent": USER_AGENT, "referer": REFERER}

    parts: dict[str, Path] = {}
    pairs = [("video", streams.video), ("audio", streams.audio)]
    pairs = [(k, u) for k, u in pairs if u]

    # 先探总大小：否则进度只能按"每个流结束"跳三下，界面看着像卡住。
    # 用 `Range: bytes=0-0` 读 Content-Range 里的总长（比 HEAD 可靠：
    # CDN 的 HEAD 常常不给 content-length）。
    total_bytes = 0
    for _, u in pairs:
        try:
            r = requests.get(u, headers={**headers, "Range": "bytes=0-0"},
                             timeout=20, stream=True)
            cr = r.headers.get("content-range", "")
            r.close()
            if "/" in cr:
                total_bytes += int(cr.rsplit("/", 1)[1])
            else:
                total_bytes += int(r.headers.get("content-length") or 0)
        except (ValueError, OSError, requests.RequestException):
            pass

    done_bytes = 0
    for kind, u in pairs:
        dst = tmpdir / f"{kind}.mp4"
        with requests.get(u, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            with dst.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if cancel_event is not None and cancel_event.is_set():
                        raise RuntimeError("已取消")
                    f.write(chunk)
                    done_bytes += len(chunk)
                    if on_progress and total_bytes:
                        on_progress("downloading", min(0.95, done_bytes / total_bytes))
        parts[kind] = dst
        if on_progress and not total_bytes:
            # 探不到总大小时退化成"每个流一个台阶"，别假装有百分比
            on_progress("downloading", 0.5 if kind == "video" else 0.9)

    if "video" not in parts:
        raise RuntimeError("没有拿到视频流")

    if "audio" in parts:
        cmd = [
            ffmpeg_exe, "-y", "-loglevel", "error",
            "-i", str(parts["video"]), "-i", str(parts["audio"]),
            "-c", "copy", "-movflags", "+faststart", str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 合并失败（rc={proc.returncode}）：{(proc.stderr or '')[:300]}")
    else:
        # 没有音频轨（少数纯视频），直接改名
        parts["video"].replace(out_path)

    for p in parts.values():
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    try:
        tmpdir.rmdir()
    except OSError:
        pass
    if on_progress:
        on_progress("downloading", 1.0)
    return out_path


# ---------------------------------------------------------------- 作者作品


def list_author_items(
    author_url: str,
    profile: Path,
    limit: int = 0,
    scroll_rounds: int = 8,
    headless: bool = True,
) -> tuple[str, list[tuple[str, str]]]:
    """列作者主页的作品，返回 `(作者名, [(url, 标题)])`。

    ⚠️ 实测（2026-10-26）：**匿名只能拿到首屏约 14 条**，滚动不加载更多。
    登录后翻页能力是否改善**未验证** —— 所以调用方要如实告诉用户
    「这是当前能看到的条数」，而不是假装是全部。
    """
    seen: dict[str, str] = {}
    with _Session(profile, headless=headless) as s:
        if not s.is_logged_in():
            logging.warning("抖音未登录，作者作品列表可能只能拿到首屏")
        page = s.page()
        page.goto(author_url, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        name = ""
        try:
            name = (page.title() or "").split("的抖音")[0].strip()
        except Exception:  # noqa: BLE001
            pass

        stagnant = 0
        for _ in range(max(1, scroll_rounds)):
            before = len(seen)
            try:
                pairs = page.eval_on_selector_all(
                    "a[href*='/video/']",
                    "els => els.map(e => [e.href, (e.getAttribute('aria-label') || e.textContent || '').trim()])",
                )
            except Exception:  # noqa: BLE001
                pairs = []
            for href, label in pairs:
                if href and "/video/" in href:
                    # 去掉追踪参数，保证去重与后续解析稳定
                    clean = href.split("?")[0]
                    seen.setdefault(clean, str(label or ""))
            if limit and len(seen) >= limit:
                break
            # 尝试点"加载更多"（有就点，没有继续滚）
            try:
                more = page.get_by_text("加载更多", exact=True)
                if more.count():
                    more.first.click(timeout=1500)
                    page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(2000)
            stagnant = stagnant + 1 if len(seen) == before else 0
            if stagnant >= 2:
                break

    items = list(seen.items())
    if limit:
        items = items[:limit]
    return name, items
