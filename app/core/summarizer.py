"""汇总阶段：把转写稿压成一份可读的总结（走 OpenAI 兼容的 `/chat/completions`）。

## 为什么存在

2026-10-27 用户实测反馈：「跑通了但只是把字幕转成文本，不是提炼总结」——
当时 `TaskStatus.SUMMARIZING` 与 `config.summarize` 字段都在，**但没有任何实现**，
`pipeline` 里抽完帧直接标 DONE。这个模块补的就是这一环。

## 三个设计决定

1. **失败不失败任务**。转写稿本身已有价值；LLM 抖动 / 超时 / 余额不足不该让
   整条任务变红。所以这里的异常只记 warning、写日志，任务照常 DONE。
   代价是"没生成总结"可能被忽略，所以必须在日志里**明确说原因**。
2. **未配置就跳过，且说清楚**。`summarize.enabled=false` 或环境变量里没有 key 时
   直接跳过（不抛错），日志写「未配置，跳过汇总」——不能让用户以为模型没干活。
3. **长稿分块**。20 分钟的视频转写稿约 2.5 万字（≈13k token），一次塞进去对小模型
   就超了。超过 `MAX_CHARS` 就先按块摘要、再合并（map-reduce）。切块**按行**切，
   保证 `[mm:ss] 文本` 不被切断 —— 时间戳是章节大纲的骨架。

## 走哪家模型

**不在这里决定** —— 由 `app/core/ai_backend.py` 统一选：本地 llama-server 优先，
起不来才回退云端。所以下面这两条路对本模块是同一条：

- 本地：`local_ai.enabled=true` + `<模型目录>/qwen3-vl-2b/` 里有 GGUF
- 云端：`summarize.base_url` + 环境变量里的密钥（`api_key_env`，默认 `VIDIGEST_API_KEY`）

密钥只从环境变量读，**不落配置文件**。都不满足时 `summarize()` 返回 None 并
把**具体原因**写进日志（是「本地没启用」还是「缺哪个变量」，不能只说「没配置」）。

## 画面材料

`visual.md`（由 `vision.py` 产出）作为附加材料拼进提示词。它不是必选项，
没有就退化成纯文本汇总 —— 两条路径共用同一份模板，靠 `{visual}` 占位符的空值区分。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core import ai_backend

#: 单次请求的最大字符数。4.8 万字符 ≈ 2.4 万 token —— 20 分钟的稿子（~2.5 万字）
#: 正好一次吃完，省掉 map-reduce 的三次调用。超过就走分块（每块 1.2 万字）。
#: 换小上下文模型（4k）时要往下调，否则服务端会 400。
MAX_CHARS = 48000

#: 每块的字符数（分块摘要时用），比 MAX_CHARS 小，好让每块都有产出余量。
CHUNK_CHARS = 12000

#: 本地小模型的重复惩罚。2B 在长中文输出上会重复到占满 token 上限
#: （实测连出 60 多个 `→`），1.15 足以压住。云端不加这个字段 ——
#: 部分服务不认它，会直接 400。
_LOCAL_PENALTY = 1.15

SYSTEM_PROMPT = (
    "你是视频内容编辑，擅长把口语化的转写稿整理成结构化的阅读材料。"
    "只依据转写稿里的内容，不要补充外部知识，不要编造数字。"
    "转写稿每行形如 `[mm:ss] 文本`，时间戳可用在章节大纲里。"
)

#: 单遍总结的提示词。要求输出 Markdown，章节带时间戳 —— 这是"能跳回去看"的关键。
ONE_SHOT_TEMPLATE = """下面是视频《{title}》的转写稿（共 {duration}，{lines} 行）。

请输出一份中文 Markdown 总结，严格按这四节：

## 一句话结论
（不超过 40 字，说清这条视频到底讲了什么、值不值得看）

## 章节大纲
（按内容实际推进分段，每段一个 `- **mm:ss** 小标题：一句话说明`；
 时间戳必须来自转写稿里出现过的时刻，不要凭空写）

## 关键要点
（5-8 条。每条一句完整的话，要具体：带上视频里提到的数字、方法名、结论。
 不要写"介绍了…""讲述了…"这种空话）

## 适合谁看
（1-2 句，说清什么人看了有用、什么人可以跳过）

{visual}---
转写稿：

{transcript}
"""

#: 分块摘要（map 阶段）的提示词：只要事实性的压缩，不要成品结构。
CHUNK_TEMPLATE = """这是视频《{title}》转写稿的第 {index}/{total} 段（{start}–{end}）。
请用 150-250 字的中文，压缩出这一段讲了什么。保留：具体数字、方法名、结论、
以及关键时间戳（形如 mm:ss）。不要评价、不要分段标题。

{chunk}
"""

#: 合并（reduce 阶段）：把各段摘要拼成成品。
REDUCE_TEMPLATE = """下面是视频《{title}》按时间顺序的分段摘要（全片 {duration}）。
请把它们合并成一份中文 Markdown 总结，严格按这四节：

## 一句话结论
（不超过 40 字）

## 章节大纲
（`- **mm:ss** 小标题：一句话说明`，时间戳取自分段摘要里出现过的）

## 关键要点
（5-8 条具体要点，带数字与方法名）

## 适合谁看
（1-2 句）

{visual}---
分段摘要：

{chunks}
"""


def skip_reason(cfg: object) -> str | None:
    """返回「开关层为什么不让汇总」；`None` 表示开关是开的。

    ⚠️ 2026-10-27 改：**不再在这里检查密钥**。本地模型可用时没有密钥也能汇总，
    「到底有没有后端」由 `ai_backend.resolve()` 判断。

    把两件事分开是因为处置方式不同：开关关着是用户的意图（静默跳过即可）；
    没有后端是配置问题（必须把缺什么写清楚，否则用户会反复确认自己开没开）。
    """
    sm = getattr(cfg, "summarize", None)
    if sm is None:
        return "配置里没有 summarize 段"
    if not getattr(sm, "enabled", False):
        return "汇总未启用（设置 → 汇总 → 启用）"
    return None


def split_chunks(text: str, chunk_chars: int = CHUNK_CHARS) -> list[str]:
    """按**行**切块，累积到 `chunk_chars` 就断开。

    按行切是为了不把 `[mm:ss] 文本` 拆散；单行超长（转写异常）时才硬切。
    """
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if len(line) > chunk_chars:
            # 极端情况：一行就超了，先冲掉缓冲区，再硬切这一行
            if buf:
                chunks.append("".join(buf))
                buf, size = [], 0
            for i in range(0, len(line), chunk_chars):
                chunks.append(line[i : i + chunk_chars])
            continue
        if size + len(line) > chunk_chars and buf:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


# 请求体的构造与发送已下沉到 `ai_backend.chat()` —— 本地与云端共用同一份，
# 避免出现「云端加了 temperature、本地漏了」这种只在一条路径上复现的漂移。
# 这里不再保留 `build_payload` / `_post`：判据改测 `ai_backend.resolve()` 的选路结果，
# 那才是现在真正决定「用谁、用什么模型」的地方。


def _strip_fence(text: str) -> str:
    """剥掉模型自作主张加上的**外层**代码围栏。

    为什么需要：让模型「输出 Markdown」，相当一部分（实测本地 Qwen3-VL-2B 必犯）
    会理解成「把 Markdown 包在 ```markdown 里」。原样落盘的话 `summary.md`
    在渲染器里会整段变成一个灰色代码块，标题层级全失效。

    两种形态都要处理：**闭合的**（结尾有 ```）和**没闭合的**（模型写到一半被
    max_tokens 截断，只剩开头围栏）—— 后者实测更常见，只判闭合会漏掉。

    只剥最外层：正文里真实的代码块不能被误伤，所以剥完若发现还夹着顶层围栏，
    就还原成原文（那说明这是模型写了多段代码，不是「整份包起来」）。
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    first_nl = t.find("\n")
    if first_nl == -1:
        return t
    body = t[first_nl + 1 :].rstrip()
    if body.endswith("```"):
        body = body[:-3].rstrip()
    if "\n```" in body:
        return t
    return body


def _fmt_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _visual_block(visual_text: str) -> str:
    """把画面材料包成提示词里的一段。空串时返回空 —— 模板原样成立。

    明确告诉模型「这份材料可能不全」：拼版图分辨率只有 320×180 一格，
    小字会读不出。不提醒的话模型会把「没读到」当成「没有」，直接断言
    「视频里没有任何图表」，而画面明明有。
    """
    text = (visual_text or "").strip()
    if not text:
        return ""
    return (
        "---\n"
        "画面材料（从视频截图里读出来的，可能不完整、可能有误读，"
        "只能作为转写稿的补充，不能取代替换）：\n\n"
        f"{text}\n\n"
    )


def summarize(
    transcript_path: Path,
    out_dir: Path,
    cfg: object,
    title: str = "",
    duration_sec: int = 0,
    on_progress=None,
    visual_text: str = "",
) -> Path | None:
    """生成 `summary.md`。**没有可用后端时返回 None（不算失败）**。

    `visual_text` 是 `vision.describe_sheets()` 的产出（已去掉标题行）。
    传空串就是纯文本汇总 —— 这是默认路径，画面理解是可选增强。
    """
    reason = skip_reason(cfg)
    if reason:
        logging.info("跳过汇总：%s", reason)
        return None

    backend, no_backend = ai_backend.resolve(cfg)
    if backend is None:
        # 这里不能只说「没配置」：用户需要知道是本地没启用、还是缺哪个环境变量
        logging.info("跳过汇总：%s", no_backend)
        return None
    logging.info("汇总后端：%s", backend.describe())

    text = Path(transcript_path).read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        logging.warning("跳过汇总：转写稿是空的")
        return None

    if on_progress:
        on_progress("summarizing", 0.15)

    duration = _fmt_ts(duration_sec) if duration_sec else "未知时长"
    lines = text.count("\n") + 1
    visual = _visual_block(visual_text)
    if visual:
        logging.info("汇总：已并入画面材料（%d 字符）", len(visual_text))

    if len(text) <= MAX_CHARS:
        logging.info("汇总：单遍（%d 字符）", len(text))
        body = ai_backend.chat(
            backend,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": ONE_SHOT_TEMPLATE.format(
                        title=title or "未命名",
                        duration=duration,
                        lines=lines,
                        visual=visual,
                        transcript=text,
                    ),
                },
            ],
            max_tokens=2000,
            timeout=300 if backend.is_local else 180,
            repeat_penalty=_LOCAL_PENALTY if backend.is_local else None,
        )
    else:
        chunks = split_chunks(text)
        logging.info("汇总：分块 map-reduce（%d 块）", len(chunks))
        parts: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            if on_progress:
                on_progress("summarizing", 0.15 + 0.7 * (i - 1) / len(chunks))
            try:
                parts.append(
                    ai_backend.chat(
                        backend,
                        [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {
                                "role": "user",
                                "content": CHUNK_TEMPLATE.format(
                                    title=title or "未命名",
                                    index=i,
                                    total=len(chunks),
                                    start=_first_ts(chunk),
                                    end=_last_ts(chunk),
                                    chunk=chunk,
                                ),
                            },
                        ],
                        max_tokens=800,
                        timeout=300 if backend.is_local else 180,
                        repeat_penalty=_LOCAL_PENALTY if backend.is_local else None,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — 单块失败不该毁掉整份
                logging.warning("第 %d/%d 块摘要失败：%s", i, len(chunks), exc)
        if not parts:
            raise RuntimeError("所有分块都失败了，没有可合并的内容")
        body = ai_backend.chat(
            backend,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": REDUCE_TEMPLATE.format(
                        title=title or "未命名",
                        duration=duration,
                        visual=visual,
                        chunks="\n\n".join(f"### 第 {i} 段\n{p}" for i, p in enumerate(parts, 1)),
                    ),
                },
            ],
            max_tokens=2500,
            timeout=300 if backend.is_local else 180,
            repeat_penalty=_LOCAL_PENALTY if backend.is_local else None,
        )

    out = Path(out_dir) / "summary.md"
    header = (
        f"# {title or '视频总结'}\n\n"
        f"> 时长 {duration} · 由 {backend.describe()} 生成"
        + ("（含画面材料）" if visual else "")
        + "\n\n"
    )
    out.write_text(header + _strip_fence(body) + "\n", encoding="utf-8")
    if on_progress:
        on_progress("summarizing", 1.0)
    logging.info("汇总完成：%s（%d 字符）", out, len(body))
    return out


def _first_ts(chunk: str) -> str:
    for line in chunk.splitlines():
        if line.startswith("[") and "]" in line:
            return line[1 : line.index("]")]
    return "未知"


def _last_ts(chunk: str) -> str:
    for line in reversed(chunk.splitlines()):
        if line.startswith("[") and "]" in line:
            return line[1 : line.index("]")]
    return "未知"
