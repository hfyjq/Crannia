// `packages/ui/src/format.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/format.test.mjs
//
// Python 那一侧由 `tests/test_desktop_format.py` 接进全量 runner。
//
// 判据（用户）：「超过 1 分钟后就改成 `1m23s` 这种格式，小时同理。
//    token 也是，改成 `1.1k`、`1.1m` 这种格式。」
//    所以下面每一条边界都钉了两个点（刚好在界内 / 刚好跨过去）——
//    因为这类函数的 bug 永远长在边界上，而不是在中间。

import assert from "node:assert/strict";
import { fmtCount, fmtDuration, fmtStepMs, retryCounter, retryLabel } from "../src/format.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (got === want) { pass++; return; }
  failed.push(`${label}: 得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`);
}

// ── 时长：越读越短（界内 / 跨界 成对钉）──────────────────────────
check("0.75 秒仍是两位小数（第一眼不能是 0.8）", fmtDuration(0.75), "0.75s");
check("9.99 秒还是秒档", fmtDuration(9.99), "9.99s");
check("10 秒跨过去 ⇒ 一位小数", fmtDuration(10), "10.0s");
check("59.9 秒还没到一分钟", fmtDuration(59.9), "59.9s");
check("★ 60 秒 ⇒ 换单位", fmtDuration(60), "1m0s");
check("★ 现场那一轮（36 步 · 2m49s）", fmtDuration(169), "2m49s");
check("3599 秒仍在分钟档", fmtDuration(3599), "59m59s");
check("★ 3600 秒 ⇒ 换到小时档", fmtDuration(3600), "1h0m");
check("★ 小时档**不再给秒**（3930s = 65m30s）", fmtDuration(3930), "1h5m");
check("负 / NaN ⇒ 空串（不是 `NaNs`）", fmtDuration(NaN), "");

// ── 计数：`1.1k` / `1.1m` ─────────────────────────────────────────
check("一千以下**原样给**（精确值比 0.98k 好读）", fmtCount(980), "980");
check("999 还在下面", fmtCount(999), "999");
check("★ 1000 ⇒ 换单位", fmtCount(1000), "1k");
check("★ `.0` 去掉（不是 1.0k）", fmtCount(1100), "1.1k");
check("五位数", fmtCount(12345), "12.3k");
check("★★ 999999 ⇒ **不是** `1000k`，是 `1m`（换档判据看四舍五入后的值）", fmtCount(999999), "1m");
check("★ 1e6 ⇒ 换到 m", fmtCount(1000000), "1m");
check("★ 现场那种量级", fmtCount(1100000), "1.1m");
check("负 / NaN ⇒ 空串", fmtCount(NaN), "");


// ── 步骤流里一步的用时（用户）─────────────────────
// 「大于 1s 的时间就用秒为单位，不要再用 ms 了」
check("**一步** < 1s 仍是毫秒（把「快」这件事留着）", fmtStepMs(55), "55ms");
check("边界：999ms 还是毫秒", fmtStepMs(999), "999ms");
check("★★ 边界：1000ms 换秒", fmtStepMs(1000), "1.00s");
check("现场那种 8574ms ⇒ 8.6s", fmtStepMs(8574), "8.57s");
check("一分钟以上仍走 `1m23s`", fmtStepMs(83000), "1m23s");
check("没给 / 坏值 ⇒ 空串（不是 0ms）", fmtStepMs(null), "");
check("负值 ⇒ 空串", fmtStepMs(-1), "");


// ── 连接重试的横幅文案 ─────────────────────────
check("连接：用户给的原话逐字", retryLabel("connection"),
      "网络连接异常--请检查宽带互联网连接");
check("超时：单独一句", retryLabel("timeout"), "网络连接超时——请检查网络或代理");
check("认不出的类别不装懂（中性一句）", retryLabel("weird"),
      "模型服务连接异常——正在尝试重新连接");
check("空类别也走中性", retryLabel(null), "模型服务连接异常——正在尝试重新连接");
check("计数：1/10", retryCounter(1, 10), "正在重试 1/10");
check("计数：夹到 max（11/10 -> 10/10）", retryCounter(11, 10), "正在重试 10/10");
check("坏值不猜（null / 0 / 非整数）", retryCounter(null, 10), "");
check("坏值不猜（max 坏）", retryCounter(1, null), "");

console.log(`format.js 自检：${pass} 通过 / ${failed.length} 失败`);
for (const f of failed) console.log("  FAIL " + f);
process.exit(failed.length ? 1 : 0);
