"""Windows 下把 pip 安装的 NVIDIA 运行库 DLL 暴露给 ctranslate2。

⚠️ **必须在 `import ctranslate2` / `import faster_whisper` 之前导入本模块。**

三层要求缺一不可（PLAN.md §9.1 / docs/实测记录.md §6，实测踩坑约 1 小时）：

1. `os.add_dll_directory()` 返回的句柄**必须被引用住**。Python 文档明确：句柄被
   GC 之后该目录会从搜索路径中移除——只调用不保存返回值等于没调用。
2. 还要注入 `os.environ["PATH"]`。`add_dll_directory` 只对
   `LoadLibraryEx(..., LOAD_LIBRARY_SEARCH_USER_DIRS)` 生效，而 ctranslate2 用的是
   裸名 `LoadLibrary("cublas64_12.dll")`，解析走**进程 PATH**。
3. 时机必须在原生库加载之前——`add_dll_directory` 只影响其后的 DLL 解析。

另注：`ctranslate2.get_cuda_device_count() == 1` **不代表 GPU 可用**，只代表检测到
硬件。真正的验证见 `env_probe.verify_cuda()`——必须跑一次真实推理。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 句柄保活容器。模块级引用，进程存活期间不会被回收。
_DLL_DIR_HANDLES: list = []


def _nvidia_search_bases() -> list[Path]:
    """返回可能包含 `<pkg>/bin` 的父目录候选，按可信度排序。

    两个来源，**必须都给**：

    1. `sys._MEIPASS/nvidia` —— PyInstaller 打包后 DLL 的位置。
       spec 里把 `nvidia/**/bin/*.dll` 收在 `nvidia/<pkg>/bin`，
       而 onedir 下 `sys._MEIPASS` = `<exe 目录>/_internal`。
       **不能只靠 `import nvidia`**：命名空间包在 frozen 下的 `__path__`
       由 PyInstaller 的 FrozenImporter 决定，行为不如显式路径可靠。
    2. `nvidia.__path__` —— 正常 pip 安装（`site-packages/nvidia/<pkg>/bin`）。
       注意 `nvidia` 是命名空间包：没有 `__file__`，只有 `__path__`。

    任何一个不存在都只是少一条候选，不影响另一个。
    """
    bases: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = Path(meipass) / "nvidia"
        if cand.is_dir():
            bases.append(cand)

    try:
        import nvidia
    except ImportError:
        return bases

    for p in getattr(nvidia, "__path__", None) or []:
        cand = Path(p)
        if cand.is_dir() and cand not in bases:
            bases.append(cand)

    return bases


def add_nvidia_dll_dirs() -> list[str]:
    """把 `nvidia/<pkg>/bin` 加入 DLL 搜索路径，返回加入的目录列表。

    非 Windows 或未安装 nvidia-* 包（也未被 PyInstaller 收进包）时返回空列表（不抛异常）。
    """
    added: list[str] = []
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return added

    seen: set[str] = set()
    for base in _nvidia_search_bases():
        for pkg in sorted(base.iterdir()):
            bin_dir = pkg / "bin"
            if not bin_dir.is_dir():
                continue
            key = str(bin_dir)
            # 两个 base 可能指向同一处（源码运行时 _MEIPASS 不存在，
            # 但重复加入会让 PATH 里出现重复项并让 `added` 失真）
            if key in seen:
                continue
            seen.add(key)
            try:
                handle = os.add_dll_directory(key)
            except OSError:
                # 目录已加入或权限不足，跳过而不是让整个流程挂掉
                continue
            _DLL_DIR_HANDLES.append(handle)  # ← 不能省：句柄被 GC 则目录失效
            added.append(key)

    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")

    return added


#: 导入本模块即完成注入（副作用即目的）。
NVIDIA_DLL_DIRS: list[str] = add_nvidia_dll_dirs()
