"""IPC 协议冒烟测试（docs/IPC协议规格.md §9 的 8 条判据）。

不需要 Rust 端——本脚本扮演宿主：spawn `python -m app.rpc`，走 stdin/stdout，
逐条核对协议契约。Rust 工程建起来之前，这就是协议层的验收手段。

用法：
    python tools/rpc_smoke.py
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: `env_verified` 要真实加载模型（约 5-8 秒），给足超时
ENV_VERIFY_TIMEOUT = 90
DEFAULT_TIMEOUT = 15


class Host:
    """扮演 Rust 宿主：管理与 worker 的两条管道。"""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "app.rpc"],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ, **(env or {})},
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr_lines: list[str] = []
        self.received: list[str] = []
        self.judgements: list[tuple[str, bool, str]] = []

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # ---- 管道 ----

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                break
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def _read_stderr(self) -> None:
        """必须独立消费 stderr，否则缓冲区满会让子进程阻塞。"""
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    # ---- 发送 ----

    def send(self, name: str, args: dict | None = None, req_id: str | None = None) -> None:
        frame: dict = {"v": 1, "type": "cmd", "name": name, "args": args or {}}
        if req_id is not None:
            frame["id"] = req_id
        self.send_raw(json.dumps(frame, ensure_ascii=False))

    def send_raw(self, text: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    # ---- 接收 ----

    def await_event(self, name: str, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
        """等到指定事件，途中记录所有原始行（用于协议纯净性检查）。"""
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                line = self.lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                return None
            self.received.append(line)
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                self.judgements.append(
                    ("协议纯净性", False, f"stdout 出现非 JSON 行：{line[:100]!r}")
                )
                continue
            if evt.get("name") == name:
                return evt

    def close_stdin(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.judgements.append((name, ok, detail))


def check_protocol_purity(host: Host) -> None:
    """判据 1：所有收到的行都必须是合法 JSON。

    这条能抓住一切 stray print——包括依赖库偷偷打到 stdout 的东西。
    """
    bad = []
    for line in host.received:
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad.append(line[:100])
    if bad:
        host.record("协议纯净性", False, f"{len(bad)} 行非法：{bad[:2]}")
    else:
        host.record("协议纯净性", True, f"{len(host.received)} 行全部合法 JSON")


def check_seq_monotonic(host: Host) -> None:
    """判据 7：`seq` 严格递增且无跳号。"""
    seqs = []
    for line in host.received:
        try:
            seqs.append(json.loads(line)["seq"])
        except (json.JSONDecodeError, KeyError):
            return
    expected = list(range(1, len(seqs) + 1))
    if seqs == expected:
        host.record("seq 连续", True, f"1..{len(seqs)} 无跳号")
    else:
        host.record("seq 连续", False, f"实际 {seqs[:10]}… 期望 1..{len(seqs)}")


def run_main_flow(env: dict[str, str]) -> Host:
    host = Host(env)

    # ---- 判据 2：握手 ----
    host.send("hello", {"client_version": "0.0.0-smoke"}, req_id="c-1")
    ready = host.await_event("ready")
    if ready is None:
        host.record("握手 hello→ready", False, "超时未收到 ready")
        return host
    data = ready.get("data", {})
    ok = (
        isinstance(data.get("gpu_usable"), bool)
        and data.get("server_version")
        and ready.get("id") == "c-1"
    )
    host.record(
        "握手 hello→ready",
        ok,
        f"server={data.get('server_version')} gpu_usable={data.get('gpu_usable')} "
        f"verified={data.get('gpu_verified')} 并发={data.get('concurrency')}",
    )

    # ---- 判据 3：未知命令不崩 ----
    host.send("no_such_command", req_id="c-2")
    err = host.await_event("error")
    ok_unknown = err is not None and err.get("data", {}).get("code") == "E_UNKNOWN_CMD"
    host.send("ping", req_id="c-3")
    pong = host.await_event("pong", timeout=5)
    host.record(
        "未知命令不崩",
        bool(ok_unknown and pong is not None),
        f"E_UNKNOWN_CMD 已回={ok_unknown}；随后 ping 仍通={pong is not None}",
    )

    # ---- 判据 4：非法 JSON 不崩 ----
    host.send_raw("this is not json at all")
    err = host.await_event("error")
    ok_frame = err is not None and err.get("data", {}).get("code") == "E_BAD_FRAME"
    host.record("非法 JSON 不崩", ok_frame, f"code={err.get('data', {}).get('code') if err else None}")

    # ---- 判据 5：心跳 2 秒内 ----
    started = time.time()
    host.send("ping", req_id="c-4")
    pong = host.await_event("pong", timeout=5)
    elapsed = time.time() - started
    host.record(
        "心跳 <2s 响应",
        pong is not None and elapsed < 2.0,
        f"往返 {elapsed * 1000:.0f} ms",
    )

    # ---- 任务入队：分享文案抽 URL + 批内去重 ----
    host.send(
        "add_tasks",
        {
            "urls": [
                "8.25 复制打开抖音，看看【某创作者】 https://v.douyin.com/AbCdEf12345/ vFh:/ 03/30",
                "https://v.douyin.com/AbCdEf12345/",
                "https://www.bilibili.com/video/BV1xx411c7mD",
            ]
        },
        req_id="c-5",
    )
    added = host.await_event("tasks_added")
    if added is None:
        host.record("add_tasks 抽 URL", False, "超时")
    else:
        d = added.get("data", {})
        # 第 1 行是分享文案 → 抽出短链；第 2 行与之相同 → 在 urls 层就被合并
        # 所以这里应是 2 条入队、0 条 skipped（不是 1 条 duplicate）
        ok_add = len(d.get("added", [])) == 2 and len(d.get("skipped", [])) == 0
        host.record(
            "add_tasks 抽 URL + 批内去重",
            ok_add,
            f"3 行输入 → 入队 {len(d.get('added', []))} 条、跳过 {len(d.get('skipped', []))} 条",
        )

    # ---- 跨批次去重：这块由 store 的 url UNIQUE 约束负责 ----
    host.send("add_tasks", {"urls": ["https://v.douyin.com/AbCdEf12345/"]}, req_id="c-5b")
    again = host.await_event("tasks_added")
    if again is None:
        host.record("add_tasks 跨批次去重", False, "超时")
    else:
        d2 = again.get("data", {})
        skipped2 = d2.get("skipped", [])
        ok_dup = (
            len(d2.get("added", [])) == 0
            and len(skipped2) == 1
            and skipped2[0].get("reason") == "duplicate"
        )
        host.record("add_tasks 跨批次去重", ok_dup, f"重复链接被识别：{skipped2}")

    # ---- list_tasks ----
    host.send("list_tasks", req_id="c-6")
    lst = host.await_event("task_list")
    n = len(lst.get("data", {}).get("tasks", [])) if lst else -1
    host.record("list_tasks", lst is not None and n == 2, f"返回 {n} 条")

    # ---- 判据：流水线控制面（阶段 2 起由 dispatcher 提供）----
    # 先把队列清空：start 会去跑 store 里的可续跑任务，留着它们会真发网络请求。
    # 清空同时验证 remove_task。
    removed = []
    for task in lst.get("data", {}).get("tasks", []) if lst else []:
        host.send("remove_task", {"task_id": task["task_id"]}, req_id=f"c-rm-{task['task_id']}")
        removed.append(host.await_event("task_removed") is not None)
    host.record("remove_task 清空队列", all(removed) and len(removed) == 2, f"移除 {sum(removed)}/2")

    host.send("start", req_id="c-7")
    started_evt = host.await_event("started")
    sd = started_evt.get("data", {}) if started_evt else {}
    host.record(
        "start → started（空队列幂等启动，落 idle 而非 running）",
        started_evt is not None and sd.get("added") == 0 and sd.get("state") == "idle",
        f"added={sd.get('added')} state={sd.get('state')}",
    )

    # 2026-10-06 语义修正：空转 start 不再把调度器留在 RUNNING（那会让
    # pipeline_state 永远停在 running，UI 开始按钮灰死）。idle 下的 pause /
    # resume 应被拒绝——接受语义的正向覆盖在 dispatcher_selftest 判据 2。
    host.send("pause", req_id="c-8")
    paused_evt = host.await_event("paused")
    host.record(
        "pause → paused（idle 下拒绝，不再假接受）",
        paused_evt is not None and paused_evt.get("data", {}).get("accepted") is False,
        f"state={paused_evt.get('data', {}).get('state') if paused_evt else None}",
    )

    host.send("resume", req_id="c-9")
    resumed_evt = host.await_event("resumed")
    host.record(
        "resume → resumed（未暂停过则拒绝）",
        resumed_evt is not None and resumed_evt.get("data", {}).get("accepted") is False,
        f"state={resumed_evt.get('data', {}).get('state') if resumed_evt else None}",
    )

    host.send("cancel_task", {"task_id": 99999}, req_id="c-10")
    err_cancel = host.await_event("error")
    host.record(
        "cancel_task 不存在的 id → E_BAD_ARGS",
        bool(err_cancel) and err_cancel.get("data", {}).get("code") == "E_BAD_ARGS",
        f"code={err_cancel.get('data', {}).get('code') if err_cancel else None}",
    )

    host.send("retry_task", {"task_id": 99999}, req_id="c-11")
    err_retry = host.await_event("error")
    host.record(
        "retry_task 不存在的 id → E_BAD_ARGS",
        bool(err_retry) and err_retry.get("data", {}).get("code") == "E_BAD_ARGS",
        f"code={err_retry.get('data', {}).get('code') if err_retry else None}",
    )

    # ---- 设置：get_config（2026-10-24 新增）----
    host.send("get_config", req_id="c-cfg-1")
    cfg_evt = host.await_event("config")
    cd = cfg_evt.get("data", {}) if cfg_evt else {}
    needed = {
        "path",
        "is_default",
        "config",
        "model_path",
        "model_present",
        "model_missing_files",
        "model_size_bytes",
        "read_only_fields",
        "languages",
        "api_key_env",
        "api_key_present",
        "download",
    }
    host.record(
        "get_config 返回全量配置 + 模型元信息",
        cfg_evt is not None and needed.issubset(cd.keys()) and isinstance(cd.get("config"), dict),
        f"缺字段={sorted(needed - set(cd.keys()))} model_present={cd.get('model_present')}",
    )
    # ---- 设置：set_config 的安全边界 ----
    host.send(
        "set_config",
        {
            "patch": {
                "summarize": {"api_key": "sk-must-not-land", "enabled": True},
                "transcribe": {"beam_size": 7},
                "bogus_section": {"x": 1},
            }
        },
        req_id="c-cfg-2",
    )
    saved = host.await_event("config_saved")
    sd = saved.get("data", {}) if saved else {}
    warn = sd.get("warnings") or []
    on_disk = Path(env["VIDIGEST_CONFIG"]).read_text(encoding="utf-8")
    no_key_in_payload = "api_key" not in (sd.get("config", {}).get("summarize") or {})
    host.record(
        "set_config 拒写 summarize.api_key（响应与磁盘都没有）",
        "E_API_KEY_NOT_ALLOWED" in warn and "sk-must-not-land" not in on_disk and no_key_in_payload,
        f"warnings={warn} 磁盘含明文={'sk-must-not-land' in on_disk}",
    )
    host.record(
        "set_config 未知段丢弃并警告",
        any(str(w).startswith("E_UNKNOWN_SECTION") for w in warn),
        f"warnings={warn}",
    )
    host.record(
        "set_config 基线参数改动 → E_BASELINE_PARAM_CHANGED 且已落盘",
        any(str(w).startswith("E_BASELINE_PARAM_CHANGED") for w in warn)
        and "beam_size: 7" in on_disk,
        f"warnings={warn} 落盘={'beam_size: 7' in on_disk}",
    )

    # ---- 设置：非法值被 validate 挡住，不写盘 ----
    before_text = Path(env["VIDIGEST_CONFIG"]).read_text(encoding="utf-8")
    host.send("set_config", {"patch": {"runtime": {"device": "tpu"}}}, req_id="c-cfg-3")
    err_cfg = host.await_event("error")
    after_text = Path(env["VIDIGEST_CONFIG"]).read_text(encoding="utf-8")
    host.record(
        "set_config 非法 device → E_BAD_ARGS 且不写盘",
        bool(err_cfg) and err_cfg.get("data", {}).get("code") == "E_BAD_ARGS" and before_text == after_text,
        f"code={err_cfg.get('data', {}).get('code') if err_cfg else None} 未写盘={before_text == after_text}",
    )

    # ---- 等 GPU 真实验证（异步，要加载模型）----
    print("   … 等待 env_verified（真实推理验证，需加载模型 5-8 秒）")
    verified = host.await_event("env_verified", timeout=ENV_VERIFY_TIMEOUT)
    if verified is None:
        host.record("env_verified 异步推送", False, f"{ENV_VERIFY_TIMEOUT}s 内未收到")
    else:
        vd = verified.get("data", {})
        host.record(
            "env_verified 异步推送",
            "gpu_usable" in vd and "concurrency" in vd and "blocked_reason" in vd,
            f"gpu_usable={vd.get('gpu_usable')} blocked_reason={vd.get('blocked_reason')} "
            f"detail={str(vd.get('detail'))[:50]}",
        )
        # 三态语义（规格 §4.2）：verifier 只认「非 null 才算验过」
        host.record(
            "env_verified 的 gpu_verified 与三态一致",
            vd.get("gpu_verified") == (vd.get("gpu_usable") is not None),
            f"gpu_usable={vd.get('gpu_usable')} gpu_verified={vd.get('gpu_verified')}",
        )

        # ---- 手动复核（设置页的「重新校验」）----
        host.send("verify_env", req_id="c-verify")
        started_evt = host.await_event("env_verify_started")
        again = host.await_event("env_verified", timeout=ENV_VERIFY_TIMEOUT)
        host.record(
            "verify_env → env_verify_started + env_verified",
            started_evt is not None and again is not None,
            f"started={'收到' if started_evt else '未收到'} "
            f"verified={'收到' if again else '未收到'}",
        )

    # ---- 清空数据（设置页「维护」）----
    work = Path(env["VIDIGEST_CONFIG"]).parent / "work"
    probe_file = work / "probe.mp4"
    work.mkdir(parents=True, exist_ok=True)
    probe_file.write_bytes(b"x")
    models_dir = ROOT / "models" / "large-v3"
    models_before = sorted(p.name for p in models_dir.iterdir()) if models_dir.is_dir() else []

    host.send("clear_data", {"scope": "workspace"}, req_id="c-clear-1")
    cleared = host.await_event("data_cleared")
    c1 = cleared.get("data", {}) if cleared else {}
    models_after = sorted(p.name for p in models_dir.iterdir()) if models_dir.is_dir() else []
    host.record(
        "clear_data(workspace)：清内容、留目录、**不碰模型**",
        cleared is not None
        and int(c1.get("files_removed") or 0) >= 1
        and not probe_file.exists()
        and work.is_dir()
        and models_before == models_after,
        f"files_removed={c1.get('files_removed')} 目录还在={work.is_dir()} "
        f"模型未变={models_before == models_after}",
    )

    host.send("clear_data", {"scope": "history"}, req_id="c-clear-2")
    cleared2 = host.await_event("data_cleared")
    host.send("list_tasks", req_id="c-clear-3")
    after = host.await_event("task_list")
    remaining = len(((after or {}).get("data") or {}).get("tasks") or [])
    host.record(
        "clear_data(history)：任务清空",
        cleared2 is not None and remaining == 0,
        f"tasks_removed={((cleared2 or {}).get('data') or {}).get('tasks_removed')} 剩余={remaining}",
    )

    host.send("clear_data", {"scope": "nonsense"}, req_id="c-clear-4")
    bad = host.await_event("error")
    host.record(
        "clear_data 非法 scope → E_BAD_ARGS",
        ((bad or {}).get("data") or {}).get("code") == "E_BAD_ARGS",
        f"code={((bad or {}).get('data') or {}).get('code')}",
    )

    # ---- 判据 8：优雅关闭 ----
    host.send("shutdown", req_id="c-8")
    bye = host.await_event("bye", timeout=10)
    host.record("优雅关闭 shutdown→bye", bye is not None, f"bye={'收到' if bye else '未收到'}")

    try:
        code = host.proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        code = None
    host.record("退出码为 0", code == 0, f"exit={code}")

    return host


def run_eof_flow(env: dict[str, str], judgements: list[tuple[str, bool, str]]) -> None:
    """宿主消失（stdin EOF）时 worker 必须自行退出，不留孤儿。

    这一步必须**单开一个 worker**（它要自杀，不能复用前面那个），因此也不能把结果
    记在 `run_main_flow` 返回的 host 上。

    ⚠️ 2026-10-03 修：原先这里新建 host 后 `record` 到自己身上，而 `main()` 只打印
    第一个 host 的判据 → **这一条从未被报告**，脚本却显示「18/18 通过」。
    一个悄悄不报告的判据比没有判据更危险：它制造虚假的信心。
    所以现在把结果列表**显式传进来**（而不是返回新 host），漏接会在类型上看得见。
    """
    host = Host(env)
    host.send("hello", {"client_version": "0.0.0-smoke"})
    ready = host.await_event("ready", timeout=15)
    if ready is None:
        judgements.append(("EOF 自杀（3s 内）", False, "握手就没成功"))
        host.proc.kill()
        return

    started = time.time()
    host.close_stdin()  # 模拟宿主进程突然消失
    try:
        code = host.proc.wait(timeout=10)
        elapsed = time.time() - started
        judgements.append(
            ("EOF 自杀（3s 内）", elapsed < 3.0, f"{elapsed:.2f}s 内退出，exit={code}")
        )
    except subprocess.TimeoutExpired:
        host.proc.kill()
        judgements.append(("EOF 自杀（3s 内）", False, "10s 仍未退出，已成孤儿进程"))

    host.proc.wait(timeout=5)


def _isolated_env() -> tuple[dict[str, str], str]:
    """构造隔离环境：临时 config + 临时 work_dir。

    测试会真实入队任务，若直接写项目根的 `state.db` 会留下脏数据，
    且第二次运行会全部被判为 duplicate。
    """
    tmp = tempfile.mkdtemp(prefix="vidigest-smoke-")
    cfg = Path(tmp) / "config.yaml"
    cfg.write_text(
        f"paths:\n  work_dir: {tmp}/work\n  model_dir: {ROOT}/models\n",
        encoding="utf-8",
    )
    return {"VIDIGEST_CONFIG": str(cfg)}, tmp


def check_verify_cuda_tristate(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """`verify_cuda` 的「没验成」必须是 `None`，不能是 `False`。

    这条直接对应 2026-10-24 的误报：模型没装返回 `False`，上层一路读成
    「GPU 不可用」，状态栏写「吞吐下降约 6 倍」，把用户引向驱动排查 —— 方向全错。
    这两条不需要加载模型，所以是**秒级**判据。
    """
    from app.core import env_probe

    missing = Path(tmp) / "no-such-model"
    ok, detail, reason = env_probe.verify_cuda(missing)
    judgements.append(
        (
            "verify_cuda：模型缺失 → (None, model_missing)",
            ok is None and reason == "model_missing",
            f"ok={ok!r} reason={reason} detail={detail[:46]}",
        )
    )

    partial = Path(tmp) / "partial-model"
    partial.mkdir(parents=True, exist_ok=True)
    (partial / "config.json").write_text("{}", encoding="utf-8")
    ok2, detail2, reason2 = env_probe.verify_cuda(partial)
    judgements.append(
        (
            "verify_cuda：模型不完整 → (None, model_incomplete)",
            ok2 is None and reason2 == "model_incomplete",
            f"ok={ok2!r} reason={reason2} detail={detail2[:46]}",
        )
    )


def check_language_table(judgements: list[tuple[str, bool, str]]) -> None:
    """语言码表与 faster-whisper 实际支持的表必须逐项一致。

    `LANGUAGE_LABELS` 是**硬编码**的（理由见 `app/models/config.py`：不能在
    config.py 里 import faster_whisper，会把原生库拖进启动路径）。硬编码就意味着
    会漂移 —— 所以这里拿已安装的包当场对表。少一个语言、多一个语言都算失败。
    """
    from app.models.config import (
        LANGUAGE_AUTO,
        LANGUAGE_LABELS,
        SUPPORTED_LANGUAGES,
        Config,
        ConfigError,
    )

    try:
        from faster_whisper.tokenizer import _LANGUAGE_CODES as upstream
    except Exception as exc:  # noqa: BLE001 — 缺包时报清楚，不要静默跳过
        judgements.append(("语言表与 faster-whisper 一致", False, f"无法导入：{exc}"))
    else:
        mine = list(SUPPORTED_LANGUAGES)
        diff = (
            f"缺={sorted(set(upstream) - set(mine))} 多={sorted(set(mine) - set(upstream))}"
            if set(upstream) != set(mine)
            else ("顺序不同" if list(upstream) != mine else "")
        )
        judgements.append(
            (
                "语言表与 faster-whisper 一致（数量 + 内容 + 顺序）",
                not diff and len(mine) == len(upstream),
                diff or f"{len(mine)} 项，标签齐全={len(LANGUAGE_LABELS) == len(mine)}",
            )
        )

    # 空串 = 自动检测，必须能过校验；随手编的码必须被挡在写盘前。
    auto_ok = False
    bogus_ok = False
    try:
        cfg = Config.from_dict({"transcribe": {"language": LANGUAGE_AUTO}})
        cfg.validate()
        auto_ok = True
    except ConfigError:
        auto_ok = False
    try:
        Config.from_dict({"transcribe": {"language": "klingon"}}).validate()
    except ConfigError:
        bogus_ok = True
    judgements.append(
        (
            "language 校验：空串=自动检测放行、乱码拒绝",
            auto_ok and bogus_ok,
            f"空串通过={auto_ok} 乱码被拒={bogus_ok}",
        )
    )


def check_default_dirs(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """首启默认目录：**优先安装目录**，安装目录不可写才回退。

    第 1 条防的是静默缺陷：`PathsConfig.resolve()` 是逐字段重建，漏一个字段就会在
    每次 `Config.from_dict` 时把它重置成默认值。表现是「引导弹了、用户点了确认、
    下次启动又弹」—— 用户会以为设置没保存。

    第 2/3 条是同一条规则的两面：装到可写位置（例如 D:////Tool////Vidigest）时默认落在
    安装目录下；装到 Program Files 且当前用户没写权限时必须**自动回退**，
    否则首启就是「保存失败」，用户完全不知道该改哪里。
    """
    from app.core import paths as paths_mod
    from app.models.config import Config

    # ---- 1. 解析不丢字段 ----
    cfg = Config.from_dict({"paths": {"work_dir_confirmed": False}}, root=Path(tmp))
    judgements.append(
        (
            "work_dir_confirmed 经 resolve 后仍保留",
            cfg.paths.work_dir_confirmed is False,
            f"读回={cfg.paths.work_dir_confirmed}",
        )
    )

    # ---- 2/3. 安装目录优先 + 不可写回退 ----
    # 这三条都要「假装打包运行」，用完立刻还回去，否则后面所有 `app_data_dir()`
    # 都跟着变，污染本脚本其余判据。
    orig_frozen = paths_mod.is_frozen
    orig_install = paths_mod.install_dir
    try:
        paths_mod.is_frozen = lambda: True  # type: ignore[assignment]

        inst = Path(tmp) / "inst"
        inst.mkdir(parents=True, exist_ok=True)
        paths_mod.install_dir = lambda: inst  # type: ignore[assignment]
        picked = paths_mod.pick_default_work_dir()
        judgements.append(
            (
                "安装目录可写 → 默认工作目录 = <安装目录>/workspace",
                picked == inst / "workspace",
                f"选了 {picked}",
            )
        )
        picked_model = paths_mod.pick_default_model_dir("large-v3", roots=[Path(tmp) / "empty"])
        judgements.append(
            (
                "安装目录可写 → 默认模型目录 = <安装目录>/models",
                picked_model == inst / "models",
                f"选了 {picked_model}",
            )
        )

        # 指向一个不存在的盘 ⇒ mkdir 必然失败 ⇒ 走得通「不可写」这一支。
        paths_mod.install_dir = lambda: Path("Z:////no-such-drive////inst")  # type: ignore[assignment]
        fallback = paths_mod.pick_default_work_dir()
        system = (os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()
        has_other = any(
            Path(f"{c}:\\").exists() and f"{c}:" != system
            for c in "DEFGHIJKLMNOPQRSTUVWXYZ"
        )
        if has_other:
            ok = fallback.drive.upper() != system and fallback.name == "workspace"
        else:
            ok = fallback == paths_mod.app_data_dir() / "workspace"
        judgements.append(
            ("安装目录不可写 → 回退到可写位置", ok, f"回退到 {fallback}（系统盘 {system}）")
        )
    finally:
        paths_mod.is_frozen = orig_frozen  # type: ignore[assignment]
        paths_mod.install_dir = orig_install  # type: ignore[assignment]

    judgements.append(
        (
            "源码运行 install_dir() 为 None",
            orig_install() is None,
            f"install_dir={orig_install()!r}",
        )
    )


def check_model_dir_detect(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """模型目录探测：**完整**才算数（少一个文件就当成没装）。

    对应「卸载删数据目录」的副作用：config.yaml 没了 ⇒ `model_dir` 回到默认值。
    如果探测把「下了一半」的目录认成可用，症状会是「刚重装就说 GPU 不可用」，
    而用户完全无从判断 —— 所以判据必须是 5 个文件全在。
    """
    from app.core import env_probe, paths as paths_mod

    base = Path(tmp) / "Vidigest"
    models = base / "models"
    full = models / "large-v3"
    full.mkdir(parents=True, exist_ok=True)
    for name in env_probe.MODEL_FILES:
        (full / name).write_bytes(b"x" * 8)

    found = paths_mod.find_existing_model_dir("large-v3", roots=[base])
    judgements.append(("已有完整模型 → 探测命中", found == models, f"返回={found}"))

    partial_base = Path(tmp) / "partial"
    partial = partial_base / "models" / "large-v3"
    partial.mkdir(parents=True, exist_ok=True)
    (partial / "config.json").write_bytes(b"x")
    found2 = paths_mod.find_existing_model_dir("large-v3", roots=[partial_base])
    judgements.append(
        (
            "模型不完整 → 探测不命中（不能把下了一半当成能用）",
            found2 is None,
            f"返回={found2}",
        )
    )


def main() -> int:
    print("IPC 协议冒烟测试 —— docs/IPC协议规格.md §9\n")

    env, tmp = _isolated_env()

    host = run_main_flow(env)
    check_protocol_purity(host)
    check_seq_monotonic(host)
    check_verify_cuda_tristate(tmp, host.judgements)
    check_language_table(host.judgements)
    check_default_dirs(tmp, host.judgements)
    check_model_dir_detect(tmp, host.judgements)

    run_eof_flow(env, host.judgements)

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'判据':<34} {'结果':<6} 说明")
    print("-" * 100)
    passed = 0
    for name, ok, detail in host.judgements:
        mark = "PASS" if ok else "FAIL"
        passed += 1 if ok else 0
        print(f"{name:<30} {mark:<6} {detail}")
    total = len(host.judgements)
    print("-" * 100)
    print(f"{passed}/{total} 通过")

    if passed < total:
        print("\n--- worker stderr 末尾 ---")
        for line in host.stderr_lines[-25:]:
            print("  " + line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
