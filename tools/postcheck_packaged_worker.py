"""对**打包后的** worker 产物做一次新命令实测（不属于常驻判据，只是出包后的一次性核对）。

判据：
  1. get_config 有响应且带 model_present / read_only_fields
  2. set_config 拒绝写 api_key（E_API_KEY_NOT_ALLOWED）
  3. set_config 未知 section 被丢弃并回 warnings（不写盘）
  4. verify_env 可被触发（收到 env_verify_started，随后 env_verified）
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
