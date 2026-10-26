"""AI 后端选择：**本地 llama-server 优先，云 API 兜底**。

## 为什么需要这一层

汇总（`summarizer.py`）和画面理解（`vision.py`）都要「调一个会说人话的模型」。
2026-10-27 之前这条路径硬绑在 `summarize.base_url` 上 —— 没配 key 就等于没有 AI
能力。但本地跑一个 Qwen3-VL-2B（GGUF + llama.cpp）实测只要 1.8 秒/张、
3.2 GiB 显存，而且 **llama-server 原生就是 OpenAI 兼容接口**。

于是「本地」和「云」在代码上是同一条路，差别只有一个 `base_url`。
这一层的职责就是把「这次用谁」这件事**收敛到一个地方**，而不是让每个调用方
自己写一遍 if/else —— 那种写法迟早会出现「vision 走了本地、summarize 走了云、
两边日志格式还不一样」的分裂。

## 选择顺序

1. `local_ai.enabled` 且 llama-server 能起来（或端口上已有健康的） → **local**
2. 否则有 `summarize.api_key_env` 环境变量 → **cloud**
3. 都没有 → 返回 `None` 并给出**具体原因**（谁没开、缺哪个变量）

第 3 步的「具体原因」是刻意的：只说「没有可用后端」会让用户去翻十分钟配置。

## 关于常驻

llama-server 是**常驻进程**，不是每次调用起一个：冷启动 2.6 秒，一条视频要看
3–6 张图，逐次重启会把 10 秒的活干成 20 秒。所以这里做进程管理，
并在 `shutdown()` 里清理（`rpc.serve()` 退出时调）。

⚠️ worker 被强杀时 `shutdown()` 不会执行，llama-server 会成为孤儿进程。
这不是漏洞而是设计：它监听固定端口，下次启动 `reuse_running` 会直接复用，
既不浪费也不冲突。真需要清理就 `taskkill /IM llama-server.exe`。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

#: llama-server 的上下文窗口。一张 1920×792 的 24 宫格拼版图约占 1500 token，
#: 8192 够放「图片 + 提示词 + 400 token 输出」。2B 模型的原生窗口很大，
#: 但开太大会明显多吃显存，而画面理解不需要长上下文。
CONTEXT_SIZE = 8192

#: 健康检查的超时（秒）。本地回环，正常是毫秒级。
_HEALTH_TIMEOUT = 2.0


@dataclass(frozen=True)
class Backend:
    """一次 AI 调用的落点。"""

    name: str  #: "local" | "cloud"
    base_url: str
    model: str
    api_key: str = ""

    @property
    def is_local(self) -> bool:
        return self.name == "local"

    def describe(self) -> str:
        return f"{self.name}({self.model})"


def api_key(cfg: object) -> str:
    """从环境变量取云端密钥。**永远不从配置文件读**（IPC 规格 §3.2）。"""
    env_name = getattr(getattr(cfg, "summarize", None), "api_key_env", "") or "VIDIGEST_API_KEY"
    return (os.environ.get(env_name) or "").strip()


# ---------------------------------------------------------------- 本地服务


class LocalServer:
    """llama-server 的进程管理。全局唯一（同一端口没办法起两个）。"""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._base: str | None = None
        self._log = None
        self._lock = threading.Lock()
        #: 上次失败的原因，给 `resolve()` 拼提示用
        self.last_error: str = ""

    # ---- 对外 ----

    def ensure(self, cfg: object) -> str | None:
        """确保本地服务可用，返回 base_url；不可用返回 None（原因见 `last_error`）。"""
        la = getattr(cfg, "local_ai", None)
        if la is None or not getattr(la, "enabled", False):
            self.last_error = "本地模型未启用（设置 → 本地模型 → 启用）"
            return None
        port = int(getattr(la, "port", 8099))
        base = f"http://127.0.0.1:{port}"

        with self._lock:
            # 自己起的进程还活着 → 直接返回
            if self._proc is not None and self._proc.poll() is None and self._base == base:
                return base

            # 端口上已有健康的 server（上次残留 / 用户手动起的）
            if getattr(la, "reuse_running", True) and _healthy(base):
                logging.info("复用已在运行的本地模型服务：%s", base)
                self._base = base
                self._proc = None
                return base

            return self._start(cfg, base, port)

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self._base = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                logging.warning("本地模型服务未响应 terminate，已强杀")
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None
        logging.info("本地模型服务已停止")

    # ---- 内部 ----

    def _start(self, cfg: object, base: str, port: int) -> str | None:
        la = cfg.local_ai
        model_dir = cfg.vision_model_path()
        server = _find_server(la, model_dir)
        if server is None:
            self.last_error = (
                f"找不到 llama-server（可在设置里指定路径，或放到 {model_dir / 'llama'}）"
            )
            return None
        model, mmproj = _find_weights(model_dir)
        if model is None:
            self.last_error = f"视觉模型未下载：{model_dir}"
            return None

        cmd = [
            str(server), "-m", str(model),
            "-ngl", str(int(getattr(la, "gpu_layers", 99))),
            "-c", str(CONTEXT_SIZE), "-np", "1",
            "--host", "127.0.0.1", "--port", str(port),
        ]
        if mmproj is not None:
            cmd[3:3] = ["--mmproj", str(mmproj)]

        log_path = model_dir / "llama-server.log"
        try:
            model_dir.mkdir(parents=True, exist_ok=True)
            self._log = log_path.open("w", encoding="utf-8", errors="replace")
        except OSError as exc:
            self.last_error = f"无法写日志 {log_path}：{exc}"
            return None

        logging.info("启动本地模型服务：%s（%s）", server.name, model.name)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                # Windows 上不让子进程继承控制台信号，避免 Ctrl-C 传到它
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self.last_error = f"启动 llama-server 失败：{exc}"
            self._log.close()
            self._log = None
            return None

        timeout = float(getattr(la, "startup_timeout", 120.0))
        ready = _wait_ready(base, proc, timeout)
        if not ready:
            tail = _tail_log(log_path)
            if proc.poll() is not None:
                hint = ""
                if "bind" in tail.lower() or "port" in tail.lower():
                    # 端口冲突是最常见的一种，而且用户自己完全看得出来
                    # 「换个端口」怎么办 —— 前提是错误信息里得写出来。
                    hint = f"（端口 {port} 可能已被占用，可在设置里换一个）"
                self.last_error = (
                    f"llama-server 启动后退出（退出码 {proc.returncode}）{hint}："
                    f"{tail or '日志无输出'}。详见 {log_path}"
                )
            else:
                self.last_error = f"llama-server 在 {timeout:.0f} 秒内未就绪，详见 {log_path}"
            proc.terminate()
            self._log.close()
            self._log = None
            return None

        self._proc = proc
        self._base = base
        self.last_error = ""
        logging.info("本地模型服务就绪：%s → %s", base, model.name)
        return base


_SERVER: LocalServer | None = None
_SERVER_LOCK = threading.Lock()


def local_server() -> LocalServer:
    global _SERVER
    with _SERVER_LOCK:
        if _SERVER is None:
            _SERVER = LocalServer()
            atexit.register(_SERVER.stop)
        return _SERVER


def shutdown() -> None:
    """停止本地服务。`rpc.serve()` 退出时调用（幂等）。"""
    global _SERVER
    with _SERVER_LOCK:
        server, _SERVER = _SERVER, None
    if server is not None:
        server.stop()


# ---------------------------------------------------------------- 选择


def resolve(cfg: object) -> tuple[Backend | None, str]:
    """选定这次用谁。返回 `(backend, 不可用原因)`，二者必有一个为空。

    调用方约定：拿到 `None` 时**不要**抛异常 —— 汇总和画面理解都是可选能力，
    该跳过就跳过，把 `reason` 写进日志。
    """
    la = getattr(cfg, "local_ai", None)
    if la is not None and getattr(la, "enabled", False):
        server = local_server()
        base = server.ensure(cfg)
        if base:
            model, _ = _find_weights(cfg.vision_model_path())
            name = model.stem if model is not None else "local"
            return Backend("local", base, name), ""
        local_reason = server.last_error or "本地模型不可用"
    else:
        local_reason = "本地模型未启用"

    sm = getattr(cfg, "summarize", None)
    if sm is not None:
        key = api_key(cfg)
        if key:
            base = (getattr(sm, "base_url", "") or "").strip().rstrip("/")
            if base:
                return Backend("cloud", base, getattr(sm, "model", "gpt-4o"), key), ""
            return None, f"{local_reason}；云端 base_url 为空"
        env_name = getattr(sm, "api_key_env", None) or "VIDIGEST_API_KEY"
        return None, f"{local_reason}；环境变量 {env_name} 为空"
    return None, local_reason


# ---------------------------------------------------------------- 自动配置


def autoconfigure(cfg: object, *, enable: bool = False) -> dict:
    """把「文件已经下好了、但配置里还是空」的本地模型路径**补进配置对象**。

    ## 为什么要有这一步

    `server_path` / `model_dir` 留空本来是「自动查找」，但它在设置界面上就是一个
    空输入框。用户花两分钟下完 2.3 GiB，回来看到空框，唯一合理的理解是
    「没配置成功」—— 2026-10-27 收到的反馈正是这句。**默认值应当自己落进配置，
    而不是要求用户理解「留空 = 自动」这个约定。**

    ## 边界

    - **只补空值**，绝不覆盖用户填过的路径 —— 那是他的选择，哪怕我们觉得另一条更好
    - 补的路径必须在「我们下载的那个落点」（`<视觉模型目录>/llama/`）里，
      不把 PATH 里来路不明的 `llama-server` 钉进配置（见 `server_in_dir()`）
    - `enable=True` 才动 `local_ai.enabled`。**只在「用户刚点完下载」时传 True**：
      那时意图明确（他就是要用本地模型）；启动时替用户改开关是越界
    - 不写盘、不启动进程 —— 落盘由调用方决定（`rpc.py` 里 `Config.save()`）

    返回 `{"changed": {...}, "summary": "...", "ready": bool}`。
    `changed` 为空表示「本来就已经配好了」，调用方据此决定要不要写文件。
    """
    la = getattr(cfg, "local_ai", None)
    changed: dict[str, object] = {}
    if la is None:
        return {"changed": changed, "summary": "配置里没有 local_ai 段", "ready": False}

    model_dir = cfg.vision_model_path()
    model, mmproj = _find_weights(model_dir)
    server = server_in_dir(model_dir)
    ready = server is not None and model is not None

    if not getattr(la, "server_path", None) and server is not None:
        la.server_path = str(server)  # type: ignore[attr-defined]
        changed["server_path"] = str(server)
    if enable and not getattr(la, "enabled", False) and ready:
        la.enabled = True  # type: ignore[attr-defined]
        changed["enabled"] = True

    if not ready:
        missing = []
        if server is None:
            missing.append("llama/llama-server.exe")
        if model is None:
            missing.append("*.gguf 主权重")
        if mmproj is None:
            missing.append("mmproj-*.gguf")
        summary = f"视觉模型目录还不完整：{model_dir}（缺 {'、'.join(missing)}）"
    elif changed:
        summary = f"已自动配置本地模型：{changed.get('server_path', getattr(la, 'server_path', ''))}"
        if changed.get("enabled"):
            summary += "，并已启用本地推理"
    else:
        summary = f"本地模型已就位：{getattr(la, 'server_path', '')}"

    return {"changed": changed, "summary": summary, "ready": ready}


# ---------------------------------------------------------------- 调用


def chat(
    backend: Backend,
    messages: list[dict],
    *,
    max_tokens: int = 2000,
    temperature: float = 0.3,
    timeout: int = 180,
    repeat_penalty: float | None = None,
) -> str:
    """发一次 `/chat/completions`，返回正文文本。异常向上抛，由调用方决定怎么处理。

    `repeat_penalty` 只对本地小模型有意义：2B 在长中文输出上会重复到占满
    token 上限（实测连出 60 多个箭头），给 1.15 即可。走云端时传 None 不加这个字段 ——
    有些云服务不认识它，会直接 400。
    """
    import requests

    payload: dict = {
        "model": backend.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if repeat_penalty is not None:
        payload["repeat_penalty"] = repeat_penalty
    headers = {"Content-Type": "application/json"}
    if backend.api_key:
        headers["Authorization"] = f"Bearer {backend.api_key}"

    resp = requests.post(
        f"{backend.base_url}/chat/completions", headers=headers, json=payload, timeout=timeout
    )
    if resp.status_code != 200:
        # 带出服务端的原文：401（密钥）/ 402（余额）/ 404（模型名）处置完全不同
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return str(resp.json()["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f"响应结构不认识：{resp.text[:300]}") from exc


def describe(cfg: object) -> dict:
    """诊断信息，给 `get_config` / 设置界面显示「现在会用哪个后端」。**不启动进程。**"""
    la = getattr(cfg, "local_ai", None)
    enabled = bool(la is not None and getattr(la, "enabled", False))
    model_dir = cfg.vision_model_path()
    server = _find_server(la, model_dir) if enabled and la is not None else None
    model, mmproj = _find_weights(model_dir)
    base = f"http://127.0.0.1:{int(getattr(la, 'port', 8099))}" if la is not None else ""
    running = bool(enabled and _healthy(base))
    has_key = bool(api_key(cfg))

    if enabled and server is not None and model is not None:
        ready = True
    else:
        ready = False
    if ready:
        active = "local"
    elif has_key:
        active = "cloud"
    else:
        active = "none"
    return {
        "local_enabled": enabled,
        #: 文件层面是否齐备（server + 权重）。**不代表已经验证能跑** ——
        #: 显存不足、CUDA 版本不匹配这些问题只有真启动才会暴露。
        "local_ready": ready,
        "local_server": str(server) if server else None,
        "local_model": str(model) if model else None,
        "local_mmproj": str(mmproj) if mmproj else None,
        "local_model_dir": str(model_dir),
        #: 端口上是否已经有一个健康的 server 在跑（只探测，不启动）
        "local_running": running,
        "cloud_key_present": has_key,
        "active": active,
    }


# ---------------------------------------------------------------- 辅助


def _healthy(base: str) -> bool:
    """`/health` 返回 200 才算健康。连不上 / 超时一律当不可用。"""
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=_HEALTH_TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if proc.poll() is not None:
            return False
        if _healthy(base):
            return True
        time.sleep(0.4)
    return False


def _tail_log(log_path: Path, limit: int = 400) -> str:
    """从 llama-server 日志里取「为什么起不来」。

    为什么必须做：`llama-server` 把失败原因**只写进日志**，退出码永远是 1。
    界面上只说「启动后退出（退出码 1），详见 <路径>」，等于把用户丢给一个文件 ——
    而实际原因往往只有一句（端口被占、模型文件损坏、CUDA 版本不匹配），
    并且那一句就在最后几行里。
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    # llama.cpp 的格式是 `0.00.787.496 E srv  ...`；取最后几条 E 行，
    # 没有 E 行就退回最后几行（有些错误只打 W 或纯文本）。
    errs = [ln for ln in lines if " E " in ln or ln.startswith("error") or "Error" in ln]
    pick = (errs or lines)[-3:]
    return " / ".join(pick)[:limit]


def server_in_dir(model_dir: Path) -> Path | None:
    """模型目录下 `llama/` 里的 llama-server。**只认这个落点**，不查 PATH。

    单独抽出来给 `autoconfigure()` 用：写进配置的路径必须是「我们下载的那个」，
    不能把 PATH 里某个来路不明的 `llama-server` 钉进用户配置 —— 那种路径在
    换台机器 / 卸载后又变成悬空指向，比留空更难排查。
    """
    names = ("llama-server.exe", "llama-server") if os.name == "nt" else ("llama-server",)
    for name in names:
        cand = Path(model_dir) / "llama" / name
        if cand.is_file():
            return cand
    return None


def _find_server(la: object, model_dir: Path) -> Path | None:
    """按「显式路径 → 模型目录下的 llama/ → PATH」找 llama-server。

    **显式路径失效时必须继续往下找**：`server_path` 是用户填过一次就会一直留着的值，
    而模型目录可能被搬家、被卸载、被换盘。早先这里 `return None`，于是
    「配置里有一行过期的绝对路径」会表现成「明明文件在、界面说找不到」。
    """
    explicit = getattr(la, "server_path", None) if la is not None else None
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        logging.warning("local_ai.server_path 指向的文件不存在：%s（改为自动查找）", p)
    found = server_in_dir(model_dir)
    if found is not None:
        return found
    import shutil

    names = ("llama-server.exe", "llama-server") if os.name == "nt" else ("llama-server",)
    for name in names:
        hit = shutil.which(name)
        if hit:
            return Path(hit)
    return None


def _find_weights(model_dir: Path) -> tuple[Path | None, Path | None]:
    """在目录里认出「主权重」与「视觉投影器」。

    判据是文件名里的 `mmproj`：llama.cpp 的 clip 投影器统一这么命名，
    主权重则不带。**不能按大小猜** —— Q8 的 mmproj 可能比 IQ1 的主权重还大。
    """
    if not model_dir.is_dir():
        return None, None
    main: Path | None = None
    mmproj: Path | None = None
    for p in sorted(model_dir.glob("*.gguf")):
        if "mmproj" in p.name.lower():
            mmproj = p if mmproj is None else mmproj
        elif main is None:
            main = p
    return main, mmproj
