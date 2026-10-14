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
