// 任务列表。
//
// ⚠️ 2026-10-27 从「8 列表格」改成「两行式列表」，原因是**布局本身是错的**，
// 不是样式旧：
//
//   `table-layout: fixed` + 七个固定宽度列（48+72+92+132+72+190+168 = 774px）
//   在 800px 的默认窗口里把 `width: auto` 的标题列压到**只剩 1 个字**。
//   实测截图（`docs/images/主界面改版-2026-10-27/改版前-正常.png`）里标题显示为
//   「用」「三」「拆」「为」—— 用户看不到自己加的是什么视频。列再多再齐也没用。
//
// 所以改成：标题独占第一行（全宽），状态/阶段/进度/耗时退成第二行的元信息。
// 列对齐让位给「标题可读」，这是任务队列这类界面的正确取舍。
//
// 保留的行为（不要顺手删）：
// - 右键菜单，**与行内按钮共用同一批回调**（`contextItems()` 不另写一套语义）
// - `progress < 0` 是「不可知」，渲染不确定态而不是 0%（规格 §5）
// - 行内按钮仍然保留 —— 右键是快捷方式，不是替代品（触控板用户要用）

import { useEffect, useState } from "react";

import {
  formatDuration,
  formatProgress,
  SETTLED_STATUSES,
  STATUS_META,
  type TaskSnapshot,
  type TaskStatus,
} from "../worker/types";
import { STAGE_NAMES, stageFill, stageView } from "./mainLayout";

interface Props {
  tasks: TaskSnapshot[];
  onCancel: (id: number) => void;
  onRetry: (id: number) => void;
  onRemove: (id: number) => void;
  onOpenDir: (path: string) => void;
  /** 打开 `<out_dir>/transcript.txt`。文件名由 Python 侧决定（`pipeline.py`）。 */
  onOpenTranscript: (outDir: string) => void;
  /** 空队列时点「添加链接」—— 由 `App` 转到输入框 */
  onAddClick?: () => void;
}

/**
 * 四段阶段指示。用「走到第几段」代替「8 个同色的状态词」：
 * 改版前 `下载中 / 待抽音频 / 转写中 / 待抽帧 / 抽帧中 / 待汇总 / 汇总中`
 * 七个状态全是同一个蓝色圆片，扫过去分不出谁在跑、谁在等。
 */
function StageDots({ fill }: { fill: ("done" | "active" | "todo")[] }) {
  return (
    <span className="stagedots" title={STAGE_NAMES.map((n, i) => `${i + 1} ${n}`).join(" → ")}>
      {fill.map((f, i) => (
        <span key={i} className={`stagedot is-${f}`} />
      ))}
    </span>
  );
}

/**
 * 进度单元。**`progress < 0` 是「不可知」，渲染不确定态而不是 0%**（规格 §5）。
 *
 * 百分比文字放在条**外面**：改版前是白字压在条上，42% 时条只盖住左半边，
 * 文字大部分落在灰底上 ⇒ 白字灰底几乎看不见（截图里只剩下一个孤零零的「%」）。
 */
function Progress({ progress, status }: { progress: number; status: TaskStatus }) {
  const text = formatProgress(progress);
  const isActive = !SETTLED_STATUSES.has(status);

  if (text === null) {
    if (!isActive) return <span className="prog-empty">--</span>;
    return (
      <span className="prog">
        <span className="prog-track" title="进度不可知">
          <span className="prog-bar is-indeterminate" />
        </span>
        <span className="prog-num">…</span>
      </span>
    );
  }

  return (
    <span className="prog" title={text}>
      <span className="prog-track">
        <span
          className={`prog-bar${status === "done" ? " is-done" : ""}`}
          style={{ width: text }}
        />
      </span>
      <span className="prog-num">{text === "100%" ? "100%" : text}</span>
    </span>
  );
}

function rowActions(t: TaskSnapshot, p: Props) {
  const settled = SETTLED_STATUSES.has(t.status);
  return (
    <div className="row-actions">
      {t.status === "failed" && (
        <button className="mini" onClick={() => p.onRetry(t.task_id)} title="重新入队">
          重试
        </button>
      )}
      {!settled && (
        <button
          className="mini"
          onClick={() => p.onCancel(t.task_id)}
          title="协作式取消：转写会在当前 segment 结束后中断"
        >
          取消
        </button>
      )}
      {t.status === "pending" && (
        <button className="mini danger" onClick={() => p.onRemove(t.task_id)} title="从队列移除">
          移除
        </button>
      )}
      {t.out_dir && (
        <>
          <button className="mini" onClick={() => p.onOpenTranscript(t.out_dir)} title="打开 transcript.txt">
            转写稿
          </button>
          <button
            className="iconbtn iconbtn-sm"
            onClick={() => p.onOpenDir(t.out_dir)}
            title={`打开输出目录\n${t.out_dir}`}
            aria-label="打开输出目录"
          >
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
              <path
                d="M1.6 4.4c0-.66.54-1.2 1.2-1.2h2.7l1.2 1.3h5.5c.66 0 1.2.54 1.2 1.2v5.9c0 .66-.54 1.2-1.2 1.2H2.8c-.66 0-1.2-.54-1.2-1.2V4.4Z"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.2"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </>
      )}
    </div>
  );
}

/** 右键菜单项。**与行内按钮共用同一批回调** —— 不要在这里另写一套语义。 */
function contextItems(t: TaskSnapshot, p: Props): { label: string; run: () => void; danger?: boolean }[] {
  const settled = SETTLED_STATUSES.has(t.status);
  const items: { label: string; run: () => void; danger?: boolean }[] = [];
  if (t.out_dir) {
    items.push({ label: "打开输出目录", run: () => p.onOpenDir(t.out_dir) });
    items.push({ label: "查看转写稿（transcript.txt）", run: () => p.onOpenTranscript(t.out_dir) });
  }
  if (t.status === "failed") {
    items.push({ label: "重试", run: () => p.onRetry(t.task_id) });
  }
  if (!settled) {
    items.push({ label: "取消（协作式）", run: () => p.onCancel(t.task_id) });
  }
  if (t.status === "pending") {
    items.push({ label: "从队列移除", run: () => p.onRemove(t.task_id), danger: true });
  }
  return items;
}

/** 菜单估算尺寸，用于贴边翻转。宁可估大一点：估小会让菜单被裁掉一截。 */
const MENU_W = 240;
const MENU_H = 200;

function ContextMenu({
  x,
  y,
  task,
  props,
  onClose,
}: {
  x: number;
  y: number;
  task: TaskSnapshot;
  props: Props;
  onClose: () => void;
}) {
  const items = contextItems(task, props);
  if (items.length === 0) return null;
  return (
    <div
      className="ctxmenu"
      style={{
        left: Math.min(x, window.innerWidth - MENU_W),
        top: Math.min(y, window.innerHeight - MENU_H),
      }}
      // 菜单上再右键不该把菜单关掉又开一个
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((it) => (
        <button
          key={it.label}
          className={it.danger ? "ctxitem danger" : "ctxitem"}
          onClick={() => {
            it.run();
            onClose();
          }}
        >
          {it.label}
        </button>
      ))}
    </div>
  );
}

function EmptyState({ onAddClick }: { onAddClick?: () => void }) {
  return (
    <div className="emptystate">
      <div className="emptystate-icon" aria-hidden="true">
        <svg viewBox="0 0 48 48" width="44" height="44">
          <rect x="6" y="10" width="36" height="28" rx="5" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M22 19.5v9M25 21.8l3.6 2.1M25 26.2l3.6-2.1" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none" />
          <path d="M20 19.5v9" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <path d="M12 33h24" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" opacity=".45" />
        </svg>
      </div>
      <div className="emptystate-title">还没有任务</div>
      <p className="emptystate-desc">
        直接粘贴抖音 / B 站 / YouTube 的链接或整段分享文案，一次可以贴多条。
        <br />
        每个视频会产出转写稿、关键帧画面理解和一份汇总。
      </p>
      {onAddClick && (
        <button className="primary" onClick={onAddClick}>
          添加链接
        </button>
      )}
      <p className="emptystate-foot">也可以把本地视频文件路径贴进去</p>
    </div>
  );
}

export function TaskTable(props: Props) {
  const { tasks } = props;
  const [menu, setMenu] = useState<{ x: number; y: number; task: TaskSnapshot } | null>(null);

  // 菜单的关闭时机。**右键监听必须走捕获阶段**（`capture: true`）：
  // 否则「在另一行上右键」会先开新菜单、再被这个冒泡监听关掉，
  // 现象是「右键别的行没反应」。捕获阶段先关、再让 React 的 `onContextMenu` 开。
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("contextmenu", close, true);
    document.addEventListener("click", close);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("contextmenu", close, true);
      document.removeEventListener("click", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [menu]);

  if (tasks.length === 0) return <EmptyState onAddClick={props.onAddClick} />;

  return (
    <>
      <div className="tasklist">
        {tasks.map((t) => {
          const meta = STATUS_META[t.status] ?? { label: t.status, tone: "muted" as const };
          const fill = stageFill(t.status);
          const { running } = stageView(t.status);
          const settled = SETTLED_STATUSES.has(t.status);
          return (
            <div
              key={t.task_id}
              className={`taskrow row-${meta.tone}${running ? " is-running" : ""}`}
              onContextMenu={(e) => {
                if (contextItems(t, props).length === 0) return; // 没有可用动作就别弹空菜单
                e.preventDefault();
                setMenu({ x: e.clientX, y: e.clientY, task: t });
              }}
            >
              <div className="taskrow-head">
                <span className={`chip chip-${meta.tone}`} title={t.stage}>
                  {meta.label}
                </span>
                <span className="taskrow-title" title={`${t.title || t.url}\n\n${t.url}`}>
                  {t.title || <span className="muted">{t.url}</span>}
                </span>
                {rowActions(t, props)}
              </div>

              <div className="taskrow-meta">
                {fill ? (
                  <>
                    <StageDots fill={fill} />
                    <span className="taskrow-stage">{t.stage || meta.label}</span>
                  </>
                ) : (
                  <span className="taskrow-stage">{t.stage || meta.label}</span>
                )}

                {/* `pending` 一条都还没开始，渲染 0% 的空条只会让人以为「卡在 0」。
                    直接不给进度位，与「已完成」不给阶段点同理。 */}
                {t.status !== "pending" && <Progress progress={t.progress} status={t.status} />}

                <span className="taskrow-time" title="已耗时 / 视频时长">
                  {settled && t.status === "done"
                    ? formatDuration(t.elapsed_sec)
                    : `${formatDuration(t.elapsed_sec)} / ${formatDuration(t.duration_sec)}`}
                </span>

                {t.error ? (
                  <span className="taskrow-error" title={t.error}>
                    {t.error}
                  </span>
                ) : t.retry_count > 0 ? (
                  <span className="taskrow-note">重试 {t.retry_count} 次</span>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>

      {/* 菜单放在滚动容器之外：列表是滚动容器，菜单不该受它的裁剪影响 */}
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          task={menu.task}
          props={props}
          onClose={() => setMenu(null)}
        />
      )}
    </>
  );
}
