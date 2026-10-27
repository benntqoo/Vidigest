"""任务数据模型。

字段与 PLAN.md §6 的 `tasks` 表一一对应，便于 store 层直接映射。
路径字段统一用 `str`（DB 友好），需要 Path 时用对应的 property。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

# 文件名里不能出现的字符（Windows：\ / : * ? " < > |）
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


class TaskStatus(str, Enum):
    """任务状态机（PLAN.md §6）。单向推进，失败可回退到上一稳定态。

    `CANCELLED` 是 2026-10-03（阶段 2）新增的：取消是**用户意图**，不是故障。
    把它并进 `FAILED` 会让「失败 N 条」把用户自己点的取消也算进去，UI 上表现为
    「我只想停一条，怎么报错了」。两者在调度器里走不同分支（见 `dispatcher.py`）。
    """

    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    FRAMING = "framing"
    #: 抽帧完成、等待汇总。2026-10-27 新增：之前抽帧直接置 DONE，汇总阶段
    #: 没有任何"入口状态"，于是 `SUMMARIZING` 成了永远到不了的名字。
    FRAMED = "framed"
    SUMMARIZING = "summarizing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 中途态 → 回退目标。进程被杀时这些状态不可信，启动时要重置。
#: 例：downloading 说明下载没跑完，回退到 pending 重来。
#: `CANCELLED` 刻意不在表里——用户主动取消的任务，重启后不该自己又跑起来。
INTERRUPTED_ROLLBACK: dict[TaskStatus, TaskStatus] = {
    TaskStatus.DOWNLOADING: TaskStatus.PENDING,
    TaskStatus.TRANSCRIBING: TaskStatus.DOWNLOADED,
    TaskStatus.FRAMING: TaskStatus.TRANSCRIBED,
    TaskStatus.FRAMED: TaskStatus.TRANSCRIBED,
    TaskStatus.SUMMARIZING: TaskStatus.FRAMED,
}

#: 不需要重新处理的状态。
TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.CANCELLED})


def now_iso() -> str:
    """本地时间 ISO 字符串（秒精度），用于 created_at / updated_at。"""
    return datetime.now().isoformat(timespec="seconds")


def safe_name(text: str | None, fallback: str = "untitled", limit: int = 60) -> str:
    """把标题转成安全的目录名。

    去掉 Windows 非法字符、压缩空白、截断长度；结果为空则用 fallback。
    """
    if not text:
        return fallback
    cleaned = _ILLEGAL_FILENAME.sub(" ", text)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:limit].rstrip(" .")


@dataclass
class Task:
    """单条视频的处理任务。"""

    url: str
    platform: str | None = None
    video_id: str | None = None
    title: str | None = None
    duration_sec: int | None = None
    status: str = TaskStatus.PENDING.value
    stage_error: str | None = None
    retry_count: int = 0
    out_dir: str | None = None
    video_path: str | None = None
    audio_path: str | None = None
    transcript_json: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    id: int | None = None

    # ---- 状态 ----

    @property
    def status_enum(self) -> TaskStatus:
        return TaskStatus(self.status)

    def set_status(self, status: TaskStatus | str, error: str | None = None) -> None:
        self.status = status.value if isinstance(status, TaskStatus) else status
        self.stage_error = error
        self.updated_at = now_iso()

    def reset_interrupted(self) -> TaskStatus | None:
        """若当前处于中途态，回退到上一稳定态并返回新状态；否则返回 None。"""
        try:
            current = self.status_enum
        except ValueError:
            # 未知状态（例如旧版本写下的），按 pending 处理最安全
            self.set_status(TaskStatus.PENDING)
            return TaskStatus.PENDING
        target = INTERRUPTED_ROLLBACK.get(current)
        if target is None:
            return None
        self.set_status(target)
        return target

    # ---- 路径 ----

    @property
    def out_path(self) -> Path | None:
        return Path(self.out_dir) if self.out_dir else None

    @property
    def video_file(self) -> Path | None:
        return Path(self.video_path) if self.video_path else None

    @property
    def audio_file(self) -> Path | None:
        return Path(self.audio_path) if self.audio_path else None

    @property
    def transcript_file(self) -> Path | None:
        """转写稿的**纯文本**路径；没有可用转写稿时为 `None`。

        两个必须说清楚的地方：

        1. `transcript_json` 这个字段名是 0.5.0 留下的，存的是**路径**不是内容，
           且指向 `.json`（分段 + 时间戳）。而汇总只吃纯文本。两者是同目录同名的
           兄弟文件 —— `transcriber._write_all()` 一次写出 `txt / srt / json`，
           所以这里把后缀换成 `.txt`。**直接把 json 喂给 LLM 是错的**：会烧掉
           一大截 token 去读时间戳和 JSON 标点。

        2. 2026-10-27 修：`pipeline.run_summarize()` 原来读的是
           `task.transcript_path` —— **这个字段从来不存在**（0.5.0 起就写错）。
           之所以没早暴露：抽帧阶段当时直接置 `done`，汇总根本没进 GUI 的链路，
           只有 `run_one()`（CLI / 判据）会走到；0.6.3 把汇总接进调度器之后，
           每条任务都会在汇总阶段抛 `AttributeError: 'Task' object has no
           attribute 'transcript_path'`。

           为什么判据没拦住：`tools/rpc_smoke.py` 当时手写了
           `task.transcript_path = str(tr)` —— Python 允许给 dataclass 实例
           动态挂属性，于是判据「自己造出了它正在测的那个字段」，
           绿得很彻底，而真实代码路径是崩的。
        """
        raw = self.transcript_json
        if not raw:
            return None
        path = Path(raw)
        text = path.with_suffix(".txt") if path.suffix.lower() == ".json" else path
        return text if text.is_file() else None

    # ---- 序列化 ----

    def to_row(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> Task:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in dict(row).items() if k in known})

    def dir_name(self) -> str:
        """输出目录名：`<id 四位补零>_<安全标题>`。

        补零是为了在文件管理器里按名称排序时与任务序号一致——IPC 协议
        `docs/IPC协议规格.md` §5.1 的示例即 `workspace/0007_示例视频标题`。
        无标题时退回 video_id / 占位符。
        """
        prefix = f"{self.id:04d}" if self.id is not None else "0000"
        stem = safe_name(self.title or self.video_id, fallback="video")
        return f"{prefix}_{stem}"
