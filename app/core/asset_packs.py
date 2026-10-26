"""可下载资产清单：**「下什么」与「怎么下」分开**。

## 为什么单独一个模块

`model_downloader.py` 里那 200 行是传输逻辑——`.part` 续传、重试退避、
进度节流、磁盘检查、取消。这些与「下的到底是 whisper 权重还是 GGUF」毫无关系。
2026-10-27 之前它把 `MODEL_FILES` 和 `Systran/faster-whisper-{model}` 写死在里面，
于是新增一种资产（本地视觉模型）只能改那 200 行——而那里每一条都有判据在守。

把清单抽出来后，新增资产 = 加一个 `AssetPack`。传输逻辑一行不动。

## 两种包

| key | 内容 | 体积 | 装到哪 |
|---|---|---|---|
| `whisper` | faster-whisper large-v3 的 5 个文件 | 2.9 GiB | `<模型目录>/large-v3/` |
| `local_ai` | llama-server（2 个 zip）+ Qwen3-VL-2B 权重 + 视觉投影器 | 2.35 GiB 下载 / +0.72 GiB 解压 | `<视觉模型目录>/` 与 `.../llama/` |

两者都是**装完再下**（PRD D1 的另一半：安装包不带模型）。区别只在
`local_ai` 多了 zip 解压——llama.cpp 官方发布的是压缩包，而 `llama-server.exe`
只有 9 KiB，真正的东西在 33 个 DLL 里（`cublasLt64_13.dll` 一个人就 470 MiB）。
所以**必须整包解压，不能只挑 exe**。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.env_probe import MODEL_FILES

#: llama.cpp 的发布版本。**刻意钉死 build 号**：`latest` 会随上游更新而变，
#: 而「实测过的那一版」才是可复现的判据基础（实测记录 §8 用的就是 b11909）。
#: 升级方式：改这个常量 + `LLAMA_VARIANT`，然后跑一次 `tools/probe_vlm.py`。
LLAMA_BUILD = "b11909"

#: CUDA 运行时变体。`cuda-13.4` 要求驱动 >= 580（本机 617.42 实测通过）。
#: 驱动更老的机器可以在设置里手动指定 `local_ai.server_path` 指向自备的
#: CPU 版或 cuda-12 版 llama-server，不影响权重下载。
LLAMA_VARIANT = "cuda-13.4"

LLAMA_RELEASE = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}"

#: 视觉权重来源。与 whisper 一样走镜像——官方 `huggingface.co` 国内不通。
DEFAULT_HF_BASE = "https://hf-mirror.com"

#: Qwen3-VL-2B 在 HF 上的仓库。选 unsloth 的量化版：它是唯一提供
#: 「主权重 + mmproj 同一仓库、同一序列」的，省掉跨仓库版本对不上的风险。
QWEN_VL_REPO = "unsloth/Qwen3-VL-2B-Instruct-GGUF"

#: 主权重。Q4_K_M 是实测档位（1.8 s/张、显存 3.2 GiB）。不要随手换更小的
#: UD-IQ1：中文 OCR 的退化在 2B 这个量级上很陡（实测记录 §8.2）。
QWEN_VL_MODEL = "Qwen3-VL-2B-Instruct-Q4_K_M.gguf"

#: 视觉投影器（clip）。**必需**——没有它模型看不见图，只会按文字瞎猜。
#: F16 与 BF16 体积几乎一样（781 vs 784 MiB），取 BF16（上游默认）。
QWEN_VL_MMPROJ = "mmproj-BF16.gguf"


@dataclass(frozen=True)
class AssetFile:
    """一个待下载的文件。"""

    name: str
    url: str
    #: 预期字节数。**仅用于「还没联网时估算总大小」**——真实值以 HEAD 为准。
    #: 0 表示不知道（例如 whisper 的清单来自固定文件名，大小随模型档位变）。
    approx_size: int = 0
    #: 相对 pack 目标目录的子目录。空 = 直接放目标目录。
    dest_dir: str = ""
    #: True 表示这是 zip，下完解压到 `与 dest_dir 相同的位置` 并删除原包。
    unzip: bool = False


@dataclass(frozen=True)
class AssetPack:
    """一组要一起到位的文件。"""

    key: str
    title: str
    target_dir: Path
    files: tuple[AssetFile, ...]

    @property
    def approx_bytes(self) -> int:
        """估算的下载总量（不含解压后的增量）。0 = 未知。"""
        return sum(f.approx_size for f in self.files)

    def describe(self) -> dict[str, Any]:
        """给 `get_config` / 设置界面用。**不联网。**"""
        return {
            "key": self.key,
            "title": self.title,
            "target_dir": str(self.target_dir),
            "file_count": len(self.files),
            "approx_bytes": self.approx_bytes,
        }


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GiB"


def whisper_pack(cfg: object) -> AssetPack:
    """faster-whisper 权重：5 个文件，平铺在 `<model_dir>/<runtime.model>/`。

    URL 与文件名与 2026-10-24 那版**完全一致**（判据 8 条守着它）：
    清单仍是 `env_probe.MODEL_FILES`，镜像仍可由 `download.base_url` 覆盖。
    """
    runtime = getattr(cfg, "runtime", None)
    paths = getattr(cfg, "paths", None)
    model = getattr(runtime, "model", "large-v3")
    model_dir = Path(getattr(paths, "model_dir", "models"))
    from app.core.model_downloader import DEFAULT_BASE_URL  # 避免循环导入

    base = getattr(getattr(cfg, "download", None), "base_url", None) or DEFAULT_BASE_URL
    base = base.format(model=model)
    return AssetPack(
        key="whisper",
        title=f"语音转写模型（{model}）",
        target_dir=model_dir / model,
        files=tuple(AssetFile(name=n, url=base + n) for n in MODEL_FILES),
    )


def local_ai_pack(cfg: object) -> AssetPack:
    """本地视觉模型：llama-server 运行包 + GGUF 权重 + 视觉投影器。

    落点由 `cfg.vision_model_path()` 决定（默认 `<模型目录>/qwen3-vl-2b`）：

    ```
    <视觉模型目录>/
      llama/                       ← 两个 zip 解压到这里（llama-server.exe + 33 个 DLL）
      Qwen3-VL-2B-Instruct-Q4_K_M.gguf
      mmproj-BF16.gguf
    ```

    目录布局必须与 `ai_backend._find_server()` / `_find_weights()` 的查找规则
    对得上，否则会出现「下完了但仍说找不到」——那种 bug 从日志里看像是下载失败。
    """
    target = cfg.vision_model_path()
    hf = f"{DEFAULT_HF_BASE}/{QWEN_VL_REPO}/resolve/main"
    return AssetPack(
        key="local_ai",
        title="本地视觉模型（Qwen3-VL-2B + llama-server）",
        target_dir=target,
        files=(
            AssetFile(
                name=f"llama-{LLAMA_BUILD}-bin-win-{LLAMA_VARIANT}-x64.zip",
                url=f"{LLAMA_RELEASE}/llama-{LLAMA_BUILD}-bin-win-{LLAMA_VARIANT}-x64.zip",
                approx_size=164_710_108,
                dest_dir="llama",
                unzip=True,
            ),
            AssetFile(
                name=f"cudart-llama-bin-win-{LLAMA_VARIANT}-x64.zip",
                url=f"{LLAMA_RELEASE}/cudart-llama-bin-win-{LLAMA_VARIANT}-x64.zip",
                approx_size=423_535_356,
                dest_dir="llama",
                unzip=True,
            ),
            AssetFile(
                name=QWEN_VL_MODEL,
                url=f"{hf}/{QWEN_VL_MODEL}",
                approx_size=1_107_410_624,
            ),
            AssetFile(
                name=QWEN_VL_MMPROJ,
                url=f"{hf}/{QWEN_VL_MMPROJ}",
                approx_size=822_540_960,
            ),
        ),
    )


#: 可选包。顺序即设置界面的展示顺序。
PACK_KEYS: tuple[str, ...] = ("whisper", "local_ai")

#: `key → 构造函数`。加新包只在这里挂一行。
_BUILDERS = {
    "whisper": whisper_pack,
    "local_ai": local_ai_pack,
}


def build_pack(key: str, cfg: object) -> AssetPack | None:
    """按 key 建包。未知 key 返回 `None`（调用方负责报 `E_BAD_ARGS`）。"""
    builder = _BUILDERS.get(key)
    return builder(cfg) if builder else None


def pack_summary(cfg: object) -> list[dict[str, Any]]:
    """所有包的概要，给设置界面显示「下多少、放哪里」。"""
    out: list[dict[str, Any]] = []
    for key in PACK_KEYS:
        pack = build_pack(key, cfg)
        if pack is not None:
            info = pack.describe()
            info["approx_human"] = _human(pack.approx_bytes) if pack.approx_bytes else ""
            out.append(info)
    return out
