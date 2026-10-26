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
    # 版本号是「用户确认自己装的哪个版本」的唯一凭据：MSI 上是 tauri.conf 的版本，
    # 界面上显示的是 worker 上报的这个值。两者不一致 = 用户永远说不清自己装的啥。
    want_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    host.record(
        "worker 上报的版本 == VERSION",
        data.get("server_version") == want_version,
        f"上报={data.get('server_version')} VERSION={want_version}",
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
        "packs",
        "ai",
    }
    host.record(
        "get_config 返回全量配置 + 模型元信息",
        cfg_evt is not None and needed.issubset(cd.keys()) and isinstance(cd.get("config"), dict),
        f"缺字段={sorted(needed - set(cd.keys()))} model_present={cd.get('model_present')}",
    )
    packs = {p.get("key"): p for p in (cd.get("packs") or [])}
    ai_info = cd.get("ai") or {}
    host.record(
        "get_config 下发资产包清单，present 与真实状态一致",
        set(packs) == {"whisper", "local_ai"}
        and packs["whisper"].get("present") == bool(cd.get("model_present"))
        and packs["local_ai"].get("present") == bool(ai_info.get("local_ready"))
        and all(isinstance(p.get("target_dir"), str) for p in packs.values()),
        f"packs={sorted(packs)} whisper={packs.get('whisper', {}).get('present')} "
        f"local_ai={packs.get('local_ai', {}).get('present')}",
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

    # 只认「数据目录」与「安装目录」两处：非系统盘上的 <盘>:\Vidigest\models **不再被识别**。
    # 这条锁的是 2026-10-26 的决定 —— 上一版会去扫各个非系统盘，导致「模型明明放
    # 安装目录了，却用了 D 盘那份」，用户完全看不出程序从哪读的。
    tmp_appdata = Path(tmp) / "appdata"
    tmp_inst = Path(tmp) / "inst2"
    orig_frozen, orig_appdata, orig_install = (
        paths_mod.is_frozen,
        paths_mod.app_data_dir,
        paths_mod.install_dir,
    )
    paths_mod.is_frozen = lambda: True  # type: ignore[assignment]
    paths_mod.app_data_dir = lambda: tmp_appdata  # type: ignore[assignment]
    paths_mod.install_dir = lambda: tmp_inst  # type: ignore[assignment]
    try:
        # 非系统盘（模拟）上放一份完整模型 —— 不该被发现
        drive_models = Path(tmp) / "fakedrive" / "Vidigest" / "models" / "large-v3"
        drive_models.mkdir(parents=True, exist_ok=True)
        for name in env_probe.MODEL_FILES:
            (drive_models / name).write_bytes(b"x" * 8)
        ignored = paths_mod.find_existing_model_dir("large-v3") is None

        # 安装目录下放一份 —— 必须被发现
        inst_models = tmp_inst / "models" / "large-v3"
        inst_models.mkdir(parents=True, exist_ok=True)
        for name in env_probe.MODEL_FILES:
            (inst_models / name).write_bytes(b"x" * 8)
        hit = paths_mod.find_existing_model_dir("large-v3") == tmp_inst / "models"
    finally:
        paths_mod.is_frozen = orig_frozen  # type: ignore[assignment]
        paths_mod.app_data_dir = orig_appdata  # type: ignore[assignment]
        paths_mod.install_dir = orig_install  # type: ignore[assignment]

    judgements.append(
        (
            "只认数据目录 + 安装目录：非系统盘的 models 不再被识别",
            ignored and hit,
            f"非系统盘被忽略={ignored} 安装目录命中={hit}",
        )
    )


def check_platform_registry(judgements: list[tuple[str, bool, str]]) -> None:
    """平台注册表：一个 URL 必须落到**对的** provider 上。

    这条防的是「加平台时漏一处分支」：现有 3 个平台散落在 fetcher 的 if/else 里，
    加第 4 个时最可能的失误是 `resolve()` 返回通用实现，于是抖音的会话要求、
    本地文件的硬链接分支都被跳过 —— 症状是「下载报错但看不出为什么」。
    """
    from app.platforms import resolve

    cases = [
        ("https://www.youtube.com/watch?v=abc123", "youtube"),
        ("https://youtu.be/abc123", "youtube"),
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
        ("https://b23.tv/abc", "bilibili"),
        ("https://x.com/foo/status/123456", "twitter"),
        ("https://twitter.com/foo/status/123456", "twitter"),
        ("https://v.douyin.com/ye_7rRWF3tc/", "douyin"),
        ("https://www.douyin.com/video/7123456789012345678", "douyin"),
        # 作者主页：批量下载的入口。2026-10-26 漏过这条 —— 落到 unknown 就会
        # 被丢给 yt-dlp（超时 120 秒），而浏览器取流那条路根本没被调用。
        ("https://www.douyin.com/user/MS4wLjABAAAAxxx", "douyin"),
        ("C:/tmp/clip.mp4", "local"),
        ("https://example.com/some/video", "unknown"),  # 交给 yt-dlp 通用兜底
    ]
    bad = [(u, resolve(u).name, want) for u, want in cases if resolve(u).name != want]
    judgements.append(
        (
            "平台注册表 resolve() 11 个形态",
            not bad,
            f"不符={bad}" if bad else "11 个全对",
        )
    )

    # 能力位：本地没有「作者作品」；抖音**有**（浏览器取流那条路，2026-10-26 打通，
    # 但只保证首屏条数）。UI 靠它决定要不要显示批量入口。
    from app.platforms import supports_author

    ok = (
        supports_author("https://v.douyin.com/x/") is True
        and supports_author("C:/tmp/a.mp4") is False
        and supports_author("https://www.bilibili.com/video/BV1") is True
    )
    judgements.append(
        (
            "能力位：本地无作者列表，抖音/B 站有",
            ok,
            f"douyin={supports_author('https://v.douyin.com/x/')} "
            f"local={supports_author('C:/tmp/a.mp4')} "
            f"bilibili={supports_author('https://www.bilibili.com/video/BV1')}",
        )
    )

    # DASH 流挑选：一次播放会发几十个 206 分段请求（URL 常完全相同），
    # 必须去重后各取一条。挑错的表现是「下了一个 0 秒的视频」。
    from app.platforms.douyin_browser import classify

    v, a = classify([
        "https://v26-web.douyinvod.com/x/video/tos/cn/o1/media-video-avc1/?a=1",
        "https://v26-web.douyinvod.com/x/video/tos/cn/o1/media-video-avc1/?a=1",  # 重复
        "https://v26-web.douyinvod.com/x/video/tos/cn/o2/media-audio-und-mp4a/?a=2",
        "https://lf-douyin-pc-web.douyinstatic.com/obj/uuu_265.mp4",  # 装饰动画，不该被选中
    ])
    judgements.append(
        (
            "抖音 DASH 流挑选（去重 / 区分音视频 / 忽略装饰 mp4）",
            "media-video" in v and "media-audio" in a and "douyinstatic" not in v,
            f"video={v.split('/')[-1][:24] or '空'} audio={a.split('/')[-1][:24] or '空'}",
        )
    )


def check_asset_packs(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """可下载资产包：清单、落点、与查找规则的一致性。

    这几条守的是**「下完了却说找不到」**——下载落点与 `ai_backend` 的查找规则
    一旦分叉，症状是 `local_ready=False` 而磁盘上文件明明在，日志里只会说
    「找不到 llama-server」。这类错从现象上看很像下载失败，实际一行路径对不上。
    """
    from app.core import ai_backend
    from app.core import asset_packs as A
    from app.core import model_downloader
    from app.core.env_probe import MODEL_FILES
    from app.models.config import Config

    root = Path(tmp) / "assets"
    cfg = Config.from_dict({}, root=str(root))

    # ---- whisper 清单没被重构改掉 ----
    wp = A.build_pack("whisper", cfg)
    names = [f.name for f in wp.files]
    judgements.append(
        (
            "资产包 whisper：文件清单仍与 MODEL_FILES 全等",
            names == list(MODEL_FILES) and wp.target_dir == cfg.model_path(),
            f"{len(names)} 个 一致={names == list(MODEL_FILES)} target={wp.target_dir == cfg.model_path()}",
        )
    )

    # ---- local_ai 的落点 ----
    la = A.build_pack("local_ai", cfg)
    zips = [f for f in la.files if f.unzip]
    ggufs = [f for f in la.files if f.name.lower().endswith(".gguf")]
    judgements.append(
        (
            "资产包 local_ai：目标目录 = vision_model_path，zip 落 llama/",
            la is not None
            and la.target_dir == cfg.vision_model_path()
            and len(zips) == 2
            and all(f.dest_dir == "llama" for f in zips)
            and len(ggufs) == 2
            and all(f.dest_dir == "" for f in ggufs),
            f"target={la.target_dir == cfg.vision_model_path()} zip={len(zips)} gguf={len(ggufs)}",
        )
    )

    # ---- 落点 → 查找：真的建文件，看 ai_backend 认不认得 ----
    probe = Path(tmp) / "layout"
    (probe / "llama").mkdir(parents=True, exist_ok=True)
    (probe / "llama" / "llama-server.exe").write_bytes(b"MZ")
    (probe / "Qwen3-VL-2B-Instruct-Q4_K_M.gguf").write_bytes(b"Q")
    (probe / "mmproj-BF16.gguf").write_bytes(b"P")
    server = ai_backend._find_server(None, probe)
    main, mmproj = ai_backend._find_weights(probe)
    dl_server = model_downloader._find_llama_server(probe)
    judgements.append(
        (
            "资产包落点与 ai_backend 的查找规则一致（下载完立刻能认出来）",
            server is not None
            and dl_server == server
            and main is not None
            and main.name.startswith("Qwen3-VL")
            and mmproj is not None
            and "mmproj" in mmproj.name,
            f"server={server.name if server else None} main={main.name if main else None} "
            f"mmproj={mmproj.name if mmproj else None}",
        )
    )

    # ---- 未知 key ----
    judgements.append(
        (
            "资产包：未知 key 返回 None，PACK_KEYS 每个都能建",
            A.build_pack("nope", cfg) is None
            and all(A.build_pack(k, cfg) is not None for k in A.PACK_KEYS),
            f"keys={A.PACK_KEYS} unknown=None",
        )
    )


def check_local_ai_autoconfig(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """下载完自动把 llama-server 路径写进配置。

    守的是 2026-10-27 那次反馈本身：文件下好了、后端也确实找得到，但配置里
    `server_path` 仍是 null ⇒ 设置页显示一个空输入框 ⇒ 用户据此判断「没配上」。
    「留空 = 自动查找」这个约定如果只活在代码里，界面就得把它画出来，
    并且在下完之后主动填进去。
    """
    import os

    from app.core import ai_backend
    from app.models.config import Config

    home = Path(tmp) / "autocfg"
    vdir = home / "models" / "qwen3-vl-2b"
    (vdir / "llama").mkdir(parents=True, exist_ok=True)
    server = vdir / "llama" / ("llama-server.exe" if os.name == "nt" else "llama-server")
    server.write_bytes(b"x")
    (vdir / "Qwen3-VL-2B-Instruct-Q4_K_M.gguf").write_bytes(b"x")
    (vdir / "mmproj-BF16.gguf").write_bytes(b"x")

    cfg = Config.from_dict(
        {"paths": {"model_dir": str(home / "models")}, "local_ai": {"enabled": False}},
        root=home,
    )
    # 前提：落点必须真的是我们造的那个目录，否则下面几条全在测空气
    same_dir = cfg.vision_model_path() == vdir
    r = ai_backend.autoconfigure(cfg, enable=True)
    judgements.append(
        (
            "自动配置：下完补 server_path 并启用本地推理",
            same_dir
            and r["changed"].get("server_path") == str(server)
            and cfg.local_ai.server_path == str(server)
            and cfg.local_ai.enabled is True,
            f"dir_ok={same_dir} changed={r['changed']} enabled={cfg.local_ai.enabled}",
        )
    )

    r2 = ai_backend.autoconfigure(cfg, enable=True)
    judgements.append(
        (
            "自动配置：已配好时不再改写（幂等，可每次启动跑）",
            r2["changed"] == {},
            f"changed={r2['changed']}",
        )
    )

    custom = str(home / "custom" / "llama-server.exe")
    cfg3 = Config.from_dict(
        {
            "paths": {"model_dir": str(home / "models")},
            "local_ai": {"server_path": custom},
        },
        root=home,
    )
    r3 = ai_backend.autoconfigure(cfg3, enable=True)
    judgements.append(
        (
            "自动配置：绝不覆盖用户手填的路径",
            "server_path" not in r3["changed"] and cfg3.local_ai.server_path == custom,
            f"changed={r3['changed']} value={cfg3.local_ai.server_path}",
        )
    )

    # 手填的路径失效时必须继续往下找。早先这里直接 return None，
    # 表现为「文件明明在、界面说找不到」——配置里一行过期绝对路径就能触发。
    found = ai_backend._find_server(cfg3.local_ai, vdir)
    judgements.append(
        (
            "路径失配：手填路径不存在时回退自动查找（不是判死）",
            found == server,
            f"found={found} 期望={server.name}",
        )
    )

    empty = home / "empty"
    (empty / "llama").mkdir(parents=True, exist_ok=True)
    cfg4 = Config.from_dict({"local_ai": {"model_dir": str(empty)}}, root=home)
    r4 = ai_backend.autoconfigure(cfg4, enable=True)
    judgements.append(
        (
            "自动配置：文件不全时不启用，并说清缺什么",
            r4["ready"] is False and r4["changed"] == {} and "缺" in r4["summary"],
            f"ready={r4['ready']} changed={r4['changed']} summary={r4['summary']}",
        )
    )

    # ---- 下载完成事件：必须在 emit 之前就填好 ----
    # 前端收到 `model_download_done` 会立刻拉一次 `get_config`。若自动配置放在
    # emit 之后，那次刷新读到的仍是空路径 —— 于是「下完了仍然是空框」原样复现。
    import io as _io

    from app.models.config import Config as _Cfg
    from app.rpc import Protocol as _Proto
    from app.rpc import Worker as _Worker

    home2 = Path(tmp) / "autocfg_event"
    vdir2 = home2 / "models" / "qwen3-vl-2b"
    (vdir2 / "llama").mkdir(parents=True, exist_ok=True)
    (vdir2 / "llama" / "llama-server.exe").write_bytes(b"x")
    (vdir2 / "m.gguf").write_bytes(b"x")
    (vdir2 / "mmproj-b.gguf").write_bytes(b"x")
    cfg5 = _Cfg.from_dict(
        {
            "paths": {"model_dir": str(home2 / "models"), "work_dir": str(home2 / "work")},
            "local_ai": {"enabled": False},
        },
        root=home2,
    )
    cfg5_path = home2 / "config.yaml"
    cfg5.save(cfg5_path)
    buf = _io.StringIO()
    worker = _Worker(cfg5, _Proto(buf), config_path=cfg5_path)
    worker._on_download_event(
        "model_download_done",
        {"pack": "local_ai", "total_bytes": 0, "detail": "测试", "verified": True},
    )
    frames = [json.loads(ln) for ln in buf.getvalue().splitlines() if ln.startswith("{")]
    done = next((f for f in frames if f.get("name") == "model_download_done"), {})
    saved = cfg5_path.read_text(encoding="utf-8")
    judgements.append(
        (
            "下载完成即自动配置：事件带 autoconfigured 且配置已落盘",
            bool(done.get("data", {}).get("autoconfigured", {}).get("server_path"))
            and "server_path: " in saved
            and "server_path: null" not in saved,
            f"autoconfigured={done.get('data', {}).get('autoconfigured')}",
        )
    )


def check_author_listing(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """`list_author` 的归一化：**只有拼得出可下载地址的条目才允许出现**。

    用一个假的 yt-dlp（真跑 `--flat-playlist -J` 需要网络，判据不能依赖网络）
    喂三种难处理的输入：
    1. 正常的绝对 url
    2. 只有 id、没有 url 的条目（必须按平台拼 `watch?v=`）
    3. 嵌套播放列表（必须跳过，不能列成一条"视频"）
    4. 既没 url 也拼不出的（必须丢弃，否则用户勾了却下不了）
    """
    import json
    import shutil
    import subprocess
    import sys as _sys

    from app.platforms.ytdlp import list_author_with_ytdlp

    payload = {
        "_type": "playlist",
        "title": "示例频道",
        "playlist_count": 4,
        "entries": [
            {"id": "aaa111", "url": "https://www.youtube.com/watch?v=aaa111", "title": "第一条", "duration": 61},
            {"id": "bbb222", "title": "第二条（只有 id）", "duration": 62},
            {"_type": "playlist", "id": "PLxxx", "title": "嵌套列表", "url": "https://x/PLxxx"},
        ],
    }
    fake = Path(tmp) / "fake_ytdlp"
    fake.mkdir(parents=True, exist_ok=True)
    js = fake / "fake.py"
    js.write_text(
        "import sys, json\nprint(json.dumps(" + repr(payload) + "))\n",
        encoding="utf-8",
    )
    bat = fake / "fake-yt-dlp.bat"
    bat.write_text(
        f'@echo off\r\n"{_sys.executable}" "{js}" %*\r\n',
        encoding="ascii",
    )

    try:
        page = list_author_with_ytdlp(
            "https://www.youtube.com/@demo", "youtube", ytdlp_path=str(bat)
        )
    except Exception as exc:  # noqa: BLE001
        judgements.append(
            ("list_author 归一化（假 yt-dlp）", False, f"{type(exc).__name__}: {exc}")
        )
        return

    urls = [i.url for i in page.items]
    ok = (
        len(page.items) == 2  # 正常 1 + 只有 id 1；嵌套那个被跳过
        and "https://www.youtube.com/watch?v=aaa111" in urls
        and "https://www.youtube.com/watch?v=bbb222" in urls  # 由 id 拼出
        and page.title == "示例频道"
    )
    judgements.append(
        (
            "list_author 归一化：拼地址 / 跳嵌套 / 丢无地址",
            ok,
            f"{len(page.items)} 条 {urls}",
        )
    )

    # limit 生效且 `truncated` 如实上报（UI 要显示"还有更多"）
    page2 = list_author_with_ytdlp(
        "https://www.youtube.com/@demo", "youtube", ytdlp_path=str(bat), limit=1
    )
    judgements.append(
        (
            "list_author 的 limit 与 truncated",
            len(page2.items) == 1 and page2.truncated is True,
            f"条数={len(page2.items)} truncated={page2.truncated}",
        )
    )


def check_download_dispatch(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """`fetcher.download()` **真的**按平台分发了（本地文件不递归）。

    这条是 2026-10-26 那次事故的回归守卫：平台层建好了，但 `pipeline` 调的
    `fetcher.download()` 内部仍是老的「if 本地 else yt-dlp + 抖音 cookie 硬拦」，
    于是抖音的浏览器取流**在生产里从没被调用过**，用户看到的是"重新采集 cookie"。

    用本地文件测最划算：离线、0.3 秒、且一旦分发写成"回调自己"就会
    RecursionError（而不是静默走错分支）—— 那种错在联网测试里也看不出来。
    """
    from app.core import fetcher
    from app.models.config import Config
    from app.models.task import Task

    src = Path(tmp) / "dispatch-probe.mp4"
    src.write_bytes(b"\x00" * 2048)
    out = Path(tmp) / "dispatch-out"
    out.mkdir(parents=True, exist_ok=True)

    try:
        res = fetcher.download(Task(url=str(src), platform="local"), Config.from_dict({}, root=Path(tmp)), out)
        ok = res.from_local and res.video_path.is_file()
        detail = f"from_local={res.from_local} 产物={res.video_path.name}"
    except RecursionError:
        ok, detail = False, "RecursionError：provider 回调了 fetcher.download() 自己"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"

    judgements.append(("fetcher.download 按平台分发（本地不递归）", ok, detail))


def check_summarizer(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """汇总阶段：跳过条件、分块边界、请求体结构、以及"未配置也要置 done"。

    这里的每一条都对应一个**用户会误解**的现象：
    - 没配 key 却没提示 → 用户以为模型没干活（其实压根没调用）
    - 分块把 `[mm:ss] 文本` 切断 → 章节大纲的时间戳全乱
    - 未配置时卡在 `framed` 不置 done → 任务列表永远有个"待汇总"的僵尸任务
    """
    from app.core import summarizer as S
    from app.models.config import Config

    # ---- 跳过条件：开关与后端是两件事（2026-10-27 改）----
    #
    # 旧判据要求「没密钥 → skip_reason 报错」。本地模型上线后这条**不再成立**：
    # 没有云端密钥、但本地 GGUF 在，照样能汇总。所以判据拆成两半：
    # 开关关着 → skip_reason 有话说；开关开着 → skip_reason 闭嘴，
    # 「到底能不能调」交给 ai_backend.resolve()。
    from app.core import ai_backend

    off = Config.from_dict({"summarize": {"enabled": False}}, root=Path(tmp))
    on_no_key = Config.from_dict({"summarize": {"enabled": True, "api_key_env": "VIDIGEST_NO_SUCH_KEY"}}, root=Path(tmp))
    import os as _os

    _os.environ["VIDIGEST_SMOKE_KEY"] = "sk-x"
    try:
        ready = Config.from_dict(
            {"summarize": {"enabled": True, "api_key_env": "VIDIGEST_SMOKE_KEY"}}, root=Path(tmp)
        )
        reasons = (S.skip_reason(off), S.skip_reason(on_no_key), S.skip_reason(ready))
        no_key_backend, no_key_why = ai_backend.resolve(on_no_key)
        ready_backend, _ = ai_backend.resolve(ready)
    finally:
        _os.environ.pop("VIDIGEST_SMOKE_KEY", None)
    judgements.append(
        (
            "汇总开关：关着报「未启用」，开着不再因缺密钥而跳过（本地可接管）",
            "未启用" in (reasons[0] or "")
            and reasons[1] is None
            and reasons[2] is None,
            f"skip_reason={reasons}",
        )
    )
    judgements.append(
        (
            "选路：本地关 + 无密钥 → 无后端，且原因同时点出两边缺什么",
            no_key_backend is None
            and "本地模型未启用" in no_key_why
            and "为空" in no_key_why,
            f"{no_key_why}",
        )
    )
    judgements.append(
        (
            "选路：有密钥时走 cloud，model 取自配置",
            ready_backend is not None
            and ready_backend.name == "cloud"
            and ready_backend.model == ready.summarize.model,
            f"{ready_backend}",
        )
    )

    # ---- 分块：不切断行、块数合理、超长行硬切 ----
    lines = [f"[{i:02d}:{(i*7) % 60:02d}] 这是第 {i} 行文本，长度大约几十个字符。\n" for i in range(600)]
    text = "".join(lines)
    chunks = S.split_chunks(text, chunk_chars=4000)
    intact = all(all(ln.startswith("[") for ln in c.splitlines() if ln.strip()) for c in chunks)
    long_line = "x" * 9000 + "\n"
    hard = S.split_chunks(long_line, chunk_chars=4000)
    judgements.append(
        (
            "汇总分块：按行切（不切断时间戳行）、超长行硬切",
            intact and len(chunks) > 1 and all(len(c) <= 4100 for c in chunks) and len(hard) >= 2,
            f"{len(chunks)} 块；行完整={intact}；超长行切成 {len(hard)} 块",
        )
    )

    # ---- 代码围栏：本地模型会把整份 Markdown 包进 ```markdown ----
    #
    # 这不是洁癖：原样落盘的话 summary.md 在渲染器里整段变成灰色代码块，
    # 标题层级全失效，用户看到的是「一堆带 ``` 的原文」。
    fenced_closed = "```markdown\n## 结论\n内容\n```"
    fenced_open = "```markdown\n## 结论\n内容"          # 被 max_tokens 截断，结尾没闭合
    plain = "## 结论\n内容"
    has_code = "## 结论\n```python\nprint(1)\n```\n结束"
    stripped = (
        S._strip_fence(fenced_closed),
        S._strip_fence(fenced_open),
        S._strip_fence(plain),
        S._strip_fence(has_code),
    )
    judgements.append(
        (
            "汇总产物：剥掉外层代码围栏（闭合与未闭合都要剥），正文代码块不动",
            stripped[0].startswith("## 结论")
            and "```" not in stripped[0]
            and stripped[1].startswith("## 结论")
            and "```" not in stripped[1]
            and stripped[2] == plain
            and stripped[3] == has_code,
            f"closed={stripped[0][:12]!r} open={stripped[1][:12]!r} "
            f"plain_ok={stripped[2] == plain} code_intact={stripped[3] == has_code}",
        )
    )

    # ---- 未配置时：run_summarize 直接置 done、不写 summary.md ----
    from app.core.store import Store
    from app.core import pipeline
    from app.models.task import Task, TaskStatus

    work = Path(tmp) / "sum-work"
    work.mkdir(parents=True, exist_ok=True)
    tr = work / "transcript.txt"
    tr.write_text("[00:00] 测试\n", encoding="utf-8")
    store = Store(Path(tmp) / "sum-state.db")
    try:
        task, _ = store.add(
            Task(url="https://example.com/x", platform="unknown", title="t", out_dir=str(work))
        )
        task.transcript_path = str(tr)
        store.set_status(task, TaskStatus.FRAMED)
        pipeline.run_summarize(task, off, store)
        status = store.get(task.id).status
        wrote = (work / "summary.md").exists()
        judgements.append(
            (
                "汇总未配置：任务照常 done，且不写 summary.md",
                status == TaskStatus.DONE.value and not wrote,
                f"status={status} 写了文件={wrote}",
            )
        )
    finally:
        store.close()


def check_ai_backend(tmp: str, judgements: list[tuple[str, bool, str]]) -> None:
    """本地优先 / 云兜底 / 画面理解降级。

    这三条守的是同一件事：**AI 能力是增强，不是主链路**。
    任何一个环节坏掉，转写稿照样要在、任务照样要 `done`。
    """
    import os

    from app.core import ai_backend, vision
    from app.models.config import Config

    # ---- 本地起不来 → 回退云端，而不是直接没有后端 ----
    vdir = Path(tmp) / "vlm"
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "Qwen3-VL-2B-Instruct-Q4_K_M.gguf").write_bytes(b"x")
    (vdir / "mmproj-BF16.gguf").write_bytes(b"x")
    fake_server = Path(tmp) / ("fake-llama-server.exe" if os.name == "nt" else "fake-llama-server")
    fake_server.write_bytes(b"this is not a runnable binary")

    os.environ["VIDIGEST_AI_KEY"] = "sk-x"
    try:
        cfg = Config.from_dict(
            {
                "local_ai": {
                    "enabled": True,
                    "server_path": str(fake_server),
                    "model_dir": str(vdir),
                    "startup_timeout": 1.0,
                    "port": 8123,
                },
                "summarize": {
                    "enabled": True,
                    "api_key_env": "VIDIGEST_AI_KEY",
                    "model": "fallback-model",
                },
            },
            root=Path(tmp),
        )
        backend, why = ai_backend.resolve(cfg)
    finally:
        os.environ.pop("VIDIGEST_AI_KEY", None)

    judgements.append(
        (
            "本地优先：本地起不来时回退云端（不是直接判定无后端）",
            backend is not None and backend.name == "cloud" and backend.model == "fallback-model",
            f"backend={backend} local_reason={why!r}",
        )
    )

    # ---- 诊断信息：只报状态，不拉起进程 ----
    # 为什么单列一条：如果 describe() 顺手启动了 llama-server，那么「打开设置页」
    # 就会吃掉 3 GiB 显存，而用户可能只是想看一眼开关在哪。
    info = ai_backend.describe(cfg)
    judgements.append(
        (
            "后端诊断：文件齐备时报 local，但不启动进程",
            info["local_ready"] is True
            and info["local_running"] is False
            and info["active"] == "local",
            f"ready={info['local_ready']} running={info['local_running']} active={info['active']}",
        )
    )

    # ---- 拼版图时间范围来自索引，不由模型猜 ----
    index = Path(tmp) / "sheets" / "frames_index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    frames = [{"file": f"frame_{i + 1:04d}.jpg", "t": i * 10.0, "time": f"{(i * 10) // 60:02d}:{(i * 10) % 60:02d}"} for i in range(48)]
    index.write_text(json.dumps({"sheets": ["sheet_01.jpg", "sheet_02.jpg"], "frames": frames}), encoding="utf-8")
    spans = vision._sheet_spans(index, 2)
    judgements.append(
        (
            "画面理解：每张拼版图的时间范围由索引算出（48 帧 → 2 张各 24 帧）",
            len(spans) == 2
            and spans[0][0] == "00:00"
            and spans[0][2] == 24
            and spans[1][2] == 24
            and spans[1][0] == "04:00",
            f"{spans[0][:2]} 格数{spans[0][2]} / {spans[1][:2]} 格数{spans[1][2]}",
        )
    )

    # ---- 索引缺失时降级，不抛错 ----
    degraded = vision._sheet_spans(Path(tmp) / "no-such-index.json", 2)
    judgements.append(
        (
            "画面理解：索引缺失时降级为「未知时间范围」，不抛错",
            len(degraded) == 2 and degraded[0][0] == "未知" and degraded[0][2] == 24,
            f"{degraded}",
        )
    )


def check_version_consistency(judgements: list[tuple[str, bool, str]]) -> None:
    """5 处版本号必须一致（VERSION / tauri.conf / package.json / Cargo.toml / rpc.py）。

    为什么用子进程跑 `bump_version.py --check` 而不是在这里重写一遍正则：
    校验逻辑只应该有一份。脚本改了、判据自动跟着改；若在这里抄一份，
    两份迟早对不上，而「版本不一致」恰恰是那种**没人会主动去看**的问题。
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "bump_version.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()
    judgements.append(
        (
            "版本号 5 处一致（VERSION / tauri / package / Cargo / rpc）",
            proc.returncode == 0,
            tail[-1] if tail else f"exit={proc.returncode}",
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
    check_version_consistency(host.judgements)
    check_platform_registry(host.judgements)
    check_download_dispatch(tmp, host.judgements)
    check_summarizer(tmp, host.judgements)
    check_asset_packs(tmp, host.judgements)
    check_ai_backend(tmp, host.judgements)
    check_local_ai_autoconfig(tmp, host.judgements)
    check_author_listing(tmp, host.judgements)

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
