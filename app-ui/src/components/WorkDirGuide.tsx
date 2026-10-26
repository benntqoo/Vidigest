// 首次运行：确认工作目录。
//
// 为什么需要这一步：打包首启的 `config.yaml` 由 `paths.ensure_default_config()`
// 自动生成，`work_dir` 预置到**非系统盘**（见 `paths.pick_default_work_dir()`），
// 但那是程序猜的。`work/` 里是 mp4 / wav / jpg，几十 GB 的量级，猜错位置
// 代价很大（要么撑爆 C 盘，要么用户找不到产出）。所以摆给用户确认一次。
//
// 触发条件是 `paths.work_dir_confirmed === false`，**不是**「路径等于默认值」——
// 后者会让「我就是要用 LOCALAPPDATA」的用户每次启动都被弹一次。

import { useState } from "react";

import { dirIsInside } from "../worker/pathGuard";

interface Props {
  /** 当前配置里的 work_dir（程序自动挑的） */
  suggested: string;
  /** 安装目录；`null` = 源码运行，不校验 */
  installDir: string | null;
  /** 保存中：worker 正在重启 */
  busy: boolean;
  onConfirm: (path: string) => void;
  onLater: () => void;
}

export function WorkDirGuide({ suggested, installDir, busy, onConfirm, onLater }: Props) {
  const [value, setValue] = useState(suggested);
  const trimmed = value.trim();
  const valid = trimmed.length > 0;
  // 放进安装目录的三个后果：Program Files 只读、卸载/升级连产出一起删、
  // UAC 虚拟化可能把写入悄悄重定向。所以这里直接拦住，而不是让他选了再报错。
  const inInstall = dirIsInside(trimmed, installDir);
  const blocked = !valid || inInstall;

  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h2>先选个地方放工作文件</h2>
        <p className="muted">
          下载的视频、抽出的音频和截图都放在这个目录里。单个视频通常几十到几百 MB，
          批量跑起来能到几十 GB —— 建议放在空间大、不是系统盘的磁盘上。
        </p>

        <label className="field">
          <span className="field-label">
            工作目录
            <em className="field-hint">已自动挑到剩余空间最大的非系统盘，也可以改</em>
          </span>
          <input
            value={value}
            disabled={busy}
            spellCheck={false}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>

        {!valid && <p className="banner banner-warn">目录不能为空</p>}
        {inInstall && (
          <p className="banner banner-warn">
            不能放在安装目录里（{installDir}）。那里是只读的，而且卸载或升级会连同产出一起删掉。
            请换一个独立目录，例如 D:\Vidigest\work。
          </p>
        )}

        <div className="modal-actions">
          <button type="button" onClick={onLater} disabled={busy}>
            稍后再说
          </button>
          <button
            type="button"
            className="primary"
            disabled={blocked || busy}
            onClick={() => onConfirm(trimmed)}
          >
            {busy ? "正在应用…" : "用这个目录"}
          </button>
        </div>

        <p className="settings-hint">
          「稍后再说」会沿用上面这个自动挑的目录，下次启动再问。设置里随时能改。
        </p>
      </div>
    </div>
  );
}
