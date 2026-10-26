// 下载进度条与文案。**两个地方共用**：语音模型面板、本地视觉模型（设置页）。
//
// 为什么抽出来：两个包的状态机是同一套（后端 `model_downloader` 就是同一个类），
// 文案和进度条各写一份必然漂移——「解压中」这种新阶段就会只在一个地方显示。

import { formatBytes, type DownloadState } from "../worker/types";

export function downloadPhaseText(d: DownloadState | null): string {
  if (!d) return "";
  switch (d.phase) {
    case "running":
      return d.totalBytes > 0 ? `下载中 ${Math.round(d.pct * 100)}%` : "下载中…";
    case "extracting":
      return `解压中 ${Math.round(d.pct * 100)}%`;
    case "verifying":
      // 文案中性化：语音模型是「真实推理校验」，视觉模型只是「二进制能不能起」。
      // 具体结论由后端在 `detail` 里给，这里不替它下判断。
      return "校验中…";
    case "done":
      if (d.verified === true) return "完成";
      if (d.verified === false) return "文件已下完，但校验未通过 —— 见下方说明";
      return "文件已就绪（未校验）";
    case "failed":
      return `失败（${d.code ?? "E_?"}）`;
    case "cancelled":
      return "已取消";
    default:
      return "";
  }
}

export function isDownloadActive(d: DownloadState | null): boolean {
  return d?.phase === "running" || d?.phase === "extracting" || d?.phase === "verifying";
}

/** 进行中的进度条。总量未知时不画百分比，而不是画一个永远 0% 的条。 */
export function DownloadProgress({ download }: { download: DownloadState | null }) {
  if (!isDownloadActive(download)) return null;
  const pct = download ? Math.max(0, Math.min(1, download.pct)) : 0;
  const indeterminate = download?.phase === "verifying";
  return (
    <div className="download-box">
      <div className="download-progress">
        <div
          className={`progress-fill${indeterminate ? " progress-indeterminate" : ""}`}
          style={{ width: `${Math.round(pct * 100)}%` }}
        />
      </div>
      <div className="download-meta">
        <span>{downloadPhaseText(download)}</span>
        {download?.phase === "running" && (
          <>
            <span className="muted">
              {formatBytes(download.doneBytes)} / {formatBytes(download.totalBytes)}
            </span>
            {download.speed > 0 && (
              <span className="muted">{formatBytes(download.speed)}/s</span>
            )}
            {download.eta > 0 && <span className="muted">剩 {Math.round(download.eta)} s</span>}
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
        {download?.phase === "extracting" && download.detail && (
          <span className="muted">{download.detail}</span>
        )}
      </div>
    </div>
  );
}

/** 终态文案。必须可操作：只说「失败」等于没说。 */
export function DownloadNote({ download }: { download: DownloadState | null }) {
  if (!download || isDownloadActive(download) || download.phase === "idle") return null;
  const cls =
    download.phase === "failed"
      ? "model-note model-note-error"
      : download.phase === "done"
        ? "model-note model-note-ok"
        : "model-note";
  return (
    <p className={cls}>
      {downloadPhaseText(download)}
      {download.elapsedSec ? `（${download.elapsedSec} s）` : ""}
      {download.detail ? ` —— ${download.detail}` : ""}
    </p>
  );
}
