"""画面理解：把拼版图交给视觉模型读，产出 `visual.md`。

## 它解决什么

`frames.py` 只把画面**截下来拼成图**，里面的信息（PPT 标题、股票界面上的数字、
代码、演示文稿）对下游完全不可读。汇总阶段只吃转写稿，于是「视频里放了一张
关键的收益曲线图」这件事在产物里彻底消失。

这个模块补的是「让模型看图并写成字」这一段。产物 `visual.md` 会被
`summarizer` 当作附加材料拼进汇总输入 —— 于是总结里能出现转写稿里没有、
但画面上写着的数字。

## 三个设计决定

1. **按拼版图读，不按单帧读**。一条 20 分钟视频约 400 帧，逐帧喂模型是 400 次
   调用；拼版后是 4–6 张（每张 24 格），调用量降两个数量级。
   代价是单格分辨率变低（320×180），细小的字会读不出 —— 这是刻意的取舍，
   画面理解要的是「大概在讲什么」，不是逐字 OCR。
2. **失败不失败任务**。沿用 `summarizer` 的约定：转写稿才是主体产物，
   视觉模型超时 / 显存不够 / 云端 400 都不该让整条任务变红。
   单张失败跳过，全失败就返回 None 并写清原因。
3. **时间范围来自 `frames_index.json`**，不由模型猜。模型对时间戳的幻觉很严重
   （实测会编出 `00:07:38` 这种不存在的时刻），而索引里每个帧都有真实的 `t`。

## 依赖

不直接持有 llama-server：后端由 `ai_backend.resolve()` 决定，
本模块只负责「怎么问」和「怎么落盘」。这样云端多模态接口和本地模型
走的是同一段代码。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from pathlib import Path
from typing import Callable

from app.core import ai_backend
from app.core.errors import TaskCancelled, raise_if_cancelled

ProgressFn = Callable[[str, float], None]

#: 单张拼版图的提问。要求「只抄看到的」是因为 2B 模型在画面信息不足时会编造
#: （实测把 `#2 00:10` 读成 "labeled 00:07:38"），必须明确禁止推测。
SHEET_PROMPT = """这是一张视频截图拼版图，共 {cells} 个画面（按时间顺序，从左上到右下）。

请用中文回答三点，每点一两句话：

1. **文字**：画面里有没有文字（标题、字幕、表格数字、代码、软件界面）？
   有就照抄最关键的几条，**只抄你确实看清的，看不清就说看不清，不要推测**。
2. **主体**：这些画面主要是人物讲话、软件界面、图表数据、产品展示，还是实景场景？
3. **信息点**：有没有能用一句话说清、而光听音频不会知道的信息？

直接给结论，不要复述这段要求。"""


def describe_sheets(
    out_dir: Path,
    cfg: object,
    backend: ai_backend.Backend,
    *,
    title: str = "",
    on_progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
) -> Path | None:
    """读拼版图写 `visual.md`。**没有任何可用图片时返回 None（不算失败）。**"""
    vis = getattr(cfg, "vision", None)
    if vis is None or not getattr(vis, "enabled", False):
        logging.info("跳过画面理解：未启用（设置 → 画面理解）")
        return None

    sheets_dir = Path(out_dir) / "sheets"
    index_path = sheets_dir / "frames_index.json"
    sheets = sorted(sheets_dir.glob("sheet_*.jpg"))
    if not sheets:
        logging.info("跳过画面理解：%s 下没有拼版图（抽帧未开启或未产出）", sheets_dir)
        return None

    # ⚠️ 时间范围必须按**全部**拼版图分配，然后再截断。
    # 反过来做（先截断再按剩下的张数分配）会把「只看前 1 张」算成
    # 「这 1 张覆盖全片 48 帧」，章节时间戳直接错到离谱 —— 2026-10-27 实测踩到。
    spans_all = _sheet_spans(index_path, len(sheets))
    limit = max(1, int(getattr(vis, "max_sheets", 6)))
    if len(sheets) > limit:
        logging.info("画面理解：共 %d 张拼版图，按配置只看前 %d 张", len(sheets), limit)
        sheets = sheets[:limit]
    spans = spans_all[: len(sheets)]
    max_tokens = int(getattr(vis, "max_tokens", 400))

    logging.info("画面理解：%d 张拼版图，后端 %s", len(sheets), backend.describe())
    sections: list[str] = []
    failures = 0
    for i, sheet in enumerate(sheets):
        raise_if_cancelled(cancel_event)
        if on_progress:
            on_progress("summarizing", 0.05 + 0.10 * i / len(sheets))
        start, end, cells = spans[i]
        try:
            text = _ask_sheet(backend, sheet, cells, max_tokens)
        except TaskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — 单张失败不该毁掉整体
            failures += 1
            logging.warning("画面 %02d 理解失败（已跳过）：%s: %s", i + 1, type(exc).__name__, exc)
            continue
        if not text:
            failures += 1
            logging.warning("画面 %02d 返回空内容（已跳过）", i + 1)
            continue
        sections.append(f"## 画面 {i + 1:02d}（{start}–{end}）\n\n{text}")

    if not sections:
        logging.warning("画面理解：%d 张全部失败，未产出 visual.md", len(sheets))
        return None

    header = (
        f"# 画面速览\n\n"
        f"> 由 {backend.describe()} 读取 {len(sections)}/{len(sheets)} 张拼版图"
        + (f"（{failures} 张失败）" if failures else "")
        + "\n\n"
    )
    out = Path(out_dir) / "visual.md"
    out.write_text(header + "\n\n".join(sections) + "\n", encoding="utf-8")
    logging.info("画面理解完成：%s（%d 段，%d 张失败）", out, len(sections), failures)
    return out


def load_visual(out_dir: Path) -> str:
    """读回 `visual.md` 的正文（去掉标题行），供 `summarizer` 注入。

    去标题是因为汇总自己会加 `# 标题` —— 两个 `#` 拼在一起会破坏那份 Markdown 的层级。
    """
    path = Path(out_dir) / "visual.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if text.startswith("#"):
        lines = text.splitlines()
        # 跳过开头连续的标题/引用行，保留正文
        i = 0
        while i < len(lines) and (lines[i].startswith("#") or lines[i].startswith(">")):
            i += 1
        return "\n".join(lines[i:]).strip()
    return text


# ---------------------------------------------------------------- 内部


def _ask_sheet(
    backend: ai_backend.Backend, sheet: Path, cells: int, max_tokens: int
) -> str:
    raw = sheet.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    prompt = SHEET_PROMPT.format(cells=cells)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }
    ]
    return ai_backend.chat(
        backend,
        messages,
        max_tokens=max_tokens,
        temperature=0.0,
        timeout=300,
        # 2B 在长中文上会重复到占满 token（实测连出 60 多个箭头）。
        # 只对本地加：有些云服务不认这个字段会直接 400。
        repeat_penalty=1.15 if backend.is_local else None,
    )


def _sheet_spans(index_path: Path, count: int) -> list[tuple[str, str, int]]:
    """算出每张拼版图对应的时间范围与格子数。

    索引缺失 / 损坏时**不抛错**：退化成「未知时间范围 + 24 格」，
    画面理解照样能做，只是章节里少了精确时刻。
    """
    fallback = [("未知", "未知", 24)] * count
    if not index_path.is_file():
        return fallback
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        frames = data.get("frames") or []
        if not frames:
            return fallback
        per = max(1, -(-len(frames) // count))  # ceil 除法，最后一组可以不满
        spans: list[tuple[str, str, int]] = []
        for i in range(count):
            chunk = frames[i * per : (i + 1) * per]
            if not chunk:
                spans.append(("未知", "未知", per))
                continue
            start = str(chunk[0].get("time") or _fmt(chunk[0].get("t")))
            end = str(chunk[-1].get("time") or _fmt(chunk[-1].get("t")))
            spans.append((start, end, len(chunk)))
        return spans
    except (OSError, ValueError, TypeError) as exc:
        logging.warning("读拼版图索引失败（%s），时间范围降级为未知", exc)
        return fallback


def _fmt(seconds: object) -> str:
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "未知"
    return f"{total // 60:02d}:{total % 60:02d}"
