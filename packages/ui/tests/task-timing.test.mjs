// `packages/ui/src/task-timing.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/task-timing.test.mjs
//
// Python 那一侧由 `tests/test_desktop_task_timing.py` 接进全量 runner。
//
// 判据（用户 2026-11-24）：后台任务 / 子代理两个面板「点开就不更新计时」。
// 服务端 `elapsed_ms` 是拉取那一刻的读数，所以这里钉两件事：
// 面板开着时按本地时间继续走，以及不该走的时候（没有基准、已经结束、
// 时钟倒退）一个毫秒都不许凭空加。

import { liveElapsedMs } from "../src/task-timing.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (got === want) { pass++; return; }
  failed.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}

const RUNNING = { elapsed_ms: 2000, status: "running" };

// ── 本地推进（面板开着的常态）──────────────────────────────────
check("响应到达那一刻：与服务端读数一致", liveElapsedMs(RUNNING, 1000000, 1000000), 2000);
check("过了 1 秒：2s 变 3s", liveElapsedMs(RUNNING, 1000000, 1001000), 3000);
check("过了 3.4 秒：2s 变 5.4s", liveElapsedMs(RUNNING, 1000000, 1003400), 5400);
check("开很久也一样走（10 分钟）", liveElapsedMs(RUNNING, 1000000, 1600000), 602000);

// ── 不该伪造的几种 ────────────────────────────────────────────
check("没有基准：只给服务端读数，不猜", liveElapsedMs(RUNNING, 0, 1005000), 2000);
check("没有基准（undefined）：同上", liveElapsedMs(RUNNING, undefined, 1005000), 2000);
check("已经结束的任务不再走秒", liveElapsedMs({ elapsed_ms: 4200, status: "done" }, 1000000, 1009000), 4200);
check("被取消 / 失败同理", liveElapsedMs({ elapsed_ms: 800, status: "killed" }, 1000000, 1009000), 800);
check("时钟往回跳：不出现负增长", liveElapsedMs(RUNNING, 1000000, 999000), 2000);
check("缺 elapsed_ms：从 0 起走", liveElapsedMs({ status: "running" }, 1000000, 1002000), 2000);
check("坏条目不当成崩溃", liveElapsedMs(null, 1, 2), 0);

if (failed.length) {
  console.error(`FAILED ${failed.length}:`);
  for (const f of failed) console.error("  - " + f);
  process.exit(1);
}
console.log(`${pass} checks passed`);
