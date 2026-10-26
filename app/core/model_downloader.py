"""模型下载器：把「一个资产包」的文件下到该包的目录，可选解压。

**为什么需要它**：语音模型 2.9 GiB、视觉模型 2.35 GiB，从不进安装包；README 的
做法是让用户敲 `curl`。而 PRD 的 D1 是「装完即用，不需要终端 / 命令行」——
让用户手敲命令等于装完不能用。2026-10-24 安装版首启实测就是这个结果：
状态栏红字、唯一出路是命令行。

设计约束：

1. **独立线程，不进 `Dispatcher`** —— 下载模型不属于任务流水线，不该被 `pause` /
   `cancel_task` 影响，且必须在 `start` 之前就能用。
2. **不新增第三方依赖** —— `urllib.request` + `zipfile` 足够。
3. **续传靠 `.part`** —— 每文件先写到 `<name>.part`，完成后 `os.replace` 原子改名。
   进程被杀后再点下载，从 `.part` 的长度接着下（区间请求），不重头。
4. **进度必须节流** —— `model.bin` 2.9 GiB 按 256 KiB 一块是 ~1.2 万次回调；
   不节流会把 stdout 打爆，而 stdout 是协议通道。
5. **校验 = 大小比对 + 一次真实加载**（不是哈希）—— 无权威 sha256 来源，
   而「模型能加载并跑出结果」比哈希更强。

## 「下什么」不在这里

2026-10-27 起文件清单来自 `app/core/asset_packs.py`：本模块只管传输。
旧构造签名 `(model_dir, model, on_event, base_url)` 保留——它是 whisper 的
快捷方式，8 条下载层判据都建在它上面，不能因为重构而集体改写（判据一起改，
等于把「行为没变」这件事变成无法验证）。

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
import zipfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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

#: zip 解压需要的额外空间的估算系数 —— 按压缩包体积乘它来预留。
#:
#: 为什么需要：llama.cpp 的 `cudart` 包 404 MiB 解出来是 493 MiB（DLL 压缩率低），
#: 而主包 157 MiB 解出来是 ~700 MiB（4 倍）。取 2.0 是两者之间的保守值——
#: 宁可多报一点磁盘，也不要在「下载 99% 时解压失败」这种位置炸掉。
DISK_UNZIP_RATIO = 2.0


class DownloadCancelled(Exception):
    """用户取消。"""


class DownloadFailed(Exception):
    """下载失败，`code` 是协议错误码。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


EventSink = Callable[[str, dict[str, Any]], None]
Verifier = Callable[[Path], tuple[bool | None, str, str]]
#: 返回「还缺哪些文件」的回调。空列表 = 齐了。
Checker = Callable[[Path], list[str]]


@dataclass(frozen=True)
class _Item:
    """一个待下载项（由 `AssetFile` 解析出来的绝对落点）。"""

    name: str
    url: str
    dest: Path
    unzip: bool = False


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


def _safe_target(root: Path, name: str) -> Path | None:
    """把 zip 里的成员名解析成 `root` 下的绝对路径；不安全返回 `None`。

    三道闸：绝对路径、`..` 段、盘符/ADS 的冒号。来源虽是自己钉死版本的
    官方发布包，但这个函数只有 8 行，而漏掉它的后果是**任意路径写**。
    """
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or ":" in name:
        return None
    if not p.parts:
        return None
    target = root.joinpath(*p.parts)
    try:
        target.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return target


class ModelDownloader:
    """单次只能跑一个下载任务（同一个包只有一份，并发下载没有意义）。"""

    def __init__(
        self,
        model_dir: Path,
        model: str,
        on_event: EventSink,
        base_url: str | None = None,
    ) -> None:
        """whisper 权重下载的快捷构造（判据用这个入口，签名不要改）。"""
        self.pack_key = "whisper"
        self.model = model
        self.model_dir = Path(model_dir) / model
        base = (base_url or DEFAULT_BASE_URL).format(model=model)
        self._items = tuple(
            _Item(name=n, url=base + n, dest=self.model_dir / n) for n in MODEL_FILES
        )
        self._init(on_event)
        self._checker: Checker = lambda d: missing_model_files(d)

        def _whisper_verify(d: Path) -> tuple[bool | None, str, str]:
            from app.core import env_probe  # 延迟导入：本模块只在下模型时才被加载

            # device 从调用方决定更合适，但这里只需要「能不能跑」——用 cuda 试，
            # 失败也不代表模型坏（可能是没 GPU），故 reason 由调用方判断。
            return env_probe.verify_cuda(d, device="cuda", compute_type="float16")

        self._verifier: Verifier = _whisper_verify

    @classmethod
    def for_pack(cls, pack: Any, on_event: EventSink) -> ModelDownloader:
        """按 `asset_packs.AssetPack` 构造。whisper 之外的所有包走这里。"""
        self = cls.__new__(cls)
        self.pack_key = pack.key
        self.model = pack.key
        self.model_dir = Path(pack.target_dir)
        self._items = tuple(
            _Item(
                name=f.name,
                url=f.url,
                dest=Path(pack.target_dir) / f.dest_dir / f.name if f.dest_dir
                else Path(pack.target_dir) / f.name,
                unzip=f.unzip,
            )
            for f in pack.files
        )
        self._init(on_event)
        self._checker, self._verifier = _pack_verifiers(pack.key, pack.target_dir)
        return self

    def _init(self, on_event: EventSink) -> None:
        self._emit = on_event
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # 进度快照，`status()` 给 UI 做首屏兜底（事件可能早于前端挂载）
        self._last: dict[str, Any] = {}
        self._cur: tuple[str, int, int, int, int] = ("", 0, 0, 0, 0)

    # ---------------------------------------------------------------- 控制

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.running, "pack": self.pack_key, **self._last}

    def start(self) -> bool:
        """启动下载线程。已在跑则返回 False（幂等，不排队）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, name=f"model-downloader-{self.pack_key}", daemon=True
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
            total = sum(sizes)

            already = sum(
                size
                for item, size in zip(self._items, sizes)
                if size > 0 and item.dest.is_file() and item.dest.stat().st_size == size
            )
            # 解压还要额外一块空间，且解压完成后原 zip 才删 —— 峰值是「压缩包 + 解压产物」
            unzip_extra = int(
                sum(
                    size for item, size in zip(self._items, sizes) if item.unzip
                )
                * DISK_UNZIP_RATIO
            )
            need = max(total - already, 0) + unzip_extra + DISK_MARGIN
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
                    "pack": self.pack_key,
                    "model": self.model,
                    "model_path": str(self.model_dir),
                    "total_bytes": total,
                    "file_count": len(self._items),
                    "already_bytes": already,
                },
            )

            done_before = already
            for index, (item, size) in enumerate(zip(self._items, sizes), start=1):
                self._check_cancel()
                if size > 0 and item.dest.is_file() and item.dest.stat().st_size == size:
                    logging.info("模型下载：%s 已存在（%s），跳过", item.name, _human_bytes(size))
                else:
                    self._fetch(item, index, len(self._items), size, done_before, total)
                    done_before += size
                if item.unzip:
                    self._extract(item)

            # 大小比对（D-S3 的前半）：宁可在这里判死，也不要让推理引擎
            # 在用户点「开始」时才抛一个看不懂的原生错误。
            missing = self._checker(self.model_dir)
            if missing:
                raise DownloadFailed(
                    "E_MODEL_INCOMPLETE", f"下载结束但缺少文件：{', '.join(missing)}"
                )

            self._publish(
                "model_download_verifying",
                {"pack": self.pack_key, "detail": "文件已就绪，正在校验…"},
            )
            ok, detail, reason = self._verifier(self.model_dir)

            self._publish(
                "model_download_done",
                {
                    "pack": self.pack_key,
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
                {"pack": self.pack_key, "detail": "已取消。已完成的文件保留，下次点下载会接着下。"},
            )
        except DownloadFailed as exc:
            logging.warning("模型下载失败：%s", exc.detail)
            self._publish(
                "model_download_failed",
                {"pack": self.pack_key, "code": exc.code, "detail": exc.detail},
            )
        except Exception as exc:  # noqa: BLE001 — 下载线程的异常绝不能静默
            logging.exception("模型下载异常")
            self._publish(
                "model_download_failed",
                {"pack": self.pack_key, "code": "E_DOWNLOAD_FAILED", "detail": f"{type(exc).__name__}: {exc}"},
            )
        finally:
            with self._lock:
                self._thread = None

    # ---------------------------------------------------------------- 细节

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise DownloadCancelled

    def _probe_sizes(self) -> list[int]:
        """HEAD 每个文件拿大小。拿不到就记 0（该文件不做大小校验、总进度不计它）。"""
        sizes: list[int] = []
        for item in self._items:
            size = 0
            try:
                req = urllib.request.Request(item.url, method="HEAD")
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    raw = resp.headers.get("Content-Length")
                    if raw and raw.isdigit():
                        size = int(raw)
            except Exception as exc:  # noqa: BLE001 — 拿不到大小不是致命错误
                logging.warning("模型下载：HEAD %s 失败（%s），该文件不做大小校验", item.name, exc)
            sizes.append(size)
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
        item: _Item,
        index: int,
        count: int,
        size: int,
        done_before: int,
        total: int,
    ) -> None:
        # 进度事件要带「第几个文件 / 共几个 / 整体进度」，由 `_stream` 读取。
        # 只有一条下载线程，所以用实例属性传递是安全的。
        self._cur = (item.name, index, count, done_before, total)
        # 目标可能是子目录（如 `llama/`）——`_run` 只建了包根目录。
        # 漏这一步的表现是 `.part` 写入直接 ENOENT，然后被当成「网络错误」重试三次。
        item.dest.parent.mkdir(parents=True, exist_ok=True)
        part = item.dest.parent / (item.dest.name + ".part")
        attempt = 0

        while True:
            attempt += 1
            offset = part.stat().st_size if part.is_file() else 0
            if size > 0 and offset > size:
                # `.part` 比目标还大 ⇒ 上一次写坏了（例如换了模型版本）。重头来。
                logging.warning("模型下载：%s 的 .part 比预期大（%d > %d），重下", item.name, offset, size)
                part.unlink(missing_ok=True)
                offset = 0
            try:
                self._stream(item.url, part, offset)
                break
            except DownloadCancelled:
                raise
            except DownloadFailed:
                raise
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                if attempt > RETRIES:
                    raise DownloadFailed(
                        "E_NETWORK",
                        f"下载 {item.name} 失败（已重试 {RETRIES} 次）：{type(exc).__name__}: {exc}",
                    ) from exc
                backoff = 2 ** (attempt - 1)
                logging.warning(
                    "模型下载：%s 第 %d 次失败（%s），%d s 后重试", item.name, attempt, exc, backoff
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
                f"{item.name} 大小不符：期望 {size} 字节，实际 {actual}。已删除半成品，请重试。",
            )
        os.replace(part, item.dest)
        logging.info("模型下载：%s 完成（%s）", item.name, _human_bytes(actual))

        self._publish(
            "model_download_progress",
            {
                "pack": self.pack_key,
                "file": item.name,
                "file_index": index,
                "file_count": count,
                "done_bytes": done_before + actual,
                "total_bytes": total,
                "overall_pct": (done_before + actual) / total if total else 0.0,
                "bytes_per_sec": 0.0,
                "eta_sec": 0.0,
            },
        )

    def _extract(self, item: _Item) -> None:
        """解压 zip 到它所在目录，成功后删除原包。

        为什么整包解压而不是只挑几个文件：llama.cpp 的 `llama-server.exe` 只有
        9 KiB，真正的实现在 `llama-server-impl.dll` 里，而且 `ggml-cpu-*.dll` 是
        按 CPU 微架构在运行时动态挑的（sapphirerapids / haswell / x64 …）。
        「白名单挑文件」省不下多少体积（非必需 exe 合计约 15 MiB），却会造成
        「在我机器上好好的，在用户机器上缺 DLL 起不来」——这类问题只有实测才现形。
        """
        src = item.dest
        dest = src.parent
        self._check_cancel()
        try:
            with zipfile.ZipFile(src) as zf:
                members = [m for m in zf.infolist() if not m.is_dir()]
                if not members:
                    raise DownloadFailed("E_UNZIP_FAILED", f"{src.name} 里没有文件")
                total = sum(m.file_size for m in members) or 1
                done = 0
                skipped: list[str] = []
                self._publish(
                    "model_download_extracting",
                    {
                        "pack": self.pack_key,
                        "file": src.name,
                        "detail": f"正在解压 {src.name}（{len(members)} 个文件）…",
                        "pct": 0.0,
                    },
                )
                for m in members:
                    self._check_cancel()
                    target = _safe_target(dest, m.filename)
                    if target is None:
                        skipped.append(m.filename)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as fsrc, open(target, "wb") as fout:
                        while True:
                            buf = fsrc.read(CHUNK)
                            if not buf:
                                break
                            fout.write(buf)
                            done += len(buf)
                    self._publish(
                        "model_download_extracting",
                        {
                            "pack": self.pack_key,
                            "file": src.name,
                            "detail": f"正在解压 {src.name}…",
                            "pct": min(done / total, 1.0),
                        },
                    )
        except zipfile.BadZipFile as exc:
            raise DownloadFailed(
                "E_UNZIP_FAILED", f"{src.name} 不是有效的 zip（{exc}）。已保留原文件，请重试。"
            ) from exc
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise DownloadFailed(
                    "E_DISK_FULL", f"解压时磁盘写满：{src.name}（已保留原文件）"
                ) from exc
            raise
        if skipped:
            # 不解压不等于失败：zip 里混了路径穿越条目时，跳过它才是正确行为。
            # 但必须留痕，否则「少了几个文件」会变成玄学。
            logging.warning("解压 %s：跳过 %d 个不安全的成员：%s", src.name, len(skipped), skipped[:5])
        src.unlink(missing_ok=True)
        logging.info("模型下载：%s 已解压到 %s", item.name, dest)

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
                        cur_name, cur_index, cur_count, cur_done_before, cur_total = self._cur
                        # `done_bytes` / `total_bytes` / `overall_pct` 是**整体**进度，
                        # `file_*` 才是当前文件。两者都要：进度条用整体，
                        # 「第 3/5 个：tokenizer.json」那行用当前文件。
                        overall_done = cur_done_before + written
                        overall_total = cur_total or total
                        self._publish(
                            "model_download_progress",
                            {
                                "pack": self.pack_key,
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
        for p in self.model_dir.rglob("*.part"):
            try:
                p.unlink()
            except OSError as exc:  # noqa: PERF203 — 单个删不掉不该中断清理
                logging.warning("模型下载：删除 %s 失败（%s）", p.name, exc)


def _pack_verifiers(key: str, target_dir: Path) -> tuple[Checker, Verifier]:
    """按包类型给「还缺什么」与「能不能真跑」两套判断。

    两者都是**可选能力**，所以校验失败只反映在 `verified` / `detail` 上，
    不把下载判成失败 —— 文件确实下完了，那是另一件事。
    """
    if key == "local_ai":
        def _check(d: Path) -> list[str]:
            missing: list[str] = []
            server = _find_llama_server(d)
            if server is None:
                missing.append("llama/llama-server.exe")
            if not any(p.name.lower().endswith(".gguf") and "mmproj" not in p.name.lower()
                       for p in d.glob("*.gguf")):
                missing.append("*.gguf（主权重）")
            if not any("mmproj" in p.name.lower() for p in d.glob("*.gguf")):
                missing.append("mmproj-*.gguf（视觉投影器）")
            return missing

        def _verify(d: Path) -> tuple[bool | None, str, str]:
            # 只验「二进制能起来」。**不加载模型**：那要 3.2 GiB 显存 + 2.6 秒，
            # 而且和「下载是否成功」无关；真要跑，用户点开始时自然会验到。
            import subprocess

            server = _find_llama_server(d)
            if server is None:
                return None, "找不到 llama-server", "E_LOCAL_SERVER_MISSING"
            try:
                proc = subprocess.run(
                    [str(server), "--version"],
                    capture_output=True, text=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return False, f"llama-server 无法启动：{exc}", "E_LOCAL_SERVER_BROKEN"
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                return (
                    False,
                    "llama-server 启动失败（退出码 "
                    f"{proc.returncode}）：{' / '.join(tail) or '无输出'}。"
                    "常见原因是显卡驱动过旧（本包需要 CUDA 13.4 / 驱动 ≥ 580）",
                    "E_LOCAL_SERVER_BROKEN",
                )
            first = (proc.stdout or "").strip().splitlines()
            version = first[0] if first else "llama-server"
            return True, f"{version}；权重已就位（未加载，显存在开始任务时才占用）", ""

        return _check, _verify

    return (lambda d: missing_model_files(d)), (
        lambda d: _generic_verify(d)
    )


def _generic_verify(d: Path) -> tuple[bool | None, str, str]:
    from app.core import env_probe

    return env_probe.verify_cuda(d, device="cuda", compute_type="float16")


def _find_llama_server(model_dir: Path) -> Path | None:
    """与 `ai_backend._find_server()` 的落点规则保持一致（下载与查找必须同一处）。"""
    names = ("llama-server.exe", "llama-server") if os.name == "nt" else ("llama-server",)
    for name in names:
        cand = Path(model_dir) / "llama" / name
        if cand.is_file():
            return cand
    return None
