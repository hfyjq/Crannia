// `error` 那一族事件的分类（`packages/ui/src/api.js::classifyErrorFrame`）。
//
// 名字 `error` 被两件事共用，而后果完全相反：
//    ① 浏览器原生的连接错误 / 后端 `{"error":"too many streams"}`
//       所以流的事，这一轮没错（不许去收口活动块、不许弹"这一轮出错了"）；
//    ② 后端命名事件 `event: error`（带 `message`），这一轮出错了。
// 混成一类时的现场：换语言后步骤流整个不见了 ——
//    每一次重连都被当成"这一轮出错"，活动块被就地收口。
//
// 输出一律 ASCII（GBK 控制台）。

import assert from "node:assert";
import { classifyErrorFrame } from "../src/api.js";

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

// ── ① 原生（没有 data）────────────────────────────────────────────
check("native error (no data) => stream", classifyErrorFrame({}) === "stream");
check("native error (null ev) => stream", classifyErrorFrame(null) === "stream");

// ── ② 后端 too-many-streams ───────────────────────────────────────
check('{"error":"too many streams"} => stream',
  classifyErrorFrame({ data: '{"error":"too many streams"}' }) === "stream");
// `to_sse` 的信封形状（data 在 env.data 里）也要认
check("envelope shape with error => stream",
  classifyErrorFrame({ data: '{"type":"error","ts":1,"data":{"error":"too many streams"}}' }) === "stream");

// ── ③ 真正的这一轮出错 ───────────────────────────────────────────
check("turn error with message => event",
  classifyErrorFrame({ data: '{"type":"error","ts":1,"data":{"message":"upstream 400"}}' }) === "event");
check("envelope with message => event",
  classifyErrorFrame({ data: '{"message":"upstream 400"}' }) === "event");

// ── ④ 坏 JSON 也按"流"处理（别把解析失败当成一次报错）──────────────
check("bad JSON => stream", classifyErrorFrame({ data: "{not json" }) === "stream");

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
