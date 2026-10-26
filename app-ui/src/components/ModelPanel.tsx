// 模型面板：模型状态 + 下载 / 取消 / 重新校验。
//
// 单独拆出来（而不是塞进 SettingsDialog）有两个原因：
// 1. 它是**唯一会长时间运行的**设置项，状态机比其它字段复杂得多；
// 2. 「模型未安装」这个入口会从状态栏直接点进来，将来也可能单独复用。
//
// 版式由 `SectionCard` 提供（标题 + 一句说明 + 右侧状态 chip），
// 内容用 `KeyValue` 排成两行事实 —— 不再用「模型-目录 / 模型-状态」这种
// 前缀灰字，那在长路径下会把值挤成第二行且对不齐。

import { formatBytes, type ConfigInfo, type DownloadState } from "../worker/types";
import { DownloadNote, DownloadProgress, isDownloadActive } from "./DownloadProgress";
import { Disclosure, KeyValue, SectionCard } from "./settings/primitives";

interface Props {
  info: ConfigInfo | null;
  /** 只传**语音模型**的下载状态：同一时刻只可能有一个包在下，按 pack 过滤过 */
  download: DownloadState | null;
  /** 有任务在跑时只读（改配置会重启 worker = 中断任务，D-S1） */
  readOnly: boolean;
  verifying: boolean;
  onDownload: () => void;
  onCancelDownload: () => void;
  onVerify: () => void;
  onReload: () => void;
}

export function ModelPanel({
  info,
  download,
  readOnly,
  verifying,
  onDownload,
  onCancelDownload,
  onVerify,
  onReload,
}: Props) {
  const running = isDownloadActive(download);
  const missing = info?.model_missing_files ?? [];

  return (
    <SectionCard
      title="语音转写模型"
      desc="转写阶段的唯一硬依赖。装好之前，任务会停在「准备模型」这一步。"
      aside={
        info?.model_present ? (
          <span className="chip chip-ok">已就绪</span>
        ) : (
          <span className="chip chip-warn">未安装</span>
        )
      }
    >
      <KeyValue k="目录">
        <code>{info?.model_path ?? "（未加载）"}</code>
      </KeyValue>
      <KeyValue k="体积">
        {info && info.model_size_bytes > 0 ? (
          <span className="muted">{formatBytes(info.model_size_bytes)}</span>
        ) : (
          <span className="muted">—</span>
        )}
      </KeyValue>
      {info?.model_present === false && missing.length > 0 && (
        <KeyValue k="缺失">
          <span className="st-err-text">{missing.join("、")}</span>
        </KeyValue>
      )}

      {/* 下载进度。总量未知时（服务端没给 Content-Length）不画百分比，
          而不是画一个永远 0% 的条 —— 后者看起来像卡住了。 */}
      <DownloadProgress download={download} />

      <DownloadNote download={download} />

      <div className="st-row">
        {running ? (
          <button
            className="danger"
            onClick={onCancelDownload}
            disabled={download?.phase === "verifying"}
          >
            取消下载
          </button>
        ) : (
          <button
            className="primary"
            onClick={onDownload}
            disabled={readOnly || verifying}
            title={
              readOnly
                ? "有任务在运行，先暂停并等当前阶段结束"
                : "从 hf-mirror 下载 5 个文件（约 2.9 GiB），支持断点续传"
            }
          >
            {info?.model_present ? "重新下载 / 补全" : "下载模型（约 2.9 GiB）"}
          </button>
        )}
        <button onClick={onVerify} disabled={verifying || running || !info?.model_present}>
          {verifying ? "校验中…" : "重新校验 GPU"}
        </button>
        <button onClick={onReload} title="重新读取配置文件与模型状态">
          刷新状态
        </button>
      </div>

      <Disclosure summary="下载源与断点续传">
        <p>
          下载源：hf-mirror（可在「存储 → 下载行为」改镜像地址）。下载完成后会自动做一次
          真实推理校验，确认模型真能跑起来，而不只是文件齐了。
        </p>
        <p>
          断点续传靠 <code>.part</code> 文件，取消或断网后重新点下载会接着下。
        </p>
      </Disclosure>
    </SectionCard>
  );
}
