"""视频获取：在线链接走 yt-dlp，本地文件直接入库。

接口契约见 PLAN.md §4.1 / §4.2：

    download(task, cfg, out_dir, on_progress) -> DownloadResult

失败时抛 `StageError("download", ...)`，消息里带 yt-dlp 输出的**尾 20 行**——
本模块不写任务状态，状态流转由调用方（cli / dispatcher）负责。这样 core 层
不必知道「任务」这个概念的状态机，也就不会与 store 耦合。

两条实测约束（PLAN.md §9.5）：

- `yt-dlp --cookies-from-browser` 在 Windows 新版浏览器上基本必然失败，
  只能用预先生成的 Netscape `cookies.txt`；
- 平台风控敏感，需要限速（`download.rate_limit_per_min`）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from app.core.errors import CookieExpired, StageError, TaskCancelled, raise_if_cancelled
from app.core.ffmpeg_locator import find_ffmpeg
from app.core.proc import kill_process
from app.models.config import Config
from app.models.task import Task
from app.core.urls import LOCAL_PLATFORM, UNKNOWN_PLATFORM, canonical_url, detect_platform

#: 失败时保留的输出行数（PLAN.md §4.1）。
TAIL_LINES = 20

#: yt-dlp 下载进度行：`[download]  45.2% of   44.11MiB at ...`
_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")

#: yt-dlp 完成行（用来把进度顶到 100%）
_PROGRESS_DONE_RE = re.compile(r"\[download\]\s+100(?:\.0)?%")

#: yt-dlp 的 cookie 类失败特征。2026-10-10 实测抖音原文：
#: "ERROR: [Douyin] <id>: Fresh cookies (not necessarily logged in) are needed"
_COOKIE_ERROR_RE = re.compile(r"fresh cookies|cookies (?:may be|are) stale", re.IGNORECASE)

INSTALL_HINT = "pip install -U yt-dlp   （或 winget install yt-dlp.yt-dlp）"

#: 探测下载耗时的超时。yt-dlp 偶发卡在网络握手，不能让它挂死整个流水线。
_DEFAULT_SOCKET_TIMEOUT = 30


def cookie_hint(configured: str | None) -> str:
    """cookie 失败的可操作提示。PRD D6：错误提示**首行即动作**。"""
    return (
        f"先在设置→维护里点「抖音登录」（扫码一次即可，登录态存在本机）；\n"
        f"或重新采集 cookie：python tools/refresh_cookies.py，并确认 config.yaml 的 "
        f"paths.cookies_file 指向生成的 cookies.txt（当前：{configured or '未配置'}）。\n"
        "原因：抖音对匿名请求也要求新鲜 cookie（ttwid 约 24 小时过期），"
        "缺失或过期时 yt-dlp 必然失败。自动重取属阶段 4（cookie.auto_refresh），尚未实现。"
    )


# ---------------------------------------------------------------- 定位


def find_yt_dlp(explicit: str | None = None) -> str:
    """按 `config.paths.yt_dlp` → `PATH` 的顺序定位 yt-dlp 可执行文件。

    yt-dlp **没有 Rust 等价物**（社区方案均为单平台或个人项目），所以这里必然
    是外部进程调用，见 PLAN.md §7.1。
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)

    found = shutil.which("yt-dlp")
    if found:
        return found

    raise StageError(
        "download",
        "未找到 yt-dlp。\n"
        f"  安装：{INSTALL_HINT}\n"
        "  或在 config.yaml 的 paths.yt_dlp 中显式指定完整路径。",
    )


def _ffprobe_of(ffmpeg: str) -> str | None:
    """ffmpeg 同目录下的 ffprobe。用于本地文件取时长（失败则返回 None）。"""
    probe = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    return str(probe) if probe.is_file() else None


def probe_duration_sec(video: str | Path, ffprobe: str | None) -> int | None:
    """读容器时长（秒，取整）。**任何失败都返回 None**，不抛异常。

    本地文件没有 yt-dlp 的 info.json，只能靠 ffprobe；拿不到就不显示时长，
    这属于展示信息缺失，不该阻断流水线。
    """
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        return int(float(proc.stdout.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------- 结果


@dataclass
class DownloadResult:
    """一次获取的产物。调用方据此回填 `Task` 的字段。"""

    video_path: Path
    title: str | None = None
    duration_sec: int | None = None
    video_id: str | None = None
    platform: str = ""
    info_json: Path | None = None
    from_local: bool = False


# ---------------------------------------------------------------- 主流程


def _normalized(task: Task) -> Task:
    """返回「URL 已归一 + 平台已重判」的**副本**，供 provider 使用。

    两件事必须一起做，否则修一半：

    1. **URL 归一**：抖音的搜索页形态（`?modal_id=<id>`）和 `/video/<id>` 是同一段
       视频，但 yt-dlp 只认后者（2026-10-27 用户实测，见 `urls.canonical_url`）。
       归一放在分发点而不是各 provider 里，是为了「以后新增平台自动受益」。
    2. **平台重判**：库里 `platform=unknown` 的老任务，靠这一步才走对 provider。

    为什么用副本而不是直接改 `task`：`store.update()` 是**整行写回**（含 url 列），
    而 url 上有 UNIQUE 约束。若库里同时存在同一视频的「搜索页形态」和
    「/video/ 形态」两条任务，改完 url 再写回会撞约束、把任务炸掉 ——
    而用户只是点了一下重试。副本让归一化只影响本次下载，DB 保持原样。
    """
    url = canonical_url(task.url)
    platform = task.platform
    if not platform or platform == UNKNOWN_PLATFORM:
        hit = detect_platform(url)
        if hit:
            platform = hit[0]
    if url == task.url and platform == task.platform:
        return task
    logging.info("任务 #%s 下载前归一：%s → %s（platform=%s）", task.id, task.url, url, platform)
    return replace(task, url=url, platform=platform)


def download(
    task: Task,
    cfg: Config,
    out_dir: str | Path,
    on_progress: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """按平台分发：`app/platforms/registry.resolve()` → provider.download()。

    ⚠️ **2026-10-26 的教训**：一度把平台层建好了却没有接进来 —— `pipeline` 调的
    始终是这个函数，而它内部还是老的「if 本地 else yt-dlp + 抖音 cookie 硬拦」。
    于是抖音的浏览器取流那条路**在生产里从没被调用过**，用户看到的是
    「重新采集抖音 cookie」。新建的抽象**必须接到真正的调用点上**，否则只是摆设。

    递归风险：各 provider 的默认实现会回调本模块的 `download_ytdlp()` /
    `take_local()`，**不能回调 `download()`** —— 那会无限递归。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _report(on_progress, "downloading", 0.0)

    from app.platforms import resolve  # 延迟导入：platforms 依赖本模块

    effective = _normalized(task)
    provider = resolve(effective.url, effective.platform)
    logging.info(
        "下载分发：platform=%s provider=%s url=%s",
        effective.platform,
        provider.name,
        effective.url,
    )
    return provider.download(effective, cfg, out, on_progress, cancel_event)


def download_ytdlp(
    task: Task,
    cfg: Config,
    out_dir: str | Path,
    on_progress: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """通用（yt-dlp）实现。**provider 的默认下载走这里，不要回调 `download()`。**"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # `_normalized` 是幂等的：`download()` 分发时已经归一过，这里再调一次
    # 只是为了让**直接调用本函数**的地方（判据、探针）拿到同样的 URL 形态。
    return _download_remote(_normalized(task), out, cfg, on_progress, cancel_event)


def take_local(
    task: Task,
    cfg: Config,
    out_dir: str | Path,
    on_progress: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    """本地文件实现（硬链接优先）。同样给 provider 用，不回调 `download()`。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return _take_local(_normalized(task), out, cfg, on_progress)


def _take_local(
    task: Task,
    out: Path,
    cfg: Config,
    on_progress: Callable[[str, float], None] | None,
) -> DownloadResult:
    """本地文件：**硬链接优先**，失败回退复制。

    硬链接让 `out_dir` 自包含（符合「每个视频一个目录」的产出约定），
    同时零额外磁盘占用；由于是链接而非移动，删除 `out_dir` 不会碰到源文件。
    """
    src = Path(task.url).expanduser()
    if not src.is_file():
        raise StageError("download", f"本地文件不存在：{src}")

    dst = out / f"video{src.suffix.lower() or '.mp4'}"
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        # 跨盘 / 非 NTFS / 权限不足 → 退化为复制
        shutil.copy2(src, dst)
    logging.info("本地文件已接入：%s → %s", src.name, dst.name)

    ffmpeg = find_ffmpeg(cfg.paths.ffmpeg)
    duration = probe_duration_sec(dst, _ffprobe_of(str(ffmpeg)))
    _report(on_progress, "downloading", 1.0)

    return DownloadResult(
        video_path=dst,
        title=src.stem,
        duration_sec=duration,
        video_id=None,
        platform=LOCAL_PLATFORM,
        from_local=True,
    )


def _download_remote(
    task: Task,
    out: Path,
    cfg: Config,
    on_progress: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadResult:
    cookies = cfg.paths.cookies_file
    if task.platform == "douyin" and not (cookies and Path(cookies).is_file()):
        # 抖音对匿名请求也要求新鲜 cookie（ttwid），缺了 yt-dlp 必失败——
        # 预检直接给出可操作提示，省一次注定失败的网络往返（还占风控配额）。
        # 放在 find_yt_dlp 之前：这条失败与 yt-dlp 是否安装无关。
        raise CookieExpired(cookie_hint(cookies))

    yt_dlp = find_yt_dlp(cfg.paths.yt_dlp)

    # 限速：`--limit-rate` 只接字节单位，按 10 条/分钟的大致估算给出软上限。
    rate_kib = max(1, int(cfg.download.rate_limit_per_min * 512))
    cmd: list[str] = [
        yt_dlp,
        "--no-playlist",  # 分享链接常带 list 参数，避免误抓整个合集
        "--newline",  # 进度按行输出，否则 \r 覆盖无法逐行解析
        "--no-color",
        "--no-warnings",
        "--progress",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*+ba/b",  # 优先合并最佳视频+音频；退化到单文件
        "--socket-timeout",
        str(_DEFAULT_SOCKET_TIMEOUT),
        "--retries",
        "3",
        "--limit-rate",
        f"{rate_kib}K",
        "--write-info-json",
        "-o",
        str(out / "video.%(ext)s"),
    ]

    cookies = cfg.paths.cookies_file
    if cookies:
        if not Path(cookies).is_file():
            raise StageError("download", f"cookies 文件不存在：{cookies}（见 PLAN.md §9.5）")
        cmd += ["--cookies", cookies]

    cmd.append(task.url)

    logging.info("yt-dlp 开始下载：%s", task.url)
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    last_pct = -1.0

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # 合并：进度在 stdout、错误在 stderr，统一读取
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            raise_if_cancelled(cancel_event)
            line = line.rstrip("\n")
            tail.append(line)
            match = _PROGRESS_RE.search(line)
            if match:
                try:
                    pct = float(match.group(1)) / 100.0
                except ValueError:
                    continue
                if pct - last_pct >= 0.01 or _PROGRESS_DONE_RE.search(line):
                    last_pct = pct
                    _report(on_progress, "downloading", min(pct, 1.0))
    except TaskCancelled:
        kill_process(proc)
        raise
    finally:
        proc.stdout.close()
        code = proc.wait()

    if code != 0:
        # cookie 类失败翻译成可操作提示（PRD D6）：配了但已过期时 yt-dlp
        # 会如实报原文，用户看不出「下一步该做什么」。
        if any(_COOKIE_ERROR_RE.search(line) for line in tail):
            raise CookieExpired(cookie_hint(cfg.paths.cookies_file))
        detail = "\n".join(f"  {l}" for l in tail)
        raise StageError(
            "download",
            f"yt-dlp 退出码 {code}，输出尾 {len(tail)} 行：\n{detail}",
        )

    video = _find_output_video(out)
    if video is None:
        raise StageError(
            "download",
            f"yt-dlp 成功退出但未找到视频文件（期望 {out}/video.*）：\n"
            + "\n".join(f"  {l}" for l in tail),
        )

    info = _read_info_json(out)
    _report(on_progress, "downloading", 1.0)
    logging.info("下载完成：%s（%.1f MB）", video.name, video.stat().st_size / 1024**2)

    return DownloadResult(
        video_path=video,
        title=info.get("title"),
        duration_sec=_as_int(info.get("duration")),
        video_id=info.get("id"),
        platform=task.platform or "",
        info_json=(out / f"{video.stem}.info.json") if (out / f"{video.stem}.info.json").is_file() else None,
    )


def _find_output_video(out: Path) -> Path | None:
    """定位 yt-dlp 的产出。优先 `video.mp4`，否则取最大的 video.* 文件。

    合并格式时中途会有 `.f137.mp4` / `.f140.m4a` 之类的临时分片，用体积排序
    可以稳定选到合并后的成品。
    """
    exact = out / "video.mp4"
    if exact.is_file():
        return exact
    candidates = [
        p
        for p in out.glob("video.*")
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".flv"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _read_info_json(out: Path) -> dict:
    """读 `--write-info-json` 的产物。读不到就返回空 dict（非致命）。"""
    for path in out.glob("video.info.json"):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("info.json 解析失败：%s", exc)
            return {}
    return {}


def _as_int(value: object) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _report(
    on_progress: Callable[[str, float], None] | None, stage: str, ratio: float
) -> None:
    if on_progress is not None:
        on_progress(stage, ratio)
