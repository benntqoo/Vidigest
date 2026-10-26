// 主界面预览（开发用，不进构建产物）。
//
// 为什么需要它：主界面的几个问题（800×600 默认窗口下标题列被挤没、工具栏两行、
// 日志区常驻 168px、8 个状态里 6 个都是同一个蓝色）**只有把真数据摆上去才看得见**，
// 读代码读不出来。和设置页的 `preview.html` 是同一套办法。
//
// 用法：`npx vite --port 1430` → http://localhost:1430/main-preview.html
//   `?state=empty`   空队列
//   `?state=broken`  worker 崩了 + GPU 未验证
//   `?state=nomodel` 模型未安装（状态栏应给可点的「去下载」）
//   `&w=800&h=600`   按默认窗口尺寸量一遍（默认就是 800×600）
//
// ⚠️ 这里**必须**引 `App.css`：不引的话 `--accent` / `.chip` / 按钮样式全缺，
// 截出来的是一张「像但不对」的图（设置页首次截图就是这么被骗的）。

import React from "react";
import { createRoot } from "react-dom/client";

import "./App.css";
import { TopBar } from "./components/TopBar";
import { LogPanel, type LogEntry } from "./components/LogPanel";
import { TaskTable } from "./components/TaskTable";
import type { TaskSnapshot, WorkerState } from "./worker/types";

const params = new URLSearchParams(location.search);
const state = params.get("state") ?? "busy";

const mockTasks: TaskSnapshot[] = [
  {
    task_id: 12,
    url: "https://v.douyin.com/iRxAbCdE/",
    platform: "douyin",
    title: "用一台旧笔记本跑本地大模型，实际能到什么程度？实测 7B 量化版的吞吐与显存占用",
    duration_sec: 743,
    status: "transcribing",
    stage: "转写",
    progress: 0.42,
    elapsed_sec: 128,
    retry_count: 0,
    out_dir: "D:\\Vidigest\\work\\12",
    error: null,
  },
  {
    task_id: 11,
    url: "https://www.bilibili.com/video/BV1xx411c7mD",
    platform: "bilibili",
    title: "三分钟讲清楚向量数据库到底解决了什么问题",
    duration_sec: 188,
    status: "downloading",
    stage: "下载",
    progress: -1,
    elapsed_sec: 6,
    retry_count: 0,
    out_dir: "",
    error: null,
  },
  {
    task_id: 10,
    url: "https://v.douyin.com/iRxFgHiJk/",
    platform: "douyin",
    title: "拆解一台 2008 年的卡片机，看看当年的 CCD 到底强在哪",
    duration_sec: 522,
    status: "done",
    stage: "完成",
    progress: 1,
    elapsed_sec: 311,
    retry_count: 0,
    out_dir: "D:\\Vidigest\\work\\10",
    error: null,
  },
  {
    task_id: 9,
    url: "https://v.douyin.com/iRxLmNoPq/",
    platform: "douyin",
    title: "为什么你的降噪耳机越戴越闷",
    duration_sec: 96,
    status: "failed",
    stage: "转写",
    progress: 0.71,
    elapsed_sec: 44,
    retry_count: 2,
    out_dir: "D:\\Vidigest\\work\\9",
    error: "cookie 已失效，下载中断（E_COOKIE_EXPIRED）",
  },
  {
    task_id: 8,
    url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    platform: "youtube",
    title: "A 40-minute talk about GPU memory hierarchy that I keep re-watching",
    duration_sec: 2412,
    status: "pending",
    stage: "等待",
    progress: 0,
    elapsed_sec: 0,
    retry_count: 0,
    out_dir: "",
    error: null,
  },
  {
    task_id: 7,
    url: "https://v.douyin.com/iRxTuVwXy/",
    platform: "douyin",
    title: "厨房收纳：把抽屉高度重新分一遍就够用了",
    duration_sec: 143,
    status: "cancelled",
    stage: "取消",
    progress: 0.2,
    elapsed_sec: 19,
    retry_count: 0,
    out_dir: "",
    error: null,
  },
  {
    task_id: 6,
    url: "https://v.douyin.com/iRxZaBcDe/",
    platform: "douyin",
    title: "把机械键盘的轴体全部换成静音红，声音差别有多大",
    duration_sec: 407,
    status: "summarizing",
    stage: "汇总",
    progress: -1,
    elapsed_sec: 233,
    retry_count: 0,
    out_dir: "D:\\Vidigest\\work\\6",
    error: null,
  },
];

function workerFor(kind: string): WorkerState {
  const base: WorkerState = {
    state: "ready",
    detail: "",
    server_version: "0.6.2",
    gpu_usable: true,
    gpu_verified: true,
    concurrency: { n_asr: 2, n_dl: 4, n_frame: 2, n_sum: 1 },
    alive: true,
    restart_count: 0,
    pipeline_state: "running",
    blocked_reason: null,
    env_detail: "CUDA 12.8 · RTX 4060 Ti 16 GB · cuDNN 就绪",
  };
  if (kind === "broken") {
    return {
      ...base,
      state: "exited",
      detail: "worker 进程意外退出（退出码 1）：CUDA out of memory\nat app/core/pipeline.py:412",
      gpu_usable: null,
      gpu_verified: true,
      concurrency: null,
      alive: false,
      pipeline_state: "aborted",
      blocked_reason: "verify_timeout",
      env_detail: "验证超时（120 s）",
    };
  }
  if (kind === "nomodel") {
    return {
      ...base,
      gpu_usable: null,
      gpu_verified: true,
      blocked_reason: "model_missing",
      env_detail: "faster-whisper 模型目录为空",
      concurrency: { n_asr: 0, n_dl: 4, n_frame: 2, n_sum: 1 },
      pipeline_state: "idle",
    };
  }
  return base;
}

const mockLogs: LogEntry[] = [
  { key: 1, level: "info", msg: "worker 已就绪，协议 v1，GPU 真实推理校验通过", source: "rpc" },
  { key: 2, level: "info", msg: "入队 3 条，跳过 1 条" },
  { key: 3, level: "warning", msg: "跳过 https://v.douyin.com/xxxx/（无法从分享文案中抽出 URL）" },
  { key: 4, level: "info", msg: "#11 下载中… 已完成 38%，速度 6.2 MiB/s" },
  { key: 5, level: "error", msg: "E_COOKIE_EXPIRED：#9 cookie 已失效，下载中断" },
  { key: 6, level: "info", msg: "#12 转写 42%（segment 118/281）" },
];

const tasks = state === "empty" ? [] : mockTasks;

function Preview() {
  // 默认铺满视口，靠**截图时的窗口尺寸**模拟真实窗口（`tauri.conf.json` 是 800×600）。
  // ⚠️ 不要用 `width:800px` 的固定容器来假装 800px 窗口：CSS 的 `@media`
  // 看的是 **viewport**，不是容器宽度 —— 固定容器会让媒体查询永远不触发，
  // 于是「窄窗口下隐藏耗时列」这类规则实测不出来（第一版就这么骗过自己）。
  const w = params.get("w");
  const h = params.get("h");
  const box: React.CSSProperties = w
    ? { width: Number(w), height: Number(h ?? 600), margin: "0 auto", border: "1px solid #c9ccd3" }
    : { width: "100%", height: "100%" };

  const pipeline = state === "broken" ? "aborted" : state === "empty" ? "idle" : "running";

  // `?logs=1` 展开日志区。日志的折叠态是默认态，展开态也必须看一眼 ——
  // 展开时它会挤掉列表高度，而列表正是这个界面唯一的主内容。
  React.useEffect(() => {
    if (params.get("logs") !== "1") return;
    const t = setTimeout(() => {
      document.querySelector<HTMLElement>(".logpanel-head")?.click();
    }, 300);
    return () => clearTimeout(t);
  }, []);

  return (
    <div
      style={{ ...box, display: "flex", flexDirection: "column", background: "var(--bg)", overflow: "hidden" }}
    >
      <div className="app">
        <TopBar
          worker={workerFor(state)}
          busy={state !== "broken"}
          pipeline={pipeline}
          canRestart={state === "broken"}
          failedCount={tasks.filter((t) => t.status === "failed").length}
          composerOpen={params.get("composer") === "1"}
          onComposerOpenChange={() => {}}
          onAddTasks={() => {}}
          onStart={() => {}}
          onPause={() => {}}
          onResume={() => {}}
          onRestart={() => {}}
          onOpenSettings={() => {}}
        />
        <TaskTable
          tasks={tasks}
          onCancel={() => {}}
          onRetry={() => {}}
          onRemove={() => {}}
          onOpenDir={() => {}}
          onOpenTranscript={() => {}}
          onAddClick={() => {}}
        />
        <LogPanel entries={mockLogs} onClear={() => {}} />
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Preview />
  </React.StrictMode>,
);
