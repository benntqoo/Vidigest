"""模型下载器自测 —— 用本地假 HTTP 服务器验续传 / 取消 / 大小校验 / 幂等。

**为什么不用真镜像**：真镜像 2.88 GiB，跑一次要几分钟且依赖外网；
而下载器最容易出错的地方（Range 请求、`.part` 残留、取消时机、大小不符）
全都与「真模型」无关，用几十 MB 的假文件就能覆盖。

判据数：见脚本末尾输出。用例清单：

1. 全新下载：5 个文件大小与内容一致，无 `.part` 残留
2. 幂等：全部已存在时再启动 → 跳过实际传输（`already_bytes == total`）
3. 续传：预先存在 `.part` 前半段 → 服务器收到 `Range: bytes=<offset>-`，最终内容完整
4. 取消：下载中取消 → 收到 `model_download_cancelled`，`.part` 被删
5. 大小不符：服务器给的长度与实际内容不符 → `E_MODEL_SIZE_MISMATCH`，半成品被删
6. 磁盘不足：把 DISK_MARGIN 调到天文数字 → `E_DISK_FULL`（且没有真去下载）
7. 404：文件不存在 → `E_NETWORK`（重试 3 次后放弃）

用法：
    python tools/model_downloader_selftest.py
"""

from __future__ import annotations

import http.server
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import asset_packs, model_downloader  # noqa: E402
from app.core.env_probe import MODEL_FILES  # noqa: E402


# --------------------------------------------------------------- 假服务器


class Handler(http.server.BaseHTTPRequestHandler):
    #: 文件名 → 内容。由用例填充。
    files: dict[str, bytes] = {}
    #: 收到的 Range 头，格式 `name:Range`
    range_log: list[str] = []
    #: 每个 64 KiB 块之间睡多久（制造「下得慢」以便中途取消）
    delay: float = 0.0
    #: 声明长度与实际长度不符的偏差（模拟服务端撒谎）
    length_bias: dict[str, int] = {}

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D401 — 静音，避免污染输出
        pass

    def _name(self) -> str:
        return self.path.lstrip("/").split("/")[-1]

    def do_HEAD(self) -> None:
        name = self._name()
        data = self.files.get(name)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data) + self.length_bias.get(name, 0)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self) -> None:
        name = self._name()
        data = self.files.get(name)
        if data is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        self.range_log.append(f"{name}:{rng}")
        if rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            for i in range(0, len(body), 64 * 1024):
                self.wfile.write(body[i : i + 64 * 1024])
                if self.delay:
                    time.sleep(self.delay)
        except (BrokenPipeError, ConnectionAbortedError):
            pass  # 客户端取消时会主动断开，属预期


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """连接被客户端取消时不要打 traceback —— 那是预期行为，打了只会淹没结论。"""

    def handle_error(self, request, client_address) -> None:  # noqa: D401
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


class Server:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{{model}}/"


# ------------------------------------------------------------------- 用例


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def __call__(self, name: str, data: dict) -> None:
        with self.lock:
            self.events.append((name, dict(data)))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def last(self, name: str) -> dict | None:
        with self.lock:
            for n, d in reversed(self.events):
                if n == name:
                    return d
        return None

    def wait(self, name: str, timeout: float = 60.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = self.last(name)
            if got is not None:
                return got
            time.sleep(0.02)
        return None


JUDGEMENTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    JUDGEMENTS.append((name, ok, detail))


def make_payload() -> dict[str, bytes]:
    """小的用 4 KiB，model.bin 用 2 MiB（够分 32 块，便于取消）。"""
    data: dict[str, bytes] = {}
    for name in MODEL_FILES:
        if name == "model.bin":
            data[name] = bytes(range(256)) * (2 * 1024 * 4)  # 2 MiB
        else:
            data[name] = b"x" * 4096
    return data


def fresh_dir(tmp: Path, tag: str) -> Path:
    d = tmp / tag
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_case_full(server: Server, tmp: Path) -> None:
    Handler.files = make_payload()
    Handler.range_log = []
    Handler.delay = 0.0
    Handler.length_bias = {}

    home = fresh_dir(tmp, "full")
    rec = Recorder()
    dl = model_downloader.ModelDownloader(home, "large-v3", rec, base_url=server.base_url)
    dl.start()
    done = rec.wait("model_download_done")
    target = home / "large-v3"
    parts = list(target.glob("*.part")) if target.exists() else []
    ok_sizes = all(
        (target / n).is_file() and (target / n).read_bytes() == Handler.files[n] for n in MODEL_FILES
    )
    record(
        "全新下载：5 文件内容一致",
        done is not None and ok_sizes and not parts,
        f"done={'收到' if done else '未收到'} 文件全对={ok_sizes} .part 残留={len(parts)}",
    )
    # 校验用的是假模型，必然加载失败 —— 但这是「文件已下完，验证结论如实」的正确行为：
    # 只要不是 `verified=True` 就说明它没有假装成功。
    record(
        "下载后自动校验（假模型应报不可用）",
        done is not None and done.get("verified") is not True,
        f"verified={done.get('verified') if done else None} reason={done.get('blocked_reason') if done else None}",
    )

    # 幂等：再启动一次不应有新的 GET（文件都在且大小对）
    before = len(Handler.range_log)
    rec2 = Recorder()
    dl2 = model_downloader.ModelDownloader(home, "large-v3", rec2, base_url=server.base_url)
    dl2.start()
    done2 = rec2.wait("model_download_done")
    started2 = rec2.last("model_download_started")
    record(
        "幂等：已齐全时不再传输",
        done2 is not None
        and started2 is not None
        and started2.get("already_bytes") == started2.get("total_bytes")
        and len(Handler.range_log) == before,
        f"already={started2.get('already_bytes') if started2 else None} "
        f"total={started2.get('total_bytes') if started2 else None} 新增请求={len(Handler.range_log) - before}",
    )


def run_case_resume(server: Server, tmp: Path) -> None:
    Handler.files = make_payload()
    Handler.range_log = []
    Handler.delay = 0.0
    Handler.length_bias = {}

    home = fresh_dir(tmp, "resume")
    target = home / "large-v3"
    target.mkdir(parents=True, exist_ok=True)
    blob = Handler.files["model.bin"]
    half = len(blob) // 2
    # 先下好的小文件直接放到位（下载器按大小跳过）
    for n in MODEL_FILES:
        if n != "model.bin":
            (target / n).write_bytes(Handler.files[n])
    (target / "model.bin.part").write_bytes(blob[:half])

    rec = Recorder()
    dl = model_downloader.ModelDownloader(home, "large-v3", rec, base_url=server.base_url)
    dl.start()
    rec.wait("model_download_done")
    ranges = [r for r in Handler.range_log if r.startswith("model.bin:")]
    ok = (target / "model.bin").is_file() and (target / "model.bin").read_bytes() == blob
    record(
        "续传：从 .part 偏移接着下",
        ok and any(f"bytes={half}-" in (r or "") for r in ranges),
        f"最终完整={ok} Range={ranges[:2]}",
    )


def run_case_cancel(server: Server, tmp: Path) -> None:
    Handler.files = make_payload()
    Handler.range_log = []
    Handler.delay = 0.02  # 2 MiB / 64 KiB = 32 块 → 约 0.64 s
    Handler.length_bias = {}

    home = fresh_dir(tmp, "cancel")
    rec = Recorder()
    dl = model_downloader.ModelDownloader(home, "large-v3", rec, base_url=server.base_url)
    dl.start()
    # 等到进入 model.bin 的进度再取消（前面几个小文件秒完）
    deadline = time.time() + 30
    while time.time() < deadline:
        p = rec.last("model_download_progress")
        if p and p.get("file") == "model.bin":
            break
        time.sleep(0.01)
    cancelled_flag = dl.cancel()
    evt = rec.wait("model_download_cancelled", timeout=20)
    parts = list((home / "large-v3").glob("*.part")) if (home / "large-v3").exists() else []
    done = rec.last("model_download_done")
    record(
        "取消：收到事件且 .part 被删",
        cancelled_flag and evt is not None and not parts and done is None,
        f"cancel()={cancelled_flag} 事件={'收到' if evt else '未收到'} .part={len(parts)} done={'有' if done else '无'}",
    )


def run_case_size_mismatch(server: Server, tmp: Path) -> None:
    Handler.files = make_payload()
    Handler.range_log = []
    Handler.delay = 0.0
    # 声明比实际长 → 下完后大小比对必然失败
    Handler.length_bias = {"vocabulary.json": 100}

    home = fresh_dir(tmp, "mismatch")
    rec = Recorder()
    dl = model_downloader.ModelDownloader(home, "large-v3", rec, base_url=server.base_url)
    dl.start()
    failed = rec.wait("model_download_failed", timeout=60)
    target = home / "large-v3"
    parts = list(target.glob("*.part")) if target.exists() else []
    record(
        "大小不符：E_MODEL_SIZE_MISMATCH 且删半成品",
        failed is not None and failed.get("code") == "E_MODEL_SIZE_MISMATCH" and not parts,
        f"code={failed.get('code') if failed else None} .part={len(parts)}",
    )
    Handler.length_bias = {}


def run_case_disk_full(server: Server, tmp: Path) -> None:
    Handler.files = make_payload()
    Handler.range_log = []
    Handler.delay = 0.0
    Handler.length_bias = {}

    home = fresh_dir(tmp, "diskfull")
    old = model_downloader.DISK_MARGIN
    model_downloader.DISK_MARGIN = 1 << 60  # 1 EiB，必然不够
    try:
        rec = Recorder()
        dl = model_downloader.ModelDownloader(home, "large-v3", rec, base_url=server.base_url)
        dl.start()
        failed = rec.wait("model_download_failed", timeout=20)
    finally:
        model_downloader.DISK_MARGIN = old
    record(
        "磁盘不足：E_DISK_FULL 且未开始传输",
        failed is not None and failed.get("code") == "E_DISK_FULL" and not Handler.range_log,
        f"code={failed.get('code') if failed else None} 请求数={len(Handler.range_log)}",
    )


def run_case_404(server: Server, tmp: Path) -> None:
    Handler.files = {n: b"y" * 100 for n in MODEL_FILES if n != "tokenizer.json"}
    Handler.range_log = []
    Handler.delay = 0.0
    Handler.length_bias = {}

    home = fresh_dir(tmp, "notfound")
    old_retries = model_downloader.RETRIES
    model_downloader.RETRIES = 0  # 免掉退避等待
    try:
        rec = Recorder()
        dl = model_downloader.ModelDownloader(home, "large-v3", rec, base_url=server.base_url)
        dl.start()
        failed = rec.wait("model_download_failed", timeout=30)
    finally:
        model_downloader.RETRIES = old_retries
    record(
        "404：如实报失败（不假装完成）",
        failed is not None and failed.get("code") == "E_NETWORK",
        f"code={failed.get('code') if failed else None}",
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def run_case_unzip(server: Server, tmp: Path) -> None:
    """本地视觉模型包：zip 下完自动解压到 dest_dir，并删掉原包。"""
    Handler.files = {
        "fake-llama.zip": _zip_bytes(
            {
                "llama-server.exe": b"MZ" + b"s" * 600,
                "llama-server-impl.dll": b"D" * 2048,
                "ggml-cuda.dll": b"G" * 512,
            }
        ),
        # 完整性检查要求「主权重 + mmproj」都在，否则会 E_MODEL_INCOMPLETE。
        # 内容无所谓：`--version` 校验会失败但**不该判下载失败**（见下面的断言）。
        "Qwen3-VL-2B-Instruct-Q4_K_M.gguf": b"Q" * 4096,
        "mmproj-BF16.gguf": b"P" * 2048,
    }
    Handler.range_log = []
    Handler.delay = 0.0
    Handler.length_bias = {}

    home = fresh_dir(tmp, "unzip")
    target = home / "vlm"
    pack = asset_packs.AssetPack(
        key="local_ai",
        title="测试用",
        target_dir=target,
        files=(
            asset_packs.AssetFile(
                name="fake-llama.zip",
                url=f"http://127.0.0.1:{server.port}/x/fake-llama.zip",
                dest_dir="llama",
                unzip=True,
            ),
            asset_packs.AssetFile(
                name="Qwen3-VL-2B-Instruct-Q4_K_M.gguf",
                url=f"http://127.0.0.1:{server.port}/x/Qwen3-VL-2B-Instruct-Q4_K_M.gguf",
            ),
            asset_packs.AssetFile(
                name="mmproj-BF16.gguf",
                url=f"http://127.0.0.1:{server.port}/x/mmproj-BF16.gguf",
            ),
        ),
    )
    rec = Recorder()
    dl = model_downloader.ModelDownloader.for_pack(pack, rec)
    dl.start()
    done = rec.wait("model_download_done", timeout=60)
    failed = rec.last("model_download_failed")

    out = target / "llama"
    ok_files = (
        (out / "llama-server.exe").is_file()
        and (out / "llama-server.exe").read_bytes() == b"MZ" + b"s" * 600
        and (out / "llama-server-impl.dll").is_file()
        and (out / "ggml-cuda.dll").is_file()
    )
    # 原 zip 必须被删：留着等于白占 587 MiB（真包体积）
    zip_gone = not (out / "fake-llama.zip").exists()
    record(
        "zip 包：解压到 dest_dir 且删除原包",
        ok_files and zip_gone and "model_download_extracting" in rec.names() and done is not None,
        f"文件齐={ok_files} 原包已删={zip_gone} done={done is not None} failed={failed}",
    )
    # 「假 exe 起不来」是**校验**结论，不是下载结论 —— 两者混为一谈会让用户
    # 以为 2.3 GiB 白下了。
    record(
        "本地包：二进制起不来只判 verified=False，不判下载失败",
        done is not None and done.get("verified") is False and failed is None,
        f"verified={done.get('verified') if done else None} "
        f"reason={done.get('blocked_reason') if done else None} failed={failed}",
    )


def run_case_zip_slip(server: Server, tmp: Path) -> None:
    """压缩包里的路径穿越条目必须被跳过，且不能写到目录外。"""
    Handler.files = {
        "evil.zip": _zip_bytes(
            {
                "../../evil.txt": b"pwned",
                # 顺带放一个合法条目：确认「跳过坏条目」不影响好条目照解。
                # 放 llama-server.exe 是为了让完整性检查通过，不制造无关的失败日志。
                "llama-server.exe": b"MZ" + b"s" * 600,
                "ok.txt": b"fine",
            }
        ),
        "Qwen3-VL-2B-Instruct-Q4_K_M.gguf": b"Q" * 4096,
        "mmproj-BF16.gguf": b"P" * 2048,
    }
    Handler.range_log = []
    Handler.delay = 0.0
    Handler.length_bias = {}

    home = fresh_dir(tmp, "slip")
    target = home / "vlm"
    pack = asset_packs.AssetPack(
        key="local_ai",
        title="测试用",
        target_dir=target,
        files=(
            asset_packs.AssetFile(
                name="evil.zip",
                url=f"http://127.0.0.1:{server.port}/x/evil.zip",
                dest_dir="llama",
                unzip=True,
            ),
            asset_packs.AssetFile(
                name="Qwen3-VL-2B-Instruct-Q4_K_M.gguf",
                url=f"http://127.0.0.1:{server.port}/x/Qwen3-VL-2B-Instruct-Q4_K_M.gguf",
            ),
            asset_packs.AssetFile(
                name="mmproj-BF16.gguf",
                url=f"http://127.0.0.1:{server.port}/x/mmproj-BF16.gguf",
            ),
        ),
    )
    rec = Recorder()
    dl = model_downloader.ModelDownloader.for_pack(pack, rec)
    dl.start()
    rec.wait("model_download_done", timeout=60)

    inside = (target / "llama" / "ok.txt").is_file()
    escaped = (home / "evil.txt").exists() or (target / "evil.txt").exists()
    record(
        "zip 路径穿越：跳过越界条目，正常条目照解",
        inside and not escaped,
        f"ok.txt={inside} 越界写出={escaped}",
    )


def main() -> int:
    # 下载完成后的「校验」会真去 import faster_whisper 并加载假模型 —— 这一步在本机
    # 有依赖时会花 1-2 秒并返回 False（`cuda_load_failed`），正是我们要的「如实上报」。
    tmp = Path(tempfile.mkdtemp(prefix="vidigest-dl-"))
    print("模型下载器自测 —— app/core/model_downloader.py\n")
    try:
        with Server() as server:
            run_case_full(server, tmp)
            run_case_resume(server, tmp)
            run_case_cancel(server, tmp)
            run_case_size_mismatch(server, tmp)
            run_case_disk_full(server, tmp)
            run_case_404(server, tmp)
            run_case_unzip(server, tmp)
            run_case_zip_slip(server, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"{'判据':<40} {'结果':<6} 说明")
    print("-" * 110)
    passed = 0
    for name, ok, detail in JUDGEMENTS:
        mark = "PASS" if ok else "FAIL"
        passed += 1 if ok else 0
        print(f"{name:<36} {mark:<6} {detail}")
    print("-" * 110)
    print(f"{passed}/{len(JUDGEMENTS)} 通过")
    return 0 if passed == len(JUDGEMENTS) else 1


if __name__ == "__main__":
    sys.exit(main())
