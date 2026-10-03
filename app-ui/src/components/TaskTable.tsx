// 任务表格。列顺序与 `PLAN §7.2` 的布局图一致：
// 序号 │ 标题 │ 时长 │ 状态 │ 进度 │ 耗时 │ 备注
//
// 行上支持右键菜单（`PLAN §7.2` 第 2 条：打开输出目录 / 查看转写稿 / 重试 / 删除）。
// 行内的按钮保留不变 —— 右键是「同一批动作的快捷方式」，不是替代品：
// 触控板用户与不熟悉右键的用户仍然要能用。

import { useEffect, useState } from "react";

import {
  formatDuration,
  formatProgress,
  SETTLED_STATUSES,
  STATUS_META,
  type TaskSnapshot,
  type TaskStatus,
} from "../worker/types";

interface Props {
  tasks: TaskSnapshot[];
  onCancel: (id: number) => void;
  onRetry: (id: number) => void;
  onRemove: (id: number) => void;
  onOpenDir: (path: string) => void;
  /** 打开 `<out_dir>/transcript.txt`。文件名由 Python 侧决定（`pipeline.py`）。 */
  onOpenTranscript: (outDir: string) => void;
}

/** 进度单元。**`progress < 0` 是「不可知」，渲染不确定态而不是 0%**（规格 §5）。 */
function ProgressCell({ progress, status }: { progress: number; status: TaskStatus }) {
  const text = formatProgress(progress);
  const isActive = !SETTLED_STATUSES.has(status);
  if (text === null) {
    return isActive ? (
      <div className="progress indeterminate" title="进度不可知">
        <div className="bar" />
      </div>
    ) : (
      <span className="muted">--</span>
    );
  }
  return (
    <div className="progress" title={text}>
      <div
        className="bar"
        style={{
          width: text,
          background: status === "done" ? "var(--ok)" : "var(--accent)",
        }}
      />
      <span className="progress-text">{text}</span>
    </div>
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
        <button className="mini" onClick={() => p.onOpenDir(t.out_dir)} title={t.out_dir}>
          目录
        </button>
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

  if (tasks.length === 0) {
    return (
      <div className="empty">
        <p>还没有任务。</p>
        <p className="muted">
          点左上角「添加链接」，可直接粘贴抖音分享文案（含干扰码也能抽出 URL），一次多条。
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="table-wrap">
        <table className="tasks">
          <thead>
            <tr>
              <th className="col-id">#</th>
              <th className="col-title">标题</th>
              <th className="col-dur">时长</th>
              <th className="col-status">状态</th>
              <th className="col-progress">进度</th>
              <th className="col-elapsed">耗时</th>
              <th className="col-note">备注</th>
              <th className="col-actions" />
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => {
              const meta = STATUS_META[t.status] ?? { label: t.status, tone: "muted" as const };
              return (
                <tr
                  key={t.task_id}
                  className={`row-${meta.tone}`}
                  onContextMenu={(e) => {
                    if (contextItems(t, props).length === 0) return; // 没有可用动作就别弹空菜单
                    e.preventDefault();
                    setMenu({ x: e.clientX, y: e.clientY, task: t });
                  }}
                >
                  <td className="col-id">{t.task_id}</td>
                  <td className="col-title" title={t.title || t.url}>
                    {t.title || <span className="muted">{t.url}</span>}
                  </td>
                  <td className="col-dur">{formatDuration(t.duration_sec)}</td>
                  <td className="col-status">
                    <span className={`chip chip-${meta.tone}`} title={t.stage}>
                      {meta.label}
                    </span>
                  </td>
                  <td className="col-progress">
                    <ProgressCell progress={t.progress} status={t.status} />
                  </td>
                  <td className="col-elapsed">{formatDuration(t.elapsed_sec)}</td>
                  <td className="col-note" title={t.error ?? ""}>
                    {t.error ? (
                      <span className="err-text">{t.error}</span>
                    ) : t.retry_count > 0 ? (
                      <span className="muted">重试 {t.retry_count} 次</span>
                    ) : null}
                  </td>
                  <td className="col-actions">{rowActions(t, props)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* 菜单放在 `.table-wrap` 之外：表是滚动容器，菜单不该受它的裁剪影响 */}
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
