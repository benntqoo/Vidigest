// 状态栏：连接状态 / GPU 可用性 / 实际并发 / 调度器状态。
//
// ⚠️ `gpu_verified === null` 表示**尚未验证**，不是「不可用」。
// 这里必须显示成「检测中…」，与 `PLAN §9.1` 的三态布尔约定一致。

import type { PipelineState, WorkerState } from "../worker/types";

interface Props {
  worker: WorkerState | null;
  pipeline: PipelineState;
}

const PIPELINE_LABELS: Record<PipelineState, string> = {
  idle: "空闲",
  running: "运行中",
  paused: "已暂停",
  aborted: "已中止",
  stopped: "已停止",
};

function gpuView(w: WorkerState | null): { text: string; tone: string; title: string } {
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
  if (w.gpu_verified && w.gpu_usable) {
    return { text: "GPU 可用", tone: "ok", title: "已通过真实推理验证" };
  }
  if (!w.gpu_usable) {
    return {
      text: "GPU 不可用",
      tone: "error",
      title: "将按 CPU 模式规划并发（吞吐下降约 6 倍），详见日志区",
    };
  }
  return { text: "GPU 状态未知", tone: "muted", title: "" };
}

export function StatusBar({ worker, pipeline }: Props) {
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

      <span className={`chip chip-${gpu.tone}`} title={gpu.title}>
        {gpu.text}
      </span>

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
