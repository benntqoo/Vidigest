"""硬件能力探测。

回答的问题是「这台机器实际能做多少并行」——即 PLAN.md §3.2 并发公式的输入。

设计约束（PLAN.md §4.1）：

- 探测项失败返回 None / 空集合，**绝不抛异常**。自检脚本不该因为查不到显卡就崩。
- `cuda.usable` 必须由**真实推理**验证，不能读 `get_cuda_device_count()`。
  后者只说明「检测到硬件」，不代表 cuBLAS/cuDNN 能加载（PLAN.md §9.1）。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

# 探测超时：nvidia-smi 偶发卡住，不能让它拖死整个自检
_NVIDIA_SMI_TIMEOUT = 15

#: faster-whisper 必需的文件名。
#:
#: **放这里而不是放在下载器里**：`verify_cuda` 要用它判「模型是否完整」，
#: 下载器要用它决定下什么。下载器导入本模块，本模块不导入下载器 —— 单向依赖。
MODEL_FILES: tuple[str, ...] = (
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "vocabulary.json",
    "model.bin",
)

#: `verify_cuda` 的失败原因码（协议 `blocked_reason` 的取值）。
#:
#: 前两个是**「没验成」**（`ok=None`），后三个是**「验了，不能用」**（`ok=False`）。
#: 两者的区别就是 2026-10-24 那次「GPU 不可用」误报的根源：模型没装被显示成
#: GPU 坏了，用户据此去查驱动，方向完全错。
REASON_MODEL_MISSING = "model_missing"
REASON_MODEL_INCOMPLETE = "model_incomplete"
REASON_IMPORT_FAILED = "import_failed"
REASON_CUDA_LOAD_FAILED = "cuda_load_failed"
REASON_CUDA_INFERENCE_FAILED = "cuda_inference_failed"


def missing_model_files(model_dir: str | Path) -> list[str]:
    """返回模型目录里**缺**的必需文件。目录不存在时返回全部。"""
    d = Path(model_dir)
    if not d.is_dir():
        return list(MODEL_FILES)
    return [name for name in MODEL_FILES if not (d / name).is_file()]


@dataclass
class CpuInfo:
    name: str | None = None
    physical: int | None = None
    logical: int | None = None
    mhz: int | None = None


@dataclass
class MemoryInfo:
    total_gb: float | None = None
    avail_gb: float | None = None
    load_pct: int | None = None


@dataclass
class DiskInfo:
    total_gb: float | None = None
    free_gb: float | None = None


@dataclass
class GpuInfo:
    name: str
    vram_total_mb: int
    vram_used_mb: int
    driver: str
    compute_cap: str


@dataclass
class CudaInfo:
    """CUDA 可用性。

    `usable` 的三态语义：None = 未验证，True/False = 已由真实推理验证。
    任何基于 `device_count` 的结论都是不可信的。
    """

    ctranslate2: str | None = None
    device_count: int | None = None
    usable: bool | None = None
    detail: str | None = None
    verified: bool = False

    @property
    def needs_verification(self) -> bool:
        return not self.verified


@dataclass
class EnvReport:
    cpu: CpuInfo = field(default_factory=CpuInfo)
    memory: MemoryInfo = field(default_factory=MemoryInfo)
    disk: dict[str, DiskInfo] = field(default_factory=dict)
    gpu: list[GpuInfo] = field(default_factory=list)
    cuda: CudaInfo = field(default_factory=CudaInfo)
    python: str = ""
    executable: str = ""
    probed_at: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["cuda"]["needs_verification"] = self.cuda.needs_verification
        return data


# ---------------------------------------------------------------- 单机探测


def cpu_info() -> CpuInfo:
    info = CpuInfo(logical=os.cpu_count() or None)
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        )
        try:
            info.name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
        for reg_name, attr in (("NumberOfCores", "physical"), ("~MHz", "mhz")):
            try:
                setattr(info, attr, winreg.QueryValueEx(key, reg_name)[0])
            except OSError:
                pass
        winreg.CloseKey(key)
    except Exception:  # noqa: BLE001 — 非 Windows 或注册表不可读，保留 logical 即可
        pass
    return info


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def memory_info() -> MemoryInfo:
    """可用内存是**本机第一约束**（实测在 12.6~26.2 GB 间波动，见实测记录 §1），

    因此每次启动都要重新读，不允许缓存后复用。
    """
    if os.name != "nt":
        # 本项目为 Windows 专用（CUDA DLL 注入与 WinGet 目录布局都依赖 Windows）。
        # 其他平台不做内存探测，返回空值由调用方按保守默认处理。
        return MemoryInfo()
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return MemoryInfo()
        return MemoryInfo(
            total_gb=round(stat.ullTotalPhys / 1024**3, 1),
            avail_gb=round(stat.ullAvailPhys / 1024**3, 1),
            load_pct=int(stat.dwMemoryLoad),
        )
    except Exception:  # noqa: BLE001
        return MemoryInfo()


def disk_info(extra_paths: Iterable[str | Path] = ()) -> dict[str, DiskInfo]:
    """探测各盘符余量。默认覆盖 C/D，并额外覆盖传入路径（如 work_dir）所在盘。"""
    drives = {"C:", "D:"}
    for raw in extra_paths:
        if not raw:
            continue
        drive = Path(raw).drive
        if drive:
            drives.add(drive)

    out: dict[str, DiskInfo] = {}
    for drive in sorted(drives):
        try:
            usage = shutil.disk_usage(f"{drive}\\")
        except OSError:
            continue
        out[drive] = DiskInfo(
            total_gb=round(usage.total / 1024**3, 1),
            free_gb=round(usage.free / 1024**3, 1),
        )
    return out


def gpu_info() -> list[GpuInfo]:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,driver_version,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_NVIDIA_SMI_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 — 无 nvidia-smi / 超时，按无 GPU 处理
        return []

    if proc.returncode != 0:
        return []

    gpus: list[GpuInfo] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append(
                GpuInfo(
                    name=parts[0],
                    vram_total_mb=int(float(parts[1])),
                    vram_used_mb=int(float(parts[2])),
                    driver=parts[3],
                    compute_cap=parts[4],
                )
            )
        except ValueError:
            continue
    return gpus


def cuda_info() -> CudaInfo:
    """只做快速探测：版本号 + 硬件计数。`usable` 留为 None 待真实验证。"""
    import app.core.cuda_dll  # noqa: F401 — 副作用：先注入 DLL 目录

    try:
        import ctranslate2
    except Exception as exc:  # noqa: BLE001
        return CudaInfo(detail=f"导入 ctranslate2 失败：{type(exc).__name__}: {exc}")

    info = CudaInfo(ctranslate2=getattr(ctranslate2, "__version__", None))
    try:
        info.device_count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001
        info.detail = f"get_cuda_device_count 失败：{type(exc).__name__}: {exc}"
    return info


# ---------------------------------------------------------------- 汇总入口


def probe(work_dir: str | Path | None = None) -> EnvReport:
    """快速探测（不加载模型，秒级返回）。

    要确认 GPU 真能用，必须再调 `verify_cuda()`——`probe()` 不做这件事，
    因为模型加载要数秒，不该塞进每次启动的自检里。
    """
    return EnvReport(
        cpu=cpu_info(),
        memory=memory_info(),
        disk=disk_info([work_dir] if work_dir else ()),
        gpu=gpu_info(),
        cuda=cuda_info(),
        python=sys.version.split()[0],
        executable=sys.executable,
        probed_at=datetime.now().isoformat(timespec="seconds"),
    )


def warmup_native_deps() -> tuple[bool, str]:
    """在**主线程**把原生依赖 import 掉，返回 `(是否齐全, 说明)`。

    ⚠️ **必须在主线程调用。** 实测（2026-10-04，PyInstaller 产物）：

    | 场景 | 结果 |
    |---|---|
    | daemon 线程里首次 `import numpy` | **静默卡死 > 2 分钟**（无异常、无 traceback，只有 faulthandler 能看出栈停在 `_multiarray_umath` 的加载） |
    | 主线程里 import 全部 6 个依赖 | **2.03 s**（numpy 0.30 / onnxruntime 0.21 / av 1.46 / 其余 ≈0） |

    为什么这不是「CUDA 验证专属」的小毛病：`dispatcher` 的阶段函数
    （下载 / 转写 / 抽帧）跑在**工作线程**里，首次 `import faster_whisper`
    会踩同一个坑 —— 表现就是「点了开始，任务永远停在 0%」。
    所以本函数**无条件**执行，与 `runtime.device` 配置无关。

    失败时**不抛异常**：让 `verify_cuda` / pipeline 自己给出更准确的错误信息
    （它们能区分「缺模型」和「缺 DLL」，这里分不出来）。
    """
    t0 = time.monotonic()
    try:
        import app.core.cuda_dll  # noqa: F401 — 必须排在 numpy / faster_whisper 之前
        import numpy  # noqa: F401
        import ctranslate2  # noqa: F401
        import onnxruntime  # noqa: F401
        import av  # noqa: F401
        from faster_whisper import WhisperModel  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    logging.info("原生依赖预热完成（%.2fs）", time.monotonic() - t0)
    return True, ""


def verify_cuda(
    model_path: str | Path,
    device: str = "cuda",
    compute_type: str = "float16",
) -> tuple[bool | None, str, str]:
    """跑一次**真实推理**确认 CUDA 可用，返回 `(是否可用, 说明, 原因码)`。

    ⚠️ **`ok` 是三态，`None` 不是「否」**：

    | 情形 | `ok` | `reason` |
    |---|---|---|
    | 模型目录不存在 | `None` | `model_missing` |
    | 模型目录在但缺文件 | `None` | `model_incomplete` |
    | 原生依赖导入失败 | `False` | `import_failed` |
    | 模型加载失败（CUDA 运行库？） | `False` | `cuda_load_failed` |
    | 能加载但推理失败 | `False` | `cuda_inference_failed` |
    | 通过 | `True` | `""` |

    为什么必须分开：2026-10-24 安装版首启，模型没装（`ok=False` + 一句「模型不存在」）
    被上层读成「GPU 不可用」，界面红字写「吞吐下降约 6 倍」——**结论是错的**，
    用户按这个提示去查驱动永远修不好。`None` = 没验成，`False` = 验过确实不行。

    为什么不能用 `get_cuda_device_count()`：它只枚举硬件，即使 cuBLAS/cuDNN
    缺失也会返回 1。唯一可信的判据是「模型能加载 + 能出结果」。

    ⚠️ **分步计时不是调试残留，是必需的**：本函数是整个进程里**唯一**会加载
    CUDA 运行库的地方，也是最容易「静默卡住」的地方（原生 LoadLibrary 卡住时
    既没有异常也没有 traceback）。实测 2026-10-04：打包产物里 `_verify_env`
    线程静默卡了 5 分钟，stderr 与 `log` 事件**一个字都没有**，完全无法定位。
    这几条 `_tick` 就是为了让下一次卡住时**一眼看出卡在哪一步**。
    """
    import app.core.cuda_dll  # noqa: F401 — 必须在 faster_whisper 之前

    model_dir = Path(model_path)
    if not model_dir.is_dir():
        return (
            None,
            f"模型目录不存在：{model_dir}（未验证，不是 GPU 不可用）",
            REASON_MODEL_MISSING,
        )
    missing = missing_model_files(model_dir)
    if missing:
        return (
            None,
            f"模型文件不完整：缺 {', '.join(missing)}（未验证，不是 GPU 不可用）",
            REASON_MODEL_INCOMPLETE,
        )

    t0 = time.monotonic()

    def _tick(what: str) -> None:
        logging.info("CUDA 验证：%s（累计 %.1fs）", what, time.monotonic() - t0)

    _tick("nvidia DLL 目录已注入")

    # 三个 import 分开打点：打包产物里实测卡在这一段（2026-10-04），
    # 合成一步时只能知道「卡在 import」，拆开后能直接指出是哪一个。
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        return False, f"导入 numpy 失败：{type(exc).__name__}: {exc}", REASON_IMPORT_FAILED
    _tick("numpy 导入完成")

    try:
        import ctranslate2  # noqa: F401 — 显式导入，把它的耗时与 faster_whisper 的区分开
    except Exception as exc:  # noqa: BLE001
        return False, f"导入 ctranslate2 失败：{type(exc).__name__}: {exc}", REASON_IMPORT_FAILED
    _tick("ctranslate2 导入完成")

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            f"导入 faster_whisper 失败：{type(exc).__name__}: {exc}",
            REASON_IMPORT_FAILED,
        )
    _tick("faster_whisper 导入完成")

    try:
        model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)
    except Exception as exc:  # noqa: BLE001
        return (
            False,
            f"模型加载失败（CUDA 运行库不可用？）：{type(exc).__name__}: {exc}",
            REASON_CUDA_LOAD_FAILED,
        )
    _tick("模型加载完成")

    try:
        silence = np.zeros(16000, dtype=np.float32)  # 1 秒静音，够触发一次前向
        segments, info = model.transcribe(silence, language="zh", beam_size=1)
        list(segments)  # 生成器必须被消费，否则推理根本没执行
    except Exception as exc:  # noqa: BLE001
        return False, f"推理失败：{type(exc).__name__}: {exc}", REASON_CUDA_INFERENCE_FAILED
    _tick("推理完成")

    return True, f"真实推理通过（输入 {info.duration:.1f}s，device={device}）", ""


def dumps(report: EnvReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
