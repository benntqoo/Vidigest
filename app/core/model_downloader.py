"""模型下载器：把 faster-whisper 的 5 个必需文件下到 `model_dir/<model>/`。

**为什么需要它**：模型 2.88 GiB，从不进安装包；README 第 3 步是 5 条 `curl`。
而 PRD 的 D1 是「装完即用，不需要终端 / 命令行」—— 让用户手敲 curl 等于装完不能用。
2026-10-24 安装版首启实测就是这个结果：状态栏红字、唯一出路是命令行。

设计约束：

1. **独立线程，不进 `Dispatcher`** —— 下载模型不属于任务流水线，不该被 `pause` /
   `cancel_task` 影响，且必须在 `start` 之前就能用。
2. **不新增第三方依赖** —— `urllib.request` 足够；`requests` 虽在依赖里但没必要
   （多一个依赖就多一处打包体积与升级风险）。
3. **续传靠 `.part`** —— 每文件先写到 `<name>.part`，完成后 `os.replace` 原子改名。
   进程被杀后再点下载，从 `.part` 的长度接着下（区间请求），不重头。
4. **进度必须节流** —— `model.bin` 2.9 GiB 按 256 KiB 一块是 ~1.2 万次回调；
   不节流会把 stdout 打爆，而 stdout 是协议通道。
5. **校验 = 大小比对 + 一次真实加载**（不是哈希）—— 无权威 sha256 来源，
   而「模型能加载并跑出结果」比哈希更强。

协议见 `docs/IPC协议规格.md` §4.3。本模块**不感知协议**：它只调 `on_event(name, data)`，
把事件翻译成协议帧是 `app/rpc.py` 的事（与 `core/` 不感知传输方式的约定一致）。
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable

from app.core.env_probe import MODEL_FILES, missing_model_files

#: 镜像基址。`{model}` 会被替换成 `runtime.model`（如 `large-v3`）。
#:
#: ⚠️ 官方 `huggingface.co` 国内不通（README 已记），所以默认走镜像。
#: 镜像失效时可用 `download.base_url` 覆盖，无需改代码。
DEFAULT_BASE_URL = "https://hf-mirror.com/Systran/faster-whisper-{model}/resolve/main/"

#: 每次读 256 KiB。太小则 Python 层循环开销显著，太大则取消响应变慢。
CHUNK = 256 * 1024

#: 进度事件最小间隔（秒）与最小百分比增量 —— 两者取「或」。
PROGRESS_INTERVAL = 0.2
PROGRESS_PCT_STEP = 0.01

#: 单文件失败重试次数（不含首次）。
RETRIES = 3

#: 单次 HTTP 操作的超时（秒）。2.9 GiB 的读取不受此限制——它限制的是「无响应」。
TIMEOUT = 30

#: 预留余量：除模型本身外还要留的空间。
DISK_MARGIN = 256 * 1024 * 1024


class DownloadCancelled(Exception):
    """用户取消。"""


class DownloadFailed(Exception):
    """下载失败，`code` 是协议错误码。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


EventSink = Callable[[str, dict[str, Any]], None]


def _existing_ancestor(path: Path) -> Path:
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


class ModelDownloader:
    """单次只能跑一个下载任务（模型只有一份，并发下载没有意义）。"""

    def __init__(
        self,
        model_dir: Path,
        model: str,
        on_event: EventSink,
        base_url: str | None = None,
    ) -> None:
        self.model_dir = Path(model_dir) / model
        self.model = model
        self.base_url = (base_url or DEFAULT_BASE_URL).format(model=model)
        self._emit = on_event
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 进度快照，`status()` 给 UI 做首屏兜底（事件可能早于前端挂载）
        self._last: dict[str, Any] = {}

    # ---------------------------------------------------------------- 控制

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.running, **self._last}

    def start(self) -> bool:
        """启动下载线程。已在跑则返回 False（幂等，不排队）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, name="model-downloader", daemon=True
            )
            self._thread.start()
            return True

    def cancel(self) -> bool:
        """置取消标志。线程在下一个 chunk 边界退出并删除 `.part`。

        返回「是否确实有一个下载在跑」——`False` 时调用方应如实告诉用户
        「本来就没在下」，而不是假装取消成功。
        """
        if not self.running:
            return False
        self._cancel.set()
        return True

    # ---------------------------------------------------------------- 流程

    def _run(self) -> None:
        t0 = time.monotonic()
        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            sizes = self._probe_sizes()
            total = sum(sizes.values())

            already = 0
            for name, size in sizes.items():
                dest = self.model_dir / name
                if size > 0 and dest.is_file() and dest.stat().st_size == size:
                    already += size
            need = max(total - already, 0) + DISK_MARGIN
            free = shutil.disk_usage(_existing_ancestor(self.model_dir)).free
            if free < need:
                raise DownloadFailed(
                    "E_DISK_FULL",
                    f"磁盘空间不足：需要约 {_human_bytes(need)}（含 {_human_bytes(DISK_MARGIN)} 余量），"
                    f"可用 {_human_bytes(free)}。请先清理，或在设置里把模型目录改到空间充足的盘。",
                )

            self._publish(
                "model_download_started",
                {
                    "model": self.model,
                    "model_path": str(self.model_dir),
                    "total_bytes": total,
                    "file_count": len(sizes),
                    "already_bytes": already,
                },
            )

            done_before = already
            for index, (name, size) in enumerate(sizes.items(), start=1):
                self._check_cancel()
                dest = self.model_dir / name
                if size > 0 and dest.is_file() and dest.stat().st_size == size:
                    logging.info("模型下载：%s 已存在（%s），跳过", name, _human_bytes(size))
                    continue
                self._fetch(name, dest, index, len(sizes), size, done_before, total)
                done_before += size

            # 大小比对（D-S3 的前半）：宁可在这里判死，也不要让 faster-whisper
            # 在用户点「开始」时才抛一个看不懂的原生错误。
            missing = missing_model_files(self.model_dir)
            if missing:
                raise DownloadFailed(
                    "E_MODEL_INCOMPLETE", f"下载结束但缺少文件：{', '.join(missing)}"
                )

            self._publish(
                "model_download_verifying",
                {"detail": "文件已就绪，正在用一次真实推理校验…"},
            )
            ok, detail, reason = self._verify()

            self._publish(
                "model_download_done",
                {
                    "model": self.model,
                    "model_path": str(self.model_dir),
                    "total_bytes": total,
                    "elapsed_sec": round(time.monotonic() - t0, 1),
                    "verified": ok,
                    "detail": detail,
                    "blocked_reason": reason or None,
                },
            )
        except DownloadCancelled:
            self._cleanup_parts()
            with self._lock:
                self._last = {"state": "cancelled"}
            self._publish(
                "model_download_cancelled",
                {"detail": "已取消。已完成的文件保留，下次点下载会接着下。"},
            )
        except DownloadFailed as exc:
            logging.warning("模型下载失败：%s", exc.detail)
            self._publish("model_download_failed", {"code": exc.code, "detail": exc.detail})
        except Exception as exc:  # noqa: BLE001 — 下载线程的异常绝不能静默
            logging.exception("模型下载异常")
            self._publish(
                "model_download_failed",
                {"code": "E_DOWNLOAD_FAILED", "detail": f"{type(exc).__name__}: {exc}"},
            )
        finally:
            with self._lock:
                self._thread = None

    def _verify(self) -> tuple[bool | None, str, str]:
        from app.core import env_probe  # 延迟导入：本模块只在下模型时才被加载

        # device 从调用方决定更合适，但这里只需要「能不能跑」——用 cuda 试，
        # 失败也不代表模型坏（可能是没 GPU），故 reason 由调用方判断。
        return env_probe.verify_cuda(self.model_dir, device="cuda", compute_type="float16")

    # ---------------------------------------------------------------- 细节

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise DownloadCancelled

    def _probe_sizes(self) -> dict[str, int]:
        """HEAD 每个文件拿大小。拿不到就记 0（该文件不做大小校验、总进度不计它）。"""
        sizes: dict[str, int] = {}
        for name in MODEL_FILES:
            url = self.base_url + name
            size = 0
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    raw = resp.headers.get("Content-Length")
                    if raw and raw.isdigit():
                        size = int(raw)
            except Exception as exc:  # noqa: BLE001 — 拿不到大小不是致命错误
                logging.warning("模型下载：HEAD %s 失败（%s），该文件不做大小校验", name, exc)
            sizes[name] = size
        return sizes

    def _publish(self, name: str, data: dict[str, Any]) -> None:
        if name.endswith("_progress"):
            with self._lock:
                self._last = dict(data, state="running")
        try:
            self._emit(name, data)
        except Exception:  # noqa: BLE001 — 上报失败不能中断下载
            logging.exception("模型下载：事件上报失败（%s）", name)

    def _fetch(
        self,
        name: str,
        dest: Path,
        index: int,
        count: int,
        size: int,
        done_before: int,
        total: int,
    ) -> None:
        # 进度事件要带「第几个文件 / 共几个 / 整体进度」，由 `_stream` 读取。
        # 只有一条下载线程，所以用实例属性传递是安全的。
        self._cur = (name, index, count, done_before, total)
        part = dest.parent / (dest.name + ".part")
        url = self.base_url + name
        attempt = 0

        while True:
            attempt += 1
            offset = part.stat().st_size if part.is_file() else 0
            if size > 0 and offset > size:
                # `.part` 比目标还大 ⇒ 上一次写坏了（例如换了模型版本）。重头来。
                logging.warning("模型下载：%s 的 .part 比预期大（%d > %d），重下", name, offset, size)
                part.unlink(missing_ok=True)
                offset = 0
            try:
                self._stream(url, part, offset)
                break
            except DownloadCancelled:
                raise
            except DownloadFailed:
                raise
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                if attempt > RETRIES:
                    raise DownloadFailed(
                        "E_NETWORK",
                        f"下载 {name} 失败（已重试 {RETRIES} 次）：{type(exc).__name__}: {exc}",
                    ) from exc
                backoff = 2 ** (attempt - 1)
                logging.warning(
                    "模型下载：%s 第 %d 次失败（%s），%d s 后重试", name, attempt, exc, backoff
                )
                # 退避期间也要能被取消，否则用户点了取消要等十几秒
                if self._cancel.wait(backoff):
                    raise DownloadCancelled from exc

        # 大小比对（D-S3 前半）。服务端未给大小时跳过。
        actual = part.stat().st_size if part.is_file() else 0
        if size > 0 and actual != size:
            part.unlink(missing_ok=True)
            raise DownloadFailed(
                "E_MODEL_SIZE_MISMATCH",
                f"{name} 大小不符：期望 {size} 字节，实际 {actual}。已删除半成品，请重试。",
            )
        os.replace(part, dest)
        logging.info("模型下载：%s 完成（%s）", name, _human_bytes(actual))

        self._publish(
            "model_download_progress",
            {
                "file": name,
                "file_index": index,
                "file_count": count,
                "done_bytes": done_before + actual,
                "total_bytes": total,
                "overall_pct": (done_before + actual) / total if total else 0.0,
                "bytes_per_sec": 0.0,
                "eta_sec": 0.0,
            },
        )

    def _stream(self, url: str, part: Path, offset: int) -> None:
        """把 `url` 从 `offset` 起写到 `part`（追加）。"""
        headers = {"User-Agent": "Vidigest/0.1 (+model-downloader)"}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(url, headers=headers)
        window: deque[tuple[float, int]] = deque()
        last_emit = 0.0
        last_pct = -1.0

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                # 服务端忽略 Range 会返回 200 + 完整内容 —— 此时必须清空重写，
                # 否则会把整个文件追加到已有部分后面，得到一个更大的坏文件。
                status = getattr(resp, "status", 200)
                raw_total = resp.headers.get("Content-Length")
                has_len = bool(raw_total and raw_total.isdigit())
                if offset > 0 and status == 206:
                    mode, written = "ab", offset
                    total = offset + int(raw_total) if has_len else 0
                else:
                    mode, written = "wb", 0
                    total = int(raw_total) if has_len else 0

                with open(part, mode) as fh:
                    while True:
                        self._check_cancel()
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)

                        now = time.monotonic()
                        window.append((now, written))
                        while len(window) > 2 and now - window[0][0] > 3.0:
                            window.popleft()
                        pct = written / total if total else 0.0
                        if now - last_emit < PROGRESS_INTERVAL and (
                            pct - last_pct < PROGRESS_PCT_STEP
                        ):
                            continue
                        last_emit = now
                        last_pct = pct
                        speed = 0.0
                        if len(window) >= 2:
                            dt = window[-1][0] - window[0][0]
                            if dt > 0:
                                speed = (window[-1][1] - window[0][1]) / dt
                        eta = (total - written) / speed if speed > 0 and total else 0.0
                        cur_name, cur_index, cur_count, cur_done_before, cur_total = getattr(
                            self, "_cur", ("", 0, 0, 0, 0)
                        )
                        # `done_bytes` / `total_bytes` / `overall_pct` 是**整体**进度，
                        # `file_*` 才是当前文件。两者都要：进度条用整体，
                        # 「第 3/5 个：tokenizer.json」那行用当前文件。
                        overall_done = cur_done_before + written
                        overall_total = cur_total or total
                        self._publish(
                            "model_download_progress",
                            {
                                "file": cur_name or part.name.removesuffix(".part"),
                                "file_index": cur_index,
                                "file_count": cur_count,
                                "done_bytes": overall_done,
                                "total_bytes": overall_total,
                                "overall_pct": (overall_done / overall_total)
                                if overall_total
                                else 0.0,
                                "file_done_bytes": written,
                                "file_total_bytes": total,
                                "bytes_per_sec": speed,
                                "eta_sec": eta,
                            },
                        )
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise DownloadFailed(
                    "E_DISK_FULL", f"磁盘写满（{_human_bytes(shutil.disk_usage(part.parent).free)} 可用）"
                ) from exc
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and offset > 0:
                # Range 越界：说明 `.part` 已经是完整文件（服务端算出来的大小与本地一致）
                raise DownloadFailed(
                    "E_RANGE_INVALID",
                    f"{part.name} 的续传区间无效（本地 {offset} 字节）。已删除半成品，请重试。",
                ) from exc
            raise

    def _cleanup_parts(self) -> None:
        """取消时删掉所有 `.part`（半成品留着会与下次的大小比对打架）。"""
        if not self.model_dir.is_dir():
            return
        for p in self.model_dir.glob("*.part"):
            try:
                p.unlink()
            except OSError as exc:  # noqa: PERF203 — 单个删不掉不该中断清理
                logging.warning("模型下载：删除 %s 失败（%s）", p.name, exc)
