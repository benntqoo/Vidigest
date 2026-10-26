// 协议类型定义。
//
// ⚠️ 这些类型是 `docs/IPC协议规格.md` 的镜像。规格改了就改这里，不要各写各的。
// 字段名与 Rust 侧 `worker/protocol.rs`、Python 侧 `app/rpc.py` 三方一致。

/** 协议主版本（规格 §2） */
export const PROTOCOL_VERSION = 1;

/** Python → Rust → 前端 的事件信封（规格 §2.2） */
export interface WorkerEvent {
  v: number;
  type: string;
  name: string;
  /** 对某条命令的响应会回填命令 id；主动推送时为 null */
  id: string | null;
  /** 自增序号，从 1 开始 */
  seq: number;
  ts: number;
  data: Record<string, unknown>;
}

/** Rust 侧维护的连接状态快照（`worker/mod.rs::StateSnapshot`） */
export interface WorkerState {
  /**
   * `starting`   —— 首次启动，还没握过手
   * `restarting` —— 崩溃后正在自动重启（**已出过一次事**，与 `starting` 不是一回事）
   * `ready`      —— 握手完成，可下命令
   * `failed`     —— 起不来（定位解释器失败 / spawn 失败 / 握手发送失败）
   * `exited`     —— 曾经起过，现在不在了（正常退出或崩溃恢复失败）
   */
  state: "starting" | "restarting" | "ready" | "failed" | "exited";
  detail: string;
  server_version: string | null;
  gpu_usable: boolean | null;
  /**
   * ⚠️ 三态布尔，**不要用 `!gpu_verified` 判断不可用**。
   * null = 尚未验证（不是「否」）——规格 §4.2。
   */
  gpu_verified: boolean | null;
  concurrency: Concurrency | null;
  alive: boolean;
  /** 已自动重启次数。Rust 侧以原子量为唯一真源，其余是它的投影 */
  restart_count: number;
  /**
   * 调度器状态。Rust 侧从 `ready` / `pipeline_state` 事件累积而来，
   * 所以前端挂载时能一次性拿到，不必等下一次状态跃迁。
   */
  pipeline_state: PipelineState | null;
  /**
   * **为什么没能验证 GPU**：`model_missing` / `model_incomplete` /
   * `import_failed` / `cuda_load_failed` / `cuda_inference_failed` / `verify_timeout`。
   *
   * ⚠️ 只在 `gpu_usable === null` 时有意义。它是「没验成」的原因，
   * 与「验过，确实不能用」（`gpu_usable === false`）是两件事 ——
   * 2026-10-24 的误报就是这两者被混成一个 `false`，界面显示「GPU 不可用」，
   * 把用户引向驱动排查，方向完全错。
   */
  blocked_reason: string | null;
  /** 验证结论的原始说明，状态栏 tooltip 用 */
  env_detail: string | null;
}

export interface Concurrency {
  n_asr: number;
  n_dl: number;
  n_frame: number;
  n_sum: number;
}

/** task 快照（规格 §5.1），`task_list` 与 `task_update` 共用 */
export interface TaskSnapshot {
  task_id: number;
  url: string;
  platform: string;
  title: string;
  duration_sec: number;
  status: TaskStatus;
  /** 中文阶段名，由 Python 侧 `rpc.STAGE_LABELS` 给出 */
  stage: string;
  /** -1.0 表示「进行中但进度不可知」，**不要显示成 0%**（规格 §5） */
  progress: number;
  elapsed_sec: number;
  retry_count: number;
  out_dir: string;
  error: string | null;
}

/** 状态机取值，与 `PLAN §6` 完全一致（规格 §5） */
export type TaskStatus =
  | "pending"
  | "downloading"
  | "downloaded"
  | "transcribing"
  | "transcribed"
  | "framing"
  | "summarizing"
  | "done"
  | "failed"
  | "cancelled";

/**
 * 「已停止推进」的状态集合 —— **只用于界面渲染**（决定要不要显示取消按钮、
 * 进度条要不要渲染不确定态）。
 *
 * ⚠️ 名字不是 `TERMINAL_STATUSES`：本项目的**终态只有 `done` 与 `cancelled`**
 * （`PLAN §6` / `app/models/task.py::TERMINAL_STATUSES`）。`failed` 不是终态——
 * 它必须显式 `retry` 才会重跑。这里把它算进来，只是因为「界面不再需要显示取消按钮」。
 * 两者含义不同，不要合并。
 */
export const SETTLED_STATUSES: ReadonlySet<TaskStatus> = new Set<TaskStatus>([
  "done",
  "failed",
  "cancelled",
]);

/** 调度器状态（`pipeline_state` 事件） */
export type PipelineState = "idle" | "running" | "paused" | "aborted" | "stopped";

/** 各状态的界面表现 */
export const STATUS_META: Record<TaskStatus, { label: string; tone: Tone }> = {
  pending: { label: "等待中", tone: "muted" },
  downloading: { label: "下载中", tone: "active" },
  downloaded: { label: "待抽音频", tone: "active" },
  transcribing: { label: "转写中", tone: "active" },
  transcribed: { label: "待抽帧", tone: "active" },
  framing: { label: "抽帧中", tone: "active" },
  summarizing: { label: "汇总中", tone: "active" },
  done: { label: "完成", tone: "ok" },
  // `failed` 与 `cancelled` 刻意不同色：前者是故障，后者是用户意图（规格 §5）
  failed: { label: "失败", tone: "error" },
  cancelled: { label: "已取消", tone: "warn" },
};

export type Tone = "muted" | "active" | "ok" | "warn" | "error";

/** 错误码 → 可操作提示（规格 §8） */
export const ERROR_HINTS: Record<string, string> = {
  E_COOKIE_EXPIRED: "cookie 已失效。请重新采集后重试该任务。",
  E_FFMPEG_MISSING: "定位不到可用的 ffmpeg。请安装后重启程序。",
  E_ENV_FATAL: "CUDA / 模型加载失败，整批已中止。修复环境后需手动重试。",
  E_DISK_FULL: "磁盘剩余空间不足 5 GB，已暂停取新任务。",
  E_BAD_ARGS: "命令参数有误（通常是前端与 worker 版本不一致）。",
  E_UNKNOWN_CMD: "worker 不认识这条命令，通常是版本不匹配。",
  E_INTERNAL: "worker 内部异常。请查看日志区的 traceback。",
};

/** 把秒格式化成 `m:ss` / `h:mm:ss` */
export function formatDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return "--";
  const s = Math.floor(sec % 60);
  const m = Math.floor((sec / 60) % 60);
  const h = Math.floor(sec / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** 进度显示。**-1 表示不可知**，返回 null 让调用方渲染不确定态（规格 §5） */
export function formatProgress(p: number): string | null {
  if (!Number.isFinite(p) || p < 0) return null;
  return `${Math.round(p * 100)}%`;
}

// ------------------------------------------------------------ 设置与模型

/**
 * 配置文件结构（`app/models/config.py` 的镜像）。
 *
 * ⚠️ 只有 `get_config` 会返回这份东西；写回走 `set_config` 的**部分更新**，
 * 不是整份覆盖。`cookie` 段刻意不在这里 —— 它不在 `EDITABLE_SECTIONS` 里，
 * 界面无从修改（cookie 采集是阶段 4 的事）。
 */
export interface AppConfig {
  paths: {
    work_dir: string;
    /** false = 首启自动生成、用户还没确认过 ⇒ 弹一次工作目录引导 */
    work_dir_confirmed: boolean;
    model_dir: string;
    ffmpeg: string | null;
    yt_dlp: string | null;
    cookies_file: string | null;
  };
  runtime: {
    device: string;
    compute_type: string;
    model: string;
    auto_concurrency: boolean;
    max_download: number;
    max_frame: number;
    max_summarize: number;
  };
  transcribe: {
    language: string;
    beam_size: number;
    vad_filter: boolean;
    vad_min_silence_ms: number;
    initial_prompt: string;
  };
  frames: {
    enabled: boolean;
    scene_threshold: number;
    max_frames: number;
    sheet_cols: number;
    sheet_rows: number;
  };
  summarize: {
    enabled: boolean;
    base_url: string;
    api_key_env: string;
    model: string;
    template: string;
  };
  download: {
    rate_limit_per_min: number;
    keep_intermediate: boolean;
    base_url: string | null;
  };
}

/** `get_config` 的响应（规格 §3.1） */
export interface ConfigInfo {
  path: string;
  is_default: boolean;
  config: AppConfig;
  /**
   * 转写语言下拉的选项。**由后端下发**（`config.py::LANGUAGE_LABELS` 是唯一真源）——
   * 前端不硬编码第二份表，否则两份必然漂移且漂移后不会有人发现。
   * 首项 `code === ""` 表示「自动检测」。
   */
  languages: { code: string; label: string }[];
  /**
   * 安装目录；`null` = 源码运行（此时不校验「目录不能在安装目录里」，
   * 因为开发布局里 `work/` 本来就在仓库根）。
   * 界面用它做即时提示，后端 `set_config` 用同一条规则做硬拦。
   */
  install_dir: string | null;
  model_path: string;
  model_present: boolean;
  model_missing_files: string[];
  model_size_bytes: number;
  /** `transcribe.beam_size` 等「改了会让 662 段基线失效」的字段 */
  read_only_fields: string[];
  api_key_env: string;
  api_key_present: boolean;
  pipeline_state: PipelineState;
  download: { running?: boolean };
}

/** 模型下载的界面状态。事件是增量推送的，这里合成一个可渲染的快照。 */
export interface DownloadState {
  phase: "idle" | "running" | "verifying" | "done" | "failed" | "cancelled";
  file?: string;
  fileIndex?: number;
  fileCount?: number;
  doneBytes: number;
  totalBytes: number;
  /** 0..1；0 表示「还不知道总量」 */
  pct: number;
  /** 字节/秒 */
  speed: number;
  /** 剩余秒数；0 表示算不出来 */
  eta: number;
  detail?: string;
  code?: string;
  verified?: boolean | null;
  elapsedSec?: number;
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "--";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i === 0 ? Math.round(v) : v.toFixed(2)} ${units[i]}`;
}
