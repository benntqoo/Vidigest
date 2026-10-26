// 首次运行：确认两个目录（工作目录 + 模型目录）。
//
// 为什么需要这一步：打包首启的 `config.yaml` 由 `paths.ensure_default_config()` 自动生成，
// 两个目录都已预置（默认在**安装目录**下：`<安装目录>\workspace` 与 `<安装目录>\models`），
// 但那是程序猜的。`workspace/` 里是 mp4 / wav / jpg，几十 GB 的量级；`models/` 是 2.9 GiB。
// 猜错位置的代价很大，所以摆给用户确认一次，两个都能改。
//
// 触发条件是 `paths.work_dir_confirmed === false`，**不是**「路径等于默认值」——
// 后者会让「我就是要用 LOCALAPPDATA」的用户每次启动都被弹一次。
// 这个确认位现在覆盖**两个**目录（一个弹窗确认两个，避免两次打扰）。

import { useState } from "react";

interface Props {
  /** 配置里的 work_dir（程序自动挑的） */
  suggestedWork: string;
  /** 配置里的 model_dir（程序自动挑的） */
  suggestedModel: string;
  /** 默认位置里是否已经找到完整模型（5 个文件齐全） */
  modelPresent: boolean;
  /** 工作目录是否已存在（= 上次安装留下的，卸载不删 workspace） */
  workReused: boolean;
  /** 保存中：worker 正在重启 */
  busy: boolean;
  onConfirm: (work: string, model: string) => void;
  onLater: () => void;
}

export function DirsGuide({
  suggestedWork,
  suggestedModel,
  modelPresent,
  workReused,
  busy,
  onConfirm,
  onLater,
}: Props) {
  const [work, setWork] = useState(suggestedWork);
  const [model, setModel] = useState(suggestedModel);
  const workTrim = work.trim();
  const modelTrim = model.trim();
  const valid = workTrim.length > 0 && modelTrim.length > 0;

  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h2>确认两个目录</h2>
        <p className="muted">
          默认都放在安装目录下，方便一处管理。也可以改到别的磁盘 ——
          如果你把程序装在受保护的位置（例如 Program Files），下面显示的会自动换到可写的地方。
        </p>

        {(workReused || modelPresent) && (
          <p className="banner banner-info">
            检测到上次安装留下的数据，已直接沿用：
            {workReused ? "工作目录" : ""}
            {workReused && modelPresent ? " + " : ""}
            {modelPresent ? "模型" : ""}
            。卸载只清配置与任务历史，产出和模型都会保留。
          </p>
        )}

        <label className="field">
          <span className="field-label">
            工作目录
            <em className="field-hint">下载的视频、抽出的音频和截图放这里；批量跑能到几十 GB</em>
          </span>
          <input
            value={work}
            disabled={busy}
            spellCheck={false}
            onChange={(e) => setWork(e.target.value)}
          />
        </label>

        <label className="field">
          <span className="field-label">
            模型目录
            <em className="field-hint">
              {modelPresent
                ? "已检测到完整模型，不用重新下载"
                : "还没有模型；确认后可在设置里一键下载（约 2.9 GiB）"}
            </em>
          </span>
          <input
            value={model}
            disabled={busy}
            spellCheck={false}
            onChange={(e) => setModel(e.target.value)}
          />
        </label>

        {!valid && <p className="banner banner-warn">两个目录都不能为空</p>}

        <div className="modal-actions">
          <button type="button" onClick={onLater} disabled={busy}>
            稍后再说
          </button>
          <button
            type="button"
            className="primary"
            disabled={!valid || busy}
            onClick={() => onConfirm(workTrim, modelTrim)}
          >
            {busy ? "正在应用…" : "用这两个目录"}
          </button>
        </div>

        <p className="settings-hint">
          「稍后再说」会沿用上面这两个自动挑的目录，下次启动再问。设置里随时能改。
        </p>
      </div>
    </div>
  );
}
