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
| `program_dir()` | `Path.cwd()` | worker exe 所在目录，即 `<安装目录>/worker` |
| `install_dir()` | `None` | `<安装目录>`（有 `vidigest.exe` 的那一层） |
| `app_data_dir()` | `Path.cwd()` | `%LOCALAPPDATA%/Vidigest` |

`app_root()` 是**唯一**用于锚定 `config.yaml` / `work_dir` / `model_dir` 的锚点。
工作目录与模型目录的**默认值**另走 `pick_default_work_dir()` / `pick_default_model_dir()`：
优先 `<安装目录>/workspace`、`<安装目录>/models`，安装目录不可写时回退非系统盘。
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

#: 工作目录 / 模型目录的**目录名**（与安装目录、非系统盘、数据目录拼接时都用它）。
#:
#: 2026-10-26 从 `work` 改名 `workspace`：`work` 太泛，用户看到 `D:\Vidigest\work`
#: 不知道是什么的 work。配置字段名 `paths.work_dir` **保持不变** —— 那是协议与
#: 配置文件里的契约，改它没有用户可见收益，只会多一处漂移风险。
WORK_DIR_NAME = "workspace"
MODELS_DIR_NAME = "models"

#: 显式指定 `config.yaml` 路径的环境变量（Rust 宿主也用它）。
CONFIG_ENV_VAR = "VIDIGEST_CONFIG"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 产物里。

    用 `sys.frozen` 而不是 `sys._MEIPASS`：后者在部分运行时钩子下才有，
    且 `frozen` 是 PyInstaller 官方约定。
    """
    return bool(getattr(sys, "frozen", False))


def program_dir() -> Path:
    """**Python 进程自身**所在目录。打包后是 `<安装目录>\\worker`（worker 的 exe 在这）。

    注意：这**不是**安装目录。worker 被放在安装目录下的 `worker/` 子目录里，
    所以「装到哪」要用 `install_dir()`。历史注释把这里说成「安装目录」，
    2026-10-26 纠正 —— 按旧理解拼出的 `<program_dir>/workspace` 会落到
    `<安装目录>\\worker\\workspace`。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def install_dir() -> Path | None:
    """应用的安装目录（`vidigest.exe` 所在位置）；源码运行返回 `None`。

    worker 在 `<安装目录>\\worker\\` 下，所以用 `program_dir()` 的**父目录**。
    判断依据是父目录里有没有主程序 —— 不用「名字是不是 worker」这种字符串规则：
    开发时把 worker 单独放到别处也不会误判。
    """
    if not is_frozen():
        return None
    here = program_dir()
    parent = here.parent
    if (parent / "vidigest.exe").is_file():
        return parent
    return here


def probe_dir_writable(path: Path) -> str | None:
    """探测目录是否可写：能建目录 + 能建删一个临时文件。

    返回 `None` 表示可写，否则返回可直接展示给用户的原因。

    **为什么要建目录**：用户常把 `model_dir` 指到一个还不存在的目录（例如
    `D:\\Vidigest\\models`），那是合法意图。这里顺手建出来，而不是回一句
    「不存在」把人挡住 —— 但建不出来时（不存在的盘、只读、网络盘掉线）
    必须**如实拒绝**，否则保存成功、下载时才炸，用户不知道是哪一步错了。

    首启挑默认目录也用它：装到 `%ProgramFiles%` 的普通用户那里是只读的，
    探测失败就回退到非系统盘 / 数据目录。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".vidigest-write-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


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


def _non_system_drives(system_drive: str | None = None) -> list[Path]:
    """按**剩余空间从大到小**返回可用的非系统盘根目录（`Path("D:\\\\")` 这种）。

    `system_drive` 只用于测试注入（例如 `"D:"` 模拟「D 才是系统盘」）。
    """
    system = (system_drive or os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()
    found: list[tuple[int, Path]] = []
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
            found.append((free, root))
    found.sort(key=lambda item: item[0], reverse=True)
    return [root for _, root in found]


def _model_complete(model_dir: Path) -> bool:
    """模型目录是否**完整**（5 个必需文件全在）。

    文件清单直接从 `env_probe.MODEL_FILES` 取，不在这里再抄一份 —— 两份清单
    迟早会有一份忘记更新，而那种偏差的表现是「明明下载完了却认不出来」。
    延迟导入是因为 `env_probe` 依赖较重（ctypes / ctypes.util），不该被
    `paths.py` 的每个使用者都拖进来。
    """
    from app.core.env_probe import MODEL_FILES  # 延迟导入：避免拖重 paths 的导入链

    return model_dir.is_dir() and all((model_dir / name).is_file() for name in MODEL_FILES)


def find_existing_model_dir(
    model: str, roots: list[Path] | None = None
) -> Path | None:
    """找一个**已经下好模型**的 `model_dir`（返回的是 `models` 这一层，不是 `models/<model>`）。

    为什么需要：卸载会删掉 `%LOCALAPPDATA%\\Vidigest`（含 config.yaml）；如果用户把
    模型放在别处，重装后 `model_dir` 回到默认值就得以 2.9 GiB 的代价重下，
    或者手动把路径再指一遍。

    候选顺序（先找已有的，再谈默认）：
    1. `%LOCALAPPDATA%\\Vidigest\\models`（老默认位置，用户可能一直没挪过）
    2. `<安装目录>\\models`（2026-10-26 起的新默认位置）
    3. 各非系统盘上的 `<盘>:\\Vidigest\\models`

    判据是 `env_probe.MODEL_FILES` **5 个文件全在** —— 下了一半的目录不算，
    否则会把一个缺 `model.bin` 的目录当成可用，症状是「装完就说 GPU 不可用」。

    `roots` 里每一项都是**「一个 app 目录」**（即 `models` 的父目录），
    不是磁盘根 —— 非系统盘要先拼上 `Vidigest` 才是 app 目录。
    """
    if roots is None:
        bases = [app_data_dir()]
        inst = install_dir()
        if inst is not None:
            bases.append(inst)
        bases.extend(drive / APP_DIR_NAME for drive in _non_system_drives())
    else:
        bases = list(roots)
    for base in bases:
        candidate = base / MODELS_DIR_NAME
        if _model_complete(candidate / model):
            return candidate
    return None


def pick_default_model_dir(model: str, roots: list[Path] | None = None) -> Path:
    """首启的 `model_dir`：**已有模型就沿用**，否则跟着工作目录放到安装目录下。

    2026-10-26 改：从「非系统盘」改成「优先安装目录」—— 用户的要求是
    「一个地方放全部，别散落」。安装目录不可写时（装到 `%ProgramFiles%` 的
    标准用户）仍按老规则回退到非系统盘 / 数据目录。
    """
    if roots is None and not is_frozen():
        # 源码运行与改造前逐字一致：相对路径、不扫盘（阶段 1/2 的判据依赖这一点）。
        return Path(MODELS_DIR_NAME)
    existing = find_existing_model_dir(model, roots)
    if existing is not None:
        return existing
    inst = install_dir()
    if inst is not None and probe_dir_writable(inst / MODELS_DIR_NAME) is None:
        return inst / MODELS_DIR_NAME
    drives = _non_system_drives()
    if drives:
        return drives[0] / APP_DIR_NAME / MODELS_DIR_NAME
    return app_data_dir() / MODELS_DIR_NAME


def pick_default_work_dir(system_drive: str | None = None) -> Path:
    """首启的工作目录：**优先安装目录下的 `workspace/`**（2026-10-26 起）。

    为什么改成优先安装目录：用户的要求是「一个地方放全部，别散落」。装在
    可写位置（例如 `D:////Tool////Vidigest`）时，`<安装目录>\\workspace` 最好找。

    ⚠️ 但**不能无条件用**：默认安装到 `%ProgramFiles%\\Vidigest` 时，标准用户
    对那里没有写权限，直接写会失败（或被 UAC 虚拟化悄悄重定向）。所以先做
    可写探测，失败就按老规则回退：

    1. `<安装目录>\\workspace`（可写时）
    2. 剩余空间最大的非系统盘 → `<盘>:\\Vidigest\\workspace`（不吃 C 盘）
    3. `%LOCALAPPDATA%\\Vidigest\\workspace`（只有 C 盘可用的机器）

    返回值是**建议值**，最终以用户确认结果为准（`paths.work_dir_confirmed`）。

    `system_drive` 只用于测试注入（例如 `"D:"` 模拟「D 是系统盘」）；
    「有没有安装目录」由 `install_dir()` 决定，测试里直接替换那个函数。
    """
    if not is_frozen():
        # 源码运行沿用现状：相对 root，阶段 1/2 的判据不受影响。
        return Path(WORK_DIR_NAME)

    inst = install_dir()
    if inst is not None and probe_dir_writable(inst / WORK_DIR_NAME) is None:
        return inst / WORK_DIR_NAME

    drives = _non_system_drives(system_drive)
    if drives:
        return drives[0] / APP_DIR_NAME / WORK_DIR_NAME
    return app_data_dir() / WORK_DIR_NAME


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
        # 首启：工作目录与模型目录都预置到**安装目录**下（可写时；不可写自动回退）。
        # - work_dir 只是建议值，还要用户确认（`work_dir_confirmed=False`）；
        # - model_dir 若在候选位置找到**完整**模型就直接沿用。
        cfg.paths.work_dir = str(pick_default_work_dir())
        cfg.paths.model_dir = str(pick_default_model_dir(cfg.runtime.model))
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
