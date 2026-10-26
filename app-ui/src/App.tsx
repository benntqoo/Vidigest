// 主界面：把协议事件流接到 UI 上。
//
// 本文件只做「事件 → 状态 → 视图」的映射，不含协议细节：
// - 通道名、命令名、类型定义全在 `worker/` 下，与 `docs/IPC协议规格.md` 对齐
// - 组件只接收普通 props，不 import `@tauri-apps/api`
//
// 两条容易写错的地方，都已按规格处理：
// 1. `task_update` 是**增量**语义（规格 §4）。这里按 task_id 合并进已有快照，
//    而不是整表替换 —— 否则并发时后到的部分更新会把别的任务抹掉。
// 2. `progress < 0` 表示「进行中但进度不可知」，不是 0%（规格 §5）。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { revealItemInDir } from "@tauri-apps/plugin-opener";

import "./App.css";
import { AddTasksBar } from "./components/AddTasksBar";
import { LogPanel, type LogEntry } from "./components/LogPanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { StatusBar } from "./components/StatusBar";
import { TaskTable } from "./components/TaskTable";
import { WorkDirGuide } from "./components/WorkDirGuide";
import * as api from "./worker/client";
import type { CloseRequest } from "./worker/client";
import type {
  AppConfig,
  ConfigInfo,
  DownloadState,
  PipelineState,
  TaskSnapshot,
  WorkerEvent,
  WorkerState,
} from "./worker/types";

/** 日志区保留的最大条数。超出后丢最旧的 —— 长跑不设上限会吃光内存。 */
const MAX_LOGS = 500;

/**
 * 拼 `out_dir/transcript.txt`。
 *
 * 刻意不引 path 库、也不做归一化：`out_dir` 是 Python 侧给的 Windows 路径，
 * 直接把反斜杠拼上去最不容易出错。这里唯一的约定（`transcript.txt` 这个名字）
 * 来自 `app/core/pipeline.py`，改那边就要改这里。
 */
function transcriptPath(outDir: string): string {
  return `${outDir.replace(/[\\/]+$/, "")}\\transcript.txt`;
}

function App() {
  const [worker, setWorker] = useState<WorkerState | null>(null);
  const [tasks, setTasks] = useState<Record<number, TaskSnapshot>>({});
  const [pipeline, setPipeline] = useState<PipelineState>("idle");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const logSeq = useRef(0);
  /** 非空 = 用户点了关闭、但还有任务在跑，等他选（`PLAN §7.2` 第 3 条） */
  const [closeReq, setCloseReq] = useState<CloseRequest | null>(null);
  /** 确认退出后 worker 还要收尾，界面停一会儿 —— 别让用户以为卡死了 */
  const [shuttingDown, setShuttingDown] = useState(false);

  // ---- 设置与模型（2026-10-24）----
  /** 设置模态开关。模型未安装时状态栏会直接点进来。 */
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [configInfo, setConfigInfo] = useState<ConfigInfo | null>(null);
  const [download, setDownload] = useState<DownloadState | null>(null);
  /** 上一次保存的 warnings（`E_BASELINE_PARAM_CHANGED` 等），由 `config_saved` 给 */
  const [saveWarnings, setSaveWarnings] = useState<string[]>([]);
  /** 非 null = 已保存、正在等 worker 重启生效。存开始时刻，用来区分「旧快照的 ready」 */
  const [applyingSince, setApplyingSince] = useState<number | null>(null);
  /** 手动重验中（`verify_env` 已发、`env_verified` 未到） */
  const [verifying, setVerifying] = useState(false);
  /** 首启工作目录引导是否已被「稍后再说」收起（只在本进程内有效，重启再问） */
  const [guideDismissed, setGuideDismissed] = useState(false);

  const pushLog = useCallback(
    (level: LogEntry["level"], msg: string, source?: string) => {
      logSeq.current += 1;
      const entry: LogEntry = { key: logSeq.current, level, msg, source };
      setLogs((prev) => {
        const next = prev.length >= MAX_LOGS ? prev.slice(prev.length - MAX_LOGS + 1) : prev.slice();
        next.push(entry);
        return next;
      });
    },
    [],
  );

  /** 拉全量任务快照。`task_list` 事件到达后也会调用，保证与库一致。 */
  const refreshTasks = useCallback(() => {
    api.listTasks().catch(() => {
      /* worker 可能尚未 ready，属正常，静默即可 */
    });
  }, []);

  /** 拉配置 + 模型元信息（响应事件 `config`）。 */
  const refreshConfig = useCallback(() => {
    api.getConfig().catch(() => {
      /* worker 尚未 ready 时忽略；设置页会停在「正在读取配置…」 */
    });
  }, []);

  /** 打开设置：先拉一次最新配置，避免拿上次的旧值做草稿。 */
  const openSettings = useCallback(() => {
    setSettingsOpen(true);
    setSaveWarnings([]);
    refreshConfig();
  }, [refreshConfig]);

  const handleEvent = useCallback(
    (ev: WorkerEvent) => {
      switch (ev.name) {
        case "ready": {
          const p = ev.data.pipeline_state;
          if (typeof p === "string") setPipeline(p as PipelineState);
          break;
        }
        case "pipeline_state":
        case "state": {
          const s = ev.data.state;
          if (typeof s === "string") setPipeline(s as PipelineState);
          break;
        }
        case "task_list": {
          const list = ev.data.tasks;
          if (Array.isArray(list)) {
            const map: Record<number, TaskSnapshot> = {};
            for (const t of list as TaskSnapshot[]) map[t.task_id] = t;
            setTasks(map);
          }
          break;
        }
        case "task_update": {
          const patch = ev.data as unknown as TaskSnapshot;
          if (typeof patch?.task_id !== "number") break;
          setTasks((prev) => ({
            ...prev,
            [patch.task_id]: { ...prev[patch.task_id], ...patch },
          }));
          break;
        }
        case "tasks_added": {
          const added = ev.data.added as unknown[] | undefined;
          const skipped = ev.data.skipped as { url?: string; reason?: string }[] | undefined;
          pushLog(
            skipped?.length ? "warning" : "info",
            `入队 ${added?.length ?? 0} 条${skipped?.length ? `，跳过 ${skipped.length} 条` : ""}`,
          );
          for (const s of skipped ?? []) {
            pushLog("warning", `跳过 ${s.url ?? "?"}（${s.reason ?? "未知原因"}）`);
          }
          refreshTasks();
          break;
        }
        case "task_removed":
          refreshTasks();
          break;
        case "log": {
          const level = (ev.data.level as LogEntry["level"]) ?? "info";
          pushLog(level, String(ev.data.msg ?? ""), ev.data.source as string | undefined);
          break;
        }
        case "error": {
          // 环境级故障会同时发 `batch_aborted` 与 `error(fatal=false)`，
          // 这里照实显示，不合并 —— 前者说「整批停了」，后者说「为什么」。
          pushLog("error", `${ev.data.code ?? "E_?"}：${ev.data.msg ?? ""}`);
          break;
        }
        case "resource_warning":
        case "batch_aborted": {
          pushLog("warning", `${ev.name}：${JSON.stringify(ev.data)}`);
          break;
        }
        case "batch_done": {
          pushLog("info", `整批结束：${JSON.stringify(ev.data)}`);
          refreshTasks();
          break;
        }

        // ---- 设置与模型（2026-10-24）----
        case "config": {
          setConfigInfo(ev.data as unknown as ConfigInfo);
          break;
        }
        case "config_saved": {
          // `config_saved` 之后 Rust 会重启 worker（约 80 ms 后），所以这里
          // **先用响应体里的 config 更新界面**，不要指望再发一条 get_config ——
          // 那条命令可能正好落在新旧进程交替的空档里，丢掉。
          const cfg = ev.data.config as unknown as AppConfig | undefined;
          if (cfg) {
            setConfigInfo((prev) =>
              prev
                ? { ...prev, config: cfg, path: String(ev.data.path ?? prev.path), is_default: false }
                : prev,
            );
          }
          setSaveWarnings(Array.isArray(ev.data.warnings) ? (ev.data.warnings as string[]) : []);
          setApplyingSince(Date.now());
          pushLog("info", "配置已保存，正在重启 worker 以生效");
          break;
        }
        case "model_download_started": {
          setDownload({
            phase: "running",
            doneBytes: Number(ev.data.already_bytes ?? 0),
            totalBytes: Number(ev.data.total_bytes ?? 0),
            pct:
              Number(ev.data.total_bytes ?? 0) > 0
                ? Number(ev.data.already_bytes ?? 0) / Number(ev.data.total_bytes)
                : 0,
            speed: 0,
            eta: 0,
          });
          pushLog("info", `开始下载模型 ${String(ev.data.model ?? "")}`.trim());
          break;
        }
        case "model_download_progress": {
          const d = ev.data;
          setDownload({
            phase: "running",
            doneBytes: Number(d.done_bytes ?? 0),
            totalBytes: Number(d.total_bytes ?? 0),
            pct: Number(d.overall_pct ?? 0),
            speed: Number(d.bytes_per_sec ?? 0),
            eta: Number(d.eta_sec ?? 0),
            file: d.file as string | undefined,
            fileIndex: Number(d.file_index ?? 0),
            fileCount: Number(d.file_count ?? 0),
          });
          break;
        }
        case "model_download_verifying": {
          setDownload((prev) =>
            prev ? { ...prev, phase: "verifying", pct: 1 } : {
              phase: "verifying",
              doneBytes: 0,
              totalBytes: 0,
              pct: 1,
              speed: 0,
              eta: 0,
            },
          );
          break;
        }
        case "model_download_done": {
          setDownload((prev) => ({
            phase: "done",
            doneBytes: Number(ev.data.total_bytes ?? prev?.doneBytes ?? 0),
            totalBytes: Number(ev.data.total_bytes ?? prev?.totalBytes ?? 0),
            pct: 1,
            speed: 0,
            eta: 0,
            verified: (ev.data.verified as boolean | null) ?? null,
            detail: String(ev.data.detail ?? ""),
            elapsedSec: Number(ev.data.elapsed_sec ?? 0),
          }));
          setVerifying(false);
          pushLog(
            ev.data.verified === true ? "info" : "warning",
            `模型下载完成：${String(ev.data.detail ?? "")}`,
          );
          // 下完再拉一次：`model_present` / 大小 / 缺失文件列表都变了
          refreshConfig();
          break;
        }
        case "model_download_failed": {
          setDownload((prev) => ({
            phase: "failed",
            doneBytes: prev?.doneBytes ?? 0,
            totalBytes: prev?.totalBytes ?? 0,
            pct: prev?.pct ?? 0,
            speed: 0,
            eta: 0,
            code: String(ev.data.code ?? "E_?"),
            detail: String(ev.data.detail ?? ""),
          }));
          pushLog("error", `模型下载失败 ${String(ev.data.code ?? "")}：${String(ev.data.detail ?? "")}`);
          break;
        }
        case "model_download_cancelled": {
          setDownload((prev) => ({
            phase: "cancelled",
            doneBytes: prev?.doneBytes ?? 0,
            totalBytes: prev?.totalBytes ?? 0,
            pct: prev?.pct ?? 0,
            speed: 0,
            eta: 0,
            detail: String(ev.data.detail ?? ""),
          }));
          pushLog("warning", "模型下载已取消");
          break;
        }
        case "env_verified": {
          // 手动重验的收尾。`gpu_usable` 的三态语义由 StatusBar 负责解释，
          // 这里只关心「不再转圈」。
          setVerifying(false);
          break;
        }
        case "env_verify_started": {
          setVerifying(true);
          break;
        }
        default:
          break;
      }
    },
    [pushLog, refreshTasks],
  );

  useEffect(() => {
    let disposed = false;
    const unlisteners: (() => void)[] = [];

    // 先订阅再拉快照：反过来会漏掉「拉快照」与「订阅生效」之间的事件。
    // 若 effect 已被回收（React 严格模式会跑两次），拿到句柄后立即注销。
    api.onState((s) => {
      if (!disposed) setWorker(s);
    }).then((fn) => (disposed ? fn() : unlisteners.push(fn)));

    api.onEvent((ev) => {
      if (!disposed) handleEvent(ev);
    }).then((fn) => (disposed ? fn() : unlisteners.push(fn)));

    // 关闭确认：Rust 侧已 `prevent_close`，由这里决定放不放行。
    // **不能只在前端判断「有没有任务在跑」** —— 那需要前端自己维护一份权威状态，
    // 而权威在 Rust（`should_confirm_close` 读的是快照）。
    api
      .onCloseRequested((req) => {
        if (!disposed) setCloseReq(req);
      })
      .then((fn) => (disposed ? fn() : unlisteners.push(fn)));

    api
      .getWorkerState()
      .then((snap) => {
        if (disposed) return;
        setWorker(snap);
        if (snap.pipeline_state) setPipeline(snap.pipeline_state);
      })
      .catch((e) => pushLog("error", `拉取连接状态失败：${e}`));

    refreshTasks();

    return () => {
      disposed = true;
      for (const fn of unlisteners) fn();
    };
  }, [handleEvent, pushLog, refreshTasks]);

  /** 命令失败要显式落到日志区 —— Rust 侧返回的 Err 不弹窗，容易静默。 */
  const run = useCallback(
    (label: string, p: Promise<unknown>) => {
      p.catch((e) => pushLog("error", `${label}失败：${e}`));
    },
    [pushLog],
  );

  /**
   * 「已保存 → 正在生效」的收尾。
   *
   * 为什么不能只看 `worker.state === "ready"`：保存那一刻前端手里的快照通常还是
   * 上一代的 `ready`，重启还没开始。所以要求「距保存 > 2 秒」，再有一个 15 秒的
   * 兜底（重启失败时不能让横幅永远挂着）。
   */
  useEffect(() => {
    if (applyingSince === null) return;
    const tick = () => {
      const waited = Date.now() - applyingSince;
      if (waited > 15000) {
        setApplyingSince(null);
        return;
      }
      if (waited > 2000 && worker?.state === "ready") {
        setApplyingSince(null);
        pushLog("info", "配置已生效（worker 已重启）");
        refreshConfig();
      }
    };
    const id = window.setInterval(tick, 500);
    return () => window.clearInterval(id);
  }, [applyingSince, worker?.state, pushLog, refreshConfig]);

  const ordered = useMemo(
    () => Object.values(tasks).sort((a, b) => a.task_id - b.task_id),
    [tasks],
  );

  const busy = worker?.state === "ready";

  // worker 不在了（起不来或崩了）才需要这个按钮。
  // `restarting` / `starting` 不给 —— 那两种状态下重启已经在路上了，
  // 按钮会诱使用户连点，反而把正在起来的进程又杀一遍。
  const canRestart =
    worker != null && (worker.state === "failed" || worker.state === "exited");

  return (
    <div className="app">
      <header className="toolbar">
        <span className="brand">Vidigest</span>
        <AddTasksBar
          disabled={!busy}
          onSubmit={(text) => {
            // 抽 URL 是 Python 侧的责任（`urls.extract_many`），前端不做正则，
            // 否则两处规则会打架。这里只按行传过去。
            const lines = text
              .split(/\r?\n/)
              .map((s) => s.trim())
              .filter(Boolean);
            run("加入队列", api.addTasks(lines));
          }}
        />

        <span className="divider" />

        <button
          className="primary"
          disabled={!busy || pipeline === "running"}
          onClick={() => run("开始", api.startPipeline())}
          title="只入队未完成且非失败的任务（failed / cancelled 需在该行点「重试」）"
        >
          开始
        </button>
        <button
          disabled={!busy || pipeline !== "running"}
          onClick={() => run("暂停", api.pausePipeline())}
          title="已在跑的任务会跑完当前阶段为止"
        >
          暂停
        </button>
        <button
          disabled={!busy || pipeline !== "paused"}
          onClick={() => run("继续", api.resumePipeline())}
        >
          继续
        </button>

        {/*
          恢复入口：worker 起不来 / 崩了之后，上面四个按钮全是灰的，
          这里若也没有，用户唯一的选择就是关掉整个应用（故障态验证发现的第 3 个缺陷）。
          样式用 danger 是为了让它在一排灰按钮里显出来 —— 此刻它是唯一能做的事。
        */}
        {canRestart && (
          <button
            className="danger"
            onClick={() => run("重启 worker", api.restartWorker())}
            title={`${worker?.detail ?? ""}\n\n不用关闭应用，直接重来一遍`}
          >
            重启 worker
          </button>
        )}

        <span className="spacer" />

        <button
          className="mini"
          onClick={openSettings}
          title="设置：模型 / 路径 / 运行 / 转写 / 抽帧 / 汇总 / 下载"
        >
          设置
        </button>
        <button className="mini" onClick={() => refreshTasks()} title="重新拉取任务表">
          刷新
        </button>
      </header>

      <StatusBar worker={worker} pipeline={pipeline} onOpenSettings={openSettings} />

      <TaskTable
        tasks={ordered}
        onCancel={(id) => run(`取消 #${id}`, api.cancelTask(id))}
        onRetry={(id) => run(`重试 #${id}`, api.retryTask(id))}
        onRemove={(id) => run(`移除 #${id}`, api.removeTask(id))}
        onOpenDir={(path) =>
          revealItemInDir(path).catch((e) => pushLog("error", `打开目录失败 ${path}：${e}`))
        }
        onOpenTranscript={(outDir) => {
          const p = transcriptPath(outDir);
          // 用系统默认程序打开（.txt → 记事本）。失败时把路径打进日志 ——
          // 「转写还没生成」和「路径拼错了」是两件事，路径在日志里能一眼分开。
          api.openLocalPath(p).catch((e) => pushLog("error", `打开转写稿失败 ${p}：${e}`));
        }}
      />

      <LogPanel entries={logs} onClear={() => setLogs([])} />

      {/*
        首启工作目录引导。触发条件是显式的确认位，而不是「路径等于默认值」——
        后者会让「我就是要用 LOCALAPPDATA」的用户每次启动都被弹。
        与设置模态互斥：先让用户处理引导，避免两个覆盖层叠在一起。
      */}
      {!settingsOpen &&
        !guideDismissed &&
        configInfo?.config.paths.work_dir_confirmed === false && (
          <WorkDirGuide
            suggested={configInfo.config.paths.work_dir}
            installDir={configInfo.install_dir}
            busy={applyingSince !== null}
            onConfirm={(workDir) => {
              // **不在这里关掉引导**：保存可能失败（目录不可写 / 非法路径），
              // 乐观关闭会让用户以为设置好了。真正的关闭条件是 `config_saved`
              // 把 `work_dir_confirmed` 变成 true —— 那说明确实落盘了。
              setVerifying(false);
              run(
                "确认工作目录",
                api.setConfig({
                  paths: { work_dir: workDir, work_dir_confirmed: true },
                }),
              );
            }}
            onLater={() => setGuideDismissed(true)}
          />
        )}

      {settingsOpen && (
        <SettingsDialog
          info={configInfo}
          download={download}
          pipeline={pipeline}
          applying={applyingSince !== null}
          warnings={saveWarnings}
          workerState={worker?.state}
          verifying={verifying}
          onClose={() => setSettingsOpen(false)}
          onSave={(patch) => {
            setVerifying(false);
            run("保存配置", api.setConfig(patch));
          }}
          onReload={refreshConfig}
          onDownload={() => {
            setDownload({ phase: "running", doneBytes: 0, totalBytes: 0, pct: 0, speed: 0, eta: 0 });
            run("下载模型", api.downloadModel());
          }}
          onCancelDownload={() => run("取消下载", api.cancelModelDownload())}
          onVerify={() => {
            setVerifying(true);
            run("重新校验 GPU", api.verifyEnv());
          }}
        />
      )}

      {/*
        关闭确认（`PLAN §7.2` 第 3 条）。Rust 侧已经 `prevent_close` 了，
        所以这里「继续运行」只是把弹窗收掉 —— 不需要再通知后端。
      */}
      {closeReq && !shuttingDown && (
        <div className="modal-backdrop">
          <div className="modal">
            <h2>还有任务在跑</h2>
            <p>{closeReq.hint}</p>
            <p className="modal-note">
              流水线状态：{closeReq.pipeline_state ?? "未知"}。
              「后台继续（最小化到托盘）」属阶段 3 待办，尚未实现 ——
              所以此刻只有两个选择：保持窗口，或者退出。
            </p>
            <div className="modal-actions">
              <button onClick={() => setCloseReq(null)}>继续运行</button>
              <button
                className="danger"
                onClick={() => {
                  setShuttingDown(true);
                  api.confirmClose().catch((e) => {
                    setShuttingDown(false);
                    pushLog("error", `退出失败：${e}`);
                  });
                }}
              >
                退出并停止任务
              </button>
            </div>
          </div>
        </div>
      )}

      {/*
        收尾期间必须留个提示。Rust 侧要等 worker 真正退出才退应用（上限 30 s），
        这段时间窗口是活的、但什么都不会发生 —— 没有这个遮罩，用户会以为程序卡死
        而用任务管理器强杀，那恰好会截断我们正要保护的那次 `state.db` 写入。
      */}
      {shuttingDown && (
        <div className="modal-backdrop">
          <div className="modal">
            <h2>正在退出…</h2>
            <p>已请 worker 收尾，上限 30 秒。请勿强制结束进程。</p>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
