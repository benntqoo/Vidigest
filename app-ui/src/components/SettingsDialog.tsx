// 设置模态：全量配置可视化 + 模型面板。
//
// 形态是**模态覆盖层**，不新开窗口 —— 新窗口要额外做跨窗口状态同步
// （worker 状态、事件流、关闭确认），收益不抵成本。
//
// 两条硬约束（`docs/设置界面与模型下载_执行单.md` D-S1 / D-S5）：
// 1. **有任务在跑时整页只读**，并且横幅给出具体原因。因为保存配置会让 Rust
//    重启 worker —— 那会中断正在跑的任务。
// 2. `beam_size` / `vad_min_silence_ms` 默认锁定，解锁要显式勾选，且保存时
//    还要二次确认。它们决定分段边界，改了 `samples/transcript_gpu.txt` 的
//    662 段金标准基线就失效 —— 那不是 bug，是参数变了。

import { useEffect, useMemo, useState } from "react";

import type {
  AppConfig,
  ConfigInfo,
  DownloadState,
  PipelineState,
} from "../worker/types";
import { ModelPanel } from "./ModelPanel";

interface Props {
  info: ConfigInfo | null;
  download: DownloadState | null;
  pipeline: PipelineState;
  /** 已保存、正在等 worker 重启生效 */
  applying: boolean;
  /** 上一次保存的警告（`E_BASELINE_PARAM_CHANGED` 等） */
  warnings: string[];
  /** worker 连接状态，仅用于「读不到配置」时的说明文案 */
  workerState?: string;
  verifying: boolean;
  onClose: () => void;
  onSave: (patch: Record<string, unknown>) => void;
  onReload: () => void;
  onDownload: () => void;
  onCancelDownload: () => void;
  onVerify: () => void;
}

const DEVICES = ["auto", "cuda", "cpu"];
const COMPUTE_TYPES = ["auto", "float16", "float32", "int8", "int8_float16"];

/** 可选路径字段的空串统一成 `null`（Python 侧 `null` = 自动定位）。 */
function clean(v: string | null): string | null {
  return v && v.trim() ? v.trim() : null;
}

function normalize(c: AppConfig): AppConfig {
  return {
    ...c,
    paths: {
      ...c.paths,
      work_dir: c.paths.work_dir.trim(),
      model_dir: c.paths.model_dir.trim(),
      ffmpeg: clean(c.paths.ffmpeg),
      yt_dlp: clean(c.paths.yt_dlp),
      cookies_file: clean(c.paths.cookies_file),
    },
  };
}

const WARNING_TEXT: Record<string, string> = {
  E_API_KEY_NOT_ALLOWED: "密钥不允许写进配置文件（只走环境变量），已忽略",
  E_BASELINE_PARAM_CHANGED: "分段参数已改，阶段 1 的 662 段金标准基线将不再成立",
};

export function SettingsDialog({
  info,
  download,
  pipeline,
  applying,
  warnings,
  workerState,
  verifying,
  onClose,
  onSave,
  onReload,
  onDownload,
  onCancelDownload,
  onVerify,
}: Props) {
  const [draft, setDraft] = useState<AppConfig | null>(null);
  const [baselineUnlocked, setBaselineUnlocked] = useState(false);
  const [confirmBaseline, setConfirmBaseline] = useState(false);

  // `info` 每次 `get_config` / `config_saved` 都会换新对象 ⇒ 草稿跟着重置。
  // 这保证「保存后看到的确实是我刚保存的值」，而不是上一版的残留。
  useEffect(() => {
    if (info) setDraft(JSON.parse(JSON.stringify(info.config)) as AppConfig);
  }, [info]);

  const busy = pipeline !== "idle";
  const readOnly = busy || applying;

  const patch = useMemo(() => (draft ? normalize(draft) : null), [draft]);

  const dirty = useMemo(() => {
    if (!patch || !info) return false;
    return JSON.stringify(patch) !== JSON.stringify(normalize(info.config));
  }, [patch, info]);

  const baselineChanged = !!patch && !!info && (
    patch.transcribe.beam_size !== info.config.transcribe.beam_size ||
    patch.transcribe.vad_min_silence_ms !== info.config.transcribe.vad_min_silence_ms
  );

  const emptyRequired =
    !patch || !patch.paths.work_dir || !patch.paths.model_dir;

  if (!info || !draft) {
    // worker 没起来时 `get_config` 根本没有响应 —— 不能只写「正在读取配置…」，
    // 那会让用户以为卡住了。如实说状态，并给一个重试入口。
    return (
      <div className="modal-backdrop">
        <div className="modal modal-lg">
          <h2>设置</h2>
          <p className="muted">
            正在读取配置…（worker 状态：{workerState ?? "未知"}）
          </p>
          <p className="settings-hint">
            如果长时间停在这里，说明 Python worker 尚未就绪 ——
            先关掉本窗口，看状态栏的 worker 状态与日志区。
          </p>
          <div className="modal-actions">
            <button onClick={onReload}>重新读取</button>
            <button onClick={onClose}>关闭</button>
          </div>
        </div>
      </div>
    );
  }

  const setPaths = <K extends keyof AppConfig["paths"]>(k: K, v: AppConfig["paths"][K]) =>
    setDraft((d) => (d ? { ...d, paths: { ...d.paths, [k]: v } } : d));
  const setRuntime = <K extends keyof AppConfig["runtime"]>(k: K, v: AppConfig["runtime"][K]) =>
    setDraft((d) => (d ? { ...d, runtime: { ...d.runtime, [k]: v } } : d));
  const setTranscribe = <K extends keyof AppConfig["transcribe"]>(
    k: K,
    v: AppConfig["transcribe"][K],
  ) => setDraft((d) => (d ? { ...d, transcribe: { ...d.transcribe, [k]: v } } : d));
  const setFrames = <K extends keyof AppConfig["frames"]>(k: K, v: AppConfig["frames"][K]) =>
    setDraft((d) => (d ? { ...d, frames: { ...d.frames, [k]: v } } : d));
  const setSummarize = <K extends keyof AppConfig["summarize"]>(
    k: K,
    v: AppConfig["summarize"][K],
  ) => setDraft((d) => (d ? { ...d, summarize: { ...d.summarize, [k]: v } } : d));
  const setDownload = <K extends keyof AppConfig["download"]>(
    k: K,
    v: AppConfig["download"][K],
  ) => setDraft((d) => (d ? { ...d, download: { ...d.download, [k]: v } } : d));

  const num = (raw: string): number => {
    const n = Number(raw);
    return Number.isFinite(n) ? n : 0;
  };

  return (
    <div className="modal-backdrop">
      <div className="modal modal-lg">
        <h2>设置</h2>

        {busy && (
          <p className="banner banner-warn">
            有任务在运行（流水线 {pipeline}），设置页暂时只读 ——
            保存配置会重启 worker，那会中断正在跑的任务。先「暂停」并等当前阶段结束再改。
          </p>
        )}
        {applying && (
          <p className="banner banner-info">
            已保存，正在重启 worker 以应用新配置（约 2-3 秒）。
          </p>
        )}
        {warnings.length > 0 && (
          <p className="banner banner-warn">
            {warnings.map((w) => WARNING_TEXT[w] ?? w).join("；")}
          </p>
        )}

        <div className="settings-body">
          <ModelPanel
            info={info}
            download={download}
            readOnly={readOnly}
            verifying={verifying}
            onDownload={onDownload}
            onCancelDownload={onCancelDownload}
            onVerify={onVerify}
            onReload={onReload}
          />

          <section className="settings-section">
            <h3>路径</h3>            <label className="field">
              <span className="field-label">
                工作目录
                <em className="field-hint">产物落这里；改完不会迁移已有产物</em>
              </span>
              <input
                value={draft.paths.work_dir}
                disabled={readOnly}
                onChange={(e) => setPaths("work_dir", e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">
                模型父目录
                <em className="field-hint">实际路径是它下面的 {draft.runtime.model}</em>
              </span>
              <input
                value={draft.paths.model_dir}
                disabled={readOnly}
                onChange={(e) => setPaths("model_dir", e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">
                ffmpeg
                <em className="field-hint">留空 = 自动定位（路径或命令名）</em>
              </span>
              <input
                value={draft.paths.ffmpeg ?? ""}
                disabled={readOnly}
                onChange={(e) => setPaths("ffmpeg", e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">
                yt-dlp
                <em className="field-hint">留空 = 自动定位</em>
              </span>
              <input
                value={draft.paths.yt_dlp ?? ""}
                disabled={readOnly}
                onChange={(e) => setPaths("yt_dlp", e.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">
                cookies.txt
                <em className="field-hint">Netscape 格式；留空 = 不带 cookie 下载</em>
              </span>
              <input
                value={draft.paths.cookies_file ?? ""}
                disabled={readOnly}
                onChange={(e) => setPaths("cookies_file", e.target.value)}
              />
            </label>
          </section>

          <section className="settings-section">
            <h3>运行</h3>
            <div className="field-row">
              <label className="field">
                <span className="field-label">设备</span>
                <select
                  value={draft.runtime.device}
                  disabled={readOnly}
                  onChange={(e) => setRuntime("device", e.target.value)}
                >
                  {DEVICES.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">计算精度</span>
                <select
                  value={draft.runtime.compute_type}
                  disabled={readOnly}
                  onChange={(e) => setRuntime("compute_type", e.target.value)}
                >
                  {COMPUTE_TYPES.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">模型名</span>
                <input
                  value={draft.runtime.model}
                  disabled={readOnly}
                  onChange={(e) => setRuntime("model", e.target.value)}
                />
              </label>
            </div>
            <label className="field field-inline">
              <input
                type="checkbox"
                checked={draft.runtime.auto_concurrency}
                disabled={readOnly}
                onChange={(e) => setRuntime("auto_concurrency", e.target.checked)}
              />
              <span>自动规划并发（关闭后按下面的上限取最小值）</span>
            </label>
            <div className="field-row">
              <label className="field">
                <span className="field-label">下载并发上限</span>
                <input
                  type="number"
                  min={1}
                  value={draft.runtime.max_download}
                  disabled={readOnly}
                  onChange={(e) => setRuntime("max_download", num(e.target.value))}
                />
              </label>
              <label className="field">
                <span className="field-label">抽帧并发上限</span>
                <input
                  type="number"
                  min={1}
                  value={draft.runtime.max_frame}
                  disabled={readOnly}
                  onChange={(e) => setRuntime("max_frame", num(e.target.value))}
                />
              </label>
              <label className="field">
                <span className="field-label">汇总并发上限</span>
                <input
                  type="number"
                  min={1}
                  value={draft.runtime.max_summarize}
                  disabled={readOnly}
                  onChange={(e) => setRuntime("max_summarize", num(e.target.value))}
                />
              </label>
            </div>
            <p className="settings-hint">
              这里的上限与实际并发不是一回事：实际值由「硬件探测 + 上限」共同决定，
              保存后可在状态栏看到。
            </p>
          </section>

          <section className="settings-section">
            <h3>转写</h3>
            <div className="field-row">
              <label className="field">
                <span className="field-label">
                  转写语言
                  <em className="field-hint">视频里说的话，不是你想要的输出语言</em>
                </span>
                {/* 选项来自 `info.languages`（后端下发），不在这里硬编码 ——
                    否则 Python 侧的 100 个语言码与前端两份表必然漂移。 */}
                <select
                  value={draft.transcribe.language}
                  disabled={readOnly}
                  onChange={(e) => setTranscribe("language", e.target.value)}
                >
                  {(info.languages ?? [{ code: "", label: "自动检测" }]).map((l) => (
                    <option key={l.code || "auto"} value={l.code}>
                      {l.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field field-inline">
                <input
                  type="checkbox"
                  checked={draft.transcribe.vad_filter}
                  disabled={readOnly}
                  onChange={(e) => setTranscribe("vad_filter", e.target.checked)}
                />
                <span>VAD 过滤静音</span>
              </label>
            </div>
            <label className="field">
              <span className="field-label">
                初始提示词
                <em className="field-hint">可写专有名词，帮助模型断句</em>
              </span>
              <input
                value={draft.transcribe.initial_prompt}
                disabled={readOnly}
                onChange={(e) => setTranscribe("initial_prompt", e.target.value)}
              />
            </label>

            <div className="baseline-box">
              <label className="field field-inline">
                <input
                  type="checkbox"
                  checked={baselineUnlocked}
                  disabled={readOnly}
                  onChange={(e) => {
                    setBaselineUnlocked(e.target.checked);
                    if (!e.target.checked) setConfirmBaseline(false);
                  }}
                />
                <span>
                  ⚠️ 解锁分段参数（<code>beam_size</code> /{" "}
                  <code>vad_min_silence_ms</code>）
                </span>
              </label>
              <p className="settings-hint">
                这两个参数直接决定分段边界。默认值 5 / 400 ms 是
                <code>samples/transcript_gpu.txt</code>（662 段金标准基线）的产生条件，
                改动会让阶段 1 的逐字符比对失败 —— <strong>那不是 bug，是参数变了</strong>。
              </p>
              <div className="field-row">
                <label className="field">
                  <span className="field-label">beam_size</span>
                  <input
                    type="number"
                    min={1}
                    value={draft.transcribe.beam_size}
                    disabled={readOnly || !baselineUnlocked}
                    onChange={(e) => setTranscribe("beam_size", num(e.target.value))}
                  />
                </label>
                <label className="field">
                  <span className="field-label">vad_min_silence_ms</span>
                  <input
                    type="number"
                    min={0}
                    value={draft.transcribe.vad_min_silence_ms}
                    disabled={readOnly || !baselineUnlocked}
                    onChange={(e) => setTranscribe("vad_min_silence_ms", num(e.target.value))}
                  />
                </label>
              </div>
            </div>
          </section>

          <section className="settings-section">
            <h3>抽帧</h3>
            <label className="field field-inline">
              <input
                type="checkbox"
                checked={draft.frames.enabled}
                disabled={readOnly}
                onChange={(e) => setFrames("enabled", e.target.checked)}
              />
              <span>启用抽帧（默认关闭 —— 多数口播视频画面没有独立信息量）</span>
            </label>
            <div className="field-row">
              <label className="field">
                <span className="field-label">场景阈值</span>
                <input
                  type="number"
                  step="0.05"
                  min={0}
                  max={1}
                  value={draft.frames.scene_threshold}
                  disabled={readOnly}
                  onChange={(e) => setFrames("scene_threshold", num(e.target.value))}
                />
              </label>
              <label className="field">
                <span className="field-label">最大帧数</span>
                <input
                  type="number"
                  min={1}
                  value={draft.frames.max_frames}
                  disabled={readOnly}
                  onChange={(e) => setFrames("max_frames", num(e.target.value))}
                />
              </label>
              <label className="field">
                <span className="field-label">拼版列 × 行</span>
                <div className="field-pair">
                  <input
                    type="number"
                    min={1}
                    value={draft.frames.sheet_cols}
                    disabled={readOnly}
                    onChange={(e) => setFrames("sheet_cols", num(e.target.value))}
                  />
                  <input
                    type="number"
                    min={1}
                    value={draft.frames.sheet_rows}
                    disabled={readOnly}
                    onChange={(e) => setFrames("sheet_rows", num(e.target.value))}
                  />
                </div>
              </label>
            </div>
          </section>

          <section className="settings-section">
            <h3>汇总</h3>
            <label className="field field-inline">
              <input
                type="checkbox"
                checked={draft.summarize.enabled}
                disabled={readOnly}
                onChange={(e) => setSummarize("enabled", e.target.checked)}
              />
              <span>启用 LLM 汇总（走 OpenAI 兼容接口）</span>
            </label>
            <label className="field">
              <span className="field-label">接口地址</span>
              <input
                value={draft.summarize.base_url}
                disabled={readOnly}
                onChange={(e) => setSummarize("base_url", e.target.value)}
              />
            </label>
            <div className="field-row">
              <label className="field">
                <span className="field-label">模型</span>
                <input
                  value={draft.summarize.model}
                  disabled={readOnly}
                  onChange={(e) => setSummarize("model", e.target.value)}
                />
              </label>
              <label className="field">
                <span className="field-label">
                  密钥环境变量名
                  <em className="field-hint">密钥本身不写进配置文件</em>
                </span>
                <input
                  value={draft.summarize.api_key_env}
                  disabled={readOnly}
                  onChange={(e) => setSummarize("api_key_env", e.target.value)}
                />
              </label>
            </div>
            <p className="settings-hint">
              当前环境里{" "}
              {info.api_key_present ? (
                <span className="chip chip-ok">已检测到 {info.api_key_env}</span>
              ) : (
                <span className="chip chip-warn">未检测到 {info.api_key_env}</span>
              )}
              。密钥只从环境变量读取，本页不提供输入框 —— 这是刻意的，
              避免密钥随配置文件被误提交或误备份。
            </p>
            <label className="field">
              <span className="field-label">汇总模板</span>
              <input
                value={draft.summarize.template}
                disabled={readOnly}
                onChange={(e) => setSummarize("template", e.target.value)}
              />
            </label>
          </section>

          <section className="settings-section">
            <h3>下载</h3>
            <div className="field-row">
              <label className="field">
                <span className="field-label">每分钟上限</span>
                <input
                  type="number"
                  min={1}
                  value={draft.download.rate_limit_per_min}
                  disabled={readOnly}
                  onChange={(e) => setDownload("rate_limit_per_min", num(e.target.value))}
                />
              </label>
              <label className="field field-inline">
                <input
                  type="checkbox"
                  checked={draft.download.keep_intermediate}
                  disabled={readOnly}
                  onChange={(e) => setDownload("keep_intermediate", e.target.checked)}
                />
                <span>保留中间产物（音频 / 视频）</span>
              </label>
            </div>
            <label className="field">
              <span className="field-label">
                模型下载镜像
                <em className="field-hint">留空 = 默认 hf-mirror；{`{model}`} 会被替换</em>
              </span>
              <input
                value={draft.download.base_url ?? ""}
                disabled={readOnly}
                onChange={(e) => setDownload("base_url", e.target.value)}
              />
            </label>
          </section>

          <p className="settings-hint">
            配置文件：<code>{info.path}</code>
            {info.is_default && "（当前是首次生成的默认配置，保存后转为用户配置）"}
            <br />
            保存会用 <code>yaml.safe_dump</code> 重写整个文件 ——
            <strong>手写的注释不会保留</strong>。这是现状，不是本次改动引入的。
          </p>
        </div>

        <div className="modal-actions">
          <span className="foot-note">
            {emptyRequired && "工作目录与模型目录不能为空"}
            {!emptyRequired && dirty && !confirmBaseline && "有未保存的修改"}
            {!emptyRequired && !dirty && "没有改动"}
          </span>
          <button onClick={onClose}>关闭</button>
          <button
            className={confirmBaseline ? "danger" : "primary"}
            disabled={readOnly || !dirty || emptyRequired || applying}
            onClick={() => {
              if (!patch) return;
              if (baselineChanged && !confirmBaseline) {
                setConfirmBaseline(true);
                return;
              }
              setConfirmBaseline(false);
              onSave(patch as unknown as Record<string, unknown>);
            }}
            title={
              readOnly
                ? "有任务在运行 / 正在应用配置"
                : baselineChanged && !confirmBaseline
                  ? "分段参数已改，需要再点一次确认"
                  : "保存并重启 worker 生效"
            }
          >
            {baselineChanged && !confirmBaseline ? "保存（需二次确认）" : "保存并生效"}
          </button>
        </div>
      </div>
    </div>
  );
}
