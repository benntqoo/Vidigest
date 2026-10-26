"""抖音直链探针 —— 验证「用浏览器打开分享页、抓它自己请求的 mp4」这条路是否可行。

## 为什么要先探针

2026-10-26 实测结论：
- 纯 HTTP 无 cookie：短链一律 302 回 `www.douyin.com` 首页，取不到 `aweme_id`；
  老的 `iesdouyin/web/api/v2/aweme/iteminfo` 对所有 id 返回 200 + 空 body（接口已下线）。
- 因此只剩「真实浏览器」这条路：让它自己播放，我们只抓它请求的媒体地址。
  参考实现是 `Mu-L/douyin-downloader` 的 `strategies/browser_strategy.py`
  （`page.on('response')` 里筛 `.mp4/.m3u8`），但那份代码在它自己的
  `orchestrator.py` 里**没有注册**，所以从未被真正验证过 —— 这就是本脚本存在的理由。

**不改产品代码**。本脚本只回答三个问题：
1. 打开分享链接后，能不能拦到 `.mp4` / `.m3u8` 请求？
2. `<video>` 的 `src` 是直链还是 `blob:`（MSE）？
3. 未登录（全新 context，无任何 cookie）能不能播？

## 用法

```bash
python tools/probe_douyin.py "https://v.douyin.com/ye_7rRWF3tc/"          # 无头
python tools/probe_douyin.py "https://v.douyin.com/xxx/" --headed        # 有头（看得到浏览器）
python tools/probe_douyin.py "..." --cookies cookies.txt                  # 带已有 cookie 再试
```

退出码：0 = 拦到可用直链；1 = 没拦到（这条路不成立）；2 = 环境问题（未安装等）。

## 已知边界

- 抖音可能对**无头**浏览器有额外检测；无头失败不代表有头失败，两种都要跑。
- 拦到的 URL 通常带**时效签名**（`x-expires` / `a_bogus` 之类），过期就 403 ——
  所以真要做成功能，"抓链"与"下载"必须紧接着完成，不能缓存 URL。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MEDIA_HINTS = (".mp4", ".m3u8", ".flv")
#: 只关心真正承载视频的域名，避免把封面图 / 头像也算进来
WANT_HOSTS = ("douyin", "bytecdn", "bytedance", "snssdk", "ixigua", "amemv", "pstatp")


def _looks_media(url: str) -> bool:
    low = url.lower()
    if not any(host in low for host in WANT_HOSTS):
        return False
    return any(hint in low for hint in MEDIA_HINTS) or "/video/tos/" in low or "mime_type=video" in low


def main() -> int:
    ap = argparse.ArgumentParser(description="抖音直链探针（不改产品代码）")
    ap.add_argument("url", help="抖音分享链接，例如 https://v.douyin.com/xxx/")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    ap.add_argument("--cookies", help="可选：Netscape 格式 cookies.txt，注入后再访问")
    ap.add_argument("--timeout", type=float, default=20.0, help="等待秒数（默认 20）")
    ap.add_argument(
        "--warm",
        action="store_true",
        help="先访问 douyin.com 让 JS 种下 ttwid/__ac_signature，再打开分享链接（更接近真实用户）",
    )
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright：pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    hits: list[str] = []
    cookies_seen: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            # 关掉最明显的自动化特征；`browser_strategy.py` 也是这么做的。
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        # 去掉 `navigator.webdriver` —— 抖音会拿它当机器人信号。
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        if args.cookies:
            jar = _load_netscape(Path(args.cookies))
            if jar:
                ctx.add_cookies(jar)
                print(f"注入 cookie {len(jar)} 条")

        page = ctx.new_page()
        page.on("response", lambda r: hits.append(r.url) if _looks_media(r.url) and r.status == 200 else None)

        if args.warm:
            # 先暖会话：抖音的 ttwid / __ac_signature 由首页 JS 计算并种下，
            # 冷启动直接打分享链接时这些还没有 ⇒ 被 302 回首页。
            print("暖会话：打开 https://www.douyin.com/ …")
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(4000)
                print(f"  暖完，cookie {len(ctx.cookies())} 条")
            except Exception as exc:  # noqa: BLE001
                print(f"  暖会话异常：{type(exc).__name__}: {exc}")

        print(f"打开：{args.url}")
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=int(args.timeout * 1000))
        except Exception as exc:  # noqa: BLE001 — 探针，只想知道结果
            print(f"goto 异常：{type(exc).__name__}: {exc}")

        # 等一会儿，让页面自己发起播放请求（自动播放常被拦，所以主动点一下）
        page.wait_for_timeout(3000)
        for sel in ("video", "button[aria-label*='播放']", ".xgplayer-icon", "#douyin-player"):
            try:
                if page.locator(sel).count():
                    page.locator(sel).first.click(timeout=2000)
                    break
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(int(args.timeout * 1000) - 3000 if args.timeout > 3 else 3000)

        print()
        print(f"最终 URL：{page.url}")

        # 问题 2：<video> 的 src 是直链还是 blob
        try:
            info = page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    if (!v) return null;
                    return {src: v.src || '', current: v.currentSrc || '',
                            duration: v.duration || 0, ready: v.readyState};
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            info = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"<video>：{info}")

        # 问题 3：拿到了什么 cookie（判断是否需要登录）
        cookies = ctx.cookies()
        cookies_seen = [c["name"] for c in cookies]
        print(f"cookie：{len(cookies)} 条 → {cookies_seen}")
        has_login = any(n in cookies_seen for n in ("sessionid", "sessionid_ss", "sid_tt"))
        print(f"是否已登录态：{'是' if has_login else '否（匿名）'}")

        # 问题 1：拦到多少媒体请求
        uniq = list(dict.fromkeys(hits))
        print()
        print(f"拦截到媒体请求 {len(hits)} 条（去重 {len(uniq)}）：")
        for u in uniq[:6]:
            print(f"  - {u[:160]}")
        video_ok = bool(info and (info.get("current") or info.get("src"))) and not str(
            info.get("current") or info.get("src")
        ).startswith("blob:")
        print()
        print(f"直达结论：拦到直链={bool(uniq)}；<video> 有非 blob 地址={video_ok}")

        browser.close()

    return 0 if hits or video_ok else 1


def _load_netscape(path: Path) -> list[dict]:
    """把 Netscape cookies.txt 转成 playwright 的 cookie 结构（够用即可）。"""
    out: list[dict] = []
    if not path.is_file():
        print(f"cookie 文件不存在：{path}")
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path_, _secure, expires, name, value = parts[:7]
        item = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path_ or "/",
            "httpOnly": False,
            "secure": True,
        }
        try:
            exp = int(float(expires))
            if exp > 0:
                item["expires"] = exp
        except ValueError:
            pass
        out.append(item)
    return out


if __name__ == "__main__":
    sys.exit(main())
