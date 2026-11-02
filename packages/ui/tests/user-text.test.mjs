// 用户消息的界面视图（`G29`，2026-10-17）—— 附件前言只给模型看。
// 运行：node packages/ui/tests/user-text.test.mjs
import assert from "node:assert";
import { userTextForDisplay } from "../src/user-text.js";

let pass = 0;
function eq(name, got, want) {
  assert.strictEqual(got, want, name + ": got " + JSON.stringify(got));
  pass++;
}

eq("path attachment is stripped",
  userTextForDisplay(
    "[附件] a.pdf（12 字节）：D:/w/a.pdf\n" +
    "  这个文件以**路径**形式给你 —— 需要内容时用你的文件工具（read_file / glob / grep）去读。\n\n" +
    "看看这个"),
  "看看这个");

eq("inline attachment is stripped",
  userTextForDisplay(
    "[附件] b.txt（3 字节）：内容已随本消息给出（**未**以路径形式提供）。\n\n" +
    "分析一下"),
  "分析一下");

eq("multiple attachments are stripped",
  userTextForDisplay(
    "[附件] a.pdf（1 字节）：a\n  说明 A\n" +
    "[附件] b.pdf（2 字节）：b\n  说明 B\n\n" +
    "都看看"),
  "都看看");

eq("plain text is unchanged", userTextForDisplay("你好"), "你好");
eq("empty becomes attachment placeholder", userTextForDisplay(""), "（附件）");
eq("attachment-only becomes placeholder",
  userTextForDisplay("[附件] a.pdf（1 字节）：a\n  说明\n\n"), "（附件）");

console.log("user-text: " + pass + "/" + pass + " passed");
