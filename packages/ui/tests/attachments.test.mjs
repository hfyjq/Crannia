// 用户消息里附件的解析（`packages/ui/src/attachments.js`）—— 零依赖，node 直接跑。
//
// 钉住的是库里那两种真实形状（`core/image_input.content_text` 与
// `Composer.attachmentPreamble` 产出的），一条都不能漏：
//   · 图片指针 `[图片 1 —— 原图在 data/attachments/<16hex>.png]`
//   · 文件前言 `[附件] 名字（123 字节）：<路径>` + 紧跟一行两空格的说明
// 漏一种 = 那句话原样漏进气泡（用户看到的是"图/文件被当成了正文"）。
//
// 输出一律 ASCII（GBK 控制台；一 print 中文就崩）。

import assert from "node:assert";
import { parseAttachments } from "../src/attachments.js";

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

// ── ① 纯文本，一个字节不动 ─────────────────────────────────────────
{
  const r = parseAttachments("hello\nworld");
  check("plain text untouched", r.text === "hello\nworld" && r.attachments.length === 0);
}

// ── ② 图片指针，一个 image（id = 文件名）────────────────────────────
{
  const src = "look at this\n[\u56fe\u7247 1 \u2014\u2014 \u539f\u56fe\u5728 data/attachments/0123456789abcdef.png]";
  const r = parseAttachments(src);
  check("image pointer parsed", r.attachments.length === 1
    && r.attachments[0].kind === "image"
    && r.attachments[0].id === "0123456789abcdef.png");
  check("image pointer removed from text", r.text === "look at this");
}

// ── ③ 没有路径的图片，不冒充图片（取不回来）────────────────────
{
  const r = parseAttachments("[\u56fe\u7247 2]");
  check("bare image marker degrades to a file chip",
    r.attachments.length === 1 && r.attachments[0].kind === "file");
  check("bare image marker removed from text", r.text === "");
}

// ── ④ 文件前言（含紧跟的两空格说明），一个 file；说明行不进正文 ──────
{
  const src = [
    "[\u9644\u4ef6] report.pdf\uff087963  \u5b57\u8282\uff09\uff1aD:\\work\\report.pdf",
    "  \u8fd9\u4e2a\u6587\u4ef6\u4ee5**\u8def\u5f84**\u5f62\u5f0f\u7ed9\u4f60 \u2014\u2014 \u9700\u8981\u5185\u5bb9\u65f6\u7528\u4f60\u7684\u6587\u4ef6\u5de5\u5177\u53bb\u8bfb\u3002",
    "",
    "please read it",
  ].join("\n");
  const r = parseAttachments(src);
  check("file preamble parsed", r.attachments.length === 1
    && r.attachments[0].kind === "file"
    && r.attachments[0].name === "report.pdf"
    && r.attachments[0].size === 7963
    && r.attachments[0].path === "D:\\work\\report.pdf");
  check("continuation line does not leak into the bubble", r.text === "please read it");
}

// ── ⑤ 混合：文件在前、图片在后（顺序就是库里那两条的先后）───────────
{
  const src = [
    "[\u9644\u4ef6] a.docx\uff0812 \u5b57\u8282\uff09\uff1a/tmp/a.docx",
    "  why",
    "[\u56fe\u7247 1 \u2014\u2014 \u539f\u56fe\u5728 data/attachments/aaaaaaaaaaaaaaaa.jpg]",
    "",
    "\u770b\u8fd9\u4e2a",
  ].join("\n");
  const r = parseAttachments(src);
  check("mixed order kept", r.attachments.length === 2
    && r.attachments[0].kind === "file" && r.attachments[1].kind === "image");
  check("mixed text is only the user's words", r.text === "\u770b\u8fd9\u4e2a");
}

// ── ⑥ 路径不是产物 id（用户手工写/旧数据），文件条目，不当图片 ───────
{
  const r = parseAttachments("[\u56fe\u7247 3 \u2014\u2014 \u539f\u56fe\u5728 C:\\tmp\\shot.png]");
  check("non-artifact path never becomes an image",
    r.attachments.length === 1 && r.attachments[0].kind === "file");
}

// ── ⑦ 只有附件、没有正文，text 是空串（气泡那边据此不画空气泡）────
{
  const r = parseAttachments("[\u9644\u4ef6] x.zip\uff081 \u5b57\u8282\uff09\uff1a/tmp/x.zip\n  why");
  check("attachment-only message has empty text", r.text === "");
}

// ── ⑧ 带编号的前言（`[附件1]`）── 编号不进名字，其余与老形状一致 ────
{
  const src = [
    "[\u9644\u4ef61] report.pdf\uff087963 \u5b57\u8282\uff09\uff1aD:\\work\\report.pdf",
    "  \u8fd9\u4e2a\u6587\u4ef6\u4ee5**\u8def\u5f84**\u5f62\u5f0f\u7ed9\u4f60",
    "",
    "please read it",
  ].join("\n");
  const r = parseAttachments(src);
  check("numbered preamble parsed",
    r.attachments.length === 1 && r.attachments[0].name === "report.pdf"
    && r.attachments[0].path === "D:\\work\\report.pdf");
  check("numbered preamble: number not in the name",
    r.attachments[0].name.indexOf("1]") < 0);
  check("numbered preamble: body is the user's words", r.text === "please read it");
}

// ── ⑨ 内联文本：正文夹在边界行里，整段不进气泡 ──────────────────
{
  const src = [
    "[\u9644\u4ef61] \u7c98\u8d34\u7684\u6587\u672c\uff082018 \u5b57\uff09\uff082018 \u5b57\uff09\uff1a"
      + "\u5185\u5bb9\u5982\u4e0b\uff08\u672a\u4ee5\u8def\u5f84\u5f62\u5f0f\u63d0\u4f9b\uff09\u3002",
    "[\u9644\u4ef6\u6b63\u6587#a1b2 \u5f00\u59cb]",
    "\u7b2c\u4e00\u884c",
    "[\u9644\u4ef6 2] \u8fd9\u4e00\u884c\u5728\u6b63\u6587\u91cc\uff0c\u4e0d\u662f\u9644\u4ef6",
    "\u7b2c\u4e8c\u884c",
    "[\u9644\u4ef6\u6b63\u6587#a1b2 \u7ed3\u675f]",
    "",
    "\u5e2e\u6211\u770b\u770b",
  ].join("\n");
  const r = parseAttachments(src);
  check("inline text parsed as one attachment",
    r.attachments.length === 1 && r.attachments[0].kind === "text");
  check("inline text keeps every line of the body",
    r.attachments[0].text === "\u7b2c\u4e00\u884c\n"
      + "[\u9644\u4ef6 2] \u8fd9\u4e00\u884c\u5728\u6b63\u6587\u91cc\uff0c\u4e0d\u662f\u9644\u4ef6\n"
      + "\u7b2c\u4e8c\u884c");
  check("inline text: the body does not leak into the bubble",
    r.text === "\u5e2e\u6211\u770b\u770b");
  check("inline text keeps the character count",
    r.attachments[0].size === 2018);
}

// ── ⑩ 更早的内联前言（没有正文）：如实记成空串，不冒充有内容 ────
{
  const src = "[\u9644\u4ef6] x.txt\uff0812 \u5b57\u8282\uff09\uff1a"
    + "\u5185\u5bb9\u5df2\u968f\u672c\u6d88\u606f\u7ed9\u51fa\u3002\n\n\u770b\u8fd9\u4e2a";
  const r = parseAttachments(src);
  check("legacy inline preamble: text kind, empty body",
    r.attachments.length === 1 && r.attachments[0].kind === "text"
    && r.attachments[0].text === "");
  check("legacy inline preamble: bubble keeps only the user's words",
    r.text === "\u770b\u8fd9\u4e2a");
}

// ── ⑪ 没有闭合边界的正文：不吞掉后面的用户正文，也不崩 ──────────
{
  const src = [
    "[\u9644\u4ef61] t\uff081 \u5b57\uff09\uff1a\u5185\u5bb9\u5982\u4e0b",
    "[\u9644\u4ef6\u6b63\u6587#zz \u5f00\u59cb]",
    "\u6ca1\u6709\u7ed3\u675f\u884c",
  ].join("\n");
  const r = parseAttachments(src);
  check("unterminated body: no crash, attachment still parsed",
    r.attachments.length === 1 && r.attachments[0].kind === "text");
}

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
