// 主界面的「状态 → 视图」纯函数。
//
// 抽出来的原因：这些判断（尤其是 GPU 的**三态**）是踩过坑的，藏在 JSX 里
// 会被后人顺手简化成 falsy 判断 —— 2026-10-24 就把「没验成」显示成了
// 「GPU 不可用」，把用户引去查驱动。
//
// 本文件不含 JSX，也不 import React：方便在预览页和将来的单测里直接用。

import type { PipelineState, TaskSnapshot, TaskStatus, WorkerState } from "../worker/types";

// ---------------------------------------------------------------- 阶段指示

/** 流水线的四个阶段，与 `PLAN §6` 一致。只用于界面上的「走到哪了」，不是状态机。 */
export const STAGE_NAMES = ["下载", "转写", "抽帧", "汇总"] as const;

export interface StageView {
  /** 当前阶段下标（0-3）；`null` = 还没进入任何阶段（`pending` / 失败前未知） */
  index: number | null;
  /** 当前阶段是否已经开始、还在跑 */
  running: boolean;
}

/**
 * `status` → 阶段。
 *
 * ⚠️ 中间态（`downloaded` / `transcribed` / `framed`）**不是**「完成」，
 * 它们是「上一阶段结束了，下一阶段还在排队」。它们必须映射到**已结束的那个阶段**
 * 并把 `running` 置 false —— 否则「待抽帧」会显示成「抽帧中」，用户以为在跑，
 * 实际在等并发位。
 */
const STAGE_MAP: Record<TaskStatus, StageView> = {
  pending: { index: null, running: false },
  downloading: { index: 0, running: true },
  downloaded: { index: 0, running: false },
  transcribing: { index: 1, running: true },
  transcribed: { index: 1, running: false },
  framing: { index: 2, running: true },
  framed: { index: 2, running: false },
  summarizing: { index: 3, running: true },
  done: { index: 3, running: false },
  failed: { index: null, running: false },
  cancelled: { index: null, running: false },
};

export function stageView(status: TaskStatus): StageView {
  return STAGE_MAP[status] ?? { index: null, running: false };
}

/**
 * 阶段视图 → 四段的填充状态。
 *
 * `failed` / `cancelled` 的阶段已经不可信（失败可能发生在任何一段），
 * 一律返回 `null`，让调用方改显示 `stage` 文本 —— **不要猜**它停在哪。
 */
export function stageFill(status: TaskStatus): ("done" | "active" | "todo")[] | null {
  const { index, running } = stageView(status);
  if (index === null) return null;
  return STAGE_NAMES.map((_, i) => {
    if (i < index) return "done";
    if (i > index) return "todo";
    // `done` 状态：最后一段也是完成
    if (status === "done") return "done";
    return running ? "active" : "done";
  });
}

// ---------------------------------------------------------------- 连接与 GPU

/** 模型问题的 reason 码（`app/core/env_probe.py` 的 `REASON_*`） */
const MODEL_REASONS = new Set(["model_missing", "model_incomplete"]);

export interface ChipView {
  text: string;
  tone: "muted" | "active" | "ok" | "warn" | "error";
  title: string;
  /** 非空表示这个 chip 可以点，点了跳设置 */
  action?: "settings";
}

/** worker 连接的短标签。版本号不再显示 —— 它对用户没有决策价值，挪进 tooltip。 */
export function connView(w: WorkerState | null): ChipView {
  if (!w) return { text: "连接中", tone: "muted", title: "" };
  switch (w.state) {
    case "ready":
      return {
        text: "已连接",
        tone: "ok",
        title: `worker ${w.server_version ?? ""} 运行中`.trim(),
      };
    case "starting":
      return { text: "启动中", tone: "warn", title: w.detail };
    case "restarting":
      // 带上计数，让「这是第几次救」可见 —— 自动重启只有一次，看到 1/1
      // 就知道下次再挂就得手动点了
      return { text: `重启中 ${w.restart_count}/1`, tone: "warn", title: w.detail };
    // 这里说的是**连接**，不是「worker 怎么了」——后者归 alert 条说。
    // 两处都写「worker 已退出」会出现同一条信息占两个位置（改版前就是这样）。
    case "failed":
      return { text: "未连接", tone: "error", title: w.detail };
    case "exited":
      return { text: "连接中断", tone: "error", title: w.detail };
    default:
      return { text: w.state, tone: "muted", title: w.detail };
  }
}

/**
 * GPU 四态。**`gpu_verified === null` 是「尚未验证」，不是「不可用」。**
 *
 * 1. 还没验完（`gpu_verified === null`）→ 检测中
 * 2. 验了但没验成（`gpu_usable === null`）→ 按 `blocked_reason` 分成
 *    「模型未安装」（去下载）与「未能验证」（去设置）
 * 3. `true` → 可用
 * 4. `false` → 确实不可用
 */
export function gpuView(w: WorkerState | null): ChipView {
  if (!w) return { text: "GPU 待检测", tone: "muted", title: "" };
  if (w.state === "failed") return { text: "GPU 未知", tone: "error", title: w.detail };
  if (w.state === "exited") return { text: "GPU 未知", tone: "error", title: w.detail };
  // 重启中：上一代的 GPU 结论已经作废，**不能**继续显示「GPU 可用」
  if (w.state === "restarting") return { text: "GPU 待检测", tone: "warn", title: w.detail };

  if (w.gpu_verified === null) {
    return {
      text: "GPU 检测中",
      tone: "warn",
      title: "快速探测已通过，正在后台跑一次真实推理做最终确认",
    };
  }

  if (w.gpu_usable === null) {
    const detail = w.env_detail ?? "";
    if (w.blocked_reason && MODEL_REASONS.has(w.blocked_reason)) {
      return {
        text: "模型未安装",
        tone: "warn",
        title: `${detail}\n\n点这里打开设置下载模型（约 2.9 GiB）。此时无法转写。`,
        action: "settings",
      };
    }
    return {
      text: "GPU 未能验证",
      tone: "warn",
      title: `${detail || "验证未完成"}${w.blocked_reason ? `\n原因码：${w.blocked_reason}` : ""}\n\n点这里打开设置`,
      action: "settings",
    };
  }

  if (w.gpu_usable) {
    return { text: "GPU 可用", tone: "ok", title: w.env_detail ?? "已通过真实推理验证" };
  }
  return {
    text: "GPU 不可用",
    tone: "error",
    title: `${w.env_detail ?? ""}\n将按 CPU 模式规划并发（吞吐下降约 6 倍），详见日志区`,
  };
}

/** 并发明细。工程指标，只进 tooltip 与小字，不占状态区位置。 */
export function concurrencyText(w: WorkerState | null): string {
  const c = w?.concurrency;
  if (!c) return "";
  return `并发 转写 ${c.n_asr} / 下载 ${c.n_dl} / 抽帧 ${c.n_frame} / 汇总 ${c.n_sum}`;
}

export const PIPELINE_LABELS: Record<PipelineState, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "已暂停",
  aborted: "已中止",
  stopped: "已停止",
};

// ---------------------------------------------------------------- 队列汇总

export interface QueueSummary {
  total: number;
  /** 正在推进的条数（`downloading` / `transcribing` / `framing` / `summarizing`） */
  active: number;
  pending: number;
  done: number;
  failed: number;
}

export function summarize(tasks: TaskSnapshot[]): QueueSummary {
  const s: QueueSummary = { total: tasks.length, active: 0, pending: 0, done: 0, failed: 0 };
  for (const t of tasks) {
    if (t.status === "done") s.done++;
    else if (t.status === "failed") s.failed++;
    else if (t.status === "pending" || t.status === "cancelled") s.pending++;
    else if (STAGE_MAP[t.status]?.running) s.active++;
    else s.pending++; // downloaded / transcribed / framed：排队等下一阶段
  }
  return s;
}
