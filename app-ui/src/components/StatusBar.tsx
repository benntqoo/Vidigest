// 状态栏：连接状态 / GPU 可用性 / 实际并发 / 调度器状态。
//
// ⚠️ `gpu_verified === null` 表示**尚未验证**，不是「不可用」。
// 这里必须显示成「检测中…」，与 `PLAN §9.1` 的三态布尔约定一致。
//
// 2026-10-24 补第四态：`gpu_usable === null`（验了但没验成）+ `blocked_reason`
// 指向模型问题 ⇒ 显示「模型未安装」。**不能**显示「GPU 不可用」——那是两个完全
// 不同的结论，后者会把用户引向驱动排查，而真正要做的是下一个模型。

import type { PipelineState, WorkerState } from "../worker/types";

interface Props {
  worker: WorkerState | null;
  pipeline: PipelineState;
  onOpenSettings?: () => void;
}

const PIPELINE_LABELS: Record<PipelineState, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "已暂停",
  aborted: "已中止",
  stopped: "已停止",
};

/** 模型问题的 reason 码（`app/core/env_probe.py` 的 `REASON_*`） */
const MODEL_REASONS = new Set(["model_missing", "model_incomplete"]);

interface GpuView {
  text: string;
  tone: string;
  title: string;
  /** 非空表示这个 chip 可以点，点了跳设置 */
  action?: "settings";
}

function gpuView(w: WorkerState | null): GpuView {
  if (!w) return { text: "连接中…", tone: "muted", title: "" };
  if (w.state === "failed")
    return { text: "worker 启动失败", tone: "error", title: w.detail };
  if (w.state === "exited")
    return { text: "worker 已退出", tone: "error", title: w.detail };
  // 重启中：上一代的 GPU 结论已经作废，**不能**继续显示「GPU 可用」
  // （Rust 侧 `spawn_worker` 会把这些字段清空，所以这里也不会走到下面的分支）
  if (w.state === "restarting")
    return { text: "worker 重启中…", tone: "warn", title: w.detail };

  // 三态：null 是「未知」，别用 falsy 判断
  if (w.gpu_verified === null) {
    return {
      text: "GPU 检测中…",
      tone: "warn",
      title: "快速探测已通过，正在后台跑一次真实推理做最终确认",
    };
  }

  // 第四态：验了，但**没验成**（gpu_usable === null）。原因是环境缺失，
  // 不是硬件坏了。这里必须按 blocked_reason 分开说。
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
  // 到这里是「验过，确实不能用」
  return {
    text: "GPU 不可用",
    tone: "error",
    title: `${w.env_detail ?? ""}\n将按 CPU 模式规划并发（吞吐下降约 6 倍），详见日志区`,
  };
}

export function StatusBar({ worker, pipeline, onOpenSettings }: Props) {
  const gpu = gpuView(worker);
  const conc = worker?.concurrency;
  const connTone =
    worker?.state === "ready"
      ? "ok"
      : worker?.state === "starting" || worker?.state === "restarting"
        ? "warn"
        : worker?.state === "failed" || worker?.state === "exited"
          ? "error"
          : "muted";

  const connText = (() => {
    if (!worker) return "connecting";
    switch (worker.state) {
      case "ready":
        return `worker ${worker.server_version ?? ""}`.trim();
      case "starting":
        return "worker 启动中…";
      case "restarting":
        // 带上计数，让「这是第几次救」可见 —— 自动重启只有一次，看到 1/1
        // 就知道下次再挂就得手动点了
        return `worker 重启中（${worker.restart_count}/1）`;
      case "failed":
        return "worker 启动失败";
      case "exited":
        return "worker 已退出";
      default:
        return worker.state;
    }
  })();

  return (
    <div className="statusbar">
      <span className={`chip chip-${connTone}`} title={worker?.detail ?? ""}>
        <span className="dot" />
        {connText}
      </span>

      {/* 可点的 chip：`<button>` 而不是给 span 加 onClick —— 键盘可达、有焦点框。
          这里唯一能做的事就是去设置，所以点整个 chip 都是安全的。 */}
      {gpu.action === "settings" && onOpenSettings ? (
        <button
          type="button"
          className={`chip chip-${gpu.tone} chip-action`}
          title={gpu.title}
          onClick={onOpenSettings}
        >
          {gpu.text}
        </button>
      ) : (
        <span className={`chip chip-${gpu.tone}`} title={gpu.title}>
          {gpu.text}
        </span>
      )}

      {conc && (
        <span className="chip chip-muted" title="实际并发：转写 / 下载 / 抽帧 / 汇总">
          并发 转写{conc.n_asr} 下载{conc.n_dl} 抽帧{conc.n_frame}
        </span>
      )}

      <span className={`chip chip-${pipeline === "running" ? "active" : "muted"}`}>
        流水线 {PIPELINE_LABELS[pipeline] ?? pipeline}
      </span>

      <span className="spacer" />

      {/*
        `failed` / `exited` / `restarting` 都要显示原因。
        前两者对用户是同一件事：worker 不在了、按钮全灰、什么都做不了 —— 此刻唯一的
        诉求就是「为什么」。把 detail 只放在 chip 的 tooltip 里，等于没给。
        （实测：只认 `failed` 时，意外退出只显示「worker 已退出」，原因一个字都看不到。）
        `restarting` 一并列出，是因为它正是「出过事」的那几秒。
      */}
      {worker &&
        (worker.state === "failed" ||
          worker.state === "exited" ||
          worker.state === "restarting") && (
          <span className="statusbar-detail" title={worker.detail}>
            {worker.detail.split("\n")[0]}
          </span>
        )}
    </div>
  );
}
