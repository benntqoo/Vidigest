# -*- mode: python ; coding: utf-8 -*-
"""Vidigest Python worker —— PyInstaller 打包配置（**单目录 / onedir**）。

**不要直接跑 `pyinstaller packaging/vidigest-worker.spec`**，用：

    <受管 venv>/Scripts/python.exe tools/build_worker.py

构建脚本会先做前置校验（解释器、依赖版本、CUDA DLL 是否齐）再调用本 spec ——
PyInstaller 自己是「打包成功 ≠ 产物可用」，前置校验才是有用的那部分。

产物：`<repo>/build/pyinstaller/vidigest-worker/vidigest-worker.exe`

链路：
    本 spec → `<repo>/build/pyinstaller/vidigest-worker/`
    → `tauri.conf.json` 的 `bundle.resources`（映射到安装目录的 `worker/`）
    → `app-ui/src-tauri/src/worker/locator.rs::locate_bundled()` 定位

⚠️ **单目录而不是单文件**（`--onefile`）：单文件每次启动把 ~2 GB 解压到 `%TEMP%`，
启动要几十秒，且 CUDA DLL 从临时目录加载的行为没人验证过。理由见 `PLAN.md §8 阶段 5`。
"""

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

#: `packaging/` 的上一级即仓库根。PyInstaller 注入 `SPECPATH` = spec 所在目录。
REPO = Path(SPECPATH).resolve().parent

# ---------------------------------------------------------------- 显式排除
# 不排除的话 PyInstaller 会顺着 `sys.path` 把装了但与本项目无关的重型包一起收走
# （本机装了 torch 2.10，误收会让产物从 ~2 GB 涨到 5 GB+）。
EXCLUDES = [
    "torch",
    "torchvision",
    "torchaudio",
    "transformers",
    "matplotlib",
    "scipy",
    "pandas",
    "sympy",
    "IPython",
    "ipykernel",
    "jupyter",
    "notebook",
    "nbconvert",
    "pytest",
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "setuptools",
    "pip",
    "wheel",
]

binaries = []
datas = []
hiddenimports = []

# ---------------------------------------------------------- 第三方原生依赖
# `collect_all` = binaries + datas + hiddenimports 一次拿全。
# 这些都是「有原生扩展或数据文件」的包，PyInstaller 的默认分析拿不全：
#   ctranslate2   → ctranslate2.dll（推理引擎本体，含 CUDA kernels）
#   faster_whisper→ assets/silero_vad.onnx 等数据文件
#   av            → PyAV 自带的 ffmpeg 动态库（**不是**系统那个 ffmpeg.exe）
#   onnxruntime   → silero VAD 的执行后端（dll + 内置模型元数据）
#   tokenizers    → 原生 .pyd
#   playwright     → **必须 collect_all**：它不是纯 python 包，`driver/` 下有
#                    node.exe 和一堆 js（playwright 用 node 驱动浏览器）。
#                    只靠 import 分析会漏掉 driver，产物里 `playwright install`
#                    之外的一切都会失败，报错还只是一句 "Executable doesn't exist"。
for pkg in ("ctranslate2", "faster_whisper", "av", "onnxruntime", "tokenizers", "playwright"):
    b, d, h = collect_all(pkg)
    binaries += b
    datas += d
    hiddenimports += h

# ----------------------------------------------------------------- CUDA DLL
# `nvidia` 是**命名空间包**（没有 `__init__.py`），`collect_all` 对它无效，
# 必须手动收。用 `find_spec` 而不是 `import nvidia`：spec 的执行环境里
# 已 import 的模块会干扰 `__path__`，而 `submodule_search_locations` 是权威值。
#
# ⚠️ 体积：本机 `nvidia/` 共 **2.0 GB**（cudnn 1.1 G / cublas 737 M / nvrtc 179 M）。
#    目前**全收**，不做子集瘦身 —— 哪些 cudnn 子 DLL 是 ctranslate2 必需的
#    没有实测依据，凭名字猜会做出「能打包、一跑就崩」的包。
#    瘦身的前提是「删了以后真实转写仍然 GPU 可用」，见 docs/打包与分发.md。
nvidia_spec = importlib.util.find_spec("nvidia")
for base in list(getattr(nvidia_spec, "submodule_search_locations", None) or []):
    base_dir = Path(base)
    if not base_dir.is_dir():
        continue
    for pkg_dir in sorted(base_dir.iterdir()):
        bin_dir = pkg_dir / "bin"
        if not bin_dir.is_dir():
            continue
        for dll in sorted(bin_dir.glob("*.dll")):
            # 目标路径与源码 `cuda_dll.py` 的查找路径一致：
            #   sys._MEIPASS/nvidia/<pkg>/bin  （onedir 下 _MEIPASS = <exe>/_internal）
            binaries.append((str(dll), f"nvidia/{pkg_dir.name}/bin"))

# 入口脚本以 `__main__` 运行，但 `app` 是包内绝对导入（`from app.core import ...`），
# 故 `pathex` 必须给仓库根，否则 Analysis 找不到 `app` 包。
a = Analysis(
    [str(REPO / "app" / "rpc.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="vidigest-worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX：**必须关**。它会改写 CUDA DLL 的节区（cudnn 明确不支持被 UPX 压过，
    # 加载即报校验失败），且会让 Windows Defender 对产物误报。
    upx=False,
    console=True,  # stdout 是协议通道；Rust 侧用 CREATE_NO_WINDOW 隐藏黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="vidigest-worker",
)

# 让 spec 自带一条「我完成了」的可观测输出：构建日志的末尾能直接看到产物大小。
# 静默成功的构建脚本最难排查（尤其是它其实少了 2 GB 的时候）。
_total = sum(f.stat().st_size for f in Path(coll.name).rglob("*") if f.is_file()) if Path(
    coll.name
).is_dir() else 0
print(f"[spec] vidigest-worker 收集完成，共 {_total / 1024 / 1024:.0f} MiB（Python {sys.version.split()[0]}）")
