// 设置页版式预览（**仅开发用，不进产物**）。
//
// 为什么需要它：设置弹窗要连上 Python worker 才能拿到 `get_config`，而改版
// 最需要判断的恰恰是「排版长什么样」。没有这层，改完只能盲改 —— 上一版
// 「九段平铺」的问题就是这样积累出来的（没人一次性看见过全貌）。
//
// 用法：
//   npx vite --port 1430
//   http://localhost:1430/preview.html            # 六个分类全部竖排
//   http://localhost:1430/preview.html?only=model # 只看一个
// 数据是写死的假值，覆盖「全就绪 + 已配置云端」这一种状态；要验空态请改这里。
//
// `preview.html` 不在 Vite build 的入口里（默认只有 index.html），
// 所以不会被 `./dev.sh release` 打进 MSI。

import React from "react";
// 预览必须带上全局样式，否则 `--accent` / `.chip` / `button.primary` 全缺失，
// 看到的是一张「像但不对」的排版（第一次截图就是这么被骗的）。
import "./App.css";
import ReactDOM from "react-dom/client";

import { SettingsDialog } from "./components/SettingsDialog";
import type { AppConfig, ConfigInfo } from "./worker/types";

const config: AppConfig = {
  paths: {
    work_dir: "D:\\Vidigest\\workspace",
    work_dir_confirmed: true,
    model_dir: "D:\\Vidigest\\models",
    ffmpeg: null,
    yt_dlp: null,
    cookies_file: null,
  },
  runtime: {
    device: "auto",
    compute_type: "auto",
    model: "large-v3",
    auto_concurrency: true,
    max_download: 4,
    max_frame: 2,
    max_summarize: 3,
  },
  transcribe: {
    language: "zh",
    beam_size: 5,
    vad_filter: true,
    vad_min_silence_ms: 400,
    initial_prompt: "",
  },
  frames: { enabled: true, scene_threshold: 0.3, max_frames: 12, sheet_cols: 4, sheet_rows: 6 },
  summarize: {
    enabled: true,
    base_url: "https://api.deepseek.com/v1",
    api_key_env: "VIDIGEST_API_KEY",
    model: "deepseek-chat",
    template: "default.md",
  },
  local_ai: {
    enabled: true,
    server_path: "D:\\Vidigest\\models\\qwen3-vl-2b\\llama\\llama-server.exe",
    model_dir: null,
    port: 8765,
    gpu_layers: 99,
    startup_timeout: 60,
    reuse_running: true,
  },
  vision: { enabled: true, max_sheets: 2, max_tokens: 1024 },
  download: { rate_limit_per_min: 30, keep_intermediate: false, base_url: null },
};

const info: ConfigInfo = {
  path: "C:\\Users\\Ben\\AppData\\Local\\Vidigest\\config.yaml",
  is_default: false,
  config,
  languages: [
    { code: "", label: "自动检测" },
    { code: "zh", label: "中文" },
    { code: "en", label: "英语" },
    { code: "ja", label: "日语" },
  ],
  model_path: "D:\\Vidigest\\models\\large-v3",
  model_present: true,
  model_missing_files: [],
  model_size_bytes: 3112345678,
  read_only_fields: [],
  work_dir_exists: true,
  api_key_env: "VIDIGEST_API_KEY",
  api_key_present: true,
  ai: {
    local_enabled: true,
    local_ready: true,
    local_server: "D:\\Vidigest\\models\\qwen3-vl-2b\\llama\\llama-server.exe",
    local_model: "D:\\Vidigest\\models\\qwen3-vl-2b\\Qwen3-VL-2B-Instruct-Q4_K_M.gguf",
    local_mmproj: "D:\\Vidigest\\models\\qwen3-vl-2b\\mmproj-BF16.gguf",
    local_model_dir: "D:\\Vidigest\\models\\qwen3-vl-2b",
    local_running: false,
    cloud_key_present: true,
    active: "local",
  },
  pipeline_state: "idle",
  download: { running: false, pack: null },
  packs: [
    {
      key: "local_ai",
      title: "本地视觉模型",
      target_dir: "D:\\Vidigest\\models\\qwen3-vl-2b",
      file_count: 4,
      approx_bytes: 2350000000,
      approx_human: "2.2 GiB",
      present: true,
    },
  ],
};

const GROUPS = ["model", "transcribe", "vision", "summarize", "storage", "maintain"];
const params = new URLSearchParams(location.search);
const only = params.get("only");
/** `?dirty=1` 改一个数字，用来验「未保存」状态（导航圆点 / 底部状态 / 保存按钮） */
const forceDirty = params.get("dirty") === "1";
const shown = only ? GROUPS.filter((g) => g === only) : GROUPS;

const noop = () => {};

function Cell({ group }: { group: string }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const index = GROUPS.indexOf(group);

  // 点一次这个分类的导航项 —— 弹窗的分类是内部 state，没有初始值 prop
  // （不为了预览去污染生产组件的接口）。
  //
  // ⚠️ 必须**重试**：首帧 `draft` 还是 null，弹窗渲染的是「正在读取配置…」，
  // 导航项根本不存在，点空。第一版没重试，于是六张截图全是「模型」，白验一轮。
  React.useEffect(() => {
    let tries = 0;
    const tick = () => {
      const items = ref.current?.querySelectorAll<HTMLButtonElement>(".st-nav-item");
      if (items && items.length > index) {
        items[index].click();
        return;
      }
      if (tries++ < 40) setTimeout(tick, 50);
    };
    tick();
  }, [index]);

  // 受控输入必须走原生 setter + input 事件，直接改 .value React 收不到
  React.useEffect(() => {
    if (!forceDirty) return;
    const timer = setTimeout(() => {
      const el = ref.current?.querySelector<HTMLInputElement>('input[type="number"]');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      if (!el || !setter) return;
      setter.call(el, String(Number(el.value) + 1));
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }, 600);
    return () => clearTimeout(timer);
  }, []);

  return (
    <div className="pv-cell" ref={ref} data-group={group}>
      <SettingsDialog
        info={info}
        download={null}
        pipeline="idle"
        applying={false}
        warnings={[]}
        workerState="ready"
        verifying={false}
        onClose={noop}
        onSave={noop}
        onReload={noop}
        onDownload={noop}
        onDownloadLocalAI={noop}
        onCancelDownload={noop}
        onVerify={noop}
        onClearData={noop}
        douyinLoginState={null}
        onDouyinLogin={noop}
      />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <div className="pv-list">
      {shown.map((g) => (
        <Cell key={g} group={g} />
      ))}
    </div>
  </React.StrictMode>,
);
