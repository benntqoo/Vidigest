"""README 自检：链接可点、图片存在、锚点能跳、没有本机细节泄漏。

## 为什么需要它

`README.md` 是仓库的**门面**——它在 GitHub 首页被渲染，而 `docs/` 里的 20 多个文件会被改名、
移动、合并。改一次文档名，README 里那条链接就静默变成 404：**渲染端不会报错，只有点的人知道**。
这个脚本把「链接指向的文件真的在」变成一条能跑的判据。

第二类判据是**本机细节泄漏**：这个仓库的文档长期以「我自己的机器」为默认上下文写，
于是 README 里会自然长出「本机」「C 盘仅剩 150 GB」「我的 4060 Ti」这类对外部读者无意义的句子。
它们是机器相关的，不是产品相关的，出现即视为失败（性能表里写明测试机型是**允许**的，那是事实）。

用法：
    python tools/readme_check.py            # 检查 README.md 与 README.en.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ("README.md", "README.en.md")

#: 出现即失败的本机/私有上下文用语。注意**不含** `RTX 4060 Ti` / `Windows 11` 这类
#: 测试机型描述 —— 性能表必须写明在什么硬件上测的，那是可信度来源。
BANNED = (
    "本机",
    "我的电脑",
    "我这台",
    "C 盘仅剩",
    "尚未安装 Rust",
    "本单位",
)

#: `[text](target)` 与 HTML 的 `src=` / `href=`
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HTML_ATTR = re.compile(r'(?:src|href)="([^"]+)"')
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)

_PASS = 0
_FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS
    if ok:
        _PASS += 1
        print(f"  [ok  ] {name}" + (f"  {detail}" if detail else ""))
    else:
        _FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  {detail}" if detail else ""))


def slug(text: str) -> str:
    """GitHub 的标题锚点规则：小写、去标点、空格换 `-`。中文原样保留。"""
    s = text.strip().lower()
    s = re.sub(r"`|\*|_|~", "", s)          # 行内代码/强调标记
    s = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", s)
    return s.replace(" ", "-")


def is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "//"))


def targets(text: str) -> list[str]:
    return MD_LINK.findall(text) + HTML_ATTR.findall(text)


def main() -> int:
    anchors: dict[str, set[str]] = {}
    bodies: dict[str, str] = {}

    for name in FILES:
        path = ROOT / name
        if not path.is_file():
            check(f"{name} 存在", False)
            continue
        body = path.read_text(encoding="utf-8")
        bodies[name] = body
        anchors[name] = {slug(h) for _, h in HEADING.findall(body)}
        check(f"{name} 存在", True, f"{len(body.splitlines())} 行")

    if not bodies:
        return 1

    # ---- 1. 相对链接/图片的目标文件真的在 ----
    for name, body in bodies.items():
        missing: list[str] = []
        total = 0
        for t in targets(body):
            if is_external(t) or t.startswith("#"):
                continue
            total += 1
            local = t.split("#", 1)[0]
            if not (ROOT / local).exists():
                missing.append(local)
        check(
            f"{name} 的相对链接都指向真实文件",
            not missing,
            f"{total} 条" if not missing else f"缺失 {sorted(set(missing))}",
        )

    # ---- 2. 页内锚点能跳到 ----
    for name, body in bodies.items():
        dead: list[str] = []
        total = 0
        for t in targets(body):
            if not t.startswith("#"):
                continue
            total += 1
            if t[1:] not in anchors[name]:
                dead.append(t)
        check(
            f"{name} 的页内锚点都能命中标题",
            not dead,
            f"{total} 条" if not dead else f"断链 {dead}",
        )

    # ---- 3. 本机细节没有泄漏到门面文件 ----
    for name, body in bodies.items():
        hits = [w for w in BANNED if w in body]
        check(f"{name} 没有本机细节泄漏", not hits, "干净" if not hits else f"命中 {hits}")

    # ---- 4. 门面必备件：许可证、图标、一行简介 ----
    check("LICENSE 存在（README 引用它）", (ROOT / "LICENSE").is_file())
    check(
        "LICENSE 里写明 MIT",
        (ROOT / "LICENSE").is_file()
        and "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8"),
    )
    check("横幅图存在", (ROOT / "docs/images/readme/banner.png").is_file())
    check(
        "README.md 首屏就有横幅",
        "docs/images/readme/banner.png" in bodies.get("README.md", "")[:1200],
    )
    check(
        "README.en.md 从中文版反向指回",
        "README.md" in bodies.get("README.en.md", ""),
    )

    # ---- 5. 图标源与生成物同源 ----
    icon_src = ROOT / "packaging/icon/app-icon.svg"
    icons_dir = ROOT / "app-ui/src-tauri/icons"
    check("图标源文件存在", icon_src.is_file())
    for f in ("icon.ico", "icon.icns", "icon.png", "32x32.png", "128x128.png"):
        check(f"生成的图标 {f} 存在", (icons_dir / f).is_file())

    total = _PASS + len(_FAIL)
    print(f"\nREADME 自检: {_PASS}/{total} 通过")
    for name in _FAIL:
        print(f"  ✗ {name}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
