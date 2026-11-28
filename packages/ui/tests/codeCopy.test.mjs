// 代码块复制按钮的文案决策（纯逻辑，node 直接跑，不碰 JSX）。
//
// 钉三件事：
//   ① 三种状态的文案各不相同 —— 失败不能长得像成功；
//   ② 失败时给退路提示，成功/初始不给；
//   ③ 回落时长与消息级复制按钮一致。
//
// 零依赖，输出 ASCII（Windows 控制台是 GBK）。

import assert from "node:assert";
import { setPack } from "../src/i18n.js";
import {
  codeCopyLabel, codeCopyTitle, CODE_COPY_RESET_MS,
} from "../src/codeCopy.js";

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

// 中文原文即键，空包时回落原文 —— 断言的就是键本身。
setPack("zh", {});

console.log("=" .repeat(62));
console.log("Code copy labels / fallback hint / reset timing");
console.log("=".repeat(62));

check("idle label is the copy-code prompt", codeCopyLabel("") === "复制代码");
check("success label is distinct", codeCopyLabel("ok") === "已复制");
check("failure label is distinct", codeCopyLabel("fail") === "复制失败");
check("three labels are all different",
      new Set([codeCopyLabel(""), codeCopyLabel("ok"),
               codeCopyLabel("fail")]).size === 3);
check("unknown state falls back to idle",
      codeCopyLabel("whatever") === "复制代码");

check("idle title is the same prompt", codeCopyTitle("") === "复制代码");
check("success title is the same prompt", codeCopyTitle("ok") === "复制代码");
check("failure title offers a way out",
      codeCopyTitle("fail") === "换个方式：复制整条回复后手动取用");
check("failure hint is not the idle title",
      codeCopyTitle("fail") !== codeCopyTitle(""));

check("reset matches the message-level copy button", CODE_COPY_RESET_MS === 1600);

console.log("=".repeat(62));
if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
if (failed.length) process.exitCode = 1;
