//! 定位 Python worker 的启动目标（程序、参数、工作目录）。
//!
//! 开发期（`tauri dev`）与打包后（sidecar）的形态完全不同，这里统一收口。
//! 定位失败时要能说清「是哪条规则都没命中」，而不是抛一句 path not found。

use std::path::{Path, PathBuf};

/// worker 的启动目标。
#[derive(Debug, Clone)]
pub struct WorkerTarget {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    /// 命中这条规则的说明。出错诊断时直接展示给用户。
    pub source: String,
}

/// 仓库根目录。`CARGO_MANIFEST_DIR` = `<repo>/app-ui/src-tauri`，
/// 因此上溯两级即 `<repo>`。编译期常量，运行时零成本。
pub fn project_root() -> Option<PathBuf> {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .map(Path::to_path_buf)
}

/// 定位 worker 启动目标。
///
/// 优先级（从上到下，**先命中先用**）：
///
/// 0. `VIDIGEST_WORKER` —— 直接给可执行文件路径。调试自建包时用，压过下面一切。
/// 1. `VIDIGEST_BUNDLED=1` —— 强制走打包产物。开发机上验 sidecar 用这条
///    （不必做完整 `tauri build`，见 `docs/打包与分发.md`）。
/// 2. 编译配置：debug 走 `locate_dev()`，release 走 `locate_bundled()`。
///
/// **开发模式**（`locate_dev`）：
/// 1. `VIDIGEST_PYTHON` 环境变量 —— 显式指定，最高优先级。本项目依赖装在受管 venv 里，
///    不在项目目录内，所以这条是日常开发的主力路径。
/// 2. `<repo>/.venv/Scripts/python.exe`（Windows）/ `<repo>/.venv/bin/python`（Unix）
/// 3. PATH 中的 `python`
///
/// **打包模式**（`locate_bundled`）见该函数的注释。
pub fn locate() -> Result<WorkerTarget, String> {
    if let Some(path) = env_path("VIDIGEST_WORKER") {
        if !path.is_file() {
            return Err(format!(
                "环境变量 VIDIGEST_WORKER 指向的文件不存在：{}\n\
                 该变量用于显式指定 worker 可执行文件（打包产物）；\
                 不设置它则按目录自动查找。",
                path.display()
            ));
        }
        let cwd = path.parent().map(Path::to_path_buf).unwrap_or_else(|| PathBuf::from("."));
        return Ok(WorkerTarget {
            program: path.clone(),
            args: Vec::new(),
            cwd,
            source: format!("环境变量 VIDIGEST_WORKER = {}", path.display()),
        });
    }

    if env_flag("VIDIGEST_BUNDLED") {
        return locate_bundled();
    }

    if cfg!(debug_assertions) {
        locate_dev()
    } else {
        locate_bundled()
    }
}

/// 读一个「路径型」环境变量。**空串视为未设置**：
/// 把变量设成空串是常见的「清空」手法，若当成路径会得到一个恒不存在的候选，
/// 反而把真正的错误信息挤到后面。
fn env_path(name: &str) -> Option<PathBuf> {
    parse_env_path(std::env::var(name).ok().as_deref())
}

fn parse_env_path(raw: Option<&str>) -> Option<PathBuf> {
    let trimmed = raw?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(PathBuf::from(trimmed))
    }
}

/// 读一个「开关型」环境变量。`1` / `true` / `yes` / `on`（不分大小写）为真。
fn env_flag(name: &str) -> bool {
    parse_env_flag(std::env::var(name).ok().as_deref())
}

/// 刻意不接受「非空即真」：否则 `VIDIGEST_BUNDLED=0` 会**打开**打包模式，
/// 而写 `=0` 的人意思恰好相反。抽成纯函数是为了这条能被单测锁住。
fn parse_env_flag(raw: Option<&str>) -> bool {
    matches!(
        raw.unwrap_or("").trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn locate_dev() -> Result<WorkerTarget, String> {
    let root = project_root().ok_or_else(|| {
        format!(
            "无法从 CARGO_MANIFEST_DIR({}) 上溯定位仓库根",
            env!("CARGO_MANIFEST_DIR")
        )
    })?;

    // 1. 显式环境变量
    if let Ok(p) = std::env::var("VIDIGEST_PYTHON") {
        let path = PathBuf::from(&p);
        if path.is_file() {
            return Ok(WorkerTarget {
                program: path,
                args: module_args(),
                cwd: root,
                source: format!("环境变量 VIDIGEST_PYTHON = {p}"),
            });
        }
        return Err(format!(
            "环境变量 VIDIGEST_PYTHON 指向的文件不存在：{p}\n\
             本项目依赖（faster-whisper / ctranslate2）不在系统 Python 里，必须指向装有依赖的解释器。"
        ));
    }

    // 2. 项目内虚拟环境
    let candidates: [PathBuf; 2] = [
        root.join(".venv").join("Scripts").join("python.exe"),
        root.join(".venv").join("bin").join("python"),
    ];
    for cand in candidates.iter() {
        if cand.is_file() {
            return Ok(WorkerTarget {
                program: cand.clone(),
                args: module_args(),
                cwd: root,
                source: format!("项目虚拟环境 {}", cand.display()),
            });
        }
    }

    // 3. PATH
    if let Some(found) = which("python") {
        return Ok(WorkerTarget {
            program: found.clone(),
            args: module_args(),
            cwd: root,
            source: format!("PATH 中的 python（{}）—— 注意：该解释器可能未安装项目依赖", found.display()),
        });
    }

    Err(format!(
        // 首行必须是**动作**：状态栏只渲染首行（见 `StatusBar.tsx`）。
        "找不到 Python 解释器：请设置环境变量 VIDIGEST_PYTHON 指向装有 faster-whisper 的解释器。\n\
         已尝试：\n\
         1. 环境变量 VIDIGEST_PYTHON\n\
         2. {}\n\
         3. PATH 中的 python",
        root.join(".venv").join("Scripts").join("python.exe").display()
    ))
}

/// 打包模式的候选路径，按优先级排列。抽成纯函数以便单测 ——
/// 「候选顺序错了」这类缺陷在运行时只表现为一句「找不到 worker」，很难定位。
fn bundled_candidates(program_dir: &Path) -> Vec<(PathBuf, &'static str)> {
    let name = if cfg!(windows) {
        "vidigest-worker.exe"
    } else {
        "vidigest-worker"
    };

    let mut out = vec![
        (
            program_dir.join("worker").join(name),
            "安装目录的 worker/（由 packaging/tauri.bundle.json 的 bundle.resources 映射）",
        ),
        (program_dir.join(name), "可执行文件同级"),
    ];

    // 开发机上还没有安装包：PyInstaller 产物直接躺在仓库里。
    // 这条只服务于 `VIDIGEST_BUNDLED=1 + tauri dev`（验 sidecar 定位但不做完整打包），
    // **不会**被普通 dev 模式命中 —— 那会导致「改了源码却跑的是旧包」这种隐式陷阱。
    if let Some(root) = project_root() {
        out.push((
            root.join("build")
                .join("pyinstaller")
                .join("vidigest-worker")
                .join(name),
            "仓库内的 PyInstaller 产物（开发机验证 sidecar 用）",
        ));
    }

    out
}

/// 打包模式：定位 PyInstaller 产出的 worker 可执行文件。
///
/// 产物是**单目录**（exe + `_internal/`），所以它整个被 `bundle.resources`
/// 映射到安装目录的 `worker/` 下，而不是走 `bundle.externalBin`
/// （那个只支持单文件，配单目录会得到一个缺 DLL 的包）。
fn locate_bundled() -> Result<WorkerTarget, String> {
    let exe = std::env::current_exe().map_err(|e| format!("取 current_exe 失败：{e}"))?;
    let dir = exe
        .parent()
        .ok_or_else(|| "可执行文件没有父目录".to_string())?;

    let candidates = bundled_candidates(dir);
    for (path, why) in &candidates {
        if path.is_file() {
            return Ok(WorkerTarget {
                program: path.clone(),
                // 打包后是独立可执行文件，不需要 `-m app.rpc`
                args: Vec::new(),
                // cwd = worker 自己的目录。打包后的配置走 `%LOCALAPPDATA%/Vidigest`
                // （见 `app/core/paths.py`），cwd 只影响相对路径的兜底行为。
                cwd: path.parent().unwrap_or(dir).to_path_buf(),
                source: format!("打包产物：{why}"),
            });
        }
    }

    // 首行必须是**动作**：状态栏只渲染首行（见 `StatusBar.tsx`）。
    let tried = candidates
        .iter()
        .map(|(p, why)| format!("  · {why}\n    {}", p.display()))
        .collect::<Vec<_>>()
        .join("\n");
    Err(format!(
        "找不到打包后的 worker：请先运行 tools/build_worker.py 生成产物。\n\
         已尝试：\n{tried}"
    ))
}

/// 开发模式以模块方式启动：`python -m app.rpc`（cwd 必须是仓库根，否则 import 不到 app 包）。
fn module_args() -> Vec<String> {
    vec!["-m".to_string(), "app.rpc".to_string()]
}

/// 在 PATH 中查找可执行文件。自己实现是为了不引入 `which` crate —— 逻辑只有十几行。
fn which(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    let exts: Vec<String> = if cfg!(windows) {
        std::env::var("PATHEXT")
            .unwrap_or_else(|_| ".EXE;.CMD;.BAT".to_string())
            .split(';')
            .map(|s| s.to_ascii_lowercase())
            .collect()
    } else {
        vec![String::new()]
    };

    for dir in std::env::split_paths(&path_var) {
        for ext in &exts {
            let cand = dir.join(format!("{name}{ext}"));
            if cand.is_file() {
                return Some(cand);
            }
            // Windows 上 PATH 里的名字大小写不定，再试一次大写
            if cfg!(windows) {
                let upper = dir.join(format!("{}{}", name.to_uppercase(), ext.to_uppercase()));
                if upper.is_file() {
                    return Some(upper);
                }
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn project_root_contains_expected_markers() {
        let root = project_root().expect("应能定位仓库根");
        // 用两个独立标记交叉验证，避免上溯级数写错还能「看起来成功」
        assert!(root.join("PLAN.md").is_file(), "缺 PLAN.md：{}", root.display());
        assert!(root.join("app").join("rpc.py").is_file(), "缺 app/rpc.py：{}", root.display());
    }

    #[test]
    fn module_args_point_to_rpc_entry() {
        assert_eq!(module_args(), vec!["-m".to_string(), "app.rpc".to_string()]);
    }

    #[test]
    fn bundled_candidates_prefer_install_dir_then_repo_build() {
        let dir = Path::new("C:/fake/app");
        let c = bundled_candidates(dir);
        let norm = |p: &Path| p.to_string_lossy().replace('\\', "/");

        // 顺序不能错：安装后 exe 就在 `%INSTDIR%`，sidecar 在它的 `worker/` 下。
        // 若把「同级」放在前面，某些安装器把 exe 平铺时能蒙对，
        // 但正常安装下会先命中一个不存在的候选 —— 只是慢，不算错，
        // 真正的风险是顺序反过来时**命中了残留的旧文件**。
        assert_eq!(norm(&c[0].0), "C:/fake/app/worker/vidigest-worker.exe");
        assert_eq!(norm(&c[1].0), "C:/fake/app/vidigest-worker.exe");

        // 仓库内产物必须在候选里：`VIDIGEST_BUNDLED=1` + `tauri dev` 靠它，
        // 少了这条，开发机上就只剩「做完整安装包」一条验证路径。
        assert!(
            c.iter()
                .any(|(p, _)| norm(p).contains("/build/pyinstaller/vidigest-worker/")),
            "缺仓库内产物候选：{c:?}"
        );
    }

    #[test]
    fn empty_env_values_are_treated_as_unset() {
        // 空串若被当成路径，会得到一个恒不存在的候选，
        // 把真正要展示给用户的错误信息挤到后面。
        assert_eq!(parse_env_path(None), None);
        assert_eq!(parse_env_path(Some("")), None);
        assert_eq!(parse_env_path(Some("   ")), None);
        assert_eq!(
            parse_env_path(Some(" D:/x/w.exe ")),
            Some(PathBuf::from("D:/x/w.exe"))
        );
    }

    #[test]
    fn flag_requires_truthy_word_not_merely_nonempty() {
        assert!(parse_env_flag(Some("1")));
        assert!(parse_env_flag(Some("TRUE")));
        assert!(parse_env_flag(Some("yes")));
        // `VIDIGEST_BUNDLED=0` 的语义是「关」，不是「非空即开」
        assert!(!parse_env_flag(Some("0")));
        assert!(!parse_env_flag(Some("")));
        assert!(!parse_env_flag(None));
    }
}
