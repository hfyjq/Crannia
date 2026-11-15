// 附件前言（`packages/ui/src/attachment-wire.js`）—— 零依赖，node 直接跑。
//
// 判据是往返：`payloadText` 拼出来的那一段，`parseAttachments` 要能逐字拆回来。
// 这一对分居两个文件、只靠字符串形状对接，所以两端必须由同一个读者钉住
// （否则改一处，另一处只是安静地少一截）。
//
// 最重要的一条：内联文本的正文必须真的在消息里 —— 前言承诺了内容，
// 而请求体只有 `text` 与 `images` 两个字段。
//
// 输出一律 ASCII（GBK 控制台；一 print 中文就崩）。

import assert from "node:assert";
import { attachmentPreamble, payloadText } from "../src/attachment-wire.js";
import { parseAttachments } from "../src/attachments.js";

let pass = 0;
const failed = [];
function check(label, cond) {
  if (cond) { pass += 1; console.log("  [ok]   " + label); }
  else { failed.push(label); console.log("  [!!]   " + label); }
}

// ── (1) 没有附件：一个字不加 ─────────────────────────────────────
{
  check("no attachments: empty preamble", attachmentPreamble([]) === "");
  check("no attachments: payload untouched", payloadText("hello", []) === "hello");
}

// ── (2) 内联文本：正文进请求，拆回来还是一模一样 ────────────────
{
  const body = "\u7b2c\u4e00\u884c\n\u7b2c\u4e8c\u884c\n\u7b2c\u4e09\u884c";
  const out = payloadText("\u5e2e\u6211\u770b\u770b", [
    { kind: "text", name: "paste.txt", size: 12, text: body },
  ]);
  check("inline text: the body is in the payload", out.indexOf(body) >= 0);
  const r = parseAttachments(out);
  check("inline text: parsed back as one text attachment",
    r.attachments.length === 1 && r.attachments[0].kind === "text");
  check("inline text: body survives the round trip",
    r.attachments[0].text === body);
  check("inline text: bubble keeps only the user's words",
    r.text === "\u5e2e\u6211\u770b\u770b");
  check("inline text: numbered in the preamble",
    out.indexOf("[\u9644\u4ef61]") === 0);
  //: \u5b57\u6570\u53d6\u6b63\u6587\u957f\u5ea6\u3002`size` \u4f20\u7684\u662f\u9519\u7684\uff08\u6a21\u62df"\u9000\u56de\u5185\u8054"\u90a3\u4e00\u652f\u7684\u5b57\u8282\u6570\uff09\uff0c
  //: \u524d\u8a00\u91cc\u4e0d\u8bb8\u628a\u5b83\u5f53\u6210\u5b57\u6570\u6284\u4e00\u904d\u3002
  const wrong = payloadText("x", [
    { kind: "text", name: "n", size: 9999, text: "abc" },
  ]);
  check("inline text: count comes from the body, not from size",
    wrong.indexOf("\uff083 \u5b57\uff09") >= 0 && wrong.indexOf("9999") < 0, wrong);
}

// ── (3) 正文里有一行长得像附件：不许被拆走 ──────────────────────
{
  const body = "x\n[\u9644\u4ef62] \u8fd9\u4e00\u884c\u5728\u6b63\u6587\u91cc\n\u5c3e";
  const out = payloadText("\u770b", [{ kind: "text", name: "t", size: 3, text: body }]);
  const r = parseAttachments(out);
  check("body that looks like a preamble stays in the body",
    r.attachments.length === 1 && r.attachments[0].text === body);
}

// ── (4) 两个内联附件：各自一对边界，互不吞并 ────────────────────
{
  const a = "\u7532\u6b63\u6587";
  const b = "\u4e59\u6b63\u6587";
  const out = payloadText("q", [
    { kind: "text", name: "a", size: 3, text: a },
    { kind: "text", name: "b", size: 3, text: b },
  ]);
  const r = parseAttachments(out);
  check("two inline attachments: both parsed",
    r.attachments.length === 2);
  check("two inline attachments: bodies kept apart",
    r.attachments[0].text === a && r.attachments[1].text === b);
  check("two inline attachments: numbered 1 and 2",
    out.indexOf("[\u9644\u4ef61]") >= 0 && out.indexOf("[\u9644\u4ef62]") >= 0);
}

// ── (5) 路径附件：拆回来是 file + path，说明行不进气泡 ──────────
{
  const out = payloadText("\u8bfb\u5b83", [
    { kind: "path", name: "r.pdf", size: 7963, path: "D:\\w\\r.pdf" },
  ]);
  const r = parseAttachments(out);
  check("path attachment: parsed as file with path",
    r.attachments.length === 1 && r.attachments[0].path === "D:\\w\\r.pdf"
    && r.attachments[0].name === "r.pdf");
  check("path attachment: bubble keeps only the user's words",
    r.text === "\u8bfb\u5b83");
}

// ── (6) 空正文：如实说，不留一句会被当成"有内容"的承诺 ──────────
{
  const pre = attachmentPreamble([{ kind: "text", name: "t", size: 0, text: "" }]);
  check("empty body: says so", pre.indexOf("\u5185\u5bb9\u4e3a\u7a7a") >= 0);
  const r = parseAttachments(payloadText("q", [
    { kind: "text", name: "t", size: 0, text: "" },
  ]));
  check("empty body: parsed as text with empty text",
    r.attachments.length === 1 && r.attachments[0].kind === "text"
    && r.attachments[0].text === "");
}

// ── (7) 混合：一个路径 + 一个内联，编号连续 ─────────────────────
{
  const out = payloadText("go", [
    { kind: "path", name: "a.pdf", size: 1, path: "/t/a.pdf" },
    { kind: "text", name: "b.txt", size: 2, text: "\u4e59" },
  ]);
  const r = parseAttachments(out);
  check("mixed: two attachments", r.attachments.length === 2);
  check("mixed: second one keeps its body", r.attachments[1].text === "\u4e59");
  check("mixed: numbering continues", out.indexOf("[\u9644\u4ef62]") >= 0);
}

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
