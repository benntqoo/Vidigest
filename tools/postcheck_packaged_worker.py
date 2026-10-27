"""对**打包后的** worker 产物做一次新命令实测（不属于常驻判据，只是出包后的一次性核对）。

判据：
  1. get_config 有响应且带 model_present / read_only_fields
  2. set_config 拒绝写 api_key（E_API_KEY_NOT_ALLOWED）
  3. set_config 未知 section 被丢弃并回 warnings（不写盘）
  4. verify_env 可被触发（收到 env_verify_started，随后 env_verified）
  5. add_tasks 把「抖音搜索结果页」链接归一成 /video/<id>（0.6.5）

判据 5 存在的理由：源码判据全绿 ≠ 产物里有这个功能。归一化在纯 Python 模块
`app/core/urls.py` 里，PyInstaller 打的是**旧版本**或漏收模块时，`tools/urls_selftest.py`
46 条照样全过，而用户装完点下载仍是那句 `Unsupported URL`。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "build" / "pyinstaller" / "vidigest-worker" / "vidigest-worker.exe"

PY = Path(sys.executable)

#: 2026-10-27 用户实测的那条（搜索结果页 + modal_id），逐字复制。
#: 它的 `added[].url` 必须是 `…/video/<id>`，platform 必须是 douyin。
_ACCIDENT_URL = (
    "https://www.douyin.com/search/%E5%87%AF%E5%BF%83"
    "?aid=96805f84-a83e-43e3-a57a-ce4331044fdf"
    "&modal_id=7700460182866331855&type=general"
)
_ACCIDENT_CANON = "https://www.douyin.com/video/7700460182866331855"


class Client:
    def __init__(self, exe: Path, cfg: Path):
        env = dict(os.environ)
        env["VIDIGEST_CONFIG"] = str(cfg)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.p = subprocess.Popen(
            [str(exe)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.q: queue.Queue[dict] = queue.Queue()
        self.err: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.seq = 0

    def _read(self):
        assert self.p.stdout
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except json.JSONDecodeError:
                pass

    def _read_err(self):
        assert self.p.stderr
        for line in self.p.stderr:
            self.err.append(line.rstrip())

    def send(self, cmd: str, args: dict | None = None) -> str:
        self.seq += 1
        req_id = f"r{self.seq}"
        msg = {"v": 1, "type": "cmd", "name": cmd, "args": args or {}, "id": req_id}
        assert self.p.stdin
        self.p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        return req_id

    def wait(self, names, timeout=30.0) -> dict | None:
        """等到任意一个 name 匹配的帧。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                f = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                return None
            name = f.get("name") or ""
            if name in names:
                return f
        return None

    def close(self):
        try:
            self.send("shutdown")
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def main() -> int:
    if not WORKER.is_file():
        print(f"找不到产物：{WORKER}")
        return 2

    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, note: str = ""):
        results.append((name, ok, note))
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}  {note}")

    tmp = Path(tempfile.mkdtemp(prefix="vidigest-postcheck-"))
    cfg = tmp / "config.yaml"
    cfg.write_text(
        "paths:\n  output_dir: " + str(tmp / "out") + "\n"
        "  model_dir: " + str(tmp / "models") + "\n",
        encoding="utf-8",
    )

    c = Client(WORKER, cfg)
    try:
        c.send("hello", {"client": "postcheck", "protocol": 1})
        f = c.wait({"ready", "error"})
        record("hello → ready（产物能起来）", bool(f and (f.get("event") or f.get("name")) == "ready"))

        # 1. get_config
        c.send("get_config")
        f = c.wait({"config", "error"}, timeout=15)
        ev = (f or {}).get("data") or {}
        has_fields = all(k in ev for k in ("model_present", "read_only_fields", "path"))
        record("get_config 带 model_present / read_only_fields", has_fields,
               f"model_present={ev.get('model_present')} ro={len(ev.get('read_only_fields') or [])}")

        # 1b. 语言下拉的选项必须由**产物**下发（前端没有第二份表）
        langs = ev.get("languages") or []
        auto_first = bool(langs) and langs[0].get("code") == ""
        record(
            "get_config 下发 languages（含自动检测）",
            len(langs) >= 100 and auto_first,
            f"{len(langs)} 项，首项={langs[0] if langs else None}",
        )

        # 1c. 资产包清单必须由**产物**下发。
        # 2026-10-27 加这条的原因：`asset_packs.py` 是新模块，PyInstaller 若漏收，
        # 源码判据全绿而产物里 `get_config` 直接缺字段 —— 只有对着 exe 跑才看得见。
        packs = {p.get("key"): p for p in (ev.get("packs") or [])}
        record(
            "get_config 下发 packs（whisper + local_ai 两个包）",
            set(packs) == {"whisper", "local_ai"}
            and all(str(p.get("target_dir") or "").strip() for p in packs.values()),
            f"keys={sorted(packs)} local_ai_target={packs.get('local_ai', {}).get('target_dir')}",
        )

        # 1d. 下载命令认识的 `pack` 参数：未知值必须回 E_BAD_ARGS 而不是静默当 whisper。
        # **不真下载** —— 这里只验参数校验还在产物里。
        c.send("download_model", {"pack": "no_such_pack"})
        f = c.wait({"error"}, timeout=15)
        code = ((f or {}).get("data") or {}).get("code")
        record(
            "download_model 未知 pack → E_BAD_ARGS（不误判成 whisper）",
            code == "E_BAD_ARGS",
            f"code={code}",
        )

        # 1e. `ai` 诊断必须是**产物里**能算出来的。
        # 2026-10-27 加这条：`ai_backend.autoconfigure()` 负责「下完自动填路径」，
        # 若 PyInstaller 漏收模块，源码 70 条全绿而装完的版本里 `get_config` 直接
        # 少一个键（0.6.1 用户看到「下完了却不会自动配置」正是这个症状的近亲）。
        ai = ev.get("ai") or {}
        ai_ok = (
            isinstance(ai, dict)
            and "active" in ai
            and "local_ready" in ai
            and ai.get("active") in ("local", "cloud", "none")
        )
        record(
            "get_config 下发 ai 诊断（本地优先选路可算）",
            ai_ok,
            f"active={ai.get('active')} ready={ai.get('local_ready')} server={ai.get('local_server')}",
        )

        # 1f. **URL 归一化必须在产物里生效**（0.6.5）。
        # db 落在 `cfg.root / "state.db"`，而 cfg 在临时目录里 ⇒ 这里写库是写临时库，
        # 不会碰到用户真实的 state.db（也不会给开着的 GUI 推事件）。
        c.send("add_tasks", {"urls": [_ACCIDENT_URL]})
        f = c.wait({"tasks_added", "error"}, timeout=15)
        added = ((f or {}).get("data") or {}).get("added") or []
        first = added[0] if added else {}
        record(
            "add_tasks 把抖音搜索页链接归一成 /video/<id>",
            first.get("url") == _ACCIDENT_CANON and first.get("platform") == "douyin",
            f"url={first.get('url')} platform={first.get('platform')}",
        )

        # 2. set_config 拒绝 api_key
        # 注意：实现的口径是「丢弃 + 警告」，**不是整体失败**（`app/rpc.py:603-606`）。
        c.send("set_config", {"patch": {"summarize": {"api_key": "sk-should-not-be-written", "enabled": True}}})
        f = c.wait({"config_saved", "error"}, timeout=15)
        sd = (f or {}).get("data") or {}
        warns = sd.get("warnings") or []
        net_has_key = "api_key" in ((sd.get("config") or {}).get("summarize") or {})
        disk_has_key = "sk-should-not-be-written" in cfg.read_text(encoding="utf-8")
        record(
            "api_key 被丢弃 + 有 E_API_KEY_NOT_ALLOWED 警告 + 磁盘无明文",
            "E_API_KEY_NOT_ALLOWED" in warns and not net_has_key and not disk_has_key,
            f"warnings={warns} 响应含 api_key={net_has_key} 磁盘含明文={disk_has_key}",
        )

        # 3. set_config 未知 section → warnings 且不落盘
        before = cfg.read_text(encoding="utf-8")
        c.send("set_config", {"patch": {"no_such_section": {"x": 1}}})
        f = c.wait({"config_saved", "error"}, timeout=15)
        warns = (((f or {}).get("data") or {}).get("warnings")) or []
        unknown_warned = any("no_such_section" in str(w) for w in warns)
        after = cfg.read_text(encoding="utf-8")
        record("未知 section 有警告且不写脏数据", unknown_warned and "no_such_section" not in after,
               f"warnings={len(warns)}")

        # 4. verify_env 可触发
        c.send("verify_env")
        started = c.wait({"env_verify_started", "error"}, timeout=15)
        record("verify_env 回执 env_verify_started", started is not None)
        done = c.wait({"env_verified"}, timeout=180)
        d = (done or {}).get("data") or {}
        record("verify_env 给出三态结论", done is not None,
               f"gpu_usable={d.get('gpu_usable')} reason={d.get('blocked_reason')} verified={d.get('gpu_verified')}")
    finally:
        c.close()

    passed = sum(1 for _, ok, _ in results if ok)
    print("-" * 90)
    print(f"{passed}/{len(results)} 通过")
    if c.err:
        print("stderr 摘要：", " | ".join(c.err[:3]))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
