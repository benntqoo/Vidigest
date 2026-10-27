#!/usr/bin/env bash
# =============================================================================
# Vidigest 构建入口
#
#   ./dev.sh release      完整分发形态：Python worker + Tauri 安装包
#   ./dev.sh bundle-dev   L1 验证：不重新打包，直接用 worker 产物驱动 GUI
#   ./dev.sh dev          日常开发（venv 驱动）
#   ./dev.sh worker       只打包 Python worker（含 28 项冒烟）
#   ./dev.sh check        跑全部判据，不写产物
#   ./dev.sh bump 0.2.1   递更版本号（VERSION + tauri + package + Cargo + rpc 五处）
#   ./dev.sh clean        列出可清理的旁置产物（加 --yes 才删）
#
# 开关（可与子命令任意顺序）：
#   --dry-run       只打印将要执行的命令
#   --skip-worker   release 时复用已有 worker 产物，跳过 PyInstaller
#   --yes, -y       clean 时真的删除
#
# 环境变量：
#   VIDIGEST_PYTHON  指定打包用的 Python（必须已装 faster-whisper / ctranslate2）
#
# 为什么要固化成一个脚本：完整构建是三个**必须按顺序、各有独立判据**的环节
# （PyInstaller → cargo compile → Tauri bundle），且环节之间有隐式耦合
# （worker 产物不存在时 cargo build 会直接失败）。每次照文档手敲三条命令，
# 迟早会在某一步用错解释器或用错工作目录 —— 而报错样子还都很难反推。
# =============================================================================

if [ -z "${BASH_VERSION:-}" ]; then
  echo "请用 bash 运行：bash dev.sh <子命令>" >&2
  exit 2
fi

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$SCRIPT_DIR"

#: 与 tools/build_worker.py 的 DIST/OUT_DIR/EXE 保持一致（那边是真源）
WORKER_EXE="$REPO/build/pyinstaller/vidigest-worker/vidigest-worker.exe"
TAURI_BUNDLE_CFG="../packaging/tauri.bundle.json"
BUNDLE_DIR="$REPO/app-ui/src-tauri/target/release/bundle"

DRY_RUN=0
SKIP_WORKER=0
CLEAN_YES=0
PY="${VIDIGEST_PYTHON:-}"
STEP_MSG=""

# ----------------------------------------------------------------- 输出小工具
if [ -t 1 ]; then
  C_RST=$'\033[0m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
else
  C_RST=''; C_DIM=''; C_BOLD=''; C_RED=''; C_GRN=''; C_YEL=''
fi

say()  { printf '%s\n' "$*"; }
step() { STEP_MSG="$*"; printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_RST"; }
ok()   { printf '%s  ok%s  %s\n' "$C_GRN" "$C_RST" "$*"; }
warn() { printf '%s  !!%s  %s\n' "$C_YEL" "$C_RST" "$*" >&2; }
die()  { printf '%s  错误%s  %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }

# 每条外部命令都先回显再执行。build 失败的排查成本主要花在
# 「刚才到底是用哪个解释器、在哪个目录跑的」—— 先打出来就不用回头猜。
run() {
  printf '%s  $ ' "$C_DIM"
  printf '%q ' "$@"
  printf '%s\n' "$C_RST"
  (( DRY_RUN )) || "$@"
}

on_exit() {
  local code=$?
  if (( code != 0 )) && [ -n "$STEP_MSG" ]; then
    printf '\n%s失败于：%s（退出码 %s）%s\n' "$C_RED" "$STEP_MSG" "$code" "$C_RST" >&2
  fi
}
trap on_exit EXIT

# msys 下 $PWD 形如 /d/Code/...，直接拼进给用户复制的 PowerShell 命令是废的。
win_path() {
  if [[ "$1" =~ ^/([a-zA-Z])/(.*)$ ]]; then
    printf '%s:\\%s\n' "${BASH_REMATCH[1]^^}" "${BASH_REMATCH[2]//\//\\}"
  else
    printf '%s\n' "$1"
  fi
}

usage() {
  sed -n '3,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --------------------------------------------------------------- 前置探测
# 判据是「**能不能 import**」，不是「文件在不在」。
# 本机的托管 3.13.12 与系统 3.13 都没装 faster-whisper，只判存在性会选中它们，
# 然后在 PyInstaller 中途才失败，报错样子离真因很远（2026-10-03 踩过）。
find_python() {
  local cand=() c
  [ -n "${VIDIGEST_PYTHON:-}" ] && cand+=("$VIDIGEST_PYTHON")
  cand+=(
    "C:/Users/Ben/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
    "$REPO/.venv/Scripts/python.exe"
    "$REPO/.venv/bin/python"
  )
  c="$(command -v python 2>/dev/null || true)";  [ -n "$c" ] && cand+=("$c")
  c="$(command -v python3 2>/dev/null || true)"; [ -n "$c" ] && cand+=("$c")

  for c in "${cand[@]}"; do
    [ -n "$c" ] || continue
    if [ -x "$c" ] && "$c" -c 'import faster_whisper, ctranslate2' >/dev/null 2>&1; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

need_python() {
  if [ -z "$PY" ]; then
    PY="$(find_python)" || {
      warn "试过的候选：VIDIGEST_PYTHON / 托管 venv / .venv / PATH"
      die "找不到装有 faster-whisper 的 Python。用 VIDIGEST_PYTHON=<路径> 指定。"
    }
    ok "Python：$PY"
  fi
}

need_node() {
  command -v npm >/dev/null 2>&1 || die "PATH 里没有 npm（Node 未装或未进 PATH）"
  [ -d "$REPO/app-ui/node_modules" ] || die "app-ui/node_modules 不存在，先跑：cd app-ui && npm install"
}

# ------------------------------------------------------------------ 子命令
cmd_worker() {
  need_python
  step "打包 Python worker（PyInstaller 单目录 + 冒烟）"
  run "$PY" "$REPO/tools/build_worker.py"
  # dry-run 下不能报 ok：产物可能根本不存在，报了就是在说假话
  if (( DRY_RUN )); then
    warn "--dry-run：未真跑；产物将落在 $WORKER_EXE"
  else
    ok "worker 产物：$WORKER_EXE"
  fi
}

cmd_release() {
  need_node
  say "仓库：$REPO"
  say "配置：$TAURI_BUNDLE_CFG（resources 映射 2.5 GB worker 产物）"

  if (( SKIP_WORKER )); then
    step "复用已有 worker 产物（--skip-worker）"
    [ -f "$WORKER_EXE" ] || die "没有可复用的产物：$WORKER_EXE
     去掉 --skip-worker，或先跑 ./dev.sh worker"
    # 守卫：python 侧改过（尤其是 `./dev.sh bump` 会改 rpc.py 的 SERVER_VERSION）
    # 却复用旧 worker ⇒ 装出来的包「MSI 是 0.2.1、界面显示 0.2.0」，
    # 正是版本号要解决的那个问题。用 mtime 判：rpc.py 比产物新就拒绝。
    if [ "$REPO/app/rpc.py" -nt "$WORKER_EXE" ]; then
      die "app/rpc.py 比 worker 产物新（可能刚 bump 过版本号）——
     SKIP_WORKER 会打出一个版本自相矛盾的包。直接跑 ./dev.sh release。"
    fi
    ok "$WORKER_EXE"
  else
    cmd_worker
  fi

  step "构建 Tauri 安装包"
  # cwd 必须是 app-ui：npm run tauri 要读那里的 package.json，
  # 而 `--config` 的相对路径是相对 cwd 解析的（不是相对 tauri.conf.json）。
  (
    cd "$REPO/app-ui" || exit 1
    run npm run tauri build -- --config "$TAURI_BUNDLE_CFG"
  )

  (( DRY_RUN )) && { say ""; warn "--dry-run：未真正执行，跳过产物报告"; return 0; }
  report_artifacts
}

cmd_bundle_dev() {
  need_node
  step "L1 验证：用打包产物驱动 GUI（不重新打包）"
  [ -f "$WORKER_EXE" ] || die "没有 worker 产物：$WORKER_EXE
     先跑 ./dev.sh worker"
  ok "$WORKER_EXE"
  say "判据：启动日志里出现「已启动：打包产物：仓库内的 PyInstaller 产物」"
  say "      —— 只有这一行能证明走的是 sidecar 而不是 venv（两者界面看起来一样）"
  (
    cd "$REPO/app-ui" || exit 1
    VIDIGEST_BUNDLED=1 run npm run tauri dev
  )
}

cmd_dev() {
  need_node
  step "开发模式（venv 驱动，需手动起 vite）"
  ( cd "$REPO/app-ui" || exit 1; run npm run tauri dev )
}

cmd_check() {
  need_python
  need_node
  step "协议层 —— tools/rpc_smoke.py"
  run "$PY" "$REPO/tools/rpc_smoke.py"
  step "调度层 —— tools/dispatcher_selftest.py"
  run "$PY" "$REPO/tools/dispatcher_selftest.py"
  step "下载层 —— tools/fetcher_selftest.py"
  run "$PY" "$REPO/tools/fetcher_selftest.py"
  # 地址解析 / 归一化。2026-10-27 之前没有任何判据覆盖 `urls.py`，而它是
  # 所有入口（CLI / RPC / 批量）的第一道关 —— 抖音搜索结果页那种形态就是在
  # 这里判不出平台，一路滑到 yt-dlp 报 Unsupported URL。
  step "地址解析 —— tools/urls_selftest.py"
  run "$PY" "$REPO/tools/urls_selftest.py"
  # 下载器判据用的是本地假 HTTP 服务器（离线、几秒），必须进常规检查 ——
  # 2026-10-27 之前它不在这里，等于 11 条判据存在但从不执行。
  step "模型下载器 —— tools/model_downloader_selftest.py"
  run "$PY" "$REPO/tools/model_downloader_selftest.py"
  step "Rust 单测 —— cargo test --lib"
  ( cd "$REPO/app-ui/src-tauri" || exit 1; run env CARGO_INCREMENTAL=0 cargo test --lib )
  step "前端类型检查 —— tsc --noEmit"
  ( cd "$REPO/app-ui" || exit 1; run npx tsc --noEmit )
  # 主界面的「状态 → 视图」映射。tsc 只能保证类型对，保证不了语义对：
  # 「待抽帧」写成「抽帧中」、「没验成」写成「GPU 不可用」都能通过编译。
  # 24 条判据，纯函数，秒级，不需要 GPU / Rust。
  step "主界面状态视图 —— tools/main_layout_selftest.mts"
  run node --experimental-strip-types "$REPO/tools/main_layout_selftest.mts"

  # README 是 GitHub 首页的门面，而 docs/ 里的文件会改名、移动、合并 ——
  # 改一次名字，README 里那条链接就静默变 404，渲染端不会报错，只有点的人知道。
  # 同时守住「本机 / C 盘 / 我的显卡」这类只对作者成立的句子不进对外文档。
  step "README 自检 —— tools/readme_check.py"
  run "$PY" "$REPO/tools/readme_check.py"

  say ""
  ok "全部判据通过"
}

cmd_clean() {
  step "PyInstaller 旁置产物"
  local list=() d
  for d in "$REPO"/build/pyinstaller/*.old-* "$REPO"/build/pyinstaller-work/*.old-*; do
    [ -d "$d" ] && list+=("$d")
  done

  if [ "${#list[@]}" -eq 0 ]; then
    ok "没有需要清理的旁置产物"
    return 0
  fi

  local total=0
  for d in "${list[@]}"; do
    printf '  %s\n' "$d"
  done
  say ""
  warn "这是删除操作，共 ${#list[@]} 个目录。"
  say "  成因：PyInstaller 覆盖旧产物时会撞沙箱的批量删除保护（一次删 >50 文件），"
  say "        所以 build_worker.py 只改名旁置、不代删。"

  if (( ! CLEAN_YES )); then
    say ""
    say "未删除任何文件。确认后重跑：./dev.sh clean --yes"
    say "等价 PowerShell（沙箱外）："
    say "  Get-ChildItem '$(win_path "$REPO/build")\\*\\*.old-*' -Directory | Remove-Item -Recurse -Force"
    return 0
  fi

  for d in "${list[@]}"; do
    run rm -rf -- "$d"
  done
  ok "已清理 ${#list[@]} 个目录"
}

report_artifacts() {
  step "产物"
  local found=0 f size
  local targets=(
    "$BUNDLE_DIR"/nsis/*.exe
    "$BUNDLE_DIR"/msi/*.msi
    "$REPO/app-ui/src-tauri/target/release/"*.exe
  )
  for f in "${targets[@]}"; do
    [ -f "$f" ] || continue
    found=1
    size="$(du -h -- "$f" 2>/dev/null | cut -f1)"
    printf '  %s  %s(%s)%s\n' "$C_BOLD$f$C_RST" "$C_DIM" "$size" "$C_RST"
  done
  if (( found == 0 )); then
    warn "没找到安装包或主程序 —— 检查上一步输出（bundle.targets 是否包含 nsis/msi）"
    return 0
  fi
  say ""
  say "安装包双击即可；装完的形态见 docs/打包与分发.md §5（路径重定向）。"
  say "⚠️ 「无 Python 无 Rust 机器」上的运行仍未验证 —— 同机模拟不算，见 §6.2。"
}

# -------------------------------------------------------------------- 入口
SUBCMD=""
ARG1=""   # bump 的版本号参数（其它子命令不接受额外参数）
for a in "$@"; do
  case "$a" in
    --dry-run)     DRY_RUN=1 ;;
    --skip-worker) SKIP_WORKER=1 ;;
    --yes|-y)      CLEAN_YES=1 ;;
    --help|-h)     usage; exit 0 ;;
    -*)            die "未知参数：$a（./dev.sh --help）" ;;
    *)             if [ -z "$SUBCMD" ]; then SUBCMD="$a"
                   elif [ "$SUBCMD" = "bump" ] && [ -z "$ARG1" ]; then ARG1="$a"
                   else die "多余的参数：$a"; fi ;;
  esac
done

cmd_bump() {
  # 版本号散在 5 个地方，少改一处不会报错，只会让「控制面板显示的版本」和
  # 「界面显示的版本」对不上 —— 用户就说不清自己装的是哪个。
  # 所以只给这一条命令，并由 rpc_smoke 的判据钉住一致性。
  local ver="${1:-}"
  need_python
  step "递更版本号"
  if [ -z "$ver" ]; then
    run "$PY" tools/bump_version.py --check
  else
    run "$PY" tools/bump_version.py "$ver"
  fi
}

case "$SUBCMD" in
  release)    cmd_release ;;
  bundle-dev) cmd_bundle_dev ;;
  dev)        cmd_dev ;;
  worker)     cmd_worker ;;
  check)      cmd_check ;;
  bump)       cmd_bump "$ARG1" ;;
  clean)      cmd_clean ;;
  "")         usage ;;
  *)          die "未知子命令：$SUBCMD（./dev.sh --help）" ;;
esac
