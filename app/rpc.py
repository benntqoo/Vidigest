"""Rust 宿主 ↔ Python Worker 的 RPC 入口（sidecar 模式）。

协议契约见 `docs/IPC协议规格.md`，本文件是它在 Python 侧的**唯一实现**。

三条不可违反的约束（违反任何一条即协议损坏）：

1. **stdout 只写协议帧**。启动时把 `sys.stdout` 指向 stderr，
   此后任何 stray `print` 都落到 stderr，无法污染协议流。
2. **stdout 写入必须加锁**。`log` / `task_update` 可能来自多个线程，
   不加锁会让两行 JSON 交错成一行。
3. **必须用独立线程读 stdin**。转写一条 20 分钟视频要 2 分 18 秒
   （PLAN.md §2.2），主线程若阻塞在转写上，就无法响应 `ping` / `pause` / `cancel`。

`app/core/` 不感知传输方式；本模块负责把 core 的 `on_progress` 回调
翻译成 `task_update` 事件。

由 Rust 侧以 `python -m app.rpc` 方式启动，cwd 应为项目根目录。
可用环境变量 `VIDIGEST_CONFIG` 指定配置文件路径（不通过 argv——argv 属于 cli.py）。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import asdict, fields as dataclass_fields
from pathlib import Path
from typing import Any, Callable, TextIO

from app.core import concurrency as concurrency_mod
from app.core import env_probe
from app.core import model_downloader
from app.core import paths
from app import platforms
from app.core.dispatcher import Dispatcher
from app.core.store import Store
from app.core.urls import extract_many
from app.models.config import (
    DEFAULT_CONFIG_NAME,
    LANGUAGE_AUTO,
    LANGUAGE_LABELS,
    Config,
    ConfigError,
)
from app.models.task import Task, TaskStatus

PROTOCOL_VERSION = 1
SERVER_VERSION = "0.3.0"

#: 内部事件名 → 协议事件名。
#: 调度器不感知协议，它只报「发生了什么」；名字翻译在这里做，与 `STAGE_LABELS` 同理。
EVENT_NAMES: dict[str, str] = {
    "state": "pipeline_state",
    "batch_done": "batch_done",
    "batch_aborted": "batch_aborted",
    "resource_warning": "resource_warning",
}

#: 协议唯一出口的原始引用。**必须在任何重定向之前拿到**，故置于模块级。
raw_stdout: TextIO = sys.stdout
raw_stdin: TextIO = sys.stdin

#: 状态 → UI 显示名。与 docs/IPC协议规格.md §5 的表一一对应。
STAGE_LABELS: dict[str, str] = {
    TaskStatus.PENDING.value: "等待中",
    TaskStatus.DOWNLOADING.value: "下载中",
    TaskStatus.DOWNLOADED.value: "待抽音频",
    TaskStatus.TRANSCRIBING.value: "转写中",
    TaskStatus.TRANSCRIBED.value: "待抽帧 / 汇总",
    TaskStatus.FRAMING.value: "抽帧中",
    TaskStatus.SUMMARIZING.value: "汇总中",
    TaskStatus.DONE.value: "完成",
    TaskStatus.FAILED.value: "失败",
    TaskStatus.CANCELLED.value: "已取消",
}


#: 设置界面允许改的配置段。**白名单，不是黑名单**：
#: 未知段与未知键一律丢弃并回 `warnings`，不静默写进 yaml。
EDITABLE_SECTIONS: tuple[str, ...] = (
    "paths",
    "runtime",
    "transcribe",
    "frames",
    "summarize",
    "download",
)

#: 保存时必须转成绝对路径的字段（相对路径会随 cwd 漂移；打包运行时 cwd 是安装目录）。
ABSOLUTE_PATH_FIELDS: tuple[str, ...] = ("work_dir", "model_dir", "cookies_file")

#: 一旦与当前值不同就必须警告的字段：它们直接决定分段边界
#: （`app/models/config.py:104-107`），改了会让 `samples/transcript_gpu.txt`
#: 的 662 段金标准基线失效。界面默认只读，但允许带警告修改。
BASELINE_FIELDS: tuple[str, ...] = (
    "transcribe.beam_size",
    "transcribe.vad_min_silence_ms",
)


def _configure_streams() -> None:
    """把三条标准流固定为 UTF-8 + LF。

    Windows 默认用 locale 编码（GBK），中文会直接写坏；换行转换还会把 `\\n`
    变成 `\\r\\n`，干扰按行分帧。
    """
    for stream in (raw_stdout, raw_stdin, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    try:
        raw_stdout.reconfigure(newline="\n")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


# ------------------------------------------------------------------ 协议出口


class Protocol:
    """协议帧的唯一出口。所有写入经此处，保证加锁与序号连续。"""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    def emit(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        req_id: str | None = None,
    ) -> None:
        """推一个事件。`req_id` 仅在响应对应命令时回填。"""
        with self._lock:
            self._seq += 1
            frame = {
                "v": PROTOCOL_VERSION,
                "type": "evt",
                "name": name,
                "id": req_id,
                "seq": self._seq,
                "ts": round(time.time(), 3),
                "data": data if data is not None else {},
            }
            self._stream.write(json.dumps(frame, ensure_ascii=False) + "\n")
            self._stream.flush()

    def error(
        self,
        code: str,
        msg: str,
        fatal: bool = False,
        req_id: str | None = None,
    ) -> None:
        self.emit("error", {"code": code, "msg": msg, "fatal": fatal}, req_id)


class _ProtocolLogHandler(logging.Handler):
    """把 logging 记录转发为 `log` 事件。

    ⚠️ 本 handler 内部不得再调用 logging，否则无限递归。
    """

    def __init__(self, proto: Protocol, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._proto = proto

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — 日志失败绝不能让进程崩
            msg = "<日志格式化失败>"
        self._proto.emit("log", {"level": record.levelname.lower(), "msg": msg})


# ------------------------------------------------------------------ Worker


class Worker:
    """命令处理与生命周期管理。

    调度职责全部委托给 `app/core/dispatcher.py`：本类只做
    「协议帧 ↔ 调度器调用」的翻译，不自己管线程与队列。
    """

    def __init__(
        self,
        cfg: Config,
        proto: Protocol,
        config_is_default: bool = False,
        config_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.proto = proto
        self.config_is_default = config_is_default
        #: 写回用的配置文件路径。**必须是实际读到的那个文件** ——
        #: `VIDIGEST_CONFIG` 指定 / 打包数据目录 / 源码运行的 `cwd/config.yaml`。
        self.config_path = Path(config_path) if config_path else Path.cwd() / DEFAULT_CONFIG_NAME
        # state.db 放仓库根（PLAN.md §4 目录树），与 work/ 分开，避免误删工作目录丢状态
        self.store = Store(cfg.root / "state.db")
        self.env = env_probe.probe(cfg.work_path())
        # 快速判据：硬件存在即按 GPU 规划并发。真实可用性由 _verify_env 异步确认。
        # 不能直接用 env.cuda.usable——probe() 后它是 None（未验证），
        # bool(None) 会退化成「按 CPU 规划」，而 CPU 模式下 8.7× 会掉到 1.44×。
        self.quick_gpu_usable = bool(self.env.cuda.device_count)
        self.concurrency = concurrency_mod.plan(
            cfg, self.env, gpu_usable=self.quick_gpu_usable
        )

        self.dispatcher = Dispatcher(
            cfg,
            self.store,
            self.concurrency,
            on_task_update=self._on_task_update,
            on_event=self._on_dispatcher_event,
        )

        self._inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()
        #: 模型下载器。**懒建**：只有点过「下载模型」才存在，避免每条命令都持有它。
        self._downloader: model_downloader.ModelDownloader | None = None
        #: 环境验证互斥。启动时有一次，用户点「重新校验」又可能来一次 ——
        #: 两次同时加载 3 GB 模型到显存，没有意义且很可能本身就失败。
        self._verify_lock = threading.Lock()
        self._handlers: dict[str, Callable[[dict[str, Any], str | None], None]] = {
            "hello": self._cmd_hello,
            "ping": self._cmd_ping,
            "add_tasks": self._cmd_add_tasks,
            "remove_task": self._cmd_remove_task,
            "list_tasks": self._cmd_list_tasks,
            "get_concurrency": self._cmd_get_concurrency,
            "start": self._cmd_start,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "cancel_task": self._cmd_cancel_task,
            "retry_task": self._cmd_retry_task,
            "shutdown": self._cmd_shutdown,
            "get_config": self._cmd_get_config,
            "set_config": self._cmd_set_config,
            "download_model": self._cmd_download_model,
            "cancel_model_download": self._cmd_cancel_model_download,
            "verify_env": self._cmd_verify_env,
            "clear_data": self._cmd_clear_data,
            "list_author": self._cmd_list_author,
        }

    # ---- 启动 ----

    def serve(self) -> int:
        _configure_streams()
        self._install_logging()
        # 关键：此后任何 stray print 都落到 stderr，协议流不会被污染
        sys.stdout = sys.stderr
        _maybe_enable_faulthandler()
        _maybe_run_import_probe()

        self._bootstrap()

        # **在主线程**预热原生依赖 —— 不能省，也不能挪进线程。
        # 实测（2026-10-04，打包产物）：非主线程里的首次原生 import 会静默卡死
        # 2 分钟以上；同一份产物在主线程里 import 全部依赖只要 2.03 s。
        # 代价是 `ready` 晚约 2 s（源码运行约 0.3 s），换来 daemon 线程不再卡死。
        ok, detail = env_probe.warmup_native_deps()
        if not ok:
            logging.warning("原生依赖预热未完成：%s（后续阶段会给出更准确的错误）", detail)

        threading.Thread(target=self._reader_loop, name="stdin-reader", daemon=True).start()
        threading.Thread(target=self._verify_env, name="env-verify", daemon=True).start()

        while not self._stop.is_set():
            try:
                frame = self._inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            if frame is None:
                # stdin EOF —— 宿主已消失。这是防孤儿进程的最后一道防线。
                logging.warning("stdin 已关闭（宿主进程消失），worker 退出")
                break
            self._dispatch(frame)

        self.store.close()
        return 0

    def _install_logging(self) -> None:
        """日志**只走协议一条路**（`log` 事件），默认不镜像到 stderr。

        为什么默认关掉镜像：root logger 若同时挂 stderr handler 与协议 handler，
        每条记录都会到达 Rust 侧两次 —— 一次是带正确 level 的结构化 `log` 事件，
        一次是被 Rust 标成 `debug` 的 stderr 原文。日志区会整体重复一遍，
        且第二份的级别是错的。

        `VIDIGEST_LOG_STDERR=1` 可打开镜像，便于在不开 UI 的情况下直接看 worker 输出。
        未捕获异常的 traceback 不受影响 —— 那由解释器的 excepthook 直接写 stderr。
        """
        root = logging.getLogger()
        root.setLevel(logging.INFO)

        want_stderr = os.environ.get("VIDIGEST_LOG_STDERR", "").strip().lower() not in (
            "",
            "0",
            "false",
            "no",
        )
        if want_stderr and not any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, _ProtocolLogHandler)
            for h in root.handlers
        ):
            stderr_handler = logging.StreamHandler(stream=sys.stderr)
            stderr_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
            )
            root.addHandler(stderr_handler)

        # 幂等：重复调用不得叠加多个协议 handler（否则同样会重复上报）
        if not any(isinstance(h, _ProtocolLogHandler) for h in root.handlers):
            root.addHandler(_ProtocolLogHandler(self.proto))

    def _bootstrap(self) -> None:
        """进程启动时的必要修复。**必须在处理任何命令前执行。**"""
        if self.config_is_default:
            logging.info("未找到 config.yaml，使用默认配置（相对路径锚定到仓库根）")
        changed = self.store.reset_interrupted()
        if changed:
            logging.info(
                "启动重置：%d 条中途态任务已回退到上一稳定态（上次进程可能被强杀）",
                len(changed),
            )
        logging.info("worker 就绪：%s", self.concurrency.to_dict())

    #: GPU 验证的看门狗超时（秒）。
    #:
    #: **为什么必须有**：`verify_cuda` 会加载原生 CUDA 运行库，而原生加载
    #: **卡住时既没有异常也没有 traceback** —— 实测 2026-10-04：打包产物里静默卡死
    #: 5 分钟以上，`env_verified` 永不推送，界面就永远停在「验证中」、
    #: `gpu_verified` 永远是 null。用户看到的是「按钮灰着，也不说为什么」。
    #:
    #: 超时后**如实上报「未知」，不是「不可用」**：三态布尔里 `None` 是「不知道」，
    #: 把它当成「否」等于拿一个未知结论去做并发规划（PLAN §4.2）。
    ENV_VERIFY_TIMEOUT = 120.0

    def _verify_env(self) -> None:
        """后台做一次**真实推理**验证 GPU，完成后推 `env_verified`。

        为什么异步：要加载模型，数秒。阻塞 `ready` 会让 UI 白屏。
        为什么不能只看 `device_count`：它只枚举硬件，即使 cuBLAS/cuDNN 缺失
        也返回 1（PLAN.md §9.1）。
        """
        if not self._verify_lock.acquire(blocking=False):
            logging.info("已有一次环境验证在进行，跳过本次")
            return
        try:
            self._verify_env_inner()
        finally:
            self._verify_lock.release()

    def _verify_env_inner(self) -> None:
        device = self.cfg.runtime.effective_device(True)
        compute_type = self.cfg.runtime.effective_compute_type(device)

        if device != "cuda":
            # 配置指定了 cpu —— 这是用户的选择，不是「没能验证」，故 reason 留空。
            self._apply_env_verification(False, "配置指定 device=cpu，跳过 GPU 验证")
            return

        box: list[tuple[bool | None, str, str]] = []

        def _run() -> None:
            try:
                box.append(
                    env_probe.verify_cuda(
                        self.cfg.model_path(), device=device, compute_type=compute_type
                    )
                )
            except BaseException as exc:  # noqa: BLE001 — 子线程里的异常绝不能静默
                box.append(
                    (False, f"验证线程异常：{type(exc).__name__}: {exc}", "verify_exception")
                )

        # 再套一层线程 + `join(timeout)`：本函数自己就住在 daemon 线程里，
        # 没法给自己设超时，而 `verify_cuda` 是同步阻塞的。
        inner = threading.Thread(target=_run, name="env-verify-inner", daemon=True)
        inner.start()
        inner.join(self.ENV_VERIFY_TIMEOUT)

        if not box:
            self._apply_env_verification(
                None,
                f"GPU 验证超过 {self.ENV_VERIFY_TIMEOUT:.0f} s 未返回，结论为**未知**（不是不可用）。"
                "常见成因：CUDA 运行库加载被安全软件拦截，或 DLL 缺失导致原生加载挂起。"
                "worker 日志里有 `CUDA 验证：…` 的分步打点，照它可定位卡在哪一步。",
                "verify_timeout",
            )
            return

        ok, detail, reason = box[0]
        self._apply_env_verification(ok, detail, reason)

    def _apply_env_verification(self, ok: bool | None, detail: str, reason: str = "") -> None:
        """写回验证结论并广播。

        `ok=None` 表示**未验证 / 未知** —— 三态布尔里它不是「否」（PLAN §4.2）。
        `reason` 是「为什么没验成 / 为什么不行」的机器可读原因码，见
        `app/core/env_probe.py` 的 `REASON_*`。它让界面能说「模型未安装」而不是
        「GPU 不可用」——后者会把用户引向完全错误的排查方向。
        """
        self.env.cuda.usable = ok
        self.env.cuda.detail = detail
        self.env.cuda.verified = ok is not None

        # 并发规划用哪个值：
        # - 已验证（True / False）→ 用结论；
        # - **未知（None）→ 保持快判结果**，不因为「没验出来」就把用户按 CPU 规划。
        planned_gpu = bool(self.quick_gpu_usable) if ok is None else ok
        self.concurrency = concurrency_mod.plan(self.cfg, self.env, gpu_usable=planned_gpu)
        self.dispatcher.conc = self.concurrency

        label = "可用" if ok is True else ("不可用" if ok is False else "未知（未验证）")
        suffix = f" [{reason}]" if reason else ""
        logging.info("环境验证：GPU %s —— %s%s", label, detail, suffix)
        self.proto.emit(
            "env_verified",
            {
                "gpu_usable": ok,
                "gpu_verified": ok is not None,
                "detail": detail,
                # 空串转 None：协议里「没有原因」就用 null，别用 "" 让前端两套判断
                "blocked_reason": reason or None,
                "concurrency": self.concurrency.to_dict(),
            },
        )

    def _reader_loop(self) -> None:
        """独立线程读 stdin，逐帧投递到 `_inbox`。

        用 `readline()` 而非 `for line in sys.stdin`——后者有预读缓冲，
        会导致命令延迟到达（转写中发 pause 要等很久才生效）。
        """
        while True:
            try:
                line = raw_stdin.readline()
            except (OSError, ValueError) as exc:  # 管道断了
                logging.warning("读取 stdin 失败：%s", exc)
                break
            if not line:  # EOF
                break
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                self.proto.error("E_BAD_FRAME", f"不是合法 JSON：{exc}")
                continue
            if not isinstance(frame, dict):
                self.proto.error("E_BAD_FRAME", "帧必须是 JSON 对象")
                continue
            self._inbox.put(frame)
        self._inbox.put(None)  # 哨兵：通知主循环退出

    # ---- 分发 ----

    def _dispatch(self, frame: dict[str, Any]) -> None:
        req_id = frame.get("id")
        if not isinstance(req_id, str):
            req_id = None
        name = frame.get("name")
        args = frame.get("args")
        if not isinstance(args, dict):
            args = {}

        if not isinstance(name, str):
            self.proto.error("E_BAD_ARGS", "命令缺少 name 字段", req_id=req_id)
            return

        handler = self._handlers.get(name)
        if handler is None:
            self.proto.error(
                "E_UNKNOWN_CMD", f"未知命令：{name}（协议版本不匹配？）", req_id=req_id
            )
            return

        try:
            handler(args, req_id)
        except Exception as exc:  # noqa: BLE001 — 单条命令失败不能拖垮 worker
            logging.exception("命令 %s 执行失败", name)
            self.proto.error("E_INTERNAL", f"{type(exc).__name__}: {exc}", req_id=req_id)

    # ---- 命令实现 ----

    def _cmd_hello(self, args: dict[str, Any], req_id: str | None) -> None:
        client_version = args.get("client_version")
        if isinstance(client_version, str):
            logging.info("宿主握手：client_version=%s", client_version)
        self.proto.emit(
            "ready",
            {
                "server_version": SERVER_VERSION,
                "protocol": PROTOCOL_VERSION,
                "gpu_usable": self.quick_gpu_usable,
                "gpu_verified": bool(self.env.cuda.verified),
                "concurrency": self.concurrency.to_dict(),
                "db_path": str(self.store.db_path),
                "config_is_default": self.config_is_default,
                "pipeline_state": self.dispatcher.state.value,
            },
            req_id,
        )

    def _cmd_ping(self, args: dict[str, Any], req_id: str | None) -> None:
        self.proto.emit("pong", {}, req_id)

    def _cmd_add_tasks(self, args: dict[str, Any], req_id: str | None) -> None:
        urls = args.get("urls")
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            self.proto.error(
                "E_BAD_ARGS", "add_tasks 需要 args.urls 为字符串数组", req_id=req_id
            )
            return

        added: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []

        for item in extract_many(urls):
            task = Task(url=item.target, platform=item.platform)
            stored, created = self.store.add(task)
            if created:
                added.append(
                    {
                        "task_id": stored.id,
                        "url": stored.url,
                        "title": stored.title,
                        "platform": stored.platform,
                        "status": stored.status,
                    }
                )
            else:
                skipped.append({"url": item.target, "reason": "duplicate"})

        logging.info("入队 %d 条，跳过 %d 条", len(added), len(skipped))
        self.proto.emit("tasks_added", {"added": added, "skipped": skipped}, req_id)

    def _cmd_remove_task(self, args: dict[str, Any], req_id: str | None) -> None:
        task_id = args.get("task_id")
        if not isinstance(task_id, int):
            self.proto.error("E_BAD_ARGS", "remove_task 需要整数 args.task_id", req_id=req_id)
            return
        # 先取消：删除一条正在跑的任务，不能只从表里抹掉而让线程继续跑它的产出
        self.dispatcher.cancel(task_id)
        removed = self.store.delete(task_id)
        if not removed:
            self.proto.error("E_BAD_ARGS", f"任务不存在：{task_id}", req_id=req_id)
            return
        self.proto.emit("task_removed", {"task_id": task_id}, req_id)

    def _cmd_list_tasks(self, args: dict[str, Any], req_id: str | None) -> None:
        self.proto.emit(
            "task_list",
            {"tasks": [self._task_payload(t) for t in self.store.list()]},
            req_id,
        )

    def _cmd_get_concurrency(self, args: dict[str, Any], req_id: str | None) -> None:
        self.proto.emit("concurrency", self.concurrency.to_dict(), req_id)

    # ---- 设置与模型（2026-10-24 新增，见 docs/设置界面与模型下载_执行单.md）----

    def _cmd_get_config(self, args: dict[str, Any], req_id: str | None) -> None:
        self.proto.emit("config", self._config_payload(), req_id)

    def _cmd_set_config(self, args: dict[str, Any], req_id: str | None) -> None:
        """部分更新配置：深合并到对应 section → validate → 写盘。

        **不在这里应用**（D-S1）：`Config` 只在进程启动时读一次，`Dispatcher` 的并发
        与模型缓存都按它算好了。Rust 侧收到 `config_saved` 会重启 worker，
        新进程自然读到新配置 —— 比热应用少一整类 bug。

        安全边界（写进实现，不是文档装饰）：
        - 只接受 `EDITABLE_SECTIONS` 白名单段与 dataclass 里真实存在的键；
          未知段 / 键丢弃并回 `warnings`，不静默写盘
        - `summarize.api_key` **永不允许**写入（密钥只走环境变量）
        - `paths.*` 的相对路径按 `cfg.root` 解析后**存绝对路径**
        - 改 `beam_size` / `vad_min_silence_ms` 必回 `E_BASELINE_PARAM_CHANGED`
        """
        patch = args.get("patch")
        if not isinstance(patch, dict):
            self.proto.error("E_BAD_ARGS", "set_config 需要 args.patch 为对象", req_id=req_id)
            return

        warnings: list[str] = []
        data = self.cfg.to_dict()

        for section, values in patch.items():
            if section not in EDITABLE_SECTIONS:
                warnings.append(f"E_UNKNOWN_SECTION:{section}")
                continue
            if not isinstance(values, dict):
                warnings.append(f"E_BAD_SECTION_TYPE:{section}")
                continue
            known = {f.name for f in dataclass_fields(type(getattr(self.cfg, section)))}
            target = data.setdefault(section, {})
            for key, value in values.items():
                if section == "summarize" and key == "api_key":
                    # 密钥永远不进配置文件（`config.py:130-137` 的设计）。
                    warnings.append("E_API_KEY_NOT_ALLOWED")
                    continue
                if key not in known:
                    warnings.append(f"E_UNKNOWN_KEY:{section}.{key}")
                    continue
                target[key] = value

        # 相对路径 → 绝对路径。否则打包运行时 cwd 是安装目录，相对路径会漂到那里。
        paths_patch = data.get("paths", {})
        for key in ABSOLUTE_PATH_FIELDS:
            raw = paths_patch.get(key)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                paths_patch[key] = None
                continue
            if not isinstance(raw, str):
                warnings.append(f"E_BAD_PATH_TYPE:{key}")
                paths_patch[key] = getattr(self.cfg.paths, key)
                continue
            p = Path(raw).expanduser()
            paths_patch[key] = str(p if p.is_absolute() else self.cfg.root / p)

        try:
            new_cfg = Config.from_dict(data, root=self.cfg.root)
            new_cfg.validate()
        except ConfigError as exc:
            self.proto.error("E_BAD_ARGS", str(exc), req_id=req_id)
            return

        # 基线参数（决定分段边界）——允许改，但必须带警告
        for dotted in BASELINE_FIELDS:
            section, key = dotted.split(".", 1)
            if getattr(getattr(new_cfg, section), key) != getattr(getattr(self.cfg, section), key):
                warnings.append(f"E_BASELINE_PARAM_CHANGED:{dotted}")

        # 目录可写探测（R3）：只探用户真的改过的目录
        for key in ("work_dir", "model_dir"):
            before = getattr(self.cfg.paths, key)
            after = getattr(new_cfg.paths, key)
            if after != before:
                problem = _probe_dir_writable(Path(after))
                if problem:
                    self.proto.error(
                        "E_PATH_NOT_WRITABLE",
                        f"paths.{key} 不可写：{after}\n  {problem}",
                        req_id=req_id,
                    )
                    return
        try:
            new_cfg.save(self.config_path)
        except OSError as exc:
            self.proto.error(
                "E_CONFIG_SAVE_FAILED", f"写入 {self.config_path} 失败：{exc}", req_id=req_id
            )
            return

        # 模型目录 / 模型名变了 ⇒ 旧的下载器指向旧路径，必须重建
        if self._downloader is not None and (
            new_cfg.paths.model_dir != self.cfg.paths.model_dir
            or new_cfg.runtime.model != self.cfg.runtime.model
        ):
            self._downloader.cancel()
            self._downloader = None

        self.cfg = new_cfg
        self.config_is_default = False
        logging.info("配置已保存：%s（warnings=%s）", self.config_path, warnings)
        self.proto.emit(
            "config_saved",
            {
                "path": str(self.config_path),
                "config": new_cfg.to_dict(),
                "warnings": warnings,
                # Rust 侧据此重启 worker；UI 据此提示「正在应用」
                "restart_required": True,
            },
            req_id,
        )

    def _cmd_download_model(self, args: dict[str, Any], req_id: str | None) -> None:
        """启动模型下载。幂等：已在下载则不重复启动。

        进度 / 完成 / 失败 / 取消全部以 `model_download_*` 事件异步推送 ——
        本命令**不阻塞**（下载 2.88 GiB 要几分钟，阻塞主循环会连心都不跳）。
        """
        model = args.get("model") or self.cfg.runtime.model
        if not isinstance(model, str):
            self.proto.error("E_BAD_ARGS", "download_model 的 args.model 必须是字符串", req_id=req_id)
            return
        if model != self.cfg.runtime.model:
            self.proto.error(
                "E_BAD_ARGS",
                f"模型名不符：请求 {model}，配置里是 {self.cfg.runtime.model}。"
                "请先在设置里改 runtime.model 并保存。",
                req_id=req_id,
            )
            return

        dl = self._ensure_downloader()
        if dl.running:
            self.proto.emit(
                "model_download_started",
                {"model": model, "already_running": True, "model_path": str(dl.model_dir)},
                req_id,
            )
            return
        dl.start()

    def _cmd_cancel_model_download(self, args: dict[str, Any], req_id: str | None) -> None:
        dl = self._downloader
        if dl is None or not dl.cancel():
            # 没有在跑就如实说，不要假装取消成功
            self.proto.emit(
                "model_download_cancelled",
                {"was_running": False, "detail": "当前没有正在进行的下载"},
                req_id,
            )

    def _cmd_verify_env(self, args: dict[str, Any], req_id: str | None) -> None:
        """手动重新校验 GPU（设置页的「重新校验」）。

        为什么需要它：改了 `model_dir` 之后用户想立刻看到结论，而不是「保存 → 重启
        worker → 等后台验证」。本命令复用启动时那条同一条验证路径。
        """
        if self._verify_lock.locked():
            self.proto.error("E_BUSY", "已有一次环境验证在进行，请稍候", req_id=req_id)
            return
        # **先发回执，再起线程**：模型缺失时 `_verify_env` 会立刻返回（不加载模型），
        # 若先起线程，`env_verified` 可能早于 `env_verify_started` 到达 ——
        # 前端先清「校验中」再被置回「校验中」，就永远转圈了。
        self.proto.emit("env_verify_started", {}, req_id)
        threading.Thread(target=self._verify_env, name="env-verify-manual", daemon=True).start()

    # ---- 设置：内部 ----

    def _ensure_downloader(self) -> model_downloader.ModelDownloader:
        if self._downloader is None:
            self._downloader = model_downloader.ModelDownloader(
                Path(self.cfg.paths.model_dir),
                self.cfg.runtime.model,
                self._on_download_event,
                base_url=self.cfg.download.base_url,
            )
        return self._downloader

    def _on_download_event(self, name: str, data: dict[str, Any]) -> None:
        """下载器事件 → 协议帧。下载器本身不感知协议（与 `core/` 的约定一致）。"""
        self.proto.emit(name, data)
        if name == "model_download_done" and "verified" in data:
            # 下载完成时已经真实加载过模型 —— 顺手把环境态更新掉，
            # 用户不必再点一次「重新校验」才看到状态栏变绿。
            self._apply_env_verification(
                data.get("verified"),
                str(data.get("detail") or ""),
                str(data.get("blocked_reason") or ""),
            )

    def _config_payload(self) -> dict[str, Any]:
        """`get_config` 的响应体：全量配置 + 元信息。"""
        cfg = self.cfg
        model_path = cfg.model_path()
        missing = env_probe.missing_model_files(model_path)
        size = 0
        if model_path.is_dir():
            try:
                size = sum(p.stat().st_size for p in model_path.iterdir() if p.is_file())
            except OSError as exc:  # noqa: PERF203 — 单个文件读不到不该让整页失败
                logging.warning("统计模型目录大小失败：%s", exc)
        api_key_env = cfg.summarize.api_key_env
        # 语言下拉的选项由**后端下发**：`LANGUAGE_LABELS` 是唯一真源，
        # 前端不另存一份（两份表必然漂移，而且漂移后没人会发现）。
        languages = [{"code": LANGUAGE_AUTO, "label": "自动检测"}]
        languages += [
            {"code": code, "label": f"{label}（{code}）"}
            for code, label in LANGUAGE_LABELS.items()
        ]
        return {
            "path": str(self.config_path),
            "is_default": self.config_is_default,
            "config": cfg.to_dict(),
            "languages": languages,
            "model_path": str(model_path),
            "model_present": not missing,
            "model_missing_files": missing,
            "model_size_bytes": size,
            "read_only_fields": list(BASELINE_FIELDS),
            # 工作目录是否已存在（且非空）。首启引导据此改文案：不是新建，
            # 而是「沿用上次安装留下的目录」。卸载保留 workspace 之后这很常见。
            "work_dir_exists": Path(cfg.paths.work_dir).is_dir(),
            "api_key_env": api_key_env,
            "api_key_present": bool(os.environ.get(api_key_env)),
            "pipeline_state": self.dispatcher.state.value,
            "download": self._downloader.status() if self._downloader else {"running": False},
        }

    def _cmd_clear_data(self, args: dict[str, Any], req_id: str | None) -> None:
        """清空**任务历史**或**工作目录内容**（`workspace`），不碰模型。

        `scope`：
        - `history`：删 `state.db` 里全部任务记录（并重置自增序号）
        - `workspace`：清空 `work_dir` 下的**内容**（目录本身保留）
        - `all`：两者都做

        **为什么不碰 `model_dir`**：模型 2.9 GiB，重下是最贵的操作。
        想删模型的人可以直接删目录，或者卸载（卸载也会保留它）。
        给一个「顺手把模型删了」的按钮，只会制造事故。

        有任务在跑时直接拒绝：一边跑一边删产出，症状是「转写突然报文件不存在」，
        而且看起来像 bug。
        """
        scope = str(args.get("scope") or "").strip()
        if scope not in ("history", "workspace", "all"):
            self.proto.error("E_BAD_ARGS", "scope 必须是 history / workspace / all", req_id=req_id)
            return
        if self.dispatcher.state.value != "idle":
            self.proto.error("E_BUSY", "有任务在跑，等它结束再清空", req_id=req_id)
            return

        result: dict[str, Any] = {"scope": scope}
        if scope in ("history", "all"):
            result["tasks_removed"] = self.store.clear_all()
        if scope in ("workspace", "all"):
            result["files_removed"], result["errors"] = _empty_directory(
                Path(self.cfg.paths.work_dir)
            )
        logging.info("清空数据：%s", result)
        self.proto.emit("data_cleared", result, req_id)

    def _cmd_list_author(self, args: dict[str, Any], req_id: str | None) -> None:
        """列一个作者/频道/合集的作品（批量下载的入口）。

        `yt-dlp --flat-playlist -J` 要跑几秒到几十秒，所以在**工作线程**里做，
        主循环继续处理 ping / 状态查询 —— 阻塞主循环会让人以为界面卡死。

        三类结果分开表达，别混成一个：
        - 成功 → `author_page`
        - 该平台不支持 → `E_AUTHOR_UNSUPPORTED`（附可操作提示）
        - 拉取失败 → `E_AUTHOR_FAILED`（网络 / 站点变了）
        把「不支持」和「失败」混成空列表，会让用户以为作者没发过作品。
        """
        url = str(args.get("url") or "").strip()
        if not url:
            self.proto.error("E_BAD_ARGS", "list_author 需要 url", req_id=req_id)
            return
        try:
            limit = int(args.get("limit") or 0)
        except (TypeError, ValueError):
            self.proto.error("E_BAD_ARGS", "limit 必须是整数", req_id=req_id)
            return
        platform = args.get("platform")
        platform = str(platform) if isinstance(platform, str) and platform else None

        cfg = self.cfg

        def work() -> None:
            try:
                page = platforms.list_author(url, cfg, platform=platform, limit=limit)
            except platforms.NotSupported as exc:
                self.proto.error("E_AUTHOR_UNSUPPORTED", str(exc), req_id=req_id)
                return
            except Exception as exc:  # noqa: BLE001 — 网络/站点变更，如实上报
                logging.exception("列举作者作品失败")
                self.proto.error(
                    "E_AUTHOR_FAILED", f"{type(exc).__name__}: {exc}", req_id=req_id
                )
                return
            self.proto.emit(
                "author_page",
                {
                    "platform": page.platform,
                    "title": page.title,
                    "total": page.total,
                    "truncated": page.truncated,
                    "url": url,
                    "items": [asdict(item) for item in page.items],
                },
                req_id,
            )

        threading.Thread(target=work, name="list-author", daemon=True).start()

    # ---- 流水线控制 ----

    def _cmd_start(self, args: dict[str, Any], req_id: str | None) -> None:
        """启动流水线：把可续跑的任务入队并启动线程池。幂等。

        **只入队「可续跑」的任务**（pending / downloaded / transcribed 等非终态且
        非 failed）。failed / cancelled 要显式 `retry_task` 才会再跑——否则一条永久
        坏链会在每次点「开始」时被重试一遍，看起来像程序在空转。

        start 在 submit **之前**（2026-10-06）：`submit()` 在 `_abort_reason`
        置位期间拒收一切任务，而清掉中止位的正是 `start()` 的重铺分支。
        先 submit 的话，环境故障中止后的第一次「开始」必然 added=0，
        用户得多点一次且看到「入队 0 条」——顺序反过来才是单次点击可恢复。
        """
        resumable = self.store.resumable()
        self.dispatcher.start()
        added = self.dispatcher.submit(resumable)
        logging.info("start：续跑入队 %d 条（state=%s）", added, self.dispatcher.state.value)
        self.proto.emit(
            "started",
            {
                "added": added,
                "state": self.dispatcher.state.value,
                "concurrency": self.concurrency.to_dict(),
            },
            req_id,
        )

    def _cmd_pause(self, args: dict[str, Any], req_id: str | None) -> None:
        accepted = self.dispatcher.pause()
        logging.info("pause：%s（state=%s）", "已接受" if accepted else "忽略", self.dispatcher.state.value)
        self.proto.emit(
            "paused", {"accepted": accepted, "state": self.dispatcher.state.value}, req_id
        )

    def _cmd_resume(self, args: dict[str, Any], req_id: str | None) -> None:
        accepted = self.dispatcher.resume()
        logging.info("resume：%s（state=%s）", "已接受" if accepted else "忽略", self.dispatcher.state.value)
        self.proto.emit(
            "resumed", {"accepted": accepted, "state": self.dispatcher.state.value}, req_id
        )

    def _cmd_cancel_task(self, args: dict[str, Any], req_id: str | None) -> None:
        task_id = args.get("task_id")
        if not isinstance(task_id, int):
            self.proto.error("E_BAD_ARGS", "cancel_task 需要整数 args.task_id", req_id=req_id)
            return
        if not self.dispatcher.cancel(task_id):
            self.proto.error("E_BAD_ARGS", f"任务无法取消（不存在或已完成）：{task_id}", req_id=req_id)
            return
        task = self.store.get(task_id)
        if task is None:
            self.proto.error("E_BAD_ARGS", f"任务不存在：{task_id}", req_id=req_id)
            return
        # 响应即当前快照：排队中的任务在这里已是 cancelled；运行中的还要等阶段
        # 中断，最终态会以一次主动推送的 task_update 到达（id=null）。
        self.proto.emit("task_update", self._task_payload(task), req_id)

    def _cmd_retry_task(self, args: dict[str, Any], req_id: str | None) -> None:
        task_id = args.get("task_id")
        if not isinstance(task_id, int):
            self.proto.error("E_BAD_ARGS", "retry_task 需要整数 args.task_id", req_id=req_id)
            return
        if not self.dispatcher.retry(task_id):
            self.proto.error(
                "E_BAD_ARGS", f"任务无法重试（不存在、已完成或仍在队列中）：{task_id}", req_id=req_id
            )
            return
        self.dispatcher.start()  # 首次调用 retry 时线程池可能还没起
        task = self.store.get(task_id)
        if task is not None:
            self.proto.emit("task_update", self._task_payload(task), req_id)

    def _cmd_shutdown(self, args: dict[str, Any], req_id: str | None) -> None:
        logging.info("收到 shutdown，等待在跑的任务结束（最长 30 秒）")
        self.dispatcher.stop(timeout=30.0)
        logging.info("worker 退出")
        self.proto.emit("bye", {}, req_id)
        self._stop.set()

    # ---- 调度器回调 ----

    def _on_task_update(self, task: Task, progress: float, elapsed: float | None) -> None:
        """调度器 → `task_update` 事件。**会从多个 worker 线程调用**，故 `emit` 必须加锁。"""
        self.proto.emit(
            "task_update",
            self._task_payload(task, progress=progress, elapsed=elapsed),
        )

    def _on_dispatcher_event(self, name: str, data: dict[str, Any]) -> None:
        proto_name = EVENT_NAMES.get(name, name)
        if proto_name == "batch_aborted":
            # 环境级故障：不是进程级致命（worker 仍能响应命令），但整批停了，
            # 所以同时给一条 fatal=false 的 error，让 Rust 侧有明确的错误通道。
            self.proto.error("E_ENV_FATAL", str(data.get("reason", "")), fatal=False)
        self.proto.emit(proto_name, data)

    # ---- 序列化 ----

    @staticmethod
    def _task_payload(
        task: Task,
        progress: float | None = None,
        elapsed: float | None = None,
    ) -> dict[str, Any]:
        """Task → IPC `task_list` / `task_update` 用的快照结构。

        字段名与 `docs/IPC协议规格.md` §5.1 一致；不在协议里的字段（如
        `video_path`）刻意不外传，减少两端耦合面。
        """
        if progress is None:
            progress = 1.0 if task.status == TaskStatus.DONE.value else 0.0
        return {
            "task_id": task.id,
            "url": task.url,
            "platform": task.platform,
            "title": task.title,
            "duration_sec": task.duration_sec,
            "status": task.status,
            "stage": STAGE_LABELS.get(task.status, task.status),
            "progress": progress,
            "elapsed_sec": elapsed,
            "retry_count": task.retry_count,
            "out_dir": task.out_dir,
            "error": task.stage_error,
        }


def _probe_dir_writable(path: Path) -> str | None:
    """目录可写探测 —— 实现在 `paths.probe_dir_writable()`。

    首启挑默认目录也要用它（安装目录可能只读），所以不能只留在 RPC 这一层。
    """
    return paths.probe_dir_writable(path)


def _empty_directory(path: Path) -> tuple[int, list[str]]:
    """清空目录**内容**，保留目录本身。返回 `(删掉的文件数, 错误列表)`。

    为什么保留目录：`work_dir` 是用户配的路径，删掉它等于改配置语义；
    而且下次启动又要重建，多一次无谓的失败面。

    为什么要返回错误列表而不是抛：删不掉的文件（被占用）不该让整次操作失败 ——
    如实报几条、让用户知道还剩什么，比半路抛异常、界面只显示「失败」有用。
    """
    removed = 0
    errors: list[str] = []
    if not path.is_dir():
        return 0, []
    for child in path.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                removed += sum(1 for f in child.rglob("*") if f.is_file())
                shutil.rmtree(child)
            else:
                child.unlink()
                removed += 1
        except OSError as exc:
            errors.append(f"{child.name}: {type(exc).__name__}: {exc}")
    return removed, errors


def _maybe_enable_faulthandler() -> None:
    """`VIDIGEST_FAULTHANDLER=<秒>` 时，定期把所有线程的栈打到 stderr。

    **为什么非它不可**：原生调用（`LoadLibrary`、CUDA 运行库初始化）卡住时，
    Python 层**没有任何日志可打** —— 打点只会停在卡住之前的最后一步。
    实测 2026-10-04：打包产物里 `import numpy` 静默卡死 2 分钟以上，
    `logging` 打点全部停在「nvidia DLL 目录已注入」，看不出栈在哪。
    `faulthandler` 是 CPython 自带、能在卡死时给出**真实栈帧**的唯一手段。

    默认关闭（它定期往 stderr 写，不该进正常路径）；
    需要时 `set VIDIGEST_FAULTHANDLER=20`。
    """
    raw = os.environ.get("VIDIGEST_FAULTHANDLER", "").strip()
    if not raw:
        return
    try:
        delay = float(raw)
    except ValueError:
        logging.warning("VIDIGEST_FAULTHANDLER 不是数字：%r，忽略", raw)
        return
    try:
        import faulthandler
    except ImportError as exc:  # pragma: no cover — 标准库，理论上不会缺
        logging.warning("无法导入 faulthandler：%s", exc)
        return
    faulthandler.dump_traceback_later(delay, repeat=True, file=sys.stderr)
    logging.info("已启用 faulthandler：每 %.0f s dump 一次全部线程栈到 stderr", delay)


def _maybe_run_import_probe() -> None:
    """`VIDIGEST_IMPORT_PROBE=1` 时，在主线程把关键原生依赖**逐个** import 并报耗时。

    **为什么需要**：原生 import 卡死时，普通打点只能告诉你「卡在 import 区间」。
    实测 2026-10-04：打包产物里 `_verify_env` **子线程**的 `import numpy`
    静默卡死 2 分钟以上，而同一份 `_internal` 在 venv 下只要 0.3 s。
    这个 probe 一次能区分两种成因：
      · probe 里每个都秒完 → 问题是「在**非主线程**里做首次原生 import」；
      · probe 里同样卡住    → 与线程无关，是打包本身的问题。
    每步都**立即**上报，所以卡住时最后一条日志就是答案。
    """
    if os.environ.get("VIDIGEST_IMPORT_PROBE", "").strip().lower() not in ("1", "true", "yes"):
        return

    import importlib
    import time as _time

    logging.info("import 探针：开始（主线程，逐个）")
    t_all = _time.monotonic()
    for name in (
        "app.core.cuda_dll",
        "numpy",
        "ctranslate2",
        "tokenizers",
        "onnxruntime",
        "av",
        "faster_whisper",
    ):
        t0 = _time.monotonic()
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — 探针不该让 worker 起不来
            logging.error("import 探针：%s 失败 —— %s: %s", name, type(exc).__name__, exc)
            continue
        logging.info("import 探针：%s 完成（%.2fs）", name, _time.monotonic() - t0)
    logging.info("import 探针：全部结束（累计 %.2fs）", _time.monotonic() - t_all)


def main() -> int:
    _configure_streams()
    # 配置来源：源码运行 = cwd/config.yaml（与改造前一致）；
    # 打包运行 = 数据目录下的 config.yaml（见 core/paths.py）。
    cfg_path = paths.resolve_config_path()
    cfg, used_default = Config.load_or_default(cfg_path, root=paths.app_root())
    # 写回的必须与读到的**是同一个文件**，否则「保存了但下次启动没生效」。
    effective = Path(cfg_path) if cfg_path else Path.cwd() / DEFAULT_CONFIG_NAME
    proto = Protocol(raw_stdout)
    return Worker(
        cfg, proto, config_is_default=used_default, config_path=effective
    ).serve()


if __name__ == "__main__":
    sys.exit(main())
