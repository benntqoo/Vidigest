// 顶部工具栏 + 状态区。
//
// 从 `App.tsx` 搬出来的（2026-10-27 版式改版）：原来它内联在 `return` 里，
// 导致「布局」和「事件映射」混在同一个 600 行文件里，也没法在预览页里单独渲染。
//
// 版式上做了三件事：
// 1. **状态并入顶栏**：改版前是「工具栏 + 状态栏」两条横条，在 800×600 的默认
//    窗口里吃掉 13% 的高度，而两条里有一半信息是重复的（「worker 已退出」
//    同时出现在两个 chip 里）。现在只留一条。
// 2. **开始 / 暂停 / 继续 合成一个主操作位**：这三个按钮任何时刻只有一个可点，
//    并排放着等于让用户先做一次「哪个是亮的」的判断题。
// 3. **异常单独一条 alert**：blocking 的事（worker 不在了）不该和「GPU 可用」
//    并列成同一种小圆片。
//
// ⚠️ 按钮的可用性判据必须留在调用处（`App.tsx` 算 `canRestart` / `busy`）：
// 那些判据依赖 worker 状态的三态语义（`gpu_verified === null` 不是 false），
// 搬进展示组件后很容易被后人简化成 falsy 判断。

import { AddTasksBar } from "./AddTasksBar";
import { concurrencyText, connView, gpuView, PIPELINE_LABELS } from "./mainLayout";
import type { PipelineState, WorkerState } from "../worker/types";

export interface TopBarProps {
  worker: WorkerState | null;
  /** worker 已 ready，能下命令 */
  busy: boolean;
  pipeline: PipelineState;
  /** worker 起不来 / 崩了 —— 只有这两种情况才给「重启」 */
  canRestart: boolean;
  /** block 住的条数（失败），用于 alert 文案 */
  failedCount: number;
  /** 输入区是否展开。受控 —— 空队列的「添加链接」按钮也要能打开同一个输入框 */
  composerOpen: boolean;
  onComposerOpenChange: (v: boolean) => void;
  onAddTasks: (text: string) => void;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onRestart: () => void;
  onOpenSettings: () => void;
}

/** 一个带小圆点的状态 chip。可点时用 `<button>`（键盘可达、有焦点框）。 */
function StatusChip({
  chip,
  onClick,
}: {
  chip: ReturnType<typeof gpuView>;
  onClick?: () => void;
}) {
  const cls = `chip chip-${chip.tone}${chip.action ? " chip-action" : ""}`;
  if (chip.action && onClick) {
    return (
      <button type="button" className={cls} title={chip.title} onClick={onClick}>
        <span className="dot" />
        {chip.text}
      </button>
    );
  }
  return (
    <span className={cls} title={chip.title}>
      <span className="dot" />
      {chip.text}
    </span>
  );
}

export function TopBar({
  worker,
  busy,
  pipeline,
  canRestart,
  failedCount,
  composerOpen,
  onComposerOpenChange,
  onAddTasks,
  onStart,
  onPause,
  onResume,
  onRestart,
  onOpenSettings,
}: TopBarProps) {
  const running = pipeline === "running";
  const paused = pipeline === "paused";
  const conn = connView(worker);
  const gpu = gpuView(worker);

  // blocking：worker 不在了 —— 此刻所有按钮都是灰的，唯一能做的是重来一遍
  const blocking = worker != null && (worker.state === "failed" || worker.state === "exited");
  // 不阻塞但会卡住流水线：模型没装（能下载、但转写跑不起来）
  const modelMissing = !blocking && gpu.action === "settings" && gpu.text === "模型未安装";

  const detailLine = (worker?.detail ?? "").split("\n")[0];

  return (
    <header className="topbar">
      <div className={`topbar-row${composerOpen ? " is-expanded" : ""}`}>
        <span className="brand">Vidigest</span>

        <AddTasksBar
          disabled={!busy}
          onSubmit={onAddTasks}
          open={composerOpen}
          onOpenChange={onComposerOpenChange}
        />

        {/*
          主操作位。`aborted` / `stopped` 都回到「开始」（可以重新跑），
          `idle` 也是「开始」—— 用户不需要知道这四者的区别。
        */}
        {paused ? (
          <button className="primary" disabled={!busy} onClick={onResume} title="继续跑未完成的任务">
            继续
          </button>
        ) : running ? (
          <button className="btn-pause" disabled={!busy} onClick={onPause} title="已在跑的任务会跑完当前阶段为止">
            <span className="pause-glyph" aria-hidden="true" />
            暂停
          </button>
        ) : (
          <button
            className="primary"
            disabled={!busy}
            onClick={onStart}
            title="只入队未完成且非失败的任务（failed / cancelled 需在该行点「重试」）"
          >
            开始
          </button>
        )}

        <span className="spacer" />

        <div className="statuschips" title={[concurrencyText(worker), `流水线 ${PIPELINE_LABELS[pipeline] ?? pipeline}`].filter(Boolean).join("\n")}>
          <StatusChip chip={conn} />
          <StatusChip chip={gpu} onClick={onOpenSettings} />
        </div>

        <button className="iconbtn" onClick={onOpenSettings} title="设置">
          <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
            <path
              d="M8 5.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6Zm0 1.4a1.4 1.4 0 1 1 0 2.8 1.4 1.4 0 0 1 0-2.8Z"
              fill="currentColor"
            />
            <path
              d="M7.1 1.6h1.8l.24 1.42c.36.12.7.28 1 .48l1.3-.6 1.27 1.27-.6 1.3c.2.3.36.64.48 1l1.42.24v1.8l-1.42.24c-.12.36-.28.7-.48 1l.6 1.3-1.27 1.27-1.3-.6c-.3.2-.64.36-1 .48l-.24 1.42H7.1l-.24-1.42c-.36-.12-.7-.28-1-.48l-1.3.6L3.3 12.5l.6-1.3c-.2-.3-.36-.64-.48-1l-1.42-.24V8.16l1.42-.24c.12-.36.28-.7.48-1l-.6-1.3L4.56 4.3l1.3.6c.3-.2.64-.36 1-.48L7.1 1.6Z"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.1"
              strokeLinejoin="round"
              transform="translate(0 .4) scale(.96)"
            />
          </svg>
          设置
        </button>
      </div>

      {/*
        异常条。只放「现在做不了事」和「做下去会出错」两类。
        改版前 worker 挂掉时状态栏是两个红色小圆片（「worker 已退出」+「worker 已退出」），
        原因挤在 right 侧的灰字里 —— 那是用户此刻唯一想知道的东西，却排在最后。
      */}
      {blocking && (
        <div className="alertbar alert-error">
          <span className="alertbar-mark" aria-hidden="true">
            !
          </span>
          <span className="alertbar-text">
            <strong>{worker?.state === "failed" ? "worker 启动失败" : "worker 已退出"}</strong>
            {detailLine && <span className="alertbar-detail">{detailLine}</span>}
          </span>
          {pipeline === "aborted" && <span className="alertbar-tag">流水线已中止</span>}
          {failedCount > 0 && (
            <span className="alertbar-tag">{failedCount} 条任务失败</span>
          )}
          <button className="mini" onClick={onRestart} title="不用关闭应用，直接重来一遍">
            重启 worker
          </button>
        </div>
      )}

      {modelMissing && (
        <div className="alertbar alert-warn">
          <span className="alertbar-mark" aria-hidden="true">
            !
          </span>
          <span className="alertbar-text">
            <strong>语音模型未安装</strong>
            <span className="alertbar-detail">
              没有模型无法转写，添加的任务会停在「下载」阶段之后。
            </span>
          </span>
          <button className="mini" onClick={onOpenSettings}>
            去下载
          </button>
        </div>
      )}

      {/*
        `canRestart` 与 `blocking` 覆盖的情形不同：前者是 App.tsx 用同一批
        状态算的，但语义上只保证「有这个入口」。这里两个都不渲染重启按钮时，
        说明 worker 是好的 —— 不会出现「弹了 alert 却没有按钮」。
      */}
      {canRestart && !blocking && (
        <div className="alertbar alert-warn">
          <span className="alertbar-mark" aria-hidden="true">
            !
          </span>
          <span className="alertbar-text">
            <strong>{conn.text}</strong>
            {detailLine && <span className="alertbar-detail">{detailLine}</span>}
          </span>
          <button className="mini" onClick={onRestart}>
            重启 worker
          </button>
        </div>
      )}
    </header>
  );
}
