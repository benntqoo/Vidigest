//! 卸载前清理用户数据：`vidigest.exe --cleanup-appdata`
//!
//! ## 为什么由应用自己做，而不是 MSI 的 `util:RemoveFolderEx`
//!
//! 2026-10-26 实测：装了带 `RemoveFolderEx` 的 MSI 再卸载，`%LOCALAPPDATA%\Vidigest`
//! **原封不动**（`config.yaml` 的 mtime 还停在卸载前）。用 `dark.exe` 反编译 MSI
//! 查到的真因：
//!
//! ```xml
//! <Custom Action="SetVIDIGEST_APPDATA_DIR" Before="CostFinalize">…</Custom>
//! ```
//!
//! `[LocalAppDataFolder]` 是**目录属性**，由 `CostFinalize` 这个动作才填出来；
//! 排在它**之前**执行 Type 51 设属性，格式化拿到的是空串 —— 于是
//! `RemoveFolderEx` 去删一个不存在的路径，**静默成功**，什么也没删。
//!
//! 改由本进程执行的好处：路径自己算，不存在「属性什么时候被格式化」这类时序问题，
//! 而且**能在本机实测**（MSI 的装/卸要提权 + GUI，验不了）。
//!
//! ## 为什么不顺手多删几个地方
//!
//! 删四处：
//!
//! | 目标 | 内容 |
//! |---|---|
//! | `<LocalAppData>\Vidigest` | state.db / config.yaml |
//! | `<LocalAppData>\com.benntqoo.vidigest` | WebView2 user data（实测卸载后留 ~70 MB） |
//! | `<安装目录>\workspace` | 产出（mp4 / wav / jpg），**只在它确实在安装目录下时** |
//! | `<安装目录>\models` | 模型（2.9 GiB），同上 |
//!
//! 后两个是 2026-10-26 加的：默认目录改成「安装目录下」之后，卸载不删它们就会在
//! 安装目录里留几十 GB。代价是**模型也没了**，重装要重新复制或下载 —— 这是
//! 「一个地方放全部」的必然结果，已记录在 docs/打包与分发.md §6.3。
//!
//! 不碰用户**改到别处**的目录：我们只删这两个固定名字的子目录，且只删安装目录下的。

use std::path::PathBuf;

/// 触发卸载清理的开关。MSI 的 `CleanupAppData` 自定义动作就是用它调用本程序。
pub const CLEANUP_FLAG: &str = "--cleanup-appdata";

/// 应用数据目录名，与 `app/core/paths.py::APP_DIR_NAME` 保持一致。
const APP_DIR_NAME: &str = "Vidigest";

/// Tauri 的 bundle identifier，与 `tauri.conf.json` 保持一致。
/// WebView2 把 user data（缓存 / LocalStorage / IndexedDB）放在这个名字下面。
const BUNDLE_IDENTIFIER: &str = "com.benntqoo.vidigest";

/// 命令行里有没有卸载清理开关。
pub fn is_cleanup_invocation(args: impl IntoIterator<Item = String>) -> bool {
    args.into_iter().any(|a| a == CLEANUP_FLAG)
}

/// 安装目录（`vidigest.exe` 所在的那一层）。
///
/// 卸载的自定义动作用的是**主程序**（`FileKey="Path"`），所以 `current_exe()`
/// 的父目录就是安装目录 —— 不是 worker 所在的 `worker/` 子目录。
///
/// `VIDIGEST_CLEANUP_EXE_DIR` 只用于测试：把安装目录换成一个临时目录，
/// 好在不碰真实安装的情况下验证「workspace / models 会被删」。
fn install_dir() -> Option<PathBuf> {
    if let Some(dir) = std::env::var_os("VIDIGEST_CLEANUP_EXE_DIR") {
        return Some(PathBuf::from(dir));
    }
    std::env::current_exe().ok()?.parent().map(PathBuf::from)
}

/// `<LocalAppData>` 根目录。
///
/// `VIDIGEST_CLEANUP_ROOT` 只用于**测试**：把它当成 `%LOCALAPPDATA%` 用，
/// 这样能在临时目录里跑一遍真删除逻辑，而不用去动真数据。
fn local_app_data_root() -> Option<PathBuf> {
    if let Some(root) = std::env::var_os("VIDIGEST_CLEANUP_ROOT") {
        return Some(PathBuf::from(root));
    }
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
}

/// 执行清理，返回进程退出码（**永远 0**：卸载不该因为这个失败而中断）。
///
/// 失败只吞掉不报错 —— 卸载已经删了程序文件，这里再抛一个错误出去只会让
/// 用户看到一个「卸载失败」的对话框，而实际上程序已经没了，数据在不在
/// 是次要问题。留下日志给 `--cleanup-appdata` 的 stderr（MSI 会忽略它）。
pub fn run_cleanup() -> i32 {
    let Some(root) = local_app_data_root() else {
        eprintln!("[cleanup] 找不到 LOCALAPPDATA，跳过清理");
        return 0;
    };

    let mut targets: Vec<PathBuf> = [APP_DIR_NAME, BUNDLE_IDENTIFIER]
        .iter()
        .map(|name| root.join(name))
        .collect();
    if let Some(inst) = install_dir() {
        // 只删这两个**固定名字**的子目录。安装目录本身不碰。
        targets.push(inst.join("workspace"));
        targets.push(inst.join("models"));
    }

    for target in targets {
        if !target.exists() {
            // 正常情况：没装过 / 已经删过。不算错误。
            continue;
        }
        // 最多试 3 次：卸载瞬间 WebView2 的辅助进程可能还没完全退出，
        // 文件被占用时 remove_dir_all 会失败。200 ms 足够它们退干净。
        let mut last: Option<std::io::Error> = None;
        for attempt in 1..=3 {
            match std::fs::remove_dir_all(&target) {
                Ok(()) => {
                    eprintln!("[cleanup] 已删除 {}", target.display());
                    last = None;
                    break;
                }
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                    last = None;
                    break;
                }
                Err(err) => {
                    eprintln!(
                        "[cleanup] 第 {attempt} 次删除 {} 失败：{err}",
                        target.display()
                    );
                    last = Some(err);
                    std::thread::sleep(std::time::Duration::from_millis(200));
                }
            }
        }
        if let Some(err) = last {
            // **如实记录但不上抛**：数据留着比卸载失败好。
            eprintln!("[cleanup] 放弃删除 {}：{err}", target.display());
        }
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flag_is_recognized_anywhere_in_argv() {
        assert!(is_cleanup_invocation(vec![CLEANUP_FLAG.to_string()]));
        assert!(is_cleanup_invocation(vec![
            "vidigest.exe".to_string(),
            "--foo".to_string(),
            CLEANUP_FLAG.to_string(),
        ]));
    }

    #[test]
    fn ordinary_launch_is_not_cleanup() {
        // 漏判的后果很严重：正常开机启动就把用户数据删了。
        assert!(!is_cleanup_invocation(Vec::<String>::new()));
        assert!(!is_cleanup_invocation(vec![
            "vidigest.exe".to_string(),
            "--cleanup-appdata-later".to_string(),
        ]));
    }
}
