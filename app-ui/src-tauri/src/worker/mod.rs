//! Python worker 的监督器。
//!
//! 职责：spawn 常驻 Python 进程 → 握手 → 双向转发 JSON Lines → 心跳 → 崩溃上报。
//! 协议细节见 `docs/IPC协议规格.md`，本模块不自行发明约定。
//!
//! 线程模型（与 Python 侧对称）：
//! ```text
//!   stdout 读线程  → 逐行解析 Event → 更新状态 + emit 到前端
//!   stderr 读线程  → 逐行转发（Python 日志全在这里，规格 §1.1）
//!   心跳线程       → 每 5s 发 ping，连续 2 次无 pong 判定死亡
//!   命令发送       → 加锁写 stdin（来自 Tauri 命令，任意线程）
//! ```

pub mod jobobject;
pub mod locator;
pub mod protocol;

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter};

// 注意：`protocol::Command` 是**协议信封**（会被 serde 序列化后写进 stdin），
// 与上面 `std::process::Command`（用来 spawn 进程）同名但完全不同。
// 这里必须起别名，否则本模块里 `Command::new` 会解析到 std 的那个，
// 报出「参数个数不符」+「Command 未实现 Serialize」一串连锁错误。
// 用 `WireCommand` 这个显式名字，让「这是要写进管道的东西」在读代码时一眼可见。
use protocol::{make_command_id, Command as WireCommand, Event, CLIENT_VERSION};

/// 心跳间隔（规格 §6）
const PING_INTERVAL: Duration = Duration::from_secs(5);
/// 连续多少次无 pong 判定 worker 死亡（规格 §6）
const MISSED_PONG_LIMIT: u32 = 2;

/// 崩溃后自动重启的最大次数（规格 §7：「自动重启**一次**」）。
///
/// 上限存在的意义不是省资源，而是**防止崩溃循环**：若 worker 每次 spawn 后立刻
/// 因同一个原因退出（例如解释器里少了依赖），无限重启只会把日志刷满，
/// 用户反而看不到真正的错误。超过上限就停下来、把原因摊开给用户。
const MAX_RESTARTS: u32 = 1;

/// 重启前的退避。1 秒足够让「上一次的 stderr 尾部」写完并被读到。
const RESTART_BACKOFF: Duration = Duration::from_millis(1000);

/// stderr 尾部保留的行数（规格 §7：「再失败则提示用户并展示 stderr 尾部」）。
const STDERR_TAIL_LINES: usize = 20;

/// 退出时留给 worker 收尾的时间上限（规格 §7 的「优雅 shutdown，Python 侧 30s 上限」）。
///
/// ⚠️ 这个数字**必须**与 `app/rpc.py::_cmd_shutdown` 里的 `dispatcher.stop(timeout=30.0)`
/// 对齐。比它小就会在 Python 还在写 `state.db` 时把应用退掉，断点续跑的正确性
/// 依赖那次写入。
const SHUTDOWN_GRACE: Duration = Duration::from_secs(30);

/// 定长环形缓冲：满了先丢最旧的一条。
///
/// 抽成自由函数是为了能单测 —— 它是纯数据操作，不该因为住在 `Inner` 里就测不到。
fn push_bounded(buf: &mut VecDeque<String>, line: &str, cap: usize) {
    if cap == 0 {
        return;
    }
    if buf.len() >= cap {
        buf.pop_front();
    }
    buf.push_back(line.to_string());
}

/// 关闭窗口前是否要先问用户（`PLAN §7.2` 第 3 条）。
///
/// 同样是抽出来的纯函数：`should_confirm_close` 要 `AppHandle`，测不到。
/// 而「什么情况下该拦下关闭」正是这里唯一会错的地方 —— 拦多了用户关不掉窗口，
/// 拦少了正在跑的任务被静默打断。
fn needs_close_confirmation(confirmed: bool, alive: bool, pipeline_state: Option<&str>) -> bool {
    if confirmed || !alive {
        return false;
    }
    // `running` 是在推进；`paused` 是已取的任务还在跑完（`PLAN` 的暂停语义）。
    // `idle` / `aborted` / `stopped` / 未知 都不拦 —— 没有正在进行的阶段可丢。
    matches!(pipeline_state, Some("running") | Some("paused"))
}

/// 崩溃后的处置决策。
///
/// 抽成纯函数是为了**能被测到**：`try_recover_after_crash` 需要 `AppHandle`
/// 才能跑（它要 emit 事件），单测里造不出来；而「什么时候该重启、什么时候该放弃」
/// 恰恰是这段逻辑里唯一会出错的地方，不能因为它住在难测的函数里就不测。
#[derive(Debug, PartialEq, Eq)]
enum Recovery {
    /// 环境级故障，不重启（规格 §8）
    NoRestartFatal,
    /// 自动重启第 `attempt` 次
    Restart { attempt: u32 },
    /// 额度用尽，交给用户手动重启
    GiveUp { attempt: u32 },
}

fn decide_recovery(fatal: bool, used: u32) -> Recovery {
    if fatal {
        return Recovery::NoRestartFatal;
    }
    let attempt = used + 1;
    if attempt <= MAX_RESTARTS {
        Recovery::Restart { attempt }
    } else {
        Recovery::GiveUp { attempt }
    }
}

/// 前端监听的事件通道
pub const EVENT_CHANNEL: &str = "worker://event";
/// 前端监听的连接状态通道
pub const STATE_CHANNEL: &str = "worker://state";
/// 「用户点了关闭、但还有任务在跑」——前端据此弹确认框。
pub const CLOSE_REQUEST_CHANNEL: &str = "worker://close-requested";

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 给前端的连接状态快照。字段扁平，前端不必再解嵌套。
#[derive(Debug, Clone, Serialize)]
pub struct StateSnapshot {
    /// `starting` | `restarting` | `ready` | `failed` | `exited`
    ///
    /// `restarting` 是自动重启的中间态：进程已不在、但已经在重启的路上。
    /// **不要合并进 `starting`** —— 两者对用户的含义不同（一个是从没起来过，
    /// 一个是起来过又死了正在救），合并会让「到底出过事没有」这个问题失去答案。
    pub state: String,
    pub detail: String,
    pub server_version: Option<String>,
    pub gpu_usable: Option<bool>,
    pub gpu_verified: Option<bool>,
    pub concurrency: Option<Value>,
    /// worker 进程是否仍在运行
    pub alive: bool,
    /// 已重启次数（规格 §7：自动重启一次）
    pub restart_count: u32,
    /// 调度器状态。**由 `ready` / `pipeline_state` 事件累积**，不额外发命令去问。
    ///
    /// 前端 `App.tsx` 早就在读 `snapshot.pipeline_state`，而 Rust 侧一直没这个字段
    /// （前端 `types.ts` 也声明了它）—— 属于三端漂移，冷启动时前端只能显示默认值
    /// 直到下一个 `pipeline_state` 事件到达。规格 §4 的 `ready` 事件本来就带
    /// `pipeline_state`，所以这里补上是「按已有契约实现」，不是新增契约。
    pub pipeline_state: Option<String>,
    /// **为什么没能验证 GPU** —— `model_missing` / `model_incomplete` /
    /// `import_failed` / `cuda_load_failed` / `cuda_inference_failed`。
    ///
    /// ⚠️ 只有 `gpu_usable == None` 时它才有意义。它是「没验成」的具体原因，
    /// 与「验过，确实不能用」（`gpu_usable == Some(false)`）是两件事 ——
    /// 2026-10-24 的误报就是这两者被混成了同一个 `false`，界面据此
    /// 显示「吞吐下降约 6 倍」，把用户引向驱动排查，方向完全错。
    pub blocked_reason: Option<String>,
    /// 验证结论的原始说明（Python 侧 `env_verified.detail`）。状态栏 tooltip 用。
    pub env_detail: Option<String>,
}

impl StateSnapshot {
    fn starting() -> Self {
        Self {
            state: "starting".into(),
            detail: "正在启动 Python worker…".into(),
            server_version: None,
            gpu_usable: None,
            gpu_verified: None,
            concurrency: None,
            alive: false,
            restart_count: 0,
            pipeline_state: None,
            blocked_reason: None,
            env_detail: None,
        }
    }

    fn failed(detail: impl Into<String>) -> Self {
        Self {
            state: "failed".into(),
            detail: detail.into(),
            ..Self::starting()
        }
    }
}

struct Inner {
    /// 指向自己的弱引用。
    ///
    /// **为什么要这个**：崩溃自动重启发生在 `on_worker_exit`（`&self`）里，而
    /// `spawn_worker` 需要 `Arc<Inner>` 才能把 `Arc` 移进两条读线程。写成
    /// `let _ = self.inner.clone()` 需要先有 `Worker`，而那里只有 `&Inner`。
    /// 存一个 `Weak`（`Arc::new_cyclic` 建的）就解开了这个鸡生蛋问题。
    ///
    /// 用 `Weak` 而不是 `Arc`：self-reference 若持强引用，`Arc` 的引用计数
    /// 永远不为 0，`Inner::drop`（里面负责 kill 子进程）就永远不会跑。
    me: Weak<Inner>,
    app: AppHandle,
    /// 写端。None 表示 worker 未运行。
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    /// worker 是否存活（stdout 线程结束即置 false）
    alive: AtomicBool,
    /// 命令 id 自增序号
    serial: AtomicU64,
    /// 期望收到的下一个 seq（丢帧检测，规格 §2.2）
    expected_seq: AtomicU64,
    /// 最近一次收到 pong 的时刻
    last_pong: Mutex<Instant>,
    /// 连续未收到 pong 的次数
    missed_pongs: AtomicU64,
    /// 当前状态快照
    snapshot: Mutex<StateSnapshot>,
    /// 是否已主动关闭（区分「正常退出」与「崩溃」）
    shutting_down: AtomicBool,
    /// 心跳线程退出信号
    stop_heartbeat: AtomicBool,
    /// 心跳线程的代号。
    ///
    /// 每次 `start_heartbeat` 自增，线程自己记住出生时的代号，代号一变就退出。
    /// **不能用「重启时把 `stop_heartbeat` 置回 false」来复用线程**：旧线程可能正
    /// 睡在 5 秒的 `sleep` 里，醒来时看到 `stop_heartbeat=false` + `alive=true`，
    /// 就会以为自己是当前那一代，于是**两个心跳线程同时 ping 同一个 worker**
    /// ——`missed_pongs` 会被数两遍，worker 提前被判死。
    heartbeat_gen: AtomicU64,
    /// 子进程的代号。每次 `spawn_worker` 自增，**读线程带着自己出生时的代号**。
    ///
    /// **为什么必须有**：`on_worker_exit` 里会 `child.lock().wait()` —— 如果旧一代的
    /// 读线程在「用户重启 / 配置保存后重启」之后才 EOF，它拿到的是**新一代的**
    /// `child`，于是 `wait()` 会一直阻塞到新 worker 退出，同时把 `alive` 置 false、
    /// 状态改成「意外退出」并触发一次多余的自动重启。
    /// 表现就是「重启完界面显示 worker 已退出，但其实进程好好活着」。
    /// （原代码只在「worker 已死」时才允许手动重启，所以这个坑一直没被踩到；
    /// 2026-10-24「保存配置即重启」让它成了一条正常路径，必须先堵上。）
    child_gen: AtomicU64,
    /// 已自动重启次数。**这是 `restart_count` 的唯一真源**；
    /// 快照里的那个字段在 `emit_state` 时从这里同步过去，避免两处各记一份。
    restart_count: AtomicU32,
    /// 用户是否已经就「有任务在跑时关闭窗口」确认过一次。
    ///
    /// 存在的理由：确认之后前端会再走一次关闭，那时 `CloseRequested` **必须放行**，
    /// 否则弹窗会无限重开（用户点「退出」→ 窗口又拦下 → 又弹一次）。
    /// 用「一次性放行」而不是「前端自己绕过关闭流程」，是为了让窗口关闭始终
    /// 只有 `CloseRequested` 一条路径 —— 多一条路径就多一处会忘记回收 worker 的地方。
    close_confirmed: AtomicBool,
    /// 是否收到过 `fatal: true` 的错误事件。
    ///
    /// 规格 §8 规定 `E_FFMPEG_MISSING` / `E_ENV_FATAL` 属**环境级**故障，
    /// 「重启也没用」，故不重启。⚠️ 现状：Python 侧目前**没有任何路径会发
    /// `fatal: true`**（`rpc.py::_on_dispatcher_event` 刻意用 `fatal=False` 发
    /// `E_ENV_FATAL`，因为那时 worker 还活着、能继续响应命令）。
    /// 这个分支是照规格实现的，等 Python 侧真的开始发致命错误时才有实际作用。
    ///
    /// 覆盖情况：**决策侧**有单测（`tests::fatal_error_suppresses_restart`）；
    /// **置位侧**（`handle_frame` 收到 `fatal:true` 才写这个标志）没有单测——
    /// 它需要真实 worker 发一条 fatal 事件，而现状无人发。
    fatal: AtomicBool,
    /// stderr 尾部（定长环形缓冲），崩溃后随状态一起摊给用户 ——
    /// 规格 §7：「再失败则提示用户并展示 stderr 尾部」。
    stderr_tail: Mutex<VecDeque<String>>,
    /// 绑定 worker 的 Job Object 句柄。
    ///
    /// **必须存在这里，不能是 `spawn_worker` 的局部变量。** 本作业设了
    /// `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，语义是「关闭本作业的**最后一个句柄**
    /// 时终止作业内所有进程」。所以句柄一旦被 drop，作业里的 Python 立刻被杀。
    /// 之前这里写的是 `let _job = jobobject::bind_child(&child);` —— 局部绑定在
    /// `spawn_worker` 返回时就 drop 了，于是**防孤儿的机制反过来把刚 spawn 的
    /// worker 当场杀死**：终端里能看到「已 spawn pid=NNNN」，紧接着进程就没了，
    /// 而且一句 stderr 都没有（进程被 TerminateProcess，来不及写）。
    ///
    /// 放进 `Inner` 后，生命周期就与监督器一致：句柄只在 `Inner` 被 drop
    /// （进程退出路径）时关闭，那时连带终止 worker 正是想要的语义。
    ///
    /// 完整排查过程见 `docs/阶段3_打通记录.md` §3。
    job: Mutex<Option<jobobject::JobObject>>,
}

/// 监督器句柄。内部是 `Arc<Inner>`，可作为 Tauri State 跨命令/跨线程共享。
#[derive(Clone)]
pub struct Worker {
    inner: Arc<Inner>,
}

impl Worker {
    /// 创建并启动。**不阻塞**——握手是异步的，前端先渲染「启动中」，
    /// 收到 `ready` 后再覆写（规格 §4.2 的同一思路）。
    pub fn start(app: AppHandle) -> Self {
        // `new_cyclic` 让 `Inner` 在构造期就拿到自身的 `Weak`（见 `Inner::me`）。
        // ⚠️ 闭包里的 `me` 是 `&Weak<Inner>`，**不要 `upgrade()`** —— 此时 `Arc`
        // 还没构造完，`upgrade()` 必然返回 `None`。
        let inner = Arc::new_cyclic(|me| Inner {
            me: me.clone(),
            app,
            stdin: Mutex::new(None),
            child: Mutex::new(None),
            alive: AtomicBool::new(false),
            serial: AtomicU64::new(0),
            expected_seq: AtomicU64::new(1),
            last_pong: Mutex::new(Instant::now()),
            missed_pongs: AtomicU64::new(0),
            snapshot: Mutex::new(StateSnapshot::starting()),
            shutting_down: AtomicBool::new(false),
            stop_heartbeat: AtomicBool::new(false),
            heartbeat_gen: AtomicU64::new(0),
            child_gen: AtomicU64::new(0),
            restart_count: AtomicU32::new(0),
            fatal: AtomicBool::new(false),
            close_confirmed: AtomicBool::new(false),
            stderr_tail: Mutex::new(VecDeque::with_capacity(STDERR_TAIL_LINES)),
            job: Mutex::new(None),
        });

        let worker = Self { inner };
        worker.spawn_worker();
        worker.start_heartbeat();
        worker
    }

    /// 用户手动重启 worker（`worker_restart` 命令）。
    ///
    /// 存在的理由：自动重启只有**一次**（规格 §7）。一次之后若还失败，界面会停在
    /// 「worker 已退出、所有按钮全灰」——**那正是故障态验证里发现的第 3 个缺陷：
    /// 没有任何恢复入口**。用户修好问题（例如此前缺的依赖补上了）之后，
    /// 必须有一条路能重来，而不是被迫关掉整个应用。
    ///
    /// 手动重启会把自动重启的计数与 fatal 标记**清零**——那是一次全新的尝试，
    /// 沿用上一轮的「已用掉一次」只会让用户点了没反应。
    pub fn restart(&self) {
        // **无条件收掉上一代**：崩溃路径上 `child` 里还留着那个已退出的 `Child`，
        // 「心跳判死但进程还活着」那条路径上它更是真的还在跑（且占着 GPU）。
        // 漏了这一步就会出现两个 worker 同时读写同一个 `state.db`。
        self.inner.reap_stale_child();
        self.inner.shutting_down.store(false, Ordering::SeqCst);
        self.inner.fatal.store(false, Ordering::SeqCst);
        self.inner.restart_count.store(0, Ordering::SeqCst);
        self.inner.emit_log("info", "supervisor", "用户请求重启 worker");
        self.spawn_worker();
        self.start_heartbeat();
    }

    /// 当前状态快照。前端首次挂载时主动拉一次，避免错过启动期的事件。
    pub fn snapshot(&self) -> StateSnapshot {
        self.inner.snapshot.lock().unwrap().clone()
    }

    /// 向 worker 发送命令。返回命令 id，便于与响应对应。
    pub fn send(&self, name: &str, args: Value) -> Result<String, String> {
        let serial = self.inner.serial.fetch_add(1, Ordering::SeqCst) + 1;
        let id = make_command_id(serial);
        let cmd = WireCommand::new(&id, name, args);
        let mut line = serde_json::to_string(&cmd)
            .map_err(|e| format!("命令序列化失败：{e}"))?;
        debug_assert!(!line.contains('\n'), "JSON Lines 不允许裸换行");

        let mut guard = self.inner.stdin.lock().unwrap();
        let writer = guard
            .as_mut()
            .ok_or_else(|| "worker 未运行，命令被拒绝".to_string())?;

        line.push('\n');
        writer
            .write_all(line.as_bytes())
            .and_then(|_| writer.flush())
            .map_err(|e| {
                // 写失败通常意味着 worker 已死。置 alive=false 让 UI 立即反映。
                self.inner.alive.store(false, Ordering::SeqCst);
                format!("写入 stdin 失败（worker 可能已退出）：{e}")
            })?;

        Ok(id)
    }

    /// 优雅关闭：发 `shutdown`。worker 会回 `bye` 后自行退出（规格 §7）。
    pub fn request_shutdown(&self) {
        self.inner.shutting_down.store(true, Ordering::SeqCst);
        let _ = self.send("shutdown", json!({}));
    }

    // ------------------------------------------------------------ 关闭确认

    /// 点关闭时要不要先问用户一句（`PLAN §7.2` 第 3 条）。
    pub fn should_confirm_close(&self) -> bool {
        let snap = self.inner.snapshot.lock().unwrap();
        needs_close_confirmation(
            self.inner.close_confirmed.load(Ordering::SeqCst),
            snap.alive,
            snap.pipeline_state.as_deref(),
        )
    }

    /// 通知前端弹确认框。同时落一条 supervisor 日志：
    /// 否则「点了 X 但窗口没关」在终端里是零线索的（与 `emit_state` 同一个理由）。
    pub fn emit_close_requested(&self) {
        let pipeline_state = self.inner.snapshot.lock().unwrap().pipeline_state.clone();
        self.inner.emit_log(
            "info",
            "supervisor",
            format!("关闭被拦下：流水线 {pipeline_state:?}，等待用户确认"),
        );
        if let Err(e) = self.inner.app.emit(
            CLOSE_REQUEST_CHANNEL,
            json!({
                "pipeline_state": pipeline_state,
                "hint": "还有任务在跑。退出会中断它们——下次启动会从断点续跑，\
                         但当前阶段已做的工作会丢弃。",
            }),
        ) {
            eprintln!("[worker] 关闭确认事件发送失败：{e}");
        }
    }

    /// 用户在确认框里选了「退出」。
    ///
    /// **为什么要等**：直接退出应用会立刻触发 `RunEvent::Exit` → `force_kill()`，
    /// 而 Python 那一刻可能正在 `dispatcher.stop()` 里把任务状态写回 `state.db`。
    /// 断点续跑的正确性依赖那次写入 —— 这正是 `PLAN §8` 里记着的
    /// 「优雅关闭的 30 s 窗口当前实际拿不到」的成因。
    /// 现在改成：先请 worker 收尾，**等它真的退出（上限 `SHUTDOWN_GRACE`）**，再退应用。
    pub fn confirm_close(&self) {
        self.inner.close_confirmed.store(true, Ordering::SeqCst);
        self.inner.emit_log(
            "info",
            "supervisor",
            format!(
                "用户确认退出：先请 worker 优雅收尾（上限 {} s），再退出应用",
                SHUTDOWN_GRACE.as_secs()
            ),
        );
        self.request_shutdown();

        let inner = self.inner.clone();
        std::thread::spawn(move || {
            let deadline = Instant::now() + SHUTDOWN_GRACE;
            while inner.alive.load(Ordering::SeqCst) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(100));
            }
            if inner.alive.load(Ordering::SeqCst) {
                // 超时也要退，别把用户锁在窗口里；Job Object + `force_kill` 会兜底。
                inner.emit_log(
                    "warning",
                    "supervisor",
                    format!("worker 超过 {} s 未收尾，强制退出", SHUTDOWN_GRACE.as_secs()),
                );
            } else {
                inner.emit_log("info", "supervisor", "worker 已收尾，退出应用");
            }
            inner.app.exit(0);
        });
    }

    /// 强制关闭。挂在应用退出路径上，保证进程内**一定**有人动手杀子进程。
    ///
    /// 回收顺序是三道，这里是最后一道：
    ///   1. 优雅 `shutdown`（Python 侧 30s 上限）
    ///   2. Job Object（父进程被强杀时连带终止）
    ///   3. 本函数
    ///
    /// **为什么第 2 道不足以替代第 3 道**：`jobobject::bind_child` 在绑定失败时只
    /// 打印一句警告就返回 `None`（例如 worker 已被别的 Job 收编时
    /// `AssignProcessToJobObject` 会失败）。那种情况下第 2 道防线是**空的**，
    /// 只剩这里能保证不留孤儿 Python 进程。
    ///
    /// 对已退出的子进程幂等：先 `try_wait` 拿到结果，已退出就不再 kill。
    pub fn force_kill(&self) {
        self.inner.shutting_down.store(true, Ordering::SeqCst);
        self.inner.stop_heartbeat.store(true, Ordering::SeqCst);

        // 先丢 stdin：这一下就让 Python 侧的 EOF 自杀防线生效，
        // 于是「kill 失败」也还有退路，同时避免有心跳线程还在往里写。
        *self.inner.stdin.lock().unwrap() = None;

        if let Some(mut child) = self.inner.child.lock().unwrap().take() {
            match child.try_wait() {
                Ok(Some(_)) => {} // 已自行退出，不必杀
                _ => {
                    let _ = child.kill();
                }
            }
            // 必须 wait 回收句柄；否则子进程成僵尸，且本函数返回时它还没真正结束。
            let _ = child.wait();
        }

        self.inner.alive.store(false, Ordering::SeqCst);
    }

    // ---------------------------------------------------------------- spawn

    fn spawn_worker(&self) {
        // 本代代号，读线程要带着它去收尾（见 `Inner::child_gen`）。
        let gen = self.inner.child_gen.fetch_add(1, Ordering::SeqCst) + 1;
        let target = match locator::locate() {
            Ok(t) => t,
            Err(msg) => {
                self.set_state(StateSnapshot::failed(msg));
                return;
            }
        };

        let mut child = match spawn_child(&target) {
            Ok(c) => c,
            Err(msg) => {
                self.set_state(StateSnapshot::failed(msg));
                return;
            }
        };

        // Job Object 必须在 spawn 后尽快绑定，否则期间派生的后代不在作业内。
        //
        // **存进 `inner.job`，不要用局部变量接。** 作业设了
        // `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，句柄被 drop = 作业内进程全被杀。
        // 写成 `let _job = ...` 的话，本函数一返回就 drop，等于刚 spawn 就杀掉
        // worker（详见 `Inner::job` 字段的注释）。重启时这里会替换掉旧句柄，
        // 顺带把上一代作业里的残留进程清掉。
        let job = jobobject::bind_child(&child);
        *self.inner.job.lock().unwrap() = job;

        // 成功路径同样要留痕：否则「窗口开了但 Python 进程不在」时，
        // 终端里既没有失败原因、也没有成功证据，只能靠猜。
        eprintln!(
            "[worker] 已 spawn pid={} program={} cwd={}（来源于 {}）",
            child.id(),
            target.program.display(),
            target.cwd.display(),
            target.source
        );

        let stdout = child.stdout.take().expect("stdout 已设为 piped");
        let stderr = child.stderr.take().expect("stderr 已设为 piped");
        let stdin = child.stdin.take().expect("stdin 已设为 piped");

        *self.inner.stdin.lock().unwrap() = Some(stdin);
        *self.inner.child.lock().unwrap() = Some(child);
        self.inner.alive.store(true, Ordering::SeqCst);
        self.inner.expected_seq.store(1, Ordering::SeqCst);
        *self.inner.last_pong.lock().unwrap() = Instant::now();
        self.inner.missed_pongs.store(0, Ordering::SeqCst);
        // 新一代进程，stderr 尾部重新累积 —— 上一代的尾部已经在退出时读走并进日志了。
        self.inner.stderr_tail.lock().unwrap().clear();

        {
            let restarts = self.inner.restart_count.load(Ordering::SeqCst);
            let mut snap = self.inner.snapshot.lock().unwrap();
            snap.state = "starting".into();
            snap.detail = if restarts > 0 {
                format!("第 {restarts} 次自动重启中：{}", target.source)
            } else {
                format!("已启动：{}", target.source)
            };
            snap.alive = true;
            // 新进程还没握手，上一代的 GPU/并发结论一律作废 —— 留着会让用户以为
            // 「重启后还是 GPU 可用」，而新进程可能根本起不来。
            snap.server_version = None;
            snap.gpu_usable = None;
            snap.gpu_verified = None;
            snap.blocked_reason = None;
            snap.env_detail = None;
            snap.concurrency = None;
        }
        self.inner.emit_state();

        // stdout 读线程：协议帧唯一来源
        {
            let inner = self.inner.clone();
            std::thread::spawn(move || {
                let reader = BufReader::new(stdout);
                for line in reader.lines() {
                    match line {
                        Ok(l) => {
                            if l.trim().is_empty() {
                                continue;
                            }
                            inner.handle_frame(&l);
                        }
                        Err(e) => {
                            eprintln!("[worker] stdout 读取失败：{e}");
                            break;
                        }
                    }
                }
                inner.on_worker_exit(gen);
            });
        }

        // stderr 读线程：**必须消费**，否则缓冲区满会让 Python 阻塞（规格 §7）
        {
            let inner = self.inner.clone();
            std::thread::spawn(move || {
                let reader = BufReader::new(stderr);
                for line in reader.lines() {
                    match line {
                        Ok(l) => {
                            if l.trim().is_empty() {
                                continue;
                            }
                            eprintln!("[py] {l}");
                            // 留一份尾部：崩溃时用户第一眼要看的就是 Python 最后的遗言。
                            inner.push_stderr_tail(&l);
                            // 同时转给前端日志区，`source` 让前端区分来源
                            inner.emit_log("debug", "stderr", l);
                        }
                        Err(_) => break,
                    }
                }
            });
        }

        // 握手：必须是第一条命令（规格 §3）
        if let Err(e) = self.send("hello", json!({"client_version": CLIENT_VERSION})) {
            self.set_state(StateSnapshot::failed(format!("握手发送失败：{e}")));
        }
    }

    // ----------------------------------------------------------- 事件分发

    fn start_heartbeat(&self) {
        // 每一代心跳线程领一个代号；代号一变就说明自己被取代了（见 `Inner::heartbeat_gen`）。
        let gen = self.inner.heartbeat_gen.fetch_add(1, Ordering::SeqCst) + 1;
        let inner = self.inner.clone();
        std::thread::spawn(move || loop {
            std::thread::sleep(PING_INTERVAL);
            if inner.heartbeat_gen.load(Ordering::SeqCst) != gen {
                break; // 已被新一代心跳取代（例如刚发生过自动重启）
            }
            if inner.stop_heartbeat.load(Ordering::SeqCst) || !inner.alive.load(Ordering::SeqCst) {
                break;
            }
            // 先判断上一轮是否超时，再发新的 ping
            let last = *inner.last_pong.lock().unwrap();
            if last.elapsed() > PING_INTERVAL + Duration::from_secs(2) {
                let missed = inner.missed_pongs.fetch_add(1, Ordering::SeqCst) + 1;
                inner.emit_log("warning", "supervisor", format!("心跳超时（第 {missed} 次未收到 pong）"));
                if missed >= MISSED_PONG_LIMIT as u64 {
                    inner.alive.store(false, Ordering::SeqCst);
                    // 必须经由统一发射口。这里原来直接 `app.emit(STATE_CHANNEL, ..)`，
                    // 绕开了日志，于是「心跳判死」这条最重要的异常路径在终端里查不到。
                    //
                    // ⚠️ **本分支刻意不杀进程、不触发自动重启**，只如实上报。
                    // 理由：`docs/IPC协议规格.md` §9 末尾与 `PLAN §11.7` 都记着一条
                    // 未验证项 —— **转写进行中主循环是否还能回 pong 没人测过**。
                    // 若长转写阻塞了主循环，心跳会「误判死亡」；此时若自动重启，
                    // 就会把一条正在正常转写的任务连同 GPU 上下文一起杀掉，
                    // 那是比「多显示一条错误」严重得多的回归。
                    // 等 §11.7 那条测清楚了（转写中 pong 往返实测），再决定要不要在这里动手。
                    *inner.snapshot.lock().unwrap() = StateSnapshot {
                        state: "exited".into(),
                        detail: format!("心跳中断：连续 {missed} 次未收到 pong（见日志区）"),
                        alive: false,
                        ..StateSnapshot::starting()
                    };
                    inner.emit_state();
                    break;
                }
            }
            if inner.send_raw("ping", json!({})).is_err() {
                break;
            }
        });
    }

    fn set_state(&self, snap: StateSnapshot) {
        *self.inner.snapshot.lock().unwrap() = snap;
        self.inner.emit_state();
    }
}

impl Inner {
    /// 处理一行协议帧。任何解析失败都**不中断读循环**——丢一帧好过整个 worker 失联。
    fn handle_frame(&self, line: &str) {
        let ev: Event = match serde_json::from_str(line) {
            Ok(ev) => ev,
            Err(e) => {
                eprintln!("[worker] 协议帧解析失败，已跳过：{e}\n  原始行：{line}");
                return;
            }
        };

        // 丢帧检测（规格 §2.2：seq 从 1 开始自增）
        if ev.seq > 0 {
            let expected = self.expected_seq.load(Ordering::SeqCst);
            if ev.seq != expected {
                let missed = ev.seq.saturating_sub(expected);
                if missed > 0 {
                    eprintln!("[worker] 检测到丢帧：期望 seq={expected}，收到 {}（丢 {missed} 帧）", ev.seq);
                }
            }
            self.expected_seq.store(ev.seq + 1, Ordering::SeqCst);
        }

        if ev.name == "pong" {
            *self.last_pong.lock().unwrap() = Instant::now();
            self.missed_pongs.store(0, Ordering::SeqCst);
            return; // pong 不必转给前端
        }

        // 配置保存成功 ⇒ **必须重启 worker 才生效**（D-S1，见
        // `docs/设置界面与模型下载_执行单.md`）。Python 侧的 `Config` 只在进程启动时
        // 读一次，`Dispatcher` 的并发与模型缓存都按它算好了；热应用要重建调度器、
        // 重算并发、清模型缓存，每条都是新的 bug 面，而重启只要 2-3 秒。
        //
        // ⚠️ **只在这个事件上重启**，不要在 `worker_send` 里无脑重启 ——
        // 那样每条命令都会重启一次。
        let restart_for_config = ev.name == "config_saved";

        // 状态类事件：同步到快照，让 `worker_state` 命令能返回最新值
        match ev.name.as_str() {
            "ready" => {
                let mut snap = self.snapshot.lock().unwrap();
                snap.state = "ready".into();
                snap.detail = "worker 已就绪".into();
                snap.alive = true;
                snap.server_version = ev.data["server_version"].as_str().map(str::to_string);
                snap.gpu_usable = ev.data["gpu_usable"].as_bool();
                snap.gpu_verified = ev.data["gpu_verified"].as_bool();
                snap.concurrency = Some(ev.data["concurrency"].clone());
                // 规格 §4：`ready` 事件本身带 `pipeline_state`，顺手记下 ——
                // 前端挂载时拉快照就能拿到调度器状态，不必等下一次跃迁。
                snap.pipeline_state = ev.data["pipeline_state"].as_str().map(str::to_string);
                drop(snap);
                self.emit_state();
            }
            "env_verified" => {
                let mut snap = self.snapshot.lock().unwrap();
                snap.gpu_usable = ev.data["gpu_usable"].as_bool();
                // ⚠️ 这里原先是 `Some(true)` **无条件** —— 于是「验证超时、结论未知」
                // （`gpu_usable: null`）也被记成「已验证」，状态栏再按 `!gpu_usable`
                // 判成「GPU 不可用」。三态布尔在 Rust 这一层被压成了二态。
                // 正确语义：非 null 才算验过。
                snap.gpu_verified = Some(!ev.data["gpu_usable"].is_null());
                snap.blocked_reason = ev.data["blocked_reason"].as_str().map(str::to_string);
                snap.env_detail = ev.data["detail"].as_str().map(str::to_string);
                if !ev.data["concurrency"].is_null() {
                    snap.concurrency = Some(ev.data["concurrency"].clone());
                }
                drop(snap);
                self.emit_state();
            }
            // 调度器状态跃迁（规格 §4）。只更新快照，不写库、不改状态机。
            "pipeline_state" => {
                if let Some(s) = ev.data["state"].as_str() {
                    self.snapshot.lock().unwrap().pipeline_state = Some(s.to_string());
                    self.emit_state();
                }
            }
            // 规格 §8：`fatal: true` 表示「重启也没用」的环境级故障，记下来，
            // 崩溃恢复时据此**不重启**（见 `Inner::fatal`）。事件本身照常转发给前端。
            "error" => {
                if ev.data["fatal"].as_bool() == Some(true) {
                    self.fatal.store(true, Ordering::SeqCst);
                    let code = ev.data["code"].as_str().unwrap_or("E_?");
                    eprintln!("[worker] 收到致命错误 {code}，后续崩溃将不再自动重启");
                }
            }
            "bye" => {
                self.shutting_down.store(true, Ordering::SeqCst);
            }
            _ => {}
        }

        // 全量转发给前端。前端按 name 自行分发。
        if let Err(e) = self.app.emit(EVENT_CHANNEL, &ev) {
            eprintln!("[worker] 事件转发失败（{}）：{e}", ev.name);
        }

        // 转发之后再重启：让 `config_saved`（含 `warnings`）先到达界面，
        // 否则用户看到的是「点了保存，界面只在转圈」。
        // 放独立线程：本函数跑在 stdout 读线程上，重启会换掉这一代的读线程与 child，
        // 在本线程里做会让「本代 EOF」与「新一代启动」交错（`child_gen` 守卫已能兜住，
        // 但少一层交错就少一次要靠守卫解释的日志）。
        if restart_for_config {
            match self.me.upgrade() {
                Some(me) => {
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_millis(80));
                        if me.shutting_down.load(Ordering::SeqCst) {
                            return;
                        }
                        let w = Worker { inner: me };
                        w.inner
                            .emit_log("info", "supervisor", "配置已保存，重启 worker 以生效");
                        w.restart();
                    });
                }
                None => eprintln!("[worker] 监督器已销毁，忽略配置重启"),
            }
        }
    }

    /// 日志事件的**唯一**发射口。
    ///
    /// 与 `emit_state` 同样的理由：手写 `app.emit(EVENT_CHANNEL, json!{..})` 的地方
    /// 一旦多起来，就会出现「有的路径记了、有的没记」。
    ///
    /// `source` 区分来源（`stderr` = Python 侧日志，`supervisor` = Rust 监督器），
    /// 前端日志区直接展示。**不要**把 Python 的 stdout/stderr 都塞成同一个 source——
    /// 排查时「这行是 Python 说的还是 Rust 说的」是第一个要回答的问题。
    fn emit_log(&self, level: &str, source: &str, msg: impl Into<String>) {
        let _ = self.app.emit(
            EVENT_CHANNEL,
            json!({
                "v": protocol::PROTOCOL_VERSION,
                "type": "evt",
                "name": "log",
                "id": null,
                "seq": null,
                "ts": 0.0,
                "data": {"level": level, "msg": msg.into(), "source": source}
            }),
        );
    }

    /// 状态快照的**唯一**发射口。所有状态变更都必须经过这里。
    ///
    /// 之前这里有两个几乎相同的函数（`Worker::emit_state` 与 `emit_state_locked`），
    /// 差别只在加锁写法；另有一条心跳路径直接 `app.emit(STATE_CHANNEL, ..)`，
    /// 把两者都绕开了。结果是「有的状态跃迁能查到、有的查不到」——排查时最容易
    /// 误判成「状态机没走到某一步」。合并成一个并把日志放在这里，就不可能漏记。
    ///
    /// 调用约定：调用方须已释放 `snapshot` 的锁（本函数内部会再取一次）。
    fn emit_state(&self) {
        let snap = {
            let mut guard = self.snapshot.lock().unwrap();
            // 快照里的 `restart_count` 由这里从原子量同步 —— 让**原子量成为唯一真源**。
            // 否则 `set_state` / 心跳线程那些「整份替换快照」的路径会把计数悄悄清零，
            // 现象是「明明重启过一次，界面显示 restart=0」。
            guard.restart_count = self.restart_count.load(Ordering::SeqCst);
            guard.clone()
        };
        // 状态跃迁必须同时落 stderr。否则失败在终端里**完全不可见**——
        // 例如 `locate()` / spawn 失败只写进 UI 快照，而那一刻前端往往还没挂载，
        // 现象就是「窗口开了、一句话没有、Python 进程也不在」，无从下手。
        // 这一行是排查这类静默失败的主要入口，不要删。
        eprintln!(
            "[worker] state → {}（{}）alive={} restart={} ver={:?} gpu={:?}/{:?}",
            snap.state,
            snap.detail,
            snap.alive,
            snap.restart_count,
            snap.server_version,
            snap.gpu_usable,
            snap.gpu_verified
        );
        if let Err(e) = self.app.emit(STATE_CHANNEL, snap) {
            eprintln!("[worker] 状态事件发送失败：{e}");
        }
    }

    /// stdout 读到 EOF —— worker 已退出。
    ///
    /// 退出**必须同时进日志区**，不能只更新状态快照：状态栏只显示 detail 的首行，
    /// 而用户排查时的第一反应是去翻日志 —— 那里若还停在崩溃前的最后一条，
    /// 得到的印象会是「一切正常，然后就没了」。
    ///
    /// 实测（2026-10-03 崩溃注入）：不加这一条时，杀了 worker 之后日志区最后一行
    /// 仍是崩溃前的 `环境验证：GPU 可用`，看不出已经出事。
    ///
    /// 本函数同时是**崩溃自动重启**的触发点（规格 §7）。
    fn on_worker_exit(&self, gen: u64) {
        // **代号守卫**：本代读线程结束时，若已经 spawn 了新一代，这次 EOF 是预期内的
        // （用户重启 / 保存配置后重启）。此时绝不能往下走 —— `child` 里已是新一代的
        // 进程，`wait()` 会阻塞到它退出，且会把 `alive` 与状态一起搞坏。
        let current = self.child_gen.load(Ordering::SeqCst);
        if current != gen {
            eprintln!("[worker] 第 {gen} 代读线程结束，已被第 {current} 代取代，跳过收尾");
            return;
        }

        // 这次 swap 的返回值告诉我们「心跳线程是否已经先一步判过死」——
        // 只用来决定要不要再报一条「意外退出」，不影响是否重启：
        // 旧版本在这里直接 `return`，导致「心跳判死 → 进程随后真的退出」这条路径
        // 永远不会重启。两条路径描述的是同一次死亡，不该各有各的命运。
        let was_alive = self.alive.swap(false, Ordering::SeqCst);
        self.stop_heartbeat.store(true, Ordering::SeqCst);
        *self.stdin.lock().unwrap() = None;

        let code = self
            .child
            .lock()
            .unwrap()
            .as_mut()
            .and_then(|c| c.wait().ok())
            .and_then(|s| s.code());

        if self.shutting_down.load(Ordering::SeqCst) {
            let detail = format!("worker 已正常退出（code={code:?}）");
            self.emit_log("info", "supervisor", detail.clone());
            let mut snap = self.snapshot.lock().unwrap();
            snap.state = "exited".into();
            snap.detail = detail;
            snap.alive = false;
            drop(snap);
            self.emit_state();
            return;
        }

        if was_alive {
            self.emit_log(
                "error",
                "supervisor",
                format!("worker 意外退出（code={code:?}）"),
            );
        } else {
            // 心跳线程已经报过原因，这里不再重复一条 error，只补上「进程确实结束了」。
            self.emit_log(
                "info",
                "supervisor",
                format!("worker 进程已确认结束（code={code:?}），进入崩溃恢复判定"),
            );
        }

        self.try_recover_after_crash(code);
    }

    /// 整份替换快照并广播。与 `Worker::set_state` 同义，只是给 `impl Inner` 用
    /// ——崩溃恢复跑在 `Inner` 的方法里，那里拿不到 `Worker`（`Worker` 只是
    /// `Arc<Inner>` 的薄包装，而这一步不需要新建一个）。
    fn set_snapshot(&self, snap: StateSnapshot) {
        *self.snapshot.lock().unwrap() = snap;
        self.emit_state();
    }

    /// 崩溃恢复判定（规格 §7：自动重启一次；再失败则提示用户并展示 stderr 尾部）。
    fn try_recover_after_crash(&self, code: Option<i32>) {
        let fatal = self.fatal.load(Ordering::SeqCst);
        let used = self.restart_count.load(Ordering::SeqCst);

        match decide_recovery(fatal, used) {
            // ① 环境级故障 —— 重启也没用（规格 §8）。把原因和 stderr 尾部摊开。
            Recovery::NoRestartFatal => {
                let detail = format!(
                    "worker 因环境级故障退出（code={code:?}），已按规格 §8 不自动重启。\
                     修复环境后可用工具栏的「重启 worker」重试。{}",
                    self.tail_hint()
                );
                self.emit_log("error", "supervisor", detail.clone());
                self.set_snapshot(StateSnapshot {
                    state: "exited".into(),
                    detail,
                    alive: false,
                    ..StateSnapshot::starting()
                });
            }

            // ② 还有额度 —— 退避后自动重启。
            Recovery::Restart { attempt } => {
                self.restart_count.store(attempt, Ordering::SeqCst);
                let detail =
                    format!("worker 意外退出（code={code:?}），正在自动重启（第 {attempt}/{MAX_RESTARTS} 次）");
                self.emit_log("warning", "supervisor", detail.clone());
                self.set_snapshot(StateSnapshot {
                    state: "restarting".into(),
                    detail,
                    alive: false,
                    ..StateSnapshot::starting()
                });

                // 在独立线程里退避后重启：本函数跑在 stdout 读线程上，
                // 在这里直接 sleep 会让该线程多活 1 秒（虽无害，但没必要）。
                match self.me.upgrade() {
                    Some(me) => {
                        std::thread::spawn(move || {
                            std::thread::sleep(RESTART_BACKOFF);
                            // 退避期间用户可能已经关了应用，别把 worker 再拉起来。
                            if me.shutting_down.load(Ordering::SeqCst) {
                                return;
                            }
                            let w = Worker { inner: me };
                            w.spawn_worker();
                            w.start_heartbeat();
                        });
                    }
                    None => eprintln!("[worker] 监督器已销毁，放弃自动重启"),
                }
            }

            // ③ 额度用尽 —— 停下来，把原因摊开，并明确告诉用户还有手动这条路。
            //    这一条对应故障态验证里发现的第 3 个缺陷（「没有任何恢复入口」）。
            Recovery::GiveUp { attempt } => {
                self.restart_count.store(attempt, Ordering::SeqCst);
                let detail = format!(
                    "worker 连续 {attempt} 次退出（code={code:?}），已放弃自动重启。\
                     排查后可用工具栏的「重启 worker」重试。{}",
                    self.tail_hint()
                );
                self.emit_log("error", "supervisor", detail.clone());
                self.set_snapshot(StateSnapshot {
                    state: "exited".into(),
                    detail,
                    alive: false,
                    ..StateSnapshot::starting()
                });
            }
        }
    }

    /// 把 stderr 尾部并成一句可读的提示（为空则返回空串）。
    fn tail_hint(&self) -> String {
        let tail = self.stderr_tail.lock().unwrap();
        if tail.is_empty() {
            return String::new();
        }
        format!("\n--- Python stderr 尾部（{} 行）---\n{}", tail.len(), tail.iter().cloned().collect::<Vec<_>>().join("\n"))
    }

    /// stderr 尾部的定长环形缓冲。
    fn push_stderr_tail(&self, line: &str) {
        let mut tail = self.stderr_tail.lock().unwrap();
        push_bounded(&mut tail, line, STDERR_TAIL_LINES);
    }

    /// 收掉上一代可能还留着的子进程句柄（手动重启前调用）。
    ///
    /// 「心跳判死但进程没死」（长转写阻塞主循环，见 `start_heartbeat` 里的说明）
    /// 会走到这里：用户点「重启 worker」时，旧进程必须真的被清掉，
    /// 否则会同时存在两个 worker，两个都在读写同一个 `state.db`。
    fn reap_stale_child(&self) {
        if let Some(mut child) = self.child.lock().unwrap().take() {
            // 只有**真的还在跑**才需要杀、才值得报。崩溃路径上这个 `Child` 已经被
            // `wait()` 收过了，那时 `kill()` 只会返回「进程已结束」的错误，
            // 报成「已终止残留进程」是在撒谎。
            if matches!(child.try_wait(), Ok(None)) {
                let _ = child.kill();
                self.emit_log("warning", "supervisor", "已终止上一代残留的 worker 进程");
            }
            let _ = child.wait();
        }
        *self.stdin.lock().unwrap() = None;
        self.stop_heartbeat.store(true, Ordering::SeqCst);
    }

    /// 心跳线程用的无锁快路径（`Worker::send` 的薄包装）。
    fn send_raw(&self, name: &str, args: Value) -> Result<String, String> {
        let serial = self.serial.fetch_add(1, Ordering::SeqCst) + 1;
        let id = make_command_id(serial);
        let cmd = WireCommand::new(&id, name, args);
        let mut line = serde_json::to_string(&cmd).map_err(|e| e.to_string())?;
        let mut guard = self.stdin.lock().unwrap();
        let writer = guard.as_mut().ok_or("worker 未运行")?;
        line.push('\n');
        writer
            .write_all(line.as_bytes())
            .and_then(|_| writer.flush())
            .map_err(|e| e.to_string())?;
        Ok(id)
    }
}

impl Drop for Inner {
    fn drop(&mut self) {
        self.stop_heartbeat.store(true, Ordering::SeqCst);
        if let Some(c) = self.child.lock().unwrap().as_mut() {
            let _ = c.kill();
        }
    }
}

/// Windows 错误码：命名管道「读方向」被外部注入层拦截时，spawn 会返回它。
/// 具详见 `docs/阶段3_界面确认.md` §7.7，判据 `tools/probe_spawn_env.py`。
const ERR_PIPE_BUSY: i32 = 231;
/// `ERROR_ACCESS_DENIED`
const ERR_ACCESS_DENIED: i32 = 5;
/// `ERROR_FILE_NOT_FOUND`
const ERR_FILE_NOT_FOUND: i32 = 2;

/// 把 spawn 失败翻译成**用户能照着做**的一句话（规格 §7 的「可操作提示」这条要求）。
///
/// 返回的字符串是详情里的**第一行** —— 状态栏只渲染首行，所以动作必须写在这一行里，
/// 放到后面几行等于没给（`StatusBar.tsx` 的注释记着同一个教训）。
///
/// 抽成纯函数是为了能单测：`spawn_child` 本身要真的去启一个失败的进程才跑得到。
fn hint_for_spawn_error(code: Option<i32>) -> Option<&'static str> {
    match code {
        Some(ERR_PIPE_BUSY) => Some(
            "命中 ERROR_PIPE_BUSY(231)：命名管道「读方向」被外部注入层拦截。\
             **请改用普通终端启动本程序**（AI 工具沙箱内必然复现，重启系统无效）。\
             判据：python tools\\probe_spawn_env.py",
        ),
        Some(ERR_ACCESS_DENIED) => Some(
            "权限不足(5)：检查解释器与工作目录的访问权限。\
             安全软件/沙箱拦截也会表现为 5。",
        ),
        Some(ERR_FILE_NOT_FOUND) => Some(
            "找不到解释器(2)：确认路径存在，或用环境变量 VIDIGEST_PYTHON 显式指定。",
        ),
        _ => None,
    }
}

/// 启动子进程。**三处细节不能省**：
/// 1. `CREATE_NO_WINDOW` —— 否则会闪一个黑色控制台窗口（规格 §7）
/// 2. `PYTHONUTF8/IOENCODING` —— 否则 Python 按 cp936 输出，父子两端编码不一致
/// 3. 三条管道全部 piped —— stderr 不接会让 Python 在缓冲区满时阻塞（规格 §7）
fn spawn_child(target: &locator::WorkerTarget) -> Result<Child, String> {
    let mut cmd = Command::new(&target.program);
    cmd.args(&target.args)
        .current_dir(&target.cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1");

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    cmd.spawn().map_err(|e| {
        // 第一行 = 结论 + 动作（状态栏只显示首行）；后面才是细节。
        // 已知错误码走 `hint_for_spawn_error`，未知错误码保留系统原文 ——
        // **不要**给未知错误编一句"听起来合理"的话，那会把人引到错的方向。
        let hint = hint_for_spawn_error(e.raw_os_error());
        let mut msg = match hint {
            Some(h) => format!("启动 worker 失败：{h}\n  原始错误：{e}"),
            None => format!("启动 worker 失败（{e}）"),
        };
        msg.push_str(&format!(
            "\n  解释器：{}\n  工作目录：{}",
            target.program.display(),
            target.cwd.display()
        ));
        msg
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_crash_triggers_exactly_one_restart() {
        // 规格 §7：崩溃后自动重启**一次**
        assert_eq!(
            decide_recovery(false, 0),
            Recovery::Restart { attempt: 1 }
        );
    }

    #[test]
    fn second_crash_gives_up_and_points_user_at_manual_restart() {
        // 已经用掉那一次，再来就是放弃 —— 不能让界面停在「按钮全灰、无路可走」
        assert_eq!(decide_recovery(false, 1), Recovery::GiveUp { attempt: 2 });
        assert_eq!(decide_recovery(false, 7), Recovery::GiveUp { attempt: 8 });
    }

    #[test]
    fn fatal_error_suppresses_restart() {
        // 规格 §8：环境级故障「重启也没用」，一次都不该重启
        assert_eq!(decide_recovery(true, 0), Recovery::NoRestartFatal);
    }

    #[test]
    fn stderr_tail_keeps_only_the_newest_lines() {
        let mut buf = VecDeque::new();
        for i in 1..=25 {
            push_bounded(&mut buf, &format!("line-{i}"), 20);
        }
        assert_eq!(buf.len(), 20, "必须定长");
        assert_eq!(buf.front().unwrap(), "line-6", "丢的是最旧的");
        assert_eq!(buf.back().unwrap(), "line-25", "留的是最新的");
    }

    #[test]
    fn stderr_tail_with_zero_capacity_stays_empty() {
        // 边界：容量为 0 时不能变成「永远保留 1 条」
        let mut buf = VecDeque::new();
        push_bounded(&mut buf, "x", 0);
        assert!(buf.is_empty());
    }

    #[test]
    fn restart_clears_stale_gpu_conclusions_from_snapshot() {
        // 重启必须把上一代的 GPU/并发结论清掉：新进程可能根本起不来，
        // 留着旧结论会让界面显示「GPU 可用」而实际没有 worker。
        // 这里锁住的是 `starting()` 的默认值 —— `spawn_worker` 依赖它来重置。
        let s = StateSnapshot::starting();
        assert_eq!(s.gpu_usable, None);
        assert_eq!(s.gpu_verified, None);
        assert!(s.concurrency.is_none());
        assert_eq!(s.state, "starting");
        assert!(!s.alive);
    }

    #[test]
    fn snapshot_serializes_pipeline_state_for_frontend() {
        // 前端 `App.tsx` 读 `snapshot.pipeline_state`，`types.ts` 也声明了它。
        // Rust 侧曾长期没有这个字段（三端漂移），这里锁住它必须出现在 JSON 里。
        let mut s = StateSnapshot::starting();
        s.pipeline_state = Some("running".into());
        let v: Value = serde_json::to_value(&s).unwrap();
        assert_eq!(v["pipeline_state"], json!("running"));
        assert!(v.get("restart_count").is_some());
    }

    #[test]
    fn snapshot_carries_blocked_reason_so_ui_can_say_model_missing() {
        // 2026-10-24 的误报：模型没装（`gpu_usable = null`）被界面说成
        // 「GPU 不可用」。Rust 这一层原先把 `gpu_verified` 无条件写成 `true`，
        // 于是三态被压成二态。这条锁住三件事：
        // 1. `gpu_verified` 必须与 `gpu_usable` 的三态一致（null 时不能是 true）；
        // 2. `blocked_reason` 必须出现在快照里（前端靠它区分「没验成」与「不能用」）；
        // 3. `env_detail`（原始说明）也要在，状态栏 tooltip 用。
        let mut s = StateSnapshot::starting();
        s.gpu_usable = None;
        s.gpu_verified = Some(false);
        s.blocked_reason = Some("model_missing".into());
        s.env_detail = Some("模型目录不存在：C:\\x\\large-v3".into());
        let v: Value = serde_json::to_value(&s).unwrap();
        assert_eq!(v["gpu_usable"], Value::Null);
        assert_eq!(v["gpu_verified"], json!(false));
        assert_eq!(v["blocked_reason"], json!("model_missing"));
        assert!(v["env_detail"].as_str().unwrap().contains("模型目录不存在"));
    }

    #[test]
    fn pipe_busy_231_is_translated_into_an_actionable_first_line() {
        // 231 是本项目最贵的一个坑（排查了两天）。用户看到 `os error 231`
        // 是没有任何用处的；首行必须直接给出**动作**。
        let hint = hint_for_spawn_error(Some(231)).expect("231 必须被翻译");
        assert!(hint.contains("普通终端"), "必须给出唯一的处置动作：{hint}");
        assert!(
            hint.contains("probe_spawn_env"),
            "必须指出可复跑判据：{hint}"
        );
        assert!(
            !hint.contains('\n'),
            "必须是单行 —— 状态栏只渲染首行，带换行会把动作截掉"
        );
    }

    #[test]
    fn other_known_codes_get_hints_and_unknown_ones_get_none() {
        assert!(hint_for_spawn_error(Some(5)).is_some());
        assert!(hint_for_spawn_error(Some(2)).is_some());
        // 未知错误码必须返回 None：调用方会保留系统原文，
        // **不要**为了"界面好看"编一句听起来合理的话。
        assert!(hint_for_spawn_error(Some(2_147_000_000)).is_none());
        assert!(hint_for_spawn_error(None).is_none());
    }

    #[test]
    fn close_is_confirmed_only_when_tasks_are_advancing() {
        // 有任务在推进 → 拦下来问
        assert!(needs_close_confirmation(false, true, Some("running")));
        assert!(needs_close_confirmation(false, true, Some("paused")));
        // 没任务 / worker 不在 → 直接放行
        assert!(!needs_close_confirmation(false, true, Some("idle")));
        assert!(!needs_close_confirmation(false, true, Some("aborted")));
        assert!(!needs_close_confirmation(false, true, Some("stopped")));
        assert!(!needs_close_confirmation(false, true, None));
        assert!(!needs_close_confirmation(false, false, Some("running")));
    }

    #[test]
    fn already_confirmed_close_is_never_re_blocked() {
        // 点过「退出」之后再走一遍关闭流程必须放行，否则弹窗会无限重开。
        assert!(!needs_close_confirmation(true, true, Some("running")));
    }
}
