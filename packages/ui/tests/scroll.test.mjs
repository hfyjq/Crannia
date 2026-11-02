// `packages/ui/src/scroll.js` 的读者 —— 零依赖，node 直接跑。
//
// 用户 2026-10-23：「用户主动上下滑动时不要再把他强行拉回底部，除非他自己滑回底部」。
// 这条判据有两个会静默坏掉的边界：
//   ① 贴底时浏览器给出的是 1~2px 而不是 0，写成 `=== 0` 就永远不跟随；
//   ② 内容比视口短（没得滚），必须算"贴着底"，否则短对话里自动跟随整个失效。

import assert from "node:assert";
import { STICK_PX, isNearBottom, nextStick } from "../src/scroll.js";

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

const box = (scrollTop, scrollHeight, clientHeight) => ({ scrollTop, scrollHeight, clientHeight });

check("贴底（gap = 0）", isNearBottom(box(420, 1000, 580)));
check("★ 贴底时的 1~2px 毛刺也算贴底（写成 === 0 会让跟随彻底失灵）",
  isNearBottom(box(419, 1000, 580)));
check("容差内（gap = STICK_PX）", isNearBottom(box(1000 - 580 - STICK_PX, 1000, 580)));
check("刚好超出容差 ⇒ 不跟", !isNearBottom(box(1000 - 580 - STICK_PX - 1, 1000, 580)));
check("★ 内容比视口短（没得滚）⇒ 贴着底", isNearBottom(box(0, 400, 580)));
check("空 / 没挂上 ⇒ 当成贴着底（宁可跟随）", isNearBottom(null));
check("坏值不炸、按贴着底处理", isNearBottom({ scrollTop: "x" }));

// ── 内容变高不许被当成"用户滑走了"（据反馈）──────
const st = (stick, lastTop) => ({ stick, lastTop });
check("内容变高（scrollTop 没变、离底变远）⇒ 继续跟随",
  nextStick(st(true, 420), box(420, 2000, 580)).stick === true);
check("用户往上滑（scrollTop 变小且离底远）⇒ 停",
  nextStick(st(true, 420), box(100, 1000, 580)).stick === false);
check("往上滑一点、但还在容差内 ⇒ 继续跟随",
  nextStick(st(true, 420), box(400, 1000, 580)).stick === true);
check("滚回底部 ⇒ 恢复跟随",
  nextStick(st(false, 100), box(420, 1000, 580)).stick === true);
check("程序化滚动（top 变大）不改变跟随状态",
  nextStick(st(false, 100), box(300, 1000, 580)).stick === false);
check("停住时也别把 lastTop 丢掉", nextStick(st(true, 420), box(420, 2000, 580)).lastTop === 420);

console.log(`scroll.js 自检：${pass} 通过 / ${failed.length} 失败`);
for (const f of failed) console.log("  FAIL " + f);
assert.equal(failed.length, 0);
