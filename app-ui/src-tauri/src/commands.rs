//! Tauri 命令 —— 前端 `invoke` 的入口。
//!
//! 这一层刻意做薄：只做参数校验与转发，业务逻辑在 `worker` 模块。
//! 命令白名单与 `docs/IPC协议规格.md` §3 的命令清单一字对应 —— 前端的拼写错误
//! 会在这里被挡住，而不是变成一条 Python 侧的 `E_UNKNOWN_CMD`。

use serde_json::{json, Value};
use tauri::State;

use crate::worker::{StateSnapshot, Worker};

/// 规格 §3 定义的**全部**命令。改动此表必须先改规格文档。
///
/// 2026-10-24 新增后 5 条（设置界面 + 模型下载）：`get_config` / `set_config` /
/// `download_model` / `cancel_model_download` / `verify_env`。
/// 前四条在执行单里写作「4 条命令」，`verify_env` 是实施时补的第 5 条 ——
/// 设置页的「重新校验」与「改完 model_dir 立刻看结论」都需要它，
/// 否则用户只能靠重启应用来触发一次验证。
const ALLOWED_COMMANDS: &[&str] = &[
    "hello",
    "ping",
    "add_tasks",
    "remove_task",
    "start",
    "pause",
    "resume",
    "cancel_task",
    "retry_task",
    "list_tasks",
    "get_concurrency",
    "shutdown",
    "get_config",
    "set_config",
    "download_model",
    "cancel_model_download",
    "verify_env",
];

/// 查当前连接状态。前端首次挂载时拉一次，避免错过启动期事件（规格 §4.2 的同一思路）。
#[tauri::command]
pub fn worker_state(worker: State<'_, Worker>) -> StateSnapshot {
    worker.snapshot()
}

/// 发送一条命令给 Python worker。
///
/// 返回命令 id（形如 `c-0001`），与随后事件信封里的 `id` 字段对应。
#[tauri::command]
pub fn worker_send(
    worker: State<'_, Worker>,
    name: String,
    args: Option<Value>,
) -> Result<String, String> {
    if !ALLOWED_COMMANDS.contains(&name.as_str()) {
        return Err(format!(
            "未知命令 {name:?}。合法命令见 docs/IPC协议规格.md §3：{}",
            ALLOWED_COMMANDS.join(", ")
        ));
    }
    worker.send(&name, args.unwrap_or_else(|| json!({})))
}

/// 请求 worker 优雅退出（发 `shutdown`，等它回 `bye` 后自行 exit）。
#[tauri::command]
pub fn worker_shutdown(worker: State<'_, Worker>) -> Result<(), String> {
    worker.request_shutdown();
    Ok(())
}

/// **手动重启 worker**。
///
/// 为什么需要它：规格 §7 的自动重启只有**一次**。一次之后若还失败，界面会停在
/// 「worker 已退出、四个按钮全灰」——这是故障态验证里发现但尚未修的第 3 个缺陷：
/// **没有任何恢复入口**。用户把问题修好（补依赖、改 `VIDIGEST_PYTHON`）之后，
/// 必须有一条路能重来，而不是被迫关掉整个应用。
///
/// 与 `worker_shutdown` 的区别：那个是「体面地结束」，这个是「不管怎样重来一遍」。
/// 它会把自动重启的计数与 fatal 标记清零（见 `Worker::restart`）。
#[tauri::command]
pub fn worker_restart(worker: State<'_, Worker>) -> Result<(), String> {
    worker.restart();
    Ok(())
}

/// 关闭确认框里选「退出并停止任务」后调用（`PLAN §7.2` 第 3 条）。
///
/// 它**不会立刻退出应用**：先请 worker 优雅收尾，等它真的退出（上限 30 s）再退 ——
/// 那段等待就是 `PLAN §7` 说的「优雅关闭 30 s 窗口」，以前窗口一关应用即退时根本拿不到。
/// 详见 `Worker::confirm_close`。
#[tauri::command]
pub fn worker_confirm_close(worker: State<'_, Worker>) -> Result<(), String> {
    worker.confirm_close();
    Ok(())
}

/// 用系统默认程序打开一个本地文件（「查看转写稿」用）。
///
/// **为什么不从前端直接调 opener 插件的 `openPath`**（这不是猜的，看过源码）：
/// `opener:default` 权限集只含 `allow-open-url` / `allow-reveal-item-in-dir` /
/// `allow-default-urls`，**不含 `allow-open-path`**；更麻烦的是那个命令走 ACL 的
/// *路径 scope* 校验，而本项目的 `work_dir` 是用户写在 `config.yaml` 里的，
/// 没法在 `capabilities/*.json` 里静态声明（写死就等于把用户配置钉死）。
///
/// 而 `tauri-plugin-opener` 的 crate 文档第 8–10 行明确写着：插件的
/// **自由函数**（即这里调的这个）**不做任何 scope 校验**。
/// 所以由宿主代开 —— 既不新增 ACL 配置，也不把用户路径写死在能力文件里。
///
/// 文件不存在会返回 IO 错误：正好把「转写还没生成」如实告诉用户，
/// 而不是静默地什么都不发生。
#[tauri::command]
pub fn open_local_path(path: String) -> Result<(), String> {
    tauri_plugin_opener::open_path(&path, None::<&str>).map_err(|e| e.to_string())
}
