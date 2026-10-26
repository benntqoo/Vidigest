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

`base_url` + `model` 都是配置项，任何 OpenAI 兼容服务都能用：
- DeepSeek：`https://api.deepseek.com/v1` + `deepseek-chat`
- 通义/千问：`https://dashscope.aliyuncs.com/compatible-mode/v1` + `qwen-plus`
- 本地 Ollama：`http://127.0.0.1:11434/v1` + `qwen2.5:14b`
密钥只从环境变量读（`api_key_env`，默认 `VIDIGEST_API_KEY`），**不落配置文件**。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

#: 单次请求的最大字符数。4.8 万字符 ≈ 2.4 万 token —— 20 分钟的稿子（~2.5 万字）
#: 正好一次吃完，省掉 map-reduce 的三次调用。超过就走分块（每块 1.2 万字）。
#: 换小上下文模型（4k）时要往下调，否则服务端会 400。
MAX_CHARS = 48000

#: 每块的字符数（分块摘要时用），比 MAX_CHARS 小，好让每块都有产出余量。
CHUNK_CHARS = 12000

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

---
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

---
分段摘要：

{chunks}
"""


def api_key(cfg: object) -> str:
    """从环境变量取密钥。**永远不从配置文件读** —— 那是约定（见 IPC 规格 §3.2）。"""
    env_name = getattr(getattr(cfg, "summarize", None), "api_key_env", "") or "VIDIGEST_API_KEY"
    return (os.environ.get(env_name) or "").strip()


def skip_reason(cfg: object) -> str | None:
    """返回「为什么这次不汇总」；`None` 表示可以汇总。

    分开返回原因而不是一个布尔：日志里要能说清是"没开"还是"没配 key"，
    否则用户会反复确认自己到底开没开。
    """
    sm = getattr(cfg, "summarize", None)
    if sm is None:
        return "配置里没有 summarize 段"
    if not getattr(sm, "enabled", False):
        return "汇总未启用（设置 → 汇总 → 启用）"
    if not api_key(cfg):
        env_name = getattr(sm, "api_key_env", None) or "VIDIGEST_API_KEY"
        return f"环境变量 {env_name} 为空（密钥只从环境变量读）"
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


def build_payload(
    cfg: object, messages: list[dict[str, str]], max_tokens: int = 2000
) -> dict:
    """构造请求体。抽成纯函数，方便判据离线检查（模型名/温度/消息结构）。"""
    sm = getattr(cfg, "summarize", None)
    return {
        "model": getattr(sm, "model", "gpt-4o"),
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }


def _post(cfg: object, messages: list[dict[str, str]], timeout: int = 180) -> str:
    """调一次 chat/completions，返回正文文本。异常向上抛，由调用方决定怎么处理。"""
    import requests

    sm = getattr(cfg, "summarize", None)
    base = (getattr(sm, "base_url", "") or "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key(cfg)}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        url, headers=headers, json=build_payload(cfg, messages), timeout=timeout
    )
    if resp.status_code != 200:
        # 把服务端的错误信息带出来：401 / 402 / 404 的处理方式完全不同
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"响应结构不认识：{json.dumps(data, ensure_ascii=False)[:300]}") from exc


def _fmt_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def summarize(
    transcript_path: Path,
    out_dir: Path,
    cfg: object,
    title: str = "",
    duration_sec: int = 0,
    on_progress=None,
) -> Path | None:
    """生成 `summary.md`。**未配置返回 None（不算失败）**；调用方的异常处理见模块说明。"""
    reason = skip_reason(cfg)
    if reason:
        logging.info("跳过汇总：%s", reason)
        return None

    text = Path(transcript_path).read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        logging.warning("跳过汇总：转写稿是空的")
        return None

    if on_progress:
        on_progress("summarizing", 0.15)

    duration = _fmt_ts(duration_sec) if duration_sec else "未知时长"
    lines = text.count("\n") + 1

    if len(text) <= MAX_CHARS:
        logging.info("汇总：单遍（%d 字符）", len(text))
        body = _post(
            cfg,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": ONE_SHOT_TEMPLATE.format(
                        title=title or "未命名", duration=duration, lines=lines, transcript=text
                    ),
                },
            ],
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
                    _post(
                        cfg,
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
                    )
                )
            except Exception as exc:  # noqa: BLE001 — 单块失败不该毁掉整份
                logging.warning("第 %d/%d 块摘要失败：%s", i, len(chunks), exc)
        if not parts:
            raise RuntimeError("所有分块都失败了，没有可合并的内容")
        body = _post(
            cfg,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": REDUCE_TEMPLATE.format(
                        title=title or "未命名",
                        duration=duration,
                        chunks="\n\n".join(f"### 第 {i} 段\n{p}" for i, p in enumerate(parts, 1)),
                    ),
                },
            ],
            max_tokens=2500,
        )

    out = Path(out_dir) / "summary.md"
    header = f"# {title or '视频总结'}\n\n> 时长 {duration} · 由 {getattr(cfg.summarize, 'model', '')} 生成\n\n"
    out.write_text(header + body.strip() + "\n", encoding="utf-8")
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
