// 日志区。转发 Python 的 `logging`（经 stdin/stdout 协议）与 worker stderr 尾部。
//
// 2026-10-27 版式改版：默认**收起成一行**，点开才占 200px。
// 原因：改版前它固定 168px 常驻，在 800×600 的默认窗口里吃掉近三成高度，
// 而多数时候用户不读日志 —— 训练用户「无视一大块区域」比不给这一块更糟。
// 收起时仍然显示条数与错误数，且出错不会自动展开（自动展开会打断正在看表的人）。

import { useEffect, useRef, useState } from "react";

export interface LogEntry {
  key: number;
  level: "debug" | "info" | "warning" | "error";
  msg: string;
  /** `stderr` 表示来自 Python 的直接 stderr 输出（带 traceback 的那种） */
  source?: string;
}

interface Props {
  entries: LogEntry[];
  onClear: () => void;
}

const LEVEL_ORDER: Record<string, number> = { debug: 0, info: 1, warning: 2, error: 3 };

export function LogPanel({ entries, onClear }: Props) {
  const boxRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  /** 默认收起；有 error 时仍然收起，但标题上带红点 */
  const [open, setOpen] = useState(false);

  const errors = entries.filter((e) => e.level === "error").length;
  const warns = entries.filter((e) => e.level === "warning").length;

  // 只在用户本来就贴着底部时才自动滚动 —— 否则他往上翻查日志会被不断拽回去
  useEffect(() => {
    const el = boxRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [entries, open]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  return (
    <div className={`logpanel${open ? " is-open" : ""}`}>
      <div
        className="logpanel-head"
        onClick={() => setOpen((v) => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen((v) => !v);
          }
        }}
        title={open ? "收起日志" : "展开日志"}
      >
        <span className={`logpanel-caret${open ? " is-open" : ""}`} aria-hidden="true">
          ▸
        </span>
        <span className="logpanel-title">日志</span>
        <span className="logpanel-count">{entries.length} 条</span>
        {errors > 0 && <span className="chip chip-error logpanel-flag">{errors} 错误</span>}
        {errors === 0 && warns > 0 && (
          <span className="chip chip-warn logpanel-flag">{warns} 警告</span>
        )}
        <span className="spacer" />
        {/* 清空不该顺手把面板也折叠了 —— 点它只清空 */}
        <button
          className="mini"
          onClick={(e) => {
            e.stopPropagation();
            onClear();
          }}
        >
          清空
        </button>
      </div>

      {open && (
        <div className="logpanel-body" ref={boxRef} onScroll={onScroll}>
          {entries.length === 0 ? (
            <div className="muted">暂无日志</div>
          ) : (
            entries.map((e) => (
              <div key={e.key} className={`logline log-${e.level}`}>
                <span className="log-level">{e.level}</span>
                {e.source === "stderr" && <span className="log-src">stderr</span>}
                <span className="log-msg">{e.msg}</span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export function levelRank(level: string): number {
  return LEVEL_ORDER[level] ?? 1;
}
