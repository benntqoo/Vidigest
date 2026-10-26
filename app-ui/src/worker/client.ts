// Rust 宿主的调用封装。前端的唯一出口，组件不直接 import `@tauri-apps/api`。
//
// 命名与 `docs/IPC协议规格.md` §3 的命令表一一对应，方便对照查找。

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import type { WorkerEvent, WorkerState } from "./types";

/** Rust 侧 emit 的事件通道（与 `worker/mod.rs` 的常量一致） */
export const EVENT_CHANNEL = "worker://event";
export const STATE_CHANNEL = "worker://state";
export const CLOSE_REQUEST_CHANNEL = "worker://close-requested";

// ---------------------------------------------------------------- 连接状态

/** 拉一次连接状态快照。前端首次挂载时必须调一次，补齐启动期错过的事件。 */
export function getWorkerState(): Promise<WorkerState> {
  return invoke<WorkerState>("worker_state");
}

export function onState(cb: (s: WorkerState) => void): Promise<UnlistenFn> {
  return listen<WorkerState>(STATE_CHANNEL, (e) => cb(e.payload));
}

export function onEvent(cb: (ev: WorkerEvent) => void): Promise<UnlistenFn> {
  return listen<WorkerEvent>(EVENT_CHANNEL, (e) => cb(e.payload));
}

// ------------------------------------------------------------------ 命令层

/** 透传一条命令。`name` 必须是规格 §3 里的命令 —— Rust 侧有白名单校验。 */
export function sendCommand(
  name: string,
  args: Record<string, unknown> = {},
): Promise<string> {
  return invoke<string>("worker_send", { name, args });
}

export function shutdownWorker(): Promise<void> {
  return invoke<void>("worker_shutdown");
}

/**
 * 手动重启 worker。
 *
 * 自动重启只有一次（规格 §7）；用尽之后界面会停在「worker 已退出、按钮全灰」。
 * 这个调用是那时**唯一的恢复入口**（故障态验证发现的第 3 个缺陷）。
 */
export function restartWorker(): Promise<void> {
  return invoke<void>("worker_restart");
}

/**
 * 用系统默认程序打开本地文件（「查看转写稿」）。
 *
 * 走宿主代开而不是插件命令：插件命令要过 ACL 路径 scope，而 `work_dir` 是用户配置的，
 * 没法在 capabilities 里静态声明。文件不存在会 reject（IO 错误）。
 */
export function openLocalPath(path: string): Promise<void> {
  return invoke<void>("open_local_path", { path });
}

// ------------------------------------------------------------------ 关闭确认

/** 用户点了窗口关闭，但还有任务在跑（`PLAN §7.2` 第 3 条）。 */
export interface CloseRequest {
  /** 触发时的流水线状态，用于文案 */
  pipeline_state: string | null;
  hint: string;
}

export function onCloseRequested(cb: (req: CloseRequest) => void): Promise<UnlistenFn> {
  return listen<CloseRequest>(CLOSE_REQUEST_CHANNEL, (e) => cb(e.payload));
}

/**
 * 确认退出。Rust 侧会先等 worker 优雅收尾（上限 30 s）再退出应用，
 * 所以调用后**界面会再停留一会儿**，这是正常的，不要催它。
 */
export function confirmClose(): Promise<void> {
  return invoke<void>("worker_confirm_close");
}

// ---------------------------------------------------------------- 业务语义

/** 批量入队。支持一次粘贴多条（含抖音分享文案，Python 侧负责抽 URL）。 */
export function addTasks(urls: string[]): Promise<string> {
  return sendCommand("add_tasks", { urls });
}

export function removeTask(taskId: number): Promise<string> {
  return sendCommand("remove_task", { task_id: taskId });
}

/** 启动流水线。幂等；只入队非终态且非 failed 的任务。 */
export function startPipeline(): Promise<string> {
  return sendCommand("start");
}

/**
 * 暂停取新任务。**已在跑的任务会跑完当前阶段为止**（规格 §3），
 * 所以界面应显示「正在暂停…」而不是瞬时完成。
 */
export function pausePipeline(): Promise<string> {
  return sendCommand("pause");
}

export function resumePipeline(): Promise<string> {
  return sendCommand("resume");
}

/**
 * 取消单条。**协作式**：转写只能在 segment 边界中断（规格 §3.1），
 * 界面要渲染成「正在取消…」。
 */
export function cancelTask(taskId: number): Promise<string> {
  return sendCommand("cancel_task", { task_id: taskId });
}

/** 重试失败项。`failed` 不是终态，必须显式重试才会重跑。 */
export function retryTask(taskId: number): Promise<string> {
  return sendCommand("retry_task", { task_id: taskId });
}

export function listTasks(): Promise<string> {
  return sendCommand("list_tasks");
}

export function getConcurrency(): Promise<string> {
  return sendCommand("get_concurrency");
}

// ------------------------------------------------------------ 设置与模型
//
// 五个命令都走 `worker_send` 透传（Rust 侧白名单已扩），**不新增 Tauri 命令** ——
// 新增一条 Tauri 命令就要同步改 capabilities 与 commands.rs 两处，
// 而这里没有任何需要宿主代劳的事（不像 `open_local_path` 要绕过 ACL scope）。

/** 拉全量配置 + 模型元信息（响应事件 `config`）。 */
export function getConfig(): Promise<string> {
  return sendCommand("get_config");
}

/**
 * 部分更新配置（响应事件 `config_saved`）。
 *
 * ⚠️ 保存后 **worker 会重启**（Rust 侧在收到 `config_saved` 时触发）——
 * 因为 `Config` 只在进程启动时读一次，热应用要重建调度器与模型缓存。
 * 所以界面上「保存成功」与「已生效」是两个时刻，中间有 2-3 秒。
 */
export function setConfig(patch: Record<string, unknown>): Promise<string> {
  return sendCommand("set_config", { patch });
}

/** 下载模型（约 2.9 GiB）。进度以 `model_download_*` 事件推送。 */
export function downloadModel(): Promise<string> {
  return sendCommand("download_model", {});
}

export function cancelModelDownload(): Promise<string> {
  return sendCommand("cancel_model_download", {});
}

/** 手动重跑一次 GPU 真实验证（响应 `env_verify_started` → `env_verified`）。 */
/** 弹一个有头 Edge 让用户扫码登录抖音（响应事件 `douyin_login_state`）。 */
export function douyinLogin(timeout = 180): Promise<string> {
  return sendCommand("douyin_login", { timeout });
}

/** 列一个作者/频道/合集的作品（批量下载入口）。响应事件 `author_page`。 */
export function listAuthor(url: string, limit = 0): Promise<string> {
  return sendCommand("list_author", { url, limit });
}

/** 清空任务历史 / 工作目录内容（**不碰模型**）。 */
export function clearData(scope: "history" | "workspace" | "all"): Promise<string> {
  return sendCommand("clear_data", { scope });
}

export function verifyEnv(): Promise<string> {
  return sendCommand("verify_env");
}
