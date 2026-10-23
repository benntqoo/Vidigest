// 模型面板：模型状态 + 下载 / 取消 / 重新校验。
//
// 单独拆出来（而不是塞进 SettingsDialog）有两个原因：
// 1. 它是**唯一会长时间运行的**设置项，状态机比其它字段复杂得多；
// 2. 「模型未安装」这个入口会从状态栏直接点进来，将来也可能单独复用。

import { formatBytes, type ConfigInfo, type DownloadState } from "../worker/types";

interface Props {
  info: ConfigInfo | null;
  download: DownloadState | null;
  /** 有任务在跑时只读（改配置会重启 worker = 中断任务，D-S1） */
  readOnly: boolean;
  verifying: boolean;
  onDownload: () => void;
  onCancelDownload: () => void;
  onVerify: () => void;
  onReload: () => void;
}

function phaseText(d: DownloadState | null): string {
  if (!d) return "";
  switch (d.phase) {
    case "running":
      return d.totalBytes > 0 ? `下载中 ${Math.round(d.pct * 100)}%` : "下载中…";
    case "verifying":
      return "校验中（用一次真实推理确认能加载）…";
    case "done":
      if (d.verified === true) return `完成，GPU 可用（${d.elapsedSec ?? "?"} s）`;
      if (d.verified === false) return "文件已下完，但真实推理失败 —— 见下方说明";
      return "文件已就绪（未校验）";
    case "failed":
      return `失败（${d.code ?? "E_?"}）`;
    case "cancelled":
      return "已取消";
    default:
      return "";
  }
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
  const running = download?.phase === "running" || download?.phase === "verifying";
  const pct = download ? Math.max(0, Math.min(1, download.pct)) : 0;
  const missing = info?.model_missing_files ?? [];

  return (
    <section className="settings-section">
      <h3>模型</h3>

      <div className="model-status">
        <div className="model-line">
          <span className="model-key">目录</span>
          <code className="model-path">{info?.model_path ?? "（未加载）"}</code>
        </div>
        <div className="model-line">
          <span className="model-key">状态</span>
          {info?.model_present ? (
            <span className="chip chip-ok">已就绪</span>
          ) : (
            <span className="chip chip-warn">未安装</span>
          )}
          {info && info.model_size_bytes > 0 && (
            <span className="muted">{formatBytes(info.model_size_bytes)}</span>
          )}
          {info?.model_present === false && missing.length > 0 && (
            <span className="muted">缺：{missing.join("、")}</span>
          )}
        </div>
      </div>

      {/* 下载进度。总量未知时（服务端没给 Content-Length）不画百分比，
          而不是画一个永远 0% 的条 —— 后者看起来像卡住了。 */}
      {running && (
        <div className="download-box">
          <div className="progress">
            <div
              className={`progress-fill${download?.phase === "verifying" ? " progress-indeterminate" : ""}`}
              style={{ width: `${Math.round(pct * 100)}%` }}
            />
          </div>
          <div className="download-meta">
            <span>{phaseText(download)}</span>
            {download?.phase === "running" && (
              <>
                <span className="muted">
                  {formatBytes(download.doneBytes)} / {formatBytes(download.totalBytes)}
                </span>
                {download.speed > 0 && (
                  <span className="muted">{formatBytes(download.speed)}/s</span>
                )}
                {download.eta > 0 && (
                  <span className="muted">剩 {Math.round(download.eta)} s</span>
                )}
                {download.file && (
                  <span className="muted">
                    {(download.fileIndex ?? 0) > 0
                      ? `第 ${download.fileIndex}/${download.fileCount ?? "?"} 个：`
                      : ""}
                    {download.file}
                  </span>
                )}
              </>
            )}
          </div>
        </div>
      )}

      {/* 终态文案：必须可操作。只说「失败」等于没说。 */}
      {!running && download && download.phase !== "idle" && (
        <p
          className={
            download.phase === "failed"
              ? "model-note model-note-error"
              : download.phase === "done"
                ? "model-note model-note-ok"
                : "model-note"
          }
        >
          {phaseText(download)}
          {download.detail ? ` —— ${download.detail}` : ""}
        </p>
      )}

      <div className="settings-actions">
        {running ? (
          <button className="danger" onClick={onCancelDownload} disabled={download?.phase === "verifying"}>
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
        <button className="mini" onClick={onReload} title="重新读取配置文件与模型状态">
          刷新状态
        </button>
      </div>

      <p className="settings-hint">
        下载源：hf-mirror（可在「运行」里改 <code>download.base_url</code>）。
        下载完成后会自动做一次真实推理校验；断点续传靠 <code>.part</code> 文件，
        取消或断网后重新点下载会接着下。
      </p>
    </section>
  );
}
