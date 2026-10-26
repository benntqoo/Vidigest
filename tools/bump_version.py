"""版本号统一管理：一条命令改全部，并给出「漏改」的判据。

## 为什么需要这个脚本

版本号散在 **5 个地方**，而它们各有各的用途，少改一处不会有任何报错：

| 位置 | 谁在用 |
|---|---|
| `VERSION` | 唯一的**真源**，给人和脚本看 |
| `app-ui/src-tauri/tauri.conf.json` | **MSI 的产品版本**（控制面板里显示的就是它） |
| `app-ui/package.json` | npm 包版本，`npm run build` 会读 |
| `app-ui/src-tauri/Cargo.toml` | Rust crate 版本；`CLIENT_VERSION` 由 `env!("CARGO_PKG_VERSION")` 取 |
| `app/rpc.py::SERVER_VERSION` | worker 在 `ready` 事件里上报，界面状态栏显示 |

只改 MSI 那处，界面就会显示旧版本 —— 用户看到「控制面板 0.2.0 / 状态栏 0.1.0」，
根本分不清自己装的到底是哪个。所以：**要么一次全改，要么别改。**

## 用法

```bash
python tools/bump_version.py 0.2.1      # 写入 5 处
python tools/bump_version.py --check    # 只核对，不改（判据 42 用的就是它）
```

退出码：0 = 成功 / 一致；1 = 不一致或写入失败。
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"

#: `(文件, 正则, 用来检查命中次数的期望值)`。正则必须**锚定**，否则会把
#: 依赖列表里的 `"version": "..."`（package.json 的子依赖没有这种写法，但
#: Cargo.lock 之类会有）误改。
TARGETS: tuple[tuple[str, str], ...] = (
    ("app-ui/src-tauri/tauri.conf.json", r'^(\s*)"version":\s*"[^"]+"'),
    ("app-ui/package.json", r'^(\s*)"version":\s*"[^"]+"'),
    ("app-ui/src-tauri/Cargo.toml", r'^version = "[^"]+"'),
    ("app/rpc.py", r'^SERVER_VERSION = "[^"]+"'),
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read_version() -> str:
    if not VERSION_FILE.is_file():
        raise SystemExit(f"找不到 {VERSION_FILE}")
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def current_versions() -> list[tuple[str, str | None]]:
    """返回 `[(文件, 当前值或 None)]`；`None` 表示没匹配到（= 该改写法了）。"""
    out: list[tuple[str, str | None]] = []
    for rel, pattern in TARGETS:
        path = ROOT / rel
        if not path.is_file():
            out.append((rel, None))
            continue
        m = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.M)
        if not m:
            out.append((rel, None))
            continue
        hit = m.group(0)
        # 从命中行里抠出**最后一个**引号串：JSON 是 `"version": "0.2.0"`，
        # 第一个引号串是键名，取第一个会得到 "version" 而不是版本值。
        quoted = re.findall(r'"([^"]*)"', hit)
        out.append((rel, quoted[-1] if quoted else None))
    return out


def check() -> int:
    want = read_version()
    rows = current_versions()
    bad = [(rel, got) for rel, got in rows if got != want]
    print(f"VERSION 真源：{want}")
    for rel, got in rows:
        mark = "ok  " if got == want else "FAIL"
        print(f"  [{mark}] {rel:<40} {got}")
    if bad:
        print(f"\n⇒ {len(bad)} 处与 VERSION 不一致。跑 `python tools/bump_version.py {want}` 修。")
        return 1
    print("\n⇒ 5 处一致。")
    return 0


def write(version: str) -> int:
    if not SEMVER.match(version):
        print(f"版本号格式不对：{version!r}，应为 x.y.z（例如 0.2.1）")
        return 1

    VERSION_FILE.write_text(version + "\n", encoding="utf-8")
    print(f"VERSION → {version}")
    for rel, pattern in TARGETS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        # 保留原行的缩进与键名，只换引号里的值
        new_text, n = re.subn(
            pattern,
            lambda m: re.sub(r'"[^"]+"$', f'"{version}"', m.group(0)),
            text,
            count=1,
            flags=re.M,
        )
        if n != 1:
            print(f"  [FAIL] {rel}：没匹配到版本行 —— 文件结构变了，去改 TARGETS 里的正则")
            return 1
        path.write_text(new_text, encoding="utf-8", newline="")
        print(f"  [ok  ] {rel}")
    print("\n⇒ 改完了。别忘了重新出包（./dev.sh release），MSI 文件名会带新版本号。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="统一管理 5 处版本号")
    ap.add_argument("version", nargs="?", help="新版本号（x.y.z）")
    ap.add_argument("--check", action="store_true", help="只核对一致性，不写")
    args = ap.parse_args()

    if args.check or not args.version:
        return check()
    return write(args.version)


if __name__ == "__main__":
    sys.exit(main())
