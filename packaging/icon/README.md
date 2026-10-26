# 应用图标的唯一真源

```
packaging/icon/app-icon.svg          ← 改这里
        │
        │  cd app-ui && npx tauri icon ../packaging/icon/app-icon.svg
        ▼
app-ui/src-tauri/icons/              ← 全部生成物，不要手改
        32x32.png  128x128.png  128x128@2x.png  64x64.png
        icon.png(512)  icon.ico(16/24/32/48/64/256)  icon.icns
        Square*Logo.png / StoreLogo.png   （WiX 安装包与 Windows 商店用）
```

## 为什么留 SVG 而不是直接放 PNG

`tauri icon` 需要一张 1024 见方的源图，它会把源图缩到 16 / 24 / 32 / 48 / 64 / 128 / 256 / 512
八个尺寸并合成 `.ico`。**用位图当源，等于每次改图标都得先找一次「原图在哪、是不是最新的」**；
留矢量就只有一个文件、可 diff、可复现。

## 重新生成

```bash
cd app-ui
npx tauri icon ../packaging/icon/app-icon.svg
rm -rf src-tauri/icons/android src-tauri/icons/ios   # 本项目只发 Windows，这两个目录是多余的
```

第二条命令不是洁癖：`tauri icon` 会**无条件**生成 `android/` 与 `ios/` 各 30+ 个文件（约 290 KB），
它们不进任何构建产物。删掉之后下次生成还会出现，所以每次生成后都要删。

## 设计约束（改之前请先读）

1. **只有两个元素**：播放三角（视频）+ 三条递减的文字条（转写稿 / 摘要）。
   台架尺寸有 16×16，任何第三个元素都会糊成一团。
2. **文字条全部 100% 白**。第一版给过 `0.85` / `0.7` 的透明度，小尺寸下三条糊成灰蓝，
   看起来像三种不同的颜色。
3. **文字条的 `rx` 给 18，不是 `height/2`**。给满圆角会变成药丸形，读起来像 skeleton 占位条。
4. **三角的圆角靠同色粗描边实现**（`stroke-width` + `stroke-linejoin: round`），
   不是手算圆角路径 —— 手算的路径在 32 px 下尖角会出锯齿。
   注意描边会向外扩 `stroke-width/2`，所以路径尺寸比看到的视觉尺寸小一圈。
5. **必须铺满画布、四角圆角 `rx=232`**：Windows 任务栏与开始菜单按原样显示，
   留白会让图标在一排图标里显得小一号。

## 验收

改完按这三个尺寸各看一眼，任何一个读不出来就是没做好：

| 尺寸 | 判据 |
|---|---|
| 16×16 | 只能看到「一块蓝色 + 白色马克」——认不出细节是正常的，**但必须能与其他图标区分** |
| 32×32 | 能看出是「播放 + 三条线」 |
| 256×256 | 三条文字条间距均匀、圆角方向一致、蓝色渐变无接缝 |

对照图：`docs/images/readme/icon-sizes.png`（浅色/深色背景各一行）。
