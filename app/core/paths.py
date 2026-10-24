"""运行时路径解析：区分「源码运行」与「打包运行（PyInstaller）」。

**为什么需要这个模块**：打包后 `sys.executable` 是 worker 自己的 exe，`Path.cwd()`
落在安装目录 —— Windows 上通常是 `C:\\Program Files\\...`，**不可写**。
而 `config.yaml` / `work/` / `models/` 原先一律锚定「项目根 = cwd」，
于是打包后第一次落盘就失败，报错形态还是「建不出 `work/<dir>`」这种绕远的样子，
很难一眼看出是路径锚点错了。

三种路径语义（**非 frozen 时全部等于现状**，阶段 1/2 的判据不受影响）：

| 函数 | 源码运行 | 打包运行 |
|---|---|---|
| `app_root()` | `Path.cwd()`（= 仓库根） | `app_data_dir()` |
| `program_dir()` | `Path.cwd()` | exe 所在目录（**只读**） |
| `app_data_dir()` | `Path.cwd()` | `%LOCALAPPDATA%/Vidigest` |

`app_root()` 是**唯一**用于锚定 `config.yaml` / `work_dir` / `model_dir` 的锚点。
判据见 `docs/打包与分发.md`。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

from app.models.config import DEFAULT_CONFIG_NAME

#: 打包运行时的数据目录名（放在 `%LOCALAPPDATA%` 下）。
#: 与 Tauri 的 `productName` 无关 —— 改名只影响用户已有的数据目录位置。
APP_DIR_NAME = "Vidigest"

#: 显式指定 `config.yaml` 路径的环境变量（Rust 宿主也用它）。
CONFIG_ENV_VAR = "VIDIGEST_CONFIG"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 产物里。

    用 `sys.frozen` 而不是 `sys._MEIPASS`：后者在部分运行时钩子下才有，
    且 `frozen` 是 PyInstaller 官方约定。
    """
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """程序自身所在目录。打包后是只读的安装目录，**不要往这里写数据**。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def app_data_dir() -> Path:
    """可写的用户数据目录。非 frozen 时就是 cwd（与改造前逐字一致）。"""
    if not is_frozen():
        return Path.cwd()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / APP_DIR_NAME


def app_root() -> Path:
    """相对路径（`config.yaml` / `work_dir` / `model_dir`）的锚点。"""
    return app_data_dir()


def pick_default_work_dir(system_drive: str | None = None) -> Path:
    """首启时挑一个放工作文件的地方：优先**非系统盘**，取剩余空间最大的那块。

    为什么不放安装目录（`%ProgramFiles%\\Vidigest\\work`）：
    MSI 是 `perMachine`，Program Files 对普通用户**只读**，往那里写 mp4/wav
    会直接失败（或触发 UAC 虚拟化把文件悄悄重定向到别处）；而且卸载/升级会清
    安装目录 —— 几十 GB 产出会跟着一起没。`program_dir()` 的说明里已经写明
    「这里只读，不要写数据」。

    为什么不固定 `%LOCALAPPDATA%\\Vidigest\\work`：`work/` 里是 mp4 / wav / jpg，
    动辄几十 GB，默认落系统盘会把 C 盘吃满（PLAN.md §2 的第一约束就是这件事）。

    返回值是**建议值**，最终以用户确认结果为准（`paths.work_dir_confirmed`）。

    `system_drive` 只用于测试注入（例如 `"D:"` 模拟「D 是系统盘」）。
    """
    if not is_frozen():
        # 源码运行沿用现状：相对 root，阶段 1/2 的判据不受影响。
        return Path("work")

    system = (system_drive or os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()
    candidates: list[tuple[int, Path]] = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:"
        root = Path(drive + "\\")
        if drive == system or not root.exists():
            continue
        try:
            free = shutil.disk_usage(str(root)).free
        except OSError:  # noqa: PERF203 — 光驱 / 断开的网络盘
            continue
        if free > 0:
            candidates.append((free, root / APP_DIR_NAME / "work"))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    # 只有 C 盘可用：回退到数据目录（用户仍可在首启引导里改）。
    return app_data_dir() / "work"


def default_config_path() -> Path:
    """`config.yaml` 的默认位置。"""
    return app_root() / DEFAULT_CONFIG_NAME


def ensure_default_config(path: Path | None = None) -> tuple[Path, bool]:
    """确保存在一份可编辑的 `config.yaml`，返回 `(路径, 是否新建)`。

    只应在**打包运行**时调用。源码运行时 `config.yaml` 是用户手动 `cp` 出来的，
    缺失时的报错与提示由 `Config.load_or_default` + `cli.py` 负责 ——
    **那个行为不能改**（`cli.py` 的错误提示依赖它）。
    """
    from app.models.config import Config  # 延迟导入：避免模块级循环

    p = path or default_config_path()
    if p.is_file():
        return p, False
    cfg = Config.from_dict({}, root=p.parent)
    if is_frozen():
        # 首启：把工作目录预置到非系统盘，并标记「用户还没确认过」，
        # 让界面弹一次性引导。见 `pick_default_work_dir()` 的理由。
        cfg.paths.work_dir = str(pick_default_work_dir())
        cfg.paths.work_dir_confirmed = False
    cfg.save(p)
    return p, True


def resolve_config_path() -> str | None:
    """决定 `config.yaml` 从哪读。**源码运行与打包运行的差别只在这一个函数里。**

    优先级：`VIDIGEST_CONFIG` 环境变量 → 打包时数据目录下的 `config.yaml` → `None`。

    返回 `None` 表示「交给 `Config.load_or_default` 的原有逻辑」——
    即 cwd 下的 `config.yaml`，**与改造前逐字一致**（阶段 1/2 的判据依赖这一点）。

    打包运行必须走数据目录：此时 `Path.cwd()` 是安装目录，Windows 上通常不可写，
    而 `Config.load_or_default` 的回退分支会把 `work_dir` / `model_dir`
    锚到那个不可写的目录上。
    """
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return env
    if not is_frozen():
        return None
    p, created = ensure_default_config()
    if created:
        logging.info("首次运行，已生成默认配置：%s", p)
    return str(p)
