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
//
// 版式（2026-10-27 改版）：**左侧分类 + 右侧卡片**，替代原来的「九段平铺、
// 一条长滚动」。理由见 `app-ui/src/components/settings/settings.css` 文首。
// 分类按「用户想修什么」分，不按配置文件的分节分：配置里 `runtime.transcribe.
// frames` 是三个 dict，用户眼里只是「转写」和「画面」两件事。

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type {
  AppConfig,
  ConfigInfo,
  DownloadState,
  PipelineState,
} from "../worker/types";
import { ModelPanel } from "./ModelPanel";
import { DownloadNote, DownloadProgress, isDownloadActive } from "./DownloadProgress";
import {
  Callout,
  CardStack,
  Disclosure,
  Field,
  FieldGrid,
  KeyValue,
  NavList,
  NumberField,
  NumberInput,
  SectionCard,
  SelectField,
  TextField,
  Toggle,
  type NavItem,
} from "./settings/primitives";
import "./settings/settings.css";

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
  /** 下载本地视觉模型（llama-server + Qwen3-VL 权重，约 2.3 GiB） */
  onDownloadLocalAI: () => void;
  onCancelDownload: () => void;
  onVerify: () => void;
  onClearData: (scope: "history" | "workspace" | "all") => void;
  /** 抖音登录状态文案（由 `douyin_login_state` 事件驱动） */
  douyinLoginState?: { state: string; message: string } | null;
  onDouyinLogin: () => void;
}

/** 文案用中文标签，值仍是后端枚举 —— 不要为了好看改值。 */
const DEVICES = [
  { value: "auto", label: "自动（推荐）" },
  { value: "cuda", label: "GPU · CUDA" },
  { value: "cpu", label: "CPU（慢约 6 倍）" },
];

const COMPUTE_TYPES = [
  { value: "auto", label: "自动" },
  { value: "float16", label: "float16 · 半精度" },
  { value: "float32", label: "float32 · 单精度" },
  { value: "int8", label: "int8 · 8 位整型" },
  { value: "int8_float16", label: "int8_float16 · 混合" },
];

const EXE_FILTER = [{ name: "可执行文件", extensions: ["exe", "bat", "cmd"] }];

/** 分类。`keys` 决定「这一组有没有未保存的改动」，与配置分节是多对多。 */
const GROUPS = [
  { key: "model", label: "模型", icon: "model", keys: ["local_ai"] },
  { key: "transcribe", label: "转写", icon: "transcribe", keys: ["runtime", "transcribe"] },
  { key: "vision", label: "画面", icon: "image", keys: ["frames", "vision"] },
  { key: "summarize", label: "汇总", icon: "summarize", keys: ["summarize"] },
  { key: "storage", label: "存储", icon: "storage", keys: ["paths", "download"] },
  { key: "maintain", label: "维护", icon: "wrench", keys: [] },
] as const satisfies readonly { key: string; label: string; icon: NavItem["icon"]; keys: readonly (keyof AppConfig)[] }[];

type GroupKey = (typeof GROUPS)[number]["key"];

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
  onDownloadLocalAI,
  onCancelDownload,
  onVerify,
  onClearData,
  douyinLoginState,
  onDouyinLogin,
}: Props) {
  const [draft, setDraft] = useState<AppConfig | null>(null);
  const [baselineUnlocked, setBaselineUnlocked] = useState(false);
  const [confirmBaseline, setConfirmBaseline] = useState(false);
  const [group, setGroup] = useState<GroupKey>("model");
  const bodyRef = useRef<HTMLDivElement>(null);

  // `info` 每次 `get_config` / `config_saved` 都会换新对象 ⇒ 草稿跟着重置。
  // 这保证「保存后看到的确实是我刚保存的值」，而不是上一版的残留。
  useEffect(() => {
    if (info) setDraft(JSON.parse(JSON.stringify(info.config)) as AppConfig);
  }, [info]);

  // 切分类回到顶部。不重置的话，从「存储」滚到底再点「模型」会落在半空中。
  useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
  }, [group]);

  const busy = pipeline !== "idle";
  const readOnly = busy || applying;

  // 同一时刻只可能有一个包在下（后端保证），按 `pack` 分给两个面板显示 ——
  // 不分开的话，下视觉模型时「语音模型」那一栏也会开始转进度条。
  const whisperDownload = download && (download.pack ?? "whisper") === "whisper" ? download : null;
  const localAIDownload = download?.pack === "local_ai" ? download : null;
  const localAIPack = info?.packs?.find((p) => p.key === "local_ai") ?? null;

  // llama-server 路径的四种状态：填了且有效 / 填了但失效 / 留空但已自动识别 / 完全没找到。
  // 一定要显式画出来 —— 「留空 = 自动查找」这个约定如果只存在于占位符里，
  // 用户下完 2.3 GiB 回来看到一个空输入框，唯一合理的结论就是「没配置成功」。
  const serverPath = (draft?.local_ai.server_path ?? "").trim();
  const resolvedServer = info?.ai.local_server ?? null;
  const serverPathState: "explicit-ok" | "explicit-bad" | "auto" | "missing" = serverPath
    ? resolvedServer === serverPath
      ? "explicit-ok"
      : "explicit-bad"
    : resolvedServer
      ? "auto"
      : "missing";

  const patch = useMemo(() => (draft ? normalize(draft) : null), [draft]);

  /** 哪些配置分节被改过 —— 分类的「未保存」圆点靠它，不另算一遍。 */
  const dirtyKeys = useMemo(() => {
    const out = new Set<keyof AppConfig>();
    if (!patch || !info) return out;
    const base = normalize(info.config);
    for (const k of Object.keys(patch) as (keyof AppConfig)[]) {
      if (JSON.stringify(patch[k]) !== JSON.stringify(base[k])) out.add(k);
    }
    return out;
  }, [patch, info]);

  const dirty = dirtyKeys.size > 0;

  const baselineChanged = !!patch && !!info && (
    patch.transcribe.beam_size !== info.config.transcribe.beam_size ||
    patch.transcribe.vad_min_silence_ms !== info.config.transcribe.vad_min_silence_ms
  );

  const emptyRequired = !patch || !patch.paths.work_dir || !patch.paths.model_dir;

  // 关闭前确认。原来点 × / 按 Esc 会**静默丢掉**整页改动 —— 用户改了三处、
  // 顺手点关闭，回来的是一脸茫然。只在真的有改动时问。
  const requestClose = useCallback(() => {
    if (dirty && !window.confirm("有未保存的修改，关闭会丢弃。确定关闭？")) return;
    onClose();
  }, [dirty, onClose]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      requestClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [requestClose]);

  if (!info || !draft) {
    // worker 没起来时 `get_config` 根本没有响应 —— 不能只写「正在读取配置…」，
    // 那会让用户以为卡住了。如实说状态，并给一个重试入口。
    return (
      <div className="modal-backdrop">
        <div className="modal">
          <h2>设置</h2>
          <p className="muted">
            正在读取配置…（worker 状态：{workerState ?? "未知"}）
          </p>
          <p className="st-hint">
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
  const setLocalAI = <K extends keyof AppConfig["local_ai"]>(
    k: K,
    v: AppConfig["local_ai"][K],
  ) => setDraft((d) => (d ? { ...d, local_ai: { ...d.local_ai, [k]: v } } : d));
  const setVision = <K extends keyof AppConfig["vision"]>(k: K, v: AppConfig["vision"][K]) =>
    setDraft((d) => (d ? { ...d, vision: { ...d.vision, [k]: v } } : d));
  const setDownload = <K extends keyof AppConfig["download"]>(
    k: K,
    v: AppConfig["download"][K],
  ) => setDraft((d) => (d ? { ...d, download: { ...d.download, [k]: v } } : d));

  const navItems: NavItem[] = GROUPS.map((g) => ({
    key: g.key,
    label: g.label,
    icon: g.icon,
    dirty: g.keys.some((k) => dirtyKeys.has(k)),
    attention: g.key === "model" && !info.model_present,
  }));

  const langOptions = (info.languages ?? [{ code: "", label: "自动检测" }]).map((l) => ({
    value: l.code,
    label: l.label,
  }));

  /* ------------------------------------------------------------- 各分类 */

  const renderModel = () => (
    <CardStack>
      <ModelPanel
        info={info}
        download={whisperDownload}
        readOnly={readOnly}
        verifying={verifying}
        onDownload={onDownload}
        onCancelDownload={onCancelDownload}
        onVerify={onVerify}
        onReload={onReload}
      />

      <SectionCard
        title="本地视觉模型"
        desc="llama.cpp 运行包 + Qwen3-VL-2B 权重，让「画面理解」能在本机跑，不产生 API 费用。"
        aside={
          info.ai.local_ready ? (
            <span className="chip chip-ok">已就位</span>
          ) : (
            <span className="chip chip-warn">未安装</span>
          )
        }
      >
        <Toggle
          checked={draft.local_ai.enabled}
          onChange={(v) => setLocalAI("enabled", v)}
          disabled={readOnly}
          label="启用本地推理"
          hint="优先于云端，起不来会自动回退，不会让任务失败"
        />

        <div className="st-kv">
          <span className="st-kv-k">状态</span>
          <div className="st-kv-v">
            {info.ai.local_ready ? (
              <span className="chip chip-ok">服务与权重已就位</span>
            ) : (
              <span className="chip chip-warn">缺少 llama-server 或模型权重</span>
            )}
            {info.ai.local_running && <span className="chip chip-ok">服务运行中</span>}
            <span className="muted">约 {localAIPack?.approx_human || "2.3 GiB"}，解压后另需约 0.7 GiB</span>
          </div>
        </div>

        <DownloadProgress download={localAIDownload} />
        <DownloadNote download={localAIDownload} />

        <div className="st-row">
          {isDownloadActive(localAIDownload) ? (
            <button
              className="danger"
              onClick={onCancelDownload}
              disabled={localAIDownload?.phase === "verifying"}
            >
              取消下载
            </button>
          ) : (
            <button
              className="primary"
              onClick={onDownloadLocalAI}
              disabled={readOnly || (isDownloadActive(download) && download?.pack !== "local_ai")}
              title={
                isDownloadActive(download) && download?.pack !== "local_ai"
                  ? "另一个模型正在下载，等它完成或先取消"
                  : "从 GitHub / hf-mirror 下载运行包与权重，支持断点续传"
              }
            >
              {localAIPack?.present ? "重新下载 / 补全" : "下载本地视觉模型"}
            </button>
          )}
        </div>

        <TextField
          label="llama-server 路径"
          value={draft.local_ai.server_path ?? ""}
          onChange={(v) => setLocalAI("server_path", v || null)}
          disabled={readOnly}
          mono
          placeholder={resolvedServer ? "已自动识别，无需填写" : "留空自动查找"}
          hint={
            serverPathState === "auto" ? (
              <>
                <span className="chip chip-ok">已自动识别</span> <code>{resolvedServer}</code>{" "}
                —— 下载完成后自动填入，不需要手填。
              </>
            ) : serverPathState === "explicit-ok" ? (
              <>
                <span className="chip chip-ok">路径有效</span>，会直接用这一个。
              </>
            ) : serverPathState === "explicit-bad" ? (
              <>
                <span className="chip chip-warn">填写的路径不可用</span>
                {resolvedServer ? (
                  <>
                    {" "}已回退到自动查找：<code>{resolvedServer}</code>。
                    建议点「浏览…」重新指定，或清空这一栏恢复自动识别。
                  </>
                ) : (
                  " 也没能自动找到 —— 点上面的「下载本地视觉模型」把它取回来。"
                )}
              </>
            ) : (
              <>
                <span className="chip chip-warn">未找到 llama-server</span>{" "}
                点上面的「下载本地视觉模型」会一次取齐运行包与权重；已经下过则确认这一栏与
                「视觉模型目录」没有被改乱。
              </>
            )
          }
          browse={{ kind: "file", title: "选择 llama-server", filters: EXE_FILTER }}
        />

        <TextField
          label="视觉模型目录"
          value={draft.local_ai.model_dir ?? ""}
          onChange={(v) => setLocalAI("model_dir", v || null)}
          disabled={readOnly}
          mono
          placeholder="留空使用默认位置"
          hint={<>留空则用 <code>{info.ai.local_model_dir || "<模型目录>/qwen3-vl-2b"}</code></>}
          browse={{ kind: "dir", title: "选择视觉模型目录" }}
          browseStart={draft.local_ai.model_dir ?? draft.paths.model_dir}
        />

        <FieldGrid cols={2}>
          <NumberField
            label="服务端口"
            value={draft.local_ai.port}
            onChange={(v) => setLocalAI("port", v)}
            disabled={readOnly}
            min={1024}
            max={65535}
            hint="固定占一个本地端口，冲突时会报出占用方"
          />
          <NumberField
            label="GPU 层数"
            value={draft.local_ai.gpu_layers}
            onChange={(v) => setLocalAI("gpu_layers", v)}
            disabled={readOnly}
            width={110}
            hint="99 = 全部卸载到显卡"
          />
        </FieldGrid>

        <Disclosure summary="下载的内容与来源">
          <p>
            一次取齐四样：llama.cpp 运行包（含 33 个 CUDA DLL）、CUDA 运行库、
            Qwen3-VL-2B 的 GGUF 权重、视觉投影器 mmproj。落点{" "}
            <code>{localAIPack?.target_dir ?? info.ai.local_model_dir}</code>。
          </p>
          <p>
            <strong>不进安装包</strong>，安装体积不因此变大；断点续传靠 <code>.part</code> 文件，
            取消或断网后重新点下载会接着下。运行包钉死 b11909 + cuda-13.4，
            需要 NVIDIA 驱动 ≥ 580 —— 只支持到 CUDA 12 的机器会在这里失败，
            此时把「启用本地推理」关掉即可，云端兜底不受影响。
          </p>
        </Disclosure>
      </SectionCard>
    </CardStack>
  );

  const renderTranscribe = () => (
    <CardStack>
      <SectionCard
        title="设备与并发"
        desc="实际并发由「硬件探测结果」与下面的上限共同决定，上限不是实际值。"
      >
        <FieldGrid cols={3}>
          <SelectField
            label="设备"
            value={draft.runtime.device}
            onChange={(v) => setRuntime("device", v)}
            options={DEVICES}
            disabled={readOnly}
          />
          <SelectField
            label="计算精度"
            value={draft.runtime.compute_type}
            onChange={(v) => setRuntime("compute_type", v)}
            options={COMPUTE_TYPES}
            disabled={readOnly}
          />
          <TextField
            label="模型名"
            value={draft.runtime.model}
            onChange={(v) => setRuntime("model", v)}
            disabled={readOnly}
            mono
          />
        </FieldGrid>

        <Toggle
          checked={draft.runtime.auto_concurrency}
          onChange={(v) => setRuntime("auto_concurrency", v)}
          disabled={readOnly}
          label="自动规划并发"
          hint="关闭后按下面的上限取最小值"
        />

        <FieldGrid cols={3}>
          <NumberField
            label="下载并发上限"
            value={draft.runtime.max_download}
            onChange={(v) => setRuntime("max_download", v)}
            disabled={readOnly}
            min={1}
            width={100}
          />
          <NumberField
            label="抽帧并发上限"
            value={draft.runtime.max_frame}
            onChange={(v) => setRuntime("max_frame", v)}
            disabled={readOnly}
            min={1}
            width={100}
          />
          <NumberField
            label="汇总并发上限"
            value={draft.runtime.max_summarize}
            onChange={(v) => setRuntime("max_summarize", v)}
            disabled={readOnly}
            min={1}
            width={100}
          />
        </FieldGrid>
      </SectionCard>

      <SectionCard title="识别" desc="只影响转写稿本身，不影响汇总。">
        <SelectField
          label="转写语言"
          value={draft.transcribe.language}
          onChange={(v) => setTranscribe("language", v)}
          options={langOptions}
          disabled={readOnly}
          hint="视频里说的话，不是你想要的输出语言。选项由后端下发。"
        />

        <TextField
          label="初始提示词"
          value={draft.transcribe.initial_prompt}
          onChange={(v) => setTranscribe("initial_prompt", v)}
          disabled={readOnly}
          placeholder="例如：人名、产品名、专业术语"
          hint="可写专有名词，帮助模型断句与拼写。"
        />

        <Toggle
          checked={draft.transcribe.vad_filter}
          onChange={(v) => setTranscribe("vad_filter", v)}
          disabled={readOnly}
          label="VAD 过滤静音"
          hint="按语音活动检测切掉无声段，通常会更快也更干净"
        />
      </SectionCard>

      <SectionCard
        title="分段参数（高级）"
        desc="决定分段边界。改了它，阶段 1 的 662 段金标准基线就不再成立 —— 那不是 bug，是参数变了。"
        aside={
          baselineUnlocked ? (
            <span className="chip chip-warn">已解锁</span>
          ) : (
            <span className="chip chip-muted">锁定</span>
          )
        }
      >
        <Toggle
          checked={baselineUnlocked}
          onChange={(v) => {
            setBaselineUnlocked(v);
            if (!v) setConfirmBaseline(false);
          }}
          disabled={readOnly}
          label="解锁这两个参数"
          hint="默认值 5 / 400 ms 是金标准基线的产生条件"
        />

        <FieldGrid cols={2}>
          <NumberField
            label="beam_size"
            value={draft.transcribe.beam_size}
            onChange={(v) => setTranscribe("beam_size", v)}
            disabled={readOnly || !baselineUnlocked}
            min={1}
            width={110}
          />
          <NumberField
            label="vad_min_silence_ms"
            value={draft.transcribe.vad_min_silence_ms}
            onChange={(v) => setTranscribe("vad_min_silence_ms", v)}
            disabled={readOnly || !baselineUnlocked}
            min={0}
            width={110}
          />
        </FieldGrid>

        {baselineUnlocked && (
          <Callout tone="warn">
            保存时会二次确认。新参数要作为新的金标准重新存档，否则后续的逐字符比对
            会一直失败在同一个位置。
          </Callout>
        )}
      </SectionCard>
    </CardStack>
  );

  const renderVision = () => (
    <CardStack>
      <SectionCard
        title="抽帧"
        desc="按场景切换抓关键帧并拼成版图。默认关闭 —— 多数口播视频画面没有独立信息量。"
      >
        <Toggle
          checked={draft.frames.enabled}
          onChange={(v) => setFrames("enabled", v)}
          disabled={readOnly}
          label="启用抽帧"
        />

        <FieldGrid cols={3}>
          <NumberField
            label="场景阈值"
            value={draft.frames.scene_threshold}
            onChange={(v) => setFrames("scene_threshold", v)}
            disabled={readOnly}
            min={0}
            max={1}
            step={0.05}
            width={100}
            hint="0–1，越大越挑剔"
          />
          <NumberField
            label="最大帧数"
            value={draft.frames.max_frames}
            onChange={(v) => setFrames("max_frames", v)}
            disabled={readOnly}
            min={1}
            width={100}
          />
          <Field label="拼版列 × 行" hint="每张拼版图由这么多画面组成">
            <div className="st-num-pair">
              <NumberInput
                ariaLabel="拼版列数"
                value={draft.frames.sheet_cols}
                onChange={(v) => setFrames("sheet_cols", v)}
                disabled={readOnly}
                min={1}
                width={72}
              />
              <span className="st-num-pair-sep">×</span>
              <NumberInput
                ariaLabel="拼版行数"
                value={draft.frames.sheet_rows}
                onChange={(v) => setFrames("sheet_rows", v)}
                disabled={readOnly}
                min={1}
                width={72}
              />
            </div>
          </Field>
        </FieldGrid>
      </SectionCard>

      <SectionCard
        title="画面理解"
        desc="让视觉模型读拼版图，产出 visual.md 并作为补充材料送进汇总。"
        aside={
          draft.vision.enabled ? (
            <span className="chip chip-ok">已启用</span>
          ) : (
            <span className="chip chip-muted">未启用</span>
          )
        }
      >
        <Toggle
          checked={draft.vision.enabled}
          onChange={(v) => setVision("enabled", v)}
          disabled={readOnly}
          label="启用画面理解"
          hint={
            draft.frames.enabled
              ? "需要本地视觉模型已就位（见「模型」）"
              : "需要先开「抽帧」才有图 —— 上面那个开关现在是关的"
          }
        />

        <FieldGrid cols={2}>
          <NumberField
            label="最多看几张拼版图"
            value={draft.vision.max_sheets}
            onChange={(v) => setVision("max_sheets", v)}
            disabled={readOnly}
            min={1}
            width={100}
            hint="每张含「列 × 行」个画面"
          />
          <NumberField
            label="单张生成上限（token）"
            value={draft.vision.max_tokens}
            onChange={(v) => setVision("max_tokens", v)}
            disabled={readOnly}
            width={130}
            hint="太小会截断 OCR 结果"
          />
        </FieldGrid>

        <p className="st-hint">
          本机实测约 1.8 秒/张（RTX 4060 Ti / Q4_K_M）。画面描述作为补充材料送进汇总 ——
          于是总结里能出现转写稿没有、但画面上写着的数字。
        </p>
      </SectionCard>
    </CardStack>
  );

  const renderSummarize = () => (
    <CardStack>
      <SectionCard
        title="当前生效的后端"
        desc="本地优先，起不来才走云端；两级都不可用时汇总会被跳过，并在日志里说明缺什么。"
      >
        <div className="st-chain">
          <div
            className={`st-chain-step${info.ai.active === "local" ? " is-active" : ""}${
              info.ai.active !== "local" && !info.ai.local_ready ? " is-missing" : ""
            }`}
          >
            <div className="st-chain-k">
              本地模型
              {info.ai.active === "local" && <span className="chip chip-ok">使用中</span>}
            </div>
            <div className="st-chain-v">
              {info.ai.local_ready
                ? info.ai.local_running
                  ? "服务已在运行"
                  : "权重已就位，首次汇总时自动拉起（约数秒）"
                : "未下载或路径失效"}
            </div>
          </div>
          <span className="st-chain-arrow">→</span>
          <div
            className={`st-chain-step${info.ai.active === "cloud" ? " is-active" : ""}${
              info.ai.active !== "cloud" && !info.api_key_present ? " is-missing" : ""
            }`}
          >
            <div className="st-chain-k">
              云接口（兜底）
              {info.ai.active === "cloud" && <span className="chip chip-ok">使用中</span>}
            </div>
            <div className="st-chain-v">
              {info.api_key_present
                ? `已检测到 ${info.api_key_env}`
                : `未检测到 ${info.api_key_env}`}
            </div>
          </div>
        </div>

        {info.ai.active === "none" && (
          <Callout tone="warn">
            现在两级都不通。要么在「模型」里下载本地视觉模型并打开开关，要么给云端配好
            <code>{info.api_key_env}</code> 环境变量 —— 否则汇总阶段会跳过，只产出转写稿。
          </Callout>
        )}

        <Toggle
          checked={draft.summarize.enabled}
          onChange={(v) => setSummarize("enabled", v)}
          disabled={readOnly}
          label="启用汇总"
          hint="关掉后仍会产出转写稿与抽帧，只是不写 summary.md"
        />
      </SectionCard>

      <SectionCard
        title="云端接口（兜底）"
        desc="本地模型不可用时的备选。密钥只从环境变量读取，不写进配置文件。"
        aside={
          info.api_key_present ? (
            <span className="chip chip-ok">已检测到 {info.api_key_env}</span>
          ) : (
            <span className="chip chip-muted">未检测到 {info.api_key_env}</span>
          )
        }
      >
        <TextField
          label="接口地址"
          value={draft.summarize.base_url}
          onChange={(v) => setSummarize("base_url", v)}
          disabled={readOnly}
          mono
          placeholder="https://api.example.com/v1"
          hint="OpenAI 兼容的 /v1/chat/completions 端点"
        />

        <FieldGrid cols={2}>
          <TextField
            label="模型"
            value={draft.summarize.model}
            onChange={(v) => setSummarize("model", v)}
            disabled={readOnly}
            mono
          />
          <TextField
            label="密钥环境变量名"
            value={draft.summarize.api_key_env}
            onChange={(v) => setSummarize("api_key_env", v)}
            disabled={readOnly}
            mono
            hint="存的是变量名，不是密钥本身"
          />
        </FieldGrid>

        <TextField
          label="汇总模板"
          value={draft.summarize.template}
          onChange={(v) => setSummarize("template", v)}
          disabled={readOnly}
          mono
          hint="模板文件名，决定 summary.md 的章节结构"
        />

        <Disclosure summary="为什么没有密钥输入框？">
          <p>
            密钥只从环境变量读，本页不提供输入框 —— 这是刻意的，避免密钥随配置文件被
            误提交或误备份。要换密钥，改系统环境变量后重启应用。
          </p>
          <p>
            同理，把密钥写进配置文件的请求会被后端直接忽略（返回
            <code> E_API_KEY_NOT_ALLOWED</code> 警告），不会静默接受。
          </p>
        </Disclosure>
      </SectionCard>
    </CardStack>
  );

  const renderStorage = () => (
    <CardStack>
      <SectionCard
        title="目录"
        desc="产物落工作目录；模型在模型父目录下，语音约 2.9 GiB、视觉约 2.0 GiB。"
      >
        <TextField
          label="工作目录"
          value={draft.paths.work_dir}
          onChange={(v) => setPaths("work_dir", v)}
          disabled={readOnly}
          mono
          hint="产物落这里；改完不会迁移已有产物。"
          browse={{ kind: "dir", title: "选择工作目录" }}
        />
        <TextField
          label="模型父目录"
          value={draft.paths.model_dir}
          onChange={(v) => setPaths("model_dir", v)}
          disabled={readOnly}
          mono
          hint={
            <>
              实际路径是它下面的 <code>{draft.runtime.model}</code>。
            </>
          }
          browse={{ kind: "dir", title: "选择模型目录" }}
        />
      </SectionCard>

      <SectionCard
        title="外部工具"
        desc="留空 = 自动定位（会先查 PATH，再查常见安装位置）。"
      >
        <TextField
          label="ffmpeg"
          value={draft.paths.ffmpeg ?? ""}
          onChange={(v) => setPaths("ffmpeg", v)}
          disabled={readOnly}
          mono
          placeholder="留空 = 自动定位"
          browse={{ kind: "file", title: "选择 ffmpeg", filters: EXE_FILTER }}
          browseStart={draft.paths.ffmpeg ?? undefined}
        />
        <TextField
          label="yt-dlp"
          value={draft.paths.yt_dlp ?? ""}
          onChange={(v) => setPaths("yt_dlp", v)}
          disabled={readOnly}
          mono
          placeholder="留空 = 自动定位"
          browse={{ kind: "file", title: "选择 yt-dlp", filters: EXE_FILTER }}
          browseStart={draft.paths.yt_dlp ?? undefined}
        />
        <TextField
          label="cookies.txt"
          value={draft.paths.cookies_file ?? ""}
          onChange={(v) => setPaths("cookies_file", v)}
          disabled={readOnly}
          mono
          placeholder="留空 = 不带 cookie 下载"
          hint="Netscape 格式。抖音的单视频不需要它，「列作者全部作品」用得上。"
          browse={{
            kind: "file",
            title: "选择 cookies.txt",
            filters: [{ name: "Netscape cookies", extensions: ["txt"] }],
          }}
          browseStart={draft.paths.cookies_file ?? undefined}
        />
      </SectionCard>

      <SectionCard title="下载行为">
        <NumberField
          label="每分钟请求上限"
          value={draft.download.rate_limit_per_min}
          onChange={(v) => setDownload("rate_limit_per_min", v)}
          disabled={readOnly}
          min={1}
          width={110}
          hint="对站点限速，调太高容易被临时封。"
        />
        <TextField
          label="模型下载镜像"
          value={draft.download.base_url ?? ""}
          onChange={(v) => setDownload("base_url", v)}
          disabled={readOnly}
          mono
          placeholder="留空 = 默认 hf-mirror"
          hint={
            <>
              留空 = 默认 hf-mirror；<code>{"{model}"}</code> 会被替换成模型名。
            </>
          }
        />
        <Toggle
          checked={draft.download.keep_intermediate}
          onChange={(v) => setDownload("keep_intermediate", v)}
          disabled={readOnly}
          label="保留中间产物（音频 / 视频）"
          hint="排查问题时要留；日常可以关掉省磁盘"
        />
      </SectionCard>
    </CardStack>
  );

  const renderMaintain = () => (
    <CardStack>
      <SectionCard
        title="抖音登录"
        desc="单个视频不需要登录；「列作者全部作品」登录后才能拿到更多。"
        aside={
          douyinLoginState ? (
            <span className="chip chip-muted">{douyinLoginState.message}</span>
          ) : undefined
        }
      >
        <div className="st-row">
          <button type="button" onClick={onDouyinLogin} disabled={readOnly}>
            打开登录窗口
          </button>
          {douyinLoginState && <span className="st-hint">{douyinLoginState.message}</span>}
        </div>
        <Disclosure summary="登录态存在哪？会不会串号？">
          <p>
            单视频不需要登录也能下（用系统自带的 Edge 打开页面取流）。但「列作者全部作品」
            只给首屏，登录后能拿到更多。登录态存在本机，只需一次。
          </p>
          <p>
            注意：这是应用自己的浏览器窗口，与你平时用的 Edge/Chrome
            <strong>不共享登录态</strong>。
          </p>
        </Disclosure>
      </SectionCard>

      <SectionCard title="清理数据" desc="两项都不会删模型（模型重下最贵）。">
        <div className="st-row">
          <button
            type="button"
            disabled={readOnly}
            onClick={() => {
              if (window.confirm("清空全部任务历史？产出文件不会被删。")) {
                onClearData("history");
              }
            }}
          >
            清空任务历史
          </button>
          <button
            type="button"
            className="danger"
            disabled={readOnly}
            onClick={() => {
              if (
                window.confirm(
                  "清空工作目录里的全部文件？\n\n产出（视频 / 音频 / 截图 / 转写稿）会被删除，模型不受影响。",
                )
              ) {
                onClearData("workspace");
              }
            }}
          >
            清空工作目录
          </button>
        </div>
        <p className="st-hint">
          卸载也只清配置与任务历史，<code>workspace</code> 与 <code>models</code> 都会保留，
          重装后直接沿用。
        </p>
      </SectionCard>

      <SectionCard title="配置文件">
        <KeyValue k="位置">
          <code>{info.path}</code>
        </KeyValue>
        {info.is_default && (
          <p className="st-hint">当前是首次生成的默认配置，保存一次后转为用户配置。</p>
        )}
        <p className="st-hint">
          保存会用 <code>yaml.safe_dump</code> 重写整个文件 ——
          <strong>手写的注释不会保留</strong>。这是现状，不是本次改动引入的。
        </p>
      </SectionCard>
    </CardStack>
  );

  const panels: Record<GroupKey, () => ReactNode> = {
    model: renderModel,
    transcribe: renderTranscribe,
    vision: renderVision,
    summarize: renderSummarize,
    storage: renderStorage,
    maintain: renderMaintain,
  };

  const footText = emptyRequired
    ? "工作目录与模型目录不能为空"
    : confirmBaseline
      ? "分段参数已改，再点一次「保存」确认"
      : dirty
        ? "有未保存的修改"
        : "没有改动";

  const footTone = emptyRequired ? "is-error" : dirty ? "is-dirty" : "";

  return (
    <div className="modal-backdrop">
      <div className="modal st-modal" role="dialog" aria-modal="true" aria-labelledby="st-title">
        <div className="st-shell">
          <NavList items={navItems} active={group} onSelect={(k) => setGroup(k as GroupKey)} />

          <div className="st-main">
            <header className="st-head">
              <div className="st-head-text">
                <h2 id="st-title">设置</h2>
                <p className="st-head-sub" title={info.path}>
                  配置文件 {info.path}
                </p>
              </div>
              <button
                type="button"
                className="st-close"
                onClick={requestClose}
                title="关闭（不保存）"
                aria-label="关闭"
              >
                ×
              </button>
            </header>

            <div className="st-status-slot">
              {busy && (
                <Callout tone="warn">
                  有任务在运行（流水线 {pipeline}），设置页暂时只读 ——
                  保存配置会重启 worker，那会中断正在跑的任务。先「暂停」并等当前阶段结束再改。
                </Callout>
              )}
              {applying && (
                <Callout tone="info">已保存，正在重启 worker 以应用新配置（约 2–3 秒）。</Callout>
              )}
              {warnings.length > 0 && (
                <Callout tone="warn">
                  {warnings.map((w) => WARNING_TEXT[w] ?? w).join("；")}
                </Callout>
              )}
            </div>

            <div className="st-body" ref={bodyRef}>
              {panels[group]()}
            </div>

            <footer className="st-foot">
              <span className={`st-foot-status ${footTone}`}>
                <span className="st-foot-dot" />
                {footText}
              </span>
              <button
                type="button"
                onClick={() => {
                  setDraft(JSON.parse(JSON.stringify(info.config)) as AppConfig);
                  setConfirmBaseline(false);
                  setBaselineUnlocked(false);
                }}
                disabled={!dirty}
                title="放弃未保存的修改，回到磁盘上的值"
              >
                还原
              </button>
              <button type="button" onClick={requestClose}>
                关闭
              </button>
              <button
                type="button"
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
            </footer>
          </div>
        </div>
      </div>
    </div>
  );
}

