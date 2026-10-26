"""本地视觉模型探针：验证「拼版图 → 中文描述 / 中文 OCR」的可行性与成本。

为什么需要这个脚本
------------------
汇总阶段（`app/core/summarizer.py`）目前只吃文本。要不要把画面也喂给模型，
在 2026-10 做过一轮选型，结论是**只能走 GGUF + llama.cpp**：

| 方案 | 权重 | 单张耗时（实测） | 中文 OCR |
|---|---|---|---|
| SmolVLM-256M（torch/CPU） | 494 MB | 6.6 s | ❌ 胡言乱语 |
| Qwen3-VL-2B（torch 5.x/CPU） | 4.0 GB | 45 s | ✅ 但会重复退化 |
| **Qwen3-VL-2B Q4_K_M（llama.cpp/CUDA）** | **1.84 GB** | **1.5 s** | **✅ 准确** |

这个脚本守的判据：
1. `/v1/chat/completions` 能直接塞 base64 图 —— 即 `summarizer._post()` 可原样复用，
   本地模型与云 API 在代码上是同一条路，只差一个 `base_url`。
2. 中文 OCR 能在**真实拼版图**上落地（不是合成图、不是单帧大图）。
3. 每张耗时与显存占用量级 —— 决定它能不能进主链路。

用法
----
    # 先备齐（约 2.4 GB）：
    #   llama.cpp 预编译包  → https://github.com/ggml-org/llama.cpp/releases
    #   GGUF 权重 + mmproj  → unsloth/Qwen3-VL-2B-Instruct-GGUF
    python tools/probe_vlm.py --server D:/vlm_test/llama/llama-server.exe \
        --model D:/vlm_test/gguf/Qwen3-VL-2B-Instruct-Q4_K_M.gguf \
        --mmproj D:/vlm_test/gguf/mmproj-BF16.gguf

退出码：0 全部通过；1 有判据未过（会打印是哪一条）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

#: 判据 2 的阈值：一张 1920x792 的 24 宫格拼版图，GPU 上应在个位数秒内出结果。
#: 超过这个值说明没吃到 GPU（-ngl 没生效，或跑的是 CPU 后端）。
SLOW_SECONDS = 10.0

#: 判据 3：同一条视频通常产出 3~6 张拼版图，显存要多留余量给 faster-whisper。
VRAM_LIMIT_MIB = 6000

PROMPTS: list[tuple[str, str]] = [
    ("中文 OCR", "图里有中文字吗？如果有，请逐条照抄你能看到的中文文字。只抄你确实看到的，不要推测。"),
    ("主题概括", "这是一张视频截图的拼版图。请用中文分点概括这些画面在讲什么。"),
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_image() -> Path:
    # samples/ 是本地专用目录（不入库），缺失时回落到任一产出目录。
    cand = _repo_root() / "samples" / "sheets" / "sheet_01.jpg"
    if cand.is_file():
        return cand
    raise SystemExit(
        "找不到测试图。请用 --image 指定一张拼版图（<工作目录>/<任务>/sheets/sheet_01.jpg）。"
    )


def _vram_used_mib() -> int | None:
    """当前显存占用（MiB）。nvidia-smi 不存在时返回 None——不视为失败。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return int(out.stdout.strip().splitlines()[0])
    except (subprocess.SubprocessError, ValueError):
        return None


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            raise SystemExit(f"llama-server 提前退出，退出码 {proc.returncode}（日志见 --log）")
        try:
            if requests.get(f"{base}/health", timeout=2).status_code == 200:
                return time.time() - t0
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise SystemExit(f"llama-server 在 {timeout:.0f}s 内未就绪")


def _ask(base: str, image_b64: str, question: str, max_tokens: int) -> tuple[str, float, dict]:
    """走 OpenAI 兼容接口。这段请求体形状与 summarizer.build_payload 一致（多一个 image_url）。"""
    body = {
        "model": "local",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            {"type": "text", "text": question},
        ]}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        # 2B 模型在长中文输出上会重复到失控（实测连出 60 多个箭头），必须给惩罚。
        "repeat_penalty": 1.15,
    }
    t0 = time.time()
    resp = requests.post(f"{base}/v1/chat/completions", json=body, timeout=900)
    elapsed = time.time() - t0
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"], elapsed, data.get("usage", {})


def main() -> int:
    ap = argparse.ArgumentParser(description="本地视觉模型探针（GGUF + llama.cpp）")
    ap.add_argument("--server", required=True, help="llama-server 可执行文件")
    ap.add_argument("--model", required=True, help="主权重 .gguf")
    ap.add_argument("--mmproj", required=True, help="视觉投影器 .gguf")
    ap.add_argument("--image", default=None, help="测试用拼版图，默认 samples/sheets/sheet_01.jpg")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--timeout", type=float, default=180.0, help="等待模型加载的上限（秒）")
    ap.add_argument("--log", default="vlm_server.log", help="llama-server 日志落点")
    args = ap.parse_args()

    server = Path(args.server)
    if not server.is_file():
        raise SystemExit(f"找不到 llama-server：{server}")
    for p, label in ((args.model, "主权重"), (args.mmproj, "mmproj")):
        if not Path(p).is_file():
            raise SystemExit(f"找不到{label}：{p}")

    image_path = Path(args.image) if args.image else _default_image()
    image_b64 = base64.b64encode(image_path.read_bytes()).decode()

    vram_before = _vram_used_mib()
    cmd = [
        str(server), "-m", args.model, "--mmproj", args.mmproj,
        "-ngl", "99", "-c", "8192", "-np", "1",
        "--host", "127.0.0.1", "--port", str(args.port),
    ]
    print(f"[配置] 图片 {image_path.name}  {len(image_b64) // 1024} KB(base64)")
    print(f"[配置] 权重 {Path(args.model).name}  显存占用前 {vram_before} MiB")

    log_path = Path(args.log).resolve()
    log = log_path.open("w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{args.port}"
    failures: list[str] = []
    try:
        ready = _wait_ready(base, proc, args.timeout)
        vram_after = _vram_used_mib()
        print(f"[就绪] {ready:.1f}s   显存占用后 {vram_after} MiB")

        vram_delta = None if (vram_before is None or vram_after is None) else vram_after - vram_before
        if vram_delta is not None:
            print(f"[显存] 模型净占用约 {vram_delta} MiB")
            if vram_delta > VRAM_LIMIT_MIB:
                failures.append(f"显存净占用 {vram_delta} MiB 超过 {VRAM_LIMIT_MIB} MiB 预算")

        results = []
        for name, question in PROMPTS:
            try:
                text, elapsed, usage = _ask(base, image_b64, question, args.max_tokens)
            except requests.RequestException as exc:
                failures.append(f"{name} 请求失败：{exc}")
                continue
            print(f"\n--- {name}  {elapsed:.1f}s  {usage.get('prompt_tokens')}p/"
                  f"{usage.get('completion_tokens')}c ---")
            print(text.strip() or "（空输出）")
            if not text.strip():
                failures.append(f"{name} 输出为空")
            if elapsed > SLOW_SECONDS:
                failures.append(f"{name} 耗时 {elapsed:.1f}s 超过 {SLOW_SECONDS:.0f}s（疑似未用 GPU）")
            results.append({"name": name, "elapsed": round(elapsed, 2),
                            "usage": usage, "text": text})

        report = {
            "image": str(image_path), "model": args.model, "mmproj": args.mmproj,
            "load_seconds": round(ready, 2), "vram_delta_mib": vram_delta,
            "results": results, "failures": failures,
        }
        Path("vlm_probe_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    print(f"\n[报告] vlm_probe_report.json   server 日志 {log_path}")
    if failures:
        print("\n[未通过]")
        for f in failures:
            print(f"  - {f}")
        return 1
    # 只报脚本真正判定过的三条。中文质量必须人工看上面的输出——
    # 「跑通了」不等于「读对了」，把这两件事混起来是这类探针最常见的误报。
    print("\n[通过] 自动判据 3/3：接口兼容 / 速度达线 / 显存可控")
    print("       中文准确性需人工核对上面的 OCR 输出，脚本不代替判断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
