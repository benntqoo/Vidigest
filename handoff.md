# Vidnote — 交接文档

> **最后更新**：2026-10-10（进度复核：补记 10-08 那次构建 + 修正产物路径与计数）
> ~~2026-10-06（应用改名 `app-ui` → `Vidnote` 之后）~~
> **本文定位**：**当前状态 + 下一步该做什么**。产品目标与需求见 `docs/PRD.md`，
> 实现细节与实测证据见 `PLAN.md`、`docs/实测记录.md`、`docs/打包与分发.md`、`docs/阶段3_*.md`。
> **改代码之前先读 `docs/工程约定.md`** —— 那里是 60+ 条踩过的坑（每条都能省掉一次重复排查）。

---

## 0. 30 秒上手

```bash
cd D:/Code/SideProject/Vidnote

./dev.sh check      # 协议层 19 + 调度层 66 + 下载层 7 + cargo test 20 + tsc —— 全部不用 GPU，秒~分钟级
./dev.sh dev        # 开发模式起 GUI（需要 vite；首次先 cd app-ui && npm install）
./dev.sh release    # 一条命令出 MSI（约 5 分钟，含 PyInstaller 复用 + Tauri 编译 + WiX）
./dev.sh --help     # 其余子命令：bundle-dev / worker / clean
```

⚠️ **在 AI 工具的沙箱里跑不起来 GUI**，这不是代码问题：沙箱注入的 `tsbx.dll` 拦截命名管道
「客户端读方向」，任何「spawn 子进程并 pipe stdin」的程序都必然 `os error 231`。
判定命令 `python tools/probe_spawn_env.py`。**手动验收必须在普通终端里做**（开始菜单 →
Windows Terminal / PowerShell）。详见 `docs/工程约定.md` 的 231 条目。

---

## 1. 当前进度总览

| PLAN §8 阶段 | 内容 | 状态 | 一句话判据 |
|---|---|---|---|
| — | 链路验证 + 环境实测 | ✅ | `docs/实测记录.md`；GPU 转写 **8.7× 实时** |
| 1 | 核心流水线（CLI） | ✅ | 产出与基线 `samples/transcript_gpu.txt` **逐字节一致**（662 段） |
| 2 | 并发调度 + 断点续跑 | ✅ | `tools/phase2_acceptance.py all` **23/23**（真跑 GPU） |
| 3 | GUI（Rust + Tauri + Python sidecar） | 🟡 **闭环已通，余两项** | 握手/事件/渲染/故障态均已确认；剩**托盘**与**沙箱外渲染复验** |
| 4 | cookie 自动化 + 汇总模块 | ❌ **未开始** | 配置项已留（`cookie.*` / `summarize.*`），无实现 |
| 5 | 打包（PyInstaller + Tauri 两层） | 🟡 **主体已完成，待干净机验证** | `./dev.sh release` exit 0 → MSI **1.45 GiB** |

**一句话概括**：**功能主体（阶段 1–3）已闭环，最难的打包链路也已提前打通**；
现在卡的不是「写代码」，而是**三个需要人或需要时间的验证**，以及**阶段 4 那一整块功能**。

阶段 5 原本排在最后，实际提前做了 —— 因为它是 `PLAN.md §11` 里风险最高的一条（CUDA DLL 随包分发
× 两层构建链），早做早暴露问题。事实证明这个决定是对的：暴露了 NSIS 不可用、`externalBin` 不支持
单目录两个硬约束。

---

## 2. 已验证的事实（都能指到判据）

| 项 | 实测值 | 命令 / 证据 |
|---|---|---|
| GPU 转写吞吐 | **8.7× 实时**（20 分钟音频 137.7 s） | `PLAN.md` §2.1 / §2.2 |
| CPU 转写吞吐 | 1.44×（同音频 13 分 54 秒） | 同上。**CPU 方案批量场景不可用** |
| 阶段 1 产出一致性 | 662 段，**逐字节**等于基线 | 基线 + 参数快照 `samples/README.md` |
| 调度正确性 | 10 条 + 1 条坏链接：10 done / 1 failed，坏链接不影响其余 | `tools/phase2_acceptance.py all` |
| 断点续跑 | `taskkill /F /T` 强杀后已完成条目的 `status` / `updated_at` / 产出 mtime **三者均未变** | 同上 |
| 流水线重叠 | 系数 **1.10×**（>1.0 即阶段确实重叠），转写峰值并发 **1** | 同上；**1.10× 低是物理事实，不是 bug** |
| IPC 协议 | **19/19** | `python tools/rpc_smoke.py` |
| 调度层自测（假阶段函数） | **66/66**，秒级，不需 GPU | `python tools/dispatcher_selftest.py` |
| 下载层自测（cookie 失败翻译） | **7/7**，纯逻辑不联网 | `python tools/fetcher_selftest.py` |
| Rust 单测 | **20/20** | `cargo test --lib`（`app-ui/src-tauri`） |
| 打包链路 | 28 项冒烟，**含真实推理验证 GPU** | `python tools/build_worker.py` |
| 安装包 | `Vidnote_0.1.0_x64_en-US.msi` **1.45 GiB**，exit 0 | `./dev.sh release`；⚠️ 磁盘上该文件时间戳为 **10-08 21:58**（比本节记录的那次更晚，见 §7 新增的「无提交」行） |
| 无孤儿进程 | 进程树 `vidnote.exe → python.exe(venv 壳) → python.exe`，热重载后旧实例消失 | Job Object 三道回收 |
| 界面渲染 | 每处显示都能指回事件来源、随异步事件更新、鼠标键盘生效 | 真实窗口截图，`docs/阶段3_界面确认.md` |
| 故障态呈现 | 真杀 worker 进程树（不模拟）→ 按钮全禁用 + 退出原因进日志区 | 同上 |

**2026-10-10 复核复跑**（工作区干净、代码无改动的前提下确认数字未漂）：协议层 **19/19**、
调度层 **66/66**、fetcher **7/7** —— 与上表一致。

**改名后的落位（2026-10-06 读 `wix/x64/main.wxs` 核过，不是推断）**：

| 项 | 值 |
|---|---|
| 安装包 / 安装目录 / 快捷方式 / 卸载项 | 全是 **`Vidnote`**（`%ProgramFiles%\Vidnote`） |
| **主程序文件名** | **`vidnote.exe`**（小写）—— Tauri 用 **cargo 包名**，`productName` 不改它 |
| 数据目录 | `%LOCALAPPDATA%\Vidnote` |

---

## 3. 未验证 / 阻塞项（诚实标注，别当成已完成）

按「卡住多少事」排序：

### P0 — 沙箱外的一次人工验证（**唯一解锁项**）

**为什么是 P0**：它一次性解锁 **5 项**复验，且**只能在普通终端做**（沙箱内必然 231）。
约 5–15 分钟。

> **进度（2026-10-10）**：**第 1 项「注入对照」✅ 已通过** —— 沙箱外 `cmd.exe` 跑
> `tools/probe_spawn_env.py`：**29 模块 / 未检出注入 / 管道两方向均 `OK`**，与沙箱内的
> `66 模块 / 有 tsbx.dll / 读方向 231` 形成唯一变量对照（`docs/阶段3_界面确认.md` §7.8）。
> 结论：**231 只在工具进程树内发生，Vidnote 无问题** —— 这条由实验闭合，不再只是推理。
> ⚠️ 但它**不等于 GUI 能起来**：第 4 步 `npm run tauri dev` 才是真验证，**其余 11 项仍未做**。
> **进度更新**：第 4 步已**部分通过** —— 窗口起了、进程树核实为
> `vidnote.exe → python.exe(venv 壳) → python.exe(基础解释器)`；**解释器定位坑已修**（加了两道
> 本机保险，见 §2 与 §7.8），但**状态栏 `已启动：…` 文本仍未抄**、其余 11 项未做。

1. **§7.8 十二步运行清单**（`docs/阶段3_界面确认.md` §7.8）—— 覆盖：自动重启、手动重启按钮、
   关闭确认弹窗、优雅关闭 30 s 无孤儿、行右键菜单、故障态那 2 处修复的渲染、
   **失败/中止后能接续新任务**（2026-10-06 卡死缺陷回归，第 11/12 步）。
   → **本轮 4 处界面改动全部只有代码级推理、零截图证据**，全卡在这一个前置上。
2. **L1**：`cd app-ui` → `set VIDNOTE_BUNDLED=1` → `npm run tauri dev`
   → 判据：日志出现「已启动：打包产物：仓库内的 PyInstaller 产物」（**只有这一行能证明走的是
   sidecar 而不是 venv**，两者界面看起来一样）。
3. **L2**：装 MSI，验安装目录 / `worker/` 落位 / 快捷方式 / 卸载项。
4. **`docs/打包与分发.md` §6.2 的 6 条判据** —— 含「干净机器」（本机有 Python 也有 Rust，
   `locate_bundled()` 可能回落到其它候选 ⇒ **同机模拟不算数**）。

### P1 — 需要时间或需要新环境

| 项 | 为什么没做 | 判据 |
|---|---|---|
| **100 条 × 20 分钟长跑** | 要 4 小时以上 | 内存护栏（低于 2 GB 停取新任务）在**真实负载**下是否触发、恢复是否平滑 —— 自测脚本用人为压低阈值验过逻辑，**但那不是真实负载** |
| 批量性能验收 | 阶段 2 只测了 **10 条 × 60 秒** | `PLAN.md` §10 要求「10 条 20 分钟的总耗时 ≤ 单条 × 10 的 60%」——**未按原判据验证过**，60 秒片段的 1.10× 重叠系数不能外推 |
| 内存峰值 ≤ 可用内存 80% | 同上 | `PLAN.md` §10 |
| 心跳在**转写进行中**是否仍通 | **刻意不测就不敢改** | 只测过空闲态往返（0 ms）。若转写阻塞主循环，UI 会假死 —— 而这关系到**心跳判死分支要不要杀进程**（`PLAN.md` §11.7） |
| **`tools/refresh_cookies.py` 首跑** | 本机没装 playwright-cli，`open`→`state-save` 时序按 PLAN §9.5 的记录实现，未实测 | `npm i -g @playwright/cli` → `python tools/refresh_cookies.py` → 产出含 `ttwid` 的 cookies.txt → 配 `paths.cookies_file` → 抖音链接下载成功 |

### P2 — 验收清单上还没实现的功能

| 项 | 现状 |
|---|---|
| **磁盘余量护栏**（低于 5 GB 停止取新任务并告警） | ❌ **未实现**，目前只有内存护栏。`env_probe.py` 只做探测/上报，没有调度侧拦截 |
| **cookie 失效可操作提示** | 🟡 **2026-10-10 已做失败翻译**：抖音缺/过期 cookie → `CookieExpired`，错误首行即动作（PRD D6）；预检在 `find_yt_dlp` 之前。**自动重取仍属阶段 4**。过渡脚本 `tools/refresh_cookies.py` **未验证**（本机没装 playwright-cli，`open`/`state-save` 序列按 PLAN §9.5 记录实现）—— 见 §3 P1 |
| **GPU 被占满时降级到 CPU 或等待** | ❌ 未验。`verify_cuda` 会如实报不可用，但没有「显存不足 → 降级/排队」的策略 |
| **ffmpeg 缺失启动即报错并给安装命令** | 🟡 部分。`ffmpeg_locator.py` 已在**定位时**校验「非空 + `PE` 头」，但「启动即报错」的行为未专门验 |
| 「粘贴抖音原始分享文案（含干扰码）」的独立判据 | 🟡 `app/core/urls.py` 实现完整（多平台 + 短链 + 长链），但**没有独立自测**，只在验收脚本里被间接用到 |

---

## 4. 接下来要做的事（按顺序）

### 第 1 步：沙箱外验证（人工，约 15 分钟）—— 先做这个

按 `docs/阶段3_界面确认.md` §7.8 → §7.9 依次跑：**结果填 §7.8 的「结果记录」表，截图回填 §六。**
这一步不做，后面每一项界面改动都还在「无证据」状态里叠加。

> 2026-10-10 已把 12 步清单 + 进程树基线命令 + 收尾模板写进 §7.8，照抄即可。
> ✅ **解释器已配好且已实测定案（2026-10-10）**：原先开发期无人设 `VIDNOTE_PYTHON`，会落到 PATH
> 兜底、命中 `C:\Python313\python.exe`（无 faster-whisper）⇒ worker 必挂。现已加**两道本机保险**
> （都在 `.gitignore` 内）：① `app-ui/src-tauri/.cargo/config.toml` 的 `[env] VIDNOTE_PYTHON`；
> ② 仓库根 `.venv` junction → 托管 venv。**直接 `npm run tauri dev` 即可**，不必再设变量。
> **实测结果：保险 1 生效** —— 读 `vidnote.exe` 的进程环境变量，确有 `VIDNOTE_PYTHON`、而 PATH 无
> venv，并有 `CARGO_*` 三个变量 ⇒ 确认宿主由 `cargo run` 启动（与 tauri-cli v2 源码一致）。
> 背景与删除注意事项见 `docs/阶段3_界面确认.md` §7.8「第 4 步的解释器陷阱」。
> **本机前置已在沙箱内验完**（不必重做）：`node_modules`/`vite`、`build/pyinstaller/vidnote-worker/`、
> `models/large-v3/`、`cargo test --lib` **20/20**。唯一预料之外的耗时是 `target/debug/` 为空
> ⇒ 第 4 步 `npm run tauri dev` 需先编译 debug 版 Rust（数分钟）。

### 第 2 步：托盘 + 后台继续（阶段 3 收尾）

- 现状：关闭确认弹窗 ✅ 已做，但其中的「后台继续（最小化到托盘）」这一半 ❌ 未做
  （`App.tsx` 里有一行说明文案，别误读成已实现）。
- 做法：给 `tauri` 加 `tray-icon` feature + 新依赖；关闭确认里选「后台继续」→ 隐藏窗口而非退出。
- **为什么排在验证之后**：它**渲染无法在沙箱内验证**，和 §3 的 P0 是同一类阻塞。
  先解锁验证通道，再叠加新的不可验证项，否则同时欠两笔账。

### 第 3 步：阶段 4 —— cookie 自动化 + 汇总模块（**剩下的功能主体**）

这是「功能上还差的那一整块」。配置项早已留在 `config.example.yaml`，但**没有任何实现**：

| 子项 | 配置已留 | 要做的事 |
|---|---|---|
| cookie 自动重取 | `cookie.browser` / `cookie.auto_refresh` |

> **过渡现状（2026-10-10）**：抖音缺/过期 cookie 已有可操作提示 + `tools/refresh_cookies.py`
> 手动刷新；「无 cookie 解析」已实测判死（`PLAN.md` §9.5）——门槛是 Argus 指纹层不是
> cookie 本身，自动化只能走 playwright 路线（即本表的方案）。 调 `playwright-cli` 导出 storage-state → 转 Netscape `cookies.txt`（`--cookies-from-browser` 在 Windows 新版浏览器上必失败，见 `PLAN.md` §9.5）。**验收：手动把 `cookies.txt` 改坏，程序能检测到并自动重新采集** |
| 汇总模块 | `summarize.*`（`base_url` / `api_key_env` / `model` / `template`） | 调 OpenAI 兼容接口生成「摘要 + 章节大纲 + 要点」。`TaskStatus.SUMMARIZING` **枚举值已存在但无实现**。注意 `PLAN.md` §11.4 的未验证项：一条 20 分钟视频转写稿约 8000–10000 字，**是否超上下文、单条成本多少，未测** |

**阶段 4 的两个设计前提**（别推翻）：
- 汇总**默认关闭**（`summarize.enabled: false`）—— 保证离线可用是硬要求。
- 汇总失败**不算任务失败**：转写稿是主产物，汇总是增值项。

### 第 4 步：补齐 P2 那几个护栏

按 `docs/PRD.md` 的「最终验收标准」逐条对；磁盘护栏是最容易实现、收益最直接的一条
（照抄 `dispatcher` 里内存护栏的采样点 `_intake_open()` 的位置即可，**但注意别在那里提前
`return False`** —— 采样会停，见 `docs/工程约定.md`）。

### 第 5 步：清理（不急，但别忘）

- ~~`bundle/msi/app-ui_0.1.0_x64_en-US.msi`（1.45 GiB）—— **已作废**，留着会一直出现在 `dev.sh` 的产物报告里。~~
  → **2026-10-10 实测改写**：作废 MSI 仍在，真实路径是
  **`app-ui/src-tauri/target/release/bundle/msi/app-ui_0.1.0_x64_en-US.msi`**（1.5 GB，时间戳 10-06 04:18）。
  原文写成简写路径 `bundle/msi/` 是错的 —— `BUNDLE_DIR="$REPO/app-ui/src-tauri/target/release/bundle"`（`dev.sh:39`）。
  **该目录共 2.9 GB**（作废 + 现行两个 MSI 各 1.5 GB），清理时别连现行那个一起删。
- `build/pyinstaller/*.old-*` —— ~~已积 5 个目录（15 GB 级）~~ → **2026-10-10 实测 6 个**
  （`20261004×4 + 20261006 + 20261008`，15 GB 级）。`./dev.sh clean --yes` 清。

---

## 5. 关键文件速查

| 想知道 | 去哪 |
|---|---|
| **改代码前必读** | `docs/工程约定.md`（60+ 条硬规则全量清单） |
| 产品目标 / 要做什么 | `docs/PRD.md` |
| 怎么实现 / 实测数据 / 风险清单 | `PLAN.md` |
| 两端通信契约 | `docs/IPC协议规格.md` |
| 打包与安装形态 | `docs/打包与分发.md` |
| 界面确认过程 / 沙箱外清单 | `docs/阶段3_界面确认.md` |
| Rust↔Python 打通的三个阻塞 | `docs/阶段3_打通记录.md` |
| 性能与环境原始数据 | `docs/实测记录.md` |
| 基线参数快照 | `samples/README.md`（**比对失败先查快照，再怀疑代码**） |

**代码地图**

```
app/                  # Python 功能层（纯逻辑，不感知传输方式：不 print / 不 sys.exit / 不读 argv）
├── core/pipeline.py      # 阶段函数 run_download / run_asr / run_frames + 串行组合 run_one
├── core/dispatcher.py    # 三级流水线 + 有界背压 + 内存护栏 + 断点续跑
├── core/store.py         # state.db（SQLite）；url UNIQUE 做跨批次去重
├── core/{fetcher,audio,transcriber,frames,urls,env_probe,cuda_dll}.py
├── cli.py                # 人用入口（阶段 1 验收走这条）
└── rpc.py                # Rust 用入口（JSON Lines over stdio）
app-ui/               # Rust + Tauri 宿主（目录名未随应用改名而改）
├── src-tauri/src/worker/mod.rs   # spawn / 握手 / 事件分发 / Job Object / 崩溃重启 ← 最核心
├── src-tauri/src/commands.rs     # 暴露给前端的命令
└── src/                          # React 19 + TS；components/ + worker/client.ts
tools/                # 验收脚本（见下表）
packaging/            # vidnote-worker.spec + tauri.bundle.json（overlay，刻意不进主配置）
```

---

## 6. 环境前置（换了机器要重做这些）

| 项 | 值 / 注意 |
|---|---|
| Python 解释器 | **必须显式**：`C:/Users/Ben/.workbuddy/binaries/python/envs/default/Scripts/python.exe` —— 托管/系统 3.13 **都没** faster-whisper。`dev.sh` 用「能不能 `import faster_whisper`」挑解释器 |
| Rust 找 Python | `VIDNOTE_PYTHON` → `<repo>/.venv/Scripts/python.exe` → PATH。⚠️ **仓库里没有任何脚本设置它**（2026-10-10 复核）⇒ 原先落到 PATH 兜底、命中 `C:\Python313\`（**无 faster-whisper**）而必挂。**已加两道本机保险**：① `app-ui/src-tauri/.cargo/config.toml` 的 `[env] VIDNOTE_PYTHON`；② 仓库根 `.venv` junction → 托管 venv（都在 `.gitignore` 内，换机器需重建）。**2026-10-10 实测：保险 ① 生效**（`VIDNOTE_PYTHON` 出现在 `vidnote.exe` 进程环境里、PATH 无 venv）+ 「`tauri dev` 走 `cargo run`」已由父进程与源码双证。**打包产物不受影响**（sidecar 自带运行时）。详见 `docs/阶段3_界面确认.md` §7.8 |
| MSVC | ⚠️ 本机 VS 2022 的 MSVC **14.44 缺 `include`**；已用项目级 `.cargo/config.toml`（**已 gitignore、本机专用**）钉死 VS 2019 BuildTools。**删掉它本机编不过任何含 C/C++ 的 crate** |
| 模型 | `models/large-v3/`（2.87 GB，**不入库**，只下一次） |
| ffmpeg | WinGet 装的 `Links/ffmpeg.exe` 是 **0 字节占位**，定位时必须校验「非空 + PE 头」 |
| API Key | 走环境变量 `VIDNOTE_API_KEY`，**不进配置文件** |

---

## 7. 近期变更（便于定位「什么时候变成这样的」）

| 提交 | 内容 |
|---|---|
| `7ff6105` | 文档同步：IPC 规格 v1.2（终态推送语义更正）+ PRD D6/FR-2/FR-9 现状对齐 |
| `9c202fe` | PLAN §9.5 补「无 cookie 解析抖音」实测判死（ttwid 可匿名签发但 Argus 指纹层拦截） |
| `7175712` | 抖音 cookie 失败翻译（D6）+ `tools/refresh_cookies.py` 过渡脚本 + fetcher 自测 7 条 |
| —（**无提交**） | **10-08 21:58 又跑过一次 `./dev.sh release`** —— 产物 `Vidnote_0.1.0_x64_en-US.msi` 与 `vidnote.exe` 时间戳晚于 10-06 那次记录，但**当时既没提交也没写文档**，2026-10-10 复核时补记。含义：当前 MSI 是 10-08 的构建，不是 §2 记录的那一次 |
| `8d9d51f` | 修「批次失败后整批卡死」：终态跃迁补推 `pipeline_state` + abort 恢复链（自测 53→66） |
| `d7193e8` | 应用改名 `app-ui` → `Vidnote`；PRD、工程约定、handoff 入库 |
| `802685d` | 一键构建 `dev.sh` + **NSIS 判死**（改走 MSI） |
| `dff9403` | 打包链路：PyInstaller 单目录 + Tauri `resources`，冒烟 28/28 |
| `cb31b13` | 关闭确认 + 优雅关闭 30 s / 行右键菜单 / 231 可操作提示 |
| `1e69b3c` | 崩溃自动重启 + 手动恢复入口 |
| `f99abea` | **更正 231 真因**：不是 Windows 故障，是注入 DLL 拦命名管道读方向 |
| `3011277` | 阶段 2 完成：并发调度 + 断点续跑（23/23） |
| `42aa858` | 阶段 3 打通：Tauri 宿主 + Python sidecar 端到端握手 |

> 详细提交信息 `git log`。**注意**：`PLAN.md` 里多处保留「原文划删 + 补日期」的更正痕迹，
> 那是项目刻意的手法（`docs/工程约定.md`「不要静默覆盖」），**不是没整理干净**。
> 同理，上表 **10-08 那行在 `git log` 里查不到** —— 那次只留下构建产物、没有提交，
> 是 2026-10-10 复核时从文件时间戳发现的。
>
> **仓库状态（2026-10-10 复核）**：`main` 领先 `origin/main` **9 个提交未推**（`origin/main` 停在
> `f99abea`）。全是本地提交 —— 换机器 / 重建仓库前先推。
