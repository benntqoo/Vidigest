"""构建 Python worker 的 PyInstaller **单目录**产物，并做一次真实冒烟校验。

用法（必须用**装齐依赖的那个解释器**跑）：

    C:/Users/Ben/.workbuddy/binaries/python/envs/default/Scripts/python.exe tools/build_worker.py
    ... tools/build_worker.py --check-only    # 只做前置校验，不打包
    ... tools/build_worker.py --skip-smoke    # 打包但不冒烟
    ... tools/build_worker.py --timeout-verify 600

产物：`build/pyinstaller/vidigest-worker/vidigest-worker.exe`

**为什么需要脚本而不是直接跑 pyinstaller**：
PyInstaller 的「成功」只代表**收集流程**没报错。它不会告诉你
① CUDA DLL 有没有被收进去（`nvidia` 是命名空间包，`collect_all` 对它无效）；
② 依赖是从哪个 site-packages 来的（本机依赖分散在两处，见下）；
③ 产物到底还起不起得来。
这三件事才是「能不能用」的判据，所以放在这里，`--check-only` 单独也能跑。

**本机依赖分散在两个 site-packages**：
  venv      的 `Lib/site-packages`          : ctranslate2 / faster_whisper / av / tokenizers / nvidia
  user-site `%APPDATA%/Python/Python313/...` : numpy / onnxruntime / PIL / yaml / huggingface_hub / tqdm
这不是缺陷（PyInstaller 跟着 `sys.path` 走，两边都会收），但换台机器时
这里会最先暴露差异 —— 所以校验会把每个包的实际路径打出来。

**冒烟为什么用匿名管道**：
本工具的沙箱拦截的是**命名管道客户端读方向**的打开（`os error 231`），
而 Rust 的 `Stdio::piped()` 恰好走那条路 —— 这就是 `app-ui` 在沙箱内起不来的原因。
Python `subprocess.PIPE` 走 `CreatePipe`（匿名管道），**2026-10-04 实测不受影响**，
所以这个脚本在沙箱内外都能跑出可信结论。
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "packaging" / "vidigest-worker.spec"
DIST = REPO / "build" / "pyinstaller"
WORKDIR = REPO / "build" / "pyinstaller-work"
#: 与 spec 里的 `name="vidigest-worker"` 一致
OUT_DIR = DIST / "vidigest-worker"
EXE = OUT_DIR / "vidigest-worker.exe"

#: (导入名, 发行名, 版本下限)。导入名与发行名不一致是 PyPI 常态，别混用。
DEPS: list[tuple[str, str, str | None]] = [
    ("faster_whisper", "faster-whisper", "1.2.1"),
    ("ctranslate2", "ctranslate2", "4.8.2"),
    ("av", "av", None),
    ("onnxruntime", "onnxruntime", None),
    ("tokenizers", "tokenizers", None),
    ("numpy", "numpy", None),
    ("PIL", "Pillow", None),
    ("yaml", "PyYAML", None),
    ("PyInstaller", "pyinstaller", None),
    ("playwright", "playwright", None),
]

#: nvidia 子包 → 每个至少要有 1 个 DLL，否则 GPU 一定不可用。
CUDA_PKGS = ("cublas", "cudnn", "cuda_nvrtc")

#: 产物体积下限（MiB）。全 CUDA 收集时实测约 2.1 GiB；
#: 明显低于此值 = DLL 没收全，而不是「包很精简」。
MIN_BUNDLE_MIB = 1500

#: 产物内必须存在的关键 DLL（相对 `_internal/`）。
REQUIRED_IN_BUNDLE = (
    "nvidia/cublas/bin/cublas64_12.dll",
    "nvidia/cudnn/bin/cudnn64_9.dll",
    "ctranslate2/ctranslate2.dll",
)


# ------------------------------------------------------------------ 输出工具


def _ver_tuple(s: str) -> tuple[int, ...]:
    parts = []
    for chunk in s.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def print_rows(rows: list[tuple[bool, str]], title: str) -> None:
    print(f"\n---- {title} ----")
    for ok, msg in rows:
        print(f"  [{'ok  ' if ok else 'FAIL'}] {msg}")


def mod_path(name: str) -> str:
    """模块的实际位置。命名空间包没有 `__file__`，退回 `submodule_search_locations`。"""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError) as exc:
        return f"<find_spec 失败：{exc}>"
    if spec is None:
        return "<未安装>"
    if spec.origin and spec.origin not in ("namespace", "built-in"):
        return spec.origin
    locs = list(getattr(spec, "submodule_search_locations", None) or [])
    return locs[0] if locs else "<命名空间包，无路径>"


# ------------------------------------------------------------------ 前置校验


def check_interpreter() -> list[tuple[bool, str]]:
    rows = [
        (True, f"解释器：{sys.executable}"),
        (True, f"Python：{sys.version.split()[0]}"),
        (True, f"prefix={sys.prefix}"),
    ]
    if sys.version_info < (3, 11):
        rows.append((False, "Python 版本低于 3.11，与验收基线不一致"))
    if "workbuddy" not in str(sys.executable).lower() and "venv" not in sys.prefix.lower():
        rows.append(
            (
                True,
                "⚠️ 未使用受管 venv 运行 —— 只要依赖齐就不影响结果，但请确认不是裸系统解释器",
            )
        )
    return rows


def check_deps() -> list[tuple[bool, str]]:
    rows: list[tuple[bool, str]] = []
    for mod, dist, floor in DEPS:
        loc = mod_path(mod)
        if loc in ("<未安装>",) or loc.startswith("<"):
            rows.append((False, f"{dist}：未找到（导入名 {mod}）"))
            continue
        try:
            ver = md.version(dist)
        except md.PackageNotFoundError:
            ver = "?"
        ok = True
        note = ""
        if floor and ver != "?" and _ver_tuple(ver) < _ver_tuple(floor):
            ok = False
            note = f"  ← 低于基线 {floor}"
        rows.append((ok, f"{dist} {ver}{note}\n          {loc}"))
    return rows


def check_cuda_dlls() -> list[tuple[bool, str]]:
    rows: list[tuple[bool, str]] = []
    spec = importlib.util.find_spec("nvidia")
    bases = list(getattr(spec, "submodule_search_locations", None) or []) if spec else []
    if not bases:
        return [(False, "找不到 nvidia 包（CUDA DLL 的唯一来源）—— 打包后 GPU 必然不可用")]
    base = Path(bases[0])
    total = 0
    for pkg in CUDA_PKGS:
        bin_dir = base / pkg / "bin"
        dlls = sorted(bin_dir.glob("*.dll")) if bin_dir.is_dir() else []
        mib = sum(d.stat().st_size for d in dlls) / 1024 / 1024
        total += mib
        rows.append((bool(dlls), f"nvidia/{pkg}/bin：{len(dlls)} 个 DLL，{mib:.0f} MiB"))
    rows.append((total >= MIN_BUNDLE_MIB, f"CUDA DLL 合计 {total:.0f} MiB（下限 {MIN_BUNDLE_MIB}）"))
    return rows


def check_spec() -> list[tuple[bool, str]]:
    if not SPEC.is_file():
        return [(False, f"找不到 spec：{SPEC}")]
    return [(True, f"spec：{SPEC}")]


def run_checks() -> tuple[list[tuple[bool, str]], bool]:
    all_rows: list[tuple[bool, str]] = []
    for title, fn in (
        ("解释器", check_interpreter),
        ("spec", check_spec),
        ("依赖", check_deps),
        ("CUDA DLL", check_cuda_dlls),
    ):
        rows = fn()
        all_rows += rows
        print_rows(rows, title)
    return all_rows, all(ok for ok, _ in all_rows)


# ------------------------------------------------------------------ 构建


def stash_previous(*dirs: Path) -> list[Path]:
    """把上一版产物**改名旁置**，返回旁置后的路径列表。

    **为什么不是 `shutil.rmtree`**：本项目所在环境对「一次删除超过 50 个文件」
    有确认拦截 —— PyInstaller 自己清理 workpath 时实测报
    `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":2083,"threshold":50}`，
    然后**整个构建以退出码 1 结束**，终端只留一句「PyInstaller 失败」，看不出原因。
    改名是**一次**系统调用，不触发计数，也不丢数据。

    ⚠️ 旁置出来的目录**不会自动清理** —— 清理属破坏性操作，
    交给人在沙箱外做（脚本末尾会打印路径与体量）。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved: list[Path] = []
    for d in dirs:
        if not d.exists():
            continue
        target = d.with_name(f"{d.name}.old-{stamp}")
        n = 1
        while target.exists():
            target = d.with_name(f"{d.name}.old-{stamp}-{n}")
            n += 1
        try:
            d.rename(target)
            moved.append(target)
        except OSError as exc:  # 旁置失败不该阻断构建，PyInstaller 还有机会自己处理
            print(f"  ⚠️ 旁置失败（将继续尝试打包）：{d} —— {exc}")
    return moved


def _dir_mib(path: Path) -> float:
    try:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024
    except OSError:
        return 0.0


def run_pyinstaller() -> int:
    log_path = REPO / "build" / "pyinstaller.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 必须在调用 PyInstaller **之前**旁置：它的 COLLECT 阶段会 rmtree 目标目录。
    stashed = stash_previous(OUT_DIR, WORKDIR / OUT_DIR.name)
    if stashed:
        print("\n---- 旁置上一版 ----")
        for p in stashed:
            print(f"  {p}  （{_dir_mib(p):.0f} MiB）")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        # 刻意用 INFO 而不是 WARN：WARN 会把「哪个 hook 没找到、哪个 DLL 没解析出来」
        # 这些**唯一**能解释失败的行一起吞掉。日志落盘，刷屏只发生在文件里。
        "--log-level",
        "INFO",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORKDIR),
        str(SPEC),
    ]
    print("\n---- 打包 ----")
    print("  $ " + " ".join(cmd))
    print(f"  完整日志：{log_path}")
    started = time.time()
    # 输出**必须落盘**：失败原因通常在中段，而 `2>&1 | tail -20` 这种用法恰好把它截掉。
    # 2026-10-04 实测：退出码 1 而终端只剩「PyInstaller 失败」，什么也查不到 ——
    # 这属于「判据悄悄不报告」，比没有判据更危险。
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        code = subprocess.call(cmd, cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)
    print(f"  PyInstaller 退出码 {code}，耗时 {time.time() - started:.0f}s")
    if code != 0:
        print("\n  ⚠️ 打包失败 —— 日志尾部 40 行（完整日志见上）：")
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        for line in tail:
            print(f"  | {line}")

    if stashed:
        total = sum(_dir_mib(p) for p in stashed)
        print(f"\n  ⚠️ 上一版产物已旁置（共 {total:.0f} MiB），**不会自动清理**：")
        for p in stashed:
            print(f"      {p}")
        print("     清理需在**沙箱外**做（本工具沙箱会拦「一次删 >50 个文件」，脚本不代删）：")
        for p in stashed:
            print(f'     cmd /c rmdir /s /q "{p}"')
    return code


def check_artifacts() -> tuple[list[tuple[bool, str]], bool]:
    rows: list[tuple[bool, str]] = []
    if not EXE.is_file():
        rows.append((False, f"产物缺失：{EXE}"))
        return rows, False
    rows.append((True, f"入口：{EXE}  （{EXE.stat().st_size / 1024:.0f} KiB）"))

    internal = OUT_DIR / "_internal"
    rows.append((internal.is_dir(), f"_internal：{internal}"))

    for rel in REQUIRED_IN_BUNDLE:
        p = internal / rel
        rows.append((p.is_file(), f"_internal/{rel}" + ("" if p.is_file() else "  <== 缺失")))

    total = sum(f.stat().st_size for f in OUT_DIR.rglob("*") if f.is_file())
    mib = total / 1024 / 1024
    rows.append((mib >= MIN_BUNDLE_MIB, f"产物合计 {mib:.0f} MiB（下限 {MIN_BUNDLE_MIB}）"))
    print_rows(rows, "产物")
    return rows, all(ok for ok, _ in rows)


# ------------------------------------------------------------------ 冒烟


def _pump(stream, sink: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            sink.put(line.rstrip("\n"))
    except Exception:  # noqa: BLE001 — 管道被对端关闭属正常收尾
        pass
    finally:
        sink.put(None)


class Smoke:
    """扮演 Rust 宿主，只做四件事：hello / ping / 等 env_verified / shutdown。

    刻意**不复用** `tools/rpc_smoke.py` 的 Host：那个脚本的判据有 19 条，
    跑一遍要建任务、要真下载。这里要的是「打包产物能不能起来、GPU 是否仍可用」，
    越短越不容易把打包问题和其他问题混在一起。
    """

    def __init__(
        self,
        argv: list[str],
        cfg: Path,
        cwd: Path,
        mirror_logs_to_stderr: bool = False,
    ) -> None:
        env = dict(os.environ)
        env["VIDIGEST_CONFIG"] = str(cfg)
        # 与 Rust 侧 spawn 时的三件套保持一致（`worker/mod.rs::build_command`）
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("PYTHONHOME", None)
        if mirror_logs_to_stderr:
            # 默认日志**只走 `log` 事件**（见 `rpc.py::_install_logging`）。
            # 出问题时打开镜像：stderr 带上时间戳，能看出「卡在哪一条之后」——
            # 这是排查挂起的唯一一手材料，而挂起恰恰没有异常可看。
            env["VIDIGEST_LOG_STDERR"] = "1"
        self.proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self.out: queue.Queue[str | None] = queue.Queue()
        self.err: queue.Queue[str | None] = queue.Queue()
        self.stderr_tail: list[str] = []
        self.logs: list[str] = []
        threading.Thread(target=_pump, args=(self.proc.stdout, self.out), daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_err(self) -> None:
        for line in self.proc.stderr:
            line = line.rstrip("\n")
            self.stderr_tail.append(line)
            if len(self.stderr_tail) > 200:
                self.stderr_tail.pop(0)
        self.err.put(None)

    def send(self, name: str, args: dict | None = None, req_id: str | None = None) -> None:
        frame: dict = {"v": 1, "type": "cmd", "name": name, "args": args or {}}
        if req_id:
            frame["id"] = req_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def await_event(self, names: set[str], timeout: float) -> tuple[str, dict] | tuple[None, None]:
        """读到任一目标事件；顺带把 `log` 事件的 msg 收进 `self.logs`。"""
        deadline = time.monotonic() + timeout
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return None, None
            try:
                line = self.out.get(timeout=min(remain, 1.0))
            except queue.Empty:
                continue
            if line is None:
                return None, None  # stdout EOF = 进程已退出
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                # stdout 只应走协议；混入非 JSON 是协议纯净性缺陷
                self.logs.append(f"[非 JSON 的 stdout] {line[:160]}")
                continue
            name = frame.get("name")
            if name == "log":
                data = frame.get("data") or {}
                self.logs.append(f"[{data.get('level', '?')}] {data.get('msg', '')}")
            if name in names:
                return name, frame

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def smoke_artifacts(
    timeout_ready: float, timeout_verify: float, mirror_logs: bool = False
) -> tuple[list[tuple[bool, str]], bool]:
    rows: list[tuple[bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="vidigest-smoke-") as tmp:
        tmp_path = Path(tmp)
        # 模型**不打进包**（3 GB），所以把 model_dir 指回仓库；
        # work_dir 指到临时目录，避免污染真实产出。
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "paths:\n"
            f'  work_dir: "{(tmp_path / "work").as_posix()}"\n'
            f'  model_dir: "{(REPO / "models").as_posix()}"\n'
            "runtime:\n"
            '  device: "auto"\n'
            '  model: "large-v3"\n',
            encoding="utf-8",
        )
        print("\n---- 冒烟 ----")
        print(f"  VIDIGEST_CONFIG={cfg}")

        sm = Smoke([str(EXE)], cfg, OUT_DIR, mirror_logs)
        try:
            sm.send("hello", {"client_version": "0.0.0-bundle-smoke"}, req_id="s-1")
            name, frame = sm.await_event({"ready"}, timeout_ready)
            if name != "ready":
                rows.append((False, f"握手失败：{timeout_ready:.0f}s 内未收到 ready（进程可能起不来）"))
            else:
                data = frame.get("data") or {}
                rows.append(
                    (
                        True,
                        "hello → ready："
                        f"server_version={data.get('server_version')} "
                        f"gpu_usable={data.get('gpu_usable')} "
                        f"config_is_default={data.get('config_is_default')}",
                    )
                )
                # 打包运行时配置必须来自 VIDIGEST_CONFIG 指定的那份，
                # 不能回落到「默认配置」（那意味着 config 没被读到）
                rows.append(
                    (
                        not data.get("config_is_default"),
                        "配置来源："
                        + ("读到了指定 config ✅" if not data.get("config_is_default") else "回落到默认值 ❌"),
                    )
                )

            sm.send("ping", req_id="s-2")
            name, _ = sm.await_event({"pong"}, 15.0)
            rows.append((name == "pong", "ping → pong（协议往返）"))

            # 最高风险项：CUDA DLL 是否随包分发后仍然可用。
            # `env_verified` 是**真实推理**的结果（加载模型 + 跑一次），
            # 不是 `device_count` 那种只看硬件的快判。
            #
            # 首次跑要加载 3 GB 模型，静默等几分钟体验太差，也看不出「是慢还是挂」——
            # 所以每 30 s 报一次仍在等。挂起时这条输出就是唯一的进度信息。
            waited = 0.0
            name, frame = None, None
            while waited < timeout_verify:
                chunk = min(30.0, timeout_verify - waited)
                name, frame = sm.await_event({"env_verified"}, chunk)
                if name:
                    break
                waited += chunk
                print(f"  … 仍在等 env_verified（已 {waited:.0f}s / {timeout_verify:.0f}s）")
            if name != "env_verified":
                rows.append(
                    (
                        False,
                        f"{timeout_verify:.0f}s 内未收到 env_verified（GPU 验证没跑完或已崩）",
                    )
                )
            else:
                data = frame.get("data") or {}
                gpu = bool(data.get("gpu_usable"))
                rows.append(
                    (
                        gpu,
                        f"env_verified：gpu_usable={gpu} —— {data.get('detail', '')}",
                    )
                )

            sm.send("shutdown", req_id="s-3")
            name, _ = sm.await_event({"bye"}, 20.0)
            rows.append((name == "bye", "shutdown → bye（优雅退出）"))
        finally:
            sm.close()

        print_rows(rows, "冒烟判据")
        if not all(ok for ok, _ in rows):
            print("\n---- worker stderr 尾部（40 行）----")
            for line in sm.stderr_tail[-40:]:
                print(f"  | {line}")
            if sm.logs:
                print("\n---- worker 日志事件尾部（15 条）----")
                for line in sm.logs[-15:]:
                    print(f"  | {line}")
    return rows, all(ok for ok, _ in rows)


# ------------------------------------------------------------------ 入口


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建并校验 PyInstaller worker 产物")
    ap.add_argument("--check-only", action="store_true", help="只做前置校验，不打包")
    ap.add_argument("--skip-smoke", action="store_true", help="打包但不冒烟")
    ap.add_argument(
        "--smoke-only",
        action="store_true",
        help="跳过打包与前置校验，只对**已有产物**冒烟（改完 spec/依赖后重跑冒烟时用）",
    )
    ap.add_argument("--timeout-ready", type=float, default=90.0, help="等 ready 的超时（秒）")
    ap.add_argument(
        "--timeout-verify",
        type=float,
        default=300.0,
        help="等 env_verified 的超时（秒）。首跑要加载 3 GB 模型，别调太小",
    )
    ap.add_argument(
        "--stderr-log",
        action="store_true",
        help="把 worker 日志同时镜像到 stderr（排查挂起时用：能看出卡在哪一条之后）",
    )
    args = ap.parse_args(argv)

    if args.smoke_only:
        if not EXE.is_file():
            print(f"⇒ 没有产物可冒烟：{EXE}\n   先跑一次不带 --smoke-only 的构建。")
            return 1
        smoke_rows, smoke_ok = smoke_artifacts(
            args.timeout_ready, args.timeout_verify, args.stderr_log
        )
        passed = sum(1 for ok, _ in smoke_rows if ok)
        print(f"\n=== 总结：{passed}/{len(smoke_rows)} 冒烟判据通过 ===")
        return 0 if smoke_ok else 1

    print("=== Vidigest worker 打包 ===")
    checks, ok = run_checks()
    if not ok:
        print("\n⇒ 前置校验未通过，**不打包**。上面的 FAIL 就是原因。")
        return 1
    if args.check_only:
        print("\n⇒ 前置校验通过（--check-only，未打包）。")
        return 0

    code = run_pyinstaller()
    if code != 0:
        print(f"\n⇒ PyInstaller 失败（退出码 {code}）。")
        return code

    art_rows, art_ok = check_artifacts()
    if not art_ok:
        print("\n⇒ 产物校验未通过（DLL 没进包 / 体积异常）。")
        return 1

    if args.skip_smoke:
        print("\n⇒ 打包完成（--skip-smoke，未冒烟）。")
        return 0

    smoke_rows, smoke_ok = smoke_artifacts(
        args.timeout_ready, args.timeout_verify, args.stderr_log
    )
    total = checks + art_rows + smoke_rows
    passed = sum(1 for ok, _ in total if ok)
    print(f"\n=== 总结：{passed}/{len(total)} 判据通过 ===")
    if not smoke_ok:
        print("⇒ 冒烟未通过。**产物不能交付**：先看上面 stderr 尾部。")
        print("   常见成因：① CUDA DLL 未进包；② 模型路径不存在；③ 打包漏了某个原生依赖。")
        return 1
    print("⇒ 产物可用。下一步见 docs/打包与分发.md 的「沙箱外验证」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
