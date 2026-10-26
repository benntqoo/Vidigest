// 系统目录选择对话框。
//
// 为什么用插件而不是让用户手填路径：手填要用户自己拼 `D:\Vidigest\workspace`，
// 打错一个字符就是「保存失败 / 目录不存在」，而他完全看不出错在哪。
// `tauri-plugin-dialog` 调的是 Windows 原生文件夹选择器（IFileDialog），
// 用户能浏览、能新建文件夹，回来的是绝对路径。
//
// 依赖三件事，缺一不可（少一处就是运行时报错 `dialog.open not allowed`）：
// 1. `app-ui/src-tauri/Cargo.toml` 的 `tauri-plugin-dialog`
// 2. `lib.rs` 里 `.plugin(tauri_plugin_dialog::init())`
// 3. `capabilities/default.json` 里的 `"dialog:default"`
//
// 取消选择时返回 `null`（插件返回 null），调用方据此「不改动输入框」。

import { open } from "@tauri-apps/plugin-dialog";

export async function pickDirectory(
  defaultPath?: string,
  title = "选择目录",
): Promise<string | null> {
  const picked = await open({
    directory: true,
    multiple: false,
    title,
    // 默认落在当前值所在目录；传了非法路径时插件会忽略，不会报错。
    defaultPath: defaultPath || undefined,
  });
  return typeof picked === "string" && picked ? picked : null;
}
