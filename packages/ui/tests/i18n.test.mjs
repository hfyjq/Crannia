// 前端 i18n 运行时（`packages/ui/src/i18n.js`）的逐条断言 —— 零依赖，node 直接跑。
//
// 钉住三件事：
//   ① 查表 / 查不到回落原文（永不显示裸键）；
//   ② 首尾空白先摘掉再查（与后端 `core/i18n.tr_fmt` 同一条）——
//      `t(" · 下一次 {at}")` 这种拿空格当分隔符的键，少了这步永远查不到；
//   ③ 占位符按名字替换，缺值原样留着。
//
// 输出一律 ASCII（Windows 控制台是 GBK，中文一 print 就崩，
//    而"崩掉的输出看起来像测试失败"）。

import assert from "node:assert";
import { setPack, t } from "../src/i18n.js";

let pass = 0;
const failed = [];

function check(label, cond) {
  if (cond) {
    pass += 1;
    console.log("  [ok]   " + label);
  } else {
    failed.push(label);
    console.log("  [!!]   " + label);
  }
}

// ── ① 没有包，原文 ─────────────────────────────────────────────────
setPack("zh", {});
check("no pack => source text", t("任务") === "任务");
check("empty input stays empty", t("") === "");

// ── ② 有包，译文 ───────────────────────────────────────────────────
setPack("en", {
  "任务": "Tasks",
  // 表里的键是归一过的（扫描器按行 strip）—— 键本身不许带空格。
  "· 下一次 {at}": "· next {at}",
  "右键可删除": "Right-click to delete",
});
check("hit => translation", t("任务") === "Tasks");

// ── ③ 首尾空白：摘掉查、原样放回 ──────────────────────────────
// 调用点写的是 " · 下一次 {at}"（前导空格，中文排版要用），
// 表里是 "· 下一次 {at}"。两边不一致是本仓库 2026-10-23 那一整族 bug 的形状。
check("leading space is preserved, lookup is stripped",
  t(" · 下一次 {at}", { at: "08:30" }) === " · next 08:30");
// U+3000（全角空格）也是空白 —— `"　右键可删除"` 那一族。
check("ideographic space is whitespace too",
  t("　右键可删除") === "　Right-click to delete");
// 查不到，原文，连空白一起
check("miss => original, whitespace included", t(" 没有这一句") === " 没有这一句");

// ── ④ 占位符按名字 ─────────────────────────────────────────────────
setPack("en", { "共 {n} 条": "{n} items" });
check("placeholder filled by name", t("共 {n} 条", { n: 3 }) === "3 items");
check("missing value leaves the placeholder",
  t("共 {n} 条", {}) === "{n} items");

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
