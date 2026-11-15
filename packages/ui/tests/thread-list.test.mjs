// `packages/ui/src/thread-list.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/thread-list.test.mjs
//
// 两条都是实际跑的时候抓到的：
//   ① 左栏多出一条「（没有用户消息） / 0 轮」的空对话；
//   ② 一轮在 A 里跑着，点开另一段，那一屏长出「正在处理」+ 没有框的步骤流。

import assert from "node:assert/strict";
import { liveEventBelongs, visibleThreads, viewKeyOf } from "../src/thread-list.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; return; }
  failed.push(`${label}${note ? " -- " + note : ""}`);
}

// ── ① 名册过滤 ───────────────────────────────────────────────
{
  const list = [
    { storage_id: "a", turn_count: 2, is_current: false },
    { storage_id: "empty", turn_count: 0, is_current: false },   // <- 噪声
    { storage_id: "cur", turn_count: 0, is_current: true },      // <- /clear 之后该看见
  ];
  const got = visibleThreads(list).map((t) => t.storage_id);
  check("0 轮且不是当前 -> 不画", !got.includes("empty"), got.join(","));
  check("当前那一条**照样画**（哪怕是 0 轮）", got.includes("cur"), got.join(","));
  check("有轮次的照画，顺序不变", got[0] === "a", got.join(","));
  check("空列表 / null 不炸", visibleThreads(null).length === 0
        && visibleThreads([]).length === 0);
  check("数组里的空位跳过", visibleThreads([null, undefined]).length === 0);
  check("turn_count 是字符串也能判", visibleThreads([
    { storage_id: "s", turn_count: "3", is_current: false },
  ]).length === 1);
}

// ── ② 直播事件的归属 ─────────────────────────────────────────
{
  check("同一段 -> 画", liveEventBelongs("A", "A") === true);
  check("★★ 不同的两段 -> 不画（这就是那次的现场）",
        liveEventBelongs("B", "A") === false);
  check("不知道这一轮属于哪段（草稿刚发出） -> 照旧画",
        liveEventBelongs("A", "") === true);
  check("没在看某段历史（当前屏） -> 照旧画",
        liveEventBelongs("", "A") === true);
  check("两边都不知道 -> 画", liveEventBelongs("", "") === true);
  check("null/undefined 当空串", liveEventBelongs(null, undefined) === true);
}

// ── ③ 这一屏的身份（第二次复现：切走之后还在「替别人」画）──
{
  check("群聊的身份是 `group:<id>`（不是空串）",
        viewKeyOf({ groupId: "g1" }) === "group:g1");
  check("看历史 -> storage_id", viewKeyOf({ viewingId: "s1" }) === "s1");
  check("草稿 -> `draft`（**不是空串** —— 空串=不知道）",
        viewKeyOf({ draft: true }) === "draft");
  check("活跃态 -> 当前段 id", viewKeyOf({ currentId: "cur" }) === "cur");
  check("读不到 -> 空串（照旧画：宁可多画，别让正常直播消失）",
        viewKeyOf({}) === "");
  check("群优先于其它几格（群里不会同时看历史）",
        viewKeyOf({ groupId: "g1", viewingId: "s1", draft: true }) === "group:g1");

  //:那次的现场：在 A 群跑着、切到 B 群 / 草稿 —— 都不属于这一屏。
  check("★★ A 群在跑、切到 B 群 -> 不画",
        liveEventBelongs(viewKeyOf({ groupId: "b" }),
                         viewKeyOf({ groupId: "a" })) === false);
  check("★★ A 群在跑、切到草稿 -> 不画（旧判据这里恒真）",
        liveEventBelongs(viewKeyOf({ draft: true }),
                         viewKeyOf({ groupId: "a" })) === false);
  check("A 群在跑、切到某段历史 -> 不画",
        liveEventBelongs(viewKeyOf({ viewingId: "s9" }),
                         viewKeyOf({ groupId: "a" })) === false);
  check("还在 A 群 -> 照旧画", liveEventBelongs(viewKeyOf({ groupId: "a" }),
                                                viewKeyOf({ groupId: "a" })) === true);
}

// ── 结果 ─────────────────────────────────────────────────────
if (failed.length) {
  for (const f of failed) console.error("  [FAIL] " + f);
  console.error(`${pass} passed, ${failed.length} failed`);
  process.exit(1);
}
console.log(`${pass} passed, 0 failed  (thread-list)`);
