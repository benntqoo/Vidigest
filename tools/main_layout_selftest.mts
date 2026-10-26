// 主界面「状态 → 视图」的判据（`app-ui/src/components/mainLayout.ts`）。
//
// 为什么这个要单独测：这些映射全是**看起来显然、改起来很容易错**的地方，
// 而且错了不会报错，只会让界面说一句错话 ——
//   ・「待抽帧」渲染成「抽帧中」（用户以为在跑，其实在等并发位）
//   ・「没验成」渲染成「GPU 不可用」（用户去查驱动，方向完全错）
//   ・失败的任务硬猜它停在第几段（猜错比不显示更糟）
//
// 跑法：`node --experimental-strip-types tools/main_layout_selftest.mts`
// 已挂进 `./dev.sh check`。**不需要 GPU、不需要 Rust、秒级。**
//
// 协议帧/样式不在这里验 —— 这里只验纯函数。界面观感见
// `docs/主界面版式改版.md` 的截图与 V 判据。

import {
  connView,
  gpuView,
  stageFill,
  stageView,
  summarize,
} from "../app-ui/src/components/mainLayout.ts";
import type { TaskSnapshot, WorkerState } from "../app-ui/src/worker/types.ts";

let pass = 0;
let fail = 0;

function check(name: string, cond: boolean, detail = "") {
  if (cond) {
    pass++;
    console.log(`  ok   ${name}`);
  } else {
    fail++;
    console.log(`  FAIL ${name}${detail ? ` —— ${detail}` : ""}`);
  }
}

function eq(name: string, actual: unknown, expected: unknown) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  check(name, a === e, `期望 ${e}，实际 ${a}`);
}

console.log("阶段指示");

// 1. 还没开始：不给阶段点。给一个「第 0 段」的暗示会让人以为已经开始了。
eq("pending 不给阶段点", stageFill("pending"), null);

// 2-4. 中间态必须映射到**已结束**的那一段，且不能是 active。
//      这是最容易写反的一组：`transcribed` 的名字里带 transcribed，
//      直觉容易写成「转写进行中」。
eq("downloaded → 第 0 段已完成（不是第 1 段在跑）", stageFill("downloaded"), [
  "done",
  "todo",
  "todo",
  "todo",
]);
eq("transcribed → 第 1 段已完成", stageFill("transcribed"), [
  "done",
  "done",
  "todo",
  "todo",
]);
eq("framed → 第 2 段已完成", stageFill("framed"), ["done", "done", "done", "todo"]);

// 5-8. 正在跑的四个状态，当前段必须是 active（界面靠它决定要不要呼吸动效）。
for (const [status, index] of [
  ["downloading", 0],
  ["transcribing", 1],
  ["framing", 2],
  ["summarizing", 3],
] as const) {
  const fill = stageFill(status);
  eq(`${status} → 第 ${index} 段 active`, fill?.[index], "active");
  check(`${status} → 阶段在跑`, stageView(status).running);
}

// 9. 完成：四段全 done（不是「第 3 段 active」——它已经停了）。
eq("done → 四段全完成", stageFill("done"), ["done", "done", "done", "done"]);

// 10-11. 失败/取消：不猜停在哪。返回 null，由调用方改显示 `stage` 文本。
eq("failed 不猜阶段", stageFill("failed"), null);
eq("cancelled 不猜阶段", stageFill("cancelled"), null);

console.log("队列汇总");

function task(id: number, status: TaskSnapshot["status"]): TaskSnapshot {
  return {
    task_id: id,
    url: "u",
    platform: "douyin",
    title: "t",
    duration_sec: 1,
    status,
    stage: "s",
    progress: 0,
    elapsed_sec: 0,
    retry_count: 0,
    out_dir: "",
    error: null,
  };
}

// 12. 中间态算「排队」不算「在跑」—— 界面上「在跑几条」如果把它算进去，
//     用户会看到「3 条在跑」但 CPU 是闲的。
eq(
  "汇总：中间态计入排队，不计入在跑",
  summarize([
    task(1, "downloading"),
    task(2, "transcribed"),
    task(3, "framed"),
    task(4, "done"),
    task(5, "failed"),
    task(6, "pending"),
  ]),
  { total: 6, active: 1, pending: 3, done: 1, failed: 1 },
);

console.log("GPU 三态");

function worker(patch: Partial<WorkerState>): WorkerState {
  return {
    state: "ready",
    detail: "",
    server_version: "0.6.2",
    gpu_usable: true,
    gpu_verified: true,
    concurrency: { n_asr: 2, n_dl: 4, n_frame: 2, n_sum: 1 },
    alive: true,
    restart_count: 0,
    pipeline_state: "idle",
    blocked_reason: null,
    env_detail: "",
    ...patch,
  };
}

// 13. **最重要的一条**：`gpu_verified === null` 是「还没验」，不是「否」。
//     2026-10-24 就是因为把它当 falsy，界面写了「GPU 不可用」，用户去查驱动。
const detecting = gpuView(worker({ gpu_verified: null, gpu_usable: null }));
check("未验证 → 显示「检测中」", detecting.text.includes("检测中"), detecting.text);
check("未验证 → 不出现「不可用」", !detecting.text.includes("不可用"), detecting.text);

// 14. 验了但没验成 + 原因是模型缺失 ⇒ 明确说「模型未安装」且可跳设置。
//     这一条和上一条是两回事：这里能去下载，那里只能等。
const noModel = gpuView(worker({ gpu_usable: null, blocked_reason: "model_missing" }));
eq("模型缺失 → 模型未安装", noModel.text, "模型未安装");
eq("模型缺失 → 可点跳设置", noModel.action, "settings");

// 15. 真的不可用：只有 `gpu_usable === false` 才允许这么说。
eq("确实不可用 → GPU 不可用", gpuView(worker({ gpu_usable: false })).text, "GPU 不可用");

// 16. 重生中：上一代的结论作废，不能继续说「可用」。
const restarting = gpuView(worker({ state: "restarting", gpu_usable: true }));
check("重启中 → 不显示「GPU 可用」", !restarting.text.includes("可用"), restarting.text);

console.log("连接状态");

// 17. 连接 chip 说的是「连接」，不是「worker 怎么了」——后者归 alert 条说。
//     两个位置说同一句话是改版前的毛病（实测出现过两个红圆片都写「worker 已退出」）。
eq("worker 已退出 → 连接中断", connView(worker({ state: "exited" })).text, "连接中断");
eq("worker 起不来 → 未连接", connView(worker({ state: "failed" })).text, "未连接");

console.log("");
if (fail === 0) {
  console.log(`全部通过：${pass}/${pass}`);
  process.exit(0);
} else {
  console.log(`失败 ${fail} 条，通过 ${pass} 条`);
  process.exit(1);
}
