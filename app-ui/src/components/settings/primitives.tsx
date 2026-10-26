// 设置页的版式与表单原语。
//
// 为什么单独一层：改版前 962 行里，每个字段都是 10 行手抄的
// `<label className="field"><span className="field-label">…`，于是
// 「标签字号 / 提示位置 / 数字框宽度」这三件事**每个字段各写一次**，
// 三十多个字段就有三十多个略微不同的版本 —— 这就是「工程 demo 感」的真正来源，
// 不是配色。收敛成原语后，字段是一行声明，视觉纪律由 CSS 一处保证。
//
// 两条约束：
// 1. **只做呈现，不做业务**。取值 / 归一化 / 只读判定都由调用方给，
//    这里不猜「留空是什么意思」—— `server_path` 的 null 语义属于 config。
// 2. **原生控件优先**。开关用 `<input type="checkbox">` 只换皮，不用 div 模拟，
//    这样键盘 Tab / 空格、读屏软件、以及 `disabled` 全部天然成立。

import { useId, type ReactNode } from "react";

import { pickDirectory, pickFile } from "../../worker/pickDir";

/* ------------------------------------------------------------------ 容器 */

/** 一组相关设置。标题 + 一句说明 + 右侧状态位，**不再用 h3 平铺**。 */
export function SectionCard({
  title,
  desc,
  aside,
  children,
}: {
  title?: string;
  desc?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="st-card">
      {(title || desc || aside) && (
        <header className="st-card-head">
          <div className="st-card-heading">
            {title && <h4 className="st-card-title">{title}</h4>}
            {desc && <p className="st-card-desc">{desc}</p>}
          </div>
          {aside && <div className="st-card-aside">{aside}</div>}
        </header>
      )}
      <div className="st-card-body">{children}</div>
    </section>
  );
}

/** 卡片竖向堆叠。用 gap 而不是 `+ .st-card { margin-top }`，后者在条件渲染下会漏。 */
export function CardStack({ children }: { children: ReactNode }) {
  return <div className="st-stack">{children}</div>;
}

/** 字段栅格。`cols` 是「宽屏下几列」，窄屏自动降级（见 settings.css）。 */
export function FieldGrid({ cols = 2, children }: { cols?: 2 | 3; children: ReactNode }) {
  return <div className={`st-grid st-grid-${cols}`}>{children}</div>;
}

/** 提示条。用于「只读」「保存中」「警告」这类**整块状态**，不是字段级说明。 */
export function Callout({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "error" | "ok";
  children: ReactNode;
}) {
  return (
    <div className={`st-callout st-callout-${tone}`} role={tone === "info" ? undefined : "status"}>
      <span className="st-callout-bar" aria-hidden="true" />
      <div className="st-callout-body">{children}</div>
    </div>
  );
}

/** 折叠说明。长解释（为什么这么设计）默认收起 —— 想知道的点开，不想知道的别挡路。 */
export function Disclosure({ summary, children }: { summary: string; children: ReactNode }) {
  return (
    <details className="st-more">
      <summary>{summary}</summary>
      <div className="st-more-body">{children}</div>
    </details>
  );
}

/** 键值行，用于「路径 / 大小 / 状态」这类只读事实。 */
export function KeyValue({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="st-kv">
      <span className="st-kv-k">{k}</span>
      <div className="st-kv-v">{children}</div>
    </div>
  );
}

/* -------------------------------------------------------------- 导航 */

export interface NavItem {
  key: string;
  label: string;
  icon: IconName;
  /** 该组有未保存的改动 */
  dirty?: boolean;
  /** 该组有需要处理的问题（例如模型没下） */
  attention?: boolean;
}

export function NavList({
  items,
  active,
  onSelect,
}: {
  items: NavItem[];
  active: string;
  onSelect: (key: string) => void;
}) {
  return (
    <nav className="st-nav" aria-label="设置分类">
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          className={`st-nav-item${it.key === active ? " is-active" : ""}`}
          aria-current={it.key === active ? "page" : undefined}
          onClick={() => onSelect(it.key)}
        >
          <Icon name={it.icon} />
          <span className="st-nav-label">{it.label}</span>
          {it.dirty && <span className="st-nav-dot st-nav-dot-dirty" title="有未保存的修改" />}
          {!it.dirty && it.attention && (
            <span className="st-nav-dot st-nav-dot-attention" title="这一组还有未完成的准备" />
          )}
        </button>
      ))}
    </nav>
  );
}

/* -------------------------------------------------------------- 字段 */

const NUM_ALIGN = { textAlign: "right" } as const;

/** 不带标签的数字输入。用于「列 × 行」这种一行两格的组合。 */
export function NumberInput({
  value,
  onChange,
  disabled,
  min,
  max,
  step,
  ariaLabel,
  width = 96,
}: {
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  min?: number;
  max?: number;
  step?: number | string;
  ariaLabel: string;
  width?: number;
}) {
  return (
    <input
      type="number"
      className="st-input st-input-num"
      style={{ ...NUM_ALIGN, width }}
      value={Number.isFinite(value) ? value : 0}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      aria-label={ariaLabel}
      onChange={(e) => {
        const n = Number(e.target.value);
        onChange(Number.isFinite(n) ? n : 0);
      }}
    />
  );
}

export interface FieldProps {
  label: string;
  hint?: ReactNode;
  /** 标签右侧的补充说明（例如「可选」「自动」） */
  tag?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
}

export function Field({ label, hint, tag, htmlFor, children }: FieldProps) {
  return (
    <div className="st-field">
      <div className="st-label-row">
        <label className="st-label" htmlFor={htmlFor}>
          {label}
        </label>
        {tag}
      </div>
      {children}
      {hint && <p className="st-hint">{hint}</p>}
    </div>
  );
}

/** 单行文本。`browse` 给了就渲染「浏览…」，并把选中的绝对路径回填。 */
export function TextField({
  label,
  value,
  onChange,
  hint,
  tag,
  placeholder,
  disabled,
  mono,
  browse,
  browseStart,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  hint?: ReactNode;
  tag?: ReactNode;
  placeholder?: string;
  disabled?: boolean;
  mono?: boolean;
  browse?: { kind: "dir" | "file"; title: string; filters?: { name: string; extensions: string[] }[] };
  /** 浏览对话框的默认落点（一般是当前值；留空的可退回上一层目录） */
  browseStart?: string;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} tag={tag} htmlFor={id}>
      <div className="st-control">
        <input
          id={id}
          type="text"
          className={`st-input${mono ? " st-input-mono" : ""}`}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          spellCheck={false}
          onChange={(e) => onChange(e.target.value)}
        />
        {browse && (
          <button
            type="button"
            className="st-browse"
            disabled={disabled}
            title={browse.title}
            onClick={async () => {
              const picked =
                browse.kind === "dir"
                  ? await pickDirectory(browseStart || value || undefined, browse.title)
                  : await pickFile(browseStart || value || undefined, browse.title, browse.filters);
              if (picked) onChange(picked);
            }}
          >
            浏览…
          </button>
        )}
      </div>
    </Field>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  hint,
  tag,
  disabled,
  min,
  max,
  step,
  width = 120,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  hint?: ReactNode;
  tag?: ReactNode;
  disabled?: boolean;
  min?: number;
  max?: number;
  step?: number | string;
  width?: number;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} tag={tag} htmlFor={id}>
      <input
        id={id}
        type="number"
        className="st-input st-input-num"
        style={{ ...NUM_ALIGN, width }}
        value={Number.isFinite(value) ? value : 0}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        onChange={(e) => {
          const n = Number(e.target.value);
          onChange(Number.isFinite(n) ? n : 0);
        }}
      />
    </Field>
  );
}

export function SelectField({
  label,
  value,
  onChange,
  options,
  hint,
  tag,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  hint?: ReactNode;
  tag?: ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <Field label={label} hint={hint} tag={tag} htmlFor={id}>
      <select
        id={id}
        className="st-input st-select"
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </Field>
  );
}

/**
 * 开关。换皮原生 checkbox：`appearance: none` 会连键盘与读屏一起换掉，
 * 所以这里只把 input 变成透明覆盖层，可聚焦、可空格切换，视觉由兄弟节点画。
 */
export function Toggle({
  checked,
  onChange,
  label,
  hint,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className={`st-toggle${disabled ? " is-disabled" : ""}`}>
      <input
        type="checkbox"
        className="st-toggle-input"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="st-toggle-track" aria-hidden="true">
        <span className="st-toggle-thumb" />
      </span>
      <span className="st-toggle-text">
        <span className="st-toggle-label">{label}</span>
        {hint && <span className="st-toggle-hint">{hint}</span>}
      </span>
    </label>
  );
}

/* -------------------------------------------------------------- 图标 */

export type IconName = "model" | "transcribe" | "image" | "summarize" | "storage" | "wrench";

const ICONS: Record<IconName, ReactNode> = {
  model: (
    <>
      <path d="M8 1.9 14 5v6l-6 3.1L2 11V5z" />
      <path d="M2 5l6 3.1L14 5M8 8.1v6" />
    </>
  ),
  transcribe: (
    <>
      <path d="M8 2.4a1.9 1.9 0 0 1 1.9 1.9v3a1.9 1.9 0 0 1-3.8 0v-3A1.9 1.9 0 0 1 8 2.4z" />
      <path d="M4.6 7.4a3.4 3.4 0 0 0 6.8 0M8 10.8v2.6M6.2 13.4h3.6" />
    </>
  ),
  image: (
    <>
      <rect x="2.2" y="3.2" width="11.6" height="9.6" rx="1.6" />
      <path d="M2.6 10.6 6 7.6l2.6 2.2 2-1.6 2.8 2.4" />
      <circle cx="5.6" cy="6" r=".9" />
    </>
  ),
  summarize: (
    <>
      <path d="M8 2.2 9.6 6 13.4 7.6 9.6 9.2 8 13 6.4 9.2 2.6 7.6 6.4 6z" />
    </>
  ),
  storage: (
    <>
      <path d="M2.2 4.4h3.6l1.4 1.9h6.6v7.2H2.2z" />
      <path d="M2.2 8.4h11.6" />
    </>
  ),
  wrench: (
    <>
      <path d="M10.8 2.4a3.4 3.4 0 0 0-3 5L3 12.2l1.4 1.4 4.8-4.8a3.4 3.4 0 0 0 4.3-4.4l-1.9 1.9-1.6-.4-.4-1.6z" />
    </>
  ),
};

export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="st-icon"
      viewBox="0 0 16 16"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.35"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {ICONS[name]}
    </svg>
  );
}
